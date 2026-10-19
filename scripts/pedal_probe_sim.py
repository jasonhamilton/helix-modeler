"""T187: Native simulation of measured-pedal mode's real-world nuisances on drive identification.

Measurement only; the mode is not approved and nothing is adopted. No pedal, no song audio.

54 of the step-4 targets, one per model (54 Native drive models at hidden off-grid Drive/Tone, docs/notes/distortion-step4.md)
stand in for real pedals. Each target's dfp-v1 probe is rendered on Native with the INPUT-side nuisances
(before the clipping, so they need a render) and the OUTPUT-side ones are applied to the base render:

    input side (renders)   in_lvl_<d>   the pedal sees the probe d dB louder (send scale unknown)
                           imp_<R>      pickup-like treble loss into an R-ohm pedal input
                           in_tilt_<s>  interface tilt s dB/decade before the pedal
    output side (base)     noise_<n>    white noise at n dBFS RMS on the capture
                           out_tilt_<s> tilt after the pedal; *_cal = divided out by the simulated loopback
                           lat_<n>      n samples of latency left in the capture
                           ret_<g>      return gain g dB, clipped at 0 dBFS
    combos                 real_cal     after L1 + L2: in +1 dB, out tilt +0.5 corrected, noise -80, lat +2
                           real_l1      after L1 only: in +6 dB, out tilt +1 corrected, noise -80, lat +2
                           real_l1_n70  the same with noise -70

Fingerprints only (no audio kept): cache/pedal-probe-sim/fp/<target>/<condition>.npz.

    nice -n 10 .venv/bin/python scripts/pedal_probe_sim.py plan
    nice -n 10 .venv/bin/python scripts/pedal_probe_sim.py run --hosts 2 --workers 4 --max-load 9
    nice -n 10 .venv/bin/python scripts/pedal_probe_sim.py analyse --workers 4
    TONEMATCH_AGENT=stomp-engine .venv/bin/python scripts/pedal_probe_sim.py ledger
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch import pedal_probe as PP
from tonematch.metrics import distortion_probe as P

CACHE = ROOT / "cache" / "pedal-probe-sim"
FP_DIR = CACHE / "fp"
NOISE_DIR = CACHE / "noise"
REPORT = ROOT / "reports" / "local" / "pedal-probe-sim"
STEP4_PLAN = ROOT / "cache" / "distortion-step4" / "plan.json"
TRUTH_DIR = ROOT / "cache" / "distortion-step4" / "truth"
DATASET = "pedal-probe-sim-v1"
DRIVE_TOL, TONE_TOL = 0.15, 0.10

IN_LEVELS = (-12.0, -6.0, -3.0, -1.0, 1.0, 3.0, 6.0, 12.0)
LOADS = (10e3, 47e3, 220e3, 1e6)
IN_TILTS = (-2.0, 2.0)
NOISES = (-90.0, -80.0, -70.0)
OUT_TILTS = (-2.0, 2.0)
LATENCIES = (2, 48, 480, 1207)          # 1207: the measured USB round trip (cache/stomp/latency.json) untrimmed
RETURNS = (6.0, 12.0)


def _f(v: float) -> str:
    return f"{v:+g}".replace("+", "p").replace("-", "m")


def _r(v: float) -> str:
    return f"{int(v / 1000)}k" if v < 1e6 else f"{int(v / 1e6)}M"


def input_variants() -> dict[str, list]:
    """name -> input-side ops on the probe (before the render)."""
    v = {"base": []}
    for d in IN_LEVELS:
        v[f"in_lvl_{_f(d)}"] = [("gain", d, False)]
    for r in LOADS:
        v[f"imp_{_r(r)}"] = [("load", r)]
    for s in IN_TILTS:
        v[f"in_tilt_{_f(s)}"] = [("tilt", s)]
    return v


def loop_spec(s: float) -> list:
    """The correction that the simulated loopback (the probe through a bare loop with tilt s) gives."""
    return [("correct_tilt", s)]


def output_variants() -> dict[str, tuple[str, list]]:
    """name -> (input variant rendered, output-side ops on that render)."""
    v: dict[str, tuple[str, list]] = {}
    for n in NOISES:
        v[f"noise_{_f(n)}"] = ("base", [("noise", n)])
    for s in OUT_TILTS:
        v[f"out_tilt_{_f(s)}"] = ("base", [("tilt", s)])
        v[f"out_tilt_{_f(s)}_cal"] = ("base", [("tilt", s)] + loop_spec(s))
    for s in IN_TILTS:
        v[f"in_tilt_{_f(s)}_cal"] = (f"in_tilt_{_f(s)}", loop_spec(s))
    for n in LATENCIES:
        v[f"lat_{n}"] = ("base", [("delay", n)])
    for g in RETURNS:
        v[f"ret_{_f(g)}"] = ("base", [("gain", g, True)])
    v["real_cal"] = ("in_lvl_p1", [("tilt", 0.5), ("noise", -80.0)] + loop_spec(0.5) + [("delay", 2)])
    v["real_l1"] = ("in_lvl_p6", [("tilt", 1.0), ("noise", -80.0)] + loop_spec(1.0) + [("delay", 2)])
    v["real_l1_n70"] = ("in_lvl_p6", [("tilt", 1.0), ("noise", -70.0)] + loop_spec(1.0) + [("delay", 2)])
    return v


# the noise takes: the same output-side ops on silence (for the mask / floor fixes)
NOISE_TAKES = {f"noise_{_f(n)}": [("noise", n)] for n in NOISES}
NOISE_TAKES.update({"real_cal": [("tilt", 0.5), ("noise", -80.0)] + loop_spec(0.5),
                    "real_l1": [("tilt", 1.0), ("noise", -80.0)] + loop_spec(1.0),
                    "real_l1_n70": [("tilt", 1.0), ("noise", -70.0)] + loop_spec(1.0)})


# ------------------------------------------------------------------------------------------- ops

_LOOP: dict[float, PP.LoopCal] = {}


def sim_loopcal(s: float) -> PP.LoopCal:
    """The bare loop with an interface tilt of s dB/decade, measured the way the tool measures it."""
    if s not in _LOOP:
        sig, _ = PP.probe()
        arr, sc = PP.fingerprint(PP.tilt(sig, s))
        lp = PP.loop_from_fingerprint(arr, sc)
        _LOOP[s] = PP.LoopCal(latency_samples=0, measurements=[0, 0, 0], clipped=False, self_lag=0, **lp)
    return _LOOP[s]


def apply_ops(y: np.ndarray, ops: list, seed: int) -> np.ndarray:
    y = np.asarray(y, float)
    for op in ops:
        k = op[0]
        if k == "gain":
            y = PP.gain(y, op[1], clip=op[2])
        elif k == "load":
            y = PP.pickup_load(y, op[1])
        elif k == "tilt":
            y = PP.tilt(y, op[1])
        elif k == "noise":
            y = PP.add_noise(y, op[1], seed=seed)
        elif k == "delay":
            y = PP.delay(y, int(op[1]))
        elif k == "correct_tilt":
            y, _ = PP.correct_capture(y, sim_loopcal(op[1]))
        else:
            raise ValueError(k)
    return y


def seed_of(tid: str) -> int:
    return 1870 + int(tid[1:])


# ------------------------------------------------------------------------------------------- plan

def cmd_plan(a) -> dict:
    p4 = json.loads(STEP4_PLAN.read_text())
    split = {e["id"]: e["split"] for e in p4["excerpts"]}
    # One target per model (its lowest id): the probe never uses the step-4 DI excerpts, so their dev/test
    # split means nothing here and the model is the unit; 54 targets keep the new renders to 810.
    first: dict[str, dict] = {}
    for t in p4["targets"]:
        first.setdefault(t["model"], t)
    targets = [dict(t, split=split[t["excerpt"]]) for t in sorted(first.values(), key=lambda t: t["id"])]
    plan = {"version": DATASET, "task": "T187", "targets": targets, "inputs": list(input_variants()),
            "outputs": {k: v[0] for k, v in output_variants().items()}, "noise_takes": list(NOISE_TAKES),
            "library_dirs": list(PP.LIB_DIRS), "note": "measurement only; mode not approved; no pedal"}
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "plan.json").write_text(json.dumps(plan, indent=1))
    return {"targets": len(targets), "renders": len(targets) * len(plan["inputs"]),
            "analyses": len(targets) * (len(plan["inputs"]) + len(plan["outputs"]))}


def load_plan() -> dict:
    return json.loads((CACHE / "plan.json").read_text())


# ------------------------------------------------------------------------------------------- run

def _init():
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    try:
        os.nice(10)
    except OSError:
        pass


def fp_job(tid: str, variant: str, y: np.ndarray, outs: dict[str, list]) -> dict:
    """Worker: fingerprint the render (as ``variant``) and each output-side condition built on it."""
    t0 = time.perf_counter()
    d = FP_DIR / tid
    d.mkdir(parents=True, exist_ok=True)
    done = []
    for cond, ops in [(variant, [])] + list(outs.items()):
        f = d / f"{cond}.npz"
        if f.exists():
            continue
        arr, sc = PP.fingerprint(apply_ops(y, ops, seed_of(tid)) if ops else y)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(P.pack(arr, sc))
        tmp.replace(f)
        done.append(cond)
    return {"tid": tid, "variant": variant, "done": done, "s": round(time.perf_counter() - t0, 1)}


def noise_takes(log=print) -> None:
    NOISE_DIR.mkdir(parents=True, exist_ok=True)
    n = PP.layout()["n"]
    for name, ops in NOISE_TAKES.items():
        f = NOISE_DIR / f"{name}.npz"
        if f.exists():
            continue
        arr, sc = PP.fingerprint(apply_ops(np.zeros(n), ops, seed=99_187))
        f.write_bytes(P.pack(arr, sc))
        log(json.dumps({"noise_take": name}))


MIN_FREE_GB = 8.0


def free_gb(path: Path = ROOT) -> float:
    import shutil

    return shutil.disk_usage(path).free / 1e9


MIN_MEM_FREE_PCT = 40


def mem_free_pct() -> int:
    """macOS free-memory level (kern.memorystatus_level, 0-100); 100 elsewhere. Below MIN_MEM_FREE_PCT new
    Native hosts would push pages to swap, which grows on the data volume."""
    import subprocess

    try:
        return int(subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True, check=False,
                                  timeout=5).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 100


def _wait_load(max_load: float, log, min_free_gb: float = MIN_FREE_GB) -> float:
    """Pause while the 1-minute load is above ``max_load`` (until it is a point under it) or the disk
    has less than ``min_free_gb`` free (swap from Native hosts grows on the data volume)."""
    ld = os.getloadavg()[0]
    if ld > max_load:
        log(json.dumps({"pause_load1": round(ld, 2)}))
        while os.getloadavg()[0] > max_load - 1.0:
            time.sleep(5)
    if min_free_gb > 0 and (free_gb() < min_free_gb or mem_free_pct() < MIN_MEM_FREE_PCT):
        log(json.dumps({"pause_disk_free_gb": round(free_gb(), 2), "mem_free_pct": mem_free_pct()}))
        while free_gb() < min_free_gb + 0.2 or mem_free_pct() < MIN_MEM_FREE_PCT + 5:
            time.sleep(15)
    return ld


def todo_for(t: dict, plan: dict) -> dict[str, dict[str, list]]:
    """input variant -> {output condition: ops} still missing for target t (the variant itself
    included as {} when its own fingerprint is missing)."""
    outs = output_variants()
    d = FP_DIR / t["id"]
    need: dict[str, dict[str, list]] = {}
    for v in plan["inputs"]:
        o = {c: ops for c, (src, ops) in outs.items() if src == v and not (d / f"{c}.npz").exists()}
        if o or not (d / f"{v}.npz").exists():
            need[v] = o
    return need


def cmd_run(a) -> dict:
    import distortion_step4 as R

    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    log = lambda s: print(s, flush=True)
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    plan = load_plan()
    catalog = load_catalog()
    targets = plan["targets"] if not a.only else [t for t in plan["targets"] if t["id"] in a.only.split(",")]
    noise_takes(log)
    for s in sorted({s for s in list(OUT_TILTS) + list(IN_TILTS) + [0.5, 1.0]}):
        sim_loopcal(s)
    sig, _ = PP.probe()
    ins = input_variants()
    stats = {"renders": 0, "failed": 0, "analyses": 0, "load1_max": 0.0, "hosts_seen": set()}
    t_all = time.perf_counter()
    _wait_load(a.max_load, log, a.min_free_gb)          # before any host opens
    pool = None
    ex = cf.ProcessPoolExecutor(max_workers=min(a.workers, 4), mp_context=mp.get_context("spawn"), initializer=_init)
    pending: dict = {}
    try:
        for i, t in enumerate(targets):
            need = todo_for(t, plan)
            if not need:
                continue
            if (free_gb() < a.min_free_gb or mem_free_pct() < MIN_MEM_FREE_PCT) and pool is not None:
                # Native hosts hold 1.5 GB each; under memory pressure that becomes swap on the data
                # volume. Close them while the disk is low, reopen after.
                pool.close()
                pool = None
                log(json.dumps({"hosts_closed_for_disk_gb": round(free_gb(), 2)}))
            _wait_load(a.max_load, log, a.min_free_gb)
            if pool is None:
                pool = nat.NativeRenderPool(workers=min(a.hosts, 2), templates_dir=lib.library_templates())
            preset = R._preset(lib, nat, catalog, t["model"], t["cc"])[0]
            names = list(need)
            jobs = [nat.RenderJob(dry=apply_ops(sig, ins[v], 0).astype(np.float32), preset=preset) for v in names]
            ys = pool.render_batch(jobs, on_error="none")
            stats["hosts_seen"].add(len(pool.engines))
            # keep at most ~2 targets of analyses in flight (memory, load)
            while len(pending) > 2 * len(ins):
                done, _ = cf.wait(list(pending), return_when=cf.FIRST_COMPLETED)
                for f in done:
                    r = f.result()
                    stats["analyses"] += len(r["done"])
                    pending.pop(f)
            for v, y in zip(names, ys, strict=True):
                stats["renders"] += 1
                if y is None:
                    stats["failed"] += 1
                    continue
                pending[ex.submit(fp_job, t["id"], v, np.asarray(y, np.float32), need[v])] = (t["id"], v)
            stats["load1_max"] = max(stats["load1_max"], _wait_load(a.max_load, log, 0.0))   # disk: at the loop top
            stats["disk_free_gb_min"] = min(stats.get("disk_free_gb_min", 1e9), round(free_gb(), 2))
            log(json.dumps({"target": i + 1, "of": len(targets), "renders": stats["renders"],
                            "analyses": stats["analyses"], "hosts": len(pool.engines),
                            "s": round(time.perf_counter() - t_all, 1), "load1": round(os.getloadavg()[0], 2)}))
        for f in cf.as_completed(list(pending)):
            stats["analyses"] += len(f.result()["done"])
    finally:
        if pool is not None:
            pool.close()
        ex.shutdown(wait=True, cancel_futures=False)
    stats["wall_s"] = round(time.perf_counter() - t_all, 1)
    stats["hosts_seen"] = sorted(stats["hosts_seen"])
    runs = json.loads((CACHE / "runs.json").read_text()) if (CACHE / "runs.json").exists() else []
    runs.append(stats)
    (CACHE / "runs.json").write_text(json.dumps(runs, indent=1))
    return stats


# ------------------------------------------------------------------------------------------- analyse

def truth_pos(t: dict) -> list[float]:
    return [t["cc"][t["drive"]] / 127.0] + ([t["cc"][t["tone"]] / 127.0] if t["tone"] else [])


def level_of(cond: str) -> float:
    if cond.startswith("in_lvl_"):
        s = cond[len("in_lvl_"):]
        return float(s[1:]) * (1 if s[0] == "p" else -1)
    return {"real_cal": 1.0, "real_l1": 6.0, "real_l1_n70": 6.0}.get(cond, 0.0)


def fixes_for(cond: str) -> list[str]:
    """Which fingerprint fixes are evaluated on a condition."""
    f = ["none"]
    noisy = cond.startswith(("noise_", "real_"))
    if cond.startswith(("in_lvl_", "real_")) or cond == "base":
        f.append("lvl")
    if noisy:
        f += ["mask", "floor"]
    if cond.startswith("real_"):
        f += ["mask+lvl"]
    return f


_A: dict = {}


def _alib():
    if "lib" not in _A:
        _A["lib"] = PP.Library()
    return _A["lib"]


def _noise(cond: str):
    key = ("noise", cond)
    if key not in _A:
        _A[key] = P.unpack((NOISE_DIR / f"{cond}.npz").read_bytes())
    return _A[key]


def eval_one(t: dict, cond: str, fix: str, lib: PP.Library) -> dict | None:
    f = FP_DIR / t["id"] / f"{cond}.npz"
    if not f.exists():
        return None
    arr, sc = P.unpack(f.read_bytes())
    use = lib
    if fix in ("mask", "mask+lvl"):
        arr = PP.noise_mask(arr, _noise(cond)[0])
    if fix == "floor":
        nz = _noise(cond)[0]
        arr = PP.noise_floor_arrays(arr, nz)
        use = PP.Library(recs=lib.recs_all, floor=(nz, float(sc["g0_ss_1k_db"])))
    res = use.identify(arr, sc, level_search=fix in ("lvl", "mask+lvl"))
    rk = res["ranking"]
    names = [r[0] for r in rk]
    kt = use.knob_for(t["model"], res["arr"], res["sc"])
    tr = truth_pos(t)
    row = {"tid": t["id"], "model": t["model"], "split": t["split"], "cond": cond, "fix": fix,
           "rank": 1 + names.index(t["model"]), "top": names[:5], "dist": [float(r[1]) for r in rk[:5]],
           "knobs_true_map": [float(v) for v in kt], "knobs_top": [float(v) for v in rk[0][2]], "truth": tr,
           "delta_db": res["delta_db"], "level_true_db": level_of(cond)}
    return row


def eval_target(tid: str) -> list[dict]:
    plan = load_plan()
    t = next(x for x in plan["targets"] if x["id"] == tid)
    lib = _alib()
    conds = list(plan["inputs"]) + list(plan["outputs"])
    rows = []
    for c in conds:
        for fx in fixes_for(c):
            r = eval_one(t, c, fx, lib)
            if r is not None:
                rows.append(r)
    return rows


LOO_CONDS = ("base", "real_cal", "real_l1")
LOO_FIXES = {"base": ("none",), "real_cal": ("mask",), "real_l1": ("mask", "mask+lvl")}


def eval_loo(model: str) -> list[dict]:
    """Leave ``model`` out of the library; its targets stand in for pedals that are not in it."""
    plan = load_plan()
    full = _alib()
    lib = PP.Library(recs=full.recs_all, drop=(model,))
    rows = []
    for t in [x for x in plan["targets"] if x["model"] == model]:
        for c in LOO_CONDS:
            f = FP_DIR / t["id"] / f"{c}.npz"
            if not f.exists():
                continue
            arr0, sc = P.unpack(f.read_bytes())
            for fx in LOO_FIXES[c]:
                arr = PP.noise_mask(arr0, _noise(c)[0]) if fx.startswith("mask") else arr0
                lvl = fx.endswith("lvl")
                res = lib.identify(arr, sc, level_search=lvl)
                rin = full.identify(arr, sc, level_search=lvl)
                rows.append({"tid": t["id"], "model": model, "split": t["split"], "cond": c, "fix": fx,
                             "loo_top": [r[0] for r in res["ranking"][:5]], "loo_dist": float(res["ranking"][0][1]),
                             "in_top": rin["ranking"][0][0], "in_dist": float(rin["ranking"][0][1])})
    return rows


def _boot(vals_by_model: dict[str, list[float]], n: int = 2000, seed: int = 0):
    rng = np.random.default_rng(seed)
    ms = [m for m in vals_by_model if vals_by_model[m]]
    allv = [v for m in ms for v in vals_by_model[m]]
    if not allv:
        return [float("nan")] * 3
    bs = []
    for _ in range(n):
        pick = rng.choice(len(ms), len(ms), replace=True)
        bs.append(np.mean([v for p in pick for v in vals_by_model[ms[p]]]))
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return [float(np.mean(allv)), float(lo), float(hi)]


def summarise(rows: list[dict], fam: dict[str, str]) -> dict:
    out = {"n": len(rows)}
    for r in rows:
        r["top1"] = float(r["rank"] == 1)
        r["top5"] = float(r["rank"] <= 5)
        r["fam_top1"] = float(fam.get(r["top"][0], r["top"][0]) == fam.get(r["model"], r["model"]))
        kt, tr = r["knobs_true_map"], r["truth"]
        r["drive_err"] = abs(kt[0] - tr[0])
        r["drive_ok"] = float(r["drive_err"] <= DRIVE_TOL + 1e-9)
        if len(tr) > 1 and len(kt) > 1:
            r["tone_err"] = abs(kt[1] - tr[1])
            r["tone_ok"] = float(r["tone_err"] <= TONE_TOL + 1e-9)
        kp = r["knobs_top"]
        ok = r["top1"] and abs(kp[0] - tr[0]) <= DRIVE_TOL + 1e-9 and (
            len(tr) == 1 or len(kp) < 2 or abs(kp[1] - tr[1]) <= TONE_TOL + 1e-9)
        r["all_ok"] = float(bool(ok))
    for key in ("top1", "top5", "fam_top1", "drive_ok", "tone_ok", "all_ok"):
        by: dict[str, list[float]] = {}
        for r in rows:
            if key in r:
                by.setdefault(r["model"], []).append(r[key])
        out[key] = _boot(by)
    for key in ("drive_err", "tone_err"):
        v = [r[key] for r in rows if key in r]
        out[f"median_{key}"] = float(np.median(v)) if v else None
    lv = [r["delta_db"] - r["level_true_db"] for r in rows if r["fix"] in ("lvl", "mask+lvl")]
    if lv:
        out["level_error_db_median_abs"] = float(np.median(np.abs(lv)))
    return out


def loo_summary(rows: list[dict], fam_members: dict[str, list[str]]) -> dict:
    out = {}
    for c in LOO_CONDS:
        for fx in LOO_FIXES[c]:
            rs = [r for r in rows if r["cond"] == c and r["fix"] == fx]
            if not rs:
                continue
            multi = [r for r in rs if len(fam_members.get(r["model"], [r["model"]])) > 1]
            fam_ok = [float(r["loo_top"][0] in fam_members.get(r["model"], [])) for r in multi]
            fam_top3 = [float(any(m in fam_members.get(r["model"], []) for m in r["loo_top"][:3])) for r in multi]
            # chance: a random other model is in the family
            ch = [(len(fam_members[r["model"]]) - 1) / 53.0 for r in multi]
            d_in = np.array([r["in_dist"] for r in rs])
            d_out = np.array([r["loo_dist"] for r in rs])
            auc = float(np.mean(d_out[:, None] > d_in[None, :]) + 0.5 * np.mean(d_out[:, None] == d_in[None, :]))
            thr = float(np.percentile(d_in, 95))
            out[f"{c}|{fx}"] = {"n": len(rs), "n_multi_family": len(multi),
                                "nearest_in_family": float(np.mean(fam_ok)) if fam_ok else None,
                                "family_in_top3": float(np.mean(fam_top3)) if fam_top3 else None,
                                "chance_in_family": float(np.mean(ch)) if ch else None,
                                "novelty_auc": auc, "in_dist_p95": thr,
                                "flagged_new_at_p95": float(np.mean(d_out > thr)),
                                "median_in_dist": float(np.median(d_in)), "median_loo_dist": float(np.median(d_out))}
    return out


def cmd_analyse(a) -> dict:
    from tonematch.search.shortlist import load_families

    plan = load_plan()
    tids = [t["id"] for t in plan["targets"]]
    rows: list[dict] = []
    t0 = time.perf_counter()
    with cf.ProcessPoolExecutor(max_workers=min(a.workers, 4), mp_context=mp.get_context("spawn"), initializer=_init) as ex:
        for i, rs in enumerate(ex.map(eval_target, tids)):
            rows += rs
            if (i + 1) % 10 == 0:
                print(json.dumps({"analysed": i + 1, "of": len(tids), "s": round(time.perf_counter() - t0, 1),
                                  "load1": round(os.getloadavg()[0], 2)}), flush=True)
        models = sorted({t["model"] for t in plan["targets"]})
        loo = [r for rs in ex.map(eval_loo, models) for r in rs]
    fam = PP.families()
    fam_members = load_families(PP.FAMILIES)
    summ: dict = {"dataset": DATASET, "n_targets": len(tids), "families": "drive-families-v1" if fam else None,
                  "library_dirs": list(PP.LIB_DIRS), "by": {}}
    keys = sorted({(r["cond"], r["fix"]) for r in rows})
    for c, fx in keys:
        for sp in ("all", "dev", "test"):
            rs = [r for r in rows if r["cond"] == c and r["fix"] == fx and (sp == "all" or r["split"] == sp)]
            if rs:
                summ["by"][f"{c}|{fx}|{sp}"] = summarise([dict(r) for r in rs], fam)
    summ["loo"] = {sp: loo_summary([r for r in loo if sp == "all" or r["split"] == sp], fam_members)
                   for sp in ("all", "dev", "test")}
    summ["analysis_s"] = round(time.perf_counter() - t0, 1)
    runs = json.loads((CACHE / "runs.json").read_text()) if (CACHE / "runs.json").exists() else []
    # renders kept = input-variant fingerprints on disk (runs.json misses the runs that were stopped while
    # paused for disk; their renders were kept and not redone)
    summ["renders"] = sum((FP_DIR / t["id"] / f"{v}.npz").exists() for t in plan["targets"] for v in plan["inputs"])
    summ["renders_logged"] = sum(r["renders"] for r in runs)
    summ["failed"] = sum(r["failed"] for r in runs)
    summ["render_wall_s"] = round(sum(r["wall_s"] for r in runs), 1)
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "rows.json").write_text(json.dumps(rows))
    (REPORT / "loo_rows.json").write_text(json.dumps(loo))
    (REPORT / "summary.json").write_text(json.dumps(summ, indent=1))
    (REPORT / "table.md").write_text(table_md(summ))
    return {"rows": len(rows), "loo_rows": len(loo), "s": summ["analysis_s"]}


def table_md(summ: dict, split: str = "all") -> str:
    lines = ["| condition | fix | n | top-1 | top-5 | family top-1 | Drive OK | Tone OK | model+knobs | level err |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in summ["by"].items():
        c, fx, sp = k.split("|")
        if sp != split:
            continue
        f = lambda key, s=s: f"{s[key][0]:.2f}" if s.get(key) and np.isfinite(s[key][0]) else "-"
        le = f"{s['level_error_db_median_abs']:.1f}" if "level_error_db_median_abs" in s else ""
        lines.append(f"| {c} | {fx} | {s['n']} | {f('top1')} | {f('top5')} | {f('fam_top1')} | {f('drive_ok')} | "
                     f"{f('tone_ok')} | {f('all_ok')} | {le} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------- ledger

LEDGER_ROWS = (("base", "none", "base_top1"), ("in_lvl_p6", "none", "in_lvl_p6_top1"),
               ("in_lvl_p6", "lvl", "in_lvl_p6_levelsearch_top1"), ("in_lvl_m6", "none", "in_lvl_m6_top1"),
               ("in_lvl_p12", "lvl", "in_lvl_p12_levelsearch_top1"), ("noise_m70", "none", "noise_m70_top1"),
               ("noise_m70", "mask", "noise_m70_mask_top1"), ("imp_10k", "none", "imp_10k_top1"),
               ("in_tilt_p2", "none", "in_tilt_p2_top1"), ("out_tilt_p2", "none", "out_tilt_p2_top1"),
               ("lat_1207", "none", "lat_1207_top1"), ("real_cal", "none", "real_cal_top1"),
               ("real_l1", "none", "real_l1_top1"), ("real_l1", "mask+lvl", "real_l1_masklvl_top1"))


def cmd_ledger(a) -> dict:
    import subprocess

    summ = json.loads((REPORT / "summary.json").read_text())
    runs = json.loads((CACHE / "runs.json").read_text())
    renders = summ["renders"]
    wall = summ["render_wall_s"]
    note = ("T187 measurement only, measured-pedal mode NOT approved; Native stand-ins for real pedals (step-4 "
            "targets at hidden off-grid knobs), dfp-v1 probe with simulated interface nuisances; library "
            "dfp-v1 + tone ends, level-free curves; 54 targets, one per model")
    n = 0
    for cond, fix, metric in LEDGER_ROWS:
        s = summ["by"].get(f"{cond}|{fix}|all")
        if not s:
            continue
        v = s["top1"][0]
        extra = (f"{note}; {cond} fix={fix}; top5 {s['top5'][0]:.3f}, family {s['fam_top1'][0]:.3f}, Drive OK "
                 f"{s['drive_ok'][0]:.3f}, Tone OK {s['tone_ok'][0]:.3f}, 95% CI top1 [{s['top1'][1]:.2f}, {s['top1'][2]:.2f}]")
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "native",
               "--dataset", DATASET, "--metric", metric, "--value", f"{v:.4f}", "--renders", str(renders),
               "--wall-clock-s", str(wall), "--notes", extra]
        subprocess.run(cmd, check=True)
        n += 1
    lo = summ["loo"]["all"].get("base|none")
    if lo:
        for metric, v in (("loo_nearest_in_family", lo["nearest_in_family"]), ("loo_novelty_auc", lo["novelty_auc"])):
            cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine",
                   "native", "--dataset", DATASET, "--metric", metric, "--value", f"{v:.4f}", "--renders", str(renders),
                   "--wall-clock-s", str(wall), "--notes", (f"{note}; leave-one-model-out on the base condition, "
                   f"{lo['n_multi_family']} targets in multi-model families; chance {lo['chance_in_family']:.3f}")]
            subprocess.run(cmd, check=True)
            n += 1
    return {"rows": n, "runs": len(runs)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("plan", "run", "analyse", "ledger"))
    ap.add_argument("--hosts", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-load", type=float, default=9.0)
    ap.add_argument("--only", default="")
    ap.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)
    a = ap.parse_args(argv)
    out = {"plan": cmd_plan, "run": cmd_run, "analyse": cmd_analyse, "ledger": cmd_ledger}[a.cmd](a)
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
