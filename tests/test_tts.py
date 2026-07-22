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

    async def synth(self, text: str, language: str | None = None) -> TTSResult:
        self.synths.append(text)
        self.languages = [*getattr(self, "languages", []), language]
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


def _chor_is_valid(chor: str) -> tuple[int, int]:
    """Choregraphy::Parse validity → (tempo_ms, last_tick)."""
    fields = chor.split(",")
    assert (len(fields) - 1) % 6 == 0
    tempo = int(fields[0])
    assert 10 <= tempo <= 2550
    last_tick = max(int(fields[i]) for i in range(1, len(fields), 6))
    return tempo, last_tick


def test_build_wake_ack_chor_short_and_valid():
    from rabbit_brain.body.chor import build_wake_ack_chor

    for side in ("left", "right", None):
        chor = build_wake_ack_chor(side, listen_pose=(0, 0))
        tempo, last_tick = _chor_is_valid(chor)
        assert 300 <= last_tick * tempo <= 600  # UX: one short non-blocking ack
        # all 5 LEDs turn green at t0
        for led in range(5):
            assert f"0,led,{led},0,255,0" in chor
        # both ears face forward immediately (0° = position 0)
        for ear in ("0", "1"):
            assert f"0,motor,{ear},0,0,1" in chor
    # DoA side never changes this global state acknowledgement.
    assert build_wake_ack_chor("left") == build_wake_ack_chor("right")
    assert "0,motor,0,36,0,1" in build_wake_ack_chor("left", listen_pose=(2, 2))


def test_listening_scanner_and_indicators_valid():
    from rabbit_brain.body.chor import (
        build_leds_off_chor,
        build_listening_chor,
        build_processing_chor,
    )

    scanner = build_listening_chor()
    tempo, last_tick = _chor_is_valid(scanner)
    assert 1700 <= tempo * last_tick <= 1800
    # all five LEDs, including the base LED 0, breathe magenta together
    for led in range(5):
        assert f"5,led,{led},255,0,255" in scanner
        assert f"11,led,{led},0,0,0" in scanner
    # Both ears traverse the full supported range with opposite directions.
    right = build_listening_chor("right", listen_pose=(0, 0))
    _chor_is_valid(right)
    assert "0,motor,0,288,0,0" in right
    assert "0,motor,1,288,0,1" in right
    assert "6,motor,0,0,0,1" in right
    assert "6,motor,1,0,0,0" in right

    processing = build_processing_chor()
    _chor_is_valid(processing)
    for led in range(5):
        assert f"0,led,{led},255,140,0" in processing  # all LEDs on, orange

    off = build_leds_off_chor()
    _chor_is_valid(off)
    for led in range(5):
        assert f"0,led,{led},0,0,0" in off  # every LED off
    stopped = build_leds_off_chor(ears_pose=(2, 3))
    assert "0,motor,0,36,0,1" in stopped
    assert "0,motor,1,54,0,1" in stopped


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


def test_mp3_server_keeps_protected_static_assets(tmp_path):
    import os
    import time

    # a static wake beep lives alongside throwaway TTS output; retention must
    # purge the expired TTS but keep the protected asset
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0, retention_s=60, protected={"wake.mp3"})
    tts, beep = tmp_path / "utt-old.mp3", tmp_path / "wake.mp3"
    tts.write_bytes(b"x")
    beep.write_bytes(b"x")
    stale = time.time() - 3600
    os.utime(tts, (stale, stale))
    os.utime(beep, (stale, stale))  # the beep is old too, but protected
    assert server.purge_now() == 1
    assert not tts.exists()
    assert beep.exists()


def test_recording_controller_matches_bodycontroller_surface():
    # Speaker only calls submit(cmd, priority); ensure the real controller has it
    assert callable(BodyController.submit)


async def test_mp3_server_storage_only_mode(tmp_path):
    """serve_http=False: no HTTP listener (Apache delivers via alias — the MTL
    decoder ignores aiohttp-served audio), but storage/URLs/purge still work."""
    server = Mp3Server(
        tmp_path, port=8090, base_url="http://192.168.66.1/brain-audio", serve_http=False
    )
    await server.start()
    try:
        assert server._runner is None  # nothing bound
        mp3 = tmp_path / "utt.mp3"
        mp3.write_bytes(b"x")
        assert server.url_for(mp3) == "http://192.168.66.1/brain-audio/utt.mp3"
        assert server._purge_task is not None  # retention still owned here
    finally:
        await server.stop()


async def test_speaker_reports_checkpoints_in_order(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        tts = FakeTTS(tmp_path)
        controller = RecordingController()
        speaker = Speaker(controller, tts, server)
        seen = []
        long_text = ("Questa è una frase piuttosto lunga sul coniglio. " * 4) + "Fine!"
        await speaker.speak(long_text, on_checkpoint=seen.append)
        assert seen == [
            "tts_start",
            "tts_first_chunk_ready",
            "first_chunk_submitted",
            "tts_complete",
            "all_submitted",
        ]
    finally:
        await server.stop()


async def test_speaker_checkpoints_single_chunk(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        tts = FakeTTS(tmp_path)
        controller = RecordingController()
        speaker = Speaker(controller, tts, server)
        seen = []
        await speaker.speak("Ciao!", on_checkpoint=seen.append)
        assert seen == [
            "tts_start",
            "tts_first_chunk_ready",
            "first_chunk_submitted",
            "tts_complete",
            "all_submitted",
        ]
    finally:
        await server.stop()


async def test_speaker_routes_language_to_provider(tmp_path):
    server = Mp3Server(tmp_path, host="127.0.0.1", port=0)
    await server.start()
    try:
        tts = FakeTTS(tmp_path)
        controller = RecordingController()
        speaker = Speaker(controller, tts, server)
        await speaker.speak("Hello there!", language="en")
        assert tts.languages == ["en"]
    finally:
        await server.stop()


async def test_deepgram_tts_voice_routing_and_request(tmp_path, monkeypatch):
    from aiohttp import web
    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    got = []

    async def handler(request: web.Request) -> web.Response:
        got.append(
            {
                "model": request.query.get("model"),
                "auth": request.headers.get("Authorization"),
                "text": (await request.json())["text"],
            }
        )
        return web.Response(body=b"fake-mp3", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_post("/v1/speak", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    monkeypatch.setattr(DeepgramTTS, "_mp3_duration", staticmethod(lambda _p: 0.5))
    try:
        async with DeepgramTTS(
            tmp_path, api_key="k", api_base=f"http://127.0.0.1:{port}/v1/speak"
        ) as tts:
            # italian by STT-detected language
            r_it = await tts.synth("Ciao dal coniglio", language="it")
            # english, including region tags
            await tts.synth("Hello!", language="en-US")
            # no language → default (italian)
            await tts.synth("Boh")
            assert r_it.path.exists() and r_it.duration_s == 0.5
    finally:
        await runner.cleanup()

    assert [g["model"] for g in got] == ["aura-2-livia-it", "aura-2-thalia-en", "aura-2-livia-it"]
    assert got[0]["auth"] == "Token k"
    assert got[0]["text"] == "Ciao dal coniglio"


async def test_elevenlabs_logs_chars_time_and_duration(tmp_path, monkeypatch, caplog):
    """ElevenLabs must log the same chars/total-time/duration breakdown as
    Deepgram, so the two are comparable in the latency benchmark (never the
    text content)."""
    import logging

    from aiohttp import web
    from rabbit_brain.tts.elevenlabs_tts import ElevenLabsTTS

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=b"fake-mp3", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_post("/v1/text-to-speech/{voice}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    duration_stub = type("I", (), {"info": type("X", (), {"length": 1.5})})()
    monkeypatch.setattr("rabbit_brain.tts.elevenlabs_tts.MP3", lambda _p: duration_stub)
    # API_BASE is read by name inside synth(), so patching the module attr routes
    # the request to the local server
    monkeypatch.setattr("rabbit_brain.tts.elevenlabs_tts.API_BASE", f"http://127.0.0.1:{port}/v1")
    tts = ElevenLabsTTS(tmp_path, voice_id="v", api_key="k")
    try:
        with caplog.at_level(logging.INFO):
            await tts.synth("Ciao dal coniglio")
    finally:
        await tts.close()
        await runner.cleanup()
    line = next(r.message for r in caplog.records if "elevenlabs tts timing" in r.message)
    assert "chars=17" in line
    assert "total_http_ms=" in line
    assert "mp3_duration_s=1.50" in line
    assert "Ciao dal coniglio" not in line  # never logs content


def test_make_tts_provider_deepgram_profile(tmp_path):
    from rabbit_brain.tts import make_tts_provider
    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    env = {
        "TTS_PROFILE": "deepgram",
        "DEEPGRAM_API_KEY": "k",
        "DEEPGRAM_TTS_VOICE_EN": "aura-2-orion-en",
    }
    prov = make_tts_provider(tmp_path, env=env)
    assert isinstance(prov, DeepgramTTS)
    assert prov.voice_for("en") == "aura-2-orion-en"
    assert prov.voice_for("it") == "aura-2-livia-it"
    assert prov._gain_db == 0.0  # off unless DEEPGRAM_TTS_GAIN_DB is set


def test_make_tts_provider_deepgram_gain_from_env(tmp_path):
    from rabbit_brain.tts import make_tts_provider

    env = {"TTS_PROFILE": "deepgram", "DEEPGRAM_API_KEY": "k", "DEEPGRAM_TTS_GAIN_DB": "6"}
    prov = make_tts_provider(tmp_path, env=env)
    assert prov._gain_db == 6.0


def _wav_bytes(seconds: float = 0.5, rate: int = 22_050) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_piper_routes_by_language():
    from rabbit_brain.tts.piper_tts import PiperTTS

    p = PiperTTS(Path("/tmp"), url_it="http://it:5001/", url_en="http://en:5002/")
    assert p.url_for("it") == "http://it:5001"
    assert p.url_for("en") == "http://en:5002"
    assert p.url_for("en-US") == "http://en:5002"  # region tag tolerated
    assert p.url_for(None) == "http://it:5001"  # default language


def test_piper_length_scale_routes_by_language():
    from rabbit_brain.tts.piper_tts import PiperTTS

    p = PiperTTS(
        Path("/tmp"),
        url_it="http://it:5001",
        url_en="http://en:5002",
        length_scale_it=1.25,
        length_scale_en=1.0,
    )
    assert p.length_scale_for("it") == 1.25
    assert p.length_scale_for("en-GB") == 1.0  # region tag tolerated
    assert p.length_scale_for(None) == 1.25  # default language
    # unset → None so the server keeps its own default pace
    bare = PiperTTS(Path("/tmp"), url_it="http://it:5001", url_en="http://en:5002")
    assert bare.length_scale_for("it") is None


async def test_piper_missing_language_server_raises():
    # bilingual is the requirement: a language with no server must NOT silently
    # reuse the other voice
    from rabbit_brain.tts.piper_tts import PiperTTS

    p = PiperTTS(Path("/tmp"), url_it="http://it:5001", url_en=None)
    import pytest

    with pytest.raises(RuntimeError, match="no Piper server"):
        await p.synth("hello", language="en")


async def test_piper_synth_hits_language_server_and_transcodes(tmp_path, monkeypatch):
    from aiohttp import web
    from rabbit_brain.tts.piper_tts import PiperTTS

    seen = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.path, (await request.json())["text"]))
        return web.Response(body=_wav_bytes(0.5), content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    # don't shell out to ffmpeg in CI; just write the mp3 stand-in
    async def fake_transcode(self, wav, mp3_path):
        Path(mp3_path).write_bytes(b"mp3")  # noqa: ASYNC240 — test stub, no real I/O contention

    monkeypatch.setattr(PiperTTS, "_transcode", fake_transcode)
    try:
        async with PiperTTS(
            tmp_path,
            url_it=f"http://127.0.0.1:{port}",
            url_en=f"http://127.0.0.1:{port}",
        ) as p:
            result = await p.synth("Ciao dal coniglio", language="it")
    finally:
        await runner.cleanup()
    assert result.path.exists()
    assert abs(result.duration_s - 0.5) < 0.01  # duration from the WAV
    assert result.provider == "piper"  # a real Piper result is tagged piper
    assert seen[0][1] == "Ciao dal coniglio"  # text reached the server


async def test_piper_sends_per_language_length_scale_in_body(tmp_path, monkeypatch):
    from aiohttp import web
    from rabbit_brain.tts.piper_tts import PiperTTS

    bodies = []

    async def handler(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        return web.Response(body=_wav_bytes(0.3), content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def fake_transcode(self, wav, mp3_path):
        Path(mp3_path).write_bytes(b"mp3")  # noqa: ASYNC240 — test stub, no real I/O contention

    monkeypatch.setattr(PiperTTS, "_transcode", fake_transcode)
    try:
        async with PiperTTS(
            tmp_path,
            url_it=f"http://127.0.0.1:{port}",
            url_en=f"http://127.0.0.1:{port}",
            length_scale_it=1.25,
            length_scale_en=1.0,
        ) as p:
            await p.synth("Ciao", language="it")
            await p.synth("Hello", language="en")
    finally:
        await runner.cleanup()
    assert bodies[0]["length_scale"] == 1.25  # IT paced per config
    assert bodies[1]["length_scale"] == 1.0  # EN paced per config


async def test_piper_omits_length_scale_when_unset(tmp_path, monkeypatch):
    from aiohttp import web
    from rabbit_brain.tts.piper_tts import PiperTTS

    bodies = []

    async def handler(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        return web.Response(body=_wav_bytes(0.3), content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def fake_transcode(self, wav, mp3_path):
        Path(mp3_path).write_bytes(b"mp3")  # noqa: ASYNC240 — test stub, no real I/O contention

    monkeypatch.setattr(PiperTTS, "_transcode", fake_transcode)
    try:
        async with PiperTTS(
            tmp_path,
            url_it=f"http://127.0.0.1:{port}",
            url_en=f"http://127.0.0.1:{port}",
        ) as p:
            await p.synth("Ciao", language="it")
    finally:
        await runner.cleanup()
    assert "length_scale" not in bodies[0]  # unset → server keeps its default


async def test_piper_falls_back_on_error_keeping_the_fallback_tag(tmp_path):
    from rabbit_brain.tts.piper_tts import PiperTTS

    class FakeFallback:
        def __init__(self):
            self.called = []

        async def synth(self, text, language=None):
            self.called.append((text, language))
            path = tmp_path / "fb.mp3"
            path.write_bytes(b"x")
            return TTSResult(path=path, duration_s=1.0, provider="deepgram")

    fb = FakeFallback()
    # unreachable server → synth raises internally → fallback runs
    p = PiperTTS(
        tmp_path,
        url_it="http://127.0.0.1:1/",
        url_en="http://127.0.0.1:1/",
        fallback=fb,
        timeout_s=0.2,
    )
    result = await p.synth("Ciao", language="it")
    assert result.duration_s == 1.0
    assert fb.called == [("Ciao", "it")]  # language forwarded to the fallback
    # crucial: the fallback clip is tagged "deepgram", NOT "piper", so a caller
    # (tts-bench) can never credit it to Piper
    assert result.provider == "deepgram"
    await p.close()


def _piper_env(**extra):
    return {
        "TTS_PROFILE": "piper",
        "PIPER_URL_IT": "http://127.0.0.1:5001",
        "PIPER_URL_EN": "http://127.0.0.1:5002",
        **extra,
    }


def test_make_tts_provider_piper_builds_http_client_with_fallback(tmp_path):
    from rabbit_brain.tts import make_tts_provider
    from rabbit_brain.tts.piper_tts import PiperTTS

    prov = make_tts_provider(tmp_path, env=_piper_env(DEEPGRAM_API_KEY="k"))
    assert isinstance(prov, PiperTTS)
    assert prov.url_for("en") == "http://127.0.0.1:5002"
    assert prov._fallback is not None  # degrades to Deepgram at runtime


def test_make_tts_provider_piper_fallback_disabled_by_flag(tmp_path):
    # the benchmark sets PIPER_FALLBACK_DEEPGRAM=0 so a Piper failure never
    # comes back as a silent Deepgram clip
    from rabbit_brain.tts import make_tts_provider

    prov = make_tts_provider(
        tmp_path, env=_piper_env(DEEPGRAM_API_KEY="k", PIPER_FALLBACK_DEEPGRAM="0")
    )
    assert prov._fallback is None


def test_make_tts_provider_piper_fallback_inherits_gain(tmp_path):
    # the Deepgram fallback must carry DEEPGRAM_TTS_GAIN_DB, or a fallback
    # utterance would suddenly be quieter than the boosted production voice
    from rabbit_brain.tts import make_tts_provider

    prov = make_tts_provider(
        tmp_path, env=_piper_env(DEEPGRAM_API_KEY="k", DEEPGRAM_TTS_GAIN_DB="6")
    )
    assert prov._fallback._gain_db == 6.0


def test_make_tts_provider_piper_requires_both_urls(tmp_path):
    import pytest
    from rabbit_brain.tts import make_tts_provider

    with pytest.raises(KeyError):  # missing PIPER_URL_EN → bench skips cleanly
        make_tts_provider(tmp_path, env={"TTS_PROFILE": "piper", "PIPER_URL_IT": "http://x:1"})


def test_make_tts_provider_piper_parses_length_scales(tmp_path):
    from rabbit_brain.tts import make_tts_provider

    prov = make_tts_provider(
        tmp_path, env=_piper_env(PIPER_LENGTH_SCALE_IT="1.25", PIPER_LENGTH_SCALE_EN="1.0")
    )
    assert prov.length_scale_for("it") == 1.25
    assert prov.length_scale_for("en") == 1.0


def test_make_tts_provider_piper_length_scale_defaults_to_none(tmp_path):
    from rabbit_brain.tts import make_tts_provider

    prov = make_tts_provider(tmp_path, env=_piper_env())
    assert prov.length_scale_for("it") is None  # unset → server default pace


def test_make_tts_provider_piper_rejects_bad_length_scale(tmp_path):
    import pytest
    from rabbit_brain.tts import make_tts_provider

    with pytest.raises(ValueError, match="must be > 0"):
        make_tts_provider(tmp_path, env=_piper_env(PIPER_LENGTH_SCALE_IT="0"))
    with pytest.raises(ValueError, match="positive float"):
        make_tts_provider(tmp_path, env=_piper_env(PIPER_LENGTH_SCALE_EN="fast"))


def test_tts_result_carries_provider_tag(tmp_path):
    # each provider labels its result so a fallback can never be mistaken for
    # the primary (tts-bench relies on this)
    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    r = TTSResult(path=tmp_path / "x.mp3", duration_s=1.0, provider="deepgram")
    assert r.provider == "deepgram"
    # the provider default is None (back-compat for fakes / older callers)
    assert TTSResult(path=tmp_path / "y.mp3", duration_s=1.0).provider is None
    assert DeepgramTTS  # imported for coverage of the module path


async def test_deepgram_tts_synth_skips_gain_when_zero(tmp_path, monkeypatch):
    import asyncio

    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    async def boom(self, path):
        raise AssertionError("ffmpeg must not run when gain_db is 0")

    monkeypatch.setattr(DeepgramTTS, "_apply_gain", boom)
    monkeypatch.setattr(DeepgramTTS, "_mp3_duration", staticmethod(lambda _p: 0.5))

    from aiohttp import web

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=b"fake-mp3", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_post("/v1/speak", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with DeepgramTTS(
            tmp_path, api_key="k", api_base=f"http://127.0.0.1:{port}/v1/speak", gain_db=0.0
        ) as tts:
            await tts.synth("Ciao")  # must not raise (would if _apply_gain ran)
    finally:
        await runner.cleanup()
    await asyncio.sleep(0)  # let any stray task settle


async def test_deepgram_tts_applies_gain_via_ffmpeg(tmp_path, monkeypatch):
    import asyncio

    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    calls = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            (tmp_path / "x.gain.mp3").write_bytes(b"boosted")
            return b"", b""

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tts = DeepgramTTS(tmp_path, api_key="k", gain_db=6.0)
    path = tmp_path / "x.mp3"
    path.write_bytes(b"fake")
    boosted = await tts._apply_gain(path)
    assert boosted.name == "x.gain.mp3"
    assert not path.exists()  # original replaced
    filter_arg = next(a for a in calls[0] if isinstance(a, str) and a.startswith("volume="))
    assert filter_arg.startswith("volume=6.0dB,")
    assert "alimiter" in filter_arg  # peak limiter, so higher gain can't clip


async def test_deepgram_tts_gain_falls_back_on_ffmpeg_failure(tmp_path, monkeypatch):
    import asyncio

    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    class FailingProc:
        returncode = 1

        async def communicate(self):
            return b"", b"ffmpeg: error"

    async def fake_exec(*args, **kwargs):
        return FailingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tts = DeepgramTTS(tmp_path, api_key="k", gain_db=6.0)
    path = tmp_path / "x.mp3"
    path.write_bytes(b"fake")
    result = await tts._apply_gain(path)
    assert result == path  # unmodified file kept
    assert path.exists()


async def test_deepgram_tts_gain_falls_back_when_ffmpeg_missing(tmp_path, monkeypatch):
    import asyncio

    from rabbit_brain.tts.deepgram_tts import DeepgramTTS

    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tts = DeepgramTTS(tmp_path, api_key="k", gain_db=6.0)
    path = tmp_path / "x.mp3"
    path.write_bytes(b"fake")
    result = await tts._apply_gain(path)
    assert result == path
    assert path.exists()


def test_make_tts_provider_profiles(monkeypatch, tmp_path):
    from rabbit_brain.tts import make_tts_provider

    # no TTS_PROFILE → None (no local speech; keys never touched)
    assert make_tts_provider(tmp_path, env={}) is None
    # elevenlabs profile builds a provider from env only
    prov = make_tts_provider(
        tmp_path,
        env={"TTS_PROFILE": "elevenlabs", "ELEVENLABS_VOICE_ID": "v", "ELEVENLABS_API_KEY": "k"},
    )
    assert prov is not None


async def test_build_speech_stack_without_profile(tmp_path):
    from rabbit_brain.tts import build_speech_stack

    stack = await build_speech_stack(RecordingController(), env={})
    assert stack.speaker is None and stack.mp3_server is None
    await stack.aclose()  # no-op, must not raise
