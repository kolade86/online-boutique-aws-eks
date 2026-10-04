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

import logger
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


def _print_investigation(result) -> None:
    print(result.answer)
    print("\n" + "-" * 72)
    print(f"Outcome: {result.outcome}   Tool calls: {len(result.evidence)}   "
          f"Elapsed: {result.elapsed_seconds:.1f}s")
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

    opts = parser.parse_args(argv)
    # Log lines can hold characters a Windows console code page cannot print
    sys.stdout.reconfigure(errors="replace")
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
        result = agent.run(prompts.system_prompt("ask", config.app_namespace), opts.question)
    else:
        with open(opts.alert_file, encoding="utf-8") as f:
            task = prompts.alert_task(json.load(f))
        result = agent.run(prompts.system_prompt("investigate", config.app_namespace), task)

    _print_investigation(result)
    return 0 if result.outcome == "answered" else 1


if __name__ == "__main__":
    sys.exit(main())
