#!/usr/bin/env python3
"""Paired CNN vs AFx-Rep comparison for predictor-pilot-v0 (T186), plus AFx-Rep on the cross-player split.

Re-extracts the log-mel features in RAM (no disk), runs the saved CNN (`all` arm) on the 6 s centre crop, refits
the AFx-Rep logistic regression from cached embeddings, and bootstraps the top-1 / top-5 difference over test DI
files (the unit of the split). Writes cache/models/predictor-pilot-v0/paired.json. Measurement only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predictor_pilot_v0 as pp
import torch

from tonematch.learning import drive_pilot as dp


def main():
    X = pp.cmd_features(ram=True)["X"]
    D = pp.Data(X=X)
    dev = pp.device()
    ck = torch.load(pp.OUT / "all" / "model.pt", map_location="cpu")
    net = dp.make_model(len(D.fams), len(D.models))
    net.load_state_dict(ck["state"])
    net = net.to(dev)
    te, tr = D.sel("test"), D.sel("train")
    o, mod_fam = pp.infer(net, D, te, dev, pp.CROP)
    lp, _ = dp.joint_logp(o["fl"], o["ml"], mod_fam, len(D.fams), ck["temperature"])
    Pc = lp.exp().numpy()
    E = np.load(pp.OUT / "afx" / "emb.npy")
    from sklearn.linear_model import LogisticRegression

    def afx_fit(tr_ids, te_ids):
        clf = LogisticRegression(C=10.0, max_iter=2000).fit(E[tr_ids], D.y_m[tr_ids])
        P = np.zeros((len(te_ids), len(D.models)))
        P[:, clf.classes_] = clf.predict_proba(E[te_ids])
        return P
    Pa = afx_fit(tr, te)
    y = D.y_m[te]

    def hits(P, k):
        return (np.argsort(-P, 1)[:, :k] == y[:, None]).any(1)
    files = np.array([D.recs[j]["di_file"] for j in te])
    uf = np.unique(files)
    rng = np.random.default_rng(186)
    out = {"test_n": len(te), "test_files": len(uf)}
    for k in (1, 5):
        hc, ha = hits(Pc, k), hits(Pa, k)
        he = hits(0.5 * Pc + 0.5 * Pa, k)
        diffs = []
        for _ in range(2000):
            s = rng.choice(uf, len(uf))
            m = np.concatenate([np.where(files == f)[0] for f in s])
            diffs.append(hc[m].mean() - ha[m].mean())
        out[f"top{k}"] = {"cnn": float(hc.mean()), "afx_logreg": float(ha.mean()), "mean_of_both": float(he.mean()),
                          "cnn_minus_afx": float(hc.mean() - ha.mean()),
                          "ci95_over_files": [float(np.quantile(diffs, 0.025)), float(np.quantile(diffs, 0.975))]}
    # AFx-Rep on the cross-player split (train P2 train files, test P1 test files), as the CNN xplayer arm
    trx, tex = D.sel("train", "P2"), D.sel("test", "P1")
    Px = afx_fit(trx, tex)
    yx = D.y_m[tex]
    rk = np.argsort(-Px, 1)
    pf = np.stack([Px[:, mod_fam.numpy() == f].sum(1) for f in range(len(D.fams))], 1)
    out["afx_xplayer"] = {"n": len(tex), "model_top1": float((rk[:, 0] == yx).mean()),
                          "model_top5": float((rk[:, :5] == yx[:, None]).any(1).mean()),
                          "family_top1": float((pf.argmax(1) == D.y_f[tex]).mean())}
    (pp.OUT / "paired.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
