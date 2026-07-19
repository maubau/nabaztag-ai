#!/usr/bin/env python3
"""Warn about (and optionally fix) drift in a local config.yaml.

config.yaml is gitignored and was created from an OLD config.example.yaml, so
`git pull` never updates it. This flags settings that have since changed in a
way that silently degrades behavior, and with --fix rewrites just those keys
(preserving the rest of the file and its comments via a targeted line edit).

    brain/scripts/config-doctor.py [config.yaml] [--fix]
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

_MISSING = object()


@dataclass(frozen=True)
class Check:
    path: tuple[str, ...]
    expected: object  # the value --fix writes
    why: str
    required: bool = True  # a missing key is a problem
    # returns True when a PRESENT value is stale and should be migrated;
    # default is exact-mismatch against `expected`
    is_stale: Callable[[object], bool] | None = None

    def stale(self, value: object) -> bool:
        if self.is_stale is not None:
            return self.is_stale(value)
        return value != self.expected


def _is_anthropic_model(value: object) -> bool:
    return isinstance(value, str) and value.lower().startswith("claude")


CHECKS: list[Check] = [
    Check(
        ("deepgram", "endpointing"),
        100,
        "Deepgram's recommendation for nova-3 multilingual it/en",
    ),
    Check(
        ("audio", "capture_device"),
        "hw:CARD=C16K6Ch,DEV=0",
        "reSpeaker XVF3800 stable ALSA card name (numeric indices drift across reboots)",
    ),
    Check(
        ("audio", "vad_end_of_speech_ms"),
        1600,
        "natural brief pauses must not close the utterance prematurely",
    ),
    # LLM provider migration: OpenAI is the active provider now. A legacy
    # config's provider defaults to openai in code, so only flag an explicit
    # anthropic; but a legacy `model: claude-*` WOULD be sent to OpenAI —
    # migrate it. Both fixes are idempotent (re-running is a no-op).
    Check(
        ("llm", "provider"),
        "openai",
        "OpenAI is the active LLM provider (§6.2.5)",
        required=False,
        is_stale=lambda v: v == "anthropic",
    ),
    Check(
        ("llm", "model"),
        "gpt-5.4-mini",
        "an Anthropic model would be sent to the OpenAI provider",
        required=False,
        is_stale=_is_anthropic_model,
    ),
]


def _get(cfg: dict, path: tuple[str, ...]):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING, False
        node = node[key]
    return node, True


def _rewrite_in_section(text: str, section: str, leaf: str, expected: object) -> str:
    """Rewrite `leaf:` only inside the top-level `section:` block. A leaf like
    `model` appears under several sections (deepgram, llm, …), so a global
    replace would hit the wrong line."""
    lines = text.splitlines(keepends=True)
    in_section = False
    for i, line in enumerate(lines):
        body = line.rstrip("\n")
        if re.match(rf"^{re.escape(section)}\s*:", body):
            in_section = True
            continue
        if not in_section:
            continue
        if re.match(r"^\S", body) and not body.lstrip().startswith("#"):
            break  # a new top-level key ended the section without a match
        m = re.match(rf"^(\s+{re.escape(leaf)}\s*:).*$", body)
        if m:
            lines[i] = m.group(1) + f" {expected}" + ("\n" if line.endswith("\n") else "")
            break
    return "".join(lines)


def diagnose(text: str, fix: bool) -> tuple[str, list[str]]:
    """Return (possibly-rewritten text, list of problem messages)."""
    cfg = yaml.safe_load(text) or {}
    problems: list[str] = []
    for check in CHECKS:
        value, present = _get(cfg, check.path)
        dotted = ".".join(check.path)
        if not present:
            if check.required:
                problems.append(f"missing {dotted} (expected {check.expected!r}) — {check.why}")
            continue
        if check.stale(value):
            problems.append(f"{dotted} is {value!r}, expected {check.expected!r} — {check.why}")
            if fix:
                text = _rewrite_in_section(text, check.path[0], check.path[-1], check.expected)
    return text, problems


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--fix"]
    fix = "--fix" in sys.argv
    path = Path(args[0]) if args else Path("config.yaml")
    if not path.exists():
        print(f"config-doctor: {path} not found (nothing to check)")
        return 0

    text, problems = diagnose(path.read_text(), fix)
    if not problems:
        print(f"config-doctor: {path} OK")
        return 0
    print(f"config-doctor: {path} needs attention:")
    for p in problems:
        print(f"  - {p}")
    if fix:
        path.write_text(text)
        print("config-doctor: applied fixes for present keys (add missing keys by hand)")
        return 0
    print("Re-run with --fix to update present keys in place.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
