#!/usr/bin/env python3
"""Response tables for the linear specialist (T117): per cab and EQ model, render the periodic probe
through the block at a grid of knob settings and cache the responses
(`tonematch.metrics.linear_specialist.build_table`). Library preparation, done once per library
version; the specialist then fits knobs at zero render cost.

  python scripts/linear_tables.py [--models M ...] [--pool-of SET ...] [--redo] [--limit N]

Default models: every cab and EQ model of the block library with continuous CC-verified knobs, the
named sets' pools first. The probe is a synthetic multisine (no DI clip, no song audio). ONE Native
host; refuses when the machine already runs 6. Output local: cache/linear-tables/<library version>/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

CATS = ("cab", "eq")


def library_models(catalog) -> dict[str, str]:
    """model -> category for every cab and EQ library model with free knobs."""
    import synthetic_v1 as S

    from tonematch.engines import native_library as NL

    out = {}
    for m, e in sorted(NL.load_index()["models"].items()):
        c = e["category"]
        if c not in CATS or NL.is_routing(m) or e.get("time_variant"):
            continue
        tpl = NL.template_for(m)
        if tpl is None or not S.free_params(m, tpl, catalog, c):
            continue
        out[m] = c
    return out


def lti_only(a, order, out_dir) -> int:
    """Two renders per existing table without an LTI verdict (tables built before the check existed)."""
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.metrics import linear_specialist as L

    todo = [m for m in order if (out_dir / f"{m}.json").exists()
            and "lti" not in json.loads((out_dir / f"{m}.json").read_text()).get("meta", {})]
    print(f"{len(todo)} tables without an LTI check", flush=True)
    if not todo:
        return 0
    if S.machine_hosts() + 1 > S.MAX_MACHINE_HOSTS:
        print("refusing: too many native_host processes", file=sys.stderr)
        return 3
    cat = load_catalog()
    engine = S.make_engine()
    try:
        for m in todo:
            t = L.ResponseTable.load(out_dir, m)
            b = S.Block(m, t.category, S.free_params(m, NL.template_for(m), cat, t.category), cat)
            engine.load(b.with_values({n: d for n, _, _, d in b.free}))
            t.meta.update(L.lti_check(lambda x: np.asarray(engine.render(x), np.float32)))
            t.save(out_dir)
            print(f"{m:40s} lti {t.meta['lti']} level {t.meta['lti_level_db']:.3f} period {t.meta['lti_period_db']:.3f} dB",
                  flush=True)
    finally:
        engine.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--pool-of", nargs="*", default=["synthetic-lindev", "synthetic-v1"],
                    help="sets whose cab/eq pools go first (never a sealed set)")
    ap.add_argument("--only-pools", action="store_true", help="only the named sets' pool models")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--lti-only", action="store_true", help="add the LTI check to existing tables that lack it")
    a = ap.parse_args(argv)
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.metrics import linear_specialist as L

    cat = load_catalog()
    allm = library_models(cat)
    first = []
    for s in a.pool_of:
        mp = ROOT / "cache" / s / "manifest.json"
        if mp.exists():
            pool = json.loads(mp.read_text())["pool"]
            first += [m for c in CATS for m in pool.get(c, {}) if m in allm and m not in first]
    order = a.models if a.models else first + ([] if a.only_pools else [m for m in allm if m not in first])
    out_dir = L.table_dir()
    if a.lti_only:
        return lti_only(a, order, out_dir)
    todo = [m for m in order if a.redo or not (out_dir / f"{m}.json").exists()]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} models to tabulate -> {out_dir}", flush=True)
    if not todo:
        return 0
    running = S.machine_hosts()
    if running + 1 > S.MAX_MACHINE_HOSTS:
        print(f"refusing: {running} native_host processes running (limit {S.MAX_MACHINE_HOSTS})", file=sys.stderr)
        return 3
    probe = L.probe_signal()
    engine = S.make_engine()
    t0 = time.time()
    try:
        for m in todo:
            c = allm.get(m) or NL.load_index()["models"][m]["category"]
            b = S.Block(m, c, S.free_params(m, NL.template_for(m), cat, c), cat)
            t1 = time.time()

            def response(values, b=b):
                engine.load(b.with_values(values))
                return L.probe_response(np.asarray(engine.render(probe), np.float32))
            t = L.build_table(response, m, c, {n: (lo, hi) for n, lo, hi, _ in b.free},
                              {n: d for n, _, _, d in b.free})
            engine.load(b.with_values({n: d for n, _, _, d in b.free}))
            t.meta.update(L.lti_check(lambda x: np.asarray(engine.render(x), np.float32)))
            t.meta.update(library_version=NL.VERSION, wall_s=round(time.time() - t1, 1))
            t.save(out_dir)
            print(f"{m:40s} {c:3s} lti {t.meta['lti']} groups {t.groups} renders {t.meta['renders']} check rms "
                  f"{t.meta['check_rms_db']:.3f} max {t.meta['check_max_db']:.2f} dB {t.meta['wall_s']} s", flush=True)
    finally:
        engine.close()
    print(f"done {len(todo)} models in {time.time() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
