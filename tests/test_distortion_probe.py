"""Tests for the T173 dfp-v1 pilot probe and fingerprint (research code, nothing adopted).
A cheap Wiener-Hammerstein system (shelf pre-filter, tanh, low-pass post-filter, no oversampling)
stands in for a drive; the full probe takes about 6 s to analyse, so it is built once."""
import numpy as np
import pytest
from scipy import signal

from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_synth as S

SR = P.SR


def _wh(asym=0.0, drive=30.0, post_fc=2000.0):
    w = 2 * np.pi * 720.0
    pre = S.analog_sos([drive / w, 1.0], [1 / w, 1.0])          # shelf: x1 below 720 Hz, x drive above
    post = S.analog_sos(*S.lp1(post_fc))

    def proc(x):
        u = signal.sosfilt(pre, x)
        return 0.3 * signal.sosfilt(post, np.tanh(20 * u + asym) - np.tanh(asym))
    return proc


@pytest.fixture(scope="module")
def probe():
    return P.build_probe("main")


@pytest.fixture(scope="module")
def fp_sym(probe):
    sig, lay = probe
    return P.analyse(_wh()(sig), lay)


def test_probe_layout_levels_and_length(probe):
    sig, lay = probe
    sweeps = [s for s in lay["segments"] if s["name"] == "sweep"]
    assert [s["level_db"] for s in sweeps] == P.SWEEP_LEVELS
    assert P.SWEEP_LEVELS[0] == -78.0 and np.all(np.diff(P.SWEEP_LEVELS[1:]) == 6.0)
    assert -6.0 in P.STAIR_LEVELS and P.STAIR_LEVELS[0] <= -70.0
    assert 100.0 < sig.size / SR < 120.0
    assert np.max(np.abs(sig)) <= 1.0 + 1e-9
    short, lay_s = P.build_probe("short")
    assert lay_s["sweep_L"] < lay["sweep_L"] and short.size / SR < 40.0


def test_fingerprint_recovers_pre_filter_corner_and_symmetry(fp_sym):
    arr, sc = fp_sym
    assert 600.0 < sc["P_corner_hz"] < 900.0                    # the 720 Hz pre-clip bass cut
    assert sc["P_100_db"] < -10.0 and abs(sc["Q_100_db"]) < 1.0   # cut before the clipper, not after
    assert 1500.0 < sc["Q_corner_hz"] < 2600.0                  # the 2 kHz post low-pass
    assert sc["even_share_db"] < -60.0                          # symmetric clipper
    assert sc["knee_1k_dbfs"] is not None and not sc["knee_at_floor"]
    assert sc["odd_harmonic_residual_db"] < 0.3 and not sc["memory_flag"]
    assert abs(sc["g0_stair_1k_db"] - sc["g0_ss_1k_db"]) < 0.5


def test_fingerprint_sees_asymmetry_and_clean_gain(probe):
    sig, lay = probe
    _, sc = P.analyse(_wh(asym=0.5)(sig), lay)
    assert sc["even_share_db"] > -25.0
    _, sc = P.analyse(0.5 * sig, lay)                           # a clean -6 dB gain
    assert sc["knee_1k_dbfs"] is None
    assert abs(sc["g0_stair_1k_db"] + 6.02) < 0.01
    assert sc["compression_at_minus6_db"] < 0.01 and sc["thd_1k_at_minus6_db"] < -100.0
    assert sc["imd_440_554_at_minus12_db"] < -100.0


def test_pack_round_trip_is_small_and_has_no_audio(fp_sym):
    arr, sc = fp_sym
    b = P.pack(arr, sc)
    assert len(b) < 8000
    arr2, sc2 = P.unpack(b)
    assert set(arr2) == set(arr)
    assert max(v.size for v in arr2.values()) < 1000              # no array anywhere near audio length
    assert np.nanmax(np.abs(arr2["M1"] - arr["M1"])) < 0.1        # float16 keeps 0.1 dB
    assert sc2["knee_1k_dbfs"] == pytest.approx(sc["knee_1k_dbfs"], abs=1e-4)


def test_two_tone_imd_of_a_cubic():
    t = np.arange(int(0.5 * SR)) / SR
    a = 0.1
    x = a * np.cos(2 * np.pi * 440 * t) + a * np.cos(2 * np.pi * 554 * t)
    y = x + 0.5 * x ** 3
    r = P.two_tone_imd(y, 440.0, 554.0)
    # 2 f1 - f2 and 2 f2 - f1 each at 3/4 * 0.5 * a^3 relative to tones ~a
    want = 10 * np.log10(2 * (0.75 * 0.5 * a ** 3) ** 2 / (2 * a ** 2))
    assert abs(r["imd3_db"] - want) < 0.3
    assert r["imd2_db"] < -100.0


def test_corner_and_scoop_helpers():
    f = P.FGRID
    lp = 20 * np.log10(np.abs(1 / (1 + 1j * f / 1000.0)))
    assert abs(P.corner_above(f, lp) - 1000.0) < 60.0
    hp = 20 * np.log10(np.abs((1j * f / 500.0) / (1 + 1j * f / 500.0)))
    assert abs(P.corner_below(f, hp) - 500.0) < 40.0
    notch = -6.0 * np.exp(-np.log(f / 1100.0) ** 2 / 0.3)
    s = P.scoop(f, notch)
    assert abs(s["f_hz"] / 1100.0 - 1) < 0.15 and s["depth_db"] > 5.0


def test_leakage_floor_and_a_long_memory(probe):
    sig, lay = probe
    L_short = P.build_probe("short")[1]["sweep_L"]
    r0 = P.leakage(sig, lay, -12.0, L_short, orders=3)              # identity: the method's floor
    assert r0["contamination_p95_db"][1] < -40.0
    last = [s for s in lay["segments"] if s["name"] == "sweep"][-1]
    n = last["start"] + last["n"]
    w = 2 * np.pi * 720.0
    pre = S.analog_sos([30.0 / w, 1.0], [1 / w, 1.0])
    post = S.analog_sos(*S.lp1(2000.0))

    def clip(x):                                                   # oversampled: no aliasing
        u = signal.sosfilt(pre, x[:n])
        v = S.os_apply(u, lambda q: np.tanh(20 * q + 0.3) - np.tanh(0.3))
        return np.concatenate([0.3 * signal.sosfilt(post, v), np.zeros(x.size - n)])
    y = clip(sig)
    r1 = P.leakage(y, lay, -12.0, L_short, orders=3)
    assert r1["rel_db"][3] > -20.0 and r1["contamination_p95_db"][1] < -30.0
    d = int(0.05 * SR)                                             # a 50 ms echo: memory past gap/2
    r2 = P.leakage(y + 0.3 * np.concatenate([np.zeros(d), y[:-d]]), lay, -12.0, L_short, orders=3)
    assert r2["contamination_max_db"][2] > r1["contamination_max_db"][2] + 10.0   # order 3 rings into 2


def test_first_order_fits_and_sweep_referenced_knee():
    f = P.FGRID
    lp = 3.0 - 10 * np.log10(1 + (f / 840.0) ** 2)
    fc, rms, n = P.fit_first_order(f, lp, "lp")
    assert abs(fc / 840.0 - 1) < 0.03 and rms < 0.05 and n == f.size
    u = f / 720.0
    hp = 20 * np.log10(u / np.sqrt(1 + u * u))
    hp[f < 250] = np.nan                                           # only part of the band identifiable
    assert abs(P.fit_first_order(f, hp, "hp")[0] / 720.0 - 1) < 0.03
    L = np.array([-71.0, -66.0, -61.0])
    assert P.knee_ref(L, L + 40.0 - np.array([0.0, 0.5, 1.5]), 40.0) == pytest.approx(-63.5)
    assert P.knee_ref(L, L + 38.0, 40.0) == -np.inf
    assert P.knee_ref(L, L + 40.0, 40.0) is None
