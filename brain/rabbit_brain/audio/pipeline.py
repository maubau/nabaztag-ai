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
# Client-side ceiling on a provider-endpointed turn (Flux): if EndOfTurn never
# arrives — silence after the wake word, or a wedged socket — abandon the turn
# and re-arm cleanly rather than holding the pipeline open (latency Gate L1).
DEFAULT_TURN_TIMEOUT_S = 15.0
# How long the PROCESSING indicator may wait for playback to actually begin
# after the reply was queued (OJN round-trip). Bounded so a lost/failed
# playback can never leave the LEDs pulsing.
PLAYBACK_START_TIMEOUT_S = 5.0
# Safety ceiling on the PLAYING drain: a runaway reply must not wedge the
# pipeline forever if audio_busy ever gets stuck (hardware round, July 2026).
PLAYING_DRAIN_TIMEOUT_S = 30.0
# Bounded catch-up drain right before REARMED, in case anything is still
# sitting in the queue at the PLAYING->REARMED boundary (scheduling jitter).
# Small on purpose: on a live mic a frame is basically always "ready", so this
# window is a deliberate fixed latency cost, not a backlog probe.
FLUSH_TIMEOUT_S = 0.08

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
    # --- provider-side endpointing (Flux; None on the nova-3+Silero path) ---
    # when Flux said the turn ended, and (diagnostics only, never acted on
    # this round) when it first speculated it had
    end_of_turn: float | None = None
    eager_end_of_turn: float | None = None
    # when the transcript was handed to the agent — closes the gap between
    # "provider said the turn ended" and "the LLM request goes out"
    llm_dispatched: float | None = None
    # when we began feeding audio to the provider, so its own audio cursor
    # can be placed on our wall clock
    stream_start: float | None = None
    # the provider's OWN audio cursor at end of speech (seconds into the
    # stream): stream_start + audio_cursor_s ≈ the instant the user really
    # stopped talking, which is what Gate L1's headline metric measures from.
    audio_cursor_s: float | None = None
    end_of_turn_confidence: float | None = None
    language: str | None = None

    def as_dict(self) -> dict[str, int | float | str | None]:
        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - self.wake) * 1000)

        def span(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        # The real Gate L1 number: from the user actually falling silent (the
        # provider's own audio cursor, not our VAD's opinion) to EndOfTurn.
        speech_end = (
            None
            if self.stream_start is None or self.audio_cursor_s is None
            else self.stream_start + self.audio_cursor_s
        )
        return {
            "wake_to_speech_ms": ms(self.speech_start),
            "wake_to_eos_ms": ms(self.end_of_speech),
            "wake_to_stt_final_ms": ms(self.stt_final),
            "wake_to_scanner_stop_enqueued_ms": ms(self.scanner_stop_enqueued),
            "stt_endpointing_ms": span(self.end_of_speech, self.stt_final),
            "wake_to_end_of_turn_ms": ms(self.end_of_turn),
            "speech_end_to_end_of_turn_ms": span(speech_end, self.end_of_turn),
            "eager_to_end_of_turn_ms": span(self.eager_end_of_turn, self.end_of_turn),
            "end_of_turn_to_llm_ms": span(self.end_of_turn, self.llm_dispatched),
            "last_speech_audio_cursor_s": self.audio_cursor_s,
            "end_of_turn_confidence": self.end_of_turn_confidence,
            "language": self.language,
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
        turn_timeout_s: float = DEFAULT_TURN_TIMEOUT_S,
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
        self._turn_timeout_s = turn_timeout_s

        self._feedback_tasks: set[asyncio.Task] = set()
        self.doa_reads = 0  # diagnostics/tests: periodic DoA reads during LISTENING
        self.last_timings: WakeTimings | None = None  # last wake cycle, for diagnostics
        self.last_doa_deg: int | None = None  # last DoA angle, for get_direction() (§6.3)
        self.last_stt_language: str | None = None  # detected language of the last utterance
        # Session-cumulative frame consumed/discarded counts per pipeline
        # state, for hardware diagnostics (§6.2.7 capture-draining round).
        self.frame_counts: dict[str, int] = {}

    def _count(self, state: str, n: int) -> None:
        if n:
            self.frame_counts[state] = self.frame_counts.get(state, 0) + n

    # --- half-duplex gate (§6.2.7) --------------------------------------

    def _gated(self) -> bool:
        """True while the rabbit is speaking OR has audio queued (playback
        timer incl. guard). Uses BodyController.audio_busy when available so
        the gate also covers audio accepted by submit() but not yet started —
        current_playback alone misses that window (hardware finding)."""
        busy = getattr(self._controller, "audio_busy", None)
        if busy is not None:
            return bool(busy)
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
                    self._count("idle_discarded", 1)
                    continue
                if self._wake.feed(frame) >= self._wake_threshold:
                    log.info("wake word detected")
                    await self._handle_wake(frames)
                    # _handle_wake owns the reset: it must happen AFTER the
                    # PLAYING drain + flush, not right when it returns to us
                    # (which is the same instant, but the ordering is the
                    # point — see the REARMED transition below).
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
        log.info("pipeline state -> LISTENING")
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
        playing_dropped = 0
        if result is not None and result.text:
            self.last_stt_language = getattr(result, "language", None)
            timings.language = self.last_stt_language
            timings.llm_dispatched = time.monotonic()  # EndOfTurn → LLM request
            # ALSA must keep being consumed for the ENTIRE processing+playback
            # window, or the capture queue backs up ("capture queue full") and
            # stale/echoed audio can fake a second wake (hardware round, July
            # 2026: draining only during on_transcript wasn't enough — the
            # capture blocked again during/after playback). One continuous
            # drain chain, no seams: PROCESSING (LLM+TTS synth) -> PLAYING
            # (queued/playing audio incl. the half-duplex guard) -> a bounded
            # FLUSH -> REARMED (wake detector reset).
            log.info("pipeline state -> PROCESSING")
            done = asyncio.Event()

            async def process() -> None:
                try:
                    await self._process_transcript(result.text)
                finally:
                    done.set()

            proc = asyncio.create_task(process())
            processing_dropped = await self._drain_frames(frames, done, state="processing")
            await proc

            log.info("pipeline state -> PLAYING (processing discarded=%d)", processing_dropped)
            playing_dropped = await self._drain_while_gated(frames, state="playing")

        flushed = await self._flush_residual(frames)
        self._wake.reset()
        log.info(
            "pipeline state -> REARMED (playing discarded=%d, flushed=%d)",
            playing_dropped,
            flushed,
        )
        self.last_timings = timings
        log.info("wake cycle timings: %s", timings.as_dict())

    async def _drain_frames(
        self, frames: AsyncIterator[bytes], until: asyncio.Event, state: str
    ) -> int:
        """Consume and discard capture frames until `until` fires (returns the
        count). Keeps the ALSA queue empty while something else has the floor."""
        dropped = 0
        while not until.is_set():
            try:
                await anext(frames)
            except StopAsyncIteration:
                break
            dropped += 1
        self._count(state, dropped)
        return dropped

    async def _drain_while_gated(
        self, frames: AsyncIterator[bytes], state: str, timeout_s: float = PLAYING_DRAIN_TIMEOUT_S
    ) -> int:
        """Consume and discard frames while the half-duplex gate holds (queued
        audio + current playback + guard — BodyController.audio_busy). Bounded
        so a stuck audio_busy can never wedge the pipeline forever."""
        dropped = 0
        deadline = time.monotonic() + timeout_s
        while self._gated():
            if time.monotonic() > deadline:
                log.warning("%s drain exceeded %.0fs, forcing rearm", state, timeout_s)
                break
            try:
                await anext(frames)
            except StopAsyncIteration:
                break
            dropped += 1
        self._count(state, dropped)
        return dropped

    async def _flush_residual(
        self, frames: AsyncIterator[bytes], timeout_s: float = FLUSH_TIMEOUT_S
    ) -> int:
        """Explicit bounded catch-up drain right before REARMED: whatever is
        already sitting in the capture queue at this instant gets discarded
        too, so no stale block survives into the next wake-listening window.

        Deliberately does NOT wrap anext() in asyncio.wait_for: cancelling an
        in-flight anext() on timeout throws CancelledError into the capture
        async generator, which — since it doesn't catch it — closes the
        generator for good (any later anext() on it then raises
        StopAsyncIteration immediately, silently ending the whole pipeline).
        The deadline is only checked BETWEEN pulls.
        """
        dropped = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                await anext(frames)
            except StopAsyncIteration:
                break
            dropped += 1
        self._count("flush", dropped)
        return dropped

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
        if reading is None:
            return None
        self.last_doa_deg = reading.angle_deg % 360  # for get_direction() (§6.3)
        return self._side_of(reading.angle_deg)

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
        """Run the agent turn, optionally showing the PROCESSING indicator.

        The indicator covers the whole "thinking" gap the user would otherwise
        experience as dead air — from end-of-turn until the rabbit ACTUALLY
        starts speaking, not merely until the audio was queued. on_transcript
        returns once the MP3 is submitted; playback only begins after the OJN
        round-trip, so we wait (bounded) for the controller to report it.
        """
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
            await self._wait_for_playback_start()
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _wait_for_playback_start(self) -> None:
        """Bounded wait until the reply is actually playing. Returns at once if
        nothing was queued (a silent turn) — the indicator must not linger."""
        if not self._gated():
            return  # nothing queued or playing: nothing to wait for
        deadline = time.monotonic() + PLAYBACK_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._controller.current_playback is not None:
                return
            await asyncio.sleep(0.02)

    # --- capture → VAD → STT (independent of the feedback task) ----------

    def _open_chunk_stream(self) -> tuple[asyncio.Queue, AsyncIterator[bytes]]:
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()

        async def chunk_stream() -> AsyncIterator[bytes]:
            while True:
                item = await queue.get()
                if item is _CLOSE:
                    return
                assert isinstance(item, bytes)
                yield item

        return queue, chunk_stream()

    async def _record_and_transcribe(
        self, frames: AsyncIterator[bytes], end_of_speech: asyncio.Event, timings: WakeTimings
    ) -> STTResult | None:
        """Dispatch on WHO decides the turn ended (stt/base.py):
        provider-side (Flux) keeps the stream open until the provider says so;
        client-side (nova-3 + Silero) closes it from the local VAD."""
        if getattr(self._stt, "detects_end_of_turn", False):
            return await self._record_provider_endpointed(frames, end_of_speech, timings)
        return await self._record_vad_endpointed(frames, end_of_speech, timings)

    async def _record_provider_endpointed(
        self, frames: AsyncIterator[bytes], end_of_speech: asyncio.Event, timings: WakeTimings
    ) -> STTResult | None:
        """Deepgram Flux: no local silence window at all. Frames keep flowing
        until the PROVIDER reports EndOfTurn; that callback stops the LISTENING
        feedback immediately, so the LEDs track the real end of speech instead
        of a 1600 ms local timeout (latency Gate L1)."""
        sr = self._capture.sample_rate
        queue, chunk_stream = self._open_chunk_stream()

        def on_end_of_turn(_text: str) -> None:
            now = time.monotonic()
            timings.end_of_turn = now
            timings.end_of_speech = now  # same instant on this path
            end_of_speech.set()  # stop LISTENING right here, before the await

        stt_task = asyncio.create_task(
            self._stt.transcribe(chunk_stream, sr, on_end_of_turn=on_end_of_turn)
        )
        timings.stream_start = time.monotonic()
        deadline = timings.stream_start + self._turn_timeout_s
        try:
            while not stt_task.done():
                if time.monotonic() > deadline:
                    log.warning(
                        "no EndOfTurn within %.0fs, abandoning the turn", self._turn_timeout_s
                    )
                    stt_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stt_task
                    return None
                try:
                    frame = await anext(frames)
                except StopAsyncIteration:
                    break
                self._count("listening", 1)
                # half-duplex (§6.2.7) unchanged: never feed the provider audio
                # of the rabbit's own voice.
                if self._gated():
                    continue
                queue.put_nowait(frame)
            queue.put_nowait(_CLOSE)
            result = await stt_task
            timings.stt_final = time.monotonic()
            if timings.end_of_turn is None:  # closed without EndOfTurn
                timings.end_of_turn = timings.stt_final
                timings.end_of_speech = timings.stt_final
            timings.audio_cursor_s = getattr(result, "audio_cursor_s", None)
            timings.end_of_turn_confidence = getattr(result, "end_of_turn_confidence", None)
            timings.eager_end_of_turn = self._eager_end_of_turn_at()
            return result
        except Exception:
            stt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stt_task
            log.exception("utterance transcription failed")
            return None
        finally:
            end_of_speech.set()

    def _eager_end_of_turn_at(self) -> float | None:
        """EagerEndOfTurn timestamp if the provider exposes one (diagnostics
        only this round — nothing speculative is dispatched on it)."""
        at = getattr(self._stt, "last_eager_end_of_turn_at", None)
        return at if isinstance(at, int | float) else None

    async def _record_vad_endpointed(
        self, frames: AsyncIterator[bytes], end_of_speech: asyncio.Event, timings: WakeTimings
    ) -> STTResult | None:
        sr = self._capture.sample_rate
        recorder = UtteranceRecorder(self._probe_factory(), sample_rate=sr, **self._recorder_kwargs)
        queue, chunk_stream = self._open_chunk_stream()

        stt_task = asyncio.create_task(self._stt.transcribe(chunk_stream, sr))
        try:
            while True:
                try:
                    frame = await anext(frames)
                except StopAsyncIteration:
                    break
                self._count("listening", 1)
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
