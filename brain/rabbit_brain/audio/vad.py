"""VAD-gated utterance recording — silero-vad, end of speech at 700 ms (§6.2.3).

`UtteranceRecorder` is a push-driven state machine generic over a
`SpeechProbe` (chunk → speech probability), so tests run with a fake probe
and production uses `SileroProbe` (pysilero-vad → onnxruntime, no torch).
Chunks are 512 samples @ 16 kHz (silero's native size); a short pre-roll ring
keeps the first syllable that fired the probe.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

VAD_CHUNK_SAMPLES = 512  # silero-vad native chunk @ 16 kHz (32 ms)
DEFAULT_END_OF_SPEECH_MS = 700  # docs/ARCHITECTURE.md §6.2.3
DEFAULT_MAX_UTTERANCE_S = 20.0
DEFAULT_START_TIMEOUT_S = 6.0  # wake fired but nobody spoke
DEFAULT_PRE_ROLL_MS = 300


@runtime_checkable
class SpeechProbe(Protocol):
    def __call__(self, chunk: bytes) -> float:
        """Speech probability (0..1) for one 512-sample s16le chunk."""
        ...


class SileroProbe:
    """silero-vad via the ONNX-only pysilero-vad package (optional dep)."""

    def __init__(self):
        from pysilero_vad import SileroVoiceActivityDetector  # 'rabbit-brain[audio]'

        self._vad = SileroVoiceActivityDetector()

    def __call__(self, chunk: bytes) -> float:
        return self._vad(chunk)


class UtteranceRecorder:
    """Collects one utterance: waits for speech, streams it, ends on silence.

    push(chunk) returns (chunks_to_emit, done). Emitted chunks go straight to
    the streaming STT; when done is True the utterance is over (end-of-speech,
    max length, or start timeout with nothing captured).
    """

    def __init__(
        self,
        probe: SpeechProbe,
        sample_rate: int = 16_000,
        threshold: float = 0.5,
        end_of_speech_ms: int = DEFAULT_END_OF_SPEECH_MS,
        max_utterance_s: float = DEFAULT_MAX_UTTERANCE_S,
        start_timeout_s: float = DEFAULT_START_TIMEOUT_S,
        pre_roll_ms: int = DEFAULT_PRE_ROLL_MS,
    ):
        self._probe = probe
        self._threshold = threshold
        chunk_ms = VAD_CHUNK_SAMPLES * 1000 / sample_rate
        self._silence_chunks = max(1, round(end_of_speech_ms / chunk_ms))
        self._max_chunks = max(1, round(max_utterance_s * 1000 / chunk_ms))
        self._timeout_chunks = max(1, round(start_timeout_s * 1000 / chunk_ms))
        self._pre_roll_max = max(1, round(pre_roll_ms / chunk_ms))

        self._buffer = bytearray()
        self._pre_roll: list[bytes] = []
        self._speaking = False
        self._silence_run = 0
        self._waited = 0
        self._emitted = 0
        self.got_speech = False

    def push(self, pcm: bytes) -> tuple[list[bytes], bool]:
        self._buffer.extend(pcm)
        out: list[bytes] = []
        chunk_bytes = VAD_CHUNK_SAMPLES * 2
        while len(self._buffer) >= chunk_bytes:
            chunk = bytes(self._buffer[:chunk_bytes])
            del self._buffer[:chunk_bytes]
            emit, done = self._process(chunk)
            out.extend(emit)
            if done:
                return out, True
        return out, False

    def _process(self, chunk: bytes) -> tuple[list[bytes], bool]:
        is_speech = self._probe(chunk) >= self._threshold

        if not self._speaking:
            if is_speech:
                self._speaking = True
                self.got_speech = True
                emit = [*self._pre_roll, chunk]
                self._pre_roll = []
                self._emitted += len(emit)
                return emit, False
            self._pre_roll.append(chunk)
            if len(self._pre_roll) > self._pre_roll_max:
                self._pre_roll.pop(0)
            self._waited += 1
            if self._waited >= self._timeout_chunks:
                log.info("no speech within start timeout, giving up")
                return [], True
            return [], False

        self._emitted += 1
        self._silence_run = 0 if is_speech else self._silence_run + 1
        if self._silence_run >= self._silence_chunks:
            log.debug("end of speech after %d chunks", self._emitted)
            return [chunk], True
        if self._emitted >= self._max_chunks:
            log.info("utterance hit max length, cutting")
            return [chunk], True
        return [chunk], False
