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
from pathlib import Path

import yaml

# key path → (expected value, why). Checked against the live config.yaml.
CHECKS = {
    ("deepgram", "endpointing"): (100, "Deepgram's recommendation for nova-3 multilingual it/en"),
    ("audio", "capture_device"): (
        "hw:CARD=C16K6Ch,DEV=0",
        "reSpeaker XVF3800 stable ALSA card name (numeric indices drift across reboots)",
    ),
}


def _get(cfg: dict, path: tuple[str, ...]):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--fix"]
    fix = "--fix" in sys.argv
    path = Path(args[0]) if args else Path("config.yaml")
    if not path.exists():
        print(f"config-doctor: {path} not found (nothing to check)")
        return 0

    text = path.read_text()
    cfg = yaml.safe_load(text) or {}
    problems: list[str] = []
    for keypath, (expected, why) in CHECKS.items():
        value, present = _get(cfg, keypath)
        dotted = ".".join(keypath)
        if not present:
            problems.append(f"missing {dotted} (expected {expected!r}) — {why}")
        elif value != expected:
            problems.append(f"{dotted} is {value!r}, expected {expected!r} — {why}")
            if fix:
                leaf = keypath[-1]
                text = re.sub(
                    rf"^(\s*{re.escape(leaf)}\s*:).*$",
                    rf"\g<1> {expected}",
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )

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
