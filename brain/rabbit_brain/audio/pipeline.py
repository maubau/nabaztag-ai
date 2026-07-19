"""Voice pipeline: capture → wake word → DoA reflex → VAD → streaming STT.

Wiring (docs/ARCHITECTURE.md §6.2):
  - half-duplex gate (§6.2.7): while the rabbit is (estimated) speaking, mic
    frames are discarded and the wake detector held in reset — there is no AEC
    reference for the rabbit speaker, so we must not hear ourselves;
  - on wake (§6.2.8): interrupt() so the rabbit snaps to attention, then a
    SINGLE sequential feedback task runs the body, ALL choreography-only (the
    posleft/posright path triggers a firmware jingle — probe #7, confirmed):
        wake ack (green LEDs + both ears forward) → a
        LISTENING loop (all-LED magenta pulse + counter-rotating ears,
        re-reading DoA once per ~1.9 s cycle). Sequential so the ack always renders
        before the scanner can replace it. VAD/STT run in a SEPARATE task and
        start immediately — they never wait for the DoA read or OJN;
  - the LISTENING feedback stops at the VAD end-of-speech event, BEFORE the
    STT result is awaited (so the LEDs are lit only while the user speaks, not
    through Deepgram endpointing/network). PROCESSING (pulsing orange) is
    opt-in for the same reason;
  - an optional short wake beep played ON THE RABBIT (§6.2, off by default):
    it rides the audio lane, so the half-duplex gate suppresses the mic while
    it sounds (no AEC) and it cannot be transcribed. Verify on hardware that
    Silero does not classify the residual as speech before enabling;
  - the utterance streams into the STT provider while still being spoken, so
    cloud endpointing overlaps with capture. WakeTimings records wake,
    speech-start, end-of-speech, STT-final and scanner-stop for diagnostics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..body.chor import (
    LISTENING_CYCLE_S,
    PROCESSING_PULSE_CYCLE_S,
    build_leds_off_chor,
    build_listening_chor,
    build_processing_chor,
    build_wake_ack_chor,
)
from ..body.controller import BodyController
from ..body.types import ChorCommand, PlayAudioCommand, Priority
from ..stt.base import STTProvider, STTResult
from .capture import MicCapture
from .doa import FailOpenDoa
from .vad import SpeechProbe, UtteranceRecorder
from .wake import WakeDetector

log = logging.getLogger(__name__)

DEFAULT_WAKE_THRESHOLD = 0.5
# Let the wake ack render before the LISTENING scanner can replace it on the
# wire (chor-interrupts-chor, probe #8). Roughly the ack's own duration.
WAKE_ACK_RENDER_S = 0.5
DEFAULT_DOA_TIMEOUT_S = 0.5  # a stalled DoA read must not freeze the feedback

_CLOSE = object()  # sentinel ending the STT chunk stream


@dataclass
class WakeTimings:
    """Monotonic diagnostics for one wake→listen→transcribe cycle. Separates
    VAD delay, Deepgram endpointing, and the feedback-stop reaction.

    scanner_stop_enqueued is when the LISTENING feedback SUBMITTED the LEDs-off
    chor (right at end-of-speech), NOT when it reached the rabbit:
    BodyController.submit only enqueues. Wire-execution latency (controller
    queue + HTTP) is a separate measurement that needs a controller ack —
    OJN_API_NOTES probe #8.
    """

    wake: float
    speech_start: float | None = None
    end_of_speech: float | None = None
    stt_final: float | None = None
    scanner_stop_enqueued: float | None = None

    def as_dict(self) -> dict[str, int | None]:
        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - self.wake) * 1000)

        endpointing = (
            None
            if self.stt_final is None or self.end_of_speech is None
            else round((self.stt_final - self.end_of_speech) * 1000)
        )
        return {
            "wake_to_speech_ms": ms(self.speech_start),
            "wake_to_eos_ms": ms(self.end_of_speech),
            "wake_to_stt_final_ms": ms(self.stt_final),
            "wake_to_scanner_stop_enqueued_ms": ms(self.scanner_stop_enqueued),
            "stt_endpointing_ms": endpointing,
        }


class VoicePipeline:
    def __init__(
        self,
        capture: MicCapture,
        wake: WakeDetector,
        probe_factory: Callable[[], SpeechProbe],
        stt: STTProvider,
        controller: BodyController,
        on_transcript: Callable[[str], Awaitable[None]],
        doa: FailOpenDoa | None = None,
        doa_moods: dict[str, Any] | None = None,
        wake_threshold: float = DEFAULT_WAKE_THRESHOLD,
        recorder_kwargs: dict[str, Any] | None = None,
        wake_beep: PlayAudioCommand | None = None,
        processing_indicator: bool = False,
        ack_render_s: float = WAKE_ACK_RENDER_S,
        listening_cycle_s: float = LISTENING_CYCLE_S,
        doa_timeout_s: float = DEFAULT_DOA_TIMEOUT_S,
    ):
        self._capture = capture
        self._wake = wake
        self._probe_factory = probe_factory
        self._stt = stt
        self._controller = controller
        self._on_transcript = on_transcript
        self._doa = doa
        self._doa_moods = doa_moods or {}
        listen = self._doa_moods.get("listen_pose", {})
        self._listen_pose = (listen.get("left", 0), listen.get("right", 0))
        self._wake_threshold = wake_threshold
        self._recorder_kwargs = recorder_kwargs or {}
        # A short confirmation sound played ON THE RABBIT (the user must hear it
        # from the Nabaztag, not the Bolt). It rides the audio lane like any
        # playback, so the half-duplex gate below suppresses the mic while it
        # sounds — the guard is the real playback timer, not a fixed offset.
        self._wake_beep = wake_beep
        self._processing_indicator = processing_indicator
        self._ack_render_s = ack_render_s
        self._listening_cycle_s = listening_cycle_s
        self._doa_timeout_s = doa_timeout_s

        self._feedback_tasks: set[asyncio.Task] = set()
        self.doa_reads = 0  # diagnostics/tests: periodic DoA reads during LISTENING
        self.last_timings: WakeTimings | None = None  # last wake cycle, for diagnostics

    # --- half-duplex gate (§6.2.7) --------------------------------------

    def _gated(self) -> bool:
        """True while the rabbit is speaking (playback timer incl. guard)."""
        handle = self._controller.current_playback
        return handle is not None and not getattr(handle, "finished", False)

    # --- main loop ------------------------------------------------------

    async def run(self) -> None:
        try:
            frames = aiter(self._capture.frames())
            while True:
                try:
                    frame = await anext(frames)
                except StopAsyncIteration:
                    return
                if self._gated():
                    self._wake.reset()
                    continue
                if self._wake.feed(frame) >= self._wake_threshold:
                    log.info("wake word detected")
                    await self._handle_wake(frames)
                    self._wake.reset()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Cancel and collect every background task (feedback loops)."""
        tasks = list(self._feedback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_wake(self, frames: AsyncIterator[bytes]) -> None:
        timings = WakeTimings(wake=time.monotonic())
        self._controller.interrupt()
        if self._wake_beep is not None:
            # play on the rabbit at USER_SPEECH_SYNC so interrupt() won't drop
            # it; the half-duplex gate in the record loop keeps it out of STT
            await self._controller.submit(self._wake_beep, Priority.USER_SPEECH_SYNC)
        # one sequential feedback task (ack → LISTENING); VAD/STT below run
        # concurrently and start immediately
        end_of_speech = asyncio.Event()
        feedback = self._spawn(
            self._listening_feedback(end_of_speech, timings), self._feedback_tasks
        )
        try:
            result = await self._record_and_transcribe(frames, end_of_speech, timings)
        finally:
            end_of_speech.set()  # ensure the feedback stops on any exit path
            with contextlib.suppress(asyncio.CancelledError):
                await feedback  # records scanner_stop_enqueued in its own finally
        if result is not None and result.text:
            await self._process_transcript(result.text)
        self.last_timings = timings
        log.info("wake cycle timings: %s", timings.as_dict())

    # --- body feedback (single sequential task, choreography-only) ------

    async def _bounded_read(self) -> str | None:
        """One DoA read, fail-open and time-bounded (a USB stall must not
        freeze the feedback)."""
        if self._doa is None:
            return None
        self.doa_reads += 1
        try:
            async with asyncio.timeout(self._doa_timeout_s):
                reading = await self._doa.read()
        except Exception:
            return None
        return None if reading is None else self._side_of(reading.angle_deg)

    async def _read_side(self, end_of_speech: asyncio.Event) -> str | None:
        """DoA read that returns early (None) the moment end-of-speech fires,
        so a slow read (up to doa_timeout_s) can't cause a post-EOS scanner."""
        read = asyncio.ensure_future(self._bounded_read())
        eos = asyncio.ensure_future(end_of_speech.wait())
        try:
            await asyncio.wait({read, eos}, return_when=asyncio.FIRST_COMPLETED)
            return read.result() if read.done() and not read.cancelled() else None
        finally:
            read.cancel()
            eos.cancel()
            await asyncio.gather(read, eos, return_exceptions=True)

    @staticmethod
    def _side_of(angle_deg: int) -> str | None:
        """Which ear to move for a DoA angle (0° = front, clockwise)."""
        angle = angle_deg % 360
        if 45 <= angle < 135:
            return "right"
        if 225 <= angle < 315:
            return "left"
        return None  # front or behind: both

    async def _listening_feedback(self, end_of_speech: asyncio.Event, timings: WakeTimings) -> None:
        try:
            if end_of_speech.is_set():
                return
            # Immediate and deterministic: the green wake acknowledgement must
            # never wait for the comparatively slow USB DoA control transfer.
            await self._submit_chor(build_wake_ack_chor(None, listen_pose=self._listen_pose))
            # let the ack render before the scanner can replace it (probe #8)
            await self._wait_or_end(end_of_speech, self._ack_render_s)
            while not end_of_speech.is_set():
                side = await self._read_side(end_of_speech)  # periodic, ~1/s, EOS-cancellable
                if end_of_speech.is_set():  # EOS during the read → no post-EOS scanner
                    break
                await self._submit_chor(build_listening_chor(side, listen_pose=self._listen_pose))
                await self._wait_or_end(end_of_speech, self._listening_cycle_s)
        finally:
            # stop LISTENING at EOS: LEDs off and both ears back to their pose
            await self._submit_chor(build_leds_off_chor(ears_pose=self._listen_pose))
            # when the stop was ENQUEUED (≈ end-of-speech); wire execution is
            # a separate, controller-level measurement (probe #8)
            timings.scanner_stop_enqueued = time.monotonic()

    async def _process_transcript(self, text: str) -> None:
        if not self._processing_indicator:
            await self._on_transcript(text)
            return
        stop = asyncio.Event()

        async def pulse() -> None:
            try:
                while True:
                    await self._submit_chor(build_processing_chor())
                    if stop.is_set():
                        break
                    await self._wait_or_end(stop, PROCESSING_PULSE_CYCLE_S)
            finally:
                await self._submit_chor(build_leds_off_chor())

        task = self._spawn(pulse(), self._feedback_tasks)
        try:
            await self._on_transcript(text)
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- capture → VAD → STT (independent of the feedback task) ----------

    async def _record_and_transcribe(
        self, frames: AsyncIterator[bytes], end_of_speech: asyncio.Event, timings: WakeTimings
    ) -> STTResult | None:
        sr = self._capture.sample_rate
        recorder = UtteranceRecorder(self._probe_factory(), sample_rate=sr, **self._recorder_kwargs)
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()

        async def chunk_stream() -> AsyncIterator[bytes]:
            while True:
                item = await queue.get()
                if item is _CLOSE:
                    return
                assert isinstance(item, bytes)
                yield item

        stt_task = asyncio.create_task(self._stt.transcribe(chunk_stream(), sr))
        try:
            while True:
                try:
                    frame = await anext(frames)
                except StopAsyncIteration:
                    break
                # half-duplex (§6.2.7): while the rabbit is playing — the wake
                # beep or a prior TTS reply — drop mic frames so we don't hear
                # ourselves (no AEC). Ties the beep's guard to the real playback
                # timer, not a fixed offset from the wake instant.
                if self._gated():
                    continue
                emit, done = recorder.push(frame)
                if emit and timings.speech_start is None:
                    timings.speech_start = time.monotonic()
                for chunk in emit:
                    queue.put_nowait(chunk)
                if done:
                    break
            timings.end_of_speech = time.monotonic()
            end_of_speech.set()  # stop LISTENING now, before STT endpointing/network
            queue.put_nowait(_CLOSE)
            if not recorder.got_speech:
                stt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stt_task
                return None
            result = await stt_task
            timings.stt_final = time.monotonic()
            return result
        except Exception:
            stt_task.cancel()
            log.exception("utterance transcription failed")
            return None
        finally:
            end_of_speech.set()

    # --- helpers ---------------------------------------------------------

    async def _submit_chor(self, chor: str) -> None:
        await self._controller.submit(ChorCommand(chor), Priority.DOA_REFLEX)

    async def _wait_or_end(self, event: asyncio.Event, seconds: float) -> None:
        """Return after `seconds` or as soon as `event` is set, whichever first."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), seconds)

    def _spawn(self, coro: Awaitable[None], taskset: set[asyncio.Task]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        taskset.add(task)
        task.add_done_callback(taskset.discard)
        return task
