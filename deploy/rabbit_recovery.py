#!/usr/bin/env python3
"""Bolt-side Wi-Fi recovery for the Nabaztag rabbit (OJN_API_NOTES #13).

The failure this heals: after long inactivity the rabbit gets disassociated and
the `nabaztag-legacy` SSID silently stops being advertised even though hostapd
is still `active` and `iw` still says `type AP`. The rabbit then cannot rejoin
(power-cycling it just gives four red LEDs — there is no AP to find). What fixes
it is a plain `systemctl restart hostapd`; the rabbit re-associates within ~2 s.
So this is self-healable ON THE BOLT — no OJN restart, no reboot, no touching
the rabbit.

The whole design is "do the one narrow thing, and only when sure":
  * ONLY ever restarts hostapd. It never restarts OJN or the Bolt — those were
    the old, wrong idea and don't fix this.
  * Restarts only when THREE signals coincide, so a healthy or a merely-idle
    rabbit is never disturbed:
        1. the rabbit is NOT in `iw dev <iface> station dump` (disassociated);
        2. the rabbit's OWN last HTTP request is old — read from the newest
           Apache log line whose client is exactly RABBIT_IP, NOT the file
           mtime (any localhost/admin request would reset that and mask a
           silent rabbit);
        3. a ghost XMPP socket lingers on :5222 (ESTAB, the stuck-Send-Q
           remnant of the dropped session).
  * A cooldown after each restart (give the rabbit time to come back) and a
    per-outage restart cap with an hourly retry-hold stop it cycling when the
    rabbit is simply switched off — that case looks identical bar the fact that
    restarting hostapd won't bring it back, so we must not loop on it.

The decision logic (`decide`) is a pure function of (signals, state, policy,
now) so it can be exhaustively tested; the process only shells out to gather
signals and to run the one restart command.

    sudo python3 deploy/rabbit_recovery.py --once     # one tick, for testing
    sudo python3 deploy/rabbit_recovery.py            # daemon loop (the service)
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum

log = logging.getLogger("rabbit-recovery")


class Action(Enum):
    NONE = "none"  # healthy, or nothing to do
    RESTART_HOSTAPD = "restart_hostapd"  # the three signals coincide → heal
    COOLDOWN = "cooldown"  # restarted recently; wait to see if it worked
    GIVE_UP = "give_up"  # cap hit → stop trying (rabbit likely powered off)
    HELD = "held"  # in the post-give-up hold, not retrying yet


@dataclass(frozen=True)
class Policy:
    http_stale_after_s: float = 600.0  # HTTP older than this counts as "old"
    cooldown_s: float = 180.0  # after a restart, wait this long before another
    max_restarts_per_outage: int = 3  # then stop — restarting clearly isn't helping
    give_up_hold_s: float = 3600.0  # after giving up, retry at most hourly


@dataclass(frozen=True)
class Signals:
    # None = "could not determine" (e.g. `iw` failed); the decision treats an
    # unknown association state as "do nothing", never as a reason to restart.
    rabbit_associated: bool | None
    # Age of the RABBIT's last HTTP request (from its own log lines). None =
    # can't be determined (no log, no rabbit line, unparseable time) → the
    # decision never treats that as "old", so it never restarts on a guess.
    last_http_age_s: float | None
    xmpp_ghost_socket: bool  # an ESTAB :5222 socket lingering


@dataclass(frozen=True)
class State:
    last_restart_at: float | None = None
    restarts_this_outage: int = 0
    given_up_at: float | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    state: State


def decide(signals: Signals, state: State, policy: Policy, now: float) -> Decision:
    """Pure: given the signals, the carried state and the clock, decide what to
    do and return the next state. No side effects."""
    # Healthy: the rabbit is associated. Clear any outage bookkeeping so the
    # next outage starts from a clean slate (this is what stops a week-long
    # power-off from poisoning a later, genuine recovery).
    if signals.rabbit_associated is True:
        return Decision(Action.NONE, "rabbit associated", State())

    # Unknown association (iw failed): never restart on a guess.
    if signals.rabbit_associated is None:
        return Decision(Action.NONE, "association unknown (iw failed); standing pat", state)

    # Disassociated. Only act if the OTHER two signals also point at the known
    # failure — otherwise it's a rabbit that just left, or is simply off with no
    # ghost socket, and hostapd is not the problem.
    http_old = (
        signals.last_http_age_s is not None and signals.last_http_age_s >= policy.http_stale_after_s
    )
    if not (http_old and signals.xmpp_ghost_socket):
        why = []
        if not http_old:
            why.append("recent/absent HTTP")
        if not signals.xmpp_ghost_socket:
            why.append("no ghost XMPP socket")
        return Decision(Action.NONE, f"disassociated but {', '.join(why)}", state)

    # The three signals coincide — this is the healable outage.
    # Are we in the post-give-up hold? Retry at most once per give_up_hold_s.
    if state.given_up_at is not None:
        if now - state.given_up_at < policy.give_up_hold_s:
            return Decision(Action.HELD, "gave up this outage; holding before retry", state)
        # Hold elapsed: allow a fresh attempt (rabbit may have been turned on).
        state = State()

    # Cooldown: a restart is in flight / just happened. Give it time to work.
    if state.last_restart_at is not None and now - state.last_restart_at < policy.cooldown_s:
        return Decision(Action.COOLDOWN, "within cooldown after a restart", state)

    # Cap reached: stop restarting. Restarting hostapd is clearly not bringing
    # the rabbit back, so it is almost certainly powered off — do not loop.
    if state.restarts_this_outage >= policy.max_restarts_per_outage:
        return Decision(
            Action.GIVE_UP,
            f"restarted {state.restarts_this_outage}x without recovery; "
            "rabbit likely powered off — holding",
            replace(state, given_up_at=now),
        )

    # Heal: restart hostapd.
    return Decision(
        Action.RESTART_HOSTAPD,
        "disassociated + old HTTP + ghost XMPP socket → restarting hostapd",
        replace(state, last_restart_at=now, restarts_this_outage=state.restarts_this_outage + 1),
    )


# --- Signal gathering (the impure edge; parsers kept separate for testing) ---


def parse_associated(station_dump: str, rabbit_mac: str) -> bool:
    """True if the rabbit's MAC appears in `iw dev <iface> station dump`."""
    return rabbit_mac.lower() in station_dump.lower()


def parse_xmpp_ghost(ss_output: str, rabbit_ip: str | None) -> bool:
    """True if an ESTAB socket on :5222 exists (to the rabbit, if its IP is
    known). With the rabbit disassociated, any such socket is by definition a
    ghost — the live session can't still be up."""
    for line in ss_output.splitlines():
        if ":5222" not in line:
            continue
        if rabbit_ip is None or rabbit_ip in line:
            return True
    return False


# Apache %t field, e.g. "[24/Jul/2026:04:52:13 +0000]". English month
# abbreviations always (Apache does not localise them); we map them ourselves
# rather than trust strptime's locale-dependent %b under the service.
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
_TS_RE = re.compile(r"\[(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]")


def parse_apache_epoch(line: str) -> float | None:
    """Epoch seconds from an Apache %t timestamp in the line, or None if it has
    no well-formed one (a malformed line must not be trusted)."""
    m = _TS_RE.search(line)
    if m is None:
        return None
    day, mon, year, hh, mm, ss, tz = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    sign = 1 if tz[0] == "+" else -1
    offset = timedelta(minutes=sign * (int(tz[1:3]) * 60 + int(tz[3:5])))
    try:
        return datetime(
            int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=timezone(offset)
        ).timestamp()
    except ValueError:
        return None


def last_rabbit_epoch(lines: Iterable[str], rabbit_ip: str) -> float | None:
    """Newest timestamp among lines whose CLIENT (the first field, %h) is
    EXACTLY rabbit_ip. `lines` must be newest-first; the first rabbit line with
    a parseable timestamp wins. Matching the client is the whole point: the
    log also carries localhost/admin requests, and those must not count as the
    rabbit being alive."""
    for line in lines:
        parts = line.split(maxsplit=1)
        if not parts or parts[0] != rabbit_ip:
            continue
        epoch = parse_apache_epoch(line)
        if epoch is not None:
            return epoch
    return None


def _tail_lines(path: str, max_bytes: int) -> list[str] | None:
    """Read only the last max_bytes of a (growing) log — never load the whole
    file. Drops the first, probably-truncated, line when we didn't start at 0."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 and lines else lines


def _gz_lines(path: str) -> list[str] | None:
    try:
        with gzip.open(path, "rt", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def last_http_age(
    access_log: str, rabbit_ip: str, now: float, max_bytes: int = 65536
) -> float | None:
    """Seconds since the rabbit's own last request, from the Apache log line
    whose client is exactly rabbit_ip — NOT the file mtime (a localhost request
    would reset that and hide a silent rabbit). Reads a bounded tail of the
    current log, then falls back to the rotated .1 / .1.gz (delaycompress leaves
    .1 uncompressed). None if it can't be determined — missing log, no rabbit
    line anywhere, or an unparseable timestamp — so the caller never acts on a
    guess."""
    for candidate in (access_log, access_log + ".1", access_log + ".1.gz"):
        lines = (
            _gz_lines(candidate) if candidate.endswith(".gz") else _tail_lines(candidate, max_bytes)
        )
        if not lines:
            continue
        epoch = last_rabbit_epoch(reversed(lines), rabbit_ip)
        if epoch is not None:
            return max(0.0, now - epoch)
    return None


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("command failed %s: %s", cmd, exc)
        return None
    if out.returncode != 0:
        log.warning("command %s exited %d: %s", cmd, out.returncode, out.stderr.strip()[:200])
        return None
    return out.stdout


@dataclass(frozen=True)
class Config:
    iface: str
    rabbit_mac: str
    rabbit_ip: str  # REQUIRED for the watchdog — the rabbit's static-lease IP
    access_log: str
    hostapd_restart_cmd: list[str]


def gather_signals(cfg: Config, now: float) -> Signals:
    station = _run(["iw", "dev", cfg.iface, "station", "dump"])
    associated = None if station is None else parse_associated(station, cfg.rabbit_mac)

    age = last_http_age(cfg.access_log, cfg.rabbit_ip, now)

    ss_out = _run(["ss", "-Htan", "state", "established", "( sport = :5222 or dport = :5222 )"])
    ghost = False if ss_out is None else parse_xmpp_ghost(ss_out, cfg.rabbit_ip)

    return Signals(
        rabbit_associated=associated,
        last_http_age_s=age,
        xmpp_ghost_socket=ghost,
    )


def execute(action: Action, cfg: Config) -> None:
    """The ONLY side effect this program can have: restart hostapd. It cannot
    restart OJN, cannot reboot — there is no code path to."""
    if action is Action.RESTART_HOSTAPD:
        log.warning("restarting hostapd: %s", " ".join(cfg.hostapd_restart_cmd))
        result = subprocess.run(
            cfg.hostapd_restart_cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            log.error("hostapd restart FAILED (%d): %s", result.returncode, result.stderr.strip())
        else:
            log.warning("hostapd restarted; expecting the rabbit to re-associate within ~2 s")


def _config_from_env() -> Config:
    mac = os.environ.get("RABBIT_MAC", "")
    if not re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        raise SystemExit("RABBIT_MAC must be set in the environment (00:11:22:33:44:55)")
    # RABBIT_IP is REQUIRED: the HTTP-age signal is scoped to the rabbit's own
    # log lines, so without its IP the watchdog cannot tell the rabbit's traffic
    # from a localhost/admin request. The rabbit has a static lease, so this is
    # always known.
    ip = os.environ.get("RABBIT_IP", "")
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
        raise SystemExit("RABBIT_IP must be set for the watchdog (the rabbit's static-lease IP)")
    restart_cmd = os.environ.get("HOSTAPD_RESTART_CMD", "systemctl restart hostapd").split()
    return Config(
        iface=os.environ.get("AP_IFACE", "wlan1"),
        rabbit_mac=mac,
        rabbit_ip=ip,
        access_log=os.environ.get("OJN_ACCESS_LOG", "/var/log/apache2/ojn-access.log"),
        hostapd_restart_cmd=restart_cmd,
    )


def _policy_from_env() -> Policy:
    def _f(name: str, default: float) -> float:
        return float(os.environ.get(name, default))

    return Policy(
        http_stale_after_s=_f("RECOVERY_HTTP_STALE_S", 600.0),
        cooldown_s=_f("RECOVERY_COOLDOWN_S", 180.0),
        max_restarts_per_outage=int(_f("RECOVERY_MAX_RESTARTS", 3)),
        give_up_hold_s=_f("RECOVERY_GIVE_UP_HOLD_S", 3600.0),
    )


def run_loop(cfg: Config, policy: Policy, interval_s: float, once: bool) -> None:
    state = State()
    while True:
        now = time.time()
        signals = gather_signals(cfg, now)
        decision = decide(signals, state, policy, now)
        state = decision.state
        level = logging.WARNING if decision.action is not Action.NONE else logging.INFO
        log.log(
            level,
            "assoc=%s http_age=%s ghost=%s -> %s (%s)",
            signals.rabbit_associated,
            None if signals.last_http_age_s is None else round(signals.last_http_age_s),
            signals.xmpp_ghost_socket,
            decision.action.value,
            decision.reason,
        )
        execute(decision.action, cfg)
        if once:
            return
        time.sleep(interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single tick and exit (for testing)"
    )
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between ticks (default 60)"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_loop(_config_from_env(), _policy_from_env(), args.interval, args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
