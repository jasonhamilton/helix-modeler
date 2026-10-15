#!/usr/bin/env python3
"""Hands-off Native chain capture by in-process file drop (T018n prototype, macOS).

For each .hlx: optionally write MIDI-CC assignments into it (so the VST3's cc_<n> host params
drive the parameters, many more than the 16 knobs), spawn a fresh Native host, set a start
state already in the file's HC mode, open the editor, drop the file onto it inside the host
process (no Accessibility or other TCC permission; see src/tonematch/engines/native_gui.py),
close the editor, capture the state, verify in another fresh host that it renders differently
from the start state and that a mapped CC moves the render, then save a ChainTemplate.

No human is needed, but a logged-in GUI session is: the editor window shows for ~12 s.
The host interpreter needs pedalboard and PyObjC (pyobjc-framework-Cocoa):

  TONEMATCH_NATIVE_PYTHON=/path/to/python-with-pedalboard-and-pyobjc \\
    .venv/bin/python scripts/native_drop_load.py fixtures/smoke/known.hlx --cc --start-state S

Generated files and states stay under cache/native/ (gitignored). Synthetic test signal only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.engines import native_state as ns
from tonematch.engines.native import (
    ChainTemplate,
    HostProcess,
    cache_dir,
    chain_id,
    chain_key,
    locate_plugin,
    null_db,
    test_signal,
)
from tonematch.engines.native_setlist import assign_ccs
from tonematch.preset import read_hlx

PRE = 4800


def ilcp_mode(raw: bytes) -> int | None:
    pl = ns.chunk_payload(ns.plugin_component(raw)[1], "ILCP")
    return int.from_bytes(pl[8:12], "little") if pl and len(pl) >= 12 else None


def render(host: HostProcess, channels_x: np.ndarray) -> np.ndarray:
    _, data = host.call("render", channels_x.astype("<f4").tobytes(), reset=True)
    return np.frombuffer(data, dtype="<f4")[PRE:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hlx", nargs="+")
    ap.add_argument("--cc", action="store_true", help="assign a free MIDI CC to every [0,1] param")
    ap.add_argument("--start-state", help="raw state (VST3) already in the files' HC mode")
    ap.add_argument("--point", type=float, nargs=2, default=None, help="drop point, XGView coords")
    ap.add_argument("--snap", action="store_true", help="save editor PNGs under cache/native/drop/")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    plugin = locate_plugin()
    if plugin is None or plugin.suffix != ".vst3":
        print("needs the Helix Native VST3 (cc_<n> params are VST3-only)", file=sys.stderr)
        return 2
    out_dir = cache_dir() / "drop"
    out_dir.mkdir(parents=True, exist_ok=True)
    start = Path(a.start_state).read_bytes() if a.start_state else None
    x = np.concatenate([np.zeros(PRE, np.float32), test_signal(2.0)])
    report = []
    for f in a.hlx:
        t0 = time.perf_counter()
        pre = read_hlx(f)
        d = pre.to_hlx_dict()
        amap, rng, skipped = {}, {}, []
        if a.cc:
            targets = [(i, n) for i, s in enumerate(pre.slots) for n in (s.params or {})]
            d, amap, rng, skipped = assign_ccs(d, targets)
        gen = out_dir / f"{chain_id([list(k) for k in chain_key(pre)])}_{Path(f).stem}.hlx"
        gen.write_text(json.dumps(d, indent=1))
        row = {"file": f, "generated": str(gen), "cc_mapped": len(amap), "cc_skipped": skipped}

        host = HostProcess(str(plugin))
        try:
            if start is not None:
                # before any render: HC-mode changes apply only then
                host.call("reload_with_state", start, path=str(plugin))
            snap = str(out_dir / gen.stem) if a.snap else None
            res, _ = host.call("editor_drop", path=str(gen.resolve()), point=a.point, snap_prefix=snap)
            row["drop"] = {k: res.get(k) for k in ("drop", "dialogs", "editor_s", "error")}
            _, state = host.call("get_state")
        finally:
            host.close()

        v = HostProcess(str(plugin))
        try:
            v.call("reload_with_state", state, path=str(plugin))
            y = render(v, x)
            row["repeat_null_db"] = round(null_db(render(v, x), y), 1)
            if amap:
                ref, prm = next(iter(amap.items()))
                v.call("set_params", values={prm: 0.25})
                lo = render(v, x)
                v.call("set_params", values={prm: 0.75})
                row["cc_probe"] = {ref: prm, "moves_db": round(null_db(render(v, x), lo), 1)}
        finally:
            v.close()
        (out_dir / f"{gen.stem}.state").write_bytes(state)  # local; for fresh-process checks
        w = HostProcess(str(plugin))  # start state, or a fresh default instance without one
        try:
            if start is not None:
                w.call("reload_with_state", start, path=str(plugin))
            row["vs_start_null_db"] = round(null_db(y, render(w, x)), 1)
        finally:
            w.close()
        row["hc_mode_word"] = hex(ilcp_mode(state) or 0)
        # loaded if Native asked nothing and either the render moved away from the start state or
        # a CC assignment that only the dropped file carries moves the render
        moved = float(row["vs_start_null_db"]) > -50.0
        cc_live = float(row.get("cc_probe", {}).get("moves_db", -999.0)) > -50.0
        loaded = bool(not row["drop"].get("dialogs") and (moved or cc_live))
        row["loaded"] = loaded
        if loaded and not a.no_save:
            tpl = ChainTemplate(chain_key(pre), state, amap, rng, name=pre.name)
            row["template"] = str(tpl.save())
        row["wall_s"] = round(time.perf_counter() - t0, 1)
        report.append(row)
        print(json.dumps(row))
    (out_dir / f"report_{int(time.time())}.json").write_text(json.dumps(report, indent=1))
    return 0 if all(r["loaded"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
