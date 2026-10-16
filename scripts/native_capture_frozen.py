#!/usr/bin/env python3
"""Capture the match_song template sets at each engine policy's frozen values (T055 follow-up).

``scripts/match_song.py`` writes the frozen values (``src/tonematch/param_policy.json``: Hum and
Ripple 0 on both engines, cab EarlyReflections 0 for Stomp-bound runs) into a set's preset before
it picks a template, so a template baked at other values is stale and the set is skipped. This
script captures, for each named set and each policy, the template that matches the frozen preset,
in the set's own templates directory, with the one-session capture (``capture_missing``). Policies
that give the same baked values (a frozen param whose CC is live is not baked) share one capture.
Old variants stay in the directory; ``pick_template`` chooses by fingerprint.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_capture_frozen.py B0 B

Needs a logged-in GUI session (Native's editor shows during drops). Local only.
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
sys.path.insert(0, str(ROOT / "scripts"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

from match_song import NATIVE_FROZEN, STOMP_FROZEN, TEMPLATE_SETS, freeze, pick_template

from tonematch.catalog import load_catalog
from tonematch.engines.native import ChainTemplate, NativeEngine, chain_key
from tonematch.preset import read_hlx

POLICIES = {"stomp": STOMP_FROZEN, "native": NATIVE_FROZEN}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="+", help=f"template set names ({', '.join(TEMPLATE_SETS)})")
    ap.add_argument("--policies", default="stomp,native")
    a = ap.parse_args()
    catalog = load_catalog()
    rows = []
    for name in a.sets:
        hlx, tdir = TEMPLATE_SETS[name]
        d = ROOT / tdir
        presets = {pol: freeze(read_hlx(ROOT / hlx, catalog), POLICIES[pol]) for pol in a.policies.split(",")}
        e = NativeEngine(templates_dir=d, autocapture=False)
        try:
            t0 = time.perf_counter()
            errs = e.capture_missing(list(presets.values()), catalog=catalog)
            wall = time.perf_counter() - t0
        finally:
            e.close()
        variants = ChainTemplate.load_variants(d)
        for pol, p in presets.items():
            t = pick_template(variants.get(chain_key(p), []), p)
            rows.append({"set": name, "policy": pol, "template": t.baked_hash[:10] if t else None,
                         "verified": len(t.verified) if t else 0, "capture_wall_s": round(wall, 2),
                         "errors": [str(x) for x in errs.values()]})
            print(json.dumps(rows[-1]), flush=True)
    return 0 if all(r["template"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
