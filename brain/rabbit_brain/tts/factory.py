"""Shared TTS/speech-stack factory (§6.2.6).

One place builds the TTS provider + Mp3Server + Speaker from the environment,
so the MCP server and the voice runtime never diverge. TTS stays ElevenLabs in
this phase (hardware-confirmed); OpenAI provides the LLM, not the voice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..body.controller import BodyController
from .base import TTSProvider
from .mp3_server import Mp3Server
from .speaker import Speaker


def make_tts_provider(
    audio_dir: str | Path, env: dict[str, str] | None = None
) -> TTSProvider | None:
    """TTS_PROFILE=deepgram|elevenlabs|piper selects the backend; unset → None
    (no local TTS, callers fall back to OJN's dead tts/say). Keys come only
    from env and are never logged."""
    env = env if env is not None else os.environ
    profile = env.get("TTS_PROFILE", "").lower()
    if profile == "deepgram":
        from .deepgram_tts import DeepgramTTS

        return DeepgramTTS(
            audio_dir,
            api_key=env.get("DEEPGRAM_API_KEY"),  # None → provider reads env itself
            voice_it=env.get("DEEPGRAM_TTS_VOICE_IT", "aura-2-livia-it"),
            voice_en=env.get("DEEPGRAM_TTS_VOICE_EN", "aura-2-thalia-en"),
            gain_db=float(env.get("DEEPGRAM_TTS_GAIN_DB", "0") or 0),
        )
    if profile == "elevenlabs":
        from .elevenlabs_tts import ElevenLabsTTS

        return ElevenLabsTTS(
            audio_dir,
            voice_id=env["ELEVENLABS_VOICE_ID"],
            api_key=env.get("ELEVENLABS_API_KEY"),  # None → provider reads env itself
            model=env.get("ELEVENLABS_MODEL", "eleven_multilingual_v2"),
        )
    if profile == "piper":
        from .piper_tts import PiperTTS

        return PiperTTS(
            audio_dir,
            model_path=env["PIPER_MODEL"],
            piper_bin=env.get("PIPER_BIN", "piper"),
        )
    return None


@dataclass
class SpeechStack:
    """The optional speech-output stack. All fields are None when TTS_PROFILE
    is unset (the caller then has no local speech)."""

    provider: TTSProvider | None = None
    mp3_server: Mp3Server | None = None
    speaker: Speaker | None = None

    async def aclose(self) -> None:
        if self.mp3_server is not None:
            await self.mp3_server.stop()
        if self.provider is not None and hasattr(self.provider, "close"):
            await self.provider.close()


async def build_speech_stack(
    controller: BodyController,
    env: dict[str, str] | None = None,
    protected_assets: set[str] | None = None,
) -> SpeechStack:
    """Build and START the TTS provider + Mp3Server + Speaker from env, or an
    empty stack if TTS_PROFILE is unset. The caller owns aclose()."""
    env = env if env is not None else os.environ
    audio_dir = env.get("NABAZTAG_AUDIO_DIR", "www/audio")
    provider = make_tts_provider(audio_dir, env)
    if provider is None:
        return SpeechStack()
    # NABAZTAG_MP3_SERVE_HTTP=0 → storage-only: Apache delivers the files via
    # the brain-audio alias (the MTL decoder ignores aiohttp-served audio —
    # hardware finding, July 2026). base_url must then point at the alias.
    serve_http = env.get("NABAZTAG_MP3_SERVE_HTTP", "1").lower() not in ("0", "false", "no")
    mp3_server = Mp3Server(
        audio_dir,
        port=int(env.get("NABAZTAG_MP3_PORT", "8090")),
        base_url=env.get("NABAZTAG_MP3_BASE_URL"),
        protected=protected_assets,
        serve_http=serve_http,
    )
    await mp3_server.start()
    return SpeechStack(provider, mp3_server, Speaker(controller, provider, mp3_server))
