"""STT providers (§6.2.4) and the profile factory."""

from __future__ import annotations

from typing import Any

from .base import STTProvider, STTResult, drain, pcm_to_wav
from .deepgram import DeepgramSTT
from .fallback import FallbackSTT
from .local_whisper import LocalWhisperSTT
from .openai_whisper import WhisperApiSTT

__all__ = [
    "DeepgramSTT",
    "FallbackSTT",
    "LocalWhisperSTT",
    "STTProvider",
    "STTResult",
    "WhisperApiSTT",
    "drain",
    "make_stt",
    "pcm_to_wav",
]


def make_stt(config: dict[str, Any]) -> STTProvider:
    """Build the provider chain from config.yaml (stt_profile: cloud | local)."""
    profile = config.get("stt_profile", "cloud")
    if profile == "cloud":
        dg = config.get("deepgram", {})
        wh = config.get("openai_whisper", {})
        return FallbackSTT(
            DeepgramSTT(
                model=dg.get("model", "nova-3"),
                language=dg.get("language", "multi"),
                endpointing_ms=dg.get("endpointing", 100),
            ),
            WhisperApiSTT(model=wh.get("model", "whisper-1")),
        )
    if profile == "local":
        lw = config.get("local_whisper", {})
        return LocalWhisperSTT(
            model=lw.get("model", "small"), compute_type=lw.get("compute_type", "int8")
        )
    raise ValueError(f"unknown stt_profile {profile!r} (expected 'cloud' or 'local')")
