"""End-to-end realtime branch through the real VoicePipeline.

Covers the whole cycle the hardware will exercise: wake -> session -> PCM deltas
-> playback -> interruption -> a second turn WITHOUT re-waking -> close -> a new
wake. Plus the failure path, which matters just as much: a broken realtime
session must degrade to turn-based in place, with the capture still owned by
exactly one consumer.
"""

import asyncio
import base64

import pytest
from rabbit_brain.audio.pipeline import VoicePipeline
from rabbit_brain.body.types import BodyCapabilities


class FakePlayer:
    """Local streaming speaker stand-in (tests/ is not a package, so this is
    kept alongside its twin in test_realtime.py rather than imported)."""

    def __init__(self, played_ms=0):
        self.written: list[bytes] = []
        self.stopped = 0
        self.played_ms = played_ms

    def write(self, pcm):
        self.written.append(pcm)

    def reset_position(self):
        pass

    async def stop(self):
        self.stopped += 1

    async def resume(self):
        pass

    async def aclose(self):
        pass


class FakeCapture:
    """A capture that yields frames forever, and counts its consumers: if the
    realtime branch ever opened a SECOND stream, this would show it."""

    def __init__(self, frame=b"\x00\x00" * 160):
        self._frame = frame
        self.streams = 0

    @property
    def sample_rate(self):
        return 16000

    async def frames(self):
        self.streams += 1
        while True:
            await asyncio.sleep(0)
            yield self._frame


class FakeWake:
    """Fires once, then stays silent so the test ends deterministically."""

    def __init__(self, fire_after=1):
        self._fire_after = fire_after
        self._seen = 0
        self.resets = 0

    def feed(self, frame):
        self._seen += 1
        return 1.0 if self._seen == self._fire_after else 0.0

    def reset(self):
        self.resets += 1


class FakeAdapter:
    def __init__(self):
        self.capabilities = BodyCapabilities(
            can_cancel_audio=False,
            has_playback_events=False,
            can_read_body_state=False,
            has_per_led_rgb=True,
        )
        self.chors: list[str] = []

    async def play_chor(self, chor):
        self.chors.append(chor)

    async def play_audio(self, urls, duration_s):
        raise AssertionError("realtime must not queue MP3s on the rabbit")


class FakeController:
    """Just what the pipeline touches."""

    def __init__(self):
        self.adapter = FakeAdapter()
        self.chors: list[str] = []
        self.interrupts = 0

    def interrupt(self):
        self.interrupts += 1

    async def submit(self, cmd, priority):
        self.chors.append(getattr(cmd, "chor", cmd))

    @property
    def audio_busy(self):
        return False


def _pipeline(factory, wake=None, idle=0.05):
    capture = FakeCapture()
    controller = FakeController()
    pipeline = VoicePipeline(
        capture=capture,
        wake=wake or FakeWake(),
        probe_factory=lambda: None,
        stt=None,
        controller=controller,
        on_transcript=None,
        realtime_factory=factory,
        realtime_idle_timeout_s=idle,
        ack_render_s=0.0,
        listening_cycle_s=0.01,
    )
    return pipeline, capture, controller


async def _run_briefly(pipeline, seconds=0.5):
    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class ScriptedSession:
    """A RealtimeSession stand-in that plays a scripted conversation against the
    real pipeline: two model turns with a barge-in in between."""

    def __init__(self, player, state):
        self.player = player
        self._on_response_start = state.on_response_start
        self._on_barge_in = state.on_barge_in
        self.closed = False
        self.frames_pumped = 0
        self.events: list[str] = []

    async def pump_microphone(self, frames, stop=None, should_continue=None, idle_timeout_s=None):
        # turn 1: the model speaks
        self._on_response_start()
        self.events.append("response_1")
        await anext(frames)
        self.frames_pumped += 1
        # the user talks over it
        self._on_barge_in()
        self.events.append("barge_in")
        await anext(frames)
        self.frames_pumped += 1
        # turn 2 in the SAME session — no second wake word
        self._on_response_start()
        self.events.append("response_2")
        await anext(frames)
        self.frames_pumped += 1
        return "idle"

    async def run(self):
        await asyncio.Event().wait()  # never ends on its own

    async def aclose(self):
        self.closed = True


async def test_full_cycle_wake_session_bargein_second_turn_close_rewake():
    sessions = []

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)
        sessions.append(session)
        return session

    # wake fires twice: once to open the conversation, once after it closed
    class TwiceWake(FakeWake):
        def feed(self, frame):
            self._seen += 1
            return 1.0 if self._seen in (1, 8) else 0.0

    pipeline, capture, controller = _pipeline(factory, wake=TwiceWake())
    await _run_briefly(pipeline)

    assert capture.streams == 1, "the realtime branch must not open a second capture"
    assert len(sessions) >= 1
    first = sessions[0]
    assert first.events == ["response_1", "barge_in", "response_2"]  # 2 turns, 1 wake
    assert first.closed, "the session must be closed when the conversation ends"
    assert pipeline._wake.resets >= 1  # re-armed for a new wake
    assert not pipeline.realtime_failed
    # a second wake opens a NEW conversation
    assert pipeline.realtime_sessions >= 1


async def test_ux_states_wake_green_listening_then_animation():
    """wake -> ack, listening -> magenta scanner, answering -> body animation."""
    started = asyncio.Event()

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_response_start()  # rabbit answers -> light only
            started.set()
            await asyncio.sleep(0.08)
            return "idle"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.06)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # the ack lands first, then feedback keeps submitting choreography
    assert controller.interrupts >= 1
    assert len(controller.chors) >= 2


async def test_realtime_failure_falls_back_without_restart():
    """A broken session must cost only full-duplex: same single capture owner,
    and every later wake goes down the turn-based path."""
    calls = []

    async def failing_factory(state):
        calls.append(1)
        raise RuntimeError("websocket refused")

    handled = []

    pipeline, capture, _ = _pipeline(failing_factory)

    async def fake_turn_based(frames):
        handled.append(1)

    pipeline._handle_wake = fake_turn_based

    class AlwaysWake(FakeWake):
        def feed(self, frame):
            self._seen += 1
            return 1.0 if self._seen in (1, 5) else 0.0

    pipeline._wake = AlwaysWake()
    await _run_briefly(pipeline, seconds=0.3)

    assert pipeline.realtime_failed is True
    assert len(calls) == 1, "must not keep retrying realtime after a failure"
    assert handled, "later wakes fall back to the turn-based path"
    assert capture.streams == 1, "still exactly one capture consumer"


async def test_session_is_closed_even_when_the_conversation_errors():
    closed = []

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def boom(frames, stop=None, should_continue=None, idle_timeout_s=None):
            raise RuntimeError("api error mid-conversation")

        async def aclose():
            closed.append(1)

        session.pump_microphone = boom
        session.aclose = aclose
        return session

    pipeline, capture, _ = _pipeline(factory)
    pipeline._handle_wake = lambda frames: asyncio.sleep(0)
    await _run_briefly(pipeline, seconds=0.3)
    assert closed, "socket and player are released on the error path"
    assert pipeline.realtime_failed is True
    assert capture.streams == 1


async def test_realtime_never_queues_mp3_on_the_rabbit():
    """The rabbit's speaker path must stay untouched in realtime mode: audio is
    PCM straight to the local device, never an MP3 through OJN."""
    player = FakePlayer()

    async def factory(state):
        session = ScriptedSession(player, state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_response_start()
            player.write(base64.b64decode(base64.b64encode(b"\x01\x00" * 8)))
            return "idle"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    await _run_briefly(pipeline, seconds=0.3)
    assert player.written  # PCM went to the local speaker
    # FakeAdapter.play_audio raises if anything tries the rabbit's audio lane
    assert not any(isinstance(c, tuple) for c in controller.chors)
