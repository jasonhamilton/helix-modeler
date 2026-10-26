#!/usr/bin/env python3
"""T221 (owner: tone-metrics): the DI-invariance ratio check, experiment 3 of
docs/notes/research-dry-signal-2026-10-23.md section 7. Measurement only; admits nothing.

Question: can a score rank candidate settings when the target was played on one dry recording (DI A)
and the candidates are rendered through another (DI B)? For every score and every (target, other DI):

  r    = d(P on A, P on B) / median over distractors Q of d(P on A, Q on A)
         (R = median of r; at or above 1 the DI change moves the score as much as a wrong setting)
  rho  = Spearman correlation of the candidate order against the target, candidates on A vs on B
         (near 0: the order the search would follow on B says nothing about A)

Scores: embed_afxrep (mid vector, 1 - cosine as in T175), ltas_tilt, envelope, crest_flatness_hnr,
mrstft_v2 (the known-DI reference). Conditions: same_player (another recording or passage of the
same player and guitar) and other_player (another player on another guitar).

Sources (cached renders only; helix-trust-v2 TRAIN models only, as the T161/T175 registrations):
  DRIVE  the T175 plan (cache/drive-xdi): 150 drive targets x ~86 candidates x 8 Guitar-TECHS DIs
         (P1, P2). AFx-Rep, LTAS and mrstft_v2 are read from the T175 cache; envelope and crest
         features are computed here on the same level-matched clips.
  V1     helix-trust-v1 items whose model is not a v2 test model (28 items, 20 candidates each)
         on DI A (target), B (another recording, 21 other-player / 7 same-player) and C (another
         passage of A's recording).
  MOD    mod-specialist calibration renders: 5 train models x 41 settings x 2 passages of one
         P3 recording (same player only); 10 targets per model, 61 candidates.
Not usable: time-specialist renders (one knob setting per file, no same-setting pair across DIs),
T185 / T196 (no audio kept).

    nice -n 10 .venv/bin/python scripts/di_invariance_check.py run --jobs 3
    .venv/bin/python scripts/di_invariance_check.py report [--ledger]
"""
from __future__ import annotations

import argparse
import json
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

import drive_xdi_scores as X  # noqa: E402  (T175 plan, clip loader, level match)

OUT = ROOT / "cache" / "di-invariance-check"
XDI = ROOT / "cache" / "drive-xdi"
V1_DIR = ROOT / "cache" / "loss-trust" / "helix-trust-v1"
V2_MANIFEST = ROOT / "cache" / "loss-trust" / "helix-trust-v2" / "manifest.json"
MOD_DIR = ROOT / "cache" / "mod-specialist"
SEED = 221
DATASET = "di-invariance-check"
SCORES = ("embed_afxrep", "ltas_tilt", "envelope", "crest_flatness_hnr", "mrstft_v2")
CONDS = ("same_player", "other_player")
SOURCES = ("DRIVE", "V1", "MOD")
MOD_SETTINGS, MOD_OTHER_MODELS, MOD_OTHER_SETTINGS = 10, 4, 5
BOOT = 1000
KNOB_TYPES = ("knob_small", "knob_medium", "knob_large")


# ----------------------------------------------------------------------------------------- helpers

def v2_test_only_models() -> set[str]:
    m = json.loads(V2_MANIFEST.read_text())
    test = {x for c in m["splits"]["test"]["models"].values() for x in c}
    train = {x for c in m["splits"]["train"]["models"].values() for x in c}
    return test - train


def v2_train_models(family: str) -> list[str]:
    return json.loads(V2_MANIFEST.read_text())["splits"]["train"]["models"][family]


def level_match(x: np.ndarray) -> np.ndarray | None:
    """Mono float32 at the T175 loudness (-23 dB active level); None if silent."""
    x = np.asarray(x, np.float32)
    x = x.mean(axis=1).astype(np.float32) if x.ndim == 2 else x
    if float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) < X.SILENT_RMS:
        return None
    return (x * 10 ** ((X.TARGET_LEVEL_DB - X.active_level_db(x)) / 20)).astype(np.float32)


def load_any(path: str) -> np.ndarray | None:
    p = ROOT / path
    if p.suffix == ".npy":
        return level_match(np.load(p))
    import soundfile as sf
    x, _ = sf.read(str(p), dtype="float32")
    return level_match(x)


def v1_label_type(label: str, knobs: dict | None, true_knobs: dict | None) -> str:
    """Candidate type of a helix-trust-v1 candidate: knob moves of the true model are binned as in
    T175 (level knobs ignored); `nbr` (same family, other model) and `random` (other family)."""
    if label == "true":
        return "true"
    if label.startswith("near"):
        return X.knob_type(X.knob_move(knobs or {}, true_knobs or {})) or "knob_small"
    if label.startswith("nbr"):
        return "same_class"
    if label.startswith("random"):
        return "diff_class"
    raise ValueError(label)


def cond_of(player_a: str, player_x: str) -> str:
    return "same_player" if player_a == player_x else "other_player"


# ------------------------------------------------------------------------------------------ groups
# A group: one target P on DI a, the same candidate list (truth first) on every DI, each DI with a
# condition. spec = path of the candidate's clip on that DI.

def groups_v1() -> list[dict]:
    man = json.loads((V1_DIR / "manifest.json").read_text())
    test = v2_test_only_models()
    out = []
    for it in man["items"]:
        if it["true_model"] in test:
            continue
        ii = it["item"]
        rd = f"cache/loss-trust/helix-trust-v1/renders/item{ii:02d}"
        tk = it["candidates"][0].get("knobs", {})
        types = [v1_label_type(c["label"], c.get("knobs"), tk) for c in it["candidates"]]
        pa = X.player(it["di"]["A"]["path"])
        dis = {"A": {"cond": "own", "clips": [f"{rd}/c{j:02d}_A.npy" for j in range(len(types))]}}
        for key, name in (("B", "B"), ("C", "C")):
            dis[name] = {"cond": cond_of(pa, X.player(it["di"][key]["path"])),
                         "clips": [f"{rd}/c{j:02d}_{key}.npy" for j in range(len(types))]}
        out.append({"gid": f"V1-{ii:02d}", "source": "V1", "family": it["category"].lower(),
                    "model": it["true_model"], "target_di": "A", "types": types, "dis": dis})
    return out


def groups_mod() -> list[dict]:
    plan = json.loads((MOD_DIR / "cal_plan.json").read_text())
    models = sorted(set(v2_train_models("mod")) & set(plan["models"]))
    rng = np.random.default_rng([SEED, 2])
    out = []

    def clip(m, j, d):
        return f"cache/mod-specialist/cal_renders/{m}/s{j:02d}_d{d}.npy"

    for m in models:
        sets = plan["models"][m]["settings"]
        ns = len(sets)
        for s in (int(v) for v in rng.choice(ns, MOD_SETTINGS, replace=False)):
            cands = [(m, s, "true")]
            for j in range(ns):
                if j != s:
                    t = X.knob_type(X.knob_move(sets[j], sets[s]))
                    if t:
                        cands.append((m, j, t))
            others = [o for o in models if o != m][:MOD_OTHER_MODELS]
            for o in others:
                no = len(plan["models"][o]["settings"])
                cands += [(o, int(j), "same_class") for j in rng.choice(no, MOD_OTHER_SETTINGS, replace=False)]
            for a in (0, 1):
                dis = {str(d): {"cond": "own" if d == a else "same_player",
                                "clips": [clip(cm, cj, d) for cm, cj, _ in cands]} for d in (0, 1)}
                out.append({"gid": f"MOD-{m}-s{s:02d}-d{a}", "source": "MOD", "family": "mod", "model": m,
                            "target_di": str(a), "types": [c[2] for c in cands], "dis": dis})
    return out


def groups_drive() -> list[dict]:
    """The T175 plan as groups; clip keys are the T175 feature keys 'model|setting|di'."""
    plan = json.loads((XDI / "plan.json").read_text())
    pl = plan["players"]
    out = []
    for t in plan["targets"]:
        a = t["di"]
        types = [c[2] for c in t["cands"]]
        dis = {str(x): {"cond": "own" if x == a else cond_of(pl[a], pl[x]),
                        "clips": [f"{m}|{j}|{x}" for m, j, _ in t["cands"]]} for x in range(plan["n_dis"])}
        out.append({"gid": f"DRIVE-{t['tid']}", "source": "DRIVE", "family": "drive", "model": t["model"],
                    "target_di": str(a), "types": types, "dis": dis, "tid": t["tid"]})
    return out


# ------------------------------------------------------------------------------------------ workers

_W: dict = {}


def _init(with_afx: bool) -> None:
    os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
    try:
        import torch
        torch.set_num_threads(1)
    except Exception:  # noqa: BLE001
        pass
    if with_afx:
        from tonematch.metrics import afxrep
        afxrep.set_embedder(afxrep.Embedder(device="cpu", threads=1))
    _W["with_afx"] = with_afx


def features_of(x: np.ndarray, with_afx: bool) -> dict:
    from tonematch.metrics import crest, envelope, ltas
    f = {"ltas": ltas.features(x), "env": envelope.features(x), "crest": crest.features(x)}
    if with_afx:
        from tonematch.metrics import afxrep
        f["afx"] = afxrep.features(x).mid.astype(np.float32)
    return f


def feature_task(task: tuple) -> dict:
    """(loader, [clip keys]) -> {key: features or None}. loader 'drive' = T175 clip key, 'path'."""
    loader, keys = task
    out = {}
    for k in keys:
        if loader == "drive":
            m, j, d = k.split("|")
            x = X.load_clip(m, int(j), int(d))
        else:
            x = load_any(k)
        out[k] = None if x is None else features_of(x, _W["with_afx"])
    return out


def mrstft_task(task: tuple) -> dict:
    """(loader, target key, [candidate keys]) -> {'t|c': mrstft_v2}."""
    from tonematch.metrics import mrstft_v2
    loader, tk, cks = task
    load = (lambda k: X.load_clip(*[f(v) for f, v in zip((str, int, int), k.split("|"))])) if loader == "drive" \
        else load_any
    t = load(tk)
    out = {}
    for c in cks:
        x = load(c)
        out[f"{tk}|{c}"] = None if (t is None or x is None) else round(float(mrstft_v2.term(t, x)), 6)
    return out


def distance(score: str, ft: dict, fc: dict) -> float:
    from tonematch.metrics import crest, envelope, ltas
    if ft is None or fc is None:
        return float("nan")
    if score == "embed_afxrep":
        return max(0.0, 1.0 - float(np.dot(ft["afx"].astype(np.float64), fc["afx"].astype(np.float64))))
    if score == "ltas_tilt":
        return float(ltas.distance(ft["ltas"], fc["ltas"]))
    if score == "envelope":
        return float(envelope.distance(ft["env"], fc["env"]))
    if score == "crest_flatness_hnr":
        return float(crest.distance(ft["crest"], fc["crest"]))
    raise KeyError(score)


def proc_check() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], check=False)


def chunks(seq: list, n: int) -> list[list]:
    n = max(1, n)
    return [seq[i::n] for i in range(n) if seq[i::n]]


# ---------------------------------------------------------------------------------------------- run

def drive_features() -> tuple[dict, dict]:
    """T175 features (afx mid, LTAS) re-keyed to this script's layout, plus its mrstft pairs
    keyed 'target clip|candidate clip'."""
    from tonematch.metrics import ltas
    with open(XDI / "features.pkl", "rb") as fh:
        raw = pickle.load(fh)
    feats = {k: None if v is None else {"afx": v["afx"],
                                        "ltas": ltas.LtasFeatures(bands_db=np.asarray(v["bands"]),
                                                                  tilt_db_oct=float(v["tilt"]))}
             for k, v in raw.items()}
    plan = json.loads((XDI / "plan.json").read_text())
    tkey = {t["tid"]: f"{t['model']}|{t['setting']}|{t['di']}" for t in plan["targets"]}
    mr = {}
    for k, v in json.loads((XDI / "mrstft.json").read_text()).items():
        tid, m, j, x = k.split("|")
        mr[f"{tkey[tid]}|{m}|{j}|{x}"] = v
    return feats, mr


def cmd_run(a) -> int:
    proc_check()
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(a.jobs, 4))
    sources = a.sources or list(SOURCES)
    groups = []
    if "DRIVE" in sources:
        groups += groups_drive()
    if "V1" in sources:
        groups += groups_v1()
    if "MOD" in sources:
        groups += groups_mod()
    t0 = time.time()
    print(f"{len(groups)} groups from {sources}; {jobs} workers", flush=True)
    feats: dict = {}
    mr: dict = {}
    # DRIVE: T175 cache + envelope/crest computed here
    dkeys = sorted({c for g in groups if g["source"] == "DRIVE" for d in g["dis"].values() for c in d["clips"]})
    if dkeys:
        df, dm = drive_features()
        mr.update(dm)
        missing = [k for k in dkeys if k not in df]
        if missing:
            print(f"{len(missing)} T175 features missing, e.g. {missing[:3]}", file=sys.stderr)
            return 2
        with ProcessPoolExecutor(jobs, initializer=_init, initargs=(False,)) as ex:
            for i, part in enumerate(ex.map(feature_task, [("drive", c) for c in chunks(dkeys, jobs * 8)])):
                for k, v in part.items():
                    feats[k] = None if (v is None or df[k] is None) else {**df[k], "env": v["env"], "crest": v["crest"]}
                print(f"  DRIVE env/crest {i + 1}/{jobs * 8}, {time.time() - t0:.0f} s", flush=True)
    # V1 and MOD: everything computed here
    pkeys = sorted({c for g in groups if g["source"] != "DRIVE" for d in g["dis"].values() for c in d["clips"]})
    if pkeys:
        missing = [k for k in pkeys if not (ROOT / k).is_file()]
        if missing:
            print(f"{len(missing)} cached clips missing, e.g. {missing[:3]}", file=sys.stderr)
            return 2
        with ProcessPoolExecutor(jobs, initializer=_init, initargs=(True,)) as ex:
            for i, part in enumerate(ex.map(feature_task, [("path", c) for c in chunks(pkeys, jobs * 8)])):
                feats.update(part)
                print(f"  V1/MOD features {i + 1}/{jobs * 8}, {time.time() - t0:.0f} s", flush=True)
        tasks = []
        for g in groups:
            if g["source"] == "DRIVE":
                continue
            tk = g["dis"][g["target_di"]]["clips"][0]
            for d in g["dis"].values():
                tasks.append(("path", tk, d["clips"]))
        with ProcessPoolExecutor(jobs, initializer=_init, initargs=(False,)) as ex:
            for i, part in enumerate(ex.map(mrstft_task, tasks)):
                mr.update(part)
                if (i + 1) % 20 == 0 or i + 1 == len(tasks):
                    print(f"  mrstft {i + 1}/{len(tasks)}, {time.time() - t0:.0f} s", flush=True)
    # distances per group, DI and score
    dist = []
    for g in groups:
        tk = g["dis"][g["target_di"]]["clips"][0]
        ft = feats.get(tk)
        rec = {k: g[k] for k in ("gid", "source", "family", "model", "target_di", "types")}
        rec["dis"] = {}
        for name, d in g["dis"].items():
            rec["dis"][name] = {"cond": d["cond"], "d": {}}
            for s in SCORES:
                if s == "mrstft_v2":
                    v = [mr.get(f"{tk}|{c}") for c in d["clips"]]
                    v = [float("nan") if x is None else x for x in v]
                else:
                    v = [distance(s, ft, feats.get(c)) for c in d["clips"]]
                rec["dis"][name]["d"][s] = [None if not np.isfinite(x) else round(float(x), 6) for x in v]
        dist.append(rec)
    (OUT / "distances.json").write_text(json.dumps(dist))
    meta = {"seconds": round(time.time() - t0, 1), "groups": len(groups), "clips": len(feats), "pairs": len(mr),
            "jobs": jobs, "sources": sources, "renders": 0}
    (OUT / "run_meta.json").write_text(json.dumps(meta))
    print(f"done: {meta}", flush=True)
    proc_check()
    return 0


# ------------------------------------------------------------------------------------------- report

def spearman(a, b) -> float:
    return X.spearman(a, b)


def _arr(v) -> np.ndarray:
    return np.asarray([np.nan if x is None else x for x in v], float)


def records(rec: dict) -> tuple[list[dict], list[dict]]:
    """Per (other DI, score): r (all / knob / model distractors), rank-1, rho against the own DI.
    Plus rho between pairs of other DIs of one condition."""
    types = np.asarray(rec["types"])
    knob = np.isin(types, KNOB_TYPES)
    model = ~knob & (types != "true")
    own = rec["dis"][rec["target_di"]]
    out, between = [], []
    others = [(n, d) for n, d in rec["dis"].items() if n != rec["target_di"]]
    for s in SCORES:
        dA = _arr(own["d"][s])
        for n, d in others:
            dx = _arr(d["d"][s])
            if not np.isfinite(dx[0]):
                continue
            dT = float(dx[0])
            rest = dx[1:]
            fin = np.isfinite(rest)
            r = {"gid": rec["gid"], "source": rec["source"], "family": rec["family"], "model": rec["model"],
                 "score": s, "di": n, "cond": d["cond"], "n_cand": int(fin.sum()) + 1,
                 "rank1": bool(fin.any() and np.all(rest[fin] > dT)),
                 "rho_own": spearman(dA, dx)}
            for name, sel in (("r", types != "true"), ("r_knob", knob), ("r_model", model)):
                va = dA[sel & np.isfinite(dA)]
                r[name] = dT / float(np.median(va)) if va.size and np.median(va) > 0 else float("nan")
            out.append(r)
        for i, (nx, dx) in enumerate(others):
            for ny, dy in others[i + 1:]:
                if dx["cond"] == dy["cond"]:
                    between.append({"gid": rec["gid"], "source": rec["source"], "model": rec["model"], "score": s,
                                    "cond": dx["cond"], "rho": spearman(_arr(dx["d"][s]), _arr(dy["d"][s]))})
    return out, between


def _med(v) -> float:
    v = np.asarray([x for x in v if np.isfinite(x)], float)
    return float(np.median(v)) if v.size else float("nan")


def _mean(v) -> float:
    v = np.asarray([x for x in v if np.isfinite(x)], float)
    return float(np.mean(v)) if v.size else float("nan")


def cell(rs: list[dict], bs: list[dict], boot: int = BOOT, seed: int = SEED) -> dict:
    """Statistics of one (source, condition, score) cell with 95% intervals from resampling the
    models (the targets of one model are not independent)."""
    if not rs:
        return {}
    c = {"n": len(rs), "n_models": len({r["model"] for r in rs}),
         "R": _med(r["r"] for r in rs), "R_knob": _med(r["r_knob"] for r in rs),
         "R_model": _med(r["r_model"] for r in rs),
         "share_r_lt1": _mean(float(r["r"] < 1) for r in rs if np.isfinite(r["r"])),
         "rank1": _mean(float(r["rank1"]) for r in rs), "chance": _mean(1.0 / r["n_cand"] for r in rs),
         "rho_own": _mean(r["rho_own"] for r in rs),
         "rho_between": _mean(b["rho"] for b in bs), "n_between": len(bs)}
    models = sorted({r["model"] for r in rs})
    by_r = {m: [r for r in rs if r["model"] == m] for m in models}
    by_b = {m: [b for b in bs if b["model"] == m] for m in models}
    rng = np.random.default_rng(seed)
    draws = {"R": [], "rho_own": [], "rho_between": []}
    for _ in range(boot):
        pick = rng.integers(len(models), size=len(models))
        rr = [r for i in pick for r in by_r[models[i]]]
        bb = [b for i in pick for b in by_b[models[i]]]
        draws["R"].append(_med(r["r"] for r in rr))
        draws["rho_own"].append(_mean(r["rho_own"] for r in rr))
        if bb:
            draws["rho_between"].append(_mean(b["rho"] for b in bb))
    c["ci"] = {k: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
               for k, v in draws.items() if v}
    return c


def summarise(dist: list[dict]) -> dict:
    recs, bets = [], []
    for rec in dist:
        a, b = records(rec)
        recs += a
        bets += b
    out = {"cells": {}, "meta": {}}
    for src in list(SOURCES) + ["ALL"]:
        for cond in CONDS:
            for s in SCORES:
                rs = [r for r in recs if r["score"] == s and r["cond"] == cond and (src == "ALL" or r["source"] == src)]
                bs = [b for b in bets if b["score"] == s and b["cond"] == cond and (src == "ALL" or b["source"] == src)]
                c = cell(rs, bs)
                if c:
                    out["cells"][f"{src}|{cond}|{s}"] = c
    fams = sorted({r["family"] for r in recs})
    out["by_family"] = {}
    for fam in fams:
        for s in SCORES:
            rs = [r for r in recs if r["score"] == s and r["family"] == fam]
            if rs:
                out["by_family"][f"{fam}|{s}"] = {"n": len(rs), "R": _med(r["r"] for r in rs),
                                                   "rho_own": _mean(r["rho_own"] for r in rs),
                                                   "rank1": _mean(float(r["rank1"]) for r in rs)}
    return out


def fmt(v, nd=2) -> str:
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{nd}f}"


def tables_md(summ: dict) -> str:
    L = ["### R (median invariance ratio) and candidate-order Spearman, by source and condition", "",
         "R: distance from the target to its own setting on the other DI, divided by the median distance to a "
         "wrong setting on the target's own DI; below 1 the score still sees the setting through the DI change. "
         "rho_own: Spearman correlation of the candidate order on the other DI with the order on the target's "
         "own DI (1 = same order, 0 = unrelated). rho_between: the same between two other DIs of that "
         "condition. 95% intervals from resampling the models. n = (target, other DI) pairs.", ""]
    for src in list(SOURCES) + ["ALL"]:
        for cond in CONDS:
            rows = [(s, summ["cells"].get(f"{src}|{cond}|{s}")) for s in SCORES]
            if not any(c for _, c in rows):
                continue
            L += [f"#### {src} / {cond}", "",
                  "| score | n | models | R | 95% | R knob-only | R other-model | share r<1 | rho_own | 95% | "
                  "rho_between | n pairs | rank-1 | chance |", "|---|" + "---|" * 13]
            for s, c in rows:
                if not c:
                    continue
                ci = c.get("ci", {})
                L.append(f"| {s} | {c['n']} | {c['n_models']} | {fmt(c['R'])} | "
                         f"{fmt(ci.get('R', [np.nan])[0])}-{fmt(ci.get('R', [np.nan, np.nan])[1])} | "
                         f"{fmt(c['R_knob'])} | {fmt(c['R_model'])} | {fmt(c['share_r_lt1'])} | "
                         f"{fmt(c['rho_own'])} | "
                         f"{fmt(ci.get('rho_own', [np.nan])[0])}-{fmt(ci.get('rho_own', [np.nan, np.nan])[1])} | "
                         f"{fmt(c['rho_between'])} | {c['n_between']} | {fmt(c['rank1'])} | {fmt(c['chance'], 3)} |")
            L.append("")
    L += ["### By family (both conditions pooled): n / R / rho_own / rank-1", "",
          "| family | " + " | ".join(SCORES) + " |", "|---|" + "---|" * len(SCORES)]
    fams = sorted({k.split("|")[0] for k in summ["by_family"]})
    for fam in fams:
        cells = [summ["by_family"].get(f"{fam}|{s}") for s in SCORES]
        L.append(f"| {fam} | " + " | ".join(f"{c['n']} / {fmt(c['R'])} / {fmt(c['rho_own'])} / {fmt(c['rank1'])}"
                                            if c else "-" for c in cells) + " |")
    return "\n".join(L) + "\n"


def write_ledger(summ: dict, meta: dict) -> None:
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
    for s in SCORES:
        oth, same = summ["cells"].get(f"ALL|other_player|{s}", {}), summ["cells"].get(f"ALL|same_player|{s}", {})
        if not oth:
            continue
        note = (f"T221 DI-invariance check (measurement only, nothing admitted). R = median d(same setting on "
                f"another DI) / median d(other setting, same DI), other-player condition, all sources "
                f"(DRIVE+V1), n={oth['n']} from {oth['n_models']} models, 95% {fmt(oth['ci']['R'][0])}-"
                f"{fmt(oth['ci']['R'][1])}; same-player R={fmt(same.get('R'))} (n={same.get('n')}); "
                f"R other-model only {fmt(oth['R_model'])}, knob-only {fmt(oth['R_knob'])}; "
                f"order Spearman vs own DI {fmt(oth['rho_own'])} (other player) / {fmt(same.get('rho_own'))} "
                f"(same player); rank-1 {fmt(oth['rank1'])} vs chance {fmt(oth['chance'], 3)}. "
                f"Cached renders only; docs/notes/di-invariance-check-2026-10-23.md")
        for metric, val in ((f"invariance_ratio_{s}", oth["R"]), (f"order_spearman_{s}", oth["rho_own"])):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark",
                            "--engine", "native", "--dataset", DATASET, "--metric", metric,
                            "--value", f"{val:.4f}", "--renders", "0",
                            "--wall-clock-s", str(int(meta.get("seconds", 0))), "--notes", note],
                           check=True, env=env)


def cmd_report(a) -> int:
    dist = json.loads((OUT / "distances.json").read_text())
    meta = json.loads((OUT / "run_meta.json").read_text()) if (OUT / "run_meta.json").is_file() else {}
    summ = summarise(dist)
    summ["meta"] = meta
    (OUT / "summary.json").write_text(json.dumps(summ, indent=1))
    md = tables_md(summ)
    (OUT / "tables.md").write_text(md)
    print(md)
    if a.ledger:
        write_ledger(summ, meta)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=int, default=3)
    r.add_argument("--sources", nargs="*", choices=SOURCES)
    r.set_defaults(fn=cmd_run)
    q = sub.add_parser("report")
    q.add_argument("--ledger", action="store_true")
    q.set_defaults(fn=cmd_report)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
