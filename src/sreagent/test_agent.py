"""Tests for the agent loop, using a scripted fake model provider."""

import unittest

import agent
from agent import Agent
from model import AssistantTurn, ToolCall, ToolResultsTurn, UserTurn
from tools import Tool, ToolError, ToolRegistry


class FakeProvider:
    """Returns pre-scripted turns in order and records what it was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, system, messages, tools):
        self.requests.append(list(messages))
        if isinstance(self.turns[0], Exception):
            raise self.turns.pop(0)
        return self.turns.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def call(name, call_id="c1", **args):
    return ToolCall(id=call_id, name=name, input=args)


def tool_turn(*calls):
    return AssistantTurn(text="", tool_calls=list(calls), stop_reason="tool_use")


def answer(text):
    return AssistantTurn(text=text, stop_reason="end_turn")


def registry(handlers, max_chars=1000):
    return ToolRegistry([Tool(name, "", {"type": "object"}, fn) for name, fn in handlers.items()],
                        max_chars)


def make_agent(provider, tools, max_tool_calls=10, timeout=300, clock=None):
    return Agent(provider, tools, max_tool_calls, timeout, clock=clock or FakeClock())


class AgentLoopTest(unittest.TestCase):
    def test_answer_without_tools(self):
        provider = FakeProvider([answer("All healthy.")])
        result = make_agent(provider, registry({})).run("sys", "Is it healthy?")

        self.assertEqual(result.outcome, agent.ANSWERED)
        self.assertEqual(result.answer, "All healthy.")
        self.assertEqual(result.evidence, [])
        self.assertEqual(provider.requests[0], [UserTurn("Is it healthy?")])

    def test_runs_tool_and_returns_result_to_model(self):
        provider = FakeProvider([tool_turn(call("list_pods", app="cartservice")),
                                 answer("cartservice is OOMKilled.")])
        tools = registry({"list_pods": lambda args: f"pods for {args['app']}"})

        result = make_agent(provider, tools).run("sys", "task")

        self.assertEqual(result.outcome, agent.ANSWERED)
        self.assertEqual(result.answer, "cartservice is OOMKilled.")
        self.assertEqual([(e.tool, e.input, e.output) for e in result.evidence],
                         [("list_pods", {"app": "cartservice"}, "pods for cartservice")])
        # Second request = task, the model's tool call, then the tool result
        second = provider.requests[1]
        self.assertIsInstance(second[2], ToolResultsTurn)
        self.assertEqual(second[2].results[0].call_id, "c1")
        self.assertEqual(second[2].results[0].content, "pods for cartservice")
        self.assertFalse(second[2].results[0].is_error)

    def test_parallel_tool_calls_return_in_one_turn(self):
        provider = FakeProvider([tool_turn(call("a", "id-a"), call("b", "id-b")), answer("done")])
        tools = registry({"a": lambda args: "A", "b": lambda args: "B"})

        make_agent(provider, tools).run("sys", "task")

        results = provider.requests[1][-1].results
        self.assertEqual([(r.call_id, r.content) for r in results], [("id-a", "A"), ("id-b", "B")])

    def test_tool_errors_are_returned_to_the_model_not_raised(self):
        def bad_input(args):
            raise ToolError("'pod' is required")

        def crashes(args):
            raise RuntimeError("boom")

        provider = FakeProvider([
            tool_turn(call("bad_input", "1"), call("crashes", "2"), call("missing", "3")),
            answer("could not tell")])
        tools = registry({"bad_input": bad_input, "crashes": crashes})

        result = make_agent(provider, tools).run("sys", "task")

        self.assertEqual(result.outcome, agent.ANSWERED)
        results = provider.requests[1][-1].results
        self.assertTrue(all(r.is_error for r in results))
        self.assertEqual(results[0].content, "'pod' is required")
        self.assertEqual(results[1].content, "RuntimeError: boom")
        self.assertEqual(results[2].content, "Unknown tool: missing")

    def test_budget_exhausted_gives_model_one_chance_to_answer(self):
        provider = FakeProvider([
            tool_turn(call("t", "1"), call("t", "2")),
            tool_turn(call("t", "3")),          # over budget: refused, not run
            answer("final from partial evidence")])
        ran = []
        tools = registry({"t": lambda args: ran.append(1) or "ok"})

        result = make_agent(provider, tools, max_tool_calls=2).run("sys", "task")

        # Answered, but the outcome shows the budget ran out first
        self.assertEqual(result.outcome, agent.ANSWERED_AT_LIMIT)
        self.assertEqual(len(ran), 2)
        self.assertEqual(len(result.evidence), 2)
        refused = provider.requests[2][-1].results[0]
        self.assertTrue(refused.is_error)
        self.assertEqual(refused.content, agent.BUDGET_EXHAUSTED)

    def test_using_exactly_the_budget_is_a_normal_answer(self):
        provider = FakeProvider([tool_turn(call("t", "1"), call("t", "2")), answer("done")])
        result = make_agent(provider, registry({"t": lambda args: "ok"}),
                            max_tool_calls=2).run("sys", "task")

        self.assertEqual(result.outcome, agent.ANSWERED)

    def test_stops_if_model_keeps_calling_tools_after_budget(self):
        provider = FakeProvider([tool_turn(call("t", "1")),
                                 tool_turn(call("t", "2")),
                                 tool_turn(call("t", "3"))])
        tools = registry({"t": lambda args: "ok"})

        result = make_agent(provider, tools, max_tool_calls=1).run("sys", "task")

        self.assertEqual(result.outcome, agent.TOOL_LIMIT)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(len(provider.requests), 3)

    def test_timeout(self):
        clock = FakeClock()

        def slow(args):
            clock.now += 200
            return "ok"

        provider = FakeProvider([tool_turn(call("slow", "1")), tool_turn(call("slow", "2")),
                                 answer("never reached")])
        result = make_agent(provider, registry({"slow": slow}), timeout=300,
                            clock=clock).run("sys", "task")

        self.assertEqual(result.outcome, agent.TIMEOUT)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(len(provider.requests), 2)

    def test_model_error(self):
        provider = FakeProvider([ConnectionError("network down")])
        result = make_agent(provider, registry({})).run("sys", "task")

        self.assertEqual(result.outcome, agent.MODEL_ERROR)
        self.assertIn("network down", result.answer)

    def test_unexpected_stop_reason(self):
        provider = FakeProvider([AssistantTurn(text="partial", stop_reason="max_tokens")])
        result = make_agent(provider, registry({})).run("sys", "task")

        self.assertEqual(result.outcome, agent.MODEL_STOPPED)
        self.assertIn("max_tokens", result.answer)

    def test_tool_output_is_redacted_and_truncated_before_the_model_sees_it(self):
        token = "ghp_" + "a" * 36
        provider = FakeProvider([tool_turn(call("logs")), answer("ok")])
        tools = registry({"logs": lambda args: f"token={token}\n" + "x" * 5000}, max_chars=200)

        make_agent(provider, tools).run("sys", "task")

        sent = provider.requests[1][-1].results[0].content
        self.assertNotIn(token, sent)
        self.assertIn("[REDACTED]", sent)
        self.assertIn("truncated", sent)
        self.assertLess(len(sent), 300)


if __name__ == "__main__":
    unittest.main()
