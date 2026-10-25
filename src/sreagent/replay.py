"""Replay a regression case: the real model, recorded evidence.

The model and the agent loop run for real (submit_assessment included), but
the tools answer from the case's recording instead of a cluster:
- calls recorded in the case (tool + input) return the saved output;
- the repository tools (read_repo_file, recent_chart_commits) read local git
  at the commit the cluster was running when the case was recorded;
- every other call fails with "no recorded output", which the model sees.

So a replay measures how the agent reasons over the evidence that was really
there, and lets its evidence score be compared with the known right answer.

Running a case and judging the run is evaluation.py:

    python cli.py eval emailservice-pod-replaced
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
    namespace = spec.get("namespace", "online-boutique-dev")
    argocd_namespace = spec.get("argocd_namespace", "argocd")

    def canonical(name, args):
        # get_resource validates its input before looking anything up, as live:
        # a replay rejects a Secret or another namespace the same way, and
        # finds the recording whether or not the model named the namespace
        if name == "get_resource":
            return k8s_tools.canonical_resource_args(args or {}, namespace, argocd_namespace)
        if name == "get_pod_logs":
            # The live tool's defaults (100 lines, the current instance) are
            # the same request whether or not the model spelled them out
            args = dict(args or {})
            if _tail_lines(args) == 100:
                args.pop("tail_lines", None)
            if not args.get("previous"):
                args.pop("previous", None)
        return args

    recorded = {}
    log_tails = {}      # (pod, container, previous) -> [(tail_lines, output)], successful calls only
    for item in spec.get("recorded", []):
        with open(os.path.join(case_dir, item["file"]), encoding="utf-8") as f:
            output = f.read()
        recorded[(item["tool"], _normalise(canonical(item["tool"], item.get("input"))))] = (
            output, item.get("is_error", False))
        if item["tool"] == "get_pod_logs" and not item.get("is_error"):
            log_tails.setdefault(_log_key(item.get("input")), []).append(
                (_tail_lines(item.get("input")), output))

    served = []         # substitutions made, for the run record

    def from_recording(name):
        def handler(args):
            key = (name, _normalise(canonical(name, args)))
            if key in recorded:
                output, is_error = recorded[key]
                if is_error:      # the call failed when it was recorded: fail the same way
                    raise ToolError(output)
                return output
            if name == "get_pod_logs" and log_tails.get(_log_key(args)):
                # The same pod's log, recorded with another tail_lines: serve that
                # recording as it is, never cut or padded, and say so in the run
                wanted = _tail_lines(args)
                tails = log_tails[_log_key(args)]
                tail, output = min((t for t in tails if t[0] >= wanted), default=None,
                                   key=lambda t: t[0]) or max(tails, key=lambda t: t[0])
                served.append(f"get_pod_logs {key[1]}: served recorded tail {tail} for {wanted}")
                return output
            raise ToolError(f"{UNAVAILABLE} for {name} {key[1]} in this replay: the call was "
                            "not made when the case was recorded. Treat it as unavailable.")
        return handler

    live = (prometheus_tools.make_tools(prometheus_tools.PrometheusClient("http://replay.invalid"))
            + k8s_tools.make_tools(k8s_tools.KubeReader(namespace, core=object(),
                                                        argocd_namespace=argocd_namespace)))
    tools = [Tool(t.name, t.description, t.input_schema, from_recording(t.name), t.keep, t.strict)
             for t in live]
    git = LocalGit(spec["repo_ref"], spec.get("values_file", "helm/online-boutique/values-dev.yaml"),
                   repo_root)
    recorded_tools = {t.name for t in tools}
    tools += github_tools.make_tools(git)
    return _ReplayRegistry(tools + list(extra_tools), max_chars, recorded_tools, served)


def _log_key(args) -> tuple:
    args = args or {}
    return (args.get("pod"), args.get("container") or None, bool(args.get("previous", False)))


def _tail_lines(args) -> int:
    """tail_lines as the live tool reads it: default 100, kept within 1-500."""
    value = (args or {}).get("tail_lines", 100)
    try:
        return max(1, min(500, int(value)))
    except (TypeError, ValueError):
        return 100


class _ReplayRegistry(ToolRegistry):
    """Recorded outputs were redacted and cut to length when the agent first saw
    them; they are returned as recorded, not cut a second time.

    `served` lists each call answered from a recording made with other input
    (a pod log with another tail_lines), e.g. "served recorded tail 40 for 50"."""

    def __init__(self, tools, max_chars, recorded_tools, served=None):
        super().__init__(tools, max_chars)
        self._recorded_tools = recorded_tools
        self.served = served if served is not None else []

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


def _head() -> str:
    r = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


# The Argo CD Application that deploys the chart (Terraform: modules/argocd, app_name)
ARGOCD_APP = "online-boutique"


def standard_pack(service: str, namespace: str, argocd_app: str = ARGOCD_APP) -> list:
    """The checks a case should record, so replays are not short of the basics.

    Both models lost points on the emailservice replay because describe_deployment
    and list_hpas had no recording; every captured case now has them. Argo CD's
    Applications too: whether the cluster runs what Git says is a question every
    investigation can ask, and the live selector-mismatch capture lacked them.
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
        ("get_resource", {"kind": "applications"}),
        ("get_resource", {"kind": "applications", "name": argocd_app}),
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
            repo_ref: str, now=None, investigation: dict = None, alert: dict = None,
            argocd_app: str = ARGOCD_APP) -> dict:
    """Record a case from the live system into files beside the case file.

    Recorded, in this order:
    - the calls the agent made in `investigation` (a /investigations record),
      except repository reads, which a replay takes from git at `repo_ref`.
      With a recording (the record carries each output), the output is the
      one the agent saw. Without, the same call is run again now, so capture
      while the cluster still shows the problem. A call that fails is recorded
      as a failure, and fails the same way in a replay.
    - the standard pack, for the calls a later run may make instead, with
      Argo CD's Applications (`argocd_app` is the one that deploys the chart).
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

    def same_call(tool, args):
        # get_resource with or without its namespace is one call: record it once
        if tool == "get_resource":
            try:
                args = k8s_tools.canonical_resource_args(args or {}, namespace, "argocd")
            except ToolError:
                pass
        return (tool, _normalise(args))

    calls, seen = [], set()          # (tool, input, output, is_error, source)
    for e in (investigation or {}).get("evidence", []):
        key = same_call(e["tool"], e.get("input"))
        if e["tool"] in NOT_RECORDED or key in seen:
            continue
        seen.add(key)
        if "output" in e:
            calls.append((e["tool"], e.get("input") or {}, e["output"], e["is_error"], "agent"))
        else:
            output, is_error = registry.run(e["tool"], e.get("input") or {})
            calls.append((e["tool"], e.get("input") or {}, output, is_error, "agent call, run again"))
    agent_calls = len(calls)
    for tool, args in standard_pack(service, namespace, argocd_app):
        if same_call(tool, args) in seen:
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
    case["format"] = 2
    case["mode"] = "alert" if alert else case.get("mode", "ask")
    # To be written by hand: what a right answer is. `cli.py lint` fails until it is
    case.setdefault("expected", {
        "kind": "FILL IN: config_fault | not_fixable_by_config | no_problem",
        "must_mention_any": [["FILL IN: a word a right answer uses", "or this one"]],
        "must_not_contain": [],
        "change": None,
    })
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
