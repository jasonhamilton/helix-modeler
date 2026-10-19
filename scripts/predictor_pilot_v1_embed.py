#!/usr/bin/env python3
"""AFx-Rep embeddings for predictor-pilot-v1 (T189, drive only). Measurement only.

Uses the T186 index (cache/models/predictor-pilot-v0/index.json: same renders, same split by whole DI file).
Writes cache/models/predictor-pilot-v1/:
  * emb_single.npy (N, 512): the 6 s centre crop, copied from T186's afx/emb.npy where present (train and test),
    computed here for the calibration files T186 did not embed;
  * emb_crops.npy (N, 3, 512): three non-overlapping 3.33 s crops covering the 10 s clip (multi-crop arm);
  * embed_progress.json (resumable: rerun continues where it stopped).
CPU, 2 threads (AFx-Rep on MPS was slower than CPU on this Mac, see metrics/afxrep.py). Digitally silent
renders (rms <= -90 dBFS) are skipped and stay zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
V0 = ROOT / "cache/models/predictor-pilot-v0"
OUT = ROOT / "cache/models/predictor-pilot-v1"
SR = 48_000
NCROP = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="embed only this many clips (speed probe)")
    ap.add_argument("--batch", type=int, default=16)
    a = ap.parse_args()
    import torch
    torch.set_num_threads(2)
    from tonematch.learning import drive_pilot as dp
    from tonematch.metrics import afxrep
    idx = json.loads((V0 / "index.json").read_text())
    recs, rms = idx["records"], np.array(idx["rms_db"])
    N = len(recs)
    use = np.where(rms > -90)[0]
    OUT.mkdir(parents=True, exist_ok=True)
    fs, fc, fp = OUT / "emb_single.npy", OUT / "emb_crops.npy", OUT / "embed_progress.json"
    if fs.is_file() and fc.is_file() and fp.is_file():
        S, C = np.load(fs), np.load(fc)
        done = set(json.loads(fp.read_text())["done"])
    else:
        S = np.load(V0 / "afx/emb.npy").astype(np.float32)
        C = np.zeros((N, NCROP, 512), np.float32)
        done = set()
    todo = [int(j) for j in use if int(j) not in done]
    if a.limit:
        todo = todo[:a.limit]
    emb = afxrep.Embedder(threads=2)
    t0, n_done, last_save = time.time(), 0, time.time()
    L = 10 * SR // NCROP
    for i in range(0, len(todo), a.batch):
        b = todo[i:i + a.batch]
        crops, need_single = [], [j for j in b if not np.any(S[j])]
        for j in b:
            x = dp.load_audio(recs[j]["path"])
            crops += [x[k * L:(k + 1) * L] for k in range(NCROP)]
        out = emb.embed_batch(crops)
        for q, j in enumerate(b):
            for k in range(NCROP):
                C[j, k] = out[q * NCROP + k].mid
        if need_single:
            singles = []
            for j in need_single:
                x = np.load(recs[j]["path"]).astype(np.float32).reshape(-1)
                s = max(0, (len(x) - 6 * SR) // 2)
                singles.append(x[s:s + 6 * SR])
            for j, f in zip(need_single, emb.embed_batch(singles)):
                S[j] = f.mid
        done.update(b)
        n_done += len(b)
        if time.time() - last_save > 300 or i + a.batch >= len(todo):
            np.save(fs, S)
            np.save(fc, C)
            fp.write_text(json.dumps({"done": sorted(done), "n_use": int(len(use))}))
            last_save = time.time()
            el = time.time() - t0
            print(f"{len(done)}/{len(use)} done, {n_done / el:.2f} clips/s, {el:.0f}s on {emb.device}", flush=True)
    print(json.dumps({"elapsed_s": time.time() - t0, "clips": n_done, "device": emb.device}))


if __name__ == "__main__":
    main()
