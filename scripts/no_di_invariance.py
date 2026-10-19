#!/usr/bin/env python3
"""No-DI step 1 (T161, owner: tone-metrics): cross-DI invariance of candidate scores.

Question: which scores can tell Helix settings apart when the target and the candidate renders come
from DIFFERENT guitar samples? Design, data, split and every free parameter were registered in
docs/DECISIONS.md (2026-10-18, "T161 registration") before any number was computed. A diagnostic,
not gate evidence; it admits no weights. Write-up: docs/notes/no-di-invariance.md.

  run      build the groups (target + candidate sets per condition), compute every score's distances
           in worker processes, write cache/no-di-invariance/distances.json (local, holds model ids)
  report   from distances.json: invariance ratio R, rank-1, pairwise wins, Spearman agreement and the
           eqfit corrections, by source, condition, family and distractor type ->
           cache/no-di-invariance/summary.json and tables.md; `--ledger` appends one
           `invariance_ratio_<score>` and one `rank1_rate_<score>` row per score (kind loss-trust,
           dataset helix-trust-v2-xdi)

    nice -n 10 .venv/bin/python scripts/no_di_invariance.py run --jobs 5
    .venv/bin/python scripts/no_di_invariance.py report [--ledger]

Sources (cached renders only, no Native; TRAIN models of helix-trust-v2 only; no sealed label read):
  V1      helix-trust-v1 items whose true model is not a helix-trust-v2 test model; DI A target,
          candidates on DI B (another recording) and C (another passage of A's recording)
  DRIVE   drive-specialist calibration renders, 8 DIs (two Guitar-TECHS players)
  MOD     mod-specialist calibration renders, 2 passages of one recording
  V2      helix-trust-v2 train items: LTI transplant to two other train DIs (linear candidates only)
          and five post-hoc pickup filters (PICKUP, condition C4)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tonematch import cli_result

OUT = ROOT / "cache" / "no-di-invariance"
SEED = 161
DATASET = "helix-trust-v2-xdi"
V1_DIR = ROOT / "cache" / "loss-trust" / "helix-trust-v1"
V2_DIR = ROOT / "cache" / "loss-trust" / "helix-trust-v2"
DRIVE_DIR = ROOT / "cache" / "drive-specialist"
MOD_DIR = ROOT / "cache" / "mod-specialist"

SCORES = ("mrstft_v2", "level_db", "mrstft_v2_guard", "embed_afxrep", "ltas_tilt", "ltas_eqfit",
          "crest_flatness_hnr", "envelope", "texmod", "mmd_cep", "presence_nodi")
FAMILIES = ("amp", "drive", "cab", "eq", "comp", "mod", "delay", "reverb")
DTYPES = ("move10", "move25", "far", "same_family_model", "other_family", "di_only")
V1_FAMILY = {"Distortion": "drive", "Delay": "delay", "Reverb": "reverb", "Dynamics": "comp", "EQ": "eq",
             "Modulation": "mod", "Amp": "amp", "Cab": "cab"}
GUARD_TOL_DB, GUARD_SLOPE = 0.25, 0.08          # Ruling 3 known-DI level guard (chain_builder)
SILENT_RMS = 1e-5
SCALE_SAMPLE = 240                               # clips per source for the label-free scales
# conditions that enter the ledger headline (registration): real or transplanted other DIs
HEADLINE_KINDS = ("other_recording", "other_passage", "other_player", "same_player",
                  "lti_other_player", "lti_same_player")
DRIVE_SETTINGS, DRIVE_SAME, DRIVE_OTHER = 6, 15, 8
MOD_SETTINGS, MOD_SAME, MOD_OTHER = 10, 19, 4


# ------------------------------------------------------------------------------------------ helpers

def move_type(knobs: dict, true_knobs: dict) -> str:
    """Distractor type of a same-model distractor from its largest knob move (CC steps / 127)."""
    keys = set(knobs) | set(true_knobs)
    mv = max((abs(float(knobs.get(k, true_knobs.get(k, 0))) - float(true_knobs.get(k, knobs.get(k, 0)))) / 127.0
              for k in keys), default=0.0)
    return "move10" if mv <= 0.175 else "move25" if mv <= 0.375 else "far"


def label_type(label: str, knobs: dict | None, true_knobs: dict | None) -> str:
    if label == "true":
        return "true"
    if label.startswith("near"):
        return move_type(knobs or {}, true_knobs or {})
    if label.startswith("nbr"):
        return "same_family_model"
    if label.startswith("random"):
        return "other_family"
    raise ValueError(label)


def player(path: str) -> str:
    """Guitar-TECHS player id (P1, P2, P3) from a library path."""
    for part in str(path).split("/"):
        if len(part) >= 2 and part[0] == "P" and part[1].isdigit():
            return part[:2]
    return "?"


def v2_test_models() -> set[str]:
    """Models only in helix-trust-v2's TEST split (reverb models are shared by both splits, so they
    count as train models)."""
    m = json.loads((V2_DIR / "manifest.json").read_text())
    test = {x for c in m["splits"]["test"]["models"].values() for x in c}
    train = {x for c in m["splits"]["train"]["models"].values() for x in c}
    return test - train


def v2_train_models() -> dict[str, list[str]]:
    m = json.loads((V2_DIR / "manifest.json").read_text())
    return m["splits"]["train"]["models"]


def cand(label: str, dtype: str, spec: list) -> dict:
    return {"label": label, "type": dtype, "spec": spec}


# ------------------------------------------------------------------------------------------- groups

def groups_v1() -> list[dict]:
    man = json.loads((V1_DIR / "manifest.json").read_text())
    test = v2_test_models()
    out = []
    for it in man["items"]:
        if it["true_model"] in test:
            continue
        ii = it["item"]
        rd = f"cache/loss-trust/helix-trust-v1/renders/item{ii:02d}"
        tk = it["candidates"][0].get("knobs", {})
        conds = {}
        for X, name in (("A", "A"), ("B", "other_recording"), ("C", "other_passage")):
            cs = [cand(c["label"], label_type(c["label"], c.get("knobs"), tk), ["npy", f"{rd}/c{j:02d}_{X}.npy"])
                  for j, c in enumerate(it["candidates"])]
            cs.append(cand("di_only", "di_only", ["wav", it["di"][X]["file"]]))
            conds[name] = cs
        out.append({"gid": f"V1-{ii:02d}", "source": "V1", "family": V1_FAMILY[it["category"]],
                    "true_model": it["true_model"], "target": ["npy", f"{rd}/c00_A.npy"], "conds": conds,
                    "spearman": [["other_recording", "other_passage"]], "task": f"V1-{ii:02d}"})
    return out


def groups_drive() -> list[dict]:
    plan = json.loads((DRIVE_DIR / "cal_plan.json").read_text())
    models = sorted(set(v2_train_models()["drive"]) & set(plan["models"]))
    pl = [player(d["path"]) for d in plan["dis"]]
    nd = len(pl)
    rng = np.random.default_rng([SEED, 1])
    out = []

    def spec(m, j, d):
        return ["npy", f"cache/drive-specialist/cal_renders/{m}/s{j:02d}_d{d}.npy"]

    for mi, m in enumerate(models):
        sets = plan["models"][m]["settings"]
        ns = len(sets)
        chosen = rng.choice(ns, DRIVE_SETTINGS, replace=False)
        for k, s in enumerate(int(v) for v in chosen):
            a = (mi * DRIVE_SETTINGS + k) % nd
            b = int(rng.choice([d for d in range(nd) if pl[d] != pl[a]]))
            b2 = int(rng.choice([d for d in range(nd) if d != a and pl[d] == pl[a]]))
            same = [int(v) for v in rng.choice([j for j in range(ns) if j != s], DRIVE_SAME, replace=False)]
            oth = [str(v) for v in rng.choice([x for x in models if x != m], DRIVE_OTHER, replace=False)]
            oth_s = [int(rng.integers(len(plan["models"][o]["settings"]))) for o in oth]
            conds = {}
            for name, d in (("A", a), ("other_player", b), ("same_player", b2)):
                cs = [cand("true", "true", spec(m, s, d))]
                cs += [cand(f"set{j:02d}", move_type(sets[j], sets[s]), spec(m, j, d)) for j in same]
                cs += [cand(f"nbr:{o}", "same_family_model", spec(o, j, d)) for o, j in zip(oth, oth_s)]
                cs.append(cand("di_only", "di_only", ["drdi", d]))
                conds[name] = cs
            out.append({"gid": f"DRIVE-{m}-s{s:02d}", "source": "DRIVE", "family": "drive", "true_model": m,
                        "target": spec(m, s, a), "conds": conds, "spearman": [["other_player", "same_player"]],
                        "task": f"DRIVE-{m}", "dis": {"A": a, "other_player": b, "same_player": b2}})
    return out


def groups_mod() -> list[dict]:
    plan = json.loads((MOD_DIR / "cal_plan.json").read_text())
    models = sorted(set(v2_train_models()["mod"]) & set(plan["models"]))
    rng = np.random.default_rng([SEED, 2])
    out = []

    def spec(m, j, d):
        return ["npy", f"cache/mod-specialist/cal_renders/{m}/s{j:02d}_d{d}.npy"]

    for m in models:
        sets = plan["models"][m]["settings"]
        ns = len(sets)
        chosen = [int(v) for v in rng.choice(ns, MOD_SETTINGS, replace=False)]
        for s in chosen:
            same = [int(v) for v in rng.choice([j for j in range(ns) if j != s], MOD_SAME, replace=False)]
            oth = [x for x in models if x != m][:MOD_OTHER]
            oth_s = [int(rng.integers(len(plan["models"][o]["settings"]))) for o in oth]
            for a in (0, 1):
                x = 1 - a
                conds = {}
                for name, d in (("A", a), ("other_passage", x)):
                    cs = [cand("true", "true", spec(m, s, d))]
                    cs += [cand(f"set{j:02d}", move_type(sets[j], sets[s]), spec(m, j, d)) for j in same]
                    cs += [cand(f"nbr:{o}", "same_family_model", spec(o, j, d)) for o, j in zip(oth, oth_s)]
                    cs.append(cand("di_only", "di_only", ["mddi", d]))
                    conds[name] = cs
                out.append({"gid": f"MOD-{m}-s{s:02d}-d{a}", "source": "MOD", "family": "mod", "true_model": m,
                            "target": spec(m, s, a), "conds": conds, "spearman": [], "task": f"MOD-{m}"})
    return out


def groups_v2() -> list[dict]:
    from tonematch.metrics.nodi_candidates import PICKUP_FILTERS
    man = json.loads((V2_DIR / "manifest.json").read_text())
    items = [it for it in man["items"] if it["split"] == "train"]
    rng = np.random.default_rng([SEED, 3])
    out = []
    pk = list(PICKUP_FILTERS)
    for it in items:
        ii = it["item"]
        rd = f"cache/loss-trust/helix-trust-v2/renders/{it['dir']}"
        dia = it["di"]["A"]
        pa = player(dia["path"])
        others = [o for o in items if o["di"]["A"]["path"] != dia["path"] and player(o["di"]["A"]["path"]) != pa]
        sames = [o for o in items if o["di"]["A"]["path"] != dia["path"] and player(o["di"]["A"]["path"]) == pa]
        ob = others[int(rng.integers(len(others)))]["di"]["A"]["file"]
        sb = sames[int(rng.integers(len(sames)))]["di"]["A"]["file"] if sames else None
        tk = it["candidates"][0].get("knobs", {})
        types = [label_type(c["label"], c.get("knobs"), tk) for c in it["candidates"]]
        files = [f"{rd}/c{j:02d}_A.npy" for j in range(len(it["candidates"]))]
        labels = [c["label"] for c in it["candidates"]]
        conds = {"A": [cand(lb, t, ["npy", f]) for lb, t, f in zip(labels, types, files)]
                 + [cand("di_only", "di_only", ["wav", dia["file"]])]}
        for name, db in (("lti_other_player", ob), ("lti_same_player", sb)):
            if db is None:
                continue
            conds[name] = ([cand(lb, t, ["lti", f, dia["file"], db]) for lb, t, f in zip(labels, types, files)]
                           + [cand("di_only", "di_only", ["wav", db])])
        for p in pk:
            conds[f"pk_{p}"] = ([cand(lb, t, ["pk", p, ["npy", f]]) for lb, t, f in zip(labels, types, files)]
                                + [cand("di_only", "di_only", ["pk", p, ["wav", dia["file"]]])])
        sp = [["lti_other_player", "lti_same_player"]] if sb else []
        sp += [[f"pk_{a}", f"pk_{b}"] for i, a in enumerate(pk) for b in pk[i + 1:]]
        out.append({"gid": f"V2-{ii:03d}", "source": "V2", "family": it["category"], "true_model": it["true_model"],
                    "target": ["npy", files[0]], "conds": conds, "spearman": sp, "task": f"V2-{ii:03d}"})
    return out


def all_groups() -> list[dict]:
    return groups_v1() + groups_drive() + groups_mod() + groups_v2()


def cond_kind(name: str) -> str:
    return name


# -------------------------------------------------------------------------------- audio from a spec

@lru_cache(maxsize=16)
def _drive_dis() -> tuple:
    import drive_calibrate as DC
    return tuple(DC.load_dis())


@lru_cache(maxsize=4)
def _mod_dis() -> tuple:
    import mod_calibrate as MC
    return tuple(MC.load_dis())


@lru_cache(maxsize=64)
def _file(path: str) -> np.ndarray:
    p = ROOT / path
    if p.suffix == ".npy":
        x = np.load(p)
    else:
        import soundfile as sf
        x, _ = sf.read(str(p), dtype="float32")
    x = np.asarray(x, np.float32)
    return x.mean(axis=1).astype(np.float32) if x.ndim == 2 else x


@lru_cache(maxsize=32)
def _lti(cand_path: str, di_a: str) -> tuple[np.ndarray, float]:
    from tonematch.metrics import nodi_candidates as N
    x, y = _file(di_a), _file(cand_path)
    h = N.lti_transfer(x, y)
    return h, N.null_db(y, N.lti_apply(x, h))


def audio(spec) -> np.ndarray:
    from tonematch.metrics import nodi_candidates as N
    k = spec[0]
    if k in ("npy", "wav"):
        return _file(spec[1])
    if k == "drdi":
        return _drive_dis()[int(spec[1])]
    if k == "mddi":
        return _mod_dis()[int(spec[1])]
    if k == "lti":
        h, _ = _lti(spec[1], spec[2])
        return N.lti_apply(_file(spec[3]), h)
    if k == "pk":
        return N.pickup(audio(spec[2]), spec[1])
    raise ValueError(spec)


def lti_null(spec) -> float | None:
    return _lti(spec[1], spec[2])[1] if spec[0] == "lti" else None


def key(spec) -> str:
    return json.dumps(spec)


# ----------------------------------------------------------------------------------------- features

def clip_features(x: np.ndarray, with_afx: bool = True) -> dict:
    from tonematch.metrics import afxrep, crest, envelope, ltas
    from tonematch.metrics import nodi_candidates as N
    f = {"ltas": ltas.features(x), "crest": crest.features(x), "env": envelope.features(x),
         "tex": N.texmod_features(x), "frames": N.frame_features(x), "pres": N.presence_vector(x)}
    if with_afx:
        f["afx"] = afxrep.features(x)
    return f


def active_level_db(x, frame: int = 2048, span_db: float = 40.0) -> float:
    """As `match_song.LevelGuardKit.active_level_db` (Ruling 3)."""
    x = np.asarray(x, np.float64).ravel()
    n = len(x) // frame
    if n < 2:
        return float(20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0, 1e-12)))
    p = (x[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
    act = p >= p.max() * 10 ** (-span_db / 10)
    return float(10 * np.log10(max(float(p[act].mean()), 1e-24)))


def distances(t: np.ndarray, ft: dict, c: np.ndarray, fc: dict, sc) -> tuple[dict, tuple[float, float]]:
    from tonematch.metrics import afxrep, crest, envelope, level, ltas, mrstft_v2
    from tonematch.metrics import nodi_candidates as N
    d = {}
    d["mrstft_v2"] = mrstft_v2.term(t, c)
    d["level_db"] = level.term(t, c)
    dl = abs(active_level_db(c) - active_level_db(t))
    d["mrstft_v2_guard"] = d["mrstft_v2"] + GUARD_SLOPE * max(0.0, dl - GUARD_TOL_DB)
    d["embed_afxrep"] = afxrep.distance(ft["afx"], fc["afx"]) if "afx" in ft and "afx" in fc else float("nan")
    d["ltas_tilt"] = ltas.distance(ft["ltas"], fc["ltas"])
    e, tilt, shelf = N.eqfit(ft["ltas"], fc["ltas"])
    d["ltas_eqfit"] = e
    d["crest_flatness_hnr"] = crest.distance(ft["crest"], fc["crest"])
    d["envelope"] = envelope.distance(ft["env"], fc["env"])
    d["texmod"] = N.texmod_distance(ft["tex"], fc["tex"], sc.tex)
    d["mmd_cep"] = N.mmd(ft["frames"], fc["frames"], sc.frame, sc.sigma)
    d["presence_nodi"] = N.vec_distance(ft["pres"], fc["pres"], sc.pres)
    return {k: float(v) for k, v in d.items()}, (float(tilt), float(shelf))


# ------------------------------------------------------------------------------------------ workers

_SCALES: dict = {}


def _init(scales_json: dict, with_afx: bool) -> None:
    os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
    try:
        import torch
        torch.set_num_threads(1)
    except ImportError:         # the embed extra is optional; --no-afx runs without torch
        torch = None
    from tonematch.metrics import nodi_candidates as N
    _SCALES.update({k: N.Scales.from_json(v) for k, v in scales_json.items()})
    _SCALES["_afx"] = with_afx
    if with_afx:
        from tonematch.metrics import afxrep
        afxrep.set_embedder(afxrep.Embedder(device="cpu", threads=1))


def _sample_features(specs: list) -> list:
    out = []
    for s in specs:
        x = audio(s)
        if float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))) < SILENT_RMS:
            continue
        f = clip_features(x, with_afx=False)
        out.append((f["tex"], f["frames"], f["pres"]))
    return out


def _rms(x) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


def process_task(groups: list[dict]) -> list[dict]:
    from tonematch.metrics import nodi_candidates as N
    with_afx = bool(_SCALES.get("_afx"))
    cache: dict[str, dict] = {}
    res = []
    for g in groups:
        sc = _SCALES[g["source"]]
        t = audio(g["target"])
        if _rms(t) < SILENT_RMS:
            res.append({"gid": g["gid"], "skipped": "silent target"})
            continue
        kt = key(g["target"])
        if kt not in cache:
            cache[kt] = clip_features(t, with_afx)
        ft = cache[kt]
        out = {k: g[k] for k in ("gid", "source", "family", "true_model", "spearman")}
        out["conds"] = {}
        for cname, cs in g["conds"].items():
            if cs[0]["spec"][0] == "lti":
                nt = lti_null(cs[0]["spec"])
                if nt is None or nt > N.LTI_NULL_MAX_DB:
                    out["conds"][cname] = {"dropped": f"truth null {nt:.1f} dB"}
                    continue
            labels, types, dd, fits, nulls = [], [], {s: [] for s in SCORES}, [], []
            for j, c in enumerate(cs):
                nl = lti_null(c["spec"])
                if nl is not None and nl > N.LTI_NULL_MAX_DB:
                    continue
                x = audio(c["spec"])
                if _rms(x) < SILENT_RMS:
                    if j == 0:
                        break
                    continue
                kc = key(c["spec"])
                if kc not in cache:
                    cache[kc] = clip_features(x, with_afx)
                d, fit = distances(t, ft, x, cache[kc], sc)
                labels.append(c["label"])
                types.append(c["type"])
                for s in SCORES:
                    dd[s].append(round(d[s], 6))
                fits.append([round(fit[0], 4), round(fit[1], 4)])
                nulls.append(None if nl is None else round(nl, 2))
            if not labels or labels[0] != "true":
                out["conds"][cname] = {"dropped": "truth silent"}
                continue
            out["conds"][cname] = {"labels": labels, "types": types, "d": dd, "eqfit": fits,
                                   **({"null_db": nulls} if any(v is not None for v in nulls) else {})}
        res.append(out)
        if len(cache) > 400:
            cache.clear()
            cache[kt] = ft
    return res


# ---------------------------------------------------------------------------------------------- run

def proc_check() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], check=False)


def fit_all_scales(groups: list[dict], jobs: int) -> dict:
    from tonematch.metrics import nodi_candidates as N
    rng = np.random.default_rng([SEED, 9])
    scales = {}
    for src in ("V1", "DRIVE", "MOD", "V2"):
        specs = [c["spec"] for g in groups if g["source"] == src for cs in g["conds"].values() for c in cs
                 if c["spec"][0] != "lti"]
        uniq = list({key(s): s for s in specs}.values())
        pick = [uniq[i] for i in rng.choice(len(uniq), min(SCALE_SAMPLE, len(uniq)), replace=False)]
        chunks = [pick[i::jobs] for i in range(jobs)]
        with ProcessPoolExecutor(jobs) as ex:
            feats = [f for part in ex.map(_sample_features, chunks) for f in part]
        sc = N.fit_scales([f[0] for f in feats], [f[1] for f in feats], [f[2] for f in feats], seed=SEED)
        scales[src] = sc.to_json()
        print(f"scales {src}: {len(feats)} clips, sigma {sc.sigma:.3f}", flush=True)
    return scales


def cmd_run(a) -> int:
    proc_check()
    jobs = max(1, min(a.jobs, 6))
    groups = all_groups()
    if a.sources:
        groups = [g for g in groups if g["source"] in a.sources]
    if a.limit:
        keep, seen = [], {}
        for g in groups:
            seen[g["source"]] = seen.get(g["source"], 0) + 1
            if seen[g["source"]] <= a.limit:
                keep.append(g)
        groups = keep
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{len(groups)} groups: " + ", ".join(f"{s} {sum(g['source'] == s for g in groups)}"
                                                 for s in ("V1", "DRIVE", "MOD", "V2")), flush=True)
    t0 = time.time()
    scales = fit_all_scales(groups, jobs)
    (OUT / "scales.json").write_text(json.dumps(scales))
    tasks: dict[str, list] = {}
    for g in groups:
        tasks.setdefault(g["task"], []).append(g)
    task_list = sorted(tasks.values(), key=lambda v: -len(v) * sum(len(c) for c in v[0]["conds"].values()))
    results = []
    with ProcessPoolExecutor(jobs, initializer=_init, initargs=(scales, not a.no_afx)) as ex:
        for i, r in enumerate(ex.map(process_task, task_list)):
            results += r
            if (i + 1) % 10 == 0 or i + 1 == len(task_list):
                print(f"  {i + 1}/{len(task_list)} tasks, {len(results)} groups, {time.time() - t0:.0f} s", flush=True)
    meta = {"dataset": DATASET, "seed": SEED, "scores": SCORES, "seconds": round(time.time() - t0, 1),
            "afx": not a.no_afx, "registration": "docs/DECISIONS.md 2026-10-18 (tone-metrics) T161 registration"}
    out = OUT / (a.out or "distances.json")
    out.write_text(json.dumps({"meta": meta, "groups": results}))
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.0f} s", flush=True)
    proc_check()
    return 0


# ------------------------------------------------------------------------------------------- report

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def spearman(a, b) -> float:
    from scipy.stats import spearmanr
    if len(a) < 3:
        return float("nan")
    r = spearmanr(a, b).statistic
    return float(r) if np.isfinite(r) else float("nan")


def item_records(data: dict) -> list[dict]:
    """One record per (group, other-DI condition, score)."""
    recs = []
    for g in data["groups"]:
        if "skipped" in g or "A" not in g["conds"] or "d" not in g["conds"]["A"]:
            continue
        A = g["conds"]["A"]
        for cname, c in g["conds"].items():
            if cname == "A" or "d" not in c:
                continue
            for s in SCORES:
                dA = np.asarray(A["d"][s][1:], float)
                tA = A["types"][1:]
                dx = np.asarray(c["d"][s], float)
                if not np.isfinite(dx).all() or not np.isfinite(dA).all() or dA.size == 0:
                    continue
                dT = dx[0]
                den = float(np.median(dA))
                rec = {"gid": g["gid"], "source": g["source"], "family": g["family"], "cond": cname, "score": s,
                       "dT": float(dT), "den": den, "r": float(dT / den) if den > 0 else float("inf"),
                       "rank1": bool(np.all(dx[1:] > dT)), "n_cand": int(dx.size),
                       "dA_by_type": {}, "wins_by_type": {}}
                for t in DTYPES:
                    va = dA[[i for i, tt in enumerate(tA) if tt == t]]
                    if va.size:
                        rec["dA_by_type"][t] = va.tolist()
                    vx = dx[1:][[i for i, tt in enumerate(c["types"][1:]) if tt == t]]
                    if vx.size:
                        rec["wins_by_type"][t] = [int(np.sum(vx > dT)), int(vx.size)]
                if s == "ltas_eqfit":
                    rec["eqfit_true"] = c["eqfit"][0]
                recs.append(rec)
    return recs


def aggregate(recs: list[dict]) -> dict:
    """R (ratio of medians), median r, rank-1 + Wilson, pairwise wins per type."""
    if not recs:
        return {}
    dT = [r["dT"] for r in recs]
    den_all = [v for r in recs for t in r["dA_by_type"] for v in r["dA_by_type"][t]]
    k = sum(r["rank1"] for r in recs)
    n = len(recs)
    out = {"n": n, "R": float(np.median(dT) / np.median(den_all)) if den_all else float("nan"),
           "median_r": float(np.median([r["r"] for r in recs])),
           "rank1": k / n, "rank1_ci": wilson(k, n),
           "mean_candidates": float(np.mean([r["n_cand"] for r in recs])), "R_by_type": {}, "win_by_type": {}}
    for t in DTYPES:
        vals = [v for r in recs for v in r["dA_by_type"].get(t, [])]
        if vals:
            out["R_by_type"][t] = float(np.median(dT) / np.median(vals))
        w = [r["wins_by_type"][t] for r in recs if t in r["wins_by_type"]]
        if w:
            out["win_by_type"][t] = sum(a for a, _ in w) / sum(b for _, b in w)
    allw = [v for r in recs for v in r["wins_by_type"].values()]
    out["win_all"] = sum(a for a, _ in allw) / max(1, sum(b for _, b in allw))
    return out


def headline(recs: list[dict], score: str, boot: int = 1000, seed: int = SEED) -> dict:
    rs = [r for r in recs if r["score"] == score and r["cond"] in HEADLINE_KINDS]
    fams = [f for f in FAMILIES if any(r["family"] == f for r in rs)]
    by = {f: [r for r in rs if r["family"] == f] for f in fams}

    def stat(sel):
        med = [float(np.median([r["r"] for r in sel[f]])) for f in fams]
        r1 = [float(np.mean([r["rank1"] for r in sel[f]])) for f in fams]
        return float(np.median(med)), float(np.mean(r1)), dict(zip(fams, med)), dict(zip(fams, r1))

    R, r1, fam_r, fam_r1 = stat(by)
    rng = np.random.default_rng(seed)
    bs_R, bs_1 = [], []
    for _ in range(boot):
        sel = {f: [by[f][i] for i in rng.integers(len(by[f]), size=len(by[f]))] for f in fams}
        a, b, _, _ = stat(sel)
        bs_R.append(a)
        bs_1.append(b)
    return {"R": R, "R_ci": [float(np.percentile(bs_R, 2.5)), float(np.percentile(bs_R, 97.5))],
            "rank1": r1, "rank1_ci": [float(np.percentile(bs_1, 2.5)), float(np.percentile(bs_1, 97.5))],
            "family_median_r": fam_r, "family_rank1": fam_r1,
            "family_n": {f: len(by[f]) for f in fams}, "n": len(rs),
            "pooled_rank1": float(np.mean([r["rank1"] for r in rs])) if rs else float("nan")}


def spearman_table(data: dict) -> dict:
    out: dict = {}
    for g in data["groups"]:
        if "skipped" in g:
            continue
        for c1, c2 in g.get("spearman", []):
            a, b = g["conds"].get(c1, {}), g["conds"].get(c2, {})
            if "d" not in a or "d" not in b:
                continue
            common = [lb for lb in a["labels"] if lb in b["labels"]]
            ia = [a["labels"].index(lb) for lb in common]
            ib = [b["labels"].index(lb) for lb in common]
            kind = "pickup" if c1.startswith("pk_") else c1 + "~" + c2
            for s in SCORES:
                va = np.asarray(a["d"][s], float)[ia]
                vb = np.asarray(b["d"][s], float)[ib]
                if np.isfinite(va).all() and np.isfinite(vb).all():
                    out.setdefault(f"{g['source']}|{kind}", {}).setdefault(s, []).append(spearman(va, vb))
    return {k: {s: {"mean": float(np.nanmean(v)), "n": int(np.isfinite(v).sum())} for s, v in d.items()}
            for k, d in out.items()}


def eqfit_table(recs: list[dict]) -> dict:
    out = {}
    for r in recs:
        if r["score"] != "ltas_eqfit" or "eqfit_true" not in r:
            continue
        kind = "pickup" if r["cond"].startswith("pk_") else r["cond"]
        out.setdefault(f"{r['source']}|{kind}", []).append(r["eqfit_true"])
    return {k: {"n": len(v), "median_abs_tilt": float(np.median([abs(a) for a, _ in v])),
                "median_abs_shelf": float(np.median([abs(b) for _, b in v])),
                "frac_tilt_at_bound": float(np.mean([abs(a) >= 1.499 for a, _ in v])),
                "frac_shelf_at_bound": float(np.mean([abs(b) >= 5.999 for _, b in v]))} for k, v in out.items()}


def fmt(v, nd=2) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    return f"{v:.{nd}f}"


def cmd_report(a) -> int:
    path = OUT / (a.inp or "distances.json")
    data = json.loads(path.read_text())
    recs = item_records(data)
    summ: dict = {"meta": data["meta"], "headline": {}, "by_source_cond": {}, "by_family": {},
                  "pickup_by_family": {}, "spearman": spearman_table(data), "eqfit": eqfit_table(recs)}
    for s in SCORES:
        summ["headline"][s] = headline(recs, s)
        rs = [r for r in recs if r["score"] == s]
        conds = sorted({(r["source"], "pickup" if r["cond"].startswith("pk_") else r["cond"]) for r in rs})
        for src, cn in conds:
            sel = [r for r in rs if r["source"] == src and
                   ((cn == "pickup" and r["cond"].startswith("pk_")) or r["cond"] == cn)]
            summ["by_source_cond"].setdefault(f"{src}|{cn}", {})[s] = aggregate(sel)
        for f in FAMILIES:
            sel = [r for r in rs if r["family"] == f and r["cond"] in HEADLINE_KINDS]
            if sel:
                summ["by_family"].setdefault(f, {})[s] = aggregate(sel)
            sel = [r for r in rs if r["family"] == f and r["cond"].startswith("pk_")]
            if sel:
                summ["pickup_by_family"].setdefault(f, {})[s] = aggregate(sel)
    drops = {}
    for g in data["groups"]:
        for cn, c in g.get("conds", {}).items():
            if "dropped" in c:
                drops.setdefault(f"{g['source']}|{cn}", 0)
                drops[f"{g['source']}|{cn}"] += 1
    summ["dropped_conditions"] = drops
    lti_kept = {}
    for g in data["groups"]:
        for cn, c in g.get("conds", {}).items():
            if cn.startswith("lti") and "d" in c:
                lti_kept.setdefault(g["family"], []).append(len(c["labels"]))
    summ["lti_candidates_kept"] = {f: {"items": len(v), "median_candidates": float(np.median(v))}
                                   for f, v in lti_kept.items()}
    (OUT / "summary.json").write_text(json.dumps(summ, indent=1))
    md = tables_md(summ)
    (OUT / "tables.md").write_text(md)
    print(md)
    if a.ledger:
        write_ledger(summ, data["meta"])
    return 0


def tables_md(summ: dict) -> str:
    L = []
    L.append("### Headline (family-balanced, real and transplanted other-DI conditions)\n")
    L.append("| score | R (median over families of median r) | 95% CI | rank-1 (mean over families) | 95% CI | pooled rank-1 | n |")
    L.append("|---|---|---|---|---|---|---|")
    for s, h in sorted(summ["headline"].items(), key=lambda kv: kv[1]["R"]):
        L.append(f"| {s} | {fmt(h['R'])} | {fmt(h['R_ci'][0])}-{fmt(h['R_ci'][1])} | {fmt(h['rank1'])} | "
                 f"{fmt(h['rank1_ci'][0])}-{fmt(h['rank1_ci'][1])} | {fmt(h['pooled_rank1'])} | {h['n']} |")
    L.append("\n### Median r by family (other-DI conditions)\n")
    L.append("| score | " + " | ".join(FAMILIES) + " |")
    L.append("|---|" + "---|" * len(FAMILIES))
    for s in SCORES:
        fr = summ["headline"][s]["family_median_r"]
        L.append(f"| {s} | " + " | ".join(fmt(fr.get(f)) for f in FAMILIES) + " |")
    n = summ["headline"][SCORES[0]]["family_n"]
    L.append("| (items x conditions) | " + " | ".join(str(n.get(f, 0)) for f in FAMILIES) + " |")
    L.append("\n### Rank-1 by family (other-DI conditions)\n")
    L.append("| score | " + " | ".join(FAMILIES) + " |")
    L.append("|---|" + "---|" * len(FAMILIES))
    for s in SCORES:
        fr = summ["headline"][s]["family_rank1"]
        L.append(f"| {s} | " + " | ".join(fmt(fr.get(f)) for f in FAMILIES) + " |")
    L.append("\n### By source and condition: R (ratio of medians) / rank-1 / pairwise win rate\n")
    keys = sorted(summ["by_source_cond"])
    L.append("| score | " + " | ".join(k.replace("|", " ") for k in keys) + " |")
    L.append("|---|" + "---|" * len(keys))
    for s in SCORES:
        cells = []
        for k in keys:
            g = summ["by_source_cond"][k].get(s, {})
            cells.append(f"{fmt(g.get('R'))} / {fmt(g.get('rank1'))} / {fmt(g.get('win_all'))}" if g else "-")
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    L.append("| n (items) | " + " | ".join(str(summ["by_source_cond"][k][SCORES[0]].get("n", "-")) for k in keys) + " |")
    L.append("| mean candidates | " + " | ".join(fmt(summ["by_source_cond"][k][SCORES[0]].get("mean_candidates"), 1)
                                               for k in keys) + " |")
    for title, field in (("R by distractor type (other-DI conditions pooled)", "R_by_type"),
                         ("Pairwise win rate by distractor type (other-DI conditions pooled)", "win_by_type")):
        L.append(f"\n### {title}\n")
        L.append("| score | " + " | ".join(DTYPES) + " |")
        L.append("|---|" + "---|" * len(DTYPES))
        for s in SCORES:
            agg = {}
            # pooled over the headline conditions: recompute from by_family is lossy; use source tables
            vals = {t: [] for t in DTYPES}
            for k, v in summ["by_source_cond"].items():
                if k.split("|")[1] in HEADLINE_KINDS and s in v:
                    for t, x in v[s].get(field, {}).items():
                        vals[t].append((x, v[s]["n"]))
            for t in DTYPES:
                if vals[t]:
                    agg[t] = sum(x * w for x, w in vals[t]) / sum(w for _, w in vals[t])
            L.append(f"| {s} | " + " | ".join(fmt(agg.get(t)) for t in DTYPES) + " |")
        L.append("\n(item-weighted mean of the per-source values)")
    L.append("\n### Pickup perturbation (C4, helix-trust-v2 train, post-hoc), by family: R / rank-1\n")
    L.append("| score | " + " | ".join(FAMILIES) + " |")
    L.append("|---|" + "---|" * len(FAMILIES))
    for s in SCORES:
        cells = []
        for f in FAMILIES:
            g = summ["pickup_by_family"].get(f, {}).get(s)
            cells.append(f"{fmt(g['R'])} / {fmt(g['rank1'])}" if g else "-")
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    L.append("\n### Spearman agreement of candidate order between two other DIs (mean over items)\n")
    keys = sorted(summ["spearman"])
    L.append("| score | " + " | ".join(k.replace("|", " ") for k in keys) + " |")
    L.append("|---|" + "---|" * len(keys))
    for s in SCORES:
        L.append(f"| {s} | " + " | ".join(fmt(summ["spearman"][k].get(s, {}).get("mean")) for k in keys) + " |")
    L.append("\n### ltas_eqfit: fitted correction for the true preset (median |tilt| dB/oct, median |shelf| dB, share at a bound)\n")
    L.append("| source condition | n | tilt | shelf | tilt at bound | shelf at bound |")
    L.append("|---|---|---|---|---|---|")
    for k, v in sorted(summ["eqfit"].items()):
        L.append(f"| {k.replace('|', ' ')} | {v['n']} | {fmt(v['median_abs_tilt'])} | {fmt(v['median_abs_shelf'])} | "
                 f"{fmt(v['frac_tilt_at_bound'])} | {fmt(v['frac_shelf_at_bound'])} |")
    L.append(f"\nDropped conditions: {summ['dropped_conditions']}")
    L.append(f"\nLTI transplant candidates kept per family: {summ['lti_candidates_kept']}")
    return "\n".join(L) + "\n"


def write_ledger(summ: dict, meta: dict) -> None:
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}
    for s in SCORES:
        h = summ["headline"][s]
        base = (f"T161 cross-DI diagnostic (not gate evidence; admits nothing); score {s}; train models only; "
                f"family-balanced over {len(h['family_n'])} families; n={h['n']} item-conditions "
                f"(V1 other recording/passage + DRIVE + MOD + LTI transplant); "
                f"registration docs/DECISIONS.md 2026-10-18 T161; docs/notes/no-di-invariance.md")
        for metric, val, ci in ((f"invariance_ratio_{s}", h["R"], h["R_ci"]), (f"rank1_rate_{s}", h["rank1"], h["rank1_ci"])):
            note = f"{base}; bootstrap 95% CI {ci[0]:.3f}-{ci[1]:.3f}"
            if metric.startswith("rank1"):
                note += f"; pooled rank-1 {h['pooled_rank1']:.3f}"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                            "--engine", "native", "--dataset", DATASET, "--metric", metric,
                            "--value", f"{val:.4f}", "--renders", "0", "--wall-clock-s", str(meta.get("seconds", "")),
                            "--notes", note], check=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=int, default=5)
    r.add_argument("--sources", nargs="*", choices=("V1", "DRIVE", "MOD", "V2"))
    r.add_argument("--limit", type=int, default=0, help="at most this many groups per source (smoke runs)")
    r.add_argument("--no-afx", action="store_true")
    r.add_argument("--out")
    p = sub.add_parser("report")
    p.add_argument("--inp")
    p.add_argument("--ledger", action="store_true")
    for q in (r, p):
        cli_result.add_result_arg(q)
    a = ap.parse_args(argv)
    return cli_result.run(a, tool=f"no_di_invariance {a.cmd}", body=lambda res: _with_result(a, res))


def _with_result(a, res) -> int:
    rc = cmd_run(a) if a.cmd == "run" else cmd_report(a)
    if a.cmd == "run":
        dist = OUT / (a.out or "distances.json")
        res.outputs["distances_json"] = dist
        if dist.is_file():
            res.metrics["meta"] = json.loads(dist.read_text()).get("meta", {})
    else:
        res.inputs["distances_json"] = OUT / (a.inp or "distances.json")
        res.outputs.update({"summary_json": OUT / "summary.json", "tables_md": OUT / "tables.md"})
        sj = OUT / "summary.json"
        if sj.is_file():
            head = json.loads(sj.read_text()).get("headline", {})
            res.metrics["headline"] = {s: {"invariance_ratio": h.get("R"), "rank1": h.get("rank1"),
                                           "pooled_rank1": h.get("pooled_rank1"), "n": h.get("n")}
                                       for s, h in head.items()}
        if a.ledger:
            res.notes = f"ledger rows kind=loss-trust dataset {DATASET}: invariance_ratio_<score>, rank1_rate_<score>"
            for s, h in res.metrics.get("headline", {}).items():
                for metric, val in ((f"invariance_ratio_{s}", h["invariance_ratio"]), (f"rank1_rate_{s}", h["rank1"])):
                    res.add_ledger_row({"kind": "loss-trust", "engine": "native", "dataset": DATASET,
                                        "metric": metric, "value": val})
    return rc


if __name__ == "__main__":
    sys.exit(main())
