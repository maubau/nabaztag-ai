"""Agent loop (§6.2.5): transcript → LLM (+ tools) → reply → TTS on the rabbit.

One turn: append the user transcript, call the LLM, run any function calls
through the BodyController, feed results back, get the final text, and speak it
via the Speaker. Multiple tool calls per response are supported, bounded by
max_tool_rounds. Every failure path stops cleanly and leaves the runtime ready
for the next wake word; API keys and request content are never logged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..body.types import Priority
from ..tts.speaker import Speaker
from .base import AssistantTurn, LLMProvider, LLMResult, Message, ToolTurn, UserTurn
from .tools import BodyTools

log = logging.getLogger(__name__)

DEFAULT_MAX_HISTORY_TURNS = 20
DEFAULT_MAX_TOOL_ROUNDS = 4


@dataclass
class AgentConfig:
    max_history_turns: int = DEFAULT_MAX_HISTORY_TURNS
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS


@dataclass
class TurnTimings:
    """Monotonic diagnostics for one conversational turn, measured from the
    moment the transcript is ready (≈ STT final; the pipeline's WakeTimings
    covers wake→STT). No request/response content is recorded.

    tts_checkpoints breaks "transcript -> audio queued" into named markers
    from Speaker.speak (tts_start, tts_first_chunk_ready, first_chunk_submitted,
    tts_complete, all_submitted) — LLM-final vs TTS-synth vs OJN-submit were
    one opaque span before (hardware round, July 2026). The rabbit's actual
    GET is NOT observable here — cross-reference the Apache access log.
    """

    start: float
    request_sent: float | None = None
    first_token: float | None = None
    final_text: float | None = None
    audio_queued: float | None = None
    tool_rounds: int = 0
    tts_checkpoints: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int | None]:
        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - self.start) * 1000)

        d: dict[str, int | None] = {
            "to_request_ms": ms(self.request_sent),
            "to_first_token_ms": ms(self.first_token),
            "to_final_text_ms": ms(self.final_text),
            "to_audio_queued_ms": ms(self.audio_queued),
            "tool_rounds": self.tool_rounds,
        }
        for name, t in self.tts_checkpoints.items():
            d[f"to_{name}_ms"] = ms(t)
        return d


@dataclass
class AgentLoop:
    provider: LLMProvider
    tools: BodyTools
    system_prompt: str
    speaker: Speaker | None = None
    config: AgentConfig = field(default_factory=AgentConfig)
    _history: list[Message] = field(default_factory=list, init=False)
    last_timings: TurnTimings | None = None

    async def handle(self, transcript: str, language: str | None = None) -> str:
        """Run one turn. Returns the spoken text ("" on empty/failed turns).
        `language` is the STT-detected utterance language, forwarded to the TTS
        for voice routing (never inferred from text). Never raises: every error
        recovers and returns for the next wake word."""
        timings = TurnTimings(start=time.monotonic())
        self._history.append(UserTurn(transcript))
        try:
            result = await self._run_rounds(timings)
        except Exception:
            log.exception("LLM turn failed; recovering")
            self._trim()
            return ""

        text = result.text.strip()
        if text and self.speaker is not None:
            try:
                await self.speaker.speak(
                    text,
                    Priority.USER_SPEECH_SYNC,
                    language=language,
                    on_checkpoint=lambda name: timings.tts_checkpoints.setdefault(
                        name, time.monotonic()
                    ),
                )
                timings.audio_queued = time.monotonic()
            except Exception:
                log.exception("TTS/playback failed; recovering")
        self._trim()
        self.last_timings = timings
        log.info("agent turn timings: %s", timings.as_dict())
        return text

    async def _run_rounds(self, timings: TurnTimings) -> LLMResult:
        specs = self.tools.specs()
        informational = {s.name for s in specs if s.informational}

        def on_delta(_delta: str) -> None:
            if timings.first_token is None:
                timings.first_token = time.monotonic()

        result = LLMResult()
        for round_i in range(self.config.max_tool_rounds + 1):
            if timings.request_sent is None:
                timings.request_sent = time.monotonic()
            result = await self.provider.respond(self.system_prompt, self._history, specs, on_delta)
            self._history.append(AssistantTurn(result.text, tuple(result.tool_calls)))
            if not result.tool_calls:
                break
            timings.tool_rounds = round_i + 1
            if round_i == self.config.max_tool_rounds:
                # out of rounds: don't call tools again, take whatever text we have
                log.warning("max_tool_rounds reached, stopping tool execution")
                break
            results = tuple([await self.tools.execute(c) for c in result.tool_calls])
            self._history.append(ToolTurn(results))
            # `express` carries its reply INSIDE the tool call (spoken_text),
            # not in the response's free-text field — the benchmark showed
            # the model does not reliably produce free text alongside a tool
            # call in the same response, so the reply can't depend on that.
            # Pull it from the raw arguments regardless of whether the tool
            # execution above succeeded (a bad gesture/mood must not silence
            # the reply too).
            express_call = next((c for c in result.tool_calls if c.name == "express"), None)
            if express_call is not None:
                spoken_text = express_call.arguments.get("spoken_text")
                if isinstance(spoken_text, str) and spoken_text.strip():
                    result.text = spoken_text
            # Skip the extra LLM round when this response ALREADY has final
            # text and every tool it called is purely expressive (no return
            # value the model needs) — the common "say X and gesture" case
            # (hardware round, July 2026: this second round-trip was pure
            # latency, ~4-5s, for a gesture nobody needed a reply about).
            # Informational tools (get_direction, body_state) still force a
            # follow-up round so the model can use their result.
            needs_followup = any(c.name in informational for c in result.tool_calls)
            if not needs_followup and result.text.strip():
                break
        timings.final_text = time.monotonic()
        return result

    def _trim(self) -> None:
        """Keep the last max_history_turns user turns (with their assistant/tool
        follow-ups). Counting user turns keeps whole exchanges intact."""
        limit = self.config.max_history_turns
        user_idx = [i for i, m in enumerate(self._history) if isinstance(m, UserTurn)]
        if len(user_idx) > limit:
            cut = user_idx[-limit]
            self._history = self._history[cut:]

    @property
    def history(self) -> list[Message]:
        return self._history
