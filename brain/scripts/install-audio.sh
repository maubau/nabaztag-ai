#!/usr/bin/env bash
# Reproducible install of the audio-in extra (Ubuntu 24.04 / Python 3.12).
#
# Why not plain `pip install -e "brain[audio]"` with openwakeword in the
# extra: openwakeword 0.6.0 hard-depends on tflite-runtime on Linux, and PyPI
# has no tflite-runtime wheel for CPython 3.12 — the resolver fails even
# though we only use the ONNX backend (inference_framework="onnx"). So the
# `audio` extra carries openwakeword's declared runtime deps minus
# tflite-runtime, and openwakeword itself goes in pinned with --no-deps.
# Runtime prerequisite (Debian/Ubuntu): libportaudio2 for sounddevice.
#
# Usage, from anywhere, venv active (or PYTHON=/path/to/python):
#   brain/scripts/install-audio.sh [--stt-local]
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHON="${PYTHON:-python3}"
OPENWAKEWORD_VERSION=0.6.0

"$PYTHON" -m pip install -e "brain[audio]"
"$PYTHON" -m pip install --no-deps "openwakeword==${OPENWAKEWORD_VERSION}"
if [[ "${1:-}" == "--stt-local" ]]; then
    "$PYTHON" -m pip install -e "brain[stt-local]"
fi

echo "== import smoke (must work without tflite-runtime) =="
"$PYTHON" - <<'EOF'
import onnxruntime  # noqa: F401
import sounddevice  # noqa: F401  (needs the libportaudio2 shared library)
import usb.core  # noqa: F401
from openwakeword.model import Model  # noqa: F401
from pysilero_vad import SileroVoiceActivityDetector

from rabbit_brain.audio import AlsaCapture, FlexUsbDoa, OpenWakeWordDetector, VoicePipeline  # noqa: F401
from rabbit_brain.stt import make_stt  # noqa: F401

SileroVoiceActivityDetector()  # the silero onnx model ships inside the wheel
print("audio extra OK (openWakeWord on the ONNX backend, no tflite-runtime)")
EOF
