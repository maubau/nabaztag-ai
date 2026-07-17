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
OJN_STATE_DIR="${OJN_STATE_DIR:-/var/lib/openjabnab}"
# OJN master as source-verified in docs/OJN_API_NOTES.md (July 2026)
OJN_COMMIT="${OJN_COMMIT:-640257f3cef63fb428d80b8c171f3b15d17ab0ed}"

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

render_ojn_dir() {
    local source=$1 destination=$2
    local rendered
    rendered=$(mktemp)
    sed "s|__OJN_DIR__|$OJN_DIR|g" "$source" > "$rendered"
    sudo install -m 644 "$rendered" "$destination"
    rm -f "$rendered"
}

patch_wrapper() {
    # PHP 8.3 fixes for the 2008-era admin UI (S1 field findings). Applied
    # after every pinned checkout -f, so they are idempotent by construction.
    log "Patching http-wrapper for PHP 8.x"
    local admin="$OJN_DIR/http-wrapper/ojn_admin"
    # session_start() lost its string argument in PHP 8
    sed -i "s/session_start('openJabNab');/session_name('openJabNab'); session_start();/" \
        "$admin/include/common-def.php"
    # split() was removed in PHP 8 (the pattern here is a literal pipe)
    sed -i 's#split("\\|"#explode("|"#g' "$admin/plugins/bunnies/cinema.plugin.php"

    # The admin UI expects install.php to write include/common.php at runtime,
    # which would need an Apache-writable tree. Generate it here instead, from
    # the (patched) template — never hand Apache write access to the checkout.
    if [ ! -f "$admin/include/common.php" ]; then
        local host="${OJN_ADMIN_HOST:-$(hostname -I | awk '{print $1}')}"
        local email="${OJN_ADMIN_EMAIL:-admin@localhost}"
        log "Generating ojn_admin/include/common.php (host=$host)"
        sed -e "s|<HOSTNAME>|$host|g" -e "s|<EMAIL>|$email|g" \
            "$admin/include/common-def.php" > "$admin/include/common.php"
    fi
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
    # OJN master is Qt4-era code and does not build against Qt5+ (Gate S1
    # finding, docs/OJN_API_NOTES.md): the daemon runs in a locally-built
    # Debian buster (last Qt4 Debian) container, pinned to OJN_COMMIT.
    # The PHP http-wrapper is served by host Apache from the host checkout.
    log "S1/S2: OJN daemon (Qt4 container) + http-wrapper (host Apache)"
    load_env
    sudo apt-get update -qq
    # php-xml: required by the admin UI on PHP 8.3 (S1 field finding)
    sudo apt-get install -y docker.io apache2 php php-xml libapache2-mod-php git

    log "Host checkout of OpenJabNab @ ${OJN_COMMIT:0:7} (serves http-wrapper/)"
    if [ ! -e "$OJN_DIR" ]; then
        # /opt requires privilege for the mkdir only; the checkout stays user-owned.
        sudo install -d -m 755 -o "$(id -u)" -g "$(id -g)" "$OJN_DIR"
        git clone https://github.com/OpenJabNab/OpenJabNab.git "$OJN_DIR"
    fi
    [ -d "$OJN_DIR/.git" ] || die "$OJN_DIR exists but is not an OpenJabNab git checkout"
    [ -w "$OJN_DIR" ] || die "$OJN_DIR is not writable; fix its ownership first"
    git -C "$OJN_DIR" fetch --quiet origin || true
    # -f resets tracked files (untracked ones, like the generated common.php,
    # survive); the PHP 8 patches below are re-applied on every run.
    git -C "$OJN_DIR" -c advice.detachedHead=false checkout -f "$OJN_COMMIT"
    patch_wrapper
    # The daemon (root in the container) writes chor/broadcast files here.
    chmod -R a+rX "$OJN_DIR/http-wrapper"

    log "Ensuring ojn.local resolves on the legacy segment (MTL bootcode quirk)"
    sed -e "s|__RABBIT_MAC__|$RABBIT_MAC|" -e "s|__AP_IFACE__|$AP_IFACE|g" \
        "$NET_DIR/dnsmasq.conf.example" > "$NET_DIR/dnsmasq.conf"
    sudo install -m 644 "$NET_DIR/dnsmasq.conf" /etc/dnsmasq.d/nabaztag-legacy.conf
    sudo systemctl restart nabaztag-dnsmasq 2>/dev/null || true

    log "Building the daemon image (reproducible; no third-party OJN images)"
    # context = ojn/ so the GPL plugin_events sources are inside it
    sudo docker build -f "$REPO_ROOT/ojn/docker/Dockerfile" \
        --build-arg OJN_COMMIT="$OJN_COMMIT" \
        -t openjabnab:qt4 "$REPO_ROOT/ojn"

    log "Installing daemon state dir ($OJN_STATE_DIR) and systemd unit"
    sudo install -d -m 755 "$OJN_STATE_DIR"
    if [ ! -f "$OJN_STATE_DIR/openjabnab.ini" ]; then
        sudo install -m 644 "$REPO_ROOT/ojn/docker/openjabnab.ini.example" \
            "$OJN_STATE_DIR/openjabnab.ini"
    else
        # Migration (S1 finding): the rabbit's bootcode needs a resolvable
        # hostname, not an IPv4 literal, as Ping/Broad/Xmpp server.
        if grep -qE '^(Ping|Broad|Xmpp)Server *= *192\.168\.66\.1' \
                "$OJN_STATE_DIR/openjabnab.ini"; then
            log "Migrating openjabnab.ini: 192.168.66.1 -> ojn.local"
            sudo sed -i -E 's#^((Ping|Broad|Xmpp)Server *= *)192\.168\.66\.1#\1ojn.local#' \
                "$OJN_STATE_DIR/openjabnab.ini"
        fi
    fi
    render_ojn_dir "$REPO_ROOT/ojn/docker/nabaztag-ojn.service.example" \
        /etc/systemd/system/nabaztag-ojn.service
    sudo systemctl daemon-reload
    sudo systemctl enable nabaztag-ojn.service
    sudo systemctl restart nabaztag-ojn.service

    log "Configuring Apache vhost for the http-wrapper"
    render_ojn_dir "$REPO_ROOT/ojn/apache/ojn-vhost.conf.example" \
        /etc/apache2/sites-available/ojn.conf
    sudo a2enmod rewrite >/dev/null
    sudo a2dissite 000-default >/dev/null 2>&1 || true
    sudo a2ensite ojn >/dev/null
    sudo systemctl reload apache2

    log "S1 smoke checks (port 8080 speaks OJN's internal binary framing, NOT"
    log "HTTP — never curl it directly; always test through Apache on :80):"
    echo "  daemon:   sudo docker logs openjabnab | tail"
    echo "  API:      curl -s http://127.0.0.1/ojn_api/global/about"
    echo "  bootcode: curl -sI http://127.0.0.1/vl/bc.jsp | head -3"
    echo "  DNS:      dig +short ojn.local @192.168.66.1   (must answer 192.168.66.1)"
    echo "  account:  ./ojn/deploy.sh account <login>   (first run only; password prompted)"
    echo "  then: register the rabbit from the admin UI (http://<bolt>/ojn_admin/) — S2"
    echo "  events:   enable the webhook plugin per bunny — see ojn/plugin_events/README.md"
}

bootstrap_account() {
    # The built-in admin/admin exists only in memory while zero accounts are
    # persisted, and is never saved. Use it once to create the first real
    # account: OJN auto-promotes that account to admin and persists it.
    # The password is prompted (never an argv/history item) and sent to curl
    # via stdin (pass@-), so it shows up neither in `ps` nor in the shell log.
    local login=$1
    [ -n "$login" ] || die "usage: $0 account <login>   (password is prompted)"
    local password password2
    read -rs -p "Password for '$login': " password; echo
    read -rs -p "Repeat password: " password2; echo
    [ -n "$password" ] || die "empty password"
    [ "$password" = "$password2" ] || die "passwords do not match"

    local api="http://127.0.0.1/ojn_api"
    local token
    token=$(curl -sfG --data-urlencode "login=admin" --data-urlencode "pass=admin" \
        "$api/accounts/auth" | sed -n 's|.*<value>\(.*\)</value>.*|\1|p')
    [ -n "$token" ] || die "default admin/admin login failed — an account already exists (good); use it instead"

    printf '%s' "$password" | curl -sfG \
        --data-urlencode "login=$login" \
        --data-urlencode "username=$login" \
        --data-urlencode "pass@-" \
        --data-urlencode "token=$token" \
        "$api/accounts/registerNewAccount" | grep -q "<ok>" || die "registerNewAccount failed"
    log "Account '$login' created and auto-promoted to admin (persisted)."
    log "The in-memory admin/admin disappears at next daemon restart:"
    sudo systemctl restart nabaztag-ojn.service
    log "Done. AllowAnonymousRegistration stays false in openjabnab.ini."
}

case "${1:-}" in
    ap)      setup_ap ;;
    ojn)     setup_ojn ;;
    verify)  verify_s0 ;;
    account) bootstrap_account "${2:-}" ;;
    *)       echo "usage: $0 {ap|ojn|verify|account <login>}"; exit 1 ;;
esac
