"""Direction of arrival from the XVF3800 USB control interface (§6.2.8).

DoA is strictly additive and fail-open: any USB/tool error is logged
(throttled) and read() returns None — wake/VAD/STT never depend on it.

Two backends, selected in config.yaml (`doa.backend`):

- "command" (default): run an external command (the official XMOS/Seeed host
  tool, e.g. `xvf_host GET_DOA_VALUE`) and parse the angle from its stdout.
  The tool stays an EXTERNAL dependency: the respeaker/reSpeaker_Flex repo
  declares no license, so none of its code is copied into this Apache-2.0
  tree (see docs/OJN_API_NOTES.md licensing note).
- "usb": our own PyUSB client speaking the documented XMOS device-control
  USB transport (vendor control transfers: bRequest 0, wValue = command id,
  wIndex = resource id; on reads the first payload byte is a status code,
  the rest is the little-endian value). The numeric resource/command ids are
  firmware-build specific and therefore config, not constants — verify them
  on the Bolt against the official tool before switching backends.

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


class XvfUsbDoa:
    """License-clean PyUSB client for the XMOS device-control USB transport.

    Written from the documented protocol, not from vendor code. resid/cmd
    numeric ids depend on the firmware's command map: take them from the
    official tool's YAML and put them in config.yaml (`doa.usb`).
    """

    _READ_BIT = 0x80  # set on the command id for read transfers
    _REQ_TYPE_READ = 0xC0  # vendor | device-to-host
    _STATUS_OK = 0

    def __init__(
        self,
        resid: int,
        cmd_doa: int,
        cmd_speech: int | None = None,
        payload_len: int = 5,  # 1 status byte + one 32-bit value
        value_format: str = "f",  # struct format of the value: "f" or "i"
        vid: int = XVF3800_VID,
        pid: int = XVF3800_PID,
        usb_timeout_ms: int = 500,
    ):
        self._resid = resid
        self._cmd_doa = cmd_doa
        self._cmd_speech = cmd_speech
        self._payload_len = payload_len
        self._value_format = value_format
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

    def _control_read(self, cmd: int) -> float:
        dev = self._ensure_device()
        data = dev.ctrl_transfer(
            self._REQ_TYPE_READ,
            0,  # bRequest
            cmd | self._READ_BIT,  # wValue: command id with read bit
            self._resid,  # wIndex: resource id
            self._payload_len,
            self._timeout_ms,
        )
        if len(data) < 1 or data[0] != self._STATUS_OK:
            raise RuntimeError(f"XVF3800 control read failed, status {data[:1]!r}")
        (value,) = struct.unpack("<" + self._value_format, bytes(data[1:5]))
        return float(value)

    async def read(self) -> DoaReading:
        angle = int(round(await asyncio.to_thread(self._control_read, self._cmd_doa))) % 360
        speech = None
        if self._cmd_speech is not None:
            speech = (await asyncio.to_thread(self._control_read, self._cmd_speech)) != 0
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
    backend = cfg.get("backend", "command")
    if backend == "command":
        inner: DoaProvider = CommandDoa(
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
        raise ValueError(f"unknown doa.backend {backend!r} (expected 'command' or 'usb')")
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
