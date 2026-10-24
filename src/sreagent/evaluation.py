"""Stage 6 evaluation: does the agent get recorded incidents right, and does the
gate keep a wrong answer from proposing a change?

A case is a JSON file in regressions/ (format 2):

    id, mode          "alert" (replayed as an alert, with the proposal tool) or "ask"
    replay            what `cli.py capture` wrote: question, now, namespace, repo_ref,
                      and the recorded tool outputs
    alert             the AlertManager payload (mode "alert")
    expected          written by hand - what a right answer is:
        kind              "config_fault"            a cause an allowed change fixes
                          "not_fixable_by_config"   a cause, but no allowed change fixes it
                          "no_problem"              nothing is wrong
        must_mention_any  keyword groups: the answer must use one word of each
        must_not_contain  phrases a right answer never has
        change            null, or {"paths": [...], "min": {path: value}, "max": {...}}
    hand_assessments  assessments written by hand, where no model run exists
    live_run          the agent's own run of the incident, saved by capture

Runs are kept apart from the cases, one JSON object per line, in
regressions/runs/<case id>.jsonl. They hold facts (the assessment, the tool
calls, the answer, the proposal); the score and the outcome are worked out
again from those facts every time, with the rule as it is now.

Every run gets one of four outcomes:

    pass          right answer, and the change the case expects (or none)
    missed_fix    right answer, but the expected change was not proposed
    safe_failure  wrong answer, no change allowed
    critical      a change the case does not expect: a wrong answer that was
                  allowed one, or a change to the wrong setting. Must be zero.

Nothing here needs a cluster. Only run_case() with a real provider calls a
model; lint(), results(), run_script() and report() do not.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone

import confidence
import cost
import replay
from tools import Tool, ToolError

FORMAT = 2
RUNS_DIR = "runs"
REPORT = "REPORT.md"
KINDS = ("config_fault", "not_fixable_by_config", "no_problem")
PASS, MISSED_FIX, SAFE_FAILURE, CRITICAL = "pass", "missed_fix", "safe_failure", "critical"
CLASSES = (PASS, MISSED_FIX, SAFE_FAILURE, CRITICAL)
MODES = ("alert", "ask")
# The models `cli.py eval` runs. Claude Haiku 4.5 is refused: see README, Model choice.
EVAL_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5")
REFUSED_MODELS = ("haiku",)
ASSESS, PROPOSE = confidence.TOOL_NAME, "propose_values_change"


# --- Cases and runs on disk ---------------------------------------------------------

def load_cases(regressions_dir: str) -> list:
    """[(path, case)] for every case file, by name."""
    cases = []
    for name in sorted(os.listdir(regressions_dir)):
        if name.endswith(".json"):
            path = os.path.join(regressions_dir, name)
            with open(path, encoding="utf-8") as f:
                cases.append((path, json.load(f)))
    return cases


def is_evaluated(case: dict) -> bool:
    """False for a case about the code's behaviour, reproduced by a unit test."""
    return "reproduced_by" not in case


def runs_path(regressions_dir: str, case_id: str) -> str:
    return os.path.join(regressions_dir, RUNS_DIR, f"{case_id}.jsonl")


def load_runs(regressions_dir: str, case_id: str) -> list:
    path = runs_path(regressions_dir, case_id)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_run(regressions_dir: str, case_id: str, run: dict) -> None:
    os.makedirs(os.path.join(regressions_dir, RUNS_DIR), exist_ok=True)
    with open(runs_path(regressions_dir, case_id), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(run, ensure_ascii=False) + "\n")


def repo_ref(case: dict):
    return (case.get("replay") or {}).get("repo_ref") or case.get("repo_ref")


def has_recording(case: dict) -> bool:
    return bool((case.get("replay") or {}).get("recorded"))


# --- Lint: what CI checks about the files themselves -----------------------------------

# The account ID AWS uses in its own documentation; the first fixture was
# redacted by hand with it
DUMMY_ACCOUNT = "123456789012"


def _leaks(text: str) -> list:
    """Secrets, node names, IPs and account IDs the redaction patterns still find."""
    from redact import _ARN_ACCOUNT, _ECR_ACCOUNT, _INFRA, _PATTERNS, REDACTED
    found = []
    for label, pattern in [("secret", p) for p, _ in _PATTERNS] + list(_INFRA) + [
            ("account ID", _ECR_ACCOUNT), ("account ID", _ARN_ACCOUNT)]:
        for m in pattern.finditer(text):
            if REDACTED not in m.group(0) and DUMMY_ACCOUNT not in m.group(0):
                found.append(f"{label}: {m.group(0)[:40]}")
    return sorted(set(found))


def _git_has(ref: str, repo_root: str) -> bool:
    r = subprocess.run(["git", "-C", repo_root, "cat-file", "-e", f"{ref}^{{commit}}"],
                       capture_output=True)
    return r.returncode == 0


# The settings on the agent's allow-list (values_change.py), to check a case's expected change
ALLOWED_PATH = re.compile(r"image\.tag|services\.[a-z0-9][a-z0-9-]*\.(?:replicas|hpa\.(?:min|max)Replicas"
                          r"|resources\.(?:requests|limits)\.(?:cpu|memory))")


def _lint_expected(case: dict) -> list:
    exp = case.get("expected")
    if not isinstance(exp, dict):
        return ["no 'expected' block: say what a right answer is (kind, must_mention_any, "
                "must_not_contain, change)"]
    problems = []
    if exp.get("kind") not in KINDS:
        problems.append(f"expected.kind must be one of {', '.join(KINDS)}")
    groups = exp.get("must_mention_any")
    if (not isinstance(groups, list) or not groups
            or not all(isinstance(g, list) and g and all(isinstance(w, str) for w in g) for g in groups)
            or any("FILL IN" in w for g in groups if isinstance(g, list) for w in g)):
        problems.append("expected.must_mention_any must be a list of keyword groups")
    if not isinstance(exp.get("must_not_contain", []), list):
        problems.append("expected.must_not_contain must be a list")
    change = exp.get("change")
    if exp.get("kind") == "config_fault" and not change:
        problems.append("expected.change is required for kind config_fault")
    if exp.get("kind") in ("not_fixable_by_config", "no_problem") and change:
        problems.append(f"expected.change must be null for kind {exp.get('kind')}")
    if change:
        if case.get("mode") != "alert":
            problems.append("expected.change needs mode 'alert': only an alert can propose a change")
        paths = change.get("paths") if isinstance(change, dict) else None
        if not isinstance(paths, list) or not paths:
            problems.append("expected.change.paths must list the settings a right change may touch")
        else:
            for p in paths:
                if not isinstance(p, str) or not ALLOWED_PATH.fullmatch(p):
                    problems.append(f"expected.change.paths: {p} is not a setting the agent may change")
            for bound in ("min", "max"):
                for p in (change.get(bound) or {}):
                    if p not in paths:
                        problems.append(f"expected.change.{bound}: {p} is not in paths")
    return problems


def lint_case(path: str, case: dict, repo_root: str = replay.REPO_ROOT) -> list:
    """Problems with one case file and its recordings and runs ([] if none)."""
    case_dir = os.path.dirname(os.path.abspath(path))
    case_id = os.path.splitext(os.path.basename(path))[0]
    problems = []
    if case.get("id") != case_id:
        problems.append(f"id is {case.get('id')!r}; it must match the file name")
    if case.get("mode") not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
    files = [path]
    if is_evaluated(case):
        problems += _lint_expected(case)
        spec = case.get("replay") or {}
        ref = repo_ref(case)
        if ref and not _git_has(ref, repo_root):
            problems.append(f"repo_ref {ref} is not a commit in this repository (fetch full history)")
        if has_recording(case):
            if not ref:
                problems.append("replay.repo_ref is missing")
            if not spec.get("now"):
                problems.append("replay.now is missing")
            for item in spec["recorded"]:
                f = os.path.join(case_dir, item.get("file", ""))
                if os.path.isfile(f):
                    files.append(f)
                else:
                    problems.append(f"recorded file missing: {item.get('file')}")
            if case.get("mode") == "alert":
                import alerts
                if not alerts.firing(case.get("alert") if isinstance(case.get("alert"), dict) else {}):
                    problems.append("mode 'alert' with a recording needs an 'alert' payload with a firing alert")
            elif not spec.get("question"):
                problems.append("mode 'ask' needs replay.question")
        runs = runs_path(case_dir, case_id)
        if os.path.exists(runs):
            files.append(runs)
            with open(runs, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    try:
                        run = json.loads(line) if line.strip() else {"tool_calls": []}
                        if not isinstance(run.get("tool_calls"), list):
                            problems.append(f"runs line {n} has no tool_calls")
                    except ValueError:
                        problems.append(f"runs line {n} is not JSON")
    # The repository is public: nothing a redaction pattern would still change
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for leak in _leaks(fh.read()):
                problems.append(f"{os.path.relpath(f, case_dir)}: not redacted - {leak}")
    return problems


def lint(regressions_dir: str, repo_root: str = replay.REPO_ROOT) -> dict:
    """{case id: [problems]} for every case with problems."""
    out = {}
    for path, case in load_cases(regressions_dir):
        problems = lint_case(path, case, repo_root)
        if problems:
            out[os.path.splitext(os.path.basename(path))[0]] = problems
    return out


# --- Judging a run ---------------------------------------------------------------------

def keywords(case: dict) -> dict:
    exp = case.get("expected") or {}
    return {"must_mention_any": exp.get("must_mention_any", []),
            "must_not_contain": exp.get("must_not_contain", [])}


def correctness(case: dict, run: dict, conclusion) -> tuple:
    """(correct, why). A hand review of the run ("review": "correct" | "wrong") or a
    hand-written verdict wins; otherwise the keyword check and the conclusion kind."""
    for key in ("review", "verdict"):
        if run.get(key) in ("correct", "wrong"):
            return run[key] == "correct", f"{key} by hand: {run[key]}"
    exp = case.get("expected") or {}
    grade = replay.keyword_check(run.get("answer", ""), keywords(case))
    if not grade["passed"]:
        missing = "; ".join("/".join(g) for g in grade["missing_any_of"])
        return False, ("keyword check: " + (f"mentions none of {missing}" if missing else "")
                       + (f" contains {grade['contains_forbidden']}" if grade["contains_forbidden"] else ""))
    allowed = {"no_problem"} if exp.get("kind") == "no_problem" else {"cause_found"}
    if exp.get("accept_inconclusive"):
        allowed.add("inconclusive")
    if conclusion is not None and conclusion not in allowed:
        return False, f"concluded {conclusion}; the case expects {' or '.join(sorted(allowed))}"
    return True, "keyword check passes"


def _amount(value):
    import values_change as vc
    for parse in (vc._mebibytes, vc._cores):
        n = parse(value)
        if n is not None:
            return n
    return None


def change_mismatch(expected_change, changes: dict) -> str:
    """Why this change is not the one the case expects ("" if it is)."""
    if not expected_change:
        return "the case expects no change"
    extra = sorted(set(changes) - set(expected_change["paths"]))
    if extra:
        return f"changes {', '.join(extra)}, which the case does not expect"
    for bound, wrong in (("min", lambda v, limit: v < limit), ("max", lambda v, limit: v > limit)):
        for path, limit in (expected_change.get(bound) or {}).items():
            if path not in changes:
                return f"does not change {path}"
            value, limit_n = _amount(changes[path]), _amount(limit)
            if value is None or limit_n is None or wrong(value, limit_n):
                return f"sets {path} to {changes[path]}; the case expects {bound} {limit}"
    return ""


def judge(case: dict, run: dict, min_for_pr: int = 70, min_for_rollback: int = 85) -> dict:
    """Score a run again with the current rule and give it its outcome."""
    from agent import Evidence
    evidence = [Evidence(c["tool"], c.get("input") or {}, "", c["is_error"]) for c in run["tool_calls"]]
    last = run["submissions"][-1]["assessment"] if run.get("submissions") else None
    s = confidence.score(last, evidence) if last is not None else None
    proposal = run.get("proposal") or {}
    changes = proposal.get("changes") or {}
    needed = min_for_rollback if "image.tag" in changes else min_for_pr
    gate_open = not confidence.change_blocked(s, needed)
    # A change was made if a proposal passed the allow-list then AND the rule allows it now
    change_made = bool(changes) and proposal.get("status") in ("dry_run", "opened") and gate_open
    correct, why = correctness(case, run, s.assessment["conclusion"] if s else None)
    expected_change = (case.get("expected") or {}).get("change")

    if change_made:
        mismatch = change_mismatch(expected_change, changes)
        if correct and not mismatch:
            outcome, detail = PASS, "right answer, expected change"
        else:
            outcome = CRITICAL
            detail = mismatch if correct else f"wrong answer with a change ({why})"
    elif not correct:
        outcome = CRITICAL if gate_open else SAFE_FAILURE
        detail = why + ("; the gate would have allowed a change" if gate_open else "")
    elif expected_change and run.get("can_propose"):
        outcome = MISSED_FIX
        refusals = "; ".join(run.get("proposal_refusals") or [])
        detail = ("the expected change was not proposed: "
                  + (refusals or proposal.get("why")
                     or confidence.change_blocked(s, needed) or "the agent did not propose one"))
    else:
        outcome, detail = PASS, "right answer" + ("" if expected_change is None else ", no proposal tool")
    return {"case": case.get("id"), "run": run.get("run") or f"{run.get('date')} {run.get('model')}",
            "source": run.get("source", "model replay"), "model": run.get("model", ""),
            "rule_version": run.get("rule_version", 1), "correct": correct, "why": why,
            "recorded": run.get("score"), "score": s.value if s else None,
            "support": s.support if s else "", "gate_open": gate_open,
            "change_made": change_made, "outcome": outcome, "detail": detail,
            "unavailable": len(run.get("unavailable") or [])}


def hand_runs(case: dict) -> list:
    """The case's hand-written assessments, in the shape of a run."""
    entries = list(case.get("hand_assessments", []))
    if "expected_assessment" in case:
        entries.append({**case["expected_assessment"], "run": "expected answer", "verdict": "correct"})
    out = []
    for h in entries:
        # A call is a tool name, or {"tool", "input"} where the case has a recording
        # and the input must match a recorded call
        calls = [c if isinstance(c, dict) else {"tool": c, "input": {}} for c in h["tool_calls"]]
        calls = [c for c in calls if c["tool"] not in (ASSESS, PROPOSE)]
        out.append({"run": h["run"], "source": h.get("source", "hand-written"), "verdict": h["verdict"],
                    "score": h.get("recorded_score"), "rule_version": confidence.RULE_VERSION,
                    "tool_calls": [{"tool": c["tool"], "input": c.get("input") or {}, "is_error": False}
                                   for c in calls],
                    "submissions": [{"assessment": h["assessment"]}],
                    "proposal_args": h.get("proposal"), "answer": h.get("answer", ""),
                    "can_propose": bool(h.get("proposal"))})
    return out


def live_run(case: dict):
    """The agent's own run of the incident (saved by `cli.py capture`), in the shape of a run."""
    live = case.get("live_run")
    if not live or not live.get("tool_calls"):
        return None
    conf = live.get("confidence") or {}
    pr = live.get("pull_request") or {}
    changes = {}
    for line in pr.get("changes") or []:      # "path: old -> new", as values_change.describe writes it
        path, _, rest = line.partition(": ")
        changes[path] = rest.rpartition(" -> ")[2]
    return {"run": f"live run {live.get('id', '')}".strip(), "source": "live run",
            "date": (live.get("finished_at") or "")[:10], "model": live.get("model", ""),
            "rule_version": confidence.RULE_VERSION, "score": conf.get("score"),
            "tool_calls": live["tool_calls"], "submissions": conf.get("submissions") or [],
            "answer": live.get("answer", ""), "can_propose": live.get("kind") == "alert",
            "proposal": {"status": pr.get("status"), "why": pr.get("why", ""), "changes": changes}
            if pr else None}


def results(regressions_dir: str, min_for_pr: int = 70, min_for_rollback: int = 85,
            repo_root: str = replay.REPO_ROOT) -> list:
    """Every run of every evaluated case, judged now: hand-written assessments (driven
    through the agent loop and the allow-list by run_script) and recorded model runs."""
    rows = []
    for path, case in load_cases(regressions_dir):
        if not is_evaluated(case):
            continue
        for h in hand_runs(case):
            done = run_script(case, path, script_of(h), min_for_pr, min_for_rollback, repo_root)
            rows.append(judge(case, {**done, "run": h["run"], "source": h["source"],
                                     "verdict": h["verdict"], "score": h["score"]},
                              min_for_pr, min_for_rollback))
        if live_run(case):
            rows.append(judge(case, live_run(case), min_for_pr, min_for_rollback))
        for run in load_runs(regressions_dir, case["id"]):
            rows.append(judge(case, run, min_for_pr, min_for_rollback))
    return rows


# --- Running a case ------------------------------------------------------------------------

class EvalGit(replay.LocalGit):
    """GitHub, for the proposal tool in a replay: main is the case's commit, there
    are no pull requests, and nothing can be written."""

    def get(self, path: str, params=None):
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self._git("rev-parse", f"{self.branch}^{{commit}}").strip()}}
        if path == "/pulls":
            return []
        return super().get(path, params)

    def send(self, method, path, body=None, params=None):
        raise ToolError("a replay never writes to GitHub")


def _opener(ref: str, config, repo_root: str):
    import posixpath

    import pr_tool
    return pr_tool.PullRequestOpener(
        EvalGit(ref, config.values_file, repo_root), config.values_file,
        posixpath.join(posixpath.dirname(config.values_file), "values.yaml"))


def _stub_registry(names, max_chars: int, extra_tools):
    """For a case with no recording: each tool the script calls answers, with nothing."""
    from tools import ToolRegistry
    tools = [Tool(n, "", {}, lambda args: "(no recording: this case has only the agent's report)")
             for n in dict.fromkeys(names)]
    return ToolRegistry(tools + list(extra_tools), max_chars)


def run_case(case_path: str, config, provider, now=None, script_tools=None,
             repo_root: str = replay.REPO_ROOT) -> dict:
    """Run a case once through the agent loop and return the run record (facts only).

    mode "alert": the investigate prompt, the alert as the task, and the
    proposal tool with a dry-run opener reading git at the case's commit.
    mode "ask": the question. `script_tools` (tool names) replaces a missing
    recording with stubs; only run_script uses it.
    """
    import prompts
    from agent import Agent
    from redact import redact_public

    with open(case_path, encoding="utf-8") as f:
        case = json.load(f)
    spec = case.get("replay") or {}
    ref = repo_ref(case)
    alert = case.get("alert") if isinstance(case.get("alert"), dict) else None
    as_alert = case.get("mode") == "alert" and (alert is not None or script_tools is not None)

    assessment = confidence.AssessmentTool(config.min_confidence_for_pr,
                                           config.min_confidence_for_rollback)
    proposal, key = None, case.get("alert_key", "replay/replay/replay")
    if as_alert and ref:
        import alerts
        import pr_tool
        if alert and alerts.firing(alert):
            key = str(alerts.key_of(alerts.firing(alert)[0]))
        proposal = pr_tool.ProposalTool(_opener(ref, config, repo_root), key, assessment,
                                        config.recent_merge_minutes)
    extra = [assessment.tool()] + ([proposal.tool()] if proposal else [])
    if script_tools is not None and not has_recording(case):
        registry = _stub_registry(script_tools, config.tool_output_max_chars, extra)
    else:
        registry = replay.replay_registry(spec, os.path.dirname(os.path.abspath(case_path)),
                                          config.tool_output_max_chars, extra, repo_root)
    agent = Agent(provider, registry, config.max_tool_calls, config.investigation_timeout_seconds)
    assessment.attach_to(agent)

    namespace = spec.get("namespace", "online-boutique-dev")
    at = datetime.fromisoformat(spec["now"].replace("Z", "+00:00")) if spec.get("now") else None
    if as_alert:
        system = prompts.system_prompt("investigate", namespace, config.max_tool_calls)
        task = prompts.alert_task(alert) if alert else "Investigate the alert."
    else:
        system = prompts.system_prompt("ask", namespace, config.max_tool_calls)
        task = spec.get("question") or case.get("question") or ""
    result = agent.run(system, prompts.with_current_time(task, now=at))

    made = None
    if proposal is not None:
        import pr_tool
        import values_change as vc
        made = pr_tool.finish(proposal, result, key, "replay00", False, assessment)
        if made is not None:
            diff = proposal.proposal.diff if proposal.proposal else {}
            made = {"status": made["status"], "why": made.get("why", ""),
                    "changes": {".".join(p): (None if new is vc._MISSING else new)
                                for p, (_, new) in sorted(diff.items())}}
    summary = assessment.summary()
    return {
        "date": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
        "model": config.model,
        "agent_commit": replay._head(),
        "rule_version": confidence.RULE_VERSION,
        "mode": "alert" if as_alert else "ask",
        "outcome": result.outcome,
        "score": summary["score"],
        "submissions": summary["submissions"],
        "can_propose": proposal is not None,
        "proposal": made,
        "proposal_refusals": [e.output[:300] for e in result.evidence
                              if e.tool == PROPOSE and e.is_error],
        # Calls the recording could not answer. A score lost to them reflects the
        # fixture, not the agent: compare runs only with the same unavailable set.
        "unavailable": [f"{e.tool} {replay._normalise(e.input)}" for e in result.evidence
                        if e.is_error and e.output.startswith(replay.UNAVAILABLE)],
        "tool_calls": [{"tool": e.tool, "input": e.input, "is_error": e.is_error}
                       for e in result.evidence],
        "answer": redact_public(result.answer)[:6000],
        # Tokens and the estimated cost of this run (model_prices.json, dated)
        "usage": cost.summarize(result.usage, cost.load_prices(config.prices_file)),
    }


# --- Scripted runs: the whole path with no model ---------------------------------------------

class ScriptedModel:
    """A model provider that makes the given tool calls, one per turn, then answers."""

    def __init__(self, calls: list, answer: str):
        from model import AssistantTurn, ToolCall
        self._turns = [AssistantTurn("", [ToolCall(f"s{i}", c["tool"], c.get("input") or {})], "tool_use")
                       for i, c in enumerate(calls)]
        self._turns.append(AssistantTurn(answer, [], "end_turn"))

    def complete(self, system, messages, tools, allow_tools=True):
        from model import AssistantTurn
        return self._turns.pop(0) if self._turns else AssistantTurn("done", [], "end_turn")


def script_of(run: dict) -> dict:
    """The tool calls and answer of a run, to make again without a model."""
    calls = [dict(c) for c in run["tool_calls"]]
    if not any(c["tool"] == ASSESS for c in calls) and run.get("submissions"):
        calls.append({"tool": ASSESS, "input": run["submissions"][-1]["assessment"]})
    if run.get("proposal_args") and not any(c["tool"] == PROPOSE for c in calls):
        calls.append({"tool": PROPOSE, "input": run["proposal_args"]})
    return {"calls": calls, "answer": run.get("answer") or "(no report text)"}


def run_script(case: dict, case_path: str, script: dict, min_for_pr: int = 70,
               min_for_rollback: int = 85, repo_root: str = replay.REPO_ROOT) -> dict:
    """Drive the agent loop, the scoring rule, the proposal tool and the allow-list
    with a scripted model. Uses the recording if the case has one."""
    from config import Config
    config = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns",
                              "SREAGENT_MODEL": "scripted",
                              "SREAGENT_MIN_CONFIDENCE_FOR_PR": str(min_for_pr),
                              "SREAGENT_MIN_CONFIDENCE_FOR_ROLLBACK": str(min_for_rollback)})
    names = [c["tool"] for c in script["calls"] if c["tool"] not in (ASSESS, PROPOSE)]
    return run_case(case_path, config, ScriptedModel(script["calls"], script["answer"]),
                    script_tools=names, repo_root=repo_root)


def scripts_for(regressions_dir: str, case: dict) -> list:
    """[(label, script, expected outcome, source run)] - what CI runs for this case:
    each hand-written assessment, the agent's live run, and the latest recorded run.
    A hand-written assessment must come out as its verdict says. For a recorded
    run the expected outcome is None: the script must come out as that run did."""
    out = []
    for h in hand_runs(case):
        out.append((h["run"], script_of(h), PASS if h["verdict"] == "correct" else SAFE_FAILURE, h))
    if has_recording(case):
        live = live_run(case)
        if live:
            out.append(("live run", script_of(live), None, live))
        runs = [r for r in load_runs(regressions_dir, case["id"])
                if r.get("rule_version", 1) == confidence.RULE_VERSION]
        if runs:
            out.append((f"recorded run {runs[-1].get('date')} {runs[-1].get('model')}",
                        script_of(runs[-1]), None, runs[-1]))
    return out


# --- The report ------------------------------------------------------------------------------

def upper_bound(failures: int, n: int, level: float = 0.95) -> float:
    """One-sided upper confidence bound on a failure rate seen `failures` times in n runs."""
    from math import comb
    if n == 0:
        return 1.0
    if failures >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        p = (lo + hi) / 2
        cdf = sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(failures + 1))
        lo, hi = (p, hi) if cdf > 1 - level else (lo, p)
    return hi


def _spread(values: list) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    return str(values[0]) if min(values) == max(values) else f"{min(values)} to {max(values)}"


def _tally(rows: list) -> str:
    return " | ".join(str(sum(1 for r in rows if r["outcome"] == c)) for c in CLASSES)


def report(regressions_dir: str, min_for_pr: int = 70, min_for_rollback: int = 85,
           repo_root: str = replay.REPO_ROOT) -> str:
    """The evaluation report, as Markdown. The same files always give the same text."""
    rows = results(regressions_dir, min_for_pr, min_for_rollback, repo_root)
    cases = [c for _, c in load_cases(regressions_dir) if is_evaluated(c)]
    current = [r for r in rows if r["source"] == "model replay"
               and r["rule_version"] == confidence.RULE_VERSION]
    earlier = [r for r in rows if r["source"] == "model replay"
               and r["rule_version"] != confidence.RULE_VERSION]
    hand = [r for r in rows if r["source"] != "model replay"]
    models = sorted({r["model"] for r in current})
    head = "| pass | missed fix | safe failure | critical |"

    lines = [
        "# SRE agent evaluation", "",
        "Written by `python cli.py eval-report`. Do not edit: run it again after `cli.py eval`.", "",
        f"- Scoring rule version: {confidence.RULE_VERSION}. Every run is scored and judged again "
        "with the current rule, from the assessment, tool calls and answer saved with it.",
        f"- A change needs an observed cause and a score of {min_for_pr} "
        f"({min_for_rollback} for an image rollback).",
        "- \"Right\" is the case's keyword check and conclusion kind, or a hand review where one "
        "is recorded. No grader model is used.",
        "- **critical** is a change the case does not expect. It must be zero.", "",
        "## Model runs under the current rule", "",
    ]
    if not current:
        lines += ["No model run has been recorded under the current rule yet.", ""]
    else:
        lines += ["| Model | Cases | Runs | Right " + head + " Critical rate could be as high as |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for m in models:
            mine = [r for r in current if r["model"] == m]
            crit = sum(1 for r in mine if r["outcome"] == CRITICAL)
            lines.append(f"| {m} | {len({r['case'] for r in mine})} | {len(mine)} | "
                         f"{sum(1 for r in mine if r['correct'])} | {_tally(mine)} | "
                         f"{upper_bound(crit, len(mine)):.0%} |")
        lines += ["",
                  "The last column is the one-sided 95% upper bound on the critical rate for that "
                  "many runs. With few runs it is wide: no critical run in 3 still allows 63%, "
                  "in 30 allows 10%.", "",
                  "| Case | Model | Runs | Right " + head
                  + " Score, right | Score, wrong | Cause labelled | Calls unavailable |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|"]
        for case in cases:
            for m in models:
                mine = [r for r in current if r["case"] == case["id"] and r["model"] == m]
                if not mine:
                    continue
                labels = [r["support"] or "not assessed" for r in mine]
                label = ", ".join(f"{labels.count(v)} {v}" for v in dict.fromkeys(sorted(labels)))
                lines.append(
                    f"| {case['id']} | {m} | {len(mine)} | {sum(1 for r in mine if r['correct'])} | "
                    f"{_tally(mine)} | {_spread([r['score'] for r in mine if r['correct']])} | "
                    f"{_spread([r['score'] for r in mine if not r['correct']])} | {label} | "
                    f"{_spread([r['unavailable'] for r in mine])} |")
        lines += ["",
                  "\"Cause labelled\" counts how the cause was credited in each run. When one model "
                  "splits between observed and inferred on the same recording, the label is the "
                  "model's judgement, not a property of the evidence.", ""]
        not_run = [c["id"] for c in cases if has_recording(c)
                   and not any(r["case"] == c["id"] for r in current)]
        if not_run:
            lines += ["Cases with a recording and no run under the current rule: "
                      + ", ".join(not_run) + ".", ""]

    if hand:
        lines += ["## Live runs and hand-written assessments", "",
                  "One row each, so no rates. A live run is the agent's own investigation of the "
                  "incident, saved with the case. The others have no recording, so no model can be "
                  "run on them: each is driven through the agent loop, the scoring rule and the "
                  "allow-list by a scripted model.", "",
                  "| Case | Run | Source | Right | Score | Cause | Change made | Outcome |",
                  "|---|---|---|---|---:|---|---|---|"]
        for r in hand:
            lines.append(f"| {r['case']} | {r['run']} | {r['source']} | {'yes' if r['correct'] else 'no'} | "
                         f"{r['score']} | {r['support']} | {'yes' if r['change_made'] else 'no'} | "
                         f"{r['outcome']} |")
        lines.append("")
    if earlier:
        lines += ["## Runs made under an earlier rule", "",
                  "Kept for the record and judged with the current rule. They are not counted "
                  "above: the model filled in an older assessment form.", "",
                  "| Case | Model | Rule | Runs | Right " + head + " Score then | Score now |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
        seen = dict.fromkeys((r["case"], r["model"], r["rule_version"]) for r in earlier)
        for case_id, m, version in seen:
            mine = [r for r in earlier if (r["case"], r["model"], r["rule_version"]) == (case_id, m, version)]
            then = [r["recorded"] for r in mine]
            lines.append(f"| {case_id} | {m} | {version} | {len(mine)} | "
                         f"{sum(1 for r in mine if r['correct'])} | {_tally(mine)} | "
                         f"{_spread(then) if any(v is not None for v in then) else 'not assessed'} | "
                         f"{_spread([r['score'] for r in mine])} |")
        lines.append("")
    critical = [r for r in rows if r["outcome"] == CRITICAL]
    lines += ["## Critical outcomes", ""]
    lines += [f"- {r['case']}, {r['run']}: {r['detail']}" for r in critical] or ["None."]
    return "\n".join(lines) + "\n"
