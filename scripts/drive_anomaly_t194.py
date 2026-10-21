#!/usr/bin/env python3
"""T194 (learning): retrain-only measurement for the drive-dataset-v1 Drive anomaly. Measurement only; nothing adopted.

No renders. Reads the T191 shards (cache/drive-dataset-v1/shards), the T186 index + T189 embeddings, and a per-clip
input-level table (--levels: clip id -> [raw loud dBFS, peak, gain_db], where gain_db is the gain the old
helix_trust_set.load_excerpt pipeline (-26.7 dBFS loud level, 0.89 peak cap) would have applied to that DI excerpt).

Arms (MLP heads of T189, multi-crop, one seed, no out-of-fold pool: calibration is fitted on the calibration files
only, the same way for every arm, so arms compare with each other, not with summary-150k):
  v1        old train only (normalised-DI renders)
  v2        old train + all new train (as T191 v2data)
  v2rand    old train + a random subset of new train, the size of v2lev
  v2lev     old train + the new train clips whose DI was within --max-gain dB of the old reference level
  v2cond    as v2, with the input gain (dB / 10) appended to the embedding (old records: 0 dB)
Writes <out>/result.json and prints a table. Uses tonematch.engines.host_budget.LoadGuard between fits.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def bias_table(o, te, y_m, K, gains=None):
    n = len(te)
    mu = o["mu"][np.arange(n), y_m[te], 0]
    k = K[te, 0]
    ok = np.isfinite(k)
    r = mu - k
    out = {"drive_bias": float(np.mean(r[ok])), "drive_mae": float(np.mean(np.abs(r[ok])))}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", required=True)
    ap.add_argument("--out", default=str(ROOT / "cache/drive-anomaly-t194"))
    ap.add_argument("--max-gain", type=float, default=3.0)
    ap.add_argument("--arms", default="main,p1p2")
    ap.add_argument("--variants", default="v1,v2,v2rand,v2lev,v2cond")
    ap.add_argument("--epochs", type=int, default=60)
    a = ap.parse_args()
    import torch
    torch.set_num_threads(2)
    import drive_dataset_render as D
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.learning import afx_heads as ah
    from tonematch.learning import drive_pilot as dp

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    guard = LoadGuard()
    lev = json.loads(Path(a.levels).read_text())
    new_l, _, new_C = D.load_new(D.OUT_DEFAULT)
    old_l, _, old_C = D.load_old()
    for r in new_l:
        r["source"] = "new"
    L = old_l + new_l
    C = np.concatenate([old_C, new_C])
    del old_C, new_C
    fams, models, m2f = dp.families()
    mi = {m: i for i, m in enumerate(models)}
    fi = {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[m2f[m]] for m in models])
    y_m = np.array([mi[r["model"]] for r in L])
    K = np.array([[float(r["drive"]), float(r["tone"])] for r in L], float)
    files = np.array([r["di_file"] for r in L])
    player = np.array([r["player"] for r in L])
    is_new = np.array([r["source"] == "new" for r in L])
    split = np.array([r["split"] for r in L])
    gain = np.array([lev[r["id"]][2] if r["source"] == "new" else 0.0 for r in L])
    old_tr, old_ca, old_te = (~is_new) & (split == "train"), (~is_new) & (split == "cal"), (~is_new) & (split == "test")
    new_tr, new_ca = is_new & (split == "train"), is_new & (split == "cal")
    ev = {"v1test_old": old_te, "newtest": is_new & (split == "test"), "v1files_new": is_new & (split == "v1test")}
    rng = np.random.default_rng(194)
    res = {"brief": "T194 drive anomaly retrain measurement; nothing adopted", "max_gain_db": a.max_gain,
           "results": {}}
    Cc = None
    for arm in a.arms.split(","):
        pl_tr = {"main": None, "p2p1": "P2", "p1p2": "P1"}[arm]
        pl_te = {"main": None, "p2p1": "P1", "p1p2": "P2"}[arm]
        lev_ok = new_tr & (gain <= a.max_gain)
        if pl_tr:
            lev_ok &= player == pl_tr
        for var in a.variants.split(","):
            if var == "v1":
                trm, cam = old_tr.copy(), old_ca.copy()
            elif var in ("v2", "v2cond"):
                trm, cam = old_tr | new_tr, old_ca | new_ca
            elif var == "v2lev":
                trm, cam = old_tr | lev_ok, old_ca | (new_ca & (gain <= a.max_gain))
            elif var == "v2rand":
                pool = np.where(new_tr & ((player == pl_tr) if pl_tr else True))[0]
                pick = np.zeros(len(L), bool)
                pick[rng.choice(pool, int(lev_ok.sum()), replace=False)] = True
                cpool = np.where(new_ca)[0]
                cpick = np.zeros(len(L), bool)
                cpick[rng.choice(cpool, int((new_ca & (gain <= a.max_gain)).sum()), replace=False)] = True
                trm, cam = old_tr | pick, old_ca | cpick
            if pl_tr:
                trm, cam = trm & (player == pl_tr), cam & (player == pl_tr)
            X = C
            if var == "v2cond":
                if Cc is None:
                    Cc = np.concatenate([C, np.repeat((gain / 10.0)[:, None, None], C.shape[1], 1)], 2).astype(np.float32)
                X = Cc
            tr, ca = np.where(trm)[0], np.where(cam)[0]
            guard.wait(log=lambda d: print(f"busy {d}: waiting", flush=True))
            t0 = time.time()
            h = ah.MLPHeads(mod_fam=mod_fam, seed=0, epochs=a.epochs).fit(X[tr], y_m[tr], K[tr])
            cal = ah.calibrate(h.predict(X[ca]), y_m[ca], K[ca], mod_fam, files[ca], player[ca])
            key = f"{arm}/{var}"
            r = {"train_clips": int(len(tr)), "new_train_clips": int((trm & is_new).sum()), "cal_clips": int(len(ca)),
                 "train_gain_median_db": float(np.median(gain[tr])), "drive_scale": float(cal.scale[0])}
            for en, em in ev.items():
                m = em & (player == pl_te) if pl_te else em
                te = np.where(m)[0]
                o = h.predict(X[te])
                s = ah.score(o, y_m[te], K[te], mod_fam, cal)
                s.update(bias_table(o, te, y_m, K))
                if en != "v1test_old":
                    mu = o["mu"][np.arange(len(te)), y_m[te], 0]
                    rr = mu - K[te, 0]
                    s["bias_by_gain"] = {f"{lo}..{hi}": float(np.nanmean(rr[(gain[te] >= lo) & (gain[te] < hi)]))
                                         for lo, hi in ((-20, 1), (1, 4), (4, 7), (7, 10), (10, 13), (13, 30))}
                else:
                    src = np.array([L[j]["source"] for j in te])
                    mu = o["mu"][np.arange(len(te)), y_m[te], 0]
                    rr = mu - K[te, 0]
                    s["bias_by_source"] = {sv: float(np.nanmean(rr[src == sv])) for sv in sorted(set(src))}
                r[en] = s
            r["fit_s"] = time.time() - t0
            res["results"][key] = r
            print(key, json.dumps({en: {k: round(r[en][k], 3) for k in ("model_top1", "drive_within_true",
                                                                         "drive_bias", "drive_cover68_global_true",
                                                                         "drive_halfwidth68_global_true")}
                                   for en in ev}), f"{r['fit_s']:.0f}s", flush=True)
            (out / "result.json").write_text(json.dumps(res, indent=1, default=float))
    print("done")


if __name__ == "__main__":
    main()
