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
    """A callable the model may invoke. `parameters` is a JSON Schema object.

    `informational`: True when the model NEEDS the tool's return value to
    answer correctly (get_direction, body_state) — a follow-up LLM round is
    required. False (default) for purely expressive/fire-and-forget actions
    (gesture_ears, set_mood_lights, play_gesture): if the model already gave
    final text in the SAME response as the tool call, AgentLoop skips the
    extra round-trip entirely (hardware round, July 2026: wake->audio queued
    ~14s, and a second OpenAI call for a gesture that needed no reply was one
    of the avoidable costs).
    """

    name: str
    description: str
    parameters: dict[str, Any]
    informational: bool = False


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
    """One assistant turn: free text plus any tool calls it requested.

    Token counts are diagnostics for the input-size work (latency Gate L2,
    July 2026: the tool schemas ride along on every turn, so shrinking them
    should show up as fewer input tokens). None when the provider doesn't
    report usage. No request/response CONTENT is ever recorded here.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None


# Called with each text delta as it streams in (for first-sentence-fast TTS).
TextDeltaCallback = Callable[[str], Awaitable[None] | None]
# Called (no args) on the first streamed output of ANY kind — visible text OR
# tool-call arguments. Needed because a turn answered through the `express`
# tool emits no output_text.delta at all (the reply lives in the function
# call's arguments), which left to_first_token_ms as None for exactly the
# turns we most wanted to measure (hardware round, July 2026).
OutputDeltaCallback = Callable[[], None]


@runtime_checkable
class LLMProvider(Protocol):
    async def respond(
        self,
        system: str,
        history: Sequence[Message],
        tools: Sequence[ToolSpec],
        on_text_delta: TextDeltaCallback | None = None,
        on_output_delta: OutputDeltaCallback | None = None,
    ) -> LLMResult:
        """Produce the next assistant turn from the history. Streams visible
        text via on_text_delta if given, and signals the first output of any
        kind (text or tool arguments) via on_output_delta; returns the full
        text and any tool calls."""
        ...
