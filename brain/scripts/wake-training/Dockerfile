# Pinned Python 3.10 image for wake-word TRAINING only (build-time tool).
#
# Why a container: the pinned generator (rhasspy/piper-sample-generator@v2.0.0)
# hard-requires piper-phonemize==1.1.0, which has NO Linux wheel for CPython
# 3.12 — the Bolt's runtime Python. 3.10 does have the wheel. The Nabaztag
# RUNTIME stays in its own 3.12 venv, untouched; only training runs in here.
#
# The image carries NO Python deps: the training venv, the two pinned checkouts,
# the corpora and the output ONNX all live in the bind-mounted repo (under
# models/wake/.training), so they persist across runs and the ~GB of downloads
# are not baked into the image. The image only provides the 3.10 interpreter and
# the system libraries the pipeline needs (libsndfile for soundfile, ffmpeg,
# espeak-ng data for piper-phonemize).
#
# Pinned by tag; train-wake-word.sh builds it as nabaztag-wake-training:py310.

FROM python:3.10.15-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        ffmpeg \
        libsndfile1 \
        espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Training is invoked as `bash brain/scripts/train-wake-word.sh <stage>` with the
# repo bind-mounted at its own host path; nothing to copy in.
WORKDIR /work
CMD ["bash"]
