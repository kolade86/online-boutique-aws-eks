"""The scoring rule against the regression cases.

Hand-written assessments are deterministic, so they can gate the rule: every
wrong answer must score below the default PR threshold and every correct one
at or above it. Model replays (recorded by `cli.py replay --record`) vary from
run to run; they are reported by `cli.py scores`, not asserted here.
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
            ("emailservice-stable-after-fix", "emailservice-after-fix.txt"): 5,
            ("emailservice-stable-after-fix", "emailservice-after-fix-confirmed.txt"): 65,
            ("emailservice-stable-after-fix", "expected answer"): 85,
            ("recommendationservice-oomkilled", "PR #19"): 98,
        })


if __name__ == "__main__":
    unittest.main()
