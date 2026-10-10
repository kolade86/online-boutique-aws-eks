"""The agent loop.

    1. Send the task, the conversation so far, and the tool list to the model.
    2. If the model answers without calling a tool, that is the final answer.
    3. Otherwise run every tool it asked for, send back the results, repeat.

Two limits stop a runaway investigation: a maximum number of tool calls, and
a wall-clock timeout. Every tool call is recorded as evidence.
"""

import logging
import time
from dataclasses import dataclass, field

from model import ModelProvider, ToolResult, ToolResultsTurn, UserTurn
from tools import ToolRegistry

log = logging.getLogger("sreagent.agent")

# Outcomes
ANSWERED = "answered"            # the model gave a final answer
ANSWERED_AT_LIMIT = "answered_at_tool_limit"  # answered only after tools were refused
TOOL_LIMIT = "tool_limit"        # kept asking for tools after the budget ran out
TIMEOUT = "timeout"              # wall-clock limit reached
MODEL_STOPPED = "model_stopped"  # refusal or max_tokens instead of a normal answer
MODEL_ERROR = "model_error"      # the model API call failed

BUDGET_EXHAUSTED = ("Not run: the tool-call limit for this investigation has been "
                    "reached. Give your final answer now, using the evidence you have.")


@dataclass
class Evidence:
    tool: str
    input: dict
    output: str
    is_error: bool


@dataclass
class Investigation:
    outcome: str
    answer: str
    evidence: list[Evidence] = field(default_factory=list)
    elapsed_seconds: float = 0.0


class Agent:
    def __init__(self, provider: ModelProvider, tools: ToolRegistry,
                 max_tool_calls: int, timeout_seconds: float, clock=time.monotonic):
        self.provider = provider
        self.tools = tools
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.evidence: list[Evidence] = []

    def run(self, system: str, task: str) -> Investigation:
        started = self.clock()
        messages = [UserTurn(task)]
        evidence: list[Evidence] = []
        self.evidence = evidence   # visible to tools during the run (submit_assessment cites it)
        refused_last_round = False

        def finish(outcome: str, answer: str) -> Investigation:
            elapsed = self.clock() - started
            log.info("investigation finished", extra={
                "outcome": outcome, "tool_calls": len(evidence), "elapsed_s": round(elapsed, 1)})
            return Investigation(outcome, answer, evidence, elapsed)

        while True:
            if self.clock() - started > self.timeout_seconds:
                return finish(TIMEOUT, f"Stopped after {self.timeout_seconds:.0f}s without a final answer.")

            try:
                turn = self.provider.complete(system, messages, self.tools.specs)
            except Exception as e:
                return finish(MODEL_ERROR, f"Model call failed: {type(e).__name__}: {e}")
            messages.append(turn)

            # No tool calls: the model is done.
            if not turn.tool_calls:
                if turn.stop_reason in ("end_turn", "stop_sequence"):
                    return finish(ANSWERED_AT_LIMIT if refused_last_round else ANSWERED, turn.text)
                return finish(MODEL_STOPPED, f"[stop_reason={turn.stop_reason}] {turn.text}")

            # It was told the budget is spent and asked for more tools anyway.
            if refused_last_round:
                return finish(TOOL_LIMIT, turn.text or "Tool-call limit reached without a final answer.")

            # Run each requested tool, or refuse it once the budget is spent.
            results = []
            for call in turn.tool_calls:
                if len(evidence) >= self.max_tool_calls:
                    results.append(ToolResult(call.id, BUDGET_EXHAUSTED, is_error=True))
                    refused_last_round = True
                    continue
                output, is_error = self.tools.run(call.name, call.input)
                evidence.append(Evidence(call.name, call.input, output, is_error))
                # Numbered so the model can cite it in submit_assessment
                results.append(ToolResult(call.id, f"[call #{len(evidence)}]\n{output}", is_error))

            messages.append(ToolResultsTurn(results))
