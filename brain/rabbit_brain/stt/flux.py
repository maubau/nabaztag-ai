"""Deepgram Flux — streaming STT with PROVIDER-SIDE turn detection (Gate L1).

Flux V2 (`wss://api.deepgram.com/v2/listen`, `model=flux-general-multi`) does
speech recognition and end-of-turn detection in one pass, so the client no
longer waits out a local silence window before it can act. That window was the
single biggest fixed cost in the voice loop: 1600 ms of Silero silence + ~275 ms
of nova-3 finalisation ≈ 1875 ms between the user actually stopping and the
transcript being usable. Flux is expected to land the same moment in the
100-500 ms range (hardware target).

Protocol shape (VERIFY ON HARDWARE — see docs/OJN_API_NOTES.md): the socket
carries JSON messages with a `type`; the interesting one is `TurnInfo`, whose
`event` is one of `StartOfTurn`, `Update`, `EagerEndOfTurn`, `TurnResumed`,
`EndOfTurn`. Individual unknown types/events and stray non-JSON frames are
ignored (a healthy stream mixes in Connected/Metadata), and every field is
read with a default, so a schema that differs SLIGHTLY degrades gracefully.

But a WHOLESALE schema mismatch (the server is clearly talking but we
recognise no TurnInfo at all) is turned into a FluxSchemaError, both when the
socket closes having produced no TurnInfo and, crucially, EARLY — after a
handful of unrecognised messages — so it doesn't sit silent until the
pipeline's turn-timeout cancels the task (which would abandon the turn with
NO Whisper fallback). Raising lets FallbackSTT replay the buffered audio to
Whisper. A real stream emits StartOfTurn on the first audio, so the early
guard never trips on a healthy connection. (Hardware round, July 2026: the
earlier "unknown schema → Whisper fallback" claim was not actually
guaranteed by the code — this closes that gap.)

This round uses ONLY `EndOfTurn`. `EagerEndOfTurn` is parsed and timestamped
for diagnostics but never acted on — speculative LLM dispatch is explicitly
out of scope until the plain path is measured.

Language: `flux-general-multi` is the it/en multilingual model. Whether it
reports a per-turn detected language is NOT yet hardware-confirmed, so
`_language_of` looks in several plausible places and falls back to None;
None means the TTS keeps its configured default voice. See #19 in
docs/OJN_API_NOTES.md — this is the one open question in this gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

import aiohttp

from .base import EndOfTurnCallback, STTResult

log = logging.getLogger(__name__)

DEFAULT_WS_BASE = "wss://api.deepgram.com/v2/listen"
DEFAULT_MODEL = "flux-general-multi"
# Deepgram's confidence that the turn ended before EndOfTurn fires. Higher =
# more patient (fewer premature cuts, slower); lower = snappier, riskier.
DEFAULT_EOT_THRESHOLD = 0.7
# Server-side ceiling: emit EndOfTurn after this much trailing silence even if
# the confidence never crosses the threshold.
DEFAULT_EOT_TIMEOUT_MS = 5000
# Hard client-side ceiling on one turn, so a silent or wedged socket can never
# hold the pipeline open indefinitely.
DEFAULT_TIMEOUT_S = 30.0
# How many messages we may receive that parse but are NOT a recognised
# TurnInfo, before we treat the stream as a wholesale schema mismatch and
# raise (→ Whisper fallback) rather than waiting out the pipeline timeout.
# A healthy stream sends a TurnInfo (StartOfTurn) on the first audio, well
# inside this budget, so it never trips in practice.
SCHEMA_MISMATCH_THRESHOLD = 12


class FluxSchemaError(RuntimeError):
    """The Flux socket produced messages but no recognisable TurnInfo — the
    wire schema doesn't match what this parser expects. Raised so FallbackSTT
    replays the buffered audio to Whisper instead of the turn being silently
    abandoned at the pipeline timeout."""


class FluxSTT:
    """Deepgram Flux V2. Signals end-of-turn itself; the caller keeps feeding."""

    # The pipeline branches on this: True means "do not close the stream on
    # local VAD, wait for on_end_of_turn" (see stt/base.py).
    detects_end_of_turn = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        eot_threshold: float = DEFAULT_EOT_THRESHOLD,
        eot_timeout_ms: int = DEFAULT_EOT_TIMEOUT_MS,
        ws_base: str = DEFAULT_WS_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key or os.environ["DEEPGRAM_API_KEY"]
        self._model = model
        self._eot_threshold = eot_threshold
        self._eot_timeout_ms = eot_timeout_ms
        self._ws_base = ws_base
        self._timeout_s = timeout_s
        # diagnostics from the last turn, read by the pipeline for its log
        self.last_eager_end_of_turn_at: float | None = None
        self.last_turn_events: list[str] = []

    async def transcribe(
        self,
        chunks: AsyncIterator[bytes],
        sample_rate: int,
        on_end_of_turn: EndOfTurnCallback | None = None,
    ) -> STTResult:
        params = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "eot_threshold": str(self._eot_threshold),
            "eot_timeout_ms": str(self._eot_timeout_ms),
        }
        url = self._ws_base + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.last_eager_end_of_turn_at = None
        self.last_turn_events = []
        async with asyncio.timeout(self._timeout_s):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, headers={"Authorization": f"Token {self._api_key}"}
                ) as ws:
                    sender = asyncio.create_task(self._send(ws, chunks))
                    try:
                        return await self._receive(ws, on_end_of_turn)
                    finally:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)

    async def _send(
        self, ws: aiohttp.ClientWebSocketResponse, chunks: AsyncIterator[bytes]
    ) -> None:
        """Pump frames until cancelled. Unlike the nova-3 path this does NOT
        end the stream on its own — Flux decides when the turn is over, and
        the caller keeps feeding until then."""
        async for chunk in chunks:
            await ws.send_bytes(chunk)

    async def _receive(
        self, ws: aiohttp.ClientWebSocketResponse, on_end_of_turn: EndOfTurnCallback | None
    ) -> STTResult:
        transcript = ""
        recognized = 0  # TurnInfo messages we understood
        unrecognized = 0  # parsed-but-not-TurnInfo, or non-JSON
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                log.warning("flux: non-JSON message ignored")
                unrecognized += 1
                self._raise_if_schema_mismatch(recognized, unrecognized)
                continue
            if data.get("type") != "TurnInfo":
                unrecognized += 1  # Connected / Metadata / anything new
                self._raise_if_schema_mismatch(recognized, unrecognized)
                continue
            recognized += 1
            event = data.get("event")
            self.last_turn_events.append(str(event))
            text = (data.get("transcript") or "").strip()
            if text:
                transcript = text  # each TurnInfo carries the turn so far
            if event == "EagerEndOfTurn":
                # parsed for diagnostics ONLY this round: no speculative
                # dispatch until the plain EndOfTurn path is measured.
                self.last_eager_end_of_turn_at = asyncio.get_running_loop().time()
            elif event == "EndOfTurn":
                if on_end_of_turn is not None:
                    on_end_of_turn(transcript)
                return STTResult(
                    text=transcript,
                    provider="deepgram-flux",
                    language=_language_of(data),
                    audio_cursor_s=_float_or_none(data.get("audio_window_end")),
                    end_of_turn_confidence=_float_or_none(data.get("end_of_turn_confidence")),
                )
        # Socket closed without an EndOfTurn. If we never recognised a single
        # TurnInfo but the server WAS sending us messages, that's a schema
        # mismatch → raise so the Whisper fallback runs (it has the audio).
        if recognized == 0 and unrecognized > 0:
            raise FluxSchemaError(
                f"flux: {unrecognized} message(s), no recognisable TurnInfo (schema mismatch?)"
            )
        # Otherwise: a real but abruptly-ended turn (return what we heard), or
        # a truly silent socket (empty text = no speech).
        log.info("flux: stream ended without EndOfTurn (events=%s)", self.last_turn_events)
        return STTResult(text=transcript, provider="deepgram-flux")

    @staticmethod
    def _raise_if_schema_mismatch(recognized: int, unrecognized: int) -> None:
        if recognized == 0 and unrecognized >= SCHEMA_MISMATCH_THRESHOLD:
            raise FluxSchemaError(
                f"flux: {unrecognized} messages, no recognisable TurnInfo — "
                "wire schema does not match (see stt/flux.py)"
            )


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _language_of(data: dict) -> str | None:
    """Detected language for TTS voice routing, if Flux reports one.

    NOT hardware-confirmed for flux-general-multi (see module docstring), so
    several plausible shapes are accepted and anything unrecognised yields
    None — never a guess from the text itself (§6.2.6).
    """
    for key in ("language", "detected_language"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    languages = data.get("languages")
    if isinstance(languages, list) and languages:
        first = languages[0]
        if isinstance(first, str) and first:
            return first
    return None
