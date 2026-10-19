#!/usr/bin/env python3
"""T185: larger end-to-end measurement of no-DI drive matching (builds on T183, scripts/nodi_drive_e2e.py).

MEASUREMENT ONLY. Nothing here is adopted: the measured tolerances, the 15 families and the second-choice family are
test settings (human 2026-10-18 "proceed with any testing"; decisions still open).

96 held-out random single-drive targets (seed 185; no T181 or T183 setting or excerpt reused), rendered from real DI excerpts
(Guitar-TECHS P1/P2, the human's own dry takes). Three conditions per target:
  blind        the matcher's DI is another PLAYER;
  same_player  the same player, a different excerpt;
  same_di      the target's own DI (an upper bound; the fingerprint inversion can run).
Four arms per condition: final order (provisional score | AFx-Rep) x shortlist (top 8 + families + second choice | plain top 8).
The coarse grid is the same for all four, so ONE job per (target, condition) renders it once (336 renders), scores the whole
expanded shortlist once on the full excerpt (the plain top 8 is a subset of it), and the arms are read off afterwards. The
renders each arm would use alone are counted per arm (336 + its own shortlist size).

    .venv/bin/python scripts/nodi_drive_e2e_v2.py plan
    .venv/bin/python scripts/nodi_drive_e2e_v2.py render
    TONEMATCH_AFXREP_DEVICE=cpu .venv/bin/python scripts/nodi_drive_e2e_v2.py run --shard 0/3     (one process per host)
    .venv/bin/python scripts/nodi_drive_e2e_v2.py analyse
    .venv/bin/python scripts/nodi_drive_e2e_v2.py clean
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
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")

SR = 48000
SEED = 185
EXC_S, LEAD_S = 5.0, 0.5
N_TARGETS, PER_PLAYER = 96, 5
OUT = ROOT / "cache" / "matches" / "t185"
AUDIO = OUT / "audio"
REPORT = ROOT / "reports" / "local" / "t185"
FAMILIES = ROOT / "cache" / "drive-maps" / "drive-families-v1.json"
SENS = ROOT / "cache" / "drive-maps" / "knob_sensitivity.json"
TAKES = ROOT / "data" / "songs" / "mother-playalong" / "takes"
GT = ROOT / "data" / "di" / "Guitar-TECHS"
CONDS = ("blind", "same_player", "same_di")
ARMS = {"prov_fam2": ("provisional", "fam2"), "afx_fam2": ("afxrep", "fam2"),
        "prov_top8": ("provisional", "top8"), "afx_top8": ("afxrep", "top8")}
GRID_RENDERS = 336
DRIVE_TOL = 0.15


def read_mono(p: Path) -> np.ndarray:
    import soundfile as sf
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    assert sr == SR, (p, sr)
    return x.mean(axis=1).astype(np.float32)


def write_wav(p: Path, x) -> None:
    import soundfile as sf
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), np.asarray(x, np.float32), SR, subtype="FLOAT")


def tol_map() -> Path:
    raw = json.loads(SENS.read_text())
    models = raw.get("models", raw)
    out = {}
    for m, v in models.items():
        if not isinstance(v, dict):
            continue
        e = {k: v[k] for k in ("drive_knobs", "tone_knobs") if k in v}
        for k in ("tone_tol", "tone_end_tol", "end_zone"):
            if k in (v.get("suggested_tol") or {}):
                e[k] = float(v["suggested_tol"][k])
        out[m] = e
    p = OUT / "tol_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"models": out, "note": "T185 TEST SETTING: suggested_tol applied explicitly, not adopted"}, indent=1))
    return p


# ------------------------------------------------------------------------------------------- stats

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar(a: list[bool], b: list[bool]) -> dict:
    """Exact two-sided McNemar on paired booleans: b01 = only b right, b10 = only a right."""
    b10 = sum(x and not y for x, y in zip(a, b))
    b01 = sum(y and not x for x, y in zip(a, b))
    n = b10 + b01
    if n == 0:
        return {"only_a": b10, "only_b": b01, "p": 1.0}
    k = min(b10, b01)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"only_a": b10, "only_b": b01, "p": p}


# ------------------------------------------------------------------------------------------- plan

def cmd_plan(a) -> None:
    import distortion_step4 as D4
    from tonematch.metrics import distortion_step3 as S3

    rng = np.random.default_rng(SEED)
    models, sem = D4.pool_models()
    taken = []
    for pl in (ROOT / "cache" / "distortion-step4" / "plan.json", ROOT / "cache" / "matches" / "t183" / "plan.json"):
        if pl.exists():
            taken += [(e["path"], e["start"], e["start"] + e["n"]) for e in json.loads(pl.read_text())["excerpts"]]
    n = int(EXC_S * SR)
    exc = []
    for player in ("P1", "P2", "human"):
        files = sorted(TAKES.glob("*_dry.wav")) if player == "human" else \
            sorted(f for d in sorted(GT.glob(f"{player}_*")) if d.name.split("_", 1)[1] in ("scales", "chords", "singlenotes", "techniques")
                   for f in (d / "audio" / "directinput").glob("*.wav"))
        order = [files[i] for _ in range(3) for i in rng.permutation(len(files))]   # a file may serve twice (human takes are few)
        k = 0
        for f in order:
            if k == PER_PLAYER:
                break
            import soundfile as sf
            if sf.info(str(f)).samplerate != SR:
                continue
            x = read_mono(f)
            if len(x) < n + 2 * SR:
                continue
            s = int(rng.integers(int(LEAD_S * SR), len(x) - n))
            seg = x[s:s + n]
            peak = 20 * np.log10(np.max(np.abs(seg)) + 1e-12)
            rms = 10 * np.log10(np.mean(seg.astype(float) ** 2) + 1e-20)
            rel = str(f.relative_to(ROOT))
            if peak < -26 or rms < -50 or any(rel == p and s < b and s + n > a_ for p, a_, b in taken):
                continue
            taken.append((rel, s, s + n))
            exc.append({"id": f"{player}{k}", "path": rel, "start": s, "n": n, "peak_dbfs": float(peak), "rms_dbfs": float(rms),
                        "player": player})
            k += 1
        assert k == PER_PLAYER, (player, k)
    old = set()
    for pl in (ROOT / "cache" / "distortion-step4" / "plan.json", ROOT / "cache" / "matches" / "t183" / "plan.json"):
        if pl.exists():
            old |= {(t["model"], tuple(sorted(t["cc"].items()))) for t in json.loads(pl.read_text())["targets"]}
    pick = [str(m) for m in rng.permutation(models)]
    pick += [str(m) for m in rng.permutation(models)[:N_TARGETS - len(models)]]
    dcc = [c for c in range(128) if c not in D4.DRIVE_MEASURED]
    tcc = [c for c in range(128) if c not in D4.TONE_MEASURED]
    one = [c for c in range(128) if c not in set(S3.ONE_KNOB)]
    ex_for = [exc[i] for i in np.tile(rng.permutation(len(exc)), 7)[:N_TARGETS]]
    targets = []
    for i, m in enumerate(pick):
        s = sem[m]
        while True:
            cc = {s["drive"]: int(rng.choice(one))} if s["tone"] is None else \
                {s["drive"]: int(rng.choice(dcc)), s["tone"]: int(rng.choice(tcc))}
            if (m, tuple(sorted(cc.items()))) not in old:
                break
        e = ex_for[i]
        other = [x for x in exc if x["player"] != e["player"]]
        same = [x for x in exc if x["player"] == e["player"] and x["id"] != e["id"]]
        targets.append({"id": f"w{i:02d}", "model": m, "excerpt": e["id"], "cc": cc, "drive": s["drive"], "tone": s["tone"],
                        "matcher": {"blind": other[int(rng.integers(len(other)))]["id"],
                                    "same_player": same[int(rng.integers(len(same)))]["id"], "same_di": e["id"]}})
    plan = {"version": "nodi-drive-e2e-v2", "seed": SEED, "excerpt_s": EXC_S, "lead_s": LEAD_S, "excerpts": exc, "targets": targets}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))
    tol_map()
    from collections import Counter
    print(len(exc), "excerpts;", len(targets), "targets;", len(set(t["model"] for t in targets)), "models;",
          Counter(e["player"] for e in exc), "targets per excerpt", Counter(t["excerpt"] for t in targets).most_common(3))


def load_plan() -> dict:
    return json.loads((OUT / "plan.json").read_text())


def cmd_render(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib
    import distortion_step4 as D4

    plan = load_plan()
    catalog = load_catalog()
    exc = {e["id"]: e for e in plan["excerpts"]}
    lead = int(LEAD_S * SR)
    for e in plan["excerpts"]:
        x = read_mono(ROOT / e["path"])
        write_wav(AUDIO / f"dry_{e['id']}.wav", x[e["start"]:e["start"] + e["n"]])
    engine = MB.make_engine(144_000)
    try:
        for t in plan["targets"]:
            if (AUDIO / f"{t['id']}_target.wav").exists() and "params" in t:
                continue
            guard.wait(lambda d: print(f"busy cores {d:.1f} > {guard.gate.pause_above:.1f}: pausing", flush=True), poll_s=10)
            e = exc[t["excerpt"]]
            x = read_mono(ROOT / e["path"])[e["start"] - lead:e["start"] + e["n"]]
            p, vals = D4._preset(lib, nat, catalog, t["model"], t["cc"])
            t["params"] = vals
            engine.load(p)
            y = np.asarray(engine.render(x), np.float32)[lead:]
            write_wav(AUDIO / f"{t['id']}_target.wav", y)
            print(t["id"], t["model"], vals, flush=True)
            (OUT / "plan.json").write_text(json.dumps(plan, indent=1))
    finally:
        engine.close()
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))


# ------------------------------------------------------------------------------------------- run

def job_path(t, cond) -> Path:
    return OUT / "jobs" / cond / f"{t['id']}.json"


class SilentSafe:
    """T185: Native sometimes returns silence for one setting of one model (EngineError); the search must not die on it. The
    render becomes near-silent noise, which AFx-Rep and the score rank as a very bad match. Counted in `silent`."""

    def __init__(self, inner):
        self.inner, self.silent = inner, 0

    def __getattr__(self, k):
        return getattr(self.inner, k)

    def render(self, dry):
        try:
            return self.inner.render(dry)
        except Exception as e:  # noqa: BLE001
            if "silence" not in str(e):
                raise
            self.silent += 1
            return (np.random.default_rng(0).standard_normal(len(dry)) * 1e-5).astype(np.float32)


def run_job(engine0, blocks, t, cond, plan, fams, second, members, inv, kit_args) -> dict:
    import match_block as MB
    from tonematch.search import shortlist as SL

    t0 = time.time()
    engine = SilentSafe(engine0)
    r0 = getattr(engine, "renders", 0)
    args = SimpleNamespace(target_wav=str(AUDIO / f"{t['id']}_target.wav"), dry_wav=str(AUDIO / f"dry_{t['matcher'][cond]}.wav"),
                           song=f"t185-{t['id']}")
    section, _ = MB.build_target_section(args)
    ranked = SL.rank_models(engine, blocks, section, log=lambda *_: None)
    top8 = [r["model"] for r in ranked[:8]]
    ex1 = SL.expand_families(top8, fams, set(blocks))
    ex2, added = SL.expand_second_choice(top8, ex1, second, members, set(blocks))
    by = {r["model"]: r for r in ranked}
    est, declined = {}, None
    if cond == "same_di" and inv is not None:
        est = inv(section.target, section.dry, ex2) or {}
        declined = not est
    rows = []
    for m in ex2:
        b = blocks[m]
        vals = {n: float(d) for n, lo, hi, d in b.free}
        got = {n: lo + min(max(float(p), 0.0), 1.0) * (hi - lo) for n, p in est.get(m, {}).items() for nn, lo, hi, _ in b.free if nn == n}
        vals.update(got or by[m]["best_values"])
        rows.append({"model": m, "values": vals, "screen": by[m]["distance"], "refined": None})
    kit = MB.make_kit(section, *kit_args)
    sub = {m: blocks[m] for m in ex2}
    fin = MB.final_check(engine, sub, rows, section, kit, len(rows), log=lambda *_: None)
    return {"id": t["id"], "cond": cond, "matcher_excerpt": t["matcher"][cond],
            "ranked": [{"model": r["model"], "distance": r["distance"], "best_values": r["best_values"]} for r in ranked],
            "top8": top8, "expanded_fam": ex1, "expanded_fam2": ex2, "second_added": added,
            "knob_path": "inversion" if est else "coarse_grid", "inversion_declined": declined,
            "final": [{"model": d["model"], "values": d["values"], "loss": d["loss"], "level_db": d["level_db"]} for d in fin],
            "renders_total": getattr(engine, "renders", 0) - r0, "silent_renders": engine.silent, "wall_s": time.time() - t0}


def cmd_run(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.search import shortlist as SL

    i, n = map(int, a.shard.split("/"))
    plan = load_plan()
    catalog = load_catalog()
    blocks = MB.make_blocks(MB.category_pool("drive", catalog), "drive", catalog)
    fams = SL.load_families(FAMILIES)
    second = SL.load_second_choice(FAMILIES, 0.5)
    members = SL.family_members(FAMILIES)
    inv = SL.fingerprint_inversion()
    jobs = [(t, c) for t in plan["targets"] for c in CONDS]
    jobs = [j for k, j in enumerate(jobs) if k % n == i and not job_path(*j).exists()]
    if a.reverse:                  # a second process on the same shard works from the other end; stops at the first job already done
        jobs = jobs[::-1]
    print(f"shard {a.shard}: {len(jobs)} jobs", flush=True)
    engine = MB.make_engine(144_000)
    try:
        for k, (t, c) in enumerate(jobs):
            guard.wait(lambda d: print(f"busy cores {d:.1f} > {guard.gate.pause_above:.1f}: pausing", flush=True), poll_s=15)
            if a.stop_file and Path(a.stop_file).exists():
                print("stop file present", flush=True)
                break
            if job_path(t, c).exists():
                continue
            res = run_job(engine, blocks, t, c, plan, fams, second, members, inv, (12.0, 0.5))
            p = job_path(t, c)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(res, default=float))
            print(f"[{k + 1}/{len(jobs)}] {t['id']} {c} {res['wall_s']:.0f}s renders {res['renders_total']} busy {guard.last:.1f}", flush=True)
    finally:
        engine.close()


# ------------------------------------------------------------------------------------------- analyse

def arm_order(job: dict, arm: str) -> list[dict]:
    """The arm's final models, best first: [{model, values}]."""
    rank, sl = ARMS[arm]
    allowed = job["expanded_fam2"] if sl == "fam2" else job["top8"]
    fin = {d["model"]: d for d in job["final"] if d["model"] in allowed}
    if rank == "afxrep":
        order = [r["model"] for r in job["ranked"] if r["model"] in fin]
    else:
        order = sorted(fin, key=lambda m: fin[m]["loss"])
    return [{"model": m, "values": fin[m]["values"], "loss": fin[m]["loss"]} for m in order]


def arm_renders(job: dict, arm: str) -> int:
    return GRID_RENDERS + len(job["expanded_fam2"] if ARMS[arm][1] == "fam2" else job["top8"])


def cmd_analyse(a) -> None:
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.search.knob_tol import make_knob_tol

    plan = load_plan()
    catalog = load_catalog()
    fam = json.loads(FAMILIES.read_text())["model_to_family"]
    kt = make_knob_tol(DRIVE_TOL, map_path=OUT / "tol_map.json")
    allb = MB.make_blocks(MB.category_pool("drive", catalog), "drive", catalog)
    rng = {m: {n: (lo, hi) for n, lo, hi, _ in b.free} for m, b in allb.items()}
    exc = {e["id"]: e for e in plan["excerpts"]}
    rows, preds = [], {}
    for t in plan["targets"]:
        if "params" not in t:
            continue
        ktm = kt.for_model(t["model"])
        unit = lambda n, v: (float(v) - rng[t["model"]][n][0]) / (rng[t["model"]][n][1] - rng[t["model"]][n][0])   # noqa: E731
        for c in CONDS:
            f = job_path(t, c)
            if not f.exists():
                continue
            j = json.loads(f.read_text())
            for arm in ARMS:
                ordr = arm_order(j, arm)
                names = [x["model"] for x in ordr]
                best = ordr[0]
                ok = best["model"] == t["model"]
                dtol = ttol = None
                d_ok = t_ok = None
                if ok:
                    d_ok = bool(abs(unit(t["drive"], best["values"][t["drive"]]) - unit(t["drive"], t["params"][t["drive"]])) <= DRIVE_TOL)
                    if t["tone"]:
                        pos = unit(t["tone"], t["params"][t["tone"]])
                        t_ok = bool(abs(unit(t["tone"], best["values"][t["tone"]]) - pos) <= ktm.tolerance(t["tone"], pos) + 1e-9)
                rows.append({"id": t["id"], "cond": c, "arm": arm, "model": t["model"], "family": fam.get(t["model"]),
                             "has_tone": bool(t["tone"]), "in_list": t["model"] in names,
                             "top1": ok, "top3": t["model"] in names[:3], "fam1": fam.get(best["model"]) == fam.get(t["model"]),
                             "drive_ok": d_ok, "tone_ok": t_ok, "renders": arm_renders(j, arm), "knob_path": j["knob_path"],
                             "afx_rank": ([r["model"] for r in j["ranked"]].index(t["model"]) + 1)})
                preds.setdefault(c, {}).setdefault(arm, {})[t["id"]] = {
                    "predicted": [{"model": x["model"], "values": x["values"]} for x in ordr[:3]],
                    "knob_path": j["knob_path"], "matcher_di": exc[t["matcher"][c]]["path"]}
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "rows.json").write_text(json.dumps(rows, indent=1))
    tinfo = {t["id"]: {"model": t["model"], "settings": t.get("params"), "cc": t["cc"], "drive_knob": t["drive"], "tone_knob": t["tone"],
                       "target_di": exc[t["excerpt"]]["path"], "target_di_start": exc[t["excerpt"]]["start"],
                       "target_di_n": exc[t["excerpt"]]["n"], "lead_in_samples": int(LEAD_S * SR),
                       "matcher_excerpts": {c: t["matcher"][c] for c in CONDS}} for t in plan["targets"]}
    (REPORT / "predictions.json").write_text(json.dumps(
        {"note": "T185 predictions, per condition and arm. Re-render each target's settings and each arm's top-1 on target_di. "
                 "LOCAL ONLY. Arms: prov_fam2 (current default order), afx_fam2, prov_top8, afx_top8.",
         "targets": tinfo, "predictions": preds}, indent=1))
    summ = summarise(rows)
    (REPORT / "summary.json").write_text(json.dumps(summ, indent=1))
    print_tables(summ)


def frac(rs, key, cond_key=None):
    v = [r[key] for r in rs if r[key] is not None]
    k, n = sum(bool(x) for x in v), len(v)
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "ci": [lo, hi]}


def summarise(rows: list[dict]) -> dict:
    out = {"arms": {}, "paired": {}}
    for c in CONDS:
        for arm in ARMS:
            rs = [r for r in rows if r["cond"] == c and r["arm"] == arm]
            if not rs:
                continue
            s = {k: frac(rs, k) for k in ("in_list", "top1", "top3", "fam1", "drive_ok", "tone_ok")}
            both = [bool(r["top1"] and r["drive_ok"] and (r["tone_ok"] if r["has_tone"] else True)) for r in rs]
            s["knobs_all_ok"] = {"k": sum(both), "n": len(rs), "ci": list(wilson(sum(both), len(rs)))}
            s["renders_mean"] = float(np.mean([r["renders"] for r in rs]))
            s["knob_path_inversion"] = sum(r["knob_path"] == "inversion" for r in rs)
            out["arms"][f"{c}/{arm}"] = s
        by = {arm: {r["id"]: r for r in rows if r["cond"] == c and r["arm"] == arm} for arm in ARMS}
        for name, (x, y) in {"final_rank (afx_fam2 vs prov_fam2)": ("afx_fam2", "prov_fam2"),
                             "final_rank (afx_top8 vs prov_top8)": ("afx_top8", "prov_top8"),
                             "shortlist (prov_fam2 vs prov_top8)": ("prov_fam2", "prov_top8"),
                             "shortlist (afx_fam2 vs afx_top8)": ("afx_fam2", "afx_top8")}.items():
            ids = sorted(set(by[x]) & set(by[y]))
            if not ids:
                continue
            for key in ("top1", "top3", "fam1"):
                out["paired"][f"{c} | {name} | {key}"] = mcnemar([by[x][i][key] for i in ids], [by[y][i][key] for i in ids])
    return out


def fmt(d):
    return f"{d['k']}/{d['n']} ({100 * d['k'] / d['n']:.0f}%, {100 * d['ci'][0]:.0f}-{100 * d['ci'][1]:.0f})" if d["n"] else "n/a"


def print_tables(summ: dict) -> None:
    for key, s in summ["arms"].items():
        print(key, "| in list", fmt(s["in_list"]), "| top1", fmt(s["top1"]), "| top3", fmt(s["top3"]), "| fam1", fmt(s["fam1"]),
              "| drive", fmt(s["drive_ok"]), "| tone", fmt(s["tone_ok"]), "| all knobs", fmt(s["knobs_all_ok"]), "| renders", round(s["renders_mean"]))
    for k, v in summ["paired"].items():
        print(k, v)


def cmd_clean(a) -> None:
    n = 0
    for f in AUDIO.glob("*.wav"):
        f.unlink()
        n += 1
    print(f"deleted {n} audio files")


def cmd_ledger(a) -> None:
    summ = json.loads((REPORT / "summary.json").read_text())
    base = ("T185 TEST SETTINGS, not adopted: measured tolerances, 15 families, second choice under 0.5. 96 held-out random drive "
            "targets (seed 185) from real DI. ")

    def put(metric, value, notes, renders):
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
               "--dataset", "nodi-drive-e2e-v2", "--metric", metric, "--value", f"{value:.4f}", "--notes", base + notes,
               "--renders", str(int(renders))]
        subprocess.run(cmd, check=True, cwd=ROOT, env={**os.environ, "TONEMATCH_AGENT": "search"})

    for key, s in summ["arms"].items():
        c, arm = key.split("/")
        for m in ("in_list", "top1", "top3", "fam1", "drive_ok", "tone_ok", "knobs_all_ok"):
            if s[m]["n"]:
                put(f"{c}_{arm}_{m}", s[m]["k"] / s[m]["n"], f"{s[m]['k']}/{s[m]['n']}", s["renders_mean"])
        put(f"{c}_{arm}_renders_per_match", s["renders_mean"], "mean", s["renders_mean"])


def main() -> None:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("plan")
    r = sp.add_parser("render")
    r.add_argument("--max-load", type=float, default=None,
                   help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    m = sp.add_parser("run")
    m.add_argument("--shard", required=True)
    m.add_argument("--max-load", type=float, default=None,
                   help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    m.add_argument("--stop-file")
    m.add_argument("--reverse", action="store_true")
    for n in ("analyse", "clean", "ledger"):
        sp.add_parser(n)
    a = ap.parse_args()
    {"plan": cmd_plan, "render": cmd_render, "run": cmd_run, "analyse": cmd_analyse, "clean": cmd_clean, "ledger": cmd_ledger}[a.cmd](a)


if __name__ == "__main__":
    main()
