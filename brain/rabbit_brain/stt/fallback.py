"""Primary→fallback STT chaining (cloud profile: Deepgram → Whisper API).

The PCM stream is teed into a buffer while the primary consumes it, so on
failure the fallback gets the complete utterance without re-recording.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .base import STTProvider, STTResult

log = logging.getLogger(__name__)


class FallbackSTT:
    def __init__(self, primary: STTProvider, fallback: STTProvider):
        self._primary = primary
        self._fallback = fallback

    async def transcribe(self, chunks: AsyncIterator[bytes], sample_rate: int) -> STTResult:
        buffered: list[bytes] = []

        async def tee() -> AsyncIterator[bytes]:
            async for chunk in chunks:
                buffered.append(chunk)
                yield chunk

        try:
            return await self._primary.transcribe(tee(), sample_rate)
        except Exception:
            log.exception("primary STT failed, falling back")
            # the primary may have died mid-stream: drain what's left so the
            # fallback sees the whole utterance
            async for chunk in chunks:
                buffered.append(chunk)

            async def replay() -> AsyncIterator[bytes]:
                for chunk in buffered:
                    yield chunk

            return await self._fallback.transcribe(replay(), sample_rate)
