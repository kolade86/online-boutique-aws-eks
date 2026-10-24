"""Tests for the replay harness (replay.py), with a scripted model instead of the API."""

import json
import os
import shutil
import tempfile
import unittest

import replay
from config import Config
from model import AssistantTurn, ToolCall
from tools import ToolError

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = {"question": "Why was the emailservice pod replaced?", "now": "2026-10-04T15:32:00Z",
        "namespace": "online-boutique-dev", "repo_ref": "HEAD",
        "recorded": [{"tool": "list_events", "input": {"app": "emailservice"},
                      "file": "emailservice-events-raw.txt"}]}


class ScriptedModel:
    """Makes the given tool calls one per turn, then answers."""

    def __init__(self, calls, answer):
        self.turns = [AssistantTurn("", [ToolCall(f"c{i}", name, args)], "tool_use")
                      for i, (name, args) in enumerate(calls)]
        self.turns.append(AssistantTurn(answer, [], "end_turn"))
        self.seen = []

    def complete(self, system, messages, tools, allow_tools=True):
        self.seen.append(messages[-1])
        return self.turns.pop(0)


class ReplayRegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = replay.replay_registry(SPEC, os.path.join(HERE, "regressions"), 6000)

    def test_same_tool_list_as_the_live_agent(self):
        names = {s.name for s in self.reg.specs}
        self.assertTrue({"list_events", "list_pods", "list_hpas", "describe_deployment",
                         "prometheus_query", "read_repo_file", "recent_chart_commits"} <= names)

    def test_recorded_call_returns_the_recording(self):
        out, err = self.reg.run("list_events", {"app": "emailservice"})
        self.assertFalse(err)
        self.assertIn("ScalingReplicaSet Deployment/emailservice", out)

    def test_unrecorded_call_is_unavailable(self):
        for name, args in (("list_hpas", {}), ("list_events", {"app": "cartservice"})):
            out, err = self.reg.run(name, args)
            self.assertTrue(err)
            self.assertIn("No recorded output", out)

    def test_repo_tools_read_local_git(self):
        out, err = self.reg.run("read_repo_file", {"path": "helm/online-boutique/values.yaml",
                                                   "search": "  emailservice:"})
        self.assertFalse(err, out)
        self.assertIn("emailservice:", out)
        out, err = self.reg.run("recent_chart_commits", {"limit": 1})
        self.assertFalse(err, out)


class KeywordCheckTest(unittest.TestCase):
    HINTS = {"must_mention_any": [["HPA", "autoscal"], ["Argo CD", "self-heal"]],
             "must_not_contain": ["root cause is readiness"]}

    def test_grades(self):
        self.assertTrue(replay.keyword_check("The HPA and Argo CD fight", self.HINTS)["passed"])
        bad = replay.keyword_check("The root cause is readiness probes; the HPA scaled", self.HINTS)
        self.assertFalse(bad["passed"])
        self.assertEqual(bad["missing_any_of"], [["Argo CD", "self-heal"]])
        self.assertEqual(bad["contains_forbidden"], ["root cause is readiness"])


class CaptureTest(unittest.TestCase):
    def test_standard_pack_is_recorded_redacted_and_replayable(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        case_path = os.path.join(tmp, "incident.json")
        ran = []

        class LiveRegistry:
            def run(self, tool, args):
                ran.append(tool)
                if tool == "list_hpas":
                    return "metrics API down", True            # failed: not recorded
                return f"{tool} {json.dumps(args)} on ip-10-0-11-45.ec2.internal", False

        spec = replay.capture(LiveRegistry(), case_path, "recommendationservice",
                              "online-boutique-dev", "Why is it OOMKilled?", "439be39",
                              now=replay.datetime(2026, 10, 10, 17, 25, tzinfo=replay.timezone.utc))

        pack = replay.standard_pack("recommendationservice", "online-boutique-dev")
        self.assertEqual(len(ran), len(pack))
        self.assertIn(("describe_deployment", {"name": "recommendationservice"}), pack)
        self.assertEqual(len(spec["recorded"]), len(pack) - 1)
        self.assertNotIn("list_hpas", [r["tool"] for r in spec["recorded"]])
        self.assertEqual((spec["now"], spec["repo_ref"]), ("2026-10-10T17:25:00Z", "439be39"))
        with open(os.path.join(tmp, spec["recorded"][0]["file"]), encoding="utf-8") as f:
            self.assertIn("<node-1>", f.read())             # redacted before it goes into Git

        # The recording answers the same calls in a replay
        reg = replay.replay_registry(spec, tmp, 6000)
        out, err = reg.run("describe_deployment", {"name": "recommendationservice"})
        self.assertFalse(err)
        out, err = reg.run("list_hpas", {})
        self.assertTrue(err)
        self.assertTrue(out.startswith(replay.UNAVAILABLE))

    # The agent's record of an incident, as GET /investigations returns it (no outputs)
    INVESTIGATION = {
        "id": "ab12cd34", "kind": "alert", "finished_at": "2026-10-12T16:03:10Z",
        "subject": "ContainerOOMKilled/online-boutique-dev/paymentservice", "outcome": "answered",
        "answer": "OOMKilled on ip-10-0-11-45.ec2.internal", "confidence": {"score": 100},
        "pull_request": {"status": "opened", "number": 40},
        "evidence": [
            {"tool": "list_pods", "input": {"app": "paymentservice"}, "is_error": False},
            {"tool": "get_pod_logs", "input": {"pod": "paymentservice-5d9f8c7b6d-abcde"}, "is_error": True},
            {"tool": "read_repo_file", "input": {"path": "helm/online-boutique/values-dev.yaml"},
             "is_error": False},
            {"tool": "submit_assessment", "input": {"conclusion": "cause_found"}, "is_error": False},
            {"tool": "propose_values_change", "input": {"changes": []}, "is_error": False}]}
    # GET /api/v2/alerts, as Alertmanager returns it
    AM_ALERTS = [
        {"labels": {"alertname": "ContainerOOMKilled", "namespace": "online-boutique-dev",
                    "pod": "paymentservice-5d9f8c7b6d-abcde", "severity": "critical",
                    "instance": "10.0.11.7:8080"},
         "annotations": {"summary": "OOMKilled"}, "startsAt": "2026-10-12T16:01:00Z",
         "status": {"state": "active"}, "fingerprint": "f1", "receivers": [{"name": "sre-agent"}]},
        {"labels": {"alertname": "PodNotReady", "namespace": "online-boutique-dev",
                    "pod": "paymentservice-5d9f8c7b6d-abcde", "severity": "warning"},
         "annotations": {}, "startsAt": "2026-10-12T16:02:00Z", "status": {"state": "active"}},
        {"labels": {"alertname": "ContainerOOMKilled", "namespace": "online-boutique-dev",
                    "pod": "cartservice-77f6c8d9b5-zzzzz", "severity": "critical"},
         "annotations": {}, "startsAt": "2026-10-12T16:01:00Z", "status": {"state": "active"}}]

    class Live:
        """The live registry: logs fail, everything else answers with a node name."""

        def __init__(self):
            self.ran = []

        def run(self, tool, args):
            self.ran.append(tool)
            if tool == "get_pod_logs":
                return "pod not found", True
            return f"{tool} live on ip-10-0-11-45.ec2.internal and ip-10-0-10-9.ec2.internal", False

    def capture(self, investigation, alert=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "payment-oom.json")
        live = self.Live()
        spec = replay.capture(live, path, "paymentservice", "online-boutique-dev", None, "HEAD",
                              investigation=investigation, alert=alert,
                              now=replay.datetime(2026, 10, 12, 16, 10, tzinfo=replay.timezone.utc))
        with open(path, encoding="utf-8") as f:
            return tmp, live, spec, json.load(f)

    def test_the_agents_own_calls_are_run_again_and_recorded_first(self):
        investigation = replay.pick_investigation([{"id": "zz", "kind": "ask", "subject": "q"},
                                                   self.INVESTIGATION], "paymentservice")
        alert = replay.alert_payload(self.AM_ALERTS, "paymentservice", "online-boutique-dev",
                                     "ContainerOOMKilled")
        tmp, live, spec, case = self.capture(investigation, alert)

        # its two cluster calls first, then the pack. Not the assessment or the proposal,
        # and not the repository read: a replay takes that from git
        self.assertEqual(live.ran[:2], ["list_pods", "get_pod_logs"])
        self.assertEqual([r["source"] for r in spec["recorded"][:2]], ["agent call, run again"] * 2)
        self.assertFalse({"submit_assessment", "read_repo_file"} & set(live.ran))
        pack = replay.standard_pack("paymentservice", "online-boutique-dev")
        self.assertEqual(len(live.ran), 2 + len(pack) - 1)      # list_pods app=... is not run twice
        self.assertEqual(spec["now"], "2026-10-12T16:10:00Z")   # outputs are from capture time

        # the alert: only this service's alert of that name, redacted with the same placeholders
        self.assertEqual(case["mode"], "alert")
        self.assertEqual([a["labels"]["alertname"] for a in case["alert"]["alerts"]],
                         ["ContainerOOMKilled"])
        self.assertEqual(case["alert"]["alerts"][0]["labels"]["instance"], "<ip-1>:8080")
        self.assertEqual(case["live_run"]["answer"], "OOMKilled on <node-1>")
        self.assertEqual(case["live_run"]["pull_request"]["number"], 40)
        with open(os.path.join(tmp, spec["recorded"][0]["file"]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "list_pods live on <node-1> and <node-2>")

        # a replay answers the agent's calls, and fails the one that failed
        reg = replay.replay_registry(spec, tmp, 6000)
        self.assertEqual(reg.run("list_pods", {"app": "paymentservice"}),
                         ("list_pods live on <node-1> and <node-2>", False))
        self.assertEqual(reg.run("get_pod_logs", {"pod": "paymentservice-5d9f8c7b6d-abcde"}),
                         ("pod not found", True))

    def test_a_recording_is_used_as_the_agent_saw_it(self):
        recording = dict(self.INVESTIGATION, started_at="2026-10-12T16:02:40Z",
                         alert={"alerts": self.AM_ALERTS[:1]},
                         evidence=[dict(e, output=f"what the agent saw from {e['tool']}")
                                   for e in self.INVESTIGATION["evidence"]])
        alert = replay.alert_payload(recording["alert"], "paymentservice", "online-boutique-dev")
        tmp, live, spec, case = self.capture(recording, alert)

        self.assertNotIn("get_pod_logs", live.ran)               # nothing of the agent's is re-run
        self.assertEqual([r["source"] for r in spec["recorded"][:2]], ["agent"] * 2)
        self.assertEqual(spec["now"], "2026-10-12T16:02:40Z")    # when the agent ran
        with open(os.path.join(tmp, spec["recorded"][1]["file"]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "what the agent saw from get_pod_logs")
        self.assertTrue(spec["recorded"][1]["is_error"])

    def test_picking_the_investigation_and_the_alert(self):
        records = [self.INVESTIGATION, dict(self.INVESTIGATION, id="older")]
        self.assertEqual(replay.pick_investigation(records, "paymentservice")["id"], "ab12cd34")
        self.assertEqual(replay.pick_investigation(records, "x", "older")["id"], "older")
        with self.assertRaisesRegex(ValueError, "it holds: ab12cd34"):
            replay.pick_investigation(records, "cartservice")
        webhook = {"alerts": [dict(self.AM_ALERTS[0], status="firing"),
                              dict(self.AM_ALERTS[1], status="resolved")]}
        kept = replay.alert_payload(webhook, "paymentservice", "online-boutique-dev")
        self.assertEqual(len(kept["alerts"]), 1)
        with self.assertRaisesRegex(ValueError, "no firing alert for online-boutique-dev/emailservice"):
            replay.alert_payload(self.AM_ALERTS, "emailservice", "online-boutique-dev")

    def test_recorded_output_is_not_cut_a_second_time(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        once = "x" * 50 + "\n... [truncated 900 characters]"
        with open(os.path.join(tmp, "pods.txt"), "w", encoding="utf-8") as f:
            f.write(once)
        spec = {"repo_ref": "HEAD", "recorded": [{"tool": "list_pods", "input": {}, "file": "pods.txt"}]}
        self.assertEqual(replay.replay_registry(spec, tmp, 50).run("list_pods", {}), (once, False))


if __name__ == "__main__":
    unittest.main()
