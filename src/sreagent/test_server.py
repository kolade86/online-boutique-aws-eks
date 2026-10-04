"""Tests for the HTTP endpoints, auth, deduplication and one-at-a-time limit."""

import unittest

try:
    from fastapi.testclient import TestClient

    import server
except ImportError:  # fastapi / httpx not installed
    server = None

from agent import ANSWERED, Investigation
from config import Config

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def config(**overrides):
    env = {"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns", "SREAGENT_API_TOKEN": TOKEN,
           "SREAGENT_DEDUP_MINUTES": "30"}
    env.update(overrides)
    return Config.from_env(env)


class StubAgent:
    def __init__(self):
        self.tasks = []

    def run(self, system, task):
        self.tasks.append(task)
        return Investigation(ANSWERED, f"answer #{len(self.tasks)}", [], 1.0)


class DeferredSpawn:
    """Holds background work until the test runs it, so 'busy' can be observed."""

    def __init__(self):
        self.pending = []

    def __call__(self, fn):
        self.pending.append(fn)

    def run_all(self):
        while self.pending:
            self.pending.pop(0)()


def alert(alertname="PodCrashLooping", pod="cartservice-6b9f7c8d4-aaaaa", status="firing"):
    return {"status": status, "labels": {"alertname": alertname, "namespace": "ns", "pod": pod,
                                         "severity": "critical"},
            "annotations": {"summary": f"{pod} is crash looping"},
            "startsAt": "2026-10-04T12:00:00Z"}


def payload(*alerts):
    return {"version": "4", "status": "firing", "alerts": list(alerts)}


@unittest.skipIf(server is None, "fastapi/httpx not installed")
class ServerTest(unittest.TestCase):
    def setUp(self):
        self.agent = StubAgent()
        self.spawn = DeferredSpawn()
        self.client = TestClient(server.create_app(config(), self.agent, spawn=self.spawn))

    def post_alert(self, *alerts):
        resp = self.client.post("/alert", json=payload(*alerts), headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["decisions"]

    def test_requires_token(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/alert", json=payload(alert()),
                                                  headers=headers).status_code, 401)
                self.assertEqual(self.client.post("/ask", json={"question": "q"},
                                                  headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/investigations").status_code, 401)

    def test_server_refuses_to_start_without_token(self):
        with self.assertRaises(ValueError):
            server.create_app(config(SREAGENT_API_TOKEN=""), self.agent)

    def test_alert_is_investigated_in_background_and_recorded(self):
        decisions = self.post_alert(alert())
        self.assertEqual(decisions, [{"key": "PodCrashLooping/ns/cartservice", "status": "accepted"}])
        self.assertEqual(self.agent.tasks, [])  # not run inline

        self.spawn.run_all()
        self.assertIn("cartservice-6b9f7c8d4-aaaaa is crash looping", self.agent.tasks[0])
        results = self.client.get("/investigations", headers=AUTH).json()
        self.assertEqual(results[0]["subject"], "PodCrashLooping/ns/cartservice")
        self.assertEqual(results[0]["answer"], "answer #1")

    def test_same_service_with_a_new_pod_is_deduplicated(self):
        self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"))
        self.spawn.run_all()

        decisions = self.post_alert(alert(pod="cartservice-7c8d9e0f1-bbbbb"))
        self.assertEqual(decisions[0]["status"], "skipped")
        self.assertIn("last 30 minutes", decisions[0]["reason"])
        self.assertEqual(len(self.agent.tasks), 1)

    def test_one_investigation_at_a_time(self):
        self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"))   # running (not finished)

        decisions = self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))
        self.assertEqual(decisions[0], {"key": "PodCrashLooping/ns/emailservice",
                                        "status": "skipped",
                                        "reason": "another investigation is running"})
        self.assertEqual(self.client.post("/ask", json={"question": "q"},
                                          headers=AUTH).status_code, 409)

        self.spawn.run_all()  # first one finishes and frees the slot
        decisions = self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))
        self.assertEqual(decisions[0]["status"], "accepted")

    def test_skipped_while_busy_is_not_marked_as_seen(self):
        self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"))
        self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))  # skipped: busy
        self.spawn.run_all()
        # Not deduplicated later: it was never investigated
        self.assertEqual(self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))[0]["status"],
                         "accepted")

    def test_group_payload_one_decision_per_service(self):
        decisions = self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"),
                                    alert(pod="cartservice-6b9f7c8d4-bbbbb"),
                                    alert(pod="emailservice-6b9f7c8d4-ccccc"))
        self.assertEqual([(d["key"], d["status"]) for d in decisions],
                         [("PodCrashLooping/ns/cartservice", "accepted"),
                          ("PodCrashLooping/ns/emailservice", "skipped")])
        self.spawn.run_all()
        task = self.agent.tasks[0]
        self.assertIn("cartservice-6b9f7c8d4-bbbbb", task)   # both cartservice pods
        self.assertNotIn("emailservice", task)               # not the other service

    def test_resolved_only(self):
        decisions = self.post_alert(alert(status="resolved"))
        self.assertEqual(decisions[0]["reason"], "no firing alerts")
        self.assertEqual(self.spawn.pending, [])

    def test_ask(self):
        resp = self.client.post("/ask", json={"question": "Is cartservice healthy?"}, headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["answer"], "answer #1")
        self.assertEqual(resp.json()["kind"], "ask")
        self.assertEqual(len(self.agent.tasks), 1)
        self.assertRegex(self.agent.tasks[0],
                         r"^Current time: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \(UTC\)\n\nIs cartservice healthy\?$")

    def test_alert_task_includes_current_time(self):
        self.post_alert(alert())
        self.spawn.run_all()
        self.assertTrue(self.agent.tasks[0].startswith("Current time: "))
        self.assertIn("Alert: PodCrashLooping", self.agent.tasks[0])

    def test_ask_validation(self):
        for body in ({}, {"question": ""}, {"question": "x" * 2001}):
            with self.subTest(body=str(body)[:30]):
                self.assertEqual(self.client.post("/ask", json=body, headers=AUTH).status_code, 422)


if __name__ == "__main__":
    unittest.main()
