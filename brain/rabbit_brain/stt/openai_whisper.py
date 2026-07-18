"""OpenAI Whisper API — cloud fallback (§6.2.4).

Non-streaming: the utterance is buffered, wrapped as WAV and posted in one
request. Acceptable for short utterances; only used when Deepgram fails.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator

import aiohttp

from .base import STTResult, drain, pcm_to_wav

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_S = 30.0


class WhisperApiSTT:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "whisper-1",
        api_base: str = DEFAULT_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key or os.environ["OPENAI_API_KEY"]
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def transcribe(self, chunks: AsyncIterator[bytes], sample_rate: int) -> STTResult:
        wav = pcm_to_wav(await drain(chunks), sample_rate)
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", self._model)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                f"{self._api_base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=form,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Whisper API HTTP {resp.status}: {await resp.text()}")
                data = await resp.json()
        return STTResult(text=data["text"].strip(), provider="whisper_api")
