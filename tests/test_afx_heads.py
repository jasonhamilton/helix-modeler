"""afx_heads (T189 predictor-pilot-v1): heads, pooling, calibration and scoring on small synthetic data."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.learning import afx_heads as ah

pytest.importorskip("sklearn")


def _data(n_per=40, seed=0):
    rng = np.random.default_rng(seed)
    mod_fam = np.array([0, 0, 1, 1, 2])
    cent = rng.standard_normal((5, 16)) * 2
    y = np.repeat(np.arange(5), n_per)
    K = rng.uniform(0, 1, (len(y), 2))
    K[y == 4, 1] = np.nan                        # model 4 has no tone knob
    X = cent[y] + 0.5 * rng.standard_normal((len(y), 16))
    X[:, 0] += 3 * K[:, 0]                       # Drive is linearly readable
    files = np.array([f"f{i % 6}" for i in range(len(y))])
    players = np.array(["P1" if i % 6 < 3 else "P2" for i in range(len(y))])
    return X, y, K, mod_fam, files, players


def test_linear_heads_learn_and_calibrate():
    X, y, K, mf, files, players = _data()
    h = ah.LinearHeads(mod_fam=mf, alpha=0.1).fit(X, y, K)
    o = h.predict(X)
    assert o["fl"].shape == (len(y), 3) and o["mu"].shape == (len(y), 5, 2)
    assert np.isnan(o["mu"][:, 4, 1]).all()
    cal = ah.calibrate(o, y, K, mf, files, players)
    r = ah.score(o, y, K, mf, cal)
    assert r["model_top1"] > 0.9 and r["family_top1"] > 0.9
    assert r["drive_within_true"] > 0.5
    assert abs(r["drive_cover68_global_true"] - 0.68) < 0.1     # fitted on the same pool


def test_multicrop_pooling_and_family_mode():
    X, y, K, mf, *_ = _data()
    Xc = np.stack([X, X + 0.01, X - 0.01], 1)
    for mode in ("flat", "separate"):
        h = ah.LinearHeads(mod_fam=mf, fam_mode=mode).fit(Xc, y, K)
        o = h.predict(Xc)
        assert o["lc"].shape == (len(y), 5)
        lj, lf = ah.joint_logp(o["fl"], o["lc"], mf)
        assert np.allclose(np.exp(lj).sum(1), 1, atol=1e-6)


def test_balance_weights_equal_per_player():
    files = np.array(["a"] * 10 + ["b"] * 2 + ["c"] * 5)
    players = np.array(["P1"] * 12 + ["P2"] * 5)
    w = ah.balance_weights(files, players)
    assert w[players == "P1"].sum() == pytest.approx(0.5)
    assert w[files == "a"].sum() == pytest.approx(w[files == "b"].sum())


def test_mlp_heads_shapes():
    pytest.importorskip("torch")
    X, y, K, mf, *_ = _data(n_per=10)
    h = ah.MLPHeads(mod_fam=mf, epochs=2, hidden=16).fit(X, y, K)
    o = h.predict(X)
    assert o["sig"].shape == (len(y), 5, 2) and np.isnan(o["sig"][:, 4, 1]).all()
