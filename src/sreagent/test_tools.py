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
from tools import ToolError, ToolRegistry


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

    def test_truncate_tail_mode_keeps_head_and_tail(self):
        out = truncate("H" * 50 + "M" * 1000 + "T" * 50, 120, keep="tail")
        self.assertTrue(out.startswith("H"))
        self.assertTrue(out.endswith("T" * 50))
        self.assertIn("truncated 980 characters", out)
        self.assertEqual(truncate("short", 100), "short")

    def test_truncate_head_mode_keeps_the_start(self):
        out = truncate("H" * 100 + "T" * 100, 120, keep="head")
        self.assertTrue(out.startswith("H" * 100 + "T" * 20))
        self.assertTrue(out.endswith("[truncated 80 characters]"))


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
    def _event(kind, name, reason, component, minutes_ago, type_="Normal",
               count=1, first_minutes_ago=None):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(minutes=minutes_ago)
        first = now - timedelta(minutes=first_minutes_ago) if first_minutes_ago else ts
        return NS(type=type_, reason=reason, message=f"{reason} message", count=count,
                  first_timestamp=first, last_timestamp=ts, event_time=None,
                  metadata=NS(creation_timestamp=ts),
                  involved_object=NS(kind=kind, name=name),
                  source=NS(component=component), reporting_component=None)

    def _list_events(self, events, **args):
        core = NS(list_namespaced_event=lambda ns, field_selector=None: NS(items=list(events)))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        tools = {t.name: t for t in k8s_tools.make_tools(kube)}
        return tools["list_events"].handler(args).splitlines()

    def test_events_by_app_cover_the_ownership_chain_controllers_first(self):
        # Shaped like the live emailservice cycle (regressions/emailservice-events-raw.txt)
        events = [
            self._event("Pod", "emailservice-6b9f7c-new01", "Unhealthy", "kubelet", 4, "Warning", count=2),
            self._event("Pod", "emailservice-6b9f7c-new01", "Started", "kubelet", 4),
            self._event("Pod", "emailservice-6b9f7c-x2k4p", "Killing", "kubelet", 5),
            self._event("ReplicaSet", "emailservice-6b9f7c", "SuccessfulDelete", "replicaset-controller", 5),
            self._event("Deployment", "emailservice", "ScalingReplicaSet", "deployment-controller", 4,
                        count=19, first_minutes_ago=95),
            self._event("HorizontalPodAutoscaler", "emailservice", "SuccessfulRescale",
                        "horizontal-pod-autoscaler", 6, count=18, first_minutes_ago=96),
            self._event("Pod", "emailservicex-1", "Started", "kubelet", 1),  # different app
            self._event("Pod", "cartservice-abc", "Killing", "kubelet", 1),
        ]

        lines = self._list_events(events, app="emailservice")

        self.assertEqual(lines[0], "Controller events (HPA, Deployment, ReplicaSet, ...), newest first:")
        self.assertEqual([l.split()[4].split("/")[0] for l in lines[1:4]],
                         ["Deployment", "ReplicaSet", "HorizontalPodAutoscaler"])
        self.assertIn("ScalingReplicaSet Deployment/emailservice [deployment-controller] "
                      "(x19 since 1h35m ago)", lines[1])
        self.assertIn("[horizontal-pod-autoscaler] (x18 since 1h36m ago)", lines[3])
        self.assertEqual(lines[4], "Pod events, newest first:")
        self.assertEqual(len(lines), 8)
        text = "\n".join(lines)
        self.assertIn("Unhealthy Pod/emailservice-6b9f7c-new01 [kubelet] (x2 since 4m ago)", text)
        self.assertNotIn("cartservice", text)
        self.assertNotIn("emailservicex", text)

    def test_controller_events_survive_truncation(self):
        events = [self._event("Pod", f"emailservice-6b9f7c-p{i:04d}", "Pulled", "kubelet", 1)
                  for i in range(40)]
        events.append(self._event("Deployment", "emailservice", "ScalingReplicaSet",
                                  "deployment-controller", 30, count=19, first_minutes_ago=95))
        core = NS(list_namespaced_event=lambda ns, field_selector=None: NS(items=events))
        kube = k8s_tools.KubeReader("ns", core=core, apps=None, autoscaling=None)
        registry = ToolRegistry(k8s_tools.make_tools(kube), max_output_chars=600)

        output, is_error = registry.run("list_events", {"app": "emailservice"})

        self.assertFalse(is_error)
        self.assertIn("ScalingReplicaSet Deployment/emailservice", output)
        self.assertIn("truncated", output)

    def test_describe_deployment_after_the_replicas_fix(self):
        now = datetime.now(timezone.utc)

        def rs(name, revision, replicas, tag, minutes):
            return NS(metadata=NS(name=name, annotations={k8s_tools.REVISION: str(revision)},
                                  creation_timestamp=now - timedelta(minutes=minutes)),
                      spec=NS(replicas=replicas, template=NS(spec=NS(containers=[
                          NS(image=f"123456789012.dkr.ecr.us-east-1.amazonaws.com/x-emailservice:{tag}")]))),
                      status=NS(ready_replicas=replicas))

        dep = NS(
            metadata=NS(name="emailservice", generation=49, annotations={
                k8s_tools.REVISION: "5",
                k8s_tools.LAST_APPLIED: '{"kind": "Deployment", "spec": {"selector": {}}}'},
                managed_fields=[
                    NS(manager="argocd-controller", operation="Update", subresource=None,
                       time=now - timedelta(minutes=20), fields_v1={"f:spec": {"f:template": {}}}),
                    NS(manager="kube-controller-manager", operation="Update", subresource="scale",
                       time=now - timedelta(minutes=10), fields_v1={"f:spec": {"f:replicas": {}}}),
                ]),
            spec=NS(replicas=1),
            status=NS(observed_generation=49, replicas=1, ready_replicas=1, updated_replicas=1,
                      available_replicas=1))
        sets = [rs("emailservice-7959ddb655", 4, 0, "v20261004-134739-d34db63e", 180),
                rs("emailservice-748b648c5f", 5, 1, "v20261004-154933-68ac8238", 18)]

        out = k8s_tools.describe_deployment(dep, sets).splitlines()

        self.assertEqual(out[1], "Live spec.replicas: 1; status: replicas 1, ready 1, updated 1, available 1")
        self.assertIn("last applied manifest", out[2])
        self.assertTrue(out[2].endswith("absent (the manifest does not set it)"))
        self.assertEqual(out[3], "Field managers of spec.replicas: "
                                 "kube-controller-manager (Update, subresource scale, 10m ago)")
        self.assertTrue(out[5].startswith("  emailservice-748b648c5f: revision 5 (current), desired 1"))
        self.assertIn("image tag v20261004-154933-68ac8238", out[5])
        self.assertTrue(out[6].startswith("  emailservice-7959ddb655: revision 4, desired 0"))

    def test_describe_deployment_before_the_fix_shows_replicas_in_manifest(self):
        dep = NS(metadata=NS(name="emailservice", generation=40, managed_fields=[],
                             annotations={k8s_tools.LAST_APPLIED: '{"spec": {"replicas": 2}}'}),
                 spec=NS(replicas=2), status=NS(observed_generation=40, replicas=2, ready_replicas=2,
                                               updated_replicas=2, available_replicas=2))
        out = k8s_tools.describe_deployment(dep, [])
        self.assertIn("present, = 2", out)
        self.assertIn("Field managers of spec.replicas: none recorded", out)

    def test_rejects_invalid_names(self):
        tools = {t.name: t for t in k8s_tools.make_tools(NS(namespace="ns"))}
        for bad in ("../etc", "Pod_Name", "a b", ""):
            with self.subTest(name=bad), self.assertRaises(ToolError):
                tools["get_pod_logs"].handler({"pod": bad})


class ReadRepoFileTest(unittest.TestCase):
    """Uses the real chart values.yaml, which is longer than the output limit."""

    def setUp(self):
        import base64
        import os
        values = os.path.join(os.path.dirname(__file__), "..", "..", "helm",
                              "online-boutique", "values.yaml")
        with open(values, "rb") as f:
            self.text = f.read().decode("utf-8")
        content = base64.b64encode(self.text.encode()).decode()
        gh = NS(branch="main", values_file="v.yaml", chart_path="helm/online-boutique",
                get=lambda path, params=None: {"encoding": "base64", "content": content})
        self.read = {t.name: t for t in github_tools.make_tools(gh)}["read_repo_file"].handler

    def test_search_then_read_finds_a_block_past_the_truncation_point(self):
        line_no = self.text.splitlines().index("  emailservice:") + 1
        # The block's HPA settings lie past the 6000-character output limit
        hpa_offset = self.text.index("minReplicas", self.text.index("  emailservice:"))
        self.assertGreater(hpa_offset, 6000)

        hits = self.read({"path": "helm/online-boutique/values.yaml", "search": "  emailservice:"})
        self.assertIn(f"{line_no}:   emailservice:", hits)

        block = self.read({"path": "helm/online-boutique/values.yaml",
                           "start_line": line_no, "max_lines": 40})
        self.assertTrue(block.startswith(f"helm/online-boutique/values.yaml lines {line_no}-"))
        self.assertIn("replicas:", block)
        self.assertIn("minReplicas:", block)
        self.assertLess(len(block), 6000)

    def test_default_window_and_paging_hint(self):
        out = self.read({"path": "helm/online-boutique/values.yaml"})
        total = len(self.text.splitlines())
        first = out.splitlines()[0]
        self.assertEqual(first, f"helm/online-boutique/values.yaml lines 1-"
                                f"{github_tools.DEFAULT_FILE_LINES} of {total}; "
                                f"read on with start_line={github_tools.DEFAULT_FILE_LINES + 1}")

    def test_search_miss(self):
        self.assertIn("not found", self.read({"path": "x.yaml", "search": "nope-not-here"}))


class RecentChartCommitsTest(unittest.TestCase):
    def test_template_changes_and_deploys_both_show(self):
        def commit(sha, message):
            return {"sha": sha, "commit": {"message": message,
                                           "author": {"date": "2026-10-04T15:49:00Z", "name": "dev"}}}

        commits = [commit("20b4232a" + "0" * 32, "chore(deploy): v20261004-154933-68ac8238 [skip ci]"),
                   commit("96cf4f5a" + "0" * 32, "fix(chart): let the HPA own replicas")]
        details = {
            commits[0]["sha"]: {"files": [{"filename": "helm/online-boutique/values-dev.yaml",
                                           "status": "modified", "patch": '-  tag: "a"\n+  tag: "b"'}]},
            commits[1]["sha"]: {"files": [
                {"filename": "helm/online-boutique/templates/deployments.yaml", "status": "modified",
                 "patch": "-  replicas: {{ $svc.replicas }}\n" + "+x\n" * 2000},
                {"filename": "README.md", "status": "modified", "patch": "+docs"}]},
        }
        seen = []

        def get(path, params=None):
            seen.append((path, params))
            return commits if path == "/commits" else details[path.rsplit("/", 1)[-1]]

        gh = NS(branch="main", values_file="helm/online-boutique/values-dev.yaml",
                chart_path="helm/online-boutique", get=get)
        tool = {t.name: t for t in github_tools.make_tools(gh)}["recent_chart_commits"]

        out = tool.handler({"limit": 2})

        self.assertEqual(seen[0], ("/commits", {"path": "helm/online-boutique", "sha": "main",
                                                "per_page": 2}))
        self.assertIn("--- helm/online-boutique/values-dev.yaml (modified)", out)
        self.assertIn("--- helm/online-boutique/templates/deployments.yaml (modified)", out)
        self.assertIn("-  replicas: {{ $svc.replicas }}", out)
        self.assertIn("[patch cut,", out)
        self.assertNotIn("README.md", out)
        self.assertLess(out.index("chore(deploy)"), out.index("fix(chart)"))

    def test_chart_path_is_the_values_file_folder(self):
        gh = github_tools.GitHubReader("o/r", None, "main", "helm/online-boutique/values-dev.yaml")
        self.assertEqual(gh.chart_path, "helm/online-boutique")


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
            self.assertIn("automated sync, self-heal and", text)
            self.assertIn("explain both halves", text)
            self.assertIn("first seconds after a container starts", text)
            self.assertNotIn("{", text.replace('{namespace="', ""))  # nothing left unformatted


class RefreshStartsAtTest(unittest.TestCase):
    def test_sets_recent_start_time(self):
        import cli
        payload = {"alerts": [{"startsAt": "2026-10-04T12:00:00Z"}, {"startsAt": "x"}]}
        now = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
        cli.refresh_starts_at(payload, 10, now=now)
        self.assertEqual([a["startsAt"] for a in payload["alerts"]],
                         ["2026-10-05T09:20:00Z"] * 2)


class ReportTest(unittest.TestCase):
    def test_report_keeps_non_ascii_and_lists_calls(self):
        import cli
        from agent import Evidence, Investigation
        result = Investigation("answered", "HPA → 1, then Argo CD → 2 — a cycle",
                               [Evidence("list_events", {"app": "emailservice"}, "...", False)], 3.2)
        report = cli.format_report(result, 15, header="ask: why?")
        self.assertTrue(report.startswith("ask: why?\n\nHPA → 1, then Argo CD → 2 — a cycle"))
        self.assertIn("Tool calls: 1 of 15 allowed", report)
        self.assertIn('[ok] list_events {"app": "emailservice"}', report)


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
