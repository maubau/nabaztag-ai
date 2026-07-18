"""Direction of arrival from the XVF3800 USB control interface (§6.2.8).

DoA is strictly additive and fail-open: any USB/tool error is logged
(throttled) and read() returns None — wake/VAD/STT never depend on it.

Three backends, selected in config.yaml (`doa.backend`):

- "flex" (default): PyUSB client for the reSpeaker Flex XVF3800 as verified
  on the Bolt (July 2026): DOA_VALUE at resid=20, cmd=18; ONE control read
  returns [status, angle_low, angle_high, speech_detected] — angle 0-359° as
  a 16-bit little-endian pair plus the speech flag, in a single transfer.
  License-clean: written from the documented XMOS device-control transport
  (vendor control transfer, bRequest 0, wValue = command id | read bit,
  wIndex = resource id), no vendor code — the respeaker/reSpeaker_Flex repo
  declares no license, so none of its code is copied into this Apache-2.0 tree.
- "command": run an external ONE-SHOT command and parse the angle from its
  stdout. Not for continuously-running tools (e.g. respeaker_get_doa.py
  never exits and would always hit the timeout).
- "usb": generic XMOS device-control read (status byte + one little-endian
  32-bit value) for other firmware builds; resid/cmd ids from config.

Ear-reflex mapping (angle → EarsCommand) reads the `doa:` section of
moods.yaml; the pipeline submits the result through the BodyController at
DOA_REFLEX priority — never directly to the adapter.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

XVF3800_VID = 0x2886
XVF3800_PID = 0x001E

# reSpeaker Flex firmware command map, hardware-verified on the Bolt (July 2026)
FLEX_DOA_RESID = 20
FLEX_DOA_CMD = 18

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class DoaReading:
    angle_deg: int  # 0..359
    speech_detected: bool | None = None


@runtime_checkable
class DoaProvider(Protocol):
    async def read(self) -> DoaReading: ...


class CommandDoa:
    """Angle via an external host-control tool (kept out of this codebase)."""

    def __init__(self, command: str, speech_command: str | None = None, timeout_s: float = 2.0):
        self._command = command
        self._speech_command = speech_command
        self._timeout_s = timeout_s

    async def _run(self, command: str) -> float:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            raise RuntimeError(f"DoA command timed out: {command!r}") from None
        if proc.returncode != 0:
            raise RuntimeError(f"DoA command failed ({proc.returncode}): {stderr.decode()!r}")
        numbers = _NUMBER_RE.findall(stdout.decode())
        if not numbers:
            raise RuntimeError(f"no number in DoA command output: {stdout.decode()!r}")
        return float(numbers[-1])

    async def read(self) -> DoaReading:
        angle = int(round(await self._run(self._command))) % 360
        speech = None
        if self._speech_command:
            speech = (await self._run(self._speech_command)) != 0
        return DoaReading(angle_deg=angle, speech_detected=speech)


class _XvfUsbBase:
    """Shared PyUSB plumbing for the XMOS device-control USB transport.

    Written from the documented protocol, not from vendor code: a read is a
    vendor control transfer with bRequest 0, wValue = command id with the
    read bit set, wIndex = resource id; the first payload byte is a status
    code (0 = success).
    """

    _READ_BIT = 0x80  # set on the command id for read transfers
    _REQ_TYPE_READ = 0xC0  # vendor | device-to-host
    _STATUS_OK = 0

    def __init__(self, vid: int = XVF3800_VID, pid: int = XVF3800_PID, usb_timeout_ms: int = 500):
        self._vid = vid
        self._pid = pid
        self._timeout_ms = usb_timeout_ms
        self._dev = None

    def _ensure_device(self):
        if self._dev is None:
            import usb.core  # optional dep: 'rabbit-brain[audio]'

            self._dev = usb.core.find(idVendor=self._vid, idProduct=self._pid)
            if self._dev is None:
                raise RuntimeError(
                    f"XVF3800 not found ({self._vid:04x}:{self._pid:04x}) — "
                    "check the udev rule (brain/udev/) and group membership"
                )
        return self._dev

    def _control_read(self, resid: int, cmd: int, length: int) -> bytes:
        dev = self._ensure_device()
        data = bytes(
            dev.ctrl_transfer(
                self._REQ_TYPE_READ,
                0,  # bRequest
                cmd | self._READ_BIT,  # wValue: command id with read bit
                resid,  # wIndex: resource id
                length,
                self._timeout_ms,
            )
        )
        if len(data) < 1 or data[0] != self._STATUS_OK:
            raise RuntimeError(f"XVF3800 control read failed, status {data[:1]!r}")
        return data


def decode_flex_doa(data: bytes) -> DoaReading:
    """Decode the Flex DOA_VALUE payload: [status, angle_low, angle_high, speech].

    The angle is a 16-bit little-endian degree value (0-359); byte 3 is the
    SPEECH_DETECTED flag. NOT a packed float/int32 — decoding bytes 1-4 as one
    number corrupts the angle whenever the speech byte is set.
    """
    if len(data) < 4:
        raise RuntimeError(f"Flex DOA payload too short: {data!r}")
    angle = (data[1] + 256 * data[2]) % 360
    return DoaReading(angle_deg=angle, speech_detected=bool(data[3]))


class FlexUsbDoa(_XvfUsbBase):
    """reSpeaker Flex XVF3800 DoA — angle and speech flag in one USB read."""

    def __init__(
        self,
        resid: int = FLEX_DOA_RESID,
        cmd: int = FLEX_DOA_CMD,
        vid: int = XVF3800_VID,
        pid: int = XVF3800_PID,
        usb_timeout_ms: int = 500,
    ):
        super().__init__(vid, pid, usb_timeout_ms)
        self._resid = resid
        self._cmd = cmd

    def _read_sync(self) -> DoaReading:
        return decode_flex_doa(self._control_read(self._resid, self._cmd, 4))

    async def read(self) -> DoaReading:
        return await asyncio.to_thread(self._read_sync)


class XvfUsbDoa(_XvfUsbBase):
    """Generic XMOS device-control value read (status + one 32-bit value).

    For firmware builds with a different command map than the Flex; resid/cmd
    ids and the value format come from config.yaml (`doa.usb`).
    """

    def __init__(
        self,
        resid: int,
        cmd_doa: int,
        cmd_speech: int | None = None,
        value_format: str = "f",  # struct format of the value: "f" or "i"
        vid: int = XVF3800_VID,
        pid: int = XVF3800_PID,
        usb_timeout_ms: int = 500,
    ):
        super().__init__(vid, pid, usb_timeout_ms)
        self._resid = resid
        self._cmd_doa = cmd_doa
        self._cmd_speech = cmd_speech
        self._value_format = value_format

    def _read_value(self, cmd: int) -> float:
        data = self._control_read(self._resid, cmd, 5)  # status + 32-bit value
        (value,) = struct.unpack("<" + self._value_format, data[1:5])
        return float(value)

    async def read(self) -> DoaReading:
        angle = int(round(await asyncio.to_thread(self._read_value, self._cmd_doa))) % 360
        speech = None
        if self._cmd_speech is not None:
            speech = (await asyncio.to_thread(self._read_value, self._cmd_speech)) != 0
        return DoaReading(angle_deg=angle, speech_detected=speech)


class FailOpenDoa:
    """Wrap any DoaProvider so failures degrade to 'no reading', never a crash."""

    def __init__(self, inner: DoaProvider, log_every_s: float = 60.0):
        self._inner = inner
        self._log_every_s = log_every_s
        self._last_logged = 0.0

    async def read(self) -> DoaReading | None:
        try:
            return await self._inner.read()
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_logged >= self._log_every_s:
                self._last_logged = now
                log.warning("DoA unavailable (fail-open, pipeline continues): %s", exc)
            return None


def make_doa(config: dict[str, Any]) -> FailOpenDoa | None:
    """Build the DoA provider from config.yaml's `doa:` section (None if disabled)."""
    cfg = config.get("doa", {})
    if not cfg.get("enabled", False):
        return None
    backend = cfg.get("backend", "flex")
    if backend == "flex":
        flex_cfg = cfg.get("flex", {})
        inner: DoaProvider = FlexUsbDoa(
            resid=flex_cfg.get("resid", FLEX_DOA_RESID),
            cmd=flex_cfg.get("cmd", FLEX_DOA_CMD),
        )
    elif backend == "command":
        inner = CommandDoa(
            command=cfg["command"],
            speech_command=cfg.get("speech_command"),
            timeout_s=cfg.get("timeout_s", 2.0),
        )
    elif backend == "usb":
        usb_cfg = cfg["usb"]
        inner = XvfUsbDoa(
            resid=usb_cfg["resid"],
            cmd_doa=usb_cfg["cmd_doa"],
            cmd_speech=usb_cfg.get("cmd_speech"),
            value_format=usb_cfg.get("value_format", "f"),
        )
    else:
        raise ValueError(f"unknown doa.backend {backend!r} (expected 'flex', 'command' or 'usb')")
    return FailOpenDoa(inner)


def angle_to_ears(angle_deg: int, doa_config: dict[str, Any]) -> tuple[int, int] | None:
    """Map a DoA angle to the ear bias from moods.yaml's `doa.sectors`.

    Sectors may wrap around 0° (e.g. from: 315, to: 45 for "front").
    """
    angle = angle_deg % 360
    for sector in doa_config.get("sectors", []):
        lo, hi = sector["from"] % 360, sector["to"] % 360
        inside = lo <= angle < hi if lo <= hi else angle >= lo or angle < hi
        if inside:
            ears = sector["ears"]
            return ears["left"], ears["right"]
    return None
