#!/usr/bin/env python3
"""Engine-push checks on Native (native-engine, T082, 2026-10-16). One Native host, synthetic
signals only, block-library templates (``native_library``). Writes
``cache/native/push_checks/<cmd>.json``.

``tail``   (N6b) how much pre-roll a reset render needs after a loud render on a long-reverb and
           a long-delay template: render loud clip A, then clip B, then B again, at each pre-roll;
           the null of B-after-A against B-after-B is the tail the host's state reset let through.
``exactlo`` (N6a) a param moved to its top and then commanded to exactly lo renders equal to the
           same template loaded with lo first (history-free).
``hum``    (N6c) the 5 preamps whose Hum CC is baked at 0: the preset renders through its template,
           the Hum CC is dead (0 vs 1 on silence and on a signal, at defaults and at full Drive),
           and a preset with Hum != 0 is refused rather than rendered at the baked 0.

  .venv/bin/python scripts/native_push_checks.py tail|exactlo|hum
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

import numpy as np

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib
from tonematch.engines.base import UnsupportedModelChange

SR = nat.SAMPLE_RATE
OUT = ROOT / "cache" / "native" / "push_checks"
HUM_BAKED = ("HD2_PreampA30FawnBrt", "HD2_PreampDividedDuo", "HD2_PreampEssexA30",
             "HD2_PreampMatchstickCh1", "HD2_PreampMatchstickCh2")
TAIL_CASES = [  # (tag, model, params moved to the long end)
    ("shimmer_12.8s", "VIC_ReverbShimmer", {"Decay": 12.8, "Mix": 1.0}),
    ("hall_decay1", "HD2_ReverbHall", {"Decay": 1.0, "Mix": 1.0}),
    # Simple Delay's Feedback CC is baked (unverified at defaults), so it stays at 0.35 there;
    # Transistor Tape's Feedback is CC-verified and goes to 0.8.
    ("simple_1.27s_fb0.35", "HD2_DelaySimpleDelay", {"Time": 1.27, "Mix": 1.0}),
    ("tape_1.27s_fb0.8", "HD2_DelayTransistorTape", {"Time": 1.27, "Feedback": 0.8, "Mix": 1.0}),
]
PREROLLS_S = (0.1, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0)


def host_count() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], check=False,
                       capture_output=True, text=True)
    return len(r.stdout.split())


def engine(preroll: int | None = None) -> nat.NativeEngine:
    if host_count() >= 6:
        raise SystemExit("6 Native hosts already running")
    return nat.NativeEngine(templates_dir=lib.library_templates(), autocapture=False, preroll=preroll)


def preset(model: str, catalog: dict, **params):
    p = lib.block_preset(model, catalog)
    p.slots[0].params.update(params)
    return p


def cmd_tail(a) -> dict:
    cat = load_catalog()
    loud = (nat.test_signal(2.0, seed=2) * 2.0).astype(np.float32)
    clip = nat.test_signal(3.0, seed=1)  # longer than the 1.27 s echo
    e = engine()
    rows = []
    try:
        for tag, mid, ps in TAIL_CASES:
            if a.only and tag not in a.only.split(","):
                continue
            if lib.is_time_variant(mid):
                continue
            e.load(preset(mid, cat, **ps))
            for pre_s in PREROLLS_S:
                e.preroll = int(pre_s * SR)
                e.render(loud)
                b_after_a = e.render(clip)
                b_after_b = e.render(clip)
                b_again = e.render(clip)
                rows.append({"case": tag, "model": mid, "preroll_s": pre_s,
                             "after_loud_null_db": round(nat.null_db(b_after_a, b_after_b), 1),
                             "repeat_null_db": round(nat.null_db(b_after_b, b_again), 1),
                             "out_rms_db": round(nat._rms_db(b_after_b), 1)})
                print(json.dumps(rows[-1]), flush=True)
    finally:
        e.close()
    return {"rows": rows, "default_tail_preroll_s": nat.NativeEngine.TAIL_PREROLL / SR}


def cmd_exactlo(a) -> dict:
    cat = load_catalog()
    cases = [("HD2_AmpEssexA30", "Mid"), ("HD2_AmpEssexA30", "Cut"), ("HD2_Cab1x12BlueBell", "EarlyReflections"),
             ("HD2_DelaySimpleDelay", "Time"), ("HD2_DistScream808", "Gain")]
    x = nat.test_signal(1.0)
    rows = []
    e = engine()
    try:
        for mid, prm in cases:
            p = lib.block_preset(mid, cat)
            tpl = lib.template_for(mid)
            ref = f"0:{prm}"
            if tpl is None or ref not in tpl.verified:
                rows.append({"model": mid, "param": prm, "skipped": "not CC-verified"})
                continue
            lo, hi = tpl.ranges.get(ref, (0.0, 1.0))
            first = preset(mid, cat, **{prm: lo})
            e.load(first)
            y_first = e.render(x)
            e.load(preset(mid, cat, **{prm: hi}))
            e.render(x)
            e.set_params({(0, prm): lo})  # back to exactly lo
            y_back = e.render(x)
            e.load(p)
            e.set_params({(0, prm): nat.cc_value(lo, hi, 0)})  # float32 lo
            y_f32 = e.render(x)
            rows.append({"model": mid, "param": prm, "lo": lo, "hi": hi, "sent": e._host_params[tpl.map[ref]],
                         "back_to_lo_vs_lo_first_db": round(nat.null_db(y_back, y_first), 1),
                         "float32_lo_vs_lo_first_db": round(nat.null_db(y_f32, y_first), 1)})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        e.close()
    return {"rows": rows}


def cmd_hum(a) -> dict:
    cat = load_catalog()
    idx = lib.load_index()
    x = nat.test_signal(1.0)
    sil = np.zeros(SR, np.float32)
    rows = []
    e = engine()
    try:
        for mid in HUM_BAKED:
            ent = idx["models"].get(mid, {})
            tpl = lib.template_for(mid)
            p = lib.block_preset(mid, cat)
            row = {"model": mid, "hum_in_preset": p.slots[0].params.get("Hum"),
                   "frozen_baked": ent.get("frozen_baked"), "baked_hum": tpl.baked.get("0:Hum") if tpl else None,
                   "hum_cc": tpl.map.get("0:Hum") if tpl else None, "matches": lib.matches(tpl, p) if tpl else False}
            e.load(p)
            row["loaded_template"] = nat.template_stem(e.current_template) == ent.get("template")
            cc = row["hum_cc"]
            for drive_tag, extra in (("defaults", {}), ("drive_max", {"Drive": 1.0})):
                if extra and "0:Drive" not in tpl.verified:
                    continue
                if extra:
                    e.set_params({(0, k): v for k, v in extra.items()})
                outs = {}
                for hv in (0.0, 1.0):
                    if cc:
                        e.host.call("set_params", values={cc: hv})
                        e._host_params[cc] = hv
                    outs[hv] = (e.render(x), e._raw_render(np.concatenate([np.zeros(e.preroll, np.float32), sil])))
                row[f"{drive_tag}_hum0_vs_hum1_signal_db"] = round(nat.null_db(outs[1.0][0], outs[0.0][0]), 1)
                row[f"{drive_tag}_silence_rms_hum0_db"] = round(nat._rms_db(outs[0.0][1][e.preroll:]), 1)
                row[f"{drive_tag}_silence_rms_hum1_db"] = round(nat._rms_db(outs[1.0][1][e.preroll:]), 1)
            q = lib.block_preset(mid, cat)
            q.slots[0].params["Hum"] = 0.5
            try:
                e.load(q)
                row["hum_0.5_refused"] = False
            except UnsupportedModelChange:
                row["hum_0.5_refused"] = True
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        e.close()
    return {"rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("tail", "exactlo", "hum"))
    ap.add_argument("--only", default="", help="tail: comma list of case tags")
    a = ap.parse_args()
    t0 = time.perf_counter()
    res = {"tail": cmd_tail, "exactlo": cmd_exactlo, "hum": cmd_hum}[a.cmd](a)
    res.update(wall_s=round(time.perf_counter() - t0, 1), library=lib.VERSION, hosts_after=host_count())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{a.cmd}{'_' + a.only.replace(',', '_') if a.only else ''}.json").write_text(json.dumps(res, indent=1, default=float))
    print("wrote", OUT / f"{a.cmd}.json", "wall", res["wall_s"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
