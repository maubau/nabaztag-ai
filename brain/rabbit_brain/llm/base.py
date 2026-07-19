"""LLMProvider contract (§6.2.5), provider-neutral.

The agent loop keeps a neutral conversation history and asks a provider to
produce the next assistant turn (text and/or tool calls). OpenAI is the only
backend in this phase; the interface exists so another (e.g. Anthropic) can be
added later without touching the agent loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """A callable the model may invoke. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str


# --- neutral conversation history ---------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolTurn:
    results: tuple[ToolResult, ...] = ()


Message = UserTurn | AssistantTurn | ToolTurn


@dataclass
class LLMResult:
    """One assistant turn: free text plus any tool calls it requested."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


# Called with each text delta as it streams in (for first-sentence-fast TTS).
TextDeltaCallback = Callable[[str], Awaitable[None] | None]


@runtime_checkable
class LLMProvider(Protocol):
    async def respond(
        self,
        system: str,
        history: Sequence[Message],
        tools: Sequence[ToolSpec],
        on_text_delta: TextDeltaCallback | None = None,
    ) -> LLMResult:
        """Produce the next assistant turn from the history. Streams text via
        on_text_delta if given; returns the full text and any tool calls."""
        ...
