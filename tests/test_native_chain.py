"""T121: the chain builder on Native, on fake hosts (no Native): library adapter, block-serial
rendering with engine-aware trim, the per-depth batched prefix renderer and build_chain_native."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest
from scipy.signal import lfilter

from tonematch.engines.base import EngineError
from tonematch.engines.native import cc_value
from tonematch.engines.native_chain import (
    TRIM,
    NativeBlockLibrary,
    NativeChainRenderer,
    build_chain_native,
    engine_trim,
    fingerprint_distance,
    render_chain,
    shift,
)
from tonematch.preset import Preset, Slot
from tonematch.search.chain_builder import (
    BlockSpec,
    BudgetExhausted,
    ChainBudget,
    ChainRenderer,
    ScoreMode,
)
from tonematch.search.chain_builder import render_chain as reference_render_chain
from tonematch.search.stubs import StubScore, fake_dry

# ------------------------------------------------------------------ fake library data

CAT = {"d_soft": "drive", "d_hard": "drive", "d_stereo": "drive", "d_tv": "drive", "d_io": "drive",
       "a_dark": "amp", "e_echo": "delay", "e_slap": "delay", "c_cab": "cab", "c_cabWithPan": "cab",
       "HD2_ImpulseResponse1024": "cab", "d_nolib": "drive"}
PARAMS = {
    "d_soft": {"Gain": 0.5, "Tone": 0.5, "Level": 0.0, "Mode": 1, "Hum": 0.3, "Bright": False},
    "d_hard": {"Gain": 0.5, "Tone": 0.5, "Level": 0.0},
    "d_stereo": {"Gain": 0.5}, "d_tv": {"Gain": 0.5}, "d_io": {"Gain": 0.5}, "d_nolib": {"Gain": 0.5},
    "a_dark": {"Tone": 0.5},
    "e_echo": {"Time": 0.5, "Mix": 0.3, "Feedback": 0.35},
    "e_slap": {"Time": 0.1, "Mix": 0.3},
    "c_cab": {"Distance": 4.0}, "c_cabWithPan": {"Distance": 4.0}, "HD2_ImpulseResponse1024": {"Mix": 1.0},
}
CATALOG = {"models": {m: {"category": c, "source_file": "", "stomp": True, "mono": m != "d_stereo",
                          "params": {"Mode": {"kind": "enum"}} if m == "d_soft" else {}}
                      for m, c in CAT.items()}}


@dataclass
class FakeTpl:
    map: dict
    ranges: dict
    verified: list = field(default_factory=list)


def fake_templates():
    out = {}
    for m, ps in PARAMS.items():
        if m == "d_nolib":
            continue
        refs = [f"0:{k}" for k in ps] + ["0:@enabled"]
        ranges = {f"0:{k}": [0.0, 1.0] for k in ps}
        ranges["0:@enabled"] = [0.0, 1.0]
        if "Time" in ps:
            ranges["0:Time"] = [0.0, 1.27]
        if "Distance" in ps:
            ranges["0:Distance"] = [1.0, 12.0]
        if "Level" in ps:
            ranges["0:Level"] = [-25.75, 6.0]
        ver = [r for r in refs if r != "0:Feedback"]   # e_echo Feedback: mapped, never verified (baked)
        out[m] = FakeTpl({r: f"cc_{i}" for i, r in enumerate(refs)}, ranges, ver)
    return out


def fake_preset(model):
    return Preset("lib", [Slot(CAT[model], model, dict(PARAMS[model]), True)])


def make_lib(**kw):
    args = {"catalog": CATALOG, "templates": fake_templates(), "preset_fn": fake_preset,
                "usage": {"d_hard": 50, "d_soft": 10, "e_slap": 30, "e_echo": 5, "c_cab": 3},
                "fingerprints": {}, "time_variant": {"d_tv"}, "unverifiable": {"d_io": "I/O block"},
                "frozen": {("drive", "Hum"): 0.0}, "cc_unreliable": {("d_soft", "Tone"): "lands off"}}
    args.update(kw)
    return NativeBlockLibrary(**args)


# ------------------------------------------------------------------ library adapter

def test_free_params_are_verified_continuous_on_template_ranges():
    lib = make_lib()
    # Tone is CC_UNRELIABLE, Hum frozen, Level fixed, Mode an enum, Bright a switch
    assert lib.free_params("d_soft") == {"Gain": (0.0, 1.0, 0.5)}
    assert lib.free_params("d_hard") == {"Gain": (0.0, 1.0, 0.5), "Tone": (0.0, 1.0, 0.5)}
    # Feedback is mapped but baked (unverified); Time on the template's own range
    assert lib.free_params("e_echo") == {"Time": (0.0, 1.27, 0.5), "Mix": (0.0, 1.0, 0.3)}
    assert "Level" in make_lib(exclude_params=()).free_params("d_hard")


def test_shortlist_excludes_and_ranks_by_usage():
    lib = make_lib()
    assert lib.shortlist("drive", 9) == ["d_hard", "d_soft"]
    assert lib.shortlist("drive", 1) == ["d_hard"]
    assert lib.shortlist("cab", 9) == ["c_cab"]
    why = lib.excluded
    assert "stereo" in why["d_stereo"] and "time-variant" in why["d_tv"] and "unverifiable" in why["d_io"]
    assert "pan twin" in why["c_cabWithPan"] and "IR block" in why["HD2_ImpulseResponse1024"]
    assert "no library template" in why["d_nolib"]
    assert make_lib(allow_ir=True).shortlist("cab", 9) == ["c_cab", "HD2_ImpulseResponse1024"]


def test_shortlist_fingerprint_match_before_usage():
    fps = {"e_echo": {"settings": [{"summary": {"delay_ms": 240}}, {"summary": {"delay_ms": 1030}}]},
           "e_slap": {"settings": [{"summary": {"delay_ms": 60}}, {"summary": {"delay_ms": 120}}]}}
    lib = make_lib(fingerprints=fps)
    assert lib.shortlist("delay", 2) == ["e_slap", "e_echo"]          # no estimate: usage order
    lib.estimates = {"delay": {"time_s": 0.5}}
    assert lib.shortlist("delay", 2) == ["e_echo", "e_slap"]          # 500 ms is inside e_echo's span
    lib.estimates = {"delay": {"time_s": 0.1}}
    assert lib.shortlist("delay", 2) == ["e_slap", "e_echo"]


def test_fingerprint_distance_rules():
    fp = {"settings": [{"summary": {"mod_kind": "amplitude", "mod_rate_hz": 2.0}},
                       {"summary": {"mod_kind": "amplitude", "mod_rate_hz": 8.0}}]}
    assert fingerprint_distance("mod", fp, {"subtype": "tremolo", "rate_hz": 4.0}) == 0.0
    assert fingerprint_distance("mod", fp, {"subtype": "chorus", "rate_hz": 4.0}) == 2.0
    assert fingerprint_distance("mod", fp, {"subtype": "tremolo", "rate_hz": 16.0}) == pytest.approx(1.0)
    rv = {"settings": [{"summary": {"rt60_s": 1.0}}, {"summary": {"rt60_s": 3.0}}]}
    assert fingerprint_distance("reverb", rv, {"rt60_s": 6.0}) == pytest.approx(1.0)
    assert fingerprint_distance("reverb", rv, {"rt60_s": float("nan")}) is None
    assert fingerprint_distance("amp", rv, {"gain_index_db": -10}) is None


def test_preset_snaps_cc_values_and_refuses_unknown_params():
    lib = make_lib()
    p = lib.preset("e_echo", {"Time": 0.351, "Mix": 0.44})
    s = p.slots[0]
    assert s.params["Time"] == cc_value(0.0, 1.27, 35) and s.params["Mix"] == cc_value(0.0, 1.0, 56)
    assert s.params["Feedback"] == 0.35                       # untouched baked value
    assert lib.preset("e_echo", {"Time": 1.5}).slots[0].params["Time"] == 1.5   # off the grid: kept
    assert make_lib(snap=False).preset("e_echo", {"Mix": 0.44}).slots[0].params["Mix"] == 0.44
    assert lib.preset("e_echo", {}).slots[0].params == PARAMS["e_echo"]
    with pytest.raises(KeyError):
        lib.preset("e_echo", {"Nope": 1.0})


# ------------------------------------------------------------------ fake hosts

def dsp(model, p, x):
    x = np.asarray(x, np.float64)
    if model.startswith("d_"):
        return np.tanh((1 + 8 * p["Gain"]) * x) * 0.5
    if model == "a_dark":
        a = 0.02 + 0.95 * (1 - p["Tone"])
        return lfilter([1 - a], [1, -a], x)
    if model.startswith("e_"):
        d = int(200 + 2000 * p["Time"])
        return x + p["Mix"] * np.concatenate([np.zeros(d), x[:-d]])
    if model == "c_cab":
        return lfilter([0.5, 0.5], [1], x)
    raise EngineError(model)


class RawHost:
    """Raw instance output: TRIM samples late, no latency() (the chain builder's fake)."""

    def __init__(self):
        self.calls: list[int] = []

    def render_many(self, dry, variants):
        self.calls.append(len(variants))
        out = []
        for v in variants:
            s = v.slots[0]
            y = dsp(s.model_id, s.params, dry)
            out.append(np.concatenate([np.zeros(TRIM), y[:-TRIM]]).astype(np.float32))
        return out


class ContractEngine(RawHost):
    """Like NativeEngine.render: latency already trimmed, latency() says by how much."""

    def latency(self):
        return TRIM

    def render_many(self, dry, variants):
        self.calls.append(len(variants))
        return [dsp(v.slots[0].model_id, v.slots[0].params, dry).astype(np.float32) for v in variants]


class FakePool:
    """NativeRenderPool stand-in: render_batch of RenderJobs (own input each), latency-trimmed."""

    def __init__(self, fail: set | None = None):
        self.engines = [ContractEngine()]
        self.batches: list[int] = []
        self.fail = fail or set()
        self.last_errors: dict = {}

    def render_batch(self, jobs, on_error="raise"):
        self.batches.append(len(jobs))
        self.last_errors = {}
        out = []
        for i, j in enumerate(jobs):
            s = j.preset.slots[0]
            if s.model_id in self.fail:
                if on_error == "raise":
                    raise EngineError("boom")
                self.last_errors[i] = EngineError(f"{s.model_id} failed")
                out.append(None)
                continue
            out.append(dsp(s.model_id, s.params, j.dry).astype(np.float32))
        return out


DRY = (lfilter([0.15], [1, -0.85], fake_dry(1.0, seed=3)) * 3).astype(np.float32)


def composed(presets, x=DRY):
    y = np.asarray(x, np.float64)
    for p in presets:
        y = dsp(p.slots[0].model_id, p.slots[0].params, y)
    return y


def test_render_chain_trim_accounts_for_engine_latency():
    lib = make_lib()
    presets = [lib.preset("d_hard", {}), lib.preset("a_dark", {}), lib.preset("e_echo", {})]
    want = composed(presets)
    for eng in (ContractEngine(), FakePool()):
        assert engine_trim(eng) == TRIM
        y = render_chain(eng, presets, DRY)
        assert len(y) == len(DRY) and np.allclose(y, want, atol=1e-5)       # exact, no shift
    raw = RawHost()
    assert engine_trim(raw) == 0
    y = render_chain(raw, presets, DRY)
    n = len(DRY) - 3 * TRIM
    assert np.allclose(y[:n], want[:n], atol=1e-5)
    assert np.allclose(y, reference_render_chain(raw, presets, DRY))       # same as the chain builder's
    # the chain builder's own render_chain on a contract engine double-trims: 16 samples early per block
    early = reference_render_chain(ContractEngine(), presets, DRY)
    assert not np.allclose(early, want, atol=1e-3)
    errs = {s: float(np.mean((early[5000:20000] - want[5000 + s:20000 + s]) ** 2)) for s in range(0, 64, 8)}
    assert min(errs, key=errs.get) == 3 * TRIM


def test_shift():
    x = np.arange(5, dtype=np.float32)
    assert list(shift(x, 5, 2)) == [2, 3, 4, 0, 0]
    assert list(shift(x, 5, -2)) == [0, 0, 0, 1, 2]
    assert list(shift(x, 5, 0)) == list(x)


def chain(lib, *items):
    return [BlockSpec(CAT[m], m).with_values(v) for m, v in items]


def test_renderer_batches_each_depth_in_one_pool_call_and_equals_reference():
    lib = make_lib()
    a = chain(lib, ("d_hard", {}), ("a_dark", {"Tone": 0.2}), ("e_echo", {}))
    b = chain(lib, ("d_hard", {}), ("a_dark", {"Tone": 0.8}), ("e_echo", {}))
    c = chain(lib, ("d_soft", {}), ("a_dark", {"Tone": 0.2}), ("e_slap", {}))
    pool = FakePool()
    r = NativeChainRenderer(pool, lib, DRY, 100)
    ref = ChainRenderer(RawHost(), lib, DRY, 100)
    assert r.cost_of([a, b, c]) == ref.cost_of([a, b, c]) == 8
    got, want = r.render_batch([a, b, c]), ref.render_batch([a, b, c])
    assert pool.batches == [2, 3, 3] and r.calls == 3      # one pool call per depth, across parents
    assert r.spent == ref.spent == 8
    n = len(DRY) - 3 * TRIM
    for g, w, ch in zip(got, want, (a, b, c), strict=True):
        exact = composed([lib.preset(s.model, s.values()) for s in ch])
        assert np.allclose(g, exact, atol=1e-5)               # contract engine: exact
        assert np.allclose(g[:n], w[:n], atol=1e-5)           # same as the reference renderer
    r.render_batch([a])
    assert r.spent == 8 and r.calls == 3                      # cached prefix: no call


def test_renderer_on_engine_groups_by_input():
    lib = make_lib()
    eng = ContractEngine()
    r = NativeChainRenderer(eng, lib, DRY, 100)
    a = chain(lib, ("d_hard", {}), ("e_echo", {"Mix": 0.2}))
    b = chain(lib, ("d_hard", {}), ("e_echo", {"Mix": 0.6}))
    c = chain(lib, ("d_soft", {}), ("e_echo", {"Mix": 0.2}))
    r.render_batch([a, b, c])
    # depth 1: both drives share the dry input (one call of 2); depth 2: two inputs (2 + 1)
    assert sorted(eng.calls) == [1, 2, 2]


def test_renderer_cap_and_failures():
    lib = make_lib()
    pool = FakePool(fail={"e_slap"})
    r = NativeChainRenderer(pool, lib, DRY, 3)
    with pytest.raises(BudgetExhausted):
        r.render_batch([chain(lib, ("d_hard", {}), ("a_dark", {}), ("e_echo", {}), ("c_cab", {}))])
    assert pool.batches == [] and r.spent == 0
    good = chain(lib, ("d_hard", {}), ("e_echo", {}))
    bad = chain(lib, ("d_hard", {}), ("e_slap", {}))
    out = r.render_batch([good, bad])
    assert out[0] is not None and out[1] is None and r.spent == 3
    assert r.errors and r.errors[-1][0] == "e_slap"


class Kit:
    def score(self, target, cand, *, stems=None, mask=None, weights=None):
        t, c = np.asarray(target, np.float64), np.asarray(cand, np.float64)[: len(target)]
        return StubScore(float(np.sqrt(np.mean((t - c) ** 2) / np.mean(t ** 2))))


def test_build_chain_native_end_to_end_on_a_fake_pool():
    lib = make_lib()
    truth = [lib.preset("d_hard", {"Gain": 0.7}), lib.preset("e_echo", {"Time": 0.4, "Mix": 0.5})]
    target = composed(truth).astype(np.float32)
    pool = FakePool()
    res = build_chain_native(target, DRY, pool, Kit(), library=lib, validate=False,
                             probs={"drive": 1, "delay": 1}, estimates={"delay": {"time_s": 0.3}},
                             budget=ChainBudget(max_renders=300, top_k=2, seed=0),
                             mode=ScoreMode(None, False, "fake"),
                             structures_fn=lambda p, k, context=None: [(("drive", "delay"), 0.6), (("delay", "drive"), 0.4)][:k])
    assert res.renders <= 300 and res.structure == ("drive", "delay")
    assert [b.model for b in res.chain] == ["d_hard", "e_echo"]
    assert res.score < 0.3
    assert lib.estimates == {"delay": {"time_s": 0.3}}
    assert all(n <= 16 for n in pool.batches) and pool.batches
