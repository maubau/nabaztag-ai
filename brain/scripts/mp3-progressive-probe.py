#!/usr/bin/env python3
"""Gate L3 probe: does the MTL decoder play an MP3 BEFORE the HTTP response ends?

The whole question for progressive/streaming TTS is whether the Nabaztag's
bootcode decoder starts playing as bytes arrive, or buffers the entire file
first. DeepgramTTS.synth() currently accumulates every chunk, writes the whole
file, applies the ffmpeg gain, and only THEN hands the rabbit a static URL —
so of the ~3-5 s Deepgram HTTP time, the ~2-3.5 s "first byte → last byte"
window is dead air the rabbit could in principle already be playing (hardware
round, July 2026: first byte arrives at ~640-700 ms, headers_to_first_byte ≈ 0).

This server serves ONE known MP3 as `audio/mpeg` but dribbles the body out in
small chunks spread over a controlled duration, and logs exactly when the
connection opened, when the first and last body bytes went out, and when it
closed. It is a PROBE, not production: it imports nothing from rabbit_brain and
changes no runtime path.

    # on the Bolt, pointing at a REAL, several-seconds MP3:
    python brain/scripts/mp3-progressive-probe.py \\
        --mp3 www/audio/some-known-clip.mp3 --spread 8 --chunk 2048 --port 8095

Apache MUST stay in front of the rabbit (a direct aiohttp response was 200 OK
but silent — OJN_API_NOTES #12), so enable ojn/apache/mp3-probe.conf.example to
reverse-proxy /mp3-probe/ → 127.0.0.1:8095, then queue it on the rabbit:

    GET /ojn/FR/api_stream.jsp?sn=<sn>&token=<t>&urlList=http://192.168.66.1/mp3-probe/slow.mp3

Two routes are served for an A/B:
    /slow.mp3  — dribbled over --spread seconds (Content-Length still sent, so
                 the client knows the total size; only delivery is slow)
    /fast.mp3  — the same bytes in one shot (sanity baseline)

What Maurizio reports decides the next step:
  - sound starts right after the FIRST chunk (well before last-byte)  → the
    decoder streams; a progressive TTS path is worth building.
  - sound starts only around last-byte (≈ --spread later)             → MTL
    buffers to EOF; streaming would not help, keep the static-file path.
  - no sound at all                                                    → a
    framing/header incompatibility; compare against the plain static file that
    is known to play, and check whether Apache itself buffered the proxy body
    (the server's own last-byte-sent timestamp vs when audio starts tells the
    two apart).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger("mp3-probe")


async def _serve(
    request: web.Request, data: bytes, spread_s: float, chunk: int
) -> web.StreamResponse:
    peer = request.remote
    t_connect = time.monotonic()
    log.info(
        "connect from %s for %s (%d bytes over %.1fs)", peer, request.path, len(data), spread_s
    )

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/mpeg",
            "Content-Length": str(len(data)),  # client knows the size; only delivery is slow
            "Cache-Control": "no-store",
        },
    )
    await resp.prepare(request)

    n_chunks = max(1, -(-len(data) // chunk))  # ceil
    delay = (spread_s / n_chunks) if spread_s > 0 else 0.0
    t_first: float | None = None
    for i in range(n_chunks):
        block = data[i * chunk : (i + 1) * chunk]
        # StreamResponse.write() flushes to the transport, so each chunk leaves
        # the socket now rather than being coalesced at the end — that is the
        # whole point of the probe.
        await resp.write(block)
        if t_first is None:
            t_first = time.monotonic()
            log.info("first %d bytes sent at +%.0f ms", len(block), (t_first - t_connect) * 1000)
        if delay and i < n_chunks - 1:
            await asyncio.sleep(delay)
    t_last = time.monotonic()
    log.info(
        "last byte sent at +%.0f ms (first→last %.0f ms); closing",
        (t_last - t_connect) * 1000,
        (t_last - (t_first or t_last)) * 1000,
    )
    await resp.write_eof()
    return resp


def build_app(data: bytes, spread_s: float, chunk: int) -> web.Application:
    app = web.Application()

    async def slow(request: web.Request) -> web.StreamResponse:
        return await _serve(request, data, spread_s, chunk)

    async def fast(request: web.Request) -> web.StreamResponse:
        return await _serve(request, data, 0.0, len(data) or 1)

    app.router.add_get("/slow.mp3", slow)
    app.router.add_get("/fast.mp3", fast)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mp3", required=True, help="path to a REAL, several-seconds MP3 to serve")
    parser.add_argument(
        "--spread", type=float, default=8.0, help="seconds to dribble /slow.mp3 over"
    )
    parser.add_argument("--chunk", type=int, default=2048, help="bytes per write")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (Apache proxies to it)")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    data = Path(args.mp3).read_bytes()
    if not data:
        raise SystemExit(f"{args.mp3} is empty")
    log.info(
        "serving %s (%d bytes) on %s:%d — /slow.mp3 over %.1fs, /fast.mp3 in one shot",
        args.mp3,
        len(data),
        args.host,
        args.port,
        args.spread,
    )
    web.run_app(
        build_app(data, args.spread, args.chunk), host=args.host, port=args.port, print=None
    )


if __name__ == "__main__":
    main()
