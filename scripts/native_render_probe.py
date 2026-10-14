#!/usr/bin/env python3
"""Measure where Native render time goes on a captured chain (native-engine diagnostics).

Sections, each preceded by a load-average snapshot and a list of heavy processes:
  a  pure render, no set_params
  b  set_params(knob) then render; set_params alone; does the state change? does it trigger a reset?
  c  clip length 0.5 / 2 / 10 s (is the reset cost fixed?), reset and flush modes
  d  parallel pool of 1/2/4/6 host processes on the chain's state
  cold  cost of one cache-missing measure_cost() (what a cost() miss costs a caller)

Synthetic excitation only (test_signal), never song audio. Writes JSON to
cache/native/probe/<utc>.json (local). Prints a summary. No ledger row: use --ledger for that.

  .venv/bin/python scripts/native_render_probe.py [--preset fixtures/smoke/known.hlx] [--sections abcd]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import NativeEngine, cache_dir, measure_parallelism, test_signal
from tonematch.preset import read_hlx

HEAVY_PCT = 50.0


def load_snapshot() -> dict:
    la = os.getloadavg()
    try:
        ps = subprocess.run(["ps", "-Ao", "pid,ppid,pcpu,comm", "-r"], capture_output=True, text=True,
                            check=False).stdout
    except OSError:
        ps = ""
    rows = []
    for line in ps.splitlines()[1:]:
        parts = line.strip().split(None, 3)
        if len(parts) == 4:
            try:
                rows.append((int(parts[0]), int(parts[1]), float(parts[2]), parts[3]))
            except ValueError:
                continue
    parent = {pid: ppid for pid, ppid, _, _ in rows}

    def ours(pid: int) -> bool:  # this probe or anything it spawned (host processes)
        seen = 0
        while pid > 1 and seen < 50:
            if pid == os.getpid():
                return True
            pid, seen = parent.get(pid, 0), seen + 1
        return False

    heavy = [f"{Path(c).name}:{p:.0f}%" for pid, _, p, c in rows if p >= HEAVY_PCT and not ours(pid)]
    return {"t": time.strftime("%H:%M:%S"), "load1": round(la[0], 2), "load5": round(la[1], 2),
            "load15": round(la[2], 2), "heavy": heavy}


def timed_render(eng: NativeEngine, x: np.ndarray) -> tuple[float, float]:
    """(wall seconds for engine.render, host-side process() seconds for that call)."""
    lat, reset = eng.latency(), eng.reset_mode == "reset"
    pre = eng.preroll if reset else int(eng.flush_s * SAMPLE_RATE)
    padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(lat, np.float32)])
    t0 = time.perf_counter()
    hdr, _ = eng.host.call("render", padded.astype("<f4").tobytes(), reset=reset)
    return time.perf_counter() - t0, float(hdr.get("render_s", float("nan")))


def med(v):
    return round(statistics.median(v), 4) if v else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default=str(ROOT / "fixtures" / "smoke" / "known.hlx"))
    ap.add_argument("--sections", default="abcd")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=3, help="renders per process in section d")
    ap.add_argument("--workers", default="1,2,4,6")
    ap.add_argument("--cold", action="store_true", help="also time one cache-missing measure_cost()")
    args = ap.parse_args()

    out: dict = {"preset": Path(args.preset).name, "cpu_count": os.cpu_count(), "sections": {}}
    clip2 = test_signal(2.0)
    preset = read_hlx(args.preset)
    with NativeEngine() as eng:
        eng.load(preset)
        eng.latency()
        tpl = eng.current_template
        knob = next(iter(tpl.map.values()), None) if tpl and tpl.map else None
        ref = next(iter(tpl.map), None) if tpl and tpl.map else None
        timed_render(eng, clip2)  # warm-up
        S = out["sections"]

        if "a" in args.sections:
            snap = load_snapshot()
            w, h = zip(*[timed_render(eng, clip2) for _ in range(args.repeats)], strict=True)
            S["a_pure_2s"] = {"load": snap, "wall": list(w), "host": list(h), "wall_med": med(w), "host_med": med(h)}

        if "b" in args.sections and ref:
            snap = load_snapshot()
            slot, name = ref.split(":", 1)
            st0 = hashlib.sha1(eng._get_state()).hexdigest()[:12]
            sp, w, h = [], [], []
            for i in range(args.repeats):
                v = 0.3 + 0.1 * i
                t0 = time.perf_counter()
                eng.set_params({(int(slot), name): v})
                sp.append(time.perf_counter() - t0)
                ww, hh = timed_render(eng, clip2)
                w.append(ww)
                h.append(hh)
            st1 = hashlib.sha1(eng._get_state()).hexdigest()[:12]
            # does a knob move persist across a reset render (no hidden state reload)?
            eng.set_params({(int(slot), name): 0.9})
            ya = eng.render(clip2)
            yb = eng.render(clip2)
            eng.set_params({(int(slot), name): 0.3})
            yc = eng.render(clip2)
            S["b_setparams_then_render_2s"] = {
                "load": snap, "knob": knob, "set_params_s": sp, "set_params_med": med(sp),
                "wall": w, "host": h, "wall_med": med(w), "host_med": med(h),
                "state_hash_before": st0, "state_hash_after": st1, "state_changed_by_knob": st0 != st1,
                "repeat_null_db": float(10 * np.log10(max(np.sum((ya - yb) ** 2), 1e-30) / np.sum(ya ** 2))),
                "knob_moves_render_db": float(10 * np.log10(max(np.sum((ya - yc) ** 2), 1e-30) / np.sum(ya ** 2))),
            }

        if "c" in args.sections:
            snap = load_snapshot()
            res = {"load": snap}
            for mode in ("reset", "flush"):
                eng.reset_mode = mode
                for s in (0.5, 2.0, 10.0):
                    x = test_signal(s)
                    timed_render(eng, x)
                    w, h = zip(*[timed_render(eng, x) for _ in range(3)], strict=True)
                    res[f"{mode}_{s:g}s"] = {"wall_med": med(w), "host_med": med(h), "wall": list(w)}
            eng.reset_mode = "reset"
            S["c_clip_length"] = res

        if "d" in args.sections:
            snap = load_snapshot()
            ks = [int(k) for k in args.workers.split(",")]
            state = eng._get_state()
            t0 = time.perf_counter()
            _, scan = measure_parallelism(eng.plugin, eng.plugin_name, state, clip2, max(ks), eng.preroll,
                                          rounds=args.rounds)
            scan = {k: v for k, v in scan.items() if k in ks}
            S["d_pool"] = {"load": snap, "renders_per_s": {str(k): round(v, 3) for k, v in scan.items()},
                           "s_per_render_per_process": {str(k): round(k / v, 3) for k, v in scan.items()},
                           "scan_wall_s": round(time.perf_counter() - t0, 1), "load_after": load_snapshot()}

        if args.cold:
            snap = load_snapshot()
            f = cache_dir() / "cost.json"
            saved = f.read_bytes() if f.is_file() else None
            try:
                if f.is_file():
                    f.unlink()
                t0 = time.perf_counter()
                c = eng.measure_cost()
                S["cold_measure_cost"] = {"load": snap, "wall_s": round(time.perf_counter() - t0, 1),
                                          "cost": c.__dict__}
            finally:
                if saved is not None:
                    f.write_bytes(saved)

    d = cache_dir() / "probe"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
    p.write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
