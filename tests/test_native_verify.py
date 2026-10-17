"""T103 verification suite: pure helpers of scripts/native_verify.py, the in-run sentinel, the host's
warm-up after a state load or knob move, and the baked-param guard. No Native, no pedalboard."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
needs_known = pytest.mark.skipif(not (ROOT / "fixtures" / "smoke" / "known.hlx").exists(),
                                 reason="fixtures/smoke/known.hlx is local only (gitignored)")

import native_verify as NV

from tonematch.engines import native as nat
from tonematch.engines import native_host as nh
from tonematch.engines.base import EngineError

# --------------------------------------------------------------------------- pure helpers


def test_population_skips_time_variant_refused_and_failed():
    idx = {"models": {"a": {"category": "drive"}, "b": {"category": "delay", "time_variant": True},
                      "c": {"category": "reverb"}, "d": {"category": "mod"}},
           "failed": {"g": "no template"}}
    pop, skipped = NV.population(idx, {"c"}, {"d"})
    assert pop["drive"] == ["a"] and pop["delay"] == [] and pop["reverb"] == [] and pop["mod"] == []
    assert any(s.startswith("b ") for s in skipped) and any(s.startswith("c ") for s in skipped)
    assert any(s.startswith("d ") and "refused" in s for s in skipped) and any(s.startswith("g ") for s in skipped)


def test_draw_models_per_category_without_replacement_then_repeats():
    pop = {c: [f"{c}{i}" for i in range(20)] for c in NV.CATEGORIES}
    pop["eq"] = [f"eq{i}" for i in range(8)]
    got = NV.draw_models(pop, 13, 3, random.Random(1))
    assert len(got) == 13 * len(NV.CATEGORIES) + 3
    for c in NV.CATEGORIES:
        ms = [m for m, cc in got[:-3] if cc == c]
        assert len(ms) == 13
        if c != "eq":
            assert len(set(ms)) == 13  # no repeats while the category lasts
    assert {m for m, c in got if c == "eq"} == set(pop["eq"])  # all 8 EQs used before any repeat


def _tpl():
    return nat.ChainTemplate(chain=(("delay", "M"),), state=b"S",
                             map={"0:@enabled": "cc_1", "0:Time": "cc_2", "0:Mix": "cc_3", "0:Spread": "cc_4"},
                             ranges={"0:@enabled": [0, 1], "0:Time": [0.0, 1.27], "0:Mix": [0.0, 1.0],
                                     "0:Spread": [0.0, 1.0]},
                             verified=["0:@enabled", "0:Time", "0:Mix"], baked={"0:Spread": 0.5}, baked_hash="h")


def test_live_params_are_verified_continuous_without_bypass():
    assert [r for r, *_ in NV.live_params(_tpl())] == ["0:Mix", "0:Time"]


def test_draw_cc_grid_and_ends():
    params = NV.live_params(_tpl())
    rng = random.Random(3)
    draws = [NV.draw_cc(rng, params) for _ in range(2000)]
    vals = [c for d in draws for c in d.values()]
    assert all(0 <= c <= 127 and isinstance(c, int) for c in vals)
    ends = sum(c in NV.END_STEPS for c in vals) / len(vals)
    assert 0.3 < ends < 0.45  # 0.35 forced + uniform hits on the six end steps
    assert {0, 127} <= set(vals)


@needs_known
def test_apply_cc_writes_cc_value_on_the_templates_range():
    from tonematch.preset import read_hlx

    p = read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx")
    t = nat.ChainTemplate(chain=nat.chain_key(p), state=b"S", map={"0:Drive": "cc_1"}, ranges={"0:Drive": [0.0, 1.0]},
                          verified=["0:Drive"])
    q = NV.apply_cc(p, t, {"0:Drive": 127})
    assert q.slots[0].params["Drive"] == nat.cc_value(0.0, 1.0, 127) == 1.0
    assert p.slots[0].params["Drive"] != 1.0  # copy, original untouched
    assert NV.apply_cc(p, t, {"0:Drive": 0}).slots[0].params["Drive"] == 0.0


def test_verdict_rules():
    assert NV.verdict(-100.0, None) == (True, "exact")
    assert NV.verdict(-99.9, None)[0] is False
    assert NV.verdict(-60.0, -62.0)[0] is True        # noisy: within floor + 3 dB
    assert NV.verdict(-58.0, -62.0)[0] is False       # 4 dB worse than its floor
    assert NV.verdict(-60.0, -150.0)[0] is False      # a quiet floor gives no slack
    assert NV.verdict(None, None, silent=True) == (True, "silent")
    assert NV.verdict(float("nan"), None)[0] is False


def test_compare_silent_and_exact():
    z = np.zeros(1000, np.float32)
    assert NV.compare(z, z)["why"] == "silent"
    a = np.sin(np.arange(1000) / 5).astype(np.float32)
    assert NV.compare(a, a.copy())["ok"]
    assert not NV.compare(a, a * np.float32(1.01))["ok"]


@needs_known
def test_hlx_doc_has_no_controller_assignments():
    from tonematch.preset import read_hlx

    d = NV.hlx_doc(read_hlx(ROOT / "fixtures" / "smoke" / "known.hlx"))
    ctl = d["data"]["tone"].get("controller", {})
    assert all(v == {} for v in ctl.values())


# --------------------------------------------------------------------------- sentinel (fake host)

N = 4800 + 24000
REF = np.sin(np.arange(24000) / 9.0).astype(np.float32)


class FakeHost(nat.HostProcess):
    def __init__(self, modes="A", reprepared="", spawn=""):
        self.mode = modes
        self.rep = iter(reprepared)
        self.spawn = iter(spawn)
        self.state = b"START"
        self.params: dict = {}
        self.calls: list = []
        self.canary = True
        self.canary_respawns = self.canary_reprepares = self.canary_fixed = 0
        self.canary_null_db = None
        self.mem_mb = None
        self.info = {"has_state": True}

    def call(self, cmd, payload=b"", **kw):
        self.calls.append(cmd)
        if cmd == "set_state":
            self.state, self.params = payload, {}
        elif cmd == "set_params":
            self.params.update(kw["values"])
        elif cmd == "reprepare":
            self.mode = next(self.rep, self.mode)
        elif cmd == "render":
            x = np.frombuffer(payload, dtype="<f4")
            if self.state == b"SENT":
                y = np.concatenate([np.zeros(4800, np.float32), REF * np.float32(1.0 if self.mode == "A" else 1.001)])
                return {}, y.astype("<f4").tobytes()
            return {}, x.astype("<f4").tobytes()
        return {}, b""

    def restart(self):
        self.mode = next(self.spawn, "A")
        self.state, self.params = b"START", {}


def _engine(host, every=3):
    e = object.__new__(nat.NativeEngine)
    e.host, e.plugin, e.plugin_name = host, "fake.vst3", None
    e._initial_state, e._hc_mode = b"START", None
    e._loaded_state, e._host_params = b"CHAIN", {"cc_1": 0.5}
    e.recycles, e.renders, e._since_sentinel = 0, 0, 0
    e.sentinel = {"checks": 0, "drifts": 0, "fixed": 0, "recycles": 0, "suspect_renders": 0, "skipped": 0,
                  "worst_db": None, "off": ""}
    e._sentinel_state, e._sentinel_ref, e.sentinel_every = b"SENT", REF.copy(), every
    e.reset_mode, e._preroll_fixed, e._plugin_latency = "reset", 0, 0
    e.current_chain, e.calibration_db, e.mem_limit_mb, e.last_gain_db = None, 0.0, 0.0, 0.0
    host.state, host.params = b"CHAIN", {"cc_1": 0.5}
    return e


def test_sentinel_no_drift_restores_the_loaded_chain():
    h = FakeHost("A")
    e = _engine(h)
    assert e.sentinel_check() <= nat.CANARY_THRESHOLD_DB
    assert e.sentinel["checks"] == 1 and e.sentinel["drifts"] == 0
    assert h.state == b"CHAIN" and h.params == {"cc_1": 0.5}


def test_sentinel_drift_is_fixed_in_place_and_counted():
    h = FakeHost("B", reprepared="BA")
    e = _engine(h)
    e._since_sentinel = 7
    e.sentinel_check()
    st = e.stats()
    assert st["sentinel_drifts"] == 1 and st["sentinel_fixed"] == 1 and st["sentinel_recycles"] == 0
    assert st["sentinel_suspect_renders"] == 7 and st["canary_reprepares"] == 2
    assert h.mode == "A" and h.state == b"CHAIN" and h.params == {"cc_1": 0.5}


def test_sentinel_recycles_when_reprepare_does_not_help():
    h = FakeHost("B", reprepared="", spawn="A")
    e = _engine(h)
    e.sentinel_check()
    assert e.sentinel["recycles"] == 1 and e.recycles == 1 and h.mode == "A"
    assert h.state == b"CHAIN" and h.params == {"cc_1": 0.5}


def test_sentinel_raises_when_recycling_does_not_help():
    e = _engine(FakeHost("B", reprepared="", spawn="B"))
    with pytest.raises(EngineError, match="after recycling"):
        e.sentinel_check()


def test_sentinel_strict_raises(monkeypatch):
    monkeypatch.setenv("TONEMATCH_NATIVE_SENTINEL_STRICT", "1")
    e = _engine(FakeHost("B", reprepared="A"))
    with pytest.raises(EngineError, match="STRICT"):
        e.sentinel_check()
    assert e.sentinel["drifts"] == 1


def test_render_runs_the_sentinel_every_n_renders():
    h = FakeHost("A")
    e = _engine(h, every=3)
    x = (0.1 * np.sin(np.arange(4800) / 3.0)).astype(np.float32)
    for _ in range(7):
        np.testing.assert_array_equal(e.render(x), x)
    assert e.stats()["renders"] == 7 and e.sentinel["checks"] == 2  # before renders 4 and 7
    h.mode = "B"  # the host drifts behind the engine's back
    h.rep = iter("A")
    for _ in range(3):
        e.render(x)
    assert e.sentinel["drifts"] == 1 and e.sentinel["fixed"] == 1 and e.sentinel["suspect_renders"] == 3


def test_sentinel_skipped_in_another_hc_mode():
    e = _engine(FakeHost("A"))
    e._hc_mode = 7
    assert e.sentinel_check() is None and e.sentinel["skipped"] == 1 and e.sentinel["checks"] == 0


def test_pool_stats_sum_the_sentinel():
    a, b = _engine(FakeHost("A")), _engine(FakeHost("B", reprepared="A"))
    a.sentinel_check()
    b.sentinel_check()
    pool = object.__new__(nat.NativeRenderPool)
    pool.engines = [a, b]
    st = pool.stats()
    assert st["sentinel_checks"] == 2 and st["sentinel_drifts"] == 1 and st["sentinel_fixed"] == 1
    assert st["sentinel_worst_db"] is not None and st["sentinel_worst_db"] > nat.CANARY_THRESHOLD_DB


def test_sentinel_every_env(monkeypatch):
    monkeypatch.setenv("TONEMATCH_NATIVE_SENTINEL_EVERY", "0")
    assert nat.sentinel_every() == 0
    monkeypatch.setenv("TONEMATCH_NATIVE_SENTINEL_EVERY", "x")
    assert nat.sentinel_every() == nat.SENTINEL_EVERY
    monkeypatch.delenv("TONEMATCH_NATIVE_SENTINEL_EVERY")
    assert nat.sentinel_every() == 50


# --------------------------------------------------------------------------- host warm-up (T103)


class _Prm:
    def __init__(self):
        self.raw_value = 0.0


class _Plugin:
    reported_latency_samples = 0

    def __init__(self):
        self.raw_state = b"S0"
        self.parameters = {"cc_1": _Prm()}
        self.blocks: list[int] = []

    def process(self, x, sr, buffer_size=512, reset=False):
        self.blocks.append(x.shape[-1])
        return x


def _host():
    h = nh.Host()
    h.plugin, h.reset_strategy = _Plugin(), "state"
    h._rebase(b"S0")
    return h


def _render(h, n=1000):
    h.plugin.blocks.clear()
    h.render(np.zeros(n, np.float32).astype("<f4").tobytes(), reset=True)
    return h.plugin.blocks[:-1]  # the pads before the render itself


def test_reset_render_warms_up_a_new_state_and_keeps_alignment(monkeypatch):
    monkeypatch.setattr(nh, "WARMUP", 24_000)  # opt-in (TONEMATCH_NATIVE_WARMUP); default 0
    h = _host()
    frame = nh.reset_frame(48_000)
    pads = _render(h)
    assert sum(pads) >= nh.WARMUP and h.processed % frame == 1000  # aligned before the 1000-sample render
    pads = _render(h)  # nothing changed: alignment only
    assert sum(pads) == frame - 1000
    h.set_state(b"S1")
    assert sum(_render(h)) >= nh.WARMUP
    h.set_params({"cc_1": 0.3})  # a knob move warms up too
    assert sum(_render(h)) >= nh.WARMUP


def test_warmup_zero_restores_the_old_pad(monkeypatch):
    monkeypatch.setattr(nh, "WARMUP", 0)
    h = _host()
    h.processed = nh.reset_frame(48_000) - 10
    assert sum(_render(h)) == 10


# --------------------------------------------------------------------------- baked-param guard (2d)


def test_moves_baked_only_for_unverified_params_off_their_baked_value():
    t = _tpl()
    assert not nat.NativeEngine._moves_baked(t, {(0, "Spread"): 0.5})   # its baked value
    assert nat.NativeEngine._moves_baked(t, {(0, "Spread"): 0.7})       # unverified, moved
    assert not nat.NativeEngine._moves_baked(t, {(0, "Time"): 0.7})     # verified
    assert not nat.NativeEngine._moves_baked(t, {(0, "@enabled"): 0.0})
    t.baked_hash = ""
    assert not nat.NativeEngine._moves_baked(t, {(0, "Spread"): 0.7})   # no fingerprint: load() refuses it anyway


def test_on_demand_defaults_off(monkeypatch):
    monkeypatch.delenv("TONEMATCH_NATIVE_ONDEMAND", raising=False)
    assert nat.on_demand_enabled() is False
    monkeypatch.setenv("TONEMATCH_NATIVE_ONDEMAND", "1")
    assert nat.on_demand_enabled() is True


# --------------------------------------------------------------------------- load() and the CC policy


def test_load_never_sends_a_baked_params_cc():
    """T103 audit: sending an unverified (baked) param's CC snapped an off-grid value to the grid
    (Simple Delay Feedback 0.35 -> -66 dB). load() sends verified params and bypass only."""
    from tonematch.preset import Preset, Slot

    tpl = _tpl()
    p = Preset(name="x", slots=[Slot(category="delay", model_id="M", params={"Time": 0.5, "Mix": 0.3, "Spread": 0.5},
                                     enabled=True)])
    vals, h = nat.baked_fingerprint(p, tpl.map, tpl.verified)
    tpl.baked, tpl.baked_hash = vals, h
    host = FakeHost("A")
    e = _engine(host)
    e.route, e._variants, e.fallback_dirs, e.on_demand, e.autocapture = "automation", {tpl.chain: [tpl]}, [], False, False
    e.current_chain = e.current_template = e._loaded_preset = None
    e._params_since_load = {}
    e.load(p)
    sent = host.params
    assert set(sent) == {"cc_1", "cc_2", "cc_3"}  # bypass, Time, Mix; never cc_4 (Spread, baked)
    with pytest.raises(nat.UnsupportedModelChange):
        e.set_params({(0, "Spread"): 0.9})  # moving the baked param is refused, not sent
    assert "cc_4" not in host.params
    e.set_params({(0, "Spread"): 0.5})  # its own baked value: a no-op
    assert "cc_4" not in host.params


def test_cc_policy_bakes_unreliable_library_params(monkeypatch):
    from tonematch.engines import native_library as NL
    from tonematch.preset import Preset, Slot

    lib = Preset(name="lib", slots=[Slot(category="reverb", model_id="HD2_ReverbNonLinear",
                                         params={"Mod": 0.5, "Mix": 0.4}, enabled=True)])
    monkeypatch.setattr(NL, "block_preset", lambda m, *a, **k: lib)
    t = nat.ChainTemplate(chain=(("reverb", "HD2_ReverbNonLinear"),), state=b"S",
                          map={"0:@enabled": "cc_1", "0:Mod": "cc_2", "0:Mix": "cc_3"},
                          ranges={"0:@enabled": [0, 1], "0:Mod": [0, 1], "0:Mix": [0, 1]},
                          verified=["0:@enabled", "0:Mod", "0:Mix"], baked={}, baked_hash="old")
    NL.apply_cc_policy(t)
    assert "0:Mod" not in t.verified and t.baked["0:Mod"] == 0.5
    assert t.baked_hash == nat.baked_fingerprint(lib, t.map, t.verified)[1]
    other = nat.ChainTemplate(chain=(("reverb", "HD2_ReverbHall"),), state=b"S", verified=["0:Mod"])
    assert NL.apply_cc_policy(other).verified == ["0:Mod"]


def test_io_blocks():
    from tonematch.engines import native_library as NL

    assert NL.is_io_block("HD2_FXLoopStereo1_2") and NL.is_io_block("HD2_SendMono1") and NL.is_io_block("HD2_ReturnMono2")
    assert not NL.is_io_block("HD2_ReverbHall") and not NL.is_io_block("HD2_DelaySimpleDelay")


class LatchHost(FakeHost):
    """FakeHost whose start state renders differently when the amp latch is lost."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.initial_state, self.primed, self.latched, self.primes = b"START", True, True, 0

    def call(self, cmd, payload=b"", **kw):
        if cmd == "render" and self.state == b"START":
            y = np.concatenate([np.zeros(4800, np.float32), REF[::-1] * np.float32(1.0 if self.latched else 1.002)])
            return {}, y.astype("<f4").tobytes()
        return super().call(cmd, payload, **kw)

    def _prime(self):
        self.latched, self.primes = True, self.primes + 1


def test_sentinel_relatches_a_host_that_lost_the_amp_latch():
    h = LatchHost()
    e = _engine(h)
    e._latch_ref = np.ascontiguousarray(REF[::-1])
    assert e.sentinel_check() <= nat.CANARY_THRESHOLD_DB
    h.latched = False  # e.g. a prepare behind the engine's back
    e.sentinel_check()
    assert e.sentinel["drifts"] == 1 and e.sentinel["fixed"] == 1 and h.primes == 1 and h.latched
    assert e.sentinel["recycles"] == 0 and h.state == b"CHAIN"
