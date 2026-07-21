"""Primary→fallback STT chaining (cloud profile: Deepgram → Whisper API).

The PCM stream is teed into a buffer while the primary consumes it, so on
failure the fallback gets the complete utterance without re-recording.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .base import EndOfTurnCallback, STTProvider, STTResult

log = logging.getLogger(__name__)


class FallbackSTT:
    def __init__(self, primary: STTProvider, fallback: STTProvider):
        self._primary = primary
        self._fallback = fallback

    @property
    def detects_end_of_turn(self) -> bool:
        """Mirrors the PRIMARY: with Flux in front, the pipeline must run the
        provider-endpointing loop (stream stays open). If the primary then
        fails, _fallback_after_primary below closes the stream itself before
        handing the buffered audio to a client-endpointing fallback, so the
        fallback still sees a finite stream (latency Gate L1)."""
        return getattr(self._primary, "detects_end_of_turn", False)

    async def transcribe(
        self,
        chunks: AsyncIterator[bytes],
        sample_rate: int,
        on_end_of_turn: EndOfTurnCallback | None = None,
    ) -> STTResult:
        buffered: list[bytes] = []

        async def tee() -> AsyncIterator[bytes]:
            async for chunk in chunks:
                buffered.append(chunk)
                yield chunk

        try:
            return await self._primary.transcribe(tee(), sample_rate, on_end_of_turn)
        except Exception:
            log.exception("primary STT failed, falling back")
            return await self._fallback_after_primary(chunks, buffered, sample_rate, on_end_of_turn)

    async def _fallback_after_primary(
        self,
        chunks: AsyncIterator[bytes],
        buffered: list[bytes],
        sample_rate: int,
        on_end_of_turn: EndOfTurnCallback | None,
    ) -> STTResult:
        # The primary may have died mid-stream. Drain what is already queued
        # so the fallback sees the whole utterance — but do NOT block waiting
        # for more: when the primary was a turn-detecting provider (Flux) the
        # caller is still feeding frames and would never close the stream, so
        # an unbounded drain here would hang the turn forever.
        if not getattr(self._primary, "detects_end_of_turn", False):
            async for chunk in chunks:
                buffered.append(chunk)

        async def replay() -> AsyncIterator[bytes]:
            for chunk in buffered:
                yield chunk

        result = await self._fallback.transcribe(replay(), sample_rate)
        # A turn-detecting primary owed the caller an end-of-turn signal and
        # never delivered one; the fallback's result IS the end of the turn,
        # so raise it here or the pipeline would wait out its own timeout.
        if on_end_of_turn is not None and getattr(self._primary, "detects_end_of_turn", False):
            on_end_of_turn(result.text)
        return result
