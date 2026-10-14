"""Preset tests. Synthetic data only; the golden round-trip test skips when goldens are absent."""
import copy
import json
from pathlib import Path

import pytest

from tonematch import preset as P

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"

CAT = {"models": {
    "HD2_AmpFake": {"category": "amp", "stomp": True, "load": 20.0, "load_stereo": None,
                    "params": {"Drive": {"min": 0.0, "max": 1.0}}},
    "HD2_CabFake": {"category": "cab", "stomp": True, "load": 7.0, "load_stereo": None,
                    "params": {"Level": {"min": -20.0, "max": 20.0}}},
    "HD2_DelayFake": {"category": "delay", "stomp": True, "load": 4.0, "load_stereo": 8.0,
                      "params": {"Mix": {"min": 0.0, "max": 1.0}}},
}}


def synth() -> dict:
    snap = {"@name": "S", "@valid": True, "blocks": {"dsp0": {"block0": True, "block1": False}},
            "controllers": {"dsp0": {"block1": {"Mix": {"@fs_enabled": False, "@value": 0.25}}}}}
    return {"data": {
        "meta": {"name": "synthetic"}, "device": P.STOMP_DEVICE_ID, "device_version": 58720256,
        "tone": {
            "dsp0": {
                "inputA": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 1},
                "block0": {"@model": "HD2_AmpFake", "@enabled": True, "@position": 1, "@path": 0,
                           "@cab": "cab0", "Drive": 0.5},
                "cab0": {"@model": "HD2_CabFake", "@enabled": True, "Level": 0.0},
                "block1": {"@model": "HD2_DelayFake", "@enabled": False, "@position": 2,
                           "@path": 0, "@stereo": False, "Mix": 0.25},
                "outputA": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1}},
            "controller": {"dsp0": {"block1": {"Mix": {"@controller": 8, "@min": 0, "@max": 1}}}},
            "snapshot0": copy.deepcopy(snap), "snapshot1": copy.deepcopy(snap),
            "snapshot2": copy.deepcopy(snap)}},
        "meta": {"original": 0}, "schema": "L6Preset", "version": 6}


def load(raw=None, cat=CAT):
    return P.from_dict(raw or synth(), cat)


def checks(viol, sev="fail"):
    return {v.check for v in viol if v.severity == sev}


def test_slots_order_and_cab_pairing():
    pre = load()
    assert [(s.key, s.category) for s in pre.slots] == [
        ("block0", "amp"), ("cab0", "cab"), ("block1", "delay")]
    assert len(pre.snapshots) == 3 and pre.snapshots[0].params["block1"]["Mix"] == 0.25


def test_dumps_format_and_roundtrip_stability(tmp_path):
    pre = load()
    f = P.write_hlx(pre, tmp_path / "a.hlx")
    text = f.read_text()
    assert '"device" : 2162694' in text and not text.endswith("\n")
    assert json.loads(text) == pre.to_hlx_dict()
    again = P.write_hlx(P.read_hlx(f, CAT), tmp_path / "b.hlx")
    assert again.read_text() == text


def test_float_17_digits():
    assert P.dumps_hlx({"a": 0.52}) == '{\n  "a" : 0.52000000000000002\n}'
    assert P.dumps_hlx({"a": {}}) == '{\n  "a" : {\n\n  }\n}'


def test_valid_preset_has_no_fail():
    assert checks(P.validate(load(), CAT)) == set()


def test_wrong_device_and_unknown_model_and_range():
    raw = synth()
    raw["data"]["device"] = 2162691
    raw["data"]["tone"]["dsp0"]["block0"]["Drive"] = 3.0
    raw["data"]["tone"]["dsp0"]["block1"]["@model"] = "HD2_Nope"
    assert {"device", "param_range", "model"} <= checks(P.validate(load(raw), CAT))


def test_unknown_param_and_missing_path():
    raw = synth()
    raw["data"]["tone"]["dsp0"]["block0"]["Bogus"] = 1
    del raw["data"]["tone"]["dsp0"]["block1"]["@path"]
    assert {"param", "block_keys"} <= checks(P.validate(load(raw), CAT))


def test_block_count_limit():
    pre = load()
    for i in range(9):
        pre.slots.append(P.Slot("delay", "HD2_DelayFake", {}, True, None, 10 + i, {"@path": 0}))
    assert "block_count" in checks(P.validate(pre, CAT))


def test_dsp_budget_enforced_when_set():
    costs = P.build_dsp_costs(CAT, budget=10.0)
    assert "dsp_budget" in checks(P.validate(load(), CAT, costs))
    costs["budget"] = None
    assert "dsp_budget" not in checks(P.validate(load(), CAT, costs))
    costs["heavy"] = ["HD2_AmpFake"]
    assert "dsp_budget" in checks(P.validate(load(), CAT, costs))


def test_cost_uses_stereo_flag():
    costs = P.build_dsp_costs(CAT)
    pre = load()
    base, _ = P.dsp_total(pre, costs)
    pre.slots[-1].extra["@stereo"] = True
    assert P.dsp_total(pre, costs)[0] == base + 4.0


def test_snapshot_block_sets_must_match():
    raw = synth()
    raw["data"]["tone"]["snapshot2"]["blocks"]["dsp0"]["block9"] = True
    assert "snapshots" in checks(P.validate(load(raw), CAT))


def test_cab_without_amp_fails_full_rig():
    pre = load()
    pre.slots = [s for s in pre.slots if s.category != "amp"]
    assert "amp_cab" in checks(P.validate(pre, CAT))


def test_effects_only_export_drops_amp_cab_and_records_standin():
    out = P.export(load(), "effects_only", {"amp": "HD2_AmpFake", "cab": "HD2_CabFake"})
    d = out.to_hlx_dict()
    assert [s.category for s in out.slots] == ["delay"]
    assert "block0" not in d["data"]["tone"]["dsp0"] and "cab0" not in d["data"]["tone"]["dsp0"]
    assert d["data"]["meta"]["tonematch"]["standin"]["amp"] == "HD2_AmpFake"
    assert "block0" not in d["data"]["tone"]["snapshot0"]["blocks"]["dsp0"]
    assert checks(P.validate(out, CAT)) == set()


def test_effects_only_without_standin_fails():
    pre = load()
    pre.export_mode = "effects_only"
    pre.slots = [s for s in pre.slots if s.category not in ("amp", "cab")]
    assert "export_mode" in checks(P.validate(pre, CAT))


def test_edit_applies_to_dict():
    pre = load()
    pre.slots[0].params["Drive"] = 0.9
    pre.snapshots[1].params["block1"]["Mix"] = 0.4
    d = pre.to_hlx_dict()["data"]["tone"]
    assert d["dsp0"]["block0"]["Drive"] == 0.9
    assert d["snapshot1"]["controllers"]["dsp0"]["block1"]["Mix"]["@value"] == 0.4
    assert load().slots[0].params["Drive"] == 0.5  # original untouched


def test_golden_diff_reports_missing_and_added():
    a, g = synth(), synth()
    del a["data"]["tone"]["controller"]
    g["data"]["tone"]["variax"] = {}
    d = P.golden_diff(a, g)
    assert d["tone"] == {"missing": ["controller", "variax"], "added": []}
    assert P.golden_diff(g, a)["tone"] == {"missing": [], "added": ["controller", "variax"]}


@pytest.mark.skipif(not (GOLDEN / "HXStomp.hlx").exists(), reason="golden exports are local only")
@pytest.mark.parametrize("name", ["HXStomp.hlx", "HelixNative.hlx"])
def test_golden_roundtrip_byte_equal(name):
    f = GOLDEN / name
    if not f.exists():
        pytest.skip("golden missing")
    assert P.dumps_hlx(P.read_hlx(f).to_hlx_dict()) == f.read_text(encoding="utf-8")
