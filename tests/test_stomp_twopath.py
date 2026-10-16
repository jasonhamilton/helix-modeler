"""T063: two-path (Path A + Path B) presets get HX Edit's SABJ layout on the HX Stomp.

Offline. The Python side (`stomp_layout.ensure_sabj`, `reamp_hlx_dict`, the junction readback) uses
synthetic .hlx dicts. The compiler tests run the local `stompd-compile` build when it and a local
device document exist (cache/, never in the repo) and are skipped otherwise."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from test_stomp import _p

from tonematch import preset as P
from tonematch.engines import stomp
from tonematch.engines import stomp_readback as RB
from tonematch.engines.stomp_layout import (
    HX_EDIT_JOIN,
    HX_EDIT_SPLIT_Y,
    ensure_sabj,
    junction_expectations,
    path_b_blocks,
)

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- fixtures


def single_template(path_b: bool = True) -> dict:
    """A single-path template as our tools write it (junctions inherited from the HX Stomp golden:
    Balance A/B 0/1, A/B Pan 0/1, topology "A"), optionally with one block on Path B."""
    dsp = {
        "inputA": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 6},
        "inputB": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 0},
        "outputA": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1},
        "outputB": {"@model": "HelixStomp_AppDSPFlowOutputSend", "@output": 0},
        "split": {"@model": "HD2_AppDSPFlowSplitY", "BalanceA": 0, "BalanceB": 1, "bypass": False,
                  "@position": 0, "@enabled": True, "@no_snapshot_bypass": False},
        "join": {"@model": "HD2_AppDSPFlowJoin", "A Level": 0, "A Pan": 0, "B Level": 0, "B Pan": 1,
                 "B Polarity": False, "Level": 0, "@position": 8, "@enabled": True, "@no_snapshot_bypass": False},
        "block0": {"@model": "HD2_AmpEssexA15", "@position": 1, "@path": 0, "@enabled": True, "Drive": 0.6},
    }
    if path_b:
        dsp["block1"] = {"@model": "HD2_AmpBritPlexiBrt", "@position": 0, "@path": 1, "@enabled": True, "Drive": 0.7}
    blocks = {"block0": True, **({"block1": True} if path_b else {})}
    return {"data": {"device": 2162694, "meta": {"name": "t"}, "tone": {
        "global": {"@topology0": "A", "@current_snapshot": 0, "@tempo": 120},
        "dsp0": dsp,
        "snapshot0": {"@name": "S1", "@valid": True, "blocks": {"dsp0": dict(blocks)}},
        "snapshot1": {"@name": "S2", "@valid": True, "blocks": {"dsp0": dict(blocks)}}}},
        "meta": {"original": 0, "pbn": 0, "premium": 0}, "schema": "L6Preset", "version": 6}


def hx_edit_sabj() -> dict:
    """An HX Edit two-path export: split before cell 4, join before cell 6, its own values."""
    d = single_template(path_b=False)
    t = d["data"]["tone"]
    t["global"]["@topology0"] = "SABJ"
    t["dsp0"]["split"].update({"BalanceA": 0.3, "BalanceB": 0.7, "@position": 4})
    t["dsp0"]["join"].update({"A Pan": 0.4, "B Pan": 0.6, "@position": 6})
    t["dsp0"]["block1"] = {"@model": "HD2_Cab4x12XXLV30", "@position": 5, "@path": 1, "@enabled": True}
    return d


# --------------------------------------------------------------------------- ensure_sabj


def test_path_b_block_on_a_single_path_template_gets_hx_edit_sabj():
    d = single_template()
    changes = ensure_sabj(d)
    t = d["data"]["tone"]
    dsp = t["dsp0"]
    assert changes
    assert t["global"]["@topology0"] == "SABJ"
    for k, v in HX_EDIT_SPLIT_Y.items():
        assert dsp["split"][k] == v
    for k, v in HX_EDIT_JOIN.items():
        assert dsp["join"][k] == v
    assert dsp["split"]["BalanceA"] == dsp["split"]["BalanceB"] == 0.5     # both paths get the same signal
    assert dsp["join"]["A Pan"] == dsp["join"]["B Pan"] == 0.5             # and are mixed equally
    assert (dsp["split"]["@position"], dsp["join"]["@position"]) == (0, 8)  # the whole row in parallel
    assert dsp["inputB"]["@input"] == 0 and dsp["outputB"]["@output"] == 0
    assert all(t[f"snapshot{n}"]["blocks"]["dsp0"]["split"] is True for n in (0, 1))
    assert ensure_sabj(d) == []   # idempotent


def test_single_path_file_is_left_alone():
    d = single_template(path_b=False)
    before = copy.deepcopy(d)
    assert ensure_sabj(d) == []
    assert d == before


def test_hx_edit_sabj_export_keeps_its_own_junctions():
    d = hx_edit_sabj()
    before = copy.deepcopy(d)
    assert ensure_sabj(d) == []
    assert d == before


def test_positions_can_be_given_and_must_be_ordered():
    d = single_template()
    ensure_sabj(d, split_position=2, join_position=5)
    assert (d["data"]["tone"]["dsp0"]["split"]["@position"], d["data"]["tone"]["dsp0"]["join"]["@position"]) == (2, 5)
    with pytest.raises(ValueError, match="before join"):
        ensure_sabj(single_template(), split_position=5, join_position=5)


def test_other_split_type_becomes_a_y_split_without_its_old_values():
    d = single_template()
    d["data"]["tone"]["dsp0"]["split"] = {"@model": "HD2_AppDSPFlowSplitXOver", "Frequency": 500.0,
                                          "Reverse": False, "bypass": False, "@position": 0, "@enabled": True}
    ensure_sabj(d)
    s = d["data"]["tone"]["dsp0"]["split"]
    assert s["@model"] == "HD2_AppDSPFlowSplitY" and "Frequency" not in s and s["BalanceA"] == 0.5


def test_reamp_hlx_dict_writes_sabj_for_two_path_presets():
    pre = P.from_dict(single_template())
    d = stomp.reamp_hlx_dict(pre, 6)
    assert path_b_blocks(d) == ["block1"]
    assert d["data"]["tone"]["global"]["@topology0"] == "SABJ"
    assert d["data"]["tone"]["dsp0"]["join"]["B Pan"] == 0.5
    single = stomp.reamp_hlx_dict(P.from_dict(single_template(path_b=False)), 6)
    assert single["data"]["tone"]["global"]["@topology0"] == "A"
    assert single["data"]["tone"]["dsp0"]["split"]["BalanceB"] == 1   # untouched


# --------------------------------------------------------------------------- junction readback


def junction_chain(bal=(0.5, 0.5), pan=(0.5, 0.5), attach=(1, 9), with_attach=True):
    split = {"pos": 10, "kind": "Split", "enabled": True, "model_id": "HD2_AppDSPFlowSplitY",
             "params": [_p(0, "BalanceA", bal[0]), _p(1, "BalanceB", bal[1]), _p(2, "bypass", 0.0, kind="Switch")]}
    join = {"pos": 19, "kind": "Join", "enabled": True, "model_id": "HD2_AppDSPFlowJoin",
            "params": [_p(0, "A Level", 0.0, -60, 12), _p(1, "A Pan", pan[0]), _p(2, "B Level", 0.0, -60, 12),
                       _p(3, "B Pan", pan[1]), _p(4, "B Polarity", 0.0, kind="Switch"), _p(5, "Level", 0.0, -60, 12)]}
    if with_attach:
        split["attach"], join["attach"] = attach
    return {"slots": [
        {"pos": 0, "kind": "Input", "enabled": True, "routing": 6},
        {"pos": 2, "kind": "Block", "enabled": True, "model_id": "HD2_AmpEssexA15", "params": [_p(0, "Drive", 0.6)]},
        {"pos": 9, "kind": "Output", "enabled": True, "routing": 1},
        split,
        {"pos": 11, "kind": "Block", "enabled": True, "model_id": "HD2_AmpBritPlexiBrt", "params": [_p(0, "Drive", 0.7)]},
        join]}


def test_junction_expectations():
    d = single_template()
    ensure_sabj(d)
    e = junction_expectations(d)
    assert e["split"]["attach"] == 1 and e["join"]["attach"] == 9
    assert junction_expectations(single_template(path_b=False)) == {}


def test_readback_accepts_hx_edit_junctions():
    d = single_template()
    ensure_sabj(d)
    diff = RB.readback_diff(junction_chain(), d)
    assert diff.ok, diff.as_dict()


def test_readback_catches_the_old_layout():
    """What the compiler wrote before T063: the base's 0/1 values and unattached junctions."""
    d = single_template()
    ensure_sabj(d)
    diff = RB.readback_diff(junction_chain(bal=(0, 1), pan=(0, 1), attach=(0, 0)), d)
    assert not diff.ok
    text = " ".join(diff.mismatches)
    assert "split.BalanceA" in text and "join.B Pan" in text and "attaches before slot 0" in text


def test_live_chain_without_attach_skips_only_the_attach_check():
    d = single_template()
    ensure_sabj(d)
    assert RB.readback_diff(junction_chain(attach=(0, 0), with_attach=False), d).ok
    assert not RB.readback_diff(junction_chain(pan=(0, 1), with_attach=False), d).ok


# --------------------------------------------------------------------------- the real compiler (local only)

BIN = ROOT / "cache" / "stomp" / "bin"
NEW, OLD = BIN / "stompd-compile", BIN / "stompd-compile.pre-sabj"
BASE = ROOT / "cache" / "stomp" / "live" / "base04C.bin"
K0 = ROOT / "cache" / "stomp" / "hw" / "presets" / "K0.hlx"
local = pytest.mark.skipif(not (NEW.exists() and BASE.exists() and K0.exists()
                                and stomp.supports(NEW, "compile") and b"attach points" in NEW.read_bytes()),
                           reason="needs the local stompd-compile (T063 build), an 04C backup and K0.hlx")


def _two_path_k0(tmp_path: Path, split_pos: int, join_pos: int, sabj: bool = True) -> Path:
    d = json.loads(K0.read_text())
    t = d["data"]["tone"]
    amp = next(k for k, v in t["dsp0"].items() if k.startswith("block") and v.get("@cab"))
    b = copy.deepcopy(t["dsp0"][amp])
    b.update({"@path": 1, "@position": 3, "@cab": "cab9"})
    t["dsp0"]["block19"], t["dsp0"]["cab9"] = b, copy.deepcopy(t["dsp0"][t["dsp0"][amp]["@cab"]])
    if sabj:
        ensure_sabj(d, split_position=split_pos, join_position=join_pos)
    p = tmp_path / "two.hlx"
    p.write_text(json.dumps(d))
    return p


@local
def test_compiler_writes_attach_points_and_hx_edit_values(tmp_path):
    rep = stomp.compile_document(_two_path_k0(tmp_path, 1, 6), BASE, tmp_path / "o.bin", compiler=NEW)
    assert rep["topology"] == "SABJ"
    ch = stomp.decode_document(tmp_path / "o.bin", compiler=NEW)
    j = {s["kind"]: s for s in ch["slots"] if s["kind"] in ("Split", "Join")}
    assert (j["Split"]["attach"], j["Join"]["attach"]) == (2, 7)        # @position + input + 1
    assert j["Split"]["b_routing"] == 0 and j["Join"]["b_routing"] == 0
    vals = {p["sym"]: p["value"] for s in j.values() for p in s["params"]}
    assert vals["BalanceA"] == vals["BalanceB"] == vals["A Pan"] == vals["B Pan"] == 0.5
    assert any(s["pos"] == 14 and s["kind"] == "Block" for s in ch["slots"])   # Path B cell 3 = slot 14


@local
def test_compiler_refuses_join_before_split(tmp_path):
    p = _two_path_k0(tmp_path, 1, 6)
    d = json.loads(p.read_text())
    d["data"]["tone"]["dsp0"]["join"]["@position"] = 1
    p.write_text(json.dumps(d))
    with pytest.raises(stomp.UnsupportedModelChange, match="attach points"):
        stomp.compile_document(p, BASE, tmp_path / "o.bin", compiler=NEW)


@local
@pytest.mark.skipif(not OLD.exists(), reason="needs the pre-T063 build for comparison")
def test_single_path_output_is_byte_identical_to_the_previous_build(tmp_path):
    src = tmp_path / "k0.hlx"
    shutil.copy(K0, src)
    stomp.compile_document(src, BASE, tmp_path / "new.bin", compiler=NEW)
    stomp.compile_document(src, BASE, tmp_path / "old.bin", compiler=OLD)
    assert (tmp_path / "new.bin").read_bytes() == (tmp_path / "old.bin").read_bytes()


@local
def test_compiler_writes_the_device_topology_code_and_junction_flags(tmp_path):
    """fw 3.80: an op-21 write with topology 0 under a Path B block is re-derived by the device
    (11C came back with attach 6/9 instead of 5/7), so the code (tone[0][21]) is written: SABJ = 1."""
    rep = stomp.compile_document(_two_path_k0(tmp_path, 1, 6), BASE, tmp_path / "o.bin", compiler=NEW)
    ch = stomp.decode_document(tmp_path / "o.bin", compiler=NEW)
    assert ch["path_topology"] == 1
    assert any("path topology" in c for c in rep["changed"])
    assert all(j["snapshot_switch"] for j in rep["junctions"])   # HX Edit: @no_snapshot_bypass false


@local
def test_compiler_sab_keeps_path_b_output_values(tmp_path):
    """SAB (Path B to its own output, the dual-render layout): code 2, Path B output values written
    ([pan, gain, Type]); with them empty the device turned the write back into SABJ."""
    p = _two_path_k0(tmp_path, 0, 8, sabj=False)
    d = json.loads(p.read_text())
    t = d["data"]["tone"]
    t["global"]["@topology0"] = "SAB"
    t["dsp0"]["outputB"]["@output"] = 6
    t["dsp0"]["split"].update({"BalanceA": 0.5, "BalanceB": 0.5})
    t["dsp0"]["join"].update({"A Pan": 0.5, "B Pan": 0.5})
    p.write_text(json.dumps(d))
    rep = stomp.compile_document(p, BASE, tmp_path / "o.bin", compiler=NEW)
    assert rep["topology"] == "SAB"
    ch = stomp.decode_document(tmp_path / "o.bin", compiler=NEW)
    assert ch["path_topology"] == 2
    join = next(s for s in ch["slots"] if s["kind"] == "Join")
    assert join["b_routing"] == 6
    assert any(c.startswith("outputB values") for c in rep["changed"])
    assert RB.readback_diff(ch, d, rep).ok


@local
def test_compiler_refuses_an_own_path_b_input(tmp_path):
    p = _two_path_k0(tmp_path, 0, 8)
    d = json.loads(p.read_text())
    d["data"]["tone"]["dsp0"]["inputB"]["@input"] = 6
    p.write_text(json.dumps(d))
    with pytest.raises(stomp.UnsupportedModelChange, match="topology AB"):
        stomp.compile_document(p, BASE, tmp_path / "o.bin", compiler=NEW)


def test_readback_catches_a_re_derived_topology():
    d = single_template()
    ensure_sabj(d)
    ch = junction_chain()
    ch["path_topology"] = 1
    assert RB.readback_diff(ch, d).ok
    ch["path_topology"] = 2
    diff = RB.readback_diff(ch, d)
    assert not diff.ok and "path topology code 2 != 1" in diff.mismatches[0]
