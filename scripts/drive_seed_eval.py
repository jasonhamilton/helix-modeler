#!/usr/bin/env python3
"""Offline check of the drive calibration table on a dev set's saved targets (T143; no renders).

For each drive item of the chosen half of synthetic-devgm the saved target (the true preset on the item's DI C)
and DI C give a feature vector; the table (calibrated on another recording) is asked for knob seeds and a model
ranking. Reports: rank of the true model among all calibrated drives and among the set's pool of drive models;
mean knob error of the true model's seed (fraction of range, level knobs apart) against 0.25 for a seed at the
middle of the range with a uniform truth; share of knobs whose truth is inside the seed band.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.search import drive_features as DF
from tonematch.search import drive_specialist as DS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="synthetic-devgm")
    ap.add_argument("--run", default="gm_base_tune")
    ap.add_argument("--half", choices=("tune", "held"), default="tune")
    ap.add_argument("--category", default="drive")
    ap.add_argument("--table", default=None)
    a = ap.parse_args(argv)
    tab = DS.DriveTable.load(a.table)
    labels = json.loads((ROOT / "cache" / a.set / "labels.json").read_text())
    man = json.loads((ROOT / "cache" / a.set / "manifest.json").read_text())
    pool = [m for m in man["pool"]["drive"]]
    rows = []
    for it in labels:
        n = int(it["id"][1:])
        if it["category"] != a.category or (n < 36) != (a.half == "tune"):
            continue
        t, _ = sf.read(ROOT / "cache" / a.set / "runs" / a.run / f"{it['id']}_target.wav")
        d, _ = sf.read(ROOT / it["di"]["C"]["file"])
        v = DF.features(t, d)
        res = tab.lookup(v)
        rk_all = sorted(res, key=lambda m: res[m]["fit"])
        rk_pool = [m for m in rk_all if m in pool]
        row = {"id": it["id"], "model": it["model"]}
        if it["model"] in res:
            row["rank_all"] = rk_all.index(it["model"]) + 1
            row["rank_pool"] = rk_pool.index(it["model"]) + 1 if it["model"] in rk_pool else None
            ks = DS.knob_seeds(res[it["model"]], {n_: tuple(r) for n_, r in it["ranges"].items()})
            errs, inband, lvl = [], [], []
            for nme, (val, hw) in ks.items():
                lo, hi = it["ranges"][nme]
                u_t = (it["values"][nme] - lo) / (hi - lo)
                u_s = (val - lo) / (hi - lo)
                (lvl if nme.lower() in DS.LEVEL_KNOBS else errs).append(abs(u_t - u_s))
                inband.append(abs(u_t - u_s) <= hw)
            row["knob_err"] = float(np.mean(errs)) if errs else None
            row["level_err"] = float(np.mean(lvl)) if lvl else None
            row["seeded_knobs"] = len(ks)
            row["in_band"] = float(np.mean(inband)) if inband else None
        rows.append(row)
    for r in rows:
        print(r)
    ok = [r for r in rows if "rank_all" in r]
    print(f"items {len(rows)}; in table {len(ok)}; true rank top-1 all {sum(r['rank_all'] == 1 for r in ok)}, "
          f"top-3 all {sum(r['rank_all'] <= 3 for r in ok)}, top-1 pool {sum(r['rank_pool'] == 1 for r in ok if r['rank_pool'])}, "
          f"top-2 pool {sum(r['rank_pool'] <= 2 for r in ok if r['rank_pool'])}")
    e = [r["knob_err"] for r in ok if r.get("knob_err") is not None]
    b = [r["in_band"] for r in ok if r.get("in_band") is not None]
    print(f"mean knob error {np.mean(e):.3f} (0.25 = no information); inside band {np.mean(b):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
