#!/usr/bin/env python3
"""T051 follow-up: which values does Helix Native render when a dropped .hlx's snapshot
controller values disagree with its block values?

Takes a file whose snapshots disagree (default: the old H drop file, delay block Mix 0.25 /
Feedback 0.40, snapshots 0.23 / 0.52), and drop-captures three variants with no CC changes:
  stale   the file as is
  block   snapshots rewritten to the block values (the lead's rule)
  snap    blocks rewritten to the snapshot values
then renders the DI through each captured state (one host, state reset, 3 s pre-roll) and
nulls stale against the other two. Whichever stale nulls with is what Native applied.

  .venv/bin/python scripts/native_snapshot_check.py [--hlx cache/native/drop/909d252a4935_auto.hlx] [--ledger]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import (
    NativeEngine,
    capture_chain,
    locate_plugin,
    null_db,
    snapshot_mismatches,
    start_state_path,
)
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "snapshot_check"


def variant(preset, mode: str):
    p = copy.deepcopy(preset)
    p.name = f"SnapChk {mode}"
    by_key = {s.key: s for s in p.slots}
    for sn in p.snapshots:
        for bkey, ps in sn.params.items():
            blk = by_key.get(bkey)
            if blk is None:
                continue
            for name in ps:
                if name not in blk.params:
                    continue
                if mode == "block":
                    ps[name] = blk.params[name]
                elif mode == "snap":
                    blk.params[name] = ps[name]
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hlx", type=Path, default=ROOT / "cache/native/drop/909d252a4935_auto.hlx")
    ap.add_argument("--di", type=Path, default=ROOT / "cache/parity/di_10s_tail.wav")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    import soundfile as sf

    from tonematch.catalog import load_catalog

    plugin = str(locate_plugin() or "")
    base = read_hlx(a.hlx)
    mism = snapshot_mismatches(base)
    print("snapshot/block mismatches:", mism)
    if not mism:
        print("file has no disagreeing snapshots; nothing to test")
        return 2
    catalog = load_catalog()
    sp = start_state_path()
    start = sp.read_bytes() if sp.is_file() else None
    states = {}
    for mode in ("stale", "block", "snap"):
        p = base if mode == "stale" else variant(base, mode)
        if mode == "stale":
            p = copy.deepcopy(base)
            p.name = "SnapChk stale"
        tpl = capture_chain(p, plugin, start_state=start, templates_dir=OUT / mode, verify_ccs=False,
                            catalog=catalog)
        states[mode] = tpl.state
    x, _ = sf.read(a.di, dtype="float32")
    pre = 3 * SAMPLE_RATE
    padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(SAMPLE_RATE // 2, np.float32)])
    e = NativeEngine(preroll=pre, autocapture=False)
    ys = {}
    try:
        for mode, st in states.items():
            e._set_state(st)
            ys[mode] = e._raw_render(padded)[pre:pre + len(x)]
    finally:
        e.close()
    res = {"file": str(a.hlx), "mismatches": mism,
           "stale_vs_block_db": round(null_db(ys["stale"], ys["block"]), 1),
           "stale_vs_snap_db": round(null_db(ys["stale"], ys["snap"]), 1),
           "block_vs_snap_db": round(null_db(ys["block"], ys["snap"]), 1)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    if a.ledger:
        applied = "snapshot" if res["stale_vs_snap_db"] < res["stale_vs_block_db"] else "block"
        for metric in ("stale_vs_block_db", "stale_vs_snap_db"):
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "parity",
                            "--engine", "native", "--dataset", "smoke", "--metric", f"snapshot_{metric}",
                            "--value", str(res[metric]), "--renders", "3", "--notes",
                            (f"T051 follow-up: drop-loaded {a.hlx.name} (snapshots disagree with blocks: "
                             f"{'; '.join(mism)}) vs variants with snapshots=block values and blocks=snapshot "
                             f"values; Native applied the {applied} values on drop load; DI {a.di.name} 3 s preroll")],
                           check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
