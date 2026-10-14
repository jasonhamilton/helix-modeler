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
             "params": [_p(0, "Mix", 0.2), _p(1, "Sync", 6.0, 1.0, 19.0, "Enum")]},
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


def test_bridge_timeout_kills_the_helper_and_names_the_9v_fix(fake_helper):
    from tonematch.engines.stomp_bridge import BridgeError, StompBridge

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
