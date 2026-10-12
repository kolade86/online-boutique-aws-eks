"""Replay a regression case: the real model, recorded evidence.

The model and the agent loop run for real (submit_assessment included), but
the tools answer from the case's recording instead of a cluster:
- calls recorded in the case (tool + input) return the saved output;
- the repository tools (read_repo_file, recent_chart_commits) read local git
  at the commit the cluster was running when the case was recorded;
- every other call fails with "no recorded output", which the model sees.

So a replay measures how the agent reasons over the evidence that was really
there, and lets its evidence score be compared with the known right answer.

    python cli.py replay regressions/emailservice-pod-replaced.json --record
"""

import base64
import json
import os
import subprocess
import urllib.parse
from datetime import datetime, timezone

import github_tools
import k8s_tools
import prometheus_tools
from tools import Tool, ToolError, ToolRegistry

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UNAVAILABLE = "No recorded output"


class LocalGit:
    """Answers the GitHub API calls github_tools makes, from local git at one commit."""

    def __init__(self, ref: str, values_file: str, repo_root: str = REPO_ROOT):
        self.branch = ref
        self.values_file = values_file
        self.chart_path = os.path.dirname(values_file).replace("\\", "/")
        self._root = repo_root

    def _git(self, *args) -> str:
        r = subprocess.run(["git", "-C", self._root, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode:
            raise ToolError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
        return r.stdout

    def get(self, path: str, params=None):
        params = params or {}
        if path.startswith("/contents/"):
            name = urllib.parse.unquote(path[len("/contents/"):])
            ref = params.get("ref", self.branch)
            kind = self._git("cat-file", "-t", f"{ref}:{name}").strip()
            if kind == "tree":
                return [{"type": "dir" if line.split()[1] == "tree" else "file",
                         "path": line.split("\t", 1)[1]}
                        for line in self._git("ls-tree", ref, name + "/").splitlines()]
            text = self._git("show", f"{ref}:{name}")
            return {"encoding": "base64", "sha": "local",
                    "content": base64.b64encode(text.encode("utf-8")).decode()}
        if path == "/commits":
            out = self._git("log", f"-n{params.get('per_page', 5)}",
                            "--format=%H%x1f%aI%x1f%an%x1f%s", params.get("sha", self.branch),
                            "--", params["path"])
            commits = []
            for line in out.splitlines():
                sha, date, name, subject = line.split("\x1f")
                commits.append({"sha": sha, "commit": {"message": subject,
                                                       "author": {"date": date, "name": name}}})
            return commits
        if path.startswith("/commits/"):
            sha = path[len("/commits/"):]
            files = []
            for line in self._git("show", "--format=", "--name-status", sha).splitlines():
                status, filename = line.split("\t")[0], line.split("\t")[-1]
                patch = self._git("show", "--format=", sha, "--", filename)
                patch = patch[patch.find("@@"):] if "@@" in patch else ""
                files.append({"filename": filename, "patch": patch,
                              "status": {"A": "added", "D": "removed"}.get(status[0], "modified")})
            return {"files": files}
        raise ToolError(f"not available in a replay: GitHub {path}")


def _normalise(args: dict) -> str:
    return json.dumps({k: v for k, v in (args or {}).items() if v not in (None, "", [], {})},
                      sort_keys=True)


def replay_registry(spec: dict, case_dir: str, max_chars: int, extra_tools=(),
                    repo_root: str = REPO_ROOT) -> ToolRegistry:
    """The agent's own tool list, answered from the recording."""
    recorded = {}
    for item in spec.get("recorded", []):
        with open(os.path.join(case_dir, item["file"]), encoding="utf-8") as f:
            recorded[(item["tool"], _normalise(item.get("input")))] = (f.read(),
                                                                      item.get("is_error", False))

    def from_recording(name):
        def handler(args):
            key = (name, _normalise(args))
            if key in recorded:
                output, is_error = recorded[key]
                if is_error:      # the call failed when it was recorded: fail the same way
                    raise ToolError(output)
                return output
            raise ToolError(f"{UNAVAILABLE} for {name} {key[1]} in this replay: the call was "
                            "not made when the case was recorded. Treat it as unavailable.")
        return handler

    namespace = spec.get("namespace", "online-boutique-dev")
    live = (prometheus_tools.make_tools(prometheus_tools.PrometheusClient("http://replay.invalid"))
            + k8s_tools.make_tools(k8s_tools.KubeReader(namespace, core=object())))
    tools = [Tool(t.name, t.description, t.input_schema, from_recording(t.name), t.keep, t.strict)
             for t in live]
    git = LocalGit(spec["repo_ref"], spec.get("values_file", "helm/online-boutique/values-dev.yaml"),
                   repo_root)
    recorded_tools = {t.name for t in tools}
    tools += github_tools.make_tools(git)
    return _ReplayRegistry(tools + list(extra_tools), max_chars, recorded_tools)


class _ReplayRegistry(ToolRegistry):
    """Recorded outputs were redacted and cut to length when the agent first saw
    them; they are returned as recorded, not cut a second time."""

    def __init__(self, tools, max_chars, recorded_tools):
        super().__init__(tools, max_chars)
        self._recorded_tools = recorded_tools

    def run(self, name: str, args: dict):
        if name not in self._recorded_tools:
            return super().run(name, args)
        try:
            return self._tools[name].handler(args), False
        except ToolError as e:
            return str(e), True


def keyword_check(answer: str, hints: dict) -> dict:
    """A rough, automatic grade from the case's keyword hints - not a human review."""
    text = answer.lower()
    missing = [group for group in hints.get("must_mention_any", [])
               if not any(word.lower() in text for word in group)]
    present = [p for p in hints.get("must_not_contain", []) if p.lower() in text]
    return {"passed": not missing and not present,
            "missing_any_of": missing, "contains_forbidden": present}


def run_case(case_path: str, config, provider, now=None) -> dict:
    """Run the case once; return the run record (assessment, score, grade, answer)."""
    import confidence
    import prompts
    from agent import Agent
    from redact import redact_public

    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)
    spec = case["replay"]
    assessment = confidence.AssessmentTool(config.min_confidence_for_pr,
                                           config.min_confidence_for_rollback)
    registry = replay_registry(spec, os.path.dirname(os.path.abspath(case_path)),
                               config.tool_output_max_chars, [assessment.tool()])
    agent = Agent(provider, registry, config.max_tool_calls, config.investigation_timeout_seconds)
    assessment.attach_to(agent)
    recorded_at = datetime.fromisoformat(spec["now"].replace("Z", "+00:00"))
    system = prompts.system_prompt("ask", spec.get("namespace", "online-boutique-dev"),
                                   config.max_tool_calls)
    result = agent.run(system, prompts.with_current_time(spec["question"], now=recorded_at))

    summary = assessment.summary()
    grade = keyword_check(result.answer, case.get("pass_criteria", {}).get("keyword_hints", {}))
    return {
        "date": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
        "model": config.model,
        "agent_commit": _head(),
        "outcome": result.outcome,
        "score": summary["score"],
        "observed": summary["observed"],
        # Whether this assessment would let a change be proposed (an observed
        # cause and the PR threshold): must never be true for a wrong answer
        "change_allowed": not confidence.change_blocked(assessment.latest,
                                                        config.min_confidence_for_pr),
        "reasons": summary["reasons"],
        "open_questions": summary["open_questions"],
        "submissions": summary["submissions"],
        "keyword_check": grade,
        # Calls the recording could not answer. A score lost to them reflects the
        # fixture, not the agent: compare runs only with the same unavailable set.
        "unavailable": [f"{e.tool} {_normalise(e.input)}" for e in result.evidence
                        if e.is_error and e.output.startswith(UNAVAILABLE)],
        "tool_calls": [{"tool": e.tool, "input": e.input, "is_error": e.is_error}
                       for e in result.evidence],
        "answer": redact_public(result.answer)[:6000],
    }


def record(case_path: str, run: dict) -> None:
    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)
    case.setdefault("replay_runs", []).append(run)
    with open(case_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(case, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _head() -> str:
    r = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def score_table(regressions_dir: str, min_for_pr: int = 70) -> list[dict]:
    """Every scored run in the regression cases: hand-written assessments (marked) and
    recorded model replays.

    Every row is scored now, with the current rule, from the assessment and the
    tool calls saved with the run; `recorded` is the score the run got under
    the rule in force when it was made. `change_allowed` says whether the
    assessment would let a change be proposed.
    """
    import confidence
    from agent import Evidence

    def row(case_id, run, source, verdict, assessment, evidence, recorded):
        s = confidence.score(assessment, evidence) if assessment is not None else None
        return {"case": case_id, "run": run, "source": source, "verdict": verdict,
                "recorded": recorded, "score": s.value if s else None,
                "support": s.support if s else "",
                "change_allowed": not confidence.change_blocked(s, min_for_pr)}

    rows = []
    for name in sorted(os.listdir(regressions_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(regressions_dir, name), encoding="utf-8") as f:
            case = json.load(f)
        case_id = case.get("id", name[:-5])
        hand = list(case.get("hand_assessments", []))
        if "expected_assessment" in case:
            hand.append({**case["expected_assessment"], "run": "expected answer", "verdict": "correct"})
        for h in hand:
            evidence = [Evidence(t, {}, "", False) for t in h["tool_calls"]]
            rows.append(row(case_id, h["run"], "hand-written", h["verdict"], h["assessment"],
                            evidence, h.get("recorded_score")))
        for r in case.get("replay_runs", []):
            evidence = [Evidence(c["tool"], c["input"], "", c["is_error"]) for c in r["tool_calls"]]
            last = r["submissions"][-1]["assessment"] if r["submissions"] else None
            rows.append(row(case_id, f"replay {r['date']} {r['model']}", "model replay",
                            "keyword check " + ("passes" if r["keyword_check"]["passed"] else "fails"),
                            last, evidence, r["score"]))
    return rows


def standard_pack(service: str, namespace: str) -> list:
    """The checks a case should record, so replays are not short of the basics.

    Both models lost points on the emailservice replay because describe_deployment
    and list_hpas had no recording; every captured case now has them.
    """
    pod = f'namespace="{namespace}",pod=~"{service}-.*"'
    return [
        ("list_pods", {"app": service}),
        ("list_pods", {}),
        ("list_events", {"app": service}),
        ("list_events", {"warnings_only": True}),
        ("list_deployments", {}),
        ("describe_deployment", {"name": service}),
        ("list_hpas", {}),
        ("prometheus_query_range", {"query": f'sum by (pod) (container_memory_working_set_bytes{{{pod},container!=""}})',
                                    "minutes": 60}),
        ("prometheus_query_range", {"query": f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod},container!=""}}[5m]))',
                                    "minutes": 60}),
        ("prometheus_query", {"query": f"sum by (pod) (kube_pod_container_status_restarts_total{{{pod}}})"}),
    ]


# Calls capture does not record: the agent's own bookkeeping, and the repository
# tools, which a replay answers from git at the case's commit
NOT_RECORDED = frozenset({"submit_assessment", "propose_values_change",
                          "read_repo_file", "recent_chart_commits"})


def pick_investigation(data, service: str, investigation_id: str = None) -> dict:
    """The record to capture from: a saved GET /investigations list (newest first),
    one record of it, or a GET /investigations/<id>/recording."""
    records = data if isinstance(data, list) else [data]
    if investigation_id:
        found = [r for r in records if r.get("id") == investigation_id]
    else:
        found = [r for r in records if r.get("kind") == "alert"
                 and str(r.get("subject", "")).endswith("/" + service)]
    if not found:
        have = ", ".join(f"{r.get('id')} ({r.get('subject')})" for r in records) or "none"
        raise ValueError(f"no investigation {investigation_id or 'of an alert for ' + service} "
                         f"in the file; it holds: {have}")
    return found[0]


def alert_payload(data, service: str, namespace: str, alertname: str = None) -> dict:
    """An AlertManager webhook payload with only this service's firing alerts.

    Accepts a saved webhook payload, or the list GET /api/v2/alerts returns
    (the same labels, annotations and startsAt, with a different wrapper).
    """
    import alerts

    def is_firing(a):
        status = a.get("status", "firing")
        return status == "firing" or (isinstance(status, dict) and status.get("state") == "active")

    kept = []
    for a in (data if isinstance(data, list) else data.get("alerts", [])):
        labels = a.get("labels", {})
        if (is_firing(a) and labels.get("namespace") == namespace
                and alerts.service_of(labels) == service
                and alertname in (None, labels.get("alertname"))):
            kept.append({"status": "firing", "labels": labels,
                         "annotations": a.get("annotations", {}), "startsAt": a.get("startsAt")})
    if not kept:
        what = f"{alertname} alert" if alertname else "alert"
        raise ValueError(f"no firing {what} for {namespace}/{service} in the alert file")
    return {"version": "4", "status": "firing", "receiver": "sre-agent", "alerts": kept}


def _redact_tree(value, names: dict):
    from redact import redact_public
    if isinstance(value, str):
        return redact_public(value, names)
    if isinstance(value, list):
        return [_redact_tree(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v, names) for k, v in value.items()}
    return value


def capture(registry, case_path: str, service: str, namespace: str, question: str,
            repo_ref: str, now=None, investigation: dict = None, alert: dict = None) -> dict:
    """Record a case from the live system into files beside the case file.

    Recorded, in this order:
    - the calls the agent made in `investigation` (a /investigations record),
      except repository reads, which a replay takes from git at `repo_ref`.
      With a recording (the record carries each output), the output is the
      one the agent saw. Without, the same call is run again now, so capture
      while the cluster still shows the problem. A call that fails is recorded
      as a failure, and fails the same way in a replay.
    - the standard pack, for the calls a later run may make instead.
    `alert` (a webhook payload) is saved with the case so a replay can run it
    as an alert, with the proposal tool. Everything is redacted with one set
    of placeholders, because it goes into Git: <node-1> is the same node in
    every file of the case.
    """
    from redact import redact_public

    captured_at = now or datetime.now(timezone.utc)
    case_dir = os.path.dirname(os.path.abspath(case_path))
    case_id = os.path.splitext(os.path.basename(case_path))[0]
    out_dir = os.path.join(case_dir, f"{case_id}-recorded")
    os.makedirs(out_dir, exist_ok=True)
    names: dict = {}

    calls, seen = [], set()          # (tool, input, output, is_error, source)
    for e in (investigation or {}).get("evidence", []):
        key = (e["tool"], _normalise(e.get("input")))
        if e["tool"] in NOT_RECORDED or key in seen:
            continue
        seen.add(key)
        if "output" in e:
            calls.append((e["tool"], e.get("input") or {}, e["output"], e["is_error"], "agent"))
        else:
            output, is_error = registry.run(e["tool"], e.get("input") or {})
            calls.append((e["tool"], e.get("input") or {}, output, is_error, "agent call, run again"))
    agent_calls = len(calls)
    for tool, args in standard_pack(service, namespace):
        if (tool, _normalise(args)) in seen:
            continue
        output, is_error = registry.run(tool, args)
        if not is_error:   # not recorded: a replay will say it is unavailable, which is true
            calls.append((tool, args, output, False, "standard pack"))

    recorded = []
    for i, (tool, args, output, is_error, source) in enumerate(calls, 1):
        name = f"{i:02d}-{tool}.txt"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(redact_public(output, names))
        item = {"tool": tool, "input": _redact_tree(args, names),
                "file": f"{case_id}-recorded/{name}", "source": source}
        if is_error:
            item["is_error"] = True
        recorded.append(item)

    if os.path.exists(case_path):
        with open(case_path, encoding="utf-8") as f:
            case = json.load(f)
    else:
        case = {"id": case_id}
    case["mode"] = "alert" if alert else case.get("mode", "ask")
    # With a recording the outputs date from the investigation; otherwise from now
    exact = bool(investigation) and all(s == "agent" for *_, s in calls[:agent_calls])
    at = (investigation or {}).get("started_at") if exact else None
    how = ("with the outputs the agent saw" if exact else "run again at capture time")
    case["replay"] = {
        "question": question,
        "now": at or captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "namespace": namespace,
        "repo_ref": repo_ref,
        "recorded": recorded,
        "note": (f"Captured with `cli.py capture` on {captured_at:%Y-%m-%d}: {agent_calls} call(s) "
                 f"the agent made" + (f" ({how})" if agent_calls else "")
                 + f", {len(calls) - agent_calls} from the standard pack. Redacted."),
    }
    if alert:
        case["alert"] = _redact_tree(alert, names)
    if investigation:
        keep = ("id", "kind", "subject", "started_at", "finished_at", "model", "outcome",
                "answer", "note", "confidence", "pull_request")
        live = {k: investigation[k] for k in keep if k in investigation}
        live["tool_calls"] = [{"tool": e["tool"], "input": e.get("input"), "is_error": e["is_error"]}
                              for e in investigation.get("evidence", [])]
        case["live_run"] = _redact_tree(live, names)
    with open(case_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(case, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return case["replay"]
