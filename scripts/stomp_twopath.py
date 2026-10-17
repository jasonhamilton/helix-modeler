#!/usr/bin/env python3
"""Two-path (Path A + Path B) render gap on the HX Stomp, fw 3.80 (T063, stomp-engine). Edit buffer only.

Reference: an HX Edit two-path preset already stored on the pedal (setlist 0, index 32, 11C
"DIR:ANGL Meteor": Y split, Cab 4x12 XXL V30 on Path A and on Path B, split before cell 4, join before
cell 6, Balance/Pan 0.5/0.5). Its .hlx comes from the human's setlist export
(fixtures/golden/HXStompSetlist.hls, local only). "Ours" is the same .hlx read and written by our Python
side (Preset round trip + reamp_hlx_dict) and compiled onto the scratch slot's (04C) edit buffer by
stompd-compile: once with the build before the fix (stompd-compile.pre-sabj) and once with the fixed one.

  prep   offline: write cache/stomp/twopath/{ref_hxedit,ours,dual}.hlx, compile ours with both builds onto
         an 04C backup, decode, print the junctions and the document check; single-path byte identity of
         old vs new compiler over every earlier .hlx (fw380, hw/presets, inject)
  run    hardware, one session: start from whatever the pedal holds, select 04C (scratch slot) as the base;
         reference = select 11C (load from flash, no write), amp Hum = Ripple = 0 and input on USB 5/6 in
         the edit buffer, backup, render; ours before the fix (old build) and after (new build): whole write
         of the compiled document, document readback (backup + decode, attach points included), render;
         single path: K0 compiled by both builds (bytes compared), written and rendered; dual check: K0 on
         Path A and B00 on Path B with independent Path B input/output and HX Edit junction values, against
         K0 and B00 alone; restore the original preset (StompEngine.close, readback + byte check) and print
         the session-close report. Results in cache/stomp/hw/<stamp>-twopath/summary.json.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch import catalog as C
from tonematch import preset as P
from tonematch.engines.stomp import (
    compile_document,
    decode_document,
    reamp_hlx_dict,
    restore_verdict,
)
from tonematch.engines.stomp_layout import HX_EDIT_JOIN, HX_EDIT_SPLIT_Y
from tonematch.engines.stomp_readback import readback_diff

STOMP = ROOT / "cache" / "stomp"
OUT = STOMP / "twopath"
BIN = STOMP / "bin"
OLD = BIN / "stompd-compile.pre-sabj"
NEW = BIN / "stompd-compile"
HLS = ROOT / "fixtures" / "golden" / "HXStompSetlist.hls"
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
REF = (0, 32, "DIR:ANGL Meteor")
SCRATCH = (0, 11, "Smoke Known")
USB56 = 6
QUIET = {"Hum": 0.0, "Ripple": 0.0}   # free-running amp noise (T056): off on both sides
DATASET = "stomp-two-path"
PROCESS = "HX Edit.app/Contents/MacOS/HX Edit"


def base_backup() -> Path:
    """The newest 04C edit-buffer backup taken at the open of a fw 3.80 session (offline base)."""
    cands = sorted((STOMP / "hw").glob("*-speed-batch/*_editbuffer.bin"))
    if not cands:
        return STOMP / "live" / "base04C.bin"
    return min(cands[-1].parent.glob("*_editbuffer.bin"))


def wrap(p: dict) -> dict:
    return {"data": p, "meta": {"original": 0, "pbn": 0, "premium": 0}, "schema": "L6Preset", "version": 6}


def ref_hlx() -> dict:
    from tonematch.engines.native_setlist import decode_hls

    p = decode_hls(HLS)["presets"][REF[1]]
    name = p.get("meta", {}).get("name")
    if name != REF[2]:
        raise SystemExit(f"setlist index {REF[1]} is {name!r}, not {REF[2]!r}")
    return wrap(p)


def ours_hlx(ref_path: Path, cat: dict) -> dict:
    """The reference read and written by our Python side, with the amp's Hum and Ripple at 0."""
    pre = P.read_hlx(ref_path, cat)
    for s in pre.slots:
        if s.category == "amp":
            s.params.update(QUIET)
    return reamp_hlx_dict(pre, USB56)


def make_dual(path_a: Path, path_b: Path) -> dict:
    """K0 on Path A and B00's Amp+Cab on Path B, topology SAB: Path B fed by the split, with its own
    output (USB 3/4: value 6), for two renders per pass; split/join with HX Edit's values across the row."""
    a, b = json.loads(path_a.read_text()), json.loads(path_b.read_text())
    d = copy.deepcopy(a)
    t, bt = d["data"]["tone"]["dsp0"], b["data"]["tone"]["dsp0"]
    key = next(k for k, v in bt.items() if k.startswith("block") and v.get("@cab"))
    amp, cab = copy.deepcopy(bt[key]), copy.deepcopy(bt[bt[key]["@cab"]])
    amp.update({"@path": 1, "@position": 0, "@cab": "cab9", "@enabled": True})
    t["block19"], t["cab9"] = amp, cab
    t["inputA"]["@input"] = USB56
    t["inputB"]["@input"] = 0      # fed by the split (the device turns an own input B into this anyway)
    t["outputB"]["@output"] = 6
    d["data"]["tone"]["global"]["@topology0"] = "SAB"
    t["split"].update({"@model": "HD2_AppDSPFlowSplitY", "@position": 0, "@enabled": True, **HX_EDIT_SPLIT_Y})
    t["join"].update({"@model": "HD2_AppDSPFlowJoin", "@position": 8, "@enabled": True, **HX_EDIT_JOIN})
    d["data"]["meta"]["name"] = "dual sabj-values"
    return d


def write(path: Path, d: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(P.dumps_hlx(d), encoding="utf-8")
    return path


def junctions(chain: dict) -> list[dict]:
    keep = ("pos", "kind", "model_id", "enabled", "attach", "b_routing")
    out = []
    for s in chain["slots"]:
        if s["kind"] in ("Split", "Join"):
            e = {k: s.get(k) for k in keep}
            e["params"] = {(p.get("sym") or p.get("id")): round(float(p["value"]), 4) for p in s.get("params", [])}
            out.append(e)
    return out


def blocks(chain: dict) -> list[tuple]:
    return [(s["pos"], s.get("model_id"), s.get("paired_id"), s.get("enabled")) for s in chain["slots"]
            if s["kind"] == "Block"]


# ----------------------------------------------------------------------------- prep (offline)


def cmd_prep(a) -> int:
    cat = C.load_catalog("current")
    base = Path(a.base) if a.base else base_backup()
    OUT.mkdir(parents=True, exist_ok=True)
    ref_p = write(OUT / "ref_hxedit.hlx", ref_hlx())
    ours = ours_hlx(ref_p, cat)
    ours_p = write(OUT / "ours.hlx", ours)
    print("base", base)
    res = {}
    for tag, exe in (("old", OLD), ("new", NEW)):
        out = OUT / f"ours_{tag}.bin"
        rep = compile_document(ours_p, base, out, compiler=exe)
        ch = decode_document(out, compiler=NEW)   # the new decode also shows the attach points
        d = readback_diff(ch, ours, rep)
        res[tag] = {"topology": rep.get("topology"), "junctions": junctions(ch), "blocks": blocks(ch),
                    "document_check_ok": d.ok, "mismatches": d.mismatches[:8], "warnings": rep.get("warnings")}
        print(tag, json.dumps(res[tag], indent=1))
    # Single-path byte identity, old vs new build, over every earlier .hlx.
    files = sorted({*(STOMP / "fw380").glob("*.hlx"), *(STOMP / "hw" / "presets").glob("*.hlx"),
                    *(STOMP / "inject").glob("*.hlx")})
    same, diff, both_refused, two = [], [], [], []
    for f in files:
        d = json.loads(f.read_text())
        if any(isinstance(v, dict) and v.get("@path") == 1 for v in d["data"]["tone"].get("dsp0", {}).values()):
            two.append(f.name)
            continue
        outs = []
        for exe in (OLD, NEW):
            o = OUT / "single" / f"{f.stem}_{exe.name}.bin"
            o.parent.mkdir(parents=True, exist_ok=True)
            try:
                compile_document(f, base, o, compiler=exe)
                outs.append(o.read_bytes())
            except Exception as e:  # noqa: BLE001 - a refusal must be the same on both builds
                outs.append(f"refused: {e}")
        if isinstance(outs[0], str) and isinstance(outs[1], str):
            both_refused.append(f.name)
        elif outs[0] == outs[1]:
            same.append(f.name)
        else:
            diff.append(f.name)
    print(json.dumps({"single_path_identical": len(same), "different": diff, "both_refused": both_refused,
                      "two_path_skipped": two}, indent=1))
    dual = write(OUT / "dual.hlx", make_dual(STOMP / "hw" / "presets" / "K0.hlx", STOMP / "hw" / "presets" / "B00.hlx"))
    rep = compile_document(dual, base, OUT / "dual_new.bin", compiler=NEW)
    print("dual", rep.get("topology"), json.dumps(junctions(decode_document(OUT / "dual_new.bin", compiler=NEW))))
    return 1 if diff else 0


# ----------------------------------------------------------------------------- run (hardware)


def cmd_run(a) -> int:
    import soundfile as sf
    from stomp_hw import aligned_null

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine

    cat = C.load_catalog("current")
    run_dir = STOMP / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-twopath"
    docs = run_dir / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    dry, _ = sf.read(DI, dtype="float32")
    n = len(dry)
    reps = a.repeats
    res: dict = {"ref": {"setlist": REF[0], "index": REF[1], "name": REF[2]}, "errors": [], "renders": 0}
    renders: dict[str, list[np.ndarray]] = {}
    t_start = time.perf_counter()
    eng = StompEngine(whole_preset="always", session_dir=run_dir)   # health check, backup of the original
    lat = eng.cal.latency_samples

    def cap(tag: str, k: int = reps) -> None:
        """k renders: USB 1/2 (and 3/4) mono, trimmed by the calibrated round trip."""
        eng._refresh()
        for i in range(k):
            rec, pre = reamp.playrec(dry, eng.device, pre_s=eng.pre_roll_s(), tail_s=lat / 48000 + 0.05, in_pair=None)
            seg = rec[pre + lat: pre + lat + n]
            renders.setdefault(tag, []).append(reamp.to_mono(seg[:, 0:2], eng.cal.mono))
            if seg.shape[1] >= 4:
                renders.setdefault(tag + "@usb34", []).append(reamp.to_mono(seg[:, 2:4], eng.cal.mono))
            sf.write(run_dir / f"{tag}_{i}.wav", renders[tag][-1], 48000, subtype="FLOAT")
            res["renders"] += 1
        print("rendered", tag, round(reamp.rms_db(renders[tag][-1]), 2), "dBFS", flush=True)

    def to(setlist: int, index: int, name: str) -> dict:
        eng.bridge.call("select", setlist=setlist, index=index, timeout_s=20)
        time.sleep(0.5)
        info = eng.bridge.call("info")
        if info.get("name") != name:
            raise SystemExit(f"after select the pedal shows {info.get('name')!r}, not {name!r}: stopping")
        eng._refresh()
        return {k: info.get(k) for k in ("setlist", "index", "label", "name")}

    def write_doc(tag: str, hlx: dict, exe: Path, base: Path) -> dict:
        """Compile, write whole into the edit buffer, read the document back and check it."""
        h = write(docs / f"{tag}.hlx", hlx)
        out = docs / f"{tag}.bin"
        rep = compile_document(h, base, out, compiler=exe)
        t0 = time.perf_counter()
        eng.bridge.call("restore", path=str(out), timeout_s=30)
        w = time.perf_counter() - t0
        eng._refresh()
        eng._route_input(force=True)
        back = docs / f"{tag}_readback.bin"
        eng.bridge.call("backup", path=str(back))
        ch = decode_document(back, compiler=NEW)
        d = readback_diff(ch, hlx, rep)
        live = readback_diff(eng.chain, hlx, rep)
        r = {"compiler": exe.name, "topology": rep.get("topology"), "write_s": round(w, 3),
             "document_readback_ok": d.ok, "live_readback_ok": live.ok,
             "mismatches": (d.mismatches + live.mismatches)[:10], "junctions": junctions(ch), "blocks": blocks(ch),
             "warnings": rep.get("warnings"), "changed": rep.get("changed")}
        print("wrote", tag, json.dumps({k: r[k] for k in ("compiler", "topology", "write_s", "document_readback_ok",
                                                         "live_readback_ok", "mismatches")}), flush=True)
        return r

    try:
        res["original"] = eng.original
        print("original", json.dumps(eng.original), flush=True)
        # Base: the scratch slot (04C), loaded from flash.
        res["scratch"] = to(*SCRATCH)
        base = docs / "base04C.bin"
        eng.bridge.call("backup", path=str(base))

        # 1. Reference: the HX Edit preset as stored, amp Hum/Ripple 0 and input on USB 5/6 (edit buffer).
        res["reference"] = to(*REF)
        eng.bridge.call("backup", path=str(docs / "ref_as_stored.bin"))
        amp = next(s for s in eng.chain["slots"] if s["kind"] == "Block" and s.get("model_id") == "HD2_AmpANGLMeteor")
        for p in amp["params"]:
            if (p.get("sym") or p.get("id")) in QUIET:
                eng.bridge.call("set", pos=amp["pos"], param=p["i"], value=QUIET[p.get("sym") or p.get("id")],
                                model=int(amp["model"]))
        eng._refresh()
        eng._route_input(force=True)
        eng.bridge.call("backup", path=str(docs / "ref_rendered.bin"))
        res["reference"]["junctions"] = junctions(decode_document(docs / "ref_rendered.bin", compiler=NEW))
        res["reference"]["blocks"] = blocks(eng.chain)
        cap("ref")

        # 2. Ours, before and after the fix, each written onto the 04C base.
        cat_ours = ours_hlx(write(docs / "ref_hxedit.hlx", ref_hlx()), cat)
        to(*SCRATCH)
        res["ours_before"] = write_doc("ours_before", cat_ours, OLD, base)
        cap("ours_before")
        to(*SCRATCH)
        res["ours_after"] = write_doc("ours_after", cat_ours, NEW, base)
        cap("ours_after")

        # 3. Single path: K0 by both builds.
        k0 = json.loads((STOMP / "hw" / "presets" / "K0.hlx").read_text())
        k0["data"]["tone"]["dsp0"]["inputA"]["@input"] = USB56
        to(*SCRATCH)
        res["single_old"] = write_doc("single_old", k0, OLD, base)
        cap("single_old", 2)
        to(*SCRATCH)
        res["single_new"] = write_doc("single_new", k0, NEW, base)
        cap("single_new", 2)
        res["single_bytes_equal"] = (docs / "single_old.bin").read_bytes() == (docs / "single_new.bin").read_bytes()

        # 4. Dual (independent Path B input/output, HX Edit junction values) vs K0 / B00 alone.
        if not a.no_dual:
            b00 = json.loads((STOMP / "hw" / "presets" / "B00.hlx").read_text())
            b00["data"]["tone"]["dsp0"]["inputA"]["@input"] = USB56
            to(*SCRATCH)
            res["single_b00"] = write_doc("single_b00", b00, NEW, base)
            cap("single_b00", 2)
            to(*SCRATCH)
            res["dual"] = write_doc("dual", make_dual(STOMP / "hw" / "presets" / "K0.hlx",
                                                      STOMP / "hw" / "presets" / "B00.hlx"), NEW, base)
            cap("dual", 2)
        to(*SCRATCH)
    except SystemExit as e:
        res["errors"].append(str(e))
    except Exception as e:  # noqa: BLE001 - always restore
        res["errors"].append(f"{type(e).__name__}: {e}")
        print("ERROR", e, flush=True)
    finally:
        eng.close(verify_bytes=True)
        res["restore_report"] = eng.restore_report
        res["session_close"] = eng.bridge.release_report
        res["usb_reset"] = eng.bridge.reset_report
        res["wall_clock_s"] = round(time.perf_counter() - t_start, 1)

    def pairs(x: str, y: str) -> list[float]:
        xs, ys = renders.get(x, []), renders.get(y, [])
        if x == y:
            return [aligned_null(xs[i], xs[j])["null_db"] for i in range(len(xs)) for j in range(i + 1, len(xs))]
        return [aligned_null(p, q)["null_db"] for p in xs for q in ys]

    def med(v: list[float]) -> float | None:
        return float(np.median(v)) if v else None

    nulls = {}
    for name, x, y in (("ref_repeat", "ref", "ref"), ("before_vs_ref", "ours_before", "ref"),
                       ("after_vs_ref", "ours_after", "ref"), ("after_repeat", "ours_after", "ours_after"),
                       ("before_repeat", "ours_before", "ours_before"),
                       ("single_old_vs_new", "single_old", "single_new"), ("single_repeat", "single_new", "single_new"),
                       ("dual_A_vs_single", "dual", "single_new"), ("dual_B_vs_single", "dual@usb34", "single_b00"),
                       ("dual_A_repeat", "dual", "dual"), ("single_b00_repeat", "single_b00", "single_b00")):
        if renders.get(x) and renders.get(y):
            v = pairs(x, y)
            nulls[name] = {"median_db": med(v), "all_db": v}
    res["nulls"] = nulls
    res["levels_dbfs"] = {k: round(float(np.median([reamp.rms_db(r) for r in v])), 2) for k, v in renders.items()}
    (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"nulls": {k: v["median_db"] for k, v in nulls.items()}, "levels": res["levels_dbfs"],
                      "single_bytes_equal": res.get("single_bytes_equal"), "restore": res["restore_report"],
                      "session_close": res["session_close"], "errors": res["errors"]}, indent=1, default=str))
    print("summary:", run_dir / "summary.json")
    ok = not res["errors"] and restore_verdict(res["restore_report"])   # decoded equality (T097)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--base", default=None, help="an 04C edit-buffer backup (default: newest fw 3.80 one)")
    r = sub.add_parser("run")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--no-dual", action="store_true")
    a = ap.parse_args(argv)
    for exe in (OLD, NEW):
        if not exe.exists():
            sys.exit(f"missing {exe}")
    if a.cmd == "run":
        if subprocess.run(["pgrep", "-f", PROCESS], capture_output=True, check=False).stdout.strip():
            sys.exit("HX Edit is running: quit it first (it holds the control interface)")
        if not os.environ.get("TONEMATCH_STOMPD") and not shutil.which("stompd"):
            sys.exit("set TONEMATCH_STOMPD to the USB helper (cache/stomp/bin/stompd)")
    return {"prep": cmd_prep, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
