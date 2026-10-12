"""The scoring rule against the regression cases.

Hand-written assessments are deterministic, so they can gate the rule: every
wrong answer must score below the default PR threshold and every correct one
at or above it.

Model replays (recorded by `cli.py replay --record`) vary from run to run, so
their scores are reported by `cli.py scores`, not asserted here. What is
asserted is the gate: re-scored with the current rule, no recorded run that
failed its keyword check may be allowed to propose a change.
"""

import os
import unittest

import replay
from config import Config

REGRESSIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regressions")
MIN_FOR_PR = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns"}).min_confidence_for_pr


class HandWrittenScoresTest(unittest.TestCase):
    def setUp(self):
        self.rows = [r for r in replay.score_table(REGRESSIONS) if r["source"] == "hand-written"]

    def test_there_is_something_to_check(self):
        self.assertTrue(any(r["verdict"] == "wrong" for r in self.rows))
        self.assertTrue(any(r["verdict"] == "correct" for r in self.rows))

    def test_wrong_answers_score_below_the_pr_threshold(self):
        for r in self.rows:
            if r["verdict"] == "wrong":
                with self.subTest(case=r["case"], run=r["run"]):
                    self.assertLess(r["score"], MIN_FOR_PR)

    def test_correct_answers_reach_the_pr_threshold(self):
        for r in self.rows:
            if r["verdict"] == "correct":
                with self.subTest(case=r["case"], run=r["run"]):
                    self.assertGreaterEqual(r["score"], MIN_FOR_PR)

    def test_known_scores(self):
        # Changing the rule in confidence.py moves these on purpose; update them
        # together, and check the separation tests above still hold.
        got = {(r["case"], r["run"]): r["score"] for r in self.rows}
        self.assertEqual(got, {
            ("emailservice-stable-after-fix", "emailservice-after-fix.txt"): 60,
            ("emailservice-stable-after-fix", "emailservice-after-fix-confirmed.txt"): 60,
            ("emailservice-stable-after-fix", "expected answer"): 100,
            ("recommendationservice-oomkilled", "PR #19"): 100,
        })


class NoWrongAnswerProposesAChangeTest(unittest.TestCase):
    def setUp(self):
        self.rows = replay.score_table(REGRESSIONS, MIN_FOR_PR)

    def test_wrong_answers_are_never_allowed_a_change(self):
        wrong = [r for r in self.rows if r["verdict"] in ("wrong", "keyword check fails")]
        self.assertTrue(any(r["source"] == "model replay" and r["score"] is not None for r in wrong))
        for r in wrong:
            with self.subTest(case=r["case"], run=r["run"]):
                self.assertFalse(r["change_allowed"])

    def test_the_run_that_scored_73_on_a_wrong_answer(self):
        # Claude Haiku 4.5, replay of 2026-10-11: "observed", citing list_events alone
        run = [r for r in self.rows if r["recorded"] == 73]
        self.assertEqual(len(run), 1)
        self.assertEqual((run[0]["verdict"], run[0]["support"], run[0]["change_allowed"]),
                         ("keyword check fails", "inferred", False))

    def test_the_correct_incident_still_gets_its_change(self):
        pr19 = [r for r in self.rows if r["run"] == "PR #19"][0]
        self.assertEqual((pr19["support"], pr19["change_allowed"]), ("observed", True))


if __name__ == "__main__":
    unittest.main()
