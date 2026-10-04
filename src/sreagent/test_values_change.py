"""The allow-list: proves allowed changes pass and disallowed changes are rejected.

Runs against the real chart files (helm/online-boutique/values.yaml and
values-dev.yaml), so a chart change that would weaken a rule shows up here.
"""

import os
import unittest

try:
    import values_change as vc
except ImportError:  # ruamel.yaml not installed
    vc = None

CHART = os.path.join(os.path.dirname(__file__), "..", "..", "helm", "online-boutique")
CURRENT_TAG = "v20261004-154933-68ac8238"
RECENT_TAGS = ["v20261004-134739-d34db63e", "v20260830-154023-ebc1fc93"]

DEV = f"""# Dev environment overrides

# Image tag deployed to dev. MANAGED BY CI - the "Build Images" workflow
# rewrites this line after pushing to ECR, and Argo CD deploys the commit.
# Change it by merging to main, not by editing the cluster.
image:
  tag: "{CURRENT_TAG}"

services:
  loadgenerator:
    replicas: 0
"""


@unittest.skipIf(vc is None, "ruamel.yaml not installed")
class AllowListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CHART, "values.yaml"), encoding="utf-8") as f:
            cls.base = vc.load(f.read())

    def propose(self, *pairs, old=DEV):
        changes = vc.parse_changes([{"path": p, "value": v} for p, v in pairs])
        new = vc.apply_changes(old, changes)
        return new, vc.validate(old, new, self.base, RECENT_TAGS)

    def assertRejected(self, *pairs, reason, old=DEV):
        with self.assertRaises(vc.Rejected) as ctx:
            self.propose(*pairs, old=old)
        self.assertTrue(any(reason in r for r in ctx.exception.reasons),
                        f"expected a reason containing {reason!r}, got {ctx.exception.reasons}")

    # --- allowed -------------------------------------------------------------

    def test_real_values_dev_round_trips_unchanged(self):
        with open(os.path.join(CHART, "values-dev.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(vc.apply_changes(text, []), text)

    def test_resources_allowed_and_comments_kept(self):
        new, diff = self.propose(("services.emailservice.resources.limits.memory", "768Mi"),
                                 ("services.emailservice.resources.requests.cpu", "150m"))
        self.assertEqual(vc.describe(diff), [
            "services.emailservice.resources.limits.memory: (unset) -> 768Mi",
            "services.emailservice.resources.requests.cpu: (unset) -> 150m"])
        self.assertTrue(new.startswith(DEV))  # existing text, comments and layout untouched
        self.assertIn("  emailservice:\n    resources:\n      limits:\n        memory: 768Mi\n", new)

    def test_hpa_bounds_allowed_for_service_with_hpa(self):
        _, diff = self.propose(("services.emailservice.hpa.minReplicas", 2),
                               ("services.emailservice.hpa.maxReplicas", 4))
        self.assertEqual(len(diff), 2)

    def test_replicas_allowed_for_service_without_hpa(self):
        _, diff = self.propose(("services.loadgenerator.replicas", 1))
        self.assertEqual(vc.describe(diff), ["services.loadgenerator.replicas: 0 -> 1"])

    def test_rollback_to_recent_tag(self):
        new, diff = self.propose(("image.tag", RECENT_TAGS[0]))
        self.assertIn(f'  tag: "{RECENT_TAGS[0]}"\n', new)  # quotes kept for update-manifest's sed
        self.assertEqual(len(diff), 1)

    # --- rejected: not on the allow-list ---------------------------------------

    def test_paths_outside_the_allow_list(self):
        cases = [
            ("services.cartservice.env", "x"),
            ("services.frontend.port", 9090),
            ("services.frontend.readinessProbe.timeoutSeconds", 5),
            ("services.frontend.hpa.cpuUtilization", 50),
            ("services.frontend.resources.limits.ephemeral-storage", "1Gi"),
            ("image.registry", "evil.example.com"),
            ("image.prefix", "other"),
            ("redis.addr", "attacker:6379"),
            ("ingress.enabled", "false"),
            ("services.frontend.enabled", "false"),
            ("services.frontend.noHpa", "true"),
        ]
        for pair in cases:
            with self.subTest(path=pair[0]):
                self.assertRejected(pair, reason="not on the allow-list")

    def test_unknown_or_disabled_service(self):
        self.assertRejected(("services.newservice.replicas", 1), reason="not a service in the chart")
        self.assertRejected(("services.shoppingassistantservice.resources.limits.cpu", "200m"),
                            reason="disabled in the chart")

    def test_agent_cannot_change_itself(self):
        self.assertIn("sreagent", self.base["services"])  # a real chart service now
        self.assertRejected(("services.sreagent.imageTag", RECENT_TAGS[0]),
                            reason="may not change its own settings")
        self.assertRejected(("services.sreagent.image.tag", RECENT_TAGS[0]),
                            reason="may not change its own settings")
        self.assertRejected(("services.sreagent.resources.limits.memory", "1Gi"),
                            reason="may not change its own settings")

    # --- rejected: context-aware HPA rule -------------------------------------

    def test_replicas_rejected_when_service_has_hpa(self):
        self.assertRejected(("services.emailservice.replicas", 3), reason="has an HPA")

    def test_hpa_rejected_when_service_has_no_hpa(self):
        self.assertRejected(("services.loadgenerator.hpa.maxReplicas", 3), reason="has no HPA")

    def test_hpa_min_above_max(self):
        self.assertRejected(("services.emailservice.hpa.minReplicas", 5),
                            reason="minReplicas is above maxReplicas")

    # --- rejected: values ----------------------------------------------------------

    def test_bad_values(self):
        cases = [
            (("services.emailservice.resources.limits.memory", "1Ti"), "memory quantity"),
            (("services.emailservice.resources.limits.memory", "8Gi"), "memory quantity"),
            (("services.emailservice.resources.limits.memory", 512), "memory quantity"),
            (("services.emailservice.resources.limits.cpu", "4"), "CPU quantity"),
            (("services.emailservice.resources.limits.cpu", "1m"), "CPU quantity"),
            (("services.emailservice.resources.limits.cpu", "lots"), "CPU quantity"),
            (("services.emailservice.hpa.maxReplicas", 50), "integer from 1 to 10"),
            (("services.emailservice.hpa.minReplicas", 0), "integer from 1 to 10"),
            (("services.loadgenerator.replicas", 9), "integer from 0 to 5"),
            (("services.loadgenerator.replicas", "2"), "integer from 0 to 5"),
        ]
        for pair, reason in cases:
            with self.subTest(change=pair):
                self.assertRejected(pair, reason=reason)

    def test_request_above_limit(self):
        # values.yaml: emailservice memory limit 128Mi (requests 64Mi)
        self.assertRejected(("services.emailservice.resources.requests.memory", "1Gi"),
                            reason="memory request is above its limit")

    # --- rejected: image tag rules ----------------------------------------------

    def test_tag_rules(self):
        self.assertRejected(("image.tag", "latest"), reason="is not a build tag")
        self.assertRejected(("image.tag", CURRENT_TAG), reason="does not modify anything")
        self.assertRejected(("image.tag", "v20250101-000000-deadbeef"),
                            reason="not one of the recently deployed tags")

    def test_rollback_must_be_alone(self):
        self.assertRejected(("image.tag", RECENT_TAGS[0]),
                            ("services.emailservice.resources.limits.memory", "256Mi"),
                            reason="must be the only change")

    # --- rejected: shape of the change -------------------------------------------

    def test_too_many_changes(self):
        pairs = [(f"services.{s}.resources.limits.memory", "256Mi") for s in
                 ("frontend", "cartservice", "emailservice", "adservice", "paymentservice",
                  "shippingservice", "currencyservice")]
        self.assertRejected(*pairs, reason="at most 6")

    def test_no_op(self):
        self.assertRejected(("services.loadgenerator.replicas", 0), reason="does not modify anything")

    def test_malformed_input(self):
        for raw in ([], "x", [{"path": "image.tag"}], [{"path": 1, "value": "x"}],
                    [{"path": "image.tag", "value": True}], [{"path": "a", "value": {"b": 1}}]):
            with self.subTest(raw=raw), self.assertRaises(vc.Rejected):
                vc.parse_changes(raw)

    # --- the validator checks the resulting text, not just the proposal ------------

    def test_validator_catches_edits_made_outside_apply_changes(self):
        def check(new_text):
            with self.assertRaises(vc.Rejected) as ctx:
                vc.validate(DEV, new_text, self.base, RECENT_TAGS)
            return ctx.exception.reasons

        # A removed key, a dropped comment, and a second "  tag:" line are all
        # caught from the text itself.
        self.assertTrue(any("removing an existing setting" in r
                            for r in check(DEV.replace("  loadgenerator:\n    replicas: 0\n", "  {}\n"))))
        self.assertTrue(any("drop comments" in r for r in check(
            DEV.replace("# Dev environment overrides\n", "") + "  emailservice:\n    replicas: 1\n")))
        self.assertTrue(any("exactly one '  tag:' line" in r for r in check(
            DEV + "  tag: x\n")))


if __name__ == "__main__":
    unittest.main()
