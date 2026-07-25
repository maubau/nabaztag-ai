#!/usr/bin/env python3
"""AEC probe: is the rabbit's own audio actually being cancelled from the mics?

The XVF3800 CAN cancel our playback on-chip, because over USB it takes the
far-end reference from the host. But that is a precondition, not a promise: it
only holds if the audio really leaves through the USB XVF3800 device, lands on
the reference channel the firmware expects, and the physical speaker reproduces
that same signal. Any one of those wrong and the microphone simply hears the
speaker — at which point barge-in fires on the rabbit's own voice and a
full-duplex conversation talks over itself. So MEASURE it before relying on it.

    python brain/scripts/aec-probe.py --device C16K6Ch            # generated noise
    python brain/scripts/aec-probe.py --device C16K6Ch --mp3 www/audio/x.mp3

Three stages:
  1. FLOOR      — record silence: the room's noise floor.
  2. SINGLE-TALK— play a signal and record at the same time. With AEC working
                  the recording stays near the floor; without it, the mic hears
                  the speaker and the level jumps.
  3. DOUBLE-TALK— play the signal again and TALK OVER IT. Your voice must still
                  come through clearly. This is what separates real cancellation
                  from the boring explanations of a quiet stage 2 (speaker off,
                  volume at zero, wrong device, dead mic) — those also suppress
                  YOUR voice, and this stage catches them.

Read the verdict together with the reported levels, not on its own. The probe
reports dB above the noise floor and its own reasoning; it deliberately does not
pretend a single threshold settles it.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import threading
import time

# Levels below this above the floor mean "the mic barely hears it".
QUIET_DB = 6.0
# Above this, the microphone is clearly picking the speaker up.
LOUD_DB = 20.0
_CHUNK = 1024


def _rms(pcm: bytes) -> float:
    import numpy as np

    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def _db_over(level: float, floor: float) -> float:
    """dB of `level` above the noise floor (guarded against silence)."""
    if level <= 0 or floor <= 0:
        return 0.0
    return 20.0 * math.log10(level / floor)


def _noise(seconds: float, rate: int, channels: int) -> bytes:
    """Broadband noise: a better echo test than a pure tone, because AEC copes
    differently with narrowband signals."""
    import numpy as np

    rng = np.random.default_rng(0)
    mono = (rng.normal(0, 0.18, int(rate * seconds)) * 32767).clip(-32768, 32767)
    block = mono.astype(np.int16)
    if channels > 1:
        block = np.repeat(block[:, None], channels, axis=1)
    return block.tobytes()


def _decode_mp3(path: str, rate: int, channels: int) -> bytes:
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
         "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "pipe:1"],
        capture_output=True, check=True,
    )  # fmt: skip
    return out.stdout


class _Recorder:
    """Captures the selected mic channel in the background while we play."""

    def __init__(self, device, rate: int, channels: int, channel: int):
        import sounddevice as sd
        from rabbit_brain.audio.capture import extract_channel

        self._sd, self._extract = sd, extract_channel
        self._device, self._rate = device, rate
        self._channels, self._channel = channels, channel
        self._frames: list[bytes] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        stream = self._sd.RawInputStream(
            samplerate=self._rate, channels=self._channels,
            dtype="int16", device=self._device, blocksize=_CHUNK,
        )  # fmt: skip
        with stream:
            while not self._stop.is_set():
                data, _ = stream.read(_CHUNK)
                self._frames.append(self._extract(bytes(data), self._channels, self._channel))

    def __enter__(self) -> _Recorder:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(0.2)  # let the stream settle before we measure
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    @property
    def level(self) -> float:
        return _rms(b"".join(self._frames))


def _play(pcm: bytes, device, rate: int, channels: int) -> None:
    import sounddevice as sd

    stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", device=device)
    stream.start()
    try:
        view = memoryview(pcm)
        step = _CHUNK * 2 * channels
        for start in range(0, len(view), step):
            stream.write(bytes(view[start : start + step]))
    finally:
        stream.stop()
        stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--device", required=True, help="OUTPUT device (PortAudio name substring)")
    parser.add_argument("--capture-device", default="C16K6Ch", help="input device name substring")
    parser.add_argument("--capture-rate", type=int, default=16000)
    parser.add_argument("--capture-channels", type=int, default=6)
    parser.add_argument(
        "--channel", type=int, default=0, help="mic channel to analyse (0=Conference, 1=ASR)"
    )
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--mp3", help="play this file instead of generated noise (more realistic)")
    args = parser.parse_args()

    import sounddevice as sd
    from rabbit_brain.audio.capture import resolve_input_device
    from rabbit_brain.audio.output import resolve_output_device

    devices = list(sd.query_devices())
    out_index = resolve_output_device(devices, args.device)
    in_index = resolve_input_device(devices, args.capture_device)
    if out_index is None:
        print(f"no output device matches {args.device!r}", file=sys.stderr)
        return 2
    if in_index is None:
        print(f"no input device matches {args.capture_device!r}", file=sys.stderr)
        return 2
    out_name = devices[out_index]["name"]
    info = dict(sd.query_devices(out_index, "output"))
    out_rate = int(info.get("default_samplerate") or args.capture_rate)
    out_channels = min(int(info.get("max_output_channels") or 1), 2) or 1
    print(f"output: [{out_index}] {out_name}  {out_rate} Hz x{out_channels}")
    print(f"input : [{in_index}] {devices[in_index]['name']}  ch{args.channel}")
    # The reference only exists if we play THROUGH the mic array's own device.
    if args.capture_device.lower().split(",")[0] not in out_name.lower():
        print(
            "\nWARNING: the output device does not look like the reSpeaker. The XVF3800\n"
            "only gets a reference for audio that leaves through ITS OWN USB device —\n"
            "playing elsewhere means no cancellation is even possible."
        )

    signal = (
        _decode_mp3(args.mp3, out_rate, out_channels)
        if args.mp3
        else _noise(args.seconds, out_rate, out_channels)
    )

    print("\n[1/3] floor — stay quiet…")
    with _Recorder(in_index, args.capture_rate, args.capture_channels, args.channel) as rec:
        time.sleep(args.seconds)
    floor = rec.level
    print(f"      floor RMS {floor:.1f}")

    print("[2/3] single-talk — playing, STAY QUIET…")
    with _Recorder(in_index, args.capture_rate, args.capture_channels, args.channel) as rec:
        _play(signal, out_index, out_rate, out_channels)
    echo = rec.level
    echo_db = _db_over(echo, floor)
    print(f"      residual RMS {echo:.1f}  ({echo_db:+.1f} dB over floor)")

    print("[3/3] double-talk — playing again: TALK OVER IT, keep talking…")
    time.sleep(0.5)
    with _Recorder(in_index, args.capture_rate, args.capture_channels, args.channel) as rec:
        _play(signal, out_index, out_rate, out_channels)
    both = rec.level
    both_db = _db_over(both, floor)
    voice_over_echo = _db_over(both, echo)
    print(f"      with your voice RMS {both:.1f}  ({both_db:+.1f} dB over floor, "
          f"{voice_over_echo:+.1f} dB over the residual)")  # fmt: skip

    print("\n--- verdict ---")
    if echo_db >= LOUD_DB:
        print(
            f"AEC does NOT look active: the mic hears the playback {echo_db:.1f} dB over the\n"
            "floor. Check that audio really leaves via the reSpeaker's USB device and that\n"
            "the speaker is driven from it. Do NOT enable barge-in yet — it would trigger\n"
            "on the rabbit's own voice."
        )
    elif echo_db <= QUIET_DB and voice_over_echo >= 10.0:
        print(
            f"AEC looks ACTIVE: playback leaves only {echo_db:.1f} dB over the floor, while\n"
            f"your voice still rises {voice_over_echo:.1f} dB above that residual — so the\n"
            "quiet stage 2 is real cancellation, not a dead mic or a silent speaker."
        )
    elif echo_db <= QUIET_DB:
        print(
            f"INCONCLUSIVE: stage 2 was quiet ({echo_db:.1f} dB) but your voice barely showed\n"
            f"in stage 3 ({voice_over_echo:.1f} dB over the residual). That is what a muted\n"
            "speaker or a dead mic also looks like. Confirm you can hear the playback and\n"
            "that stage 3 really had you speaking, then re-run."
        )
    else:
        print(
            f"PARTIAL: {echo_db:.1f} dB of residual — some leakage. Barge-in may false-trigger\n"
            "on loud passages. Lower the speaker level or improve isolation, then re-run."
        )
    print("\nLevels are the evidence; the verdict is a reading of them, not a proof.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
