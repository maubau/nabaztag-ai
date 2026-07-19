"""VoicePipeline flow with fakes: wake → DoA reflex → VAD → streaming STT."""

import asyncio

from rabbit_brain.audio.capture import WavCapture
from rabbit_brain.audio.doa import DoaReading
from rabbit_brain.audio.pipeline import VoicePipeline
from rabbit_brain.audio.vad import VAD_CHUNK_SAMPLES
from rabbit_brain.body.chor import (
    build_leds_off_chor,
    build_listening_scanner_chor,
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


class FakeCapture:
    sample_rate = 16_000

    def __init__(self, blocks):
        self._blocks = blocks

    async def frames(self):
        for block in self._blocks:
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
    def __init__(self):
        self.pcm = b""

    async def transcribe(self, chunks, sample_rate):
        async for c in chunks:
            self.pcm += c
        return STTResult(text="ciao coniglio", provider="fake")


class FakeDoa:
    def __init__(self, reading):
        self._reading = reading

    async def read(self):
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

    return VoicePipeline(
        capture=capture,
        wake=wake,
        probe_factory=lambda: loud_probe,
        stt=kwargs.pop("stt", FakeSTT()),
        controller=controller,
        on_transcript=on_transcript,
        doa=doa,
        doa_moods=DOA_MOODS,
        recorder_kwargs=RECORDER_KWARGS,
        **kwargs,
    )


async def drain_acks(pipeline: VoicePipeline) -> None:
    """Wait for the fire-and-forget wake-ack tasks spawned by _handle_wake."""
    await asyncio.gather(*pipeline._ack_tasks)


def chors(controller) -> list[str]:
    """The chor strings submitted to a FakeController, in order."""
    return [cmd.chor for cmd, _ in controller.submitted if isinstance(cmd, ChorCommand)]


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
    await drain_acks(pipeline)

    assert transcripts == ["ciao coniglio"]
    assert controller.interrupts == 1
    submitted_chors = chors(controller)
    # wake ack (right ear for 90°) as a single non-coalescable choreography —
    # two same-priority EarsCommand would be coalesced, dropping the DoA bias
    assert build_wake_ack_chor("right", listen_pose=(0, 0)) in submitted_chors
    # LISTENING scanner ran, then LEDs off; everything is chor, no EarsCommand
    assert build_listening_scanner_chor() in submitted_chors
    assert build_leds_off_chor() in submitted_chors
    assert not any(isinstance(cmd, EarsCommand) for cmd, _ in controller.submitted)
    assert all(p == Priority.DOA_REFLEX for _, p in controller.submitted)
    # the STT stream carries the utterance (pre-roll + speech + closing silence)
    assert stt.pcm.count(SPEECH) == 6


async def test_doa_fail_open_still_acks():
    # DoA returning None (fail-open) must not block the pipeline: the wake ack
    # still runs, un-sided (both ears twitch)
    blocks = [SILENCE] + [SPEECH] * 4 + [SILENCE] * 10
    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture(blocks), FakeWake(trigger_at=0), controller, transcripts, doa=FakeDoa(None)
    )
    await pipeline.run()
    await drain_acks(pipeline)
    assert transcripts == ["ciao coniglio"]
    assert build_wake_ack_chor(None, listen_pose=(0, 0)) in chors(controller)


async def test_recording_never_waits_for_doa():
    # a hung DoA read (USB stall) must not delay VAD/STT: the ack is
    # fire-and-forget. The LISTENING scanner is independent of DoA and still
    # runs; only the wake-ack chor is missing because its DoA read hangs.
    class HungDoa:
        async def read(self):
            await asyncio.Event().wait()  # never returns

    blocks = [SILENCE] + [SPEECH] * 4 + [SILENCE] * 10
    controller = FakeController()
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture(blocks), FakeWake(trigger_at=0), controller, transcripts, doa=HungDoa()
    )
    await asyncio.wait_for(pipeline.run(), 2)
    assert transcripts == ["ciao coniglio"]  # nothing blocked on DoA
    submitted_chors = chors(controller)
    assert build_listening_scanner_chor() in submitted_chors  # listening ran
    assert build_wake_ack_chor(None) not in submitted_chors  # ack still hung
    await pipeline.aclose()  # collect the hung ack task


async def test_wake_ack_and_listening_cycle_on_real_controller(controller, mock_ojn):
    """Integration through the REAL BodyController + mock OJN — the regression
    class the FakeController missed. Verifies the whole indicator cycle reaches
    the wire as choreography: wake ack → LISTENING scanner → stop (LEDs off),
    with NO posleft/posright ear calls (coalescable + jingle-inducing)."""
    blocks = [SILENCE] + [SPEECH] * 4 + [SILENCE] * 10
    transcripts = []
    pipeline = make_pipeline(
        FakeCapture(blocks),
        FakeWake(trigger_at=0),
        controller,
        transcripts,
        doa=FakeDoa(DoaReading(angle_deg=270)),
    )
    await pipeline.run()
    await drain_acks(pipeline)
    await asyncio.wait_for(controller.wait_idle(), 2)

    assert transcripts == ["ciao coniglio"]
    wire = [c.params["chor"] for c in mock_ojn.calls_of("chor")]
    assert build_wake_ack_chor("left", listen_pose=(0, 0)) in wire  # DoA 270° → left ear
    assert build_listening_scanner_chor() in wire
    # the scanner is stopped by an all-off chor AFTER it started
    assert build_leds_off_chor() in wire
    assert wire.index(build_leds_off_chor()) > wire.index(build_listening_scanner_chor())
    assert mock_ojn.calls_of("ears") == []  # choreography-only, no posleft/posright


async def test_pipeline_close_collects_pending_tasks():
    # aclose() must cancel/collect every background task (acks + indicators),
    # e.g. an indicator still looping or an ack whose DoA read hangs
    class HungDoa:
        async def read(self):
            await asyncio.Event().wait()

    controller = FakeController()
    pipeline = make_pipeline(FakeCapture([]), FakeWake(), controller, [], doa=HungDoa())
    ack = asyncio.create_task(pipeline._wake_ack())
    pipeline._ack_tasks.add(ack)
    ack.add_done_callback(pipeline._ack_tasks.discard)
    indicator = pipeline._spawn_indicator(build_listening_scanner_chor(), 0.01)
    await asyncio.sleep(0)  # let them start
    await pipeline.aclose()
    assert ack.cancelled() and indicator.cancelled()  # both collected, none leaked
    await asyncio.sleep(0)  # let the done-callbacks discard the tasks
    assert pipeline._ack_tasks == set()
    assert pipeline._indicator_tasks == set()


def test_side_of_angle():
    side = VoicePipeline._side_of
    assert side(90) == "right"
    assert side(270) == "left"
    assert side(0) is None  # front: twitch both
    assert side(180) is None  # behind
    assert side(360 + 90) == "right"


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
