"""Search tests on a numpy fake engine and a spectral loss stub (no Line 6 software needed)."""
from __future__ import annotations

import math

import numpy as np

from tonematch.search import Budget, Section, match, plan_budget, write_report
from tonematch.search.cmaes import CMAES
from tonematch.search.space import ParamSpace, fit_prior
from tonematch.search.stubs import FakeEngine, SpectralStubLoss, fake_dry, fake_preset


def B(**kw):
    kw.setdefault("clip_s", 2.0)
    kw.setdefault("short_s", 1.0)
    return Budget(**kw)


TRUE = {(0, "Gain"): 0.3, (0, "Tone"): 0.7, (0, "Level"): 0.6}


def _section(true_preset, dry=None):
    dry = fake_dry() if dry is None else dry
    e = FakeEngine()
    e.load(true_preset)
    return Section("song", "verse", e.render(dry), dry)


def test_cmaes_sphere():
    es = CMAES(np.full(10, 0.5), 0.3, seed=1)
    target = np.linspace(0.2, 0.8, 10)
    for _ in range(80):
        xs = es.ask()
        es.tell(xs, [np.sum((es.clip(x) - target) ** 2) for x in xs])
    assert np.max(np.abs(es.mean - target)) < 0.02


def test_recovers_known_params_within_budget():
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    eng = FakeEngine(engine_name="native")
    out = match(sec, eng, SpectralStubLoss(), B(max_renders=300), candidates=[fake_preset()])
    best = out[0]
    assert best.renders <= 300 + 5 and eng.renders <= 305
    assert best.score < 0.3
    for r, v in TRUE.items():
        assert abs(best.values[r] - v) < 0.1, (r, best.values[r], v)


def test_successive_halving_cheaper_and_still_recovers():
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    eng = FakeEngine(engine_name="stomp", seconds_per_render=2.7)
    plan = plan_budget(eng, B(max_renders=250))
    assert plan.halving and plan.short_frac == 0.5
    out = match(sec, eng, SpectralStubLoss(), B(max_renders=250), candidates=[fake_preset()])
    assert out[0].score < 0.5
    assert 250 * 0.9 <= out[0].renders <= 250 * 1.7  # calls; short ones count half in the budget
    assert all(abs(out[0].values[r] - v) < 0.15 for r, v in TRUE.items())


def test_plan_handles_nan_load_penalty_and_wall_cap():
    p = plan_budget(FakeEngine(seconds_per_render=2.7, parallelism=1), B(max_renders=1000, wall_s=600))
    assert p.load_penalty_assumed and math.isfinite(p.load_penalty_s)
    assert p.max_renders == int(600 / 2.7)
    n = plan_budget(FakeEngine(1.63, 6, 0.016, "native"), B(max_renders=1000, wall_s=600))
    assert not n.halving and n.max_renders == 1000 and not n.load_penalty_assumed
    assert n.popsize % 6 == 0


def test_chain_grouping_and_unsupported_chain_skipped():
    with_delay = fake_preset(0.5, 0.5, 0.5, delay=(0.3, 0.3))
    plain = fake_preset()
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    key_plain = tuple((s.category, s.model_id) for s in plain.slots)
    eng = FakeEngine(engine_name="native", allowed_chains={key_plain})
    out = match(sec, eng, SpectralStubLoss(), B(max_renders=120),
                candidates=[with_delay, plain, fake_preset(0.4, 0.4, 0.4)])
    assert len(out) == 1 and out[0].chain == key_plain
    assert any("skipped" in n for n in out[0].notes)
    # one chain: one load per phase for the search plus one for the final check
    assert eng.loads <= 3


def test_two_chains_ranked_and_loads_are_rare():
    true = fake_preset(0.7, 0.3, 0.6, delay=(0.4, 0.2))
    sec = _section(true)
    eng = FakeEngine(engine_name="native")
    cands = [fake_preset(), fake_preset(delay=(0.2, 0.5))]
    out = match(sec, eng, SpectralStubLoss(), B(max_renders=500), candidates=cands, beam=2)
    assert out[0].chain[-1][1] == "fake_delay"
    assert out[0].score < out[1].score
    assert eng.loads <= 2 * 2 + 2  # 2 chains x 2 phases + 2 final checks, not one per trial


def test_param_steps_quantise():
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    eng = FakeEngine(param_steps=11)
    out = match(sec, eng, SpectralStubLoss(), B(max_renders=150), candidates=[fake_preset()])
    for v in out[0].values.values():
        assert abs(v * 10 - round(v * 10)) < 1e-9


def test_invalid_candidate_never_rendered():
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    eng = FakeEngine()

    class V:
        severity = "fail"

        def __str__(self):
            return "FAIL nope"

    out = match(sec, eng, SpectralStubLoss(), B(max_renders=50), candidates=[fake_preset()],
                validator=lambda p: [V()])
    assert out == [] and eng.renders == 0 and eng.loads == 0


def test_prior_fit_and_report(tmp_path):
    lib = [fake_preset(0.6, 0.4, 0.5), fake_preset(0.8, 0.2, 0.7)]
    pr = fit_prior(lib, weight=0.01)
    assert abs(pr.mean[(0, "Gain")] - 0.7) < 1e-9
    sec = _section(fake_preset(0.3, 0.7, 0.6))
    eng = FakeEngine()
    b = B(max_renders=60)
    out = match(sec, eng, SpectralStubLoss(), b, candidates=[fake_preset()], prior=pr)
    p = write_report(sec, out, plan_budget(eng, b), b, tmp_path)
    txt = p.read_text()
    assert p == tmp_path / "song" / "verse.md" and "Engine: stomp" in txt and "slot 0 Gain" in txt


def test_stages_order():
    sp = ParamSpace.from_preset(fake_preset(delay=(0.3, 0.3)))
    st = sp.stages()
    assert len(st) == 2 and all(sp.categories[j] == "drive" for j in st[0])


def test_default_clip_is_long_and_weights_provisional():
    b = Budget()
    assert b.clip_s >= 10.0 and b.weights == "provisional" and not b.admitted


def test_objective_not_constant_with_real_kit_on_proxy_chain(tmp_path):
    from tonematch.metrics import DEFAULT_WEIGHTS, LossKit
    from tonematch.metrics.proxy import guitar_di
    from tonematch.search.loop import Evaluator, resolve_weights
    from tonematch.search.space import ParamSpace

    dry = guitar_di(3, 2.0).astype(np.float32)
    e = FakeEngine()
    e.load(fake_preset(0.3, 0.7, 0.6))
    sec = Section("song", "verse", e.render(dry), dry)
    kit = LossKit()
    assert all(v == 0 for v in DEFAULT_WEIGHTS.values())  # why weights must be passed explicitly
    assert any(v > 0 for v in resolve_weights("provisional").values())
    bud = B()
    eng = FakeEngine()
    pre = fake_preset()
    eng.load(pre)
    space = ParamSpace.from_preset(pre)
    ev = Evaluator(eng, kit, sec, space, plan_budget(eng, bud), bud)
    scores = [ev.score(space.start + np.array(d))[0] for d in ([0, 0, 0], [0.3, 0, 0], [0, 0.3, 0])]
    # not constant over Gain/Tone (the kit is level-invariant by design, so Level is not probed)
    assert len({round(s, 6) for s in scores}) == 3 and min(scores) > 0
    out = match(sec, FakeEngine(), kit, B(max_renders=60), candidates=[fake_preset()])
    assert out[0].loss_label == "unadmitted loss" and out[0].clip_s == 2.0
    p = write_report(sec, out, plan_budget(eng, bud), bud, tmp_path)
    assert "unadmitted loss" in p.read_text() and "clip length 2.0 s" in p.read_text()
