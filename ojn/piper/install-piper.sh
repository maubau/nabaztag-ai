#!/usr/bin/env bash
# Reproducible, PINNED setup for the local Piper TTS candidate (latency work,
# July 2026). Two persistent HTTP voice servers on localhost — Italian on 5001,
# English on 5002 — that the brain's PiperTTS talks to over HTTP.
#
#   ./ojn/piper/install-piper.sh install   # venv + pinned piper + models
#   ./ojn/piper/install-piper.sh units     # write systemd units (does NOT enable)
#   ./ojn/piper/install-piper.sh smoke     # start transiently, health + POST→WAV, stop
#
# GPL BOUNDARY (important — the brain is Apache-2.0): Piper (OHF-Voice/piper1-gpl
# 1.4.2, GPL-3.0) is installed in its OWN virtualenv, SEPARATE from the brain,
# and only ever run as an external localhost process. No Piper code enters the
# brain tree; the brain reaches it purely over HTTP, like Deepgram/OpenAI.
#
# VOICES (record each licence; honour attribution):
#   IT  it_IT-paola-medium   22.05 kHz medium   CC0 dataset
#   EN  en_GB-alba-medium    22.05 kHz medium   SELECTED on-Nabaztag (Sam was
#       judged flat). Voice model is CC BY 4.0 → ATTRIBUTION REQUIRED:
#       "en_GB-alba-medium" Piper voice, Alba dataset (University of Edinburgh),
#       model card https://huggingface.co/rhasspy/piper-voices (en/en_GB/alba).
#       en_US-sam-medium (Apache-2.0) is left installed as a comparison voice.
#
# PACE: the raw voices are too fast in Italian. PiperTTS sends length_scale per
# language (IT=1.25, EN=1.0) via PIPER_LENGTH_SCALE_IT/EN; smoke exercises it.
#
# Does NOT touch TTS_PROFILE: production stays Deepgram. Piper is promoted only
# after it wins latency AND on-Nabaztag listening. After `install` + servers up,
# put PIPER_URL_IT/PIPER_URL_EN in .env and benchmark:
#   python brain/scripts/tts-bench.py --profiles deepgram,piper --keep-audio
#
# REPRODUCIBILITY (be precise): this is ENGINE-VERSION pinned — the piper-tts
# engine is fixed at $PIPER_VERSION. It is NOT a fully locked build: pip is
# upgraded to latest, transitive deps are unconstrained, and the voice models
# are fetched from HuggingFace main WITHOUT a digest. Good enough for the
# latency probe; for a truly reproducible deploy add a pip constraints file and
# model checksums. Idempotent where it can be.

set -euo pipefail

PIPER_VERSION="${PIPER_VERSION:-1.4.2}"          # piper-tts[http]== this exact version
PIPER_HOME="${PIPER_HOME:-$HOME/.local/share/nabaztag-piper}"
VENV_DIR="$PIPER_HOME/venv"
MODELS_DIR="$PIPER_HOME/models"
VOICE_IT="${VOICE_IT:-it_IT-paola-medium}"
VOICE_EN="${VOICE_EN:-en_GB-alba-medium}"     # selected over Sam on-Nabaztag; CC BY 4.0
PORT_IT="${PORT_IT:-5001}"
PORT_EN="${PORT_EN:-5002}"
# Per-language pace sent by the smoke POST (matches PIPER_LENGTH_SCALE_IT/EN).
LENGTH_SCALE_IT="${PIPER_LENGTH_SCALE_IT:-1.25}"
LENGTH_SCALE_EN="${PIPER_LENGTH_SCALE_EN:-1.0}"
PY="$VENV_DIR/bin/python"
# systemd units must NOT run as root. Default to the invoking (non-root) user;
# override with PIPER_USER/PIPER_GROUP if the servers should run as someone else.
PIPER_USER="${PIPER_USER:-${SUDO_USER:-$(id -un)}}"
PIPER_GROUP="${PIPER_GROUP:-$(id -gn "$PIPER_USER" 2>/dev/null || echo "$PIPER_USER")}"

log() { printf '\n== %s\n' "$*"; }

cmd_install() {
  log "Piper venv (isolated from the brain — GPL boundary): $VENV_DIR"
  mkdir -p "$MODELS_DIR"
  python3 -m venv "$VENV_DIR"
  "$PY" -m pip install --upgrade pip >/dev/null
  # PINNED: the HTTP-server extra of piper1-gpl at exactly $PIPER_VERSION.
  "$PY" -m pip install "piper-tts[http]==$PIPER_VERSION"

  for voice in "$VOICE_IT" "$VOICE_EN"; do
    # A voice is usable only if BOTH the weights and their config exist; a
    # half-download (one file) must be re-fetched, not skipped.
    if [ -f "$MODELS_DIR/$voice.onnx" ] && [ -f "$MODELS_DIR/$voice.onnx.json" ]; then
      log "voice already present: $voice"
    else
      log "downloading voice: $voice (need both .onnx and .onnx.json)"
      # piper1-gpl ships a downloader; if this flag differs on your build,
      # download the .onnx + .onnx.json into $MODELS_DIR by hand.
      "$PY" -m piper.download_voices "$voice" --download-dir "$MODELS_DIR"
    fi
  done
  log "installed. models in $MODELS_DIR"
  echo "Then add to .env:"
  echo "  PIPER_URL_IT=http://127.0.0.1:$PORT_IT   PIPER_URL_EN=http://127.0.0.1:$PORT_EN"
  echo "  PIPER_LENGTH_SCALE_IT=$LENGTH_SCALE_IT   PIPER_LENGTH_SCALE_EN=$LENGTH_SCALE_EN   # tuned pace"
}

# Print a systemd unit for one voice server to stdout.
_unit() {  # $1=voice $2=port
  cat <<EOF
[Unit]
Description=Nabaztag Piper TTS ($1) on :$2
After=network.target

[Service]
# Run as a non-privileged user (never root), consistent with PIPER_HOME.
User=$PIPER_USER
Group=$PIPER_GROUP
NoNewPrivileges=true
ExecStart=$PY -m piper.http_server --model $MODELS_DIR/$1.onnx --host 127.0.0.1 --port $2
Restart=on-failure
# Keep the model warm; the whole point is not reloading per utterance.

[Install]
WantedBy=multi-user.target
EOF
}

cmd_units() {
  local out="$PIPER_HOME/systemd"
  mkdir -p "$out"
  _unit "$VOICE_IT" "$PORT_IT" > "$out/nabaztag-piper-it.service"
  _unit "$VOICE_EN" "$PORT_EN" > "$out/nabaztag-piper-en.service"
  log "wrote units to $out (NOT enabled — same caution as the runtime unit)"
  echo "To enable them (persistent, warm on boot):"
  echo "  sudo cp $out/nabaztag-piper-*.service /etc/systemd/system/"
  echo "  sudo systemctl daemon-reload && sudo systemctl enable --now nabaztag-piper-it nabaztag-piper-en"
}

_serve_bg() {  # $1=voice $2=port -> echoes PID
  "$PY" -m piper.http_server --model "$MODELS_DIR/$1.onnx" --host 127.0.0.1 --port "$2" \
    >"$PIPER_HOME/$1.log" 2>&1 &
  echo $!
}

_wait_health() {  # $1=port
  for _ in $(seq 1 40); do
    # Readiness probe: piper1-gpl serves synthesis at POST /, so GET / returns
    # 405 — GET /voices is the endpoint that answers 200 once the model is warm.
    if curl -fsS "http://127.0.0.1:$1/voices" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  return 1
}

_smoke_one() {  # $1=voice $2=port $3=text $4=length_scale
  log "smoke $1 on :$2 (length_scale=$4)"
  local pid; pid=$(_serve_bg "$1" "$2")
  trap 'kill '"$pid"' 2>/dev/null || true' RETURN
  if ! _wait_health "$2"; then
    echo "HEALTH CHECK FAILED (:$2) — see $PIPER_HOME/$1.log"; return 1
  fi
  local out="$PIPER_HOME/smoke-$1.wav"
  # Confirmed API: POST JSON {"text": ..., "length_scale": ...} → WAV body.
  curl -fsS -X POST "http://127.0.0.1:$2/" -H 'Content-Type: application/json' \
    -d "{\"text\": \"$3\", \"length_scale\": $4}" -o "$out"
  if head -c 4 "$out" | grep -q RIFF; then
    echo "OK: $out ($(wc -c <"$out") bytes, RIFF/WAV)"
  else
    echo "UNEXPECTED: response is not WAV (see $out)"; return 1
  fi
}

cmd_smoke() {
  _smoke_one "$VOICE_IT" "$PORT_IT" "Ciao, sono il coniglio." "$LENGTH_SCALE_IT"
  _smoke_one "$VOICE_EN" "$PORT_EN" "Hello, I am the rabbit." "$LENGTH_SCALE_EN"
  log "smoke passed. Servers were transient; use 'units' for the persistent setup."
}

# Only dispatch when executed directly — sourcing (e.g. from tests that exercise
# _wait_health against a mock server) must not trigger a subcommand.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    install) cmd_install ;;
    units)   cmd_units ;;
    smoke)   cmd_smoke ;;
    *) echo "usage: $0 {install|units|smoke}" >&2; exit 2 ;;
  esac
fi
