"""tts-bench.py: TTS synthesis-time benchmark (fake provider, no keys/network)."""

import importlib.util
import sys
from pathlib import Path

from rabbit_brain.tts import TTSResult

_SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "tts-bench.py"


def _load():
    spec = importlib.util.spec_from_file_location("tts_bench", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tts_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeProvider:
    def __init__(self, audio_dir):
        self.audio_dir = Path(audio_dir)
        self.calls = []

    async def synth(self, text, language=None):
        self.calls.append((text, language))
        path = self.audio_dir / f"utt{len(self.calls)}.mp3"
        path.write_bytes(b"x")
        return TTSResult(path=path, duration_s=1.0)

    async def close(self):
        pass


async def test_bench_profile_records_rows(monkeypatch, tmp_path):
    mod = _load()
    made = {}

    def fake_factory(audio_dir, env=None):
        p = FakeProvider(audio_dir)
        made["provider"] = p
        return p

    monkeypatch.setattr(mod, "make_tts_provider", fake_factory)
    sentences = [("it", "ciao"), ("en", "hello")]
    rows = await mod._bench_profile("deepgram", sentences, runs=2, audio_dir=tmp_path)
    assert len(rows) == 4  # 2 sentences x 2 runs
    assert {r.language for r in rows} == {"it", "en"}
    assert all(r.audio_s == 1.0 and r.synth_ms >= 0 for r in rows)
    assert made["provider"].calls[0] == ("ciao", "it")  # language forwarded


async def test_bench_profile_skips_when_key_missing(monkeypatch, tmp_path):
    mod = _load()

    def fake_factory(audio_dir, env=None):
        raise KeyError("DEEPGRAM_API_KEY")

    monkeypatch.setattr(mod, "make_tts_provider", fake_factory)
    rows = await mod._bench_profile("deepgram", [("it", "ciao")], runs=1, audio_dir=tmp_path)
    assert rows == []  # missing key → skipped, not crashed


async def test_bench_profile_skips_when_factory_returns_none(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(mod, "make_tts_provider", lambda audio_dir, env=None: None)
    rows = await mod._bench_profile("nope", [("it", "ciao")], runs=1, audio_dir=tmp_path)
    assert rows == []


async def test_bench_profile_survives_synth_failure(monkeypatch, tmp_path):
    mod = _load()

    class Boom(FakeProvider):
        async def synth(self, text, language=None):
            raise RuntimeError("no binary")

    monkeypatch.setattr(mod, "make_tts_provider", lambda audio_dir, env=None: Boom(audio_dir))
    rows = await mod._bench_profile("piper", [("it", "ciao")], runs=2, audio_dir=tmp_path)
    assert rows == []  # failure reported, harness keeps going


def test_median_handles_empty():
    mod = _load()
    assert mod._median([]) != mod._median([])  # nan != nan
    assert mod._median([1.0, 3.0, 2.0]) == 2.0
