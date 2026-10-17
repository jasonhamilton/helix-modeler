#!/usr/bin/env python3
"""Live check for values outside the narrowed CC ranges (T098, engine push E3).

Cases (block-library-v1.2 single-block presets at defaults, one value moved outside its v1.2 range):

  time   Simple Delay Time 2.0 s     (v1.2 grid 0..1.27 s)       -> expected: v1 template (0..8 s)
  level  Simple Delay Level -40 dB   (v1.2 grid -25.75..+6 dB)   -> expected: v1.1 template (-60..+6)
  rate   MM4 Analog Chorus Speed 8 Hz (v1.2 grid 0..5.08 Hz)     -> expected: v1 template (0..15 Hz)
  ondemand  Simple Delay Time 2.0 s with the older versions hidden: a full-range variant is captured
            on demand (``native_library.capture_on_demand``, the engine's own path), cached under
            ``on_demand/`` and recorded in the v1.2 index; a second request reuses it.

For each case ``NativeEngine.load`` + ``render`` (the engine's own template choice) is nulled against a
drop capture of the same block with every CC-driven value written at the value the chosen template's
grid plays (``parity_snap.snapped_values``: the requested value and the defaults snapped to 128 steps).
A second truth with the exact requested value (unsnapped) shows the grid error, for information.

Never more than ONE Native host at a time: the capture host (on-demand capture, then the truth drops)
is closed before the render host opens. Refuses to start when the machine already runs more than
``--max-machine-hosts`` - 1 native_host processes. Input: the licence-clean real smoke DI
(``fixtures/smoke/di_2s_real.wav``, Guitar-TECHS CC BY 4.0) plus 2.5 s of silence so 2 s echoes land.
Writes ``cache/native/fullrange_check/result.json``; ``--ledger`` appends smoke rows (dataset
``cc-range-fullrange``).

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_fullrange_check.py --ledger
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from engine_smoke import DI_REAL, read_wav_f32
from parity_snap import snapped_values

from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

OUT = ROOT / "cache" / "native" / "fullrange_check"
SR = nat.SAMPLE_RATE
THRESHOLD_DB = -100.0
# (tag, model, param, value, fallback set: "older" = v1.1/v1 templates, "ondemand" = on-demand store only)
CASES = [
    ("time", "HD2_DelaySimpleDelay", "Time", 2.0, "older"),
    ("level", "HD2_DelaySimpleDelay", "Level", -40.0, "older"),
    ("rate", "HD2_MM4AnalogChorus", "Speed", 8.0, "older"),
    ("ondemand", "HD2_DelaySimpleDelay", "Time", 2.0, "ondemand"),
]


def native_hosts() -> int:
    r = subprocess.run(["pgrep", "-f", "native_host"], capture_output=True, text=True, check=False)
    return len([x for x in r.stdout.split() if x.strip() and int(x) != os.getpid()])


def dirs_for(kind: str) -> list[Path]:
    if kind == "ondemand":
        return [lib.on_demand_templates()]
    return [lib.library_templates(v) for v in lib.fallback_versions()]


def predict(p, kind: str) -> nat.ChainTemplate | None:
    """The template NativeEngine will pick with ``fallback_dirs=dirs_for(kind)`` (offline)."""
    key = nat.chain_key(p)
    for group in ([nat.dir_variants(lib.library_templates(), key)],
                  [nat.dir_variants(d, key) for d in dirs_for(kind)]):
        ok = [t for g in group for t in g if lib.matches(t, p)]
        if ok:
            return min(ok, key=nat.range_width_key)
    return None


def with_values(p, values: dict[str, float], name: str):
    q = copy.deepcopy(p)
    for ref, v in values.items():
        i, n = ref.split(":", 1)
        q.slots[int(i)].params[n] = v
    q.name = name
    return q


def ledger(metric: str, value: float, notes: str) -> None:
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "native-engine")}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "smoke", "--engine",
                    "native", "--dataset", "cc-range-fullrange", "--metric", metric, "--value", f"{value:.2f}",
                    "--renders", "2", "--notes", notes], check=True, env=env)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", action="store_true", help="append smoke rows (dataset cc-range-fullrange)")
    ap.add_argument("--max-machine-hosts", type=int, default=6)
    a = ap.parse_args()
    running = native_hosts()
    print(f"native_host processes before start: {running}")
    if running + 1 > a.max_machine_hosts:
        print(f"refusing: {running} hosts running, one more passes {a.max_machine_hosts}")
        return 2
    t0 = time.perf_counter()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    presets = {}
    for tag, mid, prm, v, _kind in CASES:
        p = lib.block_preset(mid)
        p.slots[0].params[prm] = v
        p.name = f"FR {tag}"
        presets[tag] = p

    # 1. On-demand full-range capture (capture host only; reused if already cached).
    od = presets["ondemand"]
    od_dir = lib.on_demand_templates()
    calls = []

    def capture(p, d):
        calls.append(time.perf_counter())
        return nat.capture_chain(p, plugin, start_state=start, templates_dir=d)

    def recheck():
        ok = [t for t in nat.dir_variants(od_dir, nat.chain_key(od)) if lib.matches(t, od)]
        return min(ok, key=nat.range_width_key) if ok else None

    t_od = time.perf_counter()
    od_tpl = lib.capture_on_demand(od, capture, wide=["0:Time"], recheck=recheck)
    od_s = time.perf_counter() - t_od
    od_again = lib.capture_on_demand(od, capture, wide=["0:Time"], recheck=recheck)
    od_entry = lib.load_index().get("on_demand", {}).get(nat.template_stem(od_tpl))
    print(f"on-demand: {'captured' if calls else 'cached'} {nat.template_stem(od_tpl)} in {od_s:.2f} s "
          f"(captures {len(calls)}, second request reused: {nat.template_stem(od_again) == nat.template_stem(od_tpl)})")

    # 2. Predict each case's template; drop truths at the snapped and at the exact values.
    plan, truth_presets = {}, []
    for tag, _mid, prm, v, kind in CASES:
        p = presets[tag]
        tpl = predict(p, kind)
        if tpl is None:
            print(f"{tag}: no template holds {prm}={v}")
            return 1
        snaps = {r: sv for r, (_rq, sv, _lo, _hi) in snapped_values(p, tpl).items()}
        plan[tag] = {"template": nat.template_stem(tpl), "ranges": tpl.ranges, "snapped": snaps,
                     "dirs": [str(d.relative_to(ROOT)) for d in dirs_for(kind)]}
        truth_presets.append((tag, "snapped", with_values(p, snaps, f"FR {tag} snap")))
        truth_presets.append((tag, "exact", with_values(p, {}, f"FR {tag} exact")))
    tdir = OUT / "truth"
    st: dict = {}
    t_cap = time.perf_counter()
    outs = nat.capture_chains([q for _, _, q in truth_presets], plugin, start_state=start, templates_dir=tdir,
                              verify_ccs=False, known_live=False, replay_every=0, stats=st)
    cap_s = time.perf_counter() - t_cap
    bad = [f"{t}/{k}: {o}" for (t, k, _), o in zip(truth_presets, outs, strict=True) if isinstance(o, Exception)]
    if bad:
        print("truth capture failures:", bad)
        return 1
    truths = {(t, k): o for (t, k, _), o in zip(truth_presets, outs, strict=True)}

    # 3. One render host: engine path vs truth states.
    dry = read_wav_f32(DI_REAL)
    x = np.concatenate([dry, np.zeros(int(2.5 * SR), np.float32)])
    rows = []
    print(f"native_host processes before the render host: {native_hosts()}")
    eng = nat.NativeEngine(templates_dir=lib.library_templates(), autocapture=False, on_demand=False)
    try:
        for tag, _mid, prm, v, kind in CASES:
            p = presets[tag]
            eng.fallback_dirs = dirs_for(kind)
            eng.current_chain = eng.current_template = None
            eng.load(p)
            got = nat.template_stem(eng.current_template)
            y = eng.render(x)
            lat, pre = eng.latency(), eng.preroll
            ys = {}
            for k in ("snapped", "exact"):
                s = truths[(tag, k)].state
                eng.host.call("set_state", s)
                eng._loaded_state, eng._host_params = s, {}
                padded = np.concatenate([np.zeros(pre, np.float32), x, np.zeros(lat, np.float32)])
                ys[k] = eng._raw_render(padded, reset=True)[pre + lat:pre + lat + len(x)]
            eng.current_template = None
            half = 2 * SR
            row = {"tag": tag, "model": p.slots[0].model_id, "param": prm, "requested": v,
                   "played": plan[tag]["snapped"].get(f"0:{prm}", v),
                   "template_range": plan[tag]["ranges"].get(f"0:{prm}"),
                   "template": got, "predicted": plan[tag]["template"], "dirs": plan[tag]["dirs"],
                   "null_vs_snapped_truth_db": round(nat.null_db(y, ys["snapped"]), 1),
                   "null_vs_snapped_truth_after_2s_db": round(nat.null_db(y[half:], ys["snapped"][half:]), 1),
                   "null_vs_exact_truth_db": round(nat.null_db(y, ys["exact"]), 1),
                   "rms_db": round(nat._rms_db(y), 1)}
            row["pass"] = bool(row["template"] == row["predicted"] and row["null_vs_snapped_truth_db"] <= THRESHOLD_DB)
            rows.append(row)
            print(f"{tag:9s} {prm:6s} req {v:<6} plays {row['played']:.6g} on {row['template_range']} "
                  f"({got}{'' if got == row['predicted'] else ' != predicted ' + row['predicted']}): "
                  f"null vs snapped truth {row['null_vs_snapped_truth_db']:7.1f} dB "
                  f"(after 2 s {row['null_vs_snapped_truth_after_2s_db']:.1f}), vs exact {row['null_vs_exact_truth_db']:.1f} dB")
        default_pick = lib.match(presets["time"])
    finally:
        eng.close()
    res = {"rows": rows, "on_demand": {"template": nat.template_stem(od_tpl), "captured_now": bool(calls),
                                       "capture_s": round(od_s, 3), "reused_on_second_request":
                                       nat.template_stem(od_again) == nat.template_stem(od_tpl),
                                       "index_entry": od_entry},
           "default_pick_time_case": nat.template_stem(default_pick) if default_pick else None,
           "truth_capture_s": round(cap_s, 2), "wall_s": round(time.perf_counter() - t0, 1),
           "native_hosts_after": native_hosts(), "stats": st}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    ok = all(r["pass"] for r in rows) and res["on_demand"]["reused_on_second_request"]
    print(f"{'PASS' if ok else 'FAIL'}: {sum(r['pass'] for r in rows)}/{len(rows)} within {THRESHOLD_DB} dB; "
          f"wall {res['wall_s']} s; native_host after: {res['native_hosts_after']}")
    if a.ledger:
        for r in rows:
            ledger(f"null_vs_drop_db_{r['tag']}", r["null_vs_snapped_truth_db"],
                   f"T098 E3; {r['model']} {r['param']} requested {r['requested']} played {r['played']:.6g} on "
                   f"{r['template_range']} template {r['template']} (fallback dirs {','.join(Path(d).name if 'on_demand' not in d else 'on_demand' for d in r['dirs'])}); "
                   f"truth = drop capture with grid values; vs exact-value drop {r['null_vs_exact_truth_db']} dB; "
                   f"real DI di_2s_real + 2.5 s silence; 1 Native host; pass {r['pass']}")
        ledger("on_demand_capture_s", res["on_demand"]["capture_s"],
               f"T098 E3; on-demand full-range variant {res['on_demand']['template']} for Simple Delay Time 2.0 s "
               f"(captured now {res['on_demand']['captured_now']}, second request reused "
               f"{res['on_demand']['reused_on_second_request']}); recorded in block-library-v1.2 index on_demand")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
