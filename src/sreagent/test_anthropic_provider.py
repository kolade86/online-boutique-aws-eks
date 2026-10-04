"""Tests for the Anthropic provider's message conversion (no network calls)."""

import unittest
from types import SimpleNamespace as NS

try:
    import anthropic_provider
except ImportError:  # anthropic not installed
    anthropic_provider = None

from model import AssistantTurn, ToolCall, ToolResult, ToolResultsTurn, ToolSpec, UserTurn


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


@unittest.skipIf(anthropic_provider is None, "anthropic SDK not installed")
class AnthropicProviderTest(unittest.TestCase):
    def test_request_and_response_conversion(self):
        content = [NS(type="text", text="Checking pods."),
                   NS(type="tool_use", id="tu_1", name="list_pods", input={"app": "cart"})]
        messages = FakeMessages(NS(content=content, stop_reason="tool_use"))
        provider = anthropic_provider.AnthropicProvider(
            "test-model", 1000, 30, client=NS(messages=messages))

        history = [UserTurn("task"),
                   AssistantTurn("", [ToolCall("tu_0", "list_hpas", {})], "tool_use"),
                   ToolResultsTurn([ToolResult("tu_0", "hpa output", False)])]
        turn = provider.complete("system", history, [ToolSpec("list_pods", "desc", {"type": "object"})])

        self.assertEqual(turn.text, "Checking pods.")
        self.assertEqual(turn.tool_calls, [ToolCall("tu_1", "list_pods", {"app": "cart"})])
        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertIs(turn.provider_data, content)

        sent = messages.kwargs
        self.assertEqual(sent["model"], "test-model")
        self.assertEqual(sent["system"], "system")
        self.assertEqual(sent["tools"], [{"name": "list_pods", "description": "desc",
                                          "input_schema": {"type": "object"}}])
        self.assertEqual(sent["messages"], [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_0", "name": "list_hpas", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_0", "content": "hpa output",
                 "is_error": False}]},
        ])

    def test_previous_reply_is_sent_back_verbatim(self):
        native = [NS(type="thinking"), NS(type="tool_use")]
        msg = anthropic_provider._to_api(AssistantTurn("", [], "tool_use", provider_data=native))
        self.assertEqual(msg, {"role": "assistant", "content": native})


if __name__ == "__main__":
    unittest.main()
