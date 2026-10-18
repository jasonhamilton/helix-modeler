#!/usr/bin/env python3
"""Calibration table for the drive path (T143, owner: search). Adapted from scripts/mod_calibrate.py (T116).

`tonematch.search.drive_specialist` turns a target's measurements against its DI (`drive_features`) into knob
values per drive model by looking up the nearest calibration renders. This script makes those renders and the
table. Library preparation, done once per library version.

  render   Native, at most 2 hosts (and never more than 6 native_host processes on the machine,
           counting other agents'): every modulation model in CAL_MODELS at N_SETTINGS random settings
           on the CC grid (0.05-0.95 of each template range) plus its defaults, on two passages of the
           calibration DI. Resumable; renders in cache/mod-specialist/cal_renders/ (local).
  build    offline: features and summary vectors of every render -> cache/mod-specialist/calibration.json
           (local: holds model ids). Re-run after any change to the specialist's features.

Calibration DI: Guitar-TECHS P3_music directinput_04 (the T113 calibration file; no synthetic-v1,
helix-trust-v2 or mod-trust-v1 item uses it). No song audio.
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "cache" / "drive-specialist"
SEED = 1430
N_SETTINGS = 24
SECONDS = 10.0
CAL_DIS = (  # eight excerpts of eight recordings and four playing styles; none overlaps an item excerpt of any set
    {"path": "Guitar-TECHS/P2_chords/audio/directinput/directinput_Drop3_m7b5.wav", "start_s": 144.0},
    {"path": "Guitar-TECHS/P1_singlenotes/audio/directinput/directinput_allsinglenotes.wav", "start_s": 472.0},
    {"path": "Guitar-TECHS/P2_chords/audio/directinput/directinput_Set1_m7.wav", "start_s": 180.0},
    {"path": "Guitar-TECHS/P2_techniques/audio/directinput/directinput_PinchHarmonics.wav", "start_s": 508.0},
    {"path": "Guitar-TECHS/P1_scales/audio/directinput/directinput_Bb.wav", "start_s": 28.0},
    {"path": "Guitar-TECHS/P1_scales/audio/directinput/directinput_A.wav", "start_s": 148.0},
    {"path": "Guitar-TECHS/P2_techniques/audio/directinput/directinput_PalmMute.wav", "start_s": 436.0},
    {"path": "Guitar-TECHS/P1_techniques/audio/directinput/directinput_Vibrato.wav", "start_s": 216.0},
)
MAX_HOSTS = 2
MAX_MACHINE_HOSTS = 6
CAL_MODELS: tuple = ()      # filled by eligible_drives(): every eligible, template-backed drive with free parameters


def eligible_drives(catalog) -> tuple:
    import synthetic_v1 as SV

    from tonematch.catalog import native_unavailable
    from tonematch.engines import native_library as NL

    idx = NL.load_index()["models"]
    tv = {m for m, e in idx.items() if e.get("time_variant")} | set(native_unavailable())
    return tuple(sorted(m for m, e in idx.items() if e["category"] == "drive" and SV.eligible(m, "drive", tv)
                        and not NL.is_routing(m)))


def machine_hosts() -> int:
    """Native host processes on the machine (any agent's). Matches the host's own command line only, not
    a shell whose command text happens to name the module."""
    r = subprocess.run(["pgrep", "-f", r"[Pp]ython[^ ]* -m tonematch\.engines\.native_host"], capture_output=True,
                       text=True, check=False)
    return len([x for x in r.stdout.split() if x.strip()])


def plan(catalog) -> dict:
    """model -> {"free": [(name, lo, hi, default)], "settings": [{name: knob 0..127}]} (seeded)."""
    import synthetic_v1 as SV

    from tonematch.engines import native_library as NL

    out = {}
    for m in eligible_drives(catalog):
        tpl = NL.template_for(m)
        if tpl is None:
            continue
        free = SV.free_params(m, tpl, catalog, "drive")
        if not free:
            continue
        rng = np.random.default_rng([SEED, sum(map(ord, m))])
        sets = [{n: int(np.clip(round((d - lo) / (hi - lo) * 127), 0, 127)) for n, lo, hi, d in free}]
        for _ in range(N_SETTINGS):
            sets.append({n: round(rng.uniform(0.05, 0.95) * 127) for n, *_ in free})
        out[m] = {"free": [list(f) for f in free], "settings": sets}
    return out


def render_file(m: str, j: int, d: int) -> Path:
    return OUT / "cal_renders" / m / f"s{j:02d}_d{d}.npy"


def values_of(entry: dict, knobs: dict) -> dict[str, float]:
    import synthetic_v1 as SV

    rng = {n: (lo, hi) for n, lo, hi, _ in entry["free"]}
    return {n: SV.grid_value(*rng[n], k) for n, k in knobs.items()}


def load_dis() -> list[np.ndarray]:
    import helix_trust_set as HT
    import synthetic_v1 as SV

    return [HT.load_excerpt(dict(e), SECONDS, SV.REF_LOUD_DBFS)[0] for e in CAL_DIS]


def cmd_render(a) -> int:
    from tonematch.catalog import load_catalog

    hosts = min(a.hosts, MAX_HOSTS)
    running = machine_hosts()
    if running + hosts > MAX_MACHINE_HOSTS:
        hosts = MAX_MACHINE_HOSTS - running
        if hosts < 1:
            print(f"{running} native_host processes already running; limit {MAX_MACHINE_HOSTS}", file=sys.stderr)
            return 3
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeRenderPool, RenderJob

    catalog = load_catalog()
    pl = plan(catalog)
    if a.models:
        pl = {m: e for m, e in pl.items() if m in a.models}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cal_plan.json").write_text(json.dumps({"seed": SEED, "dis": CAL_DIS, "library_version": NL.VERSION,
                                                   "models": pl}, indent=1))
    dis = load_dis()
    todo = [(m, j, d) for m, e in pl.items() for j in range(len(e["settings"])) for d in range(len(dis))
            if not render_file(m, j, d).exists()]
    print(f"{len(todo)} renders to do; native_host before start: {running}; opening {hosts}", flush=True)
    if not todo:
        return 0
    pool = NativeRenderPool(workers=hosts, templates_dir=NL.library_templates())
    t0, errors = time.perf_counter(), {}
    try:
        for s in range(0, len(todo), a.chunk):
            chunk = todo[s:s + a.chunk]
            jobs = []
            for m, j, d in chunk:
                p = NL.block_preset(m, catalog)
                for n, v in values_of(pl[m], pl[m]["settings"][j]).items():
                    p.slots[0].params[n] = v
                jobs.append(RenderJob(dry=dis[d], preset=p))
            outs = pool.render_batch(jobs, on_error="none")
            for k, ((m, j, d), y) in enumerate(zip(chunk, outs, strict=True)):
                if y is None:
                    err = repr(pool.last_errors.get(k))
                    if "returned silence" not in err:
                        errors[f"{m}:{j}:{d}"] = err
                        continue
                    y = np.zeros(len(dis[d]), np.float32)
                y = np.asarray(y, np.float32)
                if y.ndim == 2:
                    y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
                f = render_file(m, j, d)
                f.parent.mkdir(parents=True, exist_ok=True)
                np.save(f, y)
            print(f"{s + len(chunk)}/{len(todo)} in {time.perf_counter() - t0:.0f} s; errors {len(errors)}", flush=True)
    finally:
        pool.close()
    (OUT / "cal_errors.json").write_text(json.dumps(errors, indent=1))
    print(f"done; errors {len(errors)}; native_host after close: {machine_hosts()}", flush=True)
    return 1 if errors else 0


def _features_one(args):
    m, j, d, values = args
    from tonematch.search import drive_features as DF

    dry = _DIS[d]
    y = np.load(render_file(m, j, d))
    if not np.any(y):
        return None
    return {"setting": j, "di": d, "values": values, "summary": DF.features(y, dry).tolist()}


_DIS: list = []


def _init():
    global _DIS
    _DIS = load_dis()


def cmd_build(a) -> int:
    from concurrent.futures import ProcessPoolExecutor

    from tonematch.search import drive_features as DF

    pl = json.loads((OUT / "cal_plan.json").read_text())
    jobs = [(m, j, d, values_of(e, k)) for m, e in pl["models"].items() for j, k in enumerate(e["settings"])
            for d in range(len(CAL_DIS)) if render_file(m, j, d).exists()]
    with ProcessPoolExecutor(a.jobs, initializer=_init) as ex:
        rows = list(ex.map(_features_one, jobs, chunksize=8))
    models: dict = {}
    for (m, *_), r in zip(jobs, rows, strict=True):
        if r is None:
            continue
        models.setdefault(m, {"free": pl["models"][m]["free"], "rows": []})
        models[m]["rows"].append(r)
    doc = {"version": "1.0", "summary_keys": list(DF.NAMES), "library_version": pl["library_version"],
           "dis": pl["dis"], "seed": pl["seed"], "models": models}
    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    print(f"{sum(len(e['rows']) for e in models.values())} rows, {len(models)} models -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=2)
    r.add_argument("--chunk", type=int, default=40)
    r.add_argument("--models", nargs="*")
    b = sub.add_parser("build")
    b.add_argument("--jobs", type=int, default=2)
    b.add_argument("--out", default=str(OUT / "calibration.json"))
    a = ap.parse_args(argv)
    return cmd_render(a) if a.cmd == "render" else cmd_build(a)


if __name__ == "__main__":
    raise SystemExit(main())
