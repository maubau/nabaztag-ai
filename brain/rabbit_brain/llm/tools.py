"""Body tools exposed to the agent (§6.3), choreography-only.

Every tool goes through the BodyController at AGENT_EXPRESSION priority so a
live user utterance (USER_SPEECH_SYNC) still preempts it. Ear movement is
compiled to a MOTOR CHOREOGRAPHY (ChorCommand), never posleft/posright — that
path triggers the firmware jingle (docs/OJN_API_NOTES #7). The model may NOT
emit raw choreography; it picks validated presets and bounded parameters.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..body.chor import build_gesture_ears_chor
from ..body.controller import BodyController
from ..body.types import EAR_MAX, EAR_MIN, ChorCommand, LedsCommand, LedSpec, Priority
from .base import ToolCall, ToolResult, ToolSpec

log = logging.getLogger(__name__)

# Safe, named body-language choreographies (validated chor strings). The model
# picks by name; it cannot author arbitrary sequences.
GESTURE_PRESETS: dict[str, str] = {
    "nod": "10,0,motor,0,90,0,0,0,motor,1,90,0,0,40,motor,0,0,0,1,40,motor,1,0,0,1",
    "perk_up": "10,0,motor,0,0,0,0,0,motor,1,0,0,0",
    "tilt": "10,0,motor,0,54,0,0,0,motor,1,0,0,0",
    "wiggle": "8,0,motor,0,72,0,0,0,motor,1,72,0,1,20,motor,0,72,0,1,20,motor,1,72,0,0",
}

# Named mood colors → per-LED RGB. Kept small and camera-tweakable; the model
# selects a mood, not raw RGB, so nothing off-range reaches the rabbit.
MOOD_LIGHTS: dict[str, dict[str, tuple[int, int, int]]] = {
    "neutral": {"nose": (32, 32, 32)},
    "happy": {"bottom": (0, 192, 0), "nose": (0, 255, 0)},
    "curious": {"top": (0, 128, 255), "nose": (0, 255, 255)},
    "thinking": {"nose": (255, 128, 0)},
    "calm": {"bottom": (16, 0, 32), "nose": (64, 0, 128)},
    "alert": {"nose": (255, 0, 0), "top": (255, 0, 0)},
    "off": {name: (0, 0, 0) for name in ("bottom", "left", "nose", "right", "top")},
}


class ToolError(ValueError):
    """A tool call the model got wrong (bad name/args/range). Reported back to
    the model as a function_call_output, never crashes the turn."""


class BodyTools:
    """Registry + validated executor for the agent's body tools."""

    def __init__(
        self,
        controller: BodyController,
        get_direction: Callable[[], int | None] = lambda: None,
        priority: Priority = Priority.AGENT_EXPRESSION,
    ):
        self._controller = controller
        self._get_direction = get_direction
        self._priority = priority

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "gesture_ears",
                "Move the ears for body language. left/right are 0 (fully forward) to 16 "
                "(fully back). Use sparingly and briefly.",
                {
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer", "minimum": EAR_MIN, "maximum": EAR_MAX},
                        "right": {"type": "integer", "minimum": EAR_MIN, "maximum": EAR_MAX},
                    },
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "set_mood_lights",
                "Set the LED mood. Pick a named mood; optionally pulse it.",
                {
                    "type": "object",
                    "properties": {
                        "mood": {"type": "string", "enum": sorted(MOOD_LIGHTS)},
                        "pulse": {"type": "boolean"},
                    },
                    "required": ["mood"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "play_gesture",
                "Play a short named body-language gesture.",
                {
                    "type": "object",
                    "properties": {"preset": {"type": "string", "enum": sorted(GESTURE_PRESETS)}},
                    "required": ["preset"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "get_direction",
                "Return the last known direction of the speaker in degrees (0-359), if any.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                informational=True,
            ),
            ToolSpec(
                "body_state",
                "Return the rabbit's current ear/LED/playing state.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                informational=True,
            ),
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            output = await self._dispatch(call.name, call.arguments)
        except ToolError as exc:
            output = f"error: {exc}"
        except Exception:  # a tool bug must not kill the turn
            log.exception("tool %s crashed", call.name)
            output = "error: tool failed"
        return ToolResult(call_id=call.call_id, output=output)

    async def _dispatch(self, name: str, args: dict) -> str:
        if name == "gesture_ears":
            return await self._gesture_ears(args)
        if name == "set_mood_lights":
            return await self._set_mood_lights(args)
        if name == "play_gesture":
            return await self._play_gesture(args)
        if name == "get_direction":
            deg = self._get_direction()
            return "unknown" if deg is None else f"{int(deg) % 360} degrees"
        if name == "body_state":
            s = self._controller.snapshot()
            return json.dumps({"ears": s.ears, "leds": s.leds, "playing": s.playing})
        raise ToolError(f"unknown tool {name!r}")

    # --- individual tools (validate, then submit choreography-only) -------

    async def _gesture_ears(self, args: dict) -> str:
        left, right = _req_int(args, "left"), _req_int(args, "right")
        if not (EAR_MIN <= left <= EAR_MAX and EAR_MIN <= right <= EAR_MAX):
            raise ToolError(f"ear positions must be {EAR_MIN}..{EAR_MAX}")
        await self._submit(ChorCommand(build_gesture_ears_chor(left, right)))
        return f"ears -> ({left}, {right})"

    async def _set_mood_lights(self, args: dict) -> str:
        mood = args.get("mood")
        if mood not in MOOD_LIGHTS:
            raise ToolError(f"unknown mood {mood!r}; valid: {', '.join(sorted(MOOD_LIGHTS))}")
        pulse = bool(args.get("pulse", False))
        spec = LedSpec.from_dict(MOOD_LIGHTS[mood], pulse=pulse)
        await self._submit(LedsCommand(spec))  # LEDs compile to chor=; no jingle
        return f"mood -> {mood}" + (" (pulsing)" if pulse else "")

    async def _play_gesture(self, args: dict) -> str:
        preset = args.get("preset")
        if preset not in GESTURE_PRESETS:
            valid = ", ".join(sorted(GESTURE_PRESETS))
            raise ToolError(f"unknown gesture {preset!r}; valid: {valid}")
        await self._submit(ChorCommand(GESTURE_PRESETS[preset]))
        return f"gesture -> {preset}"

    async def _submit(self, cmd) -> None:
        await self._controller.submit(cmd, self._priority)


def _req_int(args: dict, key: str) -> int:
    if key not in args:
        raise ToolError(f"missing {key!r}")
    try:
        return int(args[key])
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key!r} must be an integer") from exc
