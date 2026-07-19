"""config-doctor: legacy-config migration (Anthropic model → OpenAI, VAD, etc.)."""

import importlib.util
import sys
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "config-doctor.py"


def _load():
    # the script's filename has a hyphen; load it by path and register it in
    # sys.modules so its @dataclass can resolve its own module
    spec = importlib.util.spec_from_file_location("config_doctor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["config_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


doctor = _load()

# A realistic legacy config: deepgram.model comes BEFORE llm.model (as in the
# example), an explicit anthropic provider, and a Claude model.
LEGACY = """\
deepgram:
  model: nova-3
  language: multi
  endpointing: 100

llm:
  provider: anthropic
  model: claude-sonnet-4-6
  max_history_turns: 20

audio:
  capture_device: "hw:CARD=C16K6Ch,DEV=0"
  vad_end_of_speech_ms: 1600
"""


def test_legacy_llm_is_flagged_and_migrated():
    fixed, problems = doctor.diagnose(LEGACY, fix=True)
    joined = " ".join(problems)
    assert "llm.provider" in joined and "llm.model" in joined

    cfg = yaml.safe_load(fixed)
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-5.4-mini"
    # the section-aware rewrite must NOT touch deepgram.model
    assert cfg["deepgram"]["model"] == "nova-3"
    # unrelated keys preserved
    assert cfg["llm"]["max_history_turns"] == 20


def test_migration_is_idempotent():
    fixed_once, _ = doctor.diagnose(LEGACY, fix=True)
    fixed_twice, problems = doctor.diagnose(fixed_once, fix=True)
    assert problems == []  # nothing left to migrate
    assert fixed_twice == fixed_once


def test_claude_model_without_provider_still_migrated():
    # legacy config with no explicit provider (defaults to openai in code) but a
    # Claude model that WOULD be sent to OpenAI — the model must be migrated
    text = "llm:\n  model: claude-3-5-sonnet\n  max_history_turns: 10\n"
    fixed, problems = doctor.diagnose(text, fix=True)
    assert any("llm.model" in p for p in problems)
    assert not any("llm.provider" in p for p in problems)  # missing provider is fine
    assert yaml.safe_load(fixed)["llm"]["model"] == "gpt-5.4-mini"


def test_openai_config_is_clean():
    text = (
        "deepgram:\n  endpointing: 100\n"
        "llm:\n  provider: openai\n  model: gpt-5.4-mini\n"
        'audio:\n  capture_device: "hw:CARD=C16K6Ch,DEV=0"\n  vad_end_of_speech_ms: 1600\n'
    )
    _fixed, problems = doctor.diagnose(text, fix=True)
    assert problems == []


def test_custom_openai_model_is_not_touched():
    # a deliberate non-default OpenAI model must be left alone (only Anthropic
    # models are migrated)
    text = "llm:\n  provider: openai\n  model: gpt-4o-mini\n"
    fixed, problems = doctor.diagnose(text, fix=True)
    assert not any("llm.model" in p for p in problems)
    assert yaml.safe_load(fixed)["llm"]["model"] == "gpt-4o-mini"
