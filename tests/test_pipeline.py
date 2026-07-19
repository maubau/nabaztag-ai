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
from rabbit_brain.body.types import ChorCommand, EarsCommand, PlayAudioCommand, Priority
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
    # wake ack is immediate/global: green LEDs and both ears forward
    assert build_wake_ack_chor(None, listen_pose=(0, 0)) in submitted_chors
    # the feedback always ends by turning the LEDs off; everything is chor
    assert submitted_chors[-1] == build_leds_off_chor(ears_pose=(0, 0))
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
        assert wire[0] == build_wake_ack_chor(None, listen_pose=(0, 0))  # ack FIRST
        assert wire[1] == build_listening_chor("left", listen_pose=(0, 0))  # scanner AFTER
        assert mock_ojn.calls_of("ears") == []
    finally:
        gate.set()
        await asyncio.wait_for(run, 2)
    assert controller_transcripts == ["ciao coniglio"]


async def test_eos_during_ack_render_sends_no_scanner(controller, mock_ojn):
    """Required: if end-of-speech arrives during the ack-render wait, NO
    LISTENING chor is sent (no scanner after the utterance), and LEDs-off is
    the last command on the wire."""
    gate = asyncio.Event()
    pipeline = make_pipeline(
        GatedCapture([SILENCE, SPEECH, SPEECH], gate, [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        [],
        doa=FakeDoa(DoaReading(angle_deg=0)),  # side None
        ack_render_s=5.0,  # long render window; EOS will land inside it
        listening_cycle_s=5.0,
    )
    run = asyncio.create_task(pipeline.run())
    try:
        await wait_until(
            lambda: (
                build_wake_ack_chor(None, listen_pose=(0, 0))
                in [c.params["chor"] for c in mock_ojn.calls_of("chor")]
            )
        )
        gate.set()  # end of speech, during ack_render
        await asyncio.wait_for(run, 2)
    finally:
        gate.set()
    wire = [c.params["chor"] for c in mock_ojn.calls_of("chor")]
    assert build_listening_chor(None, listen_pose=(0, 0)) not in wire  # no scanner
    assert wire[-1] == build_leds_off_chor(ears_pose=(0, 0))


async def test_eos_during_slow_doa_read_sends_no_listening(controller, mock_ojn):
    """Required: if end-of-speech arrives while a periodic DoA read is in
    flight (up to doa_timeout_s), no new LISTENING chor is submitted for that
    cycle — the read is cancelled by the EOS event."""

    class SlowDoa:
        def __init__(self):
            self.reads = 0
            self.slow_started = asyncio.Event()

        async def read(self):
            self.reads += 1
            self.slow_started.set()
            await asyncio.sleep(5)  # loop read: slow, must be cancelled by EOS
            return DoaReading(angle_deg=0)

    gate = asyncio.Event()
    doa = SlowDoa()
    pipeline = make_pipeline(
        GatedCapture([SILENCE, SPEECH, SPEECH], gate, [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        [],
        doa=doa,
        ack_render_s=0.0,
        listening_cycle_s=0.0,
        doa_timeout_s=10.0,  # long, so only the EOS event can end the slow read
    )
    run = asyncio.create_task(pipeline.run())
    try:
        await wait_until(lambda: doa.slow_started.is_set())  # inside the slow loop read
        gate.set()  # end of speech during the read
        await asyncio.wait_for(run, 2)
    finally:
        gate.set()
    wire = [c.params["chor"] for c in mock_ojn.calls_of("chor")]
    assert build_wake_ack_chor(None, listen_pose=(0, 0)) in wire  # ack did go out
    assert build_listening_chor(None, listen_pose=(0, 0)) not in wire  # but no scanner
    assert wire[-1] == build_leds_off_chor(ears_pose=(0, 0))


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
            lambda: (
                build_leds_off_chor(ears_pose=(0, 0))
                in [c.params["chor"] for c in mock_ojn.calls_of("chor")]
            )
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


class CountdownPlayback:
    """A playback handle that reports 'playing' for its first `n` checks, then
    finished — stands in for the wake beep's TimedPlaybackHandle."""

    def __init__(self, gated_checks: int):
        self._left = gated_checks

    @property
    def finished(self) -> bool:
        if self._left > 0:
            self._left -= 1
            return False
        return True


class BeepGatingController(FakeController):
    """Sets current_playback when a beep (PlayAudioCommand) is submitted, so the
    half-duplex gate suppresses the mic for the next `gated_checks` frames."""

    def __init__(self, gated_checks: int):
        super().__init__()
        self._gated_checks = gated_checks

    async def submit(self, cmd, priority, deadline=None):
        await super().submit(cmd, priority, deadline)
        if isinstance(cmd, PlayAudioCommand):
            self.current_playback = CountdownPlayback(self._gated_checks)


async def test_wake_beep_plays_on_rabbit_and_is_gated_from_stt():
    """Required: the wake beep must not enter VAD/STT (no AEC). It plays on the
    RABBIT via the audio lane (USER_SPEECH_SYNC) and the half-duplex gate drops
    the mic frames while it sounds — no fixed guard tied to the wake instant."""
    stt = FakeSTT()
    controller = BeepGatingController(gated_checks=2)  # beep spans 2 frames
    transcripts = []
    beep = PlayAudioCommand(("http://192.168.66.1:8090/wake.mp3",), 0.12)
    # frame 0 fires wake; the next 2 frames are the beep window (gated); then speech
    pipeline = make_pipeline(
        FakeCapture([SPEECH, SPEECH, SPEECH, SILENCE, SPEECH, SPEECH, SPEECH] + [SILENCE] * 12),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=0)),
        stt=stt,
        wake_beep=beep,
    )
    await pipeline.run()
    # the beep was played on the rabbit, at a priority interrupt() won't drop
    assert (beep, Priority.USER_SPEECH_SYNC) in controller.submitted
    # 3 real SPEECH frames after the 2 gated beep frames reached the STT
    assert stt.pcm.count(SPEECH) == 3
    assert transcripts == ["ciao coniglio"]


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
    assert "wake_to_scanner_stop_enqueued_ms" in pipeline.last_timings.as_dict()


async def test_scanner_stop_metric_tracks_end_of_speech_not_stt():
    """The scanner_stop_enqueued timestamp must be the LEDs-off ENQUEUE at
    end-of-speech, not when STT finishes — with a slow STT the two diverge
    (previously they always coincided because it was set after awaiting STT)."""
    release = asyncio.Event()
    controller = FakeController()
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
    await asyncio.sleep(0.05)  # STT still blocked, but end-of-speech has passed
    release.set()
    await asyncio.wait_for(run, 2)
    t = pipeline.last_timings
    # the stop was enqueued at end-of-speech, comfortably before STT finished
    assert t.scanner_stop_enqueued is not None
    assert t.end_of_speech <= t.scanner_stop_enqueued < t.stt_final
    assert (t.stt_final - t.scanner_stop_enqueued) >= 0.04  # the ~50 ms STT block


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
    # aclose() must cancel/collect the feedback task even if it is mid-loop
    controller = FakeController()
    pipeline = make_pipeline(
        FakeCapture([]), FakeWake(), controller, [], doa=FakeDoa(None), listening_cycle_s=100.0
    )
    eos = asyncio.Event()
    from rabbit_brain.audio.pipeline import WakeTimings

    feedback = pipeline._spawn(
        pipeline._listening_feedback(eos, WakeTimings(wake=0.0)), pipeline._feedback_tasks
    )
    await asyncio.sleep(0.01)  # let it enter the (long) listening wait
    await pipeline.aclose()
    assert feedback.cancelled()
    await asyncio.sleep(0)
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
