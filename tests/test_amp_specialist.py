"""Amp specialist (T120) on the fake three-model engine; the models are given the category `amp`."""

from __future__ import annotations

import numpy as np
from test_model_choice import BUD, MODELS, NOT_TV, ModelEngine, section

from tonematch.preset import Preset, Slot
from tonematch.search import Budget, Evaluator, ParamSpace
from tonematch.search import amp_specialist as AS
from tonematch.search.hybrid import choose_models_hybrid
from tonematch.search.model_choice import ModelCandidate, ModelScore, Setting
from tonematch.search.specialists import Dispatch, build_dispatch, load_specialist
from tonematch.search.stubs import SpectralStubLoss
from tonematch.search.types import Plan

PLAN = Plan("native", 100, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)


def cand(model: str, category: str = "amp") -> ModelCandidate:
    p = Preset("x", [Slot(category, model, {"Gain": 0.5, "Level": 0.5}, True)])
    sp = ParamSpace.from_preset(p, [(0, "Gain"), (0, "Level")], None, None)
    return ModelCandidate(model, category, p, sp, [Setting("default", {})])


def score(model, screen, category="amp") -> ModelScore:
    return ModelScore(model, category, screen=screen)


def test_expected_improvement_prefers_better_mean_and_more_spread():
    ei = AS.expected_improvement(np.array([0.5, 1.5, 1.5]), np.array([0.1, 0.1, 1.0]), 1.0)
    assert ei[0] > ei[2] > ei[1] >= 0.0


def test_pick_finalists_leader_plus_close_family_models_only():
    sp = AS.AmpSpecialist(lead_ratio=1.3, max_finalists=2)
    s = [score("a", 0.10), score("b", 0.12), score("c", 0.30), score("d", 0.11, "reverb")]
    assert [x.model for x in sp.pick_finalists(s)] == ["a", "b"]  # d is close but not an amp
    assert [x.model for x in sp.pick_finalists([score("a", 0.1), score("b", 0.5)])] == ["a"]
    assert (
        sp.pick_finalists([score("r", 0.05, "reverb"), score("a", 0.06)]) == []
    )  # leader outside the family
    assert sp.pick_finalists([]) == [] and sp.pick_finalists([score("a", 1e9)]) == []
    assert (
        len(AS.AmpSpecialist(max_finalists=1).pick_finalists([score("a", 0.1), score("b", 0.1)]))
        == 1
    )


def test_bo_session_improves_on_its_start_and_counts_renders():
    eng = ModelEngine()
    sec = section("m_crunch", 0.8)
    c = cand("m_crunch")
    eng.load(c.preset_at({}))
    ev = Evaluator(eng, SpectralStubLoss(), sec, c.space, PLAN, Budget(max_renders=100, **BUD))

    def activate():
        eng.load(c.preset_at({}))
        ev.reset_applied()

    bo = AS.BOSession(ev, activate, np.random.default_rng(0))
    bo.init(8)
    f0 = bo.best()[1]
    bo.step(14)
    assert len(bo.X) == len(bo.F) == 22 and ev.spent == 22
    assert bo.best()[1] <= f0 and bo.best()[1] < 0.25  # the true setting is reachable
    assert all(0.0 <= x <= 1.0 for u in bo.X for x in u)


def test_refiner_declines_when_the_leader_is_not_an_amp():
    refine = AS.AmpSpecialist().refiner(np.zeros(10), np.zeros(10))
    out = refine(
        None, None, None, {}, [score("r", 0.1, "reverb"), score("a", 0.5)], None, None, 100.0
    )
    assert out is None


def test_refine_finalists_focuses_the_budget_on_the_leader():
    eng = ModelEngine()
    sec = section("m_crunch", 0.8)
    cs = {m: cand(m) for m in MODELS}
    scores = [score("m_crunch", 0.05), score("m_fuzz", 0.4), score("m_clean", 0.5)]
    spent, _ = AS.refine_finalists(
        eng,
        SpectralStubLoss(),
        sec,
        cs,
        [scores[0]],
        Budget(max_renders=100, **BUD),
        PLAN,
        AS.AmpSpecialist(n_init=6),
        30.0,
    )
    assert 28 <= spent <= 31 and scores[0].refined is not None and scores[0].refined < 0.25
    assert scores[1].refined is None and scores[2].refined is None  # nothing spent on the others


def test_refine_finalists_two_models_continue_the_better_one():
    eng = ModelEngine()
    sec = section("m_crunch", 0.8)
    cs = {m: cand(m) for m in MODELS}
    fin = [score("m_fuzz", 0.2), score("m_crunch", 0.21)]
    spent, notes = AS.refine_finalists(
        eng,
        SpectralStubLoss(),
        sec,
        cs,
        fin,
        Budget(max_renders=100, **BUD),
        PLAN,
        AS.AmpSpecialist(n_init_shared=6),
        40.0,
    )
    assert 38 <= spent <= 41 and fin[0].refined is not None and fin[1].refined is not None
    assert fin[1].refined < fin[0].refined and len(fin[1].rungs) == 2 and len(fin[0].rungs) == 1
    assert any("second finalist" in n for n in notes)


def test_specialist_surface_and_plug_in():
    sp = AS.make_specialist(probs_fn=lambda t, d: {"amp": 0.8, "drive": 0.5, "reverb": 0.1})
    assert sp.family == "gain" and sp.categories == ("drive", "amp") and sp.budget_share == 1.0
    assert abs(sp.screen_hint(np.zeros(4), np.zeros(4)) - 0.9) < 1e-9
    fs = sp.seed(np.zeros(4), np.zeros(4))
    assert fs.category_probs == {"drive": 0.5, "amp": 0.8} and fs.for_model("m", "amp", {}) == {}
    assert sp.score(np.zeros(4), np.zeros(4)) is None
    assert isinstance(load_specialist("gain"), AS.AmpSpecialist)
    d = build_dispatch(
        ("gain",),
        np.zeros(4),
        np.zeros(4),
        [("m_crunch", "amp"), ("x", "reverb")],
        specialists={"gain": sp},
    )
    assert d.refiner is not None and d.refiner_family == "gain" and d.category_probs["amp"] == 0.8


def test_hybrid_with_the_amp_dispatch_finds_the_model_within_budget():
    eng = ModelEngine()
    sec = section("m_crunch", 0.8)
    cs = [cand(m) for m in MODELS] + [cand("m_x", "reverb")]
    MODELS["m_x"] = (0.1, 0.9)
    try:
        sp = AS.make_specialist(probs_fn=lambda t, d: {"amp": 0.9, "drive": 0.0}, n_init=8)
        disp = build_dispatch(
            ("gain",),
            sec.target,
            sec.dry,
            [(c.model, c.category) for c in cs],
            specialists={"gain": sp},
        )
        bud = Budget(max_renders=90, **BUD)
        ch = choose_models_hybrid(
            sec,
            eng,
            SpectralStubLoss(),
            cs,
            bud,
            f=0.4,
            k=3,
            refine_scale=0.9,
            is_tv=NOT_TV,
            dispatch=disp,
        )
    finally:
        del MODELS["m_x"]
    assert ch.best.model == "m_crunch" and ch.best.refined < 0.05
    assert ch.renders <= 92 and any("amp specialist" in n for n in ch.notes)
    assert (
        sum(1 for s in ch.ranked if s.refined is not None) <= 2
    )  # only the finalists were refined


def test_hybrid_falls_back_to_halving_when_the_leader_is_not_an_amp():
    eng = ModelEngine()
    sec = section("m_crunch", 0.8)
    cs = [cand("m_crunch", "drive"), cand("m_clean"), cand("m_fuzz")]
    disp = Dispatch(
        families=("gain",),
        refiner=AS.AmpSpecialist().refiner(sec.target, sec.dry),
        refiner_family="gain",
    )
    ch = choose_models_hybrid(
        sec,
        eng,
        SpectralStubLoss(),
        cs,
        Budget(max_renders=60, **BUD),
        f=0.4,
        k=3,
        is_tv=NOT_TV,
        dispatch=disp,
    )
    assert ch.best.model == "m_crunch" and not any("amp specialist" in n for n in ch.notes)
    assert sum(1 for s in ch.ranked if s.refined is not None) >= 2  # halving refines several
