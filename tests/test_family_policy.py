"""Family policy (T143): category prior, shortlist, drive-aware gain finalists. Fakes only."""
from __future__ import annotations

import math

import numpy as np

from tonematch.search.family_policy import (
    Policy,
    final_order,
    narrow,
    penalty,
    within_category_rank,
)
from tonematch.search.gain_family import GainFamily
from tonematch.search.model_choice import ModelScore
from tonematch.search.specialists import Dispatch


def ms(model, cat, screen, refined=None):
    return ModelScore(model, cat, screen=screen, refined=refined)


def test_default_policy_is_off_and_penalty_is_zero():
    assert not Policy().active
    d = Dispatch(hints={"mod": 0.01})
    assert penalty(d, "mod", 0.0) == 0.0 and penalty(None, "mod", 1.0) == 0.0


def test_penalty_follows_the_family_hint_and_skips_families_without_one():
    d = Dispatch(hints={"mod": 1.0, "linear": 0.001})
    assert penalty(d, "mod", 0.1) == 0.0
    assert math.isclose(penalty(d, "eq", 0.1), 0.1 * -math.log(0.02))     # clipped at FLOOR
    assert math.isclose(penalty(d, "comp", 0.1), 0.1 * -math.log(0.02))   # dynamics has no detector: what is left over
    d2 = Dispatch(hints={"mod": 0.5, "linear": 0.001})
    assert math.isclose(penalty(d2, "comp", 0.1), 0.1 * -math.log(0.5))


def test_final_order_moves_the_hinted_family_ahead_of_a_slightly_better_eq():
    d = Dispatch(hints={"mod": 0.99, "linear": 0.001})
    scores = [ms("eq1", "eq", 0.2, 0.10), ms("mod1", "mod", 0.3, 0.13), ms("mod2", "mod", 0.3, 0.30)]
    ref, _ = final_order(scores, d, Policy(tau=0.02))
    assert [s.model for s in ref] == ["mod1", "eq1", "mod2"] or ref[0].model == "mod1"
    ref0, _ = final_order(scores, d, Policy())
    assert ref0[0].model == "eq1"                                        # tau 0 keeps the raw order


def test_narrow_forces_the_shortlist_then_fills_by_adjusted_screen():
    d = Dispatch(hints={"mod": 0.9, "linear": 0.01, "time": 0.05})
    d.shortlist = ["m3", "m1"]                                           # type: ignore[attr-defined]
    scores = [ms("e1", "eq", 0.1), ms("e2", "eq", 0.11), ms("m0", "mod", 0.5), ms("m1", "mod", 0.6),
              ms("m3", "mod", 0.9), ms("dl", "delay", 0.12)]
    pol = Policy(tau=0.02, shortlist_max=2)
    got = [s.model for s in narrow(scores, 4, d, pol)]
    assert got[:2] == ["m3", "m1"] and len(got) == 4 and len(set(got)) == 4
    assert [s.model for s in narrow(scores, 4, d, Policy())] == ["e1", "e2", "dl", "m0"]


def test_shortlist_is_ignored_when_the_family_hint_is_low():
    d = Dispatch(hints={"mod": 0.1})
    d.shortlist = ["m3"]                                                 # type: ignore[attr-defined]
    scores = [ms("e1", "eq", 0.1), ms("m3", "mod", 0.9)]
    assert [s.model for s in narrow(scores, 1, d, Policy(shortlist_max=1))] == ["e1"]


def test_gain_finalists_are_the_best_gain_models_within_the_ratio_drive_or_amp():
    g = GainFamily()
    scores = [ms("a1", "amp", 0.30), ms("d1", "drive", 0.45), ms("a2", "amp", 0.50), ms("e", "eq", 0.2)]
    assert [s.model for s in g.pick_finalists(scores)] == ["a1", "d1", "a2"]
    far = [ms("d1", "drive", 0.30), ms("a1", "amp", 0.9), ms("d2", "drive", 0.35)]
    assert [s.model for s in g.pick_finalists(far)] == ["d1", "d2"]            # 0.9 is beyond 2 x 0.30
    four = [ms(f"m{i}", "drive" if i % 2 else "amp", 0.3 + 0.01 * i) for i in range(5)]
    assert len(g.pick_finalists(four)) == 3
    assert g.pick_finalists([ms("e", "eq", 0.1)]) == []                  # no gain model: halving
    assert isinstance(np.zeros(1), np.ndarray)


def test_gate_prior_acts_only_when_a_family_is_confident():
    pol = Policy(tau=0.05, gate=0.9)
    low = Dispatch(hints={"mod": 0.5, "linear": 0.001, "gain": 0.2})
    assert penalty(low, "eq", pol.tau, pol.gate) == 0.0                   # nobody above the gate: frozen behaviour
    hi = Dispatch(hints={"mod": 0.99, "linear": 0.001, "gain": 0.2})
    assert penalty(hi, "mod", pol.tau, pol.gate) == 0.0
    assert math.isclose(penalty(hi, "eq", pol.tau, pol.gate), 0.05 * math.log(0.99 / 0.02))
    assert penalty(hi, "amp", pol.tau, pol.gate) > 0


def test_rank_swap_prefers_the_specialist_order_within_the_slack():
    d = Dispatch(hints={"mod": 0.9})
    d.shortlist = ["m_true", "m_other", "m_far"]                          # type: ignore[attr-defined]
    ref = [ms("m_other", "mod", 0.3, 0.20), ms("m_true", "mod", 0.3, 0.30), ms("m_far", "mod", 0.3, 0.9),
           ms("e", "eq", 0.3, 0.4)]
    pol = Policy(rank_slack=2.0)
    assert [s.model for s in within_category_rank(ref, d, pol)][:2] == ["m_true", "m_other"]
    assert within_category_rank(ref, d, Policy(rank_slack=1.2)) == ref                # 0.30 is outside 1.2 x 0.20
    assert within_category_rank(ref, d, Policy()) == ref                              # off
    assert within_category_rank([ms("e", "eq", 0.3, 0.1)] + ref, d, pol)[0].model == "e"   # winner not shortlisted


def test_only_mod_and_gain_hints_gate_and_the_raw_leader_stays():
    pol = Policy(tau=0.05, gate=0.9)
    time_hi = Dispatch(hints={"time": 0.99, "mod": 0.0, "gain": 0.1})
    assert penalty(time_hi, "mod", pol.tau, pol.gate, pol.gate_families) == 0.0       # a confident time hint does not gate
    d = Dispatch(hints={"mod": 0.99, "linear": 0.0})
    scores = [ms("eq1", "eq", 0.15), ms("m1", "mod", 0.3), ms("m2", "mod", 0.31), ms("m3", "mod", 0.32),
              ms("m4", "mod", 0.33)]
    got = [s.model for s in narrow(scores, 3, d, Policy(tau=0.05, gate=0.9))]
    assert "eq1" not in got                                                            # pushed out by the prior
    got = [s.model for s in narrow(scores, 3, d, Policy(tau=0.05, gate=0.9, keep_top=1))]
    assert got[0] == "eq1" and len(got) == 3                                           # the raw leader is kept


def test_inactive_policy_is_the_frozen_hybrid_and_an_active_one_runs_end_to_end():
    from test_model_choice import BUD, MODELS, NOT_TV, ModelEngine, cand, section

    from tonematch.search import Budget
    from tonematch.search.hybrid import choose_models_hybrid
    from tonematch.search.stubs import SpectralStubLoss

    def run(**kw):
        return choose_models_hybrid(section("m_fuzz", 0.7), ModelEngine(), SpectralStubLoss(), [cand(m) for m in MODELS],
                                    Budget(max_renders=60, **BUD), f=0.5, k=2, is_tv=NOT_TV, **kw)

    a = run(dispatch=Dispatch(families=("mod",), hints={"mod": 0.95}))
    b = run(dispatch=Dispatch(families=("mod",), hints={"mod": 0.95}), policy=Policy())
    assert [s.model for s in a.ranked] == [s.model for s in b.ranked] and a.renders == b.renders
    c = run(dispatch=Dispatch(families=("mod",), hints={"mod": 0.95}), policy=Policy(tau=0.05, gate=0.9, keep_top=1))
    assert any("family policy: refine set" in n for n in c.notes) and c.ranked
