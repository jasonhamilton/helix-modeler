#!/usr/bin/env python3
# historical, Mother-specific
"""T200: pick a loud, distorted, high-confidence 10 s guitar window per song. LOCAL ONLY, prints numbers only.

Rule (recorded in docs/notes/five-songs-t200.md): candidates are 10 s windows at 0.5 s steps, mostly active
(>= 0.8 of frames above the match_song activity floor). Keep windows whose guitar-stem loudness (BS.1770 LUFS)
is within 2 dB of the song's 90th-percentile window loudness (the loud sections) and whose crest factor
(peak over RMS of the guitar stem, dB; low = compressed/distorted) is at or below the song's median. Among the kept windows take the highest guitar-power-weighted
separator confidence; windows within 0.01 of the best are ties, broken by the lower crest factor.
"""
import json, sys
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
import match_song as MS
from tonematch.separation.audio import integrated_loudness

SR, HOP, DUR = MS.SR, MS.HOP, 10.0


def pick(stems_dir: str, dur: float = DUR) -> dict:
    song = MS.load_song(stems_dir)
    g = song.guitar
    total = len(g) / SR
    fc, fp = MS.frame_confidence(song.mask_tf, g)
    active = fp > np.percentile(fp, 95) * 10 ** (-35 / 10)
    rows = []
    for a in np.arange(0, total - dur + 1e-9, 0.5):
        x = g[int(a * SR):int((a + dur) * SR)]
        i, j = int(a * SR / HOP), int((a + dur) * SR / HOP)
        w = fp[i:j]
        conf = float((fc[i:j] * w).sum() / (w.sum() + 1e-20))
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)
        crest = 20 * np.log10(float(np.abs(x).max()) / rms + 1e-12)
        rows.append(dict(start=float(a), conf=conf, act=float(active[i:j].mean()), crest=float(crest),
                         lufs=float(integrated_loudness(x.reshape(1, -1).astype(np.float32)))))
    ok = [r for r in rows if r["act"] >= 0.8] or rows
    p90 = float(np.percentile([r["lufs"] for r in ok], 90))
    med_crest = float(np.median([r["crest"] for r in rows]))
    loud = [r for r in ok if r["lufs"] >= p90 - 2.0 and r["crest"] <= med_crest] or [r for r in ok if r["lufs"] >= p90 - 2.0]
    top = max(r["conf"] for r in loud)
    ties = [r for r in loud if r["conf"] >= top - 0.01]
    best = min(ties, key=lambda r: r["crest"])
    return dict(total_s=total, p90_lufs=p90, n_windows=len(rows), n_loud=len(loud), best=best,
                song_median_lufs=float(np.median([r["lufs"] for r in rows])),
                song_median_conf=float(np.median([r["conf"] for r in rows])),
                song_median_crest=float(np.median([r["crest"] for r in rows])))


if __name__ == "__main__":
    out = {}
    for item in sys.argv[1:]:
        k, d = item.split("=")
        out[k] = pick(d)
        b = out[k]["best"]
        print(k, f"{b['start']:.1f}:{b['start']+DUR:.1f}", {kk: round(v, 3) for kk, v in out[k].items() if kk != 'best'}, {kk: round(v, 2) for kk, v in b.items()})
    Path("cache/t200/windows.json").write_text(json.dumps(out, indent=1))
