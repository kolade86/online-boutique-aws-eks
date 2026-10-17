"""Run the agent from a terminal against the live cluster.

    python cli.py tool list_pods app=cartservice    # one tool, no model call
    python cli.py ask "Why is cartservice restarting?"
    python cli.py investigate examples/alert-podcrashlooping.json

`tool` takes key=value arguments rather than JSON, because Windows PowerShell
5.1 strips the double quotes from JSON passed to native programs.
"""

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import prompts
from config import Config, ConfigError


def _parse_kv(pairs: list[str]) -> dict:
    args = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"Expected key=value, got {pair!r}")
        if value.lower() in ("true", "false"):
            args[key] = value.lower() == "true"
        elif value.isdigit():
            args[key] = int(value)
        else:
            args[key] = value
    return args


def refresh_starts_at(payload: dict, minutes_ago: int, now=None) -> dict:
    """Set every alert's startsAt to `minutes_ago` before now.

    A saved alert file has a fixed start time that soon predates every pod in
    the cluster, which misleads the investigation.
    """
    now = now or datetime.now(timezone.utc)
    starts_at = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for alert in payload.get("alerts", []):
        alert["startsAt"] = starts_at
    return payload


def format_confidence(assessment, note: str = "") -> list[str]:
    """The evidence score block: score, reasons, revisions, and the code's note."""
    import confidence
    if assessment is None:
        return []
    lines = ["", "-" * 72]
    latest = assessment.latest
    if latest is None:
        lines.append(f"Evidence score: not assessed (PRs need {assessment.min_for_pr})")
    else:
        lines.append(f"Evidence score: {latest.value}/100 (PRs need {assessment.min_for_pr}, "
                     f"image.tag rollbacks {assessment.min_for_rollback}) - an evidence score, "
                     "not a probability")
        lines += ["  " + r for r in confidence.format_reasons(latest)]
        if len(assessment.submissions) > 1:
            lines.append("  Revised: " + " -> ".join(
                f"{s.score.value} (after {s.after_calls} calls)" for s in assessment.submissions))
    if note:
        lines += ["", note]
    return lines


def format_usage(usage: list) -> str:
    """Tokens and the estimated cost of a run, priced from model_prices.json."""
    import cost
    s = cost.summarize(usage, cost.load_prices(os.environ.get("SREAGENT_PRICES_FILE")
                                               or cost.PRICES_FILE))
    spent = ("unknown (no price for " + ", ".join(s["unpriced_models"]) + ")"
             if s["cost_usd"] is None else f"${s['cost_usd']:.4f}")
    return (f"Tokens: input {s['input']}, output {s['output']}, cache read {s['cache_read']}, "
            f"cache write {s['cache_write']}   Estimated cost: {spent} "
            f"(prices checked {s['prices_checked']})")


def format_report(result, max_tool_calls: int, header: str = "", assessment=None,
                  note: str = "") -> str:
    from redact import redact_public
    lines = [header, ""] if header else []
    lines += [result.answer]
    lines += format_confidence(assessment, note)
    lines += ["", "-" * 72,
              f"Outcome: {result.outcome}   Tool calls: {len(result.evidence)} of "
              f"{max_tool_calls} allowed   Elapsed: {result.elapsed_seconds:.1f}s"]
    if result.usage:
        lines.append(format_usage(result.usage))
    for i, e in enumerate(result.evidence, 1):
        status = "ERROR" if e.is_error else "ok"
        lines.append(f"  {i:2}. [{status}] {e.tool} {json.dumps(e.input)}")
    # Printed and saved reports (e.g. into regressions/) carry no node names,
    # IPs, account IDs or secrets
    return redact_public("\n".join(lines)) + "\n"


def format_pull_request(pr: dict) -> str:
    lines = ["", "-" * 72, f"Pull request: {pr['status']}"
             + (f" - {pr['url']} (branch {pr['branch']})" if pr.get("url") else "")]
    if pr.get("why"):
        lines.append(f"  {pr['why']}")
    lines += [f"  {c}" for c in pr["changes"]]
    lines += ["", pr["diff"].rstrip("\n")]
    return "\n".join(lines) + "\n"


def _propose(config, wiring, opts) -> int:
    """Validate (and with --open-pr, open) a change without the model or the cluster."""
    import pr_tool
    import values_change as vc

    changes = []
    for pair in opts.changes:
        path, sep, value = pair.partition("=")
        if not sep:
            print(f"Expected path=value, got {pair!r}", file=sys.stderr)
            return 2
        changes.append({"path": path, "value": int(value) if value.isdigit() else value})

    opener = wiring.build_pr_opener(config)
    tool = pr_tool.ProposalTool(opener, opts.key)
    try:
        tool.handle({"changes": changes, "reason": opts.reason})
    except pr_tool.ToolError as e:
        print(e, file=sys.stderr)
        return 1

    if not opts.open_pr:
        p = tool.proposal
        body = pr_tool.pr_body(p, opts.key, "dry-run0", "_(cli.py propose: no investigation)_",
                               [], opener.values_file)
        print(f"Valid change against {opener.gh.branch} ({p.context.base_sha[:8]}):")
        print("\n".join(f"  {c}" for c in vc.describe(p.diff)))
        print(f"\n{p.diff_text}\n--- PR title ---\n{pr_tool.title(p)}\n--- PR body ---\n{body}")
        print("\nDry run: nothing was written. Add --open-pr to open it.")
        return 0

    if not config.github_token:
        print("GITHUB_TOKEN is required to open a pull request", file=sys.stderr)
        return 2
    from agent import ANSWERED, Investigation
    result = Investigation(ANSWERED, "_(Opened with cli.py propose: no investigation was run.)_", [], 0.0)
    pr = pr_tool.finish(tool, result, opts.key, uuid.uuid4().hex[:8], open_prs=True)
    print(format_pull_request(pr), end="")
    return 0 if pr["status"] == "opened" else 1


def _capture(config, wiring, opts) -> int:
    """Record an incident into a regression case. Reads the cluster; calls no model."""
    import replay

    def load(path):
        with open(path, encoding="utf-8-sig") as f:   # PowerShell redirection writes a BOM
            return json.load(f)

    investigation = alert = None
    try:
        if opts.investigations:
            investigation = replay.pick_investigation(load(opts.investigations), opts.service, opts.id)
        alertname = investigation["subject"].split("/")[0] if investigation else None
        if opts.alert_file:
            alert = replay.alert_payload(load(opts.alert_file), opts.service,
                                         config.app_namespace, alertname)
        elif investigation and investigation.get("alert"):
            alert = replay.alert_payload(investigation["alert"], opts.service, config.app_namespace)
    except (OSError, ValueError) as e:
        print(f"capture: {e}", file=sys.stderr)
        return 2
    question = opts.question
    if not question and investigation and investigation.get("kind") == "ask":
        question = investigation["subject"]      # an /ask record: its subject is the question
    if not question and not alert:
        print("capture: give --question, or the alert (--alert-file, or a recording)", file=sys.stderr)
        return 2

    ref = opts.repo_ref or wiring._github(config).get(
        f"/git/ref/heads/{config.github_branch}")["object"]["sha"][:12]
    spec = replay.capture(wiring.build_registry(config), opts.case, opts.service,
                          config.app_namespace, question, ref,
                          investigation=investigation, alert=alert)
    by_source = {}
    for item in spec["recorded"]:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
    print(f"{opts.case}: recorded {len(spec['recorded'])} call(s) - "
          + ", ".join(f"{n} {s}" for s, n in by_source.items()))
    failed = [i["tool"] for i in spec["recorded"] if i.get("is_error")]
    if failed:
        print(f"  recorded as failures: {', '.join(failed)}")
    if investigation:
        print(f"  investigation {investigation.get('id')} ({investigation.get('subject')}), "
              f"finished {investigation.get('finished_at')}")
    else:
        print("  no investigation given: only the standard pack was recorded")
    print(f"  alert: {len(alert['alerts'])} firing alert(s) saved" if alert
          else "  no alert saved: the case can only be replayed as a question")
    print(f"  repository at {ref}; replay `now` is {spec['now']}")
    print("  Read the recorded files before committing: they are redacted by pattern, not by review.")
    return 0


REGRESSIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regressions")


def _case_path(name: str) -> str:
    """A case given as a path, a file name or an id."""
    for candidate in (name, os.path.join(REGRESSIONS, name), os.path.join(REGRESSIONS, name + ".json")):
        if os.path.isfile(candidate):
            return candidate
    raise SystemExit(f"No such case: {name}")


def _thresholds() -> tuple:
    c = Config.from_env({"GITHUB_REPO": "o/r", "APP_NAMESPACE": "ns", **os.environ})
    return c.min_confidence_for_pr, c.min_confidence_for_rollback


def _print_rows(rows) -> None:
    # "then" is the score when the run was made; "now" is the current rule
    print(f"{'case':36} {'run':38} {'right':5} {'then':>4} {'now':>4}  {'cause':9} "
          f"{'change':6} outcome")
    for r in rows:
        then = "-" if r["recorded"] is None else r["recorded"]
        now = "-" if r["score"] is None else r["score"]
        print(f"{r['case'][:36]:36} {r['run'][:38]:38} {'yes' if r['correct'] else 'NO':5} "
              f"{then:>4} {now:>4}  {r['support'] or 'none':9} "
              f"{'made' if r['change_made'] else ('open' if r['gate_open'] else 'no'):6} "
              f"{r['outcome'].upper() if r['outcome'] == 'critical' else r['outcome']}")


def _offline(opts) -> int:
    """scores, lint, eval-report: no cluster, no model, no GitHub."""
    import evaluation
    min_pr, min_rollback = _thresholds()
    if opts.command == "lint":
        problems = evaluation.lint(REGRESSIONS)
        for case_id, found in problems.items():
            print(f"{case_id}:")
            for p in found:
                print(f"  - {p}")
        print("lint: " + (f"{sum(map(len, problems.values()))} problem(s)" if problems else "ok"))
        return 1 if problems else 0
    rows = evaluation.results(REGRESSIONS, min_pr, min_rollback)
    critical = [r for r in rows if r["outcome"] == evaluation.CRITICAL]
    if opts.command == "scores":
        _print_rows(rows)
    else:
        path = os.path.join(REGRESSIONS, evaluation.REPORT)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(evaluation.report(REGRESSIONS, min_pr, min_rollback))
        print(f"wrote {path} ({len(rows)} runs)")
    for r in critical:
        print(f"CRITICAL: {r['case']}, {r['run']}: {r['detail']}", file=sys.stderr)
    return 1 if critical else 0


def _eval(config, cases: list, models: list, runs: int, record: bool) -> int:
    """Run cases through the real model against their recordings; judge and record each run."""
    import dataclasses

    import evaluation
    from anthropic_provider import AnthropicProvider

    refused = [m for m in models if any(r in m.lower() for r in evaluation.REFUSED_MODELS)]
    if refused:
        print(f"Refused: {', '.join(refused)}. Claude Haiku 4.5 must not be used for this agent "
              "(README, Model choice).", file=sys.stderr)
        return 2
    paths = [_case_path(c) for c in cases] if cases else [
        p for p, c in evaluation.load_cases(REGRESSIONS)
        if evaluation.is_evaluated(c) and evaluation.has_recording(c)]
    problems = {p: evaluation.lint_case(p, json.load(open(p, encoding="utf-8"))) for p in paths}
    if any(problems.values()):
        for p, found in problems.items():
            for line in found:
                print(f"{os.path.basename(p)}: {line}", file=sys.stderr)
        print("Fix the case files first (python cli.py lint).", file=sys.stderr)
        return 2

    worst = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        if not evaluation.has_recording(case):
            print(f"{case['id']}: no recording, cannot be run through a model")
            continue
        for model in models:
            cfg = dataclasses.replace(config, model=model)
            provider = AnthropicProvider(model, cfg.max_tokens, cfg.model_timeout_seconds)
            for i in range(1, runs + 1):
                run = evaluation.run_case(path, cfg, provider)
                row = evaluation.judge(case, run, cfg.min_confidence_for_pr,
                                       cfg.min_confidence_for_rollback)
                shown = "not assessed" if row["score"] is None else f"{row['score']}/100 {row['support']}"
                print(f"{case['id']} {model} run {i}: {row['outcome'].upper()}  "
                      f"{'right' if row['correct'] else 'WRONG'} ({row['why']})  score {shown}  "
                      f"calls {len(run['tool_calls'])}, unavailable {len(run['unavailable'])}")
                print(f"    {row['detail']}")
                if run["usage"]["cost_usd"] is not None:
                    print(f"    estimated cost ${run['usage']['cost_usd']:.4f}")
                if run["proposal"]:
                    print(f"    proposal {run['proposal']['status']}: {run['proposal']['changes']}")
                for refusal in run["proposal_refusals"]:
                    print(f"    proposal refused: {refusal[:160]}")
                if run["unavailable"]:
                    print("    no recording for: " + "; ".join(run["unavailable"]))
                if row["outcome"] == evaluation.CRITICAL:
                    worst = 1
                if record:
                    evaluation.append_run(REGRESSIONS, case["id"], run)
    if record:
        print("recorded in regressions/runs/. Now: python cli.py eval-report")
    return worst


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Online Boutique SRE agent (local runner)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log each tool call as JSON")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Also write the report to FILE as UTF-8 (PowerShell 5.1 "
                             "redirection garbles non-ASCII characters)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tool = sub.add_parser("tool", help="Run a single tool without the model")
    p_tool.add_argument("name", nargs="?", help="Tool name; omit to list tools")
    p_tool.add_argument("args", nargs="*", help="key=value arguments")

    p_ask = sub.add_parser("ask", help="Ask a question; answer only")
    p_ask.add_argument("question")

    p_inv = sub.add_parser("investigate", help="Investigate an AlertManager webhook payload")
    p_inv.add_argument("alert_file", help="Path to a JSON file in AlertManager webhook format")
    p_inv.add_argument("--started-minutes-ago", type=int, default=10, metavar="N",
                       help="Rewrite each alert's startsAt to N minutes ago (default 10)")
    p_inv.add_argument("--keep-starts-at", action="store_true",
                       help="Use the startsAt values in the file unchanged")
    p_inv.add_argument("--open-pr", action="store_true",
                       help="Open a pull request if the agent proposes a change "
                            "(default: validate and show it only). Needs a GITHUB_TOKEN with write access")

    p_prop = sub.add_parser("propose", help="Validate a values-dev.yaml change and optionally "
                                            "open it as a PR, without the model or a cluster")
    p_prop.add_argument("changes", nargs="+", metavar="path=value",
                        help="e.g. services.emailservice.resources.limits.memory=256Mi")
    p_prop.add_argument("--reason", default="Manual test of the sreagent PR path (cli.py propose).")
    p_prop.add_argument("--key", default="manual/cli/propose",
                        help="Alert key recorded in the PR; one open PR per key")
    p_prop.add_argument("--open-pr", action="store_true",
                        help="Actually open the PR (default: validate and print the diff and body)")

    p_eval = sub.add_parser("eval", help="Run regression cases through the real model against "
                                         "their recordings, judge each run, and record it in "
                                         "regressions/runs/. Needs ANTHROPIC_API_KEY; run by hand")
    p_eval.add_argument("cases", nargs="*", help="Case ids or files (default: every case with a recording)")
    p_eval.add_argument("--runs", type=int, default=3, help="Runs per case and model (default 3)")
    p_eval.add_argument("--models", nargs="+", metavar="MODEL",
                        help="Default: claude-sonnet-5-5 claude-opus-5-5. Claude Haiku is refused")
    p_eval.add_argument("--no-record", action="store_true", help="Print the runs without saving them")

    p_rep = sub.add_parser("replay", help="One case with SREAGENT_MODEL: `eval CASE --models "
                                          "$SREAGENT_MODEL`, saved only with --record")
    p_rep.add_argument("case", help="e.g. regressions/emailservice-pod-replaced.json")
    p_rep.add_argument("--runs", type=int, default=1, help="Number of runs (default 1)")
    p_rep.add_argument("--record", action="store_true", help="Append each run to regressions/runs/")

    sub.add_parser("eval-report", help="Write regressions/REPORT.md from every recorded run, "
                                       "judged with the current rule. No model, no cluster")
    sub.add_parser("lint", help="Check the case files: format, recordings present, nothing "
                                "unredacted. No model, no cluster")

    p_cap = sub.add_parser("capture", help="Record an incident from the live cluster into a "
                                           "regression case: the calls the agent made, the "
                                           "standard evidence pack, and the alert")
    p_cap.add_argument("case", help="Case file to create or update, e.g. regressions/my-incident.json")
    p_cap.add_argument("--service", required=True, help="e.g. recommendationservice")
    p_cap.add_argument("--question", help="The question a replay asks the agent (required "
                                          "without --alert-file or a recording)")
    p_cap.add_argument("--investigations", metavar="FILE",
                       help="Saved output of GET /investigations, or of GET "
                            "/investigations/<id>/recording. The calls the agent made are "
                            "recorded: from the recording if the file has the outputs, "
                            "otherwise by running the same calls again now")
    p_cap.add_argument("--id", help="Which investigation in FILE (default: the latest alert "
                                    "investigation for --service)")
    p_cap.add_argument("--alert-file", metavar="FILE",
                       help="The alert: a webhook payload, or saved output of Alertmanager's "
                            "GET /api/v2/alerts. Not needed with a recording, which has it")
    p_cap.add_argument("--repo-ref", metavar="SHA",
                       help="Commit the cluster was running (default: the head of "
                            "GITHUB_BRANCH now). Capture before merging the fix, or pass the "
                            "commit that introduced the fault")

    sub.add_parser("scores", help="Every recorded run in regressions/, scored and judged with "
                                  "the current rule")

    opts = parser.parse_args(argv)
    if opts.command in ("scores", "lint", "eval-report"):
        return _offline(opts)
    # Log lines can hold characters a Windows console code page cannot print
    sys.stdout.reconfigure(errors="replace")
    import logger  # needs python-json-logger; keep this module importable in tests
    logger.configure(logging.INFO if opts.verbose else logging.WARNING)

    try:
        config = Config.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    import wiring  # imports the kubernetes client; keep `--help` fast

    if opts.command == "tool":
        registry = wiring.build_registry(config)
        if not opts.name:
            for spec in registry.specs:
                print(f"{spec.name}: {spec.description}")
            return 0
        output, is_error = registry.run(opts.name, _parse_kv(opts.args))
        print(output)
        return 1 if is_error else 0

    if opts.command == "propose":
        return _propose(config, wiring, opts)
    if opts.command == "replay":
        return _eval(config, [opts.case], [config.model], opts.runs, opts.record)
    if opts.command == "eval":
        import evaluation
        return _eval(config, opts.cases, opts.models or list(evaluation.EVAL_MODELS),
                     opts.runs, not opts.no_record)
    if opts.command == "capture":
        return _capture(config, wiring, opts)

    import confidence
    pr, note = None, ""
    assessment = confidence.AssessmentTool(config.min_confidence_for_pr,
                                           config.min_confidence_for_rollback)
    if opts.command == "ask":
        # scored, but no proposal tool: /ask never opens a PR
        agent = wiring.build_agent(config, [assessment.tool()])
        assessment.attach_to(agent)
        system = prompts.system_prompt("ask", config.app_namespace, config.max_tool_calls)
        result = agent.run(system, prompts.with_current_time(opts.question))
    else:
        import alerts
        import pr_tool
        with open(opts.alert_file, encoding="utf-8") as f:
            payload = json.load(f)
        if not opts.keep_starts_at:
            refresh_starts_at(payload, opts.started_minutes_ago)
        firing = alerts.firing(payload)
        key = str(alerts.key_of(firing[0])) if firing else "manual/cli/investigate"
        proposal = pr_tool.ProposalTool(wiring.build_pr_opener(config), key, assessment,
                                        config.recent_merge_minutes)
        agent = wiring.build_agent(config, [assessment.tool(), proposal.tool()])
        assessment.attach_to(agent)
        system = prompts.system_prompt("investigate", config.app_namespace, config.max_tool_calls)
        result = agent.run(system, prompts.with_current_time(prompts.alert_task(payload)))
        pr = pr_tool.finish(proposal, result, key, uuid.uuid4().hex[:8], opts.open_pr, assessment)
        proposed = pr is not None and pr.get("status") in ("opened", "dry_run")
        note = confidence.note_no_change(assessment, proposed)

    print(format_report(result, config.max_tool_calls, assessment=assessment, note=note), end="")
    if pr is not None:
        print(format_pull_request(pr), end="")
    if opts.output:
        subject = opts.question if opts.command == "ask" else opts.alert_file
        header = (f"{opts.command}: {subject}\nmodel: {config.model}   "
                  f"run at: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        with open(opts.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(format_report(result, config.max_tool_calls, header, assessment, note))
    return 0 if result.outcome == "answered" else 1


if __name__ == "__main__":
    sys.exit(main())
