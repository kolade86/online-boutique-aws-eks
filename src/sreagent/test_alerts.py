"""Tests for alert identity, which alerts are worth investigating, and deduplication."""

import json
import os
import re
import unittest

import alerts
from alerts import AlertKey, Deduplicator


class ServiceOfTest(unittest.TestCase):
    def test_label_precedence_and_fallbacks(self):
        ksm = {"service": "monitoring-kube-state-metrics", "job": "kube-state-metrics",
               "container": "server"}
        cases = [
            # kube-state-metrics alerts: the pod wins over the exporter's service label
            ({**ksm, "pod": "cartservice-6b9f7c8d4-x2k4p"}, "cartservice"),
            ({**ksm, "deployment": "checkoutservice"}, "checkoutservice"),
            ({**ksm, "horizontalpodautoscaler": "adservice"}, "adservice"),
            ({"grpc_service": "hipstershop.CartService"}, "cartservice"),
            ({"pod": "emailservice-6b9f7c8d4-x2k4p"}, "emailservice"),
            ({"pod": "product-catalog-6b9f7c8d4-x2k4p"}, "product-catalog"),
            ({"pod": "redis-0"}, "redis"),
            ({"pod": "fluent-bit-x2k4p"}, "fluent-bit"),
            ({"pod": "cartservice-REPLACE-ME", "container": "server"}, "cartservice-REPLACE-ME"),
            ({"service": "redis-exporter", "job": "redis-exporter"}, "redis-exporter"),
            ({"container": "server"}, ""),  # container alone says nothing
            ({}, ""),
        ]
        for labels, expected in cases:
            with self.subTest(labels=labels):
                self.assertEqual(alerts.service_of(labels), expected)

    def test_replaced_pods_share_one_key(self):
        def alert(pod):
            return {"labels": {"alertname": "PodCrashLooping", "namespace": "ns", "pod": pod}}
        self.assertEqual(alerts.key_of(alert("cartservice-6b9f7c8d4-aaaaa")),
                         alerts.key_of(alert("cartservice-7c8d9e0f1-bbbbb")))
        self.assertEqual(str(alerts.key_of(alert("cartservice-6b9f7c8d4-aaaaa"))),
                         "PodCrashLooping/ns/cartservice")

    def test_firing_ignores_resolved(self):
        payload = {"alerts": [{"status": "firing"}, {"status": "resolved"}, {}]}
        self.assertEqual(len(alerts.firing(payload)), 2)


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CHART_RULES = os.path.join(os.path.dirname(__file__), "..", "..", "helm", "online-boutique",
                           "templates", "prometheusrules.yaml")
APP_NS = "online-boutique-dev"


class WorthInvestigatingTest(unittest.TestCase):
    def test_no_live_alert_from_2026_10_04_is_worth_an_investigation(self):
        with open(os.path.join(FIXTURES, "live-alerts-2026-10-04.json"), encoding="utf-8") as f:
            payloads = json.load(f)["payloads"]
        seen = []
        for payload in payloads:
            for a in alerts.firing(payload):
                worth, why = alerts.worth_investigating(a, APP_NS)
                seen.append(a["labels"]["alertname"])
                with self.subTest(alert=a["labels"]["alertname"], labels=a["labels"]):
                    self.assertFalse(worth)
                    self.assertTrue(why)
        self.assertEqual(len(seen), 16)
        # The 10 shop-service TargetDown alerts are in the app namespace with
        # severity warning: a namespace+severity matcher alone would send them.
        shop_target_down = [p for p in payloads if p["groupLabels"]["alertname"] == "TargetDown"][0]
        in_app_ns = [a for a in shop_target_down["alerts"] if a["labels"].get("namespace") == APP_NS]
        self.assertEqual(len(in_app_ns), 10)

    def test_listed_alerts_in_the_app_namespace_are_investigated(self):
        for name in alerts.INVESTIGATE_ALERTS:
            for severity in ("warning", "critical"):
                with self.subTest(alert=name, severity=severity):
                    a = {"labels": {"alertname": name, "namespace": APP_NS, "severity": severity}}
                    self.assertEqual(alerts.worth_investigating(a, APP_NS), (True, ""))

    def test_rejections(self):
        cases = [
            ({"alertname": "PodCrashLooping", "namespace": "kube-system", "severity": "critical"},
             "not in the app namespace"),
            ({"alertname": "TargetDown", "namespace": APP_NS, "severity": "warning"},
             "not on the agent's alert list"),
            ({"alertname": "KubePodCrashLooping", "namespace": APP_NS, "severity": "warning"},
             "not on the agent's alert list"),
            ({"alertname": "HighGrpcErrorRate", "namespace": APP_NS, "severity": "critical"},
             "not on the agent's alert list"),
            ({"alertname": "PodCrashLooping", "namespace": APP_NS, "severity": "info"},
             "below warning"),
            ({"alertname": "Watchdog", "severity": "none"}, "not in the app namespace"),
        ]
        for labels, reason in cases:
            with self.subTest(labels=labels):
                worth, why = alerts.worth_investigating({"labels": labels}, APP_NS)
                self.assertFalse(worth)
                self.assertIn(reason, why)

    def test_every_listed_alert_is_a_rule_in_the_chart(self):
        # Renaming or removing a chart rule must not silently stop investigations.
        with open(CHART_RULES, encoding="utf-8") as f:
            chart_alerts = set(re.findall(r"^\s*- alert: (\w+)", f.read(), re.MULTILINE))
        self.assertEqual(sorted(set(alerts.INVESTIGATE_ALERTS) - chart_alerts), [])

    def test_route_matchers_for_stage_4(self):
        self.assertEqual(alerts.route_matchers(APP_NS), [
            'namespace="online-boutique-dev"',
            'alertname=~"PodCrashLooping|PodNotReady|ContainerOOMKilled|HighCPUUsage|'
            'HighMemoryUsage|DeploymentReplicasMismatch|HpaMaxedOut"',
            'severity=~"warning|critical"'])


class DeduplicatorTest(unittest.TestCase):
    def test_window(self):
        now = [0.0]
        dedup = Deduplicator(window_seconds=1800, clock=lambda: now[0])
        key = AlertKey("PodCrashLooping", "ns", "cartservice")

        self.assertFalse(dedup.is_recent(key))
        dedup.mark(key)
        now[0] = 1799
        self.assertTrue(dedup.is_recent(key))
        self.assertFalse(dedup.is_recent(AlertKey("PodCrashLooping", "ns", "emailservice")))
        now[0] = 1800
        self.assertFalse(dedup.is_recent(key))

    def test_expired_keys_are_forgotten(self):
        now = [0.0]
        dedup = Deduplicator(window_seconds=10, clock=lambda: now[0])
        dedup.mark(AlertKey("a", "ns", "x"))
        now[0] = 20
        dedup.mark(AlertKey("b", "ns", "y"))
        self.assertEqual(len(dedup._seen), 1)


if __name__ == "__main__":
    unittest.main()
