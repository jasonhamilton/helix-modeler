#!/usr/bin/env python3
"""Guided per-guitar calibration session through the HX Stomp's dry input (T204).

Walks you through short prompted segments (open strings, a run up each string, chords, soft/medium/hard
picks, palm mutes, optional bends/slides/harmonics, 30 s of free playing) and saves each as its own wav under
data/guitars/<guitar-id>/session-<stamp>/ with a session.json. About 10 minutes. Local only (gitignored).

Audio path: the same as scripts/playalong_record.py (tonematch.reamp.duplex, 48 kHz): silence goes out, host
input 5 = the Stomp's dry Main L (the guitar) is recorded. Audio only; the control channel is never touched.

    python scripts/guitar_profile_record.py --guitar-id strat-main --check    # 3 s level meter first
    python scripts/guitar_profile_record.py --guitar-id strat-main            # the whole session
    python scripts/guitar_profile_record.py --guitar-id strat-main --segments a,b      # only some groups
    python scripts/guitar_profile_record.py --list                            # show the script, record nothing

Segments: a open strings, b runs, c chords, d soft/medium/hard, e palm mutes, f optional extras, g free playing.
Before running: guitar into the Stomp's main input; the preset's Input block must be the guitar input
(Main L/R or Multi), not USB 5/6. Leave the pedal on its original preset afterwards.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import cli_result, guitar_profile as G, reamp  # noqa: E402

DRY_IN = 5          # host input 5 = dry Main L
PLAY_CH = (1, 2)    # host outputs (silent)


def peak_db(x: np.ndarray) -> float:
    p = float(np.max(np.abs(x))) if x.size else 0.0
    return 20 * np.log10(p) if p > 0 else -np.inf


def warnings_for(x: np.ndarray) -> list[str]:
    pk = peak_db(x)
    w = []
    if pk < -45:
        w.append(f"nearly silent (peak {pk:.1f} dBFS): is the guitar plugged into the Stomp's main input?")
    elif pk < -30:
        w.append(f"very quiet (peak {pk:.1f} dBFS): turn the guitar up")
    if pk > -0.5:
        w.append(f"CLIPPED (peak {pk:.1f} dBFS): turn the guitar volume down")
    elif pk > -3:
        w.append(f"close to clipping (peak {pk:.1f} dBFS)")
    return w


def record(seconds: float, dev: int) -> np.ndarray:
    rec = reamp.duplex(np.zeros((int(seconds * reamp.SR), len(PLAY_CH)), np.float32), PLAY_CH, dev)
    return rec[:, DRY_IN - 1].copy()


def countdown(s: int) -> None:
    for k in range(s, 0, -1):
        print(f"   {k} ...", flush=True)
        time.sleep(1)


def meter(dev: int, seconds: float = 3.0) -> dict:
    print(f"Level check: play normally and then your hardest for {seconds:.0f} s ...", flush=True)
    x = record(seconds, dev)
    w = warnings_for(x)
    bar = int(max(0, min(40, (peak_db(x) + 60) * 40 / 60)))
    print(f"   peak {peak_db(x):6.1f} dBFS  |{'#' * bar}{'.' * (40 - bar)}|")
    for t in w:
        print("   WARNING:", t)
    if not w:
        print("   level looks good")
    return {"peak_dbfs": round(peak_db(x), 2), "warnings": w}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--guitar-id", default=None, help="name of the guitar profile, e.g. strat-main")
    ap.add_argument("--segments", default=None, help="groups a-g and/or ids (default: a-e and g)")
    ap.add_argument("--with-optional", action="store_true", help="include group f (bends, slides, harmonics)")
    ap.add_argument("--check", action="store_true", help="3 s level meter with clipping warnings, then exit")
    ap.add_argument("--list", action="store_true", help="print the session script and exit; records nothing")
    ap.add_argument("--countdown", type=int, default=3, help="seconds of countdown before each segment")
    ap.add_argument("--no-pause", action="store_true", help="do not wait for Enter between segments")
    ap.add_argument("--out", type=Path, default=G.GUITARS)
    cli_result.add_result_arg(ap)
    a = ap.parse_args(argv)
    return cli_result.run(a, tool="guitar_profile_record", body=lambda res: _run(a, res), inputs={})


def _run(a, res) -> int:
    segs = G.select_segments(a.segments, a.with_optional)
    if a.list:
        tot = 0.0
        for s in segs:
            print(f"[{s['group']}] {s['id']:<12} {s['seconds']:>3} s  {s['title']}\n      {s['text']}")
            tot += s["seconds"]
        print(f"\n{len(segs)} segments, {tot:.0f} s of playing (about {tot / 60 + len(segs) * 0.2:.0f} min with the pauses)")
        return 0
    dev = reamp.find_device()
    if a.check:
        res.metrics.update({"mode": "check", **meter(dev)})
        return 0
    if not a.guitar_id:
        raise SystemExit("--guitar-id is required (it names the profile)")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out / a.guitar_id / f"session-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Guitar profile '{a.guitar_id}': {len(segs)} segments -> {out}\nGuitar into the Stomp's main input. Ctrl-C stops (finished segments stay saved).\n")
    meter(dev)
    if not a.no_pause:
        input("\nPress Enter to begin ... ")
    done = []
    for i, s in enumerate(segs, 1):
        while True:
            print(f"\n[{i}/{len(segs)}] {s['title']}  ({s['seconds']} s)\n   {s['text']}")
            if not a.no_pause:
                input("   Press Enter when ready ... ")
            countdown(a.countdown)
            print("   RECORDING - play now", flush=True)
            x = record(s["seconds"], dev)
            print("   done.")
            w = warnings_for(x)
            for t in w:
                print("   WARNING:", t)
            if w and not a.no_pause and input("   Enter to keep, r to redo: ").strip().lower() == "r":
                continue
            break
        sf.write(out / f"{s['id']}.wav", x, reamp.SR, subtype="FLOAT")
        done.append({"id": s["id"], "group": s["group"], "file": f"{s['id']}.wav", "seconds": s["seconds"],
                     "peak_dbfs": round(peak_db(x), 2), "warnings": w, "meta": s["meta"]})
        (out / "session.json").write_text(json.dumps(
            {"guitar_id": a.guitar_id, "stamp": stamp, "device": "HX Stomp", "dry_input": "USB in 5 (Main L)",
             "sample_rate": reamp.SR, "segments": done}, indent=1))
    print(f"\nSession saved: {out}\nNext: python scripts/guitar_profile_build.py --guitar-id {a.guitar_id}")
    res.outputs.update({"session_dir": out, "session_json": out / "session.json"})
    res.metrics.update({"mode": "record", "segments": len(done), "peaks_dbfs": [d["peak_dbfs"] for d in done]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
