"""Piper TTS provider — HTTP client for a PERSISTENT local Piper service.

Design (latency round, July 2026): Piper's CLI reloads the whole voice model on
every invocation, which its own docs call slow and steer away from for repeated
use in favour of the HTTP server. Benchmarking or running the CLI per utterance
would measure a deliberately inefficient path, so this provider is an HTTP
CLIENT of a persistent Piper server that keeps the model warm. It is BILINGUAL:
one server per language (it/en), routed by the STT-detected language — the same
requirement Deepgram Aura satisfies (§6.2.6). Falling back to one voice for both
languages would regress it/en, so a language whose server is unset raises.

LICENCE / PROVENANCE (important — this brain is Apache-2.0):
  - The Piper engine (OHF-Voice/piper1-gpl, release 1.4.2) is GPL-3.0. It runs
    ONLY as an EXTERNAL localhost process/service; NO Piper code is imported,
    vendored, or copied into this tree. The brain talks to it purely over HTTP,
    exactly as it does to Deepgram/OpenAI — a clean process boundary, not a
    derivative work.
  - Voices (each has its own licence — record it, and honour attribution):
      IT: it_IT-paola-medium (22.05 kHz, medium; CC0 training dataset).
      EN: en_US-sam-medium (22.05 kHz, medium; Apache-2.0 dataset) — the
          recommended default. Alternative en_GB-alba-medium (CC BY 4.0)
          REQUIRES attribution if used.

Production TTS stays Deepgram Aura; Piper is a candidate that must win BOTH
latency and on-Nabaztag listening before it is promoted. This provider therefore
takes an optional `fallback` (Deepgram) used on timeout/error so the piper
profile degrades gracefully rather than going silent. During BENCHMARKING the
fallback is disabled (PIPER_FALLBACK_DEEPGRAM=0) so a Piper failure is never a
silent Deepgram substitution — and the fallback's result keeps its own provider
tag so a caller can tell them apart regardless.

Server API (CONFIRMED against piper1-gpl 1.4.2's HTTP server, July 2026): POST
JSON {"text": text} and the response body is WAV audio. `_request_wav` is the
single place that shape lives. The rabbit streams MP3, so the WAV is transcoded
with ffmpeg (already a dep).
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
import wave
from pathlib import Path

import aiohttp

from .base import TTSProvider, TTSResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20.0


class PiperTTS:
    def __init__(
        self,
        audio_dir: Path,
        url_it: str | None = None,
        url_en: str | None = None,
        default_language: str = "it",
        ffmpeg_bin: str = "ffmpeg",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: aiohttp.ClientSession | None = None,
        fallback: TTSProvider | None = None,
    ):
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._url_it = url_it.rstrip("/") if url_it else None
        self._url_en = url_en.rstrip("/") if url_en else None
        self._default_language = default_language
        self._ffmpeg = ffmpeg_bin
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session = session
        self._own_session = session is None
        self._fallback = fallback

    def url_for(self, language: str | None) -> str:
        """Server URL for the utterance language (it/en, region tags tolerated).
        Raises if that language has no server configured — silently reusing the
        other voice would regress the bilingual requirement."""
        lang = (language or self._default_language).lower()
        url = self._url_en if lang.startswith("en") else self._url_it
        if not url:
            raise RuntimeError(
                f"no Piper server configured for language {lang!r} "
                "(set PIPER_URL_IT / PIPER_URL_EN)"
            )
        return url

    async def __aenter__(self) -> PiperTTS:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None
        if self._fallback is not None and hasattr(self._fallback, "close"):
            await self._fallback.close()

    async def synth(self, text: str, language: str | None = None) -> TTSResult:
        try:
            return await self._synth(text, language)
        except Exception:
            if self._fallback is None:
                raise
            log.warning("piper synth failed, falling back to the secondary TTS", exc_info=True)
            # The fallback result keeps ITS OWN provider tag (e.g. "deepgram"),
            # never "piper" — so a caller (tts-bench) can tell a fallback clip
            # apart and never credit it to Piper.
            return await self._fallback.synth(text, language=language)

    async def _synth(self, text: str, language: str | None) -> TTSResult:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        url = self.url_for(language)
        t_request = time.monotonic()
        wav = await self._request_wav(url, text)
        t_wav = time.monotonic()
        wav_duration = _wav_duration(wav)
        mp3_path = self._audio_dir / f"{uuid.uuid4().hex}.mp3"
        await self._transcode(wav, mp3_path)
        log.info(
            "piper tts timing: lang=%s chars=%d server_ms=%d transcode_ms=%d duration_s=%.2f",
            (language or self._default_language),
            len(text),
            round((t_wav - t_request) * 1000),
            round((time.monotonic() - t_wav) * 1000),
            wav_duration,
        )
        return TTSResult(path=mp3_path, duration_s=wav_duration, provider="piper")

    async def _request_wav(self, url: str, text: str) -> bytes:
        """POST {"text": ...} to the Piper server and return WAV bytes
        (confirmed against piper1-gpl 1.4.2 — see module docstring). Kept in
        one place so the wire shape has a single home."""
        assert self._session is not None
        async with self._session.post(url, json={"text": text}, timeout=self._timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Piper HTTP {resp.status}: {(await resp.text())[:200]}")
            return await resp.read()

    async def _transcode(self, wav: bytes, mp3_path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-y", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-qscale:a", "4", str(mp3_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        _, stderr = await proc.communicate(wav)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {stderr.decode()[-300:]}")


def _wav_duration(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate = w.getframerate()
        return w.getnframes() / rate if rate else 0.0
