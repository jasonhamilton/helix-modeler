"""T181 (distortion step 4): the twin fit, knob maps, feature masking, output-only features, the search interface.

Research code; these check the machinery on synthetic systems with known truth, no Native and no song audio."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.metrics import distortion_id as D
from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_step4 as S4
from tonematch.metrics import distortion_synth as SY

SR = 48000


def _di(seconds: float = 6.0, seed: int = 0) -> np.ndarray:
    """A synthetic guitar-like DI: plucked notes (harmonic series, decaying, random pitch and loudness,
    some overlapping), peak -9 dBFS. No recorded audio is needed."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = np.zeros(n)
    t0 = 0
    while t0 < n - SR // 4:
        f0 = 82.4 * 2 ** (rng.integers(0, 24) / 12)
        amp = 10 ** (rng.uniform(-30, 0) / 20)
        L = min(n - t0, int(rng.uniform(0.3, 1.2) * SR))
        tt = np.arange(L) / SR
        note = sum((1 / k) * np.sin(2 * np.pi * k * f0 * tt + rng.uniform(0, 2 * np.pi)) * np.exp(-tt * (1.5 + 0.8 * k))
                   for k in range(1, 16) if k * f0 < 0.4 * SR)
        x[t0:t0 + L] += amp * note * (1 - np.exp(-tt / 0.002))
        t0 += int(rng.uniform(0.1, 0.5) * SR)
    x += 1e-4 * rng.normal(size=n)
    return 10 ** (-9 / 20) * x / np.max(np.abs(x))


def _system(name: str):
    return next(s for s in SY.build_systems() if s.name == name)


def test_twin_recovers_a_tanh_clipper_and_its_knee():
    x = _di()
    s = _system("tanh")
    y = s.process(x)
    tw = S4.fit_twin(x, y)
    assert tw.fit_db < -30.0                      # a memoryless clipper: the twin explains it (the truth runs 8x
    #                                               oversampled, the twin at 48 kHz)
    assert tw.pre_fc is None                      # no pre-filter in the truth, and none is chosen
    # the twin's static curve against the truth curve, inside the range the DI covered
    u = np.linspace(-0.5, 0.5, 101) * tw.U           # inside the range the notes cover well
    c = tw.curve(u) - tw.curve(np.zeros(1))[0]      # the curve's constant is not identifiable (no DC in or out)
    err = np.max(np.abs(c - s.f(u))) / np.max(np.abs(s.f(u)))
    assert err < 0.05
    # knee from the describing function of both curves within 1 dB
    L = np.arange(-40.0, -6.0, 1.0)
    k_tw = D.p1db_from_levels(L, np.array([D.db(D.chebyshev_truth(tw.curve, 10 ** (v / 20), 1, 2048)[1]) for v in L]))
    k_tr = D.describing_p1db(s.f, L)
    assert k_tw is not None and k_tr is not None and abs(k_tw - k_tr) < 1.0


def test_twin_finds_a_pre_high_pass():
    x = _di()
    s = _system("ts_like")
    tw = S4.fit_twin(x, s.process(x))
    assert tw.pre_fc is not None and 400.0 < tw.pre_fc < 1300.0     # truth: first-order fit about 750 Hz
    assert tw.fit_db < -20.0


def test_twin_roundtrip_and_linear_inner_region():
    x = _di()
    tw = S4.fit_twin(x, _system("asym_diode").process(x))
    tw2 = S4.Twin.from_dict(tw.to_dict())
    z = np.linspace(-1, 1, 101) * tw.U * 1.5      # includes the straight continuation past the observed peak
    assert np.allclose(tw.curve(z), tw2.curve(z))
    tw2.s_lin = 2.0 ** -9
    small = np.linspace(-1, 1, 41) * tw.U * 2.0 ** -10
    c = tw2.curve(small) - tw2.curve(np.zeros(1))[0]
    quad = np.polyval(np.polyfit(small, c, 2), small)
    assert np.max(np.abs(c - quad)) < 1e-9 * np.max(np.abs(c)) + 1e-15   # exactly linear plus an even square term


def test_knob_map_interpolates_and_inverts():
    rng = np.random.default_rng(0)
    pts = np.array([[d, t] for d in (0.2, 0.5, 0.85) for t in (0.2, 0.8)] + [[0.2, 0.5], [0.5, 0.5], [0.85, 0.5],
                                                                             [0.05, 0.5], [1.0, 0.5], [0.65, 0.35]])
    A = rng.normal(size=(3, 40))

    def field(p):
        p = np.atleast_2d(p)
        return np.stack([p[:, 0], p[:, 1], p[:, 0] * p[:, 1]], 1) @ A
    km = S4.KnobMap(pts, field(pts))
    assert km.B.shape == (km.grid.shape[0], len(pts) + 3)
    g = {tuple(np.round(q, 6)): i for i, q in enumerate(km.grid)}
    for p in pts:
        if tuple(np.round(p, 2)) in g:
            assert np.allclose(km.Zg[g[tuple(np.round(p, 2))]], field(p)[0], atol=1e-6)
    w = np.full(40, 1 / 40)
    _, best = km.best(field([0.36, 0.62])[0], w)
    assert abs(best[0] - 0.36) <= 0.04 and abs(best[1] - 0.62) <= 0.04
    km1 = S4.KnobMap(np.linspace(0, 1, 12), np.outer(np.linspace(0, 1, 12), np.ones(5)))
    assert abs(km1.best(np.full(5, 0.3), np.full(5, 0.2))[1][0] - 0.3) <= 0.02


def test_feature_space_masks_and_weights():
    rows = [{"card.a": np.array([float(i)]), "curve.b": np.arange(4.0) * i} for i in range(1, 6)]
    fs = S4.FeatureSpace(rows)
    _, w = fs.transform({"card.a": np.array([3.0]), "curve.b": np.array([np.nan, 1.0, 2.0, np.nan])})
    assert abs(w.sum() - 1.0) < 1e-12
    ib = [i for i, n in enumerate(np.concatenate([[k] * s for k, s in zip(fs.names, fs.sizes)])) if n == "curve.b"]
    assert w[ib[0]] == 0.0 and w[ib[3]] == 0.0 and w[ib[1]] > 0
    # each block counts once: the card value and the curve block carry the same total weight
    ia = fs.names.index("card.a")
    assert abs(w[ia] - sum(w[i] for i in ib)) < 1e-9


def test_curve_mask_above_and_below_levels():
    arr = {"M1": np.zeros((len(P.FGRID), len(P.SWEEP_LEVELS))), "Mh": np.zeros((4, len(P.FGRID), 3)),
           "stair1k_db": np.zeros((9, len(P.STAIR_LEVELS_V1))), "stair220_db": np.zeros((9, len(P.STAIR_LEVELS_V1))),
           "ss_db": np.zeros(len(P.FGRID)), "sat_db": np.zeros(len(P.FGRID)), "P_db": np.zeros(len(P.FGRID)),
           "Q_db": np.zeros(len(P.FGRID)), "imd": np.zeros((4, 4, 3)), "curve_y": np.linspace(-1, 1, 65)}
    b = S4.curve_vector(arr, {"g0_ss_1k_db": 0.0}, max_level_dbfs=-20.0, min_level_dbfs=-70.0)
    M1 = b["M1"].reshape(len(P.FGRID), -1)
    sw = np.asarray(P.SWEEP_LEVELS)
    assert np.isnan(M1[:, sw > -20]).all() and np.isnan(M1[:, sw < -70]).all() and np.isfinite(M1[:, (sw <= -20) & (sw >= -70)]).all()
    assert np.isnan(b["sat"]).all() and np.isfinite(b["ss"]).all()


def _note(f0=110.0, n=SR * 3, decay=1.5):
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * f0 * t) * np.exp(-t / decay) * (1 - np.exp(-t / 0.003))
    return np.concatenate([np.zeros(SR // 5), x, np.zeros(SR // 5), x[: SR]]) * 0.3


def test_blind_features_see_asymmetric_clipping_and_flat_post_eq():
    x = _note()
    sym = np.tanh(8 * x)
    asym = np.where(x > 0, np.tanh(8 * x), 0.4 * np.tanh(3 * x))
    fs_, fa = S4.blind_features(sym), S4.blind_features(asym)
    assert fa["even_odd_db"][0] > fs_["even_odd_db"][0] + 3.0
    assert fs_["n_sustained"][0] > 5
    f0 = S4.blind_features(x, S4.ltas_db(x))
    assert np.nanmax(np.abs(f0["post_eq"])) < 1e-9          # a signal against its own spectrum: flat
    # a compressor-like clip raises the level of the decaying tail: flatter decay than the clean note
    assert fs_["decay_db_s"][0] > S4.blind_features(x)["decay_db_s"][0]


def test_estimate_knobs_needs_a_matching_di(tmp_path):
    x = _di()
    assert S4.estimate_knobs(x, None, ["HD2_DistScream808"]) == {}
    r = S4.estimate_knobs_with_confidence(x, None, ["HD2_DistScream808"])
    assert r["confidence"] == "none" and "no DI" in r["reason"]
    # with no fingerprint library on disk the inversion says so instead of guessing
    rng = np.random.default_rng(1)
    noise = 0.1 * rng.normal(size=x.size)
    (tmp_path / "cache" / "distortion-fp" / "dfp-v1").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        S4.estimate_knobs_with_confidence(np.tanh(5 * x), noise, ["m"], root=tmp_path)


def test_fit_guard_rejects_unrelated_di():
    x = _di()
    rng = np.random.default_rng(2)
    tw = S4.fit_twin(0.1 * rng.normal(size=x.size), np.tanh(5 * x))
    assert tw.fit_db > S4.MIN_FIT_DB
