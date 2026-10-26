#!/usr/bin/env python3
"""Line up play-along takes with the separated guitar stem and pick the tightest one (T163).

The take and the stem are different guitars and performances, so waveforms do not correlate; their
note onsets do. Both are reduced to an onset-strength envelope (10 ms hop), then:
  1. global lag: the cross-correlation peak within +-max_lag_s;
  2. timing drift: the lag and the correlation peak in sliding windows (win_s long, hop_s apart),
     only in windows where the stem has guitar (envelope energy above a floor);
  3. score: the median windowed correlation, minus a penalty for lag spread (loose timing).
Writes <take>_align.json next to each take, with alignment.take_sample0_song_s for
scripts/match_song.py --user-di-json, and prints a ranking. Local only.

    python scripts/align_takes.py --song-id my-song data/songs/my-song-playalong/takes/*_take*_dry.wav
    python scripts/align_takes.py takes/new_dry.wav --stem path/to/guitar_stem.wav
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
from tonematch import cli_result

SR = 48_000
HOP = 480            # 10 ms


def mono(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SR:
        raise SystemExit(f"{path} is {sr} Hz, expected {SR}")
    return x.mean(axis=1)


def onset_env(x: np.ndarray) -> np.ndarray:
    import librosa

    e = librosa.onset.onset_strength(y=x, sr=SR, hop_length=HOP, n_mels=64, fmax=8000)
    return (e / (np.max(e) + 1e-12)).astype(np.float64)


def ncc(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Lag of b relative to a (b[t + lag] ~ a[t]) maximising normalised correlation, and its value."""
    a = a - a.mean()
    b = b - b.mean()
    best, best_l = -1.0, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[: len(a) - lag], b[lag: lag + len(a) - lag]
        else:
            x, y = a[-lag:], b[: len(a) + lag]
        n = min(len(x), len(y))
        if n < 10:
            continue
        x, y = x[:n], y[:n]
        d = np.sqrt(np.sum(x * x) * np.sum(y * y))
        c = float(np.sum(x * y) / d) if d > 0 else 0.0
        if c > best:
            best, best_l = c, lag
    return best_l, best


def align(stem_env: np.ndarray, take_env: np.ndarray, max_lag_s: float = 2.0, win_s: float = 6.0,
          hop_s: float = 2.0, local_s: float = 0.25, floor: float = 0.05) -> dict:
    fps = SR / HOP
    g_lag, g_c = ncc(stem_env, take_env, int(max_lag_s * fps))
    w, h, ll = int(win_s * fps), int(hop_s * fps), int(local_s * fps)
    wins = []
    for s in range(0, len(stem_env) - w, h):
        a = stem_env[s: s + w]
        if np.mean(a) < floor:       # no guitar in the stem here
            continue
        s2 = s + g_lag
        if s2 - ll < 0 or s2 + w + ll > len(take_env):
            continue
        b = take_env[s2 - ll: s2 + w + ll]
        lag, c = ncc(np.pad(a, (ll, ll)), b, ll)
        wins.append({"t_s": round(s / fps, 2), "lag_ms": round(lag / fps * 1000, 1), "corr": round(c, 3)})
    corr = np.array([x["corr"] for x in wins]) if wins else np.array([0.0])
    lags = np.array([x["lag_ms"] for x in wins]) if wins else np.array([0.0])
    spread = float(np.percentile(lags, 75) - np.percentile(lags, 25)) if len(lags) > 1 else 0.0
    return {"global_lag_s": g_lag / fps, "global_corr": round(g_c, 3), "windows": wins,
            "median_corr": round(float(np.median(corr)), 3), "lag_iqr_ms": round(spread, 1),
            "score": round(float(np.median(corr)) - spread / 1000.0, 3)}


def resolve_stem(stem: Path | None, song_id: str | None, takes: list[Path]) -> Path:
    """--stem, else the stem of --song-id, else of the play-along pack the first take sits in (data/songs/<id>-playalong/takes/)."""
    if stem is not None:
        return stem
    if song_id is None:
        pack = takes[0].resolve().parent.parent
        if pack.name.endswith("-playalong") and takes[0].resolve().parent.name == "takes":
            song_id = pack.name.removesuffix("-playalong")
            if (pack / "target_guitar_stem.wav").exists():
                return pack / "target_guitar_stem.wav"
    if song_id is None:
        raise SystemExit("give --stem WAV or --song-id ID (the takes are not inside a data/songs/<id>-playalong/takes/ folder)")
    sys.path.insert(0, str(ROOT / "scripts"))
    import transcribe_stems as TS
    return TS.resolve_stem(song_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("takes", nargs="+", type=Path)
    ap.add_argument("--stem", type=Path, default=None,
                    help="separated guitar stem wav (default: from --song-id, or from the play-along pack the takes sit in)")
    ap.add_argument("--song-id", default=None, help="song id; finds the stem (data/songs/<id>-rebuilt/stem.wav or the stems cache)")
    ap.add_argument("--take-start-song-s", type=float, default=0.0,
                    help="song time at which the played file started (0 for the no-click full mix)")
    cli_result.add_result_arg(ap)
    a = ap.parse_args(argv)
    a.stem = resolve_stem(a.stem, a.song_id, a.takes)
    return cli_result.run(a, tool="align_takes", body=lambda res: _run(a, res),
                          inputs={"stem": a.stem, "takes": a.takes})


def _run(a, res) -> int:
    se = onset_env(mono(a.stem))
    rows = []
    for t in a.takes:
        r = align(se, onset_env(mono(t)))
        # take sample (g_lag) lines up with stem sample 0, so take sample 0 is at song time -g_lag
        t0 = a.take_start_song_s - r["global_lag_s"]
        out = {"take": t.name, "stem": str(a.stem.relative_to(ROOT)) if a.stem.is_relative_to(ROOT) else str(a.stem),
               "alignment": {"take_sample0_song_s": round(t0, 4), "method": "onset-envelope NCC, 10 ms"}, **r}
        p = t.with_name(t.stem.removesuffix("_dry") + "_align.json")
        p.write_text(json.dumps(out, indent=1))
        rows.append((r["score"], t.name, r, p))
    rows.sort(key=lambda x: -x[0])
    print(f"{'take':40s} score  median_corr  lag_IQR_ms  global_lag_ms  windows")
    for sc, name, r, _ in rows:
        print(f"{name:40s} {sc:5.3f}  {r['median_corr']:11.3f}  {r['lag_iqr_ms']:10.1f}  "
              f"{r['global_lag_s'] * 1000:13.1f}  {len(r['windows'])}")
    print(f"best: {rows[0][1]}  ({rows[0][3].name})")
    res.outputs["align_json"] = [r[3] for r in rows]
    res.metrics.update({"best_take": rows[0][1], "best_score": rows[0][0],
                        "takes": {n: {"score": sc, "median_corr": r["median_corr"], "lag_iqr_ms": r["lag_iqr_ms"],
                                      "global_lag_s": r["global_lag_s"], "windows": len(r["windows"])}
                                  for sc, n, r, _ in rows}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
