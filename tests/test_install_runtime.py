"""deploy/install-runtime.sh: preflight gates and unit templating.

The preflight exists to catch the failure modes that already bit us on
hardware — an aiohttp-served MP3 the rabbit silently ignores, a missing
Deepgram key that leaves Piper with no fallback, the MCP server double-binding
the runtime's ports. Those checks are worth a test of their own.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "install-runtime.sh"
UNIT = "nabaztag-runtime.service"
UNIT_FILE = SCRIPT.parent / UNIT


def _section_of_keys(unit_text: str) -> dict[str, str]:
    """Map each directive key -> the [Section] it appears under, so we can assert
    a key lives in the right section (systemd silently ignores misplaced keys)."""
    section = None
    placement: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif "=" in line and section is not None:
            placement[line.split("=", 1)[0]] = section
    return placement


requires_bash = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash on PATH")

GOOD_ENV = """\
TTS_PROFILE=piper
PIPER_URL_IT=http://127.0.0.1:5001
PIPER_URL_EN=http://127.0.0.1:5002
PIPER_LENGTH_SCALE_IT=1.25
PIPER_LENGTH_SCALE_EN=1.0
DEEPGRAM_API_KEY=dg-test
OPENAI_API_KEY=oa-test
NABAZTAG_MP3_SERVE_HTTP=0
"""


def _make_repo(tmp_path: Path, env_text: str = GOOD_ENV, config: str = "wake:\n") -> Path:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    python = repo / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (repo / ".env").write_text(env_text)
    (repo / "config.yaml").write_text(config)
    # the script renders the unit template from its own repo layout
    (repo / "deploy").mkdir()
    shutil.copyfile(SCRIPT.parent / "nabaztag-runtime.service", repo / "deploy" / UNIT)
    return repo


def _preflight(repo: Path) -> subprocess.CompletedProcess:
    snippet = f'source "{SCRIPT}"; cmd_preflight'
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "REPO_DIR": str(repo), "RUN_USER": "tester"},
    )


@requires_bash
def test_preflight_passes_on_a_complete_production_env(tmp_path):
    result = _preflight(_make_repo(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preflight passed" in result.stdout


@requires_bash
def test_preflight_rejects_aiohttp_served_mp3(tmp_path):
    # hardware finding: the MTL decoder ignores aiohttp-served audio, so
    # NABAZTAG_MP3_SERVE_HTTP=1 means a mute rabbit
    repo = _make_repo(tmp_path, GOOD_ENV.replace("SERVE_HTTP=0", "SERVE_HTTP=1"))
    result = _preflight(repo)
    assert result.returncode != 0
    assert "NABAZTAG_MP3_SERVE_HTTP must be 0" in result.stderr


@requires_bash
def test_preflight_rejects_piper_without_deepgram_fallback(tmp_path):
    repo = _make_repo(tmp_path, GOOD_ENV.replace("DEEPGRAM_API_KEY=dg-test\n", ""))
    result = _preflight(repo)
    assert result.returncode != 0
    assert "DEEPGRAM_API_KEY missing" in result.stderr


@requires_bash
def test_preflight_requires_piper_length_scales(tmp_path):
    repo = _make_repo(tmp_path, GOOD_ENV.replace("PIPER_LENGTH_SCALE_IT=1.25\n", ""))
    result = _preflight(repo)
    assert result.returncode != 0
    assert "PIPER_LENGTH_SCALE_IT missing" in result.stderr


@requires_bash
def test_preflight_warns_about_placeholder_wake_word(tmp_path):
    repo = _make_repo(tmp_path, config="wake:\n  models: [hey_jarvis]\n")
    result = _preflight(repo)
    assert result.returncode == 0  # a warning, not a blocker
    assert "still hey_jarvis" in result.stdout


@requires_bash
def test_preflight_fails_without_config(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "config.yaml").unlink()
    result = _preflight(repo)
    assert result.returncode != 0
    assert "no config.yaml" in result.stderr


@requires_bash
def test_render_unit_substitutes_user_and_paths(tmp_path):
    repo = _make_repo(tmp_path)
    snippet = f'source "{SCRIPT}"; _render_unit'
    result = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "REPO_DIR": str(repo), "RUN_USER": "tester"},
    )
    assert result.returncode == 0, result.stderr
    assert "User=tester" in result.stdout
    assert f"WorkingDirectory={repo}" in result.stdout
    assert f"EnvironmentFile={repo}/.env" in result.stdout
    assert f"ReadWritePaths={repo}" in result.stdout
    assert f"ExecStart={repo}/.venv/bin/python -m rabbit_brain.runtime" in result.stdout
    # a dead Piper must not take the rabbit down with it: ordering/wanting only,
    # never a hard Requires= directive (the word appears in a comment, so match
    # actual directive lines)
    directives = [ln for ln in result.stdout.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("Requires=") for ln in directives)
    assert "Restart=always" in directives


def test_start_limit_keys_live_in_unit_section():
    # systemd hardware finding: StartLimitIntervalSec/StartLimitBurst under
    # [Service] are silently dropped ("Unknown key name ... ignoring"), so the
    # crash-loop cap never applies. They MUST be under [Unit].
    placement = _section_of_keys(UNIT_FILE.read_text())
    assert placement.get("StartLimitIntervalSec") == "[Unit]"
    assert placement.get("StartLimitBurst") == "[Unit]"
    # runtime knobs stay where they belong
    assert placement.get("Restart") == "[Service]"
    assert placement.get("ExecStart") == "[Service]"


@requires_bash
def test_render_preserves_start_limit_section(tmp_path):
    # the sed templating must not move keys between sections
    repo = _make_repo(tmp_path)
    result = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; _render_unit'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "REPO_DIR": str(repo), "RUN_USER": "tester"},
    )
    placement = _section_of_keys(result.stdout)
    assert placement.get("StartLimitIntervalSec") == "[Unit]"
    assert placement.get("StartLimitBurst") == "[Unit]"
