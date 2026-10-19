#!/usr/bin/env python3
"""predictor-pilot-v1 (T189, drive only): frozen AFx-Rep embeddings plus small trained heads. Measurement only.

Needs cache/models/predictor-pilot-v1/emb_{single,crops}.npy from scripts/predictor_pilot_v1_embed.py and the T186
index (cache/models/predictor-pilot-v0/index.json: same renders, same split by whole DI file, no P3, no sealed set,
no helix-trust-v2 test split).

Arms (train files -> test files):
  main  both players' train files -> all 29 test files (held-out recordings of known players)
  p2p1  P2 train files -> P1 test files (held-out player)
  p1p2  P1 train files -> P2 test files (held-out player)
Variants: head in {logreg (hierarchical logistic regression + per-model ridge), mlp (3 seeds)} x pooling in
{single (6 s centre crop), multi (3 x 3.33 s crops, predictions averaged), meanemb (mean of the 3 crop embeddings)},
control poolings mid1 (the middle 3.33 s crop alone) and multi2 (the first two crops), plus the T186 reference (flat logistic regression, single crop) and logregfam (single crop; family from a separate
family logistic regression instead of the summed flat model posterior).
Calibration pool: out-of-fold predictions over the arm's train files (5 group folds by file) plus the final model's
predictions on the arm's calibration files; weights balance players and files. Temperature, global and per-family
sigma scales are fitted on that pool only.
Writes cache/models/predictor-pilot-v1/{result.json, heads_main_*.pkl}. Reads no audio.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
V0 = ROOT / "cache/models/predictor-pilot-v0"
OUT = ROOT / "cache/models/predictor-pilot-v1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="main,p2p1,p1p2")
    ap.add_argument("--heads", default="ref,logreg,logregfam,mlp")
    ap.add_argument("--poolings", default="single,multi,meanemb")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--out", default=str(OUT / "result.json"))
    ap.add_argument("--allow-partial", action="store_true",
                    help="debug only: use clips whose crop embedding is not done yet (single pooling only)")
    a = ap.parse_args()
    import torch
    torch.set_num_threads(2)
    from sklearn.model_selection import GroupKFold

    from tonematch.learning import afx_heads as ah
    from tonematch.learning import drive_pilot as dp
    t_start = time.time()
    idx = json.loads((V0 / "index.json").read_text())
    recs, rms = idx["records"], np.array(idx["rms_db"])
    prog = json.loads((OUT / "embed_progress.json").read_text())
    S = np.load(OUT / "emb_single.npy")
    Cr = np.load(OUT / "emb_crops.npy")
    done = np.zeros(len(recs), bool)
    done[prog["done"]] = True
    usable = (done | a.allow_partial) & (rms > -90) & np.any(S != 0, 1)
    fams, models, m2f = dp.families()
    mi, fi = {m: i for i, m in enumerate(models)}, {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[m2f[m]] for m in models])
    y_m = np.array([mi[r["model"]] for r in recs])
    K = np.array([[r["drive"], r["tone"]] for r in recs], float)
    split = np.array([r["split"] for r in recs])
    player = np.array([r["player"] for r in recs])
    files = np.array([r["di_file"] for r in recs])
    src = np.array([r["source"] for r in recs])
    ME = Cr.mean(1)
    ME /= np.maximum(np.linalg.norm(ME, axis=1, keepdims=True), 1e-8)
    feats = {"single": S, "multi": Cr, "meanemb": ME, "mid1": Cr[:, 1], "multi2": Cr[:, :2]}

    def sel(sp, pl=None):
        m = usable & (split == sp)
        if pl:
            m &= player == pl
        return np.where(m)[0]
    arms = {"main": (sel("train"), sel("cal"), sel("test")),
            "p2p1": (sel("train", "P2"), sel("cal", "P2"), sel("test", "P1")),
            "p1p2": (sel("train", "P1"), sel("cal", "P1"), sel("test", "P2"))}
    res = {"brief": "T189 predictor-pilot-v1, drive only, measurement only; nothing adopted",
           "n_usable": int(usable.sum()), "n_records": len(recs),
           "arms": {k: {"train": len(v[0]), "train_files": len(set(files[v[0]])), "cal": len(v[1]),
                        "cal_files": len(set(files[v[1]])), "test": len(v[2]), "test_files": len(set(files[v[2]]))}
                    for k, v in arms.items()},
           "results": {}}
    # Ridge alpha: chosen once, on training files only (main arm, single crop, 3 group folds).
    from sklearn.linear_model import Ridge
    tr = arms["main"][0]
    best_alpha, best_mae = None, np.inf
    for alpha in (0.3, 1.0, 3.0, 10.0):
        errs = []
        for f_tr, f_va in GroupKFold(3).split(tr, groups=files[tr]):
            a_tr, a_va = tr[f_tr], tr[f_va]
            for m in range(len(models)):
                for k in range(2):
                    s_tr = a_tr[(y_m[a_tr] == m) & np.isfinite(K[a_tr, k])]
                    s_va = a_va[(y_m[a_va] == m) & np.isfinite(K[a_va, k])]
                    if len(s_tr) < 5 or not len(s_va):
                        continue
                    p = np.clip(Ridge(alpha=alpha).fit(S[s_tr], K[s_tr, k]).predict(S[s_va]), 0, 1)
                    errs.append(np.abs(p - K[s_va, k]))
        mae = float(np.concatenate(errs).mean())
        res.setdefault("ridge_alpha_cv_mae", {})[str(alpha)] = mae
        if mae < best_mae:
            best_alpha, best_mae = alpha, mae
    res["ridge_alpha"] = best_alpha
    print("ridge alpha", best_alpha, res["ridge_alpha_cv_mae"], flush=True)

    def make(head, seed):
        if head == "logreg":
            return ah.LinearHeads(mod_fam=mod_fam, alpha=best_alpha)
        if head == "logregfam":
            return ah.LinearHeads(mod_fam=mod_fam, alpha=best_alpha, fam_mode="separate")
        return ah.MLPHeads(mod_fam=mod_fam, seed=seed, epochs=a.epochs)

    def oof(head, X, tr, seed):
        outs = {}
        for f_tr, f_va in GroupKFold(5).split(tr, groups=files[tr]):
            h = make(head, seed).fit(X[tr[f_tr]], y_m[tr[f_tr]], K[tr[f_tr]])
            o = h.predict(X[tr[f_va]])
            for i, j in enumerate(tr[f_va]):
                outs[j] = {k: v[i] for k, v in o.items()}
        return {k: np.stack([outs[j][k] for j in tr]) for k in ("fl", "lc", "mu", "sig")}

    def cat(o1, o2):
        return {k: np.concatenate([o1[k], o2[k]]) for k in o1}

    def breakdown(o, te, cal):
        r = {"all": ah.score(o, y_m[te], K[te], mod_fam, cal)}
        for nm, m in (("ds_new_di_seen_settings", src[te] == "ds"), ("trust_new_di_new_settings", src[te] != "ds"),
                      ("P1", player[te] == "P1"), ("P2", player[te] == "P2")):
            if m.sum() >= 50:
                r[nm] = ah.score({k: v[m] for k, v in o.items()}, y_m[te][m], K[te][m], mod_fam, cal)
        return r
    hits = {}
    for arm in a.arms.split(","):
        tr, ca, te = arms[arm]
        for head in a.heads.split(","):
            pools = ["single"] if head in ("ref", "logregfam") else a.poolings.split(",")
            for pool in pools:
                X = feats[pool]
                seeds = range(a.seeds) if head == "mlp" else range(1)
                for seed in seeds:
                    t0 = time.time()
                    key = f"{arm}/{head}/{pool}/s{seed}"
                    if head == "ref":       # T186 reference: flat logistic regression, C=10, single crop
                        from sklearn.linear_model import LogisticRegression
                        clf = LogisticRegression(C=10.0, max_iter=2000).fit(X[tr], y_m[tr])
                        P = np.full((len(te), len(models)), 1e-12)
                        P[:, clf.classes_] = clf.predict_proba(X[te])
                        rk = np.argsort(-P, 1)
                        pf = np.stack([P[:, mod_fam == f].sum(1) for f in range(len(fams))], 1)
                        r = {"all": {"n": len(te), "model_top1": float((rk[:, 0] == y_m[te]).mean()),
                                     "model_top5": float((rk[:, :5] == y_m[te][:, None]).any(1).mean()),
                                     "family_top1": float((pf.argmax(1) == mod_fam[y_m[te]]).mean())}}
                        hits[key] = (rk[:, 0] == y_m[te]).tolist()
                    else:
                        h = make(head, seed).fit(X[tr], y_m[tr], K[tr])
                        pool_o = oof(head, X, tr, seed)
                        if len(ca):
                            pool_o = cat(pool_o, h.predict(X[ca]))
                        pidx = np.concatenate([tr, ca])
                        cal = ah.calibrate(pool_o, y_m[pidx], K[pidx], mod_fam, files[pidx], player[pidx])
                        o = h.predict(X[te])
                        r = breakdown(o, te, cal)
                        r["calibration"] = {"T": cal.T, "scale_drive": cal.scale[0], "scale_tone": cal.scale[1],
                                            "n_files": cal.n_files, "n_players": cal.n_players,
                                            "family_scales": {f"{fams[f]}/{'drive' if k == 0 else 'tone'}": v
                                                              for (f, k), v in cal.fam_scale.items()},
                                            "pool_clips": int(len(pidx))}
                        lj, _ = ah.joint_logp(o["fl"], o["lc"], mod_fam, cal.T)
                        hits[key] = (lj.argmax(1) == y_m[te]).tolist()
                        if arm == "main" and seed == 0:
                            with open(OUT / f"heads_main_{head}_{pool}.pkl", "wb") as fh:
                                if head == "mlp":
                                    h.net = h.net.cpu().state_dict()
                                pickle.dump({"heads": h, "calibration": cal, "models": models, "families": fams},
                                            fh)
                    r["fit_s"] = time.time() - t0
                    res["results"][key] = r
                    print(key, json.dumps({k: round(v, 3) for k, v in r["all"].items()
                                           if k in ("model_top1", "model_top5", "family_top1", "drive_within_true",
                                                    "tone_within_true", "drive_cover68_global_true",
                                                    "tone_cover68_global_true")}),
                          f"{r['fit_s']:.0f}s", flush=True)
                    Path(a.out).write_text(json.dumps(res, indent=1))
    # paired bootstrap over test files: top-1 difference against the T186 reference, per arm. Stratified: the
    # drive-specialist test files (1,500 clips each) and the trust-set files are resampled separately, so each draw
    # keeps the design's mix; plus the trust files alone (new DI and new settings).
    rng = np.random.default_rng(189)
    res["hits"] = {k: "".join("1" if h else "0" for h in v) for k, v in hits.items()}
    for arm in a.arms.split(","):
        te = arms[arm][2]
        tf = files[te]
        is_ds = src[te] == "ds"
        ref = hits.get(f"{arm}/ref/single/s0")
        if ref is None:
            continue
        ref = np.array(ref, float)
        strata = [sorted(set(tf[is_ds])), sorted(set(tf[~is_ds]))]
        rows = {f: np.where(tf == f)[0] for f in set(tf)}

        def draw(only_trust=False):
            out = []
            for i, st in enumerate(strata):
                if only_trust and i == 0:
                    continue
                out += [rows[st[j]] for j in rng.integers(0, len(st), len(st))]
            return np.concatenate(out)
        draws = [draw() for _ in range(2000)]
        draws_t = [draw(True) for _ in range(2000)]
        for key, hv in hits.items():
            if not key.startswith(arm + "/") or "/ref/" in key:
                continue
            hv = np.array(hv, float)
            d = [hv[m].mean() - ref[m].mean() for m in draws]
            dt = [hv[m].mean() - ref[m].mean() for m in draws_t]
            res.setdefault("vs_ref_top1", {})[key] = {
                "diff": float(hv.mean() - ref.mean()),
                "ci95_stratified_over_files": [float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))],
                "diff_trust_files": float(hv[~is_ds].mean() - ref[~is_ds].mean()),
                "ci95_trust_files": [float(np.quantile(dt, 0.025)), float(np.quantile(dt, 0.975))]}
    res["elapsed_s"] = time.time() - t_start
    Path(a.out).write_text(json.dumps(res, indent=1))
    print("done", res["elapsed_s"])


if __name__ == "__main__":
    main()
