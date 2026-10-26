#!/usr/bin/env python3
"""slot-dataset-v1 (T208, learning): unattended single-block training-data render on Helix Native, features only,
for the chain slots other than drive (amp, cab, mod, delay, reverb, eq), then a retrain and evaluation of the T189
heads. A measurement only: nothing is adopted. Every design choice is a proposal for the human
(docs/notes/slot-datasets-v1.md, "Decisions for the human").

    .venv/bin/python scripts/slot_dataset_render.py plan --slot amp            # seeded plan -> plan.json (once)
    .venv/bin/python scripts/slot_dataset_render.py render --slot amp --hours 2  # one batch; resumes, never overwrites
    .venv/bin/python scripts/slot_dataset_render.py status                     # every slot: done / planned / ETA
    .venv/bin/python scripts/slot_dataset_render.py train --slot amp           # heads + summary; keeps every result
    .venv/bin/python scripts/slot_dataset_render.py smoke --slots amp,cab --clips 12 --out <scratch>  # speed check
    scripts/slot_batch.sh amp 2                                                # the human's batch command

Shares drive-dataset-v1's code (scripts/drive_dataset_render.py): the elastic supervisor (at most 4 Native hosts,
host scaler), the busy-core pause with hysteresis, the disk guard, the --hours deadline, one retry per clip,
SIGTERM between shards, atomic shard files (the shard JSON last), progress.json with this run's rate and ETA, and
the AFx-Rep multi-crop features. Output: cache/slot-dataset-v1/<slot>/ (shards, plan, logs, summaries) and
cache/models/slot-dataset-v1/<slot>/n<clips>/ (checkpoints). P3 is never read; no song audio and no human takes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import drive_dataset_render as DD  # noqa: E402

from tonematch.learning import slot_dataset as SD  # noqa: E402

# T213: TONEMATCH_SLOT_BASE / TONEMATCH_SLOT_MODELS move every slot directory (a smoke of the overnight supervisor
# runs in a scratch tree and never touches the real cache); unset = the real cache, as before
BASE = Path(os.environ.get("TONEMATCH_SLOT_BASE") or ROOT / "cache/slot-dataset-v1")
MODELS_BASE = Path(os.environ.get("TONEMATCH_SLOT_MODELS") or ROOT / "cache/models/slot-dataset-v1")
DRIVE_PLAN = ROOT / "cache/drive-dataset-v1/plan.json"


def out_dir(a) -> Path:
    return Path(a.out) if getattr(a, "out", None) else BASE / a.slot


def finished_shards(out: Path) -> int:
    sd = Path(out) / "shards"
    return sum(1 for _ in sd.glob("s*.json")) if sd.is_dir() else 0


# ------------------------------------------------------------------------------------------ plan

def cmd_plan(a) -> int:
    out = out_dir(a)
    out.mkdir(parents=True, exist_ok=True)
    pp = out / "plan.json"
    if pp.is_file():
        done = finished_shards(out)
        if done:
            print(f"plan exists and {done} shards are rendered: {pp}. Re-planning would orphan them; refused. "
                  f"Move {out} aside first if you really want a new plan.")
            return 2
        if not a.force:
            print(f"plan exists: {pp} (use --force to resample; no shard is rendered yet)")
            return 0
    from tonematch.engines.elastic import atomic_write_json
    pool = SD.slot_pool(a.slot, all_native=a.all_native)
    splits = {} if a.fresh_splits else SD.drive_file_splits(DRIVE_PLAN)
    plan = SD.make_slot_plan(a.slot, json.loads(DD.MANIFEST.read_text()), pool, DD.read_mono_file, splits=splits,
                             seed=a.seed, target=a.target or None, limit=a.limit, all_native=a.all_native)
    plan["split_source"] = "fresh (drive rule)" if a.fresh_splits or not splits else str(DRIVE_PLAN.relative_to(ROOT))
    if getattr(a, "silence_db", None) is not None:     # T212 option B (human 2026-10-22): the plan carries its floor and records out_db
        plan["silence_db"] = float(a.silence_db)
        plan["record_out_db"] = True
    atomic_write_json(pp, plan)
    print(json.dumps({k: plan.get(k) for k in ("slot", "n_clips", "n_files", "lead_s", "split_source", "silence_db")},
                     indent=1))
    print(f"{len(plan['shards'])} shards; {len(plan['models'])} models; up to "
          f"{max(len(v) for v in plan['knobs'].values())} knobs per model; wrote {pp}")
    return 0


# ------------------------------------------------------------------------------------------ worker / render

def load_blocks(plan: dict, catalog):
    import match_block as MB
    pool = SD.slot_pool(plan["slot"], catalog, all_native=plan.get("all_native", False))
    missing = [m for m in plan["models"] if m not in pool]
    if missing:
        raise RuntimeError(f"{len(missing)} planned models are not in this catalog/library (e.g. {missing[:3]})")
    pool = {m: pool[m] for m in plan["models"]}
    return MB.make_blocks(pool, plan["category"], catalog)


def cmd_worker(a) -> int:
    import torch
    torch.set_num_threads(2)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines.elastic import StopWatch
    from tonematch.metrics import afxrep

    out = Path(a.out)
    plan = json.loads((out / "plan.json").read_text())
    blocks = load_blocks(plan, load_catalog())
    emb = afxrep.Embedder(threads=2)
    watch = StopWatch(out / "shards", a.supervisor_pid or None)
    busy = {"in_item": False}
    (out / "shards" / ".workers").mkdir(parents=True, exist_ok=True)
    watch.install_signals(lambda: busy["in_item"])
    preroll = 144_000

    def open_engine():
        return MB.make_engine(preroll, log=lambda *_: None)
    # a refill plan (T210) carries its own silence floor and records each clip's output level; a main plan has
    # neither key, so it renders exactly as before (drive's -90 dB floor, the original labels)
    keys = SD.LABEL_KEYS + (("out_db",) if plan.get("record_out_db") else ())
    n = DD.work_loop(plan, out, open_engine=open_engine, blocks=blocks, emb=emb, read_excerpt=DD.read_mono_file,
                     deadline=a.deadline or 1e18, min_free_gb=a.min_free_gb, max_load=a.max_load, watch=watch,
                     busy=busy, label_keys=keys, silence_db=float(plan.get("silence_db", DD.SILENCE_DB)))
    DD.log_line(out, f"worker {os.getpid()}: exiting after {n} shards")
    return 0


def cmd_render(a) -> int:
    out = out_dir(a)
    if not getattr(a, "out", None) and getattr(a, "refill", False):
        out = BASE / f"{a.slot}-refill"
    if not (out / "plan.json").is_file():
        print(f"no plan: run `slot_dataset_render.py plan --slot {a.slot}` first")
        return 2
    a.out = str(out)
    a.run_tag = os.environ.get("TONEMATCH_SLOT_RUN_TAG") or f"slot-dataset-v1-{a.slot}"
    a.worker_script = str(Path(__file__).resolve())
    return DD.cmd_render(a)


# ------------------------------------------------------------------------------------------ refill (T210)

DIGITAL_ZERO = "Native returned silence for a non-silent input"   # the engine's own guard: output below -240 dB


def failed_silence(out: Path) -> list[dict]:
    """Failures of a slot's finished shards whose error is silence: [{id, model, error, digital_zero}]."""
    rows = []
    for f in sorted((Path(out) / "shards").glob("s*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        for x in d.get("failures", []):
            if "silence" in (x.get("error") or ""):
                rows.append({**x, "digital_zero": DIGITAL_ZERO in x["error"]})
    return rows


def make_refill_plan(main: dict, failures: list[dict], *, silence_db: float, old: dict | None = None,
                     skip_digital_zero: bool = True, shard_size: int = SD.SHARD_SIZE) -> tuple[dict, int]:
    """A plan of the main plan's failed clips (identical clip records), to render into its own directory.

    `old`: an existing refill plan; its clips and shards are kept unchanged and only new failures are appended as new
    shards, so a rendered refill shard is never rewritten. Clips that came back as digital zero (a level or gain
    pot at CC 0) are skipped by default: no floor above -240 dB can keep them. Returns (plan, clips added)."""
    clips = {c["id"]: c for c in main["clips"]}
    have = {c["id"] for c in (old or {}).get("clips", [])}
    new = [clips[x["id"]] for x in failures if x["id"] in clips and x["id"] not in have
           and not (skip_digital_zero and x["digital_zero"])]
    seen, uniq = set(), []
    for c in new:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    shards = list((old or {}).get("shards", []))
    k0 = len(shards)
    shards += [{"id": f"s{k0 + j // shard_size:05d}", "clips": [c["id"] for c in uniq[j:j + shard_size]]}
               for j in range(0, len(uniq), shard_size)]
    keep = {k: v for k, v in main.items() if k not in ("clips", "shards", "n_clips")}
    plan = {**keep, "brief": f"T210 refill of {main['slot']}: the main plan's silence failures, re-rendered with a "
                             f"silence floor of {silence_db} dBFS; measurement only, not adopted",
            "refill_of": main["slot"], "silence_db": float(silence_db), "record_out_db": True,
            "skip_digital_zero": bool(skip_digital_zero),
            "clips": list((old or {}).get("clips", [])) + uniq, "shards": shards}
    plan["n_clips"] = len(plan["clips"])
    if old and float(old.get("silence_db", silence_db)) != float(silence_db):
        raise ValueError(f"the refill plan uses silence_db {old['silence_db']}; refusing to mix floors")
    return plan, len(uniq)


def cmd_refill(a) -> int:
    from tonematch.engines.elastic import atomic_write_json
    main_out = BASE / a.slot
    main = json.loads((main_out / "plan.json").read_text())
    rout = Path(a.out) if a.out else BASE / f"{a.slot}-refill"
    old = json.loads((rout / "plan.json").read_text()) if (rout / "plan.json").is_file() else None
    fails = failed_silence(main_out)
    plan, added = make_refill_plan(main, fails, silence_db=a.silence_db, old=old,
                                   skip_digital_zero=not a.keep_digital_zero)
    nz = sum(x["digital_zero"] for x in fails)
    print(f"{a.slot}: {len(fails)} silence failures in finished shards ({nz} digital zero"
          f"{', skipped' if not a.keep_digital_zero else ''}); refill plan {plan['n_clips']} clips "
          f"(+{added} new) in {len(plan['shards'])} shards; floor {a.silence_db} dBFS -> {rout}")
    if a.dry_run:
        return 0
    rout.mkdir(parents=True, exist_ok=True)
    atomic_write_json(rout / "plan.json", plan)
    return 0


# ------------------------------------------------------------------------------------------ status

def slot_status(out: Path) -> dict | None:
    pp = Path(out) / "plan.json"
    if not pp.is_file():
        return None
    plan = json.loads(pp.read_text())
    st = DD.shard_stats(out, plan)
    prog = {}
    if (Path(out) / "progress.json").is_file():
        try:
            prog = json.loads((Path(out) / "progress.json").read_text())
        except ValueError:
            prog = {}
    per = (st["render_s_sum"] + st["embed_s_sum"]) / max(1, st["clips_ok"] + st["clips_failed"])
    return {**st, "s_per_clip_per_host": round(per, 3), "last_update": prog.get("updated"),
            "last_rate": prog.get("clips_per_s_this_run"), "eta_remaining": prog.get("eta_remaining"),
            "stop_reason": prog.get("stop_reason"),
            "summaries": sorted(p.name for p in Path(out).glob("summary-*.md"))}


def cmd_status(a) -> int:
    rows = []
    for slot in SD.SLOTS:
        s = slot_status(BASE / slot)
        if s is None:
            rows.append(f"| {slot} | no plan | | | | | |")
            continue
        rows.append(f"| {slot} | {s['clips_ok']:,} / {s['clips_planned']:,} | {s['shards_done']} / {s['shards_total']} "
                    f"| {s['clips_failed']} | {s['last_rate'] or ''} | {s['eta_remaining'] or ''} "
                    f"| {', '.join(s['summaries']) or '-'} |")
    print("| Slot | Clips ok / planned | Shards | Failed | Last rate (clips/s) | ETA at last rate | Summaries |")
    print("|---|---|---|---|---|---|---|")
    print("\n".join(rows))
    return 0


# ------------------------------------------------------------------------------------------ train

def cmd_train(a) -> int:
    out = out_dir(a)
    plan = json.loads((out / "plan.json").read_text())
    n = DD.shard_stats(out, plan)["clips_ok"]
    frac = float(getattr(a, "fraction", 1.0) or 1.0)
    tag = f"n{n}" if frac >= 1.0 else f"frac{frac:g}-of-n{n}"     # T213: the half-size arm
    md = Path(a.models_dir) if a.models_dir else MODELS_BASE / a.slot / tag
    if md.exists() and any(md.iterdir()):
        md = md.with_name(f"{tag}-{time.strftime('%m%d-%H%M')}")     # never overwrite earlier checkpoints
    device = a.device
    if device == "auto":
        from tonematch.gpu import detect_backend
        b = detect_backend()
        device = {"cuda": "cuda", "rocm": "cuda", "mps": "mps"}.get(b, "cpu")
    extra = []
    if getattr(a, "with_refill", False):
        r = BASE / f"{a.slot}-refill"
        if not (r / "plan.json").is_file():
            print(f"--with-refill: no refill plan in {r}")
            return 2
        extra = [r]
        tag += f"-refill{DD.shard_stats(r, json.loads((r / 'plan.json').read_text()))['clips_ok']}"
        md = Path(a.models_dir) if a.models_dir else MODELS_BASE / a.slot / tag
        if md.exists() and any(md.iterdir()):
            md = md.with_name(f"{tag}-{time.strftime('%m%d-%H%M')}")
    res = SD.run_train_slot(out, md, heads=tuple(a.heads.split(",")), epochs=a.epochs, seeds_main=a.seeds,
                            ledger=not a.no_ledger, device=device, tol=a.tol, log=lambda m: DD.log_line(out, m),
                            extra_outs=extra, min_out_db=getattr(a, "min_out_db", None), fraction=frac)
    keep_md, keep_js = out / f"summary-{tag}.md", out / f"summary-{tag}.json"
    if keep_md.exists():
        keep_md, keep_js = (out / f"summary-{tag}-{time.strftime('%m%d-%H%M')}.md",
                            out / f"summary-{tag}-{time.strftime('%m%d-%H%M')}.json")
    shutil.copyfile(out / "summary.md", keep_md)
    shutil.copyfile(out / "summary.json", keep_js)
    DD.log_line(out, f"train: {res['n']} clips; wrote {keep_md.name} (and summary.md); checkpoints in {md}")
    return 0


# ------------------------------------------------------------------------------------------ smoke

def cmd_smoke(a) -> int:
    """A few clips per slot on one Native host at a time (a fresh host per slot), in-process: seconds per clip."""
    import torch
    torch.set_num_threads(2)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines.elastic import atomic_write_json
    from tonematch.metrics import afxrep

    base = Path(a.out)
    base.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog()
    manifest = json.loads(DD.MANIFEST.read_text())
    splits = SD.drive_file_splits(DRIVE_PLAN)
    if splits:   # a smoke run reads only two DI files per split (planning reads whole files)
        keep = {f for sp in ("train", "cal", "test", "v1test") for f in sorted(x for x, v in splits.items() if v == sp)[:2]}
        manifest = {"clips": [c for c in manifest["clips"] if c["path"] in keep]}
    emb = afxrep.Embedder(threads=2)
    rows = {}
    engine = None
    try:
        for slot in a.slots.split(","):
            # a fresh host per slot: one host switching categories slows to about 1.5 s per render (measured
            # 2026-10-22), which a real batch (one slot per job) never does
            if engine is not None:
                engine.close()
            engine = MB.make_engine(144_000, log=lambda *_: None)
            out = base / slot
            out.mkdir(parents=True, exist_ok=True)
            pool = SD.slot_pool(slot, catalog)
            t0 = time.time()
            plan = SD.make_slot_plan(slot, manifest, pool, DD.read_mono_file, splits=splits, seed=a.seed,
                                     limit=a.clips, shard_size=a.clips)
            t_plan = time.time() - t0
            atomic_write_json(out / "plan.json", plan)
            blocks = MB.make_blocks({m: pool[m] for m in plan["models"]}, plan["category"], catalog)
            t0 = time.time()
            DD.work_loop(plan, out, open_engine=lambda: engine, blocks=blocks, emb=emb,
                         read_excerpt=DD.read_mono_file, deadline=1e18, min_free_gb=a.min_free_gb,
                         max_load=a.max_load, label_keys=SD.LABEL_KEYS, log=lambda m: DD.log_line(out, m))
            wall = time.time() - t0
            d = json.loads((out / "shards" / "s00000.json").read_text())
            n = d["n_ok"] + d["n_failed"]
            rows[slot] = {"clips": n, "ok": d["n_ok"], "failed": d["n_failed"], "models_in_pool": len(pool),
                          "render_s_per_clip": d["render_s"] / max(1, n), "embed_s_per_clip": d["embed_s"] / max(1, n),
                          "wall_s_per_clip": wall / max(1, n), "plan_s": t_plan, "failures": d["failures"][:3]}
            print(slot, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rows[slot].items()}),
                  flush=True)
    finally:
        try:
            if engine is not None:
                engine.close()
        except Exception as e:  # noqa: BLE001
            print(f"engine close failed: {e}")
    atomic_write_json(base / "smoke.json", rows, indent=1)
    if a.ledger:
        os.environ.setdefault("TONEMATCH_AGENT", "learning")
        for slot, r in rows.items():
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "render-cost",
                            "--engine", "native", "--dataset", f"slot-dataset-v1-{slot}",
                            "--metric", "wall_s_per_clip_1host", "--value", f"{r['wall_s_per_clip']:.3f}",
                            "--renders", str(r["clips"]), "--wall-clock-s", f"{r['wall_s_per_clip'] * r['clips']:.0f}",
                            "--notes", (f"T208 smoke; 1 Native host; render {r['render_s_per_clip']:.3f} s embed "
                                        f"{r['embed_s_per_clip']:.3f} s per 10 s clip; {r['ok']} ok {r['failed']} "
                                        f"failed; {r['models_in_pool']} models in pool; features dropped after "
                                        "timing; nothing adopted")], check=False, cwd=str(ROOT))
    return 0


# ------------------------------------------------------------------------------------------ cli

def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def slot_arg(p, required=True):
        p.add_argument("--slot", choices=SD.SLOTS, required=required)
        p.add_argument("--out", default="", help="default cache/slot-dataset-v1/<slot>")
        return p

    def guard_args(p):
        p.add_argument("--max-load", type=float, default=None,
                       help="pause new renders above this many BUSY cores; default 0.95 x cores")
        p.add_argument("--min-free-gb", type=float, default=8.5)

    p = slot_arg(sub.add_parser("plan"))
    p.add_argument("--seed", type=int, default=SD.SEED)
    p.add_argument("--target", type=int, default=0, help="clips to plan (default: the slot's recommended count)")
    p.add_argument("--limit", type=int, default=0, help="shakedown: plan only this many clips")
    p.add_argument("--all-native", action="store_true",
                   help="every Native model of the category, not only the approved stand-ins (needs a ruling)")
    p.add_argument("--fresh-splits", action="store_true", help="do not reuse drive-dataset-v1's file split")
    p.add_argument("--force", action="store_true", help="resample a plan that has no rendered shard yet")
    p.add_argument("--silence-db", type=float, default=None,
                   help="silence floor stored in the plan (dBFS mean power) and record each clip's out_db label "
                        "(T212 option B: -240); default: no key, drive's -90 dB floor, no out_db (as before)")
    p = slot_arg(sub.add_parser("render"))
    guard_args(p)
    p.add_argument("--refill", action="store_true", help="render cache/slot-dataset-v1/<slot>-refill (T210)")
    p.add_argument("--hosts", type=int, default=DD.MAX_HOSTS)
    p.add_argument("--hours", type=float, default=2.0)
    p = sub.add_parser("worker")
    p.add_argument("--out", required=True)
    guard_args(p)
    p.add_argument("--supervisor-pid", type=int, default=0)
    p.add_argument("--deadline", type=float, default=0.0)
    sub.add_parser("status")
    p = slot_arg(sub.add_parser("train"))
    p.add_argument("--heads", default="logreg,mlp")
    p.add_argument("--seeds", type=int, default=1, help="MLP seeds for the main arm")
    p.add_argument("--epochs", type=int, default=60)
    p.add_argument("--tol", type=float, default=SD.KNOB_TOL, help="knob tolerance (position 0..1)")
    p.add_argument("--device", default="cpu", help="cpu (as drive-dataset-v1), auto, cuda, mps")
    p.add_argument("--models-dir", default="")
    p.add_argument("--no-ledger", action="store_true")
    p.add_argument("--with-refill", action="store_true", help="also train on cache/slot-dataset-v1/<slot>-refill (T210)")
    p.add_argument("--min-out-db", type=float, default=None,
                   help="drop refill clips whose output mean power is below this (dBFS); default keep all")
    p.add_argument("--fraction", type=float, default=1.0,
                   help="train on the first ceil(F x shards) shards of the plan (T213 half-size arm: 0.5)")
    p = slot_arg(sub.add_parser("refill"))
    p.add_argument("--silence-db", type=float, default=-240.0,
                   help="silence floor for the refill (dBFS mean power); -240 = the engine's digital-zero guard")
    p.add_argument("--keep-digital-zero", action="store_true", help="also retry clips that were digital zero")
    p.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    p = sub.add_parser("smoke")
    p.add_argument("--slots", default=",".join(SD.SLOTS))
    p.add_argument("--clips", type=int, default=12)
    p.add_argument("--seed", type=int, default=SD.SEED)
    p.add_argument("--out", required=True)
    p.add_argument("--ledger", action="store_true")
    guard_args(p)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    return {"plan": cmd_plan, "render": cmd_render, "worker": cmd_worker, "status": cmd_status, "train": cmd_train,
            "smoke": cmd_smoke, "refill": cmd_refill}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
