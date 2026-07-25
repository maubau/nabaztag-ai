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

from ..audio.streaming import Resampler

log = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
API_RATE = 24000  # the Realtime API speaks pcm16 at 24 kHz


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
        tools: list[dict] | None = None,
        tool_executor: Callable | None = None,
        session: aiohttp.ClientSession | None = None,
        on_transcript: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key
        self._player = player
        self._instructions = instructions
        self._model = model
        self._voice = voice
        self._tools = tools or []
        self._tool_executor = tool_executor
        self._on_transcript = on_transcript
        self._to_api = Resampler(mic_rate, API_RATE)
        self._session = session
        self._own_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._current_item: str | None = None
        self._responding = False
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
                    "audio": {
                        "input": {"format": "pcm16", "turn_detection": {"type": "server_vad"}},
                        "output": {"format": "pcm16", "voice": self._voice},
                    },
                    "tools": self._tools,
                },
            }
        )

    async def aclose(self) -> None:
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
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

    async def pump_microphone(self, frames: AsyncIterator[bytes]) -> None:
        """Stream mic audio up for as long as the conversation lasts. Frames are
        16 kHz mono from the reSpeaker; the API wants 24 kHz."""
        async for frame in frames:
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
            raise RealtimeError(f"realtime API error: {event.get('error')}")

        if kind == "input_audio_buffer.speech_started":
            self.timings.speech_started_ms = self._ms()
            await self._barge_in()
        elif kind == "input_audio_buffer.speech_stopped":
            self.timings.speech_stopped_ms = self._ms()
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
            self._responding = False

    async def _on_audio_delta(self, event: dict) -> None:
        payload = event.get("delta")
        if not payload:
            return
        if self.timings.first_audio_delta_ms is None:
            self.timings.first_audio_delta_ms = self._ms()
        if not self._responding:
            # a new response: playback position restarts, so a later truncate
            # reports THIS reply's played time, not a running total
            self._responding = True
            self._player.reset_position()
            await self._player.resume()
        item_id = event.get("item_id")
        if item_id:
            self._current_item = item_id
        self._player.write(base64.b64decode(payload))
        if self.timings.playback_started_ms is None:
            self.timings.playback_started_ms = self._ms()

    async def _barge_in(self) -> None:
        """User started talking: stop the speaker and tell the model exactly how
        far it got, so its transcript matches what was actually heard."""
        if not self._responding:
            return
        started = time.monotonic()
        played_ms = self._player.played_ms
        await self._player.stop()
        self._responding = False
        with contextlib.suppress(RealtimeError):
            await self._send({"type": "response.cancel"})
            if self._current_item:
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._current_item,
                        "content_index": 0,
                        "audio_end_ms": played_ms,
                    }
                )
        cut_ms = round((time.monotonic() - started) * 1000)
        self.timings.interruptions.append(cut_ms)
        log.info("barge-in: cut playback in %d ms after %d ms of speech", cut_ms, played_ms)

    async def _on_tool_call(self, event: dict) -> None:
        """Body tools (ears, LEDs, gestures) stay available in realtime mode."""
        if self._tool_executor is None:
            return
        name, call_id = event.get("name", ""), event.get("call_id")
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
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
    session: RealtimeSession, frames: AsyncIterator[bytes], timeout_s: float | None = None
) -> RealtimeTimings:
    """Run both directions until the socket ends or `timeout_s` elapses.

    Any failure surfaces as RealtimeError so the caller can re-arm the
    turn-based path — a broken realtime session must never leave the rabbit
    mute, it just costs the full-duplex behaviour for that turn.
    """
    pump = asyncio.create_task(session.pump_microphone(frames))
    events = asyncio.create_task(session.run())
    try:
        done, pending = await asyncio.wait(
            {pump, events}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in (pump, events):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return session.timings
