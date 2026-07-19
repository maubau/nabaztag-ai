"""Deepgram Aura TTS provider (docs/ARCHITECTURE.md §6.2.6).

POST /v1/speak with the text, voice selected by the LANGUAGE OF THE UTTERANCE
("it" → aura-2-livia-it, "en" → configurable English voice). The language comes
from the STT's own detection (STTResult.language), routed through
Speaker/AgentLoop — never guessed from the text. Output is MP3 with the real
duration measured (mutagen), like the other providers. DEEPGRAM_API_KEY comes
from the environment and is never logged.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import aiohttp

from .base import TTSResult

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
        async with self._session.post(
            self._api_base,
            params={"model": self.voice_for(language)},
            headers={"Authorization": f"Token {self._api_key}"},
            json={"text": text},
            timeout=self._timeout,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Deepgram TTS HTTP {resp.status}: {await resp.text()}")
            data = await resp.read()
        path = self._audio_dir / f"{uuid.uuid4().hex}.mp3"
        path.write_bytes(data)
        return TTSResult(path=path, duration_s=self._mp3_duration(path))

    @staticmethod
    def _mp3_duration(path: Path) -> float:
        from mutagen.mp3 import MP3

        return MP3(path).info.length
