"""scripts/stomp_verify.py (T108): population rules, exactness rule, lag and settle measures,
floors, the check-2 plan and the offline analysis on a synthetic run. No pedal, no Line 6 data
(a fake catalog; build_hlx needs cache/stomp/inject/K.hlx and is skipped without it)."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import itertools

import stomp_verify as V


def _p(kind="continuous", lo=0.0, hi=1.0, default=0.5, display="generic_knob", stereo_only=None):
    return {"kind": kind, "min": lo, "max": hi, "default": default, "display": display, "stereo_only": stereo_only}


CAT = {"models": {
    "HD2_DistA": {"category": "drive", "stomp": True, "mono": True, "params": {
        "Gain": _p(), "Level": _p(), "Hum": _p(), "TempoSync1": _p("bool", False, True, False),
        "Mode": _p("stepped", 0, 4, 1), "Spread": _p(stereo_only=True), "@enabled": _p("bool", False, True, True)}},
    "HD2_DistB": {"category": "drive", "stomp": True, "mono": True, "params": {"Gain": _p()}},
    "HD2_DistStereoOnly": {"category": "drive", "stomp": True, "mono": False, "params": {"Gain": _p()}},
    "HD2_DistNew": {"category": "drive", "stomp": True, "mono": True, "stomp_min_firmware": "0x03900000",
                    "params": {"Gain": _p()}},
    "HD2_DelayA": {"category": "delay", "stomp": True, "mono": True, "params": {
        "Time": _p(lo=0.0, hi=2.0, display="time_ms"), "Feedback": _p(display="percent")}},
}}


def test_pool_filters_mono_and_firmware():
    assert V.model_pool(CAT, "drive") == ["HD2_DistA", "HD2_DistB"]


def test_draw_rules_fixed_capped_and_typed():
    rng = random.Random(1)
    for _ in range(300):
        vals, us = V.draw_params(CAT, "HD2_DistA", rng, "drive")
        assert "Spread" not in vals and "@enabled" not in vals
        assert vals["Hum"] == 0.0 and us["Hum"] == 0.0
        assert vals["TempoSync1"] is False
        assert isinstance(vals["Mode"], int) and 0 <= vals["Mode"] <= 4
        assert V.LEVEL_U[0] <= us["Level"] <= V.LEVEL_U[1]
        d, du = V.draw_params(CAT, "HD2_DelayA", rng, "delay")
        assert d["Time"] <= V.DELAY_TIME_MAX_S + 1e-12 and du["Feedback"] <= V.FEEDBACK_MAX_U
        assert d["Time"] == pytest.approx(0.0 + du["Time"] * 2.0)


def test_exactness_rule():
    v = 0.123456789123
    ok = V.compare_param("Continuous", v, v, float(np.float32(v)), float(np.float32(v)), float(np.float32(v)),
                         float(np.float32(v)))
    assert ok["ok"] and ok["exact"] and ok["dev"] < 1e-7
    q = V.compare_param("Continuous", v, v, 0.12, 0.12, 0.12, 0.12)          # a device grid would fail
    assert not q["ok"] and not q["exact"]
    w = V.compare_param("Continuous", v, v, v, v, v, 0.5)                     # whole write differs
    assert not w["ok"] and not w["whole_same"]
    rep = V.compare_param("Continuous", v, v, 0.9, v, v, v)                    # set reply differs
    assert not rep["ok"]
    assert V.compare_param("Enum", 3, 3.0, 3.0, 3.0, 3.0, 3.0)["ok"]
    assert V.compare_param("Switch", True, 1.0, 1.0, 1.0, 1.0, 1.0)["ok"]
    assert not V.compare_param("Switch", True, 1.0, 1.0, 0.0, 0.0, 0.0)["ok"]
    assert V.compare_param("Enum", 6, None, None, 6.0, 6.0, 6.0)["ok"]        # set by the load (no set reply)
    assert not V.compare_param("Continuous", 0.5, 0.25, 0.5, 0.5, 0.5, 0.5)["ok"]   # sent != file value


def test_unit_types():
    assert V.unit_type("LowCut", _p(lo=19.9, hi=500, display="cab_low_cut")) == "Hz"
    assert V.unit_type("Level", _p(lo=-60, hi=6, display="volume")) == "dB"
    assert V.unit_type("Time", _p(lo=0, hi=2, display="time_ms")) == "time"
    assert V.unit_type("Gain", _p()) == "knob01"
    assert V.unit_type("Mode", _p("stepped", 0, 4)) == "menu"
    assert V.unit_type("@mic", _p("stepped", 0, 15, display="mic")) == "menu"
    assert V.unit_type("Boost", _p("bool", False, True)) == "switch"


def test_int_lag_recovers_shifts():
    rng = np.random.default_rng(0)
    w, n = V.LAG_SEARCH, 9600
    sig = rng.standard_normal(n + 4 * w).astype(np.float32)
    ref = sig[w:w + n + 2 * w]
    for lag in (0, 1, -1, 3, -7, 100):
        x = sig[w - lag:w - lag + n + 2 * w]
        assert V.int_lag(ref, x) == lag


def test_settle_index_finds_the_switch():
    rng = np.random.default_rng(1)
    n = 5 * V.SR
    x = rng.standard_normal(n)
    ya, yb = 0.2 * x, 0.8 * np.tanh(2 * x)
    y = ya.copy()
    at = int(2.3 * V.SR)
    y[at:] = yb[at:] + 1e-4 * rng.standard_normal(n - at)
    si = V.settle_index(y, yb + 1e-4 * rng.standard_normal(n))
    assert at <= si["index"] <= at + V.SETTLE_WIN
    assert si["shift"] == 0


def test_floors_time_variance_and_render_rule():
    reps = {"a": -80.0, "b": -60.0, "c": -10.0, "d": -45.0}
    cats = {"a": "drive", "b": "drive", "c": "reverb", "d": "reverb"}
    floors, tv = V.category_floors(reps, cats)
    assert tv == ["c"] and floors == {"drive": -60.0, "reverb": -45.0}
    assert V.render_pass(-58.0, -60.0) and not V.render_pass(-56.0, -60.0)
    assert not V.render_pass(-90.0, None)


def test_check2_plan_shape():
    items = [{"id": f"i{k:02d}", "category": c, "scored": c in V.SCORED, "model": f"m{k}"}
             for k, c in enumerate([c for c, n in V.COUNTS.items() for _ in range(n)])]
    chosen, seq = V.check2_plan(items)
    assert len(chosen) == V.C2_ITEMS == len(set(chosen))
    cats = {it["id"]: it["category"] for it in items}
    assert {cats[i] for i in chosen} >= set(V.SCORED)
    assert len(seq) == V.C2_ITEMS * V.C2_REPEATS + V.C2_DISTRACTORS
    assert all(seq.count(i) == V.C2_REPEATS for i in chosen)
    assert all(i not in chosen for i in seq if seq.count(i) != V.C2_REPEATS)


def test_order_items_avoids_consecutive_models():
    items = [{"id": str(k), "model": m} for k, m in enumerate("aabbccdd")]
    out = V.order_items(items, random.Random(0))
    assert all(a["model"] != b["model"] for a, b in itertools.pairwise(out))


def test_missing_params_parse():
    msgs = ["block0.TempoSync2: in the file, no such parameter on the device",
            "cab0.Wow Flt: in the file, no such parameter on the device", "block0.Gain: device 1 != file 2"]
    assert V.missing_params(msgs) == [("block0", "TempoSync2"), ("cab0", "Wow Flt")]


@pytest.mark.skipif(not V.K_HLX.exists(), reason="K.hlx frame is local (cache/)")
def test_build_hlx_single_block_frame():
    it = {"id": "i00", "model": "HD2_DistA", "params": {"Gain": 0.3}, "cab": "HD2_CabX", "cab_params": {"@mic": 3}}
    d = V.build_hlx(it)
    dsp, tone = d["data"]["tone"]["dsp0"], d["data"]["tone"]
    assert dsp["block0"]["@position"] == 0 and dsp["block0"]["@cab"] == "cab0" and dsp["cab0"]["@mic"] == 3
    assert not [k for k in dsp if k.startswith("block") and k != "block0"]
    assert dsp["inputA"]["@input"] == V.USB56 and "controller" not in tone and "footswitch" not in tone
    assert tone["snapshot0"]["blocks"] == {"dsp0": {"block0": True}}


def test_analyse_on_a_synthetic_run(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "OUT", tmp_path / "verify")
    rng = np.random.default_rng(2)
    items = [{"id": "i00", "category": "drive", "model": "m0", "scored": True},
             {"id": "i01", "category": "drive", "model": "m1", "scored": True},
             {"id": "i02", "category": "mod", "model": "m2", "scored": False}]
    (tmp_path / "verify" / "105").mkdir(parents=True)
    (tmp_path / "verify" / "105" / "items.json").write_text(json.dumps({"items": items}))
    run = tmp_path / "run"
    (run / "renders").mkdir(parents=True)
    (run / "raw").mkdir()
    n, w = 9600, V.LAG_SEARCH
    base = rng.standard_normal(n + 2 * w).astype(np.float32)
    renders = []

    def put(label, group, noise, lag=0):
        y = base[w:w + n] + noise * rng.standard_normal(n).astype(np.float32)
        np.save(run / "renders" / f"{label}.npy", y.astype(np.float32))
        np.save(run / "raw" / f"{label}.npy", np.roll(base, lag))
        renders.append({"label": label, "group": group})

    good = {"ok": True, "exact": True, "type": "knob01", "dev": 0.0, "v": 0.5, "r": 0.5}
    c1 = []
    for iid, noise in (("i00", 1e-4), ("i01", 1e-3)):
        put(f"c1_{iid}_r1", iid, noise)
        put(f"c1_{iid}_r2", iid, noise)
        put(f"c1_{iid}_in", iid, noise)
        c1.append({"id": iid, "category": "drive", "model": "m", "scored": True, "readback_ok": True,
                   "params": {"main:Gain": good}, "not_on_device": [], "whole_readback": {"ok": True}})
    c1.append({"id": "i02", "category": "mod", "model": "m2", "scored": False, "readback_ok": True,
               "params": {"main:Rate": good}, "not_on_device": [], "whole_readback": {"ok": True}})
    c2 = []
    for k, iid in enumerate(["i00", "i01", "i00", "i02", "i00", "i01", "i01"]):
        put(f"c2_{k:02d}_{iid}", iid, 1e-4 if iid == "i00" else 1e-3, lag=1 if k == 6 else 0)
        c2.append({"k": k, "id": iid, "chosen": iid != "i02", "route": "in place"})
    ses = {"seed": 105, "restore_ok": True, "errors": [], "stage": "done", "check1": c1, "check2": c2,
           "check3": {"trials": []}, "renders": renders, "latency_samples": 1207,
           "check4": {"unforced_trips": False, "detected": True, "refused_after": True},
           "sentinel_report": {"enabled": True, "checks": 3, "nulls_db": [-80, -79], "threshold_db": -60}}
    (run / "session.json").write_text(json.dumps(ses))
    v = V.analyse(run)
    assert v["floors_db"]["drive"] == pytest.approx(v["repeat_db"]["i01"])
    assert v["check1"]["readback_pass"] == 3 and v["check1"]["render_pass"] == 2
    assert v["check2"]["pass"] == v["check2"]["total"] == 4          # 6 chosen renders, 2 firsts
    assert v["check2"]["latency"]["lag_values"] == [0, 1] and len(v["check2"]["latency"]["nonzero"]) == 1
    assert not v["check2"]["passed"]                                   # one render one sample late
    assert v["check4"]["live_passed"]
    assert not v["check3"]["passed"]                                   # no check-3 data
