#!/usr/bin/env python3
"""chain-train-v1 (T215, learning): chain-rendered training pilot. Measurement only; nothing adopted.

Human 2026-10-23 "yes start pilot" (T214 option A; DECISIONS "chain training pilot approved").

    .venv/bin/python scripts/chain_train.py plan                      # ~5,000 seeded chains, TRAIN-split DIs only
    caffeinate -i nice -n 10 .venv/bin/python scripts/chain_train.py render --hosts 4 --hours 2   # resumable shards
    .venv/bin/python scripts/chain_train.py train [--ledger]          # per-slot heads + presence, two arms, half curve

Chains: the T214 generator (src/tonematch/learning/chain_transfer.py) with seed 215, the same slot probabilities,
model pools, approved knob sampling (uniform CC grid, level knobs at default) and lead-in. DIs: drive-dataset-v1
`train` split files only (never the 18 test files the T214 test set uses, never cal, never P3). Render: the T214
block-serial chain path (fresh pool per stage, busy-core LoadGuard, -240 dBFS floor, one retry, digital-zero
chains dropped), in shards of 250 chains written atomically (the shard JSON last), so a rerun resumes and nothing
is overwritten; stops between shards at the --hours deadline or on SIGTERM. Features: AFx-Rep mid, 3 x 3.33 s
crops of the finished chain output (lead-in dropped). Output: cache/chain-train-v1/ (T214's cache is never
touched). Training and scoring: `src/tonematch/learning/chain_train.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
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

import chain_transfer as T214  # noqa: E402

from tonematch.learning import chain_transfer as CT  # noqa: E402

OUT = ROOT / "cache/chain-train-v1"
TEST = ROOT / "cache/chain-transfer-v1"
MODELS = ROOT / "cache/models/chain-train-v1"
SEED = 215
N = 5000
SHARD = 250
DATASET = "chain-train-v1"


def log(msg: str, out: Path = OUT) -> None:
    T214.log(msg, out)


def train_files() -> list[str]:
    import drive_dataset_render as DD
    plan = json.loads(T214.DRIVE_PLAN.read_text())
    files = [f for f in plan["files"]["train"] if DD.player_of(f) in ("P1", "P2") and "/P3" not in f]
    held = set(plan["files"]["test"]) | set(plan["files"]["cal"]) | set(plan["files"]["v1test"])
    assert not set(files) & held, "train files overlap held-out files"
    return files


def cmd_plan(a) -> int:
    import drive_dataset_render as DD
    from tonematch.dry_rebuild import loud_level_db
    from tonematch.engines.elastic import atomic_write_json
    out = Path(a.out)
    pp = out / "chains.json"
    if pp.is_file():
        if any((out / "shards").glob("s*.json")):
            print(f"{pp} exists and shards are rendered; refusing to re-plan")
            return 2
        if not a.force:
            print(f"chain list exists: {pp} (--force to resample; nothing rendered yet)")
            return 0
    assert a.seed != CT.SEED, "use a new seed (T214 used 214)"
    files = train_files()
    test = json.loads((TEST / "chains.json").read_text())
    assert not set(files) & set(test["files"]), "train files overlap the T214 test files"
    starts = {f: DD.active_starts(DD.read_mono_file(f)) for f in files}
    dpool, _ = T214.drive_pool_knobs()
    pools = {"drive": dpool, **T214.slot_pools()}
    chains = CT.make_chains(files, starts, pools, n=a.n, seed=a.seed, player_of=DD.player_of)
    for c in chains:
        c["id"] = "t" + c["id"][1:]
    by_file: dict[str, list[dict]] = {}
    for c in chains:
        by_file.setdefault(c["di_file"], []).append(c)
    n = int(CT.CLIP_S * CT.SR)
    for f, cs in by_file.items():
        x = DD.read_mono_file(f)
        for c in cs:
            s0 = c["start"] + c["lead"]
            c["di_level_db"] = round(loud_level_db(np.asarray(x[s0:s0 + n], np.float32)), 2)
    shards = [{"id": f"s{j // a.shard:03d}", "chains": [c["id"] for c in chains[j:j + a.shard]]}
              for j in range(0, len(chains), a.shard)]
    from collections import Counter
    cnt = Counter(s for c in chains for s in CT.slots_of(c))
    doc = {"brief": "T215 chain-train-v1: random chains in the fixed slot order, drive-dataset-v1 TRAIN-split DIs only "
                    "(test, cal, v1test and P3 never used); measurement only, nothing adopted", "seed": a.seed,
           "n": len(chains), "probs": CT.PROBS, "lead_s": CT.LEAD_S, "order": CT.CHAIN_ORDER,
           "silence_db": CT.SILENCE_DB, "files": files, "slot_counts": dict(cnt), "shards": shards, "chains": chains}
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(pp, doc)
    print(json.dumps({"n": len(chains), "files": len(files), "shards": len(shards), "slot_counts": dict(cnt)}, indent=1))
    return 0


def shard_done(sdir: Path, sid: str) -> bool:
    return (sdir / f"{sid}.json").is_file()


def write_shard(sdir: Path, sid: str, chains: list[dict], res: dict, extra: dict) -> int:
    from tonematch.engines.elastic import atomic_write_bytes, atomic_write_json, atomic_write_text
    import drive_dataset_render as DD
    has = res["has"]
    rows, crops = [], []
    for i, c in enumerate(chains):
        if c["id"] in res["failed"] or not has[i].any():
            continue
        k = int(np.where(has[i])[0][-1])
        crops.append(res["crops"][i, k])
        rows.append({**c, "out_db": float(res["out_db"][i, k])})
    C = np.stack(crops).astype(np.float16) if crops else np.zeros((0, 3, 512), np.float16)
    atomic_write_bytes(sdir / f"{sid}.crops.npy", DD.npy_bytes(C))
    atomic_write_text(sdir / f"{sid}.labels.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    atomic_write_json(sdir / f"{sid}.json", {"id": sid, "n_ok": len(rows), "failed": res["failed"],
                                             "stage_s": res["stage_s"], **extra}, indent=1)
    return len(rows)


def cmd_render(a) -> int:
    import drive_dataset_render as DD
    from tonematch.engines import host_budget as hb
    from tonematch.engines.native import RenderJob

    out = Path(a.out)
    assert out.resolve() != TEST.resolve(), "never write into the T214 cache"
    pidf = out / "job.pid"
    if pidf.is_file():
        try:
            old = int(pidf.read_text())
            os.kill(old, 0)
            print(f"a render is already running (PID {old}); refusing")
            return 2
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    pidf.write_text(str(os.getpid()))
    doc = json.loads((out / "chains.json").read_text())
    byid = {c["id"]: c for c in doc["chains"]}
    assert not set(c["di_file"] for c in doc["chains"]) & set(json.loads((TEST / "chains.json").read_text())["files"])
    sdir = out / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    stop = {"flag": False}

    def on_term(*_):
        stop["flag"] = True
        log("SIGTERM: stopping after the current shard", out)
    signal.signal(signal.SIGTERM, on_term)
    deadline = time.time() + a.hours * 3600
    todo = [s for s in doc["shards"] if not shard_done(sdir, s["id"])]
    log(f"render: {len(todo)} of {len(doc['shards'])} shards to do, hosts <= {a.hosts}, deadline in {a.hours} h, "
        f"pid {os.getpid()}", out)
    presets = T214.make_presets(doc["chains"])
    open_pool = T214.pool_opener(a.hosts)
    guard = hb.LoadGuard(a.max_busy)
    embed = T214.embed_crops_fn()
    t_run, last_s, done_n, n_blocks = time.time(), None, 0, 0
    reason = "finished"
    try:
        for s in todo:
            if stop["flag"]:
                reason = "SIGTERM"
                break
            if last_s is not None and time.time() + last_s > deadline:
                reason = "deadline"
                break
            chains = [byid[i] for i in s["chains"]]
            t0 = time.time()
            res = CT.run_chains(chains, presets, DD.read_mono_file, open_pool, embed,
                                make_job=lambda d, p: RenderJob(dry=d, preset=p), guard=guard,
                                log=lambda m: log(f"{s['id']} {m}", out), chunk=a.chunk, embed_stages="final")
            last_s = time.time() - t0
            nb = sum(len(c["blocks"]) for c in chains if c["id"] not in res["failed"])
            k = write_shard(sdir, s["id"], chains, res, {"wall_s": round(last_s, 1), "blocks": nb,
                                                         "finished": time.time(), "hosts": a.hosts})
            done_n += k
            n_blocks += nb
            log(f"{s['id']}: {k} chains ok, {len(res['failed'])} dropped, {last_s:.0f} s "
                f"({last_s / max(1, len(chains)):.2f} s/chain)", out)
    finally:
        pidf.unlink(missing_ok=True)
    wall = time.time() - t_run
    log(f"render stopped ({reason}): {done_n} chains in {wall:.0f} s this run", out)
    if a.ledger and done_n:
        T214.ledger(["--kind", "render-cost", "--engine", "native", "--dataset", DATASET, "--metric", "wall_s_per_chain",
                     "--value", f"{wall / done_n:.3f}", "--renders", str(n_blocks), "--wall-clock-s", f"{wall:.0f}",
                     "--notes", f"T215 block-serial chain renders, {done_n} chains this run, <= {a.hosts} hosts, "
                     f"fresh pool per stage, final-output embedding included; stop {reason}; train-split DIs; P3 unread"])
    return 0


def cmd_train(a) -> int:
    from tonematch.learning import chain_train as TR
    TR.run(Path(a.out), TEST, Path(a.models), ledger=a.ledger, threads=a.threads, arms=tuple(a.arms.split(",")),
           fractions=tuple(float(x) for x in a.fractions.split(",")), slots=tuple(a.slots.split(",")),
           log=lambda m: log(m, Path(a.out)), resume=not a.no_resume, oracle=not a.no_oracle)
    return 0


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--n", type=int, default=N)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--shard", type=int, default=SHARD)
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=4)
    r.add_argument("--hours", type=float, default=2.0)
    r.add_argument("--chunk", type=int, default=24)
    r.add_argument("--max-busy", type=float, default=None)
    r.add_argument("--ledger", action="store_true")
    t = sub.add_parser("train")
    t.add_argument("--models", default=str(MODELS))
    t.add_argument("--threads", type=int, default=2)
    t.add_argument("--arms", default="chain,single+chain")
    t.add_argument("--fractions", default="0.5,1")
    t.add_argument("--slots", default=",".join(CT.CHAIN_ORDER))
    t.add_argument("--ledger", action="store_true")
    t.add_argument("--no-resume", action="store_true", help="refit everything (default: reuse cached fits in results.json)")
    t.add_argument("--no-oracle", action="store_true", help="skip the amp + true-cab oracle test")
    for x in (p, r, t):
        x.add_argument("--out", default=str(OUT))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    if a.cmd == "render":
        assert 1 <= a.hosts <= 4, "at most 4 Native hosts"
    return {"plan": cmd_plan, "render": cmd_render, "train": cmd_train}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
