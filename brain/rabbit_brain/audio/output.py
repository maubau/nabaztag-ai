"""Local audio output — play the reply on the BOLT instead of the rabbit.

Two selectable output backends (`audio_out.backend` in config.yaml):

  "rabbit" (default, embodied): MP3 URL -> OJN -> the Nabaztag's own speaker.
      The voice comes out of the rabbit, which is the whole charm — but the
      MTL decoder buffers each file to EOF before it plays (Gate L3,
      hardware-rejected), so audio can never start before synthesis finishes,
      there are no real playback events (duration is ESTIMATED), and there is
      a ~1.7 s gap between queued files.

  "local" (this module): decode to PCM and play through an audio device on the
      Bolt. Removes the MTL decoder from the path entirely, which buys:
        - real playback start/end (the handle finishes when the audio really
          finished, so the half-duplex gate and PLAYING state stop guessing);
        - instant cancel -> barge-in becomes possible (OJN cannot cancel audio);
        - progressive/streaming playback, the thing Gate L3 could not have.
      The rabbit still moves: ears and LEDs are choreography on the OJN path
      regardless of where the sound comes from.

ECHO CANCELLATION (why the OUTPUT DEVICE matters): the reSpeaker XVF3800 does
AEC/beamforming ON-CHIP, and over USB it takes the far-end REFERENCE from the
host — the UAC2 interface has a playback direction, and the board carries a
3.5 mm jack and a JST connector for a 5 W amplified speaker. So if the speaker
is driven THROUGH the reSpeaker, the chip knows what is being played and
subtracts it from the mics for free: the rabbit stops hearing itself, which is
the precondition for talking over it. Play through the Bolt's own HDMI/line-out
instead and there is NO reference signal — the microphone hears the speaker and
any barge-in detector will trigger on the rabbit's own voice. Prefer the
reSpeaker's output; `audio_out.device` selects it.

`sounddevice` is an optional dependency (rabbit-brain[audio]); ffmpeg does the
MP3->PCM decode and is already required for the TTS gain stage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

_CARD_RE = re.compile(r"CARD=([^,]+)")

# Decode/'write to the device' block size. Small enough that a cancel lands
# quickly (barge-in), large enough not to churn: ~21 ms of 48 kHz stereo.
_BLOCK_BYTES = 4096

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2


def resolve_output_device(devices: list[dict], wanted: str) -> int | None:
    """PortAudio OUTPUT-device index matching an ALSA-style name or substring.

    Mirrors resolve_input_device: PortAudio matches by index or by a substring
    of the device NAME, not by arbitrary ALSA PCM strings, so "hw:CARD=X,DEV=0"
    is reduced to its card token. Returns None if nothing matches.
    """
    match = _CARD_RE.search(wanted)
    token = (match.group(1) if match else wanted).lower()
    for index, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) > 0 and token in dev.get("name", "").lower():
            return index
    return None


def local_path_for(url: str, audio_dir: Path) -> Path:
    """Map a queued audio URL back to the file on disk.

    The Speaker hands out Mp3Server URLs because the RABBIT fetches them over
    HTTP; playing locally we want the file itself, and every MP3 we produce
    lives in audio_dir. Plain paths are passed through unchanged.
    """
    parsed = urlparse(url)
    name = Path(unquote(parsed.path if parsed.scheme else url)).name
    return audio_dir / name


class LocalPlaybackHandle:
    """PlaybackHandle over a real device: `wait_finished` resolves when the
    audio ACTUALLY finished (not on an estimated timer), and `cancel` stops it
    immediately."""

    def __init__(self, task: asyncio.Task, cancel_event: asyncio.Event):
        self._task = task
        self._cancel = cancel_event

    async def wait_finished(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._task)

    async def cancel(self) -> None:
        self._cancel.set()
        await self.wait_finished()


class LocalAudioPlayer:
    """Plays queued audio on a Bolt audio device. Duck-types the slice of
    BodyAdapter the controller uses for audio, so BodyController can delegate
    to it and keep its own audio lane, half-duplex gate and PLAYING state
    exactly as they are."""

    def __init__(
        self,
        audio_dir: str | Path,
        device: str | int | None = None,
        sample_rate: int | None = None,
        channels: int | None = None,
        ffmpeg_bin: str = "ffmpeg",
    ):
        self._audio_dir = Path(audio_dir)
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        self._ffmpeg = ffmpeg_bin
        self._resolved: tuple[str | int | None, int, int] | None = None

    # --- device / format -------------------------------------------------

    def _resolve(self):
        """Pick the PortAudio device and the stream format once, preferring the
        device's own defaults over guesses when the config doesn't say."""
        if self._resolved is not None:
            return self._resolved
        import sounddevice as sd

        device: str | int | None = self._device
        if isinstance(device, str):
            index = resolve_output_device(list(sd.query_devices()), device)
            if index is None:
                raise RuntimeError(
                    f"no PortAudio output device matches {device!r} "
                    "(list them with: python -m sounddevice)"
                )
            device = index
        rate, channels = self._sample_rate, self._channels
        if rate is None or channels is None:
            info = sd.query_devices(device, "output") if device is not None else {}
            if rate is None:
                rate = int(info.get("default_samplerate") or DEFAULT_SAMPLE_RATE)
            if channels is None:
                channels = min(int(info.get("max_output_channels") or DEFAULT_CHANNELS), 2) or 1
        self._resolved = (device, int(rate), int(channels))
        log.info("local audio out: device=%s rate=%d channels=%d", device, rate, channels)
        return self._resolved

    # --- the adapter-shaped bit the controller calls ---------------------

    async def play_audio(
        self, urls: tuple[str, ...], duration_s: float | None
    ) -> LocalPlaybackHandle:
        """`duration_s` is deliberately ignored: it is the ESTIMATE the rabbit
        path needs because OJN gives no playback events. Here the device tells
        us when the audio actually ended."""
        paths = [local_path_for(u, self._audio_dir) for u in urls]
        missing = [p for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"local playback: missing {missing[0]}")
        cancel = asyncio.Event()
        task = asyncio.create_task(self._play(paths, cancel))
        return LocalPlaybackHandle(task, cancel)

    async def _play(self, paths: list[Path], cancel: asyncio.Event) -> None:
        import sounddevice as sd

        device, rate, channels = self._resolve()
        stream = sd.RawOutputStream(
            samplerate=rate, channels=channels, dtype="int16", device=device
        )
        stream.start()
        try:
            for path in paths:
                if cancel.is_set():
                    break
                await self._play_one(path, stream, rate, channels, cancel)
        finally:
            # abort() drops whatever is still buffered — a cancel must be heard
            # as silence NOW, not after the device drains.
            with contextlib.suppress(Exception):
                stream.abort() if cancel.is_set() else stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    async def _play_one(self, path: Path, stream, rate: int, channels: int, cancel) -> None:
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-nostdin", "-loglevel", "error",
            "-i", str(path),
            "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            assert proc.stdout is not None
            while not cancel.is_set():
                chunk = await proc.stdout.read(_BLOCK_BYTES)
                if not chunk:
                    break
                # stream.write blocks until the device takes the block
                await asyncio.to_thread(stream.write, chunk)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
