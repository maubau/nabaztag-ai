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
        self.submitted: list[tuple[str, object]] = []
        self.interrupts = 0
        self.dropped_below: list[object] = []

    def interrupt(self, below=None):
        self.interrupts += 1
        if below is not None:
            self.dropped_below.append(below)
            # mirror the real controller: pending work below `below` is dropped
            self.chors.clear()

    async def submit(self, cmd, priority, deadline=None):
        chor = getattr(cmd, "chor", cmd)
        self.chors.append(chor)
        self.submitted.append((chor, priority))

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


async def test_ux_is_event_driven_with_no_periodic_animation():
    """Hardware-confirmed (#8): a chor QUEUES behind the running one and cannot
    be cancelled, so a periodic animation builds a remote backlog the terminator
    lands behind — the rabbit kept moving 20-30 s after the conversation ended.
    Feedback must therefore be emitted per TRANSITION, never on a timer."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_response_start()  # one transition: waiting/answering
            started.set()
            await release.wait()
            return "idle"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.25)  # >> the old ~1.9 s loop would have fired here
    emitted = len(controller.chors)
    await asyncio.sleep(0.25)  # nothing changes, so nothing more may be sent
    assert len(controller.chors) == emitted, "feedback is still firing on a timer"
    release.set()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cleanup_drops_pending_feedback_and_ends_with_the_terminator():
    """Choreography already on the rabbit cannot be recalled, so the only lever
    is refusing to lengthen the remote queue: everything still local is dropped
    and the neutral state goes out above it, LAST."""
    from rabbit_brain.body.types import Priority

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_speech_started()
            await asyncio.sleep(0)
            state.on_speech_stopped()
            await anext(frames)
            return "hangup"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    await _run_briefly(pipeline, seconds=0.4)

    assert controller.dropped_below, "pending cosmetic feedback was never dropped"
    assert Priority.USER_SPEECH_SYNC in controller.dropped_below
    last_chor, last_priority = controller.submitted[-1]
    # LEDs off + ears neutral, above the cosmetic priority
    assert "led,0,0,0,0" in last_chor
    assert last_priority == Priority.USER_SPEECH_SYNC
    assert controller.chors[-1] == last_chor, "something was queued after the terminator"


async def test_cosmetic_feedback_carries_a_deadline():
    """A late state indicator describes a moment that has passed; the controller
    must be allowed to drop it rather than fire it."""
    deadlines = []

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_speech_started()
            await asyncio.sleep(0.05)
            return "idle"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    original = controller.submit

    async def recording_submit(cmd, priority, deadline=None):
        deadlines.append(deadline)
        await original(cmd, priority, deadline)

    controller.submit = recording_submit
    await _run_briefly(pipeline, seconds=0.3)
    assert any(d is not None for d in deadlines), "cosmetic feedback had no deadline"


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


async def test_hangup_rearms_even_when_teardown_hangs():
    """The blocker: after end_conversation the pipeline never reached REARMED,
    so the microphone stopped being consumed for the rest of the run. Teardown
    is bounded now — a session that refuses to close must not prevent re-arming
    or a second conversation."""
    sessions = []

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            await anext(frames)
            return "hangup"  # the model said goodbye

        async def never_closes():
            await asyncio.sleep(30)  # a writer stuck inside PortAudio

        session.pump_microphone = pump
        session.aclose = never_closes
        sessions.append(session)
        return session

    class TwiceWake(FakeWake):
        def feed(self, frame):
            self._seen += 1
            return 1.0 if self._seen in (1, 6) else 0.0

    pipeline, capture, _ = _pipeline(factory, wake=TwiceWake())
    # cleanup is bounded at REALTIME_CLEANUP_TIMEOUT_S; keep the test brisk
    import rabbit_brain.audio.pipeline as pipeline_mod

    original = pipeline_mod.REALTIME_CLEANUP_TIMEOUT_S
    pipeline_mod.REALTIME_CLEANUP_TIMEOUT_S = 0.1
    try:
        await _run_briefly(pipeline, seconds=0.6)
    finally:
        pipeline_mod.REALTIME_CLEANUP_TIMEOUT_S = original

    assert pipeline._wake.resets >= 1, "never re-armed: the microphone would stay unconsumed"
    assert capture.streams == 1, "still exactly one capture consumer"
    # the second wake opened another conversation, so the pipeline kept running
    assert len(sessions) >= 2, "a second realtime session must be startable"


async def test_frames_keep_being_consumed_after_a_hangup():
    """After the session ends the run loop must go back to pulling frames —
    that is what stops 'capture queue full, dropping blocks'."""
    consumed_after = []

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            await anext(frames)
            return "hangup"

        session.pump_microphone = pump
        return session

    class OnceWake(FakeWake):
        def feed(self, frame):
            self._seen += 1
            if self._seen > 3:
                consumed_after.append(1)  # the run loop is pulling frames again
            return 1.0 if self._seen == 1 else 0.0

    pipeline, _, _ = _pipeline(factory, wake=OnceWake())
    await _run_briefly(pipeline, seconds=0.4)
    assert consumed_after, "the pipeline stopped consuming the microphone"


# --- ASSISTANT_SPEAKING: the body must show that the rabbit is talking -----


def _speaking_ticks(controller):
    from rabbit_brain.body.chor import SPEAKING_TICK_TEMPO_MS

    prefix = f"{SPEAKING_TICK_TEMPO_MS},"
    return [c for c in controller.submitted if str(c[0]).startswith(prefix)]


async def test_assistant_speaking_animates_for_as_long_as_playback_runs():
    """A reply lasts seconds; a body that sits still through all of it reads as
    broken. The animation is driven by the PLAYER, not by the server: the model
    finishes generating long before the rabbit finishes talking."""
    release = asyncio.Event()

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_response_start()
            state.on_playback_started()
            await release.wait()
            return "idle"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    from rabbit_brain.audio import pipeline as pipeline_mod

    pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.02  # keep the test quick
    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(0.2)
    ticks = len(_speaking_ticks(controller))
    assert ticks >= 2, "the rabbit stood still while it was speaking"
    release.set()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.9


async def test_speaking_stops_at_the_drain_and_never_on_response_done():
    """`response.done` arrives while seconds of PCM are still queued. Only the
    player's own drain may end the speaking indicator."""
    from rabbit_brain.audio.pipeline import VoicePipeline as _VP

    state = _make_state()
    state.on_response_start()
    assert _VP._realtime_ux_state(state) == "open"  # generating != talking
    state.on_playback_started()
    assert _VP._realtime_ux_state(state) == "assistant"
    state.on_playback_drained()
    assert _VP._realtime_ux_state(state) == "open"


def _make_state():
    from rabbit_brain.audio.pipeline import _SpeakingState

    return _SpeakingState()


async def test_barge_in_stops_the_ticks_and_puts_listening_above_them():
    """Worst case after a cut must be ONE tick already on the wire, not a
    backlog: the scheduler stops on the same event, whatever is still local is
    dropped, and 'listening' goes out above the cosmetic priority."""
    from rabbit_brain.body.chor import build_speech_ack_chor
    from rabbit_brain.body.types import Priority

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_response_start()
            state.on_playback_started()
            await asyncio.sleep(0.1)  # the rabbit is talking
            state.on_barge_in()  # the user cuts in
            state.on_playback_cut()
            await asyncio.sleep(0.15)
            return "hangup"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    from rabbit_brain.audio import pipeline as pipeline_mod

    pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.02
    try:
        await _run_briefly(pipeline, seconds=0.5)
    finally:
        pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.9

    order = [chor for chor, _ in controller.submitted]
    ack_index = next(
        i for i, chor in enumerate(order) if chor == build_speech_ack_chor(listen_pose=(0, 0))
    )
    from rabbit_brain.body.chor import SPEAKING_TICK_TEMPO_MS

    prefix = f"{SPEAKING_TICK_TEMPO_MS},"
    assert any(c.startswith(prefix) for c in order[:ack_index]), "no speaking animation at all"
    assert not any(c.startswith(prefix) for c in order[ack_index:]), (
        "a speaking tick was queued AFTER the barge-in"
    )
    assert Priority.USER_SPEECH_SYNC in controller.dropped_below
    assert (build_speech_ack_chor(listen_pose=(0, 0)), Priority.USER_SPEECH_SYNC) in (
        controller.submitted
    )


async def test_hangup_leaves_no_speaking_command_after_the_terminator():
    from rabbit_brain.body.chor import SPEAKING_TICK_TEMPO_MS
    from rabbit_brain.body.types import Priority

    async def factory(state):
        session = ScriptedSession(FakePlayer(), state)

        async def pump(frames, stop=None, should_continue=None, idle_timeout_s=None):
            state.on_playback_started()
            await asyncio.sleep(0.1)
            state.on_playback_drained()
            return "hangup"

        session.pump_microphone = pump
        return session

    pipeline, _, controller = _pipeline(factory)
    from rabbit_brain.audio import pipeline as pipeline_mod

    pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.02
    try:
        await _run_briefly(pipeline, seconds=0.5)
    finally:
        pipeline_mod.SPEAKING_TICK_PERIOD_S = 0.9

    last_chor, last_priority = controller.submitted[-1]
    assert "led,0,0,0,0" in last_chor and last_priority == Priority.USER_SPEECH_SYNC
    assert not last_chor.startswith(f"{SPEAKING_TICK_TEMPO_MS},")


# --- the tick itself ------------------------------------------------------


def test_speaking_tick_is_short_and_reverses_the_ears_each_phase():
    from rabbit_brain.body.chor import (
        SPEAKING_EAR_SWING,
        SPEAKING_TICK_S,
        build_speaking_tick_chor,
    )

    assert SPEAKING_TICK_S < 0.6, "a tick must be short enough to be cut cleanly"
    even = build_speaking_tick_chor(0, listen_pose=(0, 0)).split(",")
    odd = build_speaking_tick_chor(1, listen_pose=(0, 0)).split(",")

    even_motors = _motor_actions(even)
    odd_motors = _motor_actions(odd)
    assert len(even_motors) == 2 and len(odd_motors) == 2
    # the two ears always turn in OPPOSITE directions...
    assert even_motors[0]["dir"] != even_motors[1]["dir"]
    assert odd_motors[0]["dir"] != odd_motors[1]["dir"]
    # ...and the next tick reverses them
    assert even_motors[0]["dir"] != odd_motors[0]["dir"]
    # even phases swing out, odd phases come back
    assert even_motors[0]["angle"] == SPEAKING_EAR_SWING * 18
    assert odd_motors[0]["angle"] == 0


def _motor_actions(parts):
    out = []
    for i, token in enumerate(parts):
        if token == "motor":
            out.append({"ear": int(parts[i + 1]), "angle": int(parts[i + 2]), "dir": parts[i + 4]})
    return out


def test_speaking_tick_drives_the_front_leds_like_a_mouth():
    """Front/nose brightest, base and top only glowing along — and it ends DIM,
    not off, so the state stays visible between ticks without a second command
    to hold it."""
    from rabbit_brain.body.chor import build_speaking_tick_chor

    parts = build_speaking_tick_chor(0, listen_pose=(0, 0)).split(",")
    frames = {}
    for i, token in enumerate(parts):
        if token == "led":
            t, led = int(parts[i - 1]), int(parts[i + 1])
            frames.setdefault(t, {})[led] = tuple(int(x) for x in parts[i + 2 : i + 5])
    assert all(len(f) == 5 for f in frames.values()), "every LED is addressed in every frame"
    peak = max(frames, key=lambda t: sum(frames[t][2]))
    assert sum(frames[peak][2]) > sum(frames[peak][0]), "the nose must lead the base"
    last = frames[max(frames)]
    assert 0 < sum(last[2]) < sum(frames[peak][2]), "the tick must end dim, not off or bright"
