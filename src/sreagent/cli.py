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


def _print_investigation(result, max_tool_calls: int) -> None:
    print(result.answer)
    print("\n" + "-" * 72)
    print(f"Outcome: {result.outcome}   Tool calls: {len(result.evidence)} of "
          f"{max_tool_calls} allowed   Elapsed: {result.elapsed_seconds:.1f}s")
    for i, e in enumerate(result.evidence, 1):
        status = "ERROR" if e.is_error else "ok"
        print(f"  {i:2}. [{status}] {e.tool} {json.dumps(e.input)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Online Boutique SRE agent (local runner)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log each tool call as JSON")
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

    opts = parser.parse_args(argv)
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

    agent = wiring.build_agent(config)
    if opts.command == "ask":
        system = prompts.system_prompt("ask", config.app_namespace, config.max_tool_calls)
        result = agent.run(system, opts.question)
    else:
        with open(opts.alert_file, encoding="utf-8") as f:
            payload = json.load(f)
        if not opts.keep_starts_at:
            refresh_starts_at(payload, opts.started_minutes_ago)
        system = prompts.system_prompt("investigate", config.app_namespace, config.max_tool_calls)
        result = agent.run(system, prompts.alert_task(payload))

    _print_investigation(result, config.max_tool_calls)
    return 0 if result.outcome == "answered" else 1


if __name__ == "__main__":
    sys.exit(main())
