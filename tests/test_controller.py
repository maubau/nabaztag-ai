import asyncio
import time

from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.types import (
    BodyCapabilities,
    EarsCommand,
    LedsCommand,
    LedSpec,
    PlayAudioCommand,
    Priority,
    SayCommand,
)


class FakePlayback:
    def __init__(self, duration_s=None):
        self._started = asyncio.Event()
        self._started.set()
        self._finished = asyncio.Event()
        self.cancelled = False
        self.estimated_duration_s = duration_s

    def finish(self):
        self._finished.set()

    async def wait_started(self):
        await self._started.wait()

    async def wait_finished(self):
        await self._finished.wait()

    async def cancel(self):
        self.cancelled = True
        self._finished.set()


class FakeAdapter:
    def __init__(self, can_cancel_audio=False):
        self.capabilities = BodyCapabilities(
            can_cancel_audio=can_cancel_audio,
            has_playback_events=False,
            can_read_body_state=False,
            has_per_led_rgb=True,
        )
        self.calls: list[tuple] = []
        self.playbacks: list[FakePlayback] = []

    async def set_ears(self, left, right):
        self.calls.append(("ears", left, right))

    async def set_leds(self, spec):
        self.calls.append(("leds", spec.as_dict()))

    async def play_audio(self, urls, duration_s):
        self.calls.append(("play", urls))
        pb = FakePlayback(duration_s)
        self.playbacks.append(pb)
        return pb

    async def say(self, text):
        self.calls.append(("say", text))
        pb = FakePlayback()
        self.playbacks.append(pb)
        return pb

    async def play_chor(self, chor):
        self.calls.append(("chor", chor))

    async def sleep(self):
        self.calls.append(("sleep",))

    async def wake(self):
        self.calls.append(("wake",))

    async def events(self):
        return
        yield

    def push_event(self, event):
        pass


async def drain(controller):
    await asyncio.wait_for(controller.wait_idle(), 2)


async def run_controller(controller):
    task = asyncio.create_task(controller.run())
    await asyncio.sleep(0)  # let the loops start
    return task


async def test_priority_order_higher_first():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    # queue before the consumer starts so ordering is decided by priority alone
    await c.submit(EarsCommand(16, 16), Priority.AMBIENT_IDLE)
    await c.submit(EarsCommand(0, 0), Priority.SAFETY_SYSTEM)
    await c.submit(EarsCommand(4, 4), Priority.AGENT_EXPRESSION)
    task = await run_controller(c)
    await drain(c)
    assert adapter.calls == [("ears", 0, 0), ("ears", 4, 4), ("ears", 16, 16)]
    task.cancel()


async def test_coalescing_same_priority_keeps_latest():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    for pos in (1, 2, 3):
        await c.submit(EarsCommand(pos, pos), Priority.AGENT_EXPRESSION)
    task = await run_controller(c)
    await drain(c)
    assert adapter.calls == [("ears", 3, 3)]
    task.cancel()


async def test_coalescing_does_not_cross_priorities():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    await c.submit(EarsCommand(1, 1), Priority.USER_SPEECH_SYNC)
    await c.submit(EarsCommand(2, 2), Priority.AMBIENT_IDLE)
    task = await run_controller(c)
    await drain(c)
    assert adapter.calls == [("ears", 1, 1), ("ears", 2, 2)]
    task.cancel()


async def test_deadline_expired_command_is_dropped_not_fired_late():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    await c.submit(EarsCommand(5, 5), Priority.USER_SPEECH_SYNC, deadline=time.monotonic() - 1)
    await c.submit(EarsCommand(9, 9), Priority.AMBIENT_IDLE)
    task = await run_controller(c)
    await drain(c)
    assert adapter.calls == [("ears", 9, 9)]
    task.cancel()


async def test_noop_commands_suppressed():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    task = await run_controller(c)
    await c.submit(EarsCommand(8, 8), Priority.AGENT_EXPRESSION)
    await drain(c)
    await c.submit(EarsCommand(8, 8), Priority.AGENT_EXPRESSION)
    spec = LedSpec.from_dict({"nose": (0, 255, 0)})
    await c.submit(LedsCommand(spec), Priority.AGENT_EXPRESSION)
    await drain(c)
    await c.submit(LedsCommand(spec), Priority.AGENT_EXPRESSION)
    await drain(c)
    assert adapter.calls == [("ears", 8, 8), ("leds", {"nose": (0, 255, 0)})]
    assert c.snapshot().ears == (8, 8)
    task.cancel()


async def test_interrupt_drops_pending_below_priority():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    await c.submit(EarsCommand(16, 16), Priority.AMBIENT_IDLE)
    await c.submit(EarsCommand(1, 1), Priority.USER_SPEECH_SYNC)
    c.interrupt(below=Priority.USER_SPEECH_SYNC)  # wake word: snap to attention
    task = await run_controller(c)
    await drain(c)
    assert adapter.calls == [("ears", 1, 1)]
    task.cancel()


async def test_no_cancel_degradation_current_finishes_queued_drop():
    adapter = FakeAdapter(can_cancel_audio=False)
    c = BodyController(adapter)
    task = await run_controller(c)
    await c.submit(PlayAudioCommand(("http://x/now.mp3",)), Priority.AGENT_EXPRESSION)
    await drain(c)  # first playback sent, still "playing"
    await c.submit(PlayAudioCommand(("http://x/queued.mp3",)), Priority.AMBIENT_IDLE)
    c.interrupt()
    await drain(c)
    # queued play dropped; current playback NOT cancelled (body can't honor it)
    assert ("play", ("http://x/queued.mp3",)) not in adapter.calls
    assert adapter.playbacks[0].cancelled is False
    adapter.playbacks[0].finish()
    task.cancel()


async def test_cancel_capable_body_cancels_current_playback():
    adapter = FakeAdapter(can_cancel_audio=True)
    c = BodyController(adapter)
    task = await run_controller(c)
    await c.submit(SayCommand("blah"), Priority.AGENT_EXPRESSION)
    await drain(c)
    c.interrupt()
    await asyncio.sleep(0)  # let the cancel task run
    assert adapter.playbacks[0].cancelled is True
    task.cancel()


async def test_audio_serialized_second_waits_for_first():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    task = await run_controller(c)
    await c.submit(PlayAudioCommand(("http://x/1.mp3",)), Priority.USER_SPEECH_SYNC)
    await drain(c)
    await c.submit(PlayAudioCommand(("http://x/2.mp3",)), Priority.USER_SPEECH_SYNC)
    await asyncio.sleep(0.05)
    assert ("play", ("http://x/2.mp3",)) not in adapter.calls  # first still playing
    adapter.playbacks[0].finish()
    await drain(c)
    assert ("play", ("http://x/2.mp3",)) in adapter.calls
    adapter.playbacks[1].finish()
    task.cancel()


async def test_motion_runs_while_audio_plays():
    adapter = FakeAdapter()
    c = BodyController(adapter)
    task = await run_controller(c)
    await c.submit(PlayAudioCommand(("http://x/long.mp3",)), Priority.USER_SPEECH_SYNC)
    await drain(c)
    # speech-synced gesture while the rabbit is speaking
    await c.submit(EarsCommand(2, 14), Priority.USER_SPEECH_SYNC)
    await drain(c)
    assert ("ears", 2, 14) in adapter.calls
    assert c.snapshot().playing is True
    adapter.playbacks[0].finish()
    task.cancel()


async def test_audio_busy_true_during_play_audio_round_trip():
    """audio_busy must hold from the moment an entry is popped off
    _audio_pending, through the adapter.play_audio()/say() round-trip itself,
    not just once current_playback is assigned (hardware finding, July 2026:
    _audio_pending was already empty and current_playback not yet set during
    the round-trip, so audio_busy — and the pipeline's half-duplex gate —
    went False mid-turn)."""
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter(FakeAdapter):
        async def play_audio(self, urls, duration_s):
            started.set()
            await release.wait()
            return await super().play_audio(urls, duration_s)

    adapter = SlowAdapter()
    c = BodyController(adapter)
    task = await run_controller(c)
    assert c.audio_busy is False
    await c.submit(PlayAudioCommand(("http://x/s.mp3",), 1.0), Priority.USER_SPEECH_SYNC)
    await asyncio.wait_for(started.wait(), 2)
    # entry popped (_audio_pending now empty), adapter call in flight,
    # current_playback not assigned yet — this is the gap that used to leak.
    assert c.audio_busy is True
    assert adapter.calls == []  # the round-trip hasn't even reached the adapter's own call log
    release.set()
    await drain(c)
    assert c.audio_busy is True  # now actually playing
    adapter.playbacks[0].finish()
    await asyncio.sleep(0)
    assert c.audio_busy is False
    task.cancel()


async def test_end_to_end_against_mock_ojn(controller, mock_ojn):
    """Same arbitration paths, real HTTP against the mock OJN server."""
    await controller.submit(EarsCommand(3, 12), Priority.AGENT_EXPRESSION)
    await controller.submit(
        PlayAudioCommand(("http://bolt:8090/s.mp3",), duration_s=0.01),
        Priority.USER_SPEECH_SYNC,
    )
    await asyncio.wait_for(controller.wait_idle(), 2)
    assert mock_ojn.ears == (3, 12)
    assert len(mock_ojn.calls_of("stream")) == 1
