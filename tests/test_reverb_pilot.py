"""T230 reverb pilot: candidate signals (src/tonematch/metrics/reverb_signals.py) and the pre-registered analysis
(scripts/reverb_pilot_t230.py). Synthetic signals and made-up answers only; no engine, no song audio."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.metrics import reverb_signals as RV

SR = 48_000


def notes(freq: float, onsets_s, dur_s: float = 0.25, total_s: float = 6.0, seed: int = 0) -> np.ndarray:
    """Plucked harmonic tones (12 harmonics, exponential decay) at `onsets_s`; silence between."""
    x = np.zeros(int(total_s * SR))
    t = np.arange(int(dur_s * SR)) / SR
    tone = sum(np.sin(2 * np.pi * freq * h * t) / h for h in range(1, 13)) * np.exp(-t / 0.08)
    for o in onsets_s:
        i = int(o * SR)
        x[i: i + len(tone)] += tone[: len(x) - i]
    return x.astype(np.float32)


def add_reverb(x: np.ndarray, rt60: float, mix: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(rt60 * SR)
    h = rng.standard_normal(n) * 10 ** (-3 * np.arange(n) / n)
    from scipy.signal import fftconvolve
    w = fftconvolve(x, h)[: len(x)]
    w *= np.sqrt(np.mean(x ** 2) / max(np.mean(w ** 2), 1e-12))
    return ((1 - mix) * x + mix * w).astype(np.float32)


ONSETS = [0.2, 0.9, 1.3, 2.4, 2.8, 3.5, 4.6, 5.0]


# ---------------------------------------------------------------- (iv) dip fill

def test_dip_fill_rises_with_reverb_and_is_level_invariant():
    x = notes(196.0, ONSETS)
    m = RV.active_mask(x)
    d0 = RV.dip_stats(x, m)
    d1 = RV.dip_stats(add_reverb(x, 1.2, 0.4), m)
    assert d1[0] > d0[0] + 3.0                        # the dips fill in
    assert np.allclose(RV.dip_stats(10 * x, m), d0, atol=1e-6)


def test_dip_fill_invariant_to_note_content():
    a, b = notes(147.0, ONSETS), notes(220.0, ONSETS)           # same rhythm, other notes
    m = RV.active_mask(a)
    assert RV.dip_distance(RV.dip_stats(a, m), RV.dip_stats(b, m)) < 1.0
    effect = RV.dip_distance(RV.dip_stats(a, m), RV.dip_stats(add_reverb(a, 1.0, 0.4), m))
    for seed in range(3):                                       # the same reverb on other notes: much less than the reverb itself
        ra, rb = add_reverb(a, 1.0, 0.4, seed), add_reverb(b, 1.0, 0.4, seed)
        assert RV.dip_distance(RV.dip_stats(ra, m), RV.dip_stats(rb, m)) < 0.25 * effect


def test_active_mask_leaves_out_long_rests():
    x = notes(196.0, [0.2, 0.5], total_s=4.0)
    m = RV.active_mask(x)
    assert m[:60].any() and not m[200:].any()          # 10 ms frames: the rest after 2 s is out


# ---------------------------------------------------------------- (ii) note tails

def test_tail_distance_nan_and_zero():
    a = RV.TailStats(3, -40.0, -20.0, "")
    none = RV.TailStats(0, None, None, "")
    assert np.isnan(RV.tail_distance(a, none)) and np.isnan(RV.tail_distance(none, a))
    assert RV.tail_distance(a, a) == 0.0
    assert RV.tail_distance(a, RV.TailStats(2, -10.0, -14.0, "")) == pytest.approx(30 / 30 + 6 / 6)


def test_tail_stats_sees_a_synthetic_tail():
    dry = notes(196.0, [0.2, 0.35, 0.5, 0.65, 2.0, 2.15, 2.3, 2.45, 4.0, 4.15, 4.3, 4.45], dur_s=0.3)
    wet = add_reverb(dry, 1.5, 0.3)
    t_dry = RV.tail_stats(dry, dry, dry)
    t_wet = RV.tail_stats(wet, dry, dry)
    assert t_wet.measurable and t_wet.slope_db_s < -10
    assert (not t_dry.measurable) or t_dry.early_db < t_wet.early_db


# ---------------------------------------------------------------- (iii) regressors

def test_regressor_distance():
    sc = {"a.Decay": 0.1, "a.Mix": 0.05}
    v = {"a.Decay": 0.4, "a.Mix": 0.3}
    assert RV.regressor_distance(v, v, sc) == 0.0
    assert RV.regressor_distance(v, {"a.Decay": 0.5, "a.Mix": 0.35}, sc) == pytest.approx(1.0)


# ---------------------------------------------------------------- calibration

def test_ir_rt60_recovers_synthetic_decay():
    rng = np.random.default_rng(0)
    for rt in (0.8, 1.6):
        n = int(4 * rt * SR)
        y = np.zeros(int(0.5 * SR) + n)
        y[int(0.5 * SR):] = rng.standard_normal(n) * 10 ** (-3 * np.arange(n) / (rt * SR))
        r = RV.ir_rt60(y, 0.5)
        assert r["rt60_s"] == pytest.approx(rt, rel=0.1)


def test_pick_decays_hits_common_targets():
    cal = {"A": [(d, 0.5 * np.exp(1.5 * d)) for d in np.linspace(0.1, 1.0, 7)],
           "B": [(d, 0.9 * np.exp(2.5 * d)) for d in np.linspace(0.1, 1.0, 7)]}
    pk = RV.pick_decays(cal)
    lo, hi = pk["range_s"]
    assert lo == pytest.approx(0.9 * np.exp(0.25)) and hi == pytest.approx(0.5 * np.exp(1.5))
    for m, f in (("A", lambda d: 0.5 * np.exp(1.5 * d)), ("B", lambda d: 0.9 * np.exp(2.5 * d))):
        got = [f(d) for d in pk["decays"][m]]
        assert np.allclose(got, pk["targets_s"], rtol=0.05)
        assert pk["decays"][m] == sorted(pk["decays"][m])


def test_pick_decays_needs_overlap():
    with pytest.raises(ValueError):
        RV.pick_decays({"A": [(0.1, 0.2), (1.0, 0.5)], "B": [(0.1, 1.0), (1.0, 3.0)]})


# ---------------------------------------------------------------- design

def test_design_constraints_and_seed():
    import reverb_pilot_t230 as RP

    d1, d2 = RP.build_design(), RP.build_design()
    assert d1 == d2
    for d in d1.values():
        cells = d["grid_picks"]
        assert len(cells) == 4 and len(set(cells)) == 4
        types = [c.split("-")[0] for c in cells]
        assert sorted(types) == ["hall", "hall", "plate", "room"]
        assert {c.split("-")[1] for c in cells} == {"m25", "m35", "m50"}
        assert {c.split("-")[2] for c in cells} == {"short", "medium", "long"}
        assert d["candidates"][4:] == ["t229-r35", "t229-p35"]
        assert sorted(d["letters"]) == list("ABCDEFG")
        assert sorted(d["letters"].values()) == sorted(d["candidates"] + [d["repeat"]])
        assert len(d["repeat_letters"]) == 2 and all(d["letters"][L] == d["repeat"] for L in d["repeat_letters"])


# ---------------------------------------------------------------- analysis

def fake_key(n_songs: int = 7, seed: int = 0, tail_nan: bool = False):
    import reverb_pilot_t230 as RP

    rng = np.random.default_rng(seed)
    design = RP.build_design()
    songs = list(design)[:n_songs]
    items = {}
    for k in {c for d in design.values() for c in d["candidates"]}:
        t = k.split("-")[0]
        items[k] = {"type": {"room": "Room", "plate": "Plate", "hall": "Hall", "t229": "Room"}[t],
                    "mix": 0.35 if k.startswith("t229") else int(k.split("-")[1][1:]) / 100,
                    "decay_level": "t229" if k.startswith("t229") else k.split("-")[2]}
    key = {"preregistration_sha256": "x", "items": items, "songs": {}}
    for s in songs:
        d = design[s]
        good = rng.permutation(6).astype(float)               # the 'true' order: 0 = closest
        sig = {k: {"afxrep": good[i], "tail": (np.nan if tail_nan and i == 0 else -good[i]),
                   "regressor": float(rng.standard_normal()), "dipfill": 1.0} for i, k in enumerate(d["candidates"])}
        key["songs"][s] = {**d, "signals": sig}
    return key


def answers_from(key, signal="afxrep", noise=None):
    ans = {}
    for s, d in key["songs"].items():
        v = {k: d["signals"][k][signal] for k in d["candidates"]}
        order = sorted(v, key=v.get)
        rank_of = {k: i + 1 for i, k in enumerate(order)}
        ranks = {L: rank_of[k] for L, k in d["letters"].items()}
        _r1, r2 = d["repeat_letters"]
        ranks[r2] = 7                                          # repeat ranked last the second time
        ans[s] = {"ranks": ranks}
    return ans


def test_analysis_agreement_and_repeat_first():
    import reverb_pilot_t230 as RP

    key = fake_key()
    res = RP.analyse(key, answers_from(key), draws=500)
    a = res["signals"]["afxrep"]["pooled"]["all7"]
    assert a["mean_rho"] > 0.8 and a["p_one_sided"] < 0.01 and a["songs_positive"] == 7
    t = res["signals"]["tail"]["pooled"]["all7"]
    assert t["mean_rho"] < -0.8
    assert res["signals"]["dipfill"]["pooled"]["all7"]["n_songs"] == 0          # constant distances: undefined
    assert res["signals"]["afxrep"]["proposed_reading_agrees"] is True
    assert res["signals"]["tail"]["proposed_reading_agrees"] is False
    assert list(res)[:3] == ["preregistration_sha256", "unanswered", "repeat"]
    assert all(v["raw"][1] == 7 for v in res["repeat"]["by_song"].values())
    txt = RP.fmt(res)
    assert txt.index("repeat consistency") < txt.index("AFx-Rep")


def test_analysis_ties_unanswered_and_amendment():
    import reverb_pilot_t230 as RP

    key = fake_key(tail_nan=True)
    ans = answers_from(key)
    s0 = next(iter(ans))
    ans[s0] = {"ranks": {L: 1 for L in "ABCDEFG"}}            # all tied: undecided
    s1 = list(ans)[1]
    ans[s1]["ranks"].pop("A")                                  # unanswered
    res = RP.analyse(key, ans, draws=200)
    assert res["unanswered"] == [s1]
    assert s0 in res["signals"]["afxrep"]["undefined_or_undecided"]
    assert res["signals"]["afxrep"]["pairs"]["human_ties"] >= 15
    d = RP.signal_vector(key, s0, "tail")
    raw = [key["songs"][s0]["signals"][k]["tail"] for k in key["songs"][s0]["candidates"]]
    assert np.isnan(raw[0]) and np.all(np.isfinite(d)) and d[0] == pytest.approx(np.nanmax(raw) + 1)


def test_holm():
    import reverb_pilot_t230 as RP

    h = RP.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": None})
    assert h == {"a": 0.03, "c": 0.06, "b": 0.06}


def test_human_values_repeat_averaged():
    import reverb_pilot_t230 as RP

    d = RP.build_design()["mother"]
    ranks = {}
    for i, (L, k) in enumerate(sorted(d["letters"].items())):
        ranks[L] = i + 1
    h, rep = RP.human_values(d, ranks)
    assert len(h) == 6 and rep["abs_diff"] == abs(ranks[d["repeat_letters"][0]] - ranks[d["repeat_letters"][1]])
    assert RP.human_values(d, {**ranks, "A": None}) is None
