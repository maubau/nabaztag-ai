"""MCP server (stdio) wrapping rabbit-brain's BodyController (§6.5).

All commands enter at AGENT_EXPRESSION priority, so a live voice conversation
(USER_SPEECH_SYNC) still takes precedence over Claude Desktop poking the body.

Configuration (env, or NABAZTAG_MOCK_OJN=1 for the hardware-free mock):
    OJN_BASE_URL          http://127.0.0.1 when running on the Bolt — the Apache
                          wrapper on port 80. NEVER port 8080: that is OJN's
                          internal binary framing, not HTTP (OJN_API_NOTES §1)
    RABBIT_SERIAL         the rabbit's serial (sn — the MAC without colons)
    OJN_VAPI_TOKEN        VAPI token (see docs/OJN_API_NOTES.md §1)
    NABAZTAG_EVENTS_PORT  webhook listener port (default 8091; must match the
                          URL set via events/setWebhook)
    TTS_PROFILE           elevenlabs | piper — enables real speech (local synth
                          → Mp3Server → urlList playback). Unset: speak falls
                          back to OJN's dead tts/say
    ELEVENLABS_VOICE_ID / ELEVENLABS_API_KEY / ELEVENLABS_MODEL   (elevenlabs)
    PIPER_MODEL / PIPER_BIN                                       (piper)
    NABAZTAG_AUDIO_DIR    MP3 output dir (default www/audio)
    NABAZTAG_MP3_PORT     MP3 server port (default 8090)
    NABAZTAG_MP3_BASE_URL URL as seen by the RABBIT (default
                          http://192.168.66.1:8090 — never localhost)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP
from rabbit_brain.body import (
    BodyController,
    ChorCommand,
    EarsCommand,
    LedsCommand,
    LedSpec,
    Priority,
    SayCommand,
)
from rabbit_brain.body.chor import build_dance_chor
from rabbit_brain.body.events_server import EventListener
from rabbit_brain.body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from rabbit_brain.body.ojn_adapter import OjnAdapter
from rabbit_brain.tts import Mp3Server, Speaker

# Named choreographies for play_choreography(); VAPI chor strings
# (format: docs/OJN_API_NOTES.md §2). Tune on camera, like moods.yaml.
CHOREOGRAPHIES = {
    "greet": "10,0,motor,0,0,0,0,0,motor,1,0,0,0,30,motor,0,180,0,0,30,motor,1,180,0,0",
    "party": "10,0,led,0,255,0,0,20,led,2,0,255,0,40,led,4,0,0,255,60,led,2,255,0,255",
    "nod": "10,0,motor,0,90,0,0,0,motor,1,90,0,0,40,motor,0,0,0,1,40,motor,1,0,0,1",
}


@dataclass
class RabbitContext:
    controller: BodyController
    speaker: Speaker | None = None
    last_rfid: str | None = None


def _make_tts_provider(audio_dir):
    """TTS_PROFILE=elevenlabs|piper selects the backend; unset → no local TTS
    (speak falls back to OJN's built-in tts/say, whose 2010 backends are dead)."""
    profile = os.environ.get("TTS_PROFILE", "").lower()
    if profile == "elevenlabs":
        from rabbit_brain.tts.elevenlabs_tts import ElevenLabsTTS

        return ElevenLabsTTS(
            audio_dir,
            voice_id=os.environ["ELEVENLABS_VOICE_ID"],
            model=os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2"),
        )
    if profile == "piper":
        from rabbit_brain.tts.piper_tts import PiperTTS

        return PiperTTS(
            audio_dir,
            model_path=os.environ["PIPER_MODEL"],
            piper_bin=os.environ.get("PIPER_BIN", "piper"),
        )
    return None


@asynccontextmanager
async def rabbit_lifespan(_server: FastMCP) -> AsyncIterator[RabbitContext]:
    mock = None
    if os.environ.get("NABAZTAG_MOCK_OJN"):
        mock = MockOjnServer()
        await mock.start()
        base_url, serial, token = mock.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN
    else:
        base_url = os.environ["OJN_BASE_URL"]
        serial = os.environ["RABBIT_SERIAL"]
        token = os.environ["OJN_VAPI_TOKEN"]

    async with OjnAdapter(base_url, serial, token) as adapter:
        # Receives ojn-plugin-events webhooks and feeds adapter.events()
        listener = EventListener(
            adapter.push_event,
            port=int(os.environ.get("NABAZTAG_EVENTS_PORT", "8091")),
            serial=serial,
        )
        await listener.start()

        controller = BodyController(adapter)

        # Local TTS (Phase 2 audio-out): synth → Mp3Server → urlList playback
        mp3_server = None
        speaker = None
        provider = _make_tts_provider(os.environ.get("NABAZTAG_AUDIO_DIR", "www/audio"))
        if provider is not None:
            mp3_server = Mp3Server(
                os.environ.get("NABAZTAG_AUDIO_DIR", "www/audio"),
                port=int(os.environ.get("NABAZTAG_MP3_PORT", "8090")),
                base_url=os.environ.get("NABAZTAG_MP3_BASE_URL"),
            )
            await mp3_server.start()
            speaker = Speaker(controller, provider, mp3_server)

        ctx = RabbitContext(controller=controller, speaker=speaker)
        run_task = asyncio.create_task(ctx.controller.run())

        async def watch_events() -> None:
            async for event in adapter.events():
                if event.kind == "rfid":
                    ctx.last_rfid = event.data

        events_task = asyncio.create_task(watch_events())
        try:
            yield ctx
        finally:
            for task in (run_task, events_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await listener.stop()
            if mp3_server is not None:
                await mp3_server.stop()
            if provider is not None and hasattr(provider, "close"):
                await provider.close()
            if mock is not None:
                await mock.stop()


mcp = FastMCP("nabaztag", lifespan=rabbit_lifespan)


def _controller(ctx: Context) -> BodyController:
    return ctx.request_context.lifespan_context.controller


@mcp.tool()
async def speak(text: str, ctx: Context) -> str:
    """Make the rabbit speak a sentence out loud."""
    speaker = ctx.request_context.lifespan_context.speaker
    if speaker is not None:
        duration = await speaker.speak(text, Priority.AGENT_EXPRESSION)
        return f"speaking ({duration:.1f}s): {text!r}"
    # Fallback: OJN's built-in tts/say — its 2010-era backends are dead, so
    # this likely stays silent. Set TTS_PROFILE=elevenlabs|piper for real audio.
    await _controller(ctx).submit(SayCommand(text), Priority.AGENT_EXPRESSION)
    return f"sent to OJN tts/say (probably silent — no TTS_PROFILE configured): {text!r}"


@mcp.tool()
async def dance_demo(ctx: Context, text: str = "Facciamo festa! Let's dance!") -> str:
    """Speak a line while running a synchronized LED/ear dance choreography."""
    speaker = ctx.request_context.lifespan_context.speaker
    if speaker is not None:
        duration = await speaker.speak(text, Priority.AGENT_EXPRESSION)
    else:
        duration = 8.0  # no TTS configured: dance in silence
    await _controller(ctx).submit(
        ChorCommand(build_dance_chor(duration)), Priority.AGENT_EXPRESSION
    )
    return f"dancing for {duration:.1f}s" + ("" if speaker else " (silent: no TTS_PROFILE)")


@mcp.tool()
async def move_ears(left: int, right: int, ctx: Context) -> str:
    """Move the rabbit's ears. Positions are 0 (fully forward) to 16 (fully back)."""
    await _controller(ctx).submit(EarsCommand(left, right), Priority.AGENT_EXPRESSION)
    return f"ears -> ({left}, {right})"


@mcp.tool()
async def set_leds(leds: dict[str, list[int]], ctx: Context, pulse: bool = False) -> str:
    """Set LED colors. Keys: bottom, left, nose, right, top; values: [r, g, b] 0-255."""
    spec = LedSpec.from_dict({k: tuple(v) for k, v in leds.items()}, pulse=pulse)
    await _controller(ctx).submit(LedsCommand(spec), Priority.AGENT_EXPRESSION)
    return f"leds -> {leds}" + (" (pulsing)" if pulse else "")


@mcp.tool()
async def play_choreography(name: str, ctx: Context) -> str:
    """Run a named body-language choreography. Available: greet, party, nod."""
    if name not in CHOREOGRAPHIES:
        return f"unknown choreography {name!r}; available: {', '.join(CHOREOGRAPHIES)}"
    await _controller(ctx).submit(ChorCommand(CHOREOGRAPHIES[name]), Priority.AGENT_EXPRESSION)
    return f"playing choreography {name!r}"


@mcp.tool()
async def last_rfid(ctx: Context) -> str:
    """Return the last RFID tag the rabbit has seen, if any."""
    tag = ctx.request_context.lifespan_context.last_rfid
    return tag if tag else "no RFID tag seen yet"


@mcp.tool()
async def body_state(ctx: Context) -> str:
    """Current controller-tracked body state (ears, LEDs, playing)."""
    return str(_controller(ctx).snapshot())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
