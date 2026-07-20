"""OpenAI provider — Responses API, streaming + function calling (§6.2.5).

The model is configurable (never hardcoded); OPENAI_API_KEY comes only from the
environment and is never logged, nor is any request content. The neutral
history (base.py) is translated to Responses `input` items here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from .base import (
    AssistantTurn,
    LLMProvider,
    LLMResult,
    Message,
    OutputDeltaCallback,
    TextDeltaCallback,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolTurn,
    UserTurn,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.4-mini"
# Keep in step with config.example.yaml / make_llm_provider (150). Callers
# that build a provider directly — brain/scripts/llm-bench.py — inherit this,
# so a stale value here means the benchmark measures something the runtime
# never runs.
DEFAULT_MAX_OUTPUT_TOKENS = 150
DEFAULT_TIMEOUT_S = 20.0


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = "low",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        api_key: str | None = None,
        client=None,
    ):
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._timeout_s = timeout_s
        self._api_key = api_key  # else the SDK reads OPENAI_API_KEY from env
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            from openai import AsyncOpenAI  # lazy: keep import cheap / CI-safe

            # api_key defaults to OPENAI_API_KEY from the environment
            self._client = AsyncOpenAI(api_key=self._api_key, timeout=self._timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def respond(
        self,
        system: str,
        history: Sequence[Message],
        tools: Sequence[ToolSpec],
        on_text_delta: TextDeltaCallback | None = None,
        on_output_delta: OutputDeltaCallback | None = None,
    ) -> LLMResult:
        client = self._ensure_client()
        kwargs = {
            "model": self._model,
            "instructions": system,
            "input": _to_input_items(history),
            "max_output_tokens": self._max_output_tokens,
        }
        if tools:
            kwargs["tools"] = [_to_tool_param(t) for t in tools]
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}

        async with client.responses.stream(**kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)
                # A turn answered through `express` streams ONLY function-call
                # argument deltas — no output_text.delta at all — so first
                # OUTPUT and first visible TEXT are genuinely different events.
                if (
                    event_type
                    in (
                        "response.output_text.delta",
                        "response.function_call_arguments.delta",
                    )
                    and on_output_delta is not None
                ):
                    on_output_delta()
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta and on_text_delta is not None:
                        res = on_text_delta(delta)
                        if res is not None:
                            await res
            final = await stream.get_final_response()
        return _from_final_response(final)


def _to_tool_param(spec: ToolSpec) -> dict:
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def _to_input_items(history: Sequence[Message]) -> list[dict]:
    items: list[dict] = []
    for msg in history:
        if isinstance(msg, UserTurn):
            items.append({"role": "user", "content": msg.text})
        elif isinstance(msg, AssistantTurn):
            if msg.text:
                items.append({"role": "assistant", "content": msg.text})
            for call in msg.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                )
        elif isinstance(msg, ToolTurn):
            for result in msg.results:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": result.output,
                    }
                )
    return items


def _from_final_response(final) -> LLMResult:
    text = getattr(final, "output_text", "") or ""
    tool_calls: list[ToolCall] = []
    for item in getattr(final, "output", None) or []:
        if getattr(item, "type", None) == "function_call":
            raw = getattr(item, "arguments", "") or "{}"
            try:
                arguments = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                log.warning("model sent non-JSON arguments for %s", getattr(item, "name", "?"))
                arguments = {}
            tool_calls.append(
                ToolCall(
                    call_id=getattr(item, "call_id", "") or getattr(item, "id", ""),
                    name=getattr(item, "name", ""),
                    arguments=arguments,
                )
            )
    return LLMResult(text=text.strip(), tool_calls=tool_calls)


# re-exported so callers can build tool-result turns without importing base too
__all__ = ["OpenAIProvider", "ToolResult"]
