#!/usr/bin/env python3
"""Measured-seed calibration (T113): per time-based model and knob, render the knob across its range on a
calibration DI and record the measured quantity (echo time, feedback, decay, pre-delay, LFO rate, depth).
Library preparation, done once per library version; the search then inverts the curves at zero render cost.

  python scripts/seed_calibrate.py [--models M ...] [--out cache/measured-seed/calibration.json]

The calibration DI is a Guitar-TECHS file that no synthetic-v1 item uses (P3_music directinput_04), so the
calibration never sees a benchmark clip. One Native host. Local output (cache/, gitignored).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

CAL_DI = {"path": "Guitar-TECHS/P3_music/audio/directinput/directinput_04.wav", "start_s": 12.0}
DEFAULT_OUT = ROOT / "cache" / "measured-seed" / "calibration.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", nargs="*", default=None, help="default: the delay, reverb and mod models of the synthetic-v1 pool")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--set", default="synthetic-v1", help="take the pool from cache/<SET>/manifest.json")
    ap.add_argument("--merge", action="store_true",
                    help="keep the entries already in --out and add the models not in it (or all of --models)")
    a = ap.parse_args(argv)
    import helix_trust_set as HT
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.search.measured_seed import calibrate_model, save_calibration

    S.set_paths(a.set)
    man = json.loads((S.SET_DIR / "manifest.json").read_text())
    cat = load_catalog()
    have = {}
    if a.merge and Path(a.out).exists():
        have = json.loads(Path(a.out).read_text())
    models = {}
    for c in ("delay", "reverb", "mod"):
        for m in man["pool"].get(c, {}):
            if (a.models is None and m not in have) or (a.models is not None and m in a.models):
                models[m] = S.Block(m, c, S.free_params(m, NL.template_for(m), cat, c), cat)
    if not models:
        print("nothing to calibrate")
        return 0
    dry, _g = HT.load_excerpt({**CAL_DI}, S.CLIP_S, S.REF_LOUD_DBFS)
    engine = S.make_engine()
    out, t0, renders = {k: v for k, v in have.items() if k != "_meta"}, time.time(), 0
    try:
        for m, b in models.items():
            def render(values, b=b):
                nonlocal renders
                engine.load(b.with_values(values))
                renders += 1
                return np.asarray(engine.render(dry), np.float32)
            defaults = {n: d for n, _, _, d in b.free}
            out[m] = calibrate_model(render, dry, m, b.category, {n: (lo, hi) for n, lo, hi, _ in b.free}, defaults)
            print(m, [{"knobs": g["knobs"], "n": len(g["x"]), "finite": sum(all(math.isfinite(v) for v in r) for r in g["q"])}
                      for g in out[m]], flush=True)
    finally:
        engine.close()
    out["_meta"] = {**({} if not have else {"earlier": have.get("_meta")}), "di": CAL_DI, "renders": renders, "wall_s": round(time.time() - t0, 1),
                    "library_version": NL.VERSION, "grid": [0.1, 0.3, 0.5, 0.7, 0.9]}
    save_calibration(out, a.out)
    print(f"{renders} renders, {time.time() - t0:.0f} s -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
