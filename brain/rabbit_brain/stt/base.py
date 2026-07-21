"""STTProvider interface — dual profile behind one surface (§6.2.4).

Providers consume an async stream of mono s16le PCM chunks so a streaming
backend (Deepgram) can transcribe WHILE the user is still speaking; buffered
backends (Whisper API, faster-whisper) simply drain the stream first.

Two end-of-turn models are supported (latency Gate L1, July 2026):

  - CLIENT-side (nova-3 + Silero, the original): the pipeline's local VAD
    decides the utterance is over and CLOSES the chunk stream; the provider
    transcribes what it was given. Costs the full local silence window
    (1600 ms) before the provider even starts finalising.
  - PROVIDER-side (Deepgram Flux): the provider decides, and says so via
    on_end_of_turn while the chunk stream STAYS OPEN. The pipeline keeps
    feeding frames and stops when transcribe() returns. Providers that work
    this way set `detects_end_of_turn = True`, which is how the pipeline
    picks the loop to run — see VoicePipeline._record_and_transcribe.
"""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Called by a provider-side-endpointing STT the moment it decides the user's
# turn has ended (Flux EndOfTurn). Takes the transcript as known at that
# instant; the pipeline uses it to stop the LISTENING feedback immediately.
EndOfTurnCallback = Callable[[str], None]


@dataclass(frozen=True)
class STTResult:
    text: str
    provider: str
    # real-time factor (processing time / audio time) — logged for the
    # local-vs-cloud comparison (task T5); None where it isn't meaningful.
    rtf: float | None = None
    # language detected BY THE STT ("it", "en", …) — drives TTS voice routing
    # (§6.2.6); never inferred from the text with heuristics.
    language: str | None = None
    # --- provider-side endpointing diagnostics (Flux; None elsewhere) ---
    # provider's own audio cursor at the end of the speech it used, in
    # seconds from stream start — lets us compare the provider's idea of
    # "when speech ended" against our wall clock.
    audio_cursor_s: float | None = None
    # Flux's confidence that the turn really ended (eot_threshold decides).
    end_of_turn_confidence: float | None = None


@runtime_checkable
class STTProvider(Protocol):
    async def transcribe(
        self,
        chunks: AsyncIterator[bytes],
        sample_rate: int,
        on_end_of_turn: EndOfTurnCallback | None = None,
    ) -> STTResult:
        """Transcribe the chunk stream. Providers with
        `detects_end_of_turn = True` invoke on_end_of_turn when THEY decide
        the turn is over (the stream stays open); the others ignore it and
        rely on the caller closing the stream."""
        ...


async def drain(chunks: AsyncIterator[bytes]) -> bytes:
    parts = [c async for c in chunks]
    return b"".join(parts)


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
