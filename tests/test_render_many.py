"""render_many (contract amendment 2026-10-16) and the engine-push small items (N4, N6).

Fake hosts only: no Native, no pedalboard."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pytest

from tonematch.engines import base
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib
from tonematch.engines.base import EngineError, UnsupportedModelChange


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("TONEMATCH_NATIVE_START_STATE", "0")
    monkeypatch.setattr(nat, "_load_codec", lambda: None)


@dataclass
class _Slot:
    category: str
    model_id: str | None
    params: dict = field(default_factory=dict)
    enabled: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class _Preset:
    slots: list
    name: str = "p"


def amp(drive=0.5, tone=5.0, model="HD2_AmpA"):
    return _Preset([_Slot("amp", model, {"Drive": drive, "Tone": tone})])


class FakeHost:
    """A host whose render is a gain set by the CC values and the state (reset per state)."""

    instances: ClassVar[list] = []

    def __init__(self, plugin, plugin_name=None, block_size=512, initial_state=None):
        self.state, self.params, self.renders = b"START", {}, 0
        self.mem_mb, self.canary_respawns = None, 0
        self.info = {"has_state": True, "channels": 1, "version": "fake",
                     "params": [{"name": f"cc_{i}"} for i in range(16)]}
        FakeHost.instances.append(self)

    def call(self, cmd, payload=b"", **kw):
        if cmd in ("set_state", "reload_with_state"):
            self.state, self.params = payload, {}
            return {}, b""
        if cmd == "get_state":
            return {}, self.state
        if cmd == "set_params":
            self.params.update(kw["values"])
            return {"read_back": kw["values"]}, b""
        if cmd == "render":
            self.renders += 1
            x = np.frombuffer(payload, dtype="<f4")
            g = 1.0 + 0.5 * self.params.get("cc_1", 0.0) + 0.25 * self.params.get("cc_2", 0.0)
            g += 0.1 * len(self.state)
            return {"reset_via": "state"}, (x * g).astype("<f4").tobytes()
        raise AssertionError(cmd)

    def close(self):
        pass

    def restart(self):
        pass


def _template(p, d):
    amap = {"0:Drive": "cc_1", "0:Tone": "cc_2"}
    verified = list(amap)
    baked, h = nat.baked_fingerprint(p, amap, verified)
    t = nat.ChainTemplate(nat.chain_key(p), b"STATE-" + p.slots[0].model_id.encode(), amap,
                          {"0:Drive": [0.0, 1.0], "0:Tone": [0.0, 10.0]}, name=p.slots[0].model_id,
                          verified=verified, baked=baked, baked_hash=h)
    t.save(d)
    return t


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(nat, "HostProcess", FakeHost)
    monkeypatch.setattr(nat, "max_hosts_by_memory", lambda *a, **k: 4)
    monkeypatch.setenv("TONEMATCH_NATIVE_MANY_HOSTS", "3")
    d = tmp_path / "tpl"
    _template(amp(), d)
    _template(amp(model="HD2_AmpB"), d)
    e = nat.NativeEngine("fake.vst3", templates_dir=d, autocapture=False)
    yield e
    e.close()


DRY = (np.random.default_rng(0).standard_normal(2400) * 0.1).astype(np.float32)


# --------------------------------------------------------------------------- the fallback


def test_fallback_loads_presets_and_set_params_dicts():
    class Pre:
        def __init__(self, g):
            self.g = g

    class Eng:
        def __init__(self):
            self.g, self.calls = 0.0, []

        def load(self, p):
            self.calls.append("load")
            self.g = p.g

        def set_params(self, ch):
            self.calls.append("set")
            self.g += sum(ch.values())

        def render(self, dry):
            self.calls.append("render")
            return np.asarray(dry, np.float32) * self.g

    e = Eng()
    out = base.render_many(e, np.ones(3, np.float32), [Pre(1.0), {(0, "a"): 1.0}, {(0, "b"): 2.0}, Pre(5.0)])
    assert [float(y[0]) for y in out] == [1.0, 2.0, 4.0, 5.0]  # dicts accumulate on the loaded one
    assert e.calls == ["load", "render", "set", "render", "set", "render", "load", "render"]


def test_helper_uses_the_engines_own_render_many():
    class Own:
        def render_many(self, dry, variants):
            return ["own"] * len(variants)

    assert base.render_many(Own(), DRY, [1, 2]) == ["own", "own"]


def test_fallback_raises_unsupported_model_change():
    class Eng:
        def load(self, p):
            raise UnsupportedModelChange("no template")

        def render(self, dry):
            raise AssertionError("never reached")

    with pytest.raises(UnsupportedModelChange):
        base.render_many(Eng(), DRY, [object()])


# --------------------------------------------------------------------------- Native


def _serial(engine, variants):
    return base.serial_render_many(engine, DRY, variants)


def test_native_render_many_matches_serial_in_variant_order(engine, tmp_path):
    variants = [amp(0.1), {(0, "Drive"): 0.9}, {(0, "Tone"): 2.0}, amp(0.7, 8.0, "HD2_AmpB"),
                {(0, "Tone"): 0.0}, amp(0.3), {(0, "Drive"): 0.0}]
    n0 = len(FakeHost.instances)
    got = engine.render_many(DRY, variants)
    assert len(FakeHost.instances) - n0 == 2  # 3 hosts in all: this engine's own plus 2
    assert sum(h.renders > 0 for h in FakeHost.instances[n0:]) >= 1
    ref_engine = nat.NativeEngine("fake.vst3", templates_dir=tmp_path / "tpl", autocapture=False)
    try:
        want = _serial(ref_engine, variants)
    finally:
        ref_engine.close()
    assert len(got) == len(want) == len(variants)
    for g, w in zip(got, want, strict=True):
        assert np.array_equal(g, w)
    gains = [float(np.median(g[:-16] / DRY[16:])) for g in got]  # trimmed by 16 samples
    assert len({round(x, 6) for x in gains}) == len(gains)  # all different: order is meaningful


def test_native_render_many_dicts_first_apply_to_the_loaded_preset(engine, tmp_path):
    engine.load(amp(0.2))
    engine.set_params({(0, "Tone"): 4.0})
    got = engine.render_many(DRY, [{(0, "Drive"): 0.6}, {(0, "Tone"): 9.0}, {(0, "Drive"): 0.1}])
    ref = nat.NativeEngine("fake.vst3", templates_dir=tmp_path / "tpl", autocapture=False)
    try:
        ref.load(amp(0.2))
        ref.set_params({(0, "Tone"): 4.0})
        want = _serial(ref, [{(0, "Drive"): 0.6}, {(0, "Tone"): 9.0}, {(0, "Drive"): 0.1}])
    finally:
        ref.close()
    assert all(np.array_equal(g, w) for g, w in zip(got, want, strict=True))


def test_native_render_many_raises_unsupported_model_change_before_rendering(engine):
    n0 = sum(h.renders for h in FakeHost.instances)
    with pytest.raises(UnsupportedModelChange):
        engine.render_many(DRY, [amp(0.1), amp(0.2, model="HD2_NotCaptured"), amp(0.3)])
    assert sum(h.renders for h in FakeHost.instances) == n0


def test_native_render_many_needs_a_load_before_a_dict(engine):
    with pytest.raises(EngineError, match="before any load"):
        engine.render_many(DRY, [{(0, "Drive"): 0.5}, amp()])


def test_native_render_many_one_host_is_serial(engine, monkeypatch):
    monkeypatch.setenv("TONEMATCH_NATIVE_MANY_HOSTS", "1")
    n0 = len(FakeHost.instances)
    out = engine.render_many(DRY, [amp(0.1), amp(0.9)])
    assert len(out) == 2 and len(FakeHost.instances) == n0  # no extra host opened


def test_base_helper_dispatches_to_native(engine):
    out = base.render_many(engine, DRY, [amp(0.1), {(0, "Drive"): 0.4}])
    assert len(out) == 2 and engine._many_pool is not None


# --------------------------------------------------------------------------- N6a exact lo


def test_exact_lo_command_is_exactly_zero():
    assert nat.cc_norm(0.0, 0.0, 1.0) == 0.0
    assert nat.cc_norm(0.1, 0.1, 6.45) == 0.0
    lo32 = nat.cc_value(0.1, 6.45, 0)  # float32(0.1) != 0.1
    assert lo32 != 0.1 and nat.cc_norm(lo32, 0.1, 6.45) == 0.0
    assert nat.cc_norm(nat.cc_value(0.1, 6.45, 127), 0.1, 6.45) == 1.0
    assert nat.cc_norm(-60.0, -25.75, 6.0) == 0.0  # below range clips
    assert nat.cc_norm(0.5, 0.0, 1.0) == 0.5


def test_set_params_sends_exact_zero_at_lo(engine):
    engine.load(amp(0.8, 7.0))
    engine.set_params({(0, "Drive"): 0.0, (0, "Tone"): nat.cc_value(0.0, 10.0, 0)})
    h = engine.host
    assert h.params["cc_1"] == 0.0 and h.params["cc_2"] == 0.0
    assert engine._host_params["cc_1"] == 0.0


# --------------------------------------------------------------------------- N6b pre-roll


def test_preroll_default_follows_the_loaded_chain(engine, monkeypatch):
    assert engine.preroll == nat.NativeEngine.PREROLL
    engine.current_chain = (("delay", "HD2_DelaySimpleDelay"),)
    assert engine.preroll == nat.NativeEngine.TAIL_PREROLL == 3 * 48_000
    engine.current_chain = (("amp", "HD2_AmpA"), ("reverb", "HD2_ReverbRoom"))
    assert engine.preroll == nat.NativeEngine.TAIL_PREROLL
    engine.current_chain = (("amp", "HD2_AmpA"), ("reverb", None))  # empty slot: no tail
    assert engine.preroll == nat.NativeEngine.PREROLL
    engine.preroll = 1234
    assert engine.preroll == 1234


# --------------------------------------------------------------------------- N4 time variant


def test_is_time_variant_from_the_index_and_the_fallback():
    assert lib.is_time_variant("HD2_ReverbOcto")  # no index cached: the measured fallback
    assert not lib.is_time_variant("HD2_DistScream808")
    lib.save_index({"version": lib.VERSION, "failed": {},
                    "models": {"HD2_Other": {"category": "mod", "time_variant": True},
                               "HD2_DistScream808": {"category": "drive"}}})
    tv = lib.time_variant_models()
    assert "HD2_Other" in tv and "VIC_DelayGlitch" in tv and "HD2_DistScream808" not in tv


# --------------------------------------------------------------------------- N6d Block Level


def test_block_level_rule_v2():
    p = {"kind": "continuous", "min": -60.0, "max": 6.0, "default": 0.0, "display": "volume"}
    assert nat.CC_RANGE_RULES_VERSION == "cc-range-v2"
    assert nat.narrow_range("drive", "Level", p) == (-25.75, 6.0)
    assert nat.narrow_range("drive", "Send", p) is None
    assert nat.narrow_range("drive", "Level", {**p, "min": -120.0}) is None
    lo, hi = nat.BLOCK_LEVEL_RANGE_DB
    assert (hi - lo) / nat.CC_STEPS == 0.25
    assert abs(nat.cc_value(lo, hi, 103)) < 1e-5  # 0 dB on the grid
    steps = np.diff([nat.cc_value(lo, hi, c) for c in range(128)])
    assert np.allclose(steps, 0.25, atol=1e-5)


def test_block_level_value_outside_the_narrowed_range_keeps_the_catalog_range():
    cat = {"models": {"HD2_DistA": {"category": "drive", "params": {
        "Level": {"kind": "continuous", "min": -60.0, "max": 6.0, "default": 0.0, "display": "volume"}}}}}
    p = _Preset([_Slot("drive", "HD2_DistA", {"Level": 0.0})])
    assert nat.cc_ranges(p, cat)["0:Level"] == (-25.75, 6.0)
    p.slots[0].params["Level"] = -40.0
    assert nat.cc_ranges(p, cat)["0:Level"] == (-60.0, 6.0)
    json.dumps(nat.cc_ranges(p, cat))
