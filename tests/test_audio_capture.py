import array
import wave

import pytest
from rabbit_brain.audio.capture import (
    AlsaCapture,
    WavCapture,
    extract_channel,
    resolve_input_device,
)


def test_alsa_capture_default_queue_blocks_has_headroom():
    # ~9.6s @ 32ms/block (hardware round, July 2026: the old 64-block/~2s
    # buffer overflowed under real agent+TTS+playback turns).
    assert AlsaCapture()._queue_blocks == 300


async def test_alsa_capture_frames_rejects_a_second_consumer():
    """Two live consumers would fight over the same ALSA device; the guard
    fires before ever touching sounddevice (the check runs first in
    frames())."""
    capture = AlsaCapture()
    capture._started = True  # simulate an already-running consumer
    gen = capture.frames()
    with pytest.raises(RuntimeError, match="only one consumer"):
        await anext(gen)


def make_multichannel_wav(path, channels=6, sample_rate=16_000, frames=2048):
    """Fixture WAV where channel c sample n is c*1000 + (n % 500) — every
    channel is distinguishable, so extraction bugs can't cancel out."""
    data = array.array("h")
    for n in range(frames):
        for c in range(channels):
            data.append(c * 1000 + (n % 500))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())
    return path


def test_extract_channel_picks_the_right_samples():
    channels = 6
    interleaved = array.array("h")
    for n in range(100):
        for c in range(channels):
            interleaved.append(c * 1000 + n)
    for c in range(channels):
        mono = array.array("h")
        mono.frombytes(extract_channel(interleaved.tobytes(), channels, c))
        assert list(mono) == [c * 1000 + n for n in range(100)]


def test_extract_channel_mono_passthrough_and_bounds():
    pcm = b"\x01\x00\x02\x00"
    assert extract_channel(pcm, 1, 0) is pcm
    with pytest.raises(ValueError):
        extract_channel(pcm, 2, 2)


async def test_wav_capture_extracts_selected_channel(tmp_path):
    path = make_multichannel_wav(tmp_path / "six.wav", frames=1024)
    capture = WavCapture(path, selected_channel=3, block_samples=256)
    assert capture.sample_rate == 16_000
    blocks = [b async for b in capture.frames()]
    assert len(blocks) == 4  # 1024 frames / 256 per block
    mono = array.array("h")
    for b in blocks:
        mono.frombytes(b)
    assert list(mono) == [3 * 1000 + (n % 500) for n in range(1024)]


async def test_wav_capture_channel_zero_matches_respeaker_config(tmp_path):
    # the production config: 6 channels, selected_channel 0
    path = make_multichannel_wav(tmp_path / "six.wav", frames=512)
    capture = WavCapture(path, selected_channel=0)
    (block,) = [b async for b in capture.frames()]
    mono = array.array("h")
    mono.frombytes(block)
    assert list(mono) == [n % 500 for n in range(512)]


# A realistic PortAudio device listing on the Bolt: the ALSA PCM string is not
# a valid PortAudio name — resolution goes through the card token instead.
PORTAUDIO_DEVICES = [
    {"name": "HDA Intel: ALC888 (hw:0,0)", "max_input_channels": 2},
    {"name": "reSpeaker XVF3800: USB Audio (hw:1,0)", "max_input_channels": 0},  # output side
    {"name": "C16K6Ch: USB Audio (hw:2,0)", "max_input_channels": 6},
    {"name": "PC-LM1E: USB Audio (hw:3,0)", "max_input_channels": 1},
]


def test_resolve_input_device_by_card_token():
    assert resolve_input_device(PORTAUDIO_DEVICES, "hw:CARD=C16K6Ch,DEV=0") == 2
    assert resolve_input_device(PORTAUDIO_DEVICES, "plughw:CARD=PCLM1E,DEV=0") is None  # no match
    assert resolve_input_device(PORTAUDIO_DEVICES, "pc-lm1e") == 3  # plain substring works too


def test_resolve_input_device_skips_output_only():
    devices = [{"name": "C16K6Ch: USB Audio", "max_input_channels": 0}]
    assert resolve_input_device(devices, "hw:CARD=C16K6Ch,DEV=0") is None


def test_wav_capture_rejects_non_16bit(tmp_path):
    path = tmp_path / "eight.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(16_000)
        w.writeframes(b"\x00" * 64)
    with pytest.raises(ValueError, match="16-bit"):
        WavCapture(path)
