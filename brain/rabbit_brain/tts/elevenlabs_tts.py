"""ElevenLabs TTS provider (cloud profile, docs/ARCHITECTURE.md §6.2.6)."""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp
from mutagen.mp3 import MP3

from .base import TTSResult

log = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_multilingual_v2"
# A hung TTS call must not freeze the voice loop: fail fast and let the caller
# decide (the p50 budget is ~4s end-to-end).
DEFAULT_TIMEOUT_S = 20.0


class ElevenLabsTTS:
    def __init__(
        self,
        audio_dir: Path,
        voice_id: str,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: aiohttp.ClientSession | None = None,
    ):
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._voice_id = voice_id
        self._api_key = api_key or os.environ["ELEVENLABS_API_KEY"]
        self._model = model
        self._session = session
        self._own_session = session is None

    async def __aenter__(self) -> ElevenLabsTTS:
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
        del language  # one multilingual voice — no routing needed
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        url = f"{API_BASE}/text-to-speech/{self._voice_id}"
        t_request = time.monotonic()
        async with self._session.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self._api_key},
            json={"text": text, "model_id": self._model},
            timeout=self._timeout,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ElevenLabs HTTP {resp.status}: {await resp.text()}")
            data = await resp.read()
        path = self._audio_dir / f"{uuid.uuid4().hex}.mp3"
        path.write_bytes(data)
        duration_s = MP3(path).info.length
        # Comparable to DeepgramTTS.synth's timing line (latency round, July
        # 2026): chars in, total HTTP time, MP3 length — never text content.
        log.info(
            "elevenlabs tts timing: chars=%d total_http_ms=%d mp3_duration_s=%.2f",
            len(text),
            round((time.monotonic() - t_request) * 1000),
            duration_s,
        )
        return TTSResult(path=path, duration_s=duration_s, provider="elevenlabs")
