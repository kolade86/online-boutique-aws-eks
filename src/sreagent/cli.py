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


def format_report(result, max_tool_calls: int, header: str = "", assessment=None,
                  note: str = "") -> str:
    from redact import redact_public
    lines = [header, ""] if header else []
    lines += [result.answer]
    lines += format_confidence(assessment, note)
    lines += ["", "-" * 72,
              f"Outcome: {result.outcome}   Tool calls: {len(result.evidence)} of "
              f"{max_tool_calls} allowed   Elapsed: {result.elapsed_seconds:.1f}s"]
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


def _replay(config, opts) -> int:
    """Run a regression case against its recording; print and optionally record each run."""
    import replay
    from anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(config.model, config.max_tokens, config.model_timeout_seconds)
    worst = 0
    for i in range(1, opts.runs + 1):
        run = replay.run_case(opts.case, config, provider)
        grade = run["keyword_check"]
        verdict = "passes" if grade["passed"] else "FAILS"
        print(f"run {i}: model {run['model']}  outcome {run['outcome']}  "
              f"evidence score {run['score']}  keyword check {verdict}")
        for r in run["reasons"]:
            print(f"    {r['points']:+4d}  {r['text']}" if r["points"] else f"       .  {r['text']}")
        if not grade["passed"]:
            print(f"    keyword check: missing one of {grade['missing_any_of']}; "
                  f"contains {grade['contains_forbidden']}")
            if run["score"] is not None and run["score"] >= config.min_confidence_for_pr:
                print(f"    WARNING: an answer that fails the keyword check scored {run['score']}, "
                      f"at or above the PR threshold {config.min_confidence_for_pr}")
                worst = 1
        if opts.record:
            replay.record(opts.case, run)
    if opts.record:
        print(f"recorded {opts.runs} run(s) in {opts.case}")
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

    p_rep = sub.add_parser("replay", help="Re-run a regression case with the real model against "
                                          "its recorded tool outputs, and score the answer")
    p_rep.add_argument("case", help="e.g. regressions/emailservice-pod-replaced.json")
    p_rep.add_argument("--runs", type=int, default=1, help="Number of runs (default 1)")
    p_rep.add_argument("--record", action="store_true",
                       help="Append each run (assessment, score, keyword check) to the case file")

    sub.add_parser("scores", help="Show the evidence score of every scored run in regressions/")

    opts = parser.parse_args(argv)
    if opts.command == "scores":   # no cluster, model or GitHub needed
        import os

        import replay
        rows = replay.score_table(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "regressions"))
        print(f"{'case':34} {'run':42} {'source':13} {'verdict':22} score")
        for r in rows:
            print(f"{r['case']:34} {r['run']:42} {r['source']:13} {r['verdict']:22} {r['score']}")
        return 0
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
        return _replay(config, opts)

    import confidence
    pr, note = None, ""
    assessment = confidence.AssessmentTool(config.min_confidence_for_pr,
                                           config.min_confidence_for_rollback)
    if opts.command == "ask":
        # scored, but no proposal tool: /ask never opens a PR
        agent = wiring.build_agent(config, [assessment.tool()])
        assessment.attach(lambda: agent.evidence)
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
        proposal = pr_tool.ProposalTool(wiring.build_pr_opener(config), key, assessment)
        agent = wiring.build_agent(config, [assessment.tool(), proposal.tool()])
        assessment.attach(lambda: agent.evidence)
        system = prompts.system_prompt("investigate", config.app_namespace, config.max_tool_calls)
        result = agent.run(system, prompts.with_current_time(prompts.alert_task(payload)))
        pr = pr_tool.finish(proposal, result, key, uuid.uuid4().hex[:8], opts.open_pr, assessment)
        note = confidence.note_below_threshold(assessment)

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
