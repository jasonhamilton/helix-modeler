"""parity_spot.py helpers: echo finder, log-spectral distance, the pedal lock (offline)."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import parity_spot as PS

SR = 48_000


def _burst(delay_s: float, n_echo: int = 3, fb: float = 0.5) -> np.ndarray:
    x = np.zeros(4 * SR)
    x[int(0.5 * SR)] = 1.0
    for k in range(1, n_echo + 1):
        x[int((0.5 + k * delay_s) * SR)] += fb ** k
    return x


def test_echoes_find_times_and_levels():
    d, e = PS.echoes(_burst(0.375))
    assert abs(d - 0.5) < 0.003
    assert len(e) == 3
    assert abs(e[0][0] - 0.875) < 0.003 and abs(e[1][0] - 1.25) < 0.003
    assert abs((e[1][1] - e[0][1]) - 20 * np.log10(0.5)) < 1.0


def test_log_spectral_distance_zero_for_equal_and_gain_invariant():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(SR)
    assert PS.log_spectral_distance(x, x) < 1e-6
    assert PS.log_spectral_distance(2.5 * x, x) < 1e-6
    assert PS.log_spectral_distance(rng.standard_normal(SR), x) > 3


def test_manifest_models_are_unique_and_five_per_category():
    items = PS.items(None)
    names = [i["name"] for i in items]
    assert len(names) == len(set(names))
    for cat in ("Distortion", "Delay", "Reverb", "Dynamics", "EQ", "Modulation", "Amp", "Cab"):
        assert sum(1 for i in items if i["category"] == cat) >= 5
