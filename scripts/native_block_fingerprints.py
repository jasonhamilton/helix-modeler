#!/usr/bin/env python3
"""Block-probe fingerprints of the Native block library (T047, native-engine).

Renders the block-probe signal (``tonematch.probe``, ``probe-v1``, 47.25 s; ``-nodi`` if the DI
library is absent) through every block-library template at a coarse grid of settings and stores
one fingerprint set per model for search (T050: starting points per model). Order: the library's
breadth-first order (Distortion, Delay, Reverb; Dynamics, EQ, Modulation; amps, cabs, other).
Time-variant blocks (``native_library.is_time_variant``) are skipped.

Grid ``coarse-v1`` per model: ``default`` (the library preset), and for the model's two primary
params (``primary_params``: CC-verified, category priority names first) the four corners at CC 24
and 103 and the centre at CC 64, other params at their defaults. One primary: CC 24, 64, 103.
Values are on Native's CC grid (``native.cc_value`` over the template's range).

Format: docs/notes/native-spike.md (Block-probe fingerprints). Files (local, gitignored; derived
from the user's Native install, never committed): ``cache/probe/fingerprints/<library>/``
``<model>.json`` and ``index.json``. Resumable: models already stored are skipped.

At most ``--hosts`` Native hosts (default 3), and never more than ``--max-hosts`` on the machine.

  .venv/bin/python scripts/native_block_fingerprints.py run [--hosts 3] [--limit N] [--models a,b]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

import numpy as np

from tonematch.catalog import load_catalog
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

GRID = "coarse-v1"
CORNERS, CENTRE = (24, 103), 64
PRIORITY = {  # category -> param names tried first, in order (catalog names)
    "drive": ("Gain", "Drive", "Distortion", "Fuzz", "Sustain", "Dist", "Tone", "Treble", "Filter"),
    "delay": ("Time", "Feedback", "Mix"),
    "reverb": ("Decay", "Mix", "HighCut", "Tone"),
    "comp": ("Threshold", "Sensitivity", "PeakReduction", "Compression", "Sustain", "Ratio", "Attack"),
    "eq": (),
    "mod": ("Speed", "Rate", "Depth", "Intensity", "Mix"),
    "amp": ("Drive", "Master", "Treble", "Bass"),
    "cab": ("Distance", "HighCut", "LowCut"),
    "other": (),
}
EXCLUDE = {"Level", "Hum", "Ripple", "EarlyReflections", "Sag", "Bias", "BiasX", "ChVol", "Scale",
           "Spread", "Headroom", "Level_1", "A Level"}


def host_count() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], check=False,
                       capture_output=True, text=True)
    return len(r.stdout.split())


def out_dir() -> Path:
    return lib.fingerprint_dir()


def primary_params(model_id: str, catalog: dict, tpl: nat.ChainTemplate, n: int = 2) -> list[str]:
    """Up to ``n`` CC-verified continuous params to grid: category priority names first, then
    the catalog's order; output levels, Hum/Ripple, cab ER and similar are never chosen."""
    m = catalog["models"][model_id]
    ok = [k for k, p in (m.get("params") or {}).items()
          if p.get("kind") == "continuous" and f"0:{k}" in tpl.verified and f"0:{k}" in tpl.map
          and k not in EXCLUDE and not k.startswith("@")]
    pri = [k for k in PRIORITY.get(m["category"], ()) if k in ok]
    return (pri + [k for k in ok if k not in pri])[:n]


def grid(model_id: str, catalog: dict, tpl: nat.ChainTemplate) -> list[dict]:
    """Settings of grid ``coarse-v1``: ``[{"setting", "params", "cc"}]`` (params: name -> value)."""
    prims = primary_params(model_id, catalog, tpl)
    out = [{"setting": "default", "params": {}, "cc": {}}]
    if not prims:
        return out
    rng = {k: tuple(tpl.ranges.get(f"0:{k}", (0.0, 1.0))) for k in prims}
    if len(prims) == 1:
        combos = [(c,) for c in (CORNERS[0], CENTRE, CORNERS[1])]
    else:
        combos = [(a, b) for a in CORNERS for b in CORNERS] + [(CENTRE, CENTRE)]
    for cs in combos:
        params = {k: nat.cc_value(*rng[k], c) for k, c in zip(prims, cs, strict=True)}
        out.append({"setting": ",".join(f"{k}@{c}" for k, c in zip(prims, cs, strict=True)),
                    "params": params, "cc": dict(zip(prims, cs, strict=True))})
    return out


def summary(fp: dict) -> dict:
    """A few scalars per fingerprint for a quick nearest-setting lookup (search, T050)."""
    lin = fp.get("linear") or {}
    f = np.asarray(lin.get("freqs_hz") or [], float)
    mag = lin.get("mag_db") or []
    tilt = None
    if len(f) and len(mag) > 1 and mag[1]:
        m = np.asarray(mag[1], float)  # the -24 dBFS sweep
        i250, i4k = int(np.argmin(abs(f - 250))), int(np.argmin(abs(f - 4000)))
        tilt = round(float(m[i4k] - m[i250]), 2)
    thd = None
    for h in (fp.get("harmonics") or {}).get("per_freq") or []:
        if h.get("freq_hz") == 440.0 and h.get("levels_dbfs"):
            j = int(np.argmin(abs(np.asarray(h["levels_dbfs"], float) + 9.0)))  # the -9 dBFS step
            v = (h.get("thd_db") or [None] * (j + 1))[j]
            thd = None if v is None else round(float(v), 2)
    g = lin.get("gain_1k_db") or [None, None]
    sc, dl, rv, md, dy = (fp.get(k) or {} for k in ("static_curve", "delay", "reverb", "modulation", "dynamics"))
    return {"gain_1k_db": g[1] if len(g) > 1 else None, "tilt_4k_250_db": tilt, "thd_440_db": thd,
            "ratio": sc.get("ratio"), "knee_dbfs": sc.get("knee_dbfs"),
            "level_dependent_gain_db": dy.get("level_dependent_gain_db"),
            "delay_ms": dl.get("time_ms") if dl.get("detected") else None,
            "feedback": dl.get("feedback") if dl.get("detected") else None,
            "rt60_s": rv.get("rt60_s"), "mod_kind": md.get("kind"), "mod_rate_hz": md.get("rate_hz")}


def _analyse(args):
    wet, layout_path, subject = args
    from tonematch.probe import analysis, signals

    probe = signals.probe_from_layout(layout_path)
    t0 = time.perf_counter()
    fp = analysis.analyse(wet.astype(np.float64), probe, latency=0, subject=subject)
    return fp, time.perf_counter() - t0


def cmd_run(a) -> dict:
    from tonematch.probe import signals

    catalog = load_catalog()
    layout = ROOT / "cache" / "probe" / f"{signals.PROBE_VERSION}.json"
    if not layout.is_file():
        layout = ROOT / "cache" / "probe" / f"{signals.PROBE_VERSION}-nodi.json"
    probe = signals.probe_from_layout(layout)
    dry = probe.signal.astype(np.float32)
    idx = lib.load_index()
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    only = set(a.models.split(",")) if a.models else None
    todo, skipped = [], {}
    for m in lib.library_models(catalog):
        if only is not None and m not in only:
            continue
        if m not in idx["models"]:
            skipped[m] = "no template"
        elif lib.is_time_variant(m):
            skipped[m] = "time-variant"
        elif (d / f"{m}.json").is_file() and not a.force:
            continue
        else:
            todo.append(m)
    if a.limit:
        todo = todo[: a.limit]
    others = host_count()
    hosts = max(0, min(a.hosts, a.max_hosts - others))
    print(json.dumps({"todo": len(todo), "skipped": skipped, "native_hosts_running": others, "hosts": hosts}),
          flush=True)
    if not todo or hosts < 1:
        return {"todo": len(todo), "skipped": skipped, "hosts": hosts}
    t_all = time.perf_counter()
    pool = nat.NativeRenderPool(workers=hosts, templates_dir=lib.library_templates())
    per_model, errors = [], {}
    ex = cf.ProcessPoolExecutor(a.analysis_workers)
    pending: list = []

    def finish(item) -> None:
        m, grid_rows, futs, t_render, t0 = item
        out_rows, t_an = [], 0.0
        for g, fu in zip(grid_rows, futs, strict=True):
            if fu is None:
                out_rows.append({**g, "fingerprint": None, "summary": None, "error": g.pop("error", "render failed")})
                continue
            fp, dt = fu.result()
            t_an += dt
            out_rows.append({**g, "fingerprint": fp, "summary": summary(fp)})
        e = idx["models"][m]
        doc = {"schema": "tonematch.block-fingerprint-set", "schema_version": 1, "model": m,
               "category": e["category"], "library": lib.VERSION, "template": e["template"], "grid": GRID,
               "probe_version": probe.version, "probe_sha256": probe.synthetic_sha256,
               "native_version": idx.get("native_version"),
               "preroll_samples": nat.NativeEngine.TAIL_PREROLL if e["category"] in nat.TAIL_CATEGORIES
               else nat.NativeEngine.PREROLL,
               "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "settings": out_rows,
               "timing": {"render_s_per_setting_per_host": round(t_render, 3), "analysis_s": round(t_an, 2)}}
        (d / f"{m}.json").write_text(json.dumps(doc, default=float))
        per_model.append({"model": m, "category": e["category"], "settings": len(out_rows),
                          "failed": sum(r["fingerprint"] is None for r in out_rows),
                          "wall_s": round(time.perf_counter() - t0, 2)})

    try:
        for b0 in range(0, len(todo), a.batch):
            batch = todo[b0:b0 + a.batch]
            jobs, owners = [], []
            for m in batch:
                tpl = lib.template_for(m)
                for g in grid(m, catalog, tpl):
                    p = lib.block_preset(m, catalog)
                    p.slots[0].params.update(g["params"])
                    jobs.append(nat.RenderJob(dry=dry, preset=p))
                    owners.append((m, g))
            t0 = time.perf_counter()
            ys = pool.render_batch(jobs, on_error="none")
            t_b = time.perf_counter() - t0
            for i, err in pool.last_errors.items():
                errors[f"{owners[i][0]}|{owners[i][1]['setting']}"] = str(err)[:200]
            by_m: dict[str, list] = {}
            for (m, g), y, i in zip(owners, ys, range(len(ys)), strict=True):
                g = dict(g)
                if y is None:
                    g["error"] = str(pool.last_errors.get(i, "render failed"))[:200]
                fu = None if y is None else ex.submit(_analyse, (y, str(layout), {
                    "engine": "native", "model": m, "setting": g["setting"], "library": lib.VERSION}))
                by_m.setdefault(m, [[], []])
                by_m[m][0].append(g)
                by_m[m][1].append(fu)
            n_set = len(jobs)
            for m in batch:
                pending.append((m, by_m[m][0], by_m[m][1], t_b * pool.workers / max(n_set, 1), t0))
            while len(pending) > a.batch:  # keep analysis at most one batch behind rendering
                finish(pending.pop(0))
            done = len(per_model) + len(pending)
            el = time.perf_counter() - t_all
            print(json.dumps({"done": done, "of": len(todo), "elapsed_s": round(el, 1),
                              "s_per_model": round(el / done, 2), "batch_render_s": round(t_b, 1),
                              "mem_mb": round(pool.mem_mb()), "hosts_total": host_count()}), flush=True)
        while pending:
            finish(pending.pop(0))
        preroll = {e.preroll for e in pool.engines}
    finally:
        ex.shutdown(wait=True)
        pool.close()
    wall = time.perf_counter() - t_all
    write_index()
    return {"models": len(per_model), "wall_s": round(wall, 1),
            "s_per_model": round(wall / max(len(per_model), 1), 2), "hosts": hosts, "grid": GRID,
            "errors": errors, "skipped": skipped, "per_model": per_model, "prerolls_seen": sorted(preroll),
            "hosts_after": host_count()}


def write_index() -> Path:
    """``index.json``: model -> category, template, settings with their params and summaries."""
    d = out_dir()
    models = {}
    for f in sorted(d.glob("*.json")):
        if f.name == "index.json":
            continue
        doc = json.loads(f.read_text())
        models[doc["model"]] = {"category": doc["category"], "template": doc["template"], "grid": doc["grid"],
                                "settings": [{"setting": s["setting"], "params": s["params"], "cc": s["cc"],
                                              "summary": s["summary"]} for s in doc["settings"]]}
    p = d / "index.json"
    p.write_text(json.dumps({"schema": "tonematch.block-fingerprint-index", "schema_version": 1,
                             "library": lib.VERSION, "grid": GRID, "models": models}, indent=1, default=float))
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--hosts", type=int, default=3)
    r.add_argument("--max-hosts", type=int, default=6)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--models", default="")
    r.add_argument("--batch", type=int, default=6, help="models rendered per pool batch")
    r.add_argument("--analysis-workers", type=int, default=3)
    r.add_argument("--force", action="store_true")
    sub.add_parser("index")
    a = ap.parse_args()
    if a.cmd == "index":
        print(write_index())
        return 0
    res = cmd_run(a)
    runs = out_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    f = runs / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_run.json"
    f.write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps({k: v for k, v in res.items() if k not in ("per_model", "skipped")}, default=float))
    print("wrote", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
