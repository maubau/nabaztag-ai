"""Wake word detection — openWakeWord on the Bolt CPU (§6.2.2).

The detector consumes arbitrary-size mono s16le blocks and buffers them
internally to openWakeWord's native 1280-sample (80 ms @ 16 kHz) chunk.
Model and threshold come from config.yaml (`wake:` section); openwakeword
is an optional dependency (`audio` extra) — tests use a fake detector.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

OWW_CHUNK_SAMPLES = 1280  # openWakeWord's expected frame (80 ms @ 16 kHz)


@runtime_checkable
class WakeDetector(Protocol):
    def feed(self, pcm: bytes) -> float:
        """Feed mono s16le PCM; return the best wake score seen in it (0..1)."""
        ...

    def reset(self) -> None:
        """Clear internal state after a detection or a gated stretch."""
        ...


class OpenWakeWordDetector:
    def __init__(
        self,
        models: tuple[str, ...] = ("hey_jarvis",),
        inference_framework: str = "onnx",
    ):
        self._model_names = models
        self._framework = inference_framework
        self._buffer = bytearray()
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from openwakeword.model import Model  # optional dep: 'rabbit-brain[audio]'

            self._model = Model(
                wakeword_models=list(self._model_names),
                inference_framework=self._framework,
            )
        return self._model

    def feed(self, pcm: bytes) -> float:
        import numpy as np

        model = self._ensure_model()
        self._buffer.extend(pcm)
        best = 0.0
        chunk_bytes = OWW_CHUNK_SAMPLES * 2
        while len(self._buffer) >= chunk_bytes:
            chunk = np.frombuffer(bytes(self._buffer[:chunk_bytes]), dtype=np.int16)
            del self._buffer[:chunk_bytes]
            scores = model.predict(chunk)
            best = max(best, max(scores.values()))
        return best

    def reset(self) -> None:
        self._buffer.clear()
        if self._model is not None:
            self._model.reset()
