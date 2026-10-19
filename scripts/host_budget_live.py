#!/usr/bin/env python3
"""Live check of the host scaler (T133): a NativeRenderPool asks for 6 hosts, a CPU burner (plain Python
loops in 4 processes) adds ~4 load, then stops. Records load1 and host counts every 2 s and the pool's
size after each generation; the pool should shrink between generations while the burner runs and
regrow after. Aborts (kills the burner, closes the pool) if the busy cores reach ``--abort-load``.

  .venv/bin/python scripts/host_budget_live.py --out cache/host_budget/live.json   (run in background)
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TONEMATCH_AGENT", "platform")
os.environ.setdefault("TONEMATCH_RUN_TAG", "host-budget-live")


def burn(stop) -> None:
    n = 0
    while not stop.is_set():
        for _ in range(100000):
            n += 1


def hosts_running() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True, check=False)
    return len([x for x in r.stdout.split() if x.strip() and int(x) != os.getpid()])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "cache/host_budget/live.json")
    ap.add_argument("--want", type=int, default=6)
    ap.add_argument("--burners", type=int, default=4)
    ap.add_argument("--quiet-s", type=float, default=60)
    ap.add_argument("--burn-s", type=float, default=150)
    ap.add_argument("--after-s", type=float, default=180)
    ap.add_argument("--abort-load", type=float, default=None,
                    help="abort when the busy cores reach this (default 0.99 x cores; was load1 9.9; T193)")
    ap.add_argument("--start-below", type=float, default=None,
                    help="start only when the busy cores are below this (default 0.85 x cores; was load1 7.0; T193)")
    ap.add_argument("--start-hosts-max", type=int, default=0, help="wait until at most this many other hosts run")
    ap.add_argument("--max-wait-s", type=float, default=1500, help="then start anyway if the busy cores are under the line")
    a = ap.parse_args()
    import numpy as np
    import soundfile as sf

    from tonematch.engines.base import SAMPLE_RATE
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.engines.native import ChainTemplate, NativeRenderPool, chain_key
    from tonematch.preset import read_hlx

    sys.path.insert(0, str(ROOT / "scripts"))
    from native_pool_bench import random_sets

    guard = LoadGuard()
    start_below = a.start_below if a.start_below else 0.85 * guard.cores
    abort_load = a.abort_load if a.abort_load else 0.99 * guard.cores
    t_wait = time.time()
    while (guard.demand() >= start_below or hosts_running() > a.start_hosts_max) \
            and time.time() - t_wait < a.max_wait_s:
        print(f"waiting: busy cores {guard.last:.2f}, {hosts_running()} hosts on the machine", flush=True)
        time.sleep(15)
    if guard.demand() >= start_below:
        print("load too high to start", flush=True)
        return 3
    templates_dir = ROOT / "cache/native/bypass_patterns/cc_templates"
    preset = read_hlx(ROOT / "cache/blockserial/C8/8f50d3d2e224_BS_C8.hlx")
    tpl = max(ChainTemplate.load_variants(templates_dir)[chain_key(preset)], key=lambda t: len(t.map))
    dry, _ = sf.read(ROOT / "cache/parity/di_10s_tail.wav", dtype="float32")
    dry = dry[: 2 * SAMPLE_RATE]
    log: list[dict] = []
    stop_mon = threading.Event()
    state = {"phase": "open", "workers": 0}
    t_start = time.time()

    def monitor() -> None:
        while not stop_mon.wait(2.0):
            log.append({"t": round(time.time() - t_start, 1), "phase": state["phase"], "load1": os.getloadavg()[0],
                        "busy": round(guard.demand(), 2), "native_hosts_machine": hosts_running(), "pool_hosts": state["workers"]})

    threading.Thread(target=monitor, daemon=True).start()
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    burners: list = []
    gens: list[dict] = []
    pool = None
    abort = False
    try:
        pool = NativeRenderPool(workers=a.want, templates_dir=templates_dir)
        pool.use_template(tpl)
        state["workers"] = pool.workers
        opened = pool.workers
        print(f"pool opened with {opened} hosts (asked {a.want})", flush=True)
        t0 = time.time()
        phase_end = {"quiet": a.quiet_s, "burn": a.quiet_s + a.burn_s, "after": a.quiet_s + a.burn_s + a.after_s}
        while time.time() - t0 < phase_end["after"] and not abort:
            el = time.time() - t0
            phase = "quiet" if el < phase_end["quiet"] else "burn" if el < phase_end["burn"] else "after"
            if phase != state["phase"] and phase == "burn":
                burners = [ctx.Process(target=burn, args=(stop,)) for _ in range(a.burners)]
                [b.start() for b in burners]
            if phase == "after" and burners:
                stop.set()
                [b.join(10) for b in burners]
                burners = []
            state["phase"] = phase
            sets = random_sets(tpl, 4 * max(pool.workers, 1), seed=int(el))
            pool.map_generation(preset, sets, dry, lambda y: float(np.sqrt(np.mean(y * y))), on_error="none")
            state["workers"] = pool.workers
            l1 = guard.demand()
            gens.append({"t": round(time.time() - t_start, 1), "phase": phase, "hosts": pool.workers,
                         "busy": round(l1, 2), "drain": pool._lease.drain if pool._lease else None})
            print(gens[-1], flush=True)
            if l1 >= abort_load:
                print("ABORT: busy cores reached the abort line", flush=True)
                abort = True
    finally:
        stop.set()
        for b in burners:
            b.join(10)
            if b.is_alive():
                b.terminate()
        if pool is not None:
            pool.close()
        stop_mon.set()
    peak = max(x["load1"] for x in log) if log else None
    peak_busy = max(x["busy"] for x in log) if log else None
    summary = {"opened": opened if pool is not None else None, "asked": a.want, "peak_load1": peak, "peak_busy": peak_busy,
               "peak_native_hosts_machine": max((x["native_hosts_machine"] for x in log), default=None),
               "min_pool_hosts_during_burn": min((g["hosts"] for g in gens if g["phase"] == "burn"), default=None),
               "max_pool_hosts_quiet": max((g["hosts"] for g in gens if g["phase"] == "quiet"), default=None),
               "final_pool_hosts": gens[-1]["hosts"] if gens else None,
               "max_pool_hosts_after": max((g["hosts"] for g in gens if g["phase"] == "after"), default=None),
               "aborted": abort, "hosts_left": hosts_running()}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"summary": summary, "generations": gens, "samples": log}, indent=1))
    print(json.dumps(summary, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
