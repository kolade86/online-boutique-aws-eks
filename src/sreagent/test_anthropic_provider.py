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

    def test_fallback_models_use_beta_endpoint_with_fallbacks(self):
        response = NS(content=[NS(type="text", text="ok")], stop_reason="end_turn")
        plain, beta = FakeMessages(response), FakeMessages(response)
        client = NS(messages=plain, beta=NS(messages=beta))

        anthropic_provider.AnthropicProvider("claude-sonnet-5-5", 100, 30, client=client).complete(
            "s", [UserTurn("t")], [])
        self.assertEqual(beta.kwargs["fallbacks"], "default")
        self.assertEqual(beta.kwargs["betas"], [anthropic_provider.FALLBACK_BETA])
        self.assertIsNone(plain.kwargs)

        anthropic_provider.AnthropicProvider("claude-haiku-4-5-20251001", 100, 30,
                                             client=client).complete("s", [UserTurn("t")], [])
        self.assertNotIn("fallbacks", plain.kwargs)

    def test_refusal_category_is_kept(self):
        response = NS(content=[], stop_reason="refusal", stop_details=NS(category="cyber"))
        provider = anthropic_provider.AnthropicProvider(
            "test-model", 100, 30, client=NS(messages=FakeMessages(response)))
        self.assertEqual(provider.complete("s", [UserTurn("t")], []).stop_reason, "refusal:cyber")

    def test_real_sdk_request_shape(self):
        """Run the real SDK against a mock transport: checks what goes on the wire."""
        import json

        import httpx2

        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["beta"] = request.headers.get("anthropic-beta")
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "list_pods", "input": {}}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})

        client = anthropic_provider.anthropic.Anthropic(
            api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        import confidence
        turn = anthropic_provider.AnthropicProvider("claude-sonnet-5-5", 16000, 30, client=client) \
            .complete("sys", [UserTurn("task")],
                      [ToolSpec("list_pods", "d", {"type": "object"}),
                       ToolSpec(confidence.TOOL_NAME, "d", confidence.SCHEMA, strict=True)])

        # strict reaches the wire for the assessment tool only
        self.assertNotIn("strict", seen["body"]["tools"][0])
        self.assertIs(seen["body"]["tools"][1]["strict"], True)
        self.assertEqual(seen["body"]["tools"][1]["input_schema"], confidence.SCHEMA)
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertIn(anthropic_provider.FALLBACK_BETA, seen["beta"])
        self.assertEqual(seen["body"]["fallbacks"], "default")
        self.assertEqual(seen["body"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seen["body"]["max_tokens"], 16000)
        self.assertNotIn("thinking", seen["body"])  # Sonnet 5.5 default: adaptive
        self.assertNotIn("tool_choice", seen["body"])
        self.assertEqual(turn.tool_calls[0].name, "list_pods")

        # The final turn at the tool-call limit: tools still defined, none may be called
        anthropic_provider.AnthropicProvider("claude-sonnet-5-5", 16000, 30, client=client) \
            .complete("sys", [UserTurn("task")], [ToolSpec("list_pods", "d", {"type": "object"})],
                      allow_tools=False)
        self.assertEqual(seen["body"]["tool_choice"], {"type": "none"})
        self.assertEqual([t["name"] for t in seen["body"]["tools"]], ["list_pods"])

        # The reply goes back verbatim on the next request
        anthropic_provider.AnthropicProvider("claude-sonnet-5-5", 16000, 30, client=client) \
            .complete("sys", [UserTurn("task"), turn], [])
        self.assertEqual(seen["body"]["messages"][1]["content"][0]["id"], "tu_1")

    def test_usage_is_recorded_per_attempt_with_the_model_that_ran_it(self):
        usage = NS(input_tokens=1200, output_tokens=340, cache_read_input_tokens=9000,
                   cache_creation_input_tokens=800, iterations=None)
        response = NS(content=[NS(type="text", text="ok")], stop_reason="end_turn",
                      model="claude-sonnet-5-5", usage=usage)
        turn = anthropic_provider.AnthropicProvider(
            "test-model", 100, 30, client=NS(messages=FakeMessages(response))).complete(
            "s", [UserTurn("t")], [])
        self.assertEqual(turn.usage, [{"model": "claude-sonnet-5-5", "stop_reason": "end_turn",
                                       "input": 1200, "output": 340, "cache_read": 9000,
                                       "cache_write": 800}])

        # A server-side fallback: the declined attempt and the fallback, each
        # with its own model, from usage.iterations
        iterations = [NS(type="message", model="claude-sonnet-5-5", input_tokens=5000,
                         output_tokens=0, cache_read_input_tokens=0,
                         cache_creation_input_tokens=0),
                      NS(type="fallback_message", model="claude-sonnet-5", input_tokens=5000,
                         output_tokens=200, cache_read_input_tokens=0,
                         cache_creation_input_tokens=0)]
        response = NS(content=[NS(type="text", text="ok")], stop_reason="end_turn",
                      model="claude-sonnet-5", usage=NS(**{**vars(usage), "iterations": iterations}))
        turn = anthropic_provider.AnthropicProvider(
            "claude-sonnet-5-5", 100, 30,
            client=NS(messages=None, beta=NS(messages=FakeMessages(response)))).complete(
            "s", [UserTurn("t")], [])
        self.assertEqual([(u["model"], u["stop_reason"], u["input"], u["output"]) for u in turn.usage],
                         [("claude-sonnet-5-5", "refusal", 5000, 0),
                          ("claude-sonnet-5", "end_turn", 5000, 200)])

        # No usage on the response (other providers, fakes): nothing recorded
        self.assertEqual(anthropic_provider.usage_of(NS(), "end_turn"), [])

    def test_previous_reply_is_sent_back_verbatim(self):
        native = [NS(type="thinking"), NS(type="tool_use")]
        msg = anthropic_provider._to_api(AssistantTurn("", [], "tool_use", provider_data=native))
        self.assertEqual(msg, {"role": "assistant", "content": native})


if __name__ == "__main__":
    unittest.main()
