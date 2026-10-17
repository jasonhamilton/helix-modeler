"""Values outside the narrowed CC ranges (T098): full-range fallbacks, on-demand capture, range API."""
from __future__ import annotations

from test_native_cc_range import CAT, delay_preset

from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

V12, V11, V1 = lib.LADDER
NARROW = {"0:@enabled": [0.0, 1.0], "0:Time": [0.0, 1.27], "0:Mix": [0.0, 1.0]}
WIDE = {"0:@enabled": [0.0, 1.0], "0:Time": [0.0, 8.0], "0:Mix": [0.0, 1.0]}
AMAP = {"0:@enabled": "cc_1", "0:Time": "cc_2", "0:Mix": "cc_3"}


def template(p, ranges, name, state=b"s"):
    baked, h = nat.baked_fingerprint(p, AMAP, list(AMAP))
    return nat.ChainTemplate(nat.chain_key(p), state, dict(AMAP), {k: list(v) for k, v in ranges.items()},
                             name=name, verified=list(AMAP), baked=baked, baked_hash=h)


class FakeHost:
    def __init__(self):
        self.calls = []
        self.info = {}

    def call(self, cmd, payload=b"", **kw):
        self.calls.append((cmd, payload, kw))
        return {}, b""


def engine(on_demand=False, version=V12):
    """A NativeEngine on the library store of ``version`` with a fake host (no Native)."""
    e = nat.NativeEngine.__new__(nat.NativeEngine)
    tdir = lib.library_templates(version)
    e.route, e.host, e._hc_mode, e.templates_dir = "automation", FakeHost(), None, tdir
    e._variants = nat.ChainTemplate.load_variants(tdir)
    e.library_version = lib.version_of(tdir)
    e.fallback_dirs = lib.fallback_template_dirs(tdir)
    e.on_demand, e._on_demand_capture, e._on_demand_failed, e.last_on_demand = on_demand, None, {}, None
    e.autocapture = False
    e.current_chain = e.current_template = e._loaded_preset = e._loaded_state = None
    e._host_params, e._params_since_load, e._widened = {}, {}, False
    return e


def test_out_of_range_value_loads_through_an_older_full_range_template(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path))
    near, far = delay_preset(0.6), delay_preset(3.0)
    template(near, NARROW, "v1.2 narrow", b"narrow").save(lib.library_templates(V12))
    template(near, WIDE, "v1 wide", b"wide").save(lib.library_templates(V1))
    assert lib.fallback_versions(V12) == [V1]  # v1.1 not on disk here
    assert lib.version_of(lib.library_templates(V12)) == V12
    assert lib.version_of(lib.on_demand_templates(V12)) == V12
    # library API: the out-of-range value goes to the full-range template ...
    assert lib.match(far, V12).state == b"wide" and lib.out_of_range_refs(far, V12) == []
    # ... and an in-range value still gets the finer grid
    assert lib.match(near, V12).state == b"narrow"
    # the engine picks the same, and the primary narrow template still refuses the far value
    e = engine()
    assert e._match_template(far, nat.chain_key(far))[0].state == b"wide"
    assert e._match_template(near, nat.chain_key(near))[0].state == b"narrow"
    e.load(far)
    assert e.current_template.state == b"wide"
    sent = [kw["values"] for c, _, kw in e.host.calls if c == "set_params"][-1]
    assert abs(sent["cc_2"] - 3.0 / 8.0) < 1e-9  # Time driven over 0..8 s, not clamped at 1.27 s


def test_set_params_switches_template_and_back_history_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path))
    near = delay_preset(0.6)
    template(near, NARROW, "narrow", b"narrow").save(lib.library_templates(V12))
    template(near, WIDE, "wide", b"wide").save(lib.library_templates(V1))
    e = engine()
    e.load(near)
    assert e.current_template.state == b"narrow"
    e.set_params({(0, "Time"): 2.0})  # outside 0..1.27: reload on the wide template, not a clamp
    assert e.current_template.state == b"wide" and e._params_since_load == {(0, "Time"): 2.0}
    assert e._loaded_preset is near
    e.set_params({(0, "Time"): 0.6})  # fits the fine grid again: back on it
    assert e.current_template.state == b"narrow"
    sent = [kw["values"] for c, _, kw in e.host.calls if c == "set_params"][-1]
    assert abs(sent["cc_2"] - 0.6 / 1.27) < 1e-9


def test_on_demand_capture_is_triggered_once_then_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path))
    near, far = delay_preset(0.6), delay_preset(3.0)
    template(near, NARROW, "narrow", b"narrow").save(lib.library_templates(V12))  # no older versions
    calls = []

    def fake_capture(p, d):
        calls.append((p.slots[0].params["Time"], d))
        return template(p, WIDE, "on-demand wide", b"ondemand")

    e = engine(on_demand=True)
    e._on_demand_capture = fake_capture
    t, why = e._resolve_template(far, nat.chain_key(far))
    assert t is not None and t.state == b"ondemand" and why == ""
    assert calls == [(3.0, lib.on_demand_templates(V12))]
    assert e.last_on_demand["wide"] == ["0:Time"]
    idx = lib.load_index(V12)
    (stem, ent), = idx["on_demand"].items()
    assert ent["wide"] == ["0:Time"] and ent["ranges"] == {"0:Time": [0.0, 8.0]} and ent["models"] == ["HD2_DelayX"]
    assert (lib.on_demand_templates(V12) / f"{stem}.state").read_bytes() == b"ondemand"
    # cached: the same engine, a fresh engine (a new process) and the library API reuse it
    assert e._resolve_template(delay_preset(5.0), nat.chain_key(far))[0].state == b"ondemand"
    e2 = engine(on_demand=True)
    e2._on_demand_capture = fake_capture
    assert e2._resolve_template(far, nat.chain_key(far))[0].state == b"ondemand"
    assert lib.template_for_preset(far, V12, capture=fake_capture).state == b"ondemand"
    assert len(calls) == 1
    # an in-range value still picks the finer grid, on-demand variant present or not
    assert e2._resolve_template(near, nat.chain_key(near))[0].state == b"narrow"
    assert lib.match(near, V12).state == b"narrow"


def test_on_demand_off_or_failed_reports_the_range_and_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path))
    near, far = delay_preset(0.6), delay_preset(3.0)
    template(near, NARROW, "narrow").save(lib.library_templates(V12))
    e = engine(on_demand=False)
    t, why = e._resolve_template(far, nat.chain_key(far))
    assert t is None and "outside its CC range" in why
    calls = []

    def no_gui(p, d):
        calls.append(1)
        raise nat.UnsupportedModelChange("Native autocapture unavailable: no GUI session")

    e = engine(on_demand=True)
    e._on_demand_capture = no_gui
    t, why = e._resolve_template(far, nat.chain_key(far))
    assert t is None and "on-demand full-range capture failed" in why and not e.on_demand
    e._resolve_template(far, nat.chain_key(far))
    assert calls == [1]
    # no template for the structure at all: nothing to widen, no capture
    e = engine(on_demand=True)
    e._on_demand_capture = no_gui
    other = lib.single_block("HD2_ModX", CAT, near)
    assert e._resolve_template(other, nat.chain_key(other))[0] is None and calls == [1]


def test_param_ranges_expose_grid_and_full_catalog_range():
    near = delay_preset(0.6)
    t = template(near, NARROW, "narrow")
    pr = t.param_ranges(CAT)
    assert set(pr) == {"0:Time", "0:Mix"}  # bypass switch left out
    assert pr["0:Time"]["grid"] == (0.0, 1.27) and pr["0:Time"]["full"] == (0.0, 8.0) and pr["0:Time"]["narrowed"]
    assert abs(pr["0:Time"]["step"] - 0.01) < 1e-12 and pr["0:Time"]["live"]
    assert pr["0:Mix"]["full"] == (0.0, 1.0) and not pr["0:Mix"]["narrowed"]
    assert t.full_range("0:Time", CAT) == (0.0, 8.0)
    assert lib.full_range("HD2_DelayX", "Time", CAT) == (0.0, 8.0)
    assert lib.full_range("HD2_DelayX", "Nope", CAT) is None
