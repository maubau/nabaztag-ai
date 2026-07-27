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
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

_INT16_MIN, _INT16_MAX = -32768, 32767
# Fallback for how long delivered frames keep sounding when PortAudio does not
# report the stream's latency. Deliberately generous: over-estimating means a
# barge-in still fires just after the last word, while under-estimating means it
# silently does nothing — the failure we are fixing.
_ASSUMED_DEVICE_BUFFER_MS = 250.0
# Minimum silence before the buffer running dry counts as "the rabbit stopped
# talking". Deltas arrive over the network and an item boundary empties the
# buffer for a moment, so a bare underrun is not the end of the reply; without
# this debounce the speaking indicator would flicker off and on mid-sentence.
_DRAIN_DEBOUNCE_S = 0.25
# Ceiling on the remaining awaitable teardown steps (the WebSocket close). The
# player itself no longer needs one — with the callback design there is no
# Python thread that can block — but nothing in the cleanup path may hold the
# pipeline back from re-arming, so anything that can wait, waits with a bound.
CLOSE_TIMEOUT_S = 2.0


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


class AudioBackendUnrecoverable(RuntimeError):
    """The output device could not be reclaimed.

    Raised instead of limping on, because the failure mode it replaces is the
    dangerous one: a stream we abandoned still holds the device, so the pipeline
    re-arms, the wake word lights the LEDs, and the reply is SILENT while every
    log line says the runtime is healthy. Better to die and let systemd's
    Restart=always rebuild the process — and PortAudio with it.
    """


class StreamingAudioPlayer:
    """Plays PCM pushed in live, on a Bolt output device.

    CALLBACK-DRIVEN on purpose. The first implementation pushed audio with
    blocking `RawOutputStream.write()` calls inside `asyncio.to_thread`, and
    that design has no safe way out: `to_thread` cannot be cancelled, so a write
    stuck in the driver (which is exactly what happens around `abort()`) hangs
    teardown forever, and abandoning it leaves the device held by a zombie
    stream — the rabbit then wakes, lights up, and answers in silence.

    Here PortAudio pulls instead: its own callback thread copies out of a ring
    buffer and never calls into Python code that can block. That removes the
    whole class of problems at once —
      * barge-in is instant and needs no `abort()`: clearing the buffer makes
        the very next callback output silence;
      * there is no Python thread to get stuck, so teardown is bounded by
        construction;
      * `played_ms` stops being an estimate — the callback counts the frames it
        actually handed to the device.

    `source_rate` is the rate of the PCM handed to `write` (24 kHz from the
    Realtime API); it is converted to the device's own rate and channel count.
    """

    def __init__(
        self,
        device: int | str | None,
        device_rate: int,
        device_channels: int,
        source_rate: int = 24000,
        on_playback_started: Callable[[], None] | None = None,
        on_playback_drained: Callable[[], None] | None = None,
        on_playback_cut: Callable[[], None] | None = None,
    ):
        self._device = device
        self._rate = device_rate
        self._channels = device_channels
        self._source_rate = source_rate
        self._frame_bytes = 2 * device_channels
        self._resampler = Resampler(source_rate, device_rate)
        # Ring buffer of (generation, buffer, offset). The generation is what
        # keeps one reply's audio out of the next one: a barge-in bumps it, and
        # anything still carrying the old value is dropped rather than played.
        self._pending: deque[tuple[int, bytes, int]] = deque()
        self._lock = threading.Lock()
        self._generation = 0
        self._stream = None
        self._consumed_frames = 0  # frames the callback really handed over
        self._last_consumed_at: float | None = None
        self._latency_s = 0.0  # read once at open, never re-queried
        self.backend_broken = False
        # Playback lifecycle, for the UX layer. `assistant_speaking` must follow
        # the SPEAKER, not the server: `response.done` arrives while seconds of
        # PCM are still queued here, so anything driven by the server's state
        # would stop animating while the rabbit is visibly still talking.
        self._on_started = on_playback_started
        self._on_drained = on_playback_drained
        self._on_cut = on_playback_cut
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playing = False
        self._dry_since: float | None = None

    # --- playback lifecycle events ----------------------------------------

    def _emit(self, callback: Callable[[], None] | None) -> None:
        """Hand an event to the asyncio loop. Called from PortAudio's own
        thread, so it must never touch loop state directly — and it must never
        raise: an exception here would propagate into the audio callback and
        take the stream down over a cosmetic notification."""
        if callback is None:
            return
        loop = self._loop
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(callback)
            else:
                callback()
        except Exception:  # pragma: no cover - defensive
            log.debug("playback event callback failed", exc_info=True)

    def _note_fill(self, filled: int, wanted: int, now: float) -> tuple[bool, bool]:
        """Update the playing/dry state from one callback block.

        Returns (started, drained) so the caller can emit OUTSIDE the lock.
        """
        started = drained = False
        if filled:
            self._dry_since = None
            if not self._playing:
                self._playing = True
                started = True
        if filled < wanted:
            # The buffer ran out mid-block. That alone means nothing — the next
            # delta may be milliseconds away — so only a sustained gap ends the
            # utterance.
            if self._dry_since is None:
                self._dry_since = now
            elif self._playing and (now - self._dry_since) >= self._drain_window_s:
                self._playing = False
                self._dry_since = None
                drained = True
        return started, drained

    @property
    def _drain_window_s(self) -> float:
        return max(self._latency_s, _DRAIN_DEBOUNCE_S)

    @property
    def is_playing(self) -> bool:
        """True between playback_started and playback_drained/cut."""
        with self._lock:
            return self._playing

    # --- lifecycle -------------------------------------------------------

    def _open_stream(self):
        import sounddevice as sd

        return sd.RawOutputStream(
            samplerate=self._rate,
            channels=self._channels,
            dtype="int16",
            device=self._device,
            callback=self._callback,
        )

    async def start(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self._stream = self._open_stream()
        self._stream.start()
        # Read the latency ONCE, while the stream is healthy: querying it later
        # means calling into PortAudio about a device we may have just stopped.
        try:
            self._latency_s = float(getattr(self._stream, "latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._latency_s = 0.0

    async def aclose(self) -> None:
        """Release the device. Bounded by construction — there is no Python
        thread here that can block — but if PortAudio still refuses to let go we
        say so loudly instead of pretending the runtime is healthy."""
        with self._lock:
            self._generation += 1
            self._pending.clear()
            was_playing = self._playing
            self._playing = False
            self._dry_since = None
        if was_playing:
            self._emit(self._on_cut)
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            self.backend_broken = True
            log.critical("could not release the audio device: %s", exc)
            raise AudioBackendUnrecoverable(f"audio device not released: {exc}") from exc
        log.info("streaming player closed")

    # --- the audio path --------------------------------------------------

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio pulls from here on its own thread. Must never block: no
        I/O, no awaits, just a memcpy out of the ring buffer (and silence when
        it runs dry, which is what makes a barge-in instant)."""
        wanted = frames * self._frame_bytes
        out = memoryview(outdata).cast("B")
        filled = 0
        with self._lock:
            generation = self._generation
            while filled < wanted and self._pending:
                gen, buf, offset = self._pending[0]
                if gen != generation:
                    self._pending.popleft()  # belongs to an interrupted reply
                    continue
                take = min(wanted - filled, len(buf) - offset)
                out[filled : filled + take] = buf[offset : offset + take]
                filled += take
                if offset + take >= len(buf):
                    self._pending.popleft()
                else:
                    self._pending[0] = (gen, buf, offset + take)
            now = time.monotonic()
            if filled:
                self._consumed_frames += filled // self._frame_bytes
                self._last_consumed_at = now
            started, drained = self._note_fill(filled, wanted, now)
        if filled < wanted:
            out[filled:wanted] = b"\x00" * (wanted - filled)
        # Outside the lock: these hop to the asyncio loop and must not hold up
        # the next block of audio.
        if started:
            self._emit(self._on_started)
        if drained:
            self._emit(self._on_drained)

    def write(self, pcm: bytes) -> None:
        """Queue a PCM delta for immediate playback (never blocks: the model
        keeps streaming while PortAudio drains the buffer at its own pace)."""
        if not pcm:
            return
        converted = expand_channels(self._resampler.process(pcm), self._channels)
        if not converted:
            return
        with self._lock:
            self._pending.append((self._generation, converted, 0))

    # --- barge-in --------------------------------------------------------

    @property
    def has_unplayed_audio(self) -> bool:
        """Is there still audio on its way to the room?

        Covers the buffer we hold AND the device's own: the model delivers a
        long reply in a moment, so the server is finished (`response.done`) long
        before the rabbit stops talking. Judging playback by the server's state
        was the bug that made barge-in silently do nothing.
        """
        with self._lock:
            if self._pending:
                return True
            last = self._last_consumed_at
        if last is None:
            return False
        buffered_s = (self._latency_s * 1000 or _ASSUMED_DEVICE_BUFFER_MS) / 1000.0
        return (time.monotonic() - last) < buffered_s

    @property
    def played_ms(self) -> int:
        """Milliseconds of THIS response actually handed to the device.

        A real count now (the callback tallies what it copied), less the
        device's own buffer, which is still sound in the pipeline rather than in
        the room. The counter is zeroed per response by `reset_position`, and
        the callback only counts frames of the CURRENT generation, so audio from
        an interrupted reply can never inflate the next one's figure — which is
        what produced truncate values longer than the item itself.
        """
        if self._rate <= 0:
            return 0
        with self._lock:
            frames = self._consumed_frames
        delivered_ms = frames * 1000 / self._rate
        return max(0, int(delivered_ms - self._latency_s * 1000))

    def reset_position(self) -> None:
        """Begin a new response: new generation, empty buffer, zeroed counter —
        all atomically, so a straggling chunk cannot be credited to it."""
        with self._lock:
            self._generation += 1
            self._pending.clear()
            self._consumed_frames = 0
            self._last_consumed_at = None
            # Deliberately NOT touching _playing: a new item inside the same
            # reply is not the rabbit falling silent, and flapping the speaking
            # indicator at every item boundary is exactly what the debounce in
            # _note_fill exists to prevent.
            self._dry_since = None
        self._resampler = Resampler(self._source_rate, self._rate)

    async def stop(self) -> None:
        """Cut playback NOW (the user is talking).

        No `abort()` is needed: dropping the buffer means the next callback —
        within one block — outputs silence, and the stream stays healthy for the
        next reply instead of being torn down and rebuilt.
        """
        with self._lock:
            played = self._consumed_frames * 1000 / self._rate if self._rate else 0
            discarded = len(self._pending)
            self._generation += 1
            self._pending.clear()
            # The buffer is gone AND the device is about to run dry, so nothing
            # is still on its way to the room — say so at once, or the very
            # next barge-in check would think the rabbit is still talking.
            self._last_consumed_at = None
            was_playing = self._playing
            self._playing = False
            self._dry_since = None
        log.info(
            "playback drain: cut after %d ms played, %d queued chunks discarded",
            int(played),
            discarded,
        )
        if was_playing:
            # The UX layer must react to a barge-in NOW, not when the (already
            # emptied) buffer would have been noticed as dry.
            self._emit(self._on_cut)

    async def resume(self) -> None:
        """Nothing to re-open: the stream was never torn down. Kept so the
        session code reads the same for either backend."""
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.start()
