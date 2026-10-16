#!/usr/bin/env python3
"""Parity of time-based effects and distortion by direct measurement (T066, tone-metrics), from the
saved T060 spot-check renders only: no pedal, no Native.

A null cannot judge delays (clock drift), reverbs (different tails with the same decay) or modulation
(the Stomp's LFO free-runs). This scores each Stomp/Native pair with the measurements in
src/tonematch/metrics/ (delay.py, reverb.py, modulation.py, nonlinear.py) and checks them against the
proposed tolerances in src/tonematch/metrics/parity.py.

Inputs (written by scripts/parity_spot.py, layout there): cache/parity/spot380/
  raw/<name>__guitar__{stomp,native}[_rep].wav   4 s guitar + 2 s silence through K with one block changed
  raw/<name>__burst__{stomp,native}.wav          delays: a 19 ms burst at 0.5 s, 4 s long
  raw/ER_000__guitar__{stomp,native}.wav         K with the effect slot OFF and the same wide-open cab:
                                                 each engine's own input to the effect block (the reference)
  hlx/<name>.hlx, manifest.json
Output: cache/parity/spot380/timefx.json; with --ledger, rows kind=parity dataset parity-spot-fw380-timefx.

    python scripts/parity_timefx.py [--ledger] [--only Del_ Rev_ Mod_ Dis_]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.metrics import delay, modulation, nonlinear, parity, reverb
from tonematch.metrics._fx import align_gain, db10

SR = 48_000
OUT = ROOT / "cache" / "parity" / "spot380"
RAW = OUT / "raw"
HLX = OUT / "hlx"
DATASET = "parity-spot-fw380-timefx"
REF = "ER_000"
MOD_FLOOR: dict[str, float] = {}     # filled from the Stomp's own repeat (70s Chorus), used for later models
MOD_KIND = {"Tremolo": "tremolo", "Chorus": "pitch", "Vibrato": "pitch", "Flanger": "sweep", "Phaser": "sweep"}


def load(name: str, di: str, eng: str, rep: bool = False) -> np.ndarray | None:
    import soundfile as sf

    f = RAW / f"{name}__{di}__{eng}{'_rep' if rep else ''}.wav"
    if not f.exists():
        return None
    x, sr = sf.read(str(f), dtype="float64", always_2d=True)
    assert sr == SR, f
    return x.mean(axis=1)


def stomp_on_grid(names: list[str]) -> dict[str, bool]:
    """{model: True} when its Stomp render was made from Native's snapped CC-grid values (scripts/parity_snap.py; the
    marker is written by parity_spot.py stomp), else False (the T060 renders)."""
    try:
        marks = json.loads((OUT / "snapped_stomp.json").read_text())
    except (OSError, ValueError):
        marks = {}
    return {n: bool(marks.get(n)) for n in names}


def chain_matches_ref(name: str) -> bool:
    """True when the preset's amp and cab (everything before the effect slot) equal the reference's,
    so ER_000's render is that engine's input to the effect block."""
    def pre(n: str) -> dict:
        dsp = json.loads((HLX / f"{n}.hlx").read_text())["data"]["tone"]["dsp0"]
        return {k: {p: v for p, v in b.items() if p != "@enabled"} for k, b in dsp.items() if k in ("block3", "cab0")}
    return pre(name) == pre(REF)


def r(v, n=2):
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, n)
    if isinstance(v, list):
        return [r(x, n) for x in v]
    if isinstance(v, dict):
        return {k: r(x, n) for k, x in v.items()}
    return v


# ----------------------------------------------------------------------------- per category


def do_delay(name: str) -> dict | None:
    s, n = load(name, "burst", "stomp"), load(name, "burst", "native")
    if s is None or n is None:
        return None
    ps, pn = delay.echo_profile(s), delay.echo_profile(n)
    c = delay.compare(ps, pn)
    return {"stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c, "verdict": parity.delay_verdict(c)}


def do_reverb(name: str, refs: dict) -> dict | None:
    s, n = load(name, "guitar", "stomp"), load(name, "guitar", "native")
    if s is None or n is None:
        return None
    ok = chain_matches_ref(name)
    ps = reverb.reverb_profile(s, refs["stomp"] if ok else None, t_stop=None if ok else 4.0)
    pn = reverb.reverb_profile(n, refs["native"] if ok else None, t_stop=None if ok else 4.0)
    c = reverb.compare(ps, pn)
    # what one free decay can resolve on this input: two synthetic reverbs with the Stomp's measured
    # decay, driven by the Stomp's own block input, differing only in their random seed
    fl = reverb.estimator_floor(refs["stomp"] if ok else s, ps.rt60_s, t_stop=ps.t_stop_s, pairs=8)
    out = {"stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c, "estimator_floor": fl,
           "verdict": parity.reverb_verdict(c, floor=fl)}
    rep = load(name, "guitar", "stomp", rep=True)
    if rep is not None:
        pr = reverb.reverb_profile(rep, refs["stomp"] if ok else None, t_stop=None if ok else 4.0)
        out["stomp_repeat_diff"] = reverb.compare(ps, pr)
    return out


def do_mod(name: str, refs: dict) -> dict | None:
    s, n = load(name, "guitar", "stomp"), load(name, "guitar", "native")
    if s is None or n is None or not chain_matches_ref(name):
        return None
    kind = next((v for k, v in MOD_KIND.items() if k in name), "pitch")
    ps, pn = modulation.mod_profile(s, refs["stomp"], kind=kind), modulation.mod_profile(n, refs["native"], kind=kind)
    c = modulation.compare(ps, pn, kind)
    out = {"kind": kind, "stomp": ps.to_dict(), "native": pn.to_dict(), "diff": c}
    cyc, conf = min(ps.cycles, pn.cycles), min(ps.rate_confidence, pn.rate_confidence)
    rep = load(name, "guitar", "stomp", rep=True)
    if rep is not None:
        pr = modulation.mod_profile(rep, refs["stomp"], kind=kind)
        out["stomp_repeat"] = pr.to_dict()
        out["stomp_repeat_diff"] = modulation.compare(ps, pr, kind)
        out["stomp_repeat_verdict"] = parity.modulation_verdict(out["stomp_repeat_diff"], cycles=min(cyc, pr.cycles),
                                                                confidence=min(conf, pr.rate_confidence))
        MOD_FLOOR["pitch_depth_pct"] = abs(out["stomp_repeat_diff"]["pitch_depth_diff_pct"])
    out["verdict"] = parity.modulation_verdict(c, cycles=cyc, confidence=conf, floor=dict(MOD_FLOOR))
    return out


def do_dist(name: str, refs: dict, di: np.ndarray) -> dict | None:
    s, n = load(name, "guitar", "stomp"), load(name, "guitar", "native")
    if s is None or n is None:
        return None
    ok = chain_matches_ref(name) or name == REF
    d = nonlinear.diagnose(s, n, di=di, ref_a=refs["stomp"] if ok and name != REF else None,
                           ref_b=refs["native"] if ok and name != REF else None)
    return {"diagnosis": d, "class": nonlinear.classify(d), "verdict": parity.distortion_verdict(d)}


def stomp_chain_floor() -> dict | None:
    """The Stomp against itself on the one deterministic repeat saved (Reverb Room, same session):
    null over the playing and per octave."""
    a, b = load("Rev_ReverbRoom", "guitar", "stomp"), load("Rev_ReverbRoom", "guitar", "stomp", rep=True)
    if a is None or b is None:
        return None
    k = 4 * SR
    am, lag, g = align_gain(b[:k], a[:k])
    res = am - b[:k]
    return {"null_db": float(db10(np.sum(res ** 2) / np.sum(b[:k] ** 2))), "lag": lag, "gain_db": float(20 * np.log10(g)),
            "noise_floor_dbfs": float(db10(np.mean(a[-int(0.3 * SR):] ** 2) + 1e-30))}


def reverdict(res: dict) -> dict:
    """Re-apply the verdict rules (src/tonematch/metrics/parity.py) to saved measurements."""
    for x in res["delay"].values():
        x["verdict"] = parity.delay_verdict(x["diff"])
    for x in res["reverb"].values():
        x["verdict"] = parity.reverb_verdict(x["diff"], floor=x["estimator_floor"])
    floor = {}
    for x in res["modulation"].values():
        if "stomp_repeat_diff" in x:
            floor["pitch_depth_pct"] = abs(x["stomp_repeat_diff"]["pitch_depth_diff_pct"])
    for x in res["modulation"].values():
        cyc = min(x["stomp"]["cycles"], x["native"]["cycles"])
        conf = min(x["stomp"]["rate_confidence"], x["native"]["rate_confidence"])
        x["verdict"] = parity.modulation_verdict(x["diff"], cycles=cyc, confidence=conf, floor=floor)
    for x in res["distortion"].values():
        x["verdict"] = parity.distortion_verdict(x["diagnosis"])
        x["class"] = nonlinear.classify(x["diagnosis"])
    return res


# ----------------------------------------------------------------------------- ledger


def ledger(args: list[str], on: bool) -> None:
    if not on:
        print("(no ledger)", " ".join(args)[:300])
        return
    env = dict(os.environ)
    env.setdefault("TONEMATCH_AGENT", "tone-metrics")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), *args], check=True, env=env)


def status(v: dict) -> str:
    """pass: every check inside tolerance; fail: a check outside it; unresolved: nothing failed but
    something could not be measured at the JND from these renders."""
    if any(c is False for c in v["checks"].values()):
        return "fail"
    if not v.get("resolved", True) or v.get("unresolved"):
        return "unresolved"
    return "pass" if v["pass"] else "fail"


def fmt(v, n=2) -> str:
    v = r(v, n)
    return json.dumps(v, separators=(",", ":")).replace(",", " ") if isinstance(v, (list, dict)) else str(v)


def write_rows(res: dict, on: bool) -> None:
    base = ["--kind", "parity", "--engine", "both", "--dataset", DATASET]
    common = "stomp_fw=3.80;native=3.82;saved T060 renders (no new renders)"
    cats: dict[str, list[tuple[str, str]]] = {}
    for name, x in res["delay"].items():
        d = x["diff"]
        note = (f"model={name};time Native-Stomp per echo ms={fmt(d['time_err_ms'])} pct={fmt(d['time_err_pct'])};"
                f"drift={d['drift_ms_per_echo']:.2f} ms/echo;cum over {d['audible_echoes']} audible echoes={d['cum_time_err_ms_audible']:.1f} ms;"
                f"echo level diff dB={fmt(d['level_diff_db'])};feedback slope Stomp={x['stomp']['slope_db_per_echo']:.2f} "
                f"Native={x['native']['slope_db_per_echo']:.2f} dB/echo;echoes {d['n_echoes_ref']}/{d['n_echoes_other']};"
                f"checks={fmt(x['verdict']['checks'])};verdict={status(x['verdict'])};{common}")
        ledger([*base, "--metric", "delay_time_err_pct", "--value", f"{d['delay_err_pct']:.3f}", "--renders", "2", "--notes", note], on)
        cats.setdefault("Delay", []).append((name, status(x["verdict"])))
    for name, x in res["reverb"].items():
        d = x["diff"]
        judged = [i for i, f in enumerate(d["bands_hz"]) if f in parity.REVERB_JUDGED_BANDS]
        mx = max(abs(d["rt60_diff_pct"][i]) for i in judged)
        note = (f"model={name};octaves Hz={fmt([int(f) for f in d['bands_hz']])};EDT diff pct={fmt(d['edt_diff_pct'], 1)};"
                f"RT60 diff pct={fmt(d['rt60_diff_pct'], 1)};RT60 Stomp s={fmt(x['stomp']['rt60_s'])};"
                f"predelay Stomp={x['stomp']['predelay_ms']:.1f} Native={x['native']['predelay_ms']:.1f} ms;"
                f"tail spectrum max={d['tail_spectrum_max_abs_db']:.2f} rms={d['tail_spectrum_rms_db']:.2f} dB;"
                f"tail level diff={d['tail_level_diff_db']:.2f} dB;estimator floor EDT pct={fmt(x['estimator_floor']['edt_pct'], 1)} "
                f"RT60 pct={fmt(x['estimator_floor']['rt60_pct'], 1)} spectrum={x['estimator_floor']['tail_spectrum_db']:.2f} dB "
                f"tail level={x['estimator_floor']['tail_level_db']:.2f} dB predelay={x['estimator_floor']['predelay_ms']:.2f} ms;"
                f"unresolved={fmt(x['verdict']['unresolved'])};checks={fmt(x['verdict']['checks'])};"
                f"verdict={status(x['verdict'])};{common}")
        ledger([*base, "--metric", "rt60_max_abs_diff_pct", "--value", f"{mx:.2f}", "--renders", "2", "--notes", note], on)
        cats.setdefault("Reverb", []).append((name, status(x["verdict"])))
        if "stomp_repeat_diff" in x:
            rd = x["stomp_repeat_diff"]
            ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", "stomp_repeat_rt60_max_abs_diff_pct",
                    "--value", f"{max(abs(v) for v in rd['rt60_diff_pct']):.4f}", "--renders", "2", "--notes",
                    (f"model={name};the Stomp against itself (same session);EDT max={rd['max_abs_edt_diff_pct']:.4f} pct;"
                     f"predelay diff={rd['predelay_diff_ms']} ms;tail spectrum max={rd['tail_spectrum_max_abs_db']:.4f} dB;{common}")], on)
    for name, x in res["modulation"].items():
        d = x["diff"]
        note = (f"model={name};kind={x['kind']};rate Stomp={x['stomp']['rate_hz']:.3f} Native={x['native']['rate_hz']:.3f} Hz "
                f"(conf {x['stomp']['rate_confidence']:.2f}/{x['native']['rate_confidence']:.2f});"
                f"amp depth {x['stomp']['amp_depth_db']:.2f}/{x['native']['amp_depth_db']:.2f} dB;"
                f"sweep depth {x['stomp']['sweep_depth_db']:.2f}/{x['native']['sweep_depth_db']:.2f} dB;"
                f"delay swing {x['stomp']['delay_depth_ms']:.3f}/{x['native']['delay_depth_ms']:.3f} ms;"
                f"pitch depth {x['stomp']['pitch_depth_cents']:.2f}/{x['native']['pitch_depth_cents']:.2f} cents (from delay velocity "
                f"{x['stomp']['pitch_depth_cents_vel']:.2f}/{x['native']['pitch_depth_cents_vel']:.2f} position "
                f"{x['stomp']['pitch_depth_cents_pos']:.2f}/{x['native']['pitch_depth_cents_pos']:.2f}; alias steps "
                f"{x['stomp']['alias_steps']}/{x['native']['alias_steps']});"
                f"LFO cycles in clip {x['stomp']['cycles']:.1f}/{x['native']['cycles']:.1f};"
                f"LFO-free spectrum level={d['spec_level_diff_db']:.2f} shape max={d['spec_shape_max_abs_db']:.2f} dB;"
                f"LFO phase diff={d['lfo_phase_diff_rad']:.2f} rad;checks={fmt(x['verdict']['checks'])};"
                f"verdict={status(x['verdict'])};{common}")
        ledger([*base, "--metric", "lfo_rate_diff_pct", "--value", f"{d['rate_diff_pct']:.3f}", "--renders", "2", "--notes", note], on)
        cats.setdefault("Modulation", []).append((name, status(x["verdict"])))
        if "stomp_repeat_diff" in x:
            rd = x["stomp_repeat_diff"]
            ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", "stomp_repeat_lfo_rate_diff_pct",
                    "--value", f"{rd['rate_diff_pct']:.3f}", "--renders", "2", "--notes",
                    (f"model={name};the Stomp against itself (same session; null -1.5 dB because the LFO free-runs);"
                    f"rate {x['stomp']['rate_hz']:.3f}/{x['stomp_repeat']['rate_hz']:.3f} Hz (conf {x['stomp']['rate_confidence']:.2f}/"
                     f"{x['stomp_repeat']['rate_confidence']:.2f});LFO phase diff={rd['lfo_phase_diff_rad']:.2f} rad;"
                     f"pitch depth diff velocity={rd['pitch_depth_diff_cents']:.2f} position={rd['pitch_depth_pos_diff_cents']:.2f} cents;"
                    f"amp depth diff={rd['amp_depth_diff_db']:.2f} dB;sweep depth diff={rd['sweep_depth_diff_db']:.2f} dB;"
                    f"LFO-free spectrum level={rd['spec_level_diff_db']:.2f} shape max={rd['spec_shape_max_abs_db']:.2f} dB;"
                     f"checks={fmt(x['stomp_repeat_verdict']['checks'])};{common}")], on)
    for name, x in res["distortion"].items():
        d = x["diagnosis"]
        note = (f"model={name};class={x['class']};null={d['null_db']:.1f} dB;null low-passed 5k/8k/10k="
                f"{fmt(d['null_db_below_hz'], 1)};level diff={d['level_diff_db']:.2f} dB;"
                f"core 80 Hz-8 kHz 1/3-oct diff max={d['core_band_diff_max_abs_db']:.2f} rms={d['core_band_diff_rms_db']:.2f} dB;"
                f"above 10 kHz max={d['hf_band_diff_max_abs_db']:.2f} dB;residual share above 5k={d['residual_share_above_5k']:.2f} "
                f"above 10k={d['residual_share_above_10k']:.2f};harmonics 2-10 diff max={d.get('harmonic_diff_max_abs_db', float('nan')):.2f} dB "
                f"({d.get('harmonic_frames', 0)} pitched frames);inharmonic share 2-10k Stomp/Native={fmt(d.get('inharmonic_share_db'), 1)} dB;"
                f"incoherent core max={d.get('incoherent_core_max_abs_db', float('nan')):.2f} dB;"
                f"noise floor dBFS={fmt(d['noise_floor_dbfs'], 0)};checks={fmt(x['verdict']['checks'])};"
                f"verdict={status(x['verdict'])};{common}")
        ledger([*base, "--metric", "core_band_diff_max_db", "--value", f"{d['core_band_diff_max_abs_db']:.3f}", "--renders", "2",
                "--notes", note], on)
        if name.startswith("Dis_"):
            cats.setdefault("Distortion", []).append((name, status(x["verdict"])))
    fl = res.get("stomp_chain_floor")
    if fl:
        ledger(["--kind", "parity", "--engine", "stomp", "--dataset", DATASET, "--metric", "stomp_repeat_null_db",
                "--value", f"{fl['null_db']:.2f}", "--renders", "2", "--notes",
                (f"Rev_ReverbRoom rendered twice in one session (amp Essex A15 Drive 0.61 + cab + Room): the pedal's own "
                f"repeat floor over the 4 s of playing; the only deterministic repeat saved, so no per-drive repeat floor;"
                f"render noise floor {fl['noise_floor_dbfs']:.0f} dBFS (digital silence);{common}")], on)
    for c, lst in cats.items():
        ok = [n for n, st in lst if st == "pass"]
        bad = [n for n, st in lst if st == "fail"]
        unr = [n for n, st in lst if st == "unresolved"]
        ledger([*base, "--metric", "models_in_tolerance", "--value", str(len(ok)), "--renders", str(len(lst)), "--notes",
                (f"category={c};{len(ok)} of {len(lst)} within the PROPOSED JND tolerances (src/tonematch/metrics/parity.py);"
                 f"outside: {' '.join(bad) or 'none'};unresolved from these renders: {' '.join(unr) or 'none'};{common}")], on)


# ----------------------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--only", nargs="*", default=[], help="name prefixes (Del_ Rev_ Mod_ Dis_)")
    ap.add_argument("--rows-only", action="store_true", help="write the ledger rows from the last full run's timefx_full.json")
    a = ap.parse_args(argv)
    if a.rows_only:
        res = reverdict(json.loads((OUT / "timefx_full.json").read_text()))
        (OUT / "timefx.json").write_text(json.dumps(r(res, 4), indent=1, default=str))
        write_rows(res, a.ledger)
        return 0
    import soundfile as sf

    names = [it["name"] for it in json.loads((OUT / "manifest.json").read_text())["items"]]
    pick = [n for n in names if not a.only or any(n.startswith(p) for p in a.only)]
    refs = {e: load(REF, "guitar", e) for e in ("stomp", "native")}
    di, _ = sf.read(str(OUT / "di_guitar.wav"), dtype="float64")
    res: dict = {"delay": {}, "reverb": {}, "modulation": {}, "distortion": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "tolerances": parity.TOLERANCES, "reference": f"{REF} (effect slot off, same amp and wide-open cab)"}
    t0 = time.perf_counter()
    for n in pick:
        if n.startswith("Del_"):
            x = do_delay(n)
            if x:
                res["delay"][n] = x
                d = x["diff"]
                print(f"{n:28s} delay {d['delay_err_pct']:+.2f}% drift {d['drift_ms_per_echo']:+.2f} ms/echo "
                      f"lvl {fmt(d['level_diff_db'])} -> {status(x['verdict'])} {fmt(x['verdict']['checks'])}", flush=True)
        elif n.startswith("Rev_"):
            x = do_reverb(n, refs)
            if x:
                res["reverb"][n] = x
                d = x["diff"]
                print(f"{n:28s} RT60% {fmt(d['rt60_diff_pct'], 1)} EDT% {fmt(d['edt_diff_pct'], 1)} pre {d['predelay_diff_ms']} "
                      f"spec {d['tail_spectrum_max_abs_db']:.2f} lvl {d['tail_level_diff_db']:+.2f} floor RT {fmt(x['estimator_floor']['rt60_pct'], 1)}"
                      f" -> {status(x['verdict'])} {fmt(x['verdict']['checks'])}", flush=True)
        elif n.startswith("Mod_"):
            x = do_mod(n, refs)
            if x:
                res["modulation"][n] = x
                d = x["diff"]
                print(f"{n:28s} rate {x['stomp']['rate_hz']:.3f}/{x['native']['rate_hz']:.3f} ({d['rate_diff_pct']:+.2f}%) "
                      f"{d['primary_depth']} {d['primary_depth_ref']:.2f}/{d['primary_depth_other']:.2f} spec lvl {d['spec_level_diff_db']:+.2f} "
                      f"shape {d['spec_shape_max_abs_db']:.2f} -> {status(x['verdict'])} {fmt(x['verdict']['checks'])}", flush=True)
        elif n.startswith("Dis_") or n == REF:
            x = do_dist(n, refs, di)
            if x:
                res["distortion"][n] = x
                d = x["diagnosis"]
                print(f"{n:28s} {x['class']:14s} null {d['null_db']:.1f} <10k {d['null_db_below_hz']['10000']:.1f} "
                      f"core {d['core_band_diff_max_abs_db']:.2f} harm {d.get('harmonic_diff_max_abs_db', float('nan')):.2f} "
                      f"lvl {d['level_diff_db']:+.2f}", flush=True)
    res["stomp_on_native_grid"] = on_grid = stomp_on_grid(pick)
    off = [n for n, v in on_grid.items() if not v]
    if off:      # parity method rule: these Stomp renders used requested values, not Native's CC-grid values (noted, not voided)
        print(f"NOTE: {len(off)} of {len(on_grid)} models were rendered on the Stomp with off-grid values (T060, before the "
              f"method rule); grid steps may show up as engine differences: {' '.join(off)}", flush=True)
    res["stomp_chain_floor"] = stomp_chain_floor()
    res["wall_clock_s"] = wall = round(time.perf_counter() - t0, 1)
    full = OUT / "timefx_full.json"
    if a.only and full.exists():          # a partial re-run replaces only the models it scored
        old = json.loads(full.read_text())
        for cat in ("delay", "reverb", "modulation", "distortion"):
            old[cat].update(res[cat])
        old["partial_reruns"] = [*old.get("partial_reruns", []), {"only": a.only, "at": res["started"]}]
        res = old
    full.write_text(json.dumps(res, default=float))
    (OUT / "timefx.json").write_text(json.dumps(r(res, 4), indent=1, default=str))
    print(f"wrote {OUT / 'timefx.json'} in {wall} s")
    write_rows(res, a.ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
