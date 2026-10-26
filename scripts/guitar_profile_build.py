#!/usr/bin/env python3
"""Build a guitar profile (T204) from a recorded session, or from stand-in wavs.

Writes data/guitars/<id>/profile.json (level, tone EQ, coverage) and bank.json (labelled own notes in the T201
BankNote format) and prints the summary. Local only. One CPU core; no Native, no Stomp.

    python scripts/guitar_profile_build.py --guitar-id strat-main                 # latest session
    python scripts/guitar_profile_build.py --guitar-id strat-main --session session-20261023-101500
    python scripts/guitar_profile_build.py --guitar-id human-standin --free-wav a_dry.wav b_dry.wav   # free playing only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import cli_result, guitar_profile as G  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--guitar-id", required=True)
    ap.add_argument("--session", default=None, help="session folder name (default: the latest)")
    ap.add_argument("--free-wav", nargs="+", type=Path, default=None,
                    help="stand-in: existing dry wavs used as free playing (level and tone only)")
    ap.add_argument("--bank-root", type=Path, default=ROOT / "data" / "di" / "Guitar-TECHS")
    ap.add_argument("--no-basic-pitch", action="store_true")
    ap.add_argument("--out", type=Path, default=G.GUITARS)
    cli_result.add_result_arg(ap)
    a = ap.parse_args(argv)
    return cli_result.run(a, tool="guitar_profile_build", body=lambda res: _run(a, res), inputs={})


def _run(a, res) -> int:
    root = a.out / a.guitar_id
    if a.free_wav:
        # one concatenated free segment so the level is measured over everything the human played
        xs = [G.read_mono(p) for p in a.free_wav]
        root.mkdir(parents=True, exist_ok=True)
        cat = np.concatenate([np.concatenate([x, np.zeros(G.SR, np.float32)]) for x in xs])
        seg_path = root / "standin_free.wav"
        sf.write(seg_path, cat, G.SR, subtype="FLOAT")
        segments, session = {"free": seg_path}, "standin:" + ",".join(p.name for p in a.free_wav)
    else:
        sdirs = sorted(root.glob("session-*"))
        if a.session:
            sdirs = [root / a.session]
        if not sdirs:
            raise SystemExit(f"no session under {root}; run guitar_profile_record.py first")
        sd = sdirs[-1]
        sj = json.loads((sd / "session.json").read_text())
        segments = {s["id"]: sd / s["file"] for s in sj["segments"]}
        session = sd.name
    prof = G.build_profile(a.guitar_id, segments, bank_root=a.bank_root, use_basic_pitch=not a.no_basic_pitch, session=session)
    prof.save(root)
    d = prof.data
    print(json.dumps({"level": d["level"], "tone": {k: v for k, v in d["tone"].items() if k != "method"},
                      "coverage": d["coverage"]}, indent=1))
    print(f"\nSaved {root / 'profile.json'} and bank.json ({len(prof.bank)} own notes)")
    res.outputs.update({"profile": root / "profile.json", "bank": root / "bank.json"})
    res.metrics.update({"normal_loud_dbfs": d["level"]["normal_loud_dbfs"], "own_notes": len(prof.bank),
                        "eq_gains_db": d["tone"]["gains_db"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
