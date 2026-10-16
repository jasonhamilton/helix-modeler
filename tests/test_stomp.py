"""Offline tests for the HX Stomp engine and reamp maths. No pedal, no helper, no Line 6 data."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from tonematch import reamp
from tonematch.engines import stomp
from tonematch.engines.base import Cost, UnsupportedModelChange
from tonematch.preset import Preset, Slot

# --------------------------------------------------------------------------- fixtures


def _p(i, sym, value, lo=0.0, hi=1.0, kind="Continuous"):
    return {"i": i, "sym": sym, "id": sym, "name": sym, "value": value, "min": lo, "max": hi, "kind": kind}


def make_chain(model_id="AmpA", cab_id="CabA", drive=0.5, mic=6.0, delay_on=False, routing=1):
    return {
        "setlist": 0, "index": 11, "name": "Test", "assignments": ["a2", "a1"],
        "slots": [
            {"pos": 0, "kind": "Input", "enabled": True, "routing": routing},
            {"pos": 1, "kind": "Empty", "enabled": True},
            {"pos": 2, "kind": "Block", "enabled": True, "model": 20, "model_id": model_id,
             "paired": 45, "paired_id": cab_id,
             "params": [_p(0, "Drive", drive), _p(1, "Bass", 0.5)],
             "paired_params": [_p(0, "Distance", 4.0, 1.0, 12.0), {"i": 1, "extra": 0, "value": mic}]},
            {"pos": 3, "kind": "Block", "enabled": delay_on, "model": 7, "model_id": "DelayA",
             "params": [_p(0, "Mix", 0.2), _p(1, "Sync", 6.0, 1.0, 19.0, "Enum"),
                        {"i": 2, "extra": 0, "value": 1.0}]},   # @trails, past the symbol list
            {"pos": 9, "kind": "Output", "enabled": True, "routing": 1},
        ],
    }


def make_preset(model_id="AmpA", cab_id="CabA", drive=0.61, mic=6, delay_on=False):
    return Preset(name="t", slots=[
        Slot("amp", model_id, {"Drive": drive, "Bass": 0.5}, True, key="block3", position=1,
             extra={"@cab": "cab0", "@path": 0, "@type": 3}),
        Slot("cab", cab_id, {"Distance": 4}, True, key="cab0", extra={"@mic": mic}),
        Slot("delay", "DelayA", {"Mix": 0.2, "Sync": 6}, delay_on, key="block5", position=2,
             extra={"@path": 0, "@trails": True}),
    ])


class FakeBridge:
    """Stands in for StompBridge: keeps a chain, applies edits, logs calls."""

    def __init__(self, chain):
        self.flash = copy.deepcopy(chain)
        self.chain = copy.deepcopy(chain)
        self.calls: list[tuple[str, dict]] = []
        self.ready = {"presets": 126}
        self.closed = False

    def _slot(self, pos):
        return next(s for s in self.chain["slots"] if s["pos"] == pos)

    def call(self, cmd, timeout_s=30.0, **a):
        self.calls.append((cmd, a))
        if cmd == "info":
            return {"firmware": "3.50", "build": "b", "setlist": 0, "index": 11, "label": "04C", "name": "Test"}
        if cmd == "chain":
            return copy.deepcopy(self.chain)
        if cmd == "backup":
            return {"bytes": 1}
        if cmd == "model":
            s = self._slot(a["pos"])
            s["model_id"] = a["model"]
            for p in s["params"]:
                p["value"] = 0.0  # a model change resets parameters
            if a.get("cab"):
                s["paired_id"] = a["cab"]
                s["paired_params"][1]["value"] = 0.0
            return {}
        if cmd == "set":
            s = self._slot(a["pos"])
            ps = s["paired_params"] if a.get("paired") else s["params"]
            next(p for p in ps if p["i"] == a["param"])["value"] = a["value"]
            return {}
        if cmd == "enable":
            self._slot(a["pos"])["enabled"] = a["on"]
            return {}
        if cmd == "route_key":
            self._slot(a["pos"])["routing"] = a["to"]
            return {"status": 0}
        if cmd == "select":
            self.chain = copy.deepcopy(self.flash)
            return {}
        if cmd == "quit":
            return {}
        raise AssertionError(f"unexpected command {cmd}")

    def close(self):
        self.closed = True


def make_engine(monkeypatch, tmp_path, chain=None):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    cal = reamp.Calibration(latency_samples=10, measurements=[10, 10, 11], level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    b = FakeBridge(chain or make_chain())
    return stomp.StompEngine(bridge=b, calibration=cal, session_dir=tmp_path), b


# --------------------------------------------------------------------------- pure functions


def test_device_pos_maps_path_a_after_the_input():
    assert stomp.device_pos(0, 1) == 2
    assert stomp.device_pos(0, 0) == 1
    with pytest.raises(UnsupportedModelChange):
        stomp.device_pos(2, 0)


def test_plan_load_maps_amp_cab_and_bypasses_extra_blocks():
    chain = make_chain(delay_on=True)
    pre = make_preset()
    pre.slots = pre.slots[:2]  # amp + cab only; the pedal's delay must be bypassed
    plan = stomp.plan_load(pre, chain)
    t = plan["targets"][2]
    assert (t["model"], t["cab"], t["slot"], t["cab_slot"]) == ("AmpA", "CabA", 0, 1)
    assert t["cab_params"]["@mic"] == 6 and "@path" not in t["params"]
    assert plan["bypass"] == [3]


def test_plan_load_refuses_a_layout_change():
    pre = make_preset()
    pre.slots[2].position = 5  # no block at device slot 6
    with pytest.raises(UnsupportedModelChange):
        stomp.plan_load(pre, make_chain())


def test_to_native_scales_and_rounds_menus():
    assert stomp.to_native({"min": 1.0, "max": 12.0, "kind": "Continuous"}, 0.5) == pytest.approx(6.5)
    assert stomp.to_native({"min": 1.0, "max": 19.0, "kind": "Enum"}, 0.26) == 6.0
    assert stomp.to_native({"min": 0.0, "max": 1.0}, 1.7) == 1.0


def test_param_meta_names_the_cab_mic_extra():
    meta = stomp._param_meta(make_chain()["slots"][2]["paired_params"], cab=True)
    assert meta["@mic"]["i"] == 1 and meta["Distance"]["i"] == 0


def test_chain_signature_ignores_presentation_and_assignment_order():
    a = make_chain()
    b = copy.deepcopy(a)
    b["assignments"].reverse()
    for s in b["slots"]:
        for p in s.get("params", []):
            p.pop("sym", None)
            p["text"] = "shown"
    assert stomp.chain_signature(a) == stomp.chain_signature(b)
    b["slots"][2]["params"][0]["value"] = 0.9
    assert stomp.chain_signature(a) != stomp.chain_signature(b)


# --------------------------------------------------------------------------- reamp maths


def test_onset_trim_and_align():
    x = np.zeros(1000, np.float32)
    x[123] = 0.5
    assert reamp.onset(x) == 123
    rec = np.arange(100, dtype=np.float32)
    assert list(reamp.trim(rec, 10, 5, 3, 1)) == [15, 16, 17, 18]
    rng = np.random.default_rng(0)
    ref = rng.standard_normal(500).astype(np.float32)
    y_ext = np.concatenate([np.zeros(3, np.float32), ref, np.zeros(1, np.float32)])  # true offset +1
    w, off = reamp.align(y_ext, ref, len(ref), 2)
    assert off == 1 and np.allclose(w, ref)


def test_onset_raises_on_silence():
    with pytest.raises(reamp.ReampError):
        reamp.onset(np.zeros(10))


def test_calibration_round_trip_and_refusals(tmp_path):
    cal = reamp.Calibration(latency_samples=1206, measurements=[1206, 1207], level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    path = cal.save(tmp_path / "latency.json")
    back = reamp.Calibration.load(path)
    assert back.latency_samples == 1206 and back.input_usb56 == reamp.STOMP_INPUT_USB56
    back.check_firmware("3.50")
    with pytest.raises(reamp.ReampError):
        back.check_firmware("3.80")
    old = json.loads(path.read_text())
    old.pop("pa_latency")
    path.write_text(json.dumps(old))
    with pytest.raises(reamp.ReampError):  # written before pa_latency existed: default buffering
        reamp.Calibration.load(path).check_firmware("3.50")
    with pytest.raises(reamp.ReampError):
        reamp.Calibration.load(tmp_path / "missing.json")


def test_tone_level():
    assert reamp.rms_db(reamp.tone(1.0)[4800:-4800]) == pytest.approx(-18.0, abs=0.05)


# --------------------------------------------------------------------------- engine flow, fake bridge


def test_load_rebuilds_models_params_mic_and_routes_input(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path, make_chain(model_id="AmpB", cab_id="CabB"))
    eng.load(make_preset())
    cmds = [c for c, _ in b.calls]
    assert ("model", {"pos": 2, "model": "AmpA", "cab": "CabA"}) in b.calls
    amp = next(s for s in b.chain["slots"] if s["pos"] == 2)
    assert amp["params"][0]["value"] == pytest.approx(0.61)
    assert amp["paired_params"][1]["value"] == 6.0          # @mic restored after the model change
    mic_set = next(a for c, a in b.calls if c == "set" and a.get("paired") and a["param"] == 1)
    assert mic_set["wire"] == "int"
    assert b.chain["slots"][0]["routing"] == reamp.STOMP_INPUT_USB56
    assert "save" not in cmds and "restore" not in cmds     # edit buffer only
    assert eng.slot_map == {0: (2, False), 1: (2, True), 2: (3, False)}
    assert len(eng.model_load_s) == 1


def test_set_params_converts_normalised_values(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.load(make_preset())
    b.calls.clear()
    eng.set_params({(0, "Drive"): 0.25, (1, "Distance"): 0.5})
    sets = {(a["pos"], a["param"], a["paired"]): a["value"] for c, a in b.calls if c == "set"}
    assert sets[(2, 0, False)] == pytest.approx(0.25)
    assert sets[(2, 0, True)] == pytest.approx(6.5)


def test_close_restores_by_reselecting_and_verifies(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.load(make_preset(drive=0.9))
    eng.close()
    assert eng.restore_report["match"] is True
    assert eng.restore_report["steps"] == ["select original"]
    assert b.closed and stomp.chain_signature(b.chain) == stomp.chain_signature(b.flash)


def test_close_writes_the_backup_when_the_buffer_was_dirty(monkeypatch, tmp_path):
    chain = make_chain()
    eng, b = make_engine(monkeypatch, tmp_path, chain)
    b.flash["slots"][2]["params"][0]["value"] = 0.1  # flash differs from the buffer seen at open

    def restore_aware(cmd, timeout_s=30.0, _orig=b.call, **a):
        if cmd == "restore":
            b.calls.append((cmd, a))
            b.chain = copy.deepcopy(chain)
            return {}
        return _orig(cmd, timeout_s, **a)

    b.call = restore_aware
    eng.close()
    assert eng.restore_report["match"] is True
    assert eng.restore_report["steps"][-1] == "restore edit-buffer backup"


def test_cost_uses_measured_model_loads(monkeypatch, tmp_path):
    eng, _ = make_engine(monkeypatch, tmp_path, make_chain(model_id="AmpB"))
    eng.load(make_preset())
    monkeypatch.setattr(eng, "render", lambda x: x)
    c = eng.cost()
    assert isinstance(c, Cost) and c.parallelism == 1
    assert c.load_penalty_s == pytest.approx(eng.model_load_s[0])


def test_is_available_false_without_helper(monkeypatch):
    monkeypatch.setenv("TONEMATCH_STOMPD", "/nonexistent/stompd")
    assert stomp.StompEngine.is_available() is False


# --------------------------------------------------------------------------- bridge, fake helper process

FAKE_HELPER = """#!{python}
import json, sys, time
print(json.dumps({{"ok": True, "event": "ready", "presets": 126}}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)["cmd"]
    if cmd == "chain":
        time.sleep(5)  # never answers in time
    print(json.dumps({{"ok": True, "cmd": cmd}}), flush=True)
    if cmd == "quit":
        break
# Like the real helper since T059: HX Edit's session-close, reported on exit.
print(json.dumps({{"ok": True, "event": "closed", "release": True, "release_sent": True}}), flush=True)
"""


@pytest.fixture
def fake_helper(tmp_path):
    import sys

    exe = tmp_path / "stompd"
    exe.write_text(FAKE_HELPER.format(python=sys.executable))
    exe.chmod(0o755)
    return exe


def test_bridge_allowlist_blocks_flash_writes(fake_helper):
    from tonematch.engines.stomp_bridge import ALLOWED_COMMANDS, BridgeError, StompBridge

    assert not {"save", "rename", "set_setting", "ir_load", "write_slot"} & ALLOWED_COMMANDS
    with StompBridge(binary=fake_helper) as b:
        assert b.call("info")["cmd"] == "info"
        with pytest.raises(BridgeError, match="not allowed"):
            b.call("save")


def test_bridge_timeout_kills_the_helper_and_names_the_9v_fix(fake_helper, monkeypatch):
    from tonematch.engines.stomp_bridge import BridgeError, StompBridge

    monkeypatch.setattr(StompBridge, "KILL_GRACE_S", 0.2)

    b = StompBridge(binary=fake_helper)
    with pytest.raises(BridgeError, match="9V adapter"):
        b.call("chain", timeout_s=0.5)
    assert b._proc.poll() is not None          # killed: no stale reply can be read later
    with pytest.raises(BridgeError, match="dead"):
        b.call("info")
    b.close()


def test_open_engines_are_closed_at_exit(monkeypatch, tmp_path):
    eng, b = make_engine(monkeypatch, tmp_path)
    eng.load(make_preset(drive=0.9))
    stomp._close_live_engines()
    assert b.closed and eng.restore_report["match"] is True


# --------------------------------------------------------------------------- whole-preset load (T018s)


def _fake_params(node):
    """Every value a .hlx block names, as the chain shows it: knobs, then the "@" extra values."""
    names = [k for k in node if not k.startswith("@")]
    ps = [_p(i, k, float(node[k]), 0.0, max(1.0, float(node[k]))) for i, k in enumerate(names)]
    for k in ("@mic", "@trails"):
        if k in node:
            ps.append({"i": len(ps), "extra": 0, "value": float(node[k])})
    return ps


def fake_compiler(calls):
    """Stands in for `stompd compile`: turns the .hlx into the chain the pedal would then hold."""

    def comp(hlx, base, out):
        doc = json.loads(hlx.read_text())
        dsp = doc["data"]["tone"]["dsp0"]
        calls.append(doc)
        slots = [{"pos": 0, "kind": "Input", "enabled": True, "routing": dsp["inputA"]["@input"]}]
        report = []
        for key, node in dsp.items():
            if not key.startswith("block"):
                continue
            pos = 1 + node["@position"]
            s = {"pos": pos, "kind": "Block", "enabled": node["@enabled"], "model_id": node["@model"],
                 "params": _fake_params(node)}
            if node.get("@cab"):
                s.update(paired_id=dsp[node["@cab"]]["@model"], paired_params=_fake_params(dsp[node["@cab"]]))
            slots.append(s)
            report.append({"slot": pos, "key": key, "mode": "built"})
        slots.append({"pos": 9, "kind": "Output", "enabled": True, "routing": 1})
        out.write_text(json.dumps({"slots": slots}))
        return {"slots": report, "changed": ["x"]}

    return comp


def whole_engine(monkeypatch, tmp_path, mode, chain=None):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    cal = reamp.Calibration(latency_samples=10, measurements=[10, 10, 11], level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    b = FakeBridge(chain or make_chain())
    orig = b.call

    def call(cmd, timeout_s=30.0, **a):
        if cmd == "backup":
            b.calls.append((cmd, a))
            from pathlib import Path

            Path(a["path"]).write_text(json.dumps(b.chain))
            return {"bytes": 1}
        if cmd == "restore":
            b.calls.append((cmd, a))
            from pathlib import Path

            b.chain.update(json.loads(Path(a["path"]).read_text()))
            return {"restored": a["path"]}
        return orig(cmd, timeout_s, **a)

    b.call = call
    compiled: list = []
    eng = stomp.StompEngine(bridge=b, calibration=cal, session_dir=tmp_path, whole_preset=mode,
                            compiler=fake_compiler(compiled))
    return eng, b, compiled


def layout_change_preset():
    pre = make_preset(model_id="AmpB", cab_id="CabB", delay_on=True)
    pre.slots[2].position = 5  # the pedal has no block at device slot 6
    return pre


def test_whole_preset_off_still_raises_on_layout_change(monkeypatch, tmp_path):
    eng, b, compiled = whole_engine(monkeypatch, tmp_path, "off")
    with pytest.raises(UnsupportedModelChange):
        eng.load(layout_change_preset())
    assert not compiled and "restore" not in [c for c, _ in b.calls]


def test_whole_preset_fallback_writes_one_document_for_a_layout_change(monkeypatch, tmp_path):
    eng, b, compiled = whole_engine(monkeypatch, tmp_path, "fallback")
    eng.load(layout_change_preset())
    cmds = [c for c, _ in b.calls]
    assert cmds.count("restore") == 1 and "model" not in cmds and "save" not in cmds
    assert compiled[0]["data"]["tone"]["dsp0"]["inputA"]["@input"] == reamp.STOMP_INPUT_USB56
    assert eng.slot_map == {0: (2, False), 1: (2, True), 2: (6, False)}
    assert len(eng.model_load_s) == 1 and len(eng.whole_writes) == 1
    assert {"backup_s", "compile_s", "write_s", "load_s"} <= set(eng.whole_writes[0])
    b.calls.clear()
    eng.set_params({(0, "Drive"): 0.25})            # set_params works on the written chain
    assert [(c, a["pos"]) for c, a in b.calls if c == "set"] == [("set", 2)]


def test_whole_preset_fallback_keeps_in_place_edits_when_the_layout_fits(monkeypatch, tmp_path):
    eng, b, compiled = whole_engine(monkeypatch, tmp_path, "fallback")
    eng.load(make_preset(drive=0.9))
    assert not compiled and "restore" not in [c for c, _ in b.calls]


def test_whole_preset_always_and_verification(monkeypatch, tmp_path):
    eng, b, compiled = whole_engine(monkeypatch, tmp_path, "always")
    eng.load(make_preset(drive=0.9))
    assert len(compiled) == 1 and "set" not in [c for c, _ in b.calls]
    # A write that does not land as compiled is an error, not a silent mismatch.
    eng._compiler = lambda hlx, base, out: (out.write_text(json.dumps({"slots": []})),
                                            {"slots": [{"slot": 2, "key": "block3"}]})[1]
    with pytest.raises(stomp.EngineError, match="whole-preset write"):
        eng.load(make_preset())


def test_whole_preset_needs_a_document_write_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    b = FakeBridge(make_chain())
    b.allow_document_write = False
    with pytest.raises(stomp.EngineError, match="allow_document_write"):
        stomp.StompEngine(bridge=b, session_dir=tmp_path, whole_preset="fallback",
                          calibration=reamp.Calibration(latency_samples=10, measurements=[10], level_gain_db=0.0,
                                                        tone_in_dbfs=-18, tone_out_dbfs=-18, firmware="3.50",
                                                        pa_latency=reamp.PA_LATENCY))
    with pytest.raises(stomp.EngineError):
        stomp.StompEngine(bridge=FakeBridge(make_chain()), session_dir=tmp_path, whole_preset="sometimes")


def test_bridge_document_write_is_gated(fake_helper, tmp_path):
    from tonematch.engines.stomp_bridge import BridgeError, StompBridge

    doc = tmp_path / "generated.bin"
    with StompBridge(binary=fake_helper) as b:
        with pytest.raises(BridgeError, match="allow_document_write"):
            b.call("restore", path=str(doc))
        b.call("backup", path=str(doc))              # this session's own backup may go back
        assert b.call("restore", path=str(doc))["cmd"] == "restore"
    with StompBridge(binary=fake_helper, allow_document_write=True) as b:
        assert b.call("restore", path=str(tmp_path / "other.bin"))["cmd"] == "restore"


def test_compile_document_refusal_is_unsupported_model_change(tmp_path):
    import sys

    exe = tmp_path / "stompd"
    exe.write_text(f"#!{sys.executable}\n# usage: stompd compile <hlx> <base> <out>\n"
                   f"import json\nprint(json.dumps({{'ok': False, 'error': 'no room'}}))\n")
    exe.chmod(0o755)
    with pytest.raises(UnsupportedModelChange, match="no room"):
        stomp.compile_document(tmp_path / "a.hlx", tmp_path / "b.bin", tmp_path / "c.bin", compiler=exe)


def test_whole_write_resends_the_input_route_even_when_the_chain_reads_usb56(monkeypatch, tmp_path):
    # T018s hardware: after op 21 carrying Input=USB 5/6 the chain read 6 but Path A was silent.
    eng, b, _ = whole_engine(monkeypatch, tmp_path, "always")
    b.calls.clear()
    eng.load(make_preset())
    assert b.chain["slots"][0]["routing"] == reamp.STOMP_INPUT_USB56
    cmds = [c for c, _ in b.calls]
    assert cmds.index("route_key") > cmds.index("restore")
    eng.REASSERT_INPUT_AFTER_WHOLE = False
    b.calls.clear()
    eng.load(make_preset())
    assert "route_key" not in [c for c, _ in b.calls]


def test_inject_diagnose_sequence_is_edit_buffer_only(monkeypatch, tmp_path):
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("stomp_inject", Path(__file__).parents[1] / "scripts" / "stomp_inject.py")
    si = importlib.util.module_from_spec(spec)
    sys.modules["stomp_inject"] = si
    spec.loader.exec_module(si)
    eng, b, _ = whole_engine(monkeypatch, tmp_path, "always")
    silent = iter([False, True, True, True, True, True])   # control sounds, K stays silent
    monkeypatch.setattr(si, "probe", lambda e, x, d, tag: {"tag": tag, "path_a_silent": next(silent)})
    monkeypatch.setattr(si.P, "read_hlx", lambda *a, **k: make_preset())
    monkeypatch.setattr(si.C, "load_catalog", lambda *a, **k: {})
    monkeypatch.setattr(si.time, "sleep", lambda s: None)
    b.calls.clear()
    steps = si.diagnose(eng, np.zeros(10, np.float32), tmp_path)
    assert [s["tag"] for s in steps] == ["c0_original_route_key", "k1_after_write", "k2_settle_0p5s",
                                          "k3_settle_2p5s", "k4_route_key_resent", "k5_route_toggle_1_6"]
    cmds = [c for c, _ in b.calls]
    assert not {"save", "select", "rename"} & set(cmds) and cmds.count("restore") == 1
    # the K write itself must not have re-sent the route (that is what k4 tests separately)
    i = cmds.index("restore")
    assert cmds[i + 1:].index("route_key") > cmds[i + 1:].index("backup")
    assert eng.REASSERT_INPUT_AFTER_WHOLE is True


def test_inject_probe_result_is_json_serialisable_on_digital_silence(monkeypatch, tmp_path):
    # 20261014T234106Z: a digital-zero capture made path_a_silent a numpy bool and json.dumps failed.
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("stomp_inject", Path(__file__).parents[1] / "scripts" / "stomp_inject.py")
    si = importlib.util.module_from_spec(spec)
    sys.modules["stomp_inject"] = si
    spec.loader.exec_module(si)
    eng, _, _ = whole_engine(monkeypatch, tmp_path, "always")
    monkeypatch.setattr(reamp, "playrec", lambda x, dev, **k: (np.zeros((len(x) + 2000, 8), np.float32), 100))
    out = si.probe(eng, np.ones(480, np.float32), tmp_path, "zero")
    assert out["path_a_silent"] is True
    json.dumps(out)


SILENT_INFO_HELPER = """#!{python}
import json, sys, time
print(json.dumps({{"ok": True, "event": "ready", "presets": 126}}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)["cmd"]
    if cmd == "info":
        time.sleep(30)  # a wedged pedal: the session opens, nothing answers
    print(json.dumps({{"ok": True, "cmd": cmd}}), flush=True)
"""


def test_health_check_tells_the_user_to_power_cycle(tmp_path, monkeypatch):
    import sys

    from tonematch.engines import stomp_bridge as sb

    exe = tmp_path / "stompd"
    exe.write_text(SILENT_INFO_HELPER.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setattr(sb.StompBridge, "HEALTH_TIMEOUT_S", 0.5)
    with pytest.raises(sb.PowerCycleNeeded) as e:
        sb.StompBridge(binary=exe)
    msg = str(e.value)
    assert "off and on" in msg and "9V" in msg and "Nothing was written" in msg
    assert "stderr" not in msg   # plain message, no debugging noise


def test_health_check_passes_on_a_healthy_helper(fake_helper):
    from tonematch.engines.stomp_bridge import StompBridge

    with StompBridge(binary=fake_helper) as b:
        assert b.health_check()["cmd"] == "info"


def test_pre_roll_flushes_delay_and_reverb_tails(monkeypatch, tmp_path):
    eng, _b = make_engine(monkeypatch, tmp_path)
    monkeypatch.setattr(stomp, "_category_of", lambda m: {"DelayA": "delay", "RevA": "reverb"}.get(m))
    assert eng.pre_roll_s() == eng.PRE_S                       # make_chain's delay is off
    d = next(s for s in eng.chain["slots"] if s["pos"] == 3)
    d["enabled"] = True
    d["params"] = [stomp_param("Time", 0.375), stomp_param("Feedback", 0.4)]
    expect = 0.375 * np.log(1e-3) / np.log(0.4) + 0.5
    assert eng.pre_roll_s() == pytest.approx(expect)          # ~3.3 s: time to -60 dB plus 0.5 s
    d["params"].append(stomp_param("TempoSync1", 1.0))
    assert eng.pre_roll_s() == eng.MAX_FLUSH_S                 # synced: 2 s worst case, capped
    d["model_id"] = "RevA"
    assert eng.pre_roll_s() == eng.REVERB_FLUSH_S


def stomp_param(sym, v):
    return {"i": 0, "sym": sym, "id": sym, "name": sym, "value": v, "min": 0.0, "max": 2.0, "kind": "Continuous"}


def test_fallback_sends_model_changes_as_one_whole_write(monkeypatch, tmp_path):
    eng, b, compiled = whole_engine(monkeypatch, tmp_path, "fallback", make_chain(model_id="AmpB", cab_id="CabB"))
    b.calls.clear()
    eng.load(make_preset())                      # same layout, different amp model
    cmds = [c for c, _ in b.calls]
    assert "model" not in cmds and cmds.count("restore") == 1 and len(compiled) == 1
