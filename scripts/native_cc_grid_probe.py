#!/usr/bin/env python3
"""Which value does Native give a parameter when its MIDI CC is at c (of 127)? (phase 3)

For each probed (model, param) a single-block chain at catalog defaults is captured with CCs
assigned; then the param's CC is set to c/127 and the render is nulled against drop captures
of the same block with the param written as each candidate value:

  lin    lo + c/127 (hi - lo)
  log    lo (hi/lo)^(c/127)            (lo > 0 only)
  lin128 lo + c/128 (hi - lo)

The best candidate per (param, c) and its null are printed and written to
``cache/native/cc_grid_probe/result.json``. Synthetic test signal only. One editor session.

  TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
    .venv/bin/python scripts/native_cc_grid_probe.py
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


from native_capture_bench import K_HLX, single_block

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "cc_grid_probe"
PROBES = [  # (model, param, overrides for the base so the param is audible)
    ("HD2_CompressorRedSqueeze", "Level", {}),
    ("HD2_Cab1x12BlueBell", "LowCut", {}),
    ("HD2_Cab1x12BlueBell", "HighCut", {}),
    ("HD2_Cab1x12BlueBell", "Distance", {}),
    ("HD2_Cab1x12BlueBell", "Level", {}),
    ("HD2_EQParametric", "LowFreq", {"LowGain": 9.0}),
    ("HD2_EQParametric", "LowQ", {"LowGain": 9.0}),
    ("HD2_EQParametric", "MidGain", {}),
    ("HD2_EQParametric", "HighCut", {}),
    ("HD2_ChorusPlastiChorus", "Headroom", {}),
    ("HD2_DelaySimpleDelay", "Time", {}),
    ("HD2_ReverbGanymede", "Predelay", {}),
]
CS = (32, 95)


def candidates(lo: float, hi: float, c: int) -> dict[str, float]:
    t = c / 127
    out = {"lin": lo + t * (hi - lo), "lin128": lo + c / 128 * (hi - lo)}
    if lo > 0:
        out["log"] = lo * (hi / lo) ** t
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of Model:Param to probe")
    a = ap.parse_args()
    catalog = load_catalog()
    plugin, start = str(nat.locate_plugin()), nat.start_state_path().read_bytes()
    base_k = read_hlx(K_HLX)
    probes = [p for p in PROBES if not a.only or f"{p[0]}:{p[1]}" in a.only.split(",")]
    presets, meta = [], []
    for mid, prm, over in probes:
        base = single_block(mid, catalog, base_k)
        base.slots[0].params.update(over)
        base.name = f"GRID {mid[4:][:10]} {prm}"
        presets.append(base)
        meta.append(("base", mid, prm, None, None, None))
        lo, hi = catalog["models"][mid]["params"][prm]["min"], catalog["models"][mid]["params"][prm]["max"]
        for c in CS:
            for name, v in candidates(float(lo), float(hi), c).items():
                q = copy.deepcopy(base)
                q.slots[0].params[prm] = v
                q.name = f"GRID {mid[4:][:10]} {prm} {name} {c}"
                presets.append(q)
                meta.append(("cand", mid, prm, c, name, v))
    tdir = OUT / "templates"
    st: dict = {}
    tpls = nat.capture_chains(presets, plugin, start_state=start, templates_dir=tdir, catalog=catalog,
                              verify_ccs=False, known_live=False, replay_every=0, stats=st)
    bad = [str(t) for t in tpls if isinstance(t, Exception)]
    if bad:
        print("capture failures:", bad[:3])
        return 1
    host = nat.HostProcess(plugin, None, initial_state=start)
    rows = []
    try:
        pre = 3.0
        for (kind, mid, prm, c, name, v), tpl in zip(meta, tpls, strict=True):
            if kind == "base":
                ref = f"0:{prm}"
                ys = {cc: nat._render_state(host, tpl.state, {tpl.map[ref]: cc / 127}, pre, 2.0) for cc in CS}
                y0 = nat._render_state(host, tpl.state, {}, pre, 2.0)
                continue
            yt = nat._render_state(host, tpl.state, {}, pre, 2.0)
            rows.append({"model": mid, "param": prm, "c": c, "cand": name, "value": round(v, 4),
                         "null_db": round(nat.null_db(yt, ys[c]), 1),
                         "moved_db": round(nat.null_db(yt, y0), 1)})
    finally:
        host.close()
    best = {}
    for r in rows:
        k = (r["model"], r["param"], r["c"])
        if k not in best or r["null_db"] < best[k]["null_db"]:
            best[k] = r
    for r in rows:
        mark = " <" if best[(r["model"], r["param"], r["c"])] is r else ""
        print(f'{r["model"][4:]:24s} {r["param"]:9s} c={r["c"]:3d} {r["cand"]:6s} v={r["value"]:<10} '
              f'null {r["null_db"]:7.1f}  (vs base {r["moved_db"]:6.1f}){mark}')
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps({"rows": rows, "stats": st}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
