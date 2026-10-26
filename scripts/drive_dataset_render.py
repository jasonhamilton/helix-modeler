#!/usr/bin/env python3
"""drive-dataset-v1 (T191, learning): unattended single-drive dataset render on Helix Native, features only, then a
retrain and evaluation of the T189 (predictor-pilot-v1) heads. A measurement only: nothing is adopted.

    .venv/bin/python scripts/drive_dataset_render.py plan             # sample the dataset (seeded) -> plan.json
    .venv/bin/python scripts/drive_dataset_render.py render --hours 18  # supervisor; <= 4 Native hosts, load guard
    .venv/bin/python scripts/drive_dataset_render.py train            # retrain + evaluate, summary.{json,md}
    .venv/bin/python scripts/drive_dataset_render.py all              # plan if needed -> render -> train -> proc_check
    scripts/overnight_drive.sh                                        # the human's one command (nohup, nice, caffeinate)

Data. Every searchable drive model (match_block.category_pool('drive') minus the six flagged fuzzes of T181) x
random settings (every free knob uniform on the CC grid; level-type knobs at their default) x Guitar-TECHS P1/P2 DI
files from data/di/manifest.json, random 10 s excerpts that pass an activity check. P3 is never read; no human takes
and no song audio. Split by whole DI file:
  test    about 20 % of the files, drawn only from files the T186/T189 index never used (new held-out recordings);
  v1test  the T186/T189 test files: rendered for evaluation only, never trained on (keeps the v1 test split clean);
  cal     the T186/T189 calibration files;
  train   every other P1/P2 file.
Features, then the audio is dropped: frozen AFx-Rep `mid` embeddings, the 6 s centre crop and three 3.33 s crops
(the T189 multi-crop format), on CPU with 2 threads. Shards (float16 .npy + labels .jsonl) in <out>/shards/.

Robustness. The render is an elastic job (tonematch.engines.elastic): a supervisor asks the host scaler
(host_budget) for up to 4 hosts and keeps that many workers; each worker claims one shard at a time
(atomic claim), writes its files atomically and its shard JSON last (the JSON means "done"), so any rerun resumes.
Before each clip: pause while the busy cores (CPU utilisation x cores, smoothed over 3 samples; T192) are above
--max-load (default 0.95 x cores) and resume below 0.85 x cores; the 1-minute load average is only a backstop
(above 1.2 x cores). Before each shard: stop cleanly when free
disk is below --min-free-gb (8.5). A Native error or silent output is retried once, then the clip is skipped and
logged. SIGTERM: the workers finish their current shard and close their hosts; the supervisor exits.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SR = 48_000
CLIP_S = 10.0
NCROP = 3
SINGLE_S = 6.0
OUT_DEFAULT = ROOT / "cache/drive-dataset-v1"
MODELS_DEFAULT = ROOT / "cache/models/predictor-dataset-v1"
V0 = ROOT / "cache/models/predictor-pilot-v0"
V1 = ROOT / "cache/models/predictor-pilot-v1"
MANIFEST = ROOT / "data/di/manifest.json"
DI_ROOT = ROOT / "data/di"
SEED = 191
SHARD_SIZE = 100
MAX_HOSTS = 4
LEVEL_NAMES = {"Level", "Output", "Volume", "FuzzLevel", "ODLevel", "Level 1", "Level 2"}
SPLIT_WEIGHT = {"train": 1.0, "cal": 0.5, "test": 0.4, "v1test": 0.25}
DATASET = "predictor-dataset-v1"
LABEL_KEYS = ("id", "model", "family", "cc", "di_file", "start", "player", "split", "drive", "tone")


# ------------------------------------------------------------------------------------------ small helpers

def player_of(path: str) -> str:
    parts = path.split("/")
    return parts[1][:2] if len(parts) > 1 else ""


def free_gb(path: Path) -> float:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def log_line(out: Path, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(Path(out) / "render.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def npy_bytes(a: np.ndarray) -> bytes:
    b = io.BytesIO()
    np.save(b, a)
    return b.getvalue()


# ------------------------------------------------------------------------------------------ plan

def eligible_files(manifest: dict) -> list[str]:
    """P1/P2 Guitar-TECHS DI files of the manifest. P3 is never returned."""
    files = sorted({c["path"] for c in manifest["clips"]})
    return [f for f in files if player_of(f) in ("P1", "P2") and "/P3" not in f and f.startswith("Guitar-TECHS/")]


def v1_splits(index_path: Path = V0 / "index.json") -> dict[str, str]:
    """di_file -> split of the T186/T189 index (train / cal / test); {} when it is not on disk."""
    if not Path(index_path).is_file():
        return {}
    out = {}
    for r in json.loads(Path(index_path).read_text())["records"]:
        out[r["di_file"]] = r["split"]
    return out


def assign_file_splits(files: list[str], v1: dict[str, str], rng: np.random.Generator, test_frac: float = 0.2) -> dict:
    """file -> split. Test files: about test_frac of all files, from files v1 never used, balanced by player."""
    unused = [f for f in files if f not in v1]
    n_test = min(len(unused), int(round(test_frac * len(files))))
    by_p = {p: sorted(f for f in unused if player_of(f) == p) for p in ("P1", "P2")}
    for p in by_p:
        rng.shuffle(by_p[p])
    test: list[str] = []
    while len(test) < n_test and (by_p["P1"] or by_p["P2"]):
        for p in ("P1", "P2"):
            if by_p[p] and len(test) < n_test:
                test.append(by_p[p].pop())
    out = {}
    for f in files:
        s = v1.get(f)
        if f in test:
            out[f] = "test"
        elif s == "test":
            out[f] = "v1test"
        elif s == "cal":
            out[f] = "cal"
        else:
            out[f] = "train"
    return out


def active_starts(x: np.ndarray, sr: int = SR, clip_s: float = CLIP_S, hop_s: float = 0.5,
                  min_active: float = 0.6, min_onsets: int = 3) -> list[int]:
    """Start samples of clip_s windows with real playing: >= min_active of 100 ms frames above an activity floor
    (max(-60 dBFS, the file's 90th percentile frame level - 30 dB)) and >= min_onsets energy onsets (+6 dB in 100 ms)."""
    fr = int(0.1 * sr)
    nf = len(x) // fr
    n = int(clip_s * sr)
    if nf < clip_s / 0.1 or len(x) < n:
        return []
    e = 10 * np.log10(np.mean(x[:nf * fr].astype(np.float64).reshape(nf, fr) ** 2, 1) + 1e-12)
    floor = max(-60.0, float(np.percentile(e, 90)) - 30.0)
    act = e > floor
    on = np.zeros(nf, bool)
    on[1:] = (np.diff(e) > 6.0) & act[1:]
    ca, co = np.concatenate([[0], np.cumsum(act)]), np.concatenate([[0], np.cumsum(on)])
    w = int(round(clip_s / 0.1))
    out = []
    for s in range(0, len(x) - n + 1, int(hop_s * sr)):
        i = s // fr
        if i + w > nf:
            break
        if (ca[i + w] - ca[i]) >= min_active * w and (co[i + w] - co[i]) >= min_onsets:
            out.append(int(s))
    return out


def drive_pool() -> tuple[dict, dict]:
    """model -> free params, and model -> (drive knob, tone knob); the six flagged fuzzes removed (as T181)."""
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.learning import drive_pilot as dp
    from tonematch.metrics.distortion_step4 import EXCLUDED

    pool = MB.category_pool("drive", load_catalog())
    kn = dp._knob_names()
    _, _, m2f = dp.families()
    pool = {m: f for m, f in sorted(pool.items()) if m not in EXCLUDED and m in m2f and m in kn}
    return pool, kn


def draw_setting(rng: np.random.Generator, free, keep_names=()) -> dict[str, int]:
    """CC 0..127 per free knob, uniform; level-type knobs at their default (None = leave the default)."""
    out = {}
    for n, lo, hi, d in free:
        if n in LEVEL_NAMES and n not in keep_names:
            continue
        out[n] = int(rng.integers(0, 128))
    return out


def make_plan(manifest: dict, pool: dict, knobs: dict, read_audio, *, v1: dict, seed: int = SEED, target: int = 60_000,
              hosts: int = MAX_HOSTS, hours: float = 18.0, sec_per_clip: float = 0.85, shard_size: int = SHARD_SIZE,
              limit: int = 0) -> dict:
    """The full plan (pure given read_audio(path) -> mono float array)."""
    from tonematch.learning import drive_pilot as dp

    rng = np.random.default_rng(seed)
    files = eligible_files(manifest)
    splits = assign_file_splits(files, v1, rng)
    starts = {}
    for f in files:
        s = active_starts(read_audio(f))
        if s:
            starts[f] = s
    files = [f for f in files if f in starts]
    fit = int(hosts * hours * 3600 / sec_per_clip)
    n = min(target, fit)
    if limit:
        n = min(n, limit)
    w = np.array([SPLIT_WEIGHT[splits[f]] for f in files])
    file_idx = rng.choice(len(files), size=n, p=w / w.sum())
    if limit and n >= 4:   # a tiny plan still touches every split that exists
        need = [s for s in ("train", "test", "v1test", "cal") if any(splits[f] == s for f in files)]
        for k, s in enumerate(need):
            file_idx[k] = int(rng.choice([i for i, f in enumerate(files) if splits[f] == s]))
    models = sorted(pool)
    _, _, m2f = dp.families()
    clips = []
    for k, fi in enumerate(file_idx):
        f = files[int(fi)]
        m = models[int(rng.integers(len(models)))]
        dk, tk = knobs[m]
        cc = draw_setting(rng, pool[m], keep_names=(dk, tk))
        clips.append({"id": f"c{k:06d}", "model": m, "family": m2f[m], "cc": cc, "di_file": f,
                      "start": int(starts[f][int(rng.integers(len(starts[f])))]), "player": player_of(f),
                      "split": splits[f], "drive": cc[dk] / 127.0 if dk in cc else math.nan,
                      "tone": (cc[tk] / 127.0 if tk in cc else math.nan) if tk else math.nan})
    order = rng.permutation(len(clips))   # shards mix splits, files and models: a partial render stays balanced
    clips = [clips[int(i)] for i in order]
    shards = [{"id": f"s{j // shard_size:05d}", "clips": [c["id"] for c in clips[j:j + shard_size]]}
              for j in range(0, len(clips), shard_size)]
    file_rows = {s: sorted(f for f in files if splits[f] == s) for s in ("train", "cal", "test", "v1test")}
    return {"brief": "T191 drive-dataset-v1; measurement only, nothing adopted; P3 never read", "seed": seed,
            "n_clips": len(clips), "target": target, "fit_estimate": fit, "hosts": hosts, "hours": hours,
            "sec_per_clip": sec_per_clip, "shard_size": shard_size, "models": models,
            "excluded_models": sorted(_excluded()),
            "level_names_at_default": sorted(LEVEL_NAMES), "files": file_rows,
            "n_files": {s: len(v) for s, v in file_rows.items()},
            "clip_s": CLIP_S, "features": {"single_s": SINGLE_S, "ncrop": NCROP, "embedding": "AFx-Rep mid, frozen"},
            "clips": clips, "shards": shards}


def _excluded():
    try:
        from tonematch.metrics.distortion_step4 import EXCLUDED
        return EXCLUDED
    except Exception:  # noqa: BLE001
        return ()


def read_mono_file(path: str, start: int | None = None, n: int | None = None) -> np.ndarray:
    """Mono float32 at 48 kHz; `start`/`n` in 48 kHz samples. Files at another rate (some Guitar-TECHS files are
    44.1 kHz) are resampled (polyphase) and only the needed region is read."""
    import soundfile as sf
    p = DI_ROOT / path
    sr = sf.info(str(p)).samplerate
    if sr == SR:
        if start is None:
            x, _ = sf.read(str(p), dtype="float32", always_2d=True)
        else:
            x, _ = sf.read(str(p), dtype="float32", always_2d=True, start=int(start), frames=int(n))
        return x.mean(1)
    from math import gcd

    from scipy.signal import resample_poly
    g = gcd(SR, sr)
    up, down = SR // g, sr // g
    if start is None:
        x, _ = sf.read(str(p), dtype="float32", always_2d=True)
        return resample_poly(x.mean(1), up, down).astype(np.float32)
    pad = 2048
    s0 = max(0, int(start * sr / SR) - pad)
    x, _ = sf.read(str(p), dtype="float32", always_2d=True, start=s0, frames=int(n * sr / SR) + 2 * pad)
    y = resample_poly(x.mean(1), up, down).astype(np.float32)
    off = int(round(start - s0 * SR / sr))
    y = y[off:off + int(n)]
    return y if len(y) == n else np.pad(y, (0, int(n) - len(y)))


def cmd_plan(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pp = out / "plan.json"
    if pp.is_file() and not a.force:
        print(f"plan exists: {pp} (use --force to resample)")
        return 0
    from tonematch.engines.elastic import atomic_write_json
    pool, kn = drive_pool()
    plan = make_plan(json.loads(MANIFEST.read_text()), pool, kn, read_mono_file, v1=v1_splits(), seed=a.seed,
                     target=a.target, hosts=a.hosts, hours=a.hours, sec_per_clip=a.sec_per_clip, limit=a.limit)
    atomic_write_json(pp, plan)
    print(json.dumps({k: plan[k] for k in ("n_clips", "fit_estimate", "n_files", "hosts")}, indent=1))
    print(f"{len(plan['shards'])} shards; {len(plan['models'])} models; wrote {pp}")
    return 0


# ------------------------------------------------------------------------------------------ render (worker)

def embed_clip(emb, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(512,) centre 6 s crop and (3, 512) crops of the 10 s clip, AFx-Rep mid."""
    n = int(CLIP_S * SR)
    y = y[:n] if len(y) >= n else np.pad(y, (0, n - len(y)))
    L = n // NCROP
    crops = emb.embed_batch([y[k * L:(k + 1) * L] for k in range(NCROP)])
    s = (n - int(SINGLE_S * SR)) // 2
    single = emb.embed_batch([y[s:s + int(SINGLE_S * SR)]])[0]
    return np.asarray(single.mid, np.float32), np.stack([np.asarray(c.mid, np.float32) for c in crops])


SILENCE_DB = -90.0   # drive-dataset-v1's absolute floor on mean power (dBFS); T210: too high for quiet amp settings


def render_one(engine, block, clip, dry: np.ndarray, silence_db: float = SILENCE_DB) -> np.ndarray:
    vals = block.from_knobs(clip["cc"]) if hasattr(block, "from_knobs") else dict(clip["cc"])
    engine.load(block.with_values(vals))
    y = np.asarray(engine.render(dry), np.float32)
    if y.ndim == 2:
        y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
    y = y[:len(dry)]
    if not np.all(np.isfinite(y)):
        raise RuntimeError("non-finite output")
    if 10 * math.log10(float(np.mean(y.astype(np.float64) ** 2)) + 1e-30) <= silence_db:
        raise RuntimeError("Native returned silence")
    return y


def demand_now():
    """The demand tuple (busy cores first, T192). Needs no argument: the cores are counted once."""
    from tonematch.engines import host_budget as hb
    cores = demand_now.cores = getattr(demand_now, "cores", None) or hb.usable_cores()[0]
    return hb.measure_demand(cores).load


def make_gate(max_load: float | None):
    """Hysteresis gate on busy cores. ``max_load`` None: pause above 0.95 x cores. Resume at 0.85/0.95 of it."""
    from tonematch.engines import host_budget as hb
    cores = hb.usable_cores()[0]
    pause = float(max_load) if max_load else 0.95 * cores
    return hb.BusyGate(pause, pause * 0.85 / 0.95)


def render_shard(shard: dict, clips: dict, engine, blocks: dict, emb, read_excerpt, *, max_load: float | None = None,
                 loadavg=None, sleep=time.sleep, log=print, gate=None, label_keys=LABEL_KEYS,
                 silence_db: float = SILENCE_DB) -> dict:
    """Render + embed every clip of one shard. Each clip: one retry, then skipped. Returns arrays and stats.
    A clip with `lead` (samples, T208 time-based slots) is read and rendered from `start`, and the first `lead`
    samples of the output are dropped before embedding (random LFO phase, tails from the playing before).
    Drive clips have no `lead`: unchanged."""
    loadavg = loadavg or demand_now
    gate = gate or make_gate(max_load)
    singles, crops, labels, failures = [], [], [], []
    t_r = t_e = 0.0
    paused = 0.0
    consecutive = 0
    for cid in shard["clips"]:
        c = clips[cid]
        if gate.update(loadavg()[0]):
            log(f"busy cores {loadavg()[0]:.1f} > {gate.pause_above:.1f}: pausing new renders "
                f"(resume below {gate.resume_below:.1f})")
            waited = 0.0
            while gate.paused:
                sleep(5.0)
                paused += 5.0
                waited += 5.0
                gate.update(loadavg()[0])
            log(f"busy cores {loadavg()[0]:.1f} < {gate.resume_below:.1f}: resumed after {waited:.0f}s")
        lead = int(c.get("lead", 0))
        dry = read_excerpt(c["di_file"], c["start"], int(CLIP_S * SR) + lead)
        y, err = None, None
        for attempt in range(2):
            t0 = time.time()
            try:
                y = render_one(engine, blocks[c["model"]], c, dry, silence_db)
                break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                log(f"{cid} {c['model']} attempt {attempt + 1} failed: {err}")
            finally:
                t_r += time.time() - t0
        if y is None:
            failures.append({"id": cid, "model": c["model"], "error": err})
            consecutive += 1
            if consecutive >= 10:
                raise RuntimeError(f"10 clips in a row failed; last: {err}")
            continue
        consecutive = 0
        t0 = time.time()
        s, cr = embed_clip(emb, y[lead:])
        t_e += time.time() - t0
        singles.append(s)
        crops.append(cr)
        lab = {k: c[k] for k in label_keys if k in c}
        if "out_db" in label_keys:     # T210: the rendered clip's mean power (dBFS), after the lead-in
            lab["out_db"] = round(10 * math.log10(float(np.mean(y[lead:].astype(np.float64) ** 2)) + 1e-30), 2)
        labels.append(lab)
    return {"single": np.stack(singles).astype(np.float16) if singles else np.zeros((0, 512), np.float16),
            "crops": np.stack(crops).astype(np.float16) if crops else np.zeros((0, NCROP, 512), np.float16),
            "labels": labels, "failures": failures, "render_s": t_r, "embed_s": t_e, "paused_s": paused}


def write_shard(sdir: Path, sid: str, res: dict, extra: dict) -> None:
    """Arrays and labels first, the shard JSON last (atomic each): no JSON means not done."""
    from tonematch.engines.elastic import atomic_write_bytes, atomic_write_json, atomic_write_text
    atomic_write_bytes(sdir / f"{sid}.single.npy", npy_bytes(res["single"]))
    atomic_write_bytes(sdir / f"{sid}.crops.npy", npy_bytes(res["crops"]))
    atomic_write_text(sdir / f"{sid}.labels.jsonl", "".join(json.dumps(r) + "\n" for r in res["labels"]))
    atomic_write_json(sdir / f"{sid}.json", {"id": sid, "n_ok": len(res["labels"]), "n_failed": len(res["failures"]),
                                             "failures": res["failures"], "render_s": res["render_s"],
                                             "embed_s": res["embed_s"], "paused_s": res["paused_s"], **extra}, indent=1)


def stop_flag(out: Path) -> Path:
    return Path(out) / "STOP_REASON"


def work_loop(plan: dict, out: Path, *, open_engine, blocks: dict, emb, read_excerpt, deadline: float,
              min_free_gb: float, max_load: float | None = None, watch=None, busy=None, free=free_gb, log=None,
              loadavg=None, sleep=time.sleep, label_keys=LABEL_KEYS, silence_db: float = SILENCE_DB) -> int:
    """Claim and render shards until none is left, the deadline passes, the disk is low, or a stop is asked.
    `open_engine()` is called lazily (no host is opened when nothing is left to do). Returns shards written."""
    from tonematch.engines.elastic import ClaimQueue, atomic_write_text, item_done
    log = log or (lambda m: log_line(out, m))
    loadavg = loadavg or demand_now
    gate = make_gate(max_load)
    sdir = Path(out) / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    queue = ClaimQueue(sdir)
    clips = {c["id"]: c for c in plan["clips"]}
    shards = {s["id"]: s for s in plan["shards"]}
    busy = busy if busy is not None else {"in_item": False}
    engine, n = None, 0
    try:
        while True:
            if watch is not None and watch.should_stop():
                log(f"worker {os.getpid()}: stop requested; leaving between shards")
                break
            if stop_flag(out).exists():
                break
            if time.time() > deadline:
                atomic_write_text(stop_flag(out), "hours\n")
                log("time limit reached; stopping")
                break
            fg = free(out)
            if fg < min_free_gb:
                atomic_write_text(stop_flag(out), f"disk {fg:.2f} GB free < {min_free_gb} GB\n")
                log(f"free disk {fg:.2f} GB < {min_free_gb} GB: stopping cleanly")
                break
            todo = [sid for sid in shards if not item_done(sdir, sid)]
            if not todo:
                break
            claim = queue.claim_next(todo)
            if claim is None:
                break
            try:
                if item_done(sdir, claim.id):
                    continue
                if engine is None:
                    engine = open_engine()
                busy["in_item"] = True
                t0 = time.time()
                res = render_shard(shards[claim.id], clips, engine, blocks, emb, read_excerpt, max_load=max_load,
                                   loadavg=loadavg, sleep=sleep, log=log, gate=gate, label_keys=label_keys,
                                   silence_db=silence_db)
                write_shard(sdir, claim.id, res, {"wall_s": time.time() - t0, "pid": os.getpid(),
                                                  "finished": time.time()})
                busy["in_item"] = False
                n += 1
                log(f"worker {os.getpid()}: {claim.id} ok {len(res['labels'])} failed {len(res['failures'])} "
                    f"in {time.time() - t0:.0f}s (render {res['render_s']:.0f}s, embed {res['embed_s']:.0f}s), "
                    f"busy {loadavg()[0]:.1f}, free {free(out):.1f} GB")
            finally:
                busy["in_item"] = False
                claim.release()
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception as e:  # noqa: BLE001
                log(f"engine close failed: {e}")
    return n


def cmd_worker(a) -> int:
    import torch
    torch.set_num_threads(2)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines.elastic import StopWatch
    from tonematch.metrics import afxrep

    out = Path(a.out)
    plan = json.loads((out / "plan.json").read_text())
    catalog = load_catalog()
    pool, _ = drive_pool()
    pool = {m: pool[m] for m in plan["models"]}
    blocks = MB.make_blocks(pool, "drive", catalog)
    emb = afxrep.Embedder(threads=2)
    watch = StopWatch(out / "shards", a.supervisor_pid or None)
    busy = {"in_item": False}
    (out / "shards" / ".workers").mkdir(parents=True, exist_ok=True)
    watch.install_signals(lambda: busy["in_item"])

    def open_engine():
        return MB.make_engine(144_000, log=lambda *_: None)
    n = work_loop(plan, out, open_engine=open_engine, blocks=blocks, emb=emb, read_excerpt=read_mono_file,
                  deadline=a.deadline or 1e18, min_free_gb=a.min_free_gb, max_load=a.max_load, watch=watch, busy=busy)
    log_line(out, f"worker {os.getpid()}: exiting after {n} shards")
    return 0


# ------------------------------------------------------------------------------------------ render (supervisor)

def shard_stats(out: Path, plan: dict) -> dict:
    sdir = Path(out) / "shards"
    ok = failed = 0
    done = 0
    rs = es = 0.0
    for s in plan["shards"]:
        f = sdir / f"{s['id']}.json"
        if f.is_file():
            try:
                d = json.loads(f.read_text())
            except ValueError:
                continue
            done += 1
            ok += d["n_ok"]
            failed += d["n_failed"]
            rs += d.get("render_s", 0.0)
            es += d.get("embed_s", 0.0)
    return {"shards_done": done, "shards_total": len(plan["shards"]), "clips_ok": ok, "clips_failed": failed,
            "clips_planned": plan["n_clips"], "render_s_sum": round(rs, 1), "embed_s_sum": round(es, 1)}


def write_progress(out: Path, plan: dict, t_start: float, extra: dict | None = None,
                   clips_at_start: int = 0) -> dict:
    """`clips_at_start` is clips_ok when this run began, so the rate counts this run's clips only
    (a resumed run would otherwise divide every earlier batch's clips by this run's elapsed time)."""
    from tonematch.engines.elastic import atomic_write_json
    st = shard_stats(out, plan)
    el = time.time() - t_start
    run_ok = max(0, st["clips_ok"] - clips_at_start)
    rate = run_ok / el if el > 0 else 0.0
    remaining = max(0, st["clips_planned"] - st["clips_ok"] - st["clips_failed"])
    eta_s = remaining / rate if rate > 0 else None      # at this run's current rate
    eta = {"clips_remaining": remaining,
           "eta_remaining": (f"{int(eta_s // 3600)}:{int(eta_s % 3600 // 60):02d}" if eta_s is not None else None),
           "eta_finish": (time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + eta_s))
                          if eta_s is not None else None)}
    st.update({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_s": round(el), "clips_ok_this_run": run_ok,
               "clips_per_s_this_run": round(rate, 1), **eta,
               "free_gb": round(free_gb(out), 2), "load1": round(os.getloadavg()[0], 2),
               "busy_cores": round(demand_now()[0], 2),
               "stop_reason": stop_flag(out).read_text().strip() if stop_flag(out).exists() else None,
               **(extra or {})})
    atomic_write_json(Path(out) / "progress.json", st, indent=1)
    return st


def cmd_render(a) -> int:
    """Supervisor: renders nothing, keeps up to --hosts workers (as the host scaler grants)."""
    from tonematch.engines import host_budget as hb
    from tonematch.engines.elastic import ClaimQueue, Supervisor, item_done

    out = Path(a.out)
    plan = json.loads((out / "plan.json").read_text())
    sdir = out / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    with __import__("contextlib").suppress(OSError):
        stop_flag(out).unlink()          # a previous stop reason does not block a new run
    t_start = time.time()
    clips_at_start = shard_stats(out, plan)["clips_ok"]
    deadline = t_start + a.hours * 3600
    max_workers = max(1, min(a.hosts, MAX_HOSTS))
    os.environ["TONEMATCH_RUN_TAG"] = getattr(a, "run_tag", None) or "drive-dataset-v1"
    os.environ.setdefault("TONEMATCH_JOB_PRIORITY", "normal")
    job = hb.default_job()
    st0 = shard_stats(out, plan)
    log_line(out, f"render: {st0['shards_done']}/{st0['shards_total']} shards already done; up to {max_workers} "
                  f"hosts; pause above {a.max_load or '0.95 x cores'} busy cores; min free {a.min_free_gb} GB; until "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))}")
    last = {"t": 0.0}

    def left() -> int:
        if time.time() - last["t"] > 30:
            write_progress(out, plan, t_start, {"deadline": time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))},
                           clips_at_start=clips_at_start)
            last["t"] = time.time()
        if stop_flag(out).exists() or time.time() > deadline or free_gb(out) < a.min_free_gb:
            return 0
        return sum(1 for s in plan["shards"] if not item_done(sdir, s["id"]))

    script = getattr(a, "worker_script", None) or str(Path(__file__).resolve())   # T208: the slot script's own
    argv = [sys.executable, script, "worker", "--out", str(out), "--supervisor-pid",
            str(os.getpid()), "--deadline", repr(deadline), "--min-free-gb", str(a.min_free_gb)]
    if a.max_load:
        argv += ["--max-load", str(a.max_load)]
    n = 0

    def spawn():
        nonlocal n
        n += 1
        with open(out / f"worker{os.getpid()}-{n}.log", "ab") as lg:
            return subprocess.Popen(argv, stdout=lg, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=str(ROOT))
    todo = left()
    if hb.enabled():
        lease = hb.acquire(job, min(max_workers, max(todo, 1)), floor=0, demand_only=True, label="supervisor",
                           timeout=0, info={"workers": 0, "left": todo, "total": len(plan["shards"]),
                                            "supervisor": True})
        budget = hb.get_budget()
    else:
        lease, budget = hb.Lease(None, "off", job, max_workers, 0, max_workers), None
    sup = Supervisor(run_dir=sdir, left=left, total=len(plan["shards"]), spawn=spawn, lease=lease,
                     max_workers=max_workers, queue=ClaimQueue(sdir), budget=budget, poll_s=10.0, stagger_s=20.0,
                     grace_s=900.0, log=lambda m: log_line(out, m))
    state = {"term": False}

    def on_term(signum, frame):
        state["term"] = True
        log_line(out, f"supervisor: signal {signum}; workers finish their current shard, then stop")
        sup.terminate()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        finished = sup.run()
    except RuntimeError as e:          # workers keep dying: stop the render, keep what is done
        log_line(out, f"supervisor: {e}")
        from tonematch.engines.elastic import atomic_write_text
        atomic_write_text(stop_flag(out), f"workers failing: {e}\n")
        finished = True
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)   # training after the render stops on a plain SIGTERM
        signal.signal(signal.SIGINT, signal.default_int_handler)
    st = write_progress(out, plan, t_start, {"terminated": state["term"]}, clips_at_start=clips_at_start)
    log_line(out, f"render: stopped; {json.dumps(st)}")
    return 143 if state["term"] or not finished else 0


# ------------------------------------------------------------------------------------------ train

def load_new(out: Path) -> tuple[list[dict], np.ndarray, np.ndarray]:
    sdir = Path(out) / "shards"
    labels, S, C = [], [], []
    for f in sorted(sdir.glob("s*.json")):
        sid = f.stem
        try:
            s, c = np.load(sdir / f"{sid}.single.npy"), np.load(sdir / f"{sid}.crops.npy")
            lab = [json.loads(x) for x in (sdir / f"{sid}.labels.jsonl").read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            continue
        if len(lab) != len(s) or len(lab) != len(c):
            continue
        labels += lab
        S.append(s)
        C.append(c)
    if not labels:
        return [], np.zeros((0, 512), np.float32), np.zeros((0, NCROP, 512), np.float32)
    return labels, np.concatenate(S).astype(np.float32), np.concatenate(C).astype(np.float32)


def load_old(limit: int = 0, seed: int = SEED) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """T186 index + T189 embeddings (records usable as in T189), tagged source old/<split>."""
    if not (V0 / "index.json").is_file() or not (V1 / "emb_crops.npy").is_file():
        return [], np.zeros((0, 512), np.float32), np.zeros((0, NCROP, 512), np.float32)
    idx = json.loads((V0 / "index.json").read_text())
    recs, rms = idx["records"], np.array(idx["rms_db"])
    S, Cr = np.load(V1 / "emb_single.npy"), np.load(V1 / "emb_crops.npy")
    done = np.zeros(len(recs), bool)
    done[json.loads((V1 / "embed_progress.json").read_text())["done"]] = True
    use = np.where(done & (rms > -90) & np.any(S != 0, 1))[0]
    if limit and len(use) > limit:
        use = np.sort(np.random.default_rng(seed).choice(use, limit, replace=False))
    lab = [{"model": recs[j]["model"], "drive": recs[j]["drive"], "tone": recs[j]["tone"],
            "di_file": recs[j]["di_file"], "player": recs[j]["player"], "split": recs[j]["split"],
            "source": recs[j]["source"]} for j in use]
    return lab, S[use].astype(np.float32), Cr[use].astype(np.float32)


EVALS = (("v1test_old", "T186/T189 test split (29 files, old renders)"),
         ("newtest", "new held-out DI files (new renders)"),
         ("v1files_new", "T186/T189 test files, new random renders"))


def run_train(out: Path, models_dir: Path, *, heads=("logreg", "mlp"), seeds_main: int = 2, epochs: int = 60,
              old_limit: int = 0, ledger: bool = True, arms=("main", "p2p1", "p1p2"), datas=("v1data", "v2data"),
              log=print) -> dict:
    import pickle
    import warnings

    import torch
    torch.set_num_threads(2)
    warnings.filterwarnings("ignore", category=RuntimeWarning)   # empty slices on tiny eval subsets
    from sklearn.model_selection import GroupKFold

    from tonematch.learning import afx_heads as ah
    from tonematch.learning import drive_pilot as dp
    t_start = time.time()
    new_l, new_S, new_C = load_new(out)
    old_l, old_S, old_C = load_old(old_limit)
    for r in new_l:
        r["source"] = "new"
    L = old_l + new_l
    C = np.concatenate([old_C, new_C]) if len(old_C) else new_C
    N = len(L)
    fams, models, m2f = dp.families()
    mi = {m: i for i, m in enumerate(models)}
    fi = {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[m2f[m]] for m in models])
    y_m = np.array([mi[r["model"]] for r in L])
    K = np.array([[float(r["drive"]), float(r["tone"])] for r in L], float)
    files = np.array([r["di_file"] for r in L])
    player = np.array([r["player"] for r in L])
    is_new = np.array([r["source"] == "new" for r in L])
    split = np.array([r["split"] for r in L])
    assert not any(p == "P3" for p in player), "P3 in the training data"
    # grouped sets
    old_tr, old_ca, old_te = (~is_new) & (split == "train"), (~is_new) & (split == "cal"), (~is_new) & (split == "test")
    new_tr, new_ca = is_new & (split == "train"), is_new & (split == "cal")
    ev = {"v1test_old": old_te, "newtest": is_new & (split == "test"), "v1files_new": is_new & (split == "v1test")}
    test_files = set(files[old_te | ev["newtest"] | ev["v1files_new"]])
    train_files = set(files[old_tr | new_tr | old_ca | new_ca])
    assert not (test_files & train_files), "test DI files leak into training"
    res = {"brief": "T191 predictor-dataset-v1: T189 heads retrained on new drive-dataset-v1 renders + T186 train; "
                    "measurement only, nothing adopted",
           "n_old": int((~is_new).sum()), "n_new": int(is_new.sum()),
           "counts": {k: {"clips": int(m.sum()), "files": len(set(files[m]))} for k, m in
                      {"old_train": old_tr, "old_cal": old_ca, "new_train": new_tr, "new_cal": new_ca, **ev}.items()},
           "results": {}, "pooling": "multi (3 x 3.33 s crops, predictions averaged)"}
    log(json.dumps(res["counts"]))
    alpha = 1.0
    try:
        alpha = float(json.loads((V1 / "result.json").read_text()).get("ridge_alpha", 1.0))
    except (OSError, ValueError):
        pass
    res["ridge_alpha"] = alpha

    def make(head, seed):
        if head == "logreg":
            return ah.LinearHeads(mod_fam=mod_fam, alpha=alpha)
        return ah.MLPHeads(mod_fam=mod_fam, seed=seed, epochs=epochs)

    def oof(head, tr, seed):
        outs = {}
        g = files[tr]
        nf = min(5, len(set(g)))
        if nf < 2:
            return None
        for f_tr, f_va in GroupKFold(nf).split(tr, groups=g):
            h = make(head, seed).fit(C[tr[f_tr]], y_m[tr[f_tr]], K[tr[f_tr]])
            o = h.predict(C[tr[f_va]])
            for i, j in enumerate(tr[f_va]):
                outs[j] = {k: v[i] for k, v in o.items()}
        return {k: np.stack([outs[j][k] for j in tr]) for k in ("fl", "lc", "mu", "sig")}
    models_dir.mkdir(parents=True, exist_ok=True)
    for arm in arms:
        pl_tr = {"main": None, "p2p1": "P2", "p1p2": "P1"}[arm]
        pl_te = {"main": None, "p2p1": "P1", "p1p2": "P2"}[arm]
        for data in datas:
            trm = old_tr | (new_tr if data == "v2data" else False)
            cam = old_ca | (new_ca if data == "v2data" else False)
            if pl_tr:
                trm, cam = trm & (player == pl_tr), cam & (player == pl_tr)
            tr, ca = np.where(trm)[0], np.where(cam)[0]
            if len(set(files[tr])) < 2 or len(set(y_m[tr])) < 2:
                log(f"{arm}/{data}: too little training data; skipped")
                continue
            for head in heads:
                n_seeds = seeds_main if (head == "mlp" and arm == "main" and data == "v2data") else 1
                for seed in range(n_seeds):
                    t0 = time.time()
                    key = f"{arm}/{data}/{head}/multi/s{seed}"
                    h = make(head, seed).fit(C[tr], y_m[tr], K[tr])
                    po = oof(head, tr, seed)
                    pidx = tr
                    if po is not None and len(ca):
                        o_ca = h.predict(C[ca])
                        po = {k: np.concatenate([po[k], o_ca[k]]) for k in po}
                        pidx = np.concatenate([tr, ca])
                    cal = ah.calibrate(po, y_m[pidx], K[pidx], mod_fam, files[pidx], player[pidx]) if po else None
                    r = {"train_clips": len(tr), "train_files": len(set(files[tr])), "cal_clips": len(ca)}
                    for ename, em in ev.items():
                        m = em & (player == pl_te) if pl_te else em
                        te = np.where(m)[0]
                        if len(te) == 0:
                            continue
                        r[ename] = ah.score(h.predict(C[te]), y_m[te], K[te], mod_fam, cal)
                        r[ename]["files"] = len(set(files[te]))
                    r["fit_s"] = time.time() - t0
                    res["results"][key] = r
                    log(key + " " + json.dumps({e: round(r[e]["model_top1"], 3) for e, _ in EVALS if e in r})
                        + f" {r['fit_s']:.0f}s")
                    if arm == "main" and seed == 0:
                        if head == "mlp":
                            h.net = h.net.cpu().state_dict()
                        with open(models_dir / f"heads_{arm}_{data}_{head}_multi.pkl", "wb") as fh:
                            pickle.dump({"heads": h, "calibration": cal, "models": models, "families": fams,
                                         "note": "T191 measurement only; not adopted"}, fh)
                    from tonematch.engines.elastic import atomic_write_json
                    atomic_write_json(Path(out) / "summary.json", res, indent=1, default=float)
    res["elapsed_s"] = time.time() - t_start
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text
    atomic_write_json(Path(out) / "summary.json", res, indent=1, default=float)
    atomic_write_text(Path(out) / "summary.md", summary_md(res))
    if ledger:
        append_ledger(res)
    return res


def _f(r: dict, k: str) -> str:
    v = r.get(k)
    return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.2f}"


def summary_md(res: dict) -> str:
    out = ["# predictor-dataset-v1 (T191): results", "",
           "Measurement only; nothing adopted. Heads of T189 (predictor-pilot-v1), multi-crop pooling. "
           "`v1data`: trained on the T186 train split only (the v1 baseline, re-fitted here); "
           "`v2data`: T186 train + the new drive-dataset-v1 train renders. Calibration as T189 (out-of-fold over "
           "train files + calibration files).", "",
           "Counts: " + ", ".join(f"{k} {v['clips']} clips / {v['files']} files" for k, v in res["counts"].items()), ""]
    titles = {"main": "Within player (held-out recordings of known players)", "p2p1": "Cross player P2 -> P1",
              "p1p2": "Cross player P1 -> P2"}
    for arm, title in titles.items():
        for ename, edesc in EVALS:
            rows = [(k, v[ename]) for k, v in res["results"].items() if k.startswith(arm + "/") and ename in v]
            if not rows:
                continue
            out += [f"## {title}: {edesc}", "",
                    "| Data | Head | Pooling | Model top-1 | Top-5 | Family top-1 | Drive ±0.15 true / own | "
                    "Tone ±0.10 true / own | Drive cover 68 / 95 | Tone cover 68 / 95 | Drive 68% half-width | "
                    "Model ECE raw / cal | n (files) |",
                    "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for k, r in rows:
                _, data, head, pool, seed = k.split("/")
                out.append(f"| {data} | {head} {seed} | {pool} | {_f(r, 'model_top1')} | {_f(r, 'model_top5')} | "
                           f"{_f(r, 'family_top1')} | {_f(r, 'drive_within_true')} / {_f(r, 'drive_within_top1')} | "
                           f"{_f(r, 'tone_within_true')} / {_f(r, 'tone_within_top1')} | "
                           f"{_f(r, 'drive_cover68_global_true')} / {_f(r, 'drive_cover95_global_true')} | "
                           f"{_f(r, 'tone_cover68_global_true')} / {_f(r, 'tone_cover95_global_true')} | "
                           f"{_f(r, 'drive_halfwidth68_global_true')} | {_f(r, 'ece_raw')} / {_f(r, 'ece_cal')} | "
                           f"{r.get('n')} ({r.get('files')}) |")
            out.append("")
    out.append(f"Train wall {res.get('elapsed_s', 0):.0f} s.")
    return "\n".join(out) + "\n"


def append_ledger(res: dict) -> None:
    os.environ.setdefault("TONEMATCH_AGENT", "learning")
    n_new = res["n_new"]
    for k, r in res["results"].items():
        arm, data, head, pool, seed = k.split("/")
        if seed != "s0":
            continue
        for ename, _ in EVALS:
            if ename not in r:
                continue
            notes = (f"T191 measurement only not adopted. Drive-only; T189 heads multi-crop retrained; {data} "
                     f"({r['train_clips']} train clips, {r['train_files']} files); eval {ename} n {r[ename]['n']} "
                     f"files {r[ename]['files']}; top5 {r[ename]['model_top5']:.3f}; family {r[ename]['family_top1']:.3f}; "
                     f"new Native renders {n_new}; P3 unread")
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "benchmark",
                            "--engine", "-", "--dataset", DATASET, "--metric", f"{arm}_{data}_{head}_{ename}_model_top1",
                            "--value", f"{r[ename]['model_top1']:.4f}", "--renders", str(n_new),
                            "--wall-clock-s", f"{r['fit_s']:.0f}", "--notes", notes], check=False, cwd=str(ROOT))


def cmd_train(a) -> int:
    out = Path(a.out)
    md = Path(a.models_dir) if a.models_dir else (MODELS_DEFAULT if out.resolve() == OUT_DEFAULT.resolve()
                                                  else out / "models")
    run_train(out, md, heads=tuple(a.heads.split(",")), seeds_main=a.seeds, epochs=a.epochs,
              old_limit=a.old_limit, ledger=not a.no_ledger, log=lambda m: log_line(out, m))
    log_line(out, f"train: wrote {out / 'summary.json'}, {out / 'summary.md'}, checkpoints in {md}")
    return 0


# ------------------------------------------------------------------------------------------ all

def cmd_all(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log_line(out, f"all: start pid {os.getpid()}; out {out}")
    rc_render = 0
    try:
        if not (out / "plan.json").is_file():
            a.force = False
            cmd_plan(a)
        rc_render = cmd_render(a)
        if rc_render == 143:
            log_line(out, "all: stopped by signal; training skipped. Resume: run the same command again; "
                          "or train on what is done: drive_dataset_render.py train")
            return 143
        cmd_train(a)
    except Exception as e:  # noqa: BLE001
        import traceback
        log_line(out, f"all: FAILED {type(e).__name__}: {e}\n{traceback.format_exc()}")
        from tonematch.engines.elastic import atomic_write_json
        atomic_write_json(out / "failure.json", {"error": repr(e), "when": time.strftime("%Y-%m-%d %H:%M:%S")})
        rc_render = 1
    finally:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/proc_check.py"), "--kill"], cwd=str(ROOT),
                           capture_output=True, text=True)
        log_line(out, "proc_check --kill: " + (r.stdout.strip()[-800:] or r.stderr.strip()[-400:]))
    log_line(out, "all: done")
    return rc_render


# ------------------------------------------------------------------------------------------ cli

def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--out", default=str(OUT_DEFAULT))
        return p

    def plan_args(p):
        p.add_argument("--seed", type=int, default=SEED)
        p.add_argument("--target", type=int, default=60_000, help="cap on planned clips (disk / features)")
        p.add_argument("--hosts", type=int, default=MAX_HOSTS)
        p.add_argument("--hours", type=float, default=18.0)
        p.add_argument("--sec-per-clip", type=float, default=0.85, help="wall s per clip per host (plan sizing)")
        p.add_argument("--limit", type=int, default=0, help="shakedown: plan only this many clips")
        p.add_argument("--force", action="store_true")

    def render_args(p):
        p.add_argument("--max-load", type=float, default=None,
                       help="pause new renders above this many BUSY cores (CPU utilisation x cores); "
                            "default 0.95 x cores; resume below 0.85/0.95 of it")
        p.add_argument("--min-free-gb", type=float, default=8.5)

    def train_args(p):
        p.add_argument("--heads", default="logreg,mlp")
        p.add_argument("--seeds", type=int, default=2, help="MLP seeds for the main v2data arm (others: 1)")
        p.add_argument("--epochs", type=int, default=60)
        p.add_argument("--old-limit", type=int, default=0, help="shakedown: subsample the T186 records")
        p.add_argument("--models-dir", default="")
        p.add_argument("--no-ledger", action="store_true")
    plan_args(common(sub.add_parser("plan")))
    p = common(sub.add_parser("render"))
    render_args(p)
    p.add_argument("--hosts", type=int, default=MAX_HOSTS)
    p.add_argument("--hours", type=float, default=18.0)
    p = common(sub.add_parser("worker"))
    render_args(p)
    p.add_argument("--supervisor-pid", type=int, default=0)
    p.add_argument("--deadline", type=float, default=0.0)
    train_args(common(sub.add_parser("train")))
    p = common(sub.add_parser("all"))
    plan_args(p)
    render_args(p)
    train_args(p)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    if a.cmd == "all" and a.limit:
        a.no_ledger = True                 # a shakedown never writes ledger rows
    return {"plan": cmd_plan, "render": cmd_render, "worker": cmd_worker, "train": cmd_train, "all": cmd_all}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
