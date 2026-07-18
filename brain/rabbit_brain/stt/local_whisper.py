"""faster-whisper (CTranslate2) on the Bolt CPU — local profile (§6.2.4).

Buffers the utterance, transcribes in a worker thread, and measures RTF
(processing time / audio time) for the local-vs-cloud comparison (task T5).
faster-whisper is an optional dependency (`stt-local` extra).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from .base import STTResult, drain

log = logging.getLogger(__name__)


class LocalWhisperSTT:
    def __init__(
        self, model: str = "small", compute_type: str = "int8", language: str | None = None
    ):
        self._model_name = model
        self._compute_type = compute_type
        self._language = language  # None = autodetect (it/en)
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # 'rabbit-brain[stt-local]'

            self._model = WhisperModel(self._model_name, compute_type=self._compute_type)
        return self._model

    async def transcribe(self, chunks: AsyncIterator[bytes], sample_rate: int) -> STTResult:
        pcm = await drain(chunks)
        audio_s = len(pcm) / 2 / sample_rate
        start = time.monotonic()
        text = await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)
        elapsed = time.monotonic() - start
        rtf = elapsed / audio_s if audio_s > 0 else None
        log.info(
            "faster-whisper %s/%s: %.2fs audio in %.2fs (RTF %.2f)",
            self._model_name,
            self._compute_type,
            audio_s,
            elapsed,
            rtf or 0.0,
        )
        return STTResult(text=text, provider="faster_whisper", rtf=rtf)

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str:
        import numpy as np

        model = self._ensure_model()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # faster-whisper resamples internally when given a 16 kHz float array
        segments, _info = model.transcribe(audio, language=self._language)
        return " ".join(s.text.strip() for s in segments).strip()
