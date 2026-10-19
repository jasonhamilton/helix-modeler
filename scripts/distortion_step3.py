#!/usr/bin/env python3
"""T174: step 3 of docs/notes/distortion-identification.md, the dfp-v1 fingerprints of every drive.

Approved by the human ("go ahead", DECISIONS 2026-10-18): 6 s sweeps; the tone staircase from about
-90 dBFS (-91 to -1 in 5 dB steps); Kinky Boost measured as it is; the "not a simple three-stage
circuit" flag dropped; a quick check that Scream 808's Tone reaches Native first; unmapped switches at
their defaults; all drive models of match_block.category_pool('drive') at 12 settings each.
Nothing is adopted: no class list, score or bar comes from this.

    tonecheck  Scream 808 Tone 0 vs 1 at fixed Gain: template + CC against a controller-free drop of
               the same values (the T103 truth route), and the spectra of both routes
    plan       the settings (12 per model) and the knob semantics
    run        render every setting on Native (at most --hosts, leased from the host scaler) and
               fingerprint it in at most --workers processes; fingerprints only, no audio
    analyse    leave-one-setting-out identification, clusters vs circuit classes, fingerprint cards,
               figures (reports/local/distortion-id/step3/)
    ledger     append the benchmark / native / distortion-fp-v1 rows from the summary

    nice -n 10 .venv/bin/python scripts/distortion_step3.py run --hosts 5 --workers 4
    .venv/bin/python scripts/distortion_step3.py analyse --workers 4
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)
os.environ.setdefault("TONEMATCH_NATIVE_AUTOCAPTURE", "0")

from tonematch.metrics import distortion_probe as P  # noqa: E402
from tonematch.metrics import distortion_step3 as S3  # noqa: E402

FP_DIR = ROOT / "cache" / "distortion-fp" / "dfp-v1"
OUT = ROOT / "reports" / "local" / "distortion-id" / "step3"
DATASET = "distortion-fp-v1"


# ------------------------------------------------------------------------------------- tone check

def _tone_signal():
    """DI clip (2 s), then a 3 s sweep at -50 dBFS and one at -12 dBFS, each with a 1 s gap."""
    from tonematch.metrics import distortion_id as D
    import soundfile as sf

    di, sr = sf.read(str(ROOT / "fixtures" / "smoke" / "di_2s_real.wav"), dtype="float64", always_2d=True)
    assert sr == P.SR
    di = di.mean(axis=1)
    x, L, _ = D.ess_sync(P.F1, P.F2, 3.0, P.SR)
    gap = np.zeros(P.SR)
    parts, segs, pos = [], {}, 0
    for name, a in (("di", di), ("gap0", np.zeros(P.SR // 2)), ("sw50", np.concatenate([10 ** (-50 / 20) * x, gap])),
                    ("sw12", np.concatenate([10 ** (-12 / 20) * x, gap]))):
        segs[name] = (pos, len(a))
        parts.append(a)
        pos += len(a)
    return np.concatenate(parts), segs, x, L


def _tone_spectra(y, segs, x, L):
    from tonematch.metrics import distortion_id as D

    out = {}
    recs = {lv: y[segs[k][0]:segs[k][0] + segs[k][1]] for k, lv in (("sw50", -50.0), ("sw12", -12.0))}
    lev, M = D.ess_harmonic_table(recs, x, L, P.SR, P.F1, P.F2, P.FGRID, orders=3)
    out["ss_db"] = M[1][:, 0] - lev[0]
    out["hot_db"] = M[1][:, 1] - lev[1]
    d = y[segs["di"][0]:segs["di"][0] + segs["di"][1]]
    f = np.fft.rfftfreq(8192, 1 / P.SR)
    fr = [np.abs(np.fft.rfft(d[i:i + 8192] * np.hanning(8192))) ** 2 for i in range(0, d.size - 8192, 4096)]
    out["di_ltas_db"] = D.band_mag_db(f, np.sqrt(np.mean(fr, 0)), P.FGRID, 1 / 3)
    return out


def cmd_tonecheck(a) -> dict:
    import native_verify as NV
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    t0 = time.perf_counter()
    cat = load_catalog()
    sig, segs, x, L = _tone_signal()
    sig32 = sig.astype(np.float32)
    m = "HD2_DistScream808"
    tpl = lib.template_for(m)
    mapping = {"map": {k: v for k, v in tpl.map.items()}, "ranges": tpl.ranges, "verified": list(tpl.verified),
               "cc_live": {k: v for k, v in json.loads((nat.cache_dir() / "cc_live.json").read_text()).items()
                           if k.startswith(m + "|")}}
    items = []
    level_cc = NV.grid_cc(lib.block_preset(m, cat), tpl)["0:Level"]   # Level on its CC grid (T103 audit)
    for g in (25, 64, 108):
        for t in (0, 25, 64, 102, 127):
            if g != 64 and t not in (0, 127):
                continue
            cc = {"0:Gain": g, "0:Tone": t, "0:Level": level_cc}
            items.append({"gain_cc": g, "tone_cc": t, "preset": NV.apply_cc(lib.block_preset(m, cat), tpl, cc)})
    # the legacy Screamer and Stupor OD on the CC route only, for scale
    refs = []
    for rm, (dk, tk) in (("HD2_DM4Screamer", ("Drive", "Tone")), ("HD2_DistStuporOD", ("Drive", "Tone"))):
        rt = lib.template_for(rm)
        for t in (0, 127):
            refs.append({"model": rm, "tone_cc": t,
                         "preset": NV.apply_cc(lib.block_preset(rm, cat), rt, {f"0:{dk}": 64, f"0:{tk}": t})})
    plugin, start = NV.plugin_and_start()
    d = OUT / "tonecheck_drops"
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.hlx"):
        f.unlink()
    paths = []
    for i, it in enumerate(items):
        f = d / f"{i:02d}_g{it['gain_cc']}_t{it['tone_cc']}.hlx"
        f.write_text(json.dumps(NV.hlx_doc(it["preset"]), indent=1))
        paths.append(f)
    states, sessions = NV.drop_states(paths, plugin, start, max_sessions=3)
    for f in paths:          # the drop files are the user's catalog values written out: local, removed
        f.unlink()
    d.rmdir()
    rt_, rd = NV.Renderer("template"), NV.Renderer("truth")
    try:
        rt_.eng.load(items[0]["preset"])
        rd.eng._plugin_latency = rt_.eng.latency()
        rows = []
        for it, st in zip(items, states, strict=True):
            y, stem = rt_.preset(it["preset"], sig32)
            row = {"gain_cc": it["gain_cc"], "tone_cc": it["tone_cc"], "template": stem,
                   "tone_value": it["preset"].slots[0].params["Tone"], "gain_value": it["preset"].slots[0].params["Gain"]}
            sp = _tone_spectra(np.asarray(y, float), segs, x, L)
            row["cc_route"] = sp
            if isinstance(st, bytes):
                z = rd.state(st, nat.chain_key(it["preset"]), sig32)
                row["truth_null_db"] = round(float(nat.null_db(np.asarray(z, float), np.asarray(y, float))), 1)
                row["truth"] = _tone_spectra(np.asarray(z, float), segs, x, L)
            else:
                row["truth_error"] = str(st)
            rows.append(row)
        ref_rows = []
        for it in refs:
            y, stem = rt_.preset(it["preset"], sig32)
            ref_rows.append({"model": it["model"], "tone_cc": it["tone_cc"],
                             **_tone_spectra(np.asarray(y, float), segs, x, L)})
        stats = NV.engine_drifts([rt_, rd])
    finally:
        rt_.close()
        rd.close()
    res = S3.tone_reach_summary(rows, ref_rows, P.FGRID)
    res["level_cc"] = level_cc
    res["curves_gain64"] = {str(r["tone_cc"]): {k: [float(v) for v in r[route][k]] for route in ("cc_route", "truth")
                                                if route in r for k in ("ss_db",)} for r in rows if r["gain_cc"] == 64}
    res["curves_route"] = {str(r["tone_cc"]): {route: [float(v) for v in r[route]["ss_db"]]
                                               for route in ("cc_route", "truth") if route in r}
                           for r in rows if r["gain_cc"] == 64}
    res["fgrid"] = [float(v) for v in P.FGRID]
    res.update({"model": m, "template_mapping": mapping, "drop_sessions": sessions, "engine_stats": stats,
                "wall_s": round(time.perf_counter() - t0, 1)})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tonecheck.json").write_text(json.dumps(P._clean(res), indent=1))
    _tone_figure(res)
    return res


def _tone_figure(res: dict):
    """From tonecheck.json: Scream 808 small-signal response per Tone at Gain 0.5, both routes."""
    import distortion_id_steps01 as S01

    f = np.asarray(res["fgrid"])
    cols = {"0": "#1f77b4", "25": "#17becf", "64": "#2ca02c", "102": "#ff7f0e", "127": "#d62728"}
    ser = []
    for t, routes in res["curves_route"].items():
        for route, dashed in (("cc_route", False), ("truth", True)):
            if route in routes:
                v = np.asarray(routes[route])
                ser.append((f, v - np.interp(0.0, np.log(f / 1000), v),
                            f"Tone@{t} ({'CC route' if route == 'cc_route' else 'file truth'})",
                            cols.get(t, "#7f7f7f"), dashed))
    S01.svg_lines(OUT / "tonecheck_scream808.svg", ser, "Scream 808 Tone at Gain 0.5: small-signal response "
                  "(-50 dBFS sweep, 0 dB at 1 kHz); CC route solid, file truth dashed", "frequency (Hz)", "dB",
                  logx=True, ylim=(-40, 15), w=900)


# ------------------------------------------------------------------------------------- plan / run

def build_plan(catalog=None) -> tuple[list[dict], dict]:
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as lib

    catalog = catalog or load_catalog()
    pool = MB.category_pool("drive", catalog)
    sem, jobs = {}, []
    for m in sorted(pool):
        tpl = lib.template_for(m)
        k = S3.knob_semantics(m, catalog["models"].get(m, {}).get("params", {}), tpl.map)
        sem[m] = k
        for name, cc, kind in S3.settings_for(k["drive"], k["tone"]):
            jobs.append({"model": m, "setting": name, "cc": cc, "kind": kind})
    for m, name in S3.REPEATS:
        j = next(j for j in jobs if j["model"] == m and j["setting"] == name)
        jobs.append({**j, "kind": "repeat", "tag": "__rep"})
    return jobs, sem


def _fp_path(j) -> Path:
    return FP_DIR / j["model"] / f"{j['setting'].replace(',', '_').replace(' ', '')}{j.get('tag', '')}.npz"


def cmd_plan(a) -> dict:
    jobs, sem = build_plan()
    return {"models": len(sem), "renders": len(jobs), "semantics": sem,
            "per_model": {m: sum(j["model"] == m and j["kind"] != "repeat" for j in jobs) for m in sem}}


def cmd_run(a) -> dict:
    import concurrent.futures as cf
    import multiprocessing as mp

    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib
    from tonematch.engines.host_budget import LoadGuard

    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"       # each analysis worker on one core (inherited by the spawned workers)
    catalog = load_catalog()
    jobs, sem = build_plan(catalog)
    FP_DIR.mkdir(parents=True, exist_ok=True)
    idx_path = FP_DIR / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.is_file() else {"version": "dfp-v1", "settings": []}
    done = {(s["model"], s["setting"], s.get("tag", "")) for s in index["settings"] if "file" in s}
    todo = [j for j in jobs if (j["model"], j["setting"], j.get("tag", "")) not in done]
    if a.limit:
        todo = todo[:a.limit]
    index["semantics"] = sem
    sig, lay = P.build_probe("v1")
    sig32 = sig.astype(np.float32)
    t_all = time.perf_counter()
    print(json.dumps({"jobs": len(jobs), "todo": len(todo), "probe_s": round(sig.size / P.SR, 1)}), flush=True)
    render_s = analysis_s = 0.0
    guard = LoadGuard(a.max_load)
    load_max = 0.0
    pool = nat.NativeRenderPool(workers=a.hosts, templates_dir=lib.library_templates())
    hosts_seen = [len(pool.engines)]
    keep_raw = {}
    pending: dict = {}

    def collect(futs, index) -> float:
        t = time.perf_counter()
        for fu in cf.as_completed(futs):
            j = futs[fu]
            r = fu.result()
            rec = {k: j[k] for k in ("model", "setting", "cc", "kind", "params", "fixed")}
            rec["tag"] = j.get("tag", "")
            for k in ("repeat_bit_identical", "repeat_max_abs_diff"):
                if k in j:
                    rec[k] = j[k]
            rec.update({"file": str(Path(r["file"]).relative_to(ROOT)), "bytes": r["bytes"],
                        "analysis_s": r["analysis_s"]})
            index["settings"].append(rec)
        return time.perf_counter() - t

    ex = cf.ProcessPoolExecutor(max_workers=a.workers, mp_context=mp.get_context("spawn"))
    try:
        for b0 in range(0, len(todo), a.batch):
            grp = todo[b0:b0 + a.batch]
            rjobs = []
            for j in grp:
                tpl = lib.template_for(j["model"])
                p = lib.block_preset(j["model"], catalog)
                vals = {}
                for knob, c in j["cc"].items():
                    lo, hi = tpl.ranges.get(f"0:{knob}", (0.0, 1.0))
                    vals[knob] = nat.cc_value(float(lo), float(hi), int(c))
                p.slots[0].params.update(vals)
                j["params"] = vals
                j["fixed"] = {k: v for k, v in p.slots[0].params.items()
                              if k not in vals and not str(k).startswith("@")}
                rjobs.append(nat.RenderJob(dry=sig32, preset=p))
            t0 = time.perf_counter()
            ys = pool.render_batch(rjobs, on_error="none")
            render_s += time.perf_counter() - t0
            hosts_seen.append(len(pool.engines))
            t1 = time.perf_counter()
            futs = {}
            for j, y in zip(grp, ys, strict=True):
                if y is None:
                    index["settings"].append({**{k: j[k] for k in ("model", "setting", "cc", "kind")},
                                              "tag": j.get("tag", ""), "error": "render failed"})
                    continue
                key = (j["model"], j["setting"])
                if key in {tuple(r) for r in S3.REPEATS}:
                    if j.get("tag") == "__rep" and key in keep_raw:
                        y0 = keep_raw.pop(key)
                        j["repeat_bit_identical"] = bool(np.array_equal(np.asarray(y), y0))
                        j["repeat_max_abs_diff"] = float(np.max(np.abs(np.asarray(y, float) - y0)))
                    elif not j.get("tag"):
                        keep_raw[key] = np.asarray(y, float).copy()
                futs[ex.submit(S3.fingerprint_job, np.asarray(y, np.float32), str(_fp_path(j)))] = j
            # pipeline: this batch analyses while the next one renders; collect the previous batch now
            analysis_s += collect(pending, index)
            pending = futs
            analysis_s += time.perf_counter() - t1
            ld = guard.demand()
            load_max = max(load_max, ld)
            idx_path.write_text(json.dumps(P._clean(index), indent=1))
            print(json.dumps({"done": b0 + len(grp), "of": len(todo), "hosts": len(pool.engines),
                              "elapsed_s": round(time.perf_counter() - t_all, 1), "busy": round(ld, 2)}), flush=True)
            guard.wait()          # back off: let the other agents' renders through
        t1 = time.perf_counter()
        analysis_s += collect(pending, index) + (time.perf_counter() - t1)
        pending = {}
    finally:
        pool.close()
        ex.shutdown(wait=True, cancel_futures=True)
    runs = index.setdefault("runs", [])
    runs.append({"wall_s": round(time.perf_counter() - t_all, 1), "render_s": round(render_s, 1),
                 "analysis_s": round(analysis_s, 1), "renders": len(todo), "hosts_seen": sorted(set(hosts_seen)),
                 "busy_max": round(load_max, 2), "workers": a.workers})
    index["renders"] = sum(1 for s in index["settings"] if "file" in s)
    index["failed"] = sum(1 for s in index["settings"] if "error" in s)
    idx_path.write_text(json.dumps(P._clean(index), indent=1))
    return {"renders": index["renders"], "failed": index["failed"], **runs[-1]}


# ------------------------------------------------------------------------------------- analyse

def cmd_analyse(a) -> dict:
    t0 = time.perf_counter()
    idx = json.loads((FP_DIR / "index.json").read_text())
    res = S3.analyse_all(ROOT, idx, workers=a.workers)
    res["wall_s"] = round(time.perf_counter() - t0, 1)
    out = Path(a.out)
    tc = out / "tonecheck.json"
    if tc.is_file():
        t = json.loads(tc.read_text())
        res["tonecheck"] = {k: t.get(k) for k in ("worst_truth_null_db", "truth_tone_effect_max_db", "reaches",
                                                  "per_gain", "refs", "level_cc")}
    out.mkdir(parents=True, exist_ok=True)
    S3.write_figures(ROOT, idx, res, out)
    (out / "summary.json").write_text(json.dumps(P._clean(res), indent=1))
    (out / "cards.md").write_text(S3.cards_markdown(res))
    return res


def cmd_ledger(a) -> int:
    res = json.loads((Path(a.out) / "summary.json").read_text())
    idx = json.loads((FP_DIR / "index.json").read_text())
    run = (idx.get("runs") or [{}])[-1]
    wall = sum(r.get("wall_s", 0) for r in idx.get("runs", []))
    renders = idx.get("renders")
    base = (f"T174 step 3 (human 'go ahead' 2026-10-18); {res['n_models']} drives x 12 settings on Native "
            f"single-block templates; dfp-v1 probe {res['probe_s']} s (6 s sweeps, staircase -91..-1 dBFS); "
            f"hosts {run.get('hosts_seen')}, {run.get('workers')} analysis workers; nothing adopted; ")
    rows = [("valid_fingerprints", res["n_fingerprints"], renders, wall, base + "fingerprints only, cache/distortion-fp/dfp-v1"),
            ("repeat_bit_identical", res["repeats"]["identical"], res["repeats"]["n"], None, base + "repeat renders")]
    for fs, v in res["loso"].items():
        rows.append((f"loso_top1_{fs}", v["top1"][0], v["n"], None,
                     base + f"leave-one-setting-out model top-1, features {fs} ({v['dims']} dims), 95% CI "
                     f"{v['top1'][1]:.2f}-{v['top1'][2]:.2f}; top-5 {v['top5'][0]:.3f}; coarse-v1 top-1 0.34/0.59/0.75"))
        rows.append((f"loso_top5_{fs}", v["top5"][0], v["n"], None, base + f"top-5, features {fs}"))
    for fs, v in res["loso_coarse_like"].items():
        rows.append((f"loso_coarse_like_top1_{fs}", v["top1"][0], v["n"], None,
                     base + f"like-for-like with coarse-v1: only the 2x2 corners + centre per model (one-knob: Drive "
                     f"0.2/0.5/0.85), features {fs}; 95% CI {v['top1'][1]:.2f}-{v['top1'][2]:.2f}; top-5 "
                     f"{v['top5'][0]:.3f}; coarse-v1 top-1 0.34 (6 scalars) / 0.59 (all scalars) / 0.75 (curves)"))
    mo = res["monotone"]
    for k in ("g0_rises", "knee_falls", "onset_falls"):
        rows.append((f"monotone_{k}", mo[k][0] / max(1, mo[k][1]), mo[k][1], None,
                     base + f"Drive series where {k.replace('_', ' ')}: {mo[k][0]}/{mo[k][1]}; exceptions "
                     f"{mo['not_' + k.split('_')[0]][:8]}"))
    for k, v in res["clusters"]["rows"].items():
        rows.append((f"ari_{k.replace(':', '_')}", v["ari"], None, None, base + f"ARI {k}; random 95th pct {v['null95']:.3f}; "
                     "labels: distortion-circuits.md groups/family and the provisional 6-way class (not approved)"))
    rows.append(("tone_reach_scream808_db", res.get("tonecheck", {}).get("truth_tone_effect_max_db") or 0.0, None, None,
                 base + "Scream 808 Tone 0 vs 1 max small-signal difference on the file-truth route; "
                 f"CC vs truth worst null {res.get('tonecheck', {}).get('worst_truth_null_db')} dB"))
    for metric, value, n, w, notes in rows:
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "native",
               "--dataset", DATASET, "--metric", metric, "--value", str(round(float(value), 6)), "--notes", notes]
        if n is not None:
            cmd += ["--renders", str(int(n))]
        if w:
            cmd += ["--wall-clock-s", str(round(w, 1))]
        subprocess.run(cmd, check=True, env={**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")})
    print(f"{len(rows)} ledger rows")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["tonecheck", "plan", "run", "analyse", "ledger"])
    ap.add_argument("--hosts", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0, help="render at most this many settings (shakedown)")
    ap.add_argument("--max-load", type=float, default=None,
                    help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    a.hosts = min(a.hosts, 5)
    a.workers = min(a.workers, 4)
    if a.cmd == "tonecheck":
        r = cmd_tonecheck(a)
        print(json.dumps({k: r[k] for k in r if k not in ("rows", "refs", "template_mapping")}, default=str)[:3000])
        return 0
    if a.cmd == "plan":
        r = cmd_plan(a)
        print(json.dumps(r, indent=1))
        return 0
    if a.cmd == "run":
        print(json.dumps(cmd_run(a)))
        return 0
    if a.cmd == "ledger":
        return cmd_ledger(a)
    r = cmd_analyse(a)
    print(json.dumps({"loso": {k: {"top1": v["top1"][0], "top5": v["top5"][0]} for k, v in r["loso"].items()},
                      "clusters": {k: v["ari"] for k, v in r["clusters"]["rows"].items()}, "wall_s": r["wall_s"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
