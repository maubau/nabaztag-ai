import sys
from pathlib import Path

import pytest
import yaml
from rabbit_brain.audio.doa import (
    FLEX_DOA_CMD,
    FLEX_DOA_RESID,
    CommandDoa,
    DoaReading,
    FailOpenDoa,
    FlexUsbDoa,
    angle_to_ears,
    decode_flex_doa,
    make_doa,
)

MOODS_DOA = yaml.safe_load((Path(__file__).parent.parent / "moods.yaml").read_text())["doa"]


@pytest.mark.parametrize(
    ("angle", "ears"),
    [
        (0, (4, 4)),  # front
        (350, (4, 4)),  # front, wrapped around 0°
        (44, (4, 4)),
        (90, (2, 8)),  # right
        (180, (12, 12)),  # behind
        (270, (8, 2)),  # left
        (315, (4, 4)),  # sector boundary belongs to the next sector
    ],
)
def test_angle_to_ears_sectors(angle, ears):
    assert angle_to_ears(angle, MOODS_DOA) == ears


def test_angle_to_ears_no_sectors():
    assert angle_to_ears(90, {}) is None


async def test_command_doa_parses_last_number():
    doa = CommandDoa(f"{sys.executable} -c \"print('DOA_VALUE 3: 123')\"")
    assert await doa.read() == DoaReading(angle_deg=123, speech_detected=None)


async def test_command_doa_wraps_and_reads_speech():
    py = sys.executable
    doa = CommandDoa(
        command=f'{py} -c "print(370)"',
        speech_command=f'{py} -c "print(1)"',
    )
    assert await doa.read() == DoaReading(angle_deg=10, speech_detected=True)


async def test_command_doa_failure_raises():
    with pytest.raises(RuntimeError):
        await CommandDoa("false").read()
    with pytest.raises(RuntimeError):
        await CommandDoa("echo no numbers here").read()


async def test_fail_open_swallows_everything():
    failing = FailOpenDoa(CommandDoa("false"))
    assert await failing.read() is None  # never raises: DoA must not stop the pipeline
    working = FailOpenDoa(CommandDoa(f'{sys.executable} -c "print(90)"'))
    reading = await working.read()
    assert reading is not None and reading.angle_deg == 90


@pytest.mark.parametrize(
    ("payload", "angle", "speech"),
    [
        (bytes([0, 16, 0, 0]), 16, False),  # measured on hardware: 16°
        (bytes([0, 75, 1, 1]), 331, True),  # 331° = 75 + 256, speech active
        (bytes([0, 102, 1, 0]), 358, False),  # 358°
        (bytes([0, 0, 0, 1]), 0, True),
    ],
)
def test_decode_flex_doa(payload, angle, speech):
    # angle is a 16-bit LE pair + separate speech byte — decoding bytes 1-4 as
    # one int32 would corrupt the angle whenever the speech byte is set
    assert decode_flex_doa(payload) == DoaReading(angle_deg=angle, speech_detected=speech)


def test_decode_flex_doa_short_payload():
    with pytest.raises(RuntimeError, match="too short"):
        decode_flex_doa(bytes([0, 16]))


def test_make_doa_disabled_and_backends():
    assert make_doa({"doa": {"enabled": False}}) is None
    assert make_doa({}) is None
    # default backend is the hardware-verified Flex decoder
    default = make_doa({"doa": {"enabled": True}})
    assert isinstance(default, FailOpenDoa)
    assert isinstance(default._inner, FlexUsbDoa)
    assert (default._inner._resid, default._inner._cmd) == (FLEX_DOA_RESID, FLEX_DOA_CMD)
    provider = make_doa({"doa": {"enabled": True, "backend": "command", "command": "echo 1"}})
    assert isinstance(provider._inner, CommandDoa)
    with pytest.raises(ValueError):
        make_doa({"doa": {"enabled": True, "backend": "ouija"}})


async def test_flex_usb_doa_fail_open_without_device():
    # no XVF3800 on this machine (or no pyusb): the wrapped provider must
    # degrade to None, never raise — wake/VAD/STT keep running
    assert await FailOpenDoa(FlexUsbDoa()).read() is None
