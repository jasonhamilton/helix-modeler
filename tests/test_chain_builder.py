"""Chain builder (T105) on fakes: fake block library, fake engine, fake loss, fake structures."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import lfilter

from tonematch.chain_prior import top_structures
from tonematch.engines.base import EngineError
from tonematch.preset import Preset, Slot
from tonematch.search.chain_builder import (
    TRIM,
    BlockSpec,
    BudgetExhausted,
    ChainBudget,
    ChainEvaluator,
    ChainRenderer,
    ScoreMode,
    assemble_preset,
    build_chain,
    render_chain,
    rung_sizes,
)
from tonematch.search.stubs import StubScore, fake_dry

PARAMS = {
    "d_soft": {"Gain": (0, 1, 0.5), "Level": (0, 1, 0.5)},
    "d_hard": {"Gain": (0, 1, 0.5), "Level": (0, 1, 0.5)},
    "a_dark": {"Tone": (0, 1, 0.5)},
    "a_bright": {"Tone": (0, 1, 0.5)},
    "e_echo": {"Mix": (0, 1, 0.3), "Time": (0, 1, 0.5)},
    "e_slap": {"Mix": (0, 1, 0.3), "Time": (0, 1, 0.5)},
    "r_room": {"Mix": (0, 1, 0.3)},
}
CATS = {"drive": ["d_soft", "d_hard"], "amp": ["a_dark", "a_bright"], "delay": ["e_echo", "e_slap"], "reverb": ["r_room"]}
CAT_OF = {m: c for c, ms in CATS.items() for m in ms}


class FakeLib:
    def preset(self, model, values):
        ps = {k: s for k, (_, _, s) in PARAMS[model].items()} | dict(values)
        return Preset("lib", [Slot(CAT_OF[model], model, ps, True)])

    def shortlist(self, category, n):
        return CATS.get(category, [])[:n]

    def free_params(self, model):
        return PARAMS[model]


def dsp(model, p, x):
    x = np.asarray(x, np.float64)
    if model == "d_soft":
        return np.tanh((1 + 8 * p["Gain"]) * x) * (0.2 + 1.6 * p["Level"])
    if model == "d_hard":
        y = np.clip((1 + 8 * p["Gain"]) * x, -0.5, 0.5) * (0.4 + 3.2 * p["Level"])
        return y - 0.9 * lfilter([0.1], [1, -0.9], y)      # tight and bright: a different spectrum
    if model in ("a_dark", "a_bright"):
        a = 0.02 + 0.95 * (1 - p["Tone"])
        lo = lfilter([1 - a], [1, -a], x)
        return lo if model == "a_dark" else x - 0.8 * lo
    if model in ("e_echo", "e_slap"):
        d = int((300 if model == "e_slap" else 3000) + 6000 * p["Time"])
        return x + p["Mix"] * np.concatenate([np.zeros(d), x[:-d]])
    if model == "r_room":
        return x + p["Mix"] * lfilter([1], [1, -0.9], x) * 0.1
    raise EngineError(model)


class FakeEngine:
    """A pool stand-in: render_many of single-block presets, delayed TRIM samples like Native."""

    def __init__(self):
        self.renders = 0
        self.calls: list[int] = []

    def load(self, preset):
        self.p = preset

    def set_params(self, ch):
        pass

    def render(self, dry):
        return self.render_many(dry, [self.p])[0]

    def render_many(self, dry, variants):
        self.calls.append(len(variants))
        out = []
        for v in variants:
            s = v.slots[0]
            self.renders += 1
            y = dsp(s.model_id, s.params, dry)
            out.append(np.concatenate([np.zeros(TRIM), y[:-TRIM]]).astype(np.float32))
        return out


class FakeKit:
    """Log-spectrogram distance plus aligned waveform error (like a known-DI loss); records the weights."""

    def __init__(self):
        self.weights_seen = []

    def _spec(self, x, n):
        x = np.asarray(x, np.float64)[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
        return np.log(np.abs(np.fft.rfft(x, axis=1)) + 1e-4)

    def score(self, target, cand, *, stems=None, mask=None, weights=None):
        self.weights_seen.append(weights)
        d = sum(float(np.mean(np.abs(self._spec(target, n) - self._spec(cand, n)))) for n in (512, 4096)) / 2
        t, c = np.asarray(target, np.float64), np.asarray(cand, np.float64)[: len(target)]
        nmse = np.mean((t - c) ** 2) / np.mean(t ** 2)          # the DI is aligned: waveform error counts
        return StubScore(float(d + np.sqrt(nmse)))


def chain_of(*items):
    return [BlockSpec(CAT_OF[m], m).with_values(v) for m, v in items]


def true_chain():
    return chain_of(("d_hard", {"Gain": 0.7, "Level": 0.5}), ("a_dark", {"Tone": 0.3}),
                    ("e_echo", {"Mix": 0.5, "Time": 0.4}))


def target_for(dry, chain=None):
    eng = FakeEngine()
    r = ChainRenderer(eng, FakeLib(), dry, 1000)
    return r.render_batch([chain or true_chain()])[0]


DRY = lfilter([0.15], [1, -0.85], fake_dry(1.0, seed=3)).astype(np.float32) * 3   # smooth: echoes correlate over ~100 samples
STRUCTS = [(("drive", "amp", "delay"), 0.45), (("amp", "drive", "delay"), 0.25),
           (("drive", "amp"), 0.2), (("amp", "delay"), 0.1)]


def fake_structures(probs, k, context=None):
    return STRUCTS[:k]


# ------------------------------------------------------------------ rendering

def test_render_chain_is_serial_with_trim():
    lib, eng = FakeLib(), FakeEngine()
    presets = [lib.preset("d_soft", {}), lib.preset("a_dark", {})]
    y = render_chain(eng, presets, DRY)
    x = DRY.astype(np.float64)
    for p in presets:
        s = p.slots[0]
        x = dsp(s.model_id, s.params, x)
    # each instance is TRIM late and trimmed: the chain equals the plain composition
    assert np.allclose(y[: len(DRY) - 2 * TRIM], x[: len(DRY) - 2 * TRIM], atol=1e-4)
    assert eng.renders == 2 and len(y) == len(DRY)


def test_renderer_caches_prefix_and_batches_by_parent():
    eng, lib = FakeEngine(), FakeLib()
    r = ChainRenderer(eng, lib, DRY, 100)
    a = chain_of(("d_soft", {}), ("a_dark", {"Tone": 0.2}), ("e_echo", {}))
    b = chain_of(("d_soft", {}), ("a_dark", {"Tone": 0.8}), ("e_echo", {}))
    assert r.cost_of([a, b]) == 5            # shared drive; amp and delay differ
    r.render_batch([a, b])
    assert r.spent == 5 and eng.renders == 5
    assert eng.calls[:2] == [1, 2]           # drive alone, then both amps in one render_many
    r.render_batch([a])                      # fully cached
    assert r.spent == 5


def test_renderer_hard_cap_renders_nothing_when_over():
    eng = FakeEngine()
    r = ChainRenderer(eng, FakeLib(), DRY, 2)
    with pytest.raises(BudgetExhausted):
        r.render_batch([true_chain()])
    assert eng.renders == 0 and r.spent == 0


def test_engine_error_scores_bad_not_crash():
    class Boom(FakeEngine):
        def render_many(self, dry, variants):
            raise EngineError("x")
    r = ChainRenderer(Boom(), FakeLib(), DRY, 50)
    ev = ChainEvaluator(r, FakeKit(), DRY, ScoreMode(None, False, "t"))
    assert ev.score_many([true_chain()])[0] >= 1e8


# ------------------------------------------------------------------ search

def seed_from_measured(category, estimate, model):
    """What the presence module's delay estimate gives: a starting Time (it measures the echo spacing)."""
    if category == "delay" and "time_s" in estimate:
        return {"Time": (estimate["time_s"] * 48000 - 3000) / 6000}
    return {}


def run(budget_renders=700, kit=None, structures=fake_structures, mode=None, k=4, validator=None, seed=0):
    tgt = target_for(DRY)
    eng = FakeEngine()
    kit = kit or FakeKit()
    mode = mode or ScoreMode(None, False, "fake")
    res = build_chain(tgt, DRY, eng, FakeLib(), kit, budget=ChainBudget(max_renders=budget_renders, top_k=k, seed=seed),
                      mode=mode, probs={"drive": 1, "amp": 1, "delay": 1}, structures_fn=structures, validator=validator,
                      estimates={"delay": {"time_s": 5400 / 48000}}, seed_fn=seed_from_measured)
    return res, eng, kit


def test_recovers_synthetic_three_block_target():
    res, _, _ = run(budget_renders=3000)
    assert res.structure == ("drive", "amp", "delay")
    assert [b.model for b in res.chain] == ["d_hard", "a_dark", "e_echo"]
    assert res.score < 0.3          # the default chain scores about 0.8
    truth = [b.values() for b in true_chain()]
    got = [b.values() for b in res.chain]
    # the delay mix and the amp tone are well determined by the loss
    assert abs(got[1]["Tone"] - truth[1]["Tone"]) < 0.15
    assert abs(got[2]["Mix"] - truth[2]["Mix"]) < 0.2


def test_structure_order_follows_the_prior():
    got = top_structures({"drive": 1.0, "amp": 1.0, "cab": 1.0, "delay": 1.0, "reverb": 1.0}, k=3)
    assert got[0][0] == ("drive", "amp", "cab", "delay", "reverb")
    seen = []

    def spy(probs, k, context=None):
        out = top_structures(probs, k, context=context)
        seen.extend(out)
        return out
    tgt = target_for(DRY)
    lib = FakeLib()
    res = build_chain(tgt, DRY, FakeEngine(), lib, FakeKit(), budget=ChainBudget(max_renders=40, top_k=3),
                      mode=ScoreMode(None, False, "t"), probs={"drive": 1, "amp": 1, "delay": 1, "reverb": 1},
                      structures_fn=spy)
    reported = [tuple(s["structure"]) for s in res.structures]
    ps = [s["prior_p"] for s in res.structures]
    assert reported == [s for s, _ in seen] and ps == sorted(ps, reverse=True)


def test_budget_is_respected():
    for cap in (25, 120, 400):
        res, eng, _ = run(budget_renders=cap)
        assert res.renders <= cap and eng.renders == res.renders <= cap


def test_halving_drops_weak_structures():
    res, _, _ = run(budget_renders=900)
    by = {tuple(s["structure"]): s for s in res.structures}
    assert not by[("drive", "amp", "delay")]["dropped"]
    assert by[("amp", "delay")]["dropped"]           # no drive: cannot reach the target
    assert by[("amp", "delay")]["renders"] < by[("drive", "amp", "delay")]["renders"]
    assert rung_sizes(4, 2) == [4, 2, 1]


def test_known_di_mode_uses_admitted_weights_and_level_guard():
    from tonematch.metrics import KNOWN_DI_WEIGHTS
    kit = FakeKit()
    mode = ScoreMode.known_di_mode()
    res, _, _ = run(budget_renders=150, kit=kit, mode=mode)
    assert all(w == KNOWN_DI_WEIGHTS for w in kit.weights_seen)
    assert "known-DI" in res.mode_label
    tgt = target_for(DRY)
    r = ChainRenderer(FakeEngine(), FakeLib(), DRY, 50)
    ev = ChainEvaluator(r, kit, tgt, mode)
    quiet = chain_of(("d_hard", {"Gain": 0.7, "Level": 0.0}), ("a_dark", {"Tone": 0.3}),
                     ("e_echo", {"Mix": 0.5, "Time": 0.4}))
    base = ChainEvaluator(r, kit, tgt, ScoreMode(None, False, "x"))
    assert ev.score_many([quiet])[0] > base.score_many([quiet])[0] + 0.5   # 12+ dB too quiet is penalised


def test_song_mode_is_labelled_provisional():
    assert "PROVISIONAL" in ScoreMode.song_mode().label
    assert ScoreMode.song_mode().weights


def test_validator_refuses_structures_before_rendering():
    res, _, _ = run(budget_renders=200, validator=lambda ch: ["dsp over budget"] if len(ch) == 2 else [])
    by = {tuple(s["structure"]): s for s in res.structures}
    assert by[("drive", "amp")]["dropped"] and by[("drive", "amp")]["renders"] == 0
    assert "refused before rendering" in by[("amp", "delay")]["notes"][0]


def test_eight_block_limit_and_unknown_category():
    big = lambda p, k, context=None: [(("reverb",) * 9, 0.5), (("drive", "amp"), 0.5)]
    res, _, _ = run(budget_renders=60, structures=big)
    assert res.structure == ("drive", "amp")
    with pytest.raises(ValueError):
        run(budget_renders=30, structures=lambda p, k, context=None: [(("pitch",), 1.0)])


def test_assemble_preset_orders_blocks_and_links_cab():
    class L(FakeLib):
        def preset(self, model, values):
            p = super().preset(model, values) if model in PARAMS else Preset("lib", [Slot("cab", model, {}, True)])
            return p
    ch = [*chain_of(("d_soft", {}), ("a_dark", {})), BlockSpec("cab", "c_1"), *chain_of(("e_echo", {}))]
    p = assemble_preset(ch, L())
    assert [s.category for s in p.slots] == ["drive", "amp", "cab", "delay"]
    assert [s.position for s in p.slots] == [0, 1, None, 2]
    assert p.slots[1].extra["@cab"] == p.slots[2].key == "cab0"
    with pytest.raises(ValueError):
        assemble_preset([BlockSpec("reverb", "r_room")] * 9, FakeLib())
    from tonematch.preset import dumps_hlx
    assert "cab0" in dumps_hlx(p.to_hlx_dict())
