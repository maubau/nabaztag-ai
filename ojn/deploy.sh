#!/usr/bin/env bash
# Provision the UDOO Bolt: legacy WPA-TKIP AP for the rabbit (§4.1) + OpenJabNab.
# Run ON the Bolt, from the repo root, as a sudo-capable user:
#   ./ojn/deploy.sh ap      # network foundation (Gate S0)
#   ./ojn/deploy.sh ojn     # OpenJabNab server (S1/S2)
#   ./ojn/deploy.sh verify  # Gate S0 checks
#
# Reads from .env (repo root): LEGACY_AP_PASSPHRASE, RABBIT_MAC.
# Hardware steps (plugging the dongle, booting the rabbit) are Maurizio's;
# everything scriptable lives here so the setup is reproducible from the repo.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET_DIR="$REPO_ROOT/ojn/network"
AP_IFACE="${AP_IFACE:-wlan1}"
BOLT_LEGACY_IP="192.168.66.1/24"
RABBIT_IP="192.168.66.10"
OJN_DIR="${OJN_DIR:-/opt/openjabnab}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

load_env() {
    [ -f "$REPO_ROOT/.env" ] || die ".env not found — copy .env.example and fill it in"
    # shellcheck disable=SC1091
    set -a; source "$REPO_ROOT/.env"; set +a
    : "${LEGACY_AP_PASSPHRASE:?set LEGACY_AP_PASSPHRASE in .env}"
    : "${RABBIT_MAC:?set RABBIT_MAC in .env (the Wi-Fi MAC of the rabbit)}"
}

render_configs() {
    log "Rendering hostapd/dnsmasq configs from examples (+ .env values)"
    sed "s|__LEGACY_AP_PASSPHRASE__|$LEGACY_AP_PASSPHRASE|" \
        "$NET_DIR/hostapd.conf.example" > "$NET_DIR/hostapd.conf"
    sed -e "s|__RABBIT_MAC__|$RABBIT_MAC|" -e "s|^interface=.*|interface=$AP_IFACE|" \
        "$NET_DIR/dnsmasq.conf.example" > "$NET_DIR/dnsmasq.conf"
    sed -i "s|^interface=.*|interface=$AP_IFACE|" "$NET_DIR/hostapd.conf"
    chmod 600 "$NET_DIR/hostapd.conf"   # contains the passphrase; both files are gitignored
}

setup_ap() {
    load_env
    ip link show "$AP_IFACE" >/dev/null 2>&1 || die "interface $AP_IFACE not found (USB dongle plugged in? override with AP_IFACE=...)"
    render_configs

    log "Installing packages"
    sudo apt-get update -qq
    sudo apt-get install -y hostapd dnsmasq nftables

    log "Configuring $AP_IFACE with static IP $BOLT_LEGACY_IP"
    sudo ip addr replace "$BOLT_LEGACY_IP" dev "$AP_IFACE"
    sudo ip link set "$AP_IFACE" up

    log "Installing configs"
    sudo install -m 600 "$NET_DIR/hostapd.conf" /etc/hostapd/nabaztag-legacy.conf
    sudo install -m 644 "$NET_DIR/dnsmasq.conf" /etc/dnsmasq.d/nabaztag-legacy.conf
    sudo install -m 644 "$NET_DIR/nftables-rabbit.conf.example" /etc/nftables-rabbit.conf

    log "Applying isolation firewall rules"
    sudo nft -f /etc/nftables-rabbit.conf

    log "Starting services"
    sudo systemctl unmask hostapd 2>/dev/null || true
    sudo sh -c "echo 'DAEMON_CONF=/etc/hostapd/nabaztag-legacy.conf' > /etc/default/hostapd"
    sudo systemctl restart dnsmasq
    sudo systemctl enable --now hostapd || {
        echo "hostapd failed — debug with: sudo hostapd -dd /etc/hostapd/nabaztag-legacy.conf"
        echo "(an 'EAPOL-Key timeout' in the log means the eapol_version=1 issue — see §4.1)"
        exit 1
    }

    log "AP up. Now boot the rabbit and run: ./ojn/deploy.sh verify"
}

verify_s0() {
    load_env
    log "Gate S0 verification"
    echo "1) Rabbit associated + static lease:"
    grep -i "$RABBIT_MAC" /var/lib/misc/dnsmasq.leases 2>/dev/null || echo "   no lease yet — rabbit not associated?"
    echo "2) Rabbit pingable from the Bolt:"
    ping -c 3 -W 2 "$RABBIT_IP" && echo "   OK" || echo "   FAIL"
    echo "3) Isolation (run from a home-LAN host): ping $RABBIT_IP must FAIL."
    echo "4) Main Wi-Fi untouched: verify your router still shows WPA2/WPA3 only."
}

setup_ojn() {
    log "Deploying OpenJabNab to $OJN_DIR"
    sudo apt-get update -qq
    sudo apt-get install -y git build-essential qtbase5-dev apache2 php libapache2-mod-php
    if [ ! -d "$OJN_DIR" ]; then
        sudo git clone https://github.com/OpenJabNab/OpenJabNab.git "$OJN_DIR"
    else
        log "OJN already cloned; pulling"
        sudo git -C "$OJN_DIR" pull --ff-only
    fi
    log "Building the OJN daemon (server/)"
    ( cd "$OJN_DIR/server" && qmake && make -j"$(nproc)" ) || \
        die "OJN build failed — check Qt version; see docs/OJN_API_NOTES.md for build notes"
    log "OJN built. Manual steps remain: Apache vhost for the PHP wrapper, daemon start,"
    log "rabbit registration, and pointing the rabbit's 'Violet Platform address' at this Bolt (S1/S2)."
}

case "${1:-}" in
    ap)     setup_ap ;;
    ojn)    setup_ojn ;;
    verify) verify_s0 ;;
    *)      echo "usage: $0 {ap|ojn|verify}"; exit 1 ;;
esac
