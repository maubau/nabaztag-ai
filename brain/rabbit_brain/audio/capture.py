"""Microphone capture (docs/ARCHITECTURE.md §6.2.1).

The reSpeaker Flex XVF3800 shows up as USB Audio Class card "C16K6Ch"
(16 kHz / 6 channels, s16le). Channel 0 is the on-chip processed output
(beamformed, NS/AGC) — by far the loudest and clearest — so we capture all
six channels and extract the selected one (hardware-verified on the Bolt,
July 2026). Always address the card by STABLE name (hw:CARD=...), never by
numeric index: card numbers change across reboots.

All frames are mono int16 little-endian PCM. `sounddevice` is an optional
dependency (the `audio` extra); tests use WavCapture and synthetic PCM.
"""

from __future__ import annotations

import asyncio
import logging
import re
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_BLOCK_SAMPLES = 512  # 32 ms at 16 kHz — the silero-vad chunk size
_BYTES_PER_SAMPLE = 2  # s16le everywhere

_CARD_RE = re.compile(r"CARD=([^,]+)")


def resolve_input_device(devices: list[dict], wanted: str) -> int | None:
    """Find the PortAudio input-device index matching an ALSA-style name.

    PortAudio does not accept arbitrary ALSA PCM strings the way arecord
    does — sounddevice matches devices by index or by substring of the
    PortAudio device NAME. So from "hw:CARD=C16K6Ch,DEV=0" we extract the
    card token ("C16K6Ch") and look for an input-capable device whose name
    contains it. Returns None if nothing matches.
    """
    match = _CARD_RE.search(wanted)
    token = (match.group(1) if match else wanted).lower()
    for index, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0 and token in dev.get("name", "").lower():
            return index
    return None


def extract_channel(pcm: bytes, channels: int, selected: int) -> bytes:
    """Pull one channel out of interleaved s16le PCM."""
    if channels == 1:
        return pcm
    if not 0 <= selected < channels:
        raise ValueError(f"channel {selected} outside 0..{channels - 1}")
    samples = memoryview(pcm).cast("h")
    import array

    mono = array.array("h", samples[selected::channels])
    return mono.tobytes()


@runtime_checkable
class MicCapture(Protocol):
    """Source of mono int16 PCM blocks."""

    @property
    def sample_rate(self) -> int: ...

    def frames(self) -> AsyncIterator[bytes]:
        """Yield mono s16le blocks of block_samples samples each."""
        ...


class AlsaCapture:
    """ALSA capture via sounddevice, with channel selection.

    The callback runs on PortAudio's thread; blocks cross into asyncio through
    a bounded queue. If the consumer stalls (it shouldn't — the pipeline drops
    work, not frames), the oldest blocks are discarded rather than blocking
    the audio thread.
    """

    def __init__(
        self,
        device: str | int = "hw:CARD=C16K6Ch,DEV=0",
        sample_rate: int = 16_000,
        channels: int = 6,
        selected_channel: int = 0,
        block_samples: int = DEFAULT_BLOCK_SAMPLES,
        # ~9.6 s of headroom at 32 ms/block (hardware round, July 2026: "capture
        # queue full" fired repeatedly at the old 64-block/~2s buffer during
        # agent+TTS+playback turns). Cheap (~6 KB/block raw 6ch) insurance
        # against event-loop scheduling jitter while the pipeline's own drain
        # (VoicePipeline._drain_frames/_drain_while_gated) is the real fix.
        queue_blocks: int = 300,
    ):
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        self._selected = selected_channel
        self._block_samples = block_samples
        self._queue_blocks = queue_blocks
        self._dropped = 0
        self._started = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _resolve_device(self, sd) -> str | int:
        """An explicit index passes through; an ALSA-style string is resolved
        against the PortAudio device list (see resolve_input_device). If no
        match is found the original string is handed to PortAudio as-is —
        it may still match as a name substring."""
        if isinstance(self._device, int):
            return self._device
        index = resolve_input_device(list(sd.query_devices()), self._device)
        if index is not None:
            log.info("capture device %r resolved to PortAudio index %d", self._device, index)
            return index
        log.warning("no PortAudio device matches %r, passing it through", self._device)
        return self._device

    async def frames(self) -> AsyncIterator[bytes]:
        if self._started:
            # Two live consumers would fight over the same ALSA device and
            # each would starve the other's queue — always a bug, never a
            # valid topology (only one process/pipeline may own the mic).
            raise RuntimeError(
                "AlsaCapture.frames() called twice — only one consumer may own "
                "the microphone at a time"
            )
        self._started = True

        import sounddevice as sd  # optional dep: pip install 'rabbit-brain[audio]'

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._queue_blocks)

        def _push(block: bytes) -> None:
            if queue.full():
                queue.get_nowait()
                self._dropped += 1
                if self._dropped % 100 == 1:
                    log.warning("capture queue full, dropping blocks (total %d)", self._dropped)
            queue.put_nowait(block)

        def _callback(indata, frame_count, time_info, status) -> None:
            if status:
                log.warning("ALSA capture status: %s", status)
            loop.call_soon_threadsafe(_push, bytes(indata))

        stream = sd.RawInputStream(
            device=self._resolve_device(sd),
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
            blocksize=self._block_samples,
            callback=_callback,
        )
        with stream:
            while True:
                block = await queue.get()
                yield extract_channel(block, self._channels, self._selected)


class WavCapture:
    """Replay a WAV file as capture frames — CI fixtures and dev without a mic.

    Applies the same channel extraction as AlsaCapture, so multichannel
    fixtures exercise the real selection path.
    """

    def __init__(
        self,
        path: Path | str,
        selected_channel: int = 0,
        block_samples: int = DEFAULT_BLOCK_SAMPLES,
        realtime: bool = False,
    ):
        self._path = Path(path)
        self._selected = selected_channel
        self._block_samples = block_samples
        self._realtime = realtime
        with wave.open(str(self._path), "rb") as w:
            if w.getsampwidth() != _BYTES_PER_SAMPLE:
                raise ValueError(f"{self._path}: expected 16-bit PCM")
            self._sample_rate = w.getframerate()
            self._channels = w.getnchannels()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def frames(self) -> AsyncIterator[bytes]:
        block_s = self._block_samples / self._sample_rate
        with wave.open(str(self._path), "rb") as w:
            while True:
                block = w.readframes(self._block_samples)
                if not block:
                    return
                yield extract_channel(block, self._channels, self._selected)
                if self._realtime:
                    await asyncio.sleep(block_s)
                else:
                    await asyncio.sleep(0)  # let other tasks run
