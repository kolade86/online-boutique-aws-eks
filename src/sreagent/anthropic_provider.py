"""ModelProvider backed by the Anthropic API (official `anthropic` SDK)."""

import anthropic

from model import (AssistantTurn, Message, ToolCall, ToolResultsTurn, ToolSpec,
                   UserTurn)

# Models that accept server-side refusal fallback. A safety-classifier decline
# (stop_reason "refusal") is retried on another model by the API itself; for
# an agent that reads logs, a false "cyber" decline is the plausible case.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    def __init__(self, model: str, max_tokens: int, request_timeout: float,
                 client=None):
        self._model = model
        self._max_tokens = max_tokens
        # Reads ANTHROPIC_API_KEY from the environment. The SDK retries 429,
        # 5xx and connection errors itself (max_retries).
        self._client = client or anthropic.Anthropic(
            timeout=request_timeout, max_retries=2)

    def complete(self, system: str, messages: list[Message],
                 tools: list[ToolSpec]) -> AssistantTurn:
        request = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            tools=[_tool(t) for t in tools],
            messages=[_to_api(m) for m in messages],
            # Each loop iteration resends the whole conversation; caching the
            # prefix makes the repeats cheaper. Prefixes below the model's
            # minimum cacheable size are simply not cached.
            cache_control={"type": "ephemeral"},
        )
        if self._model in FALLBACK_MODELS:
            response = self._client.beta.messages.create(
                **request, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            response = self._client.messages.create(**request)

        stop_reason = response.stop_reason or "end_turn"
        details = getattr(response, "stop_details", None)
        if stop_reason == "refusal" and details is not None and details.category:
            stop_reason = f"refusal:{details.category}"
        return AssistantTurn(
            text="".join(b.text for b in response.content if b.type == "text"),
            tool_calls=[ToolCall(id=b.id, name=b.name, input=dict(b.input))
                        for b in response.content if b.type == "tool_use"],
            stop_reason=stop_reason,
            provider_data=response.content,
        )


def _tool(t: ToolSpec) -> dict:
    tool = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
    if t.strict:
        tool["strict"] = True   # the API guarantees schema-valid input
    return tool


def _to_api(message: Message) -> dict:
    if isinstance(message, UserTurn):
        return {"role": "user", "content": message.text}

    if isinstance(message, AssistantTurn):
        if message.provider_data is not None:
            # Send our own previous reply back unchanged. Reasoning models
            # attach thinking blocks to their tool calls, and the API requires
            # them back as they were (history is append-only here).
            return {"role": "assistant", "content": message.provider_data}
        blocks = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                   for c in message.tool_calls]
        return {"role": "assistant", "content": blocks}

    if isinstance(message, ToolResultsTurn):
        # All results for one assistant turn go back in a single user message.
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call_id,
             "content": r.content, "is_error": r.is_error}
            for r in message.results]}

    raise TypeError(f"unknown message type: {type(message).__name__}")
