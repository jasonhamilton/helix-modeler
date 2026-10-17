#!/usr/bin/env python3
"""Parity spot checks (T060, benchmark): at least 5 models per block category, each rendered on Helix
Native and on the HX Stomp from the same .hlx and the same DI, compared by null; delays also by echo
level and timing. Order: Distortion, Delay, Reverb, then Dynamics, EQ, Modulation; amps and cabs
topped up. Hum and Ripple 0 everywhere; cab EarlyReflections 0 (the Stomp policy) except in the ER sweep.

  gen      offline: write cache/parity/spot380/hlx/<name>.hlx (K with one block changed), validate them for
           firmware 3.80, cut the DIs, write manifest.json
  native   Native first (no pedal): NativeRenderPool, ensure_templates, one batch of renders
  stomp    pedal, ONE batched session: takes the shared pedal lock (tonematch.pedal_lock), whole-preset loads into the
           scratch slot, restores the original preset with readback and a byte diff, releases the lock
  analyse  nulls, echo metrics, ledger rows (kind=parity, dataset parity-spot-fw380)

Dual-path passes are not used (single-path whole-preset renders only).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import parity_null as PN
import parity_snap as SNAP
import stomp_fw380 as F

from tonematch import catalog as C
from tonematch import preset as P

SR = 48_000
OUT = ROOT / "cache" / "parity" / "spot380"
HLX = OUT / "hlx"
HLX_SNAPPED = OUT / "hlx_snapped"   # what the Stomp loads: Native's CC-grid values (parity method rule)
RAW = OUT / "raw"
SNAPPED_MARK = OUT / "snapped_stomp.json"
TEMPLATES = ROOT / "cache" / "native" / "templates_files" / "spot380"
DATASET = "parity-spot-fw380"
NULL_PASS_DB = -30.0
ECHO_LEVEL_TOL_DB = 2.0

SETS = {
    "Distortion": ["HD2_DistScream808", "HD2_DistKinkyBoost", "HD2_DM4HeavyDistortion", "HD2_DistTriangleFuzz",
                   "HD2_DistBitcrusher"],
    "Delay": ["HD2_DL4DigDelay", "HD2_DelaySimpleDelay", "HD2_DL4TapeEchoStereo", "HD2_DelayTransistorTape",
              "HD2_DelayAdriaticDelay"],
    "Reverb": ["HD2_ReverbRoom", "HD2_ReverbHall", "HD2_ReverbPlate", "HD2_Reverb63Spring", "HD2_ReverbChamber",
               "HD2_ReverbGanymede"],
    "Dynamics": ["HD2_CompressorLAStudioComp", "HD2_CompressorDeluxeComp", "HD2_CompressorRedSqueeze",
                 "HD2_DM4TubeComp", "HD2_CompressorKinkyComp", "HD2_GateNoiseGate"],
    "EQ": ["HD2_EQParametric", "HD2_EQGraphic10Band", "HD2_EQLowCutHighCut", "HD2_EQSimple3Band", "HD2_CaliQ"],
    "Modulation": ["HD2_Chorus70sChorus", "HD2_FlangerGrayFlanger", "HD2_PhaserDeluxePhaser",
                   "HD2_TremoloOpticalTrem", "HD2_VibratoBubbleVibrato", "HD2_MM4AnalogChorus"],
}
AMPS = ["HD2_AmpEssexA15", "HD2_AmpPlacaterDirty", "HD2_AmpUSDeluxeNrm", "HD2_AmpBritPlexiBrt",
        "HD2_AmpGermanXtraRed"]
CABS = ["HD2_Cab1x12BlueBell", "HD2_Cab1x12USDeluxe", "HD2_Cab4x12Greenback25", "HD2_Cab4x12UberV30",
        "HD2_CabMicIr_4x12CartogGuv"]
ER_VALUES = [0.0, 0.25, 0.5, 1.0]
# Helix-only parameters (the Stomp's block has no such parameter, so the readback refuses the file):
# first run on fw 3.80 refused Scale (Simple, Transistor, Adriatic), Spread and Stereo (modulation blocks).
# Those files omit the parameter; both engines then use their own default.
STOMP_LACKS = ("Scale", "Spread", "Stereo")
STRIP_FOR = ("Del_DelaySimpleDelay", "Del_DelayTransistorTape", "Del_DelayAdriaticDelay", "Mod_")
REPEAT_OF = ["Modulation:HD2_Chorus70sChorus", "Reverb:HD2_ReverbRoom"]   # rendered twice on the Stomp


def short(model: str) -> str:
    return model.replace("HD2_", "")


def defaults(cat: dict, mid: str) -> dict:
    return F._defaults(cat, mid)


def build(cat: dict, kind: str, category: str, model: str | None, er: float | None, name: str) -> dict:
    """K with one block changed. kind: block (K's delay slot), amp, cab, er."""
    p = P.read_hlx(F.INJECT / "K.hlx", cat)
    p.name = f"spot {name}"[:16]
    ai = next(i for i, s in enumerate(p.slots) if s.category == "amp")
    ci = next(i for i, s in enumerate(p.slots) if s.category == "cab")
    di = next(i for i, s in enumerate(p.slots) if s.category == "delay")
    key = p.slots[di].key
    if kind == "block":
        p.slots[di].model_id, p.slots[di].params = model, defaults(cat, model)
        p.slots[di].enabled = True
        p.slots[di].extra = {k: v for k, v in p.slots[di].extra.items() if k in ("@path", "@position")}
        prm = p.slots[di].params
        if any(name.startswith(x) for x in STRIP_FOR):
            for k in STOMP_LACKS:
                prm.pop(k, None)
        if category == "Delay":
            for k, v in (("TempoSync1", False), ("Feedback", 0.4), ("Mix", 0.4)):
                if k in prm:
                    prm[k] = v
            prm["Time"] = min(max(0.375, cat["models"][model]["params"]["Time"]["min"]), 2.0)
        if category == "EQ":
            rng = random.Random(f"eq-{model}")
            for k, pr in cat["models"][model]["params"].items():
                if k.startswith("@") or pr["kind"] != "continuous" or k.lower().startswith("level"):
                    continue
                lo, hi = pr["min"], pr["max"]
                prm[k] = round(lo + (hi - lo) * rng.uniform(0.2, 0.8), 3)
        for sn in p.snapshots:
            sn.enabled[key] = True
            sn.params.pop(key, None)
    elif kind == "amp":
        p.slots[ai].model_id, p.slots[ai].params = model, defaults(cat, model)
    elif kind == "cab":
        p.slots[ci].model_id, p.slots[ci].params = model, defaults(cat, model)
        if "@mic" in cat["models"][model]["params"]:
            p.slots[ci].extra["@mic"] = cat["models"][model]["params"]["@mic"]["default"]
        else:
            p.slots[ci].extra.pop("@mic", None)
    for k in ("Hum", "Ripple"):
        if k in p.slots[ai].params:
            p.slots[ai].params[k] = 0.0
    if "EarlyReflections" in p.slots[ci].params:
        p.slots[ci].params["EarlyReflections"] = 0.0 if er is None else er
    d = p.to_hlx_dict()
    tone = d["data"]["tone"]
    for sec in ("controller", "footswitch"):
        tone.get(sec, {}).get("dsp0", {}).pop(key, None)
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if isinstance(sn, dict):
            sn.get("controllers", {}).get("dsp0", {}).pop(key, None)
    d["data"]["device_version"] = F.FW_380
    tone["dsp0"]["inputA"]["@input"] = F.USB56
    return d


def items(cat: dict) -> list[dict]:
    out = []
    for category, models in SETS.items():
        for m in models:
            out.append({"name": f"{category[:3]}_{short(m)}", "kind": "block", "category": category, "model": m, "er": None})
    for m in AMPS:
        out.append({"name": f"Amp_{short(m)}", "kind": "amp", "category": "Amp", "model": m, "er": None})
    for m in CABS:
        out.append({"name": f"Cab_{short(m)}", "kind": "cab", "category": "Cab", "model": m, "er": None})
    for e in ER_VALUES:
        out.append({"name": f"ER_{int(e * 100):03d}", "kind": "er", "category": "ER", "model": "HD2_Cab1x12BlueBell", "er": e})
    for it in out:
        it["dis"] = ["guitar", "burst"] if it["category"] == "Delay" else ["guitar"]
    return out


def cmd_gen(_a) -> int:
    cat = C.load_catalog("current")
    bad = C.native_unavailable()
    HLX.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    its = items(cat)
    for it in its:
        assert it["model"] not in bad, it
        mf = F.min_fw(cat, it["model"])
        assert mf <= F.FW_380, it
        it["min_fw"] = hex(mf) if mf else None
        d = build(cat, it["kind"], it["category"], it["model"], it["er"], it["name"])
        it["hlx"] = str((HLX / f"{it['name']}.hlx").relative_to(ROOT))
        (ROOT / it["hlx"]).write_text(P.dumps_hlx(d), encoding="utf-8")
    rc = 0
    for it in its:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(ROOT / it["hlx"]), "--firmware",
                            hex(F.FW_380)], capture_output=True, text=True, check=False)
        it["validate_exit"] = r.returncode
        rc = max(rc, r.returncode)
        if r.returncode:
            print(it["name"], "validate", r.returncode, (r.stdout.strip().splitlines() or [""])[-1])
    # DIs: 4 s of the parity guitar clip + 2 s silence (delay and reverb tails); the burst DI for delays
    import soundfile as sf
    g = PN.load_di(PN.DI_DEFAULT)[: 4 * SR]
    g = np.concatenate([g, np.zeros(2 * SR, np.float32)])
    sf.write(str(OUT / "di_guitar.wav"), g, SR, subtype="FLOAT")
    b = PN.PARITY / "di_burst_tail.wav"
    (OUT / "di_burst.wav").write_bytes(b.read_bytes())
    (OUT / "manifest.json").write_text(json.dumps({"items": its, "repeat_of": REPEAT_OF}, indent=1))
    print(f"{len(its)} files written under {HLX}; validate failures: {sum(1 for i in its if i['validate_exit'])}")
    return rc


def manifest() -> dict:
    return json.loads((OUT / "manifest.json").read_text())


def dry_of(tag: str) -> np.ndarray:
    return PN.load_di(OUT / f"di_{tag}.wav")


def cmd_native(a) -> int:
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and PN.HOSTVENV.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(PN.HOSTVENV)
    from tonematch.engines.native import NativeRenderPool, RenderJob

    cat = C.load_catalog("current")
    mf = manifest()
    todo = [it for it in mf["items"] if not a.only or it["name"] in a.only or it["category"] in a.only]
    presets = {it["name"]: P.read_hlx(ROOT / it["hlx"], cat) for it in todo}
    TEMPLATES.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    pool = NativeRenderPool(workers=a.hosts, preroll=PN.PREROLL, templates_dir=TEMPLATES)
    summary = {"hosts": pool.workers, "errors": {}}
    try:
        t1 = time.perf_counter()
        errs = pool.ensure_templates(list(presets.values()), catalog=cat)
        names = list(presets)
        for i, e in errs.items():
            summary["errors"][names[i]] = f"capture {type(e).__name__}: {e}"
        t2 = time.perf_counter()
        jobs, tags = [], []
        for it in todo:
            if it["name"] in summary["errors"]:
                continue
            for d in it["dis"]:
                jobs.append(RenderJob(dry=dry_of(d), preset=presets[it["name"]], tag=(it["name"], d)))
                tags.append((it["name"], d))
        outs = pool.render_batch(jobs, on_error="none")
        for (name, d), y, in zip(tags, outs, strict=True):
            if y is None:
                summary["errors"][f"{name}:{d}"] = "render failed"
                continue
            PN.write_wav(RAW / f"{name}__{d}__native.wav", np.asarray(y, np.float32))
        t3 = time.perf_counter()
        summary.update(open_s=round(t1 - t0, 1), capture_s=round(t2 - t1, 1), render_s=round(t3 - t2, 1),
                       renders=len(jobs), stats=pool.stats(), firmware=getattr(pool.engines[0], "firmware_version", None))
    finally:
        pool.close()
    (OUT / "native_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, default=str))
    return 1 if summary["errors"] else 0


def hx_edit_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "HX Edit.app/Contents/MacOS/HX Edit"], capture_output=True, text=True, check=False)
    mine = {str(os.getpid()), str(os.getppid())}
    return bool([p for p in r.stdout.split() if p not in mine])


_HOLD = None   # this batch's hold on the shared pedal lock (tonematch.pedal_lock)


def take_lock(wait_s: float, poll_s: float = 60.0) -> bool:
    """Device check, then the shared pedal lock (owner.json, heartbeat, stale checks) for the whole batch.
    Raises pedal_lock.DeviceMissing / LockHeld (distinct exit codes) instead of returning False."""
    global _HOLD
    from tonematch import pedal_lock

    if _HOLD is None:
        pedal_lock.require_device()
        _HOLD = pedal_lock.PedalLock(f"{Path(sys.argv[0]).name} stomp batch", wait_s=wait_s, poll_s=poll_s).acquire()
    return True


def release_lock() -> None:
    global _HOLD
    if _HOLD is not None:
        _HOLD.release()
        _HOLD = None


def cmd_stomp(a) -> int:
    import soundfile as sf

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine, restore_verdict

    for var in ("TONEMATCH_STOMPD", "TONEMATCH_STOMPD_COMPILE"):
        if not os.environ.get(var):
            sys.exit(f"set {var}")
    if hx_edit_running():
        sys.exit("HX Edit is running: quit it first")
    cat = C.load_catalog("current")
    mf = manifest()
    todo = [it for it in mf["items"] if not a.only or it["name"] in a.only or it["category"] in a.only]
    # parity method rule: the Stomp plays the value Native's CC grid gives (needs the Native templates; offline, before the pedal)
    try:
        snap = SNAP.snap_dir([it["name"] for it in todo], HLX, HLX_SNAPPED, TEMPLATES, cat)
    except LookupError as e:
        sys.exit(f"cannot snap to Native's grid: {e}")
    print(f"snapped {sum(1 for v in snap.values() if v['moves'])} of {len(snap)} presets to Native's CC grid", flush=True)
    if not take_lock(a.lock_wait_s):
        sys.exit("pedal lock not obtained in time")
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-spot380"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"results": {}, "errors": []}
    eng = None
    try:
        eng = StompEngine(whole_preset="always", session_dir=run_dir)
        res["info"] = eng.info
        if eng.original.get("name") != F.ORIGINAL_NAME:
            raise SystemExit(f"the pedal is on {eng.original}, not 04C '{F.ORIGINAL_NAME}': nothing written")
        res["original"] = eng.original
        try:
            for it in todo:
                name = it["name"]
                r = res["results"].setdefault(name, {})
                try:
                    t0 = time.perf_counter()
                    eng.load(P.read_hlx(HLX_SNAPPED / f"{name}.hlx", cat))
                    w = eng.whole_writes[-1]
                    r.update(load_s=round(time.perf_counter() - t0, 2), readback_ok=w["checks"]["readback"].get("ok"),
                             document_ok=w["checks"].get("document", {}).get("ok"))
                    reps = 2 if f"{it['category']}:{it['model']}" in mf["repeat_of"] else 1
                    for d in it["dis"]:
                        for k in range(reps):
                            t1 = time.perf_counter()
                            y = np.asarray(eng.render(dry_of(d)), np.float32)
                            suffix = "" if k == 0 else "_rep"
                            sf.write(str(RAW / f"{name}__{d}__stomp{suffix}.wav"), y, reamp.SR, subtype="FLOAT")
                            r[f"render_s_{d}{suffix}"] = round(time.perf_counter() - t1, 2)
                            r[f"rms_db_{d}{suffix}"] = round(reamp.rms_db(y), 2)
                    marks = json.loads(SNAPPED_MARK.read_text()) if SNAPPED_MARK.exists() else {}
                    marks[name] = True        # parity_timefx.py reads this: these Stomp renders played Native's grid values
                    SNAPPED_MARK.write_text(json.dumps(marks, indent=1))
                    print(name, json.dumps(r), flush=True)
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001 - one model must not stop the restore
                    r["error"] = f"{type(e).__name__}: {e}"
                    res["errors"].append(f"{name}: {r['error']}")
                    print(name, "ERROR", r["error"], flush=True)
                    if type(e).__name__ in ("BridgeError", "PowerCycleNeeded"):
                        break
        finally:
            eng.close(verify_bytes=True)
            res["restore_report"] = eng.restore_report
            res["session_close"] = eng.bridge.release_report
            res["bytes_equal_backup"] = bool(eng.restore_report.get("bytes_equal_backup"))
            res["decoded_equal_backup"] = eng.restore_report.get("decoded_equal_backup")
            res["restore_ok"] = restore_verdict(eng.restore_report)   # decoded equality (T097)
    finally:
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        (OUT / "stomp_summary.json").write_text(json.dumps(res, indent=1, default=str))
        release_lock()
    print(json.dumps({k: res.get(k) for k in ("restore_report", "session_close", "restore_ok", "decoded_equal_backup",
                                              "bytes_equal_backup", "errors")},
                     default=str))
    return 1 if res["errors"] or not res.get("restore_ok") else 0


# ----------------------------------------------------------------------------- analysis


def echoes(x: np.ndarray, t_from: float = 0.70, n: int = 5) -> tuple[float, list[tuple[float, float]]]:
    """Direct-sound peak time (s) and up to n echo (time s, level dB re direct) peaks from the 5 ms-smoothed
    rectified signal."""
    a = np.abs(np.asarray(x, np.float64))
    w = int(0.005 * SR)
    env = np.convolve(a, np.ones(w) / w, mode="same")
    d0 = int(np.argmax(env[: int(0.58 * SR)]))
    ref = env[d0]
    found = []
    i0 = int(t_from * SR)
    seg = env[i0:]
    guard = int(0.06 * SR)
    seg = seg.copy()
    for _ in range(n):
        k = int(np.argmax(seg))
        if seg[k] < ref * 10 ** (-50 / 20):
            break
        found.append(((i0 + k) / SR, float(20 * np.log10(seg[k] / ref))))
        seg[max(0, k - guard): k + guard] = 0
    return d0 / SR, sorted(found)


def log_spectral_distance(a: np.ndarray, b: np.ndarray, n: int = 2048, lo_hz: float = 60.0, hi_hz: float = 10000.0) -> float:
    """Phase-free supplement for modulation and reverb: after one level-matching gain, the RMS over frames and bins
    (60 Hz-10 kHz, bins within 60 dB of the loudest bin of that frame) of the difference of the two log-magnitude
    spectra, in dB. 0 means the same short-time spectra; it ignores where an LFO is in its cycle only partly."""
    m = min(len(a), len(b))
    a, b = np.asarray(a[:m], np.float64), np.asarray(b[:m], np.float64)
    g = np.sqrt(np.sum(b ** 2) / max(np.sum(a ** 2), 1e-30))
    a = a * g
    win = np.hanning(n)
    f = np.fft.rfftfreq(n, 1 / SR)
    sel = (f >= lo_hz) & (f <= hi_hz)
    d = []
    for i in range(0, m - n, n // 2):
        A = np.abs(np.fft.rfft(a[i:i + n] * win))[sel]
        B = np.abs(np.fft.rfft(b[i:i + n] * win))[sel]
        if B.max() < 1e-5:
            continue
        keep = B > B.max() * 1e-3
        d.append(20 * np.log10((A[keep] + 1e-9) / (B[keep] + 1e-9)))
    return float(np.sqrt(np.mean(np.concatenate(d) ** 2))) if d else float("nan")


def load_raw(name: str, d: str, eng: str, rep: bool = False) -> np.ndarray | None:
    f = RAW / f"{name}__{d}__{eng}{'_rep' if rep else ''}.wav"
    return PN.load_di(f) if f.exists() else None


def ledger(args: list[str], on: bool) -> None:
    PN.ledger(args, on)


def cmd_analyse(a) -> int:
    mf = manifest()
    stomp_sum = json.loads((OUT / "stomp_summary.json").read_text()) if (OUT / "stomp_summary.json").exists() else {}
    fw = (stomp_sum.get("info") or {}).get("firmware") or (stomp_sum.get("info") or {}).get("firmware_version") or "3.80"
    results: dict = {}
    for it in mf["items"]:
        name = it["name"]
        s, n = load_raw(name, "guitar", "stomp"), load_raw(name, "guitar", "native")
        if s is None or n is None:
            results[name] = {"category": it["category"], "model": it["model"], "skipped": f"stomp={s is not None} native={n is not None}"}
            continue
        r = PN.analyse(s, n, tail_s=2.0)
        PN.write_wav(OUT / f"{name}_stereo.wav", np.stack([r["stomp_matched"], r["native"]], axis=1))
        res = {k: v for k, v in r.items() if k not in ("stomp_matched", "native", "residual", "bands")}
        res["files"] = {"stereo": f"{name}_stereo.wav"}
        res.update(category=it["category"], model=it["model"], er=it["er"], lsd_db=log_spectral_distance(s, n))
        rep = load_raw(name, "guitar", "stomp", rep=True)
        if rep is not None:
            res["stomp_repeat_floor_db"] = PN.analyse(rep, s, tail_s=2.0)["null_residual_db"]
        if it["category"] == "Delay":
            sb, nb = load_raw(name, "burst", "stomp"), load_raw(name, "burst", "native")
            if sb is not None and nb is not None:
                ds, es = echoes(sb)
                dn, en = echoes(nb)
                m = min(len(es), len(en))
                res["echo"] = {"direct_ms_stomp": ds * 1000, "direct_ms_native": dn * 1000, "n_stomp": len(es), "n_native": len(en),
                               "level_diff_db": [round(en[i][1] - es[i][1], 2) for i in range(m)],
                               "time_diff_ms": [round(((en[i][0] - dn) - (es[i][0] - ds)) * 1000, 2) for i in range(m)],
                               "stomp": es, "native": en}
                res["echo_level_ok"] = bool(m >= 1 and all(abs(v) <= ECHO_LEVEL_TOL_DB for v in res["echo"]["level_diff_db"])
                                            and len(es) == len(en))
        res["pass"] = bool(r["null_residual_db"] <= NULL_PASS_DB)
        results[name] = res
        print(f"{name:34s} {it['category']:11s} null {r['null_residual_db']:7.1f} dB gain {r['gain_db']:+.2f} xcorr {r['xcorr_peak']:.3f}"
              + f" lsd {res['lsd_db']:.1f}" + (f" repeat {res['stomp_repeat_floor_db']:.1f}" if "stomp_repeat_floor_db" in res else "")
              + (f" echo lvl {res['echo']['level_diff_db']} t {res['echo']['time_diff_ms']}" if "echo" in res else ""))
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str))
    # summary.json in the layout block_coverage_report.py reads (hlx found through the bisect/ link)
    (OUT / "summary.json").write_text(json.dumps(
        {"dataset": DATASET, "di": str(OUT / "di_guitar.wav"), "presets": list(results), "results": results,
         "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1, default=str))
    if not (OUT / "bisect").exists():
        (OUT / "bisect").symlink_to("hlx")
    if a.ledger:
        for name, res in results.items():
            if "null_residual_db" not in res:
                continue
            note = (f"model={res['model']};category={res['category']};preset={name};stomp_fw={fw};native=3.82;hum=0;ripple=0;"
                    f"cab_er={res['er'] if res['er'] is not None else 0};gain_db={res['gain_db']:.2f};xcorr={res['xcorr_peak']:.3f};"
                    f"verdict={'pass' if res['pass'] else 'FAIL'}(<=-30);single-path;clip=guitar 4 s+2 s tail")
            note += f";log_spectral_distance_db={res['lsd_db']:.2f}"
            if res.get("stomp_repeat_floor_db") is not None:
                note += f";stomp_repeat_floor_db={res['stomp_repeat_floor_db']:.1f}"
            if "echo" in res:
                note += f";echo_level_diff_db={res['echo']['level_diff_db']};echo_time_diff_ms={res['echo']['time_diff_ms']}"
            ledger(["--kind", "parity", "--engine", "both", "--dataset", DATASET, "--metric", "null_residual_db",
                    "--value", f"{res['null_residual_db']:.2f}", "--notes", note], True)
        cats: dict[str, list] = {}
        for name, res in results.items():
            if "null_residual_db" in res:
                cats.setdefault(res["category"], []).append((name, res))
        for c, lst in cats.items():
            ok = [n for n, r in lst if r["pass"]]
            ledger(["--kind", "parity", "--engine", "both", "--dataset", DATASET, "--metric", "models_in_parity",
                    "--value", str(len(ok)), "--renders", str(len(lst)), "--notes",
                    (f"category={c};{len(ok)} of {len(lst)} models null <= -30 dB; median null "
                     f"{float(np.median([r['null_residual_db'] for _, r in lst])):.1f} dB; "
                     f"failed: {', '.join(n for n, r in lst if not r['pass']) or 'none'};stomp_fw={fw};native=3.82")], True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")
    for nm in ("native", "stomp"):
        p = sub.add_parser(nm)
        p.add_argument("--only", nargs="*", default=[])
        if nm == "native":
            p.add_argument("--hosts", type=int, default=6)
        else:
            p.add_argument("--lock-wait-s", type=float, default=7200.0)
    p = sub.add_parser("analyse")
    p.add_argument("--ledger", action="store_true")
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "native": cmd_native, "stomp": cmd_stomp, "analyse": cmd_analyse}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
