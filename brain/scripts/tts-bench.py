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

Piper's Deepgram fallback is FORCED OFF here (PIPER_FALLBACK_DEEPGRAM=0): on the
Bolt a DEEPGRAM_API_KEY is present, so a failing Piper would otherwise return a
Deepgram clip under the "piper" label and risk promoting Deepgram by accident. A
Piper failure is recorded AS a failure; and as a second guard, any result whose
own provider tag isn't the profile being benched is discarded, never counted.

    # on the Bolt, with the relevant provider credentials already exported in
    # the environment (same names the runtime reads; see .env.example) — for the
    # local voice PIPER_URL_IT/EN and, for the tuned run, PIPER_LENGTH_SCALE_IT/EN
    # (the pace flows through the same factory the runtime uses), then:
    python brain/scripts/tts-bench.py --profiles deepgram,elevenlabs,piper --runs 3

Timing alone can't settle voice QUALITY, nor even real latency — hardware
showed ElevenLabs benched ~970ms but took ~3700ms median IN CONVERSATION, with
markedly worse Italian and very low volume, so Aura stays the production TTS
(July 2026). Pass --keep-audio (or --output-dir PATH) to retain a labelled MP3
per synth so the quality judgement is reproducible from the files. Decide a
provider on on-Nabaztag listening AND full-conversation latency, never on the
synthetic RTF/synth number alone — this is a screening tool, not the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rabbit_brain.tts import make_tts_provider

# (language, sentence). Length matters: the earlier ~45-char fragments made
# the synthetic numbers (ElevenLabs 970 ms) badly under-predict the real
# runtime (~3700 ms) — synth time scales with characters. These are ONE full
# spoken sentence each, ~110-130 chars, the length the agent's replies
# actually reach. The same texts are reused across every profile so you can
# listen to voice A vs voice B on IDENTICAL content (--keep-audio).
_IT_WEATHER = "Certo, domani lungo la costa il cielo resta sereno, con ventuno gradi e una leggera brezza che arriva da nord."  # noqa: E501
_EN_WEATHER = "Sure, tomorrow the sky stays clear all along the coast, around twenty-one degrees with a light breeze from the north."  # noqa: E501
_IT_CANT_SEE = "Mi dispiace, non riesco a vedere: ho solo un microfono, un altoparlante, le orecchie e le lucine, ma posso raccontarti qualcosa."  # noqa: E501
DEFAULT_SENTENCES: list[tuple[str, str]] = [
    ("it", _IT_WEATHER),
    ("en", _EN_WEATHER),
    ("it", _IT_CANT_SEE),
]


@dataclass
class Row:
    profile: str
    language: str
    chars: int
    synth_ms: int
    audio_s: float
    rtf: float  # synth_time / audio_length; <1 is faster than real time


def _slug(text: str, limit: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "utt"


async def _bench_profile(
    profile: str, sentences, runs: int, audio_dir: Path, output_dir: Path | None = None
) -> list[Row]:
    """Time synth() per sentence/run. When output_dir is set, a LABELLED copy
    of each MP3 is kept there (profile_lang_slug_runN.mp3) so the qualitative
    voice comparison is reproducible from the files — synthesis itself always
    uses the throwaway working dir, so timing is unaffected."""
    env = {**os.environ, "TTS_PROFILE": profile, "NABAZTAG_AUDIO_DIR": str(audio_dir)}
    # NEVER let a provider silently fall back during a benchmark: on the Bolt a
    # DEEPGRAM_API_KEY is present, so a failing Piper would otherwise return a
    # Deepgram clip recorded under the "piper" label and could promote Deepgram
    # by accident. Disable the piper→deepgram fallback here; a Piper failure
    # must count AS a failure (review, July 2026).
    env["PIPER_FALLBACK_DEEPGRAM"] = "0"
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
            for run_i in range(runs):
                start = time.monotonic()
                try:
                    result = await provider.synth(text, language=language)
                except Exception as exc:  # missing binary/key surfaced at call time
                    print(f"[{profile}/{language}] synth FAILED: {exc}")
                    break
                # Belt and suspenders to the fallback flag above: if a result
                # ever comes back tagged as a DIFFERENT backend, it's a fallback
                # leaking through — discard it, never credit it to this profile.
                actual = result.provider
                if actual is not None and actual != profile:
                    print(
                        f"[{profile}/{language}] DISCARDED: got a {actual!r} result "
                        "(fallback leaked) — not counted"
                    )
                    break
                synth_ms = round((time.monotonic() - start) * 1000)
                audio_s = result.duration_s or 0.0
                rtf = (synth_ms / 1000 / audio_s) if audio_s > 0 else float("nan")
                rows.append(Row(profile, language, len(text), synth_ms, audio_s, rtf))
                line = (
                    f"[{profile}/{language}] chars={len(text)} synth={synth_ms}ms "
                    f"audio={audio_s:.2f}s rtf={rtf:.2f} -> {text!r}"
                )
                if output_dir is not None:
                    kept = output_dir / f"{profile}_{language}_{_slug(text)}_{run_i}.mp3"
                    shutil.copyfile(result.path, kept)
                    line += f"  [kept {kept.name}]"
                print(line)
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
    parser.add_argument(
        "--output-dir",
        default=None,
        help="keep a labelled MP3 per synth here, so voice quality is reproducible from the files",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="keep MP3s in ./tts-bench-audio (shorthand for --output-dir tts-bench-audio)",
    )
    return parser.parse_args()


async def main(profiles: list[str], sentences, runs: int, output_dir: Path | None = None) -> None:
    results: list[Row] = []
    with tempfile.TemporaryDirectory() as tmp:
        audio_dir = Path(tmp)
        for profile in profiles:
            results.extend(
                await _bench_profile(profile, sentences, runs, audio_dir, output_dir=output_dir)
            )

    # Break down BY LANGUAGE, not just per profile: it and en can synthesize
    # at very different speeds (different models/voices), and the it/en quality
    # gap is exactly what sank ElevenLabs, so an aggregate would hide it.
    print("\n--- summary (median synth time per profile × language) ---")
    languages = sorted({r.language for r in results})
    for profile in profiles:
        prof_rows = [r for r in results if r.profile == profile]
        if not prof_rows:
            print(f"{profile:12s} no data")
            continue
        for language in languages:
            subset = [r for r in prof_rows if r.language == language]
            if not subset:
                continue
            chars = _median([r.chars for r in subset])
            synth = _median([r.synth_ms for r in subset])
            rtf = _median([r.rtf for r in subset if r.rtf == r.rtf])  # drop NaN
            print(
                f"{profile:12s} {language:3s} chars_median={chars:.0f}  "
                f"synth_median={synth:.0f}ms  rtf_median={rtf:.2f}  n={len(subset)}"
            )
    if output_dir is not None:
        print(f"\nMP3s kept in {output_dir}/ — listen there to judge voice quality.")
    print(
        "\nThis measures SYNTHESIS TIME only, on a warm connection — it under-predicts the real "
        "runtime (July 2026: ElevenLabs benched 970ms but took ~3700ms median in conversation) "
        "and says nothing about voice quality or volume. Do NOT pick a provider on RTF or synth "
        "time alone: decide with on-Nabaztag listening AND full-conversation latency. Piper in "
        "particular is only worth adopting if it wins BOTH on the real rabbit."
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
    # --output-dir wins; --keep-audio is the shorthand default location
    out_dir = Path(args.output_dir) if args.output_dir else None
    if out_dir is None and args.keep_audio:
        out_dir = Path("tts-bench-audio")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)  # sync setup before the event loop
    asyncio.run(
        main(
            profiles=[p.strip() for p in args.profiles.split(",") if p.strip()],
            sentences=sentence_specs,
            runs=args.runs,
            output_dir=out_dir,
        )
    )
