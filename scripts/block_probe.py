#!/usr/bin/env python3
"""block-probe, offline part (owner: tone-metrics, T046). Skill: .claude/skills/block-probe/SKILL.md.

Characterise ONE block by a controlled input and its output. This script never renders: an engine
(T047, native-engine) renders `probe-v1.wav` through the block and hands the wet file back here.

    python scripts/block_probe.py make [--out cache/probe] [--no-di]
        write the probe set (<version>.wav, float32 48 kHz) and its layout (<version>.json)
    python scripts/block_probe.py analyse WET.wav [--layout cache/probe/probe-v1.json]
                                  [--latency N] [--channel mid|left|right]
                                  [--subject key=value ...] [--out FP.json]
        fingerprint a wet render (default output: beside WET, .fingerprint.json)
    python scripts/block_probe.py compare A.json B.json [--all] [--json]
        per-feature diff (exit 1 if any feature differs beyond tolerance)
    python scripts/block_probe.py validate [--ledger] [--json OUT]
        run the probe through KNOWN numpy/scipy blocks and print the accuracy table
        (exit 1 if any check fails)

Probe audio, wet renders and fingerprints of real blocks stay under cache/ (gitignored). The DI
excerpts are read from the local library in data/di and never copied into the repo. No Line 6
content is read or written here.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.probe import analysis, fingerprint, signals, synthetic

DEFAULT_OUT = ROOT / "cache" / "probe"


def cmd_make(a: argparse.Namespace) -> int:
    probe = signals.build_probe_set(di=False if a.no_di else "auto", di_root=a.di_root)
    wav, js = signals.write_probe_set(probe, a.out)
    print(f"{probe.version}: {probe.duration_s:.2f} s, {len(probe.segments)} segments, "
          f"{len(probe.di)} DI excerpts")
    print(f"wrote {wav}\nwrote {js}")
    if not probe.di and not a.no_di:
        print("NOTE: DI library not found under data/di: built the -nodi variant "
              "(see data/di/README.md)")
    return 0


def _load_probe(layout: str | None, di_root: str | None) -> signals.ProbeSet:
    if layout:
        return signals.probe_from_layout(layout, di_root=di_root)
    for name in (signals.PROBE_VERSION, signals.PROBE_VERSION + "-nodi"):
        p = DEFAULT_OUT / f"{name}.json"
        if p.exists():
            return signals.probe_from_layout(p, di_root=di_root)
    raise SystemExit(f"no layout given and none in {DEFAULT_OUT}; run `make` first")


def cmd_analyse(a: argparse.Namespace) -> int:
    import soundfile as sf

    probe = _load_probe(a.layout, a.di_root)
    wet, sr = sf.read(a.wet, dtype="float64", always_2d=True)
    if sr != probe.sample_rate:
        print(f"{a.wet}: {sr} Hz, the probe is {probe.sample_rate} Hz", file=sys.stderr)
        return 2
    subject = dict(kv.split("=", 1) for kv in a.subject)
    subject.setdefault("wet_file", Path(a.wet).name)
    t0 = time.time()
    fp = analysis.analyse(wet if wet.shape[1] > 1 else wet[:, 0], probe, latency=a.latency,
                          subject=subject, channel=a.channel)
    out = Path(a.out) if a.out else Path(a.wet).with_suffix(".fingerprint.json")
    fingerprint.save(fp, out)
    print(f"latency {fp['alignment']['latency_samples']} samples ({fp['alignment']['source']}); "
          f"analysed in {time.time() - t0:.1f} s")
    for n in fp["notes"]:
        print("NOTE:", n)
    print(f"wrote {out}")
    return 0


def cmd_compare(a: argparse.Namespace) -> int:
    res = fingerprint.compare(fingerprint.load(a.a), fingerprint.load(a.b))
    if a.json:
        print(json.dumps(res, indent=1, default=float))
    else:
        print(fingerprint.format_compare(res, all_rows=a.all))
    return 1 if res["n_differs"] else 0


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return f"{v:.4g}"
    return str(v)


def cmd_validate(a: argparse.Namespace) -> int:
    t0 = time.time()
    probe = signals.build_probe_set(di=False)
    rows, _ = synthetic.validate(probe)
    wall = time.time() - t0
    print(f"probe {probe.version}: {probe.duration_s:.2f} s (synthetic part); "
          f"with the 3 DI excerpts {signals.build_probe_set(di='auto').duration_s:.2f} s")
    print(f"{'block':28} {'feature':32} {'true':>9} {'measured':>10} {'error':>10} "
          f"{'tol':>8}  ok")
    for r in rows:
        tol = "-" if r["tol"] is None else f"{r['tol']:g}{' rel' if r['rel'] else ''}"
        err = r["error"]
        err_s = "-" if err is None else (f"{100 * err:+.2f}%" if r["rel"] else f"{err:+.4g}")
        print(f"{r['block']:28} {r['feature']:32} {_fmt(r['true']):>9} {_fmt(r['measured']):>10} "
              f"{err_s:>10} {tol:>8}  {'PASS' if r['pass'] else 'FAIL'}")
    n_pass = sum(r["pass"] for r in rows)
    rate = n_pass / len(rows)
    print(f"{n_pass}/{len(rows)} checks pass ({len({r['block'] for r in rows})} blocks), "
          f"{wall:.1f} s")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(rows, indent=1, default=float) + "\n")
    if a.ledger:
        env = dict(os.environ)
        env.setdefault("TONEMATCH_AGENT", "tone-metrics")
        notes = (f"block-probe analysis vs KNOWN numpy/scipy blocks (NOT Helix): "
                 f"{n_pass}/{len(rows)} checks within tolerance; {probe.version} "
                 f"{probe.duration_s:.2f} s synthetic part; tests/test_probe.py")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"),
                        "--kind", "smoke", "--engine", "-", "--dataset", "probe-synth-v1",
                        "--metric", "probe_checks_pass_rate", "--value", f"{rate:.4f}",
                        "--renders", str(len({r['block'] for r in rows})),
                        "--wall-clock-s", f"{wall:.1f}", "--notes", notes],
                       check=True, env=env)
    return 0 if n_pass == len(rows) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--out", default=str(DEFAULT_OUT))
    m.add_argument("--no-di", action="store_true")
    m.add_argument("--di-root", default=None)
    m.set_defaults(func=cmd_make)
    an = sub.add_parser("analyse")
    an.add_argument("wet")
    an.add_argument("--layout", default=None)
    an.add_argument("--latency", type=int, default=None)
    an.add_argument("--channel", choices=("mid", "left", "right"), default="mid")
    an.add_argument("--subject", nargs="*", default=[], help="key=value pairs")
    an.add_argument("--out", default=None)
    an.add_argument("--di-root", default=None)
    an.set_defaults(func=cmd_analyse)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--all", action="store_true", help="also list features within tolerance")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compare)
    v = sub.add_parser("validate")
    v.add_argument("--ledger", action="store_true")
    v.add_argument("--json", default=None)
    v.set_defaults(func=cmd_validate)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
