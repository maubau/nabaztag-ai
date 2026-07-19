"""STTProvider interface — dual profile behind one surface (§6.2.4).

Providers consume an async stream of mono s16le PCM chunks so a streaming
backend (Deepgram) can transcribe WHILE the user is still speaking; buffered
backends (Whisper API, faster-whisper) simply drain the stream first.
"""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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


@runtime_checkable
class STTProvider(Protocol):
    async def transcribe(self, chunks: AsyncIterator[bytes], sample_rate: int) -> STTResult: ...


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
