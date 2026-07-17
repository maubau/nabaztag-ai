"""Piper TTS provider (local profile, docs/ARCHITECTURE.md §6.2.6).

Requires the `piper` binary and a voice model on the box, plus `ffmpeg` to
encode the rabbit-facing MP3 (the rabbit streams MP3, not WAV). Duration is
read from the intermediate WAV with the stdlib wave module.
"""

from __future__ import annotations

import asyncio
import uuid
import wave
from pathlib import Path

from .base import TTSResult


class PiperTTS:
    def __init__(
        self,
        audio_dir: Path,
        model_path: str,
        piper_bin: str = "piper",
        ffmpeg_bin: str = "ffmpeg",
    ):
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._model = model_path
        self._piper = piper_bin
        self._ffmpeg = ffmpeg_bin

    async def _run(self, *cmd: str, stdin: bytes | None = None) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(stdin)
        if proc.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {stderr.decode()[-500:]}")

    async def synth(self, text: str) -> TTSResult:
        stem = self._audio_dir / uuid.uuid4().hex
        wav_path, mp3_path = stem.with_suffix(".wav"), stem.with_suffix(".mp3")
        try:
            await self._run(
                self._piper, "-m", self._model, "-f", str(wav_path), stdin=text.encode()
            )
            with wave.open(str(wav_path), "rb") as w:
                duration = w.getnframes() / w.getframerate()
            await self._run(
                self._ffmpeg, "-y", "-loglevel", "error",
                "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "4", str(mp3_path),
            )  # fmt: skip
        finally:
            wav_path.unlink(missing_ok=True)
        return TTSResult(path=mp3_path, duration_s=duration)
