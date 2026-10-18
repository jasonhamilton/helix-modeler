#!/usr/bin/env python3
"""Find orphaned and zombie processes left by this repo's jobs (human 2026-10-17).

An orphan is a worker whose parent is gone (PPID 1) and whose working directory is this repo:
a multiprocessing pool worker or resource tracker, or a Native host process. Its work goes nowhere,
but it still costs load. A zombie is a process in state Z (exited, never reaped by its parent).

Long jobs launched with nohup also have PPID 1, so only worker kinds are flagged, never a top-level
script. `--kill` sends SIGTERM to the flagged orphans, then SIGKILL to any still alive after 5 s.
Zombies cannot be killed; the report names the parent that must reap them.

    python scripts/proc_check.py            # report; exit 1 if anything is flagged
    python scripts/proc_check.py --kill     # also stop the orphans
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_MARKS = ("multiprocessing.spawn", "multiprocessing.resource_tracker",
                "multiprocessing.forkserver", "tonematch.engines.native_host")


def ps_rows() -> list[dict]:
    out = subprocess.check_output(["ps", "-axo", "pid=,ppid=,stat=,pcpu=,etime=,command="], text=True)
    return parse_ps(out)


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, stat, pcpu, etime, cmd = parts
        rows.append({"pid": int(pid), "ppid": int(ppid), "stat": stat, "pcpu": float(pcpu),
                     "etime": etime, "cmd": cmd})
    return rows


def cwd_of(pid: int) -> str | None:
    try:
        out = subprocess.check_output(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True,
                                      stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), None)


def classify(rows: list[dict], cwd=cwd_of, root: Path = ROOT) -> dict:
    """Return {"orphans": [...], "zombies": [...]} for this repo."""
    root_s = str(root)
    orphans, zombies = [], []
    for r in rows:
        if r["stat"].startswith("Z"):
            zombies.append(r)
            continue
        if r["ppid"] != 1 or not any(m in r["cmd"] for m in WORKER_MARKS):
            continue
        d = cwd(r["pid"])
        if d and (d == root_s or d.startswith(root_s + os.sep)):
            orphans.append(r)
    return {"orphans": orphans, "zombies": zombies}


def kill(pids: list[int], grace_s: float = 5.0) -> list[int]:
    """SIGTERM, wait, SIGKILL survivors. Returns the pids still alive (should be empty)."""
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace_s
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.25)
        alive = [p for p in alive if _alive(p)]
    for p in alive:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.25)
    return [p for p in alive if _alive(p)]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kill", action="store_true", help="stop the flagged orphans (never zombies' parents)")
    a = ap.parse_args(argv)
    found = classify(ps_rows())
    o, z = found["orphans"], found["zombies"]
    load = sum(r["pcpu"] for r in o) / 100
    print(f"orphans: {len(o)} (about {load:.1f} load points); zombies: {len(z)}")
    for r in o:
        print(f"  orphan pid {r['pid']}  cpu {r['pcpu']:.0f}%  up {r['etime']}  {r['cmd'].split(' ', 1)[-1][:110]}")
    for r in z:
        print(f"  zombie pid {r['pid']}  parent {r['ppid']} must reap it  {r['cmd'].split(' ', 1)[-1][:90]}")
    if a.kill and o:
        left = kill([r["pid"] for r in o])
        print(f"stopped {len(o) - len(left)} of {len(o)} orphans" + (f"; still alive: {left}" if left else ""))
        return 1 if left else 0
    return 1 if (o or z) else 0


if __name__ == "__main__":
    sys.exit(main())
