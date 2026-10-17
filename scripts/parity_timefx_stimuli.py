#!/usr/bin/env python3
"""Parity of reverbs, modulation and one more delay time with purpose-made stimuli (T066b, benchmark).

T066 left 9 reverb and modulation checks unresolved because the T060 clips (4 s of guitar) were too short
and too busy: one 2 s free decay cannot resolve decay times, and a 4 s clip holds under 2 cycles of a slow LFO.
This adds:

  nburst   pink-noise burst (0.6 s) then 6 s of silence: reverb decay per octave, pre-delay, tail
           spectrum and level
  sine330  10 s sustained sine, 330 Hz  \\ modulation rate, level swing, pitch swing
  sine1k   10 s sustained sine, 1 kHz   /
  note     a real sustained guitar note (Guitar-TECHS single notes, about 4.5 s), existing mod_profile
  burst7   the 19 ms burst of T060, 7.5 s long, for Simple Delay at 600 ms (second delay time)

Presets are the T060 files (cache/parity/spot380/hlx: ER_000 = the effect slot off, the reference for each
engine's own block input; Rev_ x6; Mod_ x5) plus Del_DelaySimpleDelay600. The Native side renders first
(pool of at most 2 hosts); the pedal side is ONE batched session (shared pedal lock, tonematch.pedal_lock, scratch slot, restore with
readback and byte diff). The Stomp renders each model twice (the repeat floor), Native once.

  gen      offline: write hlx + stimuli + manifest under cache/parity/timefx-stimuli/
  native   Native renders
  stomp    pedal session
  analyse  scores with the T066 metrics and tolerances (+ src/tonematch/metrics/sine_mod.py); --ledger writes rows
           kind=parity dataset parity-timefx-stimuli-fw380
"""
from __future__ import annotations

import argparse
import json
import math
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

import parity_null as PN
import parity_snap as SNAP
import parity_spot as PS
import parity_timefx as PT
import stomp_fw380 as F

from tonematch import catalog as C
from tonematch import preset as P
from tonematch.metrics import delay, parity, reverb, sine_mod

SR = 48_000
SPOT = ROOT / "cache" / "parity" / "spot380"
OUT = ROOT / "cache" / "parity" / "timefx-stimuli"
HLX = OUT / "hlx"
HLX_SNAPPED = OUT / "hlx_snapped"   # what the Stomp loads: Native's CC-grid values (parity method rule)
RAW = OUT / "raw"
DATASET = "parity-timefx-stimuli-fw380"
REF = "ER_000"
REVERBS = ["Rev_ReverbRoom", "Rev_ReverbPlate", "Rev_Reverb63Spring", "Rev_ReverbChamber", "Rev_ReverbHall", "Rev_ReverbGanymede"]
UNRESOLVED_REVERBS = REVERBS[:4]
MODS = ["Mod_Chorus70sChorus", "Mod_FlangerGrayFlanger", "Mod_PhaserDeluxePhaser", "Mod_VibratoBubbleVibrato", "Mod_MM4AnalogChorus"]
MOD_KIND = {"Chorus": "pitch", "Vibrato": "pitch", "Flanger": "sweep", "Phaser": "sweep"}
DEL = "Del_DelaySimpleDelay600"
SINE_F = {"sine330": 330.0, "sine1k": 1000.0}
# T066c: 20 s sines for the two slow LFOs (under 2 cycles in 10 s); rendered on snapped (Native CC-grid) values only
LONG_SINE_F = {"sine330L": 330.0, "sine1kL": 1000.0}
LONG_MODS = ["Mod_FlangerGrayFlanger", "Mod_PhaserDeluxePhaser"]
ALL_SINE_F = {**SINE_F, **LONG_SINE_F}
SNAPPED_MARK = RAW / "snapped_stomp.json"   # renders the Stomp made from Native's grid values (parity method rule)
NOTE_SRC = ROOT / "data" / "di" / "Guitar-TECHS" / "P1_singlenotes" / "audio" / "directinput" / "directinput_allsinglenotes.wav"
NOTE_AT_S, NOTE_LEN_S = 380.0, 4.5
# Native takes every parameter through a MIDI CC with 128 steps over the catalog range (lo + c/127 (hi - lo)); the
# Stomp takes the exact value from the file. Parameters whose range is in Hz carry the LFO rate: the rate the grid
# predicts for Native is snapped / requested.
RATE_PARAM = {"Mod_PhaserDeluxePhaser": "Rate", "Mod_MM4AnalogChorus": "Speed"}
OUTPUT_PARAMS = ("Level", "Headroom", "Mix")
GRID_NOTE_PCT = 2.0           # a snapped parameter this far (relative) from its request can move depth or level


def grid_snapped(name: str) -> dict[str, tuple[float, float, float, float]]:
    """{param: (requested, snapped, lo, hi)} for the effect block's parameters that Native's CC grid moves, with lo and
    hi from the captured template (not the catalog). Parameters of the other blocks are left out."""
    cat = C.load_catalog("current")
    p = P.read_hlx(HLX / f"{name}.hlx", cat)
    moves = SNAP.snapped_values(p, SNAP.template_for(p, PS.TEMPLATES))
    out: dict = {}
    for ref, v in moves.items():
        i, k = ref.split(":", 1)
        if p.slots[int(i)].category in ("mod", "reverb", "delay"):
            out[k] = v
    return out


def stomp_was_snapped(name: str, di: str) -> bool:
    """True when the Stomp render of (name, di) was made from Native's grid values."""
    try:
        return bool(json.loads(SNAPPED_MARK.read_text()).get(f"{name}__{di}"))
    except (OSError, ValueError):
        return False


# ----------------------------------------------------------------------------- gen


def fade(x: np.ndarray, ms: float = 10.0) -> np.ndarray:
    f = int(ms * SR / 1000)
    x = x.copy()
    x[:f] *= np.linspace(0, 1, f)
    x[-f:] *= np.linspace(1, 0, f)
    return x


def make_stimuli() -> dict[str, np.ndarray]:
    import soundfile as sf

    t10 = np.arange(10 * SR) / SR
    out = {n: fade((0.12 * np.sin(2 * np.pi * f * t10)).astype(np.float32)) for n, f in SINE_F.items()}   # -21 dBFS RMS
    t20 = np.arange(20 * SR) / SR
    out.update({n: fade((0.12 * np.sin(2 * np.pi * f * t20)).astype(np.float32)) for n, f in LONG_SINE_F.items()})
    rng = np.random.default_rng(66)
    # pink noise by 1/f shaping, 0.6 s, peak -13 dBFS like the guitar DI; 0.2 s lead, 6 s of silence
    n = int(0.6 * SR)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20.0)
    pink = np.fft.irfft(spec / np.sqrt(f), n)
    pink = fade(pink / np.max(np.abs(pink)) * 0.22, 20.0)
    out["nburst"] = np.concatenate([np.zeros(int(0.2 * SR)), pink, np.zeros(6 * SR)]).astype(np.float32)
    x, sr = sf.read(str(NOTE_SRC), start=int(NOTE_AT_S * SR), frames=int(NOTE_LEN_S * SR), dtype="float64")
    assert sr == SR
    x = x if x.ndim == 1 else x.mean(axis=1)
    x = fade(x / np.max(np.abs(x)) * 0.22, 5.0)
    out["note"] = np.concatenate([x, np.zeros(int(0.5 * SR))]).astype(np.float32)
    b = PN.load_di(SPOT / "di_burst.wav")
    out["burst7"] = np.concatenate([b[:int(1.0 * SR)], np.zeros(int(6.5 * SR))]).astype(np.float32)
    return out


def cmd_gen(_a) -> int:
    HLX.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    for n in [REF, *REVERBS, *MODS]:
        shutil.copy(SPOT / "hlx" / f"{n}.hlx", HLX / f"{n}.hlx")
    d = json.loads((SPOT / "hlx" / "Del_DelaySimpleDelay.hlx").read_text())
    blk = next(v for v in d["data"]["tone"]["dsp0"].values() if isinstance(v, dict) and v.get("@model") == "HD2_DelaySimpleDelay")
    blk["Time"] = 0.6
    (HLX / f"{DEL}.hlx").write_text(P.dumps_hlx(d), encoding="utf-8")
    st = make_stimuli()
    for n, x in st.items():
        PN.write_wav(OUT / f"di_{n}.wav", x)
    items = [{"name": REF, "dis": ["sine330", "sine1k", "note", "nburst", *LONG_SINE_F], "reps": 1}]
    items += [{"name": n, "dis": ["nburst"], "reps": 2} for n in REVERBS]
    items += [{"name": n, "dis": ["sine330", "sine1k", "note", *(LONG_SINE_F if n in LONG_MODS else [])], "reps": 2} for n in MODS]
    items += [{"name": DEL, "dis": ["burst7"], "reps": 2}]
    rc = 0
    for it in items:
        it["hlx"] = str((HLX / f"{it['name']}.hlx").relative_to(ROOT))
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(ROOT / it["hlx"]), "--firmware",
                            hex(F.FW_380)], capture_output=True, text=True, check=False)
        it["validate_exit"] = r.returncode
        rc = max(rc, r.returncode)
        if r.returncode:
            print(it["name"], "validate", r.returncode, (r.stdout.strip().splitlines() or [""])[-1])
    (OUT / "manifest.json").write_text(json.dumps({"items": items, "stimuli_s": {k: len(v) / SR for k, v in st.items()}}, indent=1))
    print(f"{len(items)} presets, {sum(len(i['dis']) for i in items)} stimulus renders per engine; validate failures: "
          f"{sum(1 for i in items if i['validate_exit'])}")
    return rc


def manifest() -> dict:
    return json.loads((OUT / "manifest.json").read_text())


def dry_of(tag: str) -> np.ndarray:
    return PN.load_di(OUT / f"di_{tag}.wav")


# ----------------------------------------------------------------------------- native


def cmd_native(a) -> int:
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and PN.HOSTVENV.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(PN.HOSTVENV)
    from tonematch.engines.native import NativeRenderPool, RenderJob

    cat = C.load_catalog("current")
    todo = [it for it in manifest()["items"] if not a.only or it["name"] in a.only]
    presets = {it["name"]: P.read_hlx(ROOT / it["hlx"], cat) for it in todo}
    PS.TEMPLATES.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    pool = NativeRenderPool(workers=a.hosts, preroll=PN.PREROLL, templates_dir=PS.TEMPLATES)
    summary: dict = {"hosts": pool.workers, "errors": {}}
    try:
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
                if a.stim and d not in a.stim:
                    continue
                jobs.append(RenderJob(dry=dry_of(d), preset=presets[it["name"]], tag=(it["name"], d)))
                tags.append((it["name"], d))
        outs = pool.render_batch(jobs, on_error="none")
        for (name, d), y in zip(tags, outs, strict=True):
            if y is None:
                summary["errors"][f"{name}:{d}"] = "render failed"
                continue
            PN.write_wav(RAW / f"{name}__{d}__native.wav", np.asarray(y, np.float32))
        summary.update(capture_s=round(t2 - t0, 1), render_s=round(time.perf_counter() - t2, 1), renders=len(jobs),
                       firmware=getattr(pool.engines[0], "firmware_version", None))
    finally:
        pool.close()
    (OUT / "native_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, default=str))
    return 1 if summary["errors"] else 0


# ----------------------------------------------------------------------------- stomp (one batched session)


def cmd_stomp(a) -> int:
    import soundfile as sf

    from tonematch import reamp
    from tonematch.engines.stomp import StompEngine, restore_verdict

    for var in ("TONEMATCH_STOMPD", "TONEMATCH_STOMPD_COMPILE"):
        if not os.environ.get(var):
            sys.exit(f"set {var}")
    if PS.hx_edit_running():
        sys.exit("HX Edit is running: quit it first")
    cat = C.load_catalog("current")
    todo = [it for it in manifest()["items"] if not a.only or it["name"] in a.only]
    # parity method rule: the Stomp plays the value Native's CC grid gives (needs the Native templates; offline, before the pedal)
    try:
        snap = SNAP.snap_dir([it["name"] for it in todo], HLX, HLX_SNAPPED, PS.TEMPLATES, cat)
    except LookupError as e:
        sys.exit(f"cannot snap to Native's grid: {e}")
    print(f"snapped {sum(1 for v in snap.values() if v['moves'])} of {len(snap)} presets to Native's CC grid", flush=True)
    if not PS.take_lock(a.lock_wait_s):
        sys.exit("pedal lock not obtained in time")
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-timefx-stimuli"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"results": {}, "errors": []}
    eng = None
    t_all = time.perf_counter()
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
                    for d in it["dis"]:
                        if a.stim and d not in a.stim:
                            continue
                        for k in range(it["reps"]):
                            t1 = time.perf_counter()
                            y = np.asarray(eng.render(dry_of(d)), np.float32)
                            suffix = "" if k == 0 else "_rep"
                            sf.write(str(RAW / f"{name}__{d}__stomp{suffix}.wav"), y, reamp.SR, subtype="FLOAT")
                            r[f"render_s_{d}{suffix}"] = round(time.perf_counter() - t1, 2)
                            r[f"rms_db_{d}{suffix}"] = round(reamp.rms_db(y), 2)
                            marks = json.loads(SNAPPED_MARK.read_text()) if SNAPPED_MARK.exists() else {}
                            marks[f"{name}__{d}"] = True
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
        res["session_wall_clock_s"] = round(time.perf_counter() - t_all, 1)
        (run_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
        (OUT / "stomp_summary.json").write_text(json.dumps(res, indent=1, default=str))
        PS.release_lock()
    print(json.dumps({k: res.get(k) for k in ("restore_report", "session_close", "restore_ok", "decoded_equal_backup",
                                              "bytes_equal_backup", "errors", "session_wall_clock_s")},
                     default=str))
    return 1 if res["errors"] or not res.get("restore_ok") else 0


# ----------------------------------------------------------------------------- analysis


def load(name: str, di: str, eng: str, rep: bool = False) -> np.ndarray | None:
    import soundfile as sf

    f = RAW / f"{name}__{di}__{eng}{'_rep' if rep else ''}.wav"
    if not f.exists():
        return None
    x, sr = sf.read(str(f), dtype="float64", always_2d=True)
    assert sr == SR, f
    return x.mean(axis=1)


def combine_floor(*fl: dict) -> dict:
    """Element-wise maximum of reverb floors (estimator chance floor and the Stomp's own repeat)."""
    out: dict = {}
    for k in fl[0]:
        v = [f[k] for f in fl if k in f]
        out[k] = [float(max(z)) for z in zip(*v, strict=True)] if isinstance(v[0], list) else float(max(v))
    return out


def do_reverb(name: str, refs: dict) -> dict | None:
    s, n, rep = load(name, "nburst", "stomp"), load(name, "nburst", "native"), load(name, "nburst", "stomp", True)
    if s is None or n is None:
        return None
    ps, pn = reverb.reverb_profile(s, refs["stomp"]), reverb.reverb_profile(n, refs["native"])
    c = reverb.compare(ps, pn)
    est = reverb.estimator_floor(refs["stomp"], ps.rt60_s, t_stop=ps.t_stop_s, pairs=8)
    out = {"stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c, "estimator_floor": est}
    fl = est
    if rep is not None:
        pr = reverb.reverb_profile(rep, refs["stomp"])
        rd = reverb.compare(ps, pr)
        out["stomp_repeat_diff"] = rd
        fl = combine_floor(est, {"edt_pct": [abs(v) if math.isfinite(v) else 0.0 for v in rd["edt_diff_pct"]],
                                 "rt60_pct": [abs(v) if math.isfinite(v) else 0.0 for v in rd["rt60_diff_pct"]],
                                 "tail_spectrum_db": abs(rd["tail_spectrum_max_abs_db"]),
                                 "tail_level_db": abs(rd["tail_level_diff_db"]),
                                 "predelay_ms": abs(rd["predelay_diff_ms"] or 0.0)})
    out["floor"] = fl
    out["verdict"] = parity.reverb_verdict(c, floor=fl)
    return out


def do_sine(name: str, di: str, refs: dict) -> dict | None:
    """One sine stimulus (10 s ``SINE_F`` or 20 s ``LONG_SINE_F``) of one model."""
    s, n, rep = load(name, di, "stomp"), load(name, di, "native"), load(name, di, "stomp", True)
    if s is None or n is None:
        return None
    f0 = ALL_SINE_F[di]
    kind = next((v for k, v in MOD_KIND.items() if k in name), "pitch")
    ps = sine_mod.sine_profile(s, refs[di]["stomp"], f0)
    pn = sine_mod.sine_profile(n, refs[di]["native"], f0)
    c = sine_mod.compare(ps, pn)
    out = {"kind": kind, "stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c}
    cyc, conf = min(ps.cycles, pn.cycles), min(ps.confidence, pn.confidence)
    floor = None
    if rep is not None:
        pr = sine_mod.sine_profile(rep, refs[di]["stomp"], f0)
        rd = sine_mod.compare(ps, pr)
        out["stomp_repeat"] = pr.to_dict()
        out["stomp_repeat_diff"] = rd
        floor = {k: abs(v) for k, v in rd.items() if k.endswith(("_pct", "_db", "_cents")) and math.isfinite(v)}
        cyc, conf = min(cyc, pr.cycles), min(conf, pr.confidence)
    return finish_sine(out, name, c, kind, cyc, conf, floor, stomp_was_snapped(name, di))


def finish_sine(out: dict, name: str, c: dict, kind: str, cyc: float, conf: float, floor: dict | None,
                snapped: bool = False) -> dict:
    """Verdicts for a sine-type result. ``snapped``: the Stomp played Native's grid values (parity method rule), so the
    grid is not an engine difference and nothing is corrected for it; otherwise the rate and the depth/level checks
    are corrected for what Native's CC grid is known to do (T066b renders, made on the requested values)."""
    out["floor"] = floor
    gs = grid_snapped(name)
    out["grid_snapped"] = {k: {"requested": v[0], "snapped": v[1], "pct": 100 * (v[1] - v[0]) / v[0] if v[0] else 0.0}
                           for k, v in gs.items()}
    c2 = dict(c)
    rp = RATE_PARAM.get(name)
    if rp in gs and not snapped:
        pred = 100 * (gs[rp][1] / gs[rp][0] - 1)
        out["grid_pred_rate_pct"] = pred
        c2["rate_diff_pct"] = 100 * ((1 + c["rate_diff_pct"] / 100) / (1 + pred / 100) - 1)
        out["rate_diff_after_grid_pct"] = c2["rate_diff_pct"]
    v = parity.sine_modulation_verdict(c2, kind, cycles=cyc, confidence=conf, floor=floor)
    v["indicative_1p5_cycles"] = parity.sine_modulation_verdict(c2, kind, cycles=cyc, confidence=conf, floor=floor, min_cycles=1.5)
    moved = [k for k, g in out["grid_snapped"].items() if k not in OUTPUT_PARAMS and abs(g["pct"]) > GRID_NOTE_PCT]
    if moved and not snapped:
        # a depth or level check failed on a model whose other parameters the grid moved: not an engine difference yet
        for vv in (v, v["indicative_1p5_cycles"]):
            for kk in ("depth", "mean_level"):
                if vv["checks"].get(kk) is False:
                    vv["checks"].pop(kk)
                    vv["unresolved"].append(kk)
                    vv["grid_moved"] = moved
            vv["pass"] = False if vv["unresolved"] else all(x is True for x in vv["checks"].values())
    out["verdict"] = v
    return out


NOTE_SPAN_S = (0.4, 3.6)      # the note sustains (amp compression) for about 3.8 s


def note_f0(x: np.ndarray) -> float:
    seg = x[: 2 * SR] * np.hanning(2 * SR)
    sp = np.abs(np.fft.rfft(seg))
    return float(np.fft.rfftfreq(2 * SR, 1 / SR)[int(np.argmax(sp))])


def do_note(name: str, refs: dict) -> dict | None:
    """The real guitar note, measured like the sine at the note's fundamental (the transfer at f0, so the
    note's own pitch wobble cancels)."""
    s, n, rep = load(name, "note", "stomp"), load(name, "note", "native"), load(name, "note", "stomp", True)
    if s is None or n is None:
        return None
    f0 = note_f0(refs["note"]["native"])
    kind = next((v for k, v in MOD_KIND.items() if k in name), "pitch")
    ps = sine_mod.sine_profile(s, refs["note"]["stomp"], f0, span_s=NOTE_SPAN_S)
    pn = sine_mod.sine_profile(n, refs["note"]["native"], f0, span_s=NOTE_SPAN_S)
    c = sine_mod.compare(ps, pn)
    out = {"kind": kind, "f0": f0, "stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c}
    cyc, conf = min(ps.cycles, pn.cycles), min(ps.confidence, pn.confidence)
    floor = None
    if rep is not None:
        pr = sine_mod.sine_profile(rep, refs["note"]["stomp"], f0, span_s=NOTE_SPAN_S)
        rd = sine_mod.compare(ps, pr)
        out["stomp_repeat"], out["stomp_repeat_diff"] = pr.to_dict(), rd
        floor = {k: abs(v) for k, v in rd.items() if k.endswith(("_pct", "_db", "_cents")) and math.isfinite(v)}
        cyc, conf = min(cyc, pr.cycles), min(conf, pr.confidence)
    return finish_sine(out, name, c, kind, cyc, conf, floor, stomp_was_snapped(name, "note"))


def do_delay() -> dict | None:
    s, n, rep = load(DEL, "burst7", "stomp"), load(DEL, "burst7", "native"), load(DEL, "burst7", "stomp", True)
    if s is None or n is None:
        return None
    ps, pn = delay.echo_profile(s), delay.echo_profile(n)
    c = delay.compare(ps, pn)
    out = {"stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c, "verdict": parity.delay_verdict(c)}
    gs = grid_snapped(DEL).get("Time")
    if gs:
        req, snapped = gs[0], gs[1]
        out["time_requested_ms"], out["time_cc_grid_ms"] = 1000 * req, 1000 * snapped
        out["stomp_vs_requested_ms"] = 1000 * (ps.delay_s - req)
        out["native_vs_cc_grid_ms"] = 1000 * (pn.delay_s - snapped)
        out["grid_step_ms"] = 1000 * (gs[3] - gs[2]) / 127
        c2 = dict(c)       # the same comparison with Native's time taken as the grid value it was given
        c2["delay_err_pct"] = 100 * ((pn.delay_s - snapped) / snapped)
        c2["cum_time_err_ms_audible"] = abs(out["native_vs_cc_grid_ms"]) * c["audible_echoes"]
        out["verdict_after_grid"] = parity.delay_verdict(c2)
    if rep is not None:
        pr = delay.echo_profile(rep)
        out["stomp_repeat_diff"] = delay.compare(ps, pr)
    return out


def model_status(per_stim: dict[str, dict]) -> str:
    """Across stimuli: fail if any resolved check failed; pass if every stimulus that resolved passed and at
    least one resolved; otherwise unresolved."""
    sts = [PT.status(v['verdict']) for v in per_stim.values()]
    if "fail" in sts:
        return "fail"
    if "pass" in sts and "unresolved" not in sts:
        return "pass"
    return "unresolved" if "pass" not in sts else "pass-partial"


def agree_or_distrust(sines: list[dict]) -> None:
    """One LFO rate per model: when the two tones disagree by more than 10 %, the less confident one is not trusted."""
    if len(sines) != 2:
        return
    rs = [(min(x["stomp"]["rate_hz"], x["native"]["rate_hz"]), max(x["stomp"]["rate_hz"], x["native"]["rate_hz"])) for x in sines]
    if abs(rs[0][0] - rs[1][0]) > 0.1 * rs[0][0] or abs(rs[0][1] - rs[1][1]) > 0.1 * rs[0][1]:
        weak = min(sines, key=lambda x: min(x["stomp"]["confidence"], x["native"]["confidence"]))
        weak["verdict"] = {"checks": {}, "unresolved": ["rate", "depth", "mean_level"], "pass": False, "resolved": False,
                           "note": "the 330 Hz and 1 kHz tones give different LFO rates; the less confident one is not trusted"}


def analyse_long(a) -> int:
    """T066c: Gray Flanger and Deluxe Phaser on 20 s sines, snapped values on both engines."""
    mrefs = {d: {"stomp": load(REF, d, "stomp"), "native": load(REF, d, "native")} for d in LONG_SINE_F}
    res: dict = {"modulation": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for n in LONG_MODS:
        m: dict = {}
        for di in LONG_SINE_F:
            x = do_sine(n, di, mrefs)
            if x:
                m[di] = x
                d = x["diff"]
                print(f"{n:26s} {di:8s} rate {x['stomp']['rate_hz']:.3f}/{x['native']['rate_hz']:.3f} ({d['rate_diff_pct']:+.2f}%) conf "
                      f"{x['stomp']['confidence']:.2f}/{x['native']['confidence']:.2f} cycles {x['stomp']['cycles']:.1f}/{x['native']['cycles']:.1f} "
                      f"swing lvl {x['stomp']['level_swing_db']:.2f}/{x['native']['level_swing_db']:.2f} dB pitch "
                      f"{x['stomp']['pitch_swing_cents']:.1f}/{x['native']['pitch_swing_cents']:.1f} c snapped={stomp_was_snapped(n, di)} "
                      f"-> {PT.status(x['verdict'])} {PT.fmt(x['verdict']['checks'])} unres={x['verdict']['unresolved']}", flush=True)
        agree_or_distrust([m[d] for d in LONG_SINE_F if d in m])
        if m:
            res["modulation"][n] = m
    (OUT / "stimuli_long.json").write_text(json.dumps(PT.r(res, 4), indent=1, default=str))
    write_long_rows(res, a.ledger, a.task)
    return 0


def analyse(a) -> int:
    if a.long:
        return analyse_long(a)
    refs = {"stomp": load(REF, "nburst", "stomp"), "native": load(REF, "nburst", "native")}
    mrefs = {d: {"stomp": load(REF, d, "stomp"), "native": load(REF, d, "native")} for d in (*SINE_F, "note")}
    res: dict = {"reverb": {}, "modulation": {}, "delay": None, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for n in REVERBS:
        x = do_reverb(n, refs)
        if x:
            res["reverb"][n] = x
            d = x["diff"]
            print(f"{n:26s} RT60% {PT.fmt(d['rt60_diff_pct'], 1)} EDT% {PT.fmt(d['edt_diff_pct'], 1)} pre {d['predelay_diff_ms']} "
                  f"spec {d['tail_spectrum_max_abs_db']:.2f} lvl {d['tail_level_diff_db']:+.2f} -> {PT.status(x['verdict'])} "
                  f"unres={x['verdict']['unresolved']} checks={PT.fmt(x['verdict']['checks'])}", flush=True)
    for n in MODS:
        m: dict = {}
        for di in SINE_F:
            x = do_sine(n, di, mrefs)
            if x:
                m[di] = x
                d = x["diff"]
                print(f"{n:26s} {di:8s} rate {x['stomp']['rate_hz']:.3f}/{x['native']['rate_hz']:.3f} ({d['rate_diff_pct']:+.2f}%) conf "
                      f"{x['stomp']['confidence']:.2f}/{x['native']['confidence']:.2f} swing lvl {x['stomp']['level_swing_db']:.2f}/"
                      f"{x['native']['level_swing_db']:.2f} dB pitch {x['stomp']['pitch_swing_cents']:.1f}/{x['native']['pitch_swing_cents']:.1f} c "
                      f"-> {PT.status(x['verdict'])} {PT.fmt(x['verdict']['checks'])}", flush=True)
        sines = [m[d] for d in SINE_F if d in m]
        if len(sines) == 2:      # one rate for the model: the two tones must agree, else the less confident is not trusted
            rs = [(min(x["stomp"]["rate_hz"], x["native"]["rate_hz"]), max(x["stomp"]["rate_hz"], x["native"]["rate_hz"])) for x in sines]
            if abs(rs[0][0] - rs[1][0]) > 0.1 * rs[0][0] or abs(rs[0][1] - rs[1][1]) > 0.1 * rs[0][1]:
                weak = min(sines, key=lambda x: min(x["stomp"]["confidence"], x["native"]["confidence"]))
                weak["verdict"] = {"checks": {}, "unresolved": ["rate", "depth", "mean_level"], "pass": False, "resolved": False,
                                   "note": "the 330 Hz and 1 kHz tones give different LFO rates; the less confident one is not trusted"}
        x = do_note(n, mrefs)
        if x:
            m["note"] = x
            d = x["diff"]
            print(f"{n:26s} {'note':8s} rate {x['stomp']['rate_hz']:.3f}/{x['native']['rate_hz']:.3f} ({d['rate_diff_pct']:+.2f}%) conf "
                  f"{x['stomp']['confidence']:.2f}/{x['native']['confidence']:.2f} swing lvl {x['stomp']['level_swing_db']:.2f}/"
                  f"{x['native']['level_swing_db']:.2f} dB pitch {x['stomp']['pitch_swing_cents']:.1f}/{x['native']['pitch_swing_cents']:.1f} c "
                  f"-> {PT.status(x['verdict'])} {PT.fmt(x['verdict']['checks'])}", flush=True)
        if m:
            res["modulation"][n] = m
    res["delay"] = do_delay()
    if res["delay"]:
        d = res["delay"]["diff"]
        print(f"{DEL}: Native-Stomp delay {d['delay_err_ms']:+.2f} ms = {d['delay_err_pct']:+.3f}% echoes {d['n_echoes_ref']}/{d['n_echoes_other']} "
              f"cum {d['cum_time_err_ms_audible']:.1f} ms over {d['audible_echoes']} audible; level {PT.fmt(d['level_diff_db'])}", flush=True)
    (OUT / "stimuli.json").write_text(json.dumps(PT.r(res, 4), indent=1, default=str))
    (OUT / "stimuli_full.json").write_text(json.dumps(res, default=float))
    write_rows(res, a.ledger)
    return 0


# ----------------------------------------------------------------------------- ledger


def write_rows(res: dict, on: bool) -> None:
    base = ["--kind", "parity", "--engine", "both", "--dataset", DATASET]
    common = "stomp_fw=3.80;native=3.82;stimulus renders T066b (nburst 0.6 s pink noise + 6 s silence; sine 10 s; note 4.5 s)"
    cats: dict[str, list[tuple[str, str]]] = {"Reverb": [], "Modulation": []}
    for name, x in res["reverb"].items():
        d, v, fl = x["diff"], x["verdict"], x["floor"]
        judged = [i for i, f in enumerate(d["bands_hz"]) if f in parity.REVERB_JUDGED_BANDS]
        st = PT.status(v)
        cats["Reverb"].append((name, st))
        sets = [
            ("rt60_max_abs_diff_pct", max(abs(d["rt60_diff_pct"][i]) for i in judged if math.isfinite(d["rt60_diff_pct"][i])),
             f"RT60 diff pct by octave {PT.fmt([int(f) for f in d['bands_hz']])}={PT.fmt(d['rt60_diff_pct'], 1)};floor={PT.fmt(fl['rt60_pct'], 1)}"),
            ("edt_max_abs_diff_pct", max(abs(d["edt_diff_pct"][i]) for i in judged if math.isfinite(d["edt_diff_pct"][i])),
             f"EDT diff pct={PT.fmt(d['edt_diff_pct'], 1)};floor={PT.fmt(fl['edt_pct'], 1)}"),
            ("predelay_diff_ms", d["predelay_diff_ms"] or 0.0,
             f"predelay Stomp={x['stomp']['predelay_ms']:.2f} Native={x['native']['predelay_ms']:.2f} ms;floor={fl['predelay_ms']:.2f}"),
            ("tail_spectrum_max_abs_db", d["tail_spectrum_max_abs_db"], f"rms={d['tail_spectrum_rms_db']:.2f};floor={fl['tail_spectrum_db']:.2f}"),
            ("tail_level_diff_db", d["tail_level_diff_db"], f"floor={fl['tail_level_db']:.2f}"),
        ]
        for metric, val, extra in sets:
            note = (f"model={name};stimulus=nburst;{extra};RT60 Stomp s={PT.fmt(x['stomp']['rt60_s'])};"
                    f"unresolved={PT.fmt(v['unresolved'])};checks={PT.fmt(v['checks'])};verdict={st};{common}")
            PT.ledger([*base, "--metric", metric, "--value", f"{val:.3f}", "--renders", "3", "--notes", note], on)
        rd = x.get("stomp_repeat_diff")
        if rd:
            PT.ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", "stomp_repeat_rt60_max_abs_diff_pct", "--value",
                       f"{max(abs(v) for v in rd['rt60_diff_pct'] if math.isfinite(v)):.4f}", "--renders", "2", "--notes",
                       (f"model={name};the Stomp against itself (same session, nburst);EDT max={rd['max_abs_edt_diff_pct']:.4f} pct;"
                        f"predelay diff={rd['predelay_diff_ms']} ms;tail spectrum max={rd['tail_spectrum_max_abs_db']:.4f} dB;{common}")], on)
    for name, m in res["modulation"].items():
        sine_only = {k: v for k, v in m.items() if k in SINE_F}
        st = model_status(m)
        cats["Modulation"].append((name, st))
        for di, x in m.items():
            d, v = x["diff"], x["verdict"]
            rows = [("lfo_rate_diff_pct", d["rate_diff_pct"], f"rate Stomp={x['stomp']['rate_hz']:.3f} Native={x['native']['rate_hz']:.3f} Hz"
                     + f" (conf {x['stomp']['confidence']:.2f}/{x['native']['confidence']:.2f}; cycles {x['stomp']['cycles']:.1f}/{x['native']['cycles']:.1f})"),
                    ("level_swing_diff_db", d["level_swing_diff_db"], f"level swing Stomp={x['stomp']['level_swing_db']:.2f} Native={x['native']['level_swing_db']:.2f} dB"),
                    ("pitch_swing_diff_cents", d["pitch_swing_diff_cents"], f"pitch swing Stomp={x['stomp']['pitch_swing_cents']:.2f} Native={x['native']['pitch_swing_cents']:.2f} cents"),
                    ("mean_level_diff_db", d["mean_level_diff_db"], "mean gain at the tone")]
            fl = f"floor (Stomp vs itself)={PT.fmt(x['floor'])}"
            for metric, val, extra in rows:
                if not math.isfinite(val):
                    continue
                gnote = ""
                if x.get("grid_snapped"):
                    gnote = ";Native CC grid moved " + " ".join(f"{k} {g['pct']:+.1f}%" for k, g in x["grid_snapped"].items() if abs(g["pct"]) >= 1.0)
                    if "grid_pred_rate_pct" in x:
                        gnote += f";rate predicted by grid {x['grid_pred_rate_pct']:+.2f}% so rate diff after grid {x['rate_diff_after_grid_pct']:+.2f}%"
                ind = v.get("indicative_1p5_cycles")
                if ind:
                    gnote += f";at 1.5 cycles (indicative only) verdict={PT.status(ind)} checks={PT.fmt(ind['checks'])}"
                note = (f"model={name};stimulus={di};kind={x['kind']};{extra};{fl};unresolved={PT.fmt(v['unresolved'])};"
                        f"checks={PT.fmt(v['checks'])};resolved={v.get('resolved')};verdict={PT.status(v)}{gnote};{common}")
                PT.ledger([*base, "--metric", f"{di}_{metric}", "--value", f"{val:.3f}", "--renders", "3", "--notes", note], on)
            rd = x.get("stomp_repeat_diff")
            if rd and math.isfinite(rd["rate_diff_pct"]):
                PT.ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", f"{di}_stomp_repeat_lfo_rate_diff_pct",
                           "--value", f"{rd['rate_diff_pct']:.4f}", "--renders", "2", "--notes",
                           (f"model={name};stimulus={di};the Stomp against itself (same session; the LFO free-runs, so only phase-free "
                            f"measures repeat);level swing diff={rd['level_swing_diff_db']:.3f} dB;pitch swing diff={rd['pitch_swing_diff_cents']:.3f} cents;"
                            f"mean level diff={rd['mean_level_diff_db']:.3f} dB;{common}")], on)
        if sine_only:
            pass
    dl = res.get("delay")
    if dl:
        d = dl["diff"]
        st = PT.status(dl["verdict"])
        note = (f"model=Simple Delay Time 600 ms Feedback 0.4 Mix 0.4;time Native-Stomp per echo ms={PT.fmt(d['time_err_ms'])} pct={PT.fmt(d['time_err_pct'])};"
                f"delay Stomp={dl['stomp']['delay_s'] * 1000:.2f} Native={dl['native']['delay_s'] * 1000:.2f} ms;"
                f"cum over {d['audible_echoes']} audible echoes={d['cum_time_err_ms_audible']:.1f} ms;echo level diff dB={PT.fmt(d['level_diff_db'])};"
                f"echoes {d['n_echoes_ref']}/{d['n_echoes_other']};Stomp repeat delay diff={dl.get('stomp_repeat_diff', {}).get('delay_err_ms')} ms;"
                f"checks={PT.fmt(dl['verdict']['checks'])};verdict={st};{common}")
        note += (f";Native CC grid: requested {dl['time_requested_ms']:.2f} ms -> {dl['time_cc_grid_ms']:.3f} ms (step {dl['grid_step_ms']:.3f} ms, "
                 f"Time range 0..8 s over 128 CC values); Native measured minus grid value={dl['native_vs_cc_grid_ms']:.3f} ms; "
                 f"Stomp measured minus requested={dl['stomp_vs_requested_ms']:.4f} ms")
        PT.ledger([*base, "--metric", "delay_time_err_pct_600ms", "--value", f"{d['delay_err_pct']:.4f}", "--renders", "3", "--notes", note], on)
        PT.ledger([*base, "--metric", "delay_time_err_ms_600ms", "--value", f"{d['delay_err_ms']:.3f}", "--renders", "3", "--notes", note], on)
        PT.ledger([*base, "--metric", "delay_native_minus_cc_grid_ms", "--value", f"{dl['native_vs_cc_grid_ms']:.4f}", "--renders", "3", "--notes", note], on)
        PT.ledger([*base, "--metric", "delay_stomp_minus_requested_ms", "--value", f"{dl['stomp_vs_requested_ms']:.4f}", "--renders", "3", "--notes", note], on)
    for c, lst in cats.items():
        if not lst:
            continue
        nf = [n for n, s in lst if s != "fail"]
        PT.ledger([*base, "--metric", "models_without_failed_checks", "--value", str(len(nf)), "--renders", str(len(lst)), "--notes",
                   (f"category={c};{len(nf)} of {len(lst)} models have no check outside the accepted T066 tolerance beyond the measurement floor;"
                    f"failed: {' '.join(n for n, s in lst if s == 'fail') or 'none'};others may still hold unresolved checks (see models_in_tolerance);{common}")], on)
        ok = [n for n, s in lst if s.startswith("pass")]
        bad = [n for n, s in lst if s == "fail"]
        unr = [n for n, s in lst if s == "unresolved"]
        PT.ledger([*base, "--metric", "models_in_tolerance", "--value", str(len(ok)), "--renders", str(len(lst)), "--notes",
                   (f"category={c};{len(ok)} of {len(lst)} within the accepted T066 tolerances beyond the measurement floor;"
                    f"outside: {' '.join(bad) or 'none'};unresolved: {' '.join(unr) or 'none'};{common}")], on)


def write_long_rows(res: dict, on: bool, task: str = "T066c") -> None:
    base = ["--kind", "parity", "--engine", "both", "--dataset", DATASET]
    common = (f"task={task};stomp_fw=3.80;native=3.82;20 s sine, Stomp played Native's CC-grid values "
              f"(parity method rule, lo/hi from the spot380 template ranges, pre-T072: NOT block-library-v1.1 ranges, Native and Stomp files "
              f"use the same ones);2-cycle and 30 pct confidence bars unchanged")
    sts = []
    for name, m in res["modulation"].items():
        sts.append((name, model_status(m)))
        for di, x in m.items():
            d, v = x["diff"], x["verdict"]
            gnote = ";grid moved " + (" ".join(f"{k} {g['pct']:+.1f}%" for k, g in x["grid_snapped"].items()) or "nothing")
            rate_note = (f"rate Stomp={x['stomp']['rate_hz']:.3f} Native={x['native']['rate_hz']:.3f} Hz"
                         f" (conf {x['stomp']['confidence']:.2f}/{x['native']['confidence']:.2f}; "
                         f"cycles {x['stomp']['cycles']:.1f}/{x['native']['cycles']:.1f})")
            rows = [("lfo_rate_diff_pct", d["rate_diff_pct"], rate_note),
                    ("level_swing_diff_db", d["level_swing_diff_db"], f"level swing Stomp={x['stomp']['level_swing_db']:.2f} Native={x['native']['level_swing_db']:.2f} dB"),
                    ("pitch_swing_diff_cents", d["pitch_swing_diff_cents"], f"pitch swing Stomp={x['stomp']['pitch_swing_cents']:.2f} Native={x['native']['pitch_swing_cents']:.2f} cents"),
                    ("mean_level_diff_db", d["mean_level_diff_db"], "mean gain at the tone")]
            for metric, val, extra in rows:
                if not math.isfinite(val):
                    continue
                note = (f"model={name};stimulus={di};kind={x['kind']};{extra};floor (Stomp vs itself)={PT.fmt(x['floor'])};"
                        f"unresolved={PT.fmt(v['unresolved'])};checks={PT.fmt(v['checks'])};resolved={v.get('resolved')};"
                        f"verdict={PT.status(v)}{gnote};{common}")
                PT.ledger([*base, "--metric", f"{di}_{metric}", "--value", f"{val:.3f}", "--renders", "3", "--notes", note], on)
            rd = x.get("stomp_repeat_diff")
            if rd and math.isfinite(rd["rate_diff_pct"]):
                PT.ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", f"{di}_stomp_repeat_lfo_rate_diff_pct",
                           "--value", f"{rd['rate_diff_pct']:.4f}", "--renders", "2", "--notes",
                           (f"model={name};stimulus={di};the Stomp against itself (same session);level swing diff={rd['level_swing_diff_db']:.3f} dB;"
                            f"pitch swing diff={rd['pitch_swing_diff_cents']:.3f} cents;mean level diff={rd['mean_level_diff_db']:.3f} dB;{common}")], on)
    if sts:
        ok = [n for n, s_ in sts if s_.startswith("pass")]
        PT.ledger([*base, "--metric", "long_sine_models_in_tolerance", "--value", str(len(ok)), "--renders", str(len(sts)), "--notes",
                   (f"category=Modulation;{len(ok)} of {len(sts)} within tolerance on 20 s sines;"
                    + ";".join(f"{n}={s_}" for n, s_ in sts) + f";{common}")], on)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")
    for nm in ("native", "stomp"):
        p = sub.add_parser(nm)
        p.add_argument("--only", nargs="*", default=[])
        p.add_argument("--stim", nargs="*", default=[], help="render only these stimuli (e.g. sine330L sine1kL)")
        if nm == "native":
            p.add_argument("--hosts", type=int, default=2)
        else:
            p.add_argument("--lock-wait-s", type=float, default=7200.0)
    p = sub.add_parser("analyse")
    p.add_argument("--ledger", action="store_true")
    p.add_argument("--long", action="store_true", help="T066c: score only the 20 s sines of Gray Flanger and Deluxe Phaser")
    p.add_argument("--task", default="T066c", help="task id written into the ledger notes")
    a = ap.parse_args(argv)
    os.environ.setdefault("TONEMATCH_AGENT", "benchmark")
    return {"gen": cmd_gen, "native": cmd_native, "stomp": cmd_stomp, "analyse": analyse}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
