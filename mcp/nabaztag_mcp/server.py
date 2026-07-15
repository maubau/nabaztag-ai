"""MCP server (stdio) wrapping rabbit-brain's BodyController (§6.5).

All commands enter at AGENT_EXPRESSION priority, so a live voice conversation
(USER_SPEECH_SYNC) still takes precedence over Claude Desktop poking the body.

Configuration (env, or NABAZTAG_MOCK_OJN=1 for the hardware-free mock):
    OJN_BASE_URL     e.g. http://bolt:8080
    RABBIT_SERIAL    the rabbit's serial (sn)
    OJN_VAPI_TOKEN   VAPI token (see docs/OJN_API_NOTES.md §1)
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
from rabbit_brain.body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from rabbit_brain.body.ojn_adapter import OjnAdapter

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
    last_rfid: str | None = None


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
        ctx = RabbitContext(controller=BodyController(adapter))
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
            if mock is not None:
                await mock.stop()


mcp = FastMCP("nabaztag", lifespan=rabbit_lifespan)


def _controller(ctx: Context) -> BodyController:
    return ctx.request_context.lifespan_context.controller


@mcp.tool()
async def speak(text: str, ctx: Context) -> str:
    """Make the rabbit speak a sentence out loud (server-side TTS)."""
    await _controller(ctx).submit(SayCommand(text), Priority.AGENT_EXPRESSION)
    return f"speaking: {text!r}"


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
