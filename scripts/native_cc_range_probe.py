#!/usr/bin/env python3
"""Does Native honour a MIDI-CC assignment's own @min/@max? (T072, lead ruling on T066b)

Each probe captures one single-block chain whose param CC is assigned over the NARROWED range
(``native.cc_ranges``, rules ``native.narrow_range``) instead of the catalog range. Then, for
each CC value c, the template driven to c/127 is nulled against drop captures of the same block
with the param written as

  narrow  lo + c/127 (hi - lo)        over the assignment's @min..@max
  full    LO + c/127 (HI - LO)        over the catalog range (what Native would give if it
                                      ignored the assignment's @min/@max)

One probe moves @min off the catalog minimum too, so both ends are tested. The narrowed
template is also replayed in a fresh host (the range must live in the captured state), and
for Simple Delay the first echo of an impulse is timed against ``lo + c/127 (hi - lo)``.

Synthetic signals only, at most 2 Native hosts (one capture host, then one render host at a
time). Writes ``cache/native/cc_range_probe/result.json``.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_cc_range_probe.py
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.engines.native_library import base_preset, single_block

OUT = ROOT / "cache" / "native" / "cc_range_probe"
SR = nat.SAMPLE_RATE
# (tag, model, param, CC values, catalog min override or None, base param overrides)
PROBES = [
    ("simple", "HD2_DelaySimpleDelay", "Time", (19, 38, 47, 60, 100, 127), None, {}),
    ("simple_lo", "HD2_DelaySimpleDelay", "Time", (0, 13, 64), 0.25, {"Feedback": 0.36}),
    ("vintage_time", "HD2_DelayVintageDigital", "Time", (36, 90), None, {}),
    ("vintage_rate", "HD2_DelayVintageDigital", "Rate", (10, 64), None, {"Feedback": 0.36}),
    ("phaser", "HD2_PhaserDeluxePhaser", "Rate", (10, 64), None, {}),
    ("mm4", "HD2_MM4AnalogChorus", "Speed", (40, 100), None, {}),
    ("bloom", "VIC_ReverbDynBloom", "Decay", (18, 58), None, {}),
]
PRE_S, SECONDS = 3.0, 2.0


def echo_lag_ms(y: np.ndarray, start: int) -> float:
    """Lag of the first echo after the dry impulse at ``start``, in ms (peak picking)."""
    a = np.abs(y[start:])
    d = int(np.argmax(a[: SR // 100]))  # dry peak within 10 ms
    rest = a[d + SR // 200:]            # skip 5 ms
    e = int(np.argmax(rest)) + d + SR // 200
    return (e - d) / SR * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of probe tags")
    a = ap.parse_args()
    t0 = time.perf_counter()
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    base_k = base_preset()
    probes = [p for p in PROBES if not a.only or p[0] in a.only.split(",")]
    # The probes that move @min need their own catalog view (the rule starts at the catalog min).
    presets, meta, cats = [], [], []
    for tag, mid, prm, cs, lo_over, over in probes:
        cat = catalog
        if lo_over is not None:
            cat = copy.deepcopy(catalog)
            cat["models"][mid]["params"][prm]["min"] = lo_over
        p = single_block(mid, cat, base_k)
        p.slots[0].params.update(over)
        p.name = f"RNG {tag}"
        rng = nat.cc_ranges(p, cat)[f"0:{prm}"]
        full = (float(catalog["models"][mid]["params"][prm]["min"]), float(catalog["models"][mid]["params"][prm]["max"]))
        presets.append(p)
        cats.append(cat)
        meta.append({"kind": "base", "tag": tag, "model": mid, "param": prm, "narrow": list(rng), "full": list(full)})
        for c in cs:
            for hyp, (lo, hi) in (("narrow", rng), ("full", full)):
                q = copy.deepcopy(p)
                q.slots[0].params[prm] = nat.cc_value(lo, hi, c)
                q.name = f"RNG {tag} {hyp} {c}"
                presets.append(q)
                cats.append(cat)
                meta.append({"kind": "truth", "tag": tag, "model": mid, "param": prm, "c": c, "hyp": hyp,
                             "value": q.slots[0].params[prm]})
    tdir = OUT / "templates"
    st: dict = {}
    tpls = []
    # One capture call per catalog view (the overridden-min probe needs its own), one host each in turn.
    groups: dict[int, list[int]] = {}
    for i, c in enumerate(cats):
        groups.setdefault(id(c), []).append(i)
    tpls = [None] * len(presets)
    for idx in groups.values():
        out = nat.capture_chains([presets[i] for i in idx], plugin, start_state=start, templates_dir=tdir,
                                 catalog=cats[idx[0]], verify_ccs=False, known_live=False, replay_every=0, stats=st)
        for i, t in zip(idx, out, strict=True):
            tpls[i] = t
    bad = [f"{m['tag']}: {t}" for m, t in zip(meta, tpls, strict=True) if isinstance(t, Exception)]
    if bad:
        print("capture failures:", bad[:4])
        return 1
    t_cap = time.perf_counter() - t0
    rows, assigned = [], []
    host = nat.HostProcess(plugin, None, initial_state=start)

    try:
        for m, tpl in zip(meta, tpls, strict=True):
            if m["kind"] == "base":
                ref = f"0:{m['param']}"
                assigned.append({"tag": m["tag"], "model": m["model"], "param": m["param"],
                                 "template_range": tpl.ranges.get(ref), "catalog_range": m["full"],
                                 "cc": tpl.map.get(ref)})
                ys = {c: nat._render_state(host, tpl.state, {tpl.map[ref]: c / 127}, PRE_S, SECONDS)
                      for c in {mm["c"] for mm in meta if mm.get("tag") == m["tag"] and mm["kind"] == "truth"}}
                continue
            yt = nat._render_state(host, tpl.state, {}, PRE_S, SECONDS)
            rows.append({**m, "value": round(float(m["value"]), 6),
                         "null_db": round(nat.null_db(yt, ys[m["c"]]), 1)})
        # Echo timing on the narrowed Simple Delay template (impulse, 2 s after it).
        echoes = []
        for m, tpl in zip(meta, tpls, strict=True):
            if m["kind"] != "base" or m["model"] != "HD2_DelaySimpleDelay":
                continue
            ref = f"0:{m['param']}"
            lo, hi = tpl.ranges[ref]
            for c in sorted({mm["c"] for mm in meta if mm.get("tag") == m["tag"] and mm["kind"] == "truth"}):
                pre = int(PRE_S * SR)
                x = np.zeros(pre + int(2.0 * SR), np.float32)
                x[pre] = 0.5
                host.call("set_state", tpl.state)
                host.call("set_params", values={tpl.map[ref]: c / 127})
                _, data = host.call("render", x.astype("<f4").tobytes(), reset=True)
                y = np.frombuffer(data, dtype="<f4")
                want = nat.cc_value(lo, hi, c) * 1000.0
                got = echo_lag_ms(y, pre)
                echoes.append({"tag": m["tag"], "c": c, "expected_ms": round(want, 3), "measured_ms": round(got, 3),
                               "diff_ms": round(got - want, 3), "full_range_would_be_ms":
                               round(nat.cc_value(*m["full"], c) * 1000.0, 3)})
    finally:
        host.close()
    # Fresh host: the narrowed range must come back from the state alone.
    replays = []
    host = nat.HostProcess(plugin, None, initial_state=start)
    try:
        for m, tpl in zip(meta, tpls, strict=True):
            if m["kind"] != "base":
                continue
            ref = f"0:{m['param']}"
            c = next(mm["c"] for mm in meta if mm.get("tag") == m["tag"] and mm["kind"] == "truth")
            truth = next(t for mm, t in zip(meta, tpls, strict=True) if mm.get("tag") == m["tag"]
                         and mm.get("c") == c and mm.get("hyp") == "narrow")
            ya = nat._render_state(host, tpl.state, {tpl.map[ref]: c / 127}, PRE_S, SECONDS)
            yb = nat._render_state(host, truth.state, {}, PRE_S, SECONDS)
            replays.append({"tag": m["tag"], "c": c, "fresh_host_null_vs_narrow_truth_db": round(nat.null_db(yb, ya), 1)})
    finally:
        host.close()
    for r in rows:
        print(f'{r["tag"]:13s} {r["param"]:6s} c={r["c"]:3d} {r["hyp"]:6s} v={r["value"]:<10} null {r["null_db"]:7.1f}')
    for e in echoes:
        print(f'echo {e["tag"]:10s} c={e["c"]:3d} expected {e["expected_ms"]:8.2f} ms measured {e["measured_ms"]:8.2f} '
              f'(full range would give {e["full_range_would_be_ms"]:.2f})')
    for r in replays:
        print("fresh host", r)
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"assigned": assigned, "rows": rows, "echoes": echoes, "replays": replays,
           "capture_s": round(t_cap, 1), "wall_s": round(time.perf_counter() - t0, 1), "stats": st}
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(assigned, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
