"""Sustained-sine modulation measurement (src/tonematch/metrics/sine_mod.py) and its verdict (T066b)."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.metrics import parity, sine_mod

SR = 48_000
T = np.arange(10 * SR) / SR


def sine(f0: float = 330.0) -> np.ndarray:
    return 0.2 * np.sin(2 * np.pi * f0 * T)


def delayed(x: np.ndarray, d: np.ndarray, gain: float = 0.7) -> np.ndarray:
    return x + gain * np.interp(np.arange(x.size) - d * SR, np.arange(x.size), x)


@pytest.mark.parametrize("f0", [330.0, 1000.0])
@pytest.mark.parametrize("rate", [1.8, 6.8])
def test_chorus_rate_is_found_on_any_tone_and_phase(f0, rate):
    x = sine(f0)
    for phase in (0.0, 2.0):
        y = delayed(x, 0.008 + 0.0005 * np.sin(2 * np.pi * rate * T + phase))
        p = sine_mod.sine_profile(y, x, f0)
        assert p.rate_hz == pytest.approx(rate, rel=0.01)
        assert p.confidence > 0.9
        assert p.pitch_swing_cents > 5


def test_slow_lfo_rate_and_cycles():
    x = sine()
    y = delayed(x, 0.006 + 0.002 * np.sin(2 * np.pi * 0.4 * T))
    p = sine_mod.sine_profile(y, x, 330.0)
    assert p.rate_hz == pytest.approx(0.4, rel=0.02)
    assert p.cycles == pytest.approx(0.4 * p.span_s, rel=0.02)


def test_tremolo_depth_from_level_swing_and_no_pitch():
    x = sine()
    y = x * (1 + 0.4 * np.sin(2 * np.pi * 5 * T))
    p = sine_mod.sine_profile(y, x, 330.0)
    assert p.rate_hz == pytest.approx(5.0, rel=0.01)
    assert p.level_swing_db == pytest.approx(20 * np.log10(1.4 / 0.6), abs=0.5)
    assert p.pitch_swing_cents < 1.0


def test_compare_two_lfo_phases_agree():
    x = sine()
    a = sine_mod.sine_profile(delayed(x, 0.008 + 0.0005 * np.sin(2 * np.pi * 1.8 * T)), x, 330.0)
    b = sine_mod.sine_profile(delayed(x, 0.008 + 0.0005 * np.sin(2 * np.pi * 1.8 * T + 3.0)), x, 330.0)
    d = sine_mod.compare(a, b)
    assert abs(d["rate_diff_pct"]) < 0.5 and abs(d["pitch_swing_diff_cents"]) < 1.0 and abs(d["level_swing_diff_db"]) < 1.0


def test_span_selects_part_of_a_decaying_note():
    x = sine() * np.exp(-T / 3)
    y = delayed(x, 0.008 + 0.0005 * np.sin(2 * np.pi * 1.8 * T))
    p = sine_mod.sine_profile(y, x, 330.0, span_s=(0.4, 3.6))
    assert p.rate_hz == pytest.approx(1.8, rel=0.02)
    assert p.span_s == pytest.approx(3.2, abs=0.01)


def _diff(rate=0.0, level=0.0, cents=0.0, mean=0.0, ref_cents=10.0):
    return {"rate_diff_pct": rate, "level_swing_diff_db": level, "pitch_swing_diff_cents": cents, "mean_level_diff_db": mean,
            "pitch_swing_ref_cents": ref_cents}


def test_sine_verdict_pass_fail_and_floor():
    ok = parity.sine_modulation_verdict(_diff(1.0, 0.3, 1.0, 0.2), "pitch", cycles=20, confidence=1.0)
    assert ok["pass"] and ok["checks"] == {"rate": True, "depth": True, "mean_level": True}
    bad = parity.sine_modulation_verdict(_diff(18.0, 0.3, 1.0, 0.2), "pitch", cycles=20, confidence=1.0)
    assert bad["checks"]["rate"] is False and not bad["pass"]
    # a floor above the tolerance: inside 1.5x floor unresolved, beyond it failed
    unr = parity.sine_modulation_verdict(_diff(7.0), "pitch", cycles=20, confidence=1.0, floor={"rate_diff_pct": 6.0})
    assert "rate" in unr["unresolved"] and "rate" not in unr["checks"]
    failed = parity.sine_modulation_verdict(_diff(15.0), "pitch", cycles=20, confidence=1.0, floor={"rate_diff_pct": 6.0})
    assert failed["checks"]["rate"] is False


def test_sine_verdict_needs_two_cycles_unless_told():
    d = _diff(0.5)
    assert not parity.sine_modulation_verdict(d, "sweep", cycles=1.6, confidence=1.0)["resolved"]
    assert parity.sine_modulation_verdict(d, "sweep", cycles=1.6, confidence=1.0, min_cycles=1.5)["resolved"]
    assert not parity.sine_modulation_verdict(d, "sweep", cycles=20, confidence=0.1)["resolved"]
