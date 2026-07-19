"""Choreography builders (VAPI `chor=` text format, docs/OJN_API_NOTES.md §2).

Format: tempo,{time,led,<led#>,<r>,<g>,<b> | time,motor,<ear>,<angle°>,0,<dir>},...
tempo in ms per tick (10..2550); `time` in ticks from sequence start; motor
angle in degrees (encoded server-side as /18 → 0..16 steps).
"""

from __future__ import annotations

# The chor string travels as a GET query parameter and is compiled server-side:
# cap the dance so a long TTS reply cannot produce an absurd URL/sequence.
MAX_DANCE_S = 60.0

DANCE_COLORS = [
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
]


# Ear angles are quantized server-side by /18 (0..16 steps of 18°).
_EAR_STEP_DEG = 18
_EAR_MAX_DEG = 16 * _EAR_STEP_DEG  # 288°

# LISTENING indicator: all 5 LEDs (including the bottom/base LED) breathe
# together in magenta while both ears sweep through their full VAPI range in
# opposite directions.  This deliberately reads as a persistent state, not a
# subtle notification: the user must be able to tell whether the VAD is still
# accepting speech. One cycle spans ~1.9 s, leaving each motor almost a full
# second to reach its target before reversal, and is resubmitted until EOS.
LISTENING_TEMPO_MS = 160
_LISTENING_PULSE_LEVELS = (24, 48, 96, 160, 230, 255, 230, 160, 96, 48, 24, 0)
LISTENING_CYCLE_S = len(_LISTENING_PULSE_LEVELS) * LISTENING_TEMPO_MS / 1000
LISTENING_COLOR = (255, 0, 255)

# PROCESSING indicator: all LEDs pulsing orange (on 500 ms / off 500 ms),
# meant to loop while the transcript is handled (agent loop, §6.2.5).
PROCESSING_PULSE_TEMPO_MS = 100
PROCESSING_PULSE_CYCLE_S = 1.0


def build_wake_ack_chor(
    side: str | None,
    listen_pose: tuple[int, int] = (0, 0),
    duration_ms: int = 500,
    tempo_ms: int = 50,
) -> str:
    """Wake acknowledgement: green LEDs and both ears facing forward.

    ``side`` is retained for API compatibility but deliberately does not alter
    the acknowledgement: direction belongs to sensing, while this state must
    read consistently as "wake word accepted". The following LISTENING cycle
    changes to magenta and counter-rotating ears. Choreography-only is
    mandatory: posleft/posright triggers the firmware jingle (probe #7).
    """
    del side
    ticks = max(2, duration_ms // tempo_ms)
    parts = [str(tempo_ms)]
    # t0: green on all 5 LEDs (including base) + both ears forward.
    for led in range(5):
        parts += ["0", "led", str(led), "0", "255", "0"]
    for ear in ("0", "1"):
        parts += ["0", "motor", ear, str(listen_pose[int(ear)] * _EAR_STEP_DEG), "0", "1"]
    # end: LEDs off (the LISTENING scanner takes the LEDs from here)
    for led in range(5):
        parts += [str(ticks), "led", str(led), "0", "0", "0"]
    return ",".join(parts)


def build_listening_chor(
    side: str | None = None,
    listen_pose: tuple[int, int] = (0, 0),
    color: tuple[int, int, int] = LISTENING_COLOR,
    tempo_ms: int = LISTENING_TEMPO_MS,
) -> str:
    """One conspicuous LISTENING cycle.

    Every LED breathes magenta in unison and the ears traverse the full VAPI
    range with opposite direction flags, then return toward the listening
    pose.  ``side`` remains part of the public builder signature because the
    pipeline still samples DoA each cycle, but LISTENING intentionally moves
    both ears: this is a state indicator rather than a directional twitch.
    Choreography-only (never posleft/posright — probe #7).
    """
    r, g, b = color
    n = len(_LISTENING_PULSE_LEVELS)
    parts = [str(tempo_ms)]
    for t, level in enumerate(_LISTENING_PULSE_LEVELS):
        for led in range(5):
            parts += [
                str(t),
                "led",
                str(led),
                str(round(r * level / 255)),
                str(round(g * level / 255)),
                str(round(b * level / 255)),
            ]

    # motor ear index: 0=left, 1=right. Opposite direction flags make the
    # motion visibly counter-rotating; 288° is the maximum exposed by VAPI
    # (16 exact 18° steps), so we never send an out-of-range target.
    parts += ["0", "motor", "0", str(_EAR_MAX_DEG), "0", "0"]
    parts += ["0", "motor", "1", str(_EAR_MAX_DEG), "0", "1"]
    halfway = n // 2
    parts += [
        str(halfway),
        "motor",
        "0",
        str(listen_pose[0] * _EAR_STEP_DEG),
        "0",
        "1",
    ]
    parts += [
        str(halfway),
        "motor",
        "1",
        str(listen_pose[1] * _EAR_STEP_DEG),
        "0",
        "0",
    ]
    return ",".join(parts)


def build_processing_chor(
    color: tuple[int, int, int] = (255, 140, 0),
    tempo_ms: int = PROCESSING_PULSE_TEMPO_MS,
) -> str:
    """All-LED pulse (on then off). Loops by resubmission every
    PROCESSING_PULSE_CYCLE_S while the agent processes the utterance."""
    r, g, b = color
    parts = [str(tempo_ms)]
    for led in range(5):
        parts += ["0", "led", str(led), str(r), str(g), str(b)]
    for led in range(5):
        parts += ["5", "led", str(led), "0", "0", "0"]
    return ",".join(parts)


def build_leds_off_chor(tempo_ms: int = 10, ears_pose: tuple[int, int] | None = None) -> str:
    """Stop a looping indicator, optionally returning both ears to a pose."""
    parts = [str(tempo_ms)]
    for led in range(5):
        parts += ["0", "led", str(led), "0", "0", "0"]
    if ears_pose is not None:
        for ear in (0, 1):
            parts += ["0", "motor", str(ear), str(ears_pose[ear] * _EAR_STEP_DEG), "0", "1"]
    return ",".join(parts)


def build_gesture_ears_chor(left: int, right: int, tempo_ms: int = 100) -> str:
    """Move both ears to positions 0..16 as a MOTOR CHOREOGRAPHY (never
    posleft/posright — that path triggers the firmware jingle, probe #7).

    This is the agent's only ear-movement primitive. Positions are validated
    by the caller; here they are compiled into a single-frame chor.
    """
    for v in (left, right):
        if not 0 <= v <= 16:
            raise ValueError(f"ear position {v} outside 0..16")
    parts = [str(tempo_ms)]
    parts += ["0", "motor", "0", str(left * _EAR_STEP_DEG), "0", "0"]
    parts += ["0", "motor", "1", str(right * _EAR_STEP_DEG), "0", "0"]
    return ",".join(parts)


def build_dance_chor(duration_s: float, tempo_ms: int = 100) -> str:
    """A LED/ear dance sized to span ~duration_s (e.g. a spoken sentence).

    Every 5 ticks a LED changes color (cycling through the 5 LEDs), every 10
    ticks the ears alternate front/back — enough body language for the
    dance_demo without hammering OJN with individual commands.
    """
    duration_s = min(duration_s, MAX_DANCE_S)
    ticks = max(10, int(duration_s * 1000 / tempo_ms))
    parts = [str(tempo_ms)]
    step = 0
    for t in range(0, ticks, 5):
        led = step % 5
        r, g, b = DANCE_COLORS[step % len(DANCE_COLORS)]
        parts += [str(t), "led", str(led), str(r), str(g), str(b)]
        if step % 2 == 0:
            ear = (step // 2) % 2
            angle = 180 if (step // 4) % 2 == 0 else 0
            direction = "0" if angle else "1"
            parts += [str(t), "motor", str(ear), str(angle), "0", direction]
        step += 1
    # end pose: everything off, ears forward
    for led in range(5):
        parts += [str(ticks), "led", str(led), "0", "0", "0"]
    for ear in ("0", "1"):
        parts += [str(ticks), "motor", ear, "0", "0", "1"]
    return ",".join(parts)
