"""Modulation specialist (T116): phase invariance, note-content invariance, rate/depth/mix, sub-type,
ranking, seed and the score cache, on numpy effects with known truth."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import butter, lfilter

from tonematch.metrics import mod_specialist as MS

SR = 48_000


def guitar_di(seed: int, seconds: float = 6.0) -> np.ndarray:
    """Plucked harmonic notes, one after another, at random pitches."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = np.zeros(n)
    t = 0
    while t < n:
        f0 = rng.choice([82.4, 110.0, 146.8, 196.0, 246.9, 329.6]) * 2 ** (rng.integers(0, 12) / 12)
        L = min(int(rng.uniform(0.25, 0.7) * SR), n - t)
        tt = np.arange(L) / SR
        h = np.arange(1, 13)[:, None]
        keep = (f0 * h[:, 0]) < 10_000
        note = np.sum((0.7 ** (h[keep] - 1)) * np.sin(2 * np.pi * f0 * h[keep] * tt + rng.uniform(0, 6.3, (keep.sum(), 1))), 0)
        x[t:t + L] += note * np.exp(-tt * rng.uniform(2, 5))
        t += L
    return 0.5 * x / np.abs(x).max()


def _lfo(n, rate, phase):
    return np.sin(2 * np.pi * rate * np.arange(n) / SR + phase)


def _moddelay(x, d):
    idx = np.arange(x.size) - d
    i0 = np.clip(np.floor(idx).astype(int), 0, x.size - 2)
    fr = idx - np.floor(idx)
    return x[i0] * (1 - fr) + x[i0 + 1] * fr


def tremolo(x, rate, depth_db, phase=0.0):
    return x * 10 ** (-depth_db / 40 * (1 + _lfo(x.size, rate, phase)))


def vibrato(x, rate, depth_ms, phase=0.0):
    return _moddelay(x, (5 + depth_ms * _lfo(x.size, rate, phase)) * SR / 1000)


def chorus(x, rate, depth_ms, mix=0.5, phase=0.0):
    return (1 - mix) * x + mix * _moddelay(x, (8 + depth_ms * _lfo(x.size, rate, phase)) * SR / 1000)


def static_eq(x):
    b, a = butter(2, [400 / (SR / 2), 2500 / (SR / 2)], "band")
    return 0.6 * x + lfilter(b, a, x)


@pytest.fixture(scope="module")
def di():
    return guitar_di(1)


@pytest.fixture(scope="module")
def di2():
    return guitar_di(7)


@pytest.mark.parametrize("make,faster,rate", [
    (lambda x, p: tremolo(x, 4.0, 6.0, p), lambda x: tremolo(x, 4.6, 6.0), 4.0),
    (lambda x, p: vibrato(x, 5.0, 1.0, p), lambda x: vibrato(x, 5.75, 1.0), 5.0),
    (lambda x, p: chorus(x, 1.5, 2.0, 0.5, p), lambda x: chorus(x, 1.725, 2.0, 0.5), 1.5)])
def test_phase_invariant_and_ranks_truth_first(di, make, faster, rate):
    """The same effect with its LFO started half a cycle later is closer than the effect at 1.15 x the
    rate, a shallower tremolo, a static EQ or no effect; a frame-by-frame null of the two is far apart
    (the LFO phase matters to it)."""
    target = make(di, 0.0)
    truth = make(di, np.pi)
    d_truth = MS.mod_distance(target, truth, di)
    others = {
        "rate +15 %": MS.mod_distance(target, faster(di), di),
        "static eq": MS.mod_distance(target, static_eq(di), di),
        "dry": MS.mod_distance(target, di.copy(), di),
        "tremolo 3 dB": MS.mod_distance(target, tremolo(di, rate, 3.0), di),
    }
    assert all(d_truth < v for v in others.values()), (d_truth, others)
    null = 10 * np.log10(np.sum((target - truth) ** 2) / np.sum(target ** 2))
    assert null > -20.0


@pytest.mark.parametrize("make,rate", [(lambda x: tremolo(x, 4.0, 6.0, 0.7), 4.0),
                                       (lambda x: vibrato(x, 5.0, 1.0, 2.1), 5.0),
                                       (lambda x: chorus(x, 1.5, 2.0, 0.5, 0.3), 1.5)])
def test_note_content_invariance(di, di2, make, rate):
    """Rate and depths measured on two different performances agree: the DI is divided out."""
    a, b = MS.analyse(make(di), di), MS.analyse(make(di2), di2)
    assert a.rate_hz == pytest.approx(rate, rel=0.03)
    assert b.rate_hz == pytest.approx(rate, rel=0.03)
    assert abs(a.am_depth_db - b.am_depth_db) < 1.5
    assert abs(a.pm_depth_cents - b.pm_depth_cents) < max(3.0, 0.2 * a.pm_depth_cents)


def test_depths_mix_and_subtypes(di):
    tr = MS.analyse(tremolo(di, 4.0, 6.0), di)
    assert tr.subtype == "tremolo" and 4.5 < tr.am_depth_db < 7.0 and tr.pm_depth_cents < 3.0
    vb = MS.analyse(vibrato(di, 5.0, 1.0), di)
    # a delay swinging +-1 ms at 5 Hz shifts pitch by up to 2 pi 5 0.001 = 3.1 %, 54 cents
    assert vb.subtype == "vibrato" and vb.pm_depth_cents == pytest.approx(54.0, rel=0.15) and vb.wet_mix > 0.8
    ch = MS.analyse(chorus(di, 1.5, 2.0, 0.5), di)
    assert ch.subtype == "chorus" and 0.3 < ch.wet_mix < 0.65 and ch.pm_depth_cents > 5.0
    dry = MS.analyse(di.copy(), di)
    assert dry.am_depth_db < 0.5 and dry.pm_depth_cents < 1.0 and not dry.is_mod


def test_presence_probability_decides(di):
    a = MS.analyse(di.copy(), di, presence_mod=0.9)
    assert a.is_mod and a.presence_mod == pytest.approx(0.9)
    b = MS.analyse(tremolo(di, 4.0, 6.0), di, presence_mod=0.1)
    assert b.is_mod == (b.mod_score >= MS.OWN_STRICT)


def test_level_does_not_count(di):
    t = tremolo(di, 3.0, 5.0)
    assert MS.mod_distance(t, 0.3 * tremolo(di, 3.0, 5.0, 1.0), di) == pytest.approx(
        MS.mod_distance(t, tremolo(di, 3.0, 5.0, 1.0), di), rel=1e-6, abs=1e-9)


def test_score_cache_matches_distance(di):
    t, c = tremolo(di, 4.0, 6.0), tremolo(di, 4.5, 6.0)
    assert MS.score(t, c, di) == pytest.approx(MS.mod_distance(t, c, di))
    assert MS.score(t, c, di) == pytest.approx(MS.score(t, c, di))


def test_seed_nearest_calibration(di):
    """With a calibration table, the true model ranks first and its knob seed is near the truth."""
    rows = {}
    for model, make, knob in (("Trem", lambda r: tremolo(di, r, 6.0), "Speed"),
                              ("Vib", lambda r: vibrato(di, r, 1.0), "Speed")):
        rr = []
        for r in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0):
            y = make(r)
            f = MS.features(y, di)
            a = MS.analyse(y, di, f)
            rr.append({"values": {knob: r}, "summary": MS.summary_vector(a, f).tolist()})
        rows[model] = {"subtype": "tremolo" if model == "Trem" else "vibrato", "rows": rr,
                       "_X": np.asarray([q["summary"] for q in rr])}
    cal = {"models": rows}
    s = MS.seed(tremolo(di, 4.0, 6.0, 1.0), di, calibration=cal, presence_mod=None)
    assert s["ranking"][0] == "Trem" and s["subtype_vote"] == "tremolo"
    assert 3.0 <= s["models"]["Trem"]["values"]["Speed"] <= 5.0
    s2 = MS.seed(tremolo(di, 4.0, 6.0), di, calibration={}, presence_mod=None)
    assert s2["models"] == {} and s2["analysis"]["rate_hz"] == pytest.approx(4.0, rel=0.03)


def test_short_or_bad_input():
    x = np.zeros(1000)
    with pytest.raises(ValueError):
        MS.features(x, x)
    with pytest.raises(ValueError):
        MS.features(np.ones(SR), np.ones(SR), sr=44_100)


def test_subtype_of_model_names():
    assert MS.subtype_of_model("HD2_TremoloHarmonic") == "tremolo"
    assert MS.subtype_of_model("HD2_MM4PatternTrem") == "tremolo"
    assert MS.subtype_of_model("HD2_VibratoBubbleVibrato") == "vibrato"
    assert MS.subtype_of_model("HD2_Chorus70sChorus") == "chorus"
    assert MS.subtype_of_model("HD2_FlangerGrayFlanger") == "flanger"
    assert MS.subtype_of_model("HD2_PhaserUbiquitousVibe") == "phaser"
    assert MS.subtype_of_model("HD2_Rotary122Rotary") == "rotary"
    assert MS.subtype_of_model("HD2_EQSimpleTilt") is None


def _toy_calibration(di):
    rows = []
    for r in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0):
        for depth in (3.0, 6.0, 9.0):
            y = tremolo(di, r, depth)
            f = MS.features(y, di)
            a = MS.analyse(y, di, f)
            rows.append({"values": {"Speed": r, "Intensity": depth / 10}, "summary": MS.summary_vector(a, f).tolist(),
                         "analysis": a.to_dict()})
    return {"models": {"Trem": {"subtype": "tremolo", "rows": rows, "_X": np.asarray([q["summary"] for q in rows])}}}


def test_dispatch_adapter(di):
    """`make_specialist()` plugs into the search dispatch: hint, FamilySeed with knob seeds, no family score by
    default (not admitted until its trust bar passes), the wrapped kit adds weight x mod_distance."""
    from tonematch.search.specialists import FamilySeed, Specialist, build_dispatch

    sp = MS.make_specialist(calibration=_toy_calibration(di), presence=False)
    assert isinstance(sp, Specialist) and sp.family == "mod"
    target = tremolo(di, 4.0, 6.0, 1.0)
    h = sp.screen_hint(target, di)
    assert 0.0 <= h <= 1.0
    fs = sp.seed(target, di)
    assert isinstance(fs, FamilySeed) and fs.category_probs["mod"] == pytest.approx(h)
    seeds = fs.for_model("Trem", "mod", {"Speed": (0.0, 10.0), "Intensity": (0.0, 1.0)})
    assert 3.0 <= seeds["Speed"].value <= 5.0 and 0.04 <= seeds["Speed"].half_width <= 0.20
    assert fs.for_model("Trem", "eq", {"Speed": (0.0, 10.0)}) == {}
    assert fs.for_model("Unknown", "mod", {"Speed": (0.0, 10.0)}) == {}
    if not MS.FAMILY_SCORE_ADMITTED:
        assert sp.score(target, di) is None

    class Kit:
        def score(self, t, c, **kw):
            class S:
                total, terms, measured = 1.0, {"g": 1.0}, {}
            return S()

    wrap = MS.make_specialist(calibration={}, presence=False, score_weight=0.2).score(target, di)
    k = wrap(Kit())
    near, far = k.score(target, tremolo(di, 4.0, 6.0, 2.0)), k.score(target, static_eq(di))
    assert k.inner is not None and near.total < far.total and near.terms["g"] == 1.0
    assert near.total == pytest.approx(1.0 + 0.2 * near.terms[MS.NAME])
    d = build_dispatch(["mod"], target, di, [("Trem", "mod"), ("EQ", "eq")],
                       ranges={"Trem": {"Speed": (0.0, 10.0), "Intensity": (0.0, 1.0)}},
                       specialists={"mod": sp})
    assert "Trem" in d.seeds and "mod" in d.hints
