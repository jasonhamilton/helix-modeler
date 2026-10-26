#!/usr/bin/env python3
# historical, Mother-specific
"""T200: prepare (DI pick) and run the blind predictor-first drive match for one song id. LOCAL ONLY.

    .venv/bin/python scripts/t200_run.py <id> <stems_dir> <start:end> [--dry-run]
Window numbers come from scripts/t200_windows.py. The DI is picked as match_song does (dry.pick_di on the
target stem's register, style and density; first of 3; 10 s from the clip's file; peak -12.5 dBFS).
"""
import json, os, subprocess, sys
from pathlib import Path
import numpy as np, soundfile as sf
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
import match_song as MS

sid, stems, win = sys.argv[1:4]
dry_run = "--dry-run" in sys.argv
a, b = (float(x) for x in win.split(":"))
out = ROOT / "cache/matches" / sid
out.mkdir(parents=True, exist_ok=True)
song = MS.load_song(stems)
seg = song.guitar[round(a * MS.SR):round(b * MS.SR)]
picks, desc = MS.choose_di(seg, b - a, n=12)


def active_fraction(x, sr=MS.SR):
    """Share of 0.5 s blocks within 40 dB of the loudest block (a sparse, mostly silent DI is a poor stand-in for a riff)."""
    blk = sr // 2
    r = np.array([np.sqrt(np.mean(x[i:i + blk].astype(np.float64) ** 2)) for i in range(0, len(x) - blk + 1, blk)])
    return float(np.mean(r > r.max() * 1e-2))


fr = [active_fraction(d) for _, d in picks]
idx = next((i for i, f in enumerate(fr) if f >= 0.8), int(np.argmax(fr)))
picks = [picks[idx]] + [p for i, p in enumerate(picks) if i != idx][:2]
c, di = picks[0]
sf.write(str(out / "di_choice.wav"), di, MS.SR, subtype="FLOAT")
info = {"di_rank_in_pick_di": idx + 1, "active_fractions_top12": [round(f, 2) for f in fr], "target_desc": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in desc.items()},
        "di": [{"path": str(Path(p.path).name), "start_s": float(p.start_s), "style": p.style,
                "pitch_lo": float(p.pitch_lo), "pitch_hi": float(p.pitch_hi), "source": p.source} for p, _ in picks]}
(out / "di_choice.json").write_text(json.dumps(info, indent=1, default=str))
print(json.dumps(info, indent=1, default=str))
if dry_run:
    sys.exit(0)
env = dict(os.environ, TONEMATCH_AFXREP_DEVICE="cpu", TONEMATCH_AGENT="search")
cmd = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/match_block.py"), "--target-stems", stems,
       "--target-window", win, "--di", str(out / "di_choice.wav"), "--category", "drive", "--shortlist", "predictor",
       "--final-rank", "afxrep", "--workers", "1", "--song", sid, "--out", str(out), "--pack-lufs",
       "--preset-name", f"T200 {sid}"[:24], "--result-json", str(out / "cli_result.json")]
sys.exit(subprocess.call(cmd, env=env, cwd=ROOT))
