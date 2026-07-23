#!/usr/bin/env bash
# Install / enable the Nabaztag voice runtime as a systemd service on the Bolt.
#
#   ./deploy/install-runtime.sh preflight   # check only, changes nothing
#   ./deploy/install-runtime.sh install     # render + install the unit (NOT enabled)
#   ./deploy/install-runtime.sh enable      # enable at boot + start now
#   ./deploy/install-runtime.sh status      # unit state + recent journal
#   ./deploy/install-runtime.sh disable     # stop + disable (unit file kept)
#
# `install` deliberately does NOT enable: preflight can pass and the runtime
# still misbehave on the real rabbit, so starting at boot is a separate,
# explicit decision.
#
# The unit template lives in deploy/nabaztag-runtime.service; User and paths are
# substituted from the values below so the file in git stays generic.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
VENV="${VENV:-$REPO_DIR/.venv}"
UNIT_NAME="nabaztag-runtime.service"
UNIT_SRC="$REPO_DIR/deploy/$UNIT_NAME"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"

log()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok: %s\n' "$*"; }
fail() { printf '   FAIL: %s\n' "$*" >&2; FAILED=1; }
warn() { printf '   warn: %s\n' "$*"; }

cmd_preflight() {
  FAILED=0
  log "preflight (user=$RUN_USER repo=$REPO_DIR)"

  [ -x "$VENV/bin/python" ] && ok "venv python" || fail "no venv python at $VENV/bin/python"
  [ -f "$REPO_DIR/.env" ] && ok ".env present" || fail "no .env (keys live there, not in the unit)"
  [ -f "$REPO_DIR/config.yaml" ] && ok "config.yaml present" \
    || fail "no config.yaml (copy config.example.yaml and adjust)"

  # Single ownership (§8): the MCP server binds the same ports and builds a
  # second BodyController. Enabling both is the classic way to get a rabbit
  # that half-responds.
  if systemctl is-enabled --quiet nabaztag-mcp.service 2>/dev/null; then
    fail "nabaztag-mcp.service is ENABLED — disable it first (both bind :8090/:8091)"
  else
    ok "MCP service not enabled"
  fi

  # Production voice: Piper serves TTS, Deepgram is the fallback.
  local profile
  profile="$(grep -E '^TTS_PROFILE=' "$REPO_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  case "$profile" in
    piper)
      ok "TTS_PROFILE=piper"
      for var in PIPER_URL_IT PIPER_URL_EN PIPER_LENGTH_SCALE_IT PIPER_LENGTH_SCALE_EN; do
        grep -qE "^$var=" "$REPO_DIR/.env" && ok "$var set" || fail "$var missing from .env"
      done
      # The fallback is only real if Deepgram can actually be reached.
      grep -qE '^DEEPGRAM_API_KEY=.+' "$REPO_DIR/.env" \
        && ok "DEEPGRAM_API_KEY set (Flux STT + TTS fallback)" \
        || fail "DEEPGRAM_API_KEY missing — no Flux STT and no TTS fallback"
      _check_piper_servers
      ;;
    deepgram) warn "TTS_PROFILE=deepgram (production choice is piper)" ;;
    "")       fail "TTS_PROFILE not set in .env" ;;
    *)        warn "TTS_PROFILE=$profile (unexpected for production)" ;;
  esac

  grep -qE '^OPENAI_API_KEY=.+' "$REPO_DIR/.env" && ok "OPENAI_API_KEY set" \
    || fail "OPENAI_API_KEY missing — no LLM and no Whisper fallback"

  # The MTL decoder ignores aiohttp-served audio: Apache must deliver.
  if grep -qE '^NABAZTAG_MP3_SERVE_HTTP=0' "$REPO_DIR/.env"; then
    ok "Mp3Server storage-only (Apache delivers)"
  else
    fail "NABAZTAG_MP3_SERVE_HTTP must be 0 — the rabbit ignores aiohttp-served MP3s"
  fi

  # Wake word: hey_jarvis is a smoke-test placeholder, not the real phrase.
  if grep -qE '^\s*models:\s*\[hey_jarvis\]' "$REPO_DIR/config.yaml" 2>/dev/null; then
    warn "wake model is still hey_jarvis (placeholder, not \"Nabaztag\")"
  fi

  [ "${FAILED:-0}" -eq 0 ] || { printf '\npreflight FAILED\n' >&2; return 1; }
  printf '\npreflight passed\n'
}

_check_piper_servers() {
  # A warm server per language is the whole point; a cold one means every
  # utterance silently lands on the Deepgram fallback.
  local url
  for var in PIPER_URL_IT PIPER_URL_EN; do
    url="$(grep -E "^$var=" "$REPO_DIR/.env" | tail -1 | cut -d= -f2-)"
    [ -n "$url" ] || continue
    if curl -fsS --max-time 3 "${url%/}/voices" >/dev/null 2>&1; then
      ok "$var reachable (GET /voices)"
    else
      warn "$var NOT reachable at $url — TTS would fall back to Deepgram every turn"
    fi
  done
}

_render_unit() {
  sed -e "s|^User=.*|User=$RUN_USER|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
      -e "s|^EnvironmentFile=.*|EnvironmentFile=$REPO_DIR/.env|" \
      -e "s|^ReadWritePaths=.*|ReadWritePaths=$REPO_DIR|" \
      -e "s|^ExecStart=.*|ExecStart=$VENV/bin/python -m rabbit_brain.runtime --config config.yaml|" \
      "$UNIT_SRC"
}

cmd_install() {
  cmd_preflight
  log "installing $UNIT_DST"
  _render_unit | sudo tee "$UNIT_DST" >/dev/null
  sudo systemctl daemon-reload
  log "installed but NOT enabled (deliberate)."
  echo "Start once to watch it:   sudo systemctl start $UNIT_NAME && journalctl -u $UNIT_NAME -f"
  echo "Enable at boot when happy: ./deploy/install-runtime.sh enable"
}

cmd_enable() {
  [ -f "$UNIT_DST" ] || { echo "unit not installed; run 'install' first" >&2; exit 1; }
  sudo systemctl enable --now "$UNIT_NAME"
  log "enabled at boot and started"
  journalctl -u "$UNIT_NAME" -n 20 --no-pager || true
}

cmd_disable() {
  sudo systemctl disable --now "$UNIT_NAME" || true
  log "stopped and disabled (unit file left in place)"
}

cmd_status() {
  systemctl status "$UNIT_NAME" --no-pager || true
  journalctl -u "$UNIT_NAME" -n 40 --no-pager || true
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    preflight) cmd_preflight ;;
    install)   cmd_install ;;
    enable)    cmd_enable ;;
    disable)   cmd_disable ;;
    status)    cmd_status ;;
    *) echo "usage: $0 {preflight|install|enable|disable|status}" >&2; exit 2 ;;
  esac
fi
