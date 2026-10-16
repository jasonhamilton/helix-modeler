#!/usr/bin/env python3
"""Block bypass over MIDI CC in Helix Native, under the host's state reset (bypass investigation).

Builds variants of one chain (default: the A30 amp+cab -> DL4 PingPong drop file) and
drop-captures each one (GUI session, ~25 s each, cached):
  on        every block enabled
  off_dly   delay disabled          off_amp   amp disabled
  cc        every block enabled, delay ``@enabled`` and amp ``@enabled`` assigned to MIDI CC
            (``controller`` section, ``@controller`` 8) in the .hlx
  cc_off    the same assignments with the delay disabled in the file
Then one host renders the DI through the cc captures with the VST3 ``cc_<n>`` params driven to
0 / 1 / 0.49 / 0.51 in several orders, and nulls every render against the matching truth
capture (on / off_dly / off_amp). Also tries switch_01..16 on the cc capture.

  .venv/bin/python scripts/native_bypass_probe.py [--ledger]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from tonematch.engines.base import SAMPLE_RATE
from tonematch.engines.native import (
    NativeEngine,
    capture_chain,
    locate_plugin,
    null_db,
    start_state_path,
)
from tonematch.engines.native_setlist import free_ccs
from tonematch.preset import read_hlx

OUT = ROOT / "cache" / "native" / "bypass_probe"


def set_enabled(p, key: str, on: bool) -> None:
    for s in p.slots:
        if s.key == key:
            s.enabled = on
    for sn in p.snapshots:
        if key in sn.enabled:
            sn.enabled[key] = on
    for i in range(3):
        b = p.raw.get("data", {}).get("tone", {}).get(f"snapshot{i}", {}).get("blocks", {}).get("dsp0", {})
        if key in b:
            b[key] = on


def assign_bypass(p, key: str, cc: int, extra: dict | None = None) -> None:
    ctl = p.raw["data"]["tone"].setdefault("controller", {}).setdefault("dsp0", {}).setdefault(key, {})
    ctl["@enabled"] = {"@controller": 8, "@cc": cc, "@min": 0.0, "@max": 1.0, "@snapshot_disable": False,
                       **(extra or {})}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hlx", type=Path, default=ROOT / "cache/native/drop/4d54671b96a6_a079bb4051.hlx")
    ap.add_argument("--di", type=Path, default=ROOT / "cache/parity/di_10s_tail.wav")
    ap.add_argument("--clip-s", type=float, default=6.0)
    ap.add_argument("--ledger", action="store_true")
    a = ap.parse_args()
    import soundfile as sf

    from tonematch.catalog import load_catalog

    base = read_hlx(a.hlx)
    amp = next(s.key for s in base.slots if s.category == "amp")
    dly = next(s.key for s in base.slots if s.category == "delay")
    # Ignore bypass assignments already in the file: a capture writes its drop file under the
    # block-value hash, so the cc_off capture overwrites the default --hlx with a copy that
    # carries this probe's own @enabled assignments; counting them would move the CCs off the
    # cached captures' numbers on a re-run.
    used = {e.get("@cc") for blk in base.raw["data"]["tone"].get("controller", {}).get("dsp0", {}).values()
            for k, e in blk.items() if k != "@enabled"}
    free = [c for c in free_ccs("stomp") if c not in used]
    cc_dly, cc_amp = free[-1], free[-2]
    variants = {}
    for name in ("on", "off_dly", "off_amp", "cc", "cc_off"):
        p = copy.deepcopy(base)
        p.name = f"Bypass {name}"
        set_enabled(p, amp, name != "off_amp")
        set_enabled(p, dly, name not in ("off_dly", "cc_off"))
        if name.startswith("cc"):
            assign_bypass(p, dly, cc_dly)
            assign_bypass(p, amp, cc_amp)
        variants[name] = p
    plugin = str(locate_plugin() or "")
    start = start_state_path().read_bytes() if start_state_path().is_file() else None
    cat = load_catalog()
    tpls = {}
    for name, p in variants.items():
        d = OUT / name
        from tonematch.engines.native import ChainTemplate

        have = [t for ts in ChainTemplate.load_variants(d).values() for t in ts]
        tpls[name] = have[0] if have else capture_chain(p, plugin, start_state=start, templates_dir=d,
                                                        verify_ccs=False, catalog=cat)
    x, _ = sf.read(a.di, dtype="float32")
    x = x[: int(a.clip_s * SAMPLE_RATE)]
    e = NativeEngine(preroll=3 * SAMPLE_RATE, autocapture=False)
    res: dict = {"cc_delay": cc_dly, "cc_amp": cc_amp}
    try:
        def render(t, params=None):
            e._set_state(t.state)
            e.current_chain, e.current_template = t.chain, t
            if params:
                e.host.call("set_params", values=params)
            return e.render(x)

        truth = {k: render(tpls[k]) for k in ("on", "off_dly", "off_amp")}
        res["truth_on_vs_off_dly_db"] = round(null_db(truth["on"], truth["off_dly"]), 1)
        res["truth_on_vs_off_amp_db"] = round(null_db(truth["on"], truth["off_amp"]), 1)
        res["cc_capture_untouched_vs_on_db"] = round(null_db(truth["on"], render(tpls["cc"])), 1)
        res["cc_off_capture_untouched_vs_off_dly_db"] = round(null_db(truth["off_dly"], render(tpls["cc_off"])), 1)
        kd, ka = f"cc_{cc_dly}", f"cc_{cc_amp}"
        seq = []
        for cap in ("cc", "cc_off"):
            for vd, va in ((0.0, 1.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0), (0.49, 1.0), (0.51, 1.0),
                           (0.0, 0.0)):
                y = render(tpls[cap], {kd: vd, ka: va})
                want = "on" if vd >= 0.5 and va >= 0.5 else "off_dly" if va >= 0.5 else "off_amp" if vd >= 0.5 else None
                row = {"capture": cap, "delay_cc": vd, "amp_cc": va, "expect": want,
                       **{f"vs_{k}": round(null_db(v, y), 1) for k, v in truth.items()}}
                seq.append(row)
                print(row, flush=True)
        res["sequence"] = seq
        # 16 automation switches on the cc capture (none assigned in the file)
        sw = {}
        for i in range(1, 17):
            n = f"switch_{i:02d}"
            y0 = render(tpls["cc"], {n: 0.0, kd: 1.0, ka: 1.0})
            y1 = render(tpls["cc"], {n: 1.0, kd: 1.0, ka: 1.0})
            sw[n] = round(null_db(y0, y1), 1)
        res["switch_moves_render_db"] = sw
    finally:
        e.close()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "sequence"}, indent=1))
    if a.ledger:
        ok = [r for r in res["sequence"] if r["expect"]]
        worst = max(r[f"vs_{r['expect']}"] for r in ok)
        subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "parity", "--engine",
                        "native", "--dataset", "smoke", "--metric", "bypass_cc_vs_drop_truth_db", f"--value={worst}",
                        "--renders", str(len(res["sequence"]) + 5), "--notes",
                        ("bypass investigation scripts/native_bypass_probe.py: .hlx controller entry "
                         "@enabled -> MIDI CC (@controller 8 @min 0 @max 1) on delay and amp; driven via VST3 "
                         f"cc_<n> under the state reset; worst null vs the matching drop-captured truth over "
                         f"{len(ok)} renders (orders 0/1/0; 0.49/0.51; both off); details "
                         "cache/native/bypass_probe/result.json")],
                       check=True, env={**os.environ, "TONEMATCH_AGENT": "native-engine"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
