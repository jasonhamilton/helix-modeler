"""Block-presence detector (T102): measurements on synthetic signals, and invariance to the notes played."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import signal

from tonematch.metrics import presence as PR
from tonematch.metrics.reverb import synthetic_reverb

SR = 48_000


def plucks(seed: int, seconds: float = 6.0, n_notes: int = 9) -> np.ndarray:
    """A clean-DI stand-in: Karplus-Strong plucks of random pitches at random times."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n)
    starts = np.sort(rng.uniform(0.05, seconds - 1.2, n_notes))
    for s in starts:
        f0 = 82.4 * 2 ** (rng.integers(0, 30) / 12)
        p = round(SR / f0)
        buf = rng.uniform(-1, 1, p)
        length = int(rng.uniform(0.6, 1.4) * SR)
        note = np.empty(length)
        for i in range(length):
            note[i] = buf[i % p]
            buf[i % p] = 0.996 * 0.5 * (buf[i % p] + buf[(i + 1) % p])
        i0 = int(s * SR)
        seg = note[: n - i0]
        out[i0: i0 + seg.size] += seg * rng.uniform(0.5, 1.0)
    return out / np.max(np.abs(out)) * 0.5


def delayed(x: np.ndarray, t: float, g: float, repeats: int = 1) -> np.ndarray:
    y = x.copy()
    d = round(t * SR)
    for k in range(1, repeats + 1):
        y[k * d:] += g ** k * x[: x.size - k * d]
    return y


def mod_delay(x: np.ndarray, base_ms: float, depth_ms: float, rate_hz: float) -> np.ndarray:
    t = np.arange(x.size)
    d = (base_ms + depth_ms * np.sin(2 * np.pi * rate_hz * t / SR)) * SR / 1000
    return np.interp(t - d, t, x, left=0.0)


@pytest.fixture(scope="module")
def di_a() -> np.ndarray:
    return plucks(1)


@pytest.fixture(scope="module")
def di_b() -> np.ndarray:
    return plucks(2)


def test_delay_time_known_di_and_note_invariance(di_a, di_b):
    times = []
    for x in (di_a, di_b):
        f, est = PR.features_known_di(delayed(x, 0.300, 0.5, repeats=3), x)
        assert f["ir_echo_prom_db"] >= 10.0
        times.append(est["delay"]["time_s"])
        assert est["delay"]["feedback_db"] == pytest.approx(20 * np.log10(0.5), abs=1.5)
    assert times[0] == pytest.approx(0.300, rel=0.01)
    assert times[1] == pytest.approx(times[0], rel=0.005)


def test_no_echo_on_a_linear_filter(di_a):
    sos = signal.butter(2, 3000, fs=SR, output="sos")
    f, est = PR.features_known_di(signal.sosfilt(sos, di_a), di_a)
    assert not np.isfinite(est["delay"]["time_s"]) or f["ir_echo_prom_db"] < 10.0
    assert f["lin_resid_db"] < -25.0           # the linear part explains a filter
    assert f["mag_hf_db"] < -6.0               # and sees its high roll-off


def test_reverb_rt60_known_di_and_note_invariance(di_a, di_b):
    rts = []
    for x in (di_a, di_b):
        y = synthetic_reverb(x, [1.5] * 7, seed=3, wet_gain=0.5)
        f, est = PR.features_known_di(y, x)
        rts.append(est["reverb"]["rt60_s"])
        assert f["ir_late_db"] > -15.0
    assert rts[0] == pytest.approx(1.5, rel=0.25)
    assert rts[1] == pytest.approx(rts[0], rel=0.2)


def test_tremolo_rate_and_subtype(di_a, di_b):
    for x in (di_a, di_b):
        t = np.arange(x.size) / SR
        y = x * (1.0 + 0.6 * np.sin(2 * np.pi * 4.0 * t))
        f, est = PR.features_known_di(y, x)
        assert est["mod"]["subtype"] == "tremolo"
        assert est["mod"]["rate_hz"] == pytest.approx(4.0, rel=0.03)
        assert f["gain_period_conf"] > 0.5      # the per-frame gain swings periodically
        fd, _ = PR.features_known_di(np.tanh(12 * x), x)
        assert f["nl_resid_db"] < fd["nl_resid_db"] - 2.0   # a per-frame gain explains it better than a drive


def test_vibrato_and_chorus(di_a, di_b):
    for x in (di_a, di_b):
        f, est = PR.features_known_di(mod_delay(x, 8.0, 2.0, 0.8), x)      # fully wet: vibrato
        assert est["mod"]["pitch_depth_cents"] >= 8.0
        assert est["mod"]["subtype"] == "vibrato"
        # with a dry path the delay tracker loses the pitch (documented), but the notch sweep shows
        f, est = PR.features_known_di(x + mod_delay(x, 8.0, 2.0, 0.8), x)
        assert f["mod_sweep_depth_db"] >= 6.0
        assert est["mod"]["subtype"] != "tremolo"


def test_drive_versus_linear(di_a, di_b):
    for x in (di_a, di_b):
        fd, ed = PR.features_known_di(np.tanh(12 * x), x)
        fl, _ = PR.features_known_di(signal.lfilter([1.0, -0.6], [1.0], x), x)
        assert fd["nl_resid_db"] > -20.0
        assert fl["nl_resid_db"] < -25.0
        assert fd["d_flat_db"] > fl["d_flat_db"]
        assert ed["gain"]["label"] in ("crunch", "high")


def compress(x: np.ndarray, thr_db: float = -30.0, ratio: float = 4.0) -> np.ndarray:
    env = np.sqrt(signal.lfilter([0.002], [1, -0.998], x ** 2) + 1e-12)
    lvl = 20 * np.log10(env)
    gain_db = np.where(lvl > thr_db, (thr_db - lvl) * (1 - 1 / ratio), 0.0)
    return x * 10 ** (gain_db / 20)


def test_compressor_level_slope(di_a, di_b):
    for x in (di_a, di_b):
        fc, _ = PR.features_known_di(compress(x), x)
        fl, _ = PR.features_known_di(0.5 * x, x)
        assert fc["level_slope"] < 0.8
        assert fl["level_slope"] == pytest.approx(1.0, abs=0.05)


def test_no_di_strong_echo(di_a):
    f, est = PR.features_no_di(delayed(di_a, 0.25, 0.7, repeats=2))
    f0, _ = PR.features_no_di(di_a)
    assert est["delay"]["time_s"] == pytest.approx(0.25, rel=0.02)
    assert f["wac_hf_prom_db"] > f0["wac_hf_prom_db"]


def test_features_are_level_invariant(di_a):
    y = delayed(di_a, 0.2, 0.4)
    f1, _ = PR.features_known_di(y, di_a)
    f2, _ = PR.features_known_di(0.1 * y, 3.0 * di_a)
    for k in ("lin_resid_db", "ir_late_db", "mag_tilt_db_oct", "level_slope"):
        assert f1[k] == pytest.approx(f2[k], abs=1e-6)
    n1, _ = PR.features_no_di(y)
    n2, _ = PR.features_no_di(0.05 * y)
    for k in ("ltas_tilt_db_oct", "wac_prom_db", "crest_db"):
        assert n1[k] == pytest.approx(n2[k], abs=1e-6)


def test_mod_subtype_rules():
    assert PR.mod_subtype({"amp_depth_db": 6, "sweep_depth_db": 7, "pitch_depth_cents": 1, "mean_delay_ms": 0}, lin_resid_db=-20)[0] == "tremolo"
    assert PR.mod_subtype({"amp_depth_db": 1, "sweep_depth_db": 9, "pitch_depth_cents": 12, "mean_delay_ms": 8}, lin_resid_db=-5)[0] == "chorus"
    assert PR.mod_subtype({"amp_depth_db": 1, "sweep_depth_db": 3, "pitch_depth_cents": 20, "mean_delay_ms": 6}, lin_resid_db=-1)[0] == "vibrato"
    assert PR.mod_subtype({"amp_depth_db": 1, "sweep_depth_db": 15, "pitch_depth_cents": 2, "mean_delay_ms": 2}, lin_resid_db=-15)[0] == "flanger"
    assert PR.mod_subtype({"amp_depth_db": 1, "sweep_depth_db": 12, "pitch_depth_cents": 0.2, "mean_delay_ms": 24.8}, lin_resid_db=-15)[0] == "phaser"
    assert PR.mod_subtype({"amp_depth_db": 0.5, "sweep_depth_db": 1, "pitch_depth_cents": 0.5, "mean_delay_ms": 0}, lin_resid_db=-40)[0] is None


def test_gain_label():
    assert PR.gain_label(-40.0) == "clean"
    assert PR.gain_label(-15.0) == "crunch"
    assert PR.gain_label(-3.0) == "high"
    assert PR.gain_label(float("nan")) is None


def test_fit_predict_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    rows, labels = [], []
    for i in range(300):
        cat = ["delay", "reverb", None][i % 3]
        f = {k: float(rng.normal()) for k in PR.KNOWN_DI_FEATURES}
        if cat == "delay":
            f["ir_echo_prom_db"] += 5.0
        if cat == "reverb":
            f["ir_late_db"] += 5.0
        rows.append(f)
        labels.append({cat} if cat else set())
    m = PR.fit_model(rows, labels, "known_di", categories=("delay", "reverb"))
    p = tmp_path / "m.json"
    m.save(p)
    m2 = PR.load_model("known_di", p)
    probe = {k: 0.0 for k in PR.KNOWN_DI_FEATURES} | {"ir_echo_prom_db": 5.0}
    pr = m2.predict(probe)
    assert set(pr) == {"delay", "reverb"}
    assert pr["delay"] > 0.9
    assert pr["reverb"] < 0.1
    assert m2.predict({})["delay"] < 0.5        # missing features sit at the median


def test_detect_without_model(di_a):
    rep = PR.detect(delayed(di_a, 0.3, 0.5), di_a, model=None)
    assert rep.mode == "known_di"
    assert set(rep.categories) == set(PR.CATEGORIES)
    assert all(c.probability is None for c in rep.categories.values())
    assert rep.probabilities() == {}
    assert rep.categories["delay"].estimate["time_s"] == pytest.approx(0.3, rel=0.01)
    rep2 = PR.detect(di_a, model=None)
    assert rep2.mode == "no_di"
    assert "delay" in rep2.to_dict()["categories"]


def test_energy_response_late_energy(di_a, di_b):
    for x in (di_a, di_b):
        rev = PR.energy_response(PR._unit(synthetic_reverb(x, [1.5] * 7, seed=4, wet_gain=0.5)), PR._unit(x))
        eq = PR.energy_response(PR._unit(signal.lfilter([1.0, -0.6], [1.0], x)), PR._unit(x))
        dl = PR.energy_response(PR._unit(delayed(x, 0.3, 0.5, 2)), PR._unit(x))
        assert rev["e_late_db"] > eq["e_late_db"] + 10.0
        assert dl["e_late_db"] > eq["e_late_db"] + 10.0
        k = round(dl["e_echo_s"] / 0.3)              # the energy peak may be the second echo (documented)
        assert k in (1, 2)
        assert dl["e_echo_s"] == pytest.approx(0.3 * k, abs=0.006)
