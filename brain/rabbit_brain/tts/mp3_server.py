"""Tiny HTTP server exposing TTS MP3s to the rabbit (docs/ARCHITECTURE.md §5).

Binds 0.0.0.0 so the legacy segment can reach it; the URL handed to OJN must
be the one the RABBIT can resolve — i.e. the Bolt's legacy IP, not localhost —
hence the explicit base_url (default http://192.168.66.1:<port>).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)

DEFAULT_PORT = 8090
DEFAULT_RETENTION_S = 3600.0  # generated audio is throwaway; keep an hour for debugging
PURGE_INTERVAL_S = 300.0


class Mp3Server:
    def __init__(
        self,
        audio_dir: Path,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        base_url: str | None = None,
        retention_s: float = DEFAULT_RETENTION_S,
    ):
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._host = host
        self._port = port
        self._base_url = (base_url or f"http://192.168.66.1:{port}").rstrip("/")
        self._retention_s = retention_s
        self._app = web.Application()
        self._app.router.add_static("/", self._audio_dir)
        self._runner: web.AppRunner | None = None
        self._purge_task: asyncio.Task | None = None

    @property
    def audio_dir(self) -> Path:
        return self._audio_dir

    @property
    def port(self) -> int:
        return self._port

    def url_for(self, path: Path) -> str:
        """Rabbit-reachable URL for a file inside audio_dir."""
        rel = Path(path).resolve().relative_to(self._audio_dir.resolve())
        return f"{self._base_url}/{rel}"

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        if self._port == 0:  # ephemeral port (tests): patch base_url accordingly
            self._port = self._runner.addresses[0][1]
            self._base_url = f"http://127.0.0.1:{self._port}"
        self._purge_task = asyncio.create_task(self._purge_loop())
        log.info("mp3 server on %s:%s serving %s", self._host, self._port, self._audio_dir)

    async def stop(self) -> None:
        if self._purge_task:
            self._purge_task.cancel()
        if self._runner:
            await self._runner.cleanup()

    def purge_now(self) -> int:
        """Delete generated audio older than the retention window; returns count."""
        cutoff = time.time() - self._retention_s
        removed = 0
        for pattern in ("*.mp3", "*.wav"):
            for f in self._audio_dir.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:  # raced with a concurrent delete: fine
                    pass
        if removed:
            log.info("purged %d old audio file(s)", removed)
        return removed

    async def _purge_loop(self) -> None:
        while True:
            self.purge_now()
            await asyncio.sleep(PURGE_INTERVAL_S)
