"""Tests for the evidence score (confidence.py) and the submit_assessment tool."""

import unittest

import confidence as c
from agent import Evidence
from tools import ToolError


def calls(*names, errors=()):
    return [Evidence(n, {}, "output", i + 1 in errors) for i, n in enumerate(names)]


# PR #19's eight tool calls, in order (the incident's real investigation)
PR19_CALLS = calls("list_pods", "list_deployments", "recent_chart_commits", "list_events",
                   "prometheus_query_range", "read_repo_file", "read_repo_file",
                   "propose_values_change")

PR19_ASSESSMENT = {
    "conclusion": "cause_found",
    "cause": "Commit b3980348 set the memory limit to 40Mi, below the ~43 MB steady working set",
    "cause_support": "observed",
    "cause_evidence": [1, 3, 5],
    "alternatives": [
        {"cause": "memory growth or a leak in the service", "result": "ruled_out", "evidence": [5]},
        {"cause": "a new image", "result": "ruled_out", "evidence": [2]},
    ],
    "timing": "matches",
    "timing_evidence": [3],
    "unverified": [{"what": "logs from the killed container", "could_change_diagnosis": False}],
}


def assessment(**overrides):
    a = {"conclusion": "cause_found", "cause": "x", "cause_support": "observed",
         "cause_evidence": [1], "alternatives": [], "timing": "not_checked",
         "timing_evidence": [], "unverified": []}
    a.update(overrides)
    return a


class ScoreTest(unittest.TestCase):
    def test_pr19_worked_example(self):
        s = c.score(PR19_ASSESSMENT, PR19_CALLS)
        self.assertEqual(s.value, 98)
        self.assertEqual([(r.points, r.text) for r in s.reasons], [
            (50, "Cause directly observed: list_pods (#1), recent_chart_commits (#3), "
                 "prometheus_query_range (#5)"),
            (10, "Corroborated by 3 different tools"),
            (10, "Ruled out: memory growth or a leak in the service (prometheus_query_range (#5))"),
            (10, "Ruled out: a new image (list_deployments (#2))"),
            (20, "Timing matches the alert (recent_chart_commits (#3))"),
            (-2, "Not verified (detail only): logs from the killed container"),
        ])

    def test_same_assessment_same_score(self):
        reordered = dict(reversed(list(PR19_ASSESSMENT.items())))
        scores = {c.score(PR19_ASSESSMENT, PR19_CALLS).value for _ in range(5)}
        scores.add(c.score(reordered, PR19_CALLS).value)
        self.assertEqual(scores, {98})

    def test_observed_needs_a_valid_citation(self):
        ev = calls("list_pods", "list_events", errors=(2,))
        for cited, reason in (([9], "#9 does not exist"), ([2], "#2 (list_events) failed"), ([], None)):
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
        self.assertIn("#2 is an assessment, not evidence", s.reasons[-1].text)

    def test_corroboration_needs_different_tools(self):
        ev = calls("read_repo_file", "read_repo_file", "list_pods")
        same = c.score(assessment(cause_evidence=[1, 2]), ev)
        diff = c.score(assessment(cause_evidence=[1, 3]), ev)
        self.assertFalse(any("Corroborated" in r.text for r in same.reasons))
        self.assertTrue(any("Corroborated by 2" in r.text for r in diff.reasons))

    def test_cap_without_an_alternative_ruled_out(self):
        ev = calls("list_pods", "list_events", "recent_chart_commits")
        a = assessment(cause_evidence=[1, 2], timing="matches", timing_evidence=[3])
        s = c.score(a, ev)                       # 50 + 10 + 20 = 80 before the cap
        self.assertEqual(s.value, c.CAP_NO_ALTERNATIVE_RULED_OUT)
        self.assertEqual(s.reasons[-1].points, c.CAP_NO_ALTERNATIVE_RULED_OUT - 80)
        self.assertIn("no alternative explanation was ruled out", s.reasons[-1].text)
        # a ruled-out claim without a citation does not lift the cap
        a["alternatives"] = [{"cause": "y", "result": "ruled_out", "evidence": []}]
        self.assertEqual(c.score(a, ev).value, c.CAP_NO_ALTERNATIVE_RULED_OUT)

    def test_penalties(self):
        ev = calls("list_pods", "list_events", "list_hpas")
        base = dict(cause_evidence=[1],
                    alternatives=[{"cause": "a", "result": "ruled_out", "evidence": [2]},
                                  {"cause": "b", "result": "ruled_out", "evidence": [3]}])
        self.assertEqual(c.score(assessment(**base), ev).value, 70)
        self.assertEqual(c.score(assessment(**base, timing="does_not_match"), ev).value, 50)
        open_alt = dict(base, alternatives=base["alternatives"] + [
            {"cause": "c", "result": "not_ruled_out", "evidence": []}])
        self.assertEqual(c.score(assessment(**open_alt), ev).value, 55)
        major = [{"what": "w", "could_change_diagnosis": True}]
        self.assertEqual(c.score(assessment(**base, unverified=major), ev).value, 55)
        minor = [{"what": f"d{i}", "could_change_diagnosis": False} for i in range(5)]
        self.assertEqual(c.score(assessment(**base, unverified=minor), ev).value, 64)  # -6 at most

    def test_only_two_ruled_out_count(self):
        ev = calls("a", "b", "c", "d")
        alts = [{"cause": n, "result": "ruled_out", "evidence": [i]} for i, n in ((2, "x"), (3, "y"), (4, "z"))]
        s = c.score(assessment(alternatives=alts), ev)
        self.assertEqual(s.value, 70)
        self.assertTrue(any("beyond the 2 that count" in r.text for r in s.reasons))

    def test_inconclusive_and_empty_cause(self):
        self.assertEqual(c.score(assessment(conclusion="inconclusive"), calls("a")).value, 20)
        s = c.score(assessment(cause=""), calls("a"))
        self.assertEqual(s.value, c.INCONCLUSIVE_SCORE)
        self.assertIn("treated as inconclusive", s.reasons[0].text)

    def test_no_problem_is_scored_like_a_cause(self):
        ev = calls("describe_deployment", "list_events", "recent_chart_commits")
        a = assessment(conclusion="no_problem", cause="the cycle has stopped",
                       cause_evidence=[1, 2],
                       alternatives=[{"cause": "the cycle continues", "result": "ruled_out", "evidence": [2]}],
                       timing="matches", timing_evidence=[3])
        s = c.score(a, ev)
        self.assertEqual(s.value, 90)
        self.assertTrue(s.reasons[0].text.startswith("No problem directly observed"))

    def test_garbage_input_never_raises(self):
        for raw in (None, "text", [], {"conclusion": "maybe", "cause_evidence": "1,2",
                                        "alternatives": "none", "unverified": [1, None, {}],
                                        "timing": True}):
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

    def test_score_is_clamped(self):
        ev = calls("a")
        a = assessment(cause_support="inferred", timing="does_not_match",
                       alternatives=[{"cause": str(i), "result": "not_ruled_out", "evidence": []}
                                     for i in range(5)])
        self.assertEqual(c.score(a, ev).value, 0)


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
        out = self.submit(assessment(cause_evidence=[1, 2]))
        self.assertTrue(out.startswith("Recorded. Evidence score: 55/100."))
        self.assertIn("Below 70: do not propose a configuration change", out)
        self.assertEqual(self.tool.latest.value, 55)
        self.assertTrue(self.tool.tool().strict)

    def test_one_revision_only_after_new_evidence(self):
        self.submit(assessment())
        with self.assertRaisesRegex(ToolError, "only be revised after new tool calls"):
            self.tool.handle(assessment())
        self.evidence.append(Evidence("list_hpas", {}, "ok", False))
        out = self.submit(assessment(
            cause_evidence=[1, 2],
            alternatives=[{"cause": "a", "result": "ruled_out", "evidence": [4]},
                          {"cause": "b", "result": "ruled_out", "evidence": [2]}]))
        self.assertTrue(out.startswith("Recorded (revision). Evidence score: 80/100."))
        self.assertIn("may be proposed", out)
        self.evidence.append(Evidence("list_deployments", {}, "ok", False))
        with self.assertRaisesRegex(ToolError, "already revised once"):
            self.tool.handle(assessment())

        summary = self.tool.summary()
        self.assertEqual(summary["score"], 80)
        self.assertEqual([(s["after_calls"], s["score"]) for s in summary["submissions"]], [(2, 50), (3, 80)])


if __name__ == "__main__":
    unittest.main()
