#!/usr/bin/env python3
"""T175 (owner: tone-metrics): the different-guitar test of the drive scores.

Do `embed_afxrep`, `ltas_eqfit` and `ltas_tilt` still pick the right drive setting when the target
and the candidates are played on DIFFERENT DIs (other notes by the same player, or another player on
another guitar)? `mrstft_v2` is the control. The T161 protocol re-scoped to drive; registered in
docs/DECISIONS.md (2026-10-18, "T175: registration of the different-guitar test on drive") before
any number was computed. A measurement, not an admission: no weights change. Write-up:
docs/notes/drive-xdi-scores.md.

  plan     build targets and candidate sets -> cache/drive-xdi/plan.json
  run      features (afxrep, LTAS) of every clip and mrstft_v2 of every needed pair, in worker
           processes, one task per (DI, block of models) -> cache/drive-xdi/{features.pkl,mrstft.json}
  report   invariance ratio, rank-1, pairwise wins, Spearman agreement, by DI split and distractor
           type, bootstrap over models -> cache/drive-xdi/summary.json, tables.md; `--ledger`
           appends the registered rows (kind loss-trust, dataset helix-trust-v2-xdi-drive)

    nice -n 10 .venv/bin/python scripts/drive_xdi_scores.py run --jobs 4
    .venv/bin/python scripts/drive_xdi_scores.py report [--ledger]

Data: cache/drive-specialist/cal_renders (25 settings x 8 DIs per model; 4 DIs from Guitar-TECHS
P1, 4 from P2). Only helix-trust-v2 TRAIN drive models; no Native renders; no sealed label read.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "cache" / "drive-xdi"
DRIVE_DIR = ROOT / "cache" / "drive-specialist"
V2_MANIFEST = ROOT / "cache" / "loss-trust" / "helix-trust-v2" / "manifest.json"
SEED = 175
DATASET = "helix-trust-v2-xdi-drive"
SCORES = ("embed_afxrep", "ltas_eqfit", "ltas_tilt", "mrstft_v2")
N_TARGET_SETTINGS = 6
SAME_CLASS_MODELS, DIFF_CLASS_MODELS, SETTINGS_PER_OTHER = 2, 4, 5
SMALL_MOVE, LARGE_MOVE, SAME_SOUND_MOVE = 0.175, 0.375, 0.02
TARGET_LEVEL_DB = -23.0
SILENT_RMS = 1e-5
BOOT = 1000

# time-variant, novelty, octave or bass models (as the rating sessions; distortion-circuits.md)
EXCLUDED = frozenset({"HD2_DM4JetFuzz", "HD2_DistIndustrialFuzz", "HD2_DistThrifterFuzz", "HD2_DM4OctaveFuzz",
                      "Line6BronzeMaster"})
LEVEL_KNOBS = frozenset({"Level", "Output", "Volume", "Level 1", "Level 2", "ODLevel", "FuzzLevel"})

# docs/notes/distortion-circuits.md 1.2 / 1.4, train models only: (confusable group, family)
# family: B boost, OD overdrive, D distortion, F fuzz. Subgroups A2 and G2 count with A and G.
CIRCUIT = {
    "HD2_DistScream808": ("A", "OD"), "HD2_DistStuporOD": ("A", "OD"),
    "HD2_DistMinotaur": ("B", "OD"), "HD2_DistTeemah": ("B", "OD"),
    "HD2_DistTopSecretOD": ("C", "OD"), "HD2_DistKWB": ("C", "D"),
    "HD2_DistVerminDist": ("D", "D"),
    "HD2_DistDeezOneVintage": ("E", "D"), "HD2_DistDeezOneMod": ("E", "D"), "HD2_DistHedgehogD9": ("E", "D"),
    "HD2_DistSwedishChainsaw": ("F", "D"), "KillerZ": ("F", "D"),
    "HD2_DistTriangleFuzz": ("G", "F"), "HD2_DistRamsHead": ("G", "F"), "HD2_DistBallisticFuzz": ("G", "F"),
    "HD2_DistArbitratorFuzz": ("H", "F"), "HD2_DM4FacialFuzz": ("H", "F"),
    "HD2_DM4JumboFuzz": ("I", "F"), "HD2_DistWringerFuzz": ("I", "F"),
    "HD2_DistKinkyBoost": ("K", "B"), "HD2_DistVitalBoost": ("K", "B"), "HD2_DistDerangedMaster": ("K", "B"),
    "HD2_DistValveDriver": ("N", "OD"), "HD2_DM4TubeDrive": ("N", "OD"),
    "HD2_DistCompulsiveDrive": ("P", "OD"),
}
DTYPES = ("knob_small", "knob_medium", "knob_large", "twin", "same_class", "diff_class")
CONDS = ("same_player", "other_player")


# ------------------------------------------------------------------------------------------- plan

def player(path: str) -> str:
    for part in str(path).split("/"):
        if len(part) >= 2 and part[0] == "P" and part[1].isdigit():
            return part[:2]
    return "?"


def knob_move(knobs: dict, ref: dict) -> float:
    """Largest knob move as a fraction of range (CC/127), level knobs left out."""
    keys = (set(knobs) | set(ref)) - LEVEL_KNOBS
    return max((abs(float(knobs.get(k, ref.get(k, 0))) - float(ref.get(k, knobs.get(k, 0)))) / 127.0
                for k in keys), default=0.0)


def knob_type(move: float) -> str | None:
    if move < SAME_SOUND_MOVE:
        return None
    return "knob_small" if move <= SMALL_MOVE else "knob_medium" if move <= LARGE_MOVE else "knob_large"


def train_models(cal_models) -> list[str]:
    man = json.loads(V2_MANIFEST.read_text())
    train = set(man["splits"]["train"]["models"]["drive"])
    return sorted((train & set(cal_models)) - EXCLUDED)


def build_plan(cal: dict, models: list[str], seed: int = SEED) -> dict:
    """Targets (model, setting, DI) and per-target candidate lists [model, setting, type]."""
    missing = [m for m in models if m not in CIRCUIT]
    if missing:
        raise ValueError(f"no circuit group for {missing}")
    rng = np.random.default_rng(seed)
    pl = [player(d["path"]) for d in cal["dis"]]
    nd = len(pl)
    targets = []
    for mi, m in enumerate(models):
        sets = cal["models"][m]["settings"]
        grp, fam = CIRCUIT[m]
        twins = [o for o in models if o != m and CIRCUIT[o][0] == grp]
        same_cls = [o for o in models if CIRCUIT[o][1] == fam and CIRCUIT[o][0] != grp]
        diff_cls = [o for o in models if CIRCUIT[o][1] != fam]
        for k, s in enumerate(int(v) for v in rng.choice(len(sets), N_TARGET_SETTINGS, replace=False)):
            cands = [[m, s, "true"]]
            for j in range(len(sets)):
                if j != s:
                    t = knob_type(knob_move(sets[j], sets[s]))
                    if t:
                        cands.append([m, j, t])
            for o in twins:
                cands += [[o, j, "twin"] for j in range(len(cal["models"][o]["settings"]))]
            for pool, n, t in ((same_cls, SAME_CLASS_MODELS, "same_class"), (diff_cls, DIFF_CLASS_MODELS, "diff_class")):
                for o in rng.choice(pool, min(n, len(pool)), replace=False) if pool else []:
                    ns = len(cal["models"][str(o)]["settings"])
                    cands += [[str(o), int(j), t] for j in rng.choice(ns, SETTINGS_PER_OTHER, replace=False)]
            targets.append({"tid": f"{m}-s{s:02d}", "model": m, "setting": s, "di": (mi * N_TARGET_SETTINGS + k) % nd,
                            "group": grp, "family": fam, "cands": cands})
    return {"seed": seed, "models": models, "players": pl, "n_dis": nd, "targets": targets,
            "registration": "docs/DECISIONS.md 2026-10-18 (tone-metrics, T175) registration"}


def di_condition(pl: list[str], a: int, x: int) -> str:
    return "same_di" if a == x else "same_player" if pl[a] == pl[x] else "other_player"


# -------------------------------------------------------------------------------------------- run

def clip_path(m: str, j: int, d: int) -> Path:
    return DRIVE_DIR / "cal_renders" / m / f"s{j:02d}_d{d}.npy"


def active_level_db(x, frame: int = 2048, span_db: float = 40.0) -> float:
    """As `match_song.LevelGuardKit.active_level_db` (Ruling 3)."""
    x = np.asarray(x, np.float64).ravel()
    n = len(x) // frame
    if n < 2:
        return float(20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0, 1e-12)))
    p = (x[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
    act = p >= p.max() * 10 ** (-span_db / 10)
    return float(10 * np.log10(max(float(p[act].mean()), 1e-24)))


def load_clip(m: str, j: int, d: int) -> np.ndarray | None:
    x = np.asarray(np.load(clip_path(m, j, d)), np.float32)
    x = x.mean(axis=1).astype(np.float32) if x.ndim == 2 else x
    if float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) < SILENT_RMS:
        return None
    return (x * 10 ** ((TARGET_LEVEL_DB - active_level_db(x)) / 20)).astype(np.float32)


_W: dict = {}


def _init(plan: dict) -> None:
    os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
    import torch
    torch.set_num_threads(1)
    from tonematch.metrics import afxrep
    afxrep.set_embedder(afxrep.Embedder(device="cpu", threads=1))
    _W["targets"] = {t["tid"]: load_clip(t["model"], t["setting"], t["di"]) for t in plan["targets"]}
    need: dict = {}
    for t in plan["targets"]:
        for m, j, _ in t["cands"]:
            need.setdefault((m, j), []).append(t["tid"])
    _W["need"] = need


def process(task: tuple) -> tuple[dict, dict]:
    """task = (DI x, [(model, setting), ...]) -> (features by 'm|j|x', mrstft by 'tid|m|j|x')."""
    from tonematch.metrics import afxrep, ltas, mrstft_v2
    x_di, clips = task
    feats, mr = {}, {}
    for m, j in clips:
        k = f"{m}|{j}|{x_di}"
        c = load_clip(m, j, x_di)
        if c is None:
            feats[k] = None
            continue
        fa, fl = afxrep.features(c), ltas.features(c)
        feats[k] = {"afx": fa.mid.astype(np.float32), "bands": fl.bands_db.astype(np.float64), "tilt": fl.tilt_db_oct}
        for tid in _W["need"].get((m, j), []):
            t = _W["targets"][tid]
            if t is not None:
                mr[f"{tid}|{k}"] = round(float(mrstft_v2.term(t, c)), 6)
    return feats, mr


def proc_check() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], check=False)


def cmd_plan(a) -> dict:
    cal = json.loads((DRIVE_DIR / "cal_plan.json").read_text())
    plan = build_plan(cal, train_models(cal["models"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps(plan))
    nc = [len(t["cands"]) for t in plan["targets"]]
    by = {t: sum(sum(c[2] == t for c in tt["cands"]) for tt in plan["targets"]) / len(plan["targets"]) for t in DTYPES}
    print(f"{len(plan['models'])} models, {len(plan['targets'])} targets, candidates per target "
          f"{min(nc)}-{max(nc)} (mean {np.mean(nc):.1f}); mean per type " +
          ", ".join(f"{t} {v:.1f}" for t, v in by.items()), flush=True)
    return plan


def cmd_run(a) -> int:
    proc_check()
    plan = cmd_plan(a)
    jobs = max(1, min(a.jobs, 4))
    cal = json.loads((DRIVE_DIR / "cal_plan.json").read_text())
    needed = sorted({(m, j) for t in plan["targets"] for m, j, _ in t["cands"]})
    blocks = [needed[i::a.blocks] for i in range(a.blocks)]
    tasks = [(x, b) for x in range(plan["n_dis"]) for b in blocks]
    missing = [str(clip_path(m, j, x)) for x, b in tasks for m, j in b if not clip_path(m, j, x).is_file()]
    if missing:
        print(f"{len(missing)} cached clips missing, e.g. {missing[:3]}", file=sys.stderr)
        return 2
    del cal
    print(f"{len(needed)} presets x {plan['n_dis']} DIs = {len(needed) * plan['n_dis']} clips, {len(tasks)} tasks, "
          f"{jobs} workers", flush=True)
    t0 = time.time()
    feats, mr = {}, {}
    with ProcessPoolExecutor(jobs, initializer=_init, initargs=(plan,)) as ex:
        for i, (f, m) in enumerate(ex.map(process, tasks)):
            feats.update(f)
            mr.update(m)
            print(f"  {i + 1}/{len(tasks)} tasks, {time.time() - t0:.0f} s", flush=True)
    with open(OUT / "features.pkl", "wb") as fh:
        pickle.dump(feats, fh)
    (OUT / "mrstft.json").write_text(json.dumps(mr))
    meta = {"seconds": round(time.time() - t0, 1), "clips": len(feats), "pairs": len(mr), "jobs": jobs}
    (OUT / "run_meta.json").write_text(json.dumps(meta))
    print(f"done: {meta}", flush=True)
    proc_check()
    return 0


# ----------------------------------------------------------------------------------------- report

class Dist:
    """d(target, candidate on DI x) for every score, from stored features and mrstft pairs."""

    def __init__(self, feats: dict, mr: dict):
        from tonematch.metrics import ltas
        from tonematch.metrics import nodi_candidates as N
        self.f, self.mr, self.L, self.N = feats, mr, ltas, N

    def lf(self, f):
        return self.L.LtasFeatures(bands_db=np.asarray(f["bands"]), tilt_db_oct=float(f["tilt"]))

    def __call__(self, score: str, t: dict, m: str, j: int, x: int) -> float:
        kt, kc = f"{t['model']}|{t['setting']}|{t['di']}", f"{m}|{j}|{x}"
        ft, fc = self.f.get(kt), self.f.get(kc)
        if ft is None or fc is None:
            return float("nan")
        if score == "embed_afxrep":
            return max(0.0, 1.0 - float(np.dot(ft["afx"].astype(np.float64), fc["afx"].astype(np.float64))))
        if score == "ltas_tilt":
            return float(self.L.distance(self.lf(ft), self.lf(fc)))
        if score == "ltas_eqfit":
            return float(self.N.eqfit(self.lf(ft), self.lf(fc))[0])
        if score == "mrstft_v2":
            return float(self.mr.get(f"{t['tid']}|{kc}", float("nan")))
        raise KeyError(score)


def spearman(a, b) -> float:
    from scipy.stats import spearmanr
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3:
        return float("nan")
    r = spearmanr(a[ok], b[ok]).statistic
    return float(r) if np.isfinite(r) else float("nan")


def records(target: dict, D: dict[int, np.ndarray], pl: list[str]) -> list[dict]:
    """Per other DI x: rank-1, wins and invariance ratio by type. `D[x]` = distances of the target's
    candidates (truth first) on DI x."""
    types = [c[2] for c in target["cands"]]
    a = target["di"]
    dA = D[a]
    out = []
    for x, dx in D.items():
        if x == a or not np.isfinite(dx[0]):
            continue
        dT = float(dx[0])
        rest = dx[1:]
        fin = np.isfinite(rest)
        rec = {"tid": target["tid"], "model": target["model"], "family": target["family"], "x": x,
               "cond": di_condition(pl, a, x), "n_cand": int(fin.sum()) + 1,
               "rank1": bool(np.all(rest[fin] > dT)), "rank1_or_small": False, "first_type": "true",
               "wins": {}, "rank1_type": {}, "r": {}}
        i_best = int(np.nanargmin(dx))
        if not rec["rank1"]:
            cand = [i for i in range(1, len(dx)) if np.isfinite(dx[i]) and dx[i] <= dT]
            i_best = min(cand, key=lambda i: dx[i]) if cand else i_best
            rec["first_type"] = types[i_best]
        rec["rank1_or_small"] = rec["rank1"] or rec["first_type"] == "knob_small"
        for t in DTYPES:
            idx = [i for i in range(1, len(dx)) if types[i] == t and np.isfinite(dx[i])]
            if not idx:
                continue
            v = dx[idx]
            rec["wins"][t] = [int(np.sum(v > dT)), len(idx)]
            rec["rank1_type"][t] = bool(np.all(v > dT))
            va = dA[[i for i in idx if np.isfinite(dA[i])]]
            if va.size and np.median(va) > 0:
                rec["r"][t] = dT / float(np.median(va))
        out.append(rec)
    return out


def spearmans(target: dict, D: dict[int, np.ndarray], pl: list[str]) -> list[dict]:
    a = target["di"]
    xs = [x for x in D if x != a]
    out = [{"model": target["model"], "kind": "vs_own_di|" + di_condition(pl, a, x), "rho": spearman(D[a], D[x])}
           for x in xs]
    for i, x in enumerate(xs):
        for y in xs[i + 1:]:
            cx, cy = di_condition(pl, a, x), di_condition(pl, a, y)
            kind = "between|" + (cx if cx == cy else "mixed")
            out.append({"model": target["model"], "kind": kind, "rho": spearman(D[x], D[y])})
    return out


def summarise(recs: list[dict], sps: list[dict], models: list[str], boot: int = BOOT, seed: int = SEED) -> dict:
    """Statistics per condition, 95% bootstrap over models."""
    def stats(rs, ss):
        s = {}
        if rs:
            s["n"] = len(rs)
            s["rank1"] = float(np.mean([r["rank1"] for r in rs]))
            s["rank1_or_small"] = float(np.mean([r["rank1_or_small"] for r in rs]))
            s["chance"] = float(np.mean([1.0 / r["n_cand"] for r in rs]))
            w = [r["wins"][t] for r in rs for t in r["wins"]]
            s["win_all"] = sum(k for k, _ in w) / max(1, sum(n for _, n in w))
            for t in DTYPES:
                wt = [r["wins"][t] for r in rs if t in r["wins"]]
                if wt:
                    s[f"win_{t}"] = sum(k for k, _ in wt) / sum(n for _, n in wt)
                    s[f"rank1_{t}"] = float(np.mean([r["rank1_type"][t] for r in rs if t in r["rank1_type"]]))
                rt = [r["r"][t] for r in rs if t in r["r"]]
                if rt:
                    s[f"R_{t}"] = float(np.median(rt))
                    s[f"Rlt1_{t}"] = float(np.mean(np.asarray(rt) < 1))
            ft = {}
            for r in rs:
                if not r["rank1"]:
                    ft[r["first_type"]] = ft.get(r["first_type"], 0) + 1
            s["first_type_when_wrong"] = ft
        for k in sorted({q["kind"] for q in ss}):
            v = [q["rho"] for q in ss if q["kind"] == k and np.isfinite(q["rho"])]
            if v:
                s[f"rho_{k}"] = float(np.mean(v))
        return s

    by_model_r = {m: [r for r in recs if r["model"] == m] for m in models}
    by_model_s = {m: [q for q in sps if q["model"] == m] for m in models}
    out = {}
    rng = np.random.default_rng(seed)
    draws = [rng.integers(len(models), size=len(models)) for _ in range(boot)]
    for cond in CONDS:
        sel = lambda rs: [r for r in rs if r["cond"] == cond]  # noqa: E731
        base = stats(sel(recs), [])
        keys = [k for k, v in base.items() if isinstance(v, float)]
        bs = {k: [] for k in keys}
        for d in draws:
            rr = [r for i in d for r in sel(by_model_r[models[i]])]
            st = stats(rr, [])
            for k in keys:
                if k in st:
                    bs[k].append(st[k])
        base["ci"] = {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in bs.items() if v}
        out[cond] = base
    # rho is reported once (its kinds already carry the condition), bootstrapped over models
    rho = stats([], sps)
    rho_ci = {}
    for k in rho:
        vals = []
        for d in draws:
            v = [q["rho"] for i in d for q in by_model_s[models[i]] if q["kind"] == k[4:] and np.isfinite(q["rho"])]
            if v:
                vals.append(float(np.mean(v)))
        rho_ci[k] = [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]
    out["spearman"] = {k: {"mean": v, "ci": rho_ci[k]} for k, v in rho.items()}
    out["by_family"] = {}
    for fam in ("B", "OD", "D", "F"):
        for cond in CONDS:
            rs = [r for r in recs if r["family"] == fam and r["cond"] == cond]
            if rs:
                out["by_family"][f"{fam}|{cond}"] = {"n": len(rs), "rank1": float(np.mean([r["rank1"] for r in rs])),
                                                     "win_all": stats(rs, [])["win_all"]}
    return out


def cmd_report(a) -> int:
    plan = json.loads((OUT / "plan.json").read_text())
    with open(OUT / "features.pkl", "rb") as fh:
        feats = pickle.load(fh)
    mr = json.loads((OUT / "mrstft.json").read_text())
    meta = json.loads((OUT / "run_meta.json").read_text()) if (OUT / "run_meta.json").is_file() else {}
    dist = Dist(feats, mr)
    pl, nd = plan["players"], plan["n_dis"]
    summ = {"meta": {**meta, "models": len(plan["models"]), "targets": len(plan["targets"]), "players": pl},
            "scores": {}}
    for s in SCORES:
        recs, sps = [], []
        for t in plan["targets"]:
            D = {x: np.array([dist(s, t, m, j, x) for m, j, _ in t["cands"]]) for x in range(nd)}
            recs += records(t, D, pl)
            sps += spearmans(t, D, pl)
        summ["scores"][s] = summarise(recs, sps, plan["models"])
        print(f"{s}: done", flush=True)
    (OUT / "summary.json").write_text(json.dumps(summ, indent=1))
    md = tables_md(summ)
    (OUT / "tables.md").write_text(md)
    print(md)
    if a.ledger:
        write_ledger(summ)
    return 0


def fmt(v, nd=2) -> str:
    return "-" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{nd}f}"


def fci(s: dict, k: str, nd=2) -> str:
    ci = s.get("ci", {}).get(k)
    return fmt(s.get(k), nd) + (f" ({fmt(ci[0], nd)}-{fmt(ci[1], nd)})" if ci else "")


def tables_md(summ: dict) -> str:
    L = []
    sc = summ["scores"]
    for cond in CONDS:
        L.append(f"\n### {cond}: rank-1 among all candidates, pairwise win rate (95% CI over models)\n")
        L.append("| score | rank-1 | rank-1 or small knob | chance | pairwise win | n |")
        L.append("|---|---|---|---|---|---|")
        for s in SCORES:
            c = sc[s][cond]
            L.append(f"| {s} | {fci(c, 'rank1')} | {fci(c, 'rank1_or_small')} | {fmt(c.get('chance'), 3)} | "
                     f"{fci(c, 'win_all')} | {c.get('n')} |")
        for title, pre in (("invariance ratio R (median r; below 1 = the guitar change moves the score less "
                            "than this preset change)", "R_"),
                           ("share of r below 1", "Rlt1_"),
                           ("pairwise win rate by distractor type", "win_"),
                           ("rank-1 within the distractor type (truth beats every candidate of the type)", "rank1_")):
            L.append(f"\n#### {cond}: {title}\n")
            L.append("| score | " + " | ".join(DTYPES) + " |")
            L.append("|---|" + "---|" * len(DTYPES))
            for s in SCORES:
                c = sc[s][cond]
                L.append(f"| {s} | " + " | ".join(fci(c, pre + t) for t in DTYPES) + " |")
        L.append(f"\n#### {cond}: type of the candidate ranked first when the truth is not\n")
        L.append("| score | " + " | ".join(DTYPES) + " |")
        L.append("|---|" + "---|" * len(DTYPES))
        for s in SCORES:
            ft = sc[s][cond].get("first_type_when_wrong", {})
            L.append(f"| {s} | " + " | ".join(str(ft.get(t, 0)) for t in DTYPES) + " |")
    L.append("\n### Spearman ordering agreement (mean over targets and DI pairs; 95% CI over models)\n")
    kinds = sorted({k for s in SCORES for k in sc[s]["spearman"]})
    L.append("| score | " + " | ".join(k[4:] for k in kinds) + " |")
    L.append("|---|" + "---|" * len(kinds))
    for s in SCORES:
        sp = sc[s]["spearman"]
        L.append(f"| {s} | " + " | ".join(f"{fmt(sp[k]['mean'])} ({fmt(sp[k]['ci'][0])}-{fmt(sp[k]['ci'][1])})"
                                          if k in sp else "-" for k in kinds) + " |")
    L.append("\n### By target family: rank-1 / pairwise win\n")
    keys = sorted({k for s in SCORES for k in sc[s]["by_family"]})
    L.append("| score | " + " | ".join(keys) + " |")
    L.append("|---|" + "---|" * len(keys))
    for s in SCORES:
        bf = sc[s]["by_family"]
        L.append(f"| {s} | " + " | ".join(f"{fmt(bf[k]['rank1'])} / {fmt(bf[k]['win_all'])}" if k in bf else "-"
                                          for k in keys) + " |")
    L.append("| n | " + " | ".join(str(sc[SCORES[0]]["by_family"].get(k, {}).get("n", "-")) for k in keys) + " |")
    return "\n".join(L) + "\n"


def write_ledger(summ: dict) -> None:
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}
    meta = summ["meta"]
    for s in SCORES:
        o, sp = summ["scores"][s]["other_player"], summ["scores"][s]["same_player"]
        base = (f"T175 different-guitar test on drive (measurement; admits nothing); score {s}; other player and guitar "
                f"(Guitar-TECHS P1 vs P2); {meta['models']} train drive models; {meta['targets']} targets x 4 DIs; "
                f"cached drive-specialist renders; registration docs/DECISIONS.md 2026-10-18 T175; "
                f"docs/notes/drive-xdi-scores.md")
        rows = (("rank1_rate", "rank1", f"chance {o['chance']:.3f}; same player {sp['rank1']:.3f}"),
                ("pairwise_win", "win_all", f"same player {sp['win_all']:.3f}"),
                ("invariance_ratio_twin", "R_twin", f"same player {sp.get('R_twin', float('nan')):.3f}"))
        for metric, k, extra in rows:
            ci = o["ci"].get(k, [float("nan")] * 2)
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                            "--engine", "native", "--dataset", DATASET, "--metric", f"{metric}_{s}",
                            "--value", f"{o[k]:.4f}", "--renders", "0",
                            "--wall-clock-s", str(meta.get("seconds", "")),
                            "--notes", f"{base}; 95% CI over models {ci[0]:.3f}-{ci[1]:.3f}; {extra}"],
                           check=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=int, default=4, help="worker processes (capped at 4)")
    r.add_argument("--blocks", type=int, default=4, help="preset blocks per DI (tasks = DIs x blocks)")
    p = sub.add_parser("report")
    p.add_argument("--ledger", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        cmd_plan(a)
        return 0
    return cmd_run(a) if a.cmd == "run" else cmd_report(a)


if __name__ == "__main__":
    sys.exit(main())
