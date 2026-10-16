"""StompEngine.render_many (S1, two-path passes) and layout-changing loads (S2). Offline: a fake
bridge, a fake compiler that builds the chain a document would give (Path B and junctions too), a
fake catalog and a fake reamp whose per-path gain follows the chain. No pedal, no Line 6 data."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from tonematch import catalog as C
from tonematch import reamp
from tonematch.engines import base, stomp
from tonematch.engines.base import UnsupportedModelChange
from tonematch.engines.stomp_readback import ReadbackMismatch
from tonematch.preset import from_dict

LAT = 10
CAT = {"models": {m: {"id": m, "category": "distortion", "load": 10.0, "load_stereo": 12.0, "stomp": True,
                      "params": {"Drive": {"name": "Drive", "kind": "continuous", "min": 0.0, "max": 1.0}}}
                  for m in ("DistA", "DistB", "DistC", "DistD", "DistE")}}
CAT["models"]["DistHeavy"] = {**CAT["models"]["DistA"], "id": "DistHeavy", "load": 60.0}


def _p(i, sym, value, lo=0.0, hi=1.0):
    return {"i": i, "sym": sym, "id": sym, "name": sym, "value": float(value), "min": lo, "max": hi,
            "kind": "Continuous"}


def hlx(blocks, name="v"):
    """A single-path .hlx in K.hlx's frame: blocks = [(model, drive), ...] on Path A."""
    dsp = {"inputA": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 6, "threshold": -48},
           "inputB": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 0, "threshold": -48},
           "outputA": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1, "pan": 0.5, "gain": 0},
           "outputB": {"@model": "HelixStomp_AppDSPFlowOutputSend", "@output": 0, "pan": 0.5, "gain": 0},
           "split": {"@model": "HD2_AppDSPFlowSplitY", "@position": 0, "@enabled": True, "BalanceA": 0, "BalanceB": 1,
                     "bypass": False},
           "join": {"@model": "HD2_AppDSPFlowJoin", "@position": 8, "@enabled": True, "A Level": 0, "A Pan": 0,
                    "B Level": 0, "B Pan": 1, "B Polarity": False, "Level": 0}}
    for i, (m, d) in enumerate(blocks):
        dsp[f"block{i}"] = {"@model": m, "@position": i, "@path": 0, "@enabled": True, "Drive": d}
    tone = {"dsp0": dsp, "global": {"@topology0": "A", "@current_snapshot": 0},
            "snapshot0": {"@name": "S1", "@valid": True, "blocks": {"dsp0": {f"block{i}": True for i in range(len(blocks))}}}}
    return from_dict({"data": {"meta": {"name": name}, "device": 2162694, "tone": tone}, "schema": "L6Preset"})


def _node_params(node):
    return [_p(i, k, float(v), 0.0, max(1.0, abs(float(v))))
            for i, (k, v) in enumerate((k, v) for k, v in node.items() if not k.startswith("@"))]


def compile_chain(doc: dict, base_chain: dict) -> tuple[dict, dict]:
    """What the pedal holds after the document: blocks at 1 + @position + 10 * @path, junctions at 10/19."""
    dsp = doc["data"]["tone"]["dsp0"]
    slots = [{"pos": 0, "kind": "Input", "enabled": True, "routing": dsp["inputA"]["@input"]}]
    report = []
    for key, node in dsp.items():
        if not key.startswith("block"):
            continue
        pos = 1 + int(node["@position"]) + 10 * int(node.get("@path", 0))
        snap = doc["data"]["tone"].get("snapshot0", {}).get("blocks", {}).get("dsp0", {})
        slots.append({"pos": pos, "kind": "Block", "enabled": bool(snap.get(key, node["@enabled"])),
                      "model_id": node["@model"], "params": _node_params(node)})
        report.append({"slot": pos, "key": key, "mode": "built"})
    slots.append({"pos": 9, "kind": "Output", "enabled": True, "routing": 1})
    slots.append({"pos": 10, "kind": "Split", "enabled": True, "model_id": dsp["split"]["@model"],
                  "params": _node_params(dsp["split"])})
    slots.append({"pos": 19, "kind": "Join", "enabled": True, "model_id": dsp["join"]["@model"],
                  "params": _node_params(dsp["join"])})
    ch = {k: v for k, v in base_chain.items() if k != "slots"}
    ch["slots"] = sorted(slots, key=lambda s: s["pos"])
    ch["out_b"] = int(dsp.get("outputB", {}).get("@output", 0))
    return ch, {"slots": report, "topology": doc["data"]["tone"]["global"].get("@topology0")}


class Pedal:
    """Fake bridge: the chain, documents as JSON chains, and a log of calls."""

    def __init__(self, first):
        doc = first.to_hlx_dict()
        self.chain, _ = compile_chain(doc, {"setlist": 0, "index": 11, "name": "Smoke Known", "assignments": []})
        self.flash = copy.deepcopy(self.chain)
        self.calls: list[tuple[str, dict]] = []
        self.ready = {"presets": 126}
        self.allow_document_write = True
        self.closed = False

    def call(self, cmd, timeout_s=30.0, **a):
        self.calls.append((cmd, a))
        if cmd == "info":
            return {"firmware": "3.80", "build": "b", "setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"}
        if cmd == "chain":
            return copy.deepcopy(self.chain)
        if cmd == "backup":
            Path(a["path"]).write_text(json.dumps(self.chain))
            return {"bytes": 1}
        if cmd == "restore":
            self.chain = json.loads(Path(a["path"]).read_text())
            return {"restored": a["path"]}
        if cmd == "set":
            s = next(s for s in self.chain["slots"] if s["pos"] == a["pos"])
            next(p for p in s["params"] if p["i"] == a["param"])["value"] = a["value"]
            return {"native": a["value"]}
        if cmd == "enable":
            next(s for s in self.chain["slots"] if s["pos"] == a["pos"])["enabled"] = a["on"]
            return {}
        if cmd == "route_key":
            next(s for s in self.chain["slots"] if s["pos"] == a["pos"])["routing"] = a["to"]
            return {"status": 0}
        if cmd == "select":
            self.chain = copy.deepcopy(self.flash)
            return {}
        if cmd == "model":
            s = next(s for s in self.chain["slots"] if s["pos"] == a["pos"])
            s["model_id"] = a["model"]
            return {}
        raise AssertionError(cmd)

    def close(self):
        self.closed = True

    def gain(self, path):
        lo, hi = (1, 9) if path == "A" else (11, 19)
        g = 1.0
        for s in self.chain["slots"]:
            if s["kind"] == "Block" and lo <= s["pos"] < hi and s["enabled"]:
                g *= 1.0 + next(p["value"] for p in s["params"] if p["sym"] == "Drive")
        return g * 0.1


def fake_compiler(pedal, calls, refuse_dual=False):
    def comp(hlx_path, base_path, out):
        doc = json.loads(Path(hlx_path).read_text())
        calls.append(doc)
        if refuse_dual and doc["data"]["tone"]["global"].get("@topology0") == "SAB":
            raise UnsupportedModelChange("stompd compile refused the preset: test")
        ch, rep = compile_chain(doc, pedal.chain)
        Path(out).write_text(json.dumps(ch))
        return rep
    return comp


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.delenv(stomp.ENV_WHOLE_PRESET, raising=False)
    monkeypatch.delenv(stomp.ENV_DSP_BUDGET, raising=False)
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: CAT)
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    passes = []

    def make(first=None, refuse_dual=False, mode=None):
        pedal = Pedal(first or hlx([("DistA", 0.2), ("DistB", 0.3)]))

        def playrec(mono, device=None, pre_s=0.25, tail_s=0.1, out_pair=(5, 6), in_pair=(1, 2)):
            pre = int(pre_s * 48000)
            n = len(mono)
            rec = np.zeros((pre + n + int(tail_s * 48000), 8), np.float32)
            rec[pre + LAT:pre + LAT + n, 0:2] = (pedal.gain("A") * mono)[:, None]
            if pedal.chain.get("out_b") == stomp.OUT_B_USB34:
                rec[pre + LAT:pre + LAT + n, 2:4] = (pedal.gain("B") * mono)[:, None]
            passes.append(in_pair)
            return (rec if in_pair is None else rec[:, [in_pair[0] - 1, in_pair[1] - 1]]), pre

        monkeypatch.setattr(reamp, "playrec", playrec)
        cal = reamp.Calibration(latency_samples=LAT, measurements=[LAT], level_gain_db=0.0, tone_in_dbfs=-18,
                                tone_out_dbfs=-18, firmware="3.80", pa_latency=reamp.PA_LATENCY)
        compiled: list = []
        eng = stomp.StompEngine(bridge=pedal, calibration=cal, session_dir=tmp_path, whole_preset=mode,
                                compiler=fake_compiler(pedal, compiled, refuse_dual))
        eng.PRE_S = 0.001
        eng.pre_roll_s = lambda: 0.001
        return eng, pedal, compiled, passes

    return make


DRY = (0.3 * np.sin(np.linspace(0, 200, 4800))).astype(np.float32)


def expected(drives):
    return 0.1 * float(np.prod([1 + d for d in drives]))


# --------------------------------------------------------------------------- S1: render_many


def test_two_fitting_variants_render_in_one_pass_in_order(rig):
    eng, _pedal, compiled, passes = rig()
    va, vb = hlx([("DistA", 0.1), ("DistB", 0.2)]), hlx([("DistC", 0.5)])
    ys = base.render_many(eng, DRY, [va, vb])
    assert eng.two_path_passes == 1 and passes == [None]          # one pass, all USB inputs
    np.testing.assert_allclose(ys[0], expected([0.1, 0.2]) * DRY, rtol=1e-5)
    np.testing.assert_allclose(ys[1], expected([0.5]) * DRY, rtol=1e-5)
    d = compiled[-1]["data"]["tone"]
    assert d["global"]["@topology0"] == "SAB" and d["dsp0"]["outputB"]["@output"] == stomp.OUT_B_USB34
    assert d["dsp0"]["split"]["BalanceA"] == 0.5 and d["dsp0"]["join"]["B Pan"] == 0.5
    assert d["dsp0"]["inputB"]["@input"] == 0
    assert [v["@path"] for k, v in d["dsp0"].items() if k.startswith("block")].count(1) == 1
    assert eng.last_readback["ok"]


def test_two_path_equals_serial(rig):
    eng, _pedal, _, _ = rig()
    vs = [hlx([("DistA", 0.4)]), hlx([("DistB", 0.7), ("DistC", 0.1)])]
    dual = eng.render_many(DRY, vs)
    serial = base.serial_render_many(eng, DRY, vs)
    for a, b in zip(dual, serial, strict=True):
        np.testing.assert_allclose(a, b, rtol=1e-5)


def test_odd_count_pairs_then_serial_and_keeps_order(rig):
    eng, _, _, passes = rig()
    drives = [0.1, 0.2, 0.3, 0.4, 0.5]
    vs = [hlx([("DistA", d)]) for d in drives]
    ys = eng.render_many(DRY, vs)
    assert eng.two_path_passes == 2 and eng.serial_renders == 1 and len(passes) == 3
    for y, d in zip(ys, drives, strict=True):
        np.testing.assert_allclose(y, expected([d]) * DRY, rtol=1e-5)


def test_dict_variants_accumulate_like_the_serial_fallback(rig):
    first = hlx([("DistA", 0.2), ("DistB", 0.3)])
    eng, _, _, _ = rig(first)
    eng.load(first)
    ys = eng.render_many(DRY, [{(0, "Drive"): 0.5}, {(1, "Drive"): 0.9}, {(0, "Drive"): 0.0}])
    want = [[0.5, 0.3], [0.5, 0.9], [0.0, 0.9]]
    for y, w in zip(ys, want, strict=True):
        np.testing.assert_allclose(y, expected(w) * DRY, rtol=1e-5)
    assert eng.two_path_passes == 1


def test_dict_before_any_load_is_an_error(rig):
    eng, _, _, _ = rig()
    with pytest.raises(stomp.EngineError, match="load"):
        eng.render_many(DRY, [{(0, "Drive"): 0.5}])


def test_over_budget_or_too_many_blocks_renders_serially(rig):
    eng, _, _, _ = rig()
    heavy = [hlx([("DistHeavy", 0.1)]), hlx([("DistA", 0.2), ("DistB", 0.2)])]
    eng.render_many(DRY, heavy)
    assert eng.two_path_passes == 0 and eng.serial_renders == 2 and "estimated load" in eng.two_path_skips[-1]
    many = [hlx([("DistA", 0.1)] * 5), hlx([("DistB", 0.1)] * 4)]
    eng.render_many(DRY, many)
    assert eng.two_path_passes == 0 and "blocks on two paths" in eng.two_path_skips[-1]


def test_budget_override_by_environment(rig, monkeypatch):
    monkeypatch.setenv(stomp.ENV_DSP_BUDGET, "15")
    eng, _, _, _ = rig()
    eng.render_many(DRY, [hlx([("DistA", 0.1)]), hlx([("DistB", 0.1)])])
    assert eng.two_path_passes == 0 and "> 15.0" in eng.two_path_skips[-1]


def test_two_path_variant_is_never_paired(rig):
    rig()
    twop = hlx([("DistA", 0.1)])
    twop.slots[0].extra["@path"] = 1
    costs = {"models": {m: {"mono": 10.0} for m in CAT["models"]}}
    assert "not single-path" in stomp.two_path_fit(twop, hlx([("DistB", 0.1)]), costs=costs)
    assert stomp.two_path_fit(hlx([("DistA", 0.1)]), hlx([("DistB", 0.1)]), costs=costs) is None


def test_pair_refused_by_the_compiler_falls_back_to_serial(rig):
    eng, _, _, _ = rig(refuse_dual=True)
    ys = eng.render_many(DRY, [hlx([("DistA", 0.1)]), hlx([("DistB", 0.6)])])
    assert eng.two_path_passes == 0 and eng.serial_renders == 2 and "refused as one pass" in eng.two_path_skips[-1]
    np.testing.assert_allclose(ys[1], expected([0.6]) * DRY, rtol=1e-5)


def test_a_variant_that_cannot_load_alone_raises(rig):
    eng, _, _, _ = rig(refuse_dual=True)
    eng._compiler = lambda *a: (_ for _ in ()).throw(UnsupportedModelChange("refused"))
    with pytest.raises(UnsupportedModelChange):
        eng.render_many(DRY, [hlx([("DistA", 0.1), ("DistB", 0.1), ("DistC", 0.1)])])


def test_serial_without_document_writes(rig):
    eng, _pedal, _, passes = rig(mode="off")
    eng.render_many(DRY, [hlx([("DistA", 0.1), ("DistB", 0.2)]), hlx([("DistA", 0.3), ("DistB", 0.4)])])
    assert eng.two_path_passes == 0 and "serial" in eng.two_path_skips[-1] and passes == [(1, 2), (1, 2)]


def test_helper_uses_the_engines_own_render_many(rig):
    eng, _, _, _ = rig()
    ys = base.render_many(eng, DRY, [hlx([("DistA", 0.1)]), hlx([("DistB", 0.1)])])
    assert len(ys) == 2 and eng.two_path_passes == 1


def test_merge_renames_b_keys_and_keeps_a(rig):
    a, b = hlx([("DistA", 0.1), ("DistB", 0.2)]), hlx([("DistC", 0.3), ("DistD", 0.4)])
    m = stomp.merge_two_path(a, b).to_hlx_dict()
    dsp = m["data"]["tone"]["dsp0"]
    blocks = {k: (v["@model"], v["@path"], v["@position"]) for k, v in dsp.items() if k.startswith("block")}
    assert blocks == {"block0": ("DistA", 0, 0), "block1": ("DistB", 0, 1), "block2": ("DistC", 1, 0),
                      "block3": ("DistD", 1, 1)}
    assert m["data"]["tone"]["snapshot0"]["blocks"]["dsp0"]["block3"] is True


# --------------------------------------------------------------------------- S2: layout-changing loads


def test_default_mode_is_fallback(rig):
    eng, _, _, _ = rig()
    assert eng.whole_preset == "fallback"


def test_model_change_is_one_whole_write_by_default(rig):
    eng, pedal, compiled, _ = rig()
    eng.load(hlx([("DistC", 0.2), ("DistB", 0.3)]))
    assert len(compiled) == 1 and "model" not in [c for c, _ in pedal.calls] and eng.last_readback["ok"]


def test_layout_change_is_one_whole_write_with_readback(rig):
    eng, pedal, compiled, _ = rig()
    new = hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)])     # a block where the pedal has none
    eng.load(new)
    cmds = [c for c, _ in pedal.calls]
    assert cmds.count("restore") == 1 and len(compiled) == 1 and eng.last_readback["ok"]
    assert eng.slot_map == {0: (1, False), 1: (2, False), 2: (3, False)}


def test_layout_that_fits_stays_in_place(rig):
    eng, pedal, compiled, _ = rig()
    eng.load(hlx([("DistA", 0.6), ("DistB", 0.1)]))
    assert not compiled and "set" in [c for c, _ in pedal.calls]


def test_single_path_after_a_two_path_pass_is_written_whole(rig):
    eng, pedal, compiled, _ = rig()
    eng.render_many(DRY, [hlx([("DistA", 0.1)]), hlx([("DistB", 0.1)])])
    n = len(compiled)
    eng.load(hlx([("DistA", 0.2)]))          # the pedal still has Path B: a layout change
    assert len(compiled) == n + 1 and not any(s["pos"] > 10 and s["kind"] == "Block" for s in pedal.chain["slots"])


def test_layout_the_device_refuses_raises_unsupported(rig):
    eng, pedal, _, _ = rig()

    def rederive(hlx_path, base_path, out):
        doc = json.loads(Path(hlx_path).read_text())
        ch, rep = compile_chain(doc, pedal.chain)
        ch["slots"] = [s for s in ch["slots"] if s["pos"] != 3]     # the device dropped a block
        Path(out).write_text(json.dumps(ch))
        return rep                                                   # the compiler built all three
    eng._compiler = rederive
    with pytest.raises(UnsupportedModelChange) as e:
        eng.load(hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)]))
    assert isinstance(e.value, stomp.LayoutRefused) and isinstance(e.value, ReadbackMismatch)
    assert eng.preset is None


def test_value_mismatch_is_not_a_layout_refusal(rig):
    eng, pedal, _, _ = rig()

    def wrong_value(hlx_path, base_path, out):
        doc = json.loads(Path(hlx_path).read_text())
        ch, rep = compile_chain(doc, pedal.chain)
        next(s for s in ch["slots"] if s["pos"] == 3)["params"][0]["value"] = 0.99
        Path(out).write_text(json.dumps(ch))
        return rep
    eng._compiler = wrong_value
    with pytest.raises(ReadbackMismatch) as e:
        eng.load(hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)]))
    assert not isinstance(e.value, UnsupportedModelChange)


def test_compiler_refusal_still_raises_unsupported(rig):
    eng, _, _, _ = rig()
    eng._compiler = lambda *a: (_ for _ in ()).throw(UnsupportedModelChange("stompd compile refused the preset"))
    with pytest.raises(UnsupportedModelChange, match="refused"):
        eng.load(hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)]))


def test_no_compiler_configured_keeps_the_old_refusal(rig, monkeypatch):
    eng, _, _, _ = rig()
    eng._custom_compiler = False
    monkeypatch.delenv(stomp.ENV_COMPILER, raising=False)
    monkeypatch.setattr(stomp, "helper_path", lambda: None)
    with pytest.raises(UnsupportedModelChange, match=stomp.ENV_COMPILER):
        eng.load(hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)]))


def test_off_mode_still_raises_on_layout_change(rig):
    eng, _, compiled, _ = rig(mode="off")
    with pytest.raises(UnsupportedModelChange):
        eng.load(hlx([("DistC", 0.1), ("DistD", 0.2), ("DistE", 0.3)]))
    assert not compiled
