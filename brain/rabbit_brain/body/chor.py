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


# Ear angles are quantized server-side by /18 (0..16 steps of 18°). Use an
# exact multiple so the twitch lands where intended: 72° = 4 steps. A smaller
# angle (e.g. 45° → ~36° after rounding) reads as barely-there on hardware
# (UX finding, July 2026).
WAKE_TWITCH_DEG = 72
_EAR_STEP_DEG = 18
_EAR_MAX_DEG = 16 * _EAR_STEP_DEG  # 288°

# LISTENING indicator: a lit dot sweeping the 5 LEDs front↔back plus a gentle
# periodic ear nod toward the voice, meant to loop for the whole VAD recording.
# Cyan reads clearly and is distinct from the wake flash (white) and PROCESSING
# (orange). One sweep spans ~1 s, which is also the DoA re-read cadence.
LISTENING_TEMPO_MS = 125
_SCANNER_POSITIONS = (0, 1, 2, 3, 4, 3, 2, 1)  # bottom→top→bottom (period 8)
LISTENING_CYCLE_S = len(_SCANNER_POSITIONS) * LISTENING_TEMPO_MS / 1000  # ~1.0 s
# A 2-step (36°) nod is gentle enough to repeat every second without looking
# frantic; still choreography-only (never posleft/posright — probe #7).
LISTENING_EAR_NOD_DEG = 36

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
    """Wake acknowledgement: one short (~500 ms) non-blocking choreography.

    All 5 LEDs flash white + a firm 72° twitch (with a dwell) of the ear on
    the DoA side (`side`: "left" | "right" | None = both), returning to the
    listening pose. One ChorCommand, never two same-priority EarsCommand: the
    BodyController coalesces those and silently drops the DoA bias (UX finding).
    Choreography-only is also mandatory, not just convenient: the posleft/
    posright path triggers a long firmware jingle — chor= does not (probe #7,
    hardware-confirmed July 2026).
    """
    ticks = max(2, duration_ms // tempo_ms)
    dwell = ticks // 2
    # motor ear index: 0=left, 1=right (docs/OJN_API_NOTES.md §2)
    ears = ("0", "1") if side is None else (("0",) if side == "left" else ("1",))
    parts = [str(tempo_ms)]
    # t0: white flash on all 5 LEDs + twitch the DoA-side ear(s) out by 72°
    for led in range(5):
        parts += ["0", "led", str(led), "255", "255", "255"]
    for ear in ears:
        pose_deg = listen_pose[int(ear)] * _EAR_STEP_DEG
        twitch_deg = min(_EAR_MAX_DEG, pose_deg + WAKE_TWITCH_DEG)
        parts += ["0", "motor", ear, str(twitch_deg), "0", "0"]
    # after the dwell: ears back to the listening pose
    for ear in ("0", "1"):
        parts += [str(dwell), "motor", ear, str(listen_pose[int(ear)] * _EAR_STEP_DEG), "0", "1"]
    # end: LEDs off (the LISTENING scanner takes the LEDs from here)
    for led in range(5):
        parts += [str(ticks), "led", str(led), "0", "0", "0"]
    return ",".join(parts)


def build_listening_chor(
    side: str | None = None,
    listen_pose: tuple[int, int] = (0, 0),
    color: tuple[int, int, int] = (0, 150, 255),
    tempo_ms: int = LISTENING_TEMPO_MS,
) -> str:
    """One LISTENING cycle: a lit dot sweeping the 5 LEDs (front↔back) plus,
    if `side` is given, a gentle nod of that ear toward the voice. Loops by
    resubmission for the whole listening window; LISTENING_CYCLE_S is its wall
    duration and also the DoA re-read cadence. Choreography-only (no
    posleft/posright — probe #7)."""
    r, g, b = color
    n = len(_SCANNER_POSITIONS)
    parts = [str(tempo_ms)]
    prev: int | None = None
    for t, pos in enumerate(_SCANNER_POSITIONS):
        if prev is not None:
            parts += [str(t), "led", str(prev), "0", "0", "0"]
        parts += [str(t), "led", str(pos), str(r), str(g), str(b)]
        prev = pos
    parts += [str(n), "led", str(prev), "0", "0", "0"]
    if side is not None:
        # motor ear index: 0=left, 1=right (docs/OJN_API_NOTES.md §2)
        ear = "0" if side == "left" else "1"
        pose_deg = listen_pose[int(ear)] * _EAR_STEP_DEG
        nod_deg = min(_EAR_MAX_DEG, pose_deg + LISTENING_EAR_NOD_DEG)
        parts += ["0", "motor", ear, str(nod_deg), "0", "0"]
        parts += [str(n // 2), "motor", ear, str(pose_deg), "0", "1"]
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


def build_leds_off_chor(tempo_ms: int = 10) -> str:
    """Turn all 5 LEDs off — the terminator for a looping indicator."""
    parts = [str(tempo_ms)]
    for led in range(5):
        parts += ["0", "led", str(led), "0", "0", "0"]
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
