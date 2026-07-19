import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from rabbit_brain.body.chor import build_dance_chor
from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.types import PlayAudioCommand, Priority
from rabbit_brain.tts import Mp3Server, Speaker, TTSResult, split_sentences


@dataclass
class FakeTTS:
    audio_dir: Path
    seconds_per_synth: float = 1.5
    synths: list[str] = field(default_factory=list)

    async def synth(self, text: str) -> TTSResult:
        self.synths.append(text)
        path = self.audio_dir / f"utt{len(self.synths)}.mp3"
        path.write_bytes(b"fake-mp3")
        return TTSResult(path=path, duration_s=self.seconds_per_synth)


class RecordingController:
    """Just the submit surface Speaker needs."""

    def __init__(self):
        self.submitted = []

    async def submit(self, cmd, priority, deadline=None):
        self.submitted.append((cmd, priority))


def test_split_sentences():
    assert split_sentences("Ciao. Come stai? Bene!") == ["Ciao.", "Come stai?", "Bene!"]
    assert split_sentences("Una sola frase") == ["Una sola frase"]
    assert split_sentences("  ") == []


def test_build_dance_chor_is_valid_and_scales():
    for duration in (1.0, 5.0, 20.0):
        chor = build_dance_chor(duration)
        fields = chor.split(",")
        # Choregraphy::Parse validity: tempo + groups of 6
        assert (len(fields) - 1) % 6 == 0
        tempo = int(fields[0])
        assert 10 <= tempo <= 2550
        last_time = max(int(fields[i]) for i in range(1, len(fields), 6))
        assert last_time >= duration * 1000 / tempo * 0.9  # spans the duration
    assert len(build_dance_chor(20.0)) > len(build_dance_chor(2.0))


async def test_mp3_server_serves_files_and_builds_urls(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        mp3 = tmp_path / "hello.mp3"
        mp3.write_bytes(b"fake-mp3-bytes")
        url = server.url_for(mp3)
        async with aiohttp.ClientSession() as session, session.get(url) as resp:
            assert resp.status == 200
            assert await resp.read() == b"fake-mp3-bytes"
    finally:
        await server.stop()


async def test_speaker_short_text_single_mp3(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        tts = FakeTTS(tmp_path)
        controller = RecordingController()
        speaker = Speaker(controller, tts, server)
        total = await speaker.speak("Ciao dal coniglio. Tutto bene?")  # short → no split
        assert tts.synths == ["Ciao dal coniglio. Tutto bene?"]
        cmd, priority = controller.submitted[0]
        assert isinstance(cmd, PlayAudioCommand)
        assert len(cmd.urls) == 1
        assert cmd.duration_s == total == 1.5
        assert priority == Priority.USER_SPEECH_SYNC
    finally:
        await server.stop()


async def test_speaker_long_text_splits_into_sentence_queue(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        tts = FakeTTS(tmp_path)
        controller = RecordingController()
        speaker = Speaker(controller, tts, server)
        long_text = ("Questa è una frase piuttosto lunga che parla del coniglio. " * 4) + (
            "E questa è la chiusura!"
        )
        total = await speaker.speak(long_text, Priority.AGENT_EXPRESSION)
        assert len(tts.synths) > 1
        assert total == 1.5 * len(tts.synths)
        # time-to-first-audio: the first sentence goes out alone, immediately;
        # the rest follows as one urlList batch synthesized while it plays
        assert len(controller.submitted) == 2
        first_cmd, first_priority = controller.submitted[0]
        rest_cmd, _ = controller.submitted[1]
        assert len(first_cmd.urls) == 1
        assert first_cmd.duration_s == 1.5
        assert len(rest_cmd.urls) == len(tts.synths) - 1
        assert first_priority == Priority.AGENT_EXPRESSION
    finally:
        await server.stop()


async def test_speaker_through_real_controller_hits_ojn_stream(controller, mock_ojn, tmp_path):
    """Full path: Speaker → BodyController → OjnAdapter → mock OJN api_stream."""
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        speaker = Speaker(controller, FakeTTS(tmp_path), server)
        await speaker.speak("Ciao!")
        await asyncio.wait_for(controller.wait_idle(), 2)
        assert len(mock_ojn.calls_of("stream")) == 1
        assert "utt1.mp3" in mock_ojn.calls_of("stream")[0].params["urlList"]
    finally:
        await server.stop()


def test_build_wake_ack_chor_short_and_valid():
    from rabbit_brain.body.chor import build_wake_ack_chor

    for side in ("left", "right", None):
        chor = build_wake_ack_chor(side, listen_pose=(0, 0))
        fields = chor.split(",")
        assert (len(fields) - 1) % 6 == 0  # Choregraphy::Parse validity
        tempo = int(fields[0])
        last_tick = max(int(fields[i]) for i in range(1, len(fields), 6))
        assert 300 <= last_tick * tempo <= 500  # UX: one short non-blocking ack
        # ends in the listening pose (0° = position 0) for both ears
        for ear in ("0", "1"):
            assert f",motor,{ear},0,0,1" in chor
    # sided ack twitches only that ear at t0 (motor 0=left, 1=right)
    assert "0,motor,0,45,0,0" in build_wake_ack_chor("left", listen_pose=(0, 0))
    assert "0,motor,0,45" not in build_wake_ack_chor("right", listen_pose=(0, 0))


def test_build_dance_chor_capped():
    from rabbit_brain.body.chor import MAX_DANCE_S

    assert build_dance_chor(10_000.0) == build_dance_chor(MAX_DANCE_S)


async def test_mp3_server_purges_old_files(tmp_path):
    import os
    import time

    server = Mp3Server(tmp_path, host="127.0.0.1", port=0, retention_s=60)
    old, fresh = tmp_path / "old.mp3", tmp_path / "fresh.mp3"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    assert server.purge_now() == 1
    assert not old.exists()
    assert fresh.exists()


def test_recording_controller_matches_bodycontroller_surface():
    # Speaker only calls submit(cmd, priority); ensure the real controller has it
    assert callable(BodyController.submit)
