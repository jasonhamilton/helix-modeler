#!/usr/bin/env python3
"""HX Stomp firmware 3.80 re-checks (stomp-engine, T059 follow-up, human GO 2026-10-15). Edit buffer only.

  gen    offline: write cache/stomp/fw380/<name>.hlx with device_version 3.80 (0x3800000):
         K, A, B0 (from cache/stomp/inject), the Blue Bell Early Reflections sweep er000 / er050 / er100
         (the DS-Sample chain: er000 = best_snapshot1, ER 0), and N1..N3 built from K with models that need
         firmware newer than 3.50 (catalog stomp_min_firmware > 0x3500000); validate each for 3.80.
         File stems equal the Native template names, so parity_null.py finds their Native templates.
  check  offline: compile each file onto --base (an edit-buffer backup) and check every parameter (no USB)
  write  hardware: health check, must start on 04C; for each file a whole-document write with full
         readback, then one render of fixtures/smoke/di_2s.wav (rms, peak, non-silent); restore 04C
         and byte-diff the edit buffer against the backup taken at open; print the session-close reports.
         Results in cache/stomp/hw/<stamp>-fw380-write/summary.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch import catalog as C
from tonematch import preset as P

FW_380 = 0x3800000
FW_350 = 0x3500000
OUT = ROOT / "cache" / "stomp" / "fw380"
INJECT = ROOT / "cache" / "stomp" / "inject"
ER_DIR = ROOT / "cache" / "matches" / "DS-Sample_run4_ER-free" / "parity" / "bisect"
DI = ROOT / "fixtures" / "smoke" / "di_2s.wav"
ORIGINAL_NAME = "Smoke Known"
USB56 = 6

# Models that need firmware newer than 3.50, on K's Amp+Cab slot (the only slot of 04C that can
# change model: the delay carries controller assignments, which the compiler refuses to rebuild).
NEW_MODELS = {
    "N1": {"amp": "HD2_AmpGermanXtraRed", "cab": None},                      # 3.79 amp, K's Blue Bell
    "N2": {"amp": None, "cab": "HD2_CabMicIr_4x12CartogGuv"},                # 3.79 cab (IR-style cab)
    "N3": {"amp": "HD2_AmpBrit2203", "cab": "HD2_CabMicIr_4x12Greenback30"},  # 3.69 amp and cab
}


# One block per category in the delay slot of 04C/K (slot 3), whose controller (CC 110) and
# footswitch assignments the compiler must drop to change its model (assignment-free base).
CATEGORY_MODELS = {
    "Cdist": "HD2_DM4Screamer",      # Distortion
    "Cdly": "HD2_DL4DigDelay",       # Delay (another model)
    "Crev": "HD2_ReverbRoom",        # Reverb
    "Cdyn": "HD2_DM4RedComp",        # Dynamics
    "Ceq": "HD2_EQSimple3Band",      # EQ
    "Cmod": "HD2_MM4OptoTremolo",    # Modulation
}


def _defaults(cat: dict, model_id: str) -> dict:
    return {k: v["default"] for k, v in cat["models"][model_id]["params"].items() if not k.startswith("@")}


def min_fw(cat: dict, model_id: str) -> int:
    m = cat["models"][model_id].get("stomp_min_firmware")
    return int(m, 16) if m else 0


def as_380(src: Path) -> dict:
    d = json.loads(src.read_text())
    d["data"]["device_version"] = FW_380
    d["data"]["tone"]["dsp0"]["inputA"]["@input"] = USB56
    return d


def new_model_variant(cat: dict, k_path: Path, amp: str | None, cab: str | None, name: str) -> P.Preset:
    p = P.read_hlx(k_path, cat)
    p.name = f"fw380 {name}"
    ai = next(i for i, s in enumerate(p.slots) if s.category == "amp")
    ci = next(i for i, s in enumerate(p.slots) if s.category == "cab")
    if amp:
        p.slots[ai].model_id, p.slots[ai].params = amp, _defaults(cat, amp)
        p.slots[ai].params.update({k: 0.0 for k in ("Hum", "Ripple") if k in p.slots[ai].params})
    if cab:
        p.slots[ci].model_id, p.slots[ci].params = cab, _defaults(cat, cab)
        p.slots[ci].extra.pop("@mic", None)   # IR-style cabs carry Mic as a parameter
    return p


def category_variant(cat: dict, k_path: Path, model: str, name: str) -> dict:
    """K with the delay slot's block replaced by `model` (enabled, catalog defaults) and no
    controller or footswitch assignment on that block (the compiler drops the base's)."""
    p = P.read_hlx(k_path, cat)
    p.name = f"fw380 {name}"
    di = next(i for i, s in enumerate(p.slots) if s.category == "delay")
    key = p.slots[di].key
    p.slots[di].model_id, p.slots[di].params = model, _defaults(cat, model)
    p.slots[di].enabled = True
    p.slots[di].extra = {k: v for k, v in p.slots[di].extra.items() if k in ("@path", "@position")}
    for sn in p.snapshots:
        sn.enabled[key] = True
    d = p.to_hlx_dict()
    tone = d["data"]["tone"]
    for sec in ("controller", "footswitch"):
        tone.get(sec, {}).get("dsp0", {}).pop(key, None)
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if isinstance(sn, dict):
            sn.get("controllers", {}).get("dsp0", {}).pop(key, None)
    d["data"]["device_version"] = FW_380
    tone["dsp0"]["inputA"]["@input"] = USB56
    return d


def files() -> dict[str, Path]:
    return {n: OUT / f"{n}.hlx" for n in ("K", "A", "B0", "er000", "er050", "er100", *NEW_MODELS, *CATEGORY_MODELS)}


def cmd_gen(_a) -> int:
    cat = C.load_catalog("current")
    OUT.mkdir(parents=True, exist_ok=True)
    src = {"K": INJECT / "K.hlx", "A": INJECT / "A.hlx", "B0": INJECT / "B0.hlx",
           "er000": ROOT / "cache" / "matches" / "DS-Sample_run4_ER-free" / "best_snapshot1.hlx",
           "er050": ER_DIR / "er050.hlx", "er100": ER_DIR / "er100.hlx"}
    if not src["er000"].exists():
        src["er000"] = ER_DIR / "er050.hlx"
    for name, f in src.items():
        d = as_380(f)
        if name == "er000":
            cab = next(v for v in d["data"]["tone"]["dsp0"].values()
                       if isinstance(v, dict) and str(v.get("@model", "")).startswith("HD2_Cab1x12BlueBell"))
            cab["EarlyReflections"] = 0.0
        (OUT / f"{name}.hlx").write_text(P.dumps_hlx(d), encoding="utf-8")
    for name, m in NEW_MODELS.items():
        p = new_model_variant(cat, INJECT / "K.hlx", m["amp"], m["cab"], name)
        d = p.to_hlx_dict()
        d["data"]["device_version"] = FW_380
        d["data"]["tone"]["dsp0"]["inputA"]["@input"] = USB56
        (OUT / f"{name}.hlx").write_text(P.dumps_hlx(d), encoding="utf-8")
        for mid in filter(None, (m["amp"], m["cab"])):
            assert FW_350 < min_fw(cat, mid) <= FW_380, (mid, hex(min_fw(cat, mid)))
    for name, model in CATEGORY_MODELS.items():
        (OUT / f"{name}.hlx").write_text(P.dumps_hlx(category_variant(cat, INJECT / "K.hlx", model, name)),
                                         encoding="utf-8")
    rc = 0
    for name, f in files().items():
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(f), "--firmware", hex(FW_380)],
                           capture_output=True, text=True, check=False)
        last = (r.stdout.strip().splitlines() or [r.stderr.strip()])[-1]
        print(f"{name}: validate exit {r.returncode}: {last}")
        rc = max(rc, r.returncode)
    return rc


def cmd_check(a) -> int:
    import stomp_inject as SI

    rc = 0
    for name in a.presets:
        r = SI.document_check(files()[name], Path(a.base))
        print(name, "OK" if r["ok"] else "MISMATCH", f"({r['checked']} checks)", r["slots"],
              (r["mismatches"] + r["missing"])[:5])
        rc = max(rc, 0 if r["ok"] else 1)
    return rc


def cmd_write(a) -> int:
    import numpy as np
    import soundfile as sf

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine
    cat = C.load_catalog("current")
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-fw380-write"
    run_dir.mkdir(parents=True, exist_ok=True)
    dry, _ = sf.read(DI, dtype="float32")
    res: dict = {"presets": a.presets, "results": {}, "errors": []}
    eng = StompEngine(whole_preset="always", session_dir=run_dir)   # health check runs on open
    try:
        res["info"] = eng.info
        if eng.original.get("name") != ORIGINAL_NAME:
            raise SystemExit(f"the pedal is on {eng.original}, not 04C '{ORIGINAL_NAME}': nothing written")
        eng.keep_documents = run_dir / "documents"
        for name in a.presets:
            r: dict = {}
            res["results"][name] = r
            try:
                p = P.read_hlx(files()[name], cat)
                r["models"] = [s.model_id for s in p.blocks()]
                r["min_fw"] = {m: hex(min_fw(cat, m)) for m in r["models"] if min_fw(cat, m)}
                eng.load(p)
                w = eng.whole_writes[-1]
                r.update(write_s=round(w["write_s"], 3), load_s=round(w["load_s"], 3),
                         document_ok=w["checks"].get("document", {}).get("ok"),
                         readback_ok=w["checks"]["readback"].get("ok"),
                         readback_checked=w["checks"]["readback"].get("checked"))
                y = eng.render(dry)
                sf.write(run_dir / f"{name}.wav", y, reamp.SR, subtype="FLOAT")
                r.update(rms_dbfs=round(reamp.rms_db(y), 2),
                         peak_dbfs=round(20 * np.log10(max(float(np.max(np.abs(y))), 1e-12)), 2),
                         silent=bool(reamp.rms_db(y) < -80))
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 - one preset must not stop the restore
                r["error"] = f"{type(e).__name__}: {e}"
                if type(e).__name__ == "ReadbackMismatch":
                    r["readback"] = getattr(e, "diff", None) and e.diff.as_dict()
                res["errors"].append(f"{name}: {r['error']}")
                if type(e).__name__ in ("BridgeError", "PowerCycleNeeded"):
                    break
            print(name, json.dumps(r, default=str), flush=True)
    finally:
        eng.close(verify_bytes=True)   # restore byte-check in the same session (T061 item 1)
        res["restore_report"] = eng.restore_report
        res["session_close"] = eng.bridge.release_report
        res["usb_reset"] = eng.bridge.reset_report
        res["bytes_equal_backup"] = bool(eng.restore_report.get("bytes_equal_backup"))
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps({k: res.get(k) for k in ("restore_report", "session_close", "usb_reset",
                                                    "bytes_equal_backup", "errors")}, default=str))
        print("summary:", run_dir / "summary.json")
    return 1 if res["errors"] or not res.get("bytes_equal_backup") else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--presets", nargs="+", default=list(files()))
    w = sub.add_parser("write")
    w.add_argument("--presets", nargs="+", default=["K", "A", *NEW_MODELS])
    a = ap.parse_args(argv)
    if a.cmd == "write":
        if subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True, check=False).stdout.strip():
            sys.exit("HX Edit is running: quit it first (it holds the control interface)")
        for var in ("TONEMATCH_STOMPD", "TONEMATCH_STOMPD_COMPILE"):
            if not os.environ.get(var):
                sys.exit(f"set {var}")
    return {"gen": cmd_gen, "check": cmd_check, "write": cmd_write}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
