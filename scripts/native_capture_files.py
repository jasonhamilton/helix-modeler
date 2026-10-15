#!/usr/bin/env python3
"""Capture Native templates for specific .hlx files via load() autocapture (T018n), one
templates directory per file, so a parity run renders exactly the file the Stomp received
(bypass states and unmapped parameters included, no CC approximation).

For each file: NativeEngine(autocapture=True, templates_dir=cache/native/templates_files/<stem>)
loads it (capturing by in-process drop), renders the synthetic test signal twice
(determinism), then a second, fresh engine on the same directory loads it from the saved
template and renders again (fresh-process replay). Writes cache/native/templates_files/report.json.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_capture_files.py cache/stomp/inject/{K,A,B0,B}.hlx

Benchmark use: NativeEngine(templates_dir=cache/native/templates_files/<stem>).load(read_hlx(f)).
Synthetic excitation only; outputs are local (gitignored).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

import numpy as np

from tonematch.engines.native import NativeEngine, _hc_mode, cache_dir, null_db, test_signal
from tonematch.preset import read_hlx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hlx", nargs="+")
    ap.add_argument("--recapture", action="store_true", help="drop the file again even if captured")
    ap.add_argument("--preroll-s", type=float, default=None,
                    help="silence before each render; >= 3 s for chains with an active delay, whose "
                         "lines survive Native's reset (T018n)")
    a = ap.parse_args()
    base = cache_dir() / "templates_files"
    x = test_signal(2.0)
    rows, renders = [], {}
    for f in a.hlx:
        stem = Path(f).stem
        d = base / stem
        if a.recapture and d.exists():
            for p in d.glob("*"):
                p.unlink()
        pre = read_hlx(f)
        t0 = time.perf_counter()
        pr = None if a.preroll_s is None else int(a.preroll_s * 48_000)
        e = NativeEngine(templates_dir=d, autocapture=True, preroll=pr)
        try:
            e.load(pre)
            load_s = time.perf_counter() - t0
            y1, y2 = e.render(x), e.render(x)
            row = {"file": f, "name": pre.name, "templates_dir": str(d), "preroll_s": a.preroll_s,
                   "captured": e.last_capture_s is not None,
                   "capture_s": round(e.last_capture_s, 1) if e.last_capture_s else None,
                   "load_s": round(load_s, 1), "unmapped": e.last_unmapped,
                   "hc_mode": hex(_hc_mode(e._get_state()) or 0),
                   "repeat_null_db": round(null_db(y2, y1), 1),
                   "peak_dbfs": round(float(20 * np.log10(np.abs(y1).max() + 1e-12)), 2)}
        finally:
            e.close()
        e2 = NativeEngine(templates_dir=d, autocapture=False, preroll=pr)
        try:
            e2.load(pre)
            y3 = e2.render(x)
        finally:
            e2.close()
        row["fresh_replay_null_db"] = round(null_db(y3, y1), 1)
        renders[stem] = y1
        np.save(d / "render_test_signal.npy", y1)
        rows.append(row)
        print(json.dumps(row))
    names = list(renders)
    pairs = {f"{p}~{q}": round(null_db(renders[p], renders[q]), 1)
             for i, p in enumerate(names) for q in names[i + 1:]}
    print(json.dumps({"pairs_null_db": pairs}))
    (base / "report.json").write_text(json.dumps({"rows": rows, "pairs_null_db": pairs}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
