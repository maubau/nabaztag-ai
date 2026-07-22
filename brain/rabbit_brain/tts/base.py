"""TTS provider interface (docs/ARCHITECTURE.md §6.2.6).

Providers synthesize one utterance to a local MP3 file and report its duration
— the duration is what drives TimedPlaybackHandle and the half-duplex gate, so
it is part of the contract, not an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TTSResult:
    path: Path
    duration_s: float
    # which backend ACTUALLY produced this audio ("deepgram", "elevenlabs",
    # "piper", …). Lets a caller tell a real result from a fallback — the
    # tts-bench must never record a Deepgram-fallback clip under the "piper"
    # label (latency round, July 2026). None where a provider doesn't set it.
    provider: str | None = None


@runtime_checkable
class TTSProvider(Protocol):
    async def synth(self, text: str, language: str | None = None) -> TTSResult:
        """Synthesize `text` to an MP3 file inside the provider's audio dir.
        `language` is the utterance language as detected by the STT ("it",
        "en", …) for voice routing; providers with one voice ignore it."""
        ...
