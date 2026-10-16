#!/usr/bin/env python3
"""One HX Stomp session for the stomp-engine push (T095: S1, S2, S4, S5 evidence). Owner: stomp-engine.

Runs under ``tonematch.pedal_lock.session`` (USB device check, shared lock with owner.json) and
refuses to start if HX Edit runs or the pedal is not on the scratch preset 04C 'Smoke Known'.
Edit buffer only, never a flash write; ends with the original preset re-selected, read back and
byte-compared with the backup taken at open (``StompEngine.close(verify_bytes=True)``).

Steps, each recorded in summary.json (an error in one step is recorded and the rest still run,
except after a bridge error; the restore always runs):
  s4_11c     select 11C (read only), full readback against the human's HX Edit export of 11C
             (cache/stomp/twopath/ref_hxedit.hlx), the Simple Delay's device value list; back to 04C
  s1         for each pair: serial (load + render, twice per variant: the repeat floor) and
             render_many (two variants per pass, twice); nulls dual vs serial, wall clock
  s2         load K (single path), then K + a mono Simple Delay at a new position (a layout
             change: one whole write with full readback), render once
  s4_delay   the delay's Mix and Time read back after the whole write, then set in place
             (set_params) and read back at once and after 3 s
  s5         param-only loads in place (fallback, the default) vs the same loads as whole writes (always)

  python scripts/stomp_push_session.py [--di fixtures/smoke/di_2s.wav]
Needs TONEMATCH_STOMPD and TONEMATCH_STOMPD_COMPILE. Output: cache/stomp/hw/<stamp>-push/.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRATCH = (0, 11, "Smoke Known")         # 04C, the scratch slot the human named
REF_11C = (0, 32, "11C")
REF_11C_HLX = ROOT / "cache" / "stomp" / "twopath" / "ref_hxedit.hlx"
INJECT = ROOT / "cache" / "stomp" / "inject"
PAIRS = (("K", "A"), ("C", "K"))


def null_db(x: np.ndarray, ref: np.ndarray) -> float:
    d = float(np.sqrt(np.mean((np.asarray(x, np.float64) - ref) ** 2)))
    r = float(np.sqrt(np.mean(np.asarray(ref, np.float64) ** 2)))
    return round(20 * np.log10(max(d, 1e-12) / max(r, 1e-12)), 2)


def prep(name: str, cat: dict):
    from tonematch import preset as P

    p = P.read_hlx(INJECT / f"{name}.hlx", cat)
    for s in p.slots:   # amp noise off, as every parity run: the null then shows the path, not the hum
        for k in ("Hum", "Ripple"):
            if k in s.params:
                s.params[k] = 0.0
    return p


def with_delay(base, cat: dict):
    from tonematch.preset import Slot

    p = prep(base, cat)
    p.slots.append(Slot("delay", "HD2_DelaySimpleDelay",
                        {"Time": 0.523, "Feedback": 0.3, "Mix": 0.37, "Level": 0.0, "Scale": 0.75,
                         "SyncSelect1": 6, "TempoSync1": False}, True, key=None, position=3,
                        extra={"@path": 0, "@stereo": False, "@trails": False, "@no_snapshot_bypass": False,
                               "@type": 7}))
    return p


def delay_values(chain: dict) -> dict:
    s = next((s for s in chain["slots"] if s.get("model_id") == "HD2_DelaySimpleDelay"), None)
    if s is None:
        return {}
    return {"pos": s["pos"], "enabled": s.get("enabled"),
            "values": {(p.get("sym") or f"extra{p.get('extra')}"): round(float(p["value"]), 6) for p in s["params"]}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--di", default=str(ROOT / "fixtures" / "smoke" / "di_2s.wav"))
    ap.add_argument("--control-only", action="store_true",
                    help="no audio: skip s4_11c; s1 writes each merged pair and reads it back; s2 loads only")
    a = ap.parse_args(argv)
    if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True,
                      check=False).stdout.strip():
        sys.exit("HX Edit is running: quit it first (it holds the control interface)")
    import soundfile as sf

    from tonematch import pedal_lock, reamp
    from tonematch.catalog import load_catalog

    cat = load_catalog()
    dry, sr = sf.read(a.di, dtype="float32")
    dry = dry if dry.ndim == 1 else dry.mean(axis=1)
    assert sr == reamp.SR
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-push"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"errors": [], "steps": {}}
    t_all = time.perf_counter()
    try:
        return _session(res, run_dir, dry, cat, t_all, a.control_only)
    finally:   # an interrupt or a crash after the restore must not lose the record
        res["lock_released"] = not pedal_lock.lock_dir().exists()
        res["wall_clock_s"] = round(time.perf_counter() - t_all, 1)
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps({k: res.get(k) for k in ("errors", "bytes_equal_backup", "session_close", "lock_released",
                                                  "wall_clock_s")}, default=str))
        print(f"summary: {run_dir / 'summary.json'}")


def _session(res: dict, run_dir: Path, dry, cat: dict, t_all: float, control_only: bool = False) -> int:
    import soundfile as sf

    from tonematch import pedal_lock, reamp
    from tonematch.engines.base import render_many
    from tonematch.engines.stomp import StompEngine, merge_two_path
    from tonematch.engines.stomp_bridge import BridgeError, PowerCycleNeeded
    from tonematch.engines.stomp_readback import readback_diff

    with pedal_lock.session("stomp_push_session T095 (S1 S2 S4 S5)") as lk:
        res["lock_owner"] = lk.owner
        eng = StompEngine(session_dir=run_dir, purpose="stomp_push_session T095")
        eng.keep_documents = run_dir / "documents"
        res["info"] = {k: eng.info.get(k) for k in ("firmware", "build", "setlist", "index", "label", "name")}
        res["original"] = eng.original
        res["whole_preset"] = eng.whole_preset
        res["control_only"] = control_only
        dead = False

        def step(name, fn):
            nonlocal dead
            if dead:
                res["steps"][name] = {"skipped": "an earlier bridge error"}
                return
            t0 = time.perf_counter()
            try:
                out = fn()
                out["wall_s"] = round(time.perf_counter() - t0, 2)
                res["steps"][name] = out
                print(name, json.dumps(out, default=str)[:1500], flush=True)
            except (BridgeError, PowerCycleNeeded) as e:
                dead = True
                res["errors"].append(f"{name}: {type(e).__name__}: {e}")
                res["steps"][name] = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 - one step must not stop the restore
                res["errors"].append(f"{name}: {type(e).__name__}: {e}")
                res["steps"][name] = {"error": f"{type(e).__name__}: {e}"}
                print(name, "ERROR", e, flush=True)

        try:
            if (eng.original["setlist"], eng.original["index"], eng.original["name"]) != SCRATCH:
                raise SystemExit(f"the pedal is on {eng.original}, not 04C '{SCRATCH[2]}': nothing done")

            def s4_11c():
                r = eng.bridge.call("select", setlist=REF_11C[0], index=REF_11C[1], timeout_s=20)
                time.sleep(0.5)
                ch = eng.bridge.call("chain")
                eng.bridge.call("select", setlist=SCRATCH[0], index=SCRATCH[1], timeout_s=20)
                time.sleep(0.5)
                eng._refresh()
                (run_dir / "chain_11C.json").write_text(json.dumps(ch, indent=1))
                ref = json.loads(REF_11C_HLX.read_text())
                d = readback_diff(ch, ref, None, check_input=False)
                dl = next((s for s in ch["slots"] if s.get("model_id") == "HD2_DelaySimpleDelay"), None)
                return {"selected": r.get("label"), "name": ch.get("name"), "readback_ok": d.ok,
                        "checked": d.checked, "mismatches": d.mismatches, "missing": d.missing,
                        "scale_note": [u for u in d.unspecified if "Scale" in u],
                        "delay_device_values": [(p.get("sym") or f"extra{p.get('extra')}", round(float(p["value"]), 6))
                                                for p in (dl or {}).get("params", [])],
                        "back_on": (eng.chain.get("index"), eng.chain.get("name"))}
            if not control_only:
                step("s4_11c", s4_11c)

            def s1_control():
                out = {}
                for an, bn in PAIRS:
                    va, vb = prep(an, cat), prep(bn, cat)
                    t0 = time.perf_counter()
                    eng._load_whole(merge_two_path(va, vb, cat), t0)
                    t_dual = time.perf_counter() - t0
                    ch = eng.chain
                    out[f"{an}+{bn}"] = {
                        "dual_write_load_s": round(t_dual, 3), "readback_ok": eng.last_readback.get("ok"),
                        "readback_checked": eng.last_readback.get("checked"),
                        "mismatches": eng.last_readback.get("mismatches"), "missing": eng.last_readback.get("missing"),
                        "topology": (eng.whole_writes[-1].get("report") or {}).get("topology"),
                        "blocks": [(x["pos"], x.get("model_id"), x.get("enabled")) for x in ch["slots"]
                                   if x["kind"] in ("Block", "Split", "Join")],
                        "split": next(([(p.get("sym"), p["value"]) for p in x.get("params", [])]
                                       for x in ch["slots"] if x["kind"] == "Split"), None),
                        "outputs": [(x["pos"], x.get("routing")) for x in ch["slots"] if x["kind"] == "Output"],
                    }
                    for nm, v in (("a", va), ("b", vb)):   # the serial loads that would follow
                        t1 = time.perf_counter()
                        eng.load(v)
                        out[f"{an}+{bn}"][f"serial_load_{nm}_s"] = round(time.perf_counter() - t1, 3)
                        out[f"{an}+{bn}"][f"serial_readback_{nm}_ok"] = eng.last_readback.get("ok")
                return out

            def s1():
                out = {}
                for an, bn in PAIRS:
                    va, vb = prep(an, cat), prep(bn, cat)
                    tag = f"{an}+{bn}"
                    ser, t_ser = {}, []
                    for nm, v in (("a", va), ("b", vb)):
                        t0 = time.perf_counter()
                        eng.load(v)
                        ys = [eng.render(dry) for _ in range(2)]
                        t_ser.append(time.perf_counter() - t0)
                        ser[nm] = ys
                    passes0 = eng.two_path_passes
                    dual, t_dual = [], []
                    for _ in range(2):
                        t0 = time.perf_counter()
                        dual.append(render_many(eng, dry, [va, vb]))
                        t_dual.append(time.perf_counter() - t0)
                    for k, ys in ser.items():
                        for i, y in enumerate(ys):
                            sf.write(run_dir / f"s1_{tag}_serial_{k}{i}.wav", y, reamp.SR, subtype="FLOAT")
                    for i, ys in enumerate(dual):
                        for k, y in zip("ab", ys, strict=True):
                            sf.write(run_dir / f"s1_{tag}_dual_{k}{i}.wav", y, reamp.SR, subtype="FLOAT")
                    out[tag] = {
                        "two_path_passes": eng.two_path_passes - passes0,
                        "skips": eng.two_path_skips[-2:],
                        "readback": eng.last_readback.get("ok"),
                        "null_dual_vs_serial_a_db": [null_db(d[0], ser["a"][0]) for d in dual],
                        "null_dual_vs_serial_b_db": [null_db(d[1], ser["b"][0]) for d in dual],
                        "repeat_serial_a_db": null_db(ser["a"][1], ser["a"][0]),
                        "repeat_serial_b_db": null_db(ser["b"][1], ser["b"][0]),
                        "repeat_dual_a_db": null_db(dual[1][0], dual[0][0]),
                        "repeat_dual_b_db": null_db(dual[1][1], dual[0][1]),
                        "rms_db": {"serial_a": round(reamp.rms_db(ser["a"][0]), 2),
                                   "serial_b": round(reamp.rms_db(ser["b"][0]), 2),
                                   "dual_a": round(reamp.rms_db(dual[0][0]), 2),
                                   "dual_b": round(reamp.rms_db(dual[0][1]), 2)},
                        # serial: load + 2 renders per variant; the per-variant cost of one render is
                        # the load + one render, so compare one dual pass with two (load + render)
                        "serial_load_plus_2_renders_s": [round(t, 2) for t in t_ser],
                        "dual_pass_s": [round(t, 2) for t in t_dual],
                        "dual_whole_write_s": [round(w["load_s"], 3) for w in eng.whole_writes[-2:]],
                    }
                return out
            step("s1", s1_control if control_only else s1)

            def s2():
                t0 = time.perf_counter()
                eng.load(prep("K", cat))
                t_k = time.perf_counter() - t0
                n0 = len(eng.whole_writes)
                L = with_delay("K", cat)
                t0 = time.perf_counter()
                eng.load(L)
                t_l = time.perf_counter() - t0
                y = None
                if not control_only:
                    y = eng.render(dry)
                    sf.write(run_dir / "s2_layout.wav", y, reamp.SR, subtype="FLOAT")
                w = eng.whole_writes[-1] if len(eng.whole_writes) > n0 else {}
                return {"load_K_s": round(t_k, 3), "layout_load_s": round(t_l, 3),
                        "whole_writes_for_layout_load": len(eng.whole_writes) - n0,
                        "readback_ok": eng.last_readback.get("ok"), "readback_checked": eng.last_readback.get("checked"),
                        "readback_mismatches": eng.last_readback.get("mismatches"),
                        "readback_missing": eng.last_readback.get("missing"),
                        "scale_note": [u for u in eng.last_readback.get("unspecified", []) if "Scale" in u],
                        "compile_report_changed": (w.get("report") or {}).get("changed"),
                        "slot_map": {str(k): v for k, v in eng.slot_map.items()},
                        "rms_db": None if y is None else round(reamp.rms_db(y), 2),
                        "pre_roll_s": round(eng.last_wait_s, 2)}
            step("s2", s2)

            def s4_delay():
                after_whole = delay_values(eng.chain)
                slot = next(i for i, s in enumerate(eng.preset.slots) if s.model_id == "HD2_DelaySimpleDelay")
                eng.set_params({(slot, "Mix"): 0.6, (slot, "Time"): 0.1})    # 0..1: Mix 0.6, Time 0.8 s
                at_once = delay_values(eng.bridge.call("chain"))
                time.sleep(3.0)
                later = delay_values(eng.bridge.call("chain"))
                want = {"Time": 0.8, "Mix": 0.6}
                ok = all(abs(v["values"].get(k, -1) - w) < 1e-4 for v in (at_once, later) for k, w in want.items())
                ok_whole = all(abs(after_whole["values"].get(k, -1) - w) < 1e-4 for k, w in (("Time", 0.523), ("Mix", 0.37)))
                return {"after_whole_write": after_whole, "after_set_at_once": at_once, "after_set_3s": later,
                        "whole_mix_time_ok": ok_whole, "in_place_mix_time_ok": ok}
            step("s4_delay", s4_delay)

            def s5():
                drives = (0.40, 0.55, 0.70)
                inplace, whole = [], []
                for dv in drives:
                    p = prep("K", cat)
                    p.slots[0].params["Drive"] = dv
                    t0 = time.perf_counter()
                    eng.load(p)
                    inplace.append((round(time.perf_counter() - t0, 3), eng.last_readback.get("ok")))
                mode = eng.whole_preset
                eng.whole_preset = "always"
                try:
                    for dv in drives:
                        p = prep("K", cat)
                        p.slots[0].params["Drive"] = dv + 0.05
                        t0 = time.perf_counter()
                        eng.load(p)
                        whole.append((round(time.perf_counter() - t0, 3), eng.last_readback.get("ok")))
                finally:
                    eng.whole_preset = mode
                return {"param_only_in_place_s_ok": inplace, "param_only_whole_s_ok": whole,
                        "whole_split_s": [{k: round(w[k], 3) for k in ("backup_s", "compile_s", "write_s", "load_s")}
                                          for w in eng.whole_writes[-3:]]}
            step("s5", s5)
        except SystemExit as e:
            res["errors"].append(str(e))
        finally:
            eng.close(verify_bytes=True)
            res["restore_report"] = eng.restore_report
            res["bytes_equal_backup"] = bool(eng.restore_report.get("bytes_equal_backup"))
            res["session_close"] = eng.bridge.release_report
            res["whole_writes"] = len(eng.whole_writes)
            res["two_path_passes"] = eng.two_path_passes
    return 1 if res["errors"] or not res["bytes_equal_backup"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
