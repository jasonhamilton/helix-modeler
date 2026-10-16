"""The Native single-block library (T048): capture order, frozen values, index lookup by model id."""
from __future__ import annotations

import pytest
from test_preset import synth

from tonematch import preset as P
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

CAT = {"models": {
    "HD2_DistA": {"category": "drive", "stomp": True, "params": {"Gain": {"kind": "continuous", "min": 0.0,
                                                                        "max": 1.0, "default": 0.5}}},
    "HD2_DistB": {"category": "drive", "stomp": True, "params": {}},
    "HD2_DelayA": {"category": "delay", "stomp": True, "params": {}},
    "HD2_ReverbA": {"category": "reverb", "stomp": True, "stereo": True, "mono": True, "params": {}},
    "HD2_CompA": {"category": "comp", "stomp": True, "params": {}},
    "HD2_CompBad": {"category": "comp", "stomp": True, "params": {}},
    "HD2_AmpA": {"category": "amp", "stomp": True, "params": {
        "Drive": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.5},
        "Hum": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.5},
        "Ripple": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.5}}},
    "HD2_CabA": {"category": "cab", "stomp": True, "params": {
        "EarlyReflections": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.3}}},
    "HD2_CabAWithPan": {"category": "cab", "stomp": True, "params": {}},
    "HelixStomp_AppDSPFlowSplitY": {"category": "other", "stomp": True, "params": {}},
    "HD2_HelixOnly": {"category": "drive", "stomp": False, "params": {}},
}}


def test_order_is_breadth_first_by_tier_and_skips_routing_refused_and_non_stomp():
    got = lib.library_models(CAT, skip={"HD2_CompBad"})
    assert got == ["HD2_DistA", "HD2_DelayA", "HD2_ReverbA", "HD2_DistB", "HD2_CompA", "HD2_AmpA", "HD2_CabA"]


def test_block_preset_writes_frozen_values_of_both_policies():
    fz = lib.frozen_values()
    assert fz[("amp", "Hum")] == 0.0 and fz[("amp", "Ripple")] == 0.0 and fz[("cab", "EarlyReflections")] == 0.0
    base = P.from_dict(synth(), CAT)
    amp = lib.block_preset("HD2_AmpA", CAT, base)
    assert len(amp.slots) == 1 and amp.slots[0].params == {"Drive": 0.5, "Hum": 0.0, "Ripple": 0.0}
    assert lib.block_preset("HD2_CabA", CAT, base).slots[0].params == {"EarlyReflections": 0.0}
    rev = lib.block_preset("HD2_ReverbA", CAT, base).slots[0]
    assert rev.extra["@stereo"] is False and rev.extra["@path"] == 0


def test_index_finds_template_by_model_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path))
    base = P.from_dict(synth(), CAT)
    p = lib.block_preset("HD2_AmpA", CAT, base)
    amap = {"0:@enabled": "cc_1", "0:Drive": "cc_2", "0:Hum": "cc_3", "0:Ripple": "cc_4"}
    verified = ["0:@enabled", "0:Drive", "0:Hum"]  # Ripple's CC did not move the render: baked at 0
    baked, h = nat.baked_fingerprint(p, amap, verified)
    t = nat.ChainTemplate(nat.chain_key(p), b"state", amap, {}, name=p.name, verified=verified,
                          baked=baked, baked_hash=h)
    t.save(lib.library_templates())
    e = lib.entry("HD2_AmpA", "amp", t, p)
    assert e["frozen_verified"] == ["0:Hum"] and e["frozen_baked"] == ["0:Ripple"]
    lib.save_index({"version": lib.VERSION, "models": {"HD2_AmpA": e}, "failed": {}})
    got = lib.template_for("HD2_AmpA")
    assert got is not None and got.state == b"state" and lib.matches(got, p)
    assert lib.template_for("HD2_CabA") is None
    assert set(lib.library()) == {"HD2_AmpA"}
    p.slots[0].params["Ripple"] = 0.5  # unfrozen: the baked Ripple no longer matches
    assert not lib.matches(got, p)
    p.slots[0].params.update(Ripple=0.0, Drive=0.9)  # a CC-driven param may move freely
    assert lib.matches(got, p)


def test_frozen_values_refuse_disagreeing_policies(monkeypatch):
    from tonematch import param_policy

    monkeypatch.setattr(param_policy, "frozen_for",
                        lambda eng: {("amp", "Hum"): 0.0 if eng == "native" else 0.5})
    with pytest.raises(ValueError):
        lib.frozen_values()
