"""brain/scripts/train-wake-word.sh — install-smoke WITHOUT real training.

A first Bolt probe of the naive script failed because every packaging assumption
was wrong (wrong extra, unpinned+incompatible generator, a non-existent data
CLI, an incomplete config, a bilingual key nothing reads). These checks lock in
the corrected, verified facts so the script can't silently regress to any of
them — none of this runs openWakeWord or clones anything (that's the Bolt's job).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "train-wake-word.sh"
TEXT = SCRIPT.read_text()
# Executable lines only — the header comments deliberately NAME the wrong-path
# pitfalls (data.py, tts_language) to explain why they're avoided, so those
# checks must look past comments.
CODE = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))

requires_bash = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash on PATH")

# Every key openWakeWord 0.6.0's examples/custom_model.yml defines. A config
# missing any of these is the incompleteness the review caught.
REQUIRED_KEYS = {
    "model_name",
    "target_phrase",
    "custom_negative_phrases",
    "n_samples",
    "n_samples_val",
    "tts_batch_size",
    "augmentation_batch_size",
    "piper_sample_generator_path",
    "output_dir",
    "rir_paths",
    "background_paths",
    "background_paths_duplication_rate",
    "false_positive_validation_data_path",
    "augmentation_rounds",
    "feature_data_files",
    "batch_n_per_class",
    "model_type",
    "layer_size",
    "steps",
    "max_negative_weight",
    "target_false_positives_per_hour",
}


def _ref(name: str) -> str:
    m = re.search(rf'^{name}="([^"]*)"', TEXT, re.MULTILINE)
    assert m, f"{name} not found in the script"
    return m.group(1)


def test_both_checkouts_are_pinned_to_full_shas():
    # not a branch/tag: master was both unpinned AND incompatible (it dropped
    # generate_samples.py). A 40-hex SHA is the only reproducible pin.
    for name in ("OWW_REF", "GEN_REF"):
        assert re.fullmatch(r"[0-9a-f]{40}", _ref(name)), f"{name} must be a 40-hex SHA"


def test_generator_is_the_v2_compatible_commit():
    # the exact commit that still ships top-level generate_samples.py
    assert _ref("GEN_REF") == "195e3bd967d54589c2137c9de2b22ad526ba6b6f"


def test_uses_no_deps_and_not_the_wrong_extra():
    assert "[training]" not in TEXT  # the upstream extra is `full`, not `training`
    # openWakeWord must be installed --no-deps so its tflite-runtime base dep
    # (no cp312 wheel) is skipped
    assert re.search(r"pip install --no-deps -e \"?\$OWW_DIR", TEXT)


def test_is_onnx_only_no_tensorflow():
    # ONNX-only: none of the TFLite-conversion trio may be installed
    for banned in ("tensorflow", "tensorflow_probability", "onnx_tf", "tflite-runtime"):
        assert not re.search(rf"pip install[^\n]*{re.escape(banned)}", TEXT), (
            f"{banned} must not be installed (ONNX-only)"
        )


def test_does_not_use_the_nonexistent_data_cli():
    # the pinned openWakeWord has no `data.py --output_dir` CLI
    assert "data.py" not in CODE


def test_no_dead_bilingual_key():
    # tts_language is read by nothing in train.py — it must not reappear in code
    assert "tts_language" not in CODE


def _render(mode: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; cmd_printconfig {mode}'],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "TRAIN_DIR": "/tmp/tw"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _keys(config_text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^([a-z_]+):", config_text, re.MULTILINE)}


@requires_bash
def test_smoke_config_is_complete():
    assert _keys(_render("smoke")) >= REQUIRED_KEYS


@requires_bash
def test_full_config_is_complete():
    assert _keys(_render("full")) >= REQUIRED_KEYS


@requires_bash
def test_smoke_config_is_cheap():
    # the whole point of smoke: tiny sample counts and a 2-step train
    cfg = _render("smoke")
    assert re.search(r"^n_samples: 20$", cfg, re.MULTILINE)
    assert re.search(r"^steps: 2$", cfg, re.MULTILINE)
    # and it must not depend on the multi-GB ACAV feature file
    assert "ACAV100M" not in cfg


@requires_bash
def test_full_config_uses_the_real_corpora():
    cfg = _render("full")
    assert re.search(r"^n_samples: 30000$", cfg, re.MULTILINE)
    assert "ACAV100M_sample" in cfg  # negative features
    assert "mit_rirs" in cfg  # RIR augmentation


@pytest.mark.skipif(
    "WAKE_ONLINE_CHECK" not in __import__("os").environ,
    reason="online generator-API check; set WAKE_ONLINE_CHECK=1 to run",
)
def test_generator_commit_ships_generate_samples():
    # opt-in network check: the pinned generator commit really exposes the
    # top-level module train.py imports (`from generate_samples import ...`)
    import urllib.error
    import urllib.request

    url = (
        "https://raw.githubusercontent.com/rhasspy/piper-sample-generator/"
        f"{_ref('GEN_REF')}/generate_samples.py"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            assert resp.status == 200
    except urllib.error.URLError as exc:
        # a network/cert problem (e.g. the macOS Python cert quirk) is not a
        # verdict on the pin — don't turn connectivity into a false failure
        pytest.skip(f"generator API unreachable: {exc}")
