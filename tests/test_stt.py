import io
import json
import wave
from collections.abc import AsyncIterator

import pytest
from aiohttp import WSMsgType, web
from rabbit_brain.stt import (
    DeepgramSTT,
    FallbackSTT,
    LocalWhisperSTT,
    STTResult,
    WhisperApiSTT,
    make_stt,
    pcm_to_wav,
)

PCM = b"\x01\x00\x02\x00" * 800  # 1600 samples, 0.1 s @ 16 kHz


async def stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def start_app(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_pcm_to_wav_roundtrip():
    wav = pcm_to_wav(PCM, 16_000)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
        assert w.readframes(w.getnframes()) == PCM


async def test_deepgram_streams_pcm_and_joins_finals():
    received = bytearray()
    seen_query = {}

    async def handler(request: web.Request) -> web.WebSocketResponse:
        seen_query.update(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                received.extend(msg.data)
            elif msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "CloseStream":
                break
        for part, final in (("ciao", True), ("(interim)", False), ("coniglio", True)):
            await ws.send_json(
                {
                    "type": "Results",
                    "is_final": final,
                    "channel": {"alternatives": [{"transcript": part, "languages": ["it"]}]},
                }
            )
        await ws.send_json({"type": "Metadata"})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/v1/listen", handler)
    runner, port = await start_app(app)
    try:
        stt = DeepgramSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v1/listen")
        result = await stt.transcribe(stream([PCM, PCM]), 16_000)
    finally:
        await runner.cleanup()
    # the STT's own language detection rides along (drives TTS voice routing)
    assert result == STTResult(text="ciao coniglio", provider="deepgram", language="it")
    assert bytes(received) == PCM + PCM
    assert seen_query["model"] == "nova-3"
    assert seen_query["language"] == "multi"
    assert seen_query["endpointing"] == "100"  # Deepgram's nova-3 multilingual recommendation
    assert seen_query["encoding"] == "linear16"
    assert seen_query["sample_rate"] == "16000"


def flux_app(events, on_connect=None):
    """Mock Flux V2: consumes audio, then emits the scripted TurnInfo events."""
    seen = {"query": {}, "audio": bytearray()}

    async def handler(request: web.Request) -> web.WebSocketResponse:
        seen["query"].update(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if on_connect is not None:
            await on_connect(ws)
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                seen["audio"].extend(msg.data)
                if len(seen["audio"]) >= len(PCM):  # enough audio: end the turn
                    break
        for event in events:
            await ws.send_json(event)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/v2/listen", handler)
    return app, seen


async def test_flux_reports_end_of_turn_and_transcript():
    from rabbit_brain.stt import FluxSTT

    app, seen = flux_app(
        [
            {"type": "TurnInfo", "event": "StartOfTurn"},
            {"type": "TurnInfo", "event": "Update", "transcript": "ciao"},
            {
                "type": "TurnInfo",
                "event": "EndOfTurn",
                "transcript": "ciao coniglio",
                "audio_window_end": 1.25,
                "end_of_turn_confidence": 0.93,
                "language": "it",
            },
        ]
    )
    runner, port = await start_app(app)
    ended: list[str] = []
    try:
        stt = FluxSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v2/listen")
        result = await stt.transcribe(stream([PCM]), 16_000, on_end_of_turn=ended.append)
    finally:
        await runner.cleanup()
    assert result.text == "ciao coniglio"
    assert result.provider == "deepgram-flux"
    assert result.language == "it"  # drives TTS voice routing
    assert result.audio_cursor_s == 1.25
    assert result.end_of_turn_confidence == 0.93
    # the pipeline is told the turn ended, so LISTENING can stop immediately
    assert ended == ["ciao coniglio"]
    assert seen["query"]["model"] == "flux-general-multi"
    assert seen["query"]["eot_threshold"] == "0.7"
    assert seen["query"]["eot_timeout_ms"] == "5000"
    assert seen["query"]["encoding"] == "linear16"
    assert seen["query"]["sample_rate"] == "16000"


async def test_flux_eager_end_of_turn_is_recorded_but_not_acted_on():
    """Gate L1 ships EndOfTurn only: EagerEndOfTurn is timestamped for
    diagnostics and must NOT end the turn (no speculative dispatch yet)."""
    from rabbit_brain.stt import FluxSTT

    app, _seen = flux_app(
        [
            {"type": "TurnInfo", "event": "EagerEndOfTurn", "transcript": "ciao con"},
            {"type": "TurnInfo", "event": "TurnResumed"},
            {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "ciao coniglio"},
        ]
    )
    runner, port = await start_app(app)
    ended: list[str] = []
    try:
        stt = FluxSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v2/listen")
        result = await stt.transcribe(stream([PCM]), 16_000, on_end_of_turn=ended.append)
    finally:
        await runner.cleanup()
    assert ended == ["ciao coniglio"]  # exactly once, on the real EndOfTurn
    assert result.text == "ciao coniglio"
    assert stt.last_eager_end_of_turn_at is not None  # recorded for the log
    assert "EagerEndOfTurn" in stt.last_turn_events


async def test_flux_language_absent_stays_none():
    # flux-general-multi's language reporting is not hardware-confirmed; when
    # it says nothing we must NOT guess (TTS keeps its configured voice)
    from rabbit_brain.stt import FluxSTT

    app, _ = flux_app([{"type": "TurnInfo", "event": "EndOfTurn", "transcript": "ciao"}])
    runner, port = await start_app(app)
    try:
        stt = FluxSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v2/listen")
        result = await stt.transcribe(stream([PCM]), 16_000)
    finally:
        await runner.cleanup()
    assert result.language is None


async def test_flux_stream_closed_without_end_of_turn_returns_what_it_heard():
    from rabbit_brain.stt import FluxSTT

    app, _ = flux_app([{"type": "TurnInfo", "event": "Update", "transcript": "mezza frase"}])
    runner, port = await start_app(app)
    ended: list[str] = []
    try:
        stt = FluxSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v2/listen")
        result = await stt.transcribe(stream([PCM]), 16_000, on_end_of_turn=ended.append)
    finally:
        await runner.cleanup()
    assert result.text == "mezza frase"
    assert ended == []  # no EndOfTurn was ever reported


async def test_flux_tolerates_unknown_events_and_bad_json():
    from rabbit_brain.stt import FluxSTT

    async def send_garbage(ws):
        await ws.send_str("not json at all")
        await ws.send_json({"type": "SomethingNew", "event": "???"})

    app, _ = flux_app(
        [{"type": "TurnInfo", "event": "EndOfTurn", "transcript": "ok"}], on_connect=send_garbage
    )
    runner, port = await start_app(app)
    try:
        stt = FluxSTT(api_key="k", ws_base=f"ws://127.0.0.1:{port}/v2/listen")
        result = await stt.transcribe(stream([PCM]), 16_000)
    finally:
        await runner.cleanup()
    assert result.text == "ok"


async def test_flux_declares_provider_side_endpointing():
    from rabbit_brain.stt import DeepgramSTT, FluxSTT

    assert FluxSTT(api_key="k").detects_end_of_turn is True
    # nova-3 keeps client-side endpointing (pipeline's local VAD closes it)
    assert getattr(DeepgramSTT(api_key="k"), "detects_end_of_turn", False) is False


async def test_fallback_mirrors_primary_endpointing_and_signals_end_of_turn():
    """With Flux in front the pipeline runs the provider-endpointed loop and
    never closes the stream itself — so when Flux dies, the fallback's result
    must still raise end-of-turn or the pipeline would wait out its timeout."""
    from rabbit_brain.stt import FluxSTT

    class DeadFlux(FluxSTT):
        def __init__(self):
            super().__init__(api_key="k")

        async def transcribe(self, chunks, sample_rate, on_end_of_turn=None):
            async for _ in chunks:  # consume a little, then die
                break
            raise RuntimeError("flux down")

    stt = FallbackSTT(DeadFlux(), CapturingSTT())
    assert stt.detects_end_of_turn is True
    ended: list[str] = []
    result = await stt.transcribe(stream([PCM, PCM]), 16_000, on_end_of_turn=ended.append)
    assert result.text == "fallback ok"
    assert ended == ["fallback ok"]  # the turn was closed for the pipeline


async def test_whisper_api_posts_wav():
    got = {}

    async def handler(request: web.Request) -> web.Response:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file":
                got["wav"] = await part.read()
            elif part.name == "model":
                got["model"] = (await part.read()).decode()
        return web.json_response({"text": " ciao dal coniglio "})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", handler)
    runner, port = await start_app(app)
    try:
        stt = WhisperApiSTT(api_key="k", api_base=f"http://127.0.0.1:{port}/v1")
        result = await stt.transcribe(stream([PCM]), 16_000)
    finally:
        await runner.cleanup()
    assert result.text == "ciao dal coniglio"
    assert got["model"] == "whisper-1"
    with wave.open(io.BytesIO(got["wav"]), "rb") as w:
        assert w.readframes(w.getnframes()) == PCM


class FailingSTT:
    def __init__(self, after_chunks: int):
        self._after = after_chunks

    async def transcribe(self, chunks, sample_rate, on_end_of_turn=None):
        n = 0
        async for _ in chunks:
            n += 1
            if n >= self._after:
                raise RuntimeError("cloud down")
        raise RuntimeError("cloud down")


class CapturingSTT:
    def __init__(self):
        self.pcm = b""

    async def transcribe(self, chunks, sample_rate, on_end_of_turn=None):
        async for c in chunks:
            self.pcm += c
        return STTResult(text="fallback ok", provider="fake")


async def test_fallback_replays_full_utterance():
    # primary dies mid-stream: the fallback must still see every chunk
    capturing = CapturingSTT()
    stt = FallbackSTT(FailingSTT(after_chunks=2), capturing)
    result = await stt.transcribe(stream([b"a" * 512, b"b" * 512, b"c" * 512]), 16_000)
    assert result.text == "fallback ok"
    assert capturing.pcm == b"a" * 512 + b"b" * 512 + b"c" * 512


async def test_fallback_untouched_when_primary_succeeds():
    primary = CapturingSTT()
    stt = FallbackSTT(primary, FailingSTT(after_chunks=1))
    result = await stt.transcribe(stream([PCM]), 16_000)
    assert result.provider == "fake"
    assert primary.pcm == PCM


def test_make_stt_profiles(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cloud = make_stt({"stt_profile": "cloud", "deepgram": {"model": "nova-3"}})
    assert isinstance(cloud, FallbackSTT)
    local = make_stt({"stt_profile": "local", "local_whisper": {"model": "small"}})
    assert isinstance(local, LocalWhisperSTT)
    with pytest.raises(ValueError):
        make_stt({"stt_profile": "telepathy"})
