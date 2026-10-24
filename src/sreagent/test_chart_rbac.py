"""The agent's RBAC as the chart renders it (templates/rbac.yaml): read-only,
no Secrets, and each grant exactly where get_resource reads.

Needs helm on PATH: CI installs a pinned version (unit-test.yml), and the
test is skipped without it, which CI counts as a failure.
"""

import os
import shutil
import subprocess
import unittest

import yaml

import k8s_tools

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "helm", "online-boutique")
NAMESPACE = "online-boutique-dev"
READ_VERBS = {"get", "list", "watch"}
RBAC_KINDS = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


@unittest.skipIf(shutil.which("helm") is None, "helm not installed")
class ChartRbacTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(
            ["helm", "template", "online-boutique", CHART, "--namespace", NAMESPACE,
             "-f", os.path.join(CHART, "values-dev.yaml"),
             "--set", "image.registry=registry.example", "--set", "image.prefix=p",
             "--set", "redis.addr=redis.example:6379"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout
        docs = [d for d in yaml.safe_load_all(out) if d]
        cls.rbac = [d for d in docs if d["kind"] in RBAC_KINDS]
        cls.by_kind = {}
        for d in cls.rbac:
            cls.by_kind.setdefault(d["kind"], []).append(d)

    def _sreagent(self, kind):
        return [d for d in self.by_kind.get(kind, [])
                if any(s.get("name") == "sreagent" for s in d.get("subjects", []))
                or d["metadata"]["name"].endswith("sreagent")]

    def test_view_is_bound_in_the_app_namespace_only(self):
        bindings = [b for b in self._sreagent("RoleBinding") if b["roleRef"]["kind"] == "ClusterRole"]
        self.assertEqual([(b["roleRef"]["name"], b["metadata"].get("namespace")) for b in bindings],
                         [("view", None)])      # None: the release namespace
        self.assertEqual(bindings[0]["subjects"],
                         [{"kind": "ServiceAccount", "name": "sreagent", "namespace": NAMESPACE}])

    def test_the_cluster_role_reads_nodes_and_nothing_else(self):
        role, = self._sreagent("ClusterRole")
        self.assertEqual(role["metadata"]["name"], f"{NAMESPACE}-sreagent")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["nodes"],
                                          "verbs": ["get", "list"]}])
        binding, = self._sreagent("ClusterRoleBinding")
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io",
                                              "kind": "ClusterRole", "name": f"{NAMESPACE}-sreagent"})
        self.assertEqual(binding["subjects"],
                         [{"kind": "ServiceAccount", "name": "sreagent", "namespace": NAMESPACE}])

    def test_argocd_role_reads_applications_only(self):
        role, = [r for r in self._sreagent("Role") if r["metadata"].get("namespace") == "argocd"]
        self.assertEqual(role["rules"], [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                                          "verbs": ["get", "list"]}])
        binding, = [b for b in self._sreagent("RoleBinding") if b["metadata"].get("namespace") == "argocd"]
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        self.assertEqual(binding["subjects"][0]["namespace"], NAMESPACE)

    def test_the_old_namespaced_role_is_gone(self):
        # "view" covers everything it granted; one grant, not two to keep in step
        self.assertEqual([r for r in self.by_kind.get("Role", [])
                          if r["metadata"].get("namespace") in (None, NAMESPACE)], [])

    def test_no_secrets_no_wildcards_no_writes_anywhere(self):
        for d in self.by_kind.get("Role", []) + self.by_kind.get("ClusterRole", []):
            for rule in d["rules"]:
                with self.subTest(role=d["metadata"]["name"], rule=rule):
                    self.assertNotIn("secrets", rule["resources"])
                    self.assertNotIn("*", rule["resources"] + rule["apiGroups"] + rule["verbs"])
                    self.assertLessEqual(set(rule["verbs"]), READ_VERBS)
        for b in self.by_kind.get("RoleBinding", []) + self.by_kind.get("ClusterRoleBinding", []):
            with self.subTest(binding=b["metadata"]["name"]):
                self.assertNotIn(b["roleRef"]["name"], ("edit", "admin", "cluster-admin"))

    def test_every_get_resource_kind_has_a_grant(self):
        # view covers the app namespace kinds; the rest need their own rule
        cluster = {r for role in self._sreagent("ClusterRole") for rule in role["rules"]
                   for r in rule["resources"]}
        argocd = {r for role in self._sreagent("Role") if role["metadata"].get("namespace") == "argocd"
                  for rule in role["rules"] for r in rule["resources"]}
        for kind, spec in k8s_tools.KINDS.items():
            with self.subTest(kind=kind):
                if spec.scope == k8s_tools.CLUSTER:
                    self.assertIn(kind, cluster)
                elif spec.scope == k8s_tools.ARGOCD:
                    self.assertIn(kind, argocd)

    def test_the_argocd_namespace_matches_the_agent_setting(self):
        with open(os.path.join(CHART, "values.yaml"), encoding="utf-8") as f:
            agent = yaml.safe_load(f)["services"]["sreagent"]
        env = {e["name"]: e.get("value") for e in agent["env"]}
        self.assertEqual(list(agent["rbac"]["namespaceRules"]), [env["ARGOCD_NAMESPACE"]])


if __name__ == "__main__":
    unittest.main()
