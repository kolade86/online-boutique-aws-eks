"""The one write tool: propose a change to values-dev.yaml, opened as a pull request.

Two steps, so the model gets feedback and the PR gets the full story:

1. During the investigation the model calls `propose_values_change`. The
   change is applied to the current values-dev.yaml from main and validated
   against the allow-list (values_change.py). A rejection goes back to the
   model with the reasons; an accepted change is held, not written.
2. After the investigation finishes, `PullRequestOpener.open` re-reads main,
   re-applies and re-validates the change, and opens a PR on a new
   sre-agent/<id> branch. The body holds the final report (diagnosis), every
   tool call the loop recorded (evidence), and the exact diff.

Nothing is ever written to the base branch, and nothing touches the cluster:
a human reviews and merges, and Argo CD deploys.
"""

import base64
import re
import urllib.parse
from dataclasses import dataclass, field

import values_change as vc
from redact import redact
from tools import Tool, ToolError, require_str

BRANCH = re.compile(r"sre-agent/[a-z0-9]{8}")
TAG_IN_FILE = re.compile(r'^  tag: "?([^"\s]+)"?\s*$', re.MULTILINE)
LABEL = "sre-agent"
RECENT_TAG_COMMITS = 6          # values-dev.yaml commits searched for rollback tags
MAX_BODY_CHARS = 60000          # GitHub's limit is 65536
EVIDENCE_EXCERPT_CHARS = 1200


def key_marker(key: str) -> str:
    """Hidden in the PR body; finds an open PR for the same alert."""
    return f"<!-- sre-agent-key: {key} -->"


@dataclass
class Context:
    """main as it is now: what a change is applied to and validated against."""
    base_sha: str
    dev_text: str
    dev_blob_sha: str
    base_values: dict
    recent_tags: list[str]


@dataclass
class Proposal:
    changes: list
    reason: str
    new_text: str
    diff: dict
    diff_text: str
    context: Context = field(repr=False)


class PullRequestOpener:
    def __init__(self, gh, values_file: str, base_values_file: str):
        self.gh = gh                      # github_tools.GitHubReader (get/send)
        self.values_file = values_file
        self.base_values_file = base_values_file

    # --- reads -------------------------------------------------------------

    def _file(self, path: str, ref: str) -> tuple[str, str]:
        item = self.gh.get(f"/contents/{urllib.parse.quote(path)}", {"ref": ref})
        return base64.b64decode(item["content"]).decode("utf-8"), item["sha"]

    def load_context(self) -> Context:
        base_sha = self.gh.get(f"/git/ref/heads/{self.gh.branch}")["object"]["sha"]
        dev_text, dev_blob_sha = self._file(self.values_file, base_sha)
        base_text, _ = self._file(self.base_values_file, base_sha)
        return Context(base_sha, dev_text, dev_blob_sha, vc.load(base_text),
                       self.recent_tags(dev_text))

    def recent_tags(self, current_text: str) -> list[str]:
        """Tags deployed by the last few values-dev.yaml commits, newest first,
        excluding the current one: the only allowed rollback targets."""
        current = TAG_IN_FILE.search(current_text)
        commits = self.gh.get("/commits", {"path": self.values_file, "sha": self.gh.branch,
                                           "per_page": RECENT_TAG_COMMITS})
        tags = []
        for c in commits:
            text, _ = self._file(self.values_file, c["sha"])
            m = TAG_IN_FILE.search(text)
            if m and vc.TAG.fullmatch(m.group(1)) and m.group(1) not in tags \
                    and (current is None or m.group(1) != current.group(1)):
                tags.append(m.group(1))
        return tags

    def open_pr_for(self, key: str):
        """The open agent PR for this alert key, if any."""
        for pr in self.gh.get("/pulls", {"state": "open", "per_page": 100}):
            if pr["head"]["ref"].startswith("sre-agent/") and key_marker(key) in (pr.get("body") or ""):
                return pr
        return None

    # --- validate ----------------------------------------------------------

    def check(self, changes, reason: str, key: str = None, context: Context = None) -> Proposal:
        """Apply and validate against main. Raises vc.Rejected."""
        if key is not None:
            existing = self.open_pr_for(key)
            if existing:
                raise vc.Rejected([f"pull request #{existing['number']} is already open for "
                                   f"this alert ({existing['html_url']})"])
        context = context or self.load_context()
        new_text = vc.apply_changes(context.dev_text, changes)
        diff = vc.validate(context.dev_text, new_text, context.base_values, context.recent_tags)
        return Proposal(changes, reason, new_text, diff,
                        vc.unified_diff(context.dev_text, new_text, self.values_file), context)

    # --- write -------------------------------------------------------------

    def open(self, proposal: Proposal, key: str, investigation_id: str,
             diagnosis: str, evidence: list) -> dict:
        """Re-validate against main as it is now, then branch, commit and open the PR."""
        fresh = self.check(proposal.changes, proposal.reason, key=key)

        branch = f"sre-agent/{investigation_id}"
        if not BRANCH.fullmatch(branch) or branch == self.gh.branch:
            raise vc.Rejected([f"refusing to write to branch {branch!r}"])

        self.gh.send("POST", "/git/refs", {"ref": f"refs/heads/{branch}",
                                            "sha": fresh.context.base_sha})
        self.gh.send("PUT", f"/contents/{urllib.parse.quote(self.values_file)}", {
            "message": commit_message(fresh, key),
            "content": base64.b64encode(fresh.new_text.encode("utf-8")).decode(),
            "sha": fresh.context.dev_blob_sha,
            "branch": branch,                 # always the agent branch, never the base
        })
        pr = self.gh.send("POST", "/pulls", {
            "title": title(fresh),
            "head": branch,
            "base": self.gh.branch,
            "body": pr_body(fresh, key, investigation_id, diagnosis, evidence, self.values_file),
        })
        try:
            self.gh.send("POST", f"/issues/{pr['number']}/labels", {"labels": [LABEL]})
        except ToolError:
            pass  # the label is a convenience; the body marker is what dedup relies on
        return {"number": pr["number"], "url": pr["html_url"], "branch": branch}


def title(p: Proposal) -> str:
    lines = vc.describe(p.diff)
    head = lines[0] if len(lines) == 1 else f"{len(lines)} settings"
    return f"sre-agent: {head}"[:120]


def commit_message(p: Proposal, key: str) -> str:
    return "\n".join([title(p), "", f"Alert: {key}", "", *vc.describe(p.diff)])


def pr_body(p: Proposal, key: str, investigation_id: str, diagnosis: str,
            evidence: list, values_file: str) -> str:
    parts = [
        key_marker(key),
        "Opened by **sreagent**. Review before merging: Argo CD deploys whatever is merged.",
        "",
        f"**Alert:** `{key}`  ",
        f"**Investigation:** `{investigation_id}`  ",
        f"**Why this change:** {redact(p.reason)}",
        "",
        "## Change",
        "",
        *[f"- `{line}`" for line in vc.describe(p.diff)],
        "",
        "```diff",
        p.diff_text.rstrip("\n"),
        "```",
        "",
    ]
    if ("image", "tag") in p.diff:
        parts += [
            "> **Image rollback.** This is temporary: the next push to `src/**` rebuilds",
            "> from main and deploys the code being rolled back. Revert or fix the",
            "> offending commit as well. ECR keeps only the last 10 images per service.",
            "",
        ]
    parts += ["## Diagnosis", "", redact(diagnosis).strip() or "_(no report)_", "",
              "## Evidence", "",
              f"{len(evidence)} tool call(s) made by the agent, in order:", ""]
    for i, e in enumerate(evidence, 1):
        status = "error" if e.is_error else "ok"
        excerpt = e.output if len(e.output) <= EVIDENCE_EXCERPT_CHARS else \
            e.output[:EVIDENCE_EXCERPT_CHARS] + f"\n... [{len(e.output)} characters]"
        parts += [f"<details><summary>{i}. <code>{e.tool} {_inline(e.input)}</code> ({status})</summary>",
                  "", "```", redact(excerpt).replace("```", "'''"), "```", "</details>", ""]
    parts += ["## Validation", "",
              f"Checked against `{values_file}` and the chart values on `{p.context.base_sha[:8]}`:",
              "every changed setting is on the allow-list (image tag rollback to a recent tag; "
              "CPU/memory requests and limits; replicas only without an HPA; HPA min/max only "
              "with one), within bounds, and the file keeps its comments and single "
              "`  tag:` line."]
    body = "\n".join(parts)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n_[body cut to fit GitHub's limit]_"
    return body


def _inline(args: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in args.items())


class ProposalTool:
    """The model-facing tool. Holds at most one accepted proposal per investigation."""

    def __init__(self, opener: PullRequestOpener, key: str):
        self.opener = opener
        self.key = key
        self.proposal = None

    def handle(self, args: dict) -> str:
        reason = require_str(args, "reason")
        try:
            changes = vc.parse_changes(args.get("changes"))
            proposal = self.opener.check(changes, reason, key=self.key)
        except vc.Rejected as e:
            raise ToolError("Rejected:\n" + "\n".join(f"- {r}" for r in e.reasons)) from None
        replaced = " It replaces your earlier proposal." if self.proposal else ""
        self.proposal = proposal
        return ("Accepted." + replaced + " A pull request with this change will be opened for "
                "human review after you finish your report; your report becomes its "
                "description. Diff:\n" + proposal.diff_text)

    def tool(self) -> Tool:
        return Tool(
            "propose_values_change",
            "Propose a configuration change to helm/online-boutique/values-dev.yaml, to be "
            "opened as a pull request for human review once you finish. Only use it when "
            "your evidence shows the change would fix the problem. Allowed: image.tag "
            "(rollback to a recently deployed tag, as the only change); "
            "services.<svc>.resources.(requests|limits).(cpu|memory); "
            "services.<svc>.replicas (only for services without an HPA); "
            "services.<svc>.hpa.(minReplicas|maxReplicas) (only for services with an HPA). "
            "Anything else is rejected with the reason. One proposal per investigation; "
            "calling it again replaces the earlier one.",
            {"type": "object", "properties": {
                "changes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Dotted path, e.g. services.cartservice.resources.limits.memory"},
                        "value": {"type": ["string", "integer"],
                                  "description": "e.g. \"768Mi\", \"250m\", 3, or an image tag"}},
                    "required": ["path", "value"]}},
                "reason": {"type": "string",
                           "description": "One or two sentences: why this change fixes the problem"}},
             "required": ["changes", "reason"]},
            self.handle)


def finish(tool, result, key: str, investigation_id: str, open_prs: bool):
    """After an investigation: open the accepted proposal as a PR, or say why not.

    Returns None when nothing was proposed, else a dict for the result record.
    """
    from agent import ANSWERED, ANSWERED_AT_LIMIT

    if tool is None or tool.proposal is None:
        return None
    p = tool.proposal
    summary = {"changes": vc.describe(p.diff), "reason": p.reason, "diff": p.diff_text}
    if result.outcome not in (ANSWERED, ANSWERED_AT_LIMIT):
        return {**summary, "status": "not_opened",
                "why": f"the investigation ended with {result.outcome}, without a full report"}
    if not open_prs:
        return {**summary, "status": "dry_run",
                "why": "SREAGENT_OPEN_PRS is off; the change was validated but not opened"}
    try:
        return {**summary, "status": "opened",
                **tool.opener.open(p, key, investigation_id, result.answer, result.evidence)}
    except (vc.Rejected, ToolError) as e:
        return {**summary, "status": "failed", "why": str(e)}
