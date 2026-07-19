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
                    "channel": {"alternatives": [{"transcript": part}]},
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
    assert result == STTResult(text="ciao coniglio", provider="deepgram")
    assert bytes(received) == PCM + PCM
    assert seen_query["model"] == "nova-3"
    assert seen_query["language"] == "multi"
    assert seen_query["endpointing"] == "100"  # Deepgram's nova-3 multilingual recommendation
    assert seen_query["encoding"] == "linear16"
    assert seen_query["sample_rate"] == "16000"


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

    async def transcribe(self, chunks, sample_rate):
        n = 0
        async for _ in chunks:
            n += 1
            if n >= self._after:
                raise RuntimeError("cloud down")
        raise RuntimeError("cloud down")


class CapturingSTT:
    def __init__(self):
        self.pcm = b""

    async def transcribe(self, chunks, sample_rate):
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
