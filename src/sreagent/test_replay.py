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

    def complete(self, system, messages, tools):
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


class RunCaseTest(unittest.TestCase):
    def test_run_is_scored_graded_and_recorded(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(os.path.join(HERE, "regressions", "emailservice-events-raw.txt"), tmp)
        case_path = os.path.join(tmp, "case.json")
        with open(case_path, "w", encoding="utf-8") as f:
            json.dump({"replay": SPEC, "pass_criteria": {"keyword_hints": KeywordCheckTest.HINTS}}, f)

        assessment = {"conclusion": "cause_found", "cause": "HPA vs Argo CD self-heal",
                      "cause_support": "inferred", "cause_evidence": [1],
                      "alternatives": [{"cause": "readiness probes", "result": "ruled_out",
                                        "evidence": [1]},
                                       {"cause": "node problem", "result": "not_ruled_out",
                                        "evidence": []}],
                      "timing": "matches", "timing_evidence": [1],
                      "unverified": [{"what": "Argo CD's sync history",
                                      "could_change_diagnosis": False}]}
        model = ScriptedModel([("list_events", {"app": "emailservice"}), ("list_hpas", {}),
                               ("submit_assessment", assessment)],
                              "The HPA scales down and Argo CD self-heal restores 2 on "
                              "ip-10-0-11-231.ec2.internal.")
        config = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns"})

        run = replay.run_case(case_path, config, model)

        self.assertEqual(run["outcome"], "answered")
        # inferred 30 + ruled out 10 - open 15 + timing 20 - minor 2 = 43
        self.assertEqual(run["score"], 43)
        self.assertTrue(run["keyword_check"]["passed"])
        self.assertEqual([c["is_error"] for c in run["tool_calls"]], [False, True, False])
        self.assertIn("<node-1>", run["answer"])                 # redacted
        self.assertIn("Current time: 2026-10-04T15:32:00Z (UTC)", model.seen[0].text)
        self.assertTrue(model.seen[1].results[0].content.startswith("[call #1]"))

        self.assertEqual(run["unavailable"], ["list_hpas {}"])            # counted, not hidden

        replay.record(case_path, run)
        with open(case_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["replay_runs"][0]["score"], 43)


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


if __name__ == "__main__":
    unittest.main()
