"""Model choice (T050) on a fake engine with three one-block models and fake fingerprints."""
from __future__ import annotations

import numpy as np

from tonematch.engines.base import EngineError, render_many
from tonematch.preset import Preset, Slot
from tonematch.search import Budget, ParamSpace, Section
from tonematch.search.model_choice import (
    ModelCandidate,
    Setting,
    choose_models,
    fingerprint_settings,
    halve,
    rung_plan,
    screen,
    setting_order,
)
from tonematch.search.stubs import SpectralStubLoss, fake_dry
from tonematch.search.types import Plan

# model -> (gain scale, lowpass pole): three models that sound different.
MODELS = {"m_clean": (0.3, 0.05), "m_crunch": (1.0, 0.4), "m_fuzz": (3.0, 0.7)}


class ModelEngine:
    """Renders tanh drive + lowpass whose character depends on the model id. Counts renders."""

    def __init__(self):
        self.renders, self.loads, self.many_calls = 0, 0, 0
        self.p: Preset | None = None
        self.params: dict = {}

    def load(self, preset):
        self.p, self.loads = preset, self.loads + 1
        self.params = {(0, k): v for k, v in preset.slots[0].params.items()}

    def set_params(self, ch):
        self.params.update(ch)

    def _render(self, model, params, dry):
        from scipy.signal import lfilter
        g, a = MODELS[model]
        self.renders += 1
        x = np.tanh(g * (1 + 20 * params[(0, "Gain")]) * np.asarray(dry, np.float64))
        y = lfilter([1 - a], [1, -a], x) * (0.5 + params[(0, "Level")])
        if not np.all(np.isfinite(y)):
            raise EngineError("bad")
        return y.astype(np.float32)

    def render(self, dry):
        return self._render(self.p.slots[0].model_id, self.params, dry)

    def render_many(self, dry, variants):
        self.many_calls += 1
        return [self._render(v.slots[0].model_id, {(0, k): x for k, x in v.slots[0].params.items()}, dry)
                for v in variants]

    def cost(self):
        raise NotImplementedError

    def capabilities(self):
        raise NotImplementedError

    def close(self):
        pass


def cand(model, settings=None):
    p = Preset("x", [Slot("drive", model, {"Gain": 0.5, "Level": 0.5}, True)])
    sp = ParamSpace.from_preset(p, [(0, "Gain")], None, None)
    return ModelCandidate(model, "drive", p, sp, settings or [Setting("default", {})])


FP = {"settings": [{"setting": "Gain@24", "params": {"Gain": 0.19}}, {"setting": "default", "params": {}},
                   {"setting": "Gain@103", "params": {"Gain": 0.81}}, {"setting": "Gain@64", "params": {"Gain": 0.5}}]}


def fps(model):
    return fingerprint_settings(model, FP)


def section(model="m_crunch", gain=0.8):
    dry = fake_dry(1.0)
    e = ModelEngine()
    e.load(cand(model).preset_at({(0, "Gain"): gain}))
    return Section("s", "a", e.render(dry), dry)


NOT_TV = lambda m: False
BUD = {"clip_s": 1.0, "short_s": 1.0, "seed": 1, "weights": None}


def test_setting_order_default_centre_corners():
    assert setting_order(["Gain@24", "default", "Gain@103", "Gain@64"]) == [1, 3, 0, 2]
    labels = ["default", "A@24,B@24", "A@24,B@103", "A@103,B@24", "A@103,B@103", "A@64,B@64"]
    assert setting_order(labels)[:2] == [0, 5]
    assert [s.label for s in fingerprint_settings("m", FP, cap=2)] == ["default", "Gain@64"]
    assert fingerprint_settings("m", FP)[2].values == {(0, "Gain"): 0.19}
    assert [s.label for s in fingerprint_settings("m", None)] == ["default"]


def test_screen_ranks_true_model_first_and_counts_renders():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    sec = section("m_crunch", 0.81)   # the truth sits exactly on the model's Gain@103 setting
    sc, n, _ = screen(eng, SpectralStubLoss(), sec, cs, None, 1.0)
    assert n == eng.renders == 12 and eng.many_calls == 1   # 3 models x 4 settings, one render_many
    best = min(sc, key=lambda s: s.screen)
    assert best.model == "m_crunch" and best.setting == "Gain@103" and best.screen < 1e-3
    assert best.start == {(0, "Gain"): 0.81}


def test_screen_settings_cap_and_invalid_not_rendered():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    _, n, _ = screen(eng, SpectralStubLoss(), section(), cs, None, 1.0, settings_per_model=2)
    assert n == 6
    eng = ModelEngine()
    _, n, notes = screen(eng, SpectralStubLoss(), section(), cs, None, 1.0,
                         validator=lambda p: ["bad"] if p.slots[0].model_id == "m_fuzz" else [])
    assert n == 8 and any("m_fuzz" in x for x in notes)


def test_warm_start_is_the_nearest_fingerprint_setting():
    c = cand("m_crunch", fps("m_crunch"))
    x = c.start_vector({(0, "Gain"): 0.81})
    assert abs(x[0] - 0.81) < 1e-9
    p = c.preset_at({(0, "Gain"): 0.81})
    assert p.slots[0].params["Gain"] == 0.81 and c.preset.slots[0].params["Gain"] == 0.5   # base untouched
    eng = ModelEngine()
    ch = choose_models(section("m_crunch", 0.81), eng, SpectralStubLoss(), [c], Budget(max_renders=40, **BUD),
                       k=1, is_tv=NOT_TV)
    # the first refine score is at the warm start, not at the catalog default 0.5
    assert ch.best.setting == "Gain@103" and ch.best.rungs[0]["score"] < 0.05


def test_rung_plan_halves_to_one():
    assert rung_plan(8) == [8, 4, 2, 1] and rung_plan(5) == [5, 3, 2, 1] and rung_plan(1) == [1]
    assert rung_plan(6, eta=3) == [6, 2, 1]


def test_successive_halving_gives_survivors_more_budget():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    sec = section("m_fuzz", 0.4)
    kit, bud = SpectralStubLoss(), Budget(max_renders=120, **BUD)
    scores, _n, _ = screen(eng, kit, sec, cs, None, 1.0)
    plan = Plan("native", 120, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    spent, _ = halve(eng, kit, sec, {c.model: c for c in cs}, scores, bud, plan, 90.0, k=3, eta=2, min_per_model=4)
    rungs = {s.model: s.rungs for s in scores if s.rungs}
    assert len(rungs) == 3
    winners = [m for m, r in rungs.items() if len(r) == 3]
    assert len(winners) == 1 and [r["survivors"] for r in rungs[winners[0]]] == [3, 2, 1]
    assert sum(1 for r in rungs.values() if len(r) == 2) == 1 and sum(1 for r in rungs.values() if len(r) == 1) == 1
    assert 60 <= spent <= 90 + 3 * 12   # near the rung budget, overshoot of at most a generation per model


def test_choose_models_finds_the_true_model_within_budget():
    sec = section("m_fuzz", 0.4)   # Gain 0.4 is not on any fingerprint setting
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    ch = choose_models(sec, eng, SpectralStubLoss(), cs, Budget(max_renders=100, **BUD), k=2, is_tv=NOT_TV)
    assert ch.best.model == "m_fuzz" and ch.best.refined < 0.3
    assert ch.screen_renders == 12                 # 4 settings x 3 models fit in half the budget
    assert ch.renders <= 100 + 2 * 12              # budget accounting: screen + refine, small overshoot
    assert eng.renders >= ch.renders * 0.9
    refined = [s.model for s in ch.ranked if s.refined is not None]
    assert len(refined) == 2 and ch.ranked[0].model == refined[0]


def test_screen_fraction_cuts_settings_when_the_budget_is_small():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    ch = choose_models(section("m_clean", 0.3), eng, SpectralStubLoss(), cs, Budget(max_renders=12, **BUD),
                       k=2, is_tv=NOT_TV, screen_frac=0.5)
    assert ch.screen_renders == 6          # 12 * 0.5 / 3 models = 2 settings each
    assert ch.best.model in MODELS


def test_time_variant_models_are_excluded():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    ch = choose_models(section("m_clean", 0.3), eng, SpectralStubLoss(), cs, Budget(max_renders=60, **BUD),
                       k=2, is_tv=lambda m: m == "m_clean")
    assert all(s.model != "m_clean" for s in ch.ranked)
    assert any("time-variant" in n for n in ch.notes)


def test_render_many_helper_falls_back_to_serial_for_plain_engines():
    class Plain(ModelEngine):
        render_many = None   # type: ignore[assignment]
    e = Plain()
    del Plain.render_many
    cs = [cand(m) for m in MODELS]
    ys = render_many(e, fake_dry(0.5), [c.preset_at({}) for c in cs])
    assert len(ys) == 3 and e.renders == 3
