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
