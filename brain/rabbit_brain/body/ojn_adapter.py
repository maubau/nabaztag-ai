"""OjnAdapter — drives a stock Nabaztag:tag through OpenJabNab.

Only endpoints verified in the OJN source are used (docs/OJN_API_NOTES.md):
  - /ojn/FR/api.jsp        posleft/posright, chor, action=13/14 (VAPI)
  - /ojn/FR/api_stream.jsp urlList=url1|url2 (queued MP3-by-URL)
  - /ojn_api/bunny/<id>/tts/say?text=...   (server-side TTS smoke test)

OJN gives no audio cancel and no playback-finished callback, so playback is
tracked by a duration timer (+ guard) and capabilities.can_cancel_audio=False.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator

import aiohttp

from .types import LED_INDEX, BodyCapabilities, BodyEvent, LedSpec

# VAPI answers signalling failure (bunny.cpp ProcessVioletApiCall)
_VAPI_ERRORS = {
    "NOGOODTOKENORSERIAL",
    "APIDISABLED",
    "EARPOSITIONNOTSENT",
    "CHORNOTSENT",
    "NOCORRECTPARAMETERS",
}

PLAYBACK_GUARD_S = 0.3  # §6.2.7 half-duplex guard
DEFAULT_PLAYBACK_S = 10.0  # conservative fallback when the caller has no duration


class OjnError(RuntimeError):
    pass


class TimedPlaybackHandle:
    """PlaybackHandle for a body with no cancel and no finished-callback:
    wait_finished is a timer derived from the MP3 duration (+ guard)."""

    def __init__(self, duration_s: float | None):
        self._duration = duration_s
        self._started = asyncio.Event()
        self._finished = asyncio.Event()
        self._timer: asyncio.Task | None = None

    def mark_started(self) -> None:
        self._started.set()
        wait = self._duration if self._duration is not None else DEFAULT_PLAYBACK_S
        self._timer = asyncio.create_task(self._run_timer(wait + PLAYBACK_GUARD_S))

    async def _run_timer(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._finished.set()

    async def wait_started(self) -> None:
        await self._started.wait()

    async def wait_finished(self) -> None:
        await self._finished.wait()

    async def cancel(self) -> None:
        raise NotImplementedError("OJN cannot cancel audio (capabilities.can_cancel_audio=False)")

    @property
    def estimated_duration_s(self) -> float | None:
        return self._duration

    @property
    def finished(self) -> bool:
        return self._finished.is_set()


class OjnAdapter:
    """BodyAdapter over a self-hosted OpenJabNab instance."""

    capabilities = BodyCapabilities(
        can_cancel_audio=False,
        has_playback_events=False,
        can_read_body_state=False,
        has_per_led_rgb=True,
    )

    def __init__(
        self,
        base_url: str,
        serial: str,
        vapi_token: str,
        account_token: str = "",
        session: aiohttp.ClientSession | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._sn = serial.lower()
        self._vapi_token = vapi_token
        self._account_token = account_token
        self._session = session
        self._own_session = session is None
        # One command in flight to OJN at a time (§6.4 serialization, enforced
        # at the transport level so every caller path is covered).
        self._http_lock = asyncio.Lock()
        self._events: asyncio.Queue[BodyEvent] = asyncio.Queue()

    async def __aenter__(self) -> OjnAdapter:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    # --- HTTP plumbing -------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> str:
        if self._session is None:
            raise OjnError("adapter not started (use 'async with' or pass a session)")
        async with self._http_lock:
            async with self._session.get(self._base + path, params=params) as resp:
                if resp.status != 200:
                    raise OjnError(f"OJN HTTP {resp.status} on {path}")
                return await resp.text()

    async def _vapi(self, path: str, **params: str) -> str:
        text = await self._get(path, {"sn": self._sn, "token": self._vapi_token, **params})
        for msg in ET.fromstring(text).iter("message"):
            if msg.text in _VAPI_ERRORS:
                raise OjnError(f"OJN VAPI error: {msg.text}")
        return text

    async def _ojn_api(self, path: str, **params: str) -> str:
        if self._account_token:
            params["token"] = self._account_token
        text = await self._get("/ojn_api/" + path, params)
        err = ET.fromstring(text).find("error")
        if err is not None:
            raise OjnError(f"OJN API error: {err.text}")
        return text

    # --- BodyAdapter surface --------------------------------------------

    async def set_ears(self, left: int, right: int) -> None:
        await self._vapi("/ojn/FR/api.jsp", posleft=str(left), posright=str(right))

    async def set_leds(self, spec: LedSpec) -> None:
        await self._vapi("/ojn/FR/api.jsp", chor=led_spec_to_chor(spec))

    async def play_audio(
        self, urls: tuple[str, ...], duration_s: float | None
    ) -> TimedPlaybackHandle:
        handle = TimedPlaybackHandle(duration_s)
        await self._vapi("/ojn/FR/api_stream.jsp", urlList="|".join(urls))
        handle.mark_started()
        return handle

    async def say(self, text: str) -> TimedPlaybackHandle:
        # Server-side TTS: duration unknown, rough words-per-second estimate.
        handle = TimedPlaybackHandle(max(2.0, len(text.split()) / 2.5))
        await self._ojn_api(f"bunny/{self._sn}/tts/say", text=text)
        handle.mark_started()
        return handle

    async def play_chor(self, chor: str) -> None:
        await self._vapi("/ojn/FR/api.jsp", chor=chor)

    async def sleep(self) -> None:
        await self._vapi("/ojn/FR/api.jsp", action="14")

    async def wake(self) -> None:
        await self._vapi("/ojn/FR/api.jsp", action="13")

    def push_event(self, event: BodyEvent) -> None:
        """Called by the event ingress (callurl listener / webhook) — see OJN_API_NOTES §2."""
        self._events.put_nowait(event)

    async def events(self) -> AsyncIterator[BodyEvent]:
        while True:
            yield await self._events.get()


def led_spec_to_chor(spec: LedSpec, tempo_ms: int = 10) -> str:
    """Compile a LedSpec into a minimal VAPI choreography string.

    Format (choregraphy.cpp Parse): tempo,{time,led,<led#>,<r>,<g>,<b>},...
    A pulse is approximated by a second off-frame; the rabbit does not loop it.
    """
    parts = [str(tempo_ms)]
    t = 0
    for name, (r, g, b) in spec.colors:
        parts += [str(t), "led", str(LED_INDEX[name]), str(r), str(g), str(b)]
    if spec.pulse:
        off_t = 50  # ticks after the on-frame
        for name, _ in spec.colors:
            parts += [str(off_t), "led", str(LED_INDEX[name]), "0", "0", "0"]
        on_t = 100
        for name, (r, g, b) in spec.colors:
            parts += [str(on_t), "led", str(LED_INDEX[name]), str(r), str(g), str(b)]
    return ",".join(parts)
