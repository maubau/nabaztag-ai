"""STT providers (§6.2.4) and the profile factory."""

from __future__ import annotations

from typing import Any

from .base import STTProvider, STTResult, drain, pcm_to_wav
from .deepgram import DeepgramSTT
from .fallback import FallbackSTT
from .flux import FluxSTT
from .local_whisper import LocalWhisperSTT
from .openai_whisper import WhisperApiSTT

__all__ = [
    "DeepgramSTT",
    "FallbackSTT",
    "FluxSTT",
    "LocalWhisperSTT",
    "STTProvider",
    "STTResult",
    "WhisperApiSTT",
    "drain",
    "make_stt",
    "pcm_to_wav",
]


def make_stt(config: dict[str, Any]) -> STTProvider:
    """Build the provider chain from config.yaml.

    stt_profile:
      flux  — Deepgram Flux V2, PROVIDER-side turn detection (no local
              silence window); Whisper API still backs it up.
      cloud — nova-3 + the pipeline's local Silero VAD (client-side
              endpointing). Kept as the fallback profile, not removed.
      local — faster-whisper on the Bolt CPU.

    The CODE default stays "cloud" on purpose while Flux is unverified on
    hardware: a config that predates this key keeps the known-good path
    rather than being silently switched to an unproven one. The example
    config ships `flux`, and config-doctor nudges existing configs over —
    an explicit, visible switch instead of a default that moves under you.
    """
    profile = config.get("stt_profile", "cloud")
    if profile == "flux":
        fx = config.get("flux", {})
        wh = config.get("openai_whisper", {})
        return FallbackSTT(
            FluxSTT(
                model=fx.get("model", "flux-general-multi"),
                eot_threshold=fx.get("eot_threshold", 0.7),
                eot_timeout_ms=fx.get("eot_timeout_ms", 5000),
            ),
            WhisperApiSTT(model=wh.get("model", "whisper-1")),
        )
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
    raise ValueError(f"unknown stt_profile {profile!r} (expected 'flux', 'cloud' or 'local')")
