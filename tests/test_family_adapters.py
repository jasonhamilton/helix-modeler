"""Family adapters (T134): one dispatch test per family with fakes (no Native), and `--specialists all`."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tonematch.metrics import mod_specialist as MS
from tonematch.search import family_adapters as FA
from tonematch.search.amp_specialist import AmpSpecialist
from tonematch.search.specialists import FAMILIES, Dispatch, StubSpecialist, build_dispatch

POOL = [("drv", "drive"), ("amp1", "amp"), ("cab1", "cab"), ("eq1", "eq"), ("cmp", "comp"),
        ("dly", "delay"), ("rev", "reverb"), ("mod1", "mod"), ("mod2", "mod")]
X = np.zeros(48000)


def test_gain_adapter_is_the_amp_specialist_with_its_refiner():
    sp = FA.build_specialists(["gain"])["gain"]
    assert isinstance(sp, AmpSpecialist)
    sp.probs_fn = lambda t, d: {"drive": 0.1, "amp": 0.8}
    d = build_dispatch(("gain",), X, X, POOL, specialists={"gain": sp})
    assert d.hints["gain"] > 0.8 and d.category_probs == {"drive": 0.1, "amp": 0.8}
    assert d.refiner is not None and d.refiner_family == "gain" and d.wrap is None and d.seeds == {}


def test_mod_adapter_gives_hint_and_seeds_but_no_family_score(monkeypatch):
    sp = FA.build_specialists(["mod"])["mod"]
    assert sp.score_weight == 0.0
    an = SimpleNamespace(presence_mod=0.9, mod_score=0.95, rate_hz=2.0, rate_conf=0.9, am_depth_db=3.0,
                         pm_depth_cents=0.0, wet_mix=0.5, subtype="tremolo", subtype_reason="r",
                         to_dict=dict)
    monkeypatch.setattr(sp, "evidence", lambda t, d: (None, an))
    monkeypatch.setattr(MS, "knob_seeds", lambda *a, **k: {"Speed": (0.4, 0.05)})
    d = build_dispatch(("mod",), X, X, POOL, specialists={"mod": sp})
    assert d.hints["mod"] > 0.9 and d.wrap is None and d.kit_family is None   # score stays off
    assert set(d.seeds) == {"mod1", "mod2"} and d.seeds["mod1"]["Speed"].value == 0.4


def test_mod_family_score_is_off_even_if_the_module_default_changes(monkeypatch):
    monkeypatch.setattr(MS, "FAMILY_SCORE_ADMITTED", True)
    assert FA.build_specialists(["mod"])["mod"].score(X, X) is None


def test_linear_slot_is_the_linear_specialist_and_can_fall_back_to_a_stub(monkeypatch):
    from tonematch.metrics import linear_specialist as LS
    assert FA.LINEAR_ADAPTER is LS.make_specialist
    sp = FA.build_specialists(["linear"], time_score_weight=0.25)["linear"]
    assert isinstance(sp, LS.LinearFamily) and sp.family == "linear" and hasattr(sp, "refiner")
    monkeypatch.setattr(FA, "LINEAR_ADAPTER", None)         # the slot can still be emptied
    stub = FA.build_specialists(["linear"])["linear"]
    assert isinstance(stub, StubSpecialist) and np.isnan(stub.screen_hint(X, X))
    fake = SimpleNamespace(family="linear", categories=FAMILIES["linear"], budget_share=1.0,
                           screen_hint=lambda t, d: 0.7,
                           seed=lambda t, d: __import__("tonematch.search.specialists", fromlist=["x"]).FamilySeed("linear"),
                           score=lambda t, d: None)
    monkeypatch.setattr(FA, "LINEAR_ADAPTER", lambda **_: fake)
    d = build_dispatch(("linear",), X, X, POOL, specialists=FA.build_specialists(["linear"]))
    assert d.hints == {"linear": 0.7}


def test_dynamics_stays_a_stub():
    d = build_dispatch(("dynamics",), X, X, POOL, specialists=FA.build_specialists(["dynamics"]))
    assert d.hints == {} and d.wrap is None and d.refiner is None


def test_time_adapter_carries_the_score_weight():
    sp = FA.build_specialists(["time"], time_score_weight=0.5)["time"]
    assert sp.family == "time" and sp.score_weight == 0.5


def test_specialists_all_builds_a_dispatch(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import synthetic_v1 as S
    sps = FA.build_specialists(tuple(FAMILIES), time_score_weight=0.25)
    assert set(sps) == set(FAMILIES)
    for sp in sps.values():           # stub out measurement: no audio analysis in this test
        monkeypatch.setattr(sp, "screen_hint", lambda t, d: float("nan"), raising=False)
        monkeypatch.setattr(sp, "seed", lambda t, d, f=sp.family: __import__(
            "tonematch.search.specialists", fromlist=["x"]).FamilySeed(f), raising=False)
    d = build_dispatch(tuple(FAMILIES), X, X, POOL, specialists=sps)
    assert d.families == tuple(FAMILIES) and S.SPECIALISTS == ()


# ---- refiner routing (T134b) ------------------------------------------------------------------------------
def _fake_sp(family, tag, calls, hint=0.9):
    from tonematch.search.specialists import FamilySeed

    def refiner(t, d):
        def refine(engine, kit, section, by_model, scores, budget, plan, renders, prior=None):
            calls.append(tag)
            return 1.0, [f"{tag} refiner"]
        return refine
    return SimpleNamespace(family=family, categories=FAMILIES[family], budget_share=1.0,
                           screen_hint=lambda t, d: hint, seed=lambda t, d: FamilySeed(family),
                           score=lambda t, d: None, refiner=refiner)


def test_refiner_for_routes_by_family():
    calls = []
    sps = {"gain": _fake_sp("gain", "gain", calls), "linear": _fake_sp("linear", "linear", calls)}
    d = build_dispatch(("gain", "linear", "time"), X, X, POOL, specialists={**sps, "time": StubSpecialist("time")})
    assert set(d.refiners) == {"gain", "linear"} and d.refiner_family == "gain"     # first one kept in the old slot
    for cat, tag in (("amp", "gain"), ("drive", "gain"), ("cab", "linear"), ("eq", "linear")):
        d.refiner_for(cat)(None, None, None, {}, [], None, None, 1.0)
        assert calls[-1] == tag
    for cat in ("comp", "delay", "reverb", "mod", None, "nonsense"):
        assert d.refiner_for(cat) is None                        # the general halving
    assert Dispatch(refiner=len).refiner_for("reverb") is len    # a hand-set single refiner behaves as before


def _routed_run(target_model, calls):
    import test_model_choice as T

    cats = {"m_crunch": "amp", "m_clean": "cab", "m_fuzz": "reverb"}
    cs = []
    for m in T.MODELS:
        c = AmpCand(m, cats[m])
        cs.append(c)
    sec = T.section(target_model, 0.8)
    sps = {"gain": _fake_sp("gain", "gain", calls), "linear": _fake_sp("linear", "linear", calls)}
    d = build_dispatch(("gain", "linear"), sec.target, sec.dry, [(c.model, c.category) for c in cs], specialists=sps)
    from tonematch.search import Budget
    from tonematch.search.hybrid import choose_models_hybrid
    from tonematch.search.stubs import SpectralStubLoss
    ch = choose_models_hybrid(sec, T.ModelEngine(), SpectralStubLoss(), cs, Budget(max_renders=60, **T.BUD), f=0.4, k=3,
                              is_tv=T.NOT_TV, dispatch=d)
    return ch


def AmpCand(model, category):
    from test_amp_specialist import cand
    return cand(model, category)


def test_the_refine_stage_asks_the_refiner_of_the_leaders_family():
    for target, tag in (("m_crunch", "gain"), ("m_clean", "linear")):
        calls = []
        ch = _routed_run(target, calls)
        assert calls == [tag] and any(f"{tag} refiner" in n for n in ch.notes), (target, calls)
    calls = []
    ch = _routed_run("m_fuzz", calls)                            # reverb leader: neither refiner is asked
    assert calls == [] and ch.best.model == "m_fuzz"


def test_no_dispatch_path_is_draw_for_draw_identical():
    import test_model_choice as T

    from tonematch.search import Budget
    from tonematch.search.hybrid import choose_models_hybrid
    from tonematch.search.specialists import neutral
    from tonematch.search.stubs import SpectralStubLoss
    out = []
    for disp in (None, neutral(), Dispatch(refiners={"gain": len})):   # no families: refiners are never read
        eng = T.ModelEngine()
        cs = [AmpCand(m, "amp") for m in T.MODELS]
        ch = choose_models_hybrid(T.section("m_crunch", 0.8), eng, SpectralStubLoss(), cs, Budget(max_renders=60, **T.BUD),
                                  f=0.4, k=3, is_tv=T.NOT_TV, dispatch=disp)
        out.append(([(s.model, s.screen, s.refined) for s in ch.ranked], ch.renders, eng.renders if hasattr(eng, "renders") else None))
    assert out[0] == out[1] == out[2]


def test_specialists_all_routes_with_the_real_adapters(monkeypatch):
    sps = FA.build_specialists(tuple(FAMILIES), time_score_weight=0.25)
    for sp in sps.values():
        monkeypatch.setattr(sp, "screen_hint", lambda t, d: 0.9, raising=False)
        monkeypatch.setattr(sp, "seed", lambda t, d, f=sp.family: __import__(
            "tonematch.search.specialists", fromlist=["x"]).FamilySeed(f), raising=False)
    d = build_dispatch(tuple(FAMILIES), X, X, POOL, specialists=sps)
    assert d.refiner_for("amp") is not None and d.refiner_for("cab") is not None
    assert d.refiner_for("amp") is not d.refiner_for("eq") and d.refiner_for("cab") is d.refiner_for("eq")
    assert d.refiner_for("delay") is None and d.refiner_for("mod") is None
