"""Family dispatch (T122): registry, plug-in contract, the weights of the broad stage, the hybrid hook."""
from __future__ import annotations

import numpy as np
from test_model_choice import BUD, MODELS, NOT_TV, ModelEngine, cand, section

from tonematch.search import Budget
from tonematch.search import specialists as SP
from tonematch.search.hybrid import choose_models_hybrid
from tonematch.search.measured_seed import Measured, Seed
from tonematch.search.specialists import (
    FAMILIES,
    FAMILY_OF,
    Dispatch,
    FamilySeed,
    StubSpecialist,
    build_dispatch,
    draw_counts,
    load_specialist,
)
from tonematch.search.stubs import SpectralStubLoss

POOL = [(f"d{i}", "delay") for i in range(6)] + [(f"r{i}", "reverb") for i in range(5)] + \
       [(f"m{i}", "mod") for i in range(6)] + [(f"a{i}", "amp") for i in range(6)]


class FakeTime:
    family, categories, budget_share = "time", FAMILIES["time"], 1.0

    def __init__(self, hint=0.9, probs=None, wrap=None, boom=False):
        self.hint, self.probs, self.wrap, self.boom = hint, probs or {"delay": 0.5, "reverb": 0.4, "mod": 0.05}, wrap, boom

    def screen_hint(self, target, dry):
        if self.boom:
            raise RuntimeError("no")
        return self.hint

    def seed(self, target, dry):
        return FamilySeed("time", Measured({"delay.time_s": 0.3}), self.probs,
                          lambda m, c, r: {"Time": Seed(0.3, 0.05, "t", 0.3)} if c == "delay" else {}, ["note"])

    def score(self, target, dry):
        return self.wrap


def test_families_cover_the_eight_categories_once():
    cats = [c for cs in FAMILIES.values() for c in cs]
    assert sorted(cats) == sorted(["drive", "amp", "cab", "eq", "comp", "delay", "reverb", "mod"])
    assert FAMILY_OF["reverb"] == "time" and FAMILY_OF["amp"] == "gain" and FAMILY_OF["cab"] == "linear"


def test_a_family_without_a_module_gets_a_stub_with_no_opinion(monkeypatch):
    monkeypatch.setitem(SP.MODULES, "mod", "tonematch.nonexistent_specialist")
    sp = load_specialist("mod")
    assert isinstance(sp, StubSpecialist) and sp.family == "mod"
    assert np.isnan(sp.screen_hint(np.zeros(10), np.zeros(10))) and sp.score(np.zeros(10), np.zeros(10)) is None
    assert sp.seed(np.zeros(10), np.zeros(10)).for_model("m", "mod", {}) == {}


def test_neutral_dispatch_when_no_family_is_enabled():
    d = build_dispatch((), np.zeros(10), np.zeros(10), POOL)
    assert d.families == () and d.seeds == {} and d.wrap is None


def test_dispatch_collects_hint_seeds_probs_and_family_score():
    wrap = lambda kit: ("wrapped", kit)
    d = build_dispatch(("time",), np.zeros(10), np.zeros(10), POOL, specialists={"time": FakeTime(wrap=wrap)})
    assert d.hints == {"time": 0.9} and d.category_probs["delay"] == 0.5
    assert set(d.seeds) == {f"d{i}" for i in range(6)} and d.seeds["d0"]["Time"].value == 0.3
    assert d.wrap is wrap and d.kit_family == "time"


def test_family_score_is_not_used_when_the_hint_is_low():
    d = build_dispatch(("time",), np.zeros(10), np.zeros(10), POOL,
                       specialists={"time": FakeTime(hint=0.2, wrap=lambda k: k)})
    assert d.wrap is None and d.hints == {"time": 0.2}


def test_a_failing_specialist_is_no_opinion_not_a_crash():
    d = build_dispatch(("time",), np.zeros(10), np.zeros(10), POOL, specialists={"time": FakeTime(boom=True)})
    assert d.hints == {} and any("screen_hint failed" in n for n in d.notes)


def test_draw_counts_follow_the_probabilities_and_sum_to_the_total():
    d = Dispatch(families=("time",), category_probs={"delay": 0.5, "reverb": 0.4, "mod": 0.05})
    c = draw_counts(POOL, 120, d)
    assert sum(c) == 120 and min(c) >= SP.MIN_DRAWS
    by = {cat: sum(n for n, (_, k) in zip(c, POOL, strict=True) if k == cat) for cat in ("delay", "reverb", "mod", "amp")}
    assert by["delay"] > by["reverb"] > by["mod"] and by["amp"] >= SP.MIN_DRAWS * 6
    # the left-over probability (0.05) goes to the one category nobody has an opinion on
    assert by["amp"] <= by["mod"] + 12


def test_a_ruled_out_model_keeps_a_floor_of_draws():
    d = Dispatch(families=("time",), category_probs={"delay": 0.99, "reverb": 0.0, "mod": 0.0})
    c = draw_counts(POOL, 230, d)
    assert min(c) >= SP.MIN_DRAWS


def test_neutral_hybrid_equals_the_general_hybrid_draw_for_draw():
    cs = [cand(m) for m in MODELS]
    sec = section("m_fuzz", 0.7)
    a = choose_models_hybrid(sec, ModelEngine(), SpectralStubLoss(), cs, Budget(max_renders=60, **BUD), f=0.5, k=2, is_tv=NOT_TV)
    b = choose_models_hybrid(sec, ModelEngine(), SpectralStubLoss(), cs, Budget(max_renders=60, **BUD), f=0.5, k=2, is_tv=NOT_TV,
                             dispatch=Dispatch())
    assert [s.model for s in a.ranked] == [s.model for s in b.ranked] and a.renders == b.renders
    assert [s.screen for s in a.ranked] == [s.screen for s in b.ranked]


def test_hybrid_gives_the_hinted_models_more_broad_draws_and_uses_the_family_score():
    seen = []

    class Counting(SpectralStubLoss):
        def score(self, target, candidate, **kw):
            seen.append(1)
            return super().score(target, candidate, **kw)

    cs = [cand(m) for m in MODELS]
    sec = section("m_fuzz", 0.7)
    wrapped = []

    def wrap(kit):
        wrapped.append(kit)
        return kit

    disp = Dispatch(families=("time",), category_probs={"drive": 0.9}, wrap=wrap, kit_family="time")
    ch = choose_models_hybrid(sec, ModelEngine(), Counting(), cs, Budget(max_renders=60, **BUD), f=0.5, k=2, is_tv=NOT_TV,
                              dispatch=disp)
    assert wrapped and ch.best.model == "m_fuzz" and any("broad draws by category" in n for n in ch.notes)
