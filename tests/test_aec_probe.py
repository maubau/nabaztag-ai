"""aec-probe.py: the pure measurement helpers (no device, no audio)."""

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "aec-probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("aec_probe", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aec_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load()

# numpy ships with the audio extra, not with [dev] — the signal-shaped checks
# skip cleanly where it isn't installed (CI's lint/unit job).
requires_numpy = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None, reason="needs numpy (audio extra)"
)


def test_db_over_is_a_ratio_in_db():
    assert probe._db_over(10.0, 10.0) == 0.0  # same level -> 0 dB
    assert probe._db_over(100.0, 10.0) == pytest.approx(20.0)  # x10 -> +20 dB
    assert probe._db_over(20.0, 10.0) == pytest.approx(6.0206, abs=1e-3)


def test_db_over_is_guarded_against_silence():
    # a digital-silence floor must not produce inf/NaN and drive the verdict
    assert probe._db_over(5.0, 0.0) == 0.0
    assert probe._db_over(0.0, 5.0) == 0.0


@requires_numpy
def test_rms_of_silence_is_zero_and_scales_with_level():
    import numpy as np

    assert probe._rms(b"") == 0.0
    quiet = np.full(1000, 100, dtype=np.int16).tobytes()
    loud = np.full(1000, 1000, dtype=np.int16).tobytes()
    assert probe._rms(quiet) == pytest.approx(100.0)
    # 10x amplitude is +20 dB, which is the scale the verdict thresholds read
    assert probe._db_over(probe._rms(loud), probe._rms(quiet)) == pytest.approx(20.0, abs=1e-3)


@requires_numpy
def test_noise_signal_has_the_requested_shape():
    rate, seconds = 16000, 0.5
    mono = probe._noise(seconds, rate, 1)
    assert len(mono) == int(rate * seconds) * 2  # int16
    stereo = probe._noise(seconds, rate, 2)
    assert len(stereo) == len(mono) * 2  # duplicated per channel


def test_thresholds_are_ordered():
    # "quiet" must sit below "loud", or the verdict branches are unreachable
    assert probe.QUIET_DB < probe.LOUD_DB
    assert math.isclose(probe.QUIET_DB, 6.0)


def test_documents_that_a_quiet_result_is_ambiguous():
    # the double-talk stage exists precisely because a silent stage 2 also
    # describes a muted speaker or a dead mic — that caveat must stay written
    assert "dead mic" in _SCRIPT.read_text()
