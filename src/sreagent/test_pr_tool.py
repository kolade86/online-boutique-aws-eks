"""The PR flow end to end against an in-memory GitHub: everything but the HTTP calls."""

import base64
import os
import unittest
import urllib.parse

try:
    import pr_tool
    import values_change as vc
except ImportError:  # ruamel.yaml not installed
    pr_tool = None

from agent import ANSWERED, TIMEOUT, Evidence, Investigation
from tools import ToolError

CHART = os.path.join(os.path.dirname(__file__), "..", "..", "helm", "online-boutique")
VALUES_DEV = "helm/online-boutique/values-dev.yaml"
VALUES = "helm/online-boutique/values.yaml"
KEY = "PodCrashLooping/online-boutique-dev/emailservice"

TAGS = ["v20261004-154933-68ac8238", "v20261004-134739-d34db63e", "v20260830-154023-ebc1fc93"]


def dev_text(tag):
    return f"""# Dev environment overrides

# Image tag deployed to dev. MANAGED BY CI - the "Build Images" workflow
# rewrites this line after pushing to ECR, and Argo CD deploys the commit.
# Change it by merging to main, not by editing the cluster.
image:
  tag: "{tag}"

services:
  loadgenerator:
    replicas: 0
"""


class FakeGitHub:
    """Just enough of the GitHub REST API for pr_tool, recording every write."""

    branch = "main"

    def __init__(self):
        with open(os.path.join(CHART, "values.yaml"), encoding="utf-8") as f:
            values = f.read()
        # commit sha -> files; history newest first (each commit is a deploy)
        self.commits = {f"c{i}" + "0" * 38: {VALUES_DEV: dev_text(tag), VALUES: values}
                        for i, tag in enumerate(TAGS)}
        self.history = list(self.commits)
        self.main = self.history[0]
        self.pulls = []
        self.writes = []

    def set_main_file(self, text):
        sha = "d" * 40
        self.commits[sha] = {**self.commits[self.main], VALUES_DEV: text}
        self.history.insert(0, sha)
        self.main = sha

    def get(self, path, params=None):
        params = params or {}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path.startswith("/contents/"):
            name = urllib.parse.unquote(path[len("/contents/"):])
            text = self.commits[params["ref"]][name]
            return {"encoding": "base64", "sha": f"blob-{params['ref'][:4]}-{name[-9:]}",
                    "content": base64.b64encode(text.encode()).decode()}
        if path == "/commits":
            return [{"sha": s} for s in self.history[:params["per_page"]]]
        if path == "/pulls":
            return [p for p in self.pulls if p["state"] == params.get("state", "open")]
        raise AssertionError(f"unexpected GET {path}")

    def merge(self, number, minutes_ago=2):
        """Mark an agent PR as merged that many minutes ago."""
        from datetime import datetime, timedelta, timezone
        for p in self.pulls:
            if p["number"] == number:
                when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                p.update(state="closed", merged_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def send(self, method, path, body=None, params=None):
        if method == "GET":
            return self.get(path, params)
        self.writes.append((method, path, body))
        if path == "/pulls":
            number = 42 + len(self.pulls)
            pr = {"number": number, "html_url": f"https://github.com/o/r/pull/{number}",
                  "state": "open", "title": body["title"], "merged_at": None,
                  "head": {"ref": body["head"]}, "body": body["body"]}
            self.pulls.append(pr)
            return pr
        return {}


def evidence():
    return [Evidence("list_events", {"app": "emailservice"},
                     "Warning OOMKilled Pod/emailservice-x token=ghp_" + "a" * 36, False),
            Evidence("describe_deployment", {"name": "emailservice"}, "Live spec.replicas: 1", False)]


@unittest.skipIf(pr_tool is None, "ruamel.yaml not installed")
class PullRequestTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.opener = pr_tool.PullRequestOpener(self.gh, VALUES_DEV, VALUES)
        self.tool = pr_tool.ProposalTool(self.opener, KEY)

    def propose(self, *pairs, reason="memory limit too low: OOMKilled"):
        return self.tool.handle({"changes": [{"path": p, "value": v} for p, v in pairs],
                                 "reason": reason})

    def test_recent_tags_exclude_the_current_one(self):
        self.assertEqual(self.opener.recent_tags(dev_text(TAGS[0])), TAGS[1:])

    def test_accepted_proposal_is_held_not_written(self):
        out = self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        self.assertTrue(out.startswith("Accepted."))
        self.assertIn("+        memory: 256Mi", out)
        self.assertIsNotNone(self.tool.proposal)
        self.assertEqual(self.gh.writes, [])   # nothing written during the investigation

    def test_rejected_proposal_returns_reasons_to_the_model(self):
        with self.assertRaises(ToolError) as ctx:
            self.propose(("services.emailservice.replicas", 3), ("redis.addr", "x:6379"))
        message = str(ctx.exception)
        self.assertTrue(message.startswith("Rejected:"))
        self.assertIn("has an HPA", message)
        self.assertIn("redis.addr: not on the allow-list", message)
        self.assertIsNone(self.tool.proposal)

    def test_open_creates_branch_commit_and_pr_never_touching_main(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        result = pr_tool.finish(self.tool, Investigation(ANSWERED, "## Summary\nOOM", evidence(), 9.0),
                                KEY, "ab12cd34", open_prs=True)

        self.assertEqual(result["status"], "opened")
        self.assertEqual(result["url"], "https://github.com/o/r/pull/42")
        self.assertEqual(result["branch"], "sre-agent/ab12cd34")

        (m1, p1, ref), (m2, p2, put), (m3, p3, pull), (m4, p4, labels) = self.gh.writes
        self.assertEqual((m1, p1), ("POST", "/git/refs"))
        self.assertEqual(ref, {"ref": "refs/heads/sre-agent/ab12cd34", "sha": self.gh.main})
        self.assertEqual((m2, p2), ("PUT", "/contents/" + urllib.parse.quote(VALUES_DEV)))
        self.assertEqual(put["branch"], "sre-agent/ab12cd34")
        self.assertEqual(put["sha"], f"blob-{self.gh.main[:4]}-{VALUES_DEV[-9:]}")
        new_text = base64.b64decode(put["content"]).decode()
        self.assertTrue(new_text.startswith(dev_text(TAGS[0])))
        self.assertIn("  emailservice:\n    resources:\n      limits:\n        memory: 256Mi\n", new_text)
        self.assertEqual((m3, p3, pull["head"], pull["base"]),
                         ("POST", "/pulls", "sre-agent/ab12cd34", "main"))
        self.assertEqual((m4, p4, labels), ("POST", "/issues/42/labels", {"labels": ["sre-agent"]}))
        # No write anywhere names the base branch as its target
        for _, path, body in self.gh.writes:
            self.assertNotIn("heads/main", path)
            self.assertNotEqual((body or {}).get("branch"), "main")

    def test_pr_body_has_diagnosis_evidence_and_exact_change(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "## Diagnosis\nOOMKilled at 128Mi",
                                                evidence(), 9.0), KEY, "ab12cd34", open_prs=True)
        body = self.gh.writes[2][2]["body"]
        title = self.gh.writes[2][2]["title"]

        self.assertEqual(title, "sre-agent: services.emailservice.resources.limits.memory: (unset) -> 256Mi")
        self.assertTrue(body.startswith(pr_tool.incident_marker("online-boutique-dev/emailservice")))
        self.assertIn("**Why this change:** memory limit too low: OOMKilled", body)
        self.assertIn("```diff\n--- a/helm/online-boutique/values-dev.yaml", body)
        self.assertIn("+        memory: 256Mi", body)
        self.assertIn("## Diagnosis\n\n## Diagnosis\nOOMKilled at 128Mi", body)
        self.assertIn("2 tool call(s) made by the agent", body)
        self.assertIn("<code>list_events app=emailservice</code> (ok)", body)
        self.assertNotIn("ghp_", body)           # evidence is redacted
        self.assertNotIn("Image rollback", body)

    def test_rollback_pr_warns_and_rollback_must_be_recent(self):
        self.propose(("image.tag", TAGS[1]), reason="regression after deploy")
        pr_tool.finish(self.tool, Investigation(ANSWERED, "report", [], 1.0), KEY, "ab12cd34", True)
        body = self.gh.writes[2][2]["body"]
        self.assertIn("**Image rollback.** This is temporary", body)

        tool = pr_tool.ProposalTool(self.opener, "Other/ns/x")
        with self.assertRaises(ToolError) as ctx:
            tool.handle({"changes": [{"path": "image.tag", "value": "v20250101-000000-deadbeef"}],
                         "reason": "r"})
        self.assertIn("not one of the recently deployed tags", str(ctx.exception))

    def test_one_open_pr_per_incident_whatever_the_alert(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)

        # A different alert for the same service is the same incident
        again = pr_tool.ProposalTool(self.opener, "ContainerOOMKilled/online-boutique-dev/emailservice")
        with self.assertRaises(ToolError) as ctx:
            again.handle({"changes": [{"path": "services.emailservice.resources.limits.cpu",
                                       "value": "300m"}], "reason": "r"})
        self.assertIn("pull request #42 is already open for online-boutique-dev/emailservice",
                      str(ctx.exception))
        self.assertEqual(again.existing_pr["number"], 42)

        other = pr_tool.ProposalTool(self.opener, "PodCrashLooping/online-boutique-dev/cartservice")
        self.assertTrue(other.handle({"changes": [{"path": "services.cartservice.resources.limits.memory",
                                                   "value": "512Mi"}], "reason": "r"}).startswith("Accepted"))

    def test_incident_markers_new_and_legacy(self):
        self.assertEqual(pr_tool.incident_in(pr_tool.incident_marker("ns/svc") + "\nbody"), "ns/svc")
        legacy = "<!-- sre-agent-key: HighMemoryUsage/online-boutique-dev/recommendationservice -->"
        self.assertEqual(pr_tool.incident_in(legacy), "online-boutique-dev/recommendationservice")
        self.assertIsNone(pr_tool.incident_in("no marker"))
        self.assertEqual(pr_tool.incident_from_key("ContainerOOMKilled/ns/svc"), "ns/svc")

    def test_a_pr_opened_before_incidents_still_blocks(self):
        self.gh.pulls.append({"number": 7, "html_url": "u7", "state": "open", "title": "t",
                              "merged_at": None, "head": {"ref": "sre-agent/aaaaaaaa"},
                              "body": "<!-- sre-agent-key: HighMemoryUsage/online-boutique-dev/emailservice -->"})
        with self.assertRaisesRegex(ToolError, "pull request #7 is already open"):
            self.propose(("services.emailservice.resources.limits.memory", "256Mi"))

    def test_recently_merged_fix_is_not_proposed_again(self):
        # PR #25 then #26: the second alert must not re-propose what was just merged
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)
        self.gh.merge(42, minutes_ago=2)

        late = pr_tool.ProposalTool(self.opener, "PodNotReady/online-boutique-dev/emailservice")
        with self.assertRaises(ToolError) as ctx:
            late.handle({"changes": [{"path": "services.emailservice.resources.limits.memory",
                                      "value": "320Mi"}], "reason": "r"})
        self.assertIn("pull request #42 for online-boutique-dev/emailservice was merged", str(ctx.exception))
        self.assertIn("Check whether that change has rolled out", str(ctx.exception))
        result = pr_tool.finish(late, Investigation(ANSWERED, "r", [], 1.0),
                                "PodNotReady/online-boutique-dev/emailservice", "cd34ef56", True)
        self.assertEqual((result["status"], result["number"]), ("recently_merged", 42))
        self.assertEqual(len(self.gh.pulls), 1)

    def test_an_old_merge_does_not_block(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)
        self.gh.merge(42, minutes_ago=90)
        later = pr_tool.ProposalTool(self.opener, KEY, recent_merge_minutes=60)
        self.assertTrue(later.handle({"changes": [{"path": "services.emailservice.resources.limits.cpu",
                                                   "value": "300m"}], "reason": "r"}).startswith("Accepted"))

    def test_later_alert_comments_on_the_open_pr_instead(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)

        # ContainerOOMKilled for the same service, 2.5 minutes later: investigated, no proposal
        later = pr_tool.ProposalTool(self.opener, "ContainerOOMKilled/online-boutique-dev/emailservice")
        report = "## Diagnosis\nOOMKilled on ip-10-0-10-135.ec2.internal"
        result = pr_tool.finish(later, Investigation(ANSWERED, report, [], 1.0),
                                "ContainerOOMKilled/online-boutique-dev/emailservice", "cd34ef56", True)

        self.assertEqual((result["status"], result["number"]), ("commented", 42))
        self.assertEqual(len(self.gh.pulls), 1)                       # no second PR
        method, path, body = self.gh.writes[-1]
        self.assertEqual((method, path), ("POST", "/issues/42/comments"))
        text = body["body"]
        self.assertIn("**Another alert for this incident:** `ContainerOOMKilled/online-boutique-dev/emailservice`", text)
        self.assertIn("It was investigated (`cd34ef56`)", text)
        self.assertIn("OOMKilled on <node-1>", text)                   # redacted
        self.assertNotIn("ip-10-0-10-135", text)

    def test_comment_is_a_dry_run_when_prs_are_off(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)
        writes = len(self.gh.writes)
        later = pr_tool.ProposalTool(self.opener, "PodNotReady/online-boutique-dev/emailservice")
        result = pr_tool.finish(later, Investigation(ANSWERED, "r", [], 1.0),
                                "PodNotReady/online-boutique-dev/emailservice", "cd34ef56", False)
        self.assertEqual(result["status"], "comment_dry_run")
        self.assertEqual(len(self.gh.writes), writes)

    def test_open_revalidates_against_main_as_it_is_now(self):
        self.propose(("image.tag", TAGS[1]), reason="rollback")
        # Before the investigation finishes, CI deploys exactly that tag
        self.gh.set_main_file(dev_text(TAGS[1]))
        result = pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("does not modify anything", result["why"])
        self.assertEqual(self.gh.writes, [])

    def test_refuses_unsafe_branch_names(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        for bad_id in ("../main", "MAIN", "abc", "ab12cd34/../../main"):
            with self.subTest(id=bad_id), self.assertRaises(vc.Rejected):
                self.opener.open(self.tool.proposal, KEY, bad_id, "r", [])
        self.assertEqual(self.gh.writes, [])

    def test_finish_outcomes_without_writing(self):
        self.assertIsNone(pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0),
                                         KEY, "ab12cd34", True))  # nothing proposed
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))

        dry = pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", False)
        self.assertEqual(dry["status"], "dry_run")
        self.assertIn("+        memory: 256Mi", dry["diff"])

        timed_out = pr_tool.finish(self.tool, Investigation(TIMEOUT, "r", [], 300.0),
                                   KEY, "ab12cd34", True)
        self.assertEqual(timed_out["status"], "not_opened")
        self.assertEqual(self.gh.writes, [])

    def assessed(self, *assessments):
        """An AssessmentTool after submitting each assessment, one tool call apart."""
        import confidence
        ev = [Evidence("list_pods", {}, "out", False), Evidence("list_events", {}, "out", False)]
        tool = confidence.AssessmentTool(70, 85)
        tool.attach(lambda: ev)
        for a in assessments:
            ev.append(Evidence(confidence.TOOL_NAME, a, tool.handle(a), False))
            ev.append(Evidence("list_hpas", {}, "out", False))
        return tool

    STRONG = {"conclusion": "cause_found", "cause": "limit below working set",
              "cause_support": "observed", "cause_evidence": [1, 2],
              "alternatives": [{"cause": "a leak", "result": "ruled_out", "evidence": [2]}],
              "timing": "matches", "timing_evidence": [2], "unverified": []}

    def test_body_records_every_assessment(self):
        weak = dict(self.STRONG, alternatives=[], timing="not_checked", timing_evidence=[])
        assessment = self.assessed(weak, self.STRONG)            # 55, then 90
        self.tool.assessment = assessment
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        report = "## Diagnosis\nx\n## Confidence\nHigh. The numbers fit.\n## Evidence\n- y"
        result = pr_tool.finish(self.tool, Investigation(ANSWERED, report, evidence(), 9.0),
                                KEY, "ab12cd34", open_prs=True, assessment=assessment)
        self.assertEqual(result["status"], "opened")
        body = self.gh.writes[2][2]["body"]
        self.assertIn("**Evidence score: 90 / 100.**", body)
        self.assertIn("A pull request needs a directly observed cause and a score of at least 70.", body)
        self.assertIn("Assessment history: first after 2 tool call(s): 55; "
                      "revised after 3 tool call(s): 90.", body)
        self.assertIn("> **Agent's own assessment:** High. The numbers fit.", body)

    def test_finish_rechecks_the_latest_assessment(self):
        weak = dict(self.STRONG, alternatives=[])
        assessment = self.assessed(self.STRONG)
        self.tool.assessment = assessment
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))   # allowed at 90
        assessment = self.assessed(self.STRONG, weak)            # then revised down to 55
        result = pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY,
                                "ab12cd34", open_prs=True, assessment=assessment)
        self.assertEqual(result["status"], "below_confidence")
        self.assertIn("the evidence score is 55/100, below the 70 needed", result["why"])
        self.assertEqual(self.gh.writes, [])

    def test_body_hides_nodes_ips_and_account_ids(self):
        ev = [Evidence("list_pods", {"app": "x"},
                       "pod-a on ip-10-0-10-135.ec2.internal ip 10.0.10.135, image "
                       "073759315444.dkr.ecr.us-east-1.amazonaws.com/x:v1", False)]
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))
        pr_tool.finish(self.tool, Investigation(ANSWERED, "killed on ip-10-0-10-135.ec2.internal",
                                                ev, 1.0), KEY, "ab12cd34", open_prs=True)
        body = self.gh.writes[2][2]["body"]
        for leaked in ("ip-10-0-10-135", "10.0.10.135", "073759315444"):
            self.assertNotIn(leaked, body)
        self.assertIn("killed on <node-1>", body)
        self.assertIn("pod-a on <node-1> ip <ip-1>, image <account-id>.dkr.ecr", body)

    def test_github_failure_is_reported_not_raised(self):
        self.propose(("services.emailservice.resources.limits.memory", "256Mi"))

        def failing_send(method, path, body=None, params=None):
            if method != "GET":
                raise ToolError("GitHub HTTP 403 for POST /git/refs: Resource not accessible")
            return self.gh.get(path, params)
        self.gh.send = failing_send

        result = pr_tool.finish(self.tool, Investigation(ANSWERED, "r", [], 1.0), KEY, "ab12cd34", True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("403", result["why"])


if __name__ == "__main__":
    unittest.main()
