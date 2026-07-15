"""BodyAdapter protocol — the swappable body beneath the BodyController (§6.6).

Implementations: OjnAdapter (v1) -> ReachyMiniAdapter (P1) -> VentunoQLocalProfile (P2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import BodyCapabilities, BodyEvent, LedSpec


@runtime_checkable
class PlaybackHandle(Protocol):
    """Tracks one audio playback. What makes the half-duplex gate, speech-synced
    gestures, and preemption implementable rather than guessed (§6.6)."""

    async def wait_started(self) -> None: ...

    async def wait_finished(self) -> None: ...

    async def cancel(self) -> None:
        """Stop playback if the body supports it; otherwise raise NotImplementedError.
        Callers must check capabilities.can_cancel_audio first."""
        ...

    @property
    def estimated_duration_s(self) -> float | None: ...


@runtime_checkable
class BodyAdapter(Protocol):
    async def set_ears(self, left: int, right: int) -> None: ...

    async def set_leds(self, spec: LedSpec) -> None: ...

    async def play_audio(self, urls: tuple[str, ...], duration_s: float | None) -> PlaybackHandle:
        """Queue one or more MP3 URLs for playback and return immediately."""
        ...

    async def say(self, text: str) -> PlaybackHandle:
        """Server-side TTS, where the body offers one (OJN tts/say)."""
        ...

    async def play_chor(self, chor: str) -> None:
        """Run a raw choreography string (VAPI 'chor=' format)."""
        ...

    async def sleep(self) -> None: ...

    async def wake(self) -> None: ...

    def events(self) -> AsyncIterator[BodyEvent]:
        """Async stream of button/RFID events."""
        ...

    @property
    def capabilities(self) -> BodyCapabilities: ...
