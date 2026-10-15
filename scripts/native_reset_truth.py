#!/usr/bin/env python3
"""Independent check of the two Native reset paths on random settings (phase 1 (d)).

For K random settings of a CC template's mapped parameters, the truth is a dedicated drop
capture of the preset with those values written into the file (no CC involved). The same
settings are then rendered through the template + CCs with (1) the host's state reset and
(2) pedalboard's reset (the instance re-created), each on a fresh host, first render. Prints
and optionally ledgers the nulls of both against the truth.

  .venv/bin/python scripts/native_reset_truth.py [--hlx cache/native/drop/4d54671b96a6_a079bb4051.hlx] [--k 2] [--ledger]
"""
from __future__ import annotations

import argparse
import copy
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines.native import (
    ChainTemplate,
    NativeEngine,
    capture_chain,
    chain_key,
    locate_plugin,
    null_db,
    start_state_path,
    test_signal,
)
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "reset_truth"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hlx", type=Path, default=ROOT / "cache/native/drop/4d54671b96a6_a079bb4051.hlx")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quantize", action="store_true", help="truth values on the 7-bit CC grid")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    from tonematch.catalog import load_catalog

    plugin = str(locate_plugin() or "")
    base = read_hlx(a.hlx)
    tpl = max(ChainTemplate.load_variants()[chain_key(base)], key=lambda t: len(t.map))
    refs = [r for r in tpl.map if r in tpl.verified] or list(tpl.map)
    rng = np.random.default_rng(a.seed)
    start = start_state_path().read_bytes() if start_state_path().is_file() else None
    x = test_signal(2.0)
    rows = []
    for k in range(a.k):
        p = copy.deepcopy(base)
        p.name = f"ResetTruth {a.seed}-{k}{'q' if a.quantize else ''}"
        vals = {}
        for r in refs:
            i, name = r.split(":", 1)
            lo, hi = tpl.ranges.get(r, [0.0, 1.0])
            u = rng.uniform(0.05, 0.95)
            if a.quantize:  # put the truth on the 7-bit MIDI CC grid
                u = round(u * 127) / 127
            v = float(lo + u * (hi - lo))
            p.slots[int(i)].params[name] = v
            vals[(int(i), name)] = v
        own = capture_chain(p, plugin, start_state=start, templates_dir=OUT / f"own_{a.seed}_{k}{'q' if a.quantize else ''}",
                            verify_ccs=False, catalog=load_catalog())
        out = {}
        for mode in ("truth", "state", "reinstantiate"):
            os.environ["TONEMATCH_NATIVE_HOST_RESET"] = "state" if mode == "truth" else mode
            e = NativeEngine(preroll=144000, autocapture=False)
            try:
                if mode == "truth":
                    e._set_state(own.state)
                    e.current_chain, e.current_template = own.chain, own
                else:
                    e._match_template = lambda pr, key: (tpl, "")
                    e.load(base)
                    e.set_params(vals)
                out[mode] = e.render(x)
            finally:
                e.close()
        row = {"setting": k, "state_vs_truth_db": round(null_db(out["truth"], out["state"]), 1),
               "reinstantiate_vs_truth_db": round(null_db(out["truth"], out["reinstantiate"]), 1)}
        rows.append(row)
        print(row, flush=True)
    if a.ledger:
        for metric in ("state_vs_truth_db", "reinstantiate_vs_truth_db"):
            worst = max(r[metric] for r in rows)
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "parity",
                            "--engine", "native", "--dataset", "smoke", "--metric", f"random_cc_{metric}",
                            f"--value={worst}", "--renders", str(3 * len(rows)), "--notes",
                            (f"phase 1 (d) scripts/native_reset_truth.py{' --quantize' if a.quantize else ''}: {len(rows)} random settings of "
                             f"{len(refs)} verified CC params on template {tpl.name!r}; truth = each setting "
                             f"written into the .hlx and drop-captured; first render on a fresh host each; "
                             f"value = worst; all: {[r[metric] for r in rows]}")],
                           check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
