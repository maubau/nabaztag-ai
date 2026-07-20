"""Runtime helpers: .env loading (values never logged), bounded teardown."""

import asyncio
import logging
import os

from rabbit_brain.runtime import _teardown_step, load_env_file


def test_load_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nexport FOO=bar\nQUOTED="hello world"\nEXISTING=filevalue\n\nNOT A VALID LINE\n'
    )
    monkeypatch.setenv("EXISTING", "shell")
    try:
        assert load_env_file(env) == 2  # FOO + QUOTED; EXISTING skipped
        assert os.environ["FOO"] == "bar"  # `export ` prefix tolerated
        assert os.environ["QUOTED"] == "hello world"  # quotes stripped
        assert os.environ["EXISTING"] == "shell"  # shell environment wins
    finally:
        os.environ.pop("FOO", None)
        os.environ.pop("QUOTED", None)


def test_load_env_file_missing_is_noop(tmp_path):
    assert load_env_file(tmp_path / "absent.env") == 0


async def test_teardown_step_runs_normally():
    ran = []

    async def ok():
        ran.append(1)

    await _teardown_step("ok", ok())
    assert ran == [1]


async def test_teardown_step_swallows_exception(caplog):
    async def boom():
        raise RuntimeError("nope")

    with caplog.at_level(logging.INFO):
        await _teardown_step("boom", boom())  # must not raise
    assert "boom" in caplog.text


async def test_teardown_step_bounded_by_timeout(caplog, monkeypatch):
    """A stuck teardown step (hung network close, task ignoring cancellation)
    must not block the rest of shutdown or the final 'runtime stopped' log
    (hardware round, July 2026: no such line ever printed after several
    Ctrl-C)."""
    monkeypatch.setattr("rabbit_brain.runtime.TEARDOWN_STEP_TIMEOUT_S", 0.05)

    async def hangs():
        await asyncio.Event().wait()  # never returns

    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(_teardown_step("hangs", hangs()), 2)  # test-level safety net
    assert "hangs" in caplog.text
    assert "exceeded" in caplog.text
