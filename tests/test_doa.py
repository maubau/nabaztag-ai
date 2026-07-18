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
        # 5-byte payloads captured on the real XVF3800 (July 2026)
        (bytes([0, 181, 0, 1, 0]), 181, True),
        (bytes([0, 21, 1, 1, 0]), 277, True),
        (bytes([0, 103, 1, 1, 0]), 359, True),
        (bytes([0, 16, 0, 0, 0]), 16, False),
        (bytes([0, 75, 1, 1]), 331, True),  # 331° = 75 + 256, speech active
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


class FakeUsbDevice:
    """Records the control-transfer setup packet and answers like the XVF3800."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.calls: list[tuple] = []

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, length, timeout):
        self.calls.append((bmRequestType, bRequest, wValue, wIndex, length, timeout))
        # hardware behavior: a transfer shorter than status + DOA_VALUE.length
        # (5 bytes) is answered with an error status, not the value
        if length < len(self._payload):
            return bytes([0x42] * length)
        return self._payload


async def test_flex_usb_doa_requests_five_byte_transfer():
    dev = FakeUsbDevice(bytes([0, 181, 0, 1, 0]))  # 181°, speech — hardware sample
    doa = FlexUsbDoa()
    doa._dev = dev  # bypass usb.core.find: no hardware in CI
    assert await doa.read() == DoaReading(angle_deg=181, speech_detected=True)
    (call,) = dev.calls
    bm_request_type, b_request, w_value, w_index, length, _timeout = call
    assert length == 5  # status + DOA_VALUE.length; 4 would fail on hardware
    assert bm_request_type == 0xC0  # vendor, device-to-host
    assert b_request == 0
    assert w_value == FLEX_DOA_CMD | 0x80  # command id with the read bit
    assert w_index == FLEX_DOA_RESID


async def test_flex_usb_doa_fail_open_without_device():
    # no XVF3800 on this machine (or no pyusb): the wrapped provider must
    # degrade to None, never raise — wake/VAD/STT keep running
    assert await FailOpenDoa(FlexUsbDoa()).read() is None
