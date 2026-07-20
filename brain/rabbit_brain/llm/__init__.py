"""LLM agent loop (§6.2.5): transcript → provider (+ body tools) → TTS."""

from __future__ import annotations

from typing import Any

from .agent import AgentConfig, AgentLoop, TurnTimings
from .base import (
    AssistantTurn,
    LLMProvider,
    LLMResult,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolTurn,
    UserTurn,
)
from .tools import BodyTools

__all__ = [
    "AgentConfig",
    "AgentLoop",
    "AssistantTurn",
    "BodyTools",
    "LLMProvider",
    "LLMResult",
    "Message",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "ToolTurn",
    "TurnTimings",
    "UserTurn",
    "make_llm_provider",
]


def make_llm_provider(config: dict[str, Any]) -> LLMProvider:
    """Build the LLM provider from config.yaml's `llm:` section. OpenAI is the
    only backend in this phase; the provider stays swappable (base.LLMProvider)."""
    llm = config.get("llm", {})
    provider = llm.get("provider", "openai")
    if provider == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            model=llm.get("model", "gpt-5.4-mini"),
            # "none": OpenAI's own latency baseline, and the candidate this
            # rabbit's turns mostly don't need more than (hardware benchmark,
            # July 2026: gpt-5.4-nano was markedly worse on this agent/tool
            # loop despite being marketed as the fast option — mini stays).
            reasoning_effort=llm.get("reasoning_effort", "none"),
            max_output_tokens=llm.get("max_output_tokens", 150),
            timeout_s=llm.get("timeout_s", 20),
        )
    raise ValueError(f"unknown llm.provider {provider!r} (only 'openai' in this phase)")
