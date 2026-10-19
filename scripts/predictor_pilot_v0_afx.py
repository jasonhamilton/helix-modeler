#!/usr/bin/env python3
"""Paired AFx-Rep baseline for predictor-pilot-v0 (T186, arm B of docs/notes/predictor-design.md, drive only).

Same index, same train/test files and same 6 s centre crop as the CNN. AFx-Rep (frozen) embeds each clip; then
  * model: multinomial logistic regression on the train embeddings (family p = sum of member p);
  * model: nearest-centroid by cosine (the gallery-style shortlist T181/T183 used, here with train-DI centroids);
  * Drive / Tone, oracle model: mean of the 5 nearest train clips of the true model.
Writes cache/models/predictor-pilot-v0/afx/{emb.npy, result.json}. Measurement only.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
OUT = Path(__file__).resolve().parents[1] / "cache/models/predictor-pilot-v0"


def main():
    import torch
    torch.set_num_threads(2)
    from tonematch.learning import drive_pilot as dp
    from tonematch.metrics import afxrep
    idx = json.loads((OUT / "index.json").read_text())
    recs, rms = idx["records"], np.array(idx["rms_db"])
    fams, models, m2f = dp.families()
    mi = {m: i for i, m in enumerate(models)}
    fi = {f: i for i, f in enumerate(fams)}
    use = np.array([r["split"] in ("train", "test") for r in recs]) & (rms > -90)
    ids = np.where(use)[0]
    od = OUT / "afx"
    od.mkdir(exist_ok=True)
    ef = od / "emb.npy"
    emb = afxrep.Embedder()
    t0 = time.time()
    if ef.is_file():
        E = np.load(ef)
    else:
        E = np.zeros((len(recs), 512), np.float32)
        B = 16
        for i in range(0, len(ids), B):
            b = ids[i:i + B]
            clips = []
            for j in b:
                a = np.load(recs[j]["path"]).astype(np.float32).reshape(-1)
                n = len(a)
                L = 6 * dp.SR
                s = max(0, (n - L) // 2)
                clips.append(a[s:s + L])
            out = emb.embed_batch(clips)
            for j, f in zip(b, out):
                E[j] = f.mid
            if i % (B * 100) == 0:
                print(f"{i}/{len(ids)} {time.time() - t0:.0f}s on {emb.device}", flush=True)
        np.save(ef, E)
    t_emb = time.time() - t0
    tr = np.array([j for j in ids if recs[j]["split"] == "train"])
    te = np.array([j for j in ids if recs[j]["split"] == "test"])
    ym = np.array([mi[r["model"]] for r in recs])
    yf = np.array([fi[r["family"]] for r in recs])
    yk = np.array([[r["drive"], r["tone"]] for r in recs], float)
    mod_fam = np.array([fi[m2f[m]] for m in models])
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(C=10.0, max_iter=2000)
    clf.fit(E[tr], ym[tr])
    P = np.zeros((len(te), len(models)))
    P[:, clf.classes_] = clf.predict_proba(E[te])
    res = {"embed_s": t_emb, "device": emb.device, "train_n": len(tr), "test_n": len(te)}
    srcs = np.array([recs[j]["source"] for j in te])

    def scores(P, name):
        rank = np.argsort(-P, 1)
        out = {}
        for sub, m in (("all", np.ones(len(te), bool)), ("ds_new_di_seen_settings", srcs == "ds"),
                       ("trust_new_di_new_settings", srcs != "ds")):
            y = ym[te][m]
            pf = np.stack([P[m][:, mod_fam == f].sum(1) for f in range(len(fams))], 1)
            out[sub] = {"n": int(m.sum()), "model_top1": float((rank[m, 0] == y).mean()),
                        "model_top5": float((rank[m, :5] == y[:, None]).any(1).mean()),
                        "family_top1": float((pf.argmax(1) == yf[te][m]).mean())}
        res[name] = out
    scores(P, "logreg")
    C = np.stack([E[tr][ym[tr] == k].mean(0) for k in range(len(models))])
    C /= np.linalg.norm(C, axis=1, keepdims=True)
    scores(E[te] @ C.T, "centroid")
    # knobs, oracle model: 5 nearest train clips of the true model
    tol = (0.15, 0.10)
    kn = {"drive": [], "tone": []}
    ksrc = {"drive": [], "tone": []}
    for t in te:
        cand = tr[ym[tr] == ym[t]]
        nn = cand[np.argsort(-(E[cand] @ E[t]))[:5]]
        for k, name in ((0, "drive"), (1, "tone")):
            if not math.isnan(yk[t, k]):
                kn[name].append(abs(np.nanmean(yk[nn, k]) - yk[t, k]))
                ksrc[name].append(recs[t]["source"] == "ds")
    for k, name in ((0, "drive"), (1, "tone")):
        e = np.array(kn[name])
        res[f"knn5_{name}_oracle_within_tol"] = float((e <= tol[k]).mean())
        res[f"knn5_{name}_oracle_mae"] = float(e.mean())
        d = np.array(ksrc[name])
        res[f"knn5_{name}_oracle_within_tol_ds"] = float((e[d] <= tol[k]).mean())
        res[f"knn5_{name}_oracle_within_tol_trust"] = float((e[~d] <= tol[k]).mean())
    (od / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
