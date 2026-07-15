"""Shared body types: commands, priorities, capabilities, state, events.

Ear positions are 0..16 (Violet protocol steps of 18°). LED indices follow
choregraphy.h in OpenJabNab: 0=bottom, 1=left, 2=middle (the nose), 3=right,
4=top — see docs/OJN_API_NOTES.md §2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

EAR_MIN, EAR_MAX = 0, 16

# Violet LED indices (choregraphy.h). "nose" is the middle LED.
LED_INDEX = {"bottom": 0, "left": 1, "nose": 2, "middle": 2, "right": 3, "top": 4}


class Priority(IntEnum):
    """Arbitration priorities (docs/ARCHITECTURE.md §6.4), highest wins."""

    AMBIENT_IDLE = 0
    DOA_REFLEX = 1
    AGENT_EXPRESSION = 2
    USER_SPEECH_SYNC = 3
    SAFETY_SYSTEM = 4


@dataclass(frozen=True)
class LedSpec:
    """Desired LED colors: name -> (r, g, b), each 0..255. Missing LEDs are untouched."""

    colors: tuple[tuple[str, tuple[int, int, int]], ...]
    pulse: bool = False

    @classmethod
    def from_dict(cls, colors: dict[str, tuple[int, int, int]], pulse: bool = False) -> LedSpec:
        for name, rgb in colors.items():
            if name not in LED_INDEX:
                raise ValueError(f"unknown LED {name!r} (valid: {sorted(LED_INDEX)})")
            if not all(0 <= c <= 255 for c in rgb):
                raise ValueError(f"RGB out of range for LED {name!r}: {rgb}")
        return cls(colors=tuple(sorted(colors.items())), pulse=pulse)

    def as_dict(self) -> dict[str, tuple[int, int, int]]:
        return dict(self.colors)


@dataclass(frozen=True)
class EarsCommand:
    left: int
    right: int

    def __post_init__(self) -> None:
        for v in (self.left, self.right):
            if not EAR_MIN <= v <= EAR_MAX:
                raise ValueError(f"ear position {v} outside {EAR_MIN}..{EAR_MAX}")


@dataclass(frozen=True)
class LedsCommand:
    spec: LedSpec


@dataclass(frozen=True)
class PlayAudioCommand:
    """Play one or more MP3 URLs on the rabbit, queued in order (one VAPI urlList call)."""

    urls: tuple[str, ...]
    # Summed duration of the MP3s, if the caller knows it. OJN gives no
    # playback-finished callback, so this drives the wait_finished timer.
    duration_s: float | None = None

    def __post_init__(self) -> None:
        if not self.urls:
            raise ValueError("PlayAudioCommand needs at least one URL")


@dataclass(frozen=True)
class SayCommand:
    """Server-side TTS via OJN's own tts/say — S1/S2 smoke test and MCP speak()."""

    text: str


@dataclass(frozen=True)
class ChorCommand:
    """Raw VAPI choreography string (docs/OJN_API_NOTES.md §2, 'chor=' format)."""

    chor: str


@dataclass(frozen=True)
class SleepCommand:
    pass


@dataclass(frozen=True)
class WakeCommand:
    pass


BodyCommand = (
    EarsCommand
    | LedsCommand
    | PlayAudioCommand
    | SayCommand
    | ChorCommand
    | SleepCommand
    | WakeCommand
)

# Commands where only the latest queued target matters (coalescing, §6.4).
COALESCABLE = (EarsCommand, LedsCommand)


@dataclass(frozen=True)
class BodyCapabilities:
    """What this body can actually do; filled from the Gate G0 capability matrix.

    The BodyController consults this and never promises a preemption the body
    cannot physically honor (docs/ARCHITECTURE.md §6.6).
    """

    can_cancel_audio: bool
    has_playback_events: bool
    can_read_body_state: bool
    has_per_led_rgb: bool
    ear_range: tuple[int, int] = (EAR_MIN, EAR_MAX)


@dataclass(frozen=True)
class BodyEvent:
    """Input event from the body: button click or RFID tag."""

    kind: str  # "single_click" | "double_click" | "rfid"
    data: str = ""  # tag id (hex) for rfid
    timestamp: float = 0.0


@dataclass
class BodyState:
    """Controller-tracked state (OJN offers no readback — docs/OJN_API_NOTES.md §2)."""

    ears: tuple[int, int] | None = None
    leds: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    playing: bool = False
    last_audio_urls: tuple[str, ...] = ()
    last_rfid: str | None = None
