#!/usr/bin/env bash
# Train the custom "Nabaztag" wake-word model (openWakeWord), ONNX-ONLY,
# in a PINNED PYTHON 3.10 CONTAINER (build-time only; the runtime is untouched).
#
# WHY A CONTAINER (Bolt probe, July 2026): the pinned generator hard-requires
# piper-phonemize==1.1.0, which has NO cp312 Linux wheel — the Bolt's runtime
# Python. Rather than hack around a missing wheel, training runs in a Python
# 3.10 image (brain/scripts/wake-training/Dockerfile) where the wheel exists.
# The stages below run INSIDE that image (the script re-invokes itself there);
# the venv/checkouts/corpora/ONNX live in the bind-mounted repo so they persist.
# The Nabaztag runtime stays in its own 3.12 venv — this never touches it.
#
#   brain/scripts/train-wake-word.sh setup      # venv + pinned checkouts + voice
#   brain/scripts/train-wake-word.sh smoke      # CHEAP end-to-end: few clips, 2
#                                               #   steps, ONNX export. Run FIRST.
#   brain/scripts/train-wake-word.sh datasets   # the multi-GB corpora (only after
#                                               #   smoke passes on the Bolt)
#   brain/scripts/train-wake-word.sh config     # write the full training config
#   brain/scripts/train-wake-word.sh train      # full run -> models/wake/*.onnx
#   brain/scripts/train-wake-word.sh validate   # score real WAVs against the model
#   brain/scripts/train-wake-word.sh build      # (re)build the training image
#   brain/scripts/train-wake-word.sh provenance # (re)write the provenance record
#   brain/scripts/train-wake-word.sh printconfig {smoke|full}   # config to stdout
#
# WHY THIS SHAPE (a first Bolt probe of the naive version failed — every guess it
# made was wrong; these are the corrected, VERIFIED facts against the pins below):
#   * openWakeWord 0.6.0 and the generator are an ATOMIC PAIR, both pinned to a
#     SHA. train.py does `from generate_samples import generate_samples`, which
#     needs the generator's TOP-LEVEL generate_samples.py. rhasspy's generator
#     has that at the v2.0.0 tag but NOT on master (3.2.0 moved it into a
#     package) — so master is not just unpinned, it is INCOMPATIBLE. Verified:
#     v2.0.0 (195e3bd9) ships generate_samples.py AND requirements.txt.
#   * ONNX-ONLY on Python 3.12. openWakeWord's base deps drag in tflite-runtime
#     (no cp312 wheel) and its `full` extra pins tensorflow-cpu==2.8.1 (no cp312
#     wheel) — both only needed for the TFLite conversion we do NOT want (the
#     runtime uses onnxruntime). So: explicit training deps, openWakeWord with
#     --no-deps, and NO tensorflow/onnx_tf. train.py exports the .onnx BEFORE it
#     calls convert_onnx_to_tflite (verified, lines ~559-560), and TF is imported
#     lazily INSIDE that call — so without TF the .onnx lands and only the final
#     TFLite step raises. `_train_onnx_only` tolerates exactly that tail error,
#     and only if the .onnx actually exists.
#   * `tts_language` is read by NOTHING in train.py — the naive config's bilingual
#     claim was fiction. True it/en coverage needs generating clips with an
#     Italian piper voice too (a second pass); the generator's v2.0.0 release
#     ships only en_US-libritts_r. Tracked as a follow-up; this trains on the
#     English voice with a couple of spellings, honestly labelled.
#   * There is no `data.py --output_dir` CLI; corpora come from the HF/GitHub
#     URLs in `datasets` below (taken from the pinned notebook).
#
# BOUNDARY / LICENCES: openWakeWord is Apache-2.0 (dscripka/openWakeWord); the
# generator is a BUILD-TIME tool. Neither is imported by the brain and nothing is
# vendored. The trained .onnx is OURS; provenance (phrase, SHAs, corpora, date)
# is recorded next to it. Confirm each corpus's licence before redistributing a
# model trained on it. Weights stay out of git (models/wake/ is gitignored); the
# .provenance.md is kept.
#
# The runtime only ever loads the resulting models/wake/nabaztag.onnx.

set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_DIR="$(pwd)"
WAKE_DIR="${WAKE_DIR:-$REPO_DIR/models/wake}"
TRAIN_DIR="${TRAIN_DIR:-$WAKE_DIR/.training}"
VENV="$TRAIN_DIR/venv"
PY="$VENV/bin/python"
DATA="$TRAIN_DIR/data"
OWW_DIR="$TRAIN_DIR/openWakeWord"
GEN_DIR="$TRAIN_DIR/piper-sample-generator"

# ATOMIC PAIR — both pinned to a SHA (never a branch/tag). Bump deliberately:
# a different commit is a different model and must be re-recorded in provenance.
OWW_REPO="https://github.com/dscripka/openWakeWord"
OWW_REF="c8ef6912c5feccf1037b852d9bc6c7ed644135ba"          # tag v0.6.0
GEN_REPO="https://github.com/rhasspy/piper-sample-generator"
GEN_REF="195e3bd967d54589c2137c9de2b22ad526ba6b6f"          # tag v2.0.0 (has generate_samples.py)

VOICE_PT="en_US-libritts_r-medium.pt"
VOICE_URL="$GEN_REPO/releases/download/v2.0.0/$VOICE_PT"
# The one modest feature file: negatives for the smoke AND the false-positive
# validation set for the full run (the multi-GB ACAV file is `datasets`-only).
VAL_FEATURES_URL="https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy"
ACAV_FEATURES_URL="https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"

MODEL_NAME="${MODEL_NAME:-nabaztag}"

# Paired, VERIFIED torch/torchaudio (NOT two separate "latest" installs — that
# gave the Bolt torch 2.13.0 + torchaudio 2.11.0, a mismatch). Same version
# number is the torch↔torchaudio rule; 2.2.2 has cp310 CPU wheels.
TORCH_VER="${TORCH_VER:-2.2.2}"
TORCHAUDIO_VER="${TORCHAUDIO_VER:-2.2.2}"

# Pinned Python 3.10 training image (see wake-training/Dockerfile).
IMAGE="${WAKE_IMAGE:-nabaztag-wake-training:py310}"
DOCKERFILE_DIR="$REPO_DIR/brain/scripts/wake-training"

log() { printf '\n== %s\n' "$*"; }

_require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "docker is required for wake-word training (piper-phonemize has no" \
         "cp312 wheel; training runs in a pinned Python 3.10 image)" >&2
    exit 1
  }
}

_build_image() {
  _require_docker
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log "building pinned training image $IMAGE (Python 3.10)"
    docker build -t "$IMAGE" "$DOCKERFILE_DIR"
  fi
}

# Run a stage INSIDE the container, with the repo bind-mounted at its OWN host
# path so every path in the config/venv is identical inside and out.
#
# --user maps the HOST UID:GID into the container: without it the container is
# root, git flags the bind-mounted checkouts as "dubious ownership", and every
# file it writes (the venv!) lands root-owned in the bind mount. We do NOT paper
# over that with safe.directory. Because that UID has no /etc/passwd entry, we
# also give it a writable HOME (in the bind mount) and USER/LOGNAME, so anything
# that calls getpass.getuser()/expanduser() or wants a pip/HF cache still works.
_in_container() {  # $@ = stage + args
  _build_image
  local home="$TRAIN_DIR/home"
  mkdir -p "$home"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$REPO_DIR":"$REPO_DIR" -w "$REPO_DIR" \
    -e WAKE_IN_CONTAINER=1 -e "MODEL_NAME=$MODEL_NAME" \
    -e "HOME=$home" -e USER=trainer -e LOGNAME=trainer \
    -e "PIP_CACHE_DIR=$home/.cache/pip" -e "XDG_CACHE_HOME=$home/.cache" \
    "$IMAGE" bash "brain/scripts/train-wake-word.sh" "$@"
}

_clone_pinned() {  # $1=repo $2=sha $3=dir
  if [ ! -d "$3/.git" ]; then
    git clone "$1" "$3"
  fi
  git -C "$3" fetch --quiet origin "$2" 2>/dev/null || git -C "$3" fetch --quiet origin
  git -C "$3" checkout --quiet "$2"
  echo "   $3 @ $(git -C "$3" rev-parse HEAD)"
}

cmd_setup() {
  # Recreate the venv from scratch: an earlier probe left a Python 3.12 venv
  # here, incompatible with this 3.10 container. Checkouts (plain git) are kept.
  # Idempotent: if a prior root-owned venv can't be removed (an earlier run went
  # in as root), say exactly how to repair it once rather than failing cryptically.
  log "training venv (Python 3.10, in-container; recreated): $VENV"
  if [ -d "$VENV" ]; then
    rm -rf "$VENV" 2>/dev/null || true
    if [ -d "$VENV" ]; then
      echo "cannot remove $VENV (likely root-owned by an earlier root container run)." >&2
      echo "Repair ONCE on the host, then re-run setup:" >&2
      echo "  sudo chown -R \$(id -un):\$(id -gn) $TRAIN_DIR" >&2
      exit 1
    fi
  fi
  mkdir -p "$DATA"
  python3 -m venv "$VENV"
  "$PY" -m pip install --upgrade pip >/dev/null

  log "pinned checkouts (atomic pair)"
  _clone_pinned "$OWW_REPO" "$OWW_REF" "$OWW_DIR"
  _clone_pinned "$GEN_REPO" "$GEN_REF" "$GEN_DIR"

  log "torch/torchaudio $TORCH_VER (paired, CPU)"
  "$PY" -m pip install --index-url https://download.pytorch.org/whl/cpu \
    "torch==$TORCH_VER" "torchaudio==$TORCHAUDIO_VER"

  log "ONNX-only training deps (NO tensorflow / onnx_tf / tflite-runtime)"
  # The exact upstream training deps MINUS the TFLite-only trio, plus soundfile
  # (the datasets stage decodes audio). piper-phonemize==1.1.0 resolves here
  # because this is Python 3.10; on 3.12 it has no wheel (the whole reason for
  # the container).
  "$PY" -m pip install \
    "piper-phonemize==1.1.0" webrtcvad "mutagen==1.47.0" "torchinfo==1.8.0" \
    "torchmetrics==1.2.0" "speechbrain==0.5.14" "audiomentations==0.33.0" \
    "torch-audiomentations==0.11.0" "acoustics==0.2.6" "pronouncing==0.2.0" \
    "datasets==2.14.6" "deep-phonemizer==0.0.19" soundfile onnx
  # The generator's own deps (it has a real requirements.txt at v2.0.0).
  "$PY" -m pip install -r "$GEN_DIR/requirements.txt"
  # openWakeWord itself WITHOUT deps, so its tflite-runtime base dep is skipped.
  "$PY" -m pip install --no-deps -e "$OWW_DIR"

  log "verifying imports (fail fast if a wheel/pin is wrong)"
  # The three that matter: piper_phonemize (the cp312 blocker), the generator's
  # top-level generate_samples module train.py imports, and openwakeword itself.
  "$PY" - "$GEN_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import piper_phonemize  # noqa: F401 — the piece with no cp312 wheel
import openwakeword  # noqa: F401
import generate_samples  # noqa: F401 — top-level module train.py imports
print("imports OK: piper_phonemize, openwakeword, generate_samples")
PY

  log "voice model for clip generation"
  mkdir -p "$GEN_DIR/models"
  if [ ! -s "$GEN_DIR/models/$VOICE_PT" ]; then
    curl -fSL "$VOICE_URL" -o "$GEN_DIR/models/$VOICE_PT"
  fi

  # Fail fast if anything landed root-owned: that would mean the container is NOT
  # running as the host UID, and the next run would hit git's dubious-ownership
  # wall again. With --user mapping this stays empty.
  log "checking no root-owned files leaked into the bind mount"
  local rooted
  rooted="$(find "$TRAIN_DIR" -uid 0 -print -quit 2>/dev/null || true)"
  if [ -n "$rooted" ]; then
    echo "FAILED: root-owned files under $TRAIN_DIR (e.g. $rooted)." >&2
    echo "The container is not running as the host UID. Repair once:" >&2
    echo "  sudo chown -R \$(id -un):\$(id -gn) $TRAIN_DIR" >&2
    exit 1
  fi
  log "setup done. NEXT: smoke (cheap end-to-end) BEFORE datasets."
}

# Run the 3-step pipeline ONNX-only: tolerate the final TFLite failure iff the
# .onnx was actually produced (train.py writes ONNX before it touches TFLite).
_train_onnx_only() {  # $1=config $2=expected_onnx
  "$PY" "$OWW_DIR/openwakeword/train.py" --training_config "$1" --generate_clips
  "$PY" "$OWW_DIR/openwakeword/train.py" --training_config "$1" --augment_clips
  set +e
  "$PY" "$OWW_DIR/openwakeword/train.py" --training_config "$1" --train_model
  local rc=$?
  set -e
  if [ ! -s "$2" ]; then
    echo "FAILED: no ONNX at $2 (train.py exit $rc) — real error, see log above" >&2
    return 1
  fi
  [ "$rc" -eq 0 ] || echo "   (train.py exit $rc AFTER the ONNX export — TFLite step skipped, expected)"
}

_fetch_validation_features() {
  if [ ! -s "$DATA/validation_set_features.npy" ]; then
    log "downloading validation feature set (negatives for the smoke)"
    curl -fSL "$VAL_FEATURES_URL" -o "$DATA/validation_set_features.npy"
  fi
}

cmd_smoke() {
  [ -x "$PY" ] || { echo "run 'setup' first" >&2; exit 1; }
  _fetch_validation_features
  local cfg="$TRAIN_DIR/$MODEL_NAME-smoke.yaml"
  _write_config smoke "$cfg"
  log "SMOKE: few clips, 2 steps, ONNX export (proves the path before the corpora)"
  local onnx="$TRAIN_DIR/$MODEL_NAME-smoke/$MODEL_NAME.onnx"
  _train_onnx_only "$cfg" "$onnx"
  log "SMOKE PASSED: $onnx ($(wc -c <"$onnx") bytes). Now: datasets, then config, then train."
}

cmd_datasets() {
  [ -x "$PY" ] || { echo "run 'setup' first" >&2; exit 1; }
  _fetch_validation_features
  log "downloading the FULL corpora (multi-GB — only worth it once smoke passes)"
  if [ ! -s "$DATA/openwakeword_features_ACAV100M_2000_hrs_16bit.npy" ]; then
    log "ACAV100M negative features (~multi-GB)"
    curl -fSL "$ACAV_FEATURES_URL" \
      -o "$DATA/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
  fi
  log "MIT RIRs + a background-audio shard (via the datasets library)"
  "$PY" - "$DATA" <<'PY'
import sys, os, soundfile as sf, datasets
data = sys.argv[1]
rir = os.path.join(data, "mit_rirs"); os.makedirs(rir, exist_ok=True)
ds = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses",
                           split="train", streaming=True)
for i, row in enumerate(ds):
    sf.write(os.path.join(rir, f"rir_{i}.wav"), row["audio"]["array"], row["audio"]["sampling_rate"])
print(f"wrote {i+1} RIR wavs")
bg = os.path.join(data, "background_clips"); os.makedirs(bg, exist_ok=True)
fma = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
for i, row in zip(range(200), fma):  # a shard is plenty for augmentation
    a = row["audio"]
    sf.write(os.path.join(bg, f"bg_{i}.wav"), a["array"], a["sampling_rate"])
print(f"wrote {i+1} background wavs")
PY
  log "datasets done."
}

cmd_config() {
  local cfg="$TRAIN_DIR/$MODEL_NAME.yaml"
  _write_config full "$cfg"
  log "wrote $cfg — review it before 'train'"
}

cmd_train() {
  local cfg="$TRAIN_DIR/$MODEL_NAME.yaml"
  [ -f "$cfg" ] || { echo "no config — run 'config' first" >&2; exit 1; }
  log "FULL training (long: 30k clips, augmentation, $MODEL_NAME)"
  local onnx="$TRAIN_DIR/$MODEL_NAME/$MODEL_NAME.onnx"
  _train_onnx_only "$cfg" "$onnx"
  mkdir -p "$WAKE_DIR"
  cp "$onnx" "$WAKE_DIR/$MODEL_NAME.onnx"
  cmd_provenance
  log "model at $WAKE_DIR/$MODEL_NAME.onnx"
  printf 'Point config.yaml at it:\n  wake:\n    models: [models/wake/%s.onnx]\n' "$MODEL_NAME"
}

# Write a COMPLETE training config (every key from openWakeWord's
# examples/custom_model.yml). $1 = smoke|full, $2 = output path or '-' (stdout).
_write_config() {  # $1=mode $2=out
  local mode="$1" out="$2"
  local target n_samples n_val tts_bs aug_rounds rir bg bg_dup fdf bnpc steps out_dir
  if [ "$mode" = "smoke" ]; then
    target='["nabaztag"]'
    # augmentation_rounds MUST be >= 1: train.py multiplies the clip lists by it,
    # so 0 yields ZERO clips and the run collapses (Bolt probe). Empty rir/bg is
    # fine (augmentation just passes clips through).
    n_samples=20; n_val=10; tts_bs=10; aug_rounds=1
    rir='[]'; bg='[]'; bg_dup='[]'
    out_dir="$TRAIN_DIR/$MODEL_NAME-smoke"
    # reuse the small validation set as the negative feature source
    fdf="{\"validation\": \"$DATA/validation_set_features.npy\"}"
    bnpc='{"validation": 16, "adversarial_negative": 10, "positive": 10}'
    steps=2
  else
    # NOTE: English voice only for now; a couple of spellings nudge pronunciation.
    # True it/en coverage = a second generation pass with an Italian piper voice
    # (follow-up; the v2.0.0 release ships only en_US-libritts_r).
    target='["nabaztag", "na bazz tag"]'
    n_samples=30000; n_val=2000; tts_bs=50; aug_rounds=1
    rir="[\"$DATA/mit_rirs\"]"; bg="[\"$DATA/background_clips\"]"; bg_dup='[1]'
    out_dir="$TRAIN_DIR/$MODEL_NAME"
    fdf="{\"ACAV100M_sample\": \"$DATA/openwakeword_features_ACAV100M_2000_hrs_16bit.npy\"}"
    bnpc='{"ACAV100M_sample": 1024, "adversarial_negative": 50, "positive": 50}'
    steps=50000
  fi
  local body
  body=$(cat <<EOF
# Generated by brain/scripts/train-wake-word.sh ($mode) — edit there, not here.
model_name: "$MODEL_NAME"
target_phrase: $target
custom_negative_phrases: []
n_samples: $n_samples
n_samples_val: $n_val
tts_batch_size: $tts_bs
augmentation_batch_size: 16
piper_sample_generator_path: "$GEN_DIR"
output_dir: "$out_dir"
rir_paths: $rir
background_paths: $bg
background_paths_duplication_rate: $bg_dup
false_positive_validation_data_path: "$DATA/validation_set_features.npy"
augmentation_rounds: $aug_rounds
feature_data_files: $fdf
batch_n_per_class: $bnpc
model_type: "dnn"
layer_size: 32
steps: $steps
max_negative_weight: 1500
target_false_positives_per_hour: 0.2
EOF
)
  if [ "$out" = "-" ]; then printf '%s\n' "$body"; else printf '%s\n' "$body" > "$out"; fi
}

cmd_printconfig() {
  case "${1:-}" in
    smoke|full) _write_config "$1" - ;;
    *) echo "usage: printconfig {smoke|full}" >&2; exit 2 ;;
  esac
}

cmd_validate() {
  local model="$WAKE_DIR/$MODEL_NAME.onnx"
  [ -f "$model" ] || { echo "no model at $model — train first" >&2; exit 1; }
  local clips="${1:-$TRAIN_DIR/validation-clips}"
  mkdir -p "$clips"
  log "scoring $model against WAVs in $clips (16 kHz mono)"
  "$PY" - "$model" "$clips" <<'PY'
import pathlib, sys, wave
import numpy as np
from openwakeword.model import Model

model_path, clips_dir = sys.argv[1], sys.argv[2]
wavs = sorted(pathlib.Path(clips_dir).glob("*.wav"))
if not wavs:
    print(f"no WAVs in {clips_dir} — record a few saying the wake word (and a few not)")
    sys.exit(0)
model = Model(wakeword_models=[model_path], inference_framework="onnx")
for path in wavs:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            print(f"{path.name}: SKIP (need 16 kHz mono)")
            continue
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    model.reset()
    peak = 0.0
    for i in range(0, len(audio) - 1280, 1280):  # 80 ms frames
        for score in model.predict(audio[i:i + 1280]).values():
            peak = max(peak, float(score))
    print(f"{path.name}: peak score {peak:.3f}")
print("\nPick wake.threshold from the gap between positive and negative peaks.")
PY
}

cmd_provenance() {
  mkdir -p "$WAKE_DIR"
  local out="$WAKE_DIR/$MODEL_NAME.provenance.md"
  cat > "$out" <<EOF
# Provenance — $MODEL_NAME.onnx

Trained by \`brain/scripts/train-wake-word.sh\` on $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC).

- Wake phrase spellings: see target_phrase in $TRAIN_DIR/$MODEL_NAME.yaml
- Voice: en_US-libritts_r-medium (English). Italian pronunciation coverage is a
  follow-up (a second generation pass with an Italian piper voice).
- openWakeWord: $OWW_REPO @ $OWW_REF (Apache-2.0)
- Generator: $GEN_REPO @ $GEN_REF (build-time only)
- Export: ONNX only (onnxruntime); TFLite intentionally not produced.
- Host: $(uname -srm)

## Corpora (record each licence — some restrict redistribution of the model)

- Negative features: openwakeword_features_ACAV100M_2000_hrs_16bit.npy
  (davidscripka/openwakeword_features)
- Validation features: validation_set_features.npy
- RIR: davidscripka/MIT_environmental_impulse_responses
- Background: rudraml/fma (small)

## Licence of this artifact

Weights are ours (Apache-2.0 like the brain) SUBJECT TO the corpora terms above.
The training tools are external and not vendored.
EOF
  echo "provenance written to $out"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  stage="${1:-}"
  case "$stage" in
    # Pure/host-safe stages (just write files/text) — no container needed.
    build)       _build_image ;;
    provenance)  cmd_provenance ;;
    printconfig) shift; cmd_printconfig "$@" ;;
    # Compute stages: run INSIDE the pinned 3.10 image, unless we ARE the
    # in-container invocation (WAKE_IN_CONTAINER=1), in which case do the work.
    setup | smoke | datasets | config | train | validate)
      if [ "${WAKE_IN_CONTAINER:-0}" = "1" ]; then
        case "$stage" in
          setup)    cmd_setup ;;
          smoke)    cmd_smoke ;;
          datasets) cmd_datasets ;;
          config)   cmd_config ;;
          train)    cmd_train ;;
          validate) shift; cmd_validate "$@" ;;
        esac
      else
        _in_container "$@"
      fi
      ;;
    *)
      echo "usage: $0 {setup|smoke|datasets|config|train|validate|build|provenance|printconfig}" >&2
      exit 2
      ;;
  esac
fi
