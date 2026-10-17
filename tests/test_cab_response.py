"""Tests for tonematch.metrics.cab_response and tonematch.ir_library (synthetic signals only, no IR files)."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import butter, lfilter, sosfilt

from tonematch.ir_library import parse_ir_name
from tonematch.metrics import cab_response as cr

SR = cr.SAMPLE_RATE
G = cr.FINGERPRINT_GRID


def cab_like_ir(res_hz=110.0, hc_hz=5000.0, n=2048, seed=0):
    """A cab-shaped test IR: high-pass at 70 Hz, a low resonance, a steep 4th-order low-pass."""
    x = np.zeros(n)
    x[0] = 1.0
    x = sosfilt(butter(2, 70, "highpass", fs=SR, output="sos"), x)
    w0 = 2 * np.pi * res_hz / SR
    q, a_lin = 2.0, 10 ** (8 / 40)  # +8 dB peaking EQ (RBJ)
    alpha = np.sin(w0) / (2 * q)
    b = [1 + alpha * a_lin, -2 * np.cos(w0), 1 - alpha * a_lin]
    a = [1 + alpha / a_lin, -2 * np.cos(w0), 1 - alpha / a_lin]
    x = lfilter(b, a, x)
    x = sosfilt(butter(4, hc_hz, "lowpass", fs=SR, output="sos"), x)
    rng = np.random.default_rng(seed)
    x = x + 0.002 * rng.standard_normal(n) * np.exp(-np.arange(n) / 200)  # a little "room"
    return x


def notes(f0s, seconds=4.0, seed=0):
    """Plucked harmonic notes (note content for the invariance tests)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    y = np.zeros_like(t)
    seg = t.size // len(f0s)
    for i, f0 in enumerate(f0s):
        tt = t[: seg]
        env = np.exp(-tt * 3)
        s = sum((0.7 ** k) * np.sin(2 * np.pi * f0 * (k + 1) * tt + rng.uniform(0, 6.28)) for k in range(40)
                if f0 * (k + 1) < SR / 2)
        y[i * seg:(i + 1) * seg] = env * s
    return y + 1e-3 * rng.standard_normal(t.size)  # pick and string noise


def test_impulse_is_flat():
    x = np.zeros(2048)
    x[0] = 1.0
    m = cr.ir_response(x)
    assert np.allclose(m, 0.0, atol=1e-6)
    assert np.allclose(cr.band_levels(x), 0.0, atol=1e-6)


def test_features_find_resonance_and_rolloff():
    f = cr.response_features(G, cr.ir_response(cab_like_ir(res_hz=110, hc_hz=5000)))
    assert 90 <= f["resonance_hz"] <= 135
    assert f["resonance_db"] > 3
    assert 3500 <= f["high_rolloff_hz"] <= 9000
    assert f["low_rolloff_hz"] < 90
    g = cr.response_features(G, cr.ir_response(cab_like_ir(res_hz=110, hc_hz=2500)))
    assert g["high_rolloff_hz"] < f["high_rolloff_hz"]
    assert g["brightness_db"] < f["brightness_db"]
    assert g["centroid_hz"] < f["centroid_hz"]


def test_distance_gain_and_tilt_invariance():
    a = cr.ir_response(cab_like_ir())
    assert cr.response_distance(G, a, a) == pytest.approx(0.0, abs=1e-9)
    assert cr.response_distance(G, a, a + 7.0) == pytest.approx(0.0, abs=1e-9)
    tilt = 1.5 * np.log2(G / 1000)
    assert cr.response_distance(G, a, a + tilt) > 1.0
    assert cr.response_distance(G, a, a + tilt, detrend=1) == pytest.approx(0.0, abs=1e-9)
    bank = np.stack([a + 3, cr.ir_response(cab_like_ir(hc_hz=2500)), cr.ir_response(cab_like_ir(res_hz=200))])
    idx, d = cr.nearest(G, a, bank, k=2)
    assert idx[0] == 0 and d[0] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("seed", [0, 1])
def test_transfer_response_recovers_ir_and_ignores_notes(seed):
    ir = cab_like_ir(seed=seed)
    truth = cr.normalise(G, cr.ir_response(ir))
    a = notes([82.4, 110, 146.8, 196, 246.9, 329.6], seed=seed)  # low open-string register
    b = notes([392, 440, 523.3, 659.3, 784], seed=seed + 10)       # high register, other notes
    est = []
    for dry in (a, b):
        wet = np.convolve(dry, ir)[: dry.size]
        wet = np.concatenate([np.zeros(16), wet[:-16]])  # a 16-sample latency, as on Native
        mag, coh = cr.transfer_response(dry, wet)
        sel = (G >= 100) & (G <= 8000)
        assert np.median(coh[sel]) > 0.9
        est.append(cr.normalise(G, mag))
    for e in est:
        assert cr.response_distance(G, e, truth, (100, 8000)) < 1.0
    # note-content invariance: low and high register give the same response
    assert cr.response_distance(G, est[0], est[1], (150, 8000)) < 1.0


def test_transfer_response_is_level_invariant():
    """Quiet renders (power density ~1e-12) must give the same response and coherence as loud ones."""
    ir = cab_like_ir()
    dry = notes([110, 220, 330], seed=5)
    wet = np.convolve(dry, ir)[: dry.size]
    m1, c1 = cr.transfer_response(dry, wet)
    m2, c2 = cr.transfer_response(dry * 1e-3, wet * 1e-3)
    sel = (G >= 100) & (G <= 8000)
    assert np.allclose(m1[sel], m2[sel], atol=1e-6)
    assert np.allclose(c1[sel], c2[sel], atol=1e-6)
    assert np.median(c2[sel]) > 0.9


def test_ltas_ratio_with_same_source_statistics():
    rng = np.random.default_rng(3)
    ir = cab_like_ir()
    truth = cr.normalise(G, cr.ir_response(ir))
    src1, src2 = rng.standard_normal(4 * SR), rng.standard_normal(4 * SR)
    est = cr.normalise(G, cr.ltas_ratio_response(np.convolve(src1, ir)[: src1.size], src2))
    assert cr.response_distance(G, est, truth, (100, 8000)) < 0.7


def test_helix_length_and_load(tmp_path):
    import soundfile as sf

    ir = cab_like_ir(n=4000)
    y = cr.helix_length(ir, 2048)
    assert y.size == 2048 and abs(y[-1]) < 1e-12
    assert cr.helix_length(ir[:100], 2048).size == 2048
    stereo = np.stack([ir, 0.5 * ir], axis=1)
    p = tmp_path / "t.wav"
    sf.write(str(p), stereo[:, :] * 0.5, 44100, subtype="PCM_24")
    x, info = cr.load_ir(p)
    assert info["file_sr"] == 44100 and info["channels"] == 2
    assert info["right_differs_db"] == pytest.approx(20 * np.log10(0.5), abs=0.1)
    assert x.size == pytest.approx(4000 * 48000 / 44100, abs=2)
    # resampling keeps the response below 16 kHz (the left channel is used)
    a = cr.normalise(G, cr.ir_response(x, SR))
    b = cr.normalise(G, cr.ir_response(ir, 44100))
    assert cr.response_distance(G, a, b, (40, 15000)) < 0.3


def test_parse_names():
    p = parse_ir_name("Brandx/Brandx 4x12 Front/SM57/Brandx 4x12 SM57 3C.wav")
    assert p["group"] == "guitar" and p["brand"] == "Brandx" and p["cab"] == "Brandx 4x12 Front"
    assert p["cab_size"] == "4x12" and p["cab_size_source"] == "name"
    assert p["mics"] == ["SM57"] and (p["grid_row"], p["grid_col"]) == (3, "C")
    q = parse_ir_name("Brandy/Brandy 1960 G12M25/421/1960 G12M25 421 CapEdge45 0 5in.wav")
    assert q["mics"] == ["MD421"] and q["position"] == "cap-edge 45deg" and q["distance_in"] == 0.5
    assert q["speakers"] == ["G12M"] and q["cab_size_source"] == "inferred"
    r = parse_ir_name("##MISC##/Misc/Some 57121 AIR.wav")
    assert r["group"] == "misc" and r["multi_mic"] and set(r["mics"]) == {"SM57", "R121"}
    s = parse_ir_name("Brandz/Cab 412 V30/Cab V30 B.wav")
    assert s["cab_size"] == "4x12" and s["variant"] == "B" and s["speakers"] == ["V30"]
    assert parse_ir_name("##BASS##/X/X 8x10 4033 A.wav")["mics"] == ["AT4033"]
