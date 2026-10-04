"""ModelProvider backed by the Anthropic API (official `anthropic` SDK)."""

import anthropic

from model import (AssistantTurn, Message, ToolCall, ToolResultsTurn, ToolSpec,
                   UserTurn)


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
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            tools=[{"name": t.name, "description": t.description,
                    "input_schema": t.input_schema} for t in tools],
            messages=[_to_api(m) for m in messages],
            # Each loop iteration resends the whole conversation; caching the
            # prefix makes the repeats cheaper. Prefixes below the model's
            # minimum cacheable size are simply not cached.
            cache_control={"type": "ephemeral"},
        )
        return AssistantTurn(
            text="".join(b.text for b in response.content if b.type == "text"),
            tool_calls=[ToolCall(id=b.id, name=b.name, input=dict(b.input))
                        for b in response.content if b.type == "tool_use"],
            stop_reason=response.stop_reason or "end_turn",
            provider_data=response.content,
        )


def _to_api(message: Message) -> dict:
    if isinstance(message, UserTurn):
        return {"role": "user", "content": message.text}

    if isinstance(message, AssistantTurn):
        if message.provider_data is not None:
            # Send our own previous reply back unchanged (keeps any thinking
            # blocks a reasoning model attached to its tool calls).
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
