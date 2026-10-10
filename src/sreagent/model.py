"""Provider-neutral conversation types and the ModelProvider interface.

The agent loop only ever sees these types. Each provider converts them to and
from its own API format, so adding a Bedrock provider later means writing one
new class that implements `complete`, with no change to the loop.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, Union


@dataclass
class ToolSpec:
    """A tool the model may call: name, description and JSON Schema for its input."""
    name: str
    description: str
    input_schema: dict
    strict: bool = False


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""
    id: str
    name: str
    input: dict


@dataclass
class UserTurn:
    text: str


@dataclass
class AssistantTurn:
    """The model's reply: free text, zero or more tool calls, and why it stopped.

    `provider_data` lets a provider keep its native response (for example the
    Anthropic content blocks) so it can send the turn back verbatim on the next
    request. The loop never reads it.
    """
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    provider_data: Any = None


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class ToolResultsTurn:
    """The results of every tool call in the preceding AssistantTurn."""
    results: list[ToolResult]


Message = Union[UserTurn, AssistantTurn, ToolResultsTurn]


class ModelProvider(Protocol):
    def complete(self, system: str, messages: list[Message],
                 tools: list[ToolSpec]) -> AssistantTurn:
        """Send the conversation so far and return the model's next turn."""
        ...
