"""Deepgram Aura TTS provider (docs/ARCHITECTURE.md §6.2.6).

POST /v1/speak with the text, voice selected by the LANGUAGE OF THE UTTERANCE
("it" → aura-2-livia-it, "en" → configurable English voice). The language comes
from the STT's own detection (STTResult.language), routed through
Speaker/AgentLoop — never guessed from the text. Output is MP3 with the real
duration measured (mutagen), like the other providers. DEEPGRAM_API_KEY comes
from the environment and is never logged.

Optional gain (hardware round, July 2026: voice quality good, volume a touch
low even at +3dB). Aura's /v1/speak has no volume/gain request parameter, so
a configured gain_db is applied as a post-processing pass through ffmpeg
(already a system dependency for the piper profile), chained with a peak
limiter (alimiter) so raising the gain further can't clip — a plain volume
boost alone would. Off by default (gain_db=0) — zero behavior change unless
DEEPGRAM_TTS_GAIN_DB is set, and a failed/missing ffmpeg falls back to the
unmodified file rather than breaking TTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp

from .base import TTSResult

log = logging.getLogger(__name__)

API_BASE = "https://api.deepgram.com/v1/speak"
DEFAULT_VOICE_IT = "aura-2-livia-it"
DEFAULT_VOICE_EN = "aura-2-thalia-en"
DEFAULT_TIMEOUT_S = 20.0


class DeepgramTTS:
    def __init__(
        self,
        audio_dir: Path,
        api_key: str | None = None,
        voice_it: str = DEFAULT_VOICE_IT,
        voice_en: str = DEFAULT_VOICE_EN,
        default_language: str = "it",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        api_base: str = API_BASE,
        session: aiohttp.ClientSession | None = None,
        gain_db: float = 0.0,
    ):
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._api_key = api_key or os.environ["DEEPGRAM_API_KEY"]
        self._voice_it = voice_it
        self._voice_en = voice_en
        self._default_language = default_language
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._api_base = api_base
        self._session = session
        self._own_session = session is None
        self._gain_db = gain_db

    def voice_for(self, language: str | None) -> str:
        """Voice by utterance language ("it"/"en", region tags tolerated)."""
        lang = (language or self._default_language).lower()
        if lang.startswith("en"):
            return self._voice_en
        return self._voice_it  # it and anything unknown → the Italian voice

    async def __aenter__(self) -> DeepgramTTS:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def synth(self, text: str, language: str | None = None) -> TTSResult:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        t_request = time.monotonic()
        t_headers = t_first_byte = t_last_byte = None
        async with self._session.post(
            self._api_base,
            params={"model": self.voice_for(language)},
            headers={"Authorization": f"Token {self._api_key}"},
            json={"text": text},
            timeout=self._timeout,
        ) as resp:
            t_headers = time.monotonic()
            if resp.status != 200:
                raise RuntimeError(f"Deepgram TTS HTTP {resp.status}: {await resp.text()}")
            chunks = []
            async for chunk in resp.content.iter_any():
                if t_first_byte is None:
                    t_first_byte = time.monotonic()
                chunks.append(chunk)
            data = b"".join(chunks)
        t_last_byte = time.monotonic()
        path = self._audio_dir / f"{uuid.uuid4().hex}.mp3"
        path.write_bytes(data)
        t_gain_start = time.monotonic()
        if self._gain_db:
            path = await self._apply_gain(path)
        t_gain_ms = round((time.monotonic() - t_gain_start) * 1000)
        duration_s = self._mp3_duration(path)

        def ms(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        # Localizes where TTS latency actually goes — network/API, download,
        # or our own ffmpeg post-processing (hardware round, July 2026: ~5s
        # for "Deepgram TTS + submit" with no visibility into the split).
        # Never logs text content, only its length.
        log.info(
            "deepgram tts timing: chars=%d request_to_headers_ms=%s headers_to_first_byte_ms=%s "
            "first_to_last_byte_ms=%s total_http_ms=%s gain_ms=%d mp3_duration_s=%.2f",
            len(text),
            ms(t_request, t_headers),
            ms(t_headers, t_first_byte),
            ms(t_first_byte, t_last_byte),
            ms(t_request, t_last_byte),
            t_gain_ms,
            duration_s,
        )
        return TTSResult(path=path, duration_s=duration_s, provider="deepgram")

    async def _apply_gain(self, path: Path) -> Path:
        boosted = path.with_suffix(".gain.mp3")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-filter:a",
                # alimiter caps true peaks at ~-0.5 dBFS so a higher gain_db
                # can't clip — plain "volume=XdB" alone would (hardware
                # round, July 2026: still quiet at +3dB, +6dB was the next
                # thing to try, so headroom against clipping matters here).
                f"volume={self._gain_db}dB,alimiter=limit=0.95:attack=5:release=50",
                "-codec:a",
                "libmp3lame",
                str(boosted),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            log.warning("DEEPGRAM_TTS_GAIN_DB set but ffmpeg is not installed; unboosted audio")
            return path
        if proc.returncode != 0:
            log.warning(
                "Deepgram TTS gain (ffmpeg) failed, using unboosted audio: %s",
                stderr.decode(errors="replace")[:200],
            )
            return path
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)  # os, not Path.unlink — ASYNC240 flags blocking Path I/O
        return boosted

    @staticmethod
    def _mp3_duration(path: Path) -> float:
        from mutagen.mp3 import MP3

        return MP3(path).info.length
