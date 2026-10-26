#!/usr/bin/env python3
"""T209: fine-tune ALL free knobs of each song's T207 model against the AFx-Rep distance. LOCAL ONLY.

    .venv/bin/python scripts/finetune_knobs.py --songs a,b [--budget 96] [--jobs 3] [--variant -x] [--lock-level-knobs]
    .venv/bin/python scripts/finetune_knobs.py --songs a --stems-dir D --match-dir M --dry-dir P --out O   # explicit folders

Model fixed (the search folder's result.json finals[0]; default cache/matches/<song>-dryrebuild<variant>); same DI, window and target as T207;
score = embed_afxrep distance of the render to the separated guitar (no mask, as in T207's shortlist ranking).
One Native host per song, at most --jobs at once, a busy-core guard before each launch and generation.
Output: cache/matches/<song>-finetune<variant>/{result.json,best.hlx,best.validate.md,listen/}.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import match_block as MB  # noqa: E402
import match_song as MS  # noqa: E402

LEVEL_KNOBS = ("output", "master")       # --lock-level-knobs keeps these at the model's defaults (not approved as a default)


class _Locked:
    """A block view whose free knobs exclude the level knobs; they stay at the block's defaults."""

    def __init__(self, block, names=LEVEL_KNOBS):
        self._b = block
        self.free = [f for f in block.free if f[0].lower() not in names]
        self.locked = [f[0] for f in block.free if f[0].lower() in names]

    def with_values(self, values):
        return self._b.with_values({k: v for k, v in values.items() if k not in self.locked})

    def __getattr__(self, k):
        return getattr(self._b, k)


def t207_pick(match_dir: Path) -> dict:
    d = json.loads((Path(match_dir) / "result.json").read_text())
    return {"model": d["finals"][0]["model"], "values": d["finals"][0]["values"], "window": d["window"],
            "t207_renders": d["renders"], "t207_wall_s": d["wall_s"]}


def resolve_job(song: str, a) -> SimpleNamespace:
    """Explicit dirs win; anything not given comes from the song id through tonematch.songs (legacy variant naming)."""
    from tonematch import songs as SG

    v = a.variant or ""
    stems = Path(a.stems_dir) if a.stems_dir else SG.resolve(song).stems_dir
    return SimpleNamespace(
        song=song, stems_dir=stems,
        match_dir=Path(a.match_dir) if a.match_dir else SG.match_dir(song, v),
        dry_dir=Path(a.dry_dir) if a.dry_dir else SG.playlevel_dir(song, v),
        out=Path(a.out) if a.out else SG.finetune_dir(song, v))


def work(job: SimpleNamespace, budget: int, seed: int, patience: int, lock_level: bool = False) -> int:
    from tonematch.catalog import load_catalog
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.metrics import afxrep
    from tonematch.preset import write_hlx
    from tonematch.search.afx_refine import refine_block

    t0 = time.time()
    song, out = job.song, job.out
    out.mkdir(parents=True, exist_ok=True)
    log = MS.Log(out / "run.log")
    pick = t207_pick(job.match_dir)
    w = pick["window"]
    d = job.dry_dir
    args = SimpleNamespace(target_wav=None, target_stems=str(job.stems_dir),
                           target_window=f"{w['start_s']}:{w['end_s']}", di=str(d / "rebuilt_dry_playlevel.wav"),
                           di_json=str(d / "rebuilt_dry_playlevel_align.json"), song=song)
    section, _ = MB.build_target_section(args)
    catalog = load_catalog()
    block = MB.make_blocks({pick["model"]: MB.category_pool("drive", catalog)[pick["model"]]}, "drive", catalog)[pick["model"]]
    if lock_level:
        block = _Locked(block)
        log(f"level knobs locked at defaults: {block.locked}")
    log(f"{song}: {pick['model']} start {pick['values']}; {len(section.dry) / MS.SR:.1f} s window")
    engine = MB.make_engine(144_000, log)
    guard = LoadGuard()
    try:
        tf = afxrep.features(section.target)
        r = refine_block(engine, block, section.dry, tf, pick["values"], embed=afxrep.features, dist=afxrep.distance,
                         budget=budget, seed=seed, patience=patience, wait=lambda: guard.wait(lambda x: log(f"pause: busy cores {x}")), log=log)
        engine.load(block.with_values(r["start_values"]))
        y0 = engine.render(section.dry)
        p = block.with_values(r["final_values"])
        engine.load(p)
        y1 = engine.render(section.dry)
        renders = r["renders"] + 2
    finally:
        engine.close()
    p.name = f"{song} finetune"[:32]
    write_hlx(p, out / "best.hlx")
    code, text = MS.validate_hlx(out / "best.hlx")
    (out / "best.validate.md").write_text(text + "\n")
    info = MS.write_pack(out / "listen", section.target, y1, y0, section.dry, {"t207_match": y0}, lufs=True)
    (out / "listen" / "start_unmatched.wav").unlink(missing_ok=True)
    shutil.copy(out / "listen" / "t207_match.wav", out / "t207_match.wav")
    shutil.copy(out / "listen" / "match.wav", out / "match.wav")
    shutil.copy(out / "listen" / "ab_L-target_R-match.wav", out / "ab_L-target_R-match.wav")
    big = {k: v for k, v in r["moved_frac"].items() if v > 0.15}
    res = {"song": song, "model": pick["model"], "window": w, "engine": "native", "measure": "embed_afxrep distance to separated guitar, unmasked (as T207 final ranking)",
           "start_values": r["start_values"], "final_values": r["final_values"], "moved_frac_of_range": r["moved_frac"],
           "moved_over_0.15": big, "dist_before": r["dist_before"], "dist_after": r["dist_after"], "improved": r["dist_after"] < r["dist_before"] - 1e-9,
           "renders": renders, "search_renders": r["renders"], "stopped": r["stopped"], "trace": r["trace"],
           "wall_s": time.time() - t0, "validate_exit": code, "pack": info, "t207_renders": pick["t207_renders"], "seed": seed,
           "locked_level_knobs": getattr(block, "locked", [])}
    (out / "result.json").write_text(json.dumps(res, indent=1, default=MS.jsonable))
    log(f"done {song}: {r['dist_before']:.4f} -> {r['dist_after']:.4f}, {renders} renders, {res['wall_s']:.0f} s, validate {code}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--songs", required=True, help="comma-separated song ids (resolved through tonematch.songs); "
                    "with --stems-dir/--match-dir/--dry-dir/--out exactly one")
    ap.add_argument("--budget", type=int, default=96)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--variant", default="", help="suffix: reads <match dir> cache/matches/<song>-dryrebuild<variant>, "
                    "data/songs/<song>-rebuilt/playlevel<variant>; writes <song>-finetune<variant>")
    ap.add_argument("--stems-dir", help="separated stems dir (default: the song id resolved through tonematch.songs)")
    ap.add_argument("--match-dir", help="the search folder holding result.json (default cache/matches/<song>-dryrebuild<variant>)")
    ap.add_argument("--dry-dir", help="folder holding rebuilt_dry_playlevel.wav and _align.json (default data/songs/<song>-rebuilt/playlevel<variant>)")
    ap.add_argument("--out", help="output folder (default cache/matches/<song>-finetune<variant>)")
    ap.add_argument("--lock-level-knobs", action="store_true",
                    help="keep Output/Master at the model defaults (OFF by default: not approved by the human)")
    a = ap.parse_args(argv)
    explicit = any((a.stems_dir, a.match_dir, a.dry_dir, a.out))
    ids = [x for x in a.songs.split(",") if x]
    if explicit and len(ids) != 1:
        ap.error("explicit dirs go with exactly one song")
    if a.worker or explicit:
        song = a.worker or ids[0]
        if not a.worker:
            from tonematch.engines.host_budget import LoadGuard

            LoadGuard().wait(lambda x: print(f"pause: busy cores {x}", flush=True))
        return work(resolve_job(song, a), a.budget, a.seed, a.patience, a.lock_level_knobs)
    from tonematch.engines.host_budget import LoadGuard

    guard, todo, running, codes = LoadGuard(), ids, {}, {}
    try:
        while todo or running:
            for s, p in list(running.items()):
                if p.poll() is not None:
                    codes[s] = p.returncode
                    del running[s]
            if todo and len(running) < min(a.jobs, 3):
                guard.wait(lambda x: print(f"pause: busy cores {x}", flush=True))
                s = todo.pop(0)
                cmd = [sys.executable, __file__, "--songs", s, "--worker", s, "--budget", str(a.budget), "--seed", str(a.seed),
                       "--patience", str(a.patience), f"--variant={a.variant}"]
                cmd += ["--lock-level-knobs"] if a.lock_level_knobs else []
                running[s] = subprocess.Popen(cmd, cwd=ROOT)
                print("started", s, flush=True)
                time.sleep(3)
            else:
                time.sleep(2)
    finally:
        for p in running.values():
            if p.poll() is None:
                p.terminate()
    print(codes)
    return 0 if all(c == 0 for c in codes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
