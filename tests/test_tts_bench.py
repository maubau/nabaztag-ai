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
        made["env"] = env
        return p

    monkeypatch.setattr(mod, "make_tts_provider", fake_factory)
    sentences = [("it", "ciao"), ("en", "hello")]
    rows = await mod._bench_profile("deepgram", sentences, runs=2, audio_dir=tmp_path)
    assert len(rows) == 4  # 2 sentences x 2 runs
    assert {r.language for r in rows} == {"it", "en"}
    assert all(r.audio_s == 1.0 and r.synth_ms >= 0 for r in rows)
    # char count recorded per row (drives synth-time scaling)
    assert {r.chars for r in rows} == {len("ciao"), len("hello")}
    assert made["provider"].calls[0] == ("ciao", "it")  # language forwarded
    # the bench always disables the piper→deepgram fallback
    assert made["env"]["PIPER_FALLBACK_DEEPGRAM"] == "0"


async def test_bench_discards_fallback_leaked_result(monkeypatch, tmp_path):
    """If a result comes back tagged as a different backend (a fallback leaking
    through), it must be discarded — never counted under this profile."""
    mod = _load()

    class LeakyProvider(FakeProvider):
        async def synth(self, text, language=None):
            self.calls.append((text, language))
            path = self.audio_dir / "leak.mp3"
            path.write_bytes(b"x")
            # asked for piper, got a deepgram (fallback) clip
            return TTSResult(path=path, duration_s=1.0, provider="deepgram")

    monkeypatch.setattr(
        mod, "make_tts_provider", lambda audio_dir, env=None: LeakyProvider(audio_dir)
    )
    rows = await mod._bench_profile("piper", [("it", "ciao")], runs=3, audio_dir=tmp_path)
    assert rows == []  # nothing credited to piper


async def test_bench_counts_matching_provider_tag(monkeypatch, tmp_path):
    mod = _load()

    class TaggedProvider(FakeProvider):
        async def synth(self, text, language=None):
            self.calls.append((text, language))
            path = self.audio_dir / f"utt{len(self.calls)}.mp3"
            path.write_bytes(b"x")
            return TTSResult(path=path, duration_s=1.0, provider="piper")

    monkeypatch.setattr(
        mod, "make_tts_provider", lambda audio_dir, env=None: TaggedProvider(audio_dir)
    )
    rows = await mod._bench_profile("piper", [("it", "ciao")], runs=2, audio_dir=tmp_path)
    assert len(rows) == 2  # real piper results counted


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


async def test_output_dir_keeps_labelled_mp3s(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(
        mod, "make_tts_provider", lambda audio_dir, env=None: FakeProvider(audio_dir)
    )
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "kept"
    out.mkdir()
    rows = await mod._bench_profile(
        "deepgram", [("it", "Ciao mondo")], runs=2, audio_dir=work, output_dir=out
    )
    assert len(rows) == 2
    kept = sorted(p.name for p in out.glob("*.mp3"))
    # labelled by profile, language, a text slug, and the run index
    assert kept == ["deepgram_it_ciao-mondo_0.mp3", "deepgram_it_ciao-mondo_1.mp3"]


async def test_no_output_dir_keeps_nothing(monkeypatch, tmp_path):
    mod = _load()
    monkeypatch.setattr(
        mod, "make_tts_provider", lambda audio_dir, env=None: FakeProvider(audio_dir)
    )
    out = tmp_path / "kept"
    out.mkdir()
    await mod._bench_profile(
        "deepgram", [("it", "ciao")], runs=1, audio_dir=tmp_path, output_dir=None
    )
    assert list(out.glob("*.mp3")) == []  # nothing kept when output_dir is None


def test_slug_is_filesystem_safe():
    mod = _load()
    # ASCII-only, dash-collapsed (the accented è is dropped — fine for a label)
    slug = mod._slug("Ciao, com'è il tempo?")
    assert slug == "ciao-com-il-tempo"
    assert __import__("re").fullmatch(r"[a-z0-9-]+", slug)
    assert mod._slug("") == "utt"  # never empty
    assert len(mod._slug("x" * 100)) <= 32


def test_median_handles_empty():
    mod = _load()
    assert mod._median([]) != mod._median([])  # nan != nan
    assert mod._median([1.0, 3.0, 2.0]) == 2.0
