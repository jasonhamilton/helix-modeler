#!/usr/bin/env python3
"""Print the Native host process's memory after each kind of operation (native-engine diagnostics).

One host process, a few dozen operations, synthetic excitation only (never song audio). Refuses to
start if more than ``--max-hosts`` native_host processes already run (another job's host is
fine; do not stack probes), and aborts if its host passes ``--abort-gb``. Steps, each repeated
``--n`` times, with ``ps`` RSS and the physical footprint (macOS ``phys_footprint``, which counts
the compressed and swapped pages RSS hides; reported by the host itself) after every call:

  open           spawn the host and load the plugin (always first)
  get_state      read the raw state
  set_state      write a captured template's state (same instance)
  render_reset   3 s pre-roll + 1 s clip, reset=True (what NativeEngine.render does at preroll 144000)
  render_noreset same clip, reset=False
  set_params     move one mapped CC knob, then a reset render
  cc_switch      move a CC knob and toggle an automation switch (switch_01) together, then render
  reload         ``reload_with_state``: a fresh plugin instance with the template state
  editor         open the editor for 3 s and close it (``--editor``; needs a GUI session)

  .venv/bin/python scripts/native_mem_probe.py [--strategy state|reinstantiate] [--n 5]
      [--steps get_state,set_state,render_reset,...] [--json out.json]

``--strategy reinstantiate`` reproduces the pre-2026-10-15 behaviour (pedalboard re-creates the
instance on every reset render, ~355 MB each).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import ChainTemplate, HostProcess, locate_plugin, rss_mb, test_signal

DEFAULT_STEPS = "get_state,set_state,render_reset,render_noreset,set_params,cc_switch,reload,render_reset"


def host_count() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True,
                       check=False)
    return len(r.stdout.split())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin", default=None)
    ap.add_argument("--strategy", choices=("state", "reinstantiate"), default=None,
                    help="host reset strategy (default: the host's, i.e. state)")
    ap.add_argument("--steps", default=DEFAULT_STEPS)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--preroll-s", type=float, default=3.0)
    ap.add_argument("--max-hosts", type=int, default=1, help="refuse to start above this many hosts")
    ap.add_argument("--abort-gb", type=float, default=12.0)
    ap.add_argument("--editor", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args()

    if host_count() > a.max_hosts:
        print(f"{host_count()} native_host processes already running (> {a.max_hosts}); not starting")
        return 2
    plugin = a.plugin or str(locate_plugin() or "")
    if not plugin:
        print("Helix Native not found")
        return 2
    if a.strategy:
        os.environ["TONEMATCH_NATIVE_HOST_RESET"] = a.strategy
    tpls = sorted(ChainTemplate.load_all().values(), key=lambda t: len(t.map), reverse=True)
    tpl = tpls[0] if tpls else None
    x = np.concatenate([np.zeros(int(a.preroll_s * SAMPLE_RATE), np.float32), test_signal(1.0)])
    payload = x.astype("<f4").tobytes()
    rows: list[dict] = []
    host: HostProcess | None = None

    def mark(step: str, i: int, dt: float) -> None:
        r = rss_mb(host.proc.pid)
        row = {"step": step, "i": i, "rss_mb": r, "footprint_mb": host.mem_mb, "s": round(dt, 3)}
        rows.append(row)
        print(f"{step:15s} #{i:<2d} rss {r or 0:7.0f} MB  footprint {host.mem_mb or 0:7.0f} MB  ({dt:.2f} s)",
              flush=True)
        if max(r or 0, host.mem_mb or 0) > a.abort_gb * 1024:
            raise SystemExit(f"host above {a.abort_gb} GB: aborting probe")

    steps = [s for s in a.steps.split(",") if s and s != "open"] + (["editor"] if a.editor else [])
    try:
        t0 = time.perf_counter()
        host = HostProcess(plugin)
        mark("open", 0, time.perf_counter() - t0)
        print(f"reset strategy: {host.info.get('reset_strategy', 'reinstantiate (old host)')}")
        knob = next(iter(tpl.map.values())) if tpl and tpl.map else None
        for step in steps:
            for i in range(a.n):
                t0 = time.perf_counter()
                if step == "get_state":
                    host.call("get_state")
                elif step == "set_state" and tpl:
                    host.call("set_state", tpl.state)
                elif step == "reload" and tpl:
                    host.call("reload_with_state", tpl.state, path=plugin)
                elif step == "render_reset":
                    host.call("render", payload, reset=True)
                elif step == "render_noreset":
                    host.call("render", payload, reset=False)
                elif step == "set_params" and knob:
                    host.call("set_params", values={knob: (i % 4) / 4 + 0.1})
                    host.call("render", payload, reset=True)
                elif step == "cc_switch" and knob:
                    sw = [d["name"] for d in host.info.get("params", []) if d["name"].startswith("switch_")]
                    vals = {knob: (i % 4) / 4 + 0.1, **({sw[0]: float(i % 2)} if sw else {})}
                    host.call("set_params", values=vals)
                    host.call("render", payload, reset=True)
                elif step == "editor":
                    host.call("editor_cycle", open_s=3.0)
                else:
                    continue
                host.call("ping")  # refresh the footprint after the step
                mark(step, i, time.perf_counter() - t0)
    finally:
        if host is not None:
            host.close()
    if a.json:
        a.json.write_text(json.dumps({"plugin": plugin, "template": tpl.name if tpl else None,
                                      "strategy": a.strategy or "state", "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
