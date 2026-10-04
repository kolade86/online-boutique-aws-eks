"""Tool registry: what the model can call, and the one place outputs are sanitized."""

import logging
import time
from dataclasses import dataclass
from typing import Callable

from model import ToolSpec
from redact import sanitize

log = logging.getLogger("sreagent.tools")


class ToolError(Exception):
    """A failure the model should see and can react to (bad input, query error)."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]
    # Which end survives truncation: "head" for newest-first lists and query
    # results, "tail" for logs, whose latest lines are at the end.
    keep: str = "head"


class ToolRegistry:
    def __init__(self, tools: list[Tool], max_output_chars: int):
        self._tools = {t.name: t for t in tools}
        self._max_output_chars = max_output_chars

    @property
    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(t.name, t.description, t.input_schema)
                for t in self._tools.values()]

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool; return (output, is_error). Never raises."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True

        started = time.monotonic()
        try:
            output, is_error = tool.handler(args), False
        except ToolError as e:
            output, is_error = str(e), True
        except Exception as e:  # a tool bug must not kill the investigation
            output, is_error = f"{type(e).__name__}: {e}", True

        log.info("tool call", extra={
            "tool": name, "is_error": is_error,
            "duration_ms": int((time.monotonic() - started) * 1000)})
        return sanitize(output, self._max_output_chars, tool.keep), is_error


# Small input-validation helpers shared by the tool modules. The model's input
# is untrusted: validate it here rather than passing it straight to an API.

def require_str(args: dict, key: str, pattern=None) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' is required and must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise ToolError(f"'{key}' has an invalid value: {value!r}")
    return value


def optional_str(args: dict, key: str, pattern=None):
    if args.get(key) in (None, ""):
        return None
    return require_str(args, key, pattern)


def bounded_int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"'{key}' must be an integer")
    return max(low, min(high, value))
