"""Measured seeding before screening (T113) on fake engines: curves, inversion, banded space, ordering,
and the seeded model choice finding a delay that default-setting screening ranks badly, at the same budget."""
from __future__ import annotations

import numpy as np

from tonematch.preset import Preset, Slot
from tonematch.search import Budget, ParamSpace, Section
from tonematch.search.measured_seed import (
    BandedSpace,
    Measured,
    calibrate_model,
    category_seeded,
    choose_models_seeded,
    groups_for,
    invert,
    load_calibration,
    save_calibration,
    seeded_candidate,
    seeds_for,
    usage_counts,
    usage_rank,
)
from tonematch.search.model_choice import ModelCandidate, Setting, choose_models
from tonematch.search.stubs import StubScore

SR = 48_000
TIME_RANGE = (0.0, 1.27)


class DelayEngine:
    """m_delay: dry + Mix * dry delayed by Time seconds; m_gain: dry * Gain; m_tone: dry * Level."""

    def __init__(self):
        self.renders, self.p, self.params = 0, None, {}

    def load(self, preset):
        self.p = preset
        self.params = {(0, k): v for k, v in preset.slots[0].params.items()}

    def set_params(self, ch):
        self.params.update(ch)

    def _render(self, model, params, dry):
        self.renders += 1
        x = np.asarray(dry, np.float64)
        if model == "m_delay":
            d = min(round(params[(0, "Time")] * SR), len(x) - 1)
            y = x.copy()
            y[d:] += params[(0, "Mix")] * x[: len(x) - d]
            return y.astype(np.float32)
        return (x * params[(0, "Gain")]).astype(np.float32)

    def render(self, dry):
        return self._render(self.p.slots[0].model_id, self.params, dry)

    def render_many(self, dry, variants):
        return [self._render(v.slots[0].model_id, {(0, k): x for k, x in v.slots[0].params.items()}, dry)
                for v in variants]

    def close(self):
        pass


class MseKit:
    def score(self, target, candidate, *, stems=None, mask=None, weights=None):
        t, c = np.asarray(target, float), np.asarray(candidate, float)
        n = min(len(t), len(c))
        d = float(np.mean((t[:n] - c[:n]) ** 2) / (np.mean(t[:n] ** 2) + 1e-9))
        return StubScore(d, {"mse": d})


def delay_cand(model="m_delay", category="delay"):
    p = Preset("x", [Slot(category, model, {"Time": 0.1, "Mix": 0.5}, True)])
    sp = ParamSpace.from_preset(p, [(0, "Time"), (0, "Mix")], {(0, "Time"): TIME_RANGE}, 128)
    fp = [Setting("default", {}), Setting("Time@64", {(0, "Time"): 0.63}), Setting("Time@103", {(0, "Time"): 1.03})]
    return ModelCandidate(model, category, p, sp, fp)


def gain_cand(model, category="drive"):
    p = Preset("x", [Slot(category, model, {"Gain": 1.0}, True)])
    sp = ParamSpace.from_preset(p, [(0, "Gain")], {(0, "Gain"): (0.0, 2.0)}, 128)
    return ModelCandidate(model, category, p, sp, [Setting("default", {}), Setting("Gain@64", {(0, "Gain"): 1.0})])


CAL = {"m_delay": [{"knobs": ["Time"], "quantities": ["delay.time_s"], "half_width": [0.04],
                    "range": [[0.0, 1.27]], "shape": [5],
                    "x": [[0.127], [0.381], [0.635], [0.889], [1.143]],
                    "q": [[0.127], [0.381], [0.635], [0.889], [1.143]]}]}


def line(xs, qs):
    return {"quantities": ["q"], "x": [[x] for x in xs], "q": [[q] for q in qs]}


def test_invert_one_knob_interpolates_clips_and_refuses_uninformative_curves():
    c = CAL["m_delay"][0]
    assert abs(invert(c, [0.5])[0] - 0.5) < 1e-9
    assert invert(c, [9.0]) == [1.143] and invert(c, [-1.0]) == [0.127]
    assert invert(c, [float("nan")]) is None and invert(c, [1.0, 2.0]) is None
    assert invert(line([0, 1, 2, 3], [5, 5, 5, 5]), [5.0]) is None                                  # flat
    assert invert(line([0, 1, 2, 3], [float("nan")] * 3 + [1.0]), [1.0]) is None                    # too few points
    assert invert(line([0, 1, 2, 3, 4], [1, 4, 2, 5, 3]), [3.0]) is None                            # not monotone
    assert abs(invert(line([0, 1, 2, 3], [9, 6, 4, 1]), [5.0])[0] - 1.5) < 1e-9                      # falling curves work


def two_knob_group(f1, f2, qn=("reverb.rt60_s", "reverb.late_db")):
    g = (0.1, 0.3, 0.5, 0.7, 0.9)
    pts = [(a, b) for a in g for b in g]
    return {"knobs": ["A", "B"], "quantities": list(qn), "shape": [5, 5], "x": [list(p) for p in pts],
            "q": [[f1(a, b), f2(a, b)] for a, b in pts]}


def test_invert_two_knobs_separates_what_one_quantity_confounds():
    # rt60 grows with both knobs (as measured RT60 grows with Decay and with Mix); late level grows with B only
    grp = two_knob_group(lambda a, b: 0.5 * np.exp(2.0 * a + 1.0 * b), lambda a, b: -30 + 40 * b)
    for a, b in [(0.2, 0.7), (0.8, 0.3), (0.55, 0.55)]:
        got = invert(grp, [0.5 * np.exp(2 * a + b), -30 + 40 * b])
        assert abs(got[0] - a) < 0.03 and abs(got[1] - b) < 0.03
    # a missing quantity or an all-NaN surface cannot seed
    assert invert(grp, [1.0, float("nan")]) is None
    flat = two_knob_group(lambda a, b: 1.0, lambda a, b: 2.0)
    assert invert(flat, [1.0, 2.0]) is None


def test_groups_pick_the_quantities_of_each_knob():
    def g(cat, model, params):
        return [[(p, q) for p, q, _ in grp] for grp in groups_for(cat, model, params)]

    assert g("delay", "HD2_DelaySimpleDelay", ["Time", "Feedback", "Mix", "HighCut"]) == [
        [("Time", "delay.time_s")], [("Feedback", "delay.fb_db"), ("Mix", "delay.level_db")]]
    assert g("delay", "m", ["Time", "Mix"]) == [[("Time", "delay.time_s")], [("Mix", "delay.level_db")]]
    assert g("reverb", "HD2_ReverbPlate", ["Decay", "Mix", "Predelay", "LowCut"]) == [
        [("Decay", "reverb.rt60_s"), ("Mix", "reverb.late_db")], [("Predelay", "reverb.predelay_ms")]]
    assert g("mod", "HD2_TremoloTremolo", ["Speed", "Intensity", "DutyCycle"]) == [
        [("Speed", "mod.rate_hz")], [("Intensity", "mod.amp_depth_db")]]
    assert g("mod", "HD2_VibratoBubbleVibrato", ["Speed", "Depth", "Mix"]) == [
        [("Speed", "mod.rate_hz")], [("Depth", "mod.pitch_cents")]]
    assert g("drive", "HD2_DistScream808", ["Time", "Gain"]) == []
    t = {p: w for grp in groups_for("delay", "m", ["Time", "Feedback", "Mix"]) for p, _, w in grp}
    assert t["Time"] < t["Feedback"]                                                               # time is tighter


def test_category_gate():
    assert category_seeded("delay", {}) and category_seeded("delay", {"delay": 0.5})
    assert not category_seeded("delay", {"delay": 0.01, "reverb": 0.9})
    assert not category_seeded("drive", {})


def test_calibrate_model_records_groups(tmp_path, monkeypatch):
    import tonematch.search.measured_seed as MS

    monkeypatch.setattr(MS, "measure", lambda y, dry, with_probs=True: Measured(
        {"delay.time_s": float(y[0]), "delay.fb_db": float(y[1]), "delay.level_db": float(y[2])}))
    cal = calibrate_model(lambda v: np.array([v["Time"], v["Feedback"], v["Mix"]]), np.zeros(4), "m", "delay",
                          {"Time": (0.0, 1.0), "Feedback": (0.0, 1.0), "Mix": (0.0, 1.0), "HighCut": (0.0, 1.0)},
                          {"Time": 0.1, "Feedback": 0.2, "Mix": 0.3, "HighCut": 0.5})
    assert [g["knobs"] for g in cal] == [["Time"], ["Feedback", "Mix"]]
    assert len(cal[0]["x"]) == 5 and len(cal[1]["x"]) == 25 and cal[1]["shape"] == [5, 5]
    assert abs(invert(cal[0], [0.42])[0] - 0.42) < 1e-9
    # fb_db follows Feedback, level_db follows Mix: the 2-D inversion recovers both knobs
    got = invert(cal[1], [0.62, 0.31])
    assert abs(got[0] - 0.62) < 0.006 and abs(got[1] - 0.31) < 0.006
    save_calibration({"m": cal}, tmp_path / "c.json")
    assert load_calibration(tmp_path / "c.json")["m"][0]["quantities"] == ["delay.time_s"]
    assert load_calibration(tmp_path / "missing.json") == {}


def test_banded_space_maps_the_cube_to_a_narrow_band_and_snaps_to_the_full_grid():
    c = delay_cand()
    sp = BandedSpace.around(c.space, {0: (0.635, 0.04)})
    lo, hi = sp.band_lo[0], sp.band_hi[0]
    assert abs((hi - lo) - 2 * 0.04 * 1.27) < 1e-9 and lo < 0.635 < hi
    assert sp.band_lo[1] == 0.0 and sp.band_hi[1] == 1.0                                   # Mix: full range
    v = sp.from_unit(np.array([0.0, 0.5]))
    assert abs(v[0] - lo) < 1.27 / 127 and v[1] > 0
    assert 0.0 <= sp.to_unit(np.array([0.635, 0.5]))[0] <= 1.0
    # beyond the band: clipped to the edge, never outside the full range
    assert sp.from_unit(np.array([5.0, 5.0]))[0] <= sp.hi[0]
    # values land on the template's CC grid k/127 of the full range
    g = (sp.from_unit(np.array([0.37, 0.5]))[0] - sp.lo[0]) / 1.27 * 127
    assert abs(g - round(g)) < 1e-6


def test_seeded_candidate_overrides_settings_and_bands_the_space():
    c = delay_cand()
    seeds = seeds_for("m_delay", "delay", CAL, Measured({"delay.time_s": 0.64}), {"Time": TIME_RANGE, "Mix": (0, 1)})
    assert abs(seeds["Time"].value - 0.64) < 1e-9
    sc = seeded_candidate(c, seeds)
    assert sc.settings[0].label == "seed" and sc.settings[0].values == {(0, "Time"): seeds["Time"].value}
    assert [s.label for s in sc.settings[1:]] == ["Time@64+seed", "Time@103+seed"]
    assert all(s.values[(0, "Time")] == seeds["Time"].value for s in sc.settings)
    assert isinstance(sc.space, BandedSpace) and c.space.__class__ is ParamSpace             # original untouched
    assert seeded_candidate(c, {}) is None
    assert seeds_for("m_other", "delay", CAL, Measured({"delay.time_s": 0.64}), {}) == {}
    assert seeds_for("m_delay", "delay", CAL, Measured({}), {"Time": TIME_RANGE}) == {}      # nothing measured


def test_usage_rank_orders_by_count_and_keeps_given_order_for_ties(tmp_path):
    r = usage_rank(["a", "b", "c", "d"], {"c": 9, "a": 1})
    assert [m for m, _ in sorted(r.items(), key=lambda kv: kv[1])] == ["c", "a", "b", "d"]
    f = tmp_path / "u.json"
    f.write_text('{"amp": {"ranked": [["m1", 5, 5], ["m2", 2, 2]]}, "drive": {"ranked": [["m1", 1, 1]]}}')
    assert usage_counts(f) == {"m1": 6.0, "m2": 2.0} and usage_counts(tmp_path / "x.json") == {}


def make_pool():
    return [gain_cand("m_g1"), gain_cand("m_g2"), gain_cand("m_g3"), delay_cand()]


def make_section(time=0.64, mix=0.5):
    rng = np.random.default_rng(3)
    dry = np.zeros(SR)
    dry[:2400] = rng.standard_normal(2400) * 0.5
    dry[24000:25000] = rng.standard_normal(1000) * 0.5
    e = DelayEngine()
    e.load(delay_cand().preset_at({(0, "Time"): time, (0, "Mix"): mix}))
    return Section("s", "a", e.render(dry), dry)


RANGES = {"m_delay": {"Time": TIME_RANGE, "Mix": (0.0, 1.0)}}
BUD = {"clip_s": 1.0, "short_s": 1.0, "seed": 1, "weights": None}


def test_seeded_screen_puts_the_true_delay_first_where_default_screening_cannot():
    sec = make_section(0.64)
    pool_defaults = make_pool()
    # default-setting screening: the delay sits at Time 0.1 (and its fingerprint grid misses 0.64)
    eng0 = DelayEngine()
    plain = choose_models(sec, eng0, MseKit(), pool_defaults, Budget(max_renders=60, **BUD), k=1,
                          screen_frac=0.2, settings_per_model=1, is_tv=lambda m: False)
    dscore = {s.model: s.screen for s in plain.ranked}
    meas = Measured({"delay.time_s": 0.64}, {"delay": 0.9, "drive": 0.1})
    eng = DelayEngine()
    ch = choose_models_seeded(sec, eng, MseKit(), make_pool(), Budget(max_renders=60, **BUD), meas, CAL, RANGES,
                              k=1, screen_frac=0.3, is_tv=lambda m: False)
    sscore = {s.model: s.screen for s in ch.ranked}
    assert sscore["m_delay"] < 0.2 * dscore["m_delay"]            # the seeded setting is a far better start
    assert ch.best.model == "m_delay" and ch.best.screen == min(sscore.values())
    assert abs(ch.seeds["m_delay"]["Time"]["value"] - 0.64) < 1e-9
    assert ch.rungs["A_default"] == 3 and ch.rungs["A_seed"] == 1       # the seed replaces the delay's default render


def test_seeded_choice_stays_within_the_render_budget_and_refines_inside_the_band():
    sec = make_section(0.64)
    eng = DelayEngine()
    meas = Measured({"delay.time_s": 0.64}, {})
    ch = choose_models_seeded(sec, eng, MseKit(), make_pool(), Budget(max_renders=120, **BUD), meas, CAL, RANGES,
                              k=1, screen_frac=0.4, is_tv=lambda m: False)
    old = choose_models(sec, DelayEngine(), MseKit(), make_pool(), Budget(max_renders=120, **BUD), k=1,
                        screen_frac=0.4, is_tv=lambda m: False)
    assert ch.renders == eng.renders and ch.renders <= old.renders + 3     # same budget as T050 (halve's own slack)
    assert ch.rungs["A_default"] + ch.rungs["A_seed"] + ch.rungs["B"] <= 0.4 * 120 + ch.rungs["A_seed"]
    best = ch.best
    assert best.model == "m_delay" and best.refined is not None
    t = float(best.values[0])
    assert abs(t - 0.64) <= 0.04 * 1.27 + 1.27 / 127            # inside the +-4 % band of the seed
    assert best.refined < 0.01     # the Mix grid snap costs ~1e-5; no worse than a hair off the exact seed


def test_no_measurement_falls_back_to_plain_screen_and_halving():
    sec = make_section(0.64)
    eng = DelayEngine()
    ch = choose_models_seeded(sec, eng, MseKit(), make_pool(), Budget(max_renders=60, **BUD), Measured(), CAL, RANGES,
                              k=2, screen_frac=0.4, is_tv=lambda m: False)
    assert ch.seeds == {} and ch.rungs["A_seed"] == 0 and ch.best.model
    assert any("seeded 0 of 4" in n for n in ch.notes)


def test_second_rung_gives_every_model_its_remaining_settings():
    sec = make_section(0.64)
    eng = DelayEngine()
    meas = Measured({"delay.time_s": 0.64}, {})
    ch = choose_models_seeded(sec, eng, MseKit(), make_pool(), Budget(max_renders=100, **BUD), meas, CAL, RANGES,
                              k=1, screen_frac=0.5, rescreen_default=2, rescreen_seeded=1, is_tv=lambda m: False)
    assert ch.rungs["B"] == 5                                   # 3 gain models x 1 + delay x 2: all fit in the room
    d = next(s for s in ch.ranked if s.model == "m_delay")
    assert set(d.scores) == {"seed", "Time@64+seed", "Time@103+seed"}      # seeded knobs held in every setting
    g = next(s for s in ch.ranked if s.model == "m_g1")
    assert set(g.scores) == {"default", "Gain@64"}


def test_leftover_screen_renders_go_to_the_priority_models():
    sec = make_section(0.64)
    eng = DelayEngine()
    meas = Measured({"delay.time_s": 0.64}, {})
    # room for B = 9 - 4 = 5: one setting each for four models, the leftover one to the best-scoring (seeded) delay
    ch = choose_models_seeded(sec, eng, MseKit(), make_pool(), Budget(max_renders=100, **BUD), meas, CAL, RANGES,
                              k=1, screen_frac=0.09, rescreen_default=1, rescreen_seeded=1, is_tv=lambda m: False)
    assert ch.rungs["A_default"] + ch.rungs["A_seed"] + ch.rungs["B"] == 9
    d = next(s for s in ch.ranked if s.model == "m_delay")
    assert len(d.scores) == 3
    assert all(len(s.scores) == 2 for s in ch.ranked if s.model != "m_delay")
