"""T110-T112 (stomp-engine): stream-slip guard, device templates, delay/reverb whole writes.
Offline: fake bridge, fake audio stream, no pedal, no helper, no Line 6 data."""
from __future__ import annotations

import json

import numpy as np
import pytest
from test_stomp import FakeBridge, _p, make_chain, make_preset

from tonematch import reamp
from tonematch.engines import stomp
from tonematch.engines import stomp_readback as rb

pytestmark = pytest.mark.stomp_robust
SR = 48_000


def clip(n=SR, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32) * 0.05
    x[int(0.6 * n):int(0.75 * n)] = 0.0          # a silent gap inside the clip
    return x


def loopback(sent, pre, lat, total, gain=0.7071, slip_at=None, burst_at=None, drop=True):
    """What USB 3/4 would record: the sent stream delayed by lat, scaled, with an optional
    one-sample slip (a dropped or repeated sample) or a 3 ms burst of zeros at an index of sent."""
    s = np.asarray(sent, np.float64).copy()
    if slip_at is not None:
        s = np.delete(s, slip_at) if drop else np.insert(s, slip_at, s[slip_at])
        s = np.concatenate([s, np.zeros(len(sent) - len(s))]) if len(s) < len(sent) else s[:len(sent)]
    if burst_at is not None:
        s[burst_at:burst_at + 144] = 0.0
    y = np.zeros(total)
    y[pre + lat:pre + lat + len(s)] = gain * s
    q = 2.0 ** -23                                  # 24-bit
    return np.round(y / q) * q


# --------------------------------------------------------------------------- slip_check (pure)


def test_exact_loopback_passes_and_reports_lag_and_gain():
    x = np.concatenate([clip(), stomp.slip_marker()])
    y = loopback(x, 4800, 1207, 4800 + len(x) + 3000)
    v = stomp.slip_check(y, x, 4800, 1207)
    assert v["ok"] and v["lag"] == 1207 and abs(v["gain_db"] + 3.01) < 0.01
    assert v["worst_db"] < -90


@pytest.mark.parametrize("where", [0.2, 0.55, 0.9])
@pytest.mark.parametrize("drop", [True, False])
def test_one_sample_slip_anywhere_in_the_clip_is_caught(where, drop):
    x = np.concatenate([clip(), stomp.slip_marker()])
    y = loopback(x, 4800, 1207, 4800 + len(x) + 3000, slip_at=int(where * SR), drop=drop)
    v = stomp.slip_check(y, x, 4800, 1207)
    assert not v["ok"] and v["worst_db"] > -40


def test_slip_in_the_clips_trailing_silence_is_caught_by_the_marker():
    x0 = clip()
    x0[int(0.9 * SR):] = 0.0                       # the clip ends in silence
    x = np.concatenate([x0, stomp.slip_marker()])
    y = loopback(x, 4800, 1207, 4800 + len(x) + 3000, slip_at=int(0.95 * SR))
    assert not stomp.slip_check(y, x, 4800, 1207)["ok"]
    assert stomp.slip_check(y[:4800 + 1207 + SR], x0, 4800, 1207)["ok"]   # without the marker: unseen


def test_corrupted_burst_is_caught():
    x = np.concatenate([clip(), stomp.slip_marker()])
    y = loopback(x, 4800, 1207, 4800 + len(x) + 3000, burst_at=int(0.3 * SR))
    v = stomp.slip_check(y, x, 4800, 1207)
    assert not v["ok"] and 0.29 < v["worst_at_s"] < 0.31


def test_slip_before_the_clip_only_moves_the_lag():
    x = np.concatenate([clip(), stomp.slip_marker()])
    y = loopback(x, 4800, 1208, 4800 + len(x) + 3000)
    v = stomp.slip_check(y, x, 4800, 1207)
    assert v["ok"] and v["lag"] == 1208


def test_silent_loopback_is_not_a_pass():
    x = np.concatenate([clip(), stomp.slip_marker()])
    v = stomp.slip_check(np.zeros(4800 + len(x) + 3000), x, 4800, 1207)
    assert not v["ok"] and "no loopback" in v["reason"]


# --------------------------------------------------------------------------- pilot layout (pure)


def test_with_pilot_adds_a_bypassed_block_on_path_b_and_keeps_keys():
    p = make_preset()
    m, why = stomp.with_pilot(p)
    assert why is None
    d = m.to_hlx_dict()["data"]["tone"]
    assert d["global"]["@topology0"] == "SAB" and d["dsp0"]["outputB"]["@output"] == stomp.OUT_B_USB34
    pil = [s for s in m.slots if s.model_id == stomp.PILOT_MODEL]
    assert len(pil) == 1 and pil[0].extra.get("@path") == 1 and pil[0].enabled is False
    assert {s.key for s in p.slots} <= {s.key for s in m.slots}
    target_map = {i: (100 + i, False) for i in range(len(m.slots))}
    remap = stomp.remap_slots(p, m, target_map)
    for i, s in enumerate(p.slots):
        j = next(j for j, t in enumerate(m.slots) if t.key == s.key)
        assert remap[i] == target_map[j]


def test_pilot_unfit_reasons():
    p = make_preset()
    assert stomp.pilot_unfit(p) is None
    m, _ = stomp.with_pilot(p)
    assert "Path B" in stomp.pilot_unfit(m)
    gated = make_preset()
    raw = gated.to_hlx_dict()
    raw["data"]["tone"]["dsp0"].setdefault("inputA", {})["noiseGate"] = True
    from tonematch import preset as P

    assert "noise gate" in stomp.pilot_unfit(P.from_dict(raw))


def test_plan_load_stays_in_place_with_the_known_pilot_only():
    p, _ = stomp.with_pilot(make_preset())
    ch = make_chain()
    ch["slots"].append({"pos": stomp.PILOT_POS, "kind": "Block", "enabled": False, "model": 9,
                        "model_id": stomp.PILOT_MODEL, "params": []})
    with pytest.raises(stomp.UnsupportedModelChange):
        stomp.plan_load(p, ch)
    plan = stomp.plan_load(p, ch, allow_b=(stomp.PILOT_POS,))
    assert stomp.PILOT_POS in plan["targets"]
    ch["slots"][-1]["model_id"] = "Other"
    with pytest.raises(stomp.UnsupportedModelChange):
        stomp.plan_load(p, ch, allow_b=(stomp.PILOT_POS,))


# --------------------------------------------------------------------------- guarded renders (fake stream)


def guarded_engine(monkeypatch, tmp_path, plan):
    """An engine that believes the pilot is on the pedal; reamp.playrec returns Path A = 0.5 * x and
    the loopback, with a slip in the takes ``plan`` names (index -> slip sample)."""
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    cal = reamp.Calibration(latency_samples=1207, measurements=[1207] * 3, level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    eng = stomp.StompEngine(bridge=FakeBridge(make_chain()), calibration=cal, session_dir=tmp_path,
                            whole_preset="off", slip_guard="on")
    eng._pilot_on_pedal = True
    calls = {"n": 0}

    def playrec(mono, device=None, pre_s=1.0, tail_s=2.0, out_pair=None, in_pair=None):
        k = calls["n"]
        calls["n"] += 1
        pre, tail = int(pre_s * SR), int(tail_s * SR)
        total = pre + len(mono) + tail
        lb = loopback(mono, pre, 1207, total, slip_at=plan.get(k))
        a = loopback(mono, pre, 1207, total, gain=0.5, slip_at=plan.get(k))
        rec = np.zeros((total, 8), np.float32)
        rec[:, 0] = rec[:, 1] = a
        rec[:, 2] = rec[:, 3] = lb
        return (rec if in_pair is None else rec[:, [in_pair[0] - 1, in_pair[1] - 1]]), pre

    monkeypatch.setattr(reamp, "playrec", playrec)
    return eng, calls


def test_a_slipped_take_is_taken_again_and_counted(monkeypatch, tmp_path):
    eng, calls = guarded_engine(monkeypatch, tmp_path, {0: 20000})
    eng.keep_takes = True
    x = clip()
    y = eng.render(x)
    assert calls["n"] == 2
    st = eng.stats()["slip_guard"]
    assert st["guarded"] == 1 and st["takes"] == 2 and st["retakes"] == 1 and st["slips"] == 1
    assert np.allclose(y, 0.5 * x, atol=1e-6)
    assert [t["verdict"]["ok"] for t in eng.takes] == [False, True]


def test_a_slipped_take_never_becomes_the_alignment_reference(monkeypatch, tmp_path):
    eng, _ = guarded_engine(monkeypatch, tmp_path, {0: 20000})
    x = clip()
    eng.render(x)
    (ref,) = eng._refs.values()
    assert np.allclose(ref, 0.5 * x, atol=1e-6)


def test_every_take_slipped_raises_stream_slip(monkeypatch, tmp_path):
    eng, calls = guarded_engine(monkeypatch, tmp_path, {k: 20000 for k in range(10)})
    with pytest.raises(stomp.StreamSlip) as e:
        eng.render(clip())
    assert calls["n"] == eng.slip_retakes + 1 and len(e.value.report["verdicts"]) == eng.slip_retakes + 1
    assert eng.stats()["slip_guard"]["failed"] == 1


def test_unguarded_renders_are_counted_with_the_reason(monkeypatch, tmp_path):
    eng, _ = guarded_engine(monkeypatch, tmp_path, {})
    eng._pilot_on_pedal = False
    eng.pilot_reason = "8 blocks: no cell left for the pilot"
    eng.render(clip())
    st = eng.stats()["slip_guard"]
    assert st["unguarded"] == 1 and st["unguarded_why"] == {"8 blocks: no cell left for the pilot": 1}


def test_guard_off_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TONEMATCH_STOMP_SLIP_GUARD", "off")
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    cal = reamp.Calibration(latency_samples=10, measurements=[10] * 3, level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    eng = stomp.StompEngine(bridge=FakeBridge(make_chain()), calibration=cal, session_dir=tmp_path)
    assert eng.slip_guard == "off" and eng._pilot_target(make_preset())[1] == "slip guard off"
    monkeypatch.setenv("TONEMATCH_STOMP_SLIP_GUARD", "bogus")
    with pytest.raises(stomp.EngineError):
        stomp.StompEngine(bridge=FakeBridge(make_chain()), calibration=cal, session_dir=tmp_path)


# --------------------------------------------------------------------------- T111 policy (pure)


def test_ambient_changes_lists_delay_and_reverb_values_only(monkeypatch):
    monkeypatch.setattr(stomp, "_CATEGORY_CACHE", {"DelayA": "delay", "AmpA": "amp", "_": ""})
    p = make_preset(delay_on=True)
    ch = make_chain(delay_on=True)
    plan = stomp.plan_load(p, ch)
    dev = stomp._block_slots(ch)
    assert stomp.ambient_changes(plan, dev) == []          # the delay is as the pedal has it
    p.slots[2].params["Mix"] = 0.4
    plan = stomp.plan_load(p, ch)
    assert stomp.ambient_changes(plan, dev) == ["3:Mix"]
    p.slots[0].params["Drive"] = 0.9                       # an amp change stays in place
    assert stomp.ambient_changes(stomp.plan_load(p, ch), dev) == ["3:Mix"]
    p.slots[2].enabled = False                             # a bypassed delay: not a time change
    assert stomp.ambient_changes(stomp.plan_load(p, ch), dev) == []


# --------------------------------------------------------------------------- T110 templates (fake bridge)


class TemplateBridge(FakeBridge):
    def __init__(self, chain, refuse=()):
        super().__init__(chain)
        self.refuse = set(refuse)
        self.saved = None

    def call(self, cmd, timeout_s=30.0, **a):
        if cmd == "model" and (a["model"], a.get("cab")) in self.refuse:
            self.calls.append((cmd, a))
            from tonematch.engines.stomp_bridge import BridgeError

            raise BridgeError("model: the device refused: error -21")
        if cmd == "backup":
            self.calls.append((cmd, a))
            from pathlib import Path

            Path(a["path"]).write_bytes(json.dumps(self.chain).encode())
            return {"bytes": 1}
        if cmd == "restore":
            self.calls.append((cmd, a))
            from pathlib import Path

            self.chain = json.loads(Path(a["path"]).read_bytes())
            return {"restored": a["path"]}
        if cmd == "model":
            r = super().call(cmd, timeout_s, **a)
            s = self._slot(a["pos"])
            s["model"] = 600 + len(self.calls)
            return r
        return super().call(cmd, timeout_s, **a)


def template_engine(monkeypatch, tmp_path, refuse=()):
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    monkeypatch.setattr(stomp, "TEMPLATE_ROOT", tmp_path / "templates")
    cal = reamp.Calibration(latency_samples=10, measurements=[10] * 3, level_gain_db=0.0, tone_in_dbfs=-18,
                            tone_out_dbfs=-18, firmware="3.50", pa_latency=reamp.PA_LATENCY)
    b = TemplateBridge(make_chain(), refuse)
    eng = stomp.StompEngine(bridge=b, calibration=cal, session_dir=tmp_path, whole_preset="off")
    return eng, b


def test_needed_templates_and_capture_put_the_buffer_back(monkeypatch, tmp_path):
    eng, b = template_engine(monkeypatch, tmp_path)
    p = make_preset()
    p.slots[2].model_id = "DelayB"                          # a model the pedal does not hold at slot 3
    assert eng.needed_templates(p) == [("DelayB", None)]
    before = stomp.chain_signature(b.chain)
    got = eng.capture_templates([("DelayB", None)])
    assert list(got) == ["DelayB|"] and (eng._template_dir() / got["DelayB|"]).exists()
    assert stomp.chain_signature(b.chain) == before        # the scratch buffer is as it was
    assert eng.needed_templates(p) == []                   # cached for the firmware
    assert [c for c, _ in b.calls].count("restore") == 1


def test_a_model_the_device_refuses_is_recorded_and_never_written(monkeypatch, tmp_path):
    eng, _b = template_engine(monkeypatch, tmp_path, refuse={("AmpM", "CabW")})
    with pytest.raises(stomp.ModelRefused):
        eng.capture_templates([("AmpM", "CabW")])
    p = make_preset(model_id="AmpM", cab_id="CabW")
    with pytest.raises(stomp.ModelRefused):
        eng.needed_templates(p)
    idx = json.loads((eng._template_dir() / "index.json").read_text())
    assert "AmpM|CabW" in idx["refused"]


def test_compile_document_passes_the_template_dir(monkeypatch, tmp_path):
    seen = {}

    class R:
        returncode = 0
        stdout = json.dumps({"ok": True, "report": {"slots": []}})
        stderr = ""

    def run(args, **kw):
        seen["env"] = kw.get("env") or {}
        return R()

    monkeypatch.setattr(stomp, "_offline_exe", lambda mode, exe: tmp_path / "stompd")
    monkeypatch.setattr(stomp.subprocess, "run", run)
    (tmp_path / "t").mkdir()
    stomp.compile_document(tmp_path / "a.hlx", tmp_path / "b.bin", tmp_path / "c.bin", templates=tmp_path / "t")
    assert seen["env"]["STOMPD_TEMPLATES"] == str(tmp_path / "t")
    stomp.compile_document(tmp_path / "a.hlx", tmp_path / "b.bin", tmp_path / "c.bin", templates=tmp_path / "none")
    assert "STOMPD_TEMPLATES" not in seen["env"] or seen["env"]["STOMPD_TEMPLATES"] != str(tmp_path / "none")


# --------------------------------------------------------------------------- readback (T110)


def test_trailing_extra_is_the_trails_value_and_middle_extras_are_hidden():
    dev = [_p(0, "Decay", 1.0), {"i": 1, "extra": 0, "value": 0.362}, {"i": 2, "extra": 1, "value": 2.0},
           {"i": 3, "extra": 2, "value": 0.0}]
    node = {"@model": "X", "Decay": 1.0, "@trails": False}
    diff = rb.ReadbackDiff()
    rb._compare_params("b", node, rb.effective_values({"data": {"tone": {"dsp0": {"b": node}}}}).get("b", node),
                       dev, diff)
    assert diff.mismatches == [] and diff.missing == []
    assert sum("extra value" in u for u in diff.unspecified) == 2


def test_unnamed_param_at_its_catalog_default_is_info(monkeypatch):
    from tonematch import catalog as C

    monkeypatch.setattr(rb, "_STEREO_ONLY", {})
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: {"models": {"HD2_ReverbNonLinear": {"params": {
        "Decay": {"min": 0.05, "max": 2, "default": 1.0}, "TempoSync2": {"kind": "bool", "default": False},
        "SyncSelect2": {"kind": "stepped", "default": 6}}}}})
    dev = [_p(0, "Decay", 1.0, 0.05, 2.0)]
    for ts2, ss2, missing in ((False, 6, 0), (True, 6, 1), (False, 9, 1)):
        diff = rb.ReadbackDiff()
        node = {"@model": "HD2_ReverbNonLinear", "Decay": 1.0, "TempoSync2": ts2, "SyncSelect2": ss2}
        rb._compare_params("b", node, node, dev, diff)
        assert len(diff.missing) == missing
