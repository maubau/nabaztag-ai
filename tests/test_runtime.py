"""Runtime helpers: .env loading (values never logged)."""

import os

from rabbit_brain.runtime import load_env_file


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
