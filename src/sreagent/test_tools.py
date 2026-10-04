"""Tests for redaction, the tool modules' formatting/validation, and alert parsing."""

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import github_tools
import k8s_tools
import prometheus_tools
import prompts
from redact import redact, truncate
from tools import ToolError


class RedactTest(unittest.TestCase):
    def test_known_secret_formats(self):
        secrets = [
            "sk-ant-api03-" + "x" * 40,
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 60,
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJlc2lnbmF0dXJl",
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:12]):
                out = redact(f"value: {secret} end")
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED]", out)

    def test_key_value_pairs_and_headers(self):
        cases = {
            "password=hunter22": "hunter22",
            'DB_PASSWORD: "s3cr3tvalue"': "s3cr3tvalue",
            "api_key=abcd1234efgh": "abcd1234efgh",
            "Authorization: Bearer abc.def.ghi123": "abc.def.ghi123",
            "redis://user:p4ssw0rd@redis:6379": "p4ssw0rd",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))

    def test_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(redact(f"a {pem} b"), "a [REDACTED] b")

    def test_ordinary_text_is_untouched(self):
        text = 'max_tokens=100 grpc_code="Unavailable" memory=512Mi cpu: 100m'
        self.assertEqual(redact(text), text)

    def test_truncate_keeps_head_and_tail(self):
        out = truncate("H" * 50 + "M" * 1000 + "T" * 50, 120)
        self.assertTrue(out.startswith("H"))
        self.assertTrue(out.endswith("T" * 50))
        self.assertIn("truncated 980 characters", out)
        self.assertEqual(truncate("short", 100), "short")


class PrometheusFormatTest(unittest.TestCase):
    def test_vector(self):
        data = {"resultType": "vector", "result": [
            {"metric": {"__name__": "up", "pod": "cart-1"}, "value": [1, "1"]}]}
        self.assertEqual(prometheus_tools.format_result(data), '1 series\nup{pod="cart-1"} = 1')

    def test_empty(self):
        self.assertIn("No data", prometheus_tools.format_result({"resultType": "vector", "result": []}))

    def test_matrix_summary(self):
        values = [[1700000000 + 60 * i, str(i)] for i in range(100)]
        data = {"resultType": "matrix", "result": [{"metric": {"pod": "a"}, "values": values}]}
        out = prometheus_tools.format_result(data)
        self.assertIn("100 points, min=0 max=99 last=99", out)
        self.assertEqual(out.count("="), 4 + 10)  # label, min, max, last + 10 samples

    def test_series_are_capped(self):
        result = [{"metric": {"i": str(i)}, "value": [0, "1"]} for i in range(40)]
        out = prometheus_tools.format_result({"resultType": "vector", "result": result})
        self.assertIn("40 series, showing first 25", out)
        self.assertEqual(len(out.splitlines()), 26)

    def test_range_query_widens_step(self):
        seen = {}

        class FakeProm:
            def range(self, query, start, end, step):
                seen.update(start=start, end=end, step=step)
                return {"resultType": "matrix", "result": []}

        tool = {t.name: t for t in prometheus_tools.make_tools(FakeProm(), clock=lambda: 100000.0)}
        tool["prometheus_query_range"].handler({"query": "up", "minutes": 1440, "step_seconds": 15})
        self.assertEqual(seen["end"] - seen["start"], 1440 * 60)
        self.assertEqual(seen["step"], 1440 * 60 // prometheus_tools.MAX_RANGE_POINTS)

    def test_bad_promql_becomes_tool_error(self):
        def opener(url, timeout):
            body = io.BytesIO(json.dumps({"status": "error", "error": "parse error"}).encode())
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)

        prom = prometheus_tools.PrometheusClient("http://prom", opener=opener)
        with self.assertRaisesRegex(ToolError, "HTTP 400: parse error"):
            prom.instant("rate(")


class KubeFormatTest(unittest.TestCase):
    def test_pod_with_oomkill(self):
        now = datetime.now(timezone.utc)
        status = NS(name="server", ready=False, restart_count=4,
                    state=NS(waiting=NS(reason="CrashLoopBackOff")),
                    last_state=NS(terminated=NS(reason="OOMKilled", exit_code=137,
                                                finished_at=now - timedelta(minutes=3))))
        pod = NS(metadata=NS(name="cartservice-abc", creation_timestamp=now - timedelta(hours=2),
                             owner_references=[NS(kind="ReplicaSet", name="cartservice-7f9")]),
                 status=NS(phase="Running", container_statuses=[status]),
                 spec=NS(node_name="ip-10-0-1-1"))
        out = k8s_tools.format_pod(pod)
        self.assertIn("cartservice-abc: Running, ready 0/1, restarts 4, age 2h", out)
        self.assertIn("owner ReplicaSet/cartservice-7f9", out)
        self.assertIn("server waiting: CrashLoopBackOff", out)
        self.assertIn("last exit: OOMKilled (code 137, 3m ago)", out)

    @staticmethod
    def _event(kind, name, reason, component, minutes_ago, type_="Normal"):
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return NS(type=type_, reason=reason, message=f"{reason} message", count=1,
                  last_timestamp=ts, event_time=None, metadata=NS(creation_timestamp=ts),
                  involved_object=NS(kind=kind, name=name),
                  source=NS(component=component), reporting_component=None)

    def test_events_by_app_cover_the_ownership_chain(self):
        events = [
            self._event("Pod", "emailservice-6b9f7c-x2k4p", "Killing", "kubelet", 5),
            self._event("ReplicaSet", "emailservice-6b9f7c", "SuccessfulDelete", "replicaset-controller", 5),
            self._event("Deployment", "emailservice", "ScalingReplicaSet", "deployment-controller", 5),
            self._event("HorizontalPodAutoscaler", "emailservice", "SuccessfulRescale",
                        "horizontal-pod-autoscaler", 6),
            self._event("Pod", "emailservicex-1", "Started", "kubelet", 1),  # different app
            self._event("Pod", "cartservice-abc", "Killing", "kubelet", 1),
        ]
        core = NS(list_namespaced_event=lambda ns, field_selector=None: NS(items=list(events)))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        tools = {t.name: t for t in k8s_tools.make_tools(kube)}

        lines = tools["list_events"].handler({"app": "emailservice"}).splitlines()

        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[-1].startswith("6m ago Normal SuccessfulRescale "
                                             "HorizontalPodAutoscaler/emailservice "
                                             "[horizontal-pod-autoscaler]"))
        self.assertIn("Killing Pod/emailservice-6b9f7c-x2k4p [kubelet]", "\n".join(lines))
        self.assertNotIn("cartservice", "\n".join(lines))
        self.assertNotIn("emailservicex", "\n".join(lines))

    def test_rejects_invalid_names(self):
        tools = {t.name: t for t in k8s_tools.make_tools(NS(namespace="ns"))}
        for bad in ("../etc", "Pod_Name", "a b", ""):
            with self.subTest(name=bad), self.assertRaises(ToolError):
                tools["get_pod_logs"].handler({"pod": bad})


class GitHubPathTest(unittest.TestCase):
    def test_safe_path(self):
        self.assertEqual(github_tools.safe_path("helm/online-boutique/values.yaml"),
                         "helm/online-boutique/values.yaml")
        for bad in ("../secrets", "/etc/passwd", "helm/../../x", "a//b", "a\\b"):
            with self.subTest(path=bad), self.assertRaises(ToolError):
                github_tools.safe_path(bad)


class PromptTest(unittest.TestCase):
    def test_system_prompts_render(self):
        for kind in ("investigate", "ask"):
            text = prompts.system_prompt(kind, "online-boutique-dev", 12)
            self.assertIn('namespace="online-boutique-dev"', text)
            self.assertIn("at most 12 tool calls", text)
            self.assertIn("readiness probe only removes the pod", text)
            self.assertNotIn("{", text.replace('{namespace="', ""))  # nothing left unformatted


class RefreshStartsAtTest(unittest.TestCase):
    def test_sets_recent_start_time(self):
        import cli
        payload = {"alerts": [{"startsAt": "2026-10-04T12:00:00Z"}, {"startsAt": "x"}]}
        now = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
        cli.refresh_starts_at(payload, 10, now=now)
        self.assertEqual([a["startsAt"] for a in payload["alerts"]],
                         ["2026-10-05T09:20:00Z"] * 2)


class AlertTaskTest(unittest.TestCase):
    def test_only_firing_alerts_and_redacted(self):
        payload = {"alerts": [
            {"status": "firing", "labels": {"alertname": "PodCrashLooping", "severity": "critical",
                                            "pod": "cart-1"},
             "annotations": {"summary": "crash", "description": "token=abcdef123456"},
             "startsAt": "2026-10-04T12:00:00Z"},
            {"status": "resolved", "labels": {"alertname": "OldAlert"}}]}
        task = prompts.alert_task(payload)
        self.assertIn("Alert: PodCrashLooping (severity critical)", task)
        self.assertIn("pod=cart-1", task)
        self.assertNotIn("OldAlert", task)
        self.assertNotIn("abcdef123456", task)


if __name__ == "__main__":
    unittest.main()
