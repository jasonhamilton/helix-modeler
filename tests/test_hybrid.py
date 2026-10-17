"""Hybrid model choice (Gate 2) on the fake three-model drive engine."""
from __future__ import annotations

import numpy as np
from test_model_choice import BUD, MODELS, NOT_TV, ModelEngine, cand, fps, section

from tonematch.search import Budget
from tonematch.search.hybrid import (
    allocate,
    broad_settings,
    broad_stage,
    choose_models_hybrid,
    latin_hypercube,
)
from tonematch.search.stubs import SpectralStubLoss


def test_latin_hypercube_has_one_point_per_stratum():
    u = latin_hypercube(8, 3, np.random.default_rng(0))
    assert u.shape == (8, 3) and ((0 <= u) & (u <= 1)).all()
    for j in range(3):
        assert sorted((u[:, j] * 8).astype(int)) == list(range(8))
    assert latin_hypercube(0, 3, np.random.default_rng(0)).shape == (0, 3)


def test_allocate_spreads_the_remainder_from_the_front():
    assert allocate(3, 10) == [4, 3, 3] and sum(allocate(47, 150)) == 150 and allocate(3, 0) == [0, 0, 0]


def test_broad_settings_anchor_then_fingerprint_then_lhs():
    c = cand("m_crunch", fps("m_crunch"))
    s = broad_settings(c, 3, np.random.default_rng(1))
    assert [x.label for x in s] == ["start", "lhs0", "lhs1"]
    assert s[0].values == {(0, "Gain"): 0.5}
    f = broad_settings(c, 4, np.random.default_rng(1), fingerprint=True)
    assert f[0].label == "start" and {x.label for x in f[1:3]} <= {"Gain@24", "Gain@103", "Gain@64"}
    assert broad_settings(c, 0, np.random.default_rng(1)) == []


def test_broad_stage_spends_exactly_its_share_in_one_render_many():
    eng = ModelEngine()
    cs = [cand(m, fps(m)) for m in MODELS]
    sc, n, _ = broad_stage(section(), eng, SpectralStubLoss(), cs, Budget(max_renders=30, **BUD), None, 10)
    assert n == eng.renders == 10 and eng.many_calls == 1
    assert all(np.isfinite(s.screen) for s in sc)


def test_hybrid_finds_the_model_within_budget_and_refines_the_settings():
    eng = ModelEngine()
    cs = [cand(m) for m in MODELS]
    sec = section("m_fuzz", 0.7)
    ch = choose_models_hybrid(sec, eng, SpectralStubLoss(), cs, Budget(max_renders=60, **BUD), f=0.5, k=2,
                              is_tv=NOT_TV)
    assert ch.best.model == "m_fuzz" and ch.best.refined < 0.05
    assert ch.renders <= 66 and eng.renders <= 70       # halving rounds up a render or two per model
    assert ch.screen_renders == 30 and ch.rungs["broad"] == 30


def test_hybrid_is_deterministic_for_a_seed_and_orders_ties_by_usage():
    out = []
    for _ in range(2):
        ch = choose_models_hybrid(section("m_clean", 0.3), ModelEngine(), SpectralStubLoss(),
                                  [cand(m) for m in MODELS], Budget(max_renders=40, **BUD), f=0.5, k=2,
                                  usage={"m_fuzz": 0, "m_crunch": 1, "m_clean": 2}, is_tv=NOT_TV)
        out.append((ch.best.model, round(ch.best.refined, 9)))
    assert out[0] == out[1]


def test_hybrid_without_measurement_still_runs_and_reports_no_seeds():
    ch = choose_models_hybrid(section(), ModelEngine(), SpectralStubLoss(), [cand(m) for m in MODELS],
                              Budget(max_renders=30, **BUD), f=0.4, k=3, is_tv=NOT_TV)
    assert ch.seeds == {} and any("seeded 0 of 3" in n for n in ch.notes)


def test_broad_draws_use_the_broad_kit_and_refinement_the_main_kit():
    class Counting(SpectralStubLoss):
        def __init__(self):
            super().__init__()
            self.n = 0

        def score(self, *a, **k):
            self.n += 1
            return super().score(*a, **k)

    main, broad = Counting(), Counting()
    ch = choose_models_hybrid(section(), ModelEngine(), main, [cand(m) for m in MODELS],
                              Budget(max_renders=40, **BUD), f=0.5, k=2, broad_kit=broad, is_tv=NOT_TV)
    assert broad.n == 20 == ch.screen_renders and main.n > 0
