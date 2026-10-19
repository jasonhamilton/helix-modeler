"""T176 knob tolerance on a fake drive engine with Drive and Tone knobs: early stop, coarse grid, breadth, old behaviour."""
from __future__ import annotations

import numpy as np
from test_model_choice import BUD, NOT_TV
from tonematch.engines.base import EngineError
from tonematch.preset import Preset, Slot
from tonematch.search import Budget, ParamSpace, Section
from tonematch.search.hybrid import (
    choose_models_hybrid,
    coarse_grid,
    coarse_settings,
    coarse_size,
)
from tonematch.search.loop import Evaluator, plan_budget, refine_chain
from tonematch.search.model_choice import ModelCandidate
from tonematch.search.stubs import SpectralStubLoss, fake_dry
from tonematch.search.types import Plan

N_MODELS = 12
MODELS = {f"m{i:02d}": (0.25 * 1.35 ** i, 0.05 + 0.07 * i) for i in range(N_MODELS)}


class DriveEngine:
    """tanh drive + one-pole lowpass; model sets gain scale and pole, Drive and Tone move them. Counts renders."""

    def __init__(self):
        self.renders = 0
        self.model, self.params = None, {}

    def load(self, preset):
        self.model = preset.slots[0].model_id
        self.params = {(0, k): v for k, v in preset.slots[0].params.items()}

    def set_params(self, ch):
        self.params.update(ch)

    def render(self, dry, model=None):
        from scipy.signal import lfilter
        g, a = MODELS[model or self.model]
        self.renders += 1
        p = self.params
        x = np.tanh(g * (1 + 20 * p[(0, "Drive")]) * np.asarray(dry, np.float64))
        a_eff = float(np.clip(a * (1.3 - 0.8 * p[(0, "Tone")]), 0.0, 0.95))
        y = lfilter([1 - a_eff], [1, -a_eff], x) * (0.5 + p[(0, "Level")])
        if not np.all(np.isfinite(y)):
            raise EngineError("bad")
        return y.astype(np.float32)

    def render_many(self, dry, presets):
        out = []
        for pr in presets:
            self.load(pr)
            out.append(self.render(dry))
        return out


def cand(model):
    p = Preset("x", [Slot("drive", model, {"Drive": 0.5, "Tone": 0.5, "Level": 0.5}, True)])
    sp = ParamSpace.from_preset(p, [(0, "Drive"), (0, "Tone")], None, None)
    return ModelCandidate(model, "drive", p, sp, [])


def sec(model="m06", drive=0.7, tone=0.4):
    dry = fake_dry(1.0)
    e = DriveEngine()
    e.load(cand(model).preset_at({(0, "Drive"): drive, (0, "Tone"): tone}))
    return Section("s", "a", e.render(dry), dry)


def run(tol, renders=400, k=2, truth="m06"):
    eng = DriveEngine()
    ch = choose_models_hybrid(sec(truth), eng, SpectralStubLoss(), [cand(m) for m in MODELS],
                              Budget(max_renders=renders, **BUD), f=0.5, k=k, is_tv=NOT_TV, knob_tol=tol)
    return ch, eng


def test_coarse_grid_is_three_drive_by_two_tone_centre_first():
    g = coarse_grid(cand("m00"))
    assert len(g) == 6 and coarse_size(cand("m00")) == 7
    drives = sorted({round(s.values[(0, "Drive")], 3) for s in g})
    tones = sorted({round(s.values[(0, "Tone")], 3) for s in g})
    assert drives == [0.2, 0.5, 0.8] and tones == [0.3, 0.7]
    assert (g[0].values[(0, "Drive")], g[0].values[(0, "Tone")]) == (0.5, 0.3)
    assert [s.label for s in coarse_settings(cand("m00"), 3, np.random.default_rng(0))] == ["start", "grid0", "grid1"]


def test_model_without_drive_or_tone_has_no_grid_and_falls_back_to_lhs():
    p = Preset("x", [Slot("drive", "z", {"Gain": 0.5}, True)])
    c = ModelCandidate("z", "drive", p, ParamSpace.from_preset(p, [(0, "Gain")], None, None), [])
    assert coarse_grid(c) == [] and coarse_size(c) == 0
    assert [s.label for s in coarse_settings(c, 3, np.random.default_rng(0))] == ["start", "lhs0", "lhs1"]


def test_refine_chain_stops_inside_the_tolerance_and_spends_less():
    section = sec()
    plan = Plan("native", 400, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    bud = Budget(max_renders=400, **BUD)
    out = {}
    for tol in (0.0, 0.15):
        eng = DriveEngine()
        c = cand("m06")
        eng.load(c.preset_at({}))
        ev = Evaluator(eng, SpectralStubLoss(), section, c.space, plan, bud)
        v, f, _, log = refine_chain(ev, np.array([0.5, 0.5]), 120, 0.3, 0, tol=tol)
        out[tol] = (ev.spent, f, v, log)
    assert out[0.0][0] >= 120 - 1                       # the old behaviour spends the cap
    assert out[0.15][0] < 0.6 * out[0.0][0] and any(st["stop"] for st in out[0.15][3])
    assert all(st["stop"] is None for st in out[0.0][3])
    assert abs(out[0.15][2][0] - 0.7) < 0.2             # still lands inside the tolerance band of the truth


def test_tol_zero_is_the_old_behaviour_draw_for_draw():
    a, ea = run(0.0)
    b = choose_models_hybrid(sec(), eb := DriveEngine(), SpectralStubLoss(), [cand(m) for m in MODELS],
                             Budget(max_renders=400, **BUD), f=0.5, k=2, is_tv=NOT_TV)
    assert ea.renders == eb.renders and a.best.model == b.best.model and a.best.refined == b.best.refined
    assert a.screen_renders == 200


def test_knob_tol_uses_fewer_renders_and_keeps_the_model_in_the_top_two():
    old, eo = run(0.0, renders=600, truth="m05")
    new, en = run(0.15, renders=600, truth="m05")
    assert "m05" in [s.model for s in old.ranked[:2]] and "m05" in [s.model for s in new.ranked[:2]]
    assert new.renders < 0.75 * old.renders and en.renders < 0.75 * eo.renders
    assert new.screen_renders == N_MODELS * 7           # the coarse grid: 7 renders per model
    assert new.best.refined < 0.1
    assert any("stopped early" in n for n in new.notes)


def test_breadth_refines_more_models_with_knob_tol():
    new, _ = run(0.15, k=2)
    assert sum(s.refined is not None for s in new.ranked) > 2


def test_truncated_grid_when_the_budget_is_small():
    new, en = run(0.15, renders=60)
    assert new.screen_renders <= 30 and en.renders <= 70


def test_expected_saving_report(capsys):
    """Fake-engine estimate: renders used per match, old vs new, over every true model (printed with -s)."""
    old_r = new_r = old_t2 = new_t2 = 0
    for truth in MODELS:
        o, _ = run(0.0, renders=600, truth=truth)
        n, _ = run(0.15, renders=600, truth=truth)
        old_r, new_r = old_r + o.renders, new_r + n.renders
        old_t2 += truth in [s.model for s in o.ranked[:2]]
        new_t2 += truth in [s.model for s in n.ranked[:2]]
    saved = 1 - new_r / old_r
    print(f"600-render ceiling: old {old_r / N_MODELS:.0f} renders per match, new {new_r / N_MODELS:.0f} "
          f"({saved:.0%} fewer); true model in top 2: old {old_t2}/{N_MODELS}, new {new_t2}/{N_MODELS}")
    assert saved > 0.25 and new_t2 >= old_t2
