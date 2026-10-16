#!/usr/bin/env python3
"""Bypass patterns over MIDI CC vs drop-captured truth on an 8-block chain (Native phase 2).

One template of the chain is captured through ``NativeEngine`` autocapture with a bypass CC on
every block (``capture_chain(bypass_ccs=True)``). For each of ``--patterns`` bypass patterns of
the switchable blocks (all on, all off, then random), the chain with that pattern written into
the file is drop-captured as the truth (no CCs, nothing verified). One engine then renders the
DI through ``load(pattern)`` (bypass by CC) and through the truth state, in a shuffled order
and again in reverse order, and nulls each pair.

Default chain: the T045 C8 file (comp, drive, amp, cab, EQ, chorus, delay, reverb); its six
switchable blocks are the synthetic set's. Synthetic or Guitar-TECHS DI only.

  .venv/bin/python scripts/native_bypass_patterns.py [--patterns 8] [--ledger]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import (
    ChainTemplate,
    NativeEngine,
    capture_chain,
    null_db,
    start_state_path,
)
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "bypass_patterns"
FIXED = ("amp", "cab")  # always on in the synthetic set (full rig)


def with_pattern(base, on: dict[int, bool], name: str):
    p = copy.deepcopy(base)
    p.name = name
    for i, v in on.items():
        p.slots[i].enabled = v
        for sn in p.snapshots:
            if p.slots[i].key in sn.enabled:
                sn.enabled[p.slots[i].key] = v
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hlx", type=Path, default=ROOT / "cache/blockserial/C8/8f50d3d2e224_BS_C8.hlx")
    ap.add_argument("--di", type=Path, default=ROOT / "cache/parity/di_10s_tail.wav")
    ap.add_argument("--clip-s", type=float, default=6.0)
    ap.add_argument("--patterns", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--bypass-only", action="store_true",
                    help="template with bypass CCs only (params stay at the file's values)")
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    import soundfile as sf

    from tonematch.catalog import load_catalog

    base = read_hlx(a.hlx)
    sw = [i for i, s in enumerate(base.slots) if s.model_id and s.category not in FIXED and s.key
          and s.key.startswith("block")]
    rng = random.Random(a.seed)
    pats = [dict.fromkeys(sw, True), dict.fromkeys(sw, False)]
    seen = {tuple(p.values()) for p in pats}
    while len(pats) < min(a.patterns, 2 ** len(sw)):
        p = {i: rng.random() < 0.5 for i in sw}
        if tuple(p.values()) not in seen:
            seen.add(tuple(p.values()))
            pats.append(p)
    tag = lambda p: "".join("1" if p[i] else "0" for i in sw)
    cat = load_catalog()
    start = start_state_path().read_bytes() if start_state_path().is_file() else None
    from tonematch.engines.native import locate_plugin

    plugin = str(locate_plugin())
    x, _ = sf.read(a.di, dtype="float32")
    x = x[: int(a.clip_s * SAMPLE_RATE)]
    res: dict = {"hlx": str(a.hlx.relative_to(ROOT)), "switchable": [base.slots[i].category for i in sw]}
    tdir = OUT / ("cc_bypass_only" if a.bypass_only else "cc_templates")
    cc_s = None
    if a.bypass_only and not any(tdir.glob("*.json")):
        t0 = time.perf_counter()  # catalog={} gives no param ranges: only bypass CCs are assigned
        capture_chain(with_pattern(base, pats[0], "BP bypass-only"), plugin, start_state=start,
                      templates_dir=tdir, catalog={})
        cc_s = time.perf_counter() - t0
    e = NativeEngine(preroll=3 * SAMPLE_RATE, autocapture=not a.bypass_only, templates_dir=tdir)
    try:
        t0 = time.perf_counter()
        e.load(with_pattern(base, pats[0], "BP cc"))
        cc_s = e.last_capture_s or cc_s
        res["bypass_only"] = a.bypass_only
        res["cc_capture_s"] = round(cc_s, 1) if cc_s else None
        res["cc_load_s"] = round(time.perf_counter() - t0, 1)
        tpl = e.current_template
        res["bypass_map"] = {k: v for k, v in tpl.map.items() if k.endswith("@enabled")}
        res["bypass_verified"] = sorted(k for k in tpl.verified if k.endswith("@enabled"))
        res["mapped"], res["verified"] = len(tpl.map), len(tpl.verified)
        # Put every CC-driven value on the 7-bit grid (phase 1 (d): off-grid values miss the
        # truth by -26..-45 dB, and a chorus rate off the grid decorrelates whole renders), so
        # that only the bypass route is under test.
        for ref in [] if a.bypass_only else tpl.verified:
            i, name = ref.split(":", 1)
            if name == "@enabled":
                continue
            lo, hi = tpl.ranges.get(ref, [0.0, 1.0])
            u = (float(base.slots[int(i)].params[name]) - lo) / ((hi - lo) or 1.0)
            base.slots[int(i)].params[name] = lo + round(min(max(u, 0.0), 1.0) * 127) / 127 * (hi - lo)
        truths: dict[str, ChainTemplate] = {}
        t_truth = []
        for p in pats:
            d = OUT / ("truth" if a.bypass_only else "truth_q") / tag(p)
            have = [t for ts in ChainTemplate.load_variants(d).values() for t in ts]
            if have:
                truths[tag(p)] = have[0]
                continue
            t1 = time.perf_counter()
            truths[tag(p)] = capture_chain(with_pattern(base, p, f"BP {tag(p)}"), plugin, start_state=start,
                                           templates_dir=d, verify_ccs=False, bypass_ccs=False, catalog=cat)
            t_truth.append(time.perf_counter() - t1)
        res["truth_capture_s"] = [round(t, 1) for t in t_truth]

        def truth(p):
            t = truths[tag(p)]
            e._set_state(t.state)
            e.current_chain, e.current_template = t.chain, t
            return e.render(x)

        def via_cc(p):
            e.load(with_pattern(base, p, "BP cc"))
            if e.current_template is not tpl:
                raise RuntimeError(f"pattern {tag(p)} did not reuse the bypass-CC template")
            return e.render(x)

        rows = []
        for order_name, order in (("shuffled", rng.sample(pats, len(pats))), ("reversed", pats[::-1])):
            for p in order:
                y_cc = via_cc(p)
                y_t = truth(p)
                rows.append({"order": order_name, "pattern": tag(p), "null_db": round(null_db(y_t, y_cc), 1),
                             "vs_all_on_db": round(null_db(truth(pats[0]), y_cc), 1)})
                print(rows[-1], flush=True)
        res["rows"] = rows
        res["worst_null_db"] = max(r["null_db"] for r in rows)
    finally:
        e.close()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"result{'_bypass_only' if a.bypass_only else ''}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    if a.ledger:
        subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "parity", "--engine",
                        "native", "--dataset", "smoke", "--metric", "bypass_patterns_cc_vs_drop_truth_db",
                        f"--value={res['worst_null_db']}", "--renders", str(3 * len(rows)), "--notes",
                        (f"phase 2 scripts/native_bypass_patterns.py: {len(pats)} bypass patterns of the 6 "
                         f"switchable blocks of the C8 chain ({','.join(res['switchable'])}; amp+cab on) driven "
                         "by per-block bypass CCs (.hlx controller @enabled @controller 8) on ONE template "
                         f"(capture {res['cc_capture_s']} s; {res['verified']}/{res['mapped']} CCs verified) vs "
                         "each pattern drop-captured as truth; 2 orders; worst null; 6 s Guitar-TECHS DI 3 s "
                         "preroll; host state reset; cache/native/bypass_patterns/result.json")],
                       check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
