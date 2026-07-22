"""Gate L3 progressive-MP3 probe server (streaming delivery, timing logs)."""

import importlib.util
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

_SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "mp3-progressive-probe.py"


def _load():
    # hyphenated filename: load by path (same pattern as test_config_doctor.py)
    spec = importlib.util.spec_from_file_location("mp3_probe", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mp3_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def test_slow_route_streams_full_bytes_with_audio_mpeg():
    mod = _load()
    data = bytes(range(256)) * 40  # 10240 bytes
    runner, port = await _serve(mod.build_app(data, spread_s=0.2, chunk=1024))
    try:
        async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{port}/slow.mp3") as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "audio/mpeg"
            assert r.headers["Content-Length"] == str(len(data))
            body = await r.read()
    finally:
        await runner.cleanup()
    assert body == data  # every byte delivered, just slowly


async def test_slow_route_actually_dribbles_over_the_spread():
    mod = _load()
    data = b"x" * 8192
    spread = 0.4
    runner, port = await _serve(mod.build_app(data, spread_s=spread, chunk=1024))
    try:
        start = time.monotonic()
        async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{port}/slow.mp3") as r:
            first_at = None
            async for _chunk in r.content.iter_any():
                if first_at is None:
                    first_at = time.monotonic() - start
            total = time.monotonic() - start
    finally:
        await runner.cleanup()
    assert first_at < spread / 2  # first bytes arrive early…
    assert total >= spread * 0.6  # …but the whole body takes ~the spread


async def test_fast_route_sends_in_one_shot():
    mod = _load()
    data = b"y" * 4096
    runner, port = await _serve(mod.build_app(data, spread_s=99.0, chunk=512))
    try:
        start = time.monotonic()
        async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{port}/fast.mp3") as r:
            body = await r.read()
        elapsed = time.monotonic() - start
    finally:
        await runner.cleanup()
    assert body == data
    assert elapsed < 1.0  # /fast ignores the spread entirely
