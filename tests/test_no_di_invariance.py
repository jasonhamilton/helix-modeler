"""T161 cross-DI diagnostic: the candidate no-DI scores (tonematch.metrics.nodi_candidates) and the
statistics of scripts/no_di_invariance.py. Offline proxy audio only; no engine, no cached renders."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch.metrics import ltas, proxy
from tonematch.metrics import nodi_candidates as N
from tonematch.metrics.proxy import ProxyPreset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import no_di_invariance as X

SECONDS = 6.0


def _di(seed: int) -> np.ndarray:
    return proxy.guitar_di(seed, SECONDS, rhythm_seed=7)   # same rhythm, different notes


@pytest.fixture(scope="module")
def dis():
    return [_di(s) for s in (11, 12, 13)]


@pytest.fixture(scope="module")
def scales(dis):
    clips = [proxy.render(ProxyPreset(drive=d, cab=c), x) for d in (0.1, 0.5, 0.9) for c in (0, 2) for x in dis]
    tex = [N.texmod_features(y) for y in clips]
    fr = [N.frame_features(y) for y in clips]
    pres = [N.presence_vector(y) for y in clips]
    return N.fit_scales(tex, fr, pres)


def _score(name, a, b, sc):
    if name == "ltas_eqfit":
        return N.eqfit(ltas.features(a), ltas.features(b))[0]
    if name == "texmod":
        return N.texmod_distance(N.texmod_features(a), N.texmod_features(b), sc.tex)
    if name == "mmd_cep":
        return N.mmd(N.frame_features(a), N.frame_features(b), sc.frame, sc.sigma)
    if name == "presence_nodi":
        return N.vec_distance(N.presence_vector(a), N.presence_vector(b), sc.pres)
    raise KeyError(name)


# (tone A, tone B): B differs from A in what the score is meant to see
CASES = {
    "ltas_eqfit": (ProxyPreset(cab=2, mid=0.9, drive=0.4), ProxyPreset(cab=3, mid=0.1, drive=0.4)),
    "texmod": (ProxyPreset(drive=0.1), ProxyPreset(drive=0.1, comp=1.0, comp_thr=1.0)),
    "mmd_cep": (ProxyPreset(drive=0.05, cab=0), ProxyPreset(drive=0.9, cab=3)),
    "presence_nodi": (ProxyPreset(drive=0.05), ProxyPreset(drive=0.9)),
}


# texmod and mmd_cep FAIL this check on the proxy (measured 2026-10-18, before the Helix run): the same
# tone with other notes is as far as another tone. Kept as strict expected failures so the finding is
# recorded and a change in either is noticed. They are candidates under study, not kit terms.
NOTE_XFAIL = {"texmod": "proxy: compression is not separated from note changes",
              "mmd_cep": "proxy: frame distribution is dominated by the notes"}


@pytest.mark.parametrize("name", [pytest.param(n, marks=pytest.mark.xfail(strict=True, reason=NOTE_XFAIL[n]))
                                  if n in NOTE_XFAIL else n for n in CASES])
def test_candidate_is_insensitive_to_notes(name, dis, scales):
    """Same tone with other notes scores below another tone with other notes (the note-content
    invariance every no-DI term must have, contracts/loss-kit.md "Invariances")."""
    a, b = CASES[name]
    ya = [proxy.render(a, d) for d in dis]
    yb = [proxy.render(b, d) for d in dis]
    same = [_score(name, ya[i], ya[j], scales) for i in range(3) for j in range(3) if i != j]
    diff = [_score(name, ya[i], yb[j], scales) for i in range(3) for j in range(3) if i != j]
    assert max(same) < min(diff), (name, same, diff)


@pytest.mark.parametrize("name", list(CASES))
def test_identity_and_level_invariance(name, dis, scales):
    y = proxy.render(ProxyPreset(drive=0.5, cab=1), dis[0])
    assert _score(name, y, y, scales) == pytest.approx(0.0, abs=1e-6)
    ref = _score(name, y, proxy.render(ProxyPreset(drive=0.9, cab=3), dis[0]), scales)
    assert _score(name, y, 0.25 * y, scales) < 0.2 * ref


def test_eqfit_absorbs_a_bounded_tilt_but_not_more(dis):
    y = proxy.render(ProxyPreset(drive=0.3, cab=1), dis[0])
    for name in ("tilt_up", "tilt_down"):
        d, t, s = N.eqfit(ltas.features(y), ltas.features(N.pickup(y, name)))
        assert d < 0.5 * ltas.distance(ltas.features(y), ltas.features(N.pickup(y, name)))
        assert abs(t) <= N.EQFIT_TILT_MAX + 1e-9 and abs(s) <= N.EQFIT_SHELF_MAX + 1e-9
    # a notch is not tilt + shelf: most of it stays in the residual
    d, _, _ = N.eqfit(ltas.features(y), ltas.features(N.pickup(y, "position_notch")))
    assert d > 0.3


def test_pickup_filters_are_bounded():
    from scipy import signal
    for name, sos in N.PICKUP_FILTERS.items():
        _, h = signal.sosfreqz(sos, worN=np.geomspace(80, 8000, 200), fs=N.SR)
        db = 20 * np.log10(np.abs(h))
        assert db.max() < 7.0 and db.min() > -12.0, name


def test_lti_transplant_reproduces_a_linear_block(dis):
    from scipy import signal
    sos = signal.butter(2, [200, 3000], "bandpass", fs=N.SR, output="sos")
    x_a, x_b = dis[0], dis[1]
    y_a = signal.sosfilt(sos, x_a)
    h = N.lti_transfer(x_a, y_a, n_taps=4096)
    assert N.null_db(y_a, N.lti_apply(x_a, h)) < -40
    assert N.null_db(signal.sosfilt(sos, x_b), N.lti_apply(x_b, h)) < -30
    # a clipper is not linear: the null test rejects it
    y_nl = np.tanh(8 * x_a / np.abs(x_a).max())
    assert N.null_db(y_nl, N.lti_apply(x_a, N.lti_transfer(x_a, y_nl, n_taps=4096))) > N.LTI_NULL_MAX_DB


def test_mad_scale_handles_constant_and_nan_columns():
    rows = np.array([[1.0, 5.0, np.nan], [2.0, 5.0, np.nan], [3.0, 5.0, 1.0]])
    s = N.mad_scale(rows)
    assert np.all(np.isfinite(s)) and np.all(s > 0)
    assert N.vec_distance(np.array([1.0, np.nan]), np.array([2.0, 3.0]), np.array([1.0, 1.0])) == pytest.approx(1.0)


# ----------------------------------------------------------------------------------- script stats

def test_move_type_bins():
    assert X.move_type({"a": 64, "b": 10}, {"a": 64, "b": 0}) == "move10"
    assert X.move_type({"a": 100}, {"a": 64}) == "move25"
    assert X.move_type({"a": 127}, {"a": 0}) == "far"
    assert X.label_type("nbr:HD2_X", None, None) == "same_family_model"
    assert X.label_type("random:amp", None, None) == "other_family"
    assert X.player("Guitar-TECHS/P2_chords/audio/x.wav") == "P2"


def _fake(d_a, d_x, types):
    return {"meta": {}, "groups": [{
        "gid": "g", "source": "V1", "family": "drive", "true_model": "m", "spearman": [["x", "y"]],
        "conds": {"A": {"labels": ["true"] + [f"c{i}" for i in range(len(types))], "types": ["true"] + types,
                        "d": {s: d_a for s in X.SCORES}, "eqfit": [[0, 0]] * (len(types) + 1)},
                  "x": {"labels": ["true"] + [f"c{i}" for i in range(len(types))], "types": ["true"] + types,
                        "d": {s: d_x for s in X.SCORES}, "eqfit": [[0.5, 1.0]] * (len(types) + 1)},
                  "y": {"labels": ["true"] + [f"c{i}" for i in range(len(types))], "types": ["true"] + types,
                        "d": {s: d_x for s in X.SCORES}, "eqfit": [[0.5, 1.0]] * (len(types) + 1)}}}]}


def test_records_ratio_rank_and_wins():
    data = _fake([0.0, 2.0, 4.0, 6.0], [1.0, 3.0, 0.5, 7.0], ["move10", "same_family_model", "di_only"])
    recs = [r for r in X.item_records(data) if r["score"] == "ltas_tilt" and r["cond"] == "x"]
    r = recs[0]
    assert r["den"] == 4.0 and r["r"] == pytest.approx(0.25)
    assert r["rank1"] is False                       # the same_family_model distractor scores 0.5 < 1.0
    assert r["wins_by_type"]["move10"] == [1, 1] and r["wins_by_type"]["same_family_model"] == [0, 1]
    agg = X.aggregate(recs)
    assert agg["R"] == pytest.approx(1.0 / 4.0) and agg["rank1"] == 0.0
    sp = X.spearman_table(data)
    assert sp["V1|x~y"]["ltas_tilt"]["mean"] == pytest.approx(1.0)


def test_headline_is_family_balanced():
    recs = []
    for fam, r, ok, n in (("drive", 0.5, True, 10), ("cab", 2.0, False, 1)):
        recs += [{"score": "ltas_tilt", "cond": "other_player", "family": fam, "r": r, "rank1": ok}] * n
    h = X.headline(recs, "ltas_tilt", boot=20)
    assert h["R"] == pytest.approx(1.25) and h["rank1"] == pytest.approx(0.5)
    assert h["pooled_rank1"] == pytest.approx(10 / 11)


def test_wilson_interval():
    lo, hi = X.wilson(8, 10)
    assert 0.44 < lo < 0.5 and 0.94 < hi < 0.98
