"""BodyController — single owner of the body, mediates all access (§6.4).

Every caller (agent loop, DoA reflex, RFID handlers, MCP server, idle behavior)
goes through submit(); none touch the BodyAdapter directly.

Responsibilities implemented here:
  - serialization: motion commands execute one at a time; audio is a separate
    lane so gestures can run while the rabbit speaks (the adapter additionally
    serializes at the HTTP level);
  - priority: higher priority executes first; interrupt() drops lower-priority
    pending work ("snap to attention");
  - coalescing: rapid successive ears/LED targets at the same priority collapse
    to the latest;
  - deadlines: commands past their deadline are dropped, never fired late;
  - no-cancel degradation: if capabilities.can_cancel_audio is false, a
    preemption lets the current utterance finish and only drops queued ones;
  - state model: tracks ears/LEDs and suppresses redundant no-op commands.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass, replace

from .adapter import BodyAdapter, PlaybackHandle
from .types import (
    COALESCABLE,
    BodyCommand,
    BodyState,
    ChorCommand,
    EarsCommand,
    LedsCommand,
    PlayAudioCommand,
    Priority,
    SayCommand,
    SleepCommand,
    WakeCommand,
)

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    priority: Priority
    seq: int
    cmd: BodyCommand
    deadline: float | None

    @property
    def sort_key(self) -> tuple[int, int]:
        return (-self.priority, self.seq)

    def __lt__(self, other: _Entry) -> bool:
        return self.sort_key < other.sort_key


class BodyController:
    def __init__(self, adapter: BodyAdapter):
        self._adapter = adapter
        self._state = BodyState()
        self._seq = itertools.count()

        self._motion_heap: list[_Entry] = []
        self._motion_ready = asyncio.Event()
        self._audio_pending: list[_Entry] = []
        self._audio_ready = asyncio.Event()

        self._dropped: set[int] = set()
        # (cmd type, priority) -> newest seq, for coalescing
        self._latest: dict[tuple[type, Priority], int] = {}

        self._current_playback: PlaybackHandle | None = None
        self._outstanding = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: set[asyncio.Task] = set()

    # --- public API ------------------------------------------------------

    async def submit(
        self, cmd: BodyCommand, priority: Priority, deadline: float | None = None
    ) -> None:
        """Queue a command. `deadline` is time.monotonic()-based; commands that
        cannot execute before it are dropped rather than fired late."""
        entry = _Entry(priority, next(self._seq), cmd, deadline)
        if isinstance(cmd, COALESCABLE):
            self._latest[(type(cmd), priority)] = entry.seq
        self._outstanding += 1
        self._idle.clear()
        if isinstance(cmd, PlayAudioCommand | SayCommand):
            heapq.heappush(self._audio_pending, entry)
            self._audio_ready.set()
        else:
            heapq.heappush(self._motion_heap, entry)
            self._motion_ready.set()

    def interrupt(self, below: Priority = Priority.USER_SPEECH_SYNC) -> None:
        """Drop all pending work below `below` (e.g. on wake word, so the rabbit
        snaps to attention). Cancels running audio only if the body can honor
        it; otherwise the current utterance finishes and queued ones are dropped."""
        for heap in (self._motion_heap, self._audio_pending):
            for entry in heap:
                if entry.priority < below and entry.seq not in self._dropped:
                    self._dropped.add(entry.seq)
                    self._finish_one()
        if self._current_playback is not None and self._adapter.capabilities.can_cancel_audio:
            task = asyncio.ensure_future(self._current_playback.cancel())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def snapshot(self) -> BodyState:
        return replace(self._state, leds=dict(self._state.leds))

    async def wait_idle(self) -> None:
        """Wait until every submitted command has been executed or dropped
        (audio counts as executed once sent; playback may still be running)."""
        await self._idle.wait()

    async def run(self) -> None:
        """Single consumer of the priority queues, draining into the adapter."""
        try:
            await asyncio.gather(self._motion_loop(), self._audio_loop())
        finally:
            for t in self._tasks:
                t.cancel()

    # --- internals ---------------------------------------------------------

    def _finish_one(self) -> None:
        self._outstanding -= 1
        if self._outstanding == 0:
            self._idle.set()

    def _pop_live(self, heap: list[_Entry]) -> _Entry | None:
        """Pop the highest-priority entry that is not dropped, superseded, or expired."""
        while heap:
            entry = heapq.heappop(heap)
            if entry.seq in self._dropped:
                self._dropped.discard(entry.seq)
                continue
            key = (type(entry.cmd), entry.priority)
            if isinstance(entry.cmd, COALESCABLE) and self._latest.get(key) != entry.seq:
                log.debug("coalesced away %s", entry.cmd)
                self._finish_one()
                continue
            if entry.deadline is not None and time.monotonic() > entry.deadline:
                log.info("deadline expired, dropping %s", entry.cmd)
                self._finish_one()
                continue
            return entry
        return None

    async def _motion_loop(self) -> None:
        while True:
            await self._motion_ready.wait()
            entry = self._pop_live(self._motion_heap)
            if entry is None:
                self._motion_ready.clear()
                continue
            try:
                await self._execute_motion(entry.cmd)
            except Exception:
                log.exception("motion command failed: %s", entry.cmd)
            finally:
                self._finish_one()

    async def _execute_motion(self, cmd: BodyCommand) -> None:
        if isinstance(cmd, EarsCommand):
            if self._state.ears == (cmd.left, cmd.right):
                log.debug("no-op ears command suppressed")
                return
            await self._adapter.set_ears(cmd.left, cmd.right)
            self._state.ears = (cmd.left, cmd.right)
        elif isinstance(cmd, LedsCommand):
            target = cmd.spec.as_dict()
            if all(self._state.leds.get(k) == v for k, v in target.items()) and not cmd.spec.pulse:
                log.debug("no-op LED command suppressed")
                return
            await self._adapter.set_leds(cmd.spec)
            self._state.leds.update(target)
        elif isinstance(cmd, ChorCommand):
            await self._adapter.play_chor(cmd.chor)
        elif isinstance(cmd, SleepCommand):
            await self._adapter.sleep()
        elif isinstance(cmd, WakeCommand):
            await self._adapter.wake()
        else:
            raise TypeError(f"unhandled motion command {cmd!r}")

    async def _audio_loop(self) -> None:
        while True:
            await self._audio_ready.wait()
            # one audio stream: let the current playback finish first
            if self._current_playback is not None:
                await self._current_playback.wait_finished()
                self._current_playback = None
                self._state.playing = False
            entry = self._pop_live(self._audio_pending)
            if entry is None:
                self._audio_ready.clear()
                continue
            try:
                if isinstance(entry.cmd, PlayAudioCommand):
                    handle = await self._adapter.play_audio(entry.cmd.urls, entry.cmd.duration_s)
                    self._state.last_audio_urls = entry.cmd.urls
                else:
                    assert isinstance(entry.cmd, SayCommand)
                    handle = await self._adapter.say(entry.cmd.text)
                self._current_playback = handle
                self._state.playing = True
            except Exception:
                log.exception("audio command failed: %s", entry.cmd)
            finally:
                self._finish_one()

    @property
    def current_playback(self) -> PlaybackHandle | None:
        """The in-flight playback, if any — the half-duplex gate awaits this."""
        return self._current_playback
