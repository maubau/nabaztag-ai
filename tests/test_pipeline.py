"""VoicePipeline flow with fakes: wake → sequential feedback → VAD → streaming STT."""

import asyncio

from rabbit_brain.audio.capture import WavCapture
from rabbit_brain.audio.doa import DoaReading
from rabbit_brain.audio.pipeline import VoicePipeline
from rabbit_brain.audio.vad import VAD_CHUNK_SAMPLES
from rabbit_brain.body.chor import (
    build_leds_off_chor,
    build_listening_chor,
    build_wake_ack_chor,
)
from rabbit_brain.body.types import ChorCommand, EarsCommand, Priority
from rabbit_brain.stt import STTResult
from test_audio_capture import make_multichannel_wav

SILENCE = b"\x00\x00" * VAD_CHUNK_SAMPLES
SPEECH = b"\x00\x10" * VAD_CHUNK_SAMPLES

RECORDER_KWARGS = {"end_of_speech_ms": 96, "start_timeout_s": 0.32, "pre_roll_ms": 64}
DOA_MOODS = {
    "sectors": [{"from": 45, "to": 135, "ears": {"left": 2, "right": 8}}],
    "listen_pose": {"left": 0, "right": 0},
}
# fast feedback timings so the LISTENING loop actually iterates within a test
FAST = {"ack_render_s": 0.0, "listening_cycle_s": 0.0}


class FakeCapture:
    sample_rate = 16_000

    def __init__(self, blocks):
        self._blocks = blocks

    async def frames(self):
        for block in self._blocks:
            yield block
            await asyncio.sleep(0)


class GatedCapture:
    """Yields `pre`, then blocks on `gate` before yielding `post` — lets a test
    hold the utterance open (LISTENING running) and release end-of-speech."""

    sample_rate = 16_000

    def __init__(self, pre, gate: asyncio.Event, post):
        self._pre, self._gate, self._post = pre, gate, post

    async def frames(self):
        for block in self._pre:
            yield block
            await asyncio.sleep(0)
        await self._gate.wait()
        for block in self._post:
            yield block
            await asyncio.sleep(0)


class FakeWake:
    def __init__(self, trigger_at=None):
        self.feeds = 0
        self.resets = 0
        self._trigger_at = trigger_at

    def feed(self, pcm: bytes) -> float:
        self.feeds += 1
        return 1.0 if self.feeds - 1 == self._trigger_at else 0.0

    def reset(self) -> None:
        self.resets += 1


class FakeSTT:
    def __init__(self, release: asyncio.Event | None = None):
        self.pcm = b""
        self._release = release  # if set, block until the test releases it

    async def transcribe(self, chunks, sample_rate):
        async for c in chunks:
            self.pcm += c
        if self._release is not None:
            await self._release.wait()
        return STTResult(text="ciao coniglio", provider="fake")


class FakeDoa:
    def __init__(self, reading):
        self._reading = reading
        self.reads = 0

    async def read(self):
        self.reads += 1
        return self._reading


class FakePlayback:
    def __init__(self, finished: bool):
        self.finished = finished


class FakeController:
    def __init__(self):
        self.submitted = []
        self.interrupts = 0
        self.current_playback = None

    async def submit(self, cmd, priority, deadline=None):
        self.submitted.append((cmd, priority))

    def interrupt(self, below=Priority.USER_SPEECH_SYNC):
        self.interrupts += 1


def loud_probe(chunk: bytes) -> float:
    return 1.0 if any(chunk) else 0.0


def make_pipeline(capture, wake, controller, transcripts, doa=None, **kwargs):
    async def on_transcript(text: str) -> None:
        transcripts.append(text)

    opts = {**FAST, **kwargs}
    return VoicePipeline(
        capture=capture,
        wake=wake,
        probe_factory=lambda: loud_probe,
        stt=opts.pop("stt", FakeSTT()),
        controller=controller,
        on_transcript=opts.pop("on_transcript", on_transcript),
        doa=doa,
        doa_moods=DOA_MOODS,
        recorder_kwargs=RECORDER_KWARGS,
        **opts,
    )


def chors(controller) -> list[str]:
    """The chor strings submitted to a FakeController, in order."""
    return [cmd.chor for cmd, _ in controller.submitted if isinstance(cmd, ChorCommand)]


async def wait_until(predicate, deadline_s=2.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + deadline_s
    while not predicate():
        if loop.time() > end:
            raise AssertionError("predicate not satisfied in time")
        await asyncio.sleep(0.002)  # noqa: ASYNC110 — polling external mock-OJN state


async def test_wake_doa_vad_stt_flow():
    blocks = [SILENCE] * 3 + [SILENCE] + [SPEECH] * 6 + [SILENCE] * 10
    controller = FakeController()
    stt = FakeSTT()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture(blocks),
        FakeWake(trigger_at=3),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=90)),
        stt=stt,
    )
    await pipeline.run()

    assert transcripts == ["ciao coniglio"]
    assert controller.interrupts == 1
    submitted_chors = chors(controller)
    # wake ack (right ear for 90°) as a single non-coalescable choreography
    assert build_wake_ack_chor("right", listen_pose=(0, 0)) in submitted_chors
    # LISTENING scanner + ear nod on the voice side, then LEDs off; all chor
    assert build_listening_chor("right", listen_pose=(0, 0)) in submitted_chors
    assert submitted_chors[-1] == build_leds_off_chor()
    assert not any(isinstance(cmd, EarsCommand) for cmd, _ in controller.submitted)
    assert all(p == Priority.DOA_REFLEX for _, p in controller.submitted)
    assert stt.pcm.count(SPEECH) == 6


async def test_wake_ack_precedes_scanner_on_real_controller(controller, mock_ojn):
    """Required: the ACTUAL wire order is ack → scanner (not just membership),
    through the REAL BodyController — the ack must never be replaced by the
    scanner. Choreography-only: no posleft/posright."""
    gate = asyncio.Event()
    controller_transcripts = []
    pipeline = make_pipeline(
        GatedCapture([SILENCE, SPEECH, SPEECH], gate, [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        controller_transcripts,
        doa=FakeDoa(DoaReading(angle_deg=270)),  # left
        ack_render_s=0.0,
        listening_cycle_s=0.01,
    )
    run = asyncio.create_task(pipeline.run())
    try:
        # hold the utterance open until the LISTENING scanner has reached OJN
        await wait_until(
            lambda: (
                build_listening_chor("left", listen_pose=(0, 0))
                in [c.params["chor"] for c in mock_ojn.calls_of("chor")]
            )
        )
        wire = [c.params["chor"] for c in mock_ojn.calls_of("chor")]
        assert wire[0] == build_wake_ack_chor("left", listen_pose=(0, 0))  # ack FIRST
        assert wire[1] == build_listening_chor("left", listen_pose=(0, 0))  # scanner AFTER
        assert mock_ojn.calls_of("ears") == []
    finally:
        gate.set()
        await asyncio.wait_for(run, 2)
    assert controller_transcripts == ["ciao coniglio"]


async def test_scanner_stops_at_end_of_speech_not_at_stt(controller, mock_ojn):
    """Required: the LISTENING scanner must turn off at VAD end-of-speech even
    when STT is slow (the LEDs are lit only while the user speaks, not through
    Deepgram endpointing/network)."""
    release = asyncio.Event()  # STT stays pending until released
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture([SILENCE, SPEECH, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=0)),
        stt=FakeSTT(release=release),
        listening_cycle_s=0.01,
    )
    run = asyncio.create_task(pipeline.run())
    try:
        # LEDs go off (scanner stopped) while STT is still blocked → no transcript yet
        await wait_until(
            lambda: build_leds_off_chor() in [c.params["chor"] for c in mock_ojn.calls_of("chor")]
        )
        assert transcripts == []  # STT has not returned; the stop did not wait for it
    finally:
        release.set()
        await asyncio.wait_for(run, 2)
    assert transcripts == ["ciao coniglio"]


async def test_periodic_doa_reads_stop_at_end_of_speech():
    # the periodic DoA reads live in the feedback loop; they must stop when the
    # utterance ends (feedback cancelled/collected), not keep polling forever
    doa = FakeDoa(DoaReading(angle_deg=90))
    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture([SILENCE, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=doa,
        listening_cycle_s=0.0,
    )
    await pipeline.run()
    reads_after = doa.reads
    await asyncio.sleep(0.05)  # if the loop leaked, reads would keep climbing
    assert doa.reads == reads_after
    assert pipeline._feedback_tasks == set()  # feedback collected


async def test_recording_never_waits_for_doa():
    # a hung DoA read must not delay VAD/STT: the feedback task is separate and
    # the DoA read is time-bounded; the transcript still comes through
    class HungDoa:
        async def read(self):
            await asyncio.Event().wait()  # never returns

    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture([SILENCE] + [SPEECH] * 4 + [SILENCE] * 10),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=HungDoa(),
        doa_timeout_s=0.05,
    )
    await asyncio.wait_for(pipeline.run(), 2)
    assert transcripts == ["ciao coniglio"]  # nothing blocked on DoA


async def test_wake_beep_frames_excluded_from_stt():
    """Required: the local wake beep must not enter VAD/STT (no AEC). The
    beep-window frames are dropped, so the beep tone never reaches the STT."""
    beep_played = asyncio.Event()

    async def beep():
        beep_played.set()

    stt = FakeSTT()
    controller = FakeController()
    transcripts = []
    # the first two blocks stand in for the beep tone (loud); the guard is sized
    # to exactly those two frames (32 ms each)
    guard_ms = round(2 * (VAD_CHUNK_SAMPLES / 16_000) * 1000)  # 64 ms → drops 2 frames
    pipeline = make_pipeline(
        FakeCapture([SPEECH, SPEECH, SILENCE, SPEECH, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=0)),
        stt=stt,
        wake_sound=beep,
        beep_guard_ms=guard_ms,
    )
    await pipeline.run()
    assert beep_played.is_set()  # the beep fired
    # 5 SPEECH blocks total, 2 of them the beep window → only 3 reach the STT
    assert stt.pcm.count(SPEECH) == 3


async def test_side_of_angle():
    side = VoicePipeline._side_of
    assert side(90) == "right"
    assert side(270) == "left"
    assert side(0) is None  # front: both
    assert side(180) is None  # behind
    assert side(360 + 90) == "right"


async def test_processing_indicator_opt_in():
    from rabbit_brain.body.chor import build_processing_chor

    controller = FakeController()
    transcripts = []
    # default: no PROCESSING chor (LEDs off after speech)
    pipeline = make_pipeline(
        FakeCapture([SILENCE, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=0)),
    )
    await pipeline.run()
    assert build_processing_chor() not in chors(controller)

    # opt-in: PROCESSING pulse runs around on_transcript
    controller2 = FakeController()
    pipeline2 = make_pipeline(
        FakeCapture([SILENCE, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller2,
        [],
        doa=FakeDoa(DoaReading(angle_deg=0)),
        processing_indicator=True,
    )
    await pipeline2.run()
    assert build_processing_chor() in chors(controller2)


async def test_wake_timings_recorded(caplog):
    import logging

    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture([SILENCE, SPEECH, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=0)),
    )
    with caplog.at_level(logging.INFO):
        await pipeline.run()
    assert any("wake cycle timings" in r.message for r in caplog.records)


async def test_half_duplex_gate_blocks_wake():
    controller = FakeController()
    controller.current_playback = FakePlayback(finished=False)
    wake = FakeWake(trigger_at=0)  # would fire on the very first frame
    transcripts = []
    pipeline = make_pipeline(FakeCapture([SPEECH] * 5), wake, controller, transcripts)
    await pipeline.run()
    assert wake.feeds == 0  # never heard ourselves
    assert wake.resets == 5
    assert transcripts == []


async def test_gate_lifts_when_playback_finished():
    controller = FakeController()
    controller.current_playback = FakePlayback(finished=True)
    wake = FakeWake(trigger_at=0)
    transcripts = []
    blocks = [SILENCE] + [SPEECH] * 4 + [SILENCE] * 10
    pipeline = make_pipeline(FakeCapture(blocks), wake, controller, transcripts)
    await pipeline.run()
    assert transcripts == ["ciao coniglio"]


async def test_no_speech_after_wake_gives_no_transcript():
    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture([SILENCE] * 30), FakeWake(trigger_at=0), controller, transcripts
    )
    await pipeline.run()
    assert transcripts == []
    assert controller.interrupts == 1  # wake still snapped to attention


async def test_pipeline_close_collects_pending_tasks():
    # aclose() must cancel/collect every background task (feedback + beep)
    async def hung_beep():
        await asyncio.Event().wait()

    controller = FakeController()
    pipeline = make_pipeline(
        FakeCapture([]), FakeWake(), controller, [], doa=FakeDoa(None), wake_sound=hung_beep
    )
    beep = pipeline._spawn(pipeline._safe_beep(), pipeline._beep_tasks)
    eos = asyncio.Event()
    feedback = pipeline._spawn(pipeline._listening_feedback(eos), pipeline._feedback_tasks)
    await asyncio.sleep(0)
    await pipeline.aclose()
    assert beep.cancelled() and feedback.cancelled()
    await asyncio.sleep(0)
    assert pipeline._beep_tasks == set()
    assert pipeline._feedback_tasks == set()


async def test_pipeline_over_multichannel_wav_fixture(tmp_path):
    """CI end-to-end: 6-channel WAV → channel-0 extraction → wake → VAD → STT."""
    path = make_multichannel_wav(tmp_path / "six.wav", frames=VAD_CHUNK_SAMPLES * 8)
    capture = WavCapture(path, selected_channel=0)
    controller = FakeController()
    stt = FakeSTT()
    transcripts = []
    pipeline = make_pipeline(capture, FakeWake(trigger_at=0), controller, transcripts, stt=stt)
    await pipeline.run()
    assert transcripts == ["ciao coniglio"]  # fixture channel data is non-silent
    assert len(stt.pcm) > 0
