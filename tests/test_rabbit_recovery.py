"""deploy/rabbit_recovery.py — the pure decision logic and the signal parsers.

The whole safety of this watchdog is in `decide`: it must heal the real Wi-Fi
disassociation (OJN_API_NOTES #13) yet never restart hostapd for a healthy or a
merely-idle or a simply-powered-off rabbit, and never loop. Every branch gets a
test.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "deploy" / "rabbit_recovery.py"


def _load():
    spec = importlib.util.spec_from_file_location("rabbit_recovery", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rabbit_recovery"] = mod  # dataclass annotation resolution needs it
    spec.loader.exec_module(mod)
    return mod


rr = _load()

POLICY = rr.Policy(
    http_stale_after_s=600.0,
    cooldown_s=180.0,
    max_restarts_per_outage=3,
    give_up_hold_s=3600.0,
)

# The failure signature: disassociated + old HTTP + ghost XMPP socket.
OUTAGE = rr.Signals(rabbit_associated=False, last_http_age_s=1000.0, xmpp_ghost_socket=True)


def _decide(signals, state, now=10_000.0):
    return rr.decide(signals, state, POLICY, now)


def test_associated_rabbit_is_left_alone_and_resets_state():
    # even carrying prior outage bookkeeping, a healthy rabbit clears it
    dirty = rr.State(last_restart_at=5.0, restarts_this_outage=2, given_up_at=9.0)
    d = _decide(rr.Signals(True, 1.0, False), dirty)
    assert d.action is rr.Action.NONE
    assert d.state == rr.State()  # fully reset


def test_unknown_association_never_restarts():
    d = _decide(rr.Signals(None, 9999.0, True), rr.State())
    assert d.action is rr.Action.NONE
    assert "unknown" in d.reason


def test_disassociated_but_recent_http_does_not_restart():
    # the rabbit just left; hostapd is not the problem
    d = _decide(rr.Signals(False, 5.0, True), rr.State())
    assert d.action is rr.Action.NONE


def test_disassociated_without_ghost_socket_does_not_restart():
    # simply powered off with no lingering session → not our failure
    d = _decide(rr.Signals(False, 1000.0, False), rr.State())
    assert d.action is rr.Action.NONE


def test_missing_access_log_is_not_treated_as_old():
    # last_http_age None (no log yet) must not be read as "old"
    d = _decide(rr.Signals(False, None, True), rr.State())
    assert d.action is rr.Action.NONE


def test_the_three_signals_coinciding_restarts_hostapd():
    d = _decide(OUTAGE, rr.State())
    assert d.action is rr.Action.RESTART_HOSTAPD
    assert d.state.restarts_this_outage == 1
    assert d.state.last_restart_at == 10_000.0


def test_cooldown_blocks_a_second_restart_too_soon():
    state = rr.State(last_restart_at=9_900.0, restarts_this_outage=1)  # 100s ago < 180
    d = _decide(OUTAGE, state)
    assert d.action is rr.Action.COOLDOWN
    assert d.state.restarts_this_outage == 1  # unchanged


def test_after_cooldown_a_further_restart_is_allowed():
    state = rr.State(last_restart_at=9_700.0, restarts_this_outage=1)  # 300s ago > 180
    d = _decide(OUTAGE, state)
    assert d.action is rr.Action.RESTART_HOSTAPD
    assert d.state.restarts_this_outage == 2


def test_restart_cap_gives_up_instead_of_looping():
    # 3 restarts already, cooldown elapsed → do NOT restart a 4th time
    state = rr.State(last_restart_at=9_000.0, restarts_this_outage=3)
    d = _decide(OUTAGE, state)
    assert d.action is rr.Action.GIVE_UP
    assert d.state.given_up_at == 10_000.0


def test_after_giving_up_it_holds_and_does_not_restart():
    state = rr.State(restarts_this_outage=3, given_up_at=9_950.0)  # 50s into the hold
    d = _decide(OUTAGE, state)
    assert d.action is rr.Action.HELD


def test_hold_expiry_permits_one_fresh_attempt():
    # an hour later, allow a retry (the rabbit may have been switched on)
    state = rr.State(restarts_this_outage=3, given_up_at=10_000.0 - 3601.0)
    d = _decide(OUTAGE, state)
    assert d.action is rr.Action.RESTART_HOSTAPD
    assert d.state.restarts_this_outage == 1  # counted as a fresh outage
    assert d.state.given_up_at is None


def test_recovery_resets_the_outage_after_a_restart_worked():
    # restarted once, then the rabbit came back → association resets everything,
    # so the NEXT outage starts fresh rather than one-away from the cap
    after_restart = rr.State(last_restart_at=9_900.0, restarts_this_outage=1)
    healed = _decide(rr.Signals(True, 1.0, False), after_restart)
    assert healed.state == rr.State()


# --- parsers ---


def test_parse_associated_matches_mac_case_insensitively():
    dump = "Station AA:BB:CC:DD:EE:FF (on wlan1)\n\tsignal: -28 dBm\n"
    assert rr.parse_associated(dump, "aa:bb:cc:dd:ee:ff") is True
    assert rr.parse_associated(dump, "11:22:33:44:55:66") is False
    assert rr.parse_associated("", "aa:bb:cc:dd:ee:ff") is False


def test_parse_xmpp_ghost_detects_port_5222_socket():
    ss = "ESTAB 0 846 192.168.66.1:5222 192.168.66.10:49152\n"
    assert rr.parse_xmpp_ghost(ss, "192.168.66.10") is True
    assert rr.parse_xmpp_ghost(ss, "192.168.66.99") is False  # different peer
    assert rr.parse_xmpp_ghost(ss, None) is True  # any :5222 socket counts
    assert rr.parse_xmpp_ghost("", None) is False


def test_execute_only_acts_on_restart_action(monkeypatch):
    calls = []
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: calls.append(a) or _fake_ok())
    cfg = rr.Config(
        "wlan1", "aa:bb:cc:dd:ee:ff", None, "/tmp/x", ["systemctl", "restart", "hostapd"]
    )
    rr.execute(rr.Action.NONE, cfg)
    rr.execute(rr.Action.COOLDOWN, cfg)
    rr.execute(rr.Action.GIVE_UP, cfg)
    assert calls == []  # none of these touch the system
    rr.execute(rr.Action.RESTART_HOSTAPD, cfg)
    assert calls and calls[0][0] == ["systemctl", "restart", "hostapd"]


class _FakeProc:
    returncode = 0
    stderr = ""


def _fake_ok():
    return _FakeProc()


def test_config_from_env_requires_valid_mac(monkeypatch):
    monkeypatch.delenv("RABBIT_MAC", raising=False)
    with pytest.raises(SystemExit):
        rr._config_from_env()
    monkeypatch.setenv("RABBIT_MAC", "not-a-mac")
    with pytest.raises(SystemExit):
        rr._config_from_env()


def test_config_from_env_requires_rabbit_ip(monkeypatch):
    # RABBIT_IP is mandatory: without it the HTTP-age signal can't be scoped
    monkeypatch.setenv("RABBIT_MAC", "aa:bb:cc:dd:ee:ff")
    monkeypatch.delenv("RABBIT_IP", raising=False)
    with pytest.raises(SystemExit, match="RABBIT_IP"):
        rr._config_from_env()
    monkeypatch.setenv("RABBIT_IP", "192.168.66.10")
    cfg = rr._config_from_env()
    assert cfg.rabbit_ip == "192.168.66.10"


# --- HTTP-age from the rabbit's own log lines (the blocker fix) ---

RABBIT = "192.168.66.10"
# Apache ojn_noquery format: %h %l %u %t "%m %U %H" %>s %b "%{User-Agent}i"
_RABBIT_0452 = f'{RABBIT} - - [24/Jul/2026:04:52:13 +0000] "GET /vl/bc.jsp HTTP/1.1" 200 512 "-"'
_LOCAL_0828 = '127.0.0.1 - - [24/Jul/2026:08:28:41 +0000] "GET /server-status HTTP/1.1" 200 90 "-"'
# now = 24/Jul/2026 08:30:00 UTC
NOW = rr.parse_apache_epoch("[24/Jul/2026:08:30:00 +0000]")


def test_age_comes_from_rabbit_line_not_file_mtime(tmp_path):
    # the real trap: rabbit last spoke at 04:52, but a localhost curl at 08:28
    # rewrote the log. mtime is recent; the age MUST come from the 04:52 line.
    log = tmp_path / "ojn-access.log"
    log.write_text(_RABBIT_0452 + "\n" + _LOCAL_0828 + "\n")  # localhost line is newest
    age = rr.last_http_age(str(log), RABBIT, NOW)
    assert age == pytest.approx((8 * 3600 + 30 * 60) - (4 * 3600 + 52 * 60 + 13))  # ~12767s
    assert age is not None and age > 12000  # decisively "old", unlike mtime (~79s)


def test_age_uses_the_newest_rabbit_line():
    older = f'{RABBIT} - - [24/Jul/2026:04:00:00 +0000] "GET /a HTTP/1.1" 200 1 "-"'
    newer = f'{RABBIT} - - [24/Jul/2026:08:00:00 +0000] "GET /b HTTP/1.1" 200 1 "-"'
    # newest-first scan: caller passes reversed(lines); the 08:00 wins
    epoch = rr.last_rabbit_epoch([newer, older], RABBIT)
    assert epoch == rr.parse_apache_epoch("[24/Jul/2026:08:00:00 +0000]")


def test_missing_log_returns_none(tmp_path):
    assert rr.last_http_age(str(tmp_path / "nope.log"), RABBIT, NOW) is None


def test_no_rabbit_line_returns_none(tmp_path):
    log = tmp_path / "ojn-access.log"
    log.write_text(_LOCAL_0828 + "\n")  # only localhost traffic
    assert rr.last_http_age(str(log), RABBIT, NOW) is None


def test_malformed_timestamp_returns_none(tmp_path):
    log = tmp_path / "ojn-access.log"
    log.write_text(f'{RABBIT} - - [not-a-timestamp] "GET / HTTP/1.1" 200 1 "-"\n')
    assert rr.last_http_age(str(log), RABBIT, NOW) is None


def test_partial_ip_does_not_match(tmp_path):
    # a client 192.168.66.100 must NOT satisfy a rabbit at 192.168.66.10
    log = tmp_path / "ojn-access.log"
    log.write_text('192.168.66.100 - - [24/Jul/2026:08:29:00 +0000] "GET / HTTP/1.1" 200 1 "-"\n')
    assert rr.last_http_age(str(log), RABBIT, NOW) is None


def test_falls_back_to_rotated_log(tmp_path):
    # current log has no rabbit line; the rotated .1 does
    log = tmp_path / "ojn-access.log"
    log.write_text(_LOCAL_0828 + "\n")
    (tmp_path / "ojn-access.log.1").write_text(_RABBIT_0452 + "\n")
    age = rr.last_http_age(str(log), RABBIT, NOW)
    assert age is not None and age > 12000


def test_current_log_wins_over_rotated(tmp_path):
    # a rabbit line in the current log is newer than one in .1 → use current
    current = f'{RABBIT} - - [24/Jul/2026:08:00:00 +0000] "GET /new HTTP/1.1" 200 1 "-"'
    (tmp_path / "ojn-access.log").write_text(current + "\n")
    (tmp_path / "ojn-access.log.1").write_text(_RABBIT_0452 + "\n")
    age = rr.last_http_age(str(tmp_path / "ojn-access.log"), RABBIT, NOW)
    assert age == pytest.approx(30 * 60)  # 08:30 - 08:00 = 1800s, not the 04:52 line


def test_bounded_tail_reads_only_the_end(tmp_path):
    # a huge prefix of localhost noise, then one recent rabbit line at the end:
    # a small max_bytes still finds the rabbit line without loading the prefix
    log = tmp_path / "ojn-access.log"
    noise = (_LOCAL_0828 + "\n") * 5000
    rabbit = f'{RABBIT} - - [24/Jul/2026:08:29:00 +0000] "GET /z HTTP/1.1" 200 1 "-"\n'
    log.write_text(noise + rabbit)
    age = rr.last_http_age(str(log), RABBIT, NOW, max_bytes=4096)
    assert age == pytest.approx(60.0)  # 08:30 - 08:29
