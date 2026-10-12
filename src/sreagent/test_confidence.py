"""Tests for the evidence score (confidence.py) and the submit_assessment tool."""

import unittest

import confidence as c
from agent import Evidence
from tools import ToolError


def calls(*names, errors=()):
    return [Evidence(n, {}, "output", i + 1 in errors) for i, n in enumerate(names)]


def check(what="the check", *evidence):
    return {"what": what, "evidence": list(evidence)}


# PR #19's eight tool calls, in order (the incident's real investigation)
PR19_CALLS = calls("list_pods", "list_deployments", "recent_chart_commits", "list_events",
                   "prometheus_query_range", "read_repo_file", "read_repo_file",
                   "propose_values_change")

PR19_ASSESSMENT = {
    "conclusion": "cause_found",
    "cause": "Commit b3980348 set the memory limit to 40Mi, below the ~43 MB steady working set",
    "cause_support": "observed",
    "cause_evidence": [1, 3, 5],
    "decisive_check": check("the limit was cut to 40Mi while the working set stayed at 43 MB", 3, 5),
    "alternatives": [
        {"cause": "memory growth or a leak in the service", "result": "ruled_out", "evidence": [5]},
        {"cause": "a new image", "result": "ruled_out", "evidence": [2]},
    ],
    "timing": "matches",
    "timing_evidence": [3],
    "unverified": [{"what": "logs from the killed container", "could_change_diagnosis": False}],
}

# The replay of 2026-10-11 that scored 73 on a wrong answer: Claude Haiku 4.5
# called readiness probes an observed cause, citing only list_events
HAIKU_CALLS = calls("list_events", "list_pods", "describe_deployment", errors=(2, 3))
HAIKU_ASSESSMENT = {
    "conclusion": "cause_found",
    "cause": "Emailservice pods are repeatedly replaced because they fail readiness probes",
    "cause_support": "observed",
    "cause_evidence": [1],
    "alternatives": [{"cause": n, "result": "ruled_out", "evidence": [1]}
                     for n in ("Image pull failure", "Node failure or eviction",
                               "OOMKilled or resource limits", "Liveness probe failure")],
    "timing": "matches",
    "timing_evidence": [1],
    "unverified": [{"what": "Why the service is failing readiness checks",
                    "could_change_diagnosis": True}],
}


def assessment(**overrides):
    a = {"conclusion": "cause_found", "cause": "x", "cause_support": "observed",
         "cause_evidence": [1], "decisive_check": check(""), "alternatives": [],
         "timing": "not_checked", "timing_evidence": [], "unverified": []}
    a.update(overrides)
    return a


class ScoreTest(unittest.TestCase):
    def test_pr19_worked_example(self):
        s = c.score(PR19_ASSESSMENT, PR19_CALLS)
        self.assertEqual(s.value, 100)
        self.assertTrue(s.observed)
        self.assertEqual([(r.points, r.text) for r in s.reasons], [
            (50, "Cause directly observed: list_pods (call 1), recent_chart_commits (call 3), "
                 "prometheus_query_range (call 5)"),
            (10, "Corroborated by 3 different tools"),
            (10, "Decisive check: the limit was cut to 40Mi while the working set stayed at 43 MB "
                 "(recent_chart_commits (call 3), prometheus_query_range (call 5))"),
            (10, "Ruled out: memory growth or a leak in the service (prometheus_query_range (call 5))"),
            (10, "Ruled out: a new image (list_deployments (call 2))"),
            (20, "Timing matches the alert (recent_chart_commits (call 3))"),
            (-10, "The score stops at 100"),
        ])
        self.assertEqual(s.open_questions, ["Not verified (detail only): logs from the killed container"])

    def test_same_assessment_same_score(self):
        reordered = dict(reversed(list(PR19_ASSESSMENT.items())))
        scores = {c.score(PR19_ASSESSMENT, PR19_CALLS).value for _ in range(5)}
        scores.add(c.score(reordered, PR19_CALLS).value)
        self.assertEqual(scores, {100})

    def test_observed_needs_a_valid_citation(self):
        ev = calls("list_pods", "list_events", errors=(2,))
        for cited, reason in (([9], "call 9 does not exist"), ([2], "call 2 (list_events) failed"), ([], None)):
            with self.subTest(cited=cited):
                s = c.score(assessment(cause_evidence=cited), ev)
                self.assertEqual(s.reasons[0].points, c.POINTS["cause_inferred"])
                self.assertIn("claimed observed, but no valid citation", s.reasons[0].text)
                if reason:
                    self.assertIn(reason, s.reasons[-1].text)

    def test_citing_an_assessment_is_not_evidence(self):
        ev = calls("list_pods", c.TOOL_NAME)
        s = c.score(assessment(cause_evidence=[2]), ev)
        self.assertEqual(s.reasons[0].points, c.POINTS["cause_inferred"])
        self.assertIn("call 2 is an assessment, not evidence", s.reasons[-1].text)

    def test_corroboration_needs_different_tools(self):
        ev = calls("read_repo_file", "read_repo_file", "list_pods")
        same = c.score(assessment(cause_evidence=[1, 2]), ev)
        diff = c.score(assessment(cause_evidence=[1, 3]), ev)
        self.assertFalse(any("Corroborated" in r.text for r in same.reasons))
        self.assertTrue(any("Corroborated by 2" in r.text for r in diff.reasons))

    def test_decisive_check_needs_a_valid_citation(self):
        ev = calls("list_pods", "describe_deployment", errors=(2,))
        cited = c.score(assessment(cause_support="inferred", decisive_check=check("limit 40Mi", 1)), ev)
        failed = c.score(assessment(cause_support="inferred", decisive_check=check("limit 40Mi", 2)), ev)
        none = c.score(assessment(cause_support="inferred"), ev)
        self.assertEqual((cited.value, failed.value, none.value), (40, 30, 30))
        self.assertIn((10, "Decisive check: limit 40Mi (list_pods (call 1))"),
                      [(r.points, r.text) for r in cited.reasons])
        self.assertIn("Decisive check claimed without a valid citation: limit 40Mi",
                      [r.text for r in failed.reasons])
        self.assertIn("No decisive check named", [r.text for r in none.reasons])

    def test_decisive_check_text_is_not_cut_at_the_short_limit(self):
        # PR #32's was 242 characters; a longer one was cut mid-sentence at 300
        long = "The healthy pods use 43MB while the limit is 24Mi. " * 10        # 510 characters
        s = c.score(assessment(decisive_check=check(long, 1)), calls("list_deployments"))
        self.assertEqual(s.assessment["decisive_check"]["what"], long.strip())
        self.assertIn(long.strip(), s.reasons[1].text)
        s = c.score(assessment(decisive_check=check("x" * 2000, 1)), calls("list_deployments"))
        self.assertEqual(len(s.assessment["decisive_check"]["what"]), c.MAX_DECISIVE_TEXT)

    def test_cap_without_an_alternative_ruled_out(self):
        ev = calls("list_pods", "list_events", "recent_chart_commits")
        a = assessment(cause_evidence=[1, 2], decisive_check=check("the commit", 3),
                       timing="matches", timing_evidence=[3])
        s = c.score(a, ev)                       # 50 + 10 + 10 + 20 = 90 before the cap
        self.assertEqual(s.value, c.CAP_NO_ALTERNATIVE_RULED_OUT)
        self.assertEqual(s.reasons[-1].points, c.CAP_NO_ALTERNATIVE_RULED_OUT - 90)
        self.assertIn("no alternative explanation was ruled out", s.reasons[-1].text)
        # a ruled-out claim without a citation does not lift the cap
        a["alternatives"] = [{"cause": "y", "result": "ruled_out", "evidence": []}]
        self.assertEqual(c.score(a, ev).value, c.CAP_NO_ALTERNATIVE_RULED_OUT)

    def test_inferred_cause_is_capped(self):
        ev = calls("list_pods", "list_events", "recent_chart_commits")
        a = assessment(cause_support="inferred", cause_evidence=[1, 3],
                       decisive_check=check("the commit", 3),
                       alternatives=[{"cause": "a", "result": "ruled_out", "evidence": [2]},
                                     {"cause": "b", "result": "ruled_out", "evidence": [3]}],
                       timing="matches", timing_evidence=[3])
        s = c.score(a, ev)                       # 30 + 10 + 10 + 20 + 20 = 90 before the cap
        self.assertEqual(s.value, c.CAP_INFERRED)
        self.assertLess(c.CAP_INFERRED, 70)      # below the default PR threshold
        self.assertEqual((s.reasons[-1].points, s.reasons[-1].text),
                         (-30, "Capped at 60: the cause is inferred, not observed"))

    def test_doubts_are_listed_not_subtracted(self):
        ev = calls("list_pods", "list_events", "list_hpas")
        base = dict(cause_evidence=[1], decisive_check=check("hpa minimum", 3),
                    alternatives=[{"cause": "a", "result": "ruled_out", "evidence": [2]},
                                  {"cause": "b", "result": "ruled_out", "evidence": [3]}])
        plain = c.score(assessment(**base), ev)
        candid = c.score(assessment(**dict(
            base,
            alternatives=base["alternatives"] + [{"cause": "c", "result": "not_ruled_out", "evidence": []}],
            unverified=[{"what": "w", "could_change_diagnosis": True},
                        {"what": "d", "could_change_diagnosis": False}])), ev)
        self.assertEqual(plain.value, 80)        # 50 + 10 + 10 + 10
        self.assertEqual(candid.value, plain.value)
        self.assertEqual(plain.open_questions, [])
        self.assertEqual(candid.open_questions, [
            "Not ruled out: c",
            "Not verified, could change the diagnosis: w",
            "Not verified (detail only): d"])
        self.assertFalse(any(r.points < 0 for r in candid.reasons))

    def test_timing_mismatch_still_counts_against(self):
        ev = calls("list_pods", "list_events", "list_hpas")
        s = c.score(assessment(cause_support="inferred", timing="does_not_match"), ev)
        self.assertEqual(s.value, 10)
        self.assertIn((-20, "Timing does not match the alert"), [(r.points, r.text) for r in s.reasons])

    def test_failed_calls_are_listed_by_the_code(self):
        s = c.score(assessment(), HAIKU_CALLS)
        self.assertEqual(s.open_questions,
                         ["Calls that failed or returned nothing: list_pods (call 2), describe_deployment (call 3)"])
        inconclusive = c.score(assessment(conclusion="inconclusive"), HAIKU_CALLS)
        self.assertEqual(inconclusive.open_questions, s.open_questions)

    def test_only_two_ruled_out_count(self):
        ev = calls("a", "b", "c", "d")
        alts = [{"cause": n, "result": "ruled_out", "evidence": [i]} for i, n in ((2, "x"), (3, "y"), (4, "z"))]
        s = c.score(assessment(cause_support="inferred", alternatives=alts), ev)
        self.assertEqual(s.value, 50)
        self.assertTrue(any("beyond the 2 that count" in r.text for r in s.reasons))

    def test_inconclusive_and_empty_cause(self):
        self.assertEqual(c.score(assessment(conclusion="inconclusive"), calls("a")).value, 20)
        s = c.score(assessment(cause=""), calls("a"))
        self.assertEqual(s.value, c.INCONCLUSIVE_SCORE)
        self.assertIn("treated as inconclusive", s.reasons[0].text)

    def test_no_problem_is_scored_like_a_cause(self):
        ev = calls("describe_deployment", "list_events", "recent_chart_commits")
        a = assessment(conclusion="no_problem", cause="the cycle has stopped",
                       cause_evidence=[1, 2], decisive_check=check("replicas not in the manifest", 1),
                       alternatives=[{"cause": "the cycle continues", "result": "ruled_out", "evidence": [2]}],
                       timing="matches", timing_evidence=[3])
        s = c.score(a, ev)
        self.assertEqual(s.value, 100)
        self.assertEqual(s.support, "observed")
        self.assertTrue(s.reasons[0].text.startswith("No problem directly observed"))

    def test_garbage_input_never_raises(self):
        for raw in (None, "text", [], {"conclusion": "maybe", "cause_evidence": "1,2",
                                        "alternatives": "none", "unverified": [1, None, {}],
                                        "decisive_check": "yes", "timing": True}):
            with self.subTest(raw=str(raw)[:30]):
                s = c.score(raw, calls("a"))
                self.assertEqual(s.value, c.INCONCLUSIVE_SCORE)

    def test_limits_enforced_in_code(self):
        a = assessment(cause="x" * 1000,
                       unverified=[{"what": str(i), "could_change_diagnosis": False} for i in range(9)])
        s = c.score(a, calls("a"))
        self.assertEqual(len(s.assessment["cause"]), c.MAX_TEXT)
        self.assertEqual(len(s.assessment["unverified"]), c.MAX_ITEMS)
        self.assertTrue(any("only the first 5 'unverified'" in r.text for r in s.reasons))


class ObservedRuleTest(unittest.TestCase):
    """A cause is observed only with both ends cited: the failure in the cluster,
    and a decisive check from the side that explains it."""

    EV = calls("list_events", "list_pods", "read_repo_file", "describe_deployment")

    def test_both_ends_make_an_observed_cause(self):
        s = c.score(assessment(cause_evidence=[2], decisive_check=check("limit 40Mi", 3)), self.EV)
        self.assertTrue(s.observed)
        self.assertEqual(s.not_observed, "")

    def test_the_haiku_run_that_scored_73_is_not_observed(self):
        s = c.score(HAIKU_ASSESSMENT, HAIKU_CALLS)      # as recorded: no decisive check
        self.assertFalse(s.observed)
        self.assertEqual(s.not_observed, "no decisive check with a valid citation")
        self.assertEqual(s.value, c.CAP_INFERRED)       # 30 + 10 + 10 + 20 = 70 before the cap
        self.assertIn("does not count as observed", c.change_blocked(s, 0))

    def test_a_decisive_check_from_the_pods_side_is_a_symptom(self):
        # the same run, had the field existed: its only successful call was list_events
        a = dict(HAIKU_ASSESSMENT, decisive_check=check("Readiness probe failed events", 1))
        s = c.score(a, HAIKU_CALLS)
        self.assertFalse(s.observed)
        self.assertEqual(s.not_observed,
                         "the decisive check cites only list_events (call 1), which shows what happened "
                         "to the pods, not the setting or change that explains it")
        self.assertEqual(s.value, c.CAP_INFERRED)
        self.assertTrue(s.reasons[0].text.startswith("Cause inferred (claimed observed, but the decisive"))
        self.assertNotEqual(c.change_blocked(s, 0), "")

    def test_every_pods_side_tool_is_a_symptom(self):
        for tool in ("list_pods", "list_events", "get_pod_logs", "prometheus_query",
                     "prometheus_query_range"):
            with self.subTest(tool=tool):
                ev = calls("list_events", tool)
                s = c.score(assessment(cause_evidence=[1, 2], decisive_check=check("x", 2)), ev)
                self.assertFalse(s.observed)

    def test_the_explaining_side_alone_is_not_observed_either(self):
        s = c.score(assessment(cause_evidence=[3], decisive_check=check("replicas: 2", 3, 4)), self.EV)
        self.assertFalse(s.observed)
        self.assertEqual(s.not_observed,
                         "nothing cited shows the failure in the cluster (pods, events, logs or metrics)")

    def test_a_failed_decisive_call_does_not_count(self):
        ev = calls("list_pods", "describe_deployment", errors=(2,))
        s = c.score(assessment(decisive_check=check("field managers", 2)), ev)
        self.assertFalse(s.observed)
        self.assertEqual(s.not_observed, "no decisive check with a valid citation")

    def test_an_assessment_recorded_before_the_field_existed(self):
        old = {k: v for k, v in PR19_ASSESSMENT.items() if k != "decisive_check"}
        s = c.score(old, PR19_CALLS)
        self.assertFalse(s.observed)
        self.assertEqual(s.value, c.CAP_INFERRED)


class ChangeBlockedTest(unittest.TestCase):
    """The rule for proposing a change: an observed cause AND the threshold."""

    EV = calls("list_pods", "list_events", "recent_chart_commits")
    ALTS = [{"cause": "a", "result": "ruled_out", "evidence": [2]},
            {"cause": "b", "result": "ruled_out", "evidence": [3]}]
    CHECK = check("the commit that cut the limit", 3)

    def test_observed_and_above_threshold_is_allowed(self):
        s = c.score(assessment(cause_evidence=[1, 3], decisive_check=self.CHECK, alternatives=self.ALTS,
                               timing="matches", timing_evidence=[3]), self.EV)
        self.assertTrue(s.observed)
        self.assertEqual(s.value, 100)
        self.assertEqual(c.change_blocked(s, 70), "")

    def test_inferred_cause_never_allows_a_change_whatever_the_threshold(self):
        s = c.score(assessment(cause_support="inferred", cause_evidence=[1, 3],
                               decisive_check=self.CHECK, alternatives=self.ALTS,
                               timing="matches", timing_evidence=[3]), self.EV)
        self.assertEqual(s.value, 60)
        self.assertFalse(s.observed)
        self.assertIn("the cause is inferred, not directly observed", c.change_blocked(s, 0))

    def test_observed_without_valid_citation_is_not_observed(self):
        s = c.score(assessment(cause_evidence=[9], decisive_check=self.CHECK, alternatives=self.ALTS),
                    self.EV)
        self.assertFalse(s.observed)
        self.assertEqual(c.change_blocked(s, 0),
                         "the cause does not count as observed (no valid citation) - "
                         "a change needs an observed cause")

    def test_no_problem_is_not_a_cause_to_fix(self):
        s = c.score(assessment(conclusion="no_problem", cause="stopped", cause_evidence=[1],
                               decisive_check=self.CHECK, alternatives=self.ALTS), self.EV)
        self.assertFalse(s.observed)
        self.assertIn("not a cause that a change could fix", c.change_blocked(s, 0))

    def test_threshold_still_applies(self):
        s = c.score(assessment(cause_evidence=[1], decisive_check=self.CHECK), self.EV)  # 60, capped at 55
        self.assertTrue(s.observed)
        self.assertIn("the evidence score is 55/100, below the 70 needed", c.change_blocked(s, 70))
        self.assertEqual(c.change_blocked(s, 55), "")

    def test_not_assessed(self):
        self.assertIn("no evidence assessment was submitted", c.change_blocked(None, 70))
        self.assertEqual(c.display(None), "not assessed")
        self.assertEqual(c.display(c.score(assessment(cause_support="inferred"), self.EV)), "30/100")


class StrictSchemaTest(unittest.TestCase):
    UNSUPPORTED = {"minLength", "maxLength", "minimum", "maximum", "multipleOf", "minItems", "maxItems"}

    def walk(self, node, path="schema"):
        if isinstance(node, dict):
            self.assertFalse(self.UNSUPPORTED & node.keys(), f"{path} uses an unsupported keyword")
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False, path)
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]), path)
            for k, v in node.items():
                self.walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self.walk(v, f"{path}[{i}]")

    def test_schema_is_valid_for_strict_tool_use(self):
        self.walk(c.SCHEMA)
        self.assertIn("decisive_check", c.SCHEMA["required"])

    def test_every_real_tool_is_on_one_side_of_the_observed_rule(self):
        # A new evidence tool must be placed, or it silently counts for neither side
        import github_tools
        import k8s_tools
        import prometheus_tools
        import replay
        names = {t.name for t in (
            prometheus_tools.make_tools(prometheus_tools.PrometheusClient("http://test.invalid"))
            + k8s_tools.make_tools(k8s_tools.KubeReader("ns", core=object()))
            + github_tools.make_tools(replay.LocalGit("HEAD", "helm/online-boutique/values-dev.yaml")))}
        self.assertEqual(names, c.WORKLOAD_TOOLS | c.EXPLAINING_TOOLS)
        self.assertFalse(c.WORKLOAD_TOOLS & c.EXPLAINING_TOOLS)


class AssessmentToolTest(unittest.TestCase):
    def setUp(self):
        self.evidence = calls("list_pods", "list_events")
        self.tool = c.AssessmentTool(min_for_pr=70, min_for_rollback=85)
        self.tool.attach(lambda: self.evidence)

    def submit(self, a):
        out = self.tool.handle(a)
        self.evidence.append(Evidence(c.TOOL_NAME, a, out, False))   # as the agent loop records it
        return out

    def test_records_and_reports_the_score(self):
        out = self.submit(assessment(cause_evidence=[1, 2], unverified=[
            {"what": "the chart values", "could_change_diagnosis": True}]))
        self.assertTrue(out.startswith("Recorded. Evidence score: 40/100."))
        self.assertIn("Open questions (not scored):\n   ?  Not verified, could change the diagnosis: "
                      "the chart values", out)
        self.assertIn("Do not propose a configuration change: the cause does not count as observed "
                      "(no decisive check with a valid citation)", out)
        self.assertEqual(self.tool.latest.value, 40)
        self.assertEqual(self.tool.summary()["open_questions"],
                         ["Not verified, could change the diagnosis: the chart values"])
        self.assertTrue(self.tool.tool().strict)

    def test_one_revision_only_after_new_evidence(self):
        self.submit(assessment())
        with self.assertRaisesRegex(ToolError, "only be revised after new tool calls"):
            self.tool.handle(assessment())
        self.evidence.append(Evidence("list_hpas", {}, "ok", False))
        out = self.submit(assessment(
            cause_evidence=[1, 2], decisive_check=check("hpa minimum is 1", 4),
            alternatives=[{"cause": "a", "result": "ruled_out", "evidence": [4]},
                          {"cause": "b", "result": "ruled_out", "evidence": [2]}]))
        self.assertTrue(out.startswith("Recorded (revision). Evidence score: 90/100."))
        self.assertIn("may be proposed", out)
        self.evidence.append(Evidence("list_deployments", {}, "ok", False))
        with self.assertRaisesRegex(ToolError, "already revised once"):
            self.tool.handle(assessment())

        summary = self.tool.summary()
        self.assertEqual(summary["score"], 90)
        self.assertEqual([(s["after_calls"], s["score"]) for s in summary["submissions"]], [(2, 30), (3, 90)])


if __name__ == "__main__":
    unittest.main()
