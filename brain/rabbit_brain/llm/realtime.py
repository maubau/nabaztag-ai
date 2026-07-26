"""OpenAI Realtime session — full-duplex speech-to-speech (conversation.mode: realtime).

The turn-based path is a chain: Flux STT -> gpt-5.4-mini -> Piper -> playback,
each stage waiting for the previous one to finish. Realtime replaces all three
with one WebSocket carrying audio both ways, so the model can start speaking
before we have finished thinking about the sentence, and the user can cut in.

WHAT THIS COSTS (be clear-eyed): the model generates the voice itself, so the
Italian voices we chose by ear on hardware (Paola/Alba) are NOT used here — the
Realtime voices are OpenAI's, and their Italian is unverified. That is exactly
why this is a MODE and not a replacement: `conversation.mode: turn_based` keeps
the curated path, and any WebSocket/API failure re-arms it automatically.

REQUIREMENTS this path depends on:
  * `audio_out.backend: local`. Barge-in needs playback we can cut mid-word;
    the rabbit's MTL decoder buffers to EOF and cannot be interrupted at all
    (Gate L3). The runtime refuses realtime mode on the rabbit backend rather
    than pretending to support it.
  * Working AEC, so the rabbit does not interrupt itself. Hardware-confirmed on
    ch0 (residual below the noise floor, near-end speech +58.6 dB over it).

BARGE-IN, the part that has to be exact: when the server reports
`input_audio_buffer.speech_started` we stop the speaker immediately AND tell the
model how much of its reply was actually heard, via `conversation.item.truncate`
with the REALLY-PLAYED time from the player. Skip that and the model believes it
said sentences the user never heard, and every later turn reasons from a
transcript that does not match reality.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import aiohttp

from ..audio.streaming import CLOSE_TIMEOUT_S, Resampler

log = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
API_RATE = 24000  # the Realtime API speaks pcm at 24 kHz
# Turn detection: "semantic_vad" lets the model judge whether you actually
# finished a thought (kinder to mid-sentence pauses than a pure silence timer);
# "server_vad" is the plain energy/silence detector.
TURN_DETECTION = ("semantic_vad", "server_vad")
# The model can hang up by calling this; the conversation is continuous, so
# without it only a timeout or an error would end a session.
END_CONVERSATION_TOOL = "end_conversation"
# Errors that are RACES, not failures. With server-side VAD the server cancels
# the response itself the moment it hears the user, so a cancel/truncate we send
# in the same instant can legitimately arrive too late. Killing the session over
# that would end the conversation on every successful barge-in (hardware, July
# 2026: "response_cancel_not_active" took the whole session down).
BENIGN_ERROR_CODES = frozenset(
    {
        "response_cancel_not_active",
        "response_already_cancelled",
        "item_truncate_invalid_audio_end_ms",
        "invalid_value_for_truncate",
    }
)
# Errors that really do make the session unusable — no point limping on.
FATAL_ERROR_CODES = frozenset(
    {"invalid_api_key", "authentication_error", "session_expired", "insufficient_quota"}
)


@dataclass
class RealtimeTimings:
    """Per-turn latency breakdown — the numbers that decide whether realtime
    actually beats the turn-based path on this hardware."""

    session_open_ms: int | None = None
    speech_started_ms: int | None = None  # server heard the user start
    speech_stopped_ms: int | None = None
    first_audio_delta_ms: int | None = None  # first PCM byte from the model
    playback_started_ms: int | None = None  # first byte handed to the speaker
    interruptions: list[int] = field(default_factory=list)  # ms to cut playback

    def as_dict(self) -> dict:
        return {
            "session_open_ms": self.session_open_ms,
            "speech_started_ms": self.speech_started_ms,
            "speech_stopped_ms": self.speech_stopped_ms,
            "first_audio_delta_ms": self.first_audio_delta_ms,
            "playback_started_ms": self.playback_started_ms,
            "interruptions_ms": list(self.interruptions),
        }


class RealtimeError(RuntimeError):
    """Any WebSocket/API failure. The caller re-arms the turn-based path."""


class RealtimeSession:
    """One full-duplex conversation over a Realtime WebSocket.

    `player` is a StreamingAudioPlayer (local backend only). `tool_executor`,
    when given, is an async callable (name, arguments) -> result string, so the
    body tools — ears, LEDs, gestures — keep working in this mode too.
    """

    def __init__(
        self,
        api_key: str,
        player,
        instructions: str = "",
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        mic_rate: int = 16000,
        turn_detection: str = "semantic_vad",
        tools: list[dict] | None = None,
        tool_executor: Callable | None = None,
        session: aiohttp.ClientSession | None = None,
        on_transcript: Callable[[str], None] | None = None,
        on_response_start: Callable[[], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        on_speech_started: Callable[[], None] | None = None,
        on_speech_stopped: Callable[[], None] | None = None,
        close_player: bool = True,
    ):
        self._api_key = api_key
        self._player = player
        self._close_player = close_player
        self._instructions = instructions
        self._model = model
        self._voice = voice
        if turn_detection not in TURN_DETECTION:
            raise ValueError(f"turn_detection must be one of {TURN_DETECTION}")
        self._turn_detection = turn_detection
        # Both supported modes are SERVER-side: the server ends the turn and
        # cancels the in-flight response itself, so we must not also cancel.
        self._server_side_turn_detection = turn_detection in TURN_DETECTION
        self._tools = tools or []
        self._tool_executor = tool_executor
        self._on_transcript = on_transcript
        self._on_response_start = on_response_start
        self._on_barge_in = on_barge_in
        self._on_speech_started = on_speech_started
        self._on_speech_stopped = on_speech_stopped
        self._to_api = Resampler(mic_rate, API_RATE)
        # Continuous conversation: the wake word is NOT repeated per turn, so
        # the session ends on idle timeout, an explicit hang-up, or an error.
        self.last_activity = time.monotonic()
        self.ended_by_model = False
        self._session = session
        self._own_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._current_item: str | None = None
        # The SERVER's generation state. Local playback is tracked by the
        # player itself (has_unplayed_audio) — conflating the two is what
        # silently disabled barge-in.
        self._server_response_active = False
        self._t0 = 0.0
        self.timings = RealtimeTimings()

    def _ms(self) -> int:
        return round((time.monotonic() - self._t0) * 1000)

    # --- connection ------------------------------------------------------

    async def connect(self) -> None:
        self._t0 = time.monotonic()
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = f"{REALTIME_URL}?model={self._model}"
        try:
            self._ws = await self._session.ws_connect(
                url, headers={"Authorization": f"Bearer {self._api_key}"}, heartbeat=20
            )
        except Exception as exc:
            raise RealtimeError(f"realtime connect failed: {exc}") from exc
        self.timings.session_open_ms = self._ms()
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": self._instructions,
                    # Current audio schema: format is an object (type + rate),
                    # not the old bare "pcm16" string.
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": API_RATE},
                            "turn_detection": {"type": self._turn_detection},
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": API_RATE},
                            "voice": self._voice,
                        },
                    },
                    "tools": self._tools,
                },
            }
        )

    async def aclose(self) -> None:
        """Tear everything down: socket AND speaker. The capture iterator is NOT
        touched — it belongs to the pipeline and must survive for the turn-based
        path to keep working after a realtime failure.

        Every step is BOUNDED: a socket that will not close, or a player whose
        writer is stuck inside PortAudio, must never stop the pipeline from
        re-arming (hardware, July 2026: an unbounded wait here left the
        microphone unconsumed and "capture queue full" forever).
        """
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(self._ws.close(), CLOSE_TIMEOUT_S)
        self._ws = None
        log.info("realtime cleanup: socket closed")
        closer = getattr(self._player, "aclose", None)
        if self._close_player and closer is not None:
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(closer(), CLOSE_TIMEOUT_S + 1.0)
        log.info("realtime cleanup: player closed")
        if self._own_session and self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def _send(self, payload: dict) -> None:
        if self._ws is None or self._ws.closed:
            raise RealtimeError("realtime socket is not open")
        try:
            await self._ws.send_str(json.dumps(payload))
        except Exception as exc:
            raise RealtimeError(f"realtime send failed: {exc}") from exc

    # --- the two directions ----------------------------------------------

    async def pump_microphone(
        self,
        frames: AsyncIterator[bytes],
        stop: asyncio.Event | None = None,
        should_continue: Callable[[], bool] | None = None,
        idle_timeout_s: float | None = None,
    ) -> str:
        """Stream mic audio up for as long as the conversation lasts, INCLUDING
        while the model is speaking — that is what makes barge-in possible at
        all. Frames are 16 kHz mono from the reSpeaker; the API wants 24 kHz.

        Returns why it stopped: "stopped" | "idle" | "hangup" | "exhausted".

        The exit conditions are checked BETWEEN frames and this coroutine is
        never cancelled mid-`anext`: cancelling a task blocked on the shared
        capture iterator closes that async generator permanently, which would
        take the microphone down for the rest of the process (a bug this
        codebase has already paid for once, in _flush_residual).
        """
        while True:
            if stop is not None and stop.is_set():
                return "stopped"
            if should_continue is not None and not should_continue():
                return "stopped"
            if self.ended_by_model:
                return "hangup"
            if (
                idle_timeout_s is not None
                and time.monotonic() - self.last_activity > idle_timeout_s
            ):
                return "idle"
            try:
                frame = await anext(frames)
            except StopAsyncIteration:
                return "exhausted"
            converted = self._to_api.process(frame)
            if not converted:
                continue
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(converted).decode("ascii"),
                }
            )

    async def run(self) -> None:
        """Consume server events until the socket closes."""
        if self._ws is None:
            raise RealtimeError("connect() first")
        async for message in self._ws:
            if message.type is aiohttp.WSMsgType.TEXT:
                await self._handle(json.loads(message.data))
            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                raise RealtimeError(f"realtime socket closed: {message.type}")

    async def _handle(self, event: dict) -> None:
        kind = event.get("type", "")
        if kind == "error":
            self._handle_error(event)
            return
        # Any traffic that is the conversation actually happening keeps the
        # session alive; the idle timeout only fires on real silence.
        if kind.startswith(("input_audio_buffer.", "response.")):
            self.last_activity = time.monotonic()

        if kind == "input_audio_buffer.speech_started":
            self.timings.speech_started_ms = self._ms()
            log.info("user speech started")
            if self._on_speech_started is not None:
                self._on_speech_started()
            await self._barge_in()
        elif kind == "input_audio_buffer.speech_stopped":
            self.timings.speech_stopped_ms = self._ms()
            log.info("user speech stopped")
            if self._on_speech_stopped is not None:
                self._on_speech_stopped()
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            await self._on_audio_delta(event)
        elif kind in (
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        ):
            if self._on_transcript and event.get("delta"):
                self._on_transcript(event["delta"])
        elif kind == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "message":
                self._current_item = item.get("id")
        elif kind == "response.function_call_arguments.done":
            await self._on_tool_call(event)
        elif kind == "response.done":
            # The SERVER has finished generating. That says nothing about the
            # speaker: the model delivers a reply far faster than it takes to
            # speak it, so the rabbit is usually still talking. Local playback
            # is judged by the player, never by this event.
            self._server_response_active = False
            log.info(
                "response.done (server finished; local audio still playing: %s)",
                self._local_audio_pending(),
            )

    def _handle_error(self, event: dict) -> None:
        """Not every `error` event is fatal — treating them all as fatal is what
        made a WORKING barge-in end the conversation.

        Benign races are logged and swallowed; only errors that genuinely make
        the session unusable (auth, quota, expiry) bring it down so the caller
        can re-arm the turn-based path. Anything unrecognised is logged and the
        session continues: if it really was fatal the socket closes anyway, and
        `run()` reports that.
        """
        error = event.get("error") or {}
        code = str(error.get("code") or error.get("type") or "")
        message = error.get("message", "")
        if code in BENIGN_ERROR_CODES:
            log.debug("realtime benign race (%s): %s", code, message)
            return
        if code in FATAL_ERROR_CODES:
            raise RealtimeError(f"realtime API error [{code}]: {message}")
        log.warning("realtime API error [%s]: %s — continuing", code, message)

    async def _on_audio_delta(self, event: dict) -> None:
        payload = event.get("delta")
        if not payload:
            return
        if self.timings.first_audio_delta_ms is None:
            self.timings.first_audio_delta_ms = self._ms()
        if not self._server_response_active:
            # a new response: playback position restarts, so a later truncate
            # reports THIS reply's played time, not a running total
            self._server_response_active = True
            self._player.reset_position()
            await self._player.resume()
            if self._on_response_start is not None:
                self._on_response_start()  # UX: the rabbit is answering
        item_id = event.get("item_id")
        if item_id:
            self._current_item = item_id
        self._player.write(base64.b64decode(payload))
        if self.timings.playback_started_ms is None:
            self.timings.playback_started_ms = self._ms()

    def _local_audio_pending(self) -> bool:
        return bool(getattr(self._player, "has_unplayed_audio", False))

    async def _barge_in(self) -> None:
        """User started talking: stop the speaker and tell the model exactly how
        far it got, so its transcript matches what was actually heard.

        The trigger is LOCAL PLAYBACK, not the server's generation state. The
        model finishes generating long before the rabbit finishes speaking, so
        gating on the server's `response.done` meant that by the time the user
        cut in there was "nothing to interrupt" and the speaker just kept
        talking (hardware, July 2026: no barge-in log ever appeared).
        """
        server_active = self._server_response_active
        local_pending = self._local_audio_pending()
        if not (server_active or local_pending):
            log.info("barge-in ignored: nothing playing (server idle, no local audio)")
            return
        log.info(
            "barge-in: interrupting (server_active=%s local_audio_pending=%s)",
            server_active,
            local_pending,
        )
        started = time.monotonic()
        played_ms = self._player.played_ms
        await self._player.stop()
        self._server_response_active = False
        with contextlib.suppress(RealtimeError):
            # With server-side VAD the server cancels the response itself on
            # speech_started. Sending our own response.cancel then races it and
            # comes back "response_cancel_not_active" — harmless in itself, but
            # it used to kill the session on every successful barge-in. Only
            # cancel when nothing else will.
            if not self._server_side_turn_detection:
                await self._send({"type": "response.cancel"})
            if self._current_item and played_ms > 0:
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._current_item,
                        "content_index": 0,
                        "audio_end_ms": played_ms,
                    }
                )
            elif self._current_item:
                # Cut before a single frame reached the speaker: there is
                # nothing to trim, and truncating at 0 only invites an
                # invalid-value error. The server's own cancel already means
                # none of it was heard.
                log.debug("barge-in before any audio played; no truncate needed")
        cut_ms = round((time.monotonic() - started) * 1000)
        self.timings.interruptions.append(cut_ms)
        log.info("barge-in: cut playback in %d ms after %d ms of speech", cut_ms, played_ms)
        if self._on_barge_in is not None:
            self._on_barge_in()  # UX: straight back to listening

    async def _on_tool_call(self, event: dict) -> None:
        """Body tools (ears, LEDs, gestures) stay available in realtime mode."""
        if self._tool_executor is None:
            return
        name, call_id = event.get("name", ""), event.get("call_id")
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if name == END_CONVERSATION_TOOL:
            # Explicit hang-up: the pump sees this and closes the session.
            self.ended_by_model = True
            log.info("realtime: model ended the conversation")
            return
        try:
            result = await self._tool_executor(name, arguments)
        except Exception as exc:  # a failed gesture must not kill the call
            log.exception("realtime tool %s failed", name)
            result = f"error: {exc}"
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result) if not isinstance(result, str) else result,
                },
            }
        )
        await self._send({"type": "response.create"})


async def converse(
    session: RealtimeSession,
    frames: AsyncIterator[bytes],
    idle_timeout_s: float | None = None,
    stop: asyncio.Event | None = None,
) -> str:
    """Run one continuous conversation and return why it ended.

    The mic pump runs INLINE (never as a cancellable task): it holds the shared
    capture iterator, and cancelling it mid-`anext` would close that generator
    for good. Only the event reader is a task, and it is safe to cancel.

    Any failure surfaces as RealtimeError so the caller can re-arm the
    turn-based path — a broken realtime session must never leave the rabbit
    mute, it just costs the full-duplex behaviour.
    """
    events = asyncio.create_task(session.run())
    try:
        reason = await session.pump_microphone(
            frames,
            stop=stop,
            should_continue=lambda: not events.done(),
            idle_timeout_s=idle_timeout_s,
        )
        # The event reader failing IS the error we must report (the pump would
        # otherwise just return "stopped" and hide it).
        if events.done() and not events.cancelled():
            exc = events.exception()
            if exc is not None:
                raise exc
        return reason
    finally:
        events.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await events
