#!/usr/bin/env python3
"""Calibration table for the modulation specialist (T116, owner: tone-metrics).

`tonematch.metrics.mod_specialist.seed` turns a target's phase-invariant measurements into knob
values per model by looking up the nearest calibration renders. This script makes those renders and
the table. Library preparation, done once per library version (like `seed_calibrate.py`, T113).

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

OUT = ROOT / "cache" / "mod-specialist"
SEED = 1160
N_SETTINGS = 40
SECONDS = 10.0
CAL_DIS = ({"path": "Guitar-TECHS/P3_music/audio/directinput/directinput_04.wav", "start_s": 12.0},
           {"path": "Guitar-TECHS/P3_music/audio/directinput/directinput_04.wav", "start_s": 25.0})
MAX_HOSTS = 2
MAX_MACHINE_HOSTS = 6
# the Gate 2 stand-in list (synthetic-v1 eligibility) first, then classic flangers, phasers, choruses,
# vibratos and rotaries for sub-type coverage; ring modulators, shifters, tape and filter effects are left out
CAL_MODELS = (
    "HD2_Chorus70sChorus", "HD2_MM4AnalogChorus", "HD2_MM4BiasTremolo", "HD2_MM4OptoTremolo", "HD2_MM4PatternTrem",
    "HD2_Tremolo60sBiasTrem", "HD2_TremoloHarmonic", "HD2_TremoloOpticalTrem", "HD2_TremoloPattern",
    "HD2_TremoloTremolo", "HD2_VibratoBubbleVibrato",
    "HD2_Chorus", "HD2_Chorus4Voice", "HD2_ChorusAmpegLiquifier", "HD2_ChorusPlastiChorus", "HD2_ChorusTrinityChorus",
    "HD2_MM4TriChorus", "HD2_FlangerCourtesanFlange", "HD2_FlangerGrayFlanger", "HD2_FlangerHarmonicFlanger",
    "HD2_M1380AFlanger", "HD2_M13ACFlanger", "HD2_MM4AnalogFlanger", "HD2_MM4JetFlanger", "HD2_MM4Phaser",
    "HD2_MM4DualPhaser", "HD2_MM4ScriptPhase", "HD2_MM4UVibe", "HD2_PhaserDeluxePhaser", "HD2_PhaserPebblePhaser",
    "HD2_PhaserScriptModPhase", "HD2_PhaserUbiquitousVibe", "VIC_FlexoVibe", "HD2_MM4PitchVibrato",
    "HD2_Rotary122Rotary", "HD2_Rotary145Rotary", "HD2_RotaryVibeRotary",
)


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
    for m in CAL_MODELS:
        tpl = NL.template_for(m)
        if tpl is None:
            continue
        free = SV.free_params(m, tpl, catalog, "mod")
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
    from tonematch.metrics import mod_specialist as MS

    dry = _DIS[d]
    y = np.load(render_file(m, j, d))
    if not np.any(y):
        return None
    f = MS.features(y, dry)
    an = MS.analyse(y, dry, f)
    return {"setting": j, "di": d, "values": values, "summary": MS.summary_vector(an, f).tolist(),
            "analysis": an.to_dict()}


_DIS: list = []


def _init():
    global _DIS
    _DIS = load_dis()


def cmd_build(a) -> int:
    from concurrent.futures import ProcessPoolExecutor

    from tonematch.metrics import mod_specialist as MS

    pl = json.loads((OUT / "cal_plan.json").read_text())
    jobs = [(m, j, d, values_of(e, k)) for m, e in pl["models"].items() for j, k in enumerate(e["settings"])
            for d in range(len(CAL_DIS)) if render_file(m, j, d).exists()]
    with ProcessPoolExecutor(a.jobs, initializer=_init) as ex:
        rows = list(ex.map(_features_one, jobs, chunksize=8))
    models: dict = {}
    for (m, *_), r in zip(jobs, rows, strict=True):
        if r is None:
            continue
        models.setdefault(m, {"subtype": MS.subtype_of_model(m), "free": pl["models"][m]["free"], "rows": []})
        models[m]["rows"].append(r)
    doc = {"version": MS.VERSION, "summary_keys": list(MS.SUMMARY_KEYS), "library_version": pl["library_version"],
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
