"""Optional short local wake-confirmation beep (docs/ARCHITECTURE.md §6.2).

Played on the Bolt's own output, NOT through the rabbit. Off by default: with
no AEC on the rabbit/room path a beep could be heard by the mic, so the
pipeline drops the beep-window frames from VAD/STT (beep_guard_ms). Keep the
beep shorter than the guard. Verify with Silero before enabling: if the tone
is classified as speech or leaks into a transcript, keep it disabled.

sounddevice is an optional dependency (the `audio` extra).
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class SineBeep:
    """A short sine tone via sounddevice output (blocking play off the loop)."""

    def __init__(
        self,
        freq_hz: float = 880.0,
        duration_ms: int = 120,
        volume: float = 0.2,
        samplerate: int = 44_100,
        device: str | int | None = None,
    ):
        self._freq = freq_hz
        self._duration_ms = duration_ms
        self._volume = volume
        self._samplerate = samplerate
        self._device = device
        self._wave = None

    def _render(self):
        import numpy as np

        n = int(self._samplerate * self._duration_ms / 1000)
        t = np.arange(n) / self._samplerate
        wave = np.sin(2 * np.pi * self._freq * t) * self._volume
        # short raised-cosine fades so the on/off click doesn't itself trip VAD
        fade = min(n // 4, int(self._samplerate * 0.005))
        if fade:
            ramp = np.linspace(0.0, 1.0, fade)
            wave[:fade] *= ramp
            wave[-fade:] *= ramp[::-1]
        return wave.astype("float32")

    def _play_sync(self) -> None:
        import sounddevice as sd  # optional dep: 'rabbit-brain[audio]'

        if self._wave is None:
            self._wave = self._render()
        sd.play(self._wave, self._samplerate, device=self._device, blocking=True)

    async def __call__(self) -> None:
        await asyncio.to_thread(self._play_sync)
