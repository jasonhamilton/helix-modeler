"""Time-based specialist (T122) on synthetic signals: plucks through a delay, a reverb, a tremolo."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

from tonematch.search.specialists import build_dispatch
from tonematch.search.time_features import (
    echo_pattern,
    ir_analysis,
    onsets,
    probe_features,
    shape_features,
)
from tonematch.search.time_specialist import (
    Classifier,
    FamilyKit,
    SeedModel,
    TimeFamilyScore,
    TimeSpecialist,
    analyse,
    fit_seed_model,
    rule_probs,
    seeds_from_bank,
)

SR = 48_000


def plucks(seed=0, seconds=10.0):
    """Guitar-like DI: a pluck every 0.25-0.75 s, a few decaying harmonics, random pitch."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(seconds * SR))
    t = np.arange(int(1.2 * SR)) / SR
    starts, s = [], 0.2
    while s < seconds - 1.2:                      # irregular, so the DI has no repetition of its own
        starts.append(s)
        s += rng.uniform(0.25, 0.75)
    for s in starts:
        f0 = float(rng.choice([82.4, 110.0, 146.8, 196.0, 247.0, 330.0]))
        n = sum(np.sin(2 * np.pi * f0 * k * t + rng.uniform(0, 6)) / k for k in range(1, 7)) * np.exp(-t * rng.uniform(3, 6))
        i = int(s * SR)
        x[i:i + n.size] += n[: x.size - i]
    return (x / np.sqrt(np.mean(x ** 2)) * 0.1).astype(np.float32)


def delay(x, t_s, fb=0.5, mix=0.6, taps=6):
    y = x.astype(np.float64).copy()
    for k in range(1, taps + 1):
        d = round(k * t_s * SR)
        y[d:] += mix * fb ** (k - 1) * x[: x.size - d]
    return y.astype(np.float32)


def reverb(x, rt60=1.2, mix=0.5, predelay=0.02, seed=3):
    rng = np.random.default_rng(seed)
    n = int(1.8 * SR)
    t = np.arange(n) / SR
    h = rng.standard_normal(n) * np.exp(-6.9 * t / rt60) * np.minimum(t / 0.03, 1.0)
    h = np.concatenate([np.zeros(int(predelay * SR)), h])
    h /= np.sqrt(np.sum(h ** 2))
    return (x + mix * fftconvolve(x, h)[: x.size]).astype(np.float32)


def tremolo(x, rate=5.0, depth=0.8):
    t = np.arange(x.size) / SR
    return (x * (1 - depth * 0.5 * (1 + np.sin(2 * np.pi * rate * t)))).astype(np.float32)


@pytest.fixture(scope="module")
def di():
    return plucks()


def test_echo_pattern_finds_the_delay_time_and_its_repeats(di):
    p = echo_pattern(ir_analysis(delay(di, 0.35), di))
    assert abs(p["t"][0] - 0.35) < 0.01 and len(p["t"]) >= 3
    assert abs(p["t"][1] - 0.70) < 0.012


def test_onsets_find_the_front_of_a_smeared_echo(di):
    from scipy.signal import lfilter
    echo = np.zeros_like(di)
    d = int(0.3 * SR)
    echo[d:] = di[: di.size - d]
    a = np.exp(-1 / (0.02 * SR))                  # smear the delayed copy only: a one-pole of 20 ms, causal
    echo = lfilter([1 - a], [1, -a], echo)
    y = (di + 4.0 * echo).astype(np.float32)
    o = onsets(ir_analysis(y, di))
    assert o["t"] and abs(o["t"][0] - 0.30) < 0.04


def test_shape_features_separate_a_delay_from_a_reverb(di):
    sd = shape_features(ir_analysis(delay(di, 0.35, fb=0.6), di))
    sr = shape_features(ir_analysis(reverb(di, 1.2), di))
    assert sd["conc"] > sr["conc"] + 0.1
    assert sr["line_resid_db"] < sd["line_resid_db"]


def test_rule_probs_call_the_three_and_sum_to_one(di):
    p_del = rule_probs(analyse(delay(di, 0.35, fb=0.6), di).vector())
    p_rev = rule_probs(analyse(reverb(di, 1.5, mix=0.7), di).vector())
    p_mod = rule_probs(analyse(tremolo(di), di).vector())
    for p in (p_del, p_rev, p_mod):
        assert abs(sum(v for k, v in p.items() if k != "time") - 1.0) < 1e-9
    assert max(p_del, key=lambda k: p_del[k] if k != "time" else -1) == "delay"
    assert max(p_rev, key=lambda k: p_rev[k] if k != "time" else -1) == "reverb"
    assert p_mod["mod"] > p_mod["delay"] and p_mod["mod"] > p_mod["reverb"]


def test_probe_features_carry_time_and_decay(di):
    f = probe_features(ir_analysis(delay(di, 0.4), di))
    assert abs(f["on1"] - 0.4) < 0.04 and abs(f["t1"] - 0.4) < 0.01
    r = probe_features(ir_analysis(reverb(di, 1.0), di))
    assert 0.5 < r["rt60_full"] < 2.0


def test_family_score_is_zero_for_the_target_and_grows_with_a_wrong_time(di):
    tgt = delay(di, 0.35, fb=0.5)
    fam = TimeFamilyScore(tgt, di)
    d0, d_near, d_far = fam.distance(tgt), fam.distance(delay(di, 0.40, fb=0.5)), fam.distance(delay(di, 0.70, fb=0.5))
    assert d0 < 1e-6 and 0 < d_near and d_far > 0.03
    assert fam.distance(reverb(di, 1.2)) > d0 + 0.05


def test_family_kit_adds_the_term_and_exposes_inner(di):
    class Inner:
        def score(self, t, c, **kw):
            from types import SimpleNamespace as NS
            return NS(total=0.1, terms={"a": 1.0}, measured={})

    tgt = delay(di, 0.35)
    k = FamilyKit(Inner(), TimeFamilyScore(tgt, di), 0.5)
    s = k.score(tgt, delay(di, 0.5))
    assert s.total > 0.1 and "time_family" in s.terms and k.inner is not None and abs(k.score(tgt, tgt).total - 0.1) < 1e-6


def test_seed_model_recovers_a_noisy_linear_map_and_reports_its_error():
    rng = np.random.default_rng(1)
    n = 60
    time_knob = rng.uniform(0.1, 0.9, n)
    F = [{"t1": 1.2 * t + rng.normal(0, 0.005), "late_db": rng.normal(-10, 3), "on1": 1.2 * t} for t in time_knob]
    sm = fit_seed_model("m", "delay", ["Time"], F, time_knob[:, None], keys=("t1", "late_db", "on1"))
    assert "t1" in sm.keys or "on1" in sm.keys
    assert abs(sm.predict({"t1": 0.6, "on1": 0.6, "late_db": -10})[0] - 0.5) < 0.05 and sm.loo[0] < 0.05
    sd = seeds_from_bank(sm, {"t1": 0.6, "on1": 0.6, "late_db": -10}, {"Time": (0.0, 1.2)})
    assert abs(sd["Time"].value - 0.6) < 0.08 and sd["Time"].half_width >= 0.04
    sm2 = SeedModel.from_json(sm.to_json())
    assert np.allclose(sm2.predict({"t1": 0.3, "on1": 0.3, "late_db": -9}), sm.predict({"t1": 0.3, "on1": 0.3, "late_db": -9}))


def test_a_knob_the_features_cannot_predict_is_left_free():
    rng = np.random.default_rng(2)
    n = 50
    F = [{"t1": float(rng.uniform(0, 1))} for _ in range(n)]
    Y = rng.uniform(0.1, 0.9, (n, 1))                   # unrelated to any feature
    sm = fit_seed_model("m", "delay", ["Tone"], F, Y, keys=("t1",))
    assert seeds_from_bank(sm, {"t1": 0.5}, {"Tone": (0.0, 1.0)}) == {}


def test_classifier_round_trip():
    keys = ("late_db", "conc")
    c = Classifier(keys, np.zeros(2), np.zeros(2), np.ones(2), np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]),
                   np.zeros(4))
    c2 = Classifier.from_json(c.to_json())
    p = c2.proba({"late_db": 3.0, "conc": 0.0})
    four = {k: v for k, v in p.items() if k != "time"}
    assert abs(sum(four.values()) - 1) < 1e-9 and max(four, key=four.get) == "delay"


def test_specialist_plugs_into_the_dispatch(di):
    sp = TimeSpecialist(classifier=None, bank={}, calibration={})
    pool = [("d0", "delay"), ("r0", "reverb"), ("m0", "mod"), ("a0", "amp")]
    d = build_dispatch(("time",), delay(di, 0.35, fb=0.6), di, pool, specialists={"time": sp})
    assert d.hints["time"] > 0.5 and d.category_probs["delay"] > d.category_probs["reverb"] and d.wrap is not None
    n = build_dispatch(("time",), di, di, pool, specialists={"time": sp})        # the DI itself: no effect
    assert n.hints["time"] < 0.5 and n.wrap is None


def test_family_head_decides_time_and_the_four_way_head_splits_it():
    keys = ("late_db", "conc")
    four = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]])
    c = Classifier(keys, np.zeros(2), np.zeros(2), np.ones(2), four, np.zeros(4),
                   fam_coef=np.array([4.0, 0.0, 0.0, 0.0]), fam_intercept=0.0)
    hi = c.proba({"late_db": 3.0, "conc": 0.0})
    lo = c.proba({"late_db": -3.0, "conc": 0.0})
    assert hi["time"] > 0.99 and lo["time"] < 0.01
    assert abs(sum(v for k, v in hi.items() if k != "time") - 1) < 1e-9 and hi["delay"] > hi["reverb"]
    assert hi["time"] == hi["delay"] + hi["reverb"]
    c2 = Classifier.from_json(c.to_json())
    assert abs(c2.proba({"late_db": 1.0, "conc": 0.0})["time"] - c.proba({"late_db": 1.0, "conc": 0.0})["time"]) < 1e-12


def test_screen_hint_is_the_time_probability(di):
    sp = TimeSpecialist(classifier=None, bank={}, calibration={})
    assert sp.screen_hint(delay(di, 0.35, fb=0.6), di) > 0.5 > sp.screen_hint(di, di)


def test_tail_slopes_see_a_longer_decay_as_longer(di):
    from tonematch.search.time_features import tail_slopes

    short = tail_slopes(ir_analysis(reverb(di, 0.5, mix=1.0), di))
    long = tail_slopes(ir_analysis(reverb(di, 1.6, mix=1.0), di))
    assert long["rt60_full"] > short["rt60_full"] or long["len_full"] > short["len_full"]
