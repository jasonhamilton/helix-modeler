"""Tests for the T171 distortion identification estimators, synthetic systems and step-0 mining.
Fast: no sweeps through the 8x-oversampled systems except one short bias-drift burst."""
import numpy as np
import pytest

from tonematch.metrics import distortion_id as D
from tonematch.metrics import distortion_id_eval as E
from tonematch.metrics import distortion_synth as S
from tonematch.metrics import drive_fp_mining as F

SR = 48000


def test_ess_timing_rule_and_cubic_harmonic():
    x, L, _ = D.ess_sync(20.0, 20000.0, 2.0, SR)
    assert abs(20.0 * L - round(20.0 * L)) < 1e-9          # synchronized: f1 L integer
    A, a3 = 0.5, 0.2
    y = np.concatenate([A * x + a3 * (A * x) ** 3, np.zeros(SR // 2)])
    r = D.ess_harmonics(y, x, L, SR, 20.0, 20000.0, orders=3)
    for k in (1, 3):
        assert abs(r["peak_offset_s"][k]) * SR <= 1.0      # peak at -L ln k within a sample
    # x^3 with x = A sin: third harmonic amplitude a3 A^3 / 4
    m3 = D.band_mag_db(r["f"], r["H"][3], [3000.0])[0]
    assert abs(m3 - D.db(a3 * A ** 3 / 4)) < 0.1
    m1 = D.band_mag_db(r["f"], r["H"][1], [1000.0])[0]
    assert abs(m1 - D.db(A + 0.75 * a3 * A ** 3)) < 0.1


def test_sine_harmonics_drops_aliasing_orders():
    t = np.arange(9600) / SR
    y = 0.3 * np.cos(2 * np.pi * 8000 * t) + 0.05 * np.cos(2 * np.pi * 16000 * t)
    a = D.sine_harmonics(y, 8000.0, SR, 5)
    assert abs(abs(a[1]) - 0.3) < 1e-6 and abs(abs(a[2]) - 0.05) < 1e-6
    assert a[3] == 0 and a[5] == 0


def test_chebyshev_reconstruction_of_a_static_curve():
    A = 0.5
    c = D.chebyshev_truth(lambda u: np.tanh(4 * u) + 0.1 * u ** 2, A, 21)
    xs = np.linspace(-A, A, 501)
    assert np.max(np.abs(D.chebyshev_curve(c, A, xs) - (np.tanh(4 * xs) + 0.1 * xs ** 2))) < 1e-6
    sym = D.symmetry_stats(c, A)
    assert sym["even_share"] > 1e-4 and sym["dc_re_fund"] > 0


def test_chebyshev_coeffs_removes_post_filter_and_phase():
    A, f0 = 0.4, 1000.0
    ct = D.chebyshev_truth(np.tanh, A, 9)
    Q = lambda f: 1.0 / (1 + 1j * np.asarray(f, float) / 3000.0)
    phi = 0.7
    k = np.arange(1, 10)
    a = np.zeros(10, complex)
    a[1:] = ct[1:] * Q(k * f0) * np.exp(1j * k * phi)
    c = D.chebyshev_coeffs(a, f0, post=Q, drop_below_db=-200)
    assert np.allclose(c[1:], ct[1:], atol=1e-9)


def test_p1db_and_rapp():
    L = np.arange(-60, 1, 1.0)
    g = np.where(L < -20, 0.0, -(L + 20) * 0.2)
    assert abs(D.p1db_from_levels(L, L + g) - (-15.0)) < 1e-6
    x = np.linspace(1e-3, 1, 400)
    _, v, p, rms = D.fit_rapp(x, D.rapp(x, 3.0, 0.5, 4.0))
    assert abs(p - 4.0) < 0.05 and abs(v - 0.5) < 0.01 and rms < 1e-3


def _wh_table(system, fgrid, levels):
    return E.truth_table(system, fgrid, levels, orders=(1, 2, 3, 4, 5))


def test_level_shift_split_recovers_pre_and_post():
    sym = S.diode_curve(1, 1, 2200.0)
    s = S.wh_system("t", "t", S.cascade(S.hp1(300.0), S.gain_tf(20.0)), sym, 1.0,
                    S.lp1(2000.0), 1.0)
    fgrid = 1000.0 * 2.0 ** (np.arange(-9, 10) / 3.0)
    levels = list(np.arange(-60.0, 1.0, 5.0))
    M = _wh_table(s, fgrid, levels)
    sp = D.level_shift_split(levels, M, fgrid)
    e = E.split_errors(s, sp)
    assert e["P"]["coverage"] > 0.8
    assert e["P"]["err_identifiable"]["max"] < 0.5
    assert e["Q"]["err_identifiable"]["max"] < 0.5
    assert sp["residual_rms_db"] < 0.1


def test_split_residual_flags_parallel_clean_path():
    """A clean path in parallel with the clipper is not W-H: the fundamental curves no longer
    line up by a shift, so the residual grows."""
    hard = S.diode_curve(1, 1, 1000.0)
    s = S.wh_system("t", "t", S.cascade(S.hp1(400.0), S.gain_tf(1.0)), lambda u: hard(20 * u),
                    1.0, S.lp1(3000.0), 1.0)
    fgrid = 1000.0 * 2.0 ** (np.arange(-9, 10) / 3.0)
    levels = list(np.arange(-60.0, 1.0, 5.0))
    M = _wh_table(s, fgrid, levels)
    wh = D.level_shift_split(levels, M, fgrid)["residual_rms_db"]
    # add the clean path (in phase, a simplification): |0.5 A + c1(A P) Q|
    M2 = {k: v.copy() for k, v in M.items()}
    Pm = np.abs(s.P(fgrid))
    for i, f in enumerate(fgrid):
        for j, lv in enumerate(levels):
            A = 10 ** (lv / 20)
            c1 = D.chebyshev_truth(s.f, A * Pm[i], 1, 2048)[1]
            M2[1][i, j] = D.db(abs(0.5 * A + c1 * s.Q([f])[0]))
    blend = D.level_shift_split(levels, M2, fgrid)["residual_rms_db"]
    assert blend > 5 * max(wh, 0.02)


def test_settle_flags_bias_drift_only():
    t = np.arange(int(1.5 * SR)) / SR
    env = np.where((t >= 0.5) & (t < 1.0), 10 ** (-6 / 20), 10 ** (-30 / 20))
    burst = env * np.sin(2 * np.pi * 220.0 * t)
    sysd = {s.name: s for s in S.build_systems() if s.name in ("bias_drift", "tanh")}
    st_d = D.settle_after_burst(sysd["bias_drift"].process(burst), SR, 220.0, 1.0 + 1 / 220, 0.49)
    st_t = D.settle_after_burst(sysd["tanh"].process(burst), SR, 220.0, 1.0 + 1 / 220, 0.49)
    assert st_d["memory_flag"] and not st_t["memory_flag"]


def test_min_phase_matches_first_order_lowpass():
    """Magnitude known to 16 kHz only (as from the split); phase within 0.05 rad below 3 kHz."""
    sos = S.analog_sos(*S.lp1(1000.0))
    f = np.geomspace(40, 16000, 60)
    F_ = D.min_phase_from_db(f, D.db(S.sos_resp(sos, f)), SR, nfft=1 << 16)
    q = np.array([300.0, 1000.0, 3000.0])
    assert np.max(np.abs(np.angle(F_(q)) - np.angle(S.sos_resp(sos, q)))) < 0.05


# ---------------------------------------------------------------------------- step 0 helpers

def test_ari_basics():
    a = [0, 0, 1, 1, 2, 2]
    assert F.ari(a, [5, 5, 7, 7, 9, 9]) == pytest.approx(1.0)
    assert F.ari(a, [0, 1, 0, 1, 0, 1]) < 0.1
    assert F.ari(a, [0, 0, 1, 1, 1, 1]) == pytest.approx(F.ari([0, 0, 1, 1, 1, 1], a))


def test_loso_identification_toy():
    recs = [{"model": m, "setting": s} for m in ("a", "b", "c") for s in ("x", "y")]
    Z = np.array([[0, 0], [0.1, 0], [5, 5], [5.1, 5], [-5, 5], [-5.1, 5]], float)
    ranks, _, models = F.loso_identification(recs, F.pairwise(Z))
    assert list(ranks) == [1] * 6 and models == ["a", "b", "c"]


def test_labels_cover_the_pool_and_t164():
    assert len(F.LABELS) == 60
    groups = {g for g, _, _ in F.LABELS.values() if not g.startswith("O:")}
    assert len(groups) == 17
    assert all(m in F.LABELS for m in F.T164_TOP)
