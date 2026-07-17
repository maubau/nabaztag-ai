"""HTTP listener for ojn-plugin-events webhooks → BodyEvent stream.

The OJN daemon (host network) fires GETs like
    /event?bunny=<sn>&event=click&value=single
    /event?bunny=<sn>&event=rfid&value=<tag hex>
This listener maps them to BodyEvents and hands them to a push callable
(normally OjnAdapter.push_event, so BodyAdapter.events() yields them).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from aiohttp import web

from .types import BodyEvent

log = logging.getLogger(__name__)

DEFAULT_PORT = 8091  # 8090 is the MP3 server (config.example.yaml)


class EventListener:
    def __init__(
        self,
        push: Callable[[BodyEvent], None],
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        serial: str | None = None,
    ):
        self._push = push
        self._host = host
        self._port = port
        self._serial = serial.lower() if serial else None
        self._app = web.Application()
        self._app.router.add_get("/event", self._handle)
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/event"

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        log.info("event listener on %s", self.url)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        q = request.query
        if self._serial and q.get("bunny", "").lower() != self._serial:
            return web.Response(status=403, text="unknown bunny")
        event, value = q.get("event", ""), q.get("value", "")
        if event == "click" and value in ("single", "double"):
            body_event = BodyEvent(kind=f"{value}_click", timestamp=time.time())
        elif event == "rfid" and value:
            body_event = BodyEvent(kind="rfid", data=value, timestamp=time.time())
        else:
            return web.Response(status=400, text="bad event")
        log.debug("body event: %s", body_event)
        self._push(body_event)
        return web.Response(text="ok")
