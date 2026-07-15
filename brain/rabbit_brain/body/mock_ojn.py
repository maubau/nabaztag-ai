"""Mock OpenJabNab server (--mock-ojn): simulates the verified endpoints so the
adapter, controller, and MCP server can be developed and tested with no rabbit.

Faithful to the real surface (docs/OJN_API_NOTES.md): same paths, same params,
same XML answers — including the error answers on a bad sn/token pair.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from aiohttp import web

MOCK_SERIAL = "0013d3849ac0"
MOCK_VAPI_TOKEN = "mock-vapi-token"


@dataclass
class RecordedCall:
    endpoint: str  # "ears" | "chor" | "stream" | "action" | "tts_say"
    params: dict[str, str]
    t: float = field(default_factory=time.monotonic)


class MockOjnServer:
    """In-process OJN stand-in. `calls` is the assertion surface for tests."""

    def __init__(self, latency_s: float = 0.0):
        self.latency_s = latency_s
        self.calls: list[RecordedCall] = []
        self.ears: tuple[int, int] = (0, 0)
        self.sleeping = False
        self._app = web.Application()
        self._app.router.add_get("/ojn/FR/api.jsp", self._vapi)
        self._app.router.add_get("/ojn/FR/api_stream.jsp", self._vapi_stream)
        self._app.router.add_get("/ojn_api/bunny/{bunny}/tts/say", self._tts_say)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def calls_of(self, endpoint: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.endpoint == endpoint]

    # --- handlers ------------------------------------------------------

    @staticmethod
    def _rsp(*messages: tuple[str, str]) -> web.Response:
        body = "".join(f"<message>{m}</message><comment>{c}</comment>" for m, c in messages)
        return web.Response(
            text=f'<?xml version="1.0" encoding="UTF-8"?><rsp>{body}</rsp>',
            content_type="text/xml",
        )

    def _check_auth(self, request: web.Request) -> web.Response | None:
        if (
            request.query.get("sn", "").lower() != MOCK_SERIAL
            or request.query.get("token") != MOCK_VAPI_TOKEN
        ):
            return self._rsp(
                ("NOGOODTOKENORSERIAL", "Your token or serial number are not correct !")
            )
        return None

    async def _vapi(self, request: web.Request) -> web.Response:
        if (denied := self._check_auth(request)) is not None:
            return denied
        await asyncio.sleep(self.latency_s)
        q = request.query
        messages: list[tuple[str, str]] = []
        if "posleft" in q or "posright" in q:
            left, right = int(q.get("posleft", 0)), int(q.get("posright", 0))
            if 0 <= left <= 16 and 0 <= right <= 16:
                self.ears = (left, right)
                params = {"posleft": str(left), "posright": str(right)}
                self.calls.append(RecordedCall("ears", params))
                messages.append(("EARPOSITIONSENT", "Your ears command has been sent"))
            else:
                messages.append(("EARPOSITIONNOTSENT", "Your ears command could not be sent"))
        if "chor" in q:
            chor = q["chor"]
            # same validity rule as Choregraphy::Parse: tempo + groups of 6
            fields = chor.split(",")
            if len(fields) > 1 and (len(fields) - 1) % 6 == 0:
                self.calls.append(RecordedCall("chor", {"chor": chor}))
                messages.append(("CHORSENT", "Your chor has been sent"))
            else:
                messages.append(("CHORNOTSENT", "Your chor could not be sent (bad chor)"))
        if "action" in q:
            self.calls.append(RecordedCall("action", {"action": q["action"]}))
            if q["action"] == "14":
                self.sleeping = True
            elif q["action"] == "13":
                self.sleeping = False
            messages.append(("COMMANDSENT", "You rabbit will change status"))
        if not messages:
            messages.append(("NOCORRECTPARAMETERS", "Please check parameters !"))
        return self._rsp(*messages)

    async def _vapi_stream(self, request: web.Request) -> web.Response:
        if (denied := self._check_auth(request)) is not None:
            return denied
        await asyncio.sleep(self.latency_s)
        if "urlList" not in request.query:
            return self._rsp(("NOCORRECTPARAMETERS", "Please check urlList parameter !"))
        self.calls.append(RecordedCall("stream", {"urlList": request.query["urlList"]}))
        return self._rsp(("WEBRADIOSENT", "Your webradio has been sent"))

    async def _tts_say(self, request: web.Request) -> web.Response:
        await asyncio.sleep(self.latency_s)
        text = request.query.get("text", "")
        self.calls.append(RecordedCall("tts_say", {"text": text}))
        return web.Response(
            text='<?xml version="1.0" encoding="UTF-8"?><api><ok>Sending to bunny</ok></api>',
            content_type="text/xml",
        )
