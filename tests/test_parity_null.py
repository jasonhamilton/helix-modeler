"""parity_null.py offline: DSP (alignment, level match, bands) and the whole flow with fake engines."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("parity_null", ROOT / "scripts" / "parity_null.py")
PN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PN)

SR = PN.SR


def sig(n=SR * 3, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    x = np.convolve(x, np.ones(8) / 8, mode="same")
    x[-SR // 2:] = 0
    return x


def frac_delay(x, d):
    return PN.shift_fractional(x, -d)


def test_lag_fractional_and_gain_recovered():
    n = sig()
    s = 0.37 * frac_delay(n, 5.4)
    lag, corr = PN.estimate_lag(n, s)
    assert abs(lag - 5.4) < 0.15 and corr > 0.95
    r = PN.analyse(s, n)
    assert abs(r["lag_samples"] - 5.4) < 0.01   # refined
    assert r["null_residual_db"] < -40
    assert r["gain_db"] == pytest.approx(20 * np.log10(1 / 0.37), abs=0.1)


def test_identical_signals_null_deep_and_filter_shows_in_bands():
    n = sig(SR * 5)
    assert PN.analyse(n, n)["null_residual_db"] < -100
    spec_ = np.fft.rfft(n)
    f = np.fft.rfftfreq(len(n), 1 / SR)
    spec_[f > 4000] *= 0.25
    lp = np.fft.irfft(spec_, len(n))
    r = PN.analyse(lp, n)
    hi = next(b for b in r["bands"] if b["centre_hz"] >= 8000)
    lo = next(b for b in r["bands"] if b["centre_hz"] == 500.0)
    assert hi["stomp_minus_native_db"] < lo["stomp_minus_native_db"] - 6
    assert r["spectral_tilt_db_per_octave"] < 0
    assert r["tail_null_residual_db"] is not None


def test_band_energies_obey_parseval():
    n = sig(SR * 4, seed=3)
    be = PN.band_energies(n)
    assert be.sum() == pytest.approx(float(np.sum(n ** 2)), rel=1e-6)
    r = PN.analyse(n * 0.5, n)
    assert max(b["native_db"] for b in r["bands"]) <= 0.01      # bands are re total energy, never above it
    assert sum(10 ** (b["native_db"] / 10) for b in r["bands"]) == pytest.approx(1.0, rel=1e-3)
    assert r["spectral_tilt_db_per_octave"] is not None


def test_silent_render_rejected():
    with pytest.raises(ValueError):
        PN.analyse(np.zeros(SR), sig(SR))


class FakeEngine:
    def __init__(self, kind, gain, delay, log):
        self.kind, self.gain, self.delay, self.log = kind, gain, delay, log
        self.original = {"setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"}
        self.cal = SimpleNamespace(level_gain_db=1.5)
        self.backup_path = Path("x.bin")
        self.restore_report = {"restored": True}
        self.preset = None

    def capabilities(self):
        return SimpleNamespace(firmware_version="3.50" if self.kind == "stomp" else "3.82", extras={})

    def load(self, preset):
        self.preset = preset
        self.log.append((self.kind, preset.name))

    def render(self, x):
        # each fake "preset" is a drive amount so presets differ; stomp = gain + delay on top
        drive = 1 + len(self.preset.name) % 3
        y = np.tanh(drive * x)
        return (self.gain * frac_delay(y, self.delay)).astype(np.float32)

    def close(self):
        self.log.append((self.kind, "close"))


def test_run_with_fake_engines(tmp_path, monkeypatch):
    import soundfile as sf

    di = tmp_path / "di.wav"
    x = (0.2 * sig(SR * 4)).astype(np.float32)
    sf.write(str(di), x, SR, subtype="FLOAT")
    log = []
    rows = []
    monkeypatch.setattr(PN, "ledger", lambda args, enabled: rows.append(args))
    presets = {n: PN.INJECT / f"{n}.hlx" for n in ("K", "A")}
    if not all(p.exists() for p in presets.values()):
        pytest.skip("cache/stomp/inject variants not generated")
    s = PN.run(presets, di, tmp_path / "out", stomp_factory=lambda: FakeEngine("stomp", 0.8, -3.3, log),
               native_factory=lambda name: FakeEngine("native", 1.0, 0.0, log),
               verify_restore=lambda eng: {"after": {"name": "Smoke Known"}, "bytes_equal_backup": True},
               use_ledger=False)
    assert not s["errors"], s["errors"]
    assert [e for e in log if e[1] == "close"] == [("stomp", "close")] + [("native", "close")] * 2  # stomp released first
    for n in presets:
        r = s["results"][n]
        assert r["null_residual_db"] < -40 and abs(r["lag_samples"] + 3.3) < 0.1
        assert r["gain_db"] == pytest.approx(20 * np.log10(1 / 0.8), abs=0.1)
        st, _ = sf.read(str(tmp_path / "out" / f"{n}_stereo.wav"))
        assert st.shape[1] == 2 and (tmp_path / "out" / f"{n}_residual.wav").exists()
        assert json.loads((tmp_path / "out" / f"{n}.json").read_text())["files"]["stereo"].endswith("stereo.wav")
    metrics = [r[r.index("--metric") + 1] for r in rows]
    assert metrics.count("null_residual_db") == 2 and "restore_bytes_equal" in metrics
    assert all(r[r.index("--kind") + 1] == "parity" and r[r.index("--dataset") + 1] == "parity-v0" for r in rows)
    # resume: reuse raw renders, no engine constructed
    def boom(*_a):
        raise AssertionError("engine must not be opened")
    s2 = PN.run(presets, di, tmp_path / "out", engines=(), reuse=True, stomp_factory=boom, native_factory=boom,
                use_ledger=False)
    assert s2["results"]["K"]["null_residual_db"] == pytest.approx(s["results"]["K"]["null_residual_db"])


def test_wrong_original_preset_stops_before_any_load(tmp_path):
    import soundfile as sf

    di = tmp_path / "di.wav"
    sf.write(str(di), (0.1 * sig(SR)).astype(np.float32), SR, subtype="FLOAT")
    log = []

    def fac():
        e = FakeEngine("stomp", 1, 0, log)
        e.original = {"name": "Something Else"}
        return e
    p = PN.INJECT / "K.hlx"
    if not p.exists():
        pytest.skip("variants not generated")
    s = PN.run({"K": p}, di, tmp_path / "o", engines=("stomp",), stomp_factory=fac, use_ledger=False,
               verify_restore=lambda e: {})
    assert any("Smoke Known" in e for e in s["errors"])
    assert not any(k == "stomp" and v == "K" for k, v in log) and ("stomp", "close") in log


def test_help_does_not_touch_anything():
    import subprocess
    import sys

    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "parity_null.py"), "--help"], capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 0 and "LEFT = HX Stomp" in r.stdout
