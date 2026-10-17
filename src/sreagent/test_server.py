"""Tests for the HTTP endpoints, auth, deduplication and one-at-a-time limit."""

import os
import unittest

try:
    from fastapi.testclient import TestClient

    import server
except ImportError:  # fastapi / httpx not installed
    server = None

from agent import ANSWERED, Evidence, Investigation
from tools import ToolError
from config import Config

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def config(**overrides):
    env = {"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns", "SREAGENT_API_TOKEN": TOKEN,
           "SREAGENT_DEDUP_MINUTES": "30"}
    env.update(overrides)
    return Config.from_env(env)


class StubAgent:
    """Stands in for agent_factory and the Agent it builds.

    Records which extra tools each run was given. A run first makes the tool
    calls in `calls` (recorded as evidence), then - like the model - submits
    `assess` to submit_assessment and calls propose_values_change with
    `propose`, when those are set. Tool errors are kept, as the real loop
    returns them to the model.
    """

    def __init__(self):
        self.tasks = []
        self.extra_tools = []   # tool names given to each run
        self.calls = ["list_pods", "list_events", "recent_chart_commits"]
        self.assess = None      # args for submit_assessment
        self.propose = None     # args for propose_values_change
        self.tool_results = []
        self.answer = "## Diagnosis\nanswer\n## Confidence\nHigh. It all fits."
        self.evidence = []
        self._current = []

    def __call__(self, extra_tools):
        self._current = list(extra_tools)
        self.extra_tools.append([t.name for t in extra_tools])
        return self

    def _call(self, tool, args):
        try:
            out, err = tool.handler(args), False
        except ToolError as e:
            out, err = str(e), True
        self.tool_results.append(out)
        self.evidence.append(Evidence(tool.name, args, out, err))

    def run(self, system, task):
        self.tasks.append(task)
        self.evidence = [Evidence(name, {}, "output", False) for name in self.calls]
        tools = {t.name: t for t in self._current}
        if self.assess and "submit_assessment" in tools:
            self._call(tools["submit_assessment"], self.assess)
        if self.propose and "propose_values_change" in tools:
            self._call(tools["propose_values_change"], self.propose)
        n = len(self.tasks)
        return Investigation(ANSWERED, self.answer.replace("answer", f"answer #{n}"),
                             list(self.evidence), 1.0)


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
        self.assertEqual(decisions, [{"key": "PodCrashLooping/ns/cartservice",
                                      "incident": "ns/cartservice", "status": "accepted"}])
        self.assertEqual(self.agent.tasks, [])  # not run inline

        self.spawn.run_all()
        self.assertIn("cartservice-6b9f7c8d4-aaaaa is crash looping", self.agent.tasks[0])
        results = self.client.get("/investigations", headers=AUTH).json()
        self.assertEqual(results[0]["subject"], "PodCrashLooping/ns/cartservice")
        self.assertIn("answer #1", results[0]["answer"])

    def test_same_service_with_a_new_pod_is_deduplicated(self):
        self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"))
        self.spawn.run_all()

        decisions = self.post_alert(alert(pod="cartservice-7c8d9e0f1-bbbbb"))
        self.assertEqual(decisions[0]["status"], "skipped")
        self.assertIn("part of incident ns/cartservice, investigated 0 minute(s) ago",
                      decisions[0]["reason"])
        self.assertEqual(len(self.agent.tasks), 1)

    def test_one_investigation_at_a_time(self):
        self.post_alert(alert(pod="cartservice-6b9f7c8d4-aaaaa"))   # running (not finished)

        decisions = self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))
        self.assertEqual(decisions[0], {"key": "PodCrashLooping/ns/emailservice",
                                        "incident": "ns/emailservice", "status": "skipped",
                                        "reason": "another investigation is running"})
        self.assertEqual(self.client.post("/ask", json={"question": "q"},
                                          headers=AUTH).status_code, 409)

        self.spawn.run_all()  # first one finishes and frees the slot
        decisions = self.post_alert(alert(pod="emailservice-6b9f7c8d4-ccccc"))
        self.assertEqual(decisions[0]["status"], "accepted")

    def test_different_alerts_for_one_service_are_one_incident(self):
        self.post_alert(alert(alertname="HighMemoryUsage", pod="cartservice-6b9f7c8d4-aaaaa"))
        self.spawn.run_all()
        for name in ("ContainerOOMKilled", "PodNotReady"):
            with self.subTest(alert=name):
                d = self.post_alert(alert(alertname=name, pod="cartservice-7c8d9e0f1-bbbbb"))[0]
                self.assertEqual((d["incident"], d["status"]), ("ns/cartservice", "skipped"))
                self.assertIn("part of incident ns/cartservice", d["reason"])
        self.assertEqual(len(self.agent.tasks), 1)
        # Another service is another incident
        d = self.post_alert(alert(alertname="ContainerOOMKilled", pod="emailservice-6b9f7c8d4-ccccc"))[0]
        self.assertEqual(d["status"], "accepted")

    def test_one_decision_per_incident_in_a_payload(self):
        decisions = self.post_alert(alert(alertname="HighMemoryUsage", pod="cartservice-6b9f7c8d4-aaaaa"),
                                    alert(alertname="ContainerOOMKilled", pod="cartservice-6b9f7c8d4-bbbbb"))
        self.assertEqual([(d["incident"], d["status"]) for d in decisions], [("ns/cartservice", "accepted")])

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

    def test_live_alerts_from_2026_10_04_are_all_skipped(self):
        import json
        import os
        path = os.path.join(os.path.dirname(__file__), "fixtures", "live-alerts-2026-10-04.json")
        with open(path, encoding="utf-8") as f:
            payloads = json.load(f)["payloads"]
        client = TestClient(server.create_app(config(APP_NAMESPACE="online-boutique-dev"),
                                              self.agent, spawn=self.spawn))
        decisions = []
        for p in payloads:
            resp = client.post("/alert", json=p, headers=AUTH)
            self.assertEqual(resp.status_code, 200)
            decisions += resp.json()["decisions"]
        self.assertTrue(decisions)
        self.assertEqual({d["status"] for d in decisions}, {"skipped"})
        self.assertEqual(self.spawn.pending, [])
        self.assertEqual(self.agent.tasks, [])

    def test_ask(self):
        resp = self.client.post("/ask", json={"question": "Is cartservice healthy?"}, headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("answer #1", resp.json()["answer"])
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


try:
    import pr_tool
    from test_pr_tool import KEY, VALUES, VALUES_DEV, TAGS, FakeGitHub
except ImportError:  # ruamel.yaml not installed
    pr_tool = None

# Assessments of the stub's three calls (1 list_pods, 2 list_events, 3 recent_chart_commits)
STRONG = {  # 50 observed + 10 corroborated + 10 decisive + 10 ruled out + 20 timing = 100
    "conclusion": "cause_found", "cause": "memory limit below the working set",
    "cause_support": "observed", "cause_evidence": [1, 3],
    "decisive_check": {"what": "the commit that cut the limit", "evidence": [3]},
    "alternatives": [{"cause": "a memory leak", "result": "ruled_out", "evidence": [2]}],
    "timing": "matches", "timing_evidence": [3], "unverified": []}
MEDIUM = dict(STRONG, timing="not_checked", timing_evidence=[])                     # 80
WEAK = dict(STRONG, alternatives=[], cause_evidence=[1])                           # 80, capped at 55
MEMORY_CHANGE = {"changes": [{"path": "services.emailservice.resources.limits.memory",
                              "value": "256Mi"}], "reason": "OOM"}


@unittest.skipIf(server is None or pr_tool is None, "fastapi/httpx/ruamel.yaml not installed")
class ServerPullRequestTest(unittest.TestCase):
    def make_client(self, gh=None, **env):
        self.agent = StubAgent()
        self.spawn = DeferredSpawn()
        self.gh = gh or FakeGitHub()
        self.collected = []

        def collect_state(service):
            self.collected.append(service)
            return [(f"list_pods app={service}",
                     f"{service}-x: Running, node ip-10-0-11-45.ec2.internal")]

        opener = pr_tool.PullRequestOpener(self.gh, VALUES_DEV, VALUES)
        return TestClient(server.create_app(config(APP_NAMESPACE="online-boutique-dev", **env),
                                            self.agent, spawn=self.spawn,
                                            pr_opener=opener, collect_state=collect_state))

    def emailservice_alert(self, alertname="PodCrashLooping"):
        return payload({**alert(pod="emailservice-6b9f7c8d4-aaaaa"),
                        "labels": {"alertname": alertname, "namespace": "online-boutique-dev",
                                   "pod": "emailservice-6b9f7c8d4-aaaaa", "severity": "critical"},
                        "annotations": {"summary": f"{alertname} on emailservice"}})

    def test_one_incident_one_pr_the_pr25_pr26_sequence(self):
        """The live incident of 2026-10-10, through the server (see
        regressions/recommendationservice-duplicate-prs.json)."""
        client = self.make_client(SREAGENT_OPEN_PRS="true")
        self.agent.assess, self.agent.propose = STRONG, MEMORY_CHANGE

        # 17:22 HighMemoryUsage: investigated, PR opened (as #25 was)
        d = client.post("/alert", json=self.emailservice_alert("HighMemoryUsage"), headers=AUTH).json()
        self.assertEqual(d["decisions"][0]["status"], "accepted")
        self.spawn.run_all()
        self.assertEqual([p["number"] for p in self.gh.pulls], [42])

        # 17:24 ContainerOOMKilled: same incident -> not investigated, no second PR,
        # a comment on the open PR with the alert and the current state instead
        d = client.post("/alert", json=self.emailservice_alert("ContainerOOMKilled"), headers=AUTH).json()
        self.assertEqual(d["decisions"][0]["status"], "skipped")
        self.assertIn("part of incident online-boutique-dev/emailservice", d["decisions"][0]["reason"])
        self.spawn.run_all()
        self.assertEqual(len(self.gh.pulls), 1)
        comments = [w for w in self.gh.writes if w[1] == "/issues/42/comments"]
        self.assertEqual(len(comments), 1)
        text = comments[0][2]["body"]
        self.assertIn("**Another alert for this incident:** `ContainerOOMKilled/online-boutique-dev/emailservice`", text)
        self.assertIn("- Summary: ContainerOOMKilled on emailservice", text)
        self.assertIn("Not investigated separately", text)
        self.assertIn("emailservice-x: Running, node <node-1>", text)       # collected by code, redacted
        self.assertEqual(self.collected, ["emailservice"])

        # The same alert again (AlertManager repeat): no second comment
        client.post("/alert", json=self.emailservice_alert("ContainerOOMKilled"), headers=AUTH)
        self.spawn.run_all()
        self.assertEqual(len([w for w in self.gh.writes if w[1] == "/issues/42/comments"]), 1)

        # 17:26 the PR is merged; 17:27 PodNotReady: same incident, nothing open -> nothing posted
        self.gh.merge(42, minutes_ago=1)
        writes = len(self.gh.writes)
        d = client.post("/alert", json=self.emailservice_alert("PodNotReady"), headers=AUTH).json()
        self.spawn.run_all()
        self.assertEqual(d["decisions"][0]["status"], "skipped")
        self.assertEqual(len(self.gh.writes), writes)
        self.assertEqual(len(self.agent.tasks), 1)                         # investigated once

    def test_late_alert_after_the_window_does_not_repropose_a_merged_fix(self):
        gh = FakeGitHub()
        client = self.make_client(gh=gh, SREAGENT_OPEN_PRS="true")
        self.agent.assess, self.agent.propose = STRONG, MEMORY_CHANGE
        client.post("/alert", json=self.emailservice_alert("HighMemoryUsage"), headers=AUTH)
        self.spawn.run_all()
        gh.merge(42, minutes_ago=10)

        # A fresh server (the dedup window has passed, or the pod restarted)
        client = self.make_client(gh=gh, SREAGENT_OPEN_PRS="true")
        self.agent.assess, self.agent.propose = STRONG, MEMORY_CHANGE
        record = self.run_alert(client, STRONG)
        self.assertEqual(record["pull_request"]["status"], "recently_merged")
        self.assertEqual(record["pull_request"]["number"], 42)
        self.assertIn("was merged at", self.agent.tool_results[-1])
        self.assertEqual(len(gh.pulls), 1)

    def test_only_alerts_get_the_proposal_tool(self):
        client = self.make_client()
        client.post("/alert", json=self.emailservice_alert(), headers=AUTH)
        self.spawn.run_all()
        client.post("/ask", json={"question": "q"}, headers=AUTH)
        # Both are scored; only alerts may propose a change
        self.assertEqual(self.agent.extra_tools,
                         [["submit_assessment", "propose_values_change"], ["submit_assessment"]])

    def test_dry_run_by_default(self):
        client = self.make_client()
        self.agent.assess = STRONG
        self.agent.propose = {"changes": [{"path": "services.emailservice.resources.limits.memory",
                                           "value": "256Mi"}], "reason": "OOM"}
        client.post("/alert", json=self.emailservice_alert(), headers=AUTH)
        self.spawn.run_all()

        pr = client.get("/investigations", headers=AUTH).json()[0]["pull_request"]
        self.assertEqual(pr["status"], "dry_run")
        self.assertEqual(pr["changes"], ["services.emailservice.resources.limits.memory: (unset) -> 256Mi"])
        self.assertEqual(self.gh.writes, [])

    def test_opens_pr_when_enabled_with_the_alert_key(self):
        client = self.make_client(SREAGENT_OPEN_PRS="true")
        self.agent.assess = STRONG
        self.agent.propose = {"changes": [{"path": "services.emailservice.resources.limits.memory",
                                           "value": "256Mi"}], "reason": "OOM"}
        client.post("/alert", json=self.emailservice_alert(), headers=AUTH)
        self.spawn.run_all()

        pr = client.get("/investigations", headers=AUTH).json()[0]["pull_request"]
        self.assertEqual(pr["status"], "opened")
        self.assertTrue(pr["branch"].startswith("sre-agent/"))
        self.assertTrue(self.gh.pulls[0]["body"].startswith(
            pr_tool.incident_marker("online-boutique-dev/emailservice")))

    def run_alert(self, client, assess, propose=MEMORY_CHANGE):
        self.agent.assess, self.agent.propose = assess, propose
        client.post("/alert", json=self.emailservice_alert(), headers=AUTH)
        self.spawn.run_all()
        return client.get("/investigations", headers=AUTH).json()[0]

    def test_record_carries_the_score_reasons_and_model_words(self):
        record = self.run_alert(self.make_client(), STRONG)
        conf = record["confidence"]
        self.assertEqual((conf["score"], conf["min_for_pr"], conf["min_for_rollback"]), (100, 70, 85))
        self.assertEqual(conf["reasons"][0]["points"], 50)
        self.assertEqual(len(conf["submissions"]), 1)
        self.assertEqual(record["note"], "")
        self.assertIn("## Confidence\nHigh. It all fits.", record["answer"])   # model's words kept

    def test_recording_has_the_alert_and_the_outputs_for_capture(self):
        client = self.make_client()
        record = self.run_alert(client, STRONG)
        self.assertNotIn("output", record["evidence"][0])        # the list stays small
        self.assertEqual(record["model"], "claude-sonnet-5-5")
        self.assertTrue(record["started_at"] <= record["finished_at"])

        url = f"/investigations/{record['id']}/recording"
        self.assertEqual(client.get(url).status_code, 401)
        full = client.get(url, headers=AUTH).json()
        self.assertEqual(full["evidence"][0], {"tool": "list_pods", "input": {}, "is_error": False,
                                               "output": "output"})
        self.assertEqual(full["alert"]["alerts"][0]["labels"]["alertname"],
                         self.emailservice_alert()["alerts"][0]["labels"]["alertname"])
        self.assertEqual(client.get("/investigations/nope/recording", headers=AUTH).status_code, 404)

        # and capture accepts it as it is
        import replay
        picked = replay.pick_investigation(full, "emailservice")
        alert = replay.alert_payload(picked["alert"], "emailservice", "online-boutique-dev")
        self.assertEqual(len(alert["alerts"]), 1)

    def test_below_threshold_reports_but_proposes_nothing(self):
        client = self.make_client(SREAGENT_OPEN_PRS="true")
        record = self.run_alert(client, WEAK)
        self.assertEqual(record["confidence"]["score"], 55)
        self.assertIsNone(record["pull_request"])
        self.assertEqual(self.gh.writes, [])
        self.assertIn("Not proposed: the evidence score is 55/100, below the 70 needed",
                      self.agent.tool_results[-1])
        self.assertTrue(record["note"].startswith(
            "No change proposed: the evidence score is 55/100, below the 70 needed"))
        self.assertIn("no alternative explanation was ruled out", record["note"])

    def test_inferred_cause_opens_no_pr_even_above_threshold(self):
        client = self.make_client(SREAGENT_OPEN_PRS="true")
        record = self.run_alert(client, dict(STRONG, cause_support="inferred"))   # 80, capped at 60
        self.assertEqual(record["confidence"]["score"], 60)
        self.assertFalse(record["confidence"]["observed"])
        self.assertIsNone(record["pull_request"])
        self.assertEqual(self.gh.writes, [])
        self.assertIn("the cause is inferred, not directly observed", self.agent.tool_results[-1])
        self.assertIn("No change proposed: the cause is inferred", record["note"])

    def test_threshold_is_configurable(self):
        client = self.make_client(SREAGENT_OPEN_PRS="true", SREAGENT_MIN_CONFIDENCE_FOR_PR="85")
        record = self.run_alert(client, MEDIUM)                     # 80 < 85
        self.assertIsNone(record["pull_request"])
        self.assertIn("below the 85 needed", record["note"])

    def test_no_assessment_no_change(self):
        record = self.run_alert(self.make_client(), None)
        self.assertIsNone(record["confidence"]["score"])
        self.assertIsNone(record["pull_request"])
        self.assertIn("submit_assessment", self.agent.tool_results[-1])
        self.assertIn("no evidence assessment was submitted", record["note"])

    def test_rollback_needs_the_higher_threshold(self):
        rollback = {"changes": [{"path": "image.tag", "value": TAGS[1]}], "reason": "bad deploy"}
        client = self.make_client()
        record = self.run_alert(client, MEDIUM, rollback)                      # 80
        self.assertIsNone(record["pull_request"])
        self.assertIn("the evidence score is 80/100, below the 85 needed",
                      self.agent.tool_results[-1])
        record = self.run_alert(self.make_client(), STRONG, rollback)          # 100
        self.assertEqual(record["pull_request"]["status"], "dry_run")

    def test_opened_pr_body_shows_the_score(self):
        record = self.run_alert(self.make_client(SREAGENT_OPEN_PRS="true"), STRONG)
        self.assertEqual(record["pull_request"]["status"], "opened")
        body = self.gh.pulls[0]["body"]
        self.assertIn("## Evidence score\n\n**Evidence score: 100 / 100.**", body)
        self.assertIn("| +50 | Cause directly observed: list_pods (call 1), recent_chart_commits (call 3) |", body)
        self.assertIn("> **Agent's own assessment:** High. It all fits.", body)
        self.assertLess(body.index("## Evidence score"), body.index("## Agent's report"))
        # the report's own sections sit one level down, so no heading appears twice
        headings = [line for line in body.split("\n") if line.startswith("#")]
        self.assertEqual(len(headings), len(set(headings)), headings)
        self.assertIn("### Confidence", headings)
        self.assertNotIn("(#", body)                 # GitHub would link "#3" to pull request 3

    def test_reports_are_redacted(self):
        client = self.make_client()
        self.agent.answer = "OOMKilled on ip-10-0-10-135.ec2.internal (10.0.10.135), image " \
                            "073759315444.dkr.ecr.us-east-1.amazonaws.com/x:v1"
        record = self.run_alert(client, STRONG)
        self.assertEqual(record["answer"], "OOMKilled on <node-1> (<ip-1>), image "
                                           "<account-id>.dkr.ecr.us-east-1.amazonaws.com/x:v1")

    def test_ask_is_scored_too(self):
        client = self.make_client()
        self.agent.assess = STRONG
        resp = client.post("/ask", json={"question": "why?"}, headers=AUTH).json()
        self.assertEqual(resp["confidence"]["score"], 100)
        self.assertIsNone(resp["pull_request"])

    def test_no_proposal_no_pull_request(self):
        client = self.make_client(SREAGENT_OPEN_PRS="true")
        client.post("/alert", json=self.emailservice_alert(), headers=AUTH)
        self.spawn.run_all()
        self.assertIsNone(client.get("/investigations", headers=AUTH).json()[0]["pull_request"])
        self.assertEqual(self.gh.writes, [])


HERE = os.path.dirname(os.path.abspath(__file__))


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


@unittest.skipIf(server is None, "fastapi/httpx not installed")
class ChatPageTest(unittest.TestCase):
    def setUp(self):
        self.agent = StubAgent()
        self.client = TestClient(server.create_app(config(), self.agent, spawn=DeferredSpawn()))

    def test_chat_needs_no_token_and_serves_the_page_with_security_headers(self):
        import base64
        import hashlib
        import re

        resp = self.client.get("/chat")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/html"))
        self.assertEqual(resp.text, read("chat.html"))
        self.assertIn('<input id="token" type="password"', resp.text)
        self.assertIn('<textarea id="question"', resp.text)

        script, = re.findall(r"<script>(.*?)</script>", resp.text, re.DOTALL)
        digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
        self.assertEqual(resp.headers["content-security-policy"],
                         f"default-src 'none'; script-src 'sha256-{digest}'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        self.assertEqual(resp.headers["referrer-policy"], "no-referrer")
        self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_ask_still_requires_the_token(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            with self.subTest(headers=headers):
                resp = self.client.post("/ask", json={"question": "q"}, headers=headers)
                self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.agent.tasks, [])
        resp = self.client.post("/ask", json={"question": "Are all deployments healthy?"},
                                headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        # what the page shows: the answer, the tools used and the duration
        body = resp.json()
        for key in ("answer", "evidence", "elapsed_seconds", "tool_calls", "max_tool_calls",
                    "outcome", "confidence", "note", "id"):
            self.assertIn(key, body)

    def test_page_renders_text_only_and_keeps_the_token_in_memory(self):
        page = read("chat.html")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                       "localStorage", "sessionStorage", "document.cookie",
                       "http://", "https://", "<link", "src="):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, page)
        self.assertIn("textContent", page)
        self.assertIn('fetch("/ask"', page)
        self.assertIn('"Authorization": "Bearer " + token', page)

    # The page's own script, run under Node against a minimal fake DOM
    # (chat_page_harness.js): one session with an answer, a 401, and a send
    # without a token.
    MARKDOWN = ("First paragraph with **bold** and `kubectl get pods`.\nSecond line.\n\n"
                "- one\n- two **b**\n\n"
                "<script>alert(1)</script> <img src=x onerror=alert(1)>\n"
                "## A heading, **unclosed bold and `unclosed code")

    def run_page(self, answer):
        import json
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        body = {"answer": answer, "evidence": [], "elapsed_seconds": 1.5, "tool_calls": 1,
                "max_tool_calls": 15, "outcome": "answered"}
        out = subprocess.run([node, os.path.join(HERE, "chat_page_harness.js"),
                              os.path.join(HERE, "chat.html"), json.dumps(body)],
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return json.loads(out.stdout.decode("utf-8"))

    @staticmethod
    def nodes(tree, tag=None):
        """Every element node in the tree (or those with this tag), depth first."""
        found = []
        if "tag" in tree:
            if tag is None or tree["tag"] == tag:
                found.append(tree)
            for child in tree["children"]:
                found += ChatPageTest.nodes(child, tag)
        return found

    @staticmethod
    def text(tree):
        return tree["text"] if "text" in tree else "".join(map(ChatPageTest.text, tree["children"]))

    def test_markdown_subset_renders_as_elements(self):
        answer = [n for n in self.nodes(self.run_page(self.MARKDOWN)["reply"])
                  if n["class"] == "answer"][0]
        paragraphs = self.nodes(answer, "p")
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(self.text(paragraphs[0]),
                         "First paragraph with bold and kubectl get pods.\nSecond line.")
        self.assertEqual([self.text(n) for n in self.nodes(answer, "strong")], ["bold", "b"])
        self.assertEqual([self.text(n) for n in self.nodes(answer, "code")], ["kubectl get pods"])
        lists = self.nodes(answer, "ul")
        self.assertEqual(len(lists), 1)
        self.assertEqual([self.text(li) for li in self.nodes(lists[0], "li")], ["one", "two b"])
        # anything outside the subset stays as the characters it was
        self.assertIn("## A heading, **unclosed bold and `unclosed code", self.text(paragraphs[1]))

    def test_script_and_img_tags_in_an_answer_stay_plain_text(self):
        reply = self.run_page(self.MARKDOWN)["reply"]
        tags = {n["tag"] for n in self.nodes(reply)}
        self.assertEqual(tags - {"div", "p", "ul", "li", "strong", "code"}, set())
        self.assertIn("<script>alert(1)</script> <img src=x onerror=alert(1)>", self.text(reply))

    def test_token_is_trimmed_and_cleared_after_a_401(self):
        session = self.run_page("ok")
        self.assertEqual(session["tokenFieldAfterSet"], "")
        self.assertEqual(session["statusAfterSet"], "Token set (held in memory until you reload).")
        # pasted as "  abc-123_XYZ\r\n": sent without the spaces and line break
        self.assertEqual(session["firstRequest"]["headers"]["Authorization"], "Bearer abc-123_XYZ")
        self.assertEqual(session["firstRequest"]["url"], "/ask")
        self.assertFalse(session["sendDisabledAfterAnswer"])

        self.assertIn("401: wrong or missing token", self.text(session["errorReply"]))
        self.assertEqual(session["statusAfter401"], "Token rejected and cleared. Enter it again above.")
        self.assertEqual(session["statusClassAfter401"], "warn")
        # the token really is gone: the next send is not made
        self.assertEqual(session["requestsAfterNoToken"], 2)
        self.assertEqual(session["statusAfterNoToken"], "Enter the token first.")

    def test_server_refuses_to_start_without_the_page(self):
        import tempfile
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir", "chat.html")
        with self.assertRaisesRegex(RuntimeError, "chat page .* cannot be read"):
            server.create_app(config(), self.agent, chat_page=missing)

    def test_server_refuses_a_page_without_exactly_one_script(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write("<html><script>a()</script><script>b()</script></html>")
        self.addCleanup(os.remove, f.name)
        with self.assertRaisesRegex(RuntimeError, "exactly one inline <script>, found 2"):
            server.create_app(config(), self.agent, chat_page=f.name)

    def test_the_image_includes_the_page(self):
        self.assertIn("COPY --chown=root:root chat.html ./", read("Dockerfile"))


if __name__ == "__main__":
    unittest.main()
