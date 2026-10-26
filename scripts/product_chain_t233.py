#!/usr/bin/env python3
"""product-chain-v1 (T233, learning): the product-chain predictor overnight. Measurement only; nothing adopted.

Human 2026-10-26 "Do both now" (DECISIONS "product-chain predictor overnight; PRS calibration recordings as a training
player"). Chain drive -> (EQ p 0.5) -> (reverb p 0.6), no amp / cab / delay / mod (src/tonematch/learning/product_chain.py).

    .venv/bin/python scripts/product_chain_t233.py plan                          # seeded chains -> cache/product-chain-v1/chains.json
    .venv/bin/python scripts/product_chain_t233.py render --hosts 4 --deadline "2026-10-26 20:00" --ledger
    .venv/bin/python scripts/product_chain_t233.py train --threads 4 --ledger    # heads, two arms, half and full
    .venv/bin/python scripts/product_chain_t233.py songs                         # the seven songs vs the human's picks
    .venv/bin/python scripts/product_chain_t233.py note                          # docs/notes/product-chain-t233.md
    .venv/bin/python scripts/product_chain_t233.py overnight --deadline "2026-10-26 20:00"   # all of the above, unattended

The overnight supervisor is started under nohup and caffeinate (see docs/TOOLS.md); `kill <PID>` stops it safely:
the render finishes its current shard and closes its Native hosts, a training child is stopped, proc_check runs.
DIs: drive-dataset-v1 train-split Guitar-TECHS P1/P2 files and the human's prs-7-tone session (training part) for
training; the test-split files and the held-out part of the session for evaluation; P3 is never read. Features only
(AFx-Rep mid, 3 x 3.33 s crops of the finished output, lead-in dropped); no audio is kept. Everything stays local.
"""
from __future__ import annotations

import argparse
import json
import math
import os
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

from tonematch.learning import product_chain as PC  # noqa: E402

OUT = ROOT / "cache/product-chain-v1"
MODELS = ROOT / "cache/models/product-chain-v1"
NOTE = ROOT / "docs/notes/product-chain-t233.md"
SESSION = ROOT / "data/guitars/prs-7-tone/session-20261022-164806"
CAL = ROOT / "cache/reverb-pilot-t230/calibration.json"
DRIVE_PLAN = ROOT / "cache/drive-dataset-v1/plan.json"
T229 = ROOT / "cache/colour-t229"
SONGS = ("mother", "seek-destroy", "teen-spirit", "territorial", "satisfaction", "cherub-rock", "ds-sample")
SEED = 233
DATASET = "product-chain-v1"


def log(msg: str, out: Path = OUT) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "run.log", "a") as f:
        f.write(line + "\n")


def parse_deadline(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


# ------------------------------------------------------------------------------------------ DI sources

def lib_files() -> tuple[list[str], list[str]]:
    import drive_dataset_render as DD
    plan = json.loads(DRIVE_PLAN.read_text())
    ok = lambda f: DD.player_of(f) in ("P1", "P2") and "/P3" not in f   # noqa: E731
    train = [f for f in plan["files"]["train"] if ok(f)]
    test = [f for f in plan["files"]["test"] if ok(f)]
    held = set(plan["files"]["test"]) | set(plan["files"]["cal"]) | set(plan["files"]["v1test"])
    assert not set(train) & held, "train files overlap held-out files"
    return train, test


def prs_read(f: str) -> np.ndarray:
    import soundfile as sf
    x, sr = sf.read(str(SESSION / f), dtype="float32", always_2d=True)
    assert sr == PC.SR, f"{f}: {sr} Hz"
    return x.mean(1)


_PRS: dict[str, np.ndarray] = {}


def prs_source(name: str) -> np.ndarray:
    if name not in _PRS:
        spec = {**PC.PRS_TRAIN, **PC.PRS_TEST}[name]
        _PRS[name] = PC.build_source(spec, prs_read)
    return _PRS[name]


def read_excerpt(f: str, start: int, n: int) -> np.ndarray:
    if f.startswith("prs:"):
        return PC.excerpt(prs_source(f), start, n)
    import drive_dataset_render as DD
    return PC.excerpt(DD.read_mono_file(f, int(start), int(n)), 0, int(n))


def read_whole(f: str) -> np.ndarray:
    if f.startswith("prs:"):
        return prs_source(f)
    import drive_dataset_render as DD
    return DD.read_mono_file(f)


def source_starts(f: str) -> list[int]:
    import drive_dataset_render as DD
    x = read_whole(f)
    s = DD.active_starts(x)
    return s if s or not f.startswith("prs:") else PC.fallback_starts(x)


# ------------------------------------------------------------------------------------------ plan

def cmd_plan(a) -> int:
    from tonematch.dry_rebuild import loud_level_db, peak_db
    from tonematch.engines.elastic import atomic_write_json
    import chain_transfer as T214
    out = Path(a.out)
    pp = out / "chains.json"
    if pp.is_file():
        if any((out / "shards").glob("*.json")):
            print(f"{pp} exists and shards are rendered; refusing to re-plan")
            return 2
        if not a.force:
            print(f"chain list exists: {pp} (--force to resample; nothing rendered yet)")
            return 0
    import soundfile as sf
    dur = {f: sf.info(str(SESSION / f)).duration for parts in {**PC.PRS_TRAIN, **PC.PRS_TEST}.values() for f, _, _ in parts}
    PC.check_prs_split(durations=dur)
    train_f, test_f = lib_files()
    cal = json.loads(CAL.read_text())["rows"]
    dpool, _ = T214.drive_pool_knobs()
    starts = {f: source_starts(f) for f in train_f + test_f + list(PC.PRS_TRAIN) + list(PC.PRS_TEST)}
    for f in list(PC.PRS_TRAIN) + list(PC.PRS_TEST):
        log(f"{f}: {len(prs_source(f)) / PC.SR:.1f} s, {len(starts[f])} window starts", out)
    lib_tr = [f for f in train_f if starts[f]]
    prs_tr = [f for f in PC.PRS_TRAIN if starts[f]]
    w = {f: (1.0 - a.prs_frac) / len(lib_tr) for f in lib_tr}
    tot = sum(len(starts[f]) for f in prs_tr)
    w.update({f: a.prs_frac * len(starts[f]) / tot for f in prs_tr})
    sets = [("train", "t", lib_tr + prs_tr, a.n, a.seed, w),
            ("lib-test", "e", test_f, a.n_lib_test, a.seed + 1, None),
            ("prs-test", "h", [f for f in PC.PRS_TEST if starts[f]], a.n_prs_test, a.seed + 2, None)]
    chains_by = {}
    for split, pre, srcs, n, seed, wt in sets:
        chains_by[split] = PC.make_chains(srcs, starts, dpool, cal, n=n, seed=seed, split=split, prefix=pre,
                                          source_weights=wt)
    n10 = int(PC.CLIP_S * PC.SR)
    for split, cs in chains_by.items():
        by_file: dict[str, list[dict]] = {}
        for c in cs:
            by_file.setdefault(c["di_file"], []).append(c)
        for f, cc in by_file.items():
            x = read_whole(f)
            for c in cc:
                seg = PC.excerpt(x, c["start"], n10 + c["lead"])
                raw = loud_level_db(seg[c["lead"]:])
                c["raw_level_db"] = round(raw, 2)
                c["peak_dbfs"] = round(peak_db(seg), 2)
                c["gain_db"] = round(PC.level_gain(c["gain_draw_db"], c["peak_dbfs"]), 2)
                c["di_level_db"] = round(raw + c["gain_db"], 2)
    assert not {c["di_file"] for c in chains_by["train"]} & set(test_f)
    assert not {c["di_file"] for c in chains_by["train"]} & set(PC.PRS_TEST)
    assert not any("/P3" in c["di_file"] for cs in chains_by.values() for c in cs)
    shards = []
    for split, pre in (("lib-test", "e"), ("prs-test", "h"), ("train", "s")):
        cs = chains_by[split]
        for j in range(0, len(cs), a.shard):
            shards.append({"id": f"{pre}{j // a.shard:03d}", "split": split, "chains": [c["id"] for c in cs[j:j + a.shard]]})
    chains = chains_by["lib-test"] + chains_by["prs-test"] + chains_by["train"]
    from collections import Counter
    cnt = {s: dict(Counter(b["slot"] for c in cs for b in c["blocks"])) for s, cs in chains_by.items()}
    src = {s: dict(Counter(c["source"] for c in cs)) for s, cs in chains_by.items()}
    doc = {"brief": "T233 product-chain-v1: drive -> (EQ) -> (reverb); Guitar-TECHS P1/P2 train-split + prs-7-tone "
                    "training part for training; test-split + held-out session part for evaluation; P3 never read; "
                    "measurement only, nothing adopted",
           "seed": a.seed, "probs": PC.PROBS, "prs_frac": a.prs_frac, "gain_db": PC.GAIN_DB, "lead_s": PC.LEAD_S,
           "files": {"train": lib_tr, "test": test_f}, "prs_train": PC.PRS_TRAIN, "prs_test": PC.PRS_TEST,
           "session": str(SESSION.relative_to(ROOT)), "starts_n": {f: len(v) for f, v in starts.items()},
           "slot_counts": cnt, "source_counts": src, "shards": shards, "chains": chains}
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(pp, doc)
    print(json.dumps({"chains": len(chains), "shards": len(shards), "slot_counts": cnt, "source_counts": src}, indent=1))
    return 0


# ------------------------------------------------------------------------------------------ render

def make_presets(chains: list[dict]):
    import chain_transfer as T214
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.learning import slot_dataset as SD
    catalog = load_catalog()
    _, dfree = T214.drive_pool_knobs()
    need: dict[str, set] = {}
    for c in chains:
        for b in c["blocks"]:
            need.setdefault(b["slot"], set()).add(b["model"])
    blocks: dict[str, dict] = {}
    for slot, ms in need.items():
        if slot == "drive":
            blocks[slot] = MB.make_blocks({m: dfree[m] for m in ms}, "drive", catalog)
        else:
            full = SD.slot_pool(slot, catalog)
            miss = [m for m in ms if m not in full]
            if miss:
                raise RuntimeError(f"{slot}: {miss} not in this catalog/library")
            blocks[slot] = MB.make_blocks({m: full[m] for m in ms}, SD.SLOT_CONFIG[slot]["category"], catalog)

    def presets(b):
        blk = blocks[b["slot"]][b["model"]]
        return blk.with_values(blk.from_knobs(b["cc"]) if "cc" in b else b["values"])
    return presets


def write_shard(sdir: Path, s: dict, chains: list[dict], res: dict, curves: dict, extra: dict) -> int:
    import drive_dataset_render as DD
    from tonematch.engines.elastic import atomic_write_bytes, atomic_write_json, atomic_write_text
    has = res["has"]
    rows, crops = [], []
    for i, c in enumerate(chains):
        if c["id"] in res["failed"] or not has[i].any():
            continue
        k = int(np.where(has[i])[0][-1])
        crops.append(res["crops"][i, k])
        blocks = [dict(b) for b in c["blocks"]]
        for b in blocks:
            if b["slot"] == "eq":
                b["curve_db"] = curves.get(i)
        rows.append({**c, "blocks": blocks, "out_db": float(res["out_db"][i, k])})
    C = np.stack(crops).astype(np.float16) if crops else np.zeros((0, 3, 512), np.float16)
    atomic_write_bytes(sdir / f"{s['id']}.crops.npy", DD.npy_bytes(C))
    atomic_write_text(sdir / f"{s['id']}.labels.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    atomic_write_json(sdir / f"{s['id']}.json", {"id": s["id"], "split": s["split"], "n_ok": len(rows),
                                                 "failed": res["failed"], "stage_s": res["stage_s"], **extra}, indent=1)
    return len(rows)


def shard_stats(out: Path) -> dict:
    plan = json.loads((Path(out) / "chains.json").read_text())
    sdir = Path(out) / "shards"
    st = {"shards_total": len(plan["shards"]), "shards_done": 0, "chains_ok": 0, "chains_failed": 0, "wall_s": 0.0,
          "blocks": 0, "by_split": {}}
    for s in plan["shards"]:
        f = sdir / f"{s['id']}.json"
        b = st["by_split"].setdefault(s["split"], {"done": 0, "total": 0, "ok": 0})
        b["total"] += 1
        if f.is_file():
            try:
                d = json.loads(f.read_text())
            except ValueError:
                continue
            st["shards_done"] += 1
            b["done"] += 1
            b["ok"] += d["n_ok"]
            st["chains_ok"] += d["n_ok"]
            st["chains_failed"] += len(d["failed"])
            st["wall_s"] += d.get("wall_s", 0.0)
            st["blocks"] += d.get("blocks", 0)
    return st


def cmd_render(a) -> int:
    import chain_transfer as T214
    from tonematch.engines import host_budget as hb
    from tonematch.engines.native import RenderJob
    from tonematch.learning import chain_transfer as CT
    out = Path(a.out)
    pidf = out / "render.pid"
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
    assert not any("/P3" in c["di_file"] for c in doc["chains"]), "P3 in the plan"
    sdir = out / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    stop = {"flag": False}

    def on_term(*_):
        stop["flag"] = True
        log("SIGTERM: stopping after the current shard", out)
    signal.signal(signal.SIGTERM, on_term)
    deadline = parse_deadline(a.deadline) if a.deadline else time.time() + a.hours * 3600
    todo = [s for s in doc["shards"] if not (sdir / f"{s['id']}.json").is_file()]
    if a.max_shards:
        todo = todo[: a.max_shards]
    log(f"render: {len(todo)} of {len(doc['shards'])} shards to do, hosts <= {a.hosts}, deadline "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))}, pid {os.getpid()}", out)
    presets = make_presets(doc["chains"])
    open_pool = T214.pool_opener(a.hosts)
    guard = hb.LoadGuard(a.max_busy)
    embed = T214.embed_crops_fn()
    t_run, per_chain, done_n, n_blocks, reason = time.time(), a.est_s_per_chain, 0, 0, "finished"
    n10 = int(PC.CLIP_S * PC.SR)
    try:
        for s in todo:
            if stop["flag"]:
                reason = "SIGTERM"
                break
            if time.time() + per_chain * len(s["chains"]) > deadline:
                reason = "deadline"
                break
            chains = [byid[i] for i in s["chains"]]
            curves: dict[int, list[float]] = {}

            def hook(slot, i, x_in, y_out, chains=chains, curves=curves):
                if slot == "eq":
                    L = int(chains[i]["lead"])
                    curves[i] = PC.eq_curve_db(x_in[L:L + n10], y_out[L:L + n10])
            t0 = time.time()
            res = CT.run_chains(chains, presets, read_excerpt, open_pool, embed,
                                make_job=lambda d, p: RenderJob(dry=d, preset=p), guard=guard,
                                log=lambda m, sid=s["id"]: log(f"{sid} {m}", out), chunk=a.chunk,
                                order=PC.ORDER, embed_stages="final", stage_hook=hook)
            wall = time.time() - t0
            nb = sum(len(c["blocks"]) for c in chains if c["id"] not in res["failed"])
            k = write_shard(sdir, s, chains, res, curves, {"wall_s": round(wall, 1), "blocks": nb,
                                                            "finished": time.time(), "hosts": a.hosts})
            per_chain = wall / max(1, len(chains))
            done_n += k
            n_blocks += nb
            log(f"{s['id']} ({s['split']}): {k} chains ok, {len(res['failed'])} dropped, {wall:.0f} s "
                f"({per_chain:.2f} s/chain, {wall / max(1, nb):.2f} s/block)", out)
    finally:
        pidf.unlink(missing_ok=True)
    wall = time.time() - t_run
    log(f"render stopped ({reason}): {done_n} chains, {n_blocks} blocks in {wall:.0f} s this run", out)
    if a.ledger and done_n:
        ledger(["--kind", "render-cost", "--engine", "native", "--dataset", DATASET, "--metric", "wall_s_per_chain",
                "--value", f"{wall / done_n:.3f}", "--renders", str(n_blocks), "--wall-clock-s", f"{wall:.0f}",
                "--notes", f"T233 block-serial product-chain renders (drive, EQ p0.5, reverb p0.6), {done_n} chains "
                f"this run, {n_blocks / done_n:.2f} blocks/chain, <= {a.hosts} hosts, fresh pool per stage, final-output "
                f"AFx-Rep embedding included; stop {reason}; train-split + PRS training DIs, test-split + held-out PRS "
                f"for evaluation; P3 unread; features only"])
    return 0


def ledger(args: list[str]) -> None:
    os.environ.setdefault("TONEMATCH_AGENT", "learning")
    subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), *args], check=False, cwd=str(ROOT))


# ------------------------------------------------------------------------------------------ train

def cmd_train(a) -> int:
    import chain_transfer as T214
    from tonematch.learning import product_chain_train as TR
    dpool, _ = T214.drive_pool_knobs()
    TR.run(Path(a.out), Path(a.models), dpool, arms=tuple(a.arms.split(",")),
           fractions=tuple(float(x) for x in a.fractions.split(",")), heads=tuple(a.heads.split(",")),
           threads=a.threads, folds=a.folds, epochs=a.epochs, aux_epochs=a.aux_epochs, uncond=not a.no_uncond,
           resume=not a.no_resume, log=lambda m: log(m, Path(a.out)), ledger=a.ledger)
    return 0


# ------------------------------------------------------------------------------------------ seven songs

def human_reverb(song: str) -> dict:
    """The human's closest-ranked reverb item(s) per session (T230 pilot; T232 confirmation where run)."""
    cal = json.loads(CAL.read_text())
    out = {}
    pk = ROOT / "cache/ratings/reverb-pilot-v1.key.json"
    settings = json.loads(pk.read_text())["items"] if pk.is_file() else {}    # T232 reuses the T230 item ids
    for sess in ("reverb-pilot-v1", "reverb-confirm-v1"):
        kp, ap = ROOT / f"cache/ratings/{sess}.key.json", ROOT / f"reports/local/ratings/{sess}.answers.json"
        if not (kp.is_file() and ap.is_file()):
            continue
        key, ans = json.loads(kp.read_text()), json.loads(ap.read_text())
        ks, rk = key["songs"].get(song), ans["songs"].get(song, {}).get("ranks", {})
        if not ks or not rk:
            continue
        best = min(rk.values())
        items = sorted({ks["letters"][L] for L, r in rk.items() if r == best})
        desc = []
        for it in items:
            st = settings[it]
            rows = cal["rows"][st["model"]]
            desc.append({"item": it, "type": st["type"], "mix": st["mix"],
                         "rt60_s": round(PC.rt60_for_decay(rows, st["decay"]), 2)})
        out[sess] = {"best_rank": best, "items": desc}
    return out


def cmd_songs(a) -> int:
    import soundfile as sf
    import chain_transfer as T214
    from tonematch.dry_rebuild import loud_level_db
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text
    from tonematch.learning import product_chain_train as TR
    out, mdir = Path(a.out), Path(a.models)
    embed = T214.embed_crops_fn()
    heads = {}
    for arm in ("lib+prs", "lib"):
        for kind in ("mlp", "logreg"):
            p = mdir / f"drive__{arm.replace('+', '_')}__1__{kind}__cond.pkl"
            if p.is_file():
                heads[f"drive/{arm}/{kind}"] = TR.load_pickle(p)
            for task in TR.AUX_TASKS:
                q = mdir / f"{task}__{arm.replace('+', '_')}__{kind}.pkl"
                if q.is_file():
                    heads[f"{task}/{arm}/{kind}"] = TR.load_pickle(q)["model"]
    rows = []
    n10 = int(PC.CLIP_S * PC.SR)
    for song in SONGS:
        d = T229 / song
        stem_p, dry_p = d / "audio/stem.wav", d / "audio/dry.wav"
        if not stem_p.is_file():
            continue
        stem, _ = sf.read(str(stem_p), dtype="float32", always_2d=True)
        dry, _ = sf.read(str(dry_p), dtype="float32", always_2d=True)
        stem, dry = stem.mean(1), dry.mean(1)
        lev = loud_level_db(dry)
        X = np.asarray(embed(PC.excerpt(stem, 0, n10)), np.float32)[None]
        Xl = PC.with_level(X, np.array([lev]))
        st = json.loads((d / "state.json").read_text())
        pred = {}
        for k, h in heads.items():
            if k.startswith("drive/"):
                pred[k] = TR.drive_predict(h, Xl)[0]
            else:
                v = h.predict(Xl)[0]
                pred[k] = [float(x) for x in np.atleast_1d(v)]
        rows.append({"song": song, "input_level_db": round(lev, 2), "stem_s": round(len(stem) / PC.SR, 1),
                     "human_drive": st["a"]["blocks"][0][0], "t229_eq_gains_db": st.get("c", {}).get("gains_db"),
                     "human_reverb": human_reverb(song), "pred": pred})
    doc = {"brief": "T233 seven songs: the product-chain heads on each T229 stem (first 10 s), input level from the "
                    "T211 calibrated dry; descriptive, no claim", "songs": rows}
    atomic_write_json(out / "songs.json", doc, indent=1, default=float)
    atomic_write_text(out / "songs.md", songs_md(doc))
    print(songs_md(doc))
    return 0


def _short(m: str) -> str:
    m = (m or "").replace("HD2_", "")
    return m[4:] if m.startswith("Dist") else m


def songs_md(doc: dict) -> str:
    rt = list(PC.REVERB_MODELS)
    L = ["| Song | Input level dBFS | Human's drive (T211) | Predicted top-3 (lib+prs MLP, p) | rank of the human's drive "
         "in top-3 | Reverb present p | Type p Room/Plate/Hall | Mix | RT60 s | Human's closest reverb (T230; T232) |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    E = ["| Song | EQ present p | Parametric / Graphic p | Predicted stage curve 125 Hz..16 kHz (dB) | "
         "T229 fitted graphic gains 125 Hz..16 kHz (dB) |", "|---|---|---|---|---|"]
    for r in doc["songs"]:
        p = r["pred"]
        dr = p.get("drive/lib+prs/mlp", {})
        top = dr.get("top3", [])
        order = dr.get("ranking", [])
        rank = order.index(r["human_drive"]) + 1 if r["human_drive"] in order else "-"
        f = lambda k, j=0: (f"{p[k][j]:.2f}" if k in p else "")  # noqa: E731
        typ = "/".join(f"{x:.2f}" for x in p.get("reverb_type/lib+prs/mlp", [])) or ""
        rt60 = f"{math.exp(p['reverb_logrt60/lib+prs/mlp'][0]):.2f}" if "reverb_logrt60/lib+prs/mlp" in p else ""
        hr = []
        for sess, v in r["human_reverb"].items():
            hr.append(("T230 " if sess.startswith("reverb-pilot") else "T232 ") + ", ".join(
                f"{x['type']} m{x['mix']:.2f} {x['rt60_s']:.1f}s" for x in v["items"]))
        L.append(f"| {r['song']} | {r['input_level_db']:.1f} | {_short(r['human_drive'])} | "
                 + ", ".join(f"{_short(m)} {q:.2f}" for m, q in top) + f" | {rank} | {f('reverb_present/lib+prs/mlp')} | "
                 f"{typ} | {f('reverb_mix/lib+prs/mlp')} | {rt60} | {'; '.join(hr) or 'not rated'} |")
        cur = p.get("eq_curve/lib+prs/mlp")
        g = r.get("t229_eq_gains_db") or []
        E.append(f"| {r['song']} | {f('eq_present/lib+prs/mlp')} | "
                 + "/".join(f"{x:.2f}" for x in p.get("eq_type/lib+prs/mlp", [])) + " | "
                 + (" ".join(f"{x:+.1f}" for x in cur) if cur else "") + " | " + " ".join(f"{x:+.1f}" for x in g[2:]) + " |")
    _ = rt
    return "\n".join(L + [""] + E) + "\n"


# ------------------------------------------------------------------------------------------ note

def cmd_note(a) -> int:
    from tonematch.engines.elastic import atomic_write_text
    from tonematch.learning import product_chain_train as TR
    out = Path(a.out)
    plan = json.loads((out / "chains.json").read_text())
    st = shard_stats(out)
    res = json.loads((out / "results.json").read_text()) if (out / "results.json").is_file() else None
    songs = json.loads((out / "songs.json").read_text()) if (out / "songs.json").is_file() else None
    ev = []
    sv = out / "overnight" / "events.json"
    if sv.is_file():
        ev = json.loads(sv.read_text())
    L = ["# Product-chain predictor overnight, product-chain-v1 ([T233](../../board/TASKS.md#t233 \"Product-chain predictor\"))", "",
         f"Owner: learning. Written by `scripts/product_chain_t233.py note`, {time.strftime('%Y-%m-%d %H:%M')} local. "
         "Human approval: DECISIONS 2026-10-26 (\"Do both now\": the overnight predictor, and the human's prs-7-tone "
         "recordings as a training player). **Measurement only: no pass bar, nothing adopted; nothing enters the "
         "matcher until the human hears it.** All audio (the session, the stems) stayed local; only features and "
         "numbers are on disk.", "",
         "## Method", "",
         "- **Chain** (the product chain, DECISIONS 2026-10-25): drive always (54 drive-dataset-v1 models, every drawn "
         "knob uniform on the CC grid, level knobs at default) -> EQ with p 0.5 (Parametric: Low/Mid/High gain uniform "
         "+-6 dB, frequencies log-uniform 80-400 / 300-5000 / 1500-8000 Hz, Q default, high-cut log-uniform 5-12 kHz; or "
         "Graphic 10 Band: a smoothed random curve on 125 Hz-16 kHz with its largest band uniform 1.5-6 dB, 31 and 62 Hz "
         "at 0 as the T229 fit) -> reverb with p 0.6 (Room / Plate / Hall, Mix uniform 0.15-0.50, RT60 log-uniform over "
         "the part of 0.8-2.5 s the model reaches, inverted to Decay by the T230 impulse calibration, Predelay 11 ms, "
         "LowCut 150, HighCut 3700). No amp, cab, delay or modulation. The approved 0.5-2 s lead-in before a reverb.",
         "- **DIs.** Training: the 40 drive-dataset-v1 train-split Guitar-TECHS P1/P2 files and the human's prs-7-tone "
         "session (runs b; open chords E/A/D/G/C of c; palm-muted notes and riff e; free playing g, first 15 s), "
         f"PRS share {plan['prs_frac']:.0%} of the training draws. Evaluation: the 18 test-split files (lib-test) and the "
         "held-out session part (free playing second half, E5/A5/D5 power chords; prs-test). P3 never read. Each "
         f"excerpt's input gain uniform +-{plan['gain_db']:.0f} dB (capped so the peak stays <= -1 dBFS); its loud-frame "
         "level after the gain (T194 convention) is a label and the heads' extra input ((level + 26.7) / 10 appended to "
         "every crop, T194 v2cond).",
         "- **Render.** Helix Native, block-serial (T214 path, a fresh pool per stage), <= 4 hosts, nice 10, busy-core "
         "LoadGuard, -240 dBFS floor, one retry; Room/Plate/Hall folded to mono. The EQ stage's curve is measured on the "
         "fly (output / input long-term spectrum, 8 half-octave bands 125 Hz-16 kHz) and kept as the EQ label for both "
         "EQ types. Features only: AFx-Rep mid, 3 x 3.33 s crops of the finished output.",
         "- **Heads.** Drive: the T189 heads (logreg, MLP), model + family + knobs, calibrated on out-of-fold "
         "predictions grouped by DI source, level-conditioned; one unconditioned pair on the largest arm. Colouring: "
         "reverb presence, type, Mix, log RT60; EQ presence, type, stage curve; logistic/ridge and a small MLP, "
         "level-conditioned. Arms `lib` (Guitar-TECHS chains only) and `lib+prs`; half (first half of the finished "
         "training shards, plan order is random) and full. CPU, nice 15.", "",
         "## Counts", "",
         f"Planned: {len(plan['chains']):,} chains in {st['shards_total']} shards; slot counts {plan['slot_counts']}; "
         f"sources {plan['source_counts']}.", "",
         f"Rendered: {st['shards_done']} of {st['shards_total']} shards, {st['chains_ok']:,} chains ok, "
         f"{st['chains_failed']} dropped, {st['blocks']:,} block renders, {st['wall_s'] / 3600:.1f} h of shard wall "
         f"({st['wall_s'] / max(1, st['chains_ok'] + st['chains_failed']):.2f} s per chain). By split: "
         + "; ".join(f"{k} {v['done']}/{v['total']} shards, {v['ok']:,} chains" for k, v in st["by_split"].items()) + ".", ""]
    if res:
        L += [TR.results_md(res).split("\n", 2)[2], ""]
        L += half_full_md(res) + [""] + arm_md(res) + [""]
    else:
        L += ["Training did not produce results.json.", ""]
    if songs:
        L += ["## The seven songs (descriptive; no claim)", "",
              "Each T229 stem's first 10 s through the lib+prs full heads (MLP), input level from the T211 calibrated "
              "dry (the human's PRS rebuild). The human's drive is the T211 fine-tuned match the human liked; the "
              "human's reverb is the closest-ranked item of the blind T230 pages (and T232 for Mother and Territorial); "
              "Cherub Rock was excluded by the human from T230. Note the stems are separated from full mixes and the "
              "songs' real chains include amps, cabs and room sound the product chain does not model.", "",
              songs_md(songs), ""]
    L += ["## What went wrong", ""]
    probs = [e for e in ev if e.get("problem")]
    L += [f"- {e.get('when', '')} {e.get('step', '')}: {e['problem']}" for e in probs] or ["- Nothing recorded by the supervisor."]
    if st["chains_failed"]:
        L.append(f"- {st['chains_failed']} chains dropped by the renderer (failed twice or silent); listed per shard in "
                 "`cache/product-chain-v1/shards/*.json`.")
    if st["shards_done"] < st["shards_total"]:
        L.append(f"- The render stopped before the plan's end ({st['shards_done']} of {st['shards_total']} shards); the "
                 "plan is shuffled, so the finished part is a random subset, and it is resumable.")
    L += ["", "## Files", "",
          "- Code: `scripts/product_chain_t233.py`, `src/tonematch/learning/product_chain.py`, "
          "`src/tonematch/learning/product_chain_train.py`; `chain_transfer.run_chains` gained a per-chain input gain and "
          "a stage hook (old callers unchanged). Tests: `tests/test_product_chain.py`.",
          "- Data (local, gitignored): `cache/product-chain-v1/` (chains.json, shards, results.json/md, songs.json/md, "
          "run.log, overnight/). Heads: `cache/models/product-chain-v1/` (pilot only, not adopted, not in models/; "
          "Native-derived, local use).",
          "- Ledger: `render-cost / native / product-chain-v1` and `benchmark / native / product-chain-v1` rows.", ""]
    atomic_write_text(Path(a.note), "\n".join(L))
    print(f"wrote {a.note}")
    return 0


def half_full_md(res: dict) -> list[str]:
    F = res["fits"]
    L = ["## Half -> full (lib+prs arm, MLP)", "", "| Head | Set | Half | Full |", "|---|---|---|---|"]
    for e in ("lib-test", "prs-test"):
        for k, lab in (("top3", "drive top-3"), ("knob", "drive knobs +-0.15")):
            h = F.get("drive/lib+prs/0.5/mlp/cond", {}).get(e, {}).get("all", {}).get(k)
            f = F.get("drive/lib+prs/1/mlp/cond", {}).get(e, {}).get("all", {}).get(k)
            L.append(f"| {lab} | {e} | {_f(h)} | {_f(f)} |")
        for task, k in (("reverb_present", "balanced"), ("reverb_type", "acc"), ("reverb_mix", "mae"),
                        ("reverb_logrt60", "mae"), ("eq_present", "balanced"), ("eq_type", "acc"), ("eq_curve", "mae")):
            h = F.get(f"{task}/lib+prs/0.5/mlp", {}).get(e, {}).get(k)
            f = F.get(f"{task}/lib+prs/1/mlp", {}).get(e, {}).get(k)
            L.append(f"| {task} {k} | {e} | {_f(h)} | {_f(f)} |")
    return L


def arm_md(res: dict) -> list[str]:
    F = res["fits"]
    L = ["## Library-only vs library + PRS (full, MLP; the prs-test column is the human's own guitar)", "",
         "| Head | lib arm on lib-test | lib+prs on lib-test | lib arm on prs-test | lib+prs on prs-test |",
         "|---|---|---|---|---|"]
    for k, lab in (("top1", "drive top-1"), ("top3", "drive top-3"), ("knob", "drive knobs +-0.15")):
        v = [F.get(f"drive/{arm}/1/mlp/cond", {}).get(e, {}).get("all", {}).get(k)
             for e in ("lib-test", "prs-test") for arm in ("lib", "lib+prs")]
        L.append(f"| {lab} | " + " | ".join(_f(x) for x in v) + " |")
    for task, k in (("reverb_present", "balanced"), ("reverb_type", "acc"), ("reverb_mix", "mae"),
                    ("reverb_logrt60", "mae"), ("eq_present", "balanced"), ("eq_type", "acc"), ("eq_curve", "mae")):
        v = [F.get(f"{task}/{arm}/1/mlp", {}).get(e, {}).get(k) for e in ("lib-test", "prs-test") for arm in ("lib", "lib+prs")]
        L.append(f"| {task} {k} | " + " | ".join(_f(x) for x in v) + " |")
    u = [F.get("drive/lib+prs/1/mlp/uncond", {}).get(e, {}).get("all", {}).get("knob") for e in ("lib-test", "prs-test")]
    L += ["", f"Level input off (lib+prs full MLP, knobs +-0.15): lib-test {_f(u[0])}, prs-test {_f(u[1])}."]
    return L


def _f(x) -> str:
    return "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.3f}"


# ------------------------------------------------------------------------------------------ overnight supervisor

class Overnight:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.dir = self.out / "overnight"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path, self.status_path = self.dir / "overnight.log", self.dir / "status.txt"
        self.py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
        self.deadline = parse_deadline(a.deadline)
        self.events: list[dict] = []
        self.child: subprocess.Popen | None = None
        self.stop = False
        self.phase = "starting"
        nice0 = os.nice(0)
        self.render_nice, self.train_nice = max(0, 10 - nice0), max(0, 15 - nice0)

    def log(self, m: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}"
        with open(self.log_path, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def status(self) -> None:
        from tonematch.engines.elastic import atomic_write_json, atomic_write_text
        parts = [time.strftime("%Y-%m-%d %H:%M:%S"), f"phase: {self.phase}",
                 f"render deadline {time.strftime('%m-%d %H:%M', time.localtime(self.deadline))}"]
        try:
            st = shard_stats(self.out)
            parts.append(f"shards {st['shards_done']}/{st['shards_total']}, chains ok {st['chains_ok']:,}, dropped "
                         f"{st['chains_failed']}")
            if st["chains_ok"]:
                parts.append(f"{st['wall_s'] / max(1, st['chains_ok']):.2f} s/chain")
        except Exception:  # noqa: BLE001  (a status line must never stop the run)
            pass
        if self.child is not None:
            parts.append(f"child PID {self.child.pid}")
        parts.append(f"supervisor PID {os.getpid()}")
        atomic_write_text(self.status_path, " | ".join(parts) + "\n")
        atomic_write_json(self.dir / "events.json", self.events, indent=1, default=str)

    def event(self, step: str, **kw) -> None:
        self.events.append({"step": step, "when": time.strftime("%H:%M"), **kw})
        self.status()

    def run(self, name: str, argv: list[str], nice: int) -> int:
        cmd = (["nice", "-n", str(nice)] if nice else []) + [self.py, str(ROOT / "scripts/product_chain_t233.py"), *argv]
        env = dict(os.environ)
        env.setdefault("TONEMATCH_AGENT", "learning")
        env["TONEMATCH_AFXREP_DEVICE"] = "cpu"
        if name != "render":
            t = str(self.a.train_threads)
            env.update({"OMP_NUM_THREADS": t, "OPENBLAS_NUM_THREADS": t, "MKL_NUM_THREADS": t, "VECLIB_MAXIMUM_THREADS": t})
        t0 = time.time()
        with open(self.dir / f"{name}.log", "ab") as lf:
            self.child = p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                              cwd=str(ROOT), env=env)
            self.log(f"{name}: started PID {p.pid}: {' '.join(argv)}")
            self.status()
            termed = False
            while p.poll() is None:
                if self.stop and not termed:
                    self.log(f"{name}: stop requested; SIGTERM to PID {p.pid}")
                    p.terminate()
                    termed = True
                time.sleep(5)
                if int(time.time()) % 60 < 5:
                    self.status()
        self.child = None
        self.log(f"{name}: PID {p.pid} exited {p.returncode} after {time.time() - t0:.0f} s")
        self.event(name, rc=p.returncode, wall_s=round(time.time() - t0),
                   **({"problem": f"{name} exited {p.returncode} (see overnight/{name}.log)"}
                      if p.returncode not in (0, None) and not termed else {}))
        return p.returncode

    def proc_check(self, after: str) -> None:
        r = subprocess.run([self.py, str(ROOT / "scripts/proc_check.py")], capture_output=True, text=True, cwd=str(ROOT))
        if r.returncode != 0:
            time.sleep(10)
            r = subprocess.run([self.py, str(ROOT / "scripts/proc_check.py")], capture_output=True, text=True, cwd=str(ROOT))
        txt = (r.stdout + r.stderr).strip().replace("\n", " / ")
        self.log(f"proc_check after {after}: exit {r.returncode}: {txt[:400]}")
        if r.returncode != 0:
            self.event("proc_check", problem=f"after {after}: {txt[:300]}")

    def mark_board(self) -> None:
        p = ROOT / "board/TASKS.md"
        s = p.read_text()
        tag = '<a id="t233"></a>T233'
        lines = s.splitlines(keepends=True)
        for i, ln in enumerate(lines):
            if tag in ln and ln.startswith("- [~]"):
                lines[i] = ln.rstrip("\n").replace("- [~]", "- [x]", 1) + (
                    f" · done {time.strftime('%Y-%m-%d')} (learning, unattended job): docs/notes/product-chain-t233.md; "
                    "ledger benchmark / native / product-chain-v1\n")
                p.write_text("".join(lines))
                self.log("board: T233 marked done")
                return
        self.log("board: T233 line not found as in progress; left unchanged")

    def main(self) -> int:
        def on_term(*_):
            self.stop = True
            self.log("supervisor: SIGTERM received; stopping the current child safely")
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        a = self.a
        (self.dir / "supervisor.pid").write_text(f"{os.getpid()}\n")
        self.log(f"supervisor PID {os.getpid()} up; render deadline {a.deadline}; out {self.out}")
        try:
            self.proc_check("start")
            if not (self.out / "chains.json").is_file():
                self.phase = "plan"
                plan_args = ["plan", "--out", str(self.out)] + (a.plan_args.split() if a.plan_args else [])
                if self.run("plan", plan_args, self.render_nice) != 0:
                    return 1
            if not self.stop:
                self.phase = "render"
                rargs = ["render", "--out", str(self.out), "--hosts", str(a.hosts), "--deadline", a.deadline,
                         "--chunk", str(a.chunk)] + ([] if a.no_ledger else ["--ledger"])
                self.run("render", rargs, self.render_nice)
                self.proc_check("render")
            if not self.stop:
                self.phase = "train"
                targs = ["train", "--out", str(self.out), "--models", str(a.models), "--threads", str(a.train_threads)]
                targs += (a.train_args.split() if a.train_args else []) + ([] if a.no_ledger else ["--ledger"])
                rc_t = self.run("train", targs, self.train_nice)
                self.proc_check("train")
            else:
                rc_t = None
            if not self.stop and rc_t == 0:
                self.phase = "songs"
                self.run("songs", ["songs", "--out", str(self.out), "--models", str(a.models)], self.train_nice)
            self.phase = "note"
            self.run("note", ["note", "--out", str(self.out), "--note", str(a.note)], 0)
            if not self.stop and rc_t == 0 and not a.no_board:
                self.mark_board()
            self.phase = "done" if not self.stop else "stopped"
        finally:
            self.proc_check("end")
            self.status()
            (self.dir / "supervisor.pid").unlink(missing_ok=True)
            self.log(f"supervisor: {self.phase}")
        return 0


def cmd_overnight(a) -> int:
    pf = Path(a.out) / "overnight" / "supervisor.pid"
    if pf.is_file():
        try:
            os.kill(int(pf.read_text()), 0)
            print(f"a supervisor is already running (PID {pf.read_text().strip()}); refusing")
            return 2
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    return Overnight(a).main()


# ------------------------------------------------------------------------------------------ cli

def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--n", type=int, default=40_000)
    p.add_argument("--n-lib-test", type=int, default=1000)
    p.add_argument("--n-prs-test", type=int, default=600)
    p.add_argument("--prs-frac", type=float, default=0.15)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--shard", type=int, default=400)
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=4)
    r.add_argument("--deadline", default=None, help='"YYYY-MM-DD HH:MM" local; no shard starts that would end after it')
    r.add_argument("--hours", type=float, default=2.0, help="used when --deadline is not given")
    r.add_argument("--chunk", type=int, default=24)
    r.add_argument("--max-busy", type=float, default=None)
    r.add_argument("--max-shards", type=int, default=0)
    r.add_argument("--est-s-per-chain", type=float, default=1.2)
    r.add_argument("--ledger", action="store_true")
    t = sub.add_parser("train")
    t.add_argument("--models", default=str(MODELS))
    t.add_argument("--threads", type=int, default=2)
    t.add_argument("--arms", default="lib,lib+prs")
    t.add_argument("--fractions", default="0.5,1")
    t.add_argument("--heads", default="logreg,mlp")
    t.add_argument("--folds", type=int, default=3)
    t.add_argument("--epochs", type=int, default=60)
    t.add_argument("--aux-epochs", type=int, default=30)
    t.add_argument("--no-uncond", action="store_true")
    t.add_argument("--no-resume", action="store_true")
    t.add_argument("--ledger", action="store_true")
    s = sub.add_parser("songs")
    s.add_argument("--models", default=str(MODELS))
    n = sub.add_parser("note")
    n.add_argument("--note", default=str(NOTE))
    o = sub.add_parser("overnight")
    o.add_argument("--deadline", required=True)
    o.add_argument("--hosts", type=int, default=4)
    o.add_argument("--chunk", type=int, default=24)
    o.add_argument("--models", default=str(MODELS))
    o.add_argument("--note", default=str(NOTE))
    o.add_argument("--train-threads", type=int, default=4)
    o.add_argument("--plan-args", default="", help="extra plan flags (smoke: '--n 40 --shard 20')")
    o.add_argument("--train-args", default="", help="extra train flags (smoke: '--epochs 2 --aux-epochs 2')")
    o.add_argument("--no-ledger", action="store_true")
    o.add_argument("--no-board", action="store_true")
    for x in (p, r, t, s, n, o):
        x.add_argument("--out", default=str(OUT))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    if a.cmd in ("render", "overnight"):
        assert 1 <= a.hosts <= 4, "at most 4 Native hosts"
    return {"plan": cmd_plan, "render": cmd_render, "train": cmd_train, "songs": cmd_songs, "note": cmd_note,
            "overnight": cmd_overnight}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
