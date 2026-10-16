#!/usr/bin/env python3
"""Tests per second of NativeRenderPool for a list of host counts on a real chain (phase 3).

For each host count N (one pool at a time, closed before the next): open the pool, warm up
(chain load + latency per host), then for each clip length render ``--renders`` random CC-grid
settings of the chain as one generation with ``map_generation`` and, with ``--score``, the
MR-STFT loss against a fixed target render ("test" = render + score). Recorded per N and clip:
tests/s, seconds per test (wall / tests), per-item latency (median, p90), footprint of every
host (phys_footprint, not RSS), system memory pressure (``memory_pressure`` free %) and swap
used, load average, watchdog recycles and canary replacements. Before the pools, a single
engine renders the first ``--spot`` settings serially; the pools' renders of the same settings
must null against them (bit-identity spot check).

Default chain: the 8-block C8 chain (comp drive amp cab eq chorus delay reverb) and its
template with bypass CCs and verified param CCs. DI: Guitar-TECHS excerpt, never song audio.
Hosts above the RAM cap need ``TONEMATCH_NATIVE_MAX_HOSTS``; the sweep stops early when free
memory drops under ``--min-free-pct`` or swap grows by more than ``--max-swap-growth-mb``.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_pool_bench.py --hosts 1,2,3,4,5 --clips 2,10 --renders 60 --score
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import (
    ChainTemplate,
    NativeEngine,
    NativeRenderPool,
    chain_key,
    null_db,
)
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "pool_bench"


def mem_pressure() -> dict:
    """System-wide free memory % (macOS ``memory_pressure``) and swap used (MB)."""
    out = {}
    try:
        r = subprocess.run(["memory_pressure", "-Q"], check=False, capture_output=True, text=True, timeout=20)
        m = re.search(r"free percentage:\s*(\d+)%", r.stdout)
        out["free_pct"] = int(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired):
        out["free_pct"] = None
    try:
        r = subprocess.run(["sysctl", "-n", "vm.swapusage"], check=False, capture_output=True, text=True, timeout=10)
        m = re.search(r"used = ([\d.]+)M", r.stdout)
        out["swap_used_mb"] = float(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired):
        out["swap_used_mb"] = None
    out.update(pressure_counters())
    return out


def pressure_counters() -> dict:
    """macOS memory pressure level (1 normal/green, 2 warning/yellow, 4 critical/red) and the
    cumulative swap-out page count."""
    out: dict = {"level": None, "swapouts": None}
    try:
        r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], check=False,
                           capture_output=True, text=True, timeout=10)
        out["level"] = int(r.stdout.strip())
        r = subprocess.run(["vm_stat"], check=False, capture_output=True, text=True, timeout=10)
        m = re.search(r"Swapouts:\s+(\d+)", r.stdout)
        out["swapouts"] = int(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return out


class Sampler:
    """Worst pressure level and swap-outs while a generation runs (sampled every second)."""

    def __init__(self):
        import threading

        self.stop, self.levels = threading.Event(), []
        self.start = pressure_counters()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self.stop.wait(1.0):
            self.levels.append(pressure_counters()["level"] or 0)

    def done(self) -> dict:
        self.stop.set()
        self.t.join(timeout=5)
        end = pressure_counters()
        so = (end["swapouts"] - self.start["swapouts"]) if end["swapouts"] is not None and \
            self.start["swapouts"] is not None else None
        return {"pressure_level_max": max(self.levels + [end["level"] or 0]), "swapout_pages": so}


def random_sets(tpl: ChainTemplate, n: int, seed: int) -> list[dict]:
    """Random settings of the template's verified [0, 1] knobs on the CC grid."""
    refs = [r for r in tpl.map if r in tpl.verified and not r.endswith("@enabled")
            and list(tpl.ranges.get(r, [0.0, 1.0])) == [0.0, 1.0]] or [r for r in tpl.map if not r.endswith("@enabled")]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        out.append({(int(r.split(":")[0]), r.split(":", 1)[1]): round(rng.uniform(0.2, 0.8) * 127) / 127
                    for r in refs})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hlx", type=Path, default=ROOT / "cache/blockserial/C8/8f50d3d2e224_BS_C8.hlx")
    ap.add_argument("--templates-dir", type=Path, default=ROOT / "cache/native/bypass_patterns/cc_templates")
    ap.add_argument("--di", type=Path, default=ROOT / "cache/parity/di_10s_tail.wav")
    ap.add_argument("--hosts", default="1,2,3,4,5")
    ap.add_argument("--clips", default="2,10", help="clip lengths in seconds")
    ap.add_argument("--renders", type=int, default=60, help="tests per (N, clip) after warm-up")
    ap.add_argument("--score", action="store_true", help="test = render + MR-STFT loss")
    ap.add_argument("--spot", type=int, default=4, help="settings checked against serial renders")
    ap.add_argument("--min-free-pct", type=int, default=20)
    ap.add_argument("--max-swap-growth-mb", type=float, default=1024.0)
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    import soundfile as sf

    preset = read_hlx(a.hlx)
    tpl = max(ChainTemplate.load_variants(a.templates_dir)[chain_key(preset)], key=lambda t: len(t.map))
    x, _ = sf.read(a.di, dtype="float32")
    clips = {float(c): x[: int(float(c) * SAMPLE_RATE)] for c in a.clips.split(",")}
    pre = 3 * SAMPLE_RATE
    if a.score:
        from tonematch.metrics.mrstft import term as mrstft

    # serial references (one engine), and the fixed scoring targets
    t0 = time.perf_counter()
    eng = NativeEngine(preroll=pre, templates_dir=a.templates_dir)
    try:
        eng.templates[tpl.chain] = tpl
        eng._variants[tpl.chain] = [tpl]
        spot_sets = random_sets(tpl, a.spot, seed=99)
        serial: dict[float, list[np.ndarray]] = {}
        targets: dict[float, np.ndarray] = {}
        for c, dry in clips.items():
            serial[c] = []
            for ps in spot_sets:
                eng.load(preset)
                eng.set_params(ps)
                serial[c].append(eng.render(dry))
            eng.load(preset)
            targets[c] = eng.render(dry)
        serial_s = time.perf_counter() - t0
    finally:
        eng.close()
    print(f"serial references done in {serial_s:.1f} s", flush=True)

    rows: list[dict] = []
    mem0 = mem_pressure()
    for n in [int(v) for v in a.hosts.split(",")]:
        before = mem_pressure()
        t0 = time.perf_counter()
        pool = NativeRenderPool(workers=n, preroll=pre, templates_dir=a.templates_dir)
        open_s = time.perf_counter() - t0
        try:
            if pool.workers < n:
                print(f"N={n}: only {pool.workers} hosts allowed (RAM cap; set TONEMATCH_NATIVE_MAX_HOSTS)")
            pool.use_template(tpl)
            for c, dry in clips.items():
                pool.render_generation(preset, [{}] * pool.workers, dry)  # warm-up
                # spot check: the same settings through the pool
                got = pool.render_generation(preset, spot_sets, dry)
                spot = max(null_db(s, g) for s, g in zip(serial[c], got, strict=True))
                sets = random_sets(tpl, a.renders, seed=1000 + n)
                tgt = targets[c]
                fn = (lambda y, t=tgt: mrstft(t, y)) if a.score else (lambda y: float(np.sqrt(np.mean(y * y))))
                la0 = os.getloadavg()[0]
                smp = Sampler()
                t1 = time.perf_counter()
                res = pool.map_generation(preset, sets, dry, fn, on_error="none")
                wall = time.perf_counter() - t1
                press = smp.done()
                la1 = os.getloadavg()[0]
                lat = sorted(pool.last_item_s)
                mp = mem_pressure()
                st = pool.stats()
                row = {"hosts": pool.workers, "clip_s": c, "tests": len(sets),
                       "ok": sum(r is not None for r in res), "scored": bool(a.score),
                       "wall_s": round(wall, 2), "tests_per_s": round(len(sets) / wall, 3),
                       "s_per_test": round(wall / len(sets), 3),
                       "latency_median_s": round(statistics.median(lat), 3),
                       "latency_p90_s": round(lat[int(0.9 * (len(lat) - 1))], 3),
                       "open_s": round(open_s, 1), "mem_total_mb": st["mem_mb"],
                       "mem_per_host_mb": st["mem_mb_per_host"], "free_pct": mp["free_pct"],
                       "swap_used_mb": mp["swap_used_mb"], "load_before": round(la0, 2),
                       "load_after": round(la1, 2), "recycles": st["recycles"],
                       "canary_respawns": st["canary_respawns"], "spot_null_db": round(spot, 1),
                       "pressure_level_max": press["pressure_level_max"],
                       "swapout_pages": press["swapout_pages"],
                       "errors": len(pool.last_errors)}
                rows.append(row)
                print(json.dumps(row), flush=True)
        finally:
            pool.close()
        after = mem_pressure()
        grew = (after["swap_used_mb"] or 0) - (mem0["swap_used_mb"] or 0)
        low = rows and rows[-1]["free_pct"] is not None and rows[-1]["free_pct"] < a.min_free_pct
        red = any((r["pressure_level_max"] or 0) >= 4 for r in rows if r["hosts"] == n)
        swapping = sum(r["swapout_pages"] or 0 for r in rows if r["hosts"] == n) * 16384 / 2**20
        if low or red or grew > a.max_swap_growth_mb or swapping > a.max_swap_growth_mb:
            print(f"stopping at N={n}: free {rows[-1]['free_pct']}% (min {a.min_free_pct}), pressure red {red}, "
                  f"swap used grew {grew:.0f} MB since start, swapped out {swapping:.0f} MB during N={n} "
                  f"(before this N: {before})", flush=True)
            break
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    f = OUT / f"{stamp}{'_' + a.tag if a.tag else ''}.json"
    f.write_text(json.dumps({"rows": rows, "chain": tpl.name, "serial_s": serial_s, "cpus": os.cpu_count(),
                             "mem_start": mem0}, indent=1))
    print("\n| hosts | clip s | tests/s | s/test | latency med/p90 s | total MB | MB/host | free % | swap MB "
          "| pressure | swapped out MB | load | recycles | spot null dB |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        per = round(r["mem_total_mb"] / max(1, r["hosts"]))
        print(f"| {r['hosts']} | {r['clip_s']:g} | {r['tests_per_s']} | {r['s_per_test']} | "
              f"{r['latency_median_s']}/{r['latency_p90_s']} | {r['mem_total_mb']} | {per} | {r['free_pct']} | "
              f"{r['swap_used_mb']} | {r['pressure_level_max']} | "
              f"{round((r['swapout_pages'] or 0) * 16384 / 2**20)} | {r['load_after']} | {r['recycles']} | "
              f"{r['spot_null_db']} |")
    print("wrote", f)
    if a.ledger:
        for r in rows:
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "render-cost",
                            "--engine", "native", "--dataset", "smoke", "--metric", "renders_per_s",
                            f"--value={r['tests_per_s']}", "--renders", str(r["tests"]),
                            f"--wall-clock-s={r['wall_s']}", "--notes",
                            (f"scripts/native_pool_bench.py N={r['hosts']} hosts; {r['clip_s']:g} s DI clip + 3 s "
                             f"preroll on {tpl.name!r} (8 blocks); {'render + MR-STFT score' if r['scored'] else 'render'}"
                             f" per test; {r['s_per_test']} s/test; latency median {r['latency_median_s']} p90 "
                             f"{r['latency_p90_s']} s; footprint {r['mem_total_mb']} MB total; free memory "
                             f"{r['free_pct']}%; pressure level {r['pressure_level_max']} (1 green 2 yellow 4 red); swapped out "
                             f"{round((r['swapout_pages'] or 0) * 16384 / 2**20)} MB; swap {r['swap_used_mb']} MB; load {r['load_after']}; recycles "
                             f"{r['recycles']}; canary replacements {r['canary_respawns']}; spot null vs serial "
                             f"{r['spot_null_db']} dB; {os.cpu_count()} CPUs")],
                           check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
