"""Tests for alert identity (alertname + namespace + service) and deduplication."""

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
