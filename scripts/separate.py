#!/usr/bin/env python3
"""separate (owner: separation). See .claude/skills/separate/SKILL.md.

  python scripts/separate.py <song> [--stem guitar.wav] [--model htdemucs_6s|bs-roformer|...]
         [--backend cpu|mps|cuda|rocm] [--override "1-8:0,9-16:1"] [--dataset ID] [--no-ledger]

Writes cache/stems/<hash>/<model>/{mix,guitar,others,<stem>}.wav, confidence.npy, sections.json,
manifest.json and appends kind=separate ledger rows. No audio leaves the machine.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from tonematch.separation import (
    DEFAULT_MODEL,
    ROFORMER_MODELS,
    SectionMap,
    SeparatorUnavailable,
    confidence_mask,
    segment,
)
from tonematch.separation import separate as run_separate


def ledger(dataset: str, metric: str, value: float, notes: str, wall: float | None = None) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "separate",
           "--dataset", dataset, "--metric", metric, "--value", f"{value:.6g}", "--notes", notes]
    if wall is not None:
        cmd += ["--wall-clock-s", f"{wall:.3f}"]
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "separation")}
    subprocess.run(cmd, check=True, env=env, cwd=ROOT)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("song")
    p.add_argument("--stem", help="user-supplied guitar stem; skips separation (mix still required)")
    p.add_argument("--model", default=DEFAULT_MODEL, choices=[DEFAULT_MODEL, *ROFORMER_MODELS])
    p.add_argument("--backend", choices=["cpu", "mps", "cuda", "rocm", "directml"])
    p.add_argument("--override", help='bar->snapshot override, 1-based inclusive: "1-8:0,9-16:1"')
    p.add_argument("--dataset", help="ledger dataset / song id (default: file stem + hash)")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-ledger", action="store_true")
    a = p.parse_args()

    try:
        st = run_separate(a.song, a.stem, a.model, a.backend, use_cache=not a.no_cache)
    except SeparatorUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    d = st.cache_dir
    mask = confidence_mask(st)
    np.save(d / "confidence.npy", mask.values.astype(np.float16))
    sections = segment(st)
    if a.override:
        sections = sections.override(a.override)
    elif (d / "sections.json").exists() and SectionMap.load(d / "sections.json").overridden:
        # never clobber a user's override on a plain re-run
        sections = SectionMap.load(d / "sections.json")
    sections.save(d / "sections.json")

    mf = json.loads((d / "manifest.json").read_text())
    mf["confidence"] = mask.summary()
    mf["sections"] = {"tempo_bpm": sections.tempo_bpm, "bars": len(sections.bar_snapshot),
                      "n_snapshots": sections.n_snapshots, "overridden": sections.overridden}
    (d / "manifest.json").write_text(json.dumps(mf, indent=2))

    print(f"{'cache hit' if st.cached else 'separated'}: {d}")
    print(f"model={st.model} backend={st.backend} wall_clock_s={st.wall_clock_s:.2f} "
          f"confidence={mask.energy_weighted_mean:.3f} exposed_fraction={mask.exposed_fraction:.2f}")
    print(f"tempo {sections.tempo_bpm} bpm, {len(sections.bar_snapshot)} bars, "
          f"{sections.n_snapshots} snapshot(s); exposed windows: {len(sections.exposed_windows)}")
    print(sections.table())
    if st.cached or a.no_ledger:
        return 0
    ds = a.dataset or f"{Path(a.song).stem}-{st.song_hash[:8]}"
    note = f"{st.model}/{st.backend}" + ("; user-supplied stem, others=mix-guitar (approximate)"
                                         if st.user_supplied else "")
    ledger(ds, "wall_clock_s", st.wall_clock_s, note, st.wall_clock_s)
    ledger(ds, "confidence_mean", mask.energy_weighted_mean,
           f"{note}; heuristic dominance*consistency proxy, uncalibrated; "
           f"exposed_fraction={mask.exposed_fraction:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
