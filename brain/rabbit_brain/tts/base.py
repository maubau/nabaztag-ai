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


@runtime_checkable
class TTSProvider(Protocol):
    async def synth(self, text: str) -> TTSResult:
        """Synthesize `text` to an MP3 file inside the provider's audio dir."""
        ...
