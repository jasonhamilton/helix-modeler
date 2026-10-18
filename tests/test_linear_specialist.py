"""Linear specialist (T117): probe, transfer, tables, fit, dispatch face. Synthetic signals only."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import butter, lfilter, sosfilt

from tonematch.metrics import linear_specialist as L

SR = L.SR


# ---------------------------------------------------------------- fake linear "models" (zero phase)
def hp_db(f, fc):
    return 10 * np.log10(f ** 4 / (f ** 4 + fc ** 4))


def peak_db(f, f0, gain):
    return gain * np.exp(-(np.log2(f / f0)) ** 2 / (2 * 0.4 ** 2))


def fake_eq_db(f, v):
    """Two interacting knobs (Freq x Gain) and an independent LowCut."""
    return peak_db(f, v["Freq"], v["Gain"]) + hp_db(f, v["LowCut"])


def fake_graphic_db(f, v):
    return sum(v[f"B{i}"] * np.exp(-(np.log2(f / fc)) ** 2 / (2 * 0.5 ** 2))
               for i, fc in enumerate((100.0, 400.0, 1600.0, 6400.0)))


EQ_RANGES = {"Freq": (100.0, 8000.0), "Gain": (-12.0, 12.0), "LowCut": (20.0, 1000.0)}
EQ_DEFAULTS = {"Freq": 1000.0, "Gain": 0.0, "LowCut": 20.0}
GR_RANGES = {f"B{i}": (-12.0, 12.0) for i in range(4)}
GR_DEFAULTS = {f"B{i}": 0.0 for i in range(4)}


def apply_db(x, fn, v):
    """Zero-phase filtering of x by a magnitude response fn(f, v) in dB (circular, via the FFT)."""
    X = np.fft.rfft(x)
    f = np.maximum(np.fft.rfftfreq(x.size, 1 / SR), 1.0)
    return np.fft.irfft(X * 10 ** (fn(f, v) / 20), x.size)


def probe_render(fn):
    p = L.probe_signal()
    n = L.PROBE_N
    return lambda v: L.probe_response(np.tile(apply_db(p[:n].astype(float), fn, v), L.PROBE_PERIODS))


def guitarish(seed, seconds=4.0):
    """Plucked harmonic notes with a decaying envelope: different notes for different seeds."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    y = np.zeros_like(t)
    for start in np.arange(0, seconds, 0.5):
        f0 = 82.4 * 2 ** (rng.integers(0, 24) / 12)
        env = np.where(t >= start, np.exp(-(t - start) * 3.0), 0.0)
        for h in range(1, 40):
            if f0 * h < 15000:
                y += env * np.sin(2 * np.pi * f0 * h * t + rng.random() * 6.28) / h
    y += 1e-3 * rng.standard_normal(t.size)   # pick and string noise: some energy in every band
    return (0.1 * y / np.max(np.abs(y))).astype(np.float64)


# ---------------------------------------------------------------- probe
def test_probe_identity_is_flat():
    p = L.probe_signal()
    r = L.probe_response(p)
    assert np.max(np.abs(r)) < 1e-6
    assert abs(20 * np.log10(np.sqrt(np.mean(p.astype(float) ** 2))) - L.PROBE_RMS_DB) < 0.01


def test_probe_matches_a_causal_filter():
    sos = butter(2, 500, "highpass", fs=SR, output="sos")
    r = L.probe_response(sosfilt(sos, L.probe_signal().astype(float)))
    from scipy.signal import sosfreqz
    _, h = sosfreqz(sos, worN=L.GRID, fs=SR)
    sel = L.GRID > 100
    assert np.max(np.abs(r[sel] - 20 * np.log10(np.abs(h[sel])))) < 0.3


# ---------------------------------------------------------------- transfer: invariance to note content
def test_transfer_is_invariant_to_the_notes():
    b, a = butter(2, [300, 3000], "bandpass", fs=SR)
    x1, x2 = guitarish(1), guitarish(2)
    t1, t2 = L.transfer(lfilter(b, a, x1), x1), L.transfer(lfilter(b, a, x2), x2)
    w = np.minimum(t1.weight, t2.weight)
    assert L.response_distance(t1.mag_db, t2.mag_db, w) < 0.3
    # and both match the probe's measurement of the same filter
    r = L.probe_response(lfilter(b, a, L.probe_signal().astype(float)))
    assert L.response_distance(t1.mag_db, r, t1.weight) < 0.5


def test_incoherent_fraction_separates_linear_from_nonlinear():
    x = guitarish(3)
    b, a = butter(2, 2000, fs=SR)
    lin = L.transfer(lfilter(b, a, x), x)
    drive = L.transfer(np.tanh(20 * x), x)
    assert lin.incoherent_db < -40
    assert drive.incoherent_db > lin.incoherent_db + 20


def test_family_score_zero_for_same_filter_and_grows_with_difference():
    sp = L.LinearSpecialist(tables={})
    x = guitarish(4)
    y = apply_db(x, fake_eq_db, {"Freq": 800.0, "Gain": 6.0, "LowCut": 100.0})
    same = apply_db(x, fake_eq_db, {"Freq": 800.0, "Gain": 6.0, "LowCut": 100.0})
    near = apply_db(x, fake_eq_db, {"Freq": 800.0, "Gain": 4.0, "LowCut": 100.0})
    far = apply_db(x, fake_eq_db, {"Freq": 800.0, "Gain": -6.0, "LowCut": 100.0})
    assert sp.score(y, same, x) < 0.05
    assert sp.score(y, same, x) < sp.score(y, near, x) < sp.score(y, far, x)


# ---------------------------------------------------------------- tables and fit
@pytest.fixture(scope="module")
def tables():
    eq = L.build_table(probe_render(fake_eq_db), "FakeEQ", "eq", EQ_RANGES, EQ_DEFAULTS, n_check=4)
    gr = L.build_table(probe_render(fake_graphic_db), "FakeGraphic", "eq", GR_RANGES, GR_DEFAULTS, n_check=4)
    return {"FakeEQ": eq, "FakeGraphic": gr}


def test_groups_found_from_renders(tables):
    eq = tables["FakeEQ"]
    assert sorted(map(sorted, eq.groups)) == [["Freq", "Gain"], ["LowCut"]]
    assert all(len(g) == 1 for g in tables["FakeGraphic"].groups)
    assert eq.meta["check_rms_db"] < 0.3
    assert tables["FakeGraphic"].meta["check_max_db"] < 0.01   # additive model is exact here


@pytest.mark.parametrize("seed", range(4))
def test_fit_recovers_knobs(tables, seed):
    rng = np.random.default_rng(seed)
    t = tables["FakeEQ"]
    ks = {k: int(rng.integers(13, 115)) for k in t.knobs}
    vals = t.values({k: v / L.STEPS for k, v in ks.items()})
    target = probe_render(fake_eq_db)(vals)
    w = np.ones(L.GRID.size) * ((L.GRID >= 40) & (L.GRID <= 16000))
    f = L.fit(t, target, w)
    assert f.fit_db < 0.5
    assert abs(f.knobs["LowCut"] - ks["LowCut"]) <= 3
    if abs(vals["Gain"]) > 3:          # a nearly flat peak has no identifiable frequency
        assert abs(f.knobs["Gain"] - ks["Gain"]) <= 4
        assert abs(f.knobs["Freq"] - ks["Freq"]) <= 6


def test_table_round_trip(tmp_path, tables):
    t = tables["FakeEQ"]
    t.save(tmp_path)
    t2 = L.ResponseTable.load(tmp_path, "FakeEQ")
    u = {"Freq": 0.3, "Gain": 0.7, "LowCut": 0.2}
    assert np.allclose(t.predict(u), t2.predict(u))
    assert L.load_tables(d=tmp_path).keys() == {"FakeEQ"}


def test_rank_prefers_fewer_knobs_on_a_tie():
    def mk(m, db, n):
        return L.Fit(m, "eq", {}, {}, db, n, np.zeros(L.GRID.size))
    r = L.rank([mk("Big", 0.02, 11), mk("Small", 0.05, 2), mk("Bad", 1.0, 1)])
    assert [f.model for f in r] == ["Small", "Big", "Bad"]


def test_specialist_seed_hint_and_refine(tables):
    sp = L.LinearSpecialist(tables=tables)
    x = guitarish(5)
    vals = tables["FakeGraphic"].values({"B0": 0.2, "B1": 0.8, "B2": 0.35, "B3": 0.6})
    y = apply_db(x, fake_graphic_db, vals)
    seeds = sp.seed(y, x)
    assert seeds[0].model == "FakeGraphic" and seeds[0].fit_db < 0.3
    h = sp.screen_hint(y, x)
    assert h.family == "linear" and h.family_prob > 0.9 and h.shortlist[0] == "FakeGraphic"
    assert h.category_probs["eq"] == pytest.approx(1.0)
    fns = {"FakeEQ": fake_eq_db, "FakeGraphic": fake_graphic_db}
    calls = []

    def render_probe(model, values):
        calls.append(model)
        p = L.probe_signal().astype(float)
        return np.tile(apply_db(p[:L.PROBE_N], fns[model], values), L.PROBE_PERIODS)
    fits, used = sp.refine(render_probe, y, x, top=2, corrections=1, on="probe")
    assert used == len(calls) <= 4
    assert fits[0].model == "FakeGraphic" and fits[0].rendered
    assert all(abs(fits[0].knobs[k] - round(u * 127)) <= 2
               for k, u in {"B0": 0.2, "B1": 0.8, "B2": 0.35, "B3": 0.6}.items())


def test_nonlinear_slot_gets_a_low_family_prob(tables):
    sp = L.LinearSpecialist(tables=tables)
    x = guitarish(6)
    assert sp.screen_hint(np.tanh(30 * x), x).family_prob < 0.1


def test_refine_on_the_dry_scores_the_right_setting_zero(tables):
    sp = L.LinearSpecialist(tables=tables)
    x = guitarish(7)
    u = {"B0": 0.6, "B1": 0.3, "B2": 0.75, "B3": 0.45}
    vals = tables["FakeGraphic"].values({k: round(v * 127) / 127 for k, v in u.items()})
    y = apply_db(x, fake_graphic_db, vals)
    fns = {"FakeEQ": fake_eq_db, "FakeGraphic": fake_graphic_db}
    fits, used = sp.refine(lambda m, v: apply_db(x, fns[m], v), y, x, top=2, corrections=2)
    assert used <= 6 and fits[0].model == "FakeGraphic" and fits[0].rendered
    assert fits[0].fit_db < 0.05


def test_lti_check_flags_level_and_time_dependence():
    sos = butter(2, 1000, fs=SR, output="sos")
    assert L.lti_check(lambda x: sosfilt(sos, x))["lti"]
    drive = L.lti_check(lambda x: np.tanh(30 * x))
    assert not drive["lti"] and drive["lti_level_db"] > L.LTI_TOL_DB
    t = np.arange(L.PROBE_N * L.PROBE_PERIODS) / SR
    trem = L.lti_check(lambda x: x * (1 + 0.5 * np.sin(2 * np.pi * 1.3 * t[: x.size])))
    assert not trem["lti"] and trem["lti_period_db"] > L.LTI_TOL_DB


# ---------------------------------------------------------------- the dispatch adapter
class FilterEngine:
    """Renders the fake linear models by model id, physical knob values in the preset."""

    def __init__(self):
        self.p, self.params, self.renders = None, {}, 0

    def load(self, preset):
        self.p = preset
        self.params = dict(preset.slots[0].params)

    def set_params(self, ch):
        self.params.update({k: v for (_, k), v in ch.items()})

    def render(self, dry):
        self.renders += 1
        fn = {"FakeEQ": fake_eq_db, "FakeGraphic": fake_graphic_db}[self.p.slots[0].model_id]
        return apply_db(np.asarray(dry, float), fn, self.params).astype(np.float32)


def fake_candidates():
    from tonematch.preset import Preset, Slot
    from tonematch.search.model_choice import ModelCandidate, Setting
    from tonematch.search.space import ParamSpace

    out = []
    for m, rng, dflt in (("FakeEQ", EQ_RANGES, EQ_DEFAULTS), ("FakeGraphic", GR_RANGES, GR_DEFAULTS)):
        p = Preset("x", [Slot("eq", m, dict(dflt), True)])
        refs = [(0, k) for k in sorted(rng)]
        sp = ParamSpace.from_preset(p, refs, {(0, k): rng[k] for k in rng}, 128)
        out.append(ModelCandidate(m, "eq", p, sp, [Setting("default", {})]))
    return out


def test_adapter_hint_seed_and_for_model(tables):
    fam = L.make_specialist(spec=L.LinearSpecialist(tables=tables))
    assert fam.family == "linear" and fam.categories == ("cab", "eq") and fam.score(None, None) is None
    x = guitarish(8)
    vals = tables["FakeGraphic"].values({"B0": 0.3, "B1": 0.7, "B2": 0.55, "B3": 0.2})
    y = apply_db(x, fake_graphic_db, vals)
    assert fam.screen_hint(y, x) > 0.9
    fs = fam.seed(y, x)
    assert fs.family == "linear" and fs.category_probs["eq"] > 0.9
    sd = fs.for_model("FakeGraphic", "eq", {k: GR_RANGES[k] for k in GR_RANGES})
    assert set(sd) == set(GR_RANGES) and all(abs(sd[k].value - vals[k]) < 0.5 for k in sd)
    assert fs.for_model("NotTabled", "eq", {}) == {}
    assert fam.refiner(np.tanh(30 * x), x) is None       # not linear: the general halving keeps the refine stage


def test_adapter_refiner_in_the_hybrid_stops_early_on_an_exact_match(tables):
    from tonematch.search import Budget, Section
    from tonematch.search.hybrid import choose_models_hybrid
    from tonematch.search.specialists import build_dispatch
    from tonematch.search.stubs import SpectralStubLoss

    x = guitarish(9, seconds=2.0).astype(np.float32)
    vals = tables["FakeEQ"].values({"Freq": 70 / 127, "Gain": 100 / 127, "LowCut": 30 / 127})
    y = apply_db(x, fake_eq_db, vals).astype(np.float32)
    fam = L.make_specialist(spec=L.LinearSpecialist(tables=tables))
    cands = fake_candidates()
    disp = build_dispatch(["linear"], y, x, [(c.model, c.category) for c in cands], specialists={"linear": fam})
    eng = FilterEngine()
    bud = Budget(max_renders=40, clip_s=2.0, short_s=2.0, seed=1, weights=None)
    ch = choose_models_hybrid(Section("s", "a", y, x), eng, SpectralStubLoss(), cands, bud, f=0.25, k=2,
                              is_tv=lambda m: False, dispatch=disp)
    assert ch.best.model == "FakeEQ"
    assert any("general halving skipped" in n for n in ch.notes)
    assert ch.rungs["refine"] <= 8 and eng.renders <= 10 + 8


def test_the_dispatch_slot_builds_the_linear_specialist(monkeypatch):
    """What search's `family_adapters.LINEAR_ADAPTER` slot gets once filled with `make_specialist`."""
    from tonematch.search import family_adapters as FA
    from tonematch.search.specialists import Specialist

    monkeypatch.setattr(FA, "LINEAR_ADAPTER", L.make_specialist)
    sp = FA.build_specialists(["linear"], time_score_weight=0.25)["linear"]    # other families' opts are ignored
    assert isinstance(sp, L.LinearFamily) and isinstance(sp, Specialist)
    assert sp.family == "linear" and sp.budget_share == 1.0 and hasattr(sp, "refiner")
