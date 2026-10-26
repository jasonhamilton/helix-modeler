#!/usr/bin/env python3
"""Play a backing track through the HX Stomp and record the player's dry guitar while they play along (T163).

One full-duplex stream per take (tonematch.reamp.duplex: 48 kHz, the calibrated PortAudio setting):
  host out 1/2  -> the Stomp's main outs / headphones (the backing track)
  host in  5/6  <- the Stomp's dry Main L/R inputs (the guitar DI; Main L = the guitar)
  host in  1/2  <- Path A, the processed guitar (kept as a reference, not used for matching)
The recording is shifted by the calibrated round trip (cache/stomp/latency.json) so sample 0 of each
take lines up with sample 0 of the backing file the player heard. Fine alignment to the stem is done
later, from the audio.

Local only: the song and the takes stay under data/songs/ (gitignored). Never upload or commit them.

    python scripts/playalong_record.py --check            # 3 s silent level meter: play a few notes
    python scripts/playalong_record.py --song-id my-song --takes 3          # three takes, 10 s countdown before each
    python scripts/playalong_record.py --pack data/songs/my-song-playalong --takes 1 --seconds 20 --level-db -12

Before running: the preset's Input block must be the guitar input (Main L/R or Multi), NOT "USB 5/6",
or the Stomp will not hear the guitar. Leave the pedal on its original preset afterwards.
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

from tonematch import cli_result, reamp

SONGS = ROOT / "data" / "songs"
BACKING = "1_backing_no_guitar_with_countin.wav"
FULL_MIX = "3_full_mix_with_countin.wav"
NO_CLICK = {BACKING: "5_backing_no_guitar.wav", FULL_MIX: "4_full_mix.wav"}  # same audio, click count-in removed
PLAY_CH = (1, 2)   # host outputs -> Stomp main outs / phones
DRY_CH = (5, 6)    # host inputs <- dry Main L/R
WET_CH = (1, 2)    # host inputs <- Path A


def peak_db(x: np.ndarray) -> float:
    p = float(np.max(np.abs(x))) if x.size else 0.0
    return 20 * np.log10(p) if p > 0 else -np.inf


def level_report(dry: np.ndarray, wet: np.ndarray) -> list[str]:
    """Plain warnings about the take's levels (empty when the take looks usable)."""
    warn = []
    pd, pw = peak_db(dry), peak_db(wet)
    if pd < -45:
        warn.append(f"dry input is nearly silent (peak {pd:.1f} dBFS): is the guitar in the Stomp's main input?")
    if pd > -0.5:
        warn.append(f"dry input clipped (peak {pd:.1f} dBFS): lower the guitar volume or the input pad")
    if pw < -60 and pd >= -45:
        warn.append("processed path is silent while the dry input has signal: is the Input block set to USB 5/6?")
    return warn


def take_frames(rec: np.ndarray, latency: int, n: int) -> np.ndarray:
    """Shift a duplex recording by the round trip so it lines up with the played file, length n."""
    out = rec[latency:latency + n]
    if len(out) < n:
        out = np.vstack([out, np.zeros((n - len(out), rec.shape[1]), rec.dtype)])
    return out


def load_backing(path: Path, seconds: float | None, level_db: float) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != reamp.SR:
        raise SystemExit(f"{path.name} is {sr} Hz; the Stomp stream runs at {reamp.SR} Hz")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    if seconds:
        x = x[: int(seconds * sr)]
    return x[:, :2] * np.float32(10 ** (level_db / 20))


def countdown(s: int) -> None:
    for k in range(s, 0, -1):
        print(f"  starting in {k} ...", flush=True)
        time.sleep(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--song-id", default=None, help="song id: the pack is data/songs/<id>-playalong/")
    ap.add_argument("--pack", type=Path, default=None, help="play-along pack folder (backing wavs inside; takes go to <pack>/takes/)")
    ap.add_argument("--backing", default=BACKING)
    ap.add_argument("--with-guitar", action="store_true",
                    help="play the full mix (original guitar included) instead of the backing without guitar")
    ap.add_argument("--no-click", action="store_true",
                    help="play the files without the click count-in (the song's own count-in only)")
    ap.add_argument("--takes", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=None, help="play only the first N seconds")
    ap.add_argument("--level-db", type=float, default=-6.0, help="backing playback gain (dB)")
    ap.add_argument("--countdown", type=int, default=10, help="seconds before each take")
    ap.add_argument("--check", action="store_true", help="3 s of silence out; report the input levels")
    cli_result.add_result_arg(ap)
    a = ap.parse_args(argv)
    if a.pack is None and a.song_id:
        a.pack = SONGS / f"{a.song_id}-playalong"
    if a.pack is None and not a.check:
        ap.error("give --song-id ID or --pack DIR (not needed with --check)")
    return cli_result.run(a, tool="playalong_record", body=lambda res: _run(a, res),
                          inputs={"pack": a.pack})


def _run(a, res) -> int:
    dev = reamp.find_device()
    cal = reamp.Calibration.load()
    latency = int(cal.latency_samples)

    if a.check:
        print("Level check: play a few notes now (3 s) ...", flush=True)
        rec = reamp.duplex(np.zeros((3 * reamp.SR, 2), np.float32), PLAY_CH, dev)
        dry, wet = rec[:, DRY_CH[0] - 1], rec[:, [c - 1 for c in WET_CH]]
        print(f"dry (USB in 5) peak {peak_db(dry):.1f} dBFS; processed (USB in 1/2) peak {peak_db(wet):.1f} dBFS")
        for w in level_report(dry, wet):
            print("WARNING:", w)
        res.metrics.update({"mode": "check", "dry_peak_dbfs": round(peak_db(dry), 2),
                            "processed_peak_dbfs": round(peak_db(wet), 2), "warnings": level_report(dry, wet)})
        return 0

    if a.with_guitar:
        a.backing = FULL_MIX
    if a.no_click:
        a.backing = NO_CLICK.get(a.backing, a.backing)
    backing = load_backing(a.pack / a.backing, a.seconds, a.level_db)
    n = len(backing)
    out_dir = a.pack / "takes"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"HX Stomp device {dev}; backing {a.backing} ({n / reamp.SR:.1f} s at {a.level_db:+.0f} dB); "
          f"round trip {latency} samples; {a.takes} take(s)")
    results = []
    for t in range(1, a.takes + 1):
        print(f"\nTake {t} of {a.takes}: play along.")
        countdown(a.countdown)
        print("  recording ...", flush=True)
        rec = reamp.duplex(backing, PLAY_CH, dev)
        rec = take_frames(rec, latency, n)
        dry = rec[:, DRY_CH[0] - 1]
        wet = rec[:, [c - 1 for c in WET_CH]]
        base = out_dir / f"{stamp}_take{t}"
        sf.write(f"{base}_dry.wav", dry, reamp.SR, subtype="FLOAT")
        sf.write(f"{base}_processed.wav", wet, reamp.SR, subtype="FLOAT")
        warn = level_report(dry, wet)
        info = {"take": t, "dry": f"{base.name}_dry.wav", "processed": f"{base.name}_processed.wav",
                "backing": a.backing, "seconds": n / reamp.SR, "level_db": a.level_db,
                "latency_samples": latency, "latency_firmware": cal.firmware,
                "dry_peak_dbfs": round(peak_db(dry), 2), "processed_peak_dbfs": round(peak_db(wet), 2),
                "warnings": warn}
        results.append(info)
        print(f"  saved {base.name}_dry.wav (peak {info['dry_peak_dbfs']} dBFS) and _processed.wav")
        for w in warn:
            print("  WARNING:", w)
    (out_dir / f"{stamp}_session.json").write_text(json.dumps(
        {"date": stamp, "device": "HX Stomp", "dry_input": "USB in 5 (Main L)", "takes": results}, indent=1))
    res.inputs["backing"] = a.pack / a.backing
    res.outputs.update({"takes_dir": out_dir, "session_json": out_dir / f"{stamp}_session.json",
                        "dry_wavs": [out_dir / r["dry"] for r in results],
                        "processed_wavs": [out_dir / r["processed"] for r in results]})
    res.metrics.update({"mode": "record", "takes": len(results), "seconds": n / reamp.SR,
                        "latency_samples": latency, "device": str(dev),
                        "dry_peak_dbfs": [r["dry_peak_dbfs"] for r in results],
                        "warnings": [w for r in results for w in r["warnings"]]})
    print(f"\nDone: {len(results)} take(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
