"""T051: full-parameter readback for HX Stomp whole-preset loads. Offline: fake bridge, fake
compiler and decoder; no pedal, no stompd, no Line 6 data.

The bug: the pedal recalls the current snapshot's controller values over the block values on
load; the compiler kept the base document's snapshot values, so an in-place delay whose Mix and
Feedback are on a controller ran the base's 0.23 / 0.52 instead of the .hlx's 0.25 / 0.40."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_stomp import FakeBridge, _p

from tonematch import reamp
from tonematch.engines import stomp
from tonematch.engines import stomp_readback as RB
from tonematch.preset import Preset, Slot, Snapshot

# --------------------------------------------------------------------------- fixtures


def hlx(mix=0.25, fb=0.40, snap_mix=None, snap_fb=None, delay_on=True, current=0, extra_node=None):
    """A Placater-like Amp+Cab plus a DL4-like delay whose Mix and Feedback are on a controller."""
    snaps = {}
    for n in range(3):
        snaps[f"snapshot{n}"] = {
            "@name": f"SNAPSHOT {n + 1}", "@valid": True, "blocks": {"dsp0": {"block3": True, "block5": delay_on}},
            "controllers": {"dsp0": {"block5": {
                "Mix": {"@fs_enabled": False, "@value": mix if snap_mix is None else snap_mix},
                "Feedback": {"@fs_enabled": False, "@value": fb if snap_fb is None else snap_fb}}}}}
    dsp = {
        "inputA": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 6},
        "block3": {"@model": "AmpP", "@position": 1, "@path": 0, "@enabled": True, "@cab": "cab0",
                   "Drive": 0.836, "Fat": True, "SSwitch": 2},
        "cab0": {"@model": "CabU", "@enabled": True, "@mic": 1, "HighCut": 8000.0},
        "block5": {"@model": "DelayD", "@position": 2, "@path": 0, "@enabled": delay_on, "@trails": True,
                   "Time": 0.375, "Feedback": fb, "Mix": mix, "TempoSync1": False},
    }
    if extra_node:
        dsp["block3"].update(extra_node)
    return {"data": {"tone": {"global": {"@current_snapshot": current, "@tempo": 81}, "dsp0": dsp,
                              "controller": {"dsp0": {"block5": {"Mix": {"@controller": 8, "@cc": 110},
                                                                 "Feedback": {"@controller": 8, "@cc": 110}}}},
                              **snaps}}}


def chain(mix=0.25, fb=0.40, time_=0.375, highcut=8000.0, mic=1.0, sswitch=2.0, delay_on=True, routing=6,
          snap=None, extra_block=None):
    """What `chain` (or `stompd decode`) shows for that preset. `snap` = per-snapshot (mix, fb) for a
    decoded document's snapshot controller entries."""
    c = {"slots": [
        {"pos": 0, "kind": "Input", "enabled": True, "routing": routing},
        {"pos": 2, "kind": "Block", "enabled": True, "model_id": "AmpP", "paired_id": "CabU",
         "params": [_p(0, "Drive", 0.836), _p(1, "Fat", 1.0, kind="Switch"), _p(2, "SSwitch", sswitch, 0, 3, "Enum")],
         "paired_params": [_p(0, "HighCut", highcut, 1000.0, 20100.0), {"i": 1, "extra": 0, "value": mic}]},
        {"pos": 3, "kind": "Block", "enabled": delay_on, "model_id": "DelayD",
         "params": [_p(0, "Time", time_, 0.0, 8.0), _p(1, "Feedback", fb), _p(2, "TempoSync1", 0.0, kind="Switch"),
                    _p(3, "Mix", mix), {"i": 4, "extra": 0, "value": 1.0}]},
        {"pos": 9, "kind": "Output", "enabled": True, "routing": 1},
    ]}
    if extra_block:
        c["slots"].insert(3, extra_block)
    if snap is not None:
        c["snapshot_controllers"] = [
            e for n, (m, f) in enumerate(snap)
            for e in ({"snapshot": n, "place": 0, "slot": 3, "paired": False, "param": 1, "value": f},
                      {"snapshot": n, "place": 1, "slot": 3, "paired": False, "param": 3, "value": m})]
    return c


REPORT = {"slots": [{"slot": 2, "key": "block3"}, {"slot": 3, "key": "block5"}]}

# --------------------------------------------------------------------------- the diff


def test_matching_chain_passes_every_parameter():
    d = RB.readback_diff(chain(), hlx(), REPORT)
    assert d.ok, d.as_dict()
    # models x3, enabled x2, 3 amp + 2 cab (incl. @mic) + 5 delay (incl. @trails) params, input
    assert d.checked == 3 + 2 + 3 + 2 + 5 + 1


def test_the_t051_delay_is_caught():
    # rank2_H live: the pedal read back Feedback 0.52 (the base snapshot value) for a file saying 0.40
    d = RB.readback_diff(chain(fb=0.52, mix=0.72, time_=0.47), hlx(), REPORT)
    assert not d.ok
    bad = " ".join(d.mismatches)
    assert "block5.Feedback" in bad and "block5.Mix" in bad and "block5.Time" in bad
    with pytest.raises(RB.ReadbackMismatch, match="block5.Feedback"):
        RB.check(chain(fb=0.52), hlx(), REPORT)


@pytest.mark.parametrize("kw,ok", [
    ({"highcut": 8000.0 + 0.15}, True),       # 1e-5 of a 1-20.1 kHz range is 0.19 Hz
    ({"highcut": 8000.0 + 0.5}, False),
    ({"time_": 0.375 + 5e-5}, True),          # 8e-5 s on a 0..8 s time
    ({"time_": 0.375 + 2e-4}, False),
    ({"mix": 0.25 + 5e-6}, True),             # 1e-5 on a 0..1 knob
    ({"mix": 0.25 + 5e-5}, False),
    ({"sswitch": 3.0}, False),                # menus exact
    ({"mic": 2.0}, False),                    # cab @mic (extra value) exact
    ({"routing": 1}, False),                  # input still on the guitar, not USB 5/6
    ({"delay_on": False}, False),             # bypass state
])
def test_tolerances_by_kind_and_unit(kw, ok):
    assert RB.readback_diff(chain(**kw), hlx(), REPORT).ok is ok


def test_switch_compares_as_bool():
    c = chain()
    c["slots"][1]["params"][1]["value"] = 0.0      # Fat off on the device, on in the file
    assert not RB.readback_diff(c, hlx(), REPORT).ok


def test_a_file_parameter_the_device_lacks_fails_and_unnamed_device_parameters_are_info():
    d = RB.readback_diff(chain(), hlx(extra_node={"Bogus": 0.5}), REPORT)
    assert not d.ok and d.missing == ["block3.Bogus: in the file, no such parameter on the device"]
    c = chain()
    c["slots"][1]["params"].append(_p(3, "Hum", 0.5))
    d = RB.readback_diff(c, hlx(), REPORT)
    assert d.ok and any("block3.Hum" in u for u in d.unspecified)


def test_an_enabled_block_the_file_does_not_have_fails():
    stray = {"pos": 4, "kind": "Block", "enabled": True, "model_id": "Comp", "params": []}
    assert not RB.readback_diff(chain(extra_block=stray), hlx(), REPORT).ok
    assert RB.readback_diff(chain(extra_block={**stray, "enabled": False}), hlx(), REPORT).ok


def test_slots_follow_the_stomp_layout_without_a_report():
    assert RB.slot_map(hlx()) == {"block3": (2, False), "cab0": (2, True), "block5": (3, False)}
    assert RB.readback_diff(chain(), hlx()).ok


def test_effective_values_follow_the_current_snapshot():
    h = hlx(snap_fb=0.52)
    assert RB.effective_values(h)["block5"]["Feedback"] == 0.52
    h["data"]["tone"]["snapshot1"]["controllers"]["dsp0"]["block5"]["Feedback"]["@value"] = 0.7
    h["data"]["tone"]["global"]["@current_snapshot"] = 1
    assert RB.effective_values(h)["block5"]["Feedback"] == 0.7


def test_snapshot_conflicts_and_sync():
    h = hlx(snap_mix=0.23, snap_fb=0.52)       # the pre-T051 H.hlx: blocks 0.25/0.40, snapshots 0.23/0.52
    conflicts = RB.snapshot_conflicts(h)
    assert len(conflicts) == 6 and "block5.Feedback" in conflicts[1]
    assert RB.sync_snapshot_controllers(h) == conflicts
    assert RB.snapshot_conflicts(h) == [] and RB.effective_values(h)["block5"]["Mix"] == 0.25


def test_decoded_document_snapshot_values_are_checked():
    # The old compiler: block values right, snapshot values left at the base's.
    stale = chain(snap=[(0.23, 0.52)] * 3)
    d = RB.readback_diff(stale, hlx(), REPORT)
    assert not d.ok and sum("snapshot" in m and "recalls" in m for m in d.mismatches) == 6
    assert RB.readback_diff(chain(snap=[(0.25, 0.40)] * 3), hlx(), REPORT).ok


# --------------------------------------------------------------------------- the engine


def make_preset(mix=0.25, fb=0.40, snap_mix=None, snap_fb=None):
    snaps = [Snapshot(name=f"SNAPSHOT {n + 1}", enabled={"block3": True, "block5": True},
                      params={"block5": {"Mix": mix if snap_mix is None else snap_mix,
                                         "Feedback": fb if snap_fb is None else snap_fb}}) for n in range(3)]
    return Preset(name="rank2_H", snapshots=snaps, slots=[
        Slot("amp", "AmpP", {"Drive": 0.836, "Fat": True, "SSwitch": 2}, True, key="block3", position=1,
             extra={"@cab": "cab0", "@path": 0}),
        Slot("cab", "CabU", {"HighCut": 8000.0}, True, key="cab0", extra={"@mic": 1}),
        Slot("delay", "DelayD", {"Time": 0.375, "Feedback": fb, "Mix": mix, "TempoSync1": False}, True,
             key="block5", position=2, extra={"@path": 0, "@trails": True}),
    ])


class Pedal:
    """A fake pedal and a fake stompd compile/decode working on JSON 'documents' that hold block
    values and per-snapshot controller values. On a document write the pedal recalls snapshot 0's
    controller values over the block values, as the HX Stomp does (T051)."""

    def __init__(self, old_compiler=False):
        self.old = old_compiler
        self.base = {"chain": chain(mix=0.23, fb=0.52, time_=0.605, routing=1, delay_on=False),
                     "snap": [[0.23, 0.52]] * 3}
        self.compiled: list[dict] = []

    def compile(self, hlx_path: Path, base: Path, out: Path) -> dict:
        d = json.loads(hlx_path.read_text())
        doc = json.loads(base.read_text())
        tone = d["data"]["tone"]
        slots = {s["pos"]: s for s in doc["chain"]["slots"]}
        for key, pos in (("block3", 2), ("block5", 3)):
            node = tone["dsp0"][key]
            slots[pos]["enabled"] = node["@enabled"]
            for p in slots[pos]["params"]:
                if p.get("sym") in node:
                    p["value"] = float(node[p["sym"]])
        slots[0]["routing"] = tone["dsp0"]["inputA"]["@input"]
        if not self.old:   # the T051 fix: snapshot controller values from the file
            doc["snap"] = [[tone[f"snapshot{n}"]["controllers"]["dsp0"]["block5"][k]["@value"]
                            for k in ("Mix", "Feedback")] for n in range(3)]
        out.write_text(json.dumps(doc))
        self.compiled.append(d)
        return copy.deepcopy(REPORT)

    @staticmethod
    def decode(doc_path: Path) -> dict:
        doc = json.loads(doc_path.read_text())
        c = copy.deepcopy(doc["chain"])
        c["snapshot_controllers"] = chain(snap=[tuple(s) for s in doc["snap"]])["snapshot_controllers"]
        return c

    def bridge(self):
        b = FakeBridge(self.base["chain"])
        orig, pedal = b.call, self

        def call(cmd, timeout_s=30.0, **a):
            if cmd == "backup":
                b.calls.append((cmd, a))
                Path(a["path"]).write_text(json.dumps({"chain": b.chain, "snap": pedal.base["snap"]}))
                return {"bytes": 1}
            if cmd == "restore":
                b.calls.append((cmd, a))
                doc = json.loads(Path(a["path"]).read_text())
                b.chain = copy.deepcopy(doc["chain"])
                mix, fb = doc["snap"][0]           # snapshot recall over the block values
                ps = {p["sym"]: p for p in b._slot(3)["params"] if "sym" in p}
                ps["Mix"]["value"], ps["Feedback"]["value"] = mix, fb
                pedal.base["snap"] = doc["snap"]
                return {"restored": a["path"]}
            return orig(cmd, timeout_s, **a)

        b.call = call
        return b


def engine(monkeypatch, tmp_path, pedal, decoder=True):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    cal = reamp.Calibration(latency_samples=10, measurements=[10, 10, 11], level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    b = pedal.bridge()
    eng = stomp.StompEngine(bridge=b, calibration=cal, session_dir=tmp_path, whole_preset="always",
                            compiler=pedal.compile, decoder=pedal.decode if decoder else None)
    return eng, b


def test_old_compiler_is_refused_before_anything_is_written(monkeypatch, tmp_path):
    pedal = Pedal(old_compiler=True)
    eng, b = engine(monkeypatch, tmp_path, pedal)
    with pytest.raises(stomp.ReadbackMismatch, match="compiled document.*snapshot0.controllers.block5"):
        eng.load(make_preset())
    assert "restore" not in [c for c, _ in b.calls] and eng.preset is None


def test_without_a_decoder_the_post_write_readback_catches_the_recall(monkeypatch, tmp_path):
    pedal = Pedal(old_compiler=True)
    eng, b = engine(monkeypatch, tmp_path, pedal, decoder=False)
    with pytest.raises(stomp.ReadbackMismatch, match="read-back chain.*block5.Feedback: device 0.52 != file 0.4"):
        eng.load(make_preset())
    assert "restore" in [c for c, _ in b.calls]
    assert eng.preset is None and eng.whole_writes[-1]["checks"]["readback"]["ok"] is False
    with pytest.raises(stomp.EngineError, match="before load"):
        eng.set_params({(2, "Mix"): 0.1})


def test_fixed_compiler_lands_every_parameter(monkeypatch, tmp_path):
    pedal = Pedal()
    eng, b = engine(monkeypatch, tmp_path, pedal)
    eng.load(make_preset())
    checks = eng.whole_writes[-1]["checks"]
    assert checks["document"]["ok"] and checks["readback"]["ok"] and checks["snapshot_sync"] == []
    ps = {p["sym"]: p["value"] for p in b._slot(3)["params"] if "sym" in p}
    assert (ps["Mix"], ps["Feedback"], ps["Time"]) == (0.25, 0.40, 0.375)
    assert eng.last_readback["checked"] > 10


def test_engine_renders_block_values_when_the_snapshots_disagree(monkeypatch, tmp_path):
    # The pre-T051 H.hlx: blocks 0.25/0.40, snapshot controllers 0.23/0.52 (copied from known.hlx).
    pedal = Pedal()
    eng, b = engine(monkeypatch, tmp_path, pedal)
    eng.load(make_preset(snap_mix=0.23, snap_fb=0.52))
    assert len(eng.whole_writes[-1]["checks"]["snapshot_sync"]) == 6
    ctl = pedal.compiled[-1]["data"]["tone"]["snapshot0"]["controllers"]["dsp0"]["block5"]
    assert (ctl["Mix"]["@value"], ctl["Feedback"]["@value"]) == (0.25, 0.40)
    ps = {p["sym"]: p["value"] for p in b._slot(3)["params"] if "sym" in p}
    assert (ps["Mix"], ps["Feedback"]) == (0.25, 0.40)


def test_in_place_load_runs_the_same_readback(monkeypatch, tmp_path):
    from test_stomp import make_engine
    from test_stomp import make_preset as small_preset

    eng, b = make_engine(monkeypatch, tmp_path)
    eng.load(small_preset(drive=0.9))
    assert eng.last_readback["ok"] and eng.last_readback["checked"] >= 8
    orig = b.call

    def lossy(cmd, timeout_s=30.0, **a):   # a pedal that ignores parameter writes
        return {} if cmd == "set" else orig(cmd, timeout_s, **a)

    b.call = lossy
    with pytest.raises(stomp.ReadbackMismatch, match="block3.Drive"):
        eng.load(small_preset(drive=0.3))
    assert eng.preset is None


# --------------------------------------------------------------------------- offline binaries


def _fake_exe(tmp_path, name, modes, reply):
    import sys

    exe = tmp_path / name
    marks = "".join(f"# usage: stompd {m} <...>\n" for m in modes)
    exe.write_text(f"#!{sys.executable}\n{marks}import json\nprint(json.dumps({reply!r}))\n")
    exe.chmod(0o755)
    return exe


def test_a_binary_without_the_offline_mode_is_never_run(tmp_path):
    usb_helper = _fake_exe(tmp_path, "stompd", [], {"ok": True, "event": "ready"})
    with pytest.raises(stomp.EngineError, match="no offline `compile` mode"):
        stomp.compile_document(tmp_path / "a.hlx", tmp_path / "b.bin", tmp_path / "c.bin", compiler=usb_helper)
    with pytest.raises(stomp.EngineError, match="no offline `decode` mode"):
        stomp.decode_document(tmp_path / "c.bin", compiler=usb_helper)
    old_compile = _fake_exe(tmp_path, "old", ["compile"], {"ok": True})
    assert stomp.supports(old_compile, "compile") and not stomp.supports(old_compile, "decode")


def test_decode_document_returns_the_chain(tmp_path):
    exe = _fake_exe(tmp_path, "stompd-compile", ["compile", "decode"], {"ok": True, "chain": chain(snap=[(0.25, 0.4)])})
    c = stomp.decode_document(tmp_path / "c.bin", compiler=exe)
    assert RB.readback_diff(c, hlx(), REPORT).ok


# --------------------------------------------------------------------------- scripts


def _load_script(name):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_inject_chain_diff_compares_every_parameter(tmp_path):
    si = _load_script("stomp_inject")
    f = tmp_path / "H.hlx"
    f.write_text(json.dumps(hlx()))
    assert si.chain_diff(chain(), f, REPORT) == []
    diff = si.chain_diff(chain(fb=0.52), f, REPORT)
    assert len(diff) == 1 and diff[0].startswith("block5.Feedback")


def test_parity_records_the_readback_and_skips_a_preset_that_did_not_land(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    pn = _load_script("parity_null")
    presets = {n: pn.INJECT / f"{n}.hlx" for n in ("K", "A")}
    if not all(p.exists() for p in presets.values()):
        pytest.skip("cache/stomp/inject variants not generated")
    di = tmp_path / "di.wav"
    sf.write(str(di), (0.1 * np.sin(np.arange(48000) / 7)).astype(np.float32), 48000, subtype="FLOAT")

    class Eng:
        def __init__(self):
            self.original = {"name": "Smoke Known"}
            self.whole_writes, self.restore_report, self.backup_path = [], {}, Path("x")

        def capabilities(self):
            from types import SimpleNamespace

            return SimpleNamespace(firmware_version="3.50", extras={})

        def load(self, preset):
            ok = preset.name != "T018 A drive"
            self.whole_writes.append({"checks": {"readback": {"ok": ok}}})
            if not ok:
                raise stomp.ReadbackMismatch("the pedal's read-back chain does not match", RB.ReadbackDiff(["x"]))

        def render(self, x):
            return x.copy()

        def close(self):
            pass

    s = pn.run(presets, di, tmp_path / "o", engines=("stomp",), stomp_factory=Eng, use_ledger=False,
               verify_restore=lambda e: {})
    rb = s["engines"]["stomp"]["readback"]
    assert rb["K"]["readback"]["ok"] is True and rb["A"]["readback"]["ok"] is False
    assert any("ReadbackMismatch" in e for e in s["errors"])
    assert (tmp_path / "o" / "raw" / "K_stomp.wav").exists() and not (tmp_path / "o" / "raw" / "A_stomp.wav").exists()
