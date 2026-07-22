#!/usr/bin/env python3
"""TTS synthesis-time A/B benchmark (latency, after Gate L3).

Gate L3 was hardware-rejected (OJN_API_NOTES #21): the MTL decoder buffers the
whole MP3 before playing, so delivery cannot be overlapped with synthesis — the
rabbit waits for the COMPLETE file. That makes total TTS synthesis time a hard
floor on "reply text ready → first audio", and the residual latency lever.

This harness times `provider.synth()` end-to-end for each configured TTS
profile on the same fixed it/en sentences, and reports synth time, the measured
audio length, and their ratio (a TTS real-time factor). It answers the concrete
next question — cloud vs local voice — with numbers: Piper runs on the Bolt CPU
with no network round-trip and may win on latency even if the voice is plainer;
Deepgram Aura / ElevenLabs pay network + server synthesis. It picks no winner:
judge voice quality yourself, weigh it against the measured time.

Nothing here changes the production path. Providers are built from the SAME
factory the runtime uses (rabbit_brain.tts.make_tts_provider), one profile at a
time; a profile whose keys/binaries are missing is skipped with a note, so you
can run whichever subset is configured on the box.

    # on the Bolt, with the relevant provider credentials already exported in
    # the environment (same names the runtime reads; see .env.example) plus
    # PIPER_MODEL for the local voice, then:
    python brain/scripts/tts-bench.py --profiles deepgram,elevenlabs,piper --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rabbit_brain.tts import make_tts_provider

# (language, sentence). Kept short and spoken-style, like real replies; one is
# the length the prompt targets, one a touch longer to expose scaling.
DEFAULT_SENTENCES: list[tuple[str, str]] = [
    ("it", "Certo, il sole domani splende su tutta la costa."),
    ("en", "Sure, tomorrow looks bright and clear along the whole coast."),
    ("it", "Ci sono ventuno gradi adesso, con una leggera brezza da nord."),
]


@dataclass
class Row:
    profile: str
    language: str
    synth_ms: int
    audio_s: float
    rtf: float  # synth_time / audio_length; <1 is faster than real time


async def _bench_profile(profile: str, sentences, runs: int, audio_dir: Path) -> list[Row]:
    env = {**os.environ, "TTS_PROFILE": profile, "NABAZTAG_AUDIO_DIR": str(audio_dir)}
    try:
        provider = make_tts_provider(audio_dir, env=env)
    except KeyError as missing:
        print(f"[{profile}] skipped: missing env {missing}")
        return []
    if provider is None:
        print(f"[{profile}] skipped: factory returned no provider")
        return []

    rows: list[Row] = []
    try:
        for language, text in sentences:
            for _ in range(runs):
                start = time.monotonic()
                try:
                    result = await provider.synth(text, language=language)
                except Exception as exc:  # missing binary/key surfaced at call time
                    print(f"[{profile}/{language}] synth failed: {exc}")
                    break
                synth_ms = round((time.monotonic() - start) * 1000)
                audio_s = result.duration_s or 0.0
                rtf = (synth_ms / 1000 / audio_s) if audio_s > 0 else float("nan")
                rows.append(Row(profile, language, synth_ms, audio_s, rtf))
                print(
                    f"[{profile}/{language}] synth={synth_ms}ms audio={audio_s:.2f}s "
                    f"rtf={rtf:.2f} -> {text!r}"
                )
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
    return rows


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profiles", default="deepgram,elevenlabs,piper")
    parser.add_argument("--runs", type=int, default=3, help="repeats per (profile, sentence)")
    parser.add_argument("--sentences", default=None, help="'lang<TAB>text' per line, in a file")
    return parser.parse_args()


async def main(profiles: list[str], sentences, runs: int) -> None:
    results: list[Row] = []
    with tempfile.TemporaryDirectory() as tmp:
        audio_dir = Path(tmp)
        for profile in profiles:
            results.extend(await _bench_profile(profile, sentences, runs, audio_dir))

    print("\n--- summary (median synth time per profile; DECIDE ON synth_ms) ---")
    for profile in profiles:
        subset = [r for r in results if r.profile == profile]
        if not subset:
            print(f"{profile:12s} no data")
            continue
        synth = _median([r.synth_ms for r in subset])
        rtf = _median([r.rtf for r in subset if r.rtf == r.rtf])  # drop NaN
        print(f"{profile:12s} synth_median={synth:.0f}ms  rtf_median={rtf:.2f}  n={len(subset)}")
    print(
        "\nJudge voice QUALITY yourself; this only measures time. Remember the rabbit waits for "
        "the complete file (Gate L3 rejected), so synth time is the floor on first-audio."
    )


if __name__ == "__main__":
    args = _parse_args()
    if args.sentences:
        pairs = []
        for line in Path(args.sentences).read_text().splitlines():
            if not line.strip():
                continue
            lang, _, text = line.partition("\t")
            pairs.append((lang.strip(), text.strip()))
        sentence_specs = pairs
    else:
        sentence_specs = DEFAULT_SENTENCES
    asyncio.run(
        main(
            profiles=[p.strip() for p in args.profiles.split(",") if p.strip()],
            sentences=sentence_specs,
            runs=args.runs,
        )
    )
