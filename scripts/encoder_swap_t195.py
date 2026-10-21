#!/usr/bin/env python3
"""encoder-swap-v1 (T195, learning): permissive encoders (PANNs Cnn14, DAC encoder) in place of AFx-Rep under the
drive-predictor heads, on a subset of drive-dataset-v1. A measurement only: nothing is adopted.

    .venv/bin/python scripts/encoder_swap_t195.py select           # subset of drive-dataset-v1 clips -> subset.json
    .venv/bin/python scripts/encoder_swap_t195.py render --hours 10 # re-render on ONE Native host, embed, drop audio
    .venv/bin/python scripts/encoder_swap_t195.py train            # heads per encoder, paired bootstrap, ledger rows

Subset (seed 195). drive-dataset-v1 (T191) clips that rendered there: 27,000 train clips drawn uniformly from the
105k train clips, and EVERY cal, test (new held-out files) and v1test (T186/T189 test files, new renders) clip. Same
file splits as T191; the T186 old renders are not used (they have no plan entry).

Render. The plan gives model, CC values, DI file and start; each clip is rendered again on Helix Native through
match_block.make_engine / make_blocks (the T191 path, one host, which leases itself from the host budget), then
embedded and the audio dropped. A LoadGuard (busy cores, T193) is checked before every clip. Embeddings: PANNs on CPU
(2 torch threads) and DAC on MPS (or CPU when MPS is absent), each in its own thread; 3 crops of 3.33 s per clip, as
T189. Every 50th clip is also embedded with AFx-Rep and compared with the stored T191 AFx-Rep crops (a re-render
check: the AFx-Rep arm reuses the stored features). Shards of 100 clips, atomic, resumable; SIGTERM stops after the
current clip and drops the unfinished shard.

Train. Feature sets: afx_l2 (the stored T191 AFx-Rep mid crops, unit norm: the T191 input), afx_z, panns_z, dac_z
(z: per-dimension standardisation on the arm's train crops, then / sqrt(d)). Heads, multi-crop pooling,
calibration and arms exactly as drive_dataset_render.run_train (T191/T189). Evaluated on `newtest` and `v1files_new`.
Paired differences against afx_l2 and afx_z: bootstrap over test DI files (2,000 resamples, 95 % percentile).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
os.environ.setdefault("TONEMATCH_AGENT", "learning")
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DD = ROOT / "cache/drive-dataset-v1"
OUT = ROOT / "cache/encoder-swap-t195"
SEED = 195
N_TRAIN = 27_000
SHARD = 100
SR = 48_000
CLIP_N = 480_000
NCROP = 3
AFX_CHECK_EVERY = 50
DATASET = "encoder-swap-v1"
EVALS = ("newtest", "v1files_new")
FEATS = ("afx_l2", "afx_z", "panns_z", "dac_z")
MAX_OWN_GB = 2.3     # features of this task; weights are another ~0.63 GB (cap 3 GB in total)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(OUT / "run.log", "a") as f:
        f.write(line + "\n")


def dir_gb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9


# ------------------------------------------------------------------------------------------ select

def t191_ok() -> dict[str, tuple[str, int]]:
    """clip id -> (T191 shard id, row) for every clip that rendered in drive-dataset-v1."""
    out = {}
    for f in sorted((DD / "shards").glob("s*.labels.jsonl")):
        sid = f.name.split(".")[0]
        if not (DD / "shards" / f"{sid}.json").is_file():
            continue
        for i, line in enumerate(x for x in f.read_text().splitlines() if x.strip()):
            out[json.loads(line)["id"]] = (sid, i)
    return out


def cmd_select(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "subset.json"
    if p.exists() and not a.force:
        print(f"{p} exists")
        return 0
    plan = json.loads((DD / "plan.json").read_text())
    ok = t191_ok()
    rng = np.random.default_rng(SEED)
    by = {s: sorted(c["id"] for c in plan["clips"] if c["split"] == s and c["id"] in ok)
          for s in ("train", "cal", "test", "v1test")}
    tr = sorted(rng.choice(by["train"], size=min(a.n_train, len(by["train"])), replace=False).tolist())
    ids = tr + by["cal"] + by["test"] + by["v1test"]
    order = [ids[int(i)] for i in rng.permutation(len(ids))]
    shards = [{"id": f"e{j // SHARD:04d}", "clips": order[j:j + SHARD]} for j in range(0, len(order), SHARD)]
    sub = {"brief": "T195 encoder-swap-v1 subset of drive-dataset-v1; measurement only", "seed": SEED,
           "counts": {"train": len(tr), "cal": len(by["cal"]), "test": len(by["test"]), "v1test": len(by["v1test"])},
           "train_pool": len(by["train"]), "n": len(order), "shards": shards}
    p.write_text(json.dumps(sub))
    print(json.dumps(sub["counts"]), len(shards), "shards")
    return 0


# ------------------------------------------------------------------------------------------ render

def crops_of(y: np.ndarray) -> list[np.ndarray]:
    y = y[:CLIP_N] if len(y) >= CLIP_N else np.pad(y, (0, CLIP_N - len(y)))
    L = CLIP_N // NCROP
    return [y[k * L:(k + 1) * L] for k in range(NCROP)]


class EmbedThread(threading.Thread):
    """Takes (index, crops) from a queue and fills out[index] with the encoder's (3, d) features."""

    def __init__(self, emb, extra=None):
        super().__init__(daemon=True)
        self.emb, self.extra = emb, extra
        self.q: queue.Queue = queue.Queue(maxsize=6)
        self.out: dict = {}
        self.err: BaseException | None = None
        self.busy_s = 0.0

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                self.q.task_done()
                return
            k, crops = item
            try:
                t0 = time.time()
                self.out[k] = self.emb.embed_batch(crops).astype(np.float16)
                if self.extra is not None:
                    self.extra(k, crops)
                self.busy_s += time.time() - t0
            except BaseException as e:  # noqa: BLE001
                self.err = e
            finally:
                self.q.task_done()


def cmd_render(a) -> int:
    import torch
    torch.set_num_threads(2)
    import drive_dataset_render as D
    import match_block as MB

    from tonematch.catalog import load_catalog
    from tonematch.engines.elastic import atomic_write_bytes, atomic_write_json
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.learning import encoders as E
    from tonematch.metrics import afxrep

    sub = json.loads((OUT / "subset.json").read_text())
    plan = json.loads((DD / "plan.json").read_text())
    clips = {c["id"]: c for c in plan["clips"]}
    ok = t191_ok()
    sdir = OUT / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    todo = [s for s in sub["shards"] if not (sdir / f"{s['id']}.json").is_file()]
    log(f"render: {len(sub['shards']) - len(todo)}/{len(sub['shards'])} shards done; hours {a.hours}")
    if a.max_shards:
        todo = todo[:a.max_shards]
    if not todo:
        return 0
    stop = {"flag": False}

    def on_term(signum, frame):
        stop["flag"] = True
        log(f"signal {signum}: stopping after the current clip")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    guard = LoadGuard(a.max_busy)
    pool, _ = D.drive_pool()
    pool = {m: pool[m] for m in plan["models"]}
    blocks = MB.make_blocks(pool, "drive", load_catalog())
    dac_dev = "mps" if torch.backends.mps.is_available() else "cpu"
    panns = E.PannsEmbedder(threads=2)
    dac = E.DacEmbedder(device=dac_dev)
    afx = afxrep.Embedder(device="cpu", threads=2)
    stored_cache: dict = {}
    checks: dict = {}

    def afx_check(k, crops):
        cid = k[1]
        if k[2] % AFX_CHECK_EVERY:
            return
        sid, row = ok[cid]
        if sid not in stored_cache:
            stored_cache.clear()
            stored_cache[sid] = np.load(DD / "shards" / f"{sid}.crops.npy")
        ref = stored_cache[sid][row].astype(np.float32)
        new = np.stack([f.mid for f in afx.embed_batch(crops)]).astype(np.float32)
        checks[cid] = float(np.min(np.sum(ref * new, 1) / np.linalg.norm(ref, axis=1) / np.linalg.norm(new, axis=1)))
    tp, td = EmbedThread(panns, afx_check), EmbedThread(dac)
    tp.start()
    td.start()
    deadline = time.time() + a.hours * 3600
    engine = MB.make_engine(144_000, log=lambda *_: None)
    n_done = 0
    t_run = time.time()
    try:
        for sh in todo:
            if stop["flag"] or time.time() > deadline:
                break
            own = dir_gb(OUT)
            if own > MAX_OWN_GB or D.free_gb(OUT) < 8.5:
                log(f"disk stop: own {own:.2f} GB, free {D.free_gb(OUT):.1f} GB")
                break
            ids, fails, t_r, paused = [], [], 0.0, 0.0
            tp.out.clear()
            td.out.clear()
            for j, cid in enumerate(sh["clips"]):
                if stop["flag"]:
                    break
                paused += guard.wait(lambda d: log(f"busy cores {d:.1f}: pausing"), poll_s=5)
                c = clips[cid]
                dry = D.read_mono_file(c["di_file"], c["start"], CLIP_N)
                y, err = None, None
                t0 = time.time()
                for attempt in range(2):
                    try:
                        y = D.render_one(engine, blocks[c["model"]], c, dry)
                        break
                    except Exception as e:  # noqa: BLE001
                        err = f"{type(e).__name__}: {e}"[:200]
                t_r += time.time() - t0
                if y is None:
                    fails.append({"id": cid, "error": err})
                    continue
                cr = crops_of(y)
                k = (sh["id"], cid, len(ids) + n_done)
                ids.append(cid)
                tp.q.put((k, cr))
                td.q.put((k, cr))
                for t in (tp, td):
                    if t.err:
                        raise RuntimeError(f"embed thread failed: {t.err!r}")
            if stop["flag"]:
                break
            tp.q.join()
            td.q.join()
            for t in (tp, td):
                if t.err:
                    raise RuntimeError(f"embed thread failed: {t.err!r}")
            keys = sorted(tp.out, key=lambda k: k[2])
            assert [k[1] for k in keys] == ids
            P = np.stack([tp.out[k] for k in keys]) if keys else np.zeros((0, NCROP, 2048), np.float16)
            Q = np.stack([td.out[k] for k in keys]) if keys else np.zeros((0, NCROP, 2048), np.float16)
            atomic_write_bytes(sdir / f"{sh['id']}.panns.npy", D.npy_bytes(P))
            atomic_write_bytes(sdir / f"{sh['id']}.dac.npy", D.npy_bytes(Q))
            atomic_write_json(sdir / f"{sh['id']}.json", {"id": sh["id"], "ids": ids, "failures": fails,
                                                         "render_s": t_r, "paused_s": paused,
                                                         "afx_check": {c: checks.pop(c) for c in list(checks)
                                                                       if c in set(ids)},
                                                         "finished": time.time()})
            n_done += len(ids)
            el = time.time() - t_run
            log(f"{sh['id']} ok {len(ids)} failed {len(fails)} render {t_r:.0f}s; run {n_done} clips "
                f"{n_done / el:.2f}/s; panns busy {tp.busy_s:.0f}s dac busy {td.busy_s:.0f}s; busy cores "
                f"{guard.last:.1f}")
    finally:
        for t in (tp, td):
            with __import__("contextlib").suppress(Exception):
                t.q.put(None, timeout=5)
        try:
            engine.close()
        except Exception as e:  # noqa: BLE001
            log(f"engine close failed: {e}")
    log(f"render: stopped; {n_done} clips this run")
    return 0


# ------------------------------------------------------------------------------------------ train

def load_features(sub: dict):
    """Labels, and {feat: (n, 3, d) float32} for every subset clip present in all encoders' shards."""
    sdir = OUT / "shards"
    plan = json.loads((DD / "plan.json").read_text())
    clips = {c["id"]: c for c in plan["clips"]}
    ok = t191_ok()
    ids, P, Q = [], [], []
    checks = {}
    for s in sub["shards"]:
        f = sdir / f"{s['id']}.json"
        if not f.is_file():
            continue
        d = json.loads(f.read_text())
        p, q = np.load(sdir / f"{s['id']}.panns.npy"), np.load(sdir / f"{s['id']}.dac.npy")
        if len(p) != len(d["ids"]) or len(q) != len(d["ids"]):
            continue
        ids += d["ids"]
        P.append(p)
        Q.append(q)
        checks.update(d.get("afx_check", {}))
    A = np.zeros((len(ids), NCROP, 512), np.float32)
    by_shard: dict = {}
    for i, cid in enumerate(ids):
        sid, row = ok[cid]
        by_shard.setdefault(sid, []).append((i, row))
    for sid, rows in by_shard.items():
        cr = np.load(DD / "shards" / f"{sid}.crops.npy")
        for i, row in rows:
            A[i] = cr[row]
    labels = [clips[c] for c in ids]
    return labels, {"afx": A, "panns": np.concatenate(P).astype(np.float32),
                    "dac": np.concatenate(Q).astype(np.float32)}, checks


def zprep(X: np.ndarray, tr: np.ndarray) -> np.ndarray:
    flat = X[tr].reshape(-1, X.shape[-1])
    mu, sd = flat.mean(0), flat.std(0) + 1e-6
    return ((X - mu) / sd / math.sqrt(X.shape[-1])).astype(np.float32)


def per_clip(out: dict, y_m, K, mod_fam, cal) -> dict:
    """Per-clip hit vectors, mirroring afx_heads.score (nan where a knob is absent)."""
    from tonematch.learning import afx_heads as ah
    T = cal.T if cal else 1.0
    lj, lf = ah.joint_logp(out["fl"], out["lc"], mod_fam, T)
    rank = np.argsort(-lj, 1)
    top1 = rank[:, 0]
    idx = np.arange(len(y_m))
    r = {"model_top1": (top1 == y_m).astype(float), "model_top5": (rank[:, :5] == y_m[:, None]).any(1).astype(float),
         "family_top1": (lf.argmax(1) == mod_fam[y_m]).astype(float)}
    for k, name in ((0, "drive"), (1, "tone")):
        ok = np.isfinite(K[:, k])
        for who, mm in (("true", y_m), ("top1", top1)):
            err = np.abs(K[:, k] - out["mu"][idx, mm, k])
            hit = np.where(np.isfinite(err), err <= ah.TOL[k], False).astype(float)
            hit[~ok] = np.nan
            r[f"{name}_within_{who}"] = hit
    return r


METRICS = ("model_top1", "model_top5", "family_top1", "drive_within_true", "drive_within_top1", "tone_within_true",
           "tone_within_top1")


def paired_boot(a: np.ndarray, b: np.ndarray, files: np.ndarray, n: int = 2000, seed: int = SEED) -> dict:
    """Mean of a - b over clips, 95 % percentile interval resampling whole files (clips with nan dropped)."""
    ok = np.isfinite(a) & np.isfinite(b)
    a, b, files = a[ok], b[ok], files[ok]
    uf, inv = np.unique(files, return_inverse=True)
    s = np.bincount(inv, weights=a - b, minlength=len(uf))
    c = np.bincount(inv, minlength=len(uf)).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(uf), size=(n, len(uf)))
    bs = s[draws].sum(1) / c[draws].sum(1)
    return {"diff": float(s.sum() / c.sum()), "lo": float(np.percentile(bs, 2.5)), "hi": float(np.percentile(bs, 97.5)),
            "files": len(uf), "clips": int(c.sum())}


def cmd_train(a) -> int:
    import pickle
    import warnings

    import torch
    torch.set_num_threads(2)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    from sklearn.model_selection import GroupKFold

    from tonematch.learning import afx_heads as ah
    from tonematch.learning import drive_pilot as dp
    t_start = time.time()
    sub = json.loads((OUT / "subset.json").read_text())
    L, F, checks = load_features(sub)
    fams, models, m2f = dp.families()
    mi = {m: i for i, m in enumerate(models)}
    fi = {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[m2f[m]] for m in models])
    y_m = np.array([mi[r["model"]] for r in L])
    K = np.array([[float(r["drive"]), float(r["tone"])] for r in L], float)
    files = np.array([r["di_file"] for r in L])
    player = np.array([r["player"] for r in L])
    split = np.array([r["split"] for r in L])
    assert not any(p == "P3" for p in player)
    ev = {"newtest": split == "test", "v1files_new": split == "v1test"}
    assert not (set(files[ev["newtest"] | ev["v1files_new"]]) & set(files[(split == "train") | (split == "cal")]))
    cks = np.array(list(checks.values())) if checks else np.zeros(0)
    res = {"brief": "T195 encoder-swap-v1: AFx-Rep vs PANNs Cnn14 vs DAC-24k encoders under the T189/T191 heads; "
                    "measurement only, nothing adopted",
           "counts": {k: {"clips": int(m.sum()), "files": len(set(files[m]))} for k, m in
                      {"train": split == "train", "cal": split == "cal", **ev}.items()},
           "afx_rerender_check": {"n": len(cks), "min_cos_min": float(cks.min()) if len(cks) else None,
                                  "median_cos_min": float(np.median(cks)) if len(cks) else None,
                                  "frac_below_0.99": float((cks < 0.99).mean()) if len(cks) else None},
           "results": {}, "paired": {}}
    log(json.dumps(res["counts"]) + " " + json.dumps(res["afx_rerender_check"]))
    alpha = 1.0
    try:
        alpha = float(json.loads((ROOT / "cache/models/predictor-pilot-v1/result.json").read_text())
                      .get("ridge_alpha", 1.0))
    except (OSError, ValueError):
        pass
    res["ridge_alpha"] = alpha
    feats = [f for f in a.feats.split(",") if f]
    heads = a.heads.split(",")
    arms = a.arms.split(",")
    hits: dict = {}
    md = ROOT / "cache/models/encoder-swap-t195"
    md.mkdir(parents=True, exist_ok=True)

    def make(head, seed=0):
        return ah.LinearHeads(mod_fam=mod_fam, alpha=alpha) if head == "logreg" else \
            ah.MLPHeads(mod_fam=mod_fam, seed=seed, epochs=a.epochs)
    for arm in arms:
        pl_tr = {"main": None, "p2p1": "P2", "p1p2": "P1"}[arm]
        pl_te = {"main": None, "p2p1": "P1", "p1p2": "P2"}[arm]
        trm, cam = split == "train", split == "cal"
        if pl_tr:
            trm, cam = trm & (player == pl_tr), cam & (player == pl_tr)
        tr, ca = np.where(trm)[0], np.where(cam)[0]
        for feat in feats:
            enc, prep = feat.split("_")
            X = F[enc] if prep == "l2" else zprep(F[enc], tr)
            for head in heads:
                key = f"{arm}/{feat}/{head}"
                t0 = time.time()
                h = make(head).fit(X[tr], y_m[tr], K[tr])
                outs = {}
                g = files[tr]
                for f_tr, f_va in GroupKFold(min(5, len(set(g)))).split(tr, groups=g):
                    hh = make(head).fit(X[tr[f_tr]], y_m[tr[f_tr]], K[tr[f_tr]])
                    o = hh.predict(X[tr[f_va]])
                    for i, j in enumerate(tr[f_va]):
                        outs[j] = {k: v[i] for k, v in o.items()}
                po = {k: np.stack([outs[j][k] for j in tr]) for k in ("fl", "lc", "mu", "sig")}
                pidx = tr
                if len(ca):
                    o_ca = h.predict(X[ca])
                    po = {k: np.concatenate([po[k], o_ca[k]]) for k in po}
                    pidx = np.concatenate([tr, ca])
                cal = ah.calibrate(po, y_m[pidx], K[pidx], mod_fam, files[pidx], player[pidx])
                r = {"train_clips": len(tr), "train_files": len(set(files[tr])), "cal_clips": len(ca)}
                for ename, em in ev.items():
                    te = np.where(em & (player == pl_te) if pl_te else em)[0]
                    o = h.predict(X[te])
                    r[ename] = ah.score(o, y_m[te], K[te], mod_fam, cal)
                    r[ename]["files"] = len(set(files[te]))
                    hits[(key, ename)] = (per_clip(o, y_m[te], K[te], mod_fam, cal), files[te])
                    pc = hits[(key, ename)][0]
                    assert abs(np.nanmean(pc["model_top1"]) - r[ename]["model_top1"]) < 1e-9
                r["fit_s"] = time.time() - t0
                res["results"][key] = r
                log(key + " " + json.dumps({e: [round(r[e][m], 3) for m in ("model_top1", "model_top5",
                                                                           "family_top1")] for e in ev})
                    + f" {r['fit_s']:.0f}s")
                if arm == "main":
                    if head == "mlp":
                        h.net = h.net.cpu().state_dict()
                    with open(md / f"heads_{arm}_{feat}_{head}.pkl", "wb") as fh:
                        pickle.dump({"heads": h, "calibration": cal, "models": models, "families": fams,
                                     "feature": feat, "note": "T195 measurement only; not adopted"}, fh)
                (OUT / "result.json").write_text(json.dumps(res, indent=1, default=float))
    for (key, ename), (pc, fl) in hits.items():
        arm, feat, head = key.split("/")
        if feat.startswith("afx"):
            continue
        for ref in ("afx_l2", "afx_z"):
            rk = (f"{arm}/{ref}/{head}", ename)
            if rk not in hits:
                continue
            rc, rf = hits[rk]
            assert (rf == fl).all()
            res["paired"][f"{key}|{ename}|vs_{ref}"] = {m: paired_boot(pc[m], rc[m], fl) for m in METRICS}
    res["elapsed_s"] = time.time() - t_start
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=float))
    (OUT / "summary.md").write_text(summary_md(res))
    if not a.no_ledger:
        ledger(res)
    log(f"train: done in {res['elapsed_s']:.0f}s; {OUT / 'summary.md'}")
    return 0


def _f(v) -> str:
    return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.2f}"


def summary_md(res: dict) -> str:
    out = ["# encoder-swap-v1 (T195): results", "", "Measurement only; nothing adopted.", "",
           "Counts: " + ", ".join(f"{k} {v['clips']} clips / {v['files']} files" for k, v in res["counts"].items()),
           "", f"AFx-Rep re-render check: {json.dumps(res['afx_rerender_check'])}", ""]
    titles = {"main": "Within player", "p2p1": "Cross player P2 -> P1", "p1p2": "Cross player P1 -> P2"}
    for arm, title in titles.items():
        for ename in EVALS:
            rows = [(k, v[ename]) for k, v in res["results"].items() if k.startswith(arm + "/") and ename in v]
            if not rows:
                continue
            out += [f"## {title}: {ename}", "",
                    ("| Feature | Head | Model top-1 | Top-5 | Family | Drive ±0.15 true / own | Tone ±0.10 true / own | "
                     "Drive cover 68 / 95 | ECE cal | n (files) |"), "|---|---|---|---|---|---|---|---|---|---|"]
            for k, r in rows:
                _, feat, head = k.split("/")
                out.append(f"| {feat} | {head} | {_f(r['model_top1'])} | {_f(r['model_top5'])} | "
                           f"{_f(r['family_top1'])} | {_f(r['drive_within_true'])} / {_f(r['drive_within_top1'])} | "
                           f"{_f(r['tone_within_true'])} / {_f(r['tone_within_top1'])} | "
                           f"{_f(r.get('drive_cover68_global_true'))} / {_f(r.get('drive_cover95_global_true'))} | "
                           f"{_f(r['ece_cal'])} | {r['n']} ({r['files']}) |")
            out.append("")
    out += ["## Paired differences (encoder minus AFx-Rep), 95 % interval over test files", "",
            "| Arm | Feature | Head | Eval | vs | " + " | ".join(METRICS) + " |",
            "|---|---|---|---|---|" + "---|" * len(METRICS)]
    for k, d in res["paired"].items():
        key, ename, vs = k.split("|")
        arm, feat, head = key.split("/")
        out.append(f"| {arm} | {feat} | {head} | {ename} | {vs[3:]} | " + " | ".join(
            f"{d[m]['diff']:+.3f} [{d[m]['lo']:+.3f}, {d[m]['hi']:+.3f}]" for m in METRICS) + " |")
    out.append("")
    out.append(f"Train wall {res.get('elapsed_s', 0):.0f} s.")
    return "\n".join(out) + "\n"


def ledger(res: dict) -> None:
    n_r = sum(v["clips"] for v in res["counts"].values())
    for k, r in res["results"].items():
        arm, feat, head = k.split("/")
        for ename in EVALS:
            if ename not in r:
                continue
            e = r[ename]
            pd = res["paired"].get(f"{k}|{ename}|vs_afx_l2", {}).get("model_top1")
            notes = (f"T195 measurement only not adopted. Drive-only encoder swap; {feat} + {head} multi-crop, T189/T191 "
                     f"calibration; train {r['train_clips']} clips {r['train_files']} files; eval {ename} n {e['n']} "
                     f"files {e['files']}; top5 {e['model_top5']:.3f}; family {e['family_top1']:.3f}; drive15 own "
                     f"{e['drive_within_top1']:.3f}; tone10 own {e['tone_within_top1']:.3f}"
                     + (f"; vs afx_l2 {pd['diff']:+.3f} [{pd['lo']:+.3f},{pd['hi']:+.3f}]" if pd else "")
                     + "; P3 unread")
            subprocess.run([sys.executable, str(ROOT / "scripts/ledger_append.py"), "--kind", "benchmark",
                            "--engine", "-", "--dataset", DATASET, "--metric", f"{arm}_{feat}_{head}_{ename}_model_top1",
                            "--value", f"{e['model_top1']:.4f}", "--renders", str(n_r),
                            "--wall-clock-s", f"{r['fit_s']:.0f}", "--notes", notes], check=False, cwd=str(ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("select")
    p.add_argument("--n-train", type=int, default=N_TRAIN)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("render")
    p.add_argument("--hours", type=float, default=10.0)
    p.add_argument("--max-busy", type=float, default=None, help="pause above this many busy cores (default 0.95 x)")
    p.add_argument("--max-shards", type=int, default=0, help="shakedown: stop after this many shards")
    p = sub.add_parser("train")
    p.add_argument("--feats", default=",".join(FEATS))
    p.add_argument("--heads", default="logreg,mlp")
    p.add_argument("--arms", default="main,p2p1,p1p2")
    p.add_argument("--epochs", type=int, default=60)
    p.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    return {"select": cmd_select, "render": cmd_render, "train": cmd_train}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
