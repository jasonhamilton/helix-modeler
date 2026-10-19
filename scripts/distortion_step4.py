#!/usr/bin/env python3
"""T181: step 4 of docs/notes/distortion-identification.md: identify a drive model and its Drive / Tone from
real guitar DI at random held-out settings, with the dfp-v1 fingerprint cards and curves.

Approved by the human ("go ahead", DECISIONS 2026-10-18 item 3). Measurement plan registered before any
number was computed: DECISIONS "T181: step 4 measurement plan". Measurement, not admission.

    plan      choose the 8 DI excerpts (4 Guitar-TECHS P1/P2, 4 of the human's own dry takes, none used in a
              rating session) and the 120 targets (random off-grid Drive / Tone); writes plan.json (no audio)
    render    on Native, at most --hosts (<= 3) leased from the host scaler, pausing above --max-load busy cores:
              targets   120 renders, audio kept in the small target cache (deleted by `clean`)
              truth     the dfp-v1 probe at each target's own setting -> fingerprint only (diagnostic)
              gallery   54 models x 12 step-3 settings x 8 excerpts; features on the fly (AFx-Rep embedding,
                        output-only features, mrstft_v2 against the targets that need it); no audio kept
    analyse   condition A (twin fit -> fingerprint -> knob maps) and B (output only), baselines, metrics
    ledger    benchmark / native / distortion-step4-dryknown and distortion-step4-blind rows
    clean     delete the target audio cache

    nice -n 10 .venv/bin/python scripts/distortion_step4.py plan
    nice -n 10 .venv/bin/python scripts/distortion_step4.py render --what targets,truth,gallery --hosts 3
    nice -n 10 .venv/bin/python scripts/distortion_step4.py analyse --workers 3
"""
from __future__ import annotations

import argparse
import json
import os
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

from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_step3 as S3
from tonematch.metrics import distortion_step4 as S4

SR = 48000
CACHE = ROOT / "cache" / "distortion-step4"
TARGET_DIR = CACHE / "targets"            # the small target audio cache (deleted by `clean`)
TRUTH_DIR = CACHE / "truth"
GALLERY_DIR = CACHE / "gallery"
OUT = ROOT / "reports" / "local" / "distortion-id" / "step4"
FP_INDEX = ROOT / "cache" / "distortion-fp" / "dfp-v1" / "index.json"
TAKES = ROOT / "data" / "songs" / "mother-playalong" / "takes"
GT = ROOT / "data" / "di" / "Guitar-TECHS"
KEYS = ROOT / "cache" / "ratings"
CAL_PLAN = ROOT / "cache" / "drive-specialist" / "cal_plan.json"
SEED = 181
EXC_S = 6.0
LEAD_S = 0.5
N_PER_EXCERPT = 15

# excerpt slots: id -> (source kind, selector, split, player)
SLOTS = [
    ("E1", "gt", "P1_scales", "dev", "P1"),
    ("E2", "gt", "P1_chords", "test", "P1"),
    ("E3", "gt", "P2_scales", "test", "P2"),
    ("E4", "gt", "P2_chords", "test", "P2"),
    ("E5", "human", "112935:1", "dev", "human"),
    ("E6", "human", "112935:2", "test", "human"),
    ("E7", "human", "112935:3", "test", "human"),
    ("E8", "human", "112611:1", "test", "human"),       # no 113158 window is free of rating excerpts
]
# condition-B mrstft_v2 baseline: each excerpt's partner from another player
PARTNER = {"E1": "E3", "E2": "E4", "E3": "E1", "E4": "E2", "E5": "E2", "E6": "E3", "E7": "E4", "E8": "E1"}

# CC steps of every measured setting (step 3 grid and centre line, T179 tone ends): targets avoid them
DRIVE_MEASURED = {6, 25, 64, 83, 108, 127}
TONE_MEASURED = {0, 13, 25, 44, 64, 102, 114, 127}


def read_mono(p: Path) -> np.ndarray:
    import soundfile as sf
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    assert sr == SR, (p, sr)
    return x.mean(axis=1).astype(np.float32)


# ------------------------------------------------------------------------------------------- plan

def _used_windows() -> dict[str, list[tuple[float, float]]]:
    """file -> [(start_s, end_s)] already used by a rating session or the drive-specialist calibration."""
    out: dict[str, list[tuple[float, float]]] = {}
    for v in ("v2", "v3", "v4"):
        p = KEYS / f"human-ratings-{v}.key.json"
        if p.exists():
            for t in json.loads(p.read_text())["targets"].values():
                f = str(TAKES / f"20261018-113158_take{int(t['take'])}_dry.wav")
                out.setdefault(f, []).append((t["excerpt_start_s"], t["excerpt_start_s"] + 7.0))
    if CAL_PLAN.exists():
        for d in json.loads(CAL_PLAN.read_text())["dis"]:
            f = str(ROOT / "data" / "di" / d["path"])
            out.setdefault(f, []).append((d["start_s"], d["start_s"] + 10.0))
    return out


def _windows(x: np.ndarray, n: int, hop_s: float = 0.5, min_peak_db: float = -26.0):
    import rating_v2 as R2
    hop = int(hop_s * SR)
    rows = []
    for s in range(int(LEAD_S * SR), len(x) - n + 1, hop):
        seg = x[s:s + n]
        if 20 * np.log10(np.max(np.abs(seg)) + 1e-12) < min_peak_db:
            continue
        rows.append((R2.window_score(x, s, n), s))
    return sorted(rows, reverse=True)


def choose_excerpts(rng) -> list[dict]:
    used = _used_windows()
    n = int(EXC_S * SR)
    out, taken = [], {}
    for sid, kind, sel, split, player in SLOTS:
        cands = []
        if kind == "gt":
            for f in sorted((GT / sel / "audio" / "directinput").glob("*.wav")):
                x = read_mono(f)
                for sc, s in _windows(x, n)[:3]:
                    cands.append((sc, str(f), s))
        else:
            tag, _ = sel.split(":")
            files = sorted(TAKES.glob(f"20261018-{tag}_take*_dry.wav"))
            for f in files:
                x = read_mono(f)
                for sc, s in _windows(x, n)[:40]:
                    cands.append((sc, str(f), s))
        cands.sort(reverse=True)
        pick = None
        for sc, f, s in cands[:60]:
            a, b = (s - LEAD_S * SR) / SR, (s + n) / SR
            clash = any(a < e and b > u for u, e in used.get(f, []) + taken.get(f, []))
            if not clash:
                pick = (sc, f, s)
                break
        if pick is None:
            raise SystemExit(f"no fresh window for {sid}")
        sc, f, s = pick
        taken.setdefault(f, []).append(((s - LEAD_S * SR) / SR, (s + n) / SR))
        x = read_mono(Path(f))[s:s + n]
        out.append({"id": sid, "source": kind, "group": sel, "split": split, "player": player,
                    "path": str(Path(f).relative_to(ROOT)), "start": int(s), "n": n, "score": float(sc),
                    "peak_dbfs": float(20 * np.log10(np.max(np.abs(x)) + 1e-12)),
                    "rms_dbfs": float(10 * np.log10(np.mean(x.astype(float) ** 2) + 1e-20))})
    return out


def pool_models(catalog=None) -> tuple[list[str], dict]:
    idx = json.loads(FP_INDEX.read_text())
    sem = idx["semantics"]
    return sorted(m for m in sem if m not in S4.EXCLUDED), sem


def sample_targets(rng, models: list[str], sem: dict, excerpts: list[dict]) -> list[dict]:
    n_t = N_PER_EXCERPT * len(excerpts)
    per = {m: 2 for m in models}
    for m in rng.choice(models, n_t - 2 * len(models), replace=False):
        per[str(m)] += 1
    slots = {e["id"]: N_PER_EXCERPT for e in excerpts}
    order = [m for m in models for _ in range(per[m])]
    for _ in range(2000):
        rng.shuffle(order)
        free = dict(slots)
        got: dict[str, list[str]] = {}
        ok = True
        for m in order:
            choices = [e for e, k in free.items() if k > 0 and e not in got.get(m, [])]
            if not choices:
                ok = False
                break
            e = choices[int(rng.integers(len(choices)))]
            free[e] -= 1
            got.setdefault(m, []).append(e)
        if ok:
            break
    else:
        raise RuntimeError("no target assignment")
    dcc = [c for c in range(128) if c not in DRIVE_MEASURED]
    tcc = [c for c in range(128) if c not in TONE_MEASURED]
    one = [c for c in range(128) if c not in set(S3.ONE_KNOB)]
    targets = []
    for m in models:
        for e in got[m]:
            s = sem[m]
            if s["tone"] is None:
                cc = {s["drive"]: int(rng.choice(one))}
            else:
                cc = {s["drive"]: int(rng.choice(dcc)), s["tone"]: int(rng.choice(tcc))}
            targets.append({"model": m, "excerpt": e, "cc": cc, "drive": s["drive"], "tone": s["tone"]})
    rng.shuffle(targets)
    targets.sort(key=lambda t: t["excerpt"])
    for i, t in enumerate(targets):
        t["id"] = f"t{i:03d}"
    return targets


def cmd_plan(a) -> dict:
    rng = np.random.default_rng(SEED)
    exc = choose_excerpts(rng)
    models, sem = pool_models()
    tg = sample_targets(rng, models, sem, exc)
    plan = {"version": "distortion-step4-v1", "seed": SEED, "excerpt_s": EXC_S, "lead_s": LEAD_S,
            "excluded_models": list(S4.EXCLUDED), "models": models, "excerpts": exc, "partner": PARTNER,
            "targets": tg, "drive_measured_cc": sorted(DRIVE_MEASURED), "tone_measured_cc": sorted(TONE_MEASURED)}
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "plan.json").write_text(json.dumps(plan, indent=1))
    return {"excerpts": [{k: e[k] for k in ("id", "path", "start", "peak_dbfs", "rms_dbfs", "split")} for e in exc],
            "targets": len(tg), "models": len(models)}


def load_plan() -> dict:
    return json.loads((CACHE / "plan.json").read_text())


def excerpt_audio(e: dict) -> np.ndarray:
    """The excerpt with its lead-in (rendered, then thrown away)."""
    x = read_mono(ROOT / e["path"])
    lead = int(LEAD_S * SR)
    return x[e["start"] - lead:e["start"] + e["n"]].astype(np.float32)


# ------------------------------------------------------------------------------------------- render

def _preset(lib, nat, catalog, model: str, cc: dict):
    tpl = lib.template_for(model)
    p = lib.block_preset(model, catalog)
    vals = {}
    for knob, c in cc.items():
        lo, hi = tpl.ranges.get(f"0:{knob}", (0.0, 1.0))
        vals[knob] = nat.cc_value(float(lo), float(hi), int(c))
    p.slots[0].params.update(vals)
    return p, vals


def _wait_load(guard, log) -> float:
    """Pause while the busy cores are above the guard's line (T193); returns the busy cores now."""
    guard.wait(lambda d: log(json.dumps({"pause_busy_cores": d})))
    return guard.last


def gallery_jobs(plan: dict, sem: dict) -> list[dict]:
    jobs = []
    for e in plan["excerpts"]:
        for m in plan["models"]:
            s = sem[m]
            for name, cc, kind in S3.settings_for(s["drive"], s["tone"]):
                jobs.append({"excerpt": e["id"], "model": m, "setting": name, "cc": cc, "kind": kind})
    return jobs


_W: dict = {}


def _worker_init():
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    try:
        import torch
        torch.set_num_threads(1)
    except ImportError:
        pass


def _target_spectra(tid: str):
    from tonematch.metrics import mrstft_v2 as M
    c = _W.setdefault("spec", {})
    if tid not in c:
        if len(c) > 40:
            c.clear()
        t = np.load(TARGET_DIR / f"{tid}.npy").astype(np.float64)
        cfg = M.DEFAULT
        t = t / M.level_scale(t, cfg.level)
        c[tid] = [M.spectrum(t, n, M.hop_for(n, cfg)) for n in cfg.fft_sizes]
    return c[tid]


def mrstft_vs(y: np.ndarray, tids: list[str]) -> dict[str, float]:
    from tonematch.metrics import mrstft_v2 as M
    cfg = M.DEFAULT
    y = np.asarray(y, np.float64)
    y = y / M.level_scale(y, cfg.level)
    ys = [M.spectrum(y, n, M.hop_for(n, cfg)) for n in cfg.fft_sizes]
    out = {}
    for tid in tids:
        ts = _target_spectra(tid)
        parts = [M.compare(t, c, n, cfg) for t, c, n in zip(ts, ys, cfg.fft_sizes)]
        out[tid] = M.combine(parts, cfg)
    return out


def gallery_features(y: np.ndarray, tids_a: list[str], tids_b: list[str]) -> dict:
    """Worker: AFx-Rep embedding, output-only features (raw LTAS kept; the reference is subtracted at
    analysis), mrstft_v2 against the condition-A and condition-B targets that use this render."""
    from tonematch.metrics import afxrep
    y = np.asarray(y, np.float32)
    emb = afxrep.get_embedder().embed(y).mid.astype(np.float32)
    bf = S4.blind_features(y, None)
    return {"afx": emb, "blind": {k: np.asarray(v, float).tolist() for k, v in bf.items()},
            "mr_a": mrstft_vs(y, tids_a), "mr_b": mrstft_vs(y, tids_b)}


def truth_job(y: np.ndarray, path: str) -> dict:
    return S3.fingerprint_job(y, path)


def cmd_render(a) -> dict:
    import concurrent.futures as cf
    import multiprocessing as mp

    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    log = lambda s: print(s, flush=True)
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    plan = load_plan()
    _, sem = pool_models()
    catalog = load_catalog()
    what = a.what.split(",")
    exc = {e["id"]: e for e in plan["excerpts"]}
    audio = {k: excerpt_audio(e) for k, e in exc.items()}
    lead = int(LEAD_S * SR)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    stats = {"renders": 0, "failed": 0, "busy_max": 0.0, "hosts_seen": set()}
    t_all = time.perf_counter()
    pool = nat.NativeRenderPool(workers=min(a.hosts, 3), templates_dir=lib.library_templates())
    ex = cf.ProcessPoolExecutor(max_workers=a.workers, mp_context=mp.get_context("spawn"), initializer=_worker_init)
    try:
        if "targets" in what:
            todo = [t for t in plan["targets"] if not (TARGET_DIR / f"{t['id']}.npy").exists()]
            for b0 in range(0, len(todo), a.batch):
                grp = todo[b0:b0 + a.batch]
                jobs = []
                for t in grp:
                    p, vals = _preset(lib, nat, catalog, t["model"], t["cc"])
                    t["params"] = vals
                    jobs.append(nat.RenderJob(dry=audio[t["excerpt"]], preset=p))
                ys = pool.render_batch(jobs, on_error="none")
                for t, y in zip(grp, ys, strict=True):
                    stats["renders"] += 1
                    if y is None:
                        stats["failed"] += 1
                        continue
                    np.save(TARGET_DIR / f"{t['id']}.npy", np.asarray(y, np.float32)[lead:])
                stats["hosts_seen"].add(len(pool.engines))
                stats["busy_max"] = max(stats["busy_max"], _wait_load(guard, log))
                log(json.dumps({"targets": b0 + len(grp), "of": len(todo), "hosts": len(pool.engines),
                                "s": round(time.perf_counter() - t_all, 1), "busy": round(guard.last, 2)}))
        if "truth" in what:
            sig, _ = P.build_probe("v1")
            sig32 = sig.astype(np.float32)
            todo = [t for t in plan["targets"] if not (TRUTH_DIR / f"{t['id']}.npz").exists()]
            pending = {}
            for b0 in range(0, len(todo), max(1, a.batch // 3)):
                grp = todo[b0:b0 + max(1, a.batch // 3)]
                jobs = [nat.RenderJob(dry=sig32, preset=_preset(lib, nat, catalog, t["model"], t["cc"])[0]) for t in grp]
                ys = pool.render_batch(jobs, on_error="none")
                for f in list(pending):
                    f.result()
                pending = {}
                for t, y in zip(grp, ys, strict=True):
                    stats["renders"] += 1
                    if y is None:
                        stats["failed"] += 1
                        continue
                    pending[ex.submit(truth_job, np.asarray(y, np.float32), str(TRUTH_DIR / f"{t['id']}.npz"))] = t
                stats["busy_max"] = max(stats["busy_max"], _wait_load(guard, log))
                log(json.dumps({"truth": b0 + len(grp), "of": len(todo), "hosts": len(pool.engines),
                                "s": round(time.perf_counter() - t_all, 1), "busy": round(guard.last, 2)}))
            for f in pending:
                f.result()
        if "gallery" in what:
            jobs_all = gallery_jobs(plan, sem)
            tg_a = {}
            tg_b = {}
            for t in plan["targets"]:
                tg_a.setdefault(t["excerpt"], []).append(t["id"])
                tg_b.setdefault(PARTNER[t["excerpt"]], []).append(t["id"])
            done = set()
            for f in GALLERY_DIR.glob("*.json"):
                for r in json.loads(f.read_text()):
                    done.add((r["excerpt"], r["model"], r["setting"]))
            todo = [j for j in jobs_all if (j["excerpt"], j["model"], j["setting"]) not in done]
            log(json.dumps({"gallery_jobs": len(jobs_all), "todo": len(todo)}))
            pending: dict = {}
            part = len(list(GALLERY_DIR.glob("*.json")))

            def flush(pending, part):
                rows = []
                for fu, j in pending.items():
                    r = fu.result()
                    rows.append({**j, "mr_a": r["mr_a"], "mr_b": r["mr_b"], "blind": r["blind"]})
                    np.save(GALLERY_DIR / f"{j['excerpt']}__{j['model']}__{j['setting'].replace(',', '_')}.afx.npy",
                            r["afx"])
                if rows:
                    (GALLERY_DIR / f"part{part:04d}.json").write_text(json.dumps(rows))
                return part + (1 if rows else 0)
            for b0 in range(0, len(todo), a.batch):
                grp = todo[b0:b0 + a.batch]
                rjobs = [nat.RenderJob(dry=audio[j["excerpt"]], preset=_preset(lib, nat, catalog, j["model"], j["cc"])[0])
                         for j in grp]
                ys = pool.render_batch(rjobs, on_error="none")
                part = flush(pending, part)
                pending = {}
                for j, y in zip(grp, ys, strict=True):
                    stats["renders"] += 1
                    if y is None:
                        stats["failed"] += 1
                        continue
                    e = j["excerpt"]
                    pending[ex.submit(gallery_features, np.asarray(y, np.float32)[lead:], tg_a.get(e, []),
                                      tg_b.get(e, []))] = j
                stats["hosts_seen"].add(len(pool.engines))
                stats["busy_max"] = max(stats["busy_max"], _wait_load(guard, log))
                log(json.dumps({"gallery": b0 + len(grp), "of": len(todo), "hosts": len(pool.engines),
                                "s": round(time.perf_counter() - t_all, 1), "busy": round(guard.last, 2)}))
            part = flush(pending, part)
    finally:
        pool.close()
        ex.shutdown(wait=True, cancel_futures=True)
    stats["hosts_seen"] = sorted(stats["hosts_seen"])
    stats["wall_s"] = round(time.perf_counter() - t_all, 1)
    runs = CACHE / "runs.json"
    hist = json.loads(runs.read_text()) if runs.exists() else []
    hist.append({**stats, "what": what, "hosts": a.hosts, "workers": a.workers})
    runs.write_text(json.dumps(hist, indent=1))
    return stats


def cmd_clean(a) -> dict:
    n = 0
    for f in TARGET_DIR.glob("*.npy"):
        f.unlink()
        n += 1
    return {"deleted_target_clips": n}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["plan", "render", "analyse", "ledger", "clean"])
    ap.add_argument("--what", default="targets,truth,gallery")
    ap.add_argument("--hosts", type=int, default=3)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--max-load", type=float, default=None,
                    help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--only", default="", choices=["", "B"], help="analyse: recompute condition B only, reusing "
                    "the condition-A rows of the last run")
    a = ap.parse_args(argv)
    a.hosts = min(a.hosts, 3)
    a.workers = min(a.workers, 4)
    if a.cmd == "plan":
        print(json.dumps(cmd_plan(a), indent=1))
    elif a.cmd == "render":
        print(json.dumps(cmd_render(a)))
    elif a.cmd == "clean":
        print(json.dumps(cmd_clean(a)))
    elif a.cmd == "analyse":
        import distortion_step4_analysis as AN
        print(json.dumps(AN.run(a), default=str)[:4000])
    elif a.cmd == "ledger":
        import distortion_step4_analysis as AN
        return AN.ledger(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
