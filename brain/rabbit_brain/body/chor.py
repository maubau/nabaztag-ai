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


def build_wake_ack_chor(
    side: str | None,
    listen_pose: tuple[int, int] = (0, 0),
    duration_ms: int = 400,
    tempo_ms: int = 50,
) -> str:
    """Wake acknowledgement: one short (300-500 ms) non-blocking choreography.

    LED flash on the nose + a quick twitch of the ear on the DoA side
    (`side`: "left" | "right" | None = both), ending in the listening pose.
    One ChorCommand instead of two same-priority EarsCommand: EarsCommand is
    coalescable, so the real BodyController would drop the DoA bias and keep
    only the final pose (UX finding, July 2026). Motor-only choreography also
    avoids posleft/posright while the jingle side effect is under
    investigation (docs/OJN_API_NOTES.md).
    """
    ticks = max(2, duration_ms // tempo_ms)
    mid = ticks // 2
    # motor ear index: 0=left, 1=right (docs/OJN_API_NOTES.md §2)
    ears = ("0", "1") if side is None else (("0",) if side == "left" else ("1",))
    parts = [str(tempo_ms)]
    # t0: nose flash + twitch out (45° off the listening pose, capped at 180°)
    parts += ["0", "led", "2", "0", "128", "255"]
    for ear in ears:
        pose_deg = listen_pose[int(ear)] * 18
        twitch_deg = min(180, pose_deg + 45)
        parts += ["0", "motor", ear, str(twitch_deg), "0", "0"]
    # mid: back toward the listening pose
    for ear in ("0", "1"):
        parts += [str(mid), "motor", ear, str(listen_pose[int(ear)] * 18), "0", "1"]
    # end: LED off, listening pose held
    parts += [str(ticks), "led", "2", "0", "0", "0"]
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
