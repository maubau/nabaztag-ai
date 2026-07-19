"""Voice pipeline: capture → wake word → DoA reflex → VAD → streaming STT.

Wiring (docs/ARCHITECTURE.md §6.2):
  - half-duplex gate (§6.2.7): while the rabbit is (estimated) speaking, mic
    frames are discarded and the wake detector held in reset — there is no AEC
    reference for the rabbit speaker, so we must not hear ourselves;
  - on wake (§6.2.8): interrupt() so the rabbit snaps to attention, then a
    small LED/ear state machine, ALL choreography-only (the posleft/posright
    path triggers a firmware jingle — probe #7, hardware-confirmed):
      * a SINGLE non-blocking wake-ack chor (white flash + 72° twitch on the
        DoA side → listening pose), fired as a background task so VAD/STT
        never wait for the DoA read or the OJN round-trip. One ChorCommand,
        never two same-priority EarsCommand (the BodyController coalesces
        those and silently drops the DoA bias — UX finding). DoA is
        fail-open: None just means an un-sided ack;
      * a persistent LISTENING indicator (cyan LED scanner) that loops for
        the whole VAD recording and is turned off in finally;
      * a PROCESSING indicator (pulsing orange) while the transcript is
        handled by on_transcript (the agent loop, §6.2.5).
    All body output goes through BodyController.submit at DOA_REFLEX priority,
    never the adapter;
  - the utterance streams into the STT provider while still being spoken
    (VAD chunks go out as they pass the recorder), so cloud endpointing
    overlaps with capture.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..body.chor import (
    LISTENING_SCANNER_CYCLE_S,
    PROCESSING_PULSE_CYCLE_S,
    build_leds_off_chor,
    build_listening_scanner_chor,
    build_processing_chor,
    build_wake_ack_chor,
)
from ..body.controller import BodyController
from ..body.types import ChorCommand, Priority
from ..stt.base import STTProvider, STTResult
from .capture import MicCapture
from .doa import FailOpenDoa
from .vad import SpeechProbe, UtteranceRecorder
from .wake import WakeDetector

log = logging.getLogger(__name__)

DEFAULT_WAKE_THRESHOLD = 0.5

_CLOSE = object()  # sentinel ending the STT chunk stream


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
    ):
        self._capture = capture
        self._wake = wake
        self._probe_factory = probe_factory
        self._stt = stt
        self._controller = controller
        self._on_transcript = on_transcript
        self._doa = doa
        self._doa_moods = doa_moods or {}
        self._wake_threshold = wake_threshold
        self._recorder_kwargs = recorder_kwargs or {}
        self._ack_tasks: set[asyncio.Task] = set()
        self._indicator_tasks: set[asyncio.Task] = set()

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
        """Cancel and collect every background task (wake acks + indicators)."""
        tasks = list(self._ack_tasks | self._indicator_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_wake(self, frames: AsyncIterator[bytes]) -> None:
        self._controller.interrupt()
        # fire-and-forget: listening must start NOW, not after the DoA USB
        # read and the OJN round-trip
        ack = asyncio.create_task(self._wake_ack())
        self._ack_tasks.add(ack)
        ack.add_done_callback(self._ack_tasks.discard)
        # persistent LISTENING indicator for the whole recording window
        listening = self._spawn_indicator(build_listening_scanner_chor(), LISTENING_SCANNER_CYCLE_S)
        try:
            result = await self._record_and_transcribe(frames)
        finally:
            await self._stop_indicator(listening)
            await self._submit_chor(build_leds_off_chor())
        if result is not None and result.text:
            log.info("transcript (%s): %s", result.provider, result.text)
            await self._process_transcript(result.text)

    async def _process_transcript(self, text: str) -> None:
        # PROCESSING indicator while the agent loop handles the utterance
        processing = self._spawn_indicator(build_processing_chor(), PROCESSING_PULSE_CYCLE_S)
        try:
            await self._on_transcript(text)
        finally:
            await self._stop_indicator(processing)
            await self._submit_chor(build_leds_off_chor())

    # --- body indicators (all via BodyController, choreography-only) ------

    async def _submit_chor(self, chor: str) -> None:
        await self._controller.submit(ChorCommand(chor), Priority.DOA_REFLEX)

    def _spawn_indicator(self, chor: str, cycle_s: float) -> asyncio.Task:
        """A looping indicator that resubmits `chor` every cycle_s until stopped."""

        async def loop() -> None:
            while True:
                await self._submit_chor(chor)
                await asyncio.sleep(cycle_s)

        task = asyncio.create_task(loop())
        self._indicator_tasks.add(task)
        task.add_done_callback(self._indicator_tasks.discard)
        return task

    async def _stop_indicator(self, task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _side_of(angle_deg: int) -> str | None:
        """Which ear to twitch for a DoA angle (0° = front, clockwise)."""
        angle = angle_deg % 360
        if 45 <= angle < 135:
            return "right"
        if 225 <= angle < 315:
            return "left"
        return None  # front or behind: twitch both

    async def _wake_ack(self) -> None:
        side = None
        if self._doa is not None:
            reading = await self._doa.read()  # fail-open: None on any error
            if reading is not None:
                side = self._side_of(reading.angle_deg)
        listen = self._doa_moods.get("listen_pose", {})
        pose = (listen.get("left", 0), listen.get("right", 0))
        chor = build_wake_ack_chor(side, listen_pose=pose)
        await self._controller.submit(ChorCommand(chor), Priority.DOA_REFLEX)

    async def _record_and_transcribe(self, frames: AsyncIterator[bytes]) -> STTResult | None:
        recorder = UtteranceRecorder(
            self._probe_factory(), sample_rate=self._capture.sample_rate, **self._recorder_kwargs
        )
        queue: asyncio.Queue[bytes | object] = asyncio.Queue()

        async def chunk_stream() -> AsyncIterator[bytes]:
            while True:
                item = await queue.get()
                if item is _CLOSE:
                    return
                assert isinstance(item, bytes)
                yield item

        stt_task = asyncio.create_task(
            self._stt.transcribe(chunk_stream(), self._capture.sample_rate)
        )
        try:
            while True:
                try:
                    frame = await anext(frames)
                except StopAsyncIteration:
                    break
                emit, done = recorder.push(frame)
                for chunk in emit:
                    queue.put_nowait(chunk)
                if done:
                    break
            queue.put_nowait(_CLOSE)
            if not recorder.got_speech:
                stt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stt_task
                return None
            return await stt_task
        except Exception:
            stt_task.cancel()
            log.exception("utterance transcription failed")
            return None
