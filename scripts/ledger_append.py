#!/usr/bin/env python3
"""Append one row to ledger/runs.csv. See contracts/ledger-format.md."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import subprocess
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "ledger" / "runs.csv"
KINDS = {"smoke", "render-cost", "separate", "loss-trust", "benchmark", "stress",
         "parity", "engine-compare", "ab", "gate"}


def git_commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
        dirty = subprocess.call(["git", "diff", "--quiet"]) != 0
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--kind", required=True, choices=sorted(KINDS))
    p.add_argument("--engine", default="-")
    p.add_argument("--dataset", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--value", required=True, type=float)
    p.add_argument("--renders", default="")
    p.add_argument("--wall-clock-s", default="")
    p.add_argument("--notes", default="")
    a = p.parse_args()

    row = [
        dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        git_commit(),
        os.environ.get("TONEMATCH_AGENT", "human"),
        a.kind, a.engine, a.dataset, a.metric, a.value, a.renders, a.wall_clock_s, a.notes,
    ]
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    new = not LEDGER.exists() or LEDGER.stat().st_size == 0
    with LEDGER.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "commit", "agent", "kind", "engine", "dataset",
                        "metric", "value", "renders", "wall_clock_s", "notes"])
        w.writerow(row)
    print(",".join(str(x) for x in row))


if __name__ == "__main__":
    main()
