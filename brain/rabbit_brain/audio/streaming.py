"""Streaming PCM playback + rate conversion for the Realtime path.

The turn-based path synthesizes a whole MP3 and then plays it. The Realtime
path cannot: audio arrives as PCM deltas while the model is still speaking, and
the whole point is to start playing the first delta immediately rather than
waiting for the utterance to finish. So this module provides:

  * `Resampler` — the mic runs at 16 kHz (the reSpeaker's 6-channel USB
    firmware) while the Realtime API speaks pcm16 at 24 kHz, so every chunk
    crosses a rate boundary in both directions. It is STATEFUL on purpose:
    resampling each chunk independently would restart the interpolation at every
    boundary and add a click per chunk, ~50 times a second.

  * `StreamingAudioPlayer` — push PCM in, hear it now. It also answers the one
    question barge-in depends on: HOW MUCH have we actually played? When the
    user talks over the rabbit we must tell the model exactly how far it got
    (`conversation.item.truncate`), otherwise its transcript keeps words the
    user never heard and the conversation drifts out of sync.

Both are deliberately dependency-free (no numpy): at 16-24 kHz mono the work is
trivial, and it keeps them testable anywhere.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)

_INT16_MIN, _INT16_MAX = -32768, 32767


class Resampler:
    """Stateful linear-interpolation resampler for mono int16 PCM.

    Keeps the last input sample and the fractional read position across calls,
    so a stream cut into arbitrary chunks resamples exactly as if it had been
    one buffer.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self._step = src_rate / dst_rate
        self._prev: int | None = None
        self._pos = 0.0

    def process(self, pcm: bytes) -> bytes:
        if self.src_rate == self.dst_rate:
            return pcm
        samples = array.array("h")
        samples.frombytes(pcm)
        if not samples:
            return b""
        buf = array.array("h")
        if self._prev is not None:
            buf.append(self._prev)
        buf.extend(samples)
        limit = len(buf) - 1
        if limit < 1:
            self._prev = buf[-1]
            return b""
        out = array.array("h")
        pos = self._pos
        while pos < limit:
            index = int(pos)
            frac = pos - index
            first, second = buf[index], buf[index + 1]
            value = int(first + (second - first) * frac)
            out.append(max(_INT16_MIN, min(_INT16_MAX, value)))
            pos += self._step
        self._pos = pos - limit
        self._prev = buf[-1]
        return out.tobytes()


def expand_channels(pcm: bytes, channels: int) -> bytes:
    """Duplicate mono int16 PCM across `channels` interleaved channels."""
    if channels <= 1:
        return pcm
    mono = array.array("h")
    mono.frombytes(pcm)
    out = array.array("h", bytes(len(pcm) * channels))
    for i, sample in enumerate(mono):
        base = i * channels
        for c in range(channels):
            out[base + c] = sample
    return out.tobytes()


class StreamingAudioPlayer:
    """Plays PCM pushed in live, on a Bolt output device.

    `source_rate` is the rate of the PCM handed to `write` (24 kHz from the
    Realtime API); it is converted to the device's own rate and channel count.
    """

    def __init__(
        self,
        device: int | str | None,
        device_rate: int,
        device_channels: int,
        source_rate: int = 24000,
    ):
        self._device = device
        self._rate = device_rate
        self._channels = device_channels
        self._source_rate = source_rate
        self._resampler = Resampler(source_rate, device_rate)
        # Queued chunks carry the EPOCH they belong to. A barge-in bumps the
        # epoch, so audio from the cancelled reply that is already in flight is
        # dropped instead of leaking into the next one.
        self._queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        self._epoch = 0
        self._stream = None
        self._writer: asyncio.Task | None = None
        self._device_frames = 0  # frames handed to the device
        self._stopping = False

    # --- lifecycle -------------------------------------------------------

    def _open_stream(self):
        import sounddevice as sd

        return sd.RawOutputStream(
            samplerate=self._rate, channels=self._channels, dtype="int16", device=self._device
        )

    def _ensure_writer(self) -> None:
        """There must ALWAYS be a live consumer once the device is open.

        The writer used to die permanently the first time a barge-in aborted the
        stream mid-write: abort() makes the blocked write raise, the task
        returned, and resume() restarted the device but not the consumer — so
        every later reply queued silently forever.
        """
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._drain())

    async def start(self) -> None:
        self._stream = self._open_stream()
        self._stream.start()
        self._stopping = False
        self._ensure_writer()

    async def aclose(self) -> None:
        await self.stop()
        self._queue.put_nowait(None)  # let the writer finish
        if self._writer is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer
            self._writer = None
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.close()
            self._stream = None

    # --- the audio path --------------------------------------------------

    def write(self, pcm: bytes) -> None:
        """Queue a PCM delta for immediate playback (never blocks the caller —
        the model keeps streaming while the device drains)."""
        if self._stopping or not pcm:
            return
        converted = expand_channels(self._resampler.process(pcm), self._channels)
        if converted:
            self._queue.put_nowait((self._epoch, converted))
            self._ensure_writer()

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            epoch, chunk = item
            if self._stopping or epoch != self._epoch:
                continue  # belongs to a reply the user interrupted
            try:
                await asyncio.to_thread(self._stream.write, chunk)
            except Exception:
                # An intentional abort() makes the in-flight write raise
                # (PortAudio -9999). That is the barge-in working, not a
                # failure — and it must NEVER end the writer, or the next
                # reply would queue into a consumer that no longer exists.
                if self._stopping or epoch != self._epoch:
                    continue
                log.warning("streaming write failed; keeping the writer alive", exc_info=True)
                continue
            if epoch == self._epoch:
                self._device_frames += len(chunk) // (2 * self._channels)

    # --- barge-in --------------------------------------------------------

    @property
    def played_ms(self) -> int:
        """ESTIMATED milliseconds actually heard, since `reset_position`.

        This is an estimate, not a measurement: it counts frames DELIVERED to
        the device, and the last few are still sitting in the device buffer
        rather than in the room. Where PortAudio reports the stream's output
        latency we subtract it, which removes most of that error; where it does
        not, the figure runs slightly ahead of reality.

        It is what `conversation.item.truncate` gets, so the bias matters: the
        residual error is bounded by the device buffer (tens of ms, i.e. well
        under a word) and it errs towards claiming slightly MORE was heard.
        That is the safer direction — trimming a word the user just caught is
        recoverable, whereas telling the model it never said something the user
        did hear leaves the two of you disagreeing about the conversation.
        """
        if self._rate <= 0:
            return 0
        delivered_ms = self._device_frames * 1000 / self._rate
        return max(0, int(delivered_ms - self._output_latency_ms()))

    def _output_latency_ms(self) -> float:
        """The device's own buffering, if PortAudio exposes it (0 otherwise)."""
        try:
            latency = float(getattr(self._stream, "latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return latency * 1000.0

    def reset_position(self) -> None:
        """Start counting a new response's playback from zero."""
        self._device_frames = 0

    async def stop(self) -> None:
        """Cut playback NOW and drop anything queued (user is talking)."""
        self._stopping = True
        # Bumping the epoch invalidates chunks already queued or mid-write, so
        # the interrupted reply cannot bleed into the next one.
        self._epoch += 1
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        if self._stream is not None:
            with contextlib.suppress(Exception):
                # abort() discards the device buffer; stop() would let it drain
                # and the rabbit would keep talking over the user.
                self._stream.abort()

    async def resume(self) -> None:
        """Re-open the device after a barge-in, ready for the next response."""
        if self._stream is None:
            return
        self._stopping = False
        self._resampler = Resampler(self._source_rate, self._rate)
        with contextlib.suppress(Exception):
            self._stream.start()
        # The abort almost certainly killed the in-flight write; make sure a
        # consumer exists again before the next delta arrives.
        self._ensure_writer()
