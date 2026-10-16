#!/usr/bin/env python3
"""render_many on real Native (T080): results equal serial renders, in variant order.

Mixed variants over block-library templates (a Preset per model, then parameter dicts on top,
which accumulate as in the serial fallback), rendered once through ``NativeEngine.render_many``
(this engine plus a pool, ``--hosts`` in all) and once through ``base.serial_render_many`` on a
separate single engine. Every pair must null at the repeat floor (-250 dB or better for
non-time-variant blocks; Native renders are bit-identical across hosts). Also an
``UnsupportedModelChange`` variant must raise. Prints a JSON summary with timings.

  .venv/bin/python scripts/native_render_many_check.py [--hosts 3]
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
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

from tonematch.catalog import load_catalog
from tonematch.engines import base
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib
from tonematch.engines.base import UnsupportedModelChange

MODELS = {"HD2_DistScream808": ("Gain", "Tone"), "HD2_DelayTransistorTape": ("Time", "Feedback"),
          "HD2_AmpEssexA30": ("Drive", "Treble"), "HD2_Reverb63Spring": ("Decay", "Mix")}
FLOOR_DB = -250.0


def host_count() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], check=False,
                       capture_output=True, text=True)
    return len(r.stdout.split())


def variants(cat: dict) -> list:
    out: list = []
    for m, (p1, p2) in MODELS.items():
        out.append(lib.block_preset(m, cat))
        tpl = lib.template_for(m)
        r1, r2 = (tuple(tpl.ranges.get(f"0:{k}", (0.0, 1.0))) for k in (p1, p2))
        out += [{(0, p1): nat.cc_value(*r1, 30)}, {(0, p2): nat.cc_value(*r2, 90)},
                {(0, p1): nat.cc_value(*r1, 110)}]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hosts", type=int, default=3)
    a = ap.parse_args()
    if host_count() + a.hosts + 1 > 6:
        raise SystemExit(f"{host_count()} Native hosts running; would pass 6")
    os.environ["TONEMATCH_NATIVE_MANY_HOSTS"] = str(a.hosts)
    cat = load_catalog()
    vs = variants(cat)
    dry = nat.test_signal(2.0)
    t0 = time.perf_counter()
    e = nat.NativeEngine(templates_dir=lib.library_templates(), autocapture=False)
    ref = nat.NativeEngine(templates_dir=lib.library_templates(), autocapture=False)
    try:
        t1 = time.perf_counter()
        many = base.render_many(e, dry, vs)
        t_first = time.perf_counter() - t1  # includes opening the extra hosts
        t1 = time.perf_counter()
        many2 = base.render_many(e, dry, vs)
        t_many = time.perf_counter() - t1
        t1 = time.perf_counter()
        serial = base.serial_render_many(ref, dry, vs)
        t_serial = time.perf_counter() - t1
        nulls = [round(nat.null_db(x, y), 1) for x, y in zip(many, serial, strict=True)]
        again = [round(nat.null_db(x, y), 1) for x, y in zip(many, many2, strict=True)]
        neighbours = [round(nat.null_db(serial[i], serial[i + 1]), 1) for i in range(len(serial) - 1)]
        raised = False
        bad = lib.block_preset("HD2_DistScream808", cat)
        bad.slots[0].model_id = "HD2_NotAModel"
        try:
            base.render_many(e, dry, [vs[0], bad])
        except UnsupportedModelChange:
            raised = True
        hosts_used = 1 + (e._many_pool.workers if e._many_pool else 0)
    finally:
        e.close()
        ref.close()
    res = {"variants": len(vs), "hosts": hosts_used, "worst_null_vs_serial_db": max(nulls),
           "nulls_vs_serial_db": nulls, "worst_repeat_db": max(again),
           "closest_neighbour_db": max(neighbours), "order_distinct": max(neighbours) > -60,
           "unsupported_raised": raised, "render_many_s": round(t_many, 2),
           "render_many_first_call_s": round(t_first, 2), "serial_s": round(t_serial, 2),
           "speedup": round(t_serial / t_many, 2), "wall_s": round(time.perf_counter() - t0, 1),
           "pass": max(nulls) <= FLOOR_DB and raised, "hosts_after": host_count()}
    out = ROOT / "cache" / "native" / "render_many_check.json"
    res = json.loads(json.dumps(res, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
