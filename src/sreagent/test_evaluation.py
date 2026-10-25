"""Stage 6 evaluation (evaluation.py): what CI checks with no cluster and no model.

- The case files: format, recordings present, nothing unredacted (lint).
- Every recorded run, scored and judged again with the current rule: none may
  be critical (a change the case does not expect).
- A scripted-model run per case, through the agent loop, the scoring rule,
  the proposal tool and the allow-list.
- The committed report matches the recorded runs.

The cases pin commits of this repository (repo_ref), so the checkout needs
full history.
"""

import json
import os
import shutil
import tempfile
import unittest

import confidence
import evaluation as ev
import replay
from config import Config

try:
    import pr_tool
except ImportError:  # ruamel.yaml not installed
    pr_tool = None

HERE = os.path.dirname(os.path.abspath(__file__))
REGRESSIONS = os.path.join(HERE, "regressions")
CONFIG = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns"})
MIN_FOR_PR = CONFIG.min_confidence_for_pr
needs_packages = unittest.skipIf(pr_tool is None, "ruamel.yaml not installed")

MEMORY = "services.recommendationservice.resources.{}.memory"
# main after PR #31: values-dev.yaml holds recommendationservice memory 24Mi/24Mi
FAULT_COMMIT = "a6f077a"

ALERT = {"status": "firing", "alerts": [{
    "status": "firing", "startsAt": "2026-10-12T17:01:50Z",
    "labels": {"alertname": "ContainerOOMKilled", "namespace": "online-boutique-dev",
               "pod": "recommendationservice-7cb8759f8f-dllgs", "severity": "critical"},
    "annotations": {"summary": "Container OOMKilled"}}]}

RIGHT = ("The pod is OOMKilled: commit 6576d338 cut the memory limit to 24Mi, below the "
         "~43MB working set.")
OBSERVED = {
    "conclusion": "cause_found", "cause": "the 24Mi limit is below the working set",
    "cause_support": "observed", "cause_evidence": [1, 2],
    "decisive_check": {"what": "the commit that set 24Mi", "evidence": [2]},
    "alternatives": [{"cause": "a new image", "result": "ruled_out", "evidence": [2]}],
    "timing": "matches", "timing_evidence": [2], "unverified": []}


def propose(limit="128Mi", path=MEMORY.format("limits")):
    return {"tool": "propose_values_change",
            "input": {"changes": [{"path": path, "value": limit}], "reason": "OOM"}}


@needs_packages
class OutcomeTest(unittest.TestCase):
    """The four outcomes, on a small alert case replayed with a scripted model."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        with open(os.path.join(self.dir, "pods.txt"), "w", encoding="utf-8") as f:
            f.write("recommendationservice-7cb8759f8f-dllgs: last exit OOMKilled (code 137)")
        self.case = {
            "id": "oom", "format": 2, "mode": "alert", "alert": ALERT,
            "replay": {"now": "2026-10-12T17:02:40Z", "namespace": "online-boutique-dev",
                       "repo_ref": FAULT_COMMIT, "question": None,
                       "recorded": [{"tool": "list_pods", "input": {"app": "recommendationservice"},
                                     "file": "pods.txt"}]},
            "expected": {"kind": "config_fault",
                         "must_mention_any": [["OOMKilled"], ["24Mi"]], "must_not_contain": [],
                         "change": {"paths": [MEMORY.format("requests"), MEMORY.format("limits")],
                                    "min": {MEMORY.format("limits"): "64Mi"}}}}
        self.path = os.path.join(self.dir, "oom.json")

    def run_script(self, *last_calls, answer=RIGHT, assessment=OBSERVED, case=None):
        case = case or self.case
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(case, f)
        calls = [{"tool": "list_pods", "input": {"app": "recommendationservice"}},
                 {"tool": "recent_chart_commits", "input": {"limit": 3}},
                 {"tool": "submit_assessment", "input": assessment}, *last_calls]
        run = ev.run_script(case, self.path, {"calls": calls, "answer": answer})
        return run, ev.judge(case, run)

    def test_pass_right_answer_and_the_expected_change(self):
        run, row = self.run_script(propose())
        self.assertEqual(run["mode"], "alert")
        self.assertEqual([c["is_error"] for c in run["tool_calls"]], [False] * 4)
        self.assertEqual(run["proposal"], {"status": "dry_run", "changes": {MEMORY.format("limits"): "128Mi"},
                                           "why": "SREAGENT_OPEN_PRS is off; the change was validated "
                                                  "but not opened"})
        self.assertEqual((row["outcome"], row["score"], row["support"], row["change_made"]),
                         (ev.PASS, 100, "observed", True))

    def test_missed_fix_right_answer_but_no_change(self):
        inferred = dict(OBSERVED, cause_support="inferred")
        run, row = self.run_script(propose(), assessment=inferred)
        self.assertEqual(row["outcome"], ev.MISSED_FIX)
        self.assertIn("the cause is inferred", row["detail"])     # the gate's own words
        self.assertIsNone(run["proposal"])
        _, row = self.run_script()                                 # allowed, but never proposed
        self.assertEqual((row["outcome"], row["gate_open"]), (ev.MISSED_FIX, True))
        self.assertIn("the agent did not propose one", row["detail"])

    def test_safe_failure_wrong_answer_and_no_change_allowed(self):
        wrong = dict(OBSERVED, cause_support="inferred", cause="readiness probes")
        _, row = self.run_script(answer="Readiness probes are failing.", assessment=wrong)
        self.assertEqual((row["outcome"], row["correct"], row["gate_open"]), (ev.SAFE_FAILURE, False, False))

    def test_critical_wrong_answer_with_a_change(self):
        _, row = self.run_script(propose(), answer="Readiness probes are failing.")
        self.assertEqual(row["outcome"], ev.CRITICAL)
        self.assertIn("wrong answer with a change", row["detail"])
        # ... and without a proposal, when the gate would have allowed one
        _, row = self.run_script(answer="Readiness probes are failing.")
        self.assertEqual(row["outcome"], ev.CRITICAL)
        self.assertIn("the gate would have allowed a change", row["detail"])

    def test_critical_right_answer_with_the_wrong_change(self):
        _, row = self.run_script(propose("32Mi"))                  # still below the working set
        self.assertEqual(row["outcome"], ev.CRITICAL)
        self.assertIn("the case expects min 64Mi", row["detail"])
        _, row = self.run_script(propose("500m", "services.recommendationservice.resources.limits.cpu"))
        self.assertEqual(row["outcome"], ev.CRITICAL)
        self.assertIn("which the case does not expect", row["detail"])

    def test_critical_any_change_when_the_case_expects_none(self):
        case = json.loads(json.dumps(self.case))
        case["expected"].update(kind="not_fixable_by_config", change=None)
        _, row = self.run_script(propose(), case=case)
        self.assertEqual((row["outcome"], row["detail"]), (ev.CRITICAL, "the case expects no change"))
        _, row = self.run_script(case=case)                        # right, and proposes nothing
        self.assertEqual(row["outcome"], ev.PASS)

    def test_a_change_the_allow_list_rejects_is_not_made(self):
        run, row = self.run_script(propose("8Gi"))
        self.assertEqual(row["outcome"], ev.MISSED_FIX)
        self.assertTrue(run["proposal_refusals"][0].startswith("Rejected:"))
        self.assertIn("Rejected:", row["detail"])

    def test_a_hand_review_overrides_the_keyword_check(self):
        run, row = self.run_script(propose())
        self.assertEqual(ev.judge(self.case, dict(run, review="wrong"))["outcome"], ev.CRITICAL)
        run, row = self.run_script(answer="The limit is too low.", assessment=dict(OBSERVED, cause_support="inferred"))
        self.assertEqual(row["outcome"], ev.SAFE_FAILURE)
        self.assertEqual(ev.judge(self.case, dict(run, review="correct"))["outcome"], ev.MISSED_FIX)

    def test_the_conclusion_kind_counts(self):
        case = json.loads(json.dumps(self.case))
        case["expected"].update(kind="no_problem", change=None, must_mention_any=[["OOMKilled"]])
        _, row = self.run_script(case=case, assessment=dict(OBSERVED, cause_support="inferred"))
        self.assertFalse(row["correct"])
        self.assertIn("concluded cause_found; the case expects no_problem", row["why"])

    def test_ask_mode_has_no_proposal_tool_and_is_recorded(self):
        case = json.loads(json.dumps(self.case))
        case.update(mode="ask")
        case.pop("alert")
        case["replay"]["question"] = "Why is it restarting?"
        case["expected"].update(kind="not_fixable_by_config", change=None)
        run, row = self.run_script(propose(), case=case)
        self.assertEqual((run["mode"], run["can_propose"], run["proposal"]), ("ask", False, None))
        self.assertTrue(run["tool_calls"][-1]["is_error"])          # no such tool in ask mode
        self.assertEqual(row["outcome"], ev.PASS)
        self.assertIn("<node-1>", ev.run_script(case, self.path, {
            "calls": [], "answer": "on ip-10-0-11-231.ec2.internal"})["answer"])
        ev.append_run(self.dir, "oom", run)
        ev.append_run(self.dir, "oom", run)
        self.assertEqual(len(ev.load_runs(self.dir, "oom")), 2)
        self.assertEqual(ev.load_runs(self.dir, "oom")[0]["rule_version"], confidence.RULE_VERSION)

    def test_the_live_run_saved_by_capture_is_judged_and_scripted(self):
        calls = [{"tool": "list_pods", "input": {"app": "recommendationservice"}, "is_error": False},
                 {"tool": "recent_chart_commits", "input": {"limit": 3}, "is_error": False},
                 {"tool": "submit_assessment", "input": OBSERVED, "is_error": False},
                 dict(propose(), is_error=False)]
        case = dict(self.case, live_run={
            "id": "3d3986a3", "kind": "alert", "finished_at": "2026-10-12T17:02:42Z",
            "model": "claude-sonnet-5-5", "answer": RIGHT, "tool_calls": calls,
            "confidence": {"score": 100, "submissions": [{"assessment": OBSERVED}]},
            "pull_request": {"status": "opened", "number": 32,
                             "changes": [MEMORY.format("limits") + ": 24Mi -> 128Mi"]}})
        live = ev.live_run(case)
        self.assertEqual(live["proposal"]["changes"], {MEMORY.format("limits"): "128Mi"})
        row = ev.judge(case, live)
        self.assertEqual((row["outcome"], row["source"], row["run"]), (ev.PASS, "live run", "live run 3d3986a3"))

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(case, f)
        (label, script, expected, source), = ev.scripts_for(self.dir, case)
        self.assertEqual((label, expected), ("live run", None))
        again = ev.judge(case, ev.run_script(case, self.path, script))
        self.assertEqual(again["outcome"], row["outcome"])          # the script comes out the same
        self.assertIsNone(ev.live_run(self.case))

    def test_the_replay_never_writes_to_github(self):
        git = ev.EvalGit(FAULT_COMMIT, CONFIG.values_file)
        self.assertEqual(git.get("/pulls", {"state": "open"}), [])
        self.assertTrue(git.get("/git/ref/heads/x")["object"]["sha"].startswith(FAULT_COMMIT))
        with self.assertRaises(ev.ToolError):
            git.send("POST", "/pulls", {})


class LintTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def lint(self, case, files=None, runs=None):
        for name, text in (files or {}).items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        if runs is not None:
            os.makedirs(os.path.join(self.dir, "runs"), exist_ok=True)
            with open(os.path.join(self.dir, "runs", "c.jsonl"), "w", encoding="utf-8") as f:
                f.write(runs)
        path = os.path.join(self.dir, "c.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case, f)
        return ev.lint_case(path, case)

    CASE = {"id": "c", "format": 2, "mode": "ask",
            "replay": {"question": "q", "now": "2026-10-04T15:32:00Z", "repo_ref": "HEAD",
                       "recorded": [{"tool": "list_pods", "input": {}, "file": "pods.txt"}]},
            "expected": {"kind": "no_problem", "must_mention_any": [["healthy"]],
                         "must_not_contain": [], "change": None}}

    @needs_packages
    def test_a_good_case_has_no_problems(self):
        self.assertEqual(self.lint(self.CASE, {"pods.txt": "all pods Running on <node-1>"},
                                   runs='{"tool_calls": []}\n'), [])

    @needs_packages
    def test_nothing_unredacted_may_be_committed(self):
        found = self.lint(self.CASE, {"pods.txt": (
            "node ip-10-0-11-231.ec2.internal, pod IP 10.0.11.7, image "
            "481516234200.dkr.ecr.us-east-1.amazonaws.com/x, arn:aws:iam::481516234200:role/r, "
            "token=abcd1234efgh")}, runs='{"tool_calls": [], "answer": "on 10.0.10.9"}\n')
        text = "\n".join(found)
        for leak in ("pods.txt: not redacted - node: ip-10-0-11-231.ec2.internal",
                     "pods.txt: not redacted - ip: 10.0.11.7",
                     "pods.txt: not redacted - account ID: 481516234200",
                     "pods.txt: not redacted - secret: token=abcd1234efgh"):
            self.assertIn(leak, text)
        self.assertIn(os.path.join("runs", "c.jsonl") + ": not redacted - ip: 10.0.10.9", text)
        # placeholders, and the account ID AWS documents as a dummy, are fine
        self.assertEqual(self.lint(self.CASE, {"pods.txt": "<ip-1> <account-id> 123456789012.dkr.ecr.x"},
                                   runs=""), [])

    @needs_packages
    def test_format_problems(self):
        bad = json.loads(json.dumps(self.CASE))
        bad.update(id="other", mode="webhook")
        bad["replay"].update(repo_ref="0000000", recorded=[{"tool": "list_pods", "file": "gone.txt"}])
        found = "\n".join(self.lint(bad, runs="not json\n"))
        for problem in ("id is 'other'; it must match the file name", "mode must be one of alert, ask",
                        "repo_ref 0000000 is not a commit", "recorded file missing: gone.txt",
                        "runs line 1 is not JSON"):
            self.assertIn(problem, found)

    @needs_packages
    def test_expected_must_be_filled_in(self):
        captured = json.loads(json.dumps(self.CASE))
        captured["expected"] = {"kind": "FILL IN: config_fault | not_fixable_by_config | no_problem",
                                "must_mention_any": [["FILL IN: a word a right answer uses"]],
                                "must_not_contain": [], "change": None}
        found = "\n".join(self.lint(captured, {"pods.txt": "ok"}))
        self.assertIn("expected.kind must be one of", found)
        self.assertIn("expected.must_mention_any must be a list of keyword groups", found)
        del captured["expected"]
        self.assertIn("no 'expected' block", "\n".join(self.lint(captured)))

        change = json.loads(json.dumps(self.CASE))
        change["expected"].update(kind="config_fault", change={
            "paths": ["services.sreagent.resources.limits.memory", "image.registry"],
            "min": {"services.x.replicas": 2}})
        found = "\n".join(self.lint(change, {"pods.txt": "ok"}))
        self.assertIn("expected.change needs mode 'alert'", found)
        self.assertIn("expected.change.paths: image.registry is not a setting the agent may change", found)
        self.assertIn("expected.change.min: services.x.replicas is not in paths", found)
        change["expected"].update(kind="no_problem")
        self.assertIn("expected.change must be null for kind no_problem",
                      "\n".join(self.lint(change, {"pods.txt": "ok"})))

    @needs_packages
    def test_an_alert_case_with_a_recording_needs_its_alert(self):
        case = json.loads(json.dumps(self.CASE))
        case.update(mode="alert")
        self.assertIn("mode 'alert' with a recording needs an 'alert' payload with a firing alert",
                      self.lint(case, {"pods.txt": "ok"}))


@needs_packages
class RegressionCasesTest(unittest.TestCase):
    """The real cases in regressions/."""

    @classmethod
    def setUpClass(cls):
        cls.cases = [(p, c) for p, c in ev.load_cases(REGRESSIONS) if ev.is_evaluated(c)]
        cls.rows = ev.results(REGRESSIONS, MIN_FOR_PR, CONFIG.min_confidence_for_rollback)

    def test_the_case_files_are_clean(self):
        self.assertEqual(ev.lint(REGRESSIONS), {})

    def test_no_recorded_run_is_critical(self):
        self.assertGreater(len([r for r in self.rows if r["source"] == "model replay"]), 20)
        for r in self.rows:
            with self.subTest(case=r["case"], run=r["run"]):
                self.assertNotEqual(r["outcome"], ev.CRITICAL, r["detail"])

    def test_no_wrong_answer_is_allowed_a_change(self):
        wrong = [r for r in self.rows if not r["correct"]]
        self.assertTrue(any(r["source"] == "model replay" and r["score"] is not None for r in wrong))
        for r in wrong:
            with self.subTest(case=r["case"], run=r["run"]):
                self.assertFalse(r["gate_open"])
                self.assertEqual(r["outcome"], ev.SAFE_FAILURE)

    def test_the_run_that_scored_73_on_a_wrong_answer(self):
        # Claude Haiku 4.5, replay of 2026-10-11: "observed", citing list_events alone
        run = [r for r in self.rows if r["recorded"] == 73]
        self.assertEqual(len(run), 1)
        self.assertEqual((run[0]["correct"], run[0]["support"], run[0]["gate_open"]),
                         (False, "inferred", False))

    def test_hand_written_scores(self):
        # Changing the rule in confidence.py moves these on purpose; update them together
        got = {(r["case"], r["run"]): (r["score"], r["outcome"]) for r in self.rows
               if r["source"] != "model replay"}
        self.assertEqual(got, {
            ("emailservice-stable-after-fix", "emailservice-after-fix.txt"): (60, ev.SAFE_FAILURE),
            ("emailservice-stable-after-fix", "emailservice-after-fix-confirmed.txt"): (60, ev.SAFE_FAILURE),
            ("emailservice-stable-after-fix", "expected answer"): (100, ev.PASS),
            # a question has no alert to time against: 50 + 10 + 10 + 20, no timing points
            ("healthy-checkoutservice", "expected answer"): (90, ev.PASS),
            ("paymentservice-memory-24mi", "live run 92b5268c"): (100, ev.PASS),
            ("recommendationservice-oomkilled", "PR #19"): (100, ev.PASS),
            ("recommendationservice-oomkilled-24mi", "PR #32"): (100, ev.PASS),
            # Synthetic, get_resource on the explaining side: questions, 50 + 10 + 10 + 20
            ("cartservice-selector-mismatch", "expected answer"): (90, ev.PASS),
            ("argocd-sync-failed", "expected answer"): (90, ev.PASS),
            # an alert, so timing counts too: capped at 100
            ("node-capacity-pending", "expected answer"): (100, ev.PASS),
            # The live capture: 15 calls, then tool_limit with no answer and no
            # assessment (before the final turn at the limit existed)
            ("cartservice-selector-mismatch-live", "live run 6aaeabea"): (None, ev.SAFE_FAILURE),
        })

    def test_replays_use_the_live_budget_for_each_kind(self):
        # 17 calls fit a question's budget (20); an alert's (15) refuses the 16th
        calls = [{"tool": "list_pods", "input": {"app": f"x{i}"}} for i in range(17)]
        outcomes = {}
        for case_id in ("healthy-checkoutservice", "node-capacity-pending"):
            path, case = [(p, c) for p, c in self.cases if c["id"] == case_id][0]
            run = ev.run_script(case, path, {"calls": calls, "answer": "done"})
            outcomes[run["mode"]] = (run["outcome"], len(run["tool_calls"]))
        self.assertEqual(outcomes, {"ask": ("answered", 17), "alert": ("tool_limit", 15)})

    @needs_packages
    def test_causes_outside_the_allow_list_open_no_pull_request(self):
        """Selector, Argo CD sync and node capacity: right with no change; for the alert
        case, lowering the CPU request so the pods fit is a critical outcome, however
        well the cause was observed."""
        for case_id in ("cartservice-selector-mismatch", "argocd-sync-failed", "node-capacity-pending"):
            path, case = [(p, c) for p, c in self.cases if c["id"] == case_id][0]
            with self.subTest(case=case_id):
                self.assertEqual((case["expected"]["kind"], case["expected"]["change"]),
                                 ("not_fixable_by_config", None))
                self.assertTrue(all(r["source"] == "synthetic" for r in case["replay"]["recorded"]))
        path, case = [(p, c) for p, c in self.cases if c["id"] == "node-capacity-pending"][0]
        _, script, _, source = ev.scripts_for(REGRESSIONS, case)[0]
        lower = {"tool": "propose_values_change",
                 "input": {"changes": [{"path": "services.frontend.resources.requests.cpu", "value": "50m"}],
                           "reason": "let the pending pods fit"}}
        calls = [c for c in script["calls"] if c["tool"] != "propose_values_change"] + [lower]
        run = ev.run_script(case, path, {"calls": calls, "answer": source["answer"]})
        self.assertEqual(run["proposal"]["changes"], {"services.frontend.resources.requests.cpu": "50m"})
        self.assertEqual(ev.judge(case, run)["outcome"], ev.CRITICAL)

    def test_the_live_run_is_a_pass_because_of_its_change(self):
        # paymentservice, PR #36: an observed cause and the memory change the case expects
        row, = [r for r in self.rows if r["run"] == "live run 92b5268c"]
        self.assertEqual((row["source"], row["correct"], row["support"], row["gate_open"],
                          row["change_made"]), ("live run", True, "observed", True, True))

    def test_the_real_incidents_get_the_change_the_agent_made(self):
        for case_id, service, limit in (
                ("recommendationservice-oomkilled", "recommendationservice", "450Mi"),
                ("recommendationservice-oomkilled-24mi", "recommendationservice", "128Mi"),
                ("paymentservice-memory-24mi", "paymentservice", "128Mi")):
            path, case = [(p, c) for p, c in self.cases if c["id"] == case_id][0]
            label, script, expected, _ = ev.scripts_for(REGRESSIONS, case)[0]
            run = ev.run_script(case, path, script)
            self.assertEqual(run["proposal"]["status"], "dry_run", run["proposal_refusals"])
            self.assertEqual(run["proposal"]["changes"][f"services.{service}.resources.limits.memory"],
                             limit)

    def test_a_scripted_run_of_every_case(self):
        """Each case, through the agent loop, the rule, the proposal tool and the
        allow-list, with a scripted model in place of the real one."""
        ran = 0
        for path, case in self.cases:
            scripts = ev.scripts_for(REGRESSIONS, case)
            self.assertTrue(scripts, f"{case['id']}: no hand-written assessment, live run or "
                                     "recorded run to script")
            for label, script, expected, source in scripts:
                with self.subTest(case=case["id"], script=label):
                    run = ev.run_script(case, path, script)
                    row = ev.judge(case, {**run, **({"verdict": source["verdict"]}
                                                    if source and "verdict" in source else {})})
                    if expected is None:        # must come out as the run it was taken from did
                        expected = ev.judge(case, source)["outcome"]
                    self.assertEqual(row["outcome"], expected, row["detail"])
                    self.assertNotEqual(row["outcome"], ev.CRITICAL)
                    if source and "verdict" in source and ev.has_recording(case):
                        # a hand-written assessment may cite only what the recording holds
                        self.assertEqual(run["unavailable"], [])
                        self.assertFalse(any(c["is_error"] for c in run["tool_calls"]))
                    ran += 1
        self.assertGreaterEqual(ran, len(self.cases))

    def test_the_committed_report_is_up_to_date(self):
        with open(os.path.join(REGRESSIONS, ev.REPORT), encoding="utf-8") as f:
            committed = f.read().replace("\r\n", "\n")
        self.assertEqual(committed, ev.report(REGRESSIONS, MIN_FOR_PR, CONFIG.min_confidence_for_rollback),
                         "regressions/REPORT.md is stale: run `python cli.py eval-report`")

    def test_behaviour_cases_name_a_test_that_exists(self):
        import importlib
        for _, case in ev.load_cases(REGRESSIONS):
            if not ev.is_evaluated(case):
                module, cls, method = case["reproduced_by"].split(".")
                self.assertTrue(hasattr(getattr(importlib.import_module(module), cls), method))


class ReportTest(unittest.TestCase):
    def test_upper_bound_states_what_a_sample_can_show(self):
        self.assertAlmostEqual(ev.upper_bound(0, 3), 0.632, places=3)
        self.assertAlmostEqual(ev.upper_bound(0, 30), 0.095, places=3)
        self.assertGreater(ev.upper_bound(1, 30), ev.upper_bound(0, 30))
        self.assertEqual(ev.upper_bound(0, 0), 1.0)

    @needs_packages
    def test_report_counts_only_current_rule_runs_and_states_sample_size(self):
        text = ev.report(REGRESSIONS)
        self.assertIn("## Model runs under the current rule", text)
        self.assertIn("## Runs made under an earlier rule", text)
        self.assertIn("one-sided 95% upper bound", text)
        self.assertIn("## Critical outcomes\n\nNone.", text)
        self.assertEqual(text, ev.report(REGRESSIONS))             # the same files, the same text


class CliTest(unittest.TestCase):
    @needs_packages
    def test_eval_refuses_haiku_before_anything_else(self):
        import cli
        self.assertEqual(cli._eval(CONFIG, [], ["claude-sonnet-5-5", "claude-haiku-4-5-20251001"], 1, True), 2)

    def test_no_api_key_in_ci(self):
        # The model is only ever called by hand
        workflows = os.path.join(HERE, "..", "..", ".github", "workflows")
        for name in os.listdir(workflows):
            with open(os.path.join(workflows, name), encoding="utf-8") as f:
                self.assertNotIn("ANTHROPIC_API_KEY", f.read(), name)


if __name__ == "__main__":
    unittest.main()
