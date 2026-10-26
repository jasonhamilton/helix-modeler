"""slot-dataset-v1 (T208, learning): single-block training data for the chain slots other than drive.

Slot order (human 2026-10-22): drive > amp > cab > mod > delay > reverb > EQ, every slot optional. Drive has its own
dataset (drive-dataset-v1, scripts/drive_dataset_render.py); this module plans and trains the other six with the
same conventions: Guitar-TECHS P1/P2 DI files (P3 never read), random 10 s active excerpts, every free knob uniform
on the CC grid with level-type knobs at their default, frozen AFx-Rep `mid` multi-crop features, audio dropped.

Every design choice below is a PROPOSAL until the human approves it (CLAUDE.md, 2026-10-18); see
docs/notes/slot-datasets-v1.md "Decisions for the human". Nothing here is adopted.

Pure functions (no Native, no audio decoding) so the planning and the scoring can be tested with fakes:
  slot_pool(...)          model -> free params of a slot (the user's catalog and Native library, read at runtime)
  family_of(slot, model)  amp/preamp twins and legacy/mic-IR cab twins share a family; other slots: one per model
  make_slot_plan(...)     the seeded plan (clips, shards, file splits)
  knob_matrix(...)        (n, W) knob targets 0..1: column j = the model's j-th drawn knob, nan where it has none
  score_slot(...)         model / family top-k, knob within +-tol (true model and own top-1), coverage
  run_train_slot(...)     the T189 heads (logreg, MLP), multi-crop, calibrated as T189, three player arms
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import afx_heads as ah

SR = 48_000
CLIP_S = 10.0
SLOTS = ("amp", "cab", "mod", "delay", "reverb", "eq")
CHAIN_ORDER = ("drive", "amp", "cab", "mod", "delay", "reverb", "eq")   # human 2026-10-22
DRIVE_LEVEL_NAMES = {"Level", "Output", "Volume", "FuzzLevel", "ODLevel", "Level 1", "Level 2"}  # = drive-dataset-v1
SEED = 208
SHARD_SIZE = 100
SPLIT_WEIGHT = {"train": 1.0, "cal": 0.5, "test": 0.4, "v1test": 0.25}   # = drive-dataset-v1
KNOB_TOL = 0.15

# Per-slot settings (proposals). `target`: recommended clip count; `lead_s`: random lead-in before the 10 s clip
# (read, rendered, then dropped) so time-based effects start at a random LFO phase with tails from earlier playing;
# `keep_default`: knobs left at their catalog default (level-type, plus cab Early Reflections, which the Stomp checks).
SLOT_CONFIG = {
    "amp": {"category": "amp", "target": 120_000, "lead_s": (0.0, 0.0),
            "keep_default": DRIVE_LEVEL_NAMES | {"Ch Vol", "ChVol"}},
    "cab": {"category": "cab", "target": 50_000, "lead_s": (0.0, 0.0),
            "keep_default": DRIVE_LEVEL_NAMES | {"Early Reflections", "EarlyReflections"}},
    "mod": {"category": "mod", "target": 12_000, "lead_s": (0.5, 2.0), "keep_default": set(DRIVE_LEVEL_NAMES)},
    "delay": {"category": "delay", "target": 45_000, "lead_s": (0.5, 2.0), "keep_default": set(DRIVE_LEVEL_NAMES)},
    "reverb": {"category": "reverb", "target": 6_000, "lead_s": (0.5, 2.0), "keep_default": set(DRIVE_LEVEL_NAMES)},
    "eq": {"category": "eq", "target": 8_000, "lead_s": (0.0, 0.0), "keep_default": set(DRIVE_LEVEL_NAMES)},
}
LABEL_KEYS = ("id", "slot", "model", "family", "cc", "di_file", "start", "lead", "player", "split", "di_level_db")


# ------------------------------------------------------------------------------------------ pool

def slot_pool(slot: str, catalog=None, all_native: bool = False) -> dict:
    """model -> free params (name, lo, hi, default) for `slot`, read from the user's catalog and Native library.

    Default: `match_block.category_pool` (every searchable model; for mod and reverb this is the approved Native
    stand-in list, DECISIONS 2026-10-16). `all_native`: every Native model of the category that is not
    time-variant, not refused by Native and not a routing block (needs a stand-in ruling from the human)."""
    import match_block as MB
    from tonematch.catalog import load_catalog

    cat = SLOT_CONFIG[slot]["category"]
    catalog = catalog or load_catalog()
    if not all_native:
        return dict(sorted(MB.category_pool(cat, catalog).items()))
    import synthetic_v1 as SV
    from tonematch.catalog import native_unavailable
    from tonematch.engines import native_library as NL

    idx = NL.load_index()["models"]
    drop = {m for m, e in idx.items() if e.get("time_variant")} | set(native_unavailable()) \
        | set(NL.time_variant_models())
    out = {}
    for m in sorted(m for m, e in idx.items() if e["category"] == cat and m not in drop and not NL.is_routing(m)):
        tpl = NL.template_for(m)
        if tpl is None:
            continue
        fp = SV.free_params(m, tpl, catalog, cat)
        if fp:
            out[m] = fp
    return out


def family_of(slot: str, model: str) -> str:
    """Proposal: amp and preamp twins share a family (HD2_AmpX / HD2_PreampX -> X); a legacy cab and its mic-IR twin
    share one (HD2_CabX / HD2_CabMicIr_X -> X). Every other slot: one family per model."""
    if slot == "amp":
        for p in ("HD2_Preamp", "HD2_Amp"):
            if model.startswith(p):
                return model[len(p):] or model
    if slot == "cab":
        for p in ("HD2_CabMicIr_", "HD2_Cab"):
            if model.startswith(p):
                return model[len(p):] or model
    return model


def drawn_knobs(free, keep_default) -> list[str]:
    """The knobs a clip draws, in the model's own (sorted template) order: every free knob except keep_default."""
    return [n for n, *_ in free if n not in keep_default]


# ------------------------------------------------------------------------------------------ plan

def drive_file_splits(plan_path: Path) -> dict[str, str]:
    """file -> split of drive-dataset-v1 (so a DI file held out for drive is held out for every slot)."""
    p = Path(plan_path)
    if not p.is_file():
        return {}
    files = json.loads(p.read_text()).get("files", {})
    return {f: s for s, fs in files.items() for f in fs}


def make_slot_plan(slot: str, manifest: dict, pool: dict, read_audio, *, splits: dict[str, str] | None = None,
                   seed: int = SEED, target: int | None = None, shard_size: int = SHARD_SIZE, limit: int = 0,
                   lead_s: tuple | None = None, keep_default=None, all_native: bool = False) -> dict:
    """The full plan (pure given read_audio(path) -> mono float array at 48 kHz).

    `splits` file -> train/cal/test/v1test (default: drive-dataset-v1's, passed in by the caller); when empty, the
    drive rule is applied afresh (DD.assign_file_splits). P3 files are never read."""
    import drive_dataset_render as DD
    from tonematch.dry_rebuild import loud_level_db

    cfg = SLOT_CONFIG[slot]
    target = int(target or cfg["target"])
    lead_lo, lead_hi = lead_s if lead_s is not None else cfg["lead_s"]
    keep = set(cfg["keep_default"] if keep_default is None else keep_default)
    rng = np.random.default_rng([seed, SLOTS.index(slot)])
    files = DD.eligible_files(manifest)
    if not splits:
        splits = DD.assign_file_splits(files, DD.v1_splits(), rng)
    files = [f for f in files if f in splits and DD.player_of(f) in ("P1", "P2")]
    starts = {}
    for f in files:
        s = DD.active_starts(read_audio(f))
        if s:
            starts[f] = s
    files = [f for f in files if f in starts]
    n = min(target, limit) if limit else target
    w = np.array([SPLIT_WEIGHT[splits[f]] for f in files])
    file_idx = rng.choice(len(files), size=n, p=w / w.sum())
    if limit and n >= 4:   # a tiny plan still touches every split that exists
        need = [s for s in ("train", "test", "v1test", "cal") if any(splits[f] == s for f in files)]
        for k, s in enumerate(need[:n]):
            file_idx[k] = int(rng.choice([i for i, f in enumerate(files) if splits[f] == s]))
    models = sorted(pool)
    knobs = {m: drawn_knobs(pool[m], keep) for m in models}
    clips = []
    for k, fi in enumerate(file_idx):
        f = files[int(fi)]
        m = models[int(rng.integers(len(models)))]
        cc = {nm: int(rng.integers(0, 128)) for nm in knobs[m]}
        s = int(starts[f][int(rng.integers(len(starts[f])))])
        lead = int(round(rng.uniform(lead_lo, lead_hi) * SR)) if lead_hi > 0 else 0
        lead = min(lead, s)
        clips.append({"id": f"c{k:06d}", "slot": slot, "model": m, "family": family_of(slot, m), "cc": cc,
                      "di_file": f, "start": s - lead, "lead": lead, "player": DD.player_of(f), "split": splits[f]})
    # the input level of each clip's 10 s window (T194 confound): loud-frame dBFS, a label, not a gain
    by_file: dict[str, list[dict]] = {}
    for c in clips:
        by_file.setdefault(c["di_file"], []).append(c)
    nclip = int(CLIP_S * SR)
    for f, cs in by_file.items():
        x = read_audio(f)
        for c in cs:
            s0 = c["start"] + c["lead"]
            c["di_level_db"] = round(loud_level_db(np.asarray(x[s0:s0 + nclip], np.float32)), 2)
    order = rng.permutation(len(clips))
    clips = [clips[int(i)] for i in order]
    shards = [{"id": f"s{j // shard_size:05d}", "clips": [c["id"] for c in clips[j:j + shard_size]]}
              for j in range(0, len(clips), shard_size)]
    file_rows = {s: sorted(f for f in files if splits[f] == s) for s in ("train", "cal", "test", "v1test")}
    return {"brief": f"T208 slot-dataset-v1 {slot}; single block on a raw DI; measurement only, nothing adopted; "
                     "P3 never read", "slot": slot, "category": cfg["category"], "seed": seed,
            "n_clips": len(clips), "target": target, "shard_size": shard_size, "models": models,
            "families": {m: family_of(slot, m) for m in models}, "knobs": knobs, "all_native": bool(all_native),
            "keep_default": sorted(keep), "lead_s": [lead_lo, lead_hi], "files": file_rows,
            "n_files": {s: len(v) for s, v in file_rows.items()}, "clip_s": CLIP_S, "level": "raw DI (as drive-dataset-v1)",
            "features": {"single_s": 6.0, "ncrop": 3, "embedding": "AFx-Rep mid, frozen"},
            "clips": clips, "shards": shards}


# ------------------------------------------------------------------------------------------ train / score

def knob_matrix(labels: list[dict], models: list[str], knobs: dict[str, list[str]]) -> np.ndarray:
    """(n, W) knob positions 0..1; column j = knobs[model][j]; nan where the model has no j-th knob."""
    W = max((len(knobs[m]) for m in models), default=0) or 1
    K = np.full((len(labels), W), np.nan)
    for i, r in enumerate(labels):
        for j, nm in enumerate(knobs[r["model"]]):
            if nm in r["cc"]:
                K[i, j] = r["cc"][nm] / 127.0
    return K


def families_index(models: list[str], fam_map: dict[str, str]) -> tuple[list[str], np.ndarray]:
    fams = sorted(set(fam_map[m] for m in models))
    fi = {f: i for i, f in enumerate(fams)}
    return fams, np.array([fi[fam_map[m]] for m in models])


def score_slot(out: dict, y_m: np.ndarray, K: np.ndarray, mod_fam: np.ndarray, cal, *, tol: float = KNOB_TOL,
               knob_names: list[list[str]] | None = None) -> dict:
    """Model/family accuracy, and knob accuracy pooled over every (clip, knob) the true model has.

    knob_within_true: |pred - true| <= tol with the true model's knob predictions.
    knob_within_top1: the same with the head's own top-1 model; a wrong model counts as a miss for all its knobs
                      (column j of another model is another knob).
    all_within_true:  share of clips whose every knob is within tol (true model).
    by_name:          knob_within_true per knob name (needs knob_names[model] lists)."""
    n = len(y_m)
    T = cal.T if cal else 1.0
    lj, lf = ah.joint_logp(out["fl"], out["lc"], mod_fam, T)
    lj1, _ = ah.joint_logp(out["fl"], out["lc"], mod_fam, 1.0)
    rank = np.argsort(-lj, 1)
    top1 = rank[:, 0]
    y_f = mod_fam[y_m]
    r = {"n": int(n), "model_top1": float((top1 == y_m).mean()) if n else math.nan,
         "model_top3": float((rank[:, :3] == y_m[:, None]).any(1).mean()) if n else math.nan,
         "model_top5": float((rank[:, :5] == y_m[:, None]).any(1).mean()) if n else math.nan,
         "family_top1": float((lf.argmax(1) == y_f).mean()) if n else math.nan,
         "ece_raw": ah.ece(np.exp(lj1.max(1)), lj1.argmax(1) == y_m),
         "ece_cal": ah.ece(np.exp(lj.max(1)), top1 == y_m), "temperature": T, "tol": tol}
    idx = np.arange(n)
    mu = out["mu"][idx, y_m]                   # (n, W) true model
    sg = out["sig"][idx, y_m]
    ok = np.isfinite(K) & np.isfinite(mu)
    err = np.abs(K - mu)
    hit = np.where(ok, err <= tol, False)
    has = np.isfinite(K)
    r["knob_within_true"] = float(hit[has].mean()) if has.any() else math.nan
    r["knob_mae_true"] = float(np.nanmean(np.where(ok, err, np.nan))) if ok.any() else math.nan
    r["all_within_true"] = float(np.mean([hit[i][has[i]].all() for i in range(n) if has[i].any()])) \
        if has.any() else math.nan
    right = (top1 == y_m)[:, None]
    r["knob_within_top1"] = float((hit & right)[has].mean()) if has.any() else math.nan
    if cal is not None:
        s = np.array([cal.scale[k] if k < len(cal.scale) else 1.0 for k in range(K.shape[1])])[None, :]
        v = ok & np.isfinite(sg)
        r["knob_cover68_global_true"] = float((err[v] <= ah.Z68 * (sg * s)[v]).mean()) if v.any() else math.nan
        r["knob_cover95_global_true"] = float((err[v] <= ah.Z95 * (sg * s)[v]).mean()) if v.any() else math.nan
    if knob_names is not None:
        by: dict[str, list[bool]] = {}
        for i in range(n):
            for j, nm in enumerate(knob_names[int(y_m[i])]):
                if has[i, j]:
                    by.setdefault(nm, []).append(bool(hit[i, j]))
        r["by_name"] = {nm: {"within_true": float(np.mean(v)), "n": len(v)} for nm, v in sorted(by.items())}
    return r


EVALS = (("test", "held-out DI files (drive-dataset-v1 test files; no head has trained on them)"),
         ("v1test", "the T186/T189 test files (held out for drive too)"))


def run_train_slot(out: Path, models_dir: Path, *, heads=("logreg", "mlp"), epochs: int = 60, seeds_main: int = 1,
                   arms=("main", "p2p1", "p1p2"), ledger: bool = True, device: str = "cpu", tol: float = KNOB_TOL,
                   log=print, extra_outs=(), min_out_db: float | None = None, fraction: float = 1.0) -> dict:
    """Retrain the T189 heads on one slot's shards and evaluate on its held-out files. Writes summary.{json,md}
    and (main arm, seed 0) checkpoints; appends `benchmark / - / slot-dataset-v1-<slot>` ledger rows.

    `fraction` < 1 (T213, the approved half-size arm): only the clips of the first ceil(fraction x shards) shards of
    the main plan, in plan order (the plan's clips are shuffled before sharding, so this is a seeded random subset),
    and the refill clips of those same clip ids."""
    import pickle
    import warnings

    import torch
    torch.set_num_threads(int(os.environ.get("TONEMATCH_TRAIN_THREADS", "2")))
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    from sklearn.model_selection import GroupKFold

    import drive_dataset_render as DD
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text

    out = Path(out)
    plan = json.loads((out / "plan.json").read_text())
    slot = plan["slot"]
    t_start = time.time()
    L, _S, C = DD.load_new(out)
    n_refill = n_dropped = 0
    refill_ids: set[str] = set()
    for eo in extra_outs:          # T210: refill shards of failed clips (same clip ids, never in the main shards)
        L2, _S2, C2 = DD.load_new(eo)
        if min_out_db is not None:
            keep = np.array([r.get("out_db", 0.0) >= min_out_db for r in L2], bool)
            n_dropped += int((~keep).sum())
            L2, C2 = [r for r, k in zip(L2, keep) if k], C2[keep]
        ids = {r["id"] for r in L}
        assert not ids & {r["id"] for r in L2}, f"refill clip ids already in the main shards ({eo})"
        n_refill += len(L2)
        refill_ids |= {r["id"] for r in L2}
        L, C = L + L2, (np.concatenate([C, C2]) if len(L2) else C)
    n_before_fraction = len(L)
    if fraction < 1.0:
        k = max(1, math.ceil(fraction * len(plan["shards"])))
        keep_ids = {cid for s in plan["shards"][:k] for cid in s["clips"]}
        sel = np.array([r["id"] in keep_ids for r in L], bool)
        L, C = [r for r, k_ in zip(L, sel) if k_], C[sel]
        n_refill = sum(1 for r in L if r["id"] in refill_ids)
    if not L:
        raise RuntimeError(f"no finished shards in {out / 'shards'}")
    models = plan["models"]
    fams, mod_fam = families_index(models, plan["families"])
    mi = {m: i for i, m in enumerate(models)}
    y_m = np.array([mi[r["model"]] for r in L])
    K = knob_matrix(L, models, plan["knobs"])
    names = [plan["knobs"][m] for m in models]
    files = np.array([r["di_file"] for r in L])
    player = np.array([r["player"] for r in L])
    split = np.array([r["split"] for r in L])
    assert not any(p == "P3" for p in player), "P3 in the training data"
    tr_all, ca_all = split == "train", split == "cal"
    ev = {"test": split == "test", "v1test": split == "v1test"}
    assert not (set(files[ev["test"] | ev["v1test"]]) & set(files[tr_all | ca_all])), "test DI files leak"
    res = {"brief": f"T208 slot-dataset-v1 {slot}: T189 heads (multi-crop) on single-{slot} Native renders; "
                    "measurement only, nothing adopted", "slot": slot, "n": len(L), "n_models": len(models),
           "n_families": len(fams), "knob_columns": int(K.shape[1]), "tol": tol, "device": device,
           "n_refill": n_refill, "n_refill_dropped_quiet": n_dropped, "min_out_db": min_out_db,
           "fraction": float(fraction), "n_before_fraction": n_before_fraction,
           "counts": {k: {"clips": int(m.sum()), "files": len(set(files[m]))}
                      for k, m in {"train": tr_all, "cal": ca_all, **ev}.items()},
           "results": {}, "pooling": "multi (3 x 3.33 s crops, predictions averaged)"}
    log(json.dumps(res["counts"]))

    def make(head, seed):
        if head == "logreg":
            return ah.LinearHeads(mod_fam=mod_fam, alpha=1.0)
        return ah.MLPHeads(mod_fam=mod_fam, seed=seed, epochs=epochs, device=device)

    def oof(head, tr, seed):
        outs = {}
        g = files[tr]
        nf = min(5, len(set(g)))
        if nf < 2:
            return None
        for f_tr, f_va in GroupKFold(nf).split(tr, groups=g):
            h = make(head, seed).fit(C[tr[f_tr]], y_m[tr[f_tr]], K[tr[f_tr]])
            o = h.predict(C[tr[f_va]])
            for i, j in enumerate(tr[f_va]):
                outs[j] = {k: v[i] for k, v in o.items()}
        return {k: np.stack([outs[j][k] for j in tr]) for k in ("fl", "lc", "mu", "sig")}
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    for arm in arms:
        pl_tr = {"main": None, "p2p1": "P2", "p1p2": "P1"}[arm]
        pl_te = {"main": None, "p2p1": "P1", "p1p2": "P2"}[arm]
        trm, cam = tr_all.copy(), ca_all.copy()
        if pl_tr:
            trm, cam = trm & (player == pl_tr), cam & (player == pl_tr)
        tr, ca = np.where(trm)[0], np.where(cam)[0]
        if len(set(files[tr])) < 2 or len(set(y_m[tr])) < 2:
            log(f"{arm}: too little training data; skipped")
            continue
        for head in heads:
            for seed in range(seeds_main if (head == "mlp" and arm == "main") else 1):
                t0 = time.time()
                key = f"{arm}/{head}/multi/s{seed}"
                h = make(head, seed).fit(C[tr], y_m[tr], K[tr])
                po = oof(head, tr, seed)
                pidx = tr
                if po is not None and len(ca):
                    o_ca = h.predict(C[ca])
                    po = {k: np.concatenate([po[k], o_ca[k]]) for k in po}
                    pidx = np.concatenate([tr, ca])
                cal = ah.calibrate(po, y_m[pidx], K[pidx], mod_fam, files[pidx], player[pidx]) if po else None
                r = {"train_clips": len(tr), "train_files": len(set(files[tr])), "cal_clips": len(ca)}
                for ename, em in ev.items():
                    te = np.where(em & (player == pl_te) if pl_te else em)[0]
                    if len(te) == 0:
                        continue
                    r[ename] = score_slot(h.predict(C[te]), y_m[te], K[te], mod_fam, cal, tol=tol, knob_names=names)
                    r[ename]["files"] = len(set(files[te]))
                r["fit_s"] = time.time() - t0
                res["results"][key] = r
                log(key + " " + json.dumps({e: (round(r[e]["model_top1"], 3), round(r[e]["knob_within_true"], 3))
                                            for e, _ in EVALS if e in r}) + f" {r['fit_s']:.0f}s")
                if arm == "main" and seed == 0:
                    if head == "mlp":
                        h.net = h.net.cpu().state_dict()
                        h.device = "cpu"
                    with open(models_dir / f"heads_{arm}_{head}_multi.pkl", "wb") as fh:
                        pickle.dump({"heads": h, "calibration": cal, "models": models, "families": fams,
                                     "family_of": plan["families"], "knobs": plan["knobs"], "slot": slot,
                                     "note": "T208 measurement only; not adopted"}, fh)
                atomic_write_json(out / "summary.json", res, indent=1, default=float)
    res["elapsed_s"] = time.time() - t_start
    atomic_write_json(out / "summary.json", res, indent=1, default=float)
    atomic_write_text(out / "summary.md", summary_md(res))
    if ledger:
        append_ledger(res)
    return res


def _f(r: dict, k: str) -> str:
    v = r.get(k)
    return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.2f}"


def summary_md(res: dict) -> str:
    slot = res["slot"]
    out = [f"# slot-dataset-v1 {slot} (T208): results", "",
           f"Measurement only; nothing adopted. T189 heads, multi-crop pooling, on single-{slot} Native renders "
           f"({res['n_models']} models, {res['n_families']} families, {res['knob_columns']} knob columns). Knob "
           f"accuracy: within +-{res['tol']:.2f} of the true position, pooled over every knob of the true model.", "",
           f"Training data: {res['n']} clips (train fraction {res.get('fraction', 1.0):g} of "
           f"{res.get('n_before_fraction', res['n'])}; refill clips {res.get('n_refill', 0)}, quiet refill clips "
           f"dropped {res.get('n_refill_dropped_quiet', 0)}).", "",
           "Counts: " + ", ".join(f"{k} {v['clips']} clips / {v['files']} files" for k, v in res["counts"].items()), ""]
    titles = {"main": "Within player", "p2p1": "Cross player P2 -> P1", "p1p2": "Cross player P1 -> P2"}
    for arm, title in titles.items():
        for ename, edesc in EVALS:
            rows = [(k, v[ename]) for k, v in res["results"].items() if k.startswith(arm + "/") and ename in v]
            if not rows:
                continue
            out += [f"## {title}: {edesc}", "",
                    "| Head | Model top-1 | Top-3 | Top-5 | Family top-1 | Knobs within, true / own | All knobs within (true) "
                    "| Knob MAE (true) | Cover 68 / 95 | ECE raw / cal | n (files) |",
                    "|---|---|---|---|---|---|---|---|---|---|---|"]
            for k, r in rows:
                _, head, _pool, seed = k.split("/")
                out.append(f"| {head} {seed} | {_f(r, 'model_top1')} | {_f(r, 'model_top3')} | {_f(r, 'model_top5')} | {_f(r, 'family_top1')} "
                           f"| {_f(r, 'knob_within_true')} / {_f(r, 'knob_within_top1')} | {_f(r, 'all_within_true')} "
                           f"| {_f(r, 'knob_mae_true')} | {_f(r, 'knob_cover68_global_true')} / "
                           f"{_f(r, 'knob_cover95_global_true')} | {_f(r, 'ece_raw')} / {_f(r, 'ece_cal')} "
                           f"| {r.get('n')} ({r.get('files')}) |")
            out.append("")
    main = res["results"].get("main/mlp/multi/s0", {}).get("test", {}).get("by_name")
    if main:
        out += ["## Knob within +-tol by name (main, MLP, held-out files, true model)", "",
                "| Knob | Within | n |", "|---|---|---|"]
        out += [f"| {nm} | {v['within_true']:.2f} | {v['n']} |" for nm, v in main.items()]
        out.append("")
    out.append(f"Train wall {res.get('elapsed_s', 0):.0f} s.")
    return "\n".join(out) + "\n"


def append_ledger(res: dict, root: Path | None = None) -> None:
    root = root or Path(__file__).resolve().parents[3]
    os.environ.setdefault("TONEMATCH_AGENT", "learning")
    ds = f"slot-dataset-v1-{res['slot']}"
    for k, r in res["results"].items():
        arm, head, _pool, seed = k.split("/")
        if seed != "s0":
            continue
        for ename, _ in EVALS:
            if ename not in r:
                continue
            e = r[ename]
            notes = (f"T208 measurement only not adopted. Single {res['slot']} block on raw Guitar-TECHS DI; T189 "
                     f"heads multi-crop; {r['train_clips']} train clips {r['train_files']} files; eval {ename} n "
                     f"{e['n']} files {e['files']}; train fraction {res.get('fraction', 1.0):g}; refill clips "
                     f"{res.get('n_refill', 0)}; top3 {e.get('model_top3', float('nan')):.3f}; top5 {e['model_top5']:.3f}; family {e['family_top1']:.3f}; "
                     f"knob within {res['tol']:.2f} true {e['knob_within_true']:.3f}; P3 unread")
            for metric, val in ((f"{arm}_{head}_{ename}_model_top1", e["model_top1"]),
                                (f"{arm}_{head}_{ename}_knob_within_true", e["knob_within_true"])):
                subprocess.run([sys.executable, str(root / "scripts/ledger_append.py"), "--kind", "benchmark",
                                "--engine", "-", "--dataset", ds, "--metric", metric, "--value", f"{val:.4f}",
                                "--renders", str(res["n"]), "--wall-clock-s", f"{r['fit_s']:.0f}", "--notes", notes],
                               check=False, cwd=str(root))
