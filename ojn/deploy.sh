#!/usr/bin/env bash
# Provision the UDOO Bolt: legacy WPA-TKIP AP for the rabbit (§4.1) + OpenJabNab.
# Run ON the Bolt, from the repo root, as a sudo-capable user:
#   ./ojn/deploy.sh ap      # network foundation (Gate S0)
#   ./ojn/deploy.sh ojn     # OpenJabNab server (S1/S2)
#   ./ojn/deploy.sh verify  # Gate S0 checks
#
# Reads from .env (repo root): AP_IFACE, LEGACY_AP_PASSPHRASE, RABBIT_MAC.
# Hardware steps (booting/configuring the rabbit) are Maurizio's;
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
    [[ "$AP_IFACE" =~ ^[a-zA-Z0-9_.:-]+$ ]] || die "invalid AP_IFACE: $AP_IFACE"
    [[ "$RABBIT_MAC" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || \
        die "RABBIT_MAC must look like 00:11:22:33:44:55"
    [[ "$LEGACY_AP_PASSPHRASE" =~ ^[a-zA-Z0-9._-]{8,63}$ ]] || \
        die "LEGACY_AP_PASSPHRASE must be 8-63 letters/digits/dot/underscore/hyphen"
}

render_configs() {
    log "Rendering hostapd/dnsmasq configs from examples (+ .env values)"
    sed -e "s|__LEGACY_AP_PASSPHRASE__|$LEGACY_AP_PASSPHRASE|" \
        -e "s|__AP_IFACE__|$AP_IFACE|g" \
        "$NET_DIR/hostapd.conf.example" > "$NET_DIR/hostapd.conf"
    sed -e "s|__RABBIT_MAC__|$RABBIT_MAC|" -e "s|__AP_IFACE__|$AP_IFACE|g" \
        "$NET_DIR/dnsmasq.conf.example" > "$NET_DIR/dnsmasq.conf"
    chmod 600 "$NET_DIR/hostapd.conf"   # contains the passphrase; both files are gitignored
}

install_rendered() {
    local source=$1 destination=$2 mode=${3:-644}
    local rendered
    rendered=$(mktemp)
    sed "s|__AP_IFACE__|$AP_IFACE|g" "$source" > "$rendered"
    sudo install -m "$mode" "$rendered" "$destination"
    rm -f "$rendered"
}

setup_ap() {
    load_env
    ip link show "$AP_IFACE" >/dev/null 2>&1 || \
        die "interface $AP_IFACE not found (check 'ip -br link' and AP_IFACE in .env)"
    render_configs

    log "Installing packages"
    sudo apt-get update -qq
    sudo apt-get install -y hostapd dnsmasq-base nftables

    log "Installing configs"
    sudo install -d -m 755 /etc/dnsmasq.d
    # Ubuntu's hostapd unit has a ConditionFileNotEmpty check on this canonical
    # path, even when DAEMON_CONF is overridden in /etc/default/hostapd.
    sudo install -m 600 "$NET_DIR/hostapd.conf" /etc/hostapd/hostapd.conf
    sudo install -m 644 "$NET_DIR/dnsmasq.conf" /etc/dnsmasq.d/nabaztag-legacy.conf
    install_rendered "$NET_DIR/nftables-rabbit.conf.example" /etc/nftables-rabbit.conf
    install_rendered "$NET_DIR/nabaztag-ap-interface.service.example" \
        /etc/systemd/system/nabaztag-ap-interface.service
    sudo install -m 644 "$NET_DIR/nabaztag-firewall.service.example" \
        /etc/systemd/system/nabaztag-firewall.service
    sudo install -m 644 "$NET_DIR/nabaztag-dnsmasq.service.example" \
        /etc/systemd/system/nabaztag-dnsmasq.service

    # Ubuntu Server normally uses systemd-networkd. If NetworkManager is present,
    # keep it from reclaiming the radio that hostapd owns.
    if command -v nmcli >/dev/null 2>&1; then
        install_rendered "$NET_DIR/NetworkManager-unmanaged.conf.example" \
            /etc/NetworkManager/conf.d/90-nabaztag-ap-unmanaged.conf
        sudo nmcli device set "$AP_IFACE" managed no || true
    fi

    log "Enabling persistent interface and isolation firewall"
    sudo systemctl daemon-reload
    sudo systemctl enable --now nabaztag-ap-interface.service
    sudo systemctl enable --now nabaztag-firewall.service

    log "Starting services"
    sudo systemctl unmask hostapd 2>/dev/null || true
    echo 'DAEMON_CONF=/etc/hostapd/hostapd.conf' | \
        sudo tee /etc/default/hostapd >/dev/null
    sudo systemctl enable --now nabaztag-dnsmasq.service
    sudo systemctl enable --now hostapd || {
        echo "hostapd failed — debug with: sudo hostapd -dd /etc/hostapd/hostapd.conf"
        echo "(an 'EAPOL-Key timeout' in the log means the eapol_version=1 issue — see §4.1)"
        exit 1
    }

    log "AP up. Now boot the rabbit and run: ./ojn/deploy.sh verify"
}

verify_s0() {
    load_env
    log "Gate S0 verification"
    echo "1) Persistent services:"
    systemctl is-active nabaztag-ap-interface nabaztag-firewall hostapd \
        nabaztag-dnsmasq || true
    echo "2) AP address and radio mode:"
    ip -4 -br address show dev "$AP_IFACE"
    iw dev "$AP_IFACE" info 2>/dev/null | sed -n '1,12p' || true
    echo "3) Isolation rules (XMPP 5222 must be listed):"
    sudo nft list table inet rabbit_isolation 2>/dev/null | \
        grep -E 'iifname|oifname|dport' || echo "   firewall table missing"
    echo "4) Rabbit associated + static lease:"
    grep -i "$RABBIT_MAC" /var/lib/misc/dnsmasq.leases 2>/dev/null || echo "   no lease yet — rabbit not associated?"
    echo "5) Rabbit alive on the segment (the firmware does NOT answer ping/arping,"
    echo "   so liveness = neighbor entry and/or its HTTP bootcode requests):"
    ip neigh show "$RABBIT_IP" | grep -q . && ip neigh show "$RABBIT_IP" \
        || echo "   no neighbor entry yet"
    echo "   watch its traffic with:"
    echo "     sudo tcpdump -ni $AP_IFACE host $RABBIT_IP and tcp port 80 -c 3"
    echo "   (a GET /vl/bc.jsp?... is the rabbit fetching its Violet bootcode = alive)"
    echo "6) Isolation: connect a temporary client to the legacy SSID; home LAN/internet must fail."
    echo "7) Main Wi-Fi untouched: verify your router still shows WPA2/WPA3 only."
}

setup_ojn() {
    log "Deploying OpenJabNab to $OJN_DIR"
    sudo apt-get update -qq
    sudo apt-get install -y git build-essential qtbase5-dev apache2 php libapache2-mod-php
    if [ ! -e "$OJN_DIR" ]; then
        # /opt requires privilege, but qmake/make must be able to write into the
        # checkout as the invoking user. Do not create a root-owned git tree.
        sudo install -d -m 755 -o "$(id -u)" -g "$(id -g)" "$OJN_DIR"
        git clone https://github.com/OpenJabNab/OpenJabNab.git "$OJN_DIR"
    elif [ -d "$OJN_DIR/.git" ]; then
        [ -w "$OJN_DIR" ] || \
            die "$OJN_DIR is not writable; fix its ownership before building"
        log "OJN already cloned; pulling"
        git -C "$OJN_DIR" pull --ff-only
    else
        die "$OJN_DIR exists but is not an OpenJabNab git checkout"
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
