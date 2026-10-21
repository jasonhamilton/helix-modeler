"""Offline checks for the T198 tuning helpers (no Native, no pedal, no song audio)."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ds_sample_tune_t198 as T  # noqa: E402


def test_x_values_round_trip_and_bounds():
    tut = {(s, n): (lo + hi) / 2 for s, n, lo, hi in T.KNOBS}
    x = T.values_to_x(tut)
    assert np.allclose(x, 0.5)
    v = T.x_to_values(np.array([-1.0] + [2.0] * (len(T.KNOBS) - 1)))
    assert v[(0, "Gain")] == 0.0 and v[(1, "Drive")] == 1.0 and v[(4, "16kHz")] == T.EQ_LIM


def test_arm_a_freezes_bands_below_250hz():
    tut = {(s, n): 0.123 for s, n, _, _ in T.KNOBS}
    v = T.x_to_values(np.full(len(T.KNOBS), 0.9), "a", None, tut)
    for k in T.LOW_EQ:
        assert v[k] == 0.123
    assert v[(4, "250Hz")] != 0.123


def test_band_diff_ignores_pure_level_and_sees_tilt():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(48_000 * 4).astype(np.float32)
    assert max(abs(v) for v in T.band_diff(3.0 * x, x)) < 0.05
    lo = T.highpass(x, 250.0)
    d = T.band_diff(lo, x)
    assert d[1] < -10 and abs(d[-2]) < 1.5       # 62-125 Hz band lost, 8-16 kHz band kept


def test_mrstft_highpass_variant_ignores_the_bass():
    rng = np.random.default_rng(1)
    base = rng.standard_normal(48_000 * 3).astype(np.float32)
    t = T.highpass(base, 250.0)
    bass = np.sin(2 * np.pi * 60 * np.arange(len(t)) / T.SR).astype(np.float32)
    s_hp = T.Mrstft(t, hp=True)
    s_full = T.Mrstft(t, hp=False)
    assert s_hp(t + 3 * bass) < 0.05 < s_full(t + 3 * bass)
    assert s_hp(t) < 1e-6
