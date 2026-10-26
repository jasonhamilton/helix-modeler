"""chain-train-v1 (T215, learning): per-slot heads trained on finished-chain audio, scored on the T214 test chains.

Human 2026-10-23 "yes start pilot". Measurement only: no pass bar, nothing adopted.

Per slot (drive, amp, cab, mod, delay, reverb, eq) the T189 heads (`afx_heads.LinearHeads`, `MLPHeads`), one knob
column per drawn knob of the model (as slot-dataset-v1), multi-crop, temperature and sigma calibrated as T189 on
out-of-fold predictions grouped by DI file. Arms:
  chain           only the chain clips that contain the slot (features of the finished chain output);
  single+chain    the slot's single-block training clips (slot-dataset-v1, drive-dataset-v1; `train` split only)
                  plus the chain clips.
A learning-curve point uses the chains of the first half of the plan's shards (the plan is seeded and shuffled).
Presence: one logistic regression and one small multi-label MLP for the optional slots (drive, mod, delay, reverb,
eq), on chain clips only (single-block clips have no chain context, so both arms share them).

Scoring is T214's (`chain_transfer.item_scores` / `aggregate` / `contexts`) on the 295 T214 test chains.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import afx_heads as ah
from . import chain_transfer as CT

OPTIONAL = ("drive", "mod", "delay", "reverb", "eq")
ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------------------------------ data

def load_chain_shards(out: Path) -> tuple[list[dict], np.ndarray]:
    """Finished shards of a chain-train plan: (chain labels, (n, 3, 512) float32 crops), in plan shard order."""
    sdir = Path(out) / "shards"
    L, C = [], []
    for f in sorted(sdir.glob("s*.json")):
        sid = f.stem
        try:
            lab = [json.loads(x) for x in (sdir / f"{sid}.labels.jsonl").read_text().splitlines() if x.strip()]
            c = np.load(sdir / f"{sid}.crops.npy")
        except (OSError, ValueError):
            continue
        if len(lab) != len(c):
            continue
        for r in lab:
            r["shard"] = sid
        L += lab
        C.append(c.astype(np.float32))
    return L, (np.concatenate(C) if C else np.zeros((0, 3, 512), np.float32))


def load_test(test_dir: Path) -> tuple[list[dict], np.ndarray]:
    """The T214 test chains that rendered (failed ones dropped) and the crops of their finished output."""
    test_dir = Path(test_dir)
    doc = json.loads((test_dir / "chains.json").read_text())
    F = np.load(test_dir / "features.npz")
    failed = set(json.loads((test_dir / "render_meta.json").read_text())["failed"])
    byid = {c["id"]: c for c in doc["chains"]}
    has = F["has"]
    chains, rows = [], []
    for i, cid in enumerate(F["ids"]):
        if cid in failed or not has[i].any():
            continue
        chains.append(byid[str(cid)])
        rows.append(int(np.where(has[i])[0][-1]))
    crops = F["crops"][[i for i, cid in enumerate(F["ids"]) if cid not in failed and has[i].any()], rows]
    return chains, crops.astype(np.float32)


def slot_samples(chains: list[dict], C: np.ndarray, slot: str) -> tuple[list[dict], np.ndarray]:
    """Per-slot labels {model, cc, di_file, player} for the chains containing `slot`, and their crops."""
    lab, idx = [], []
    for i, c in enumerate(chains):
        b = CT.block_of(c, slot)
        if b is not None:
            lab.append({"model": b["model"], "cc": b["cc"], "di_file": c["di_file"], "player": c.get("player", "")})
            idx.append(i)
    return lab, C[idx] if len(idx) else np.zeros((0,) + C.shape[1:], np.float32)


# ------------------------------------------------------------------------------------------ heads

def make_head(kind: str, mod_fam: np.ndarray, *, seed: int = 0, epochs: int = 60, device: str = "cpu"):
    if kind == "logreg":
        return ah.LinearHeads(mod_fam=mod_fam, alpha=1.0)
    return ah.MLPHeads(mod_fam=mod_fam, seed=seed, epochs=epochs, device=device)


def fit_calibrated(kind: str, X, y, K, mod_fam, files, players, *, folds: int = 5, seed: int = 0, epochs: int = 60,
                   device: str = "cpu", calib_mask: np.ndarray | None = None):
    """Fit on everything, then calibrate (T189) on out-of-fold predictions grouped by DI file. `calib_mask` limits
    the calibration pool to some items (e.g. the chain clips of a mixed arm); the folds still use every item."""
    from sklearn.model_selection import GroupKFold
    h = make_head(kind, mod_fam, seed=seed, epochs=epochs, device=device).fit(X, y, K)
    nf = min(folds, len(set(files)))
    cal = None
    if nf >= 2:
        sel = np.arange(len(y)) if calib_mask is None else np.where(calib_mask)[0]
        outs = {}
        for f_tr, f_va in GroupKFold(nf).split(X, groups=files):
            va = np.intersect1d(f_va, sel)
            if not len(va):
                continue
            hh = make_head(kind, mod_fam, seed=seed, epochs=epochs, device=device).fit(X[f_tr], y[f_tr], K[f_tr])
            o = hh.predict(X[va])
            for j, i in enumerate(va):
                outs[int(i)] = {k: v[j] for k, v in o.items()}
        ids = sorted(outs)
        if ids:
            po = {k: np.stack([outs[i][k] for i in ids]) for k in ("fl", "lc", "mu", "sig")}
            ids = np.array(ids)
            cal = ah.calibrate(po, y[ids], K[ids], mod_fam, files[ids], players[ids])
    if kind == "mlp":
        h.net = h.net.cpu()
        h.device = "cpu"
    return h, cal


def head_logp(h, cal, mod_fam, X):
    o = h.predict(np.asarray(X, np.float32))
    T = cal.T if cal is not None else 1.0
    lj, lf = ah.joint_logp(o["fl"], o["lc"], mod_fam, T)
    return lj, lf, o["mu"]


class PresenceMLP:
    """Small multi-label MLP (sigmoid per optional slot) on crop embeddings; crops averaged at predict time."""

    def __init__(self, n_out: int, hidden: int = 256, epochs: int = 40, seed: int = 0, dropout: float = 0.3):
        self.n_out, self.hidden, self.epochs, self.seed, self.dropout = n_out, hidden, epochs, seed, dropout

    def fit(self, X, Y):
        import torch
        from torch import nn
        torch.manual_seed(self.seed)
        rng = np.random.default_rng(self.seed)
        Xf = X.reshape(-1, X.shape[-1]) if X.ndim == 3 else X
        Yf = np.repeat(Y, X.shape[1], 0) if X.ndim == 3 else Y
        self.net = nn.Sequential(nn.Linear(Xf.shape[1], self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                 nn.Linear(self.hidden, self.hidden), nn.GELU(), nn.Dropout(self.dropout),
                                 nn.Linear(self.hidden, self.n_out))
        Xt, Yt = torch.tensor(Xf, dtype=torch.float32), torch.tensor(Yf, dtype=torch.float32)
        opt = torch.optim.AdamW(self.net.parameters(), lr=1e-3, weight_decay=1e-3)
        lossf = nn.BCEWithLogitsLoss()
        self.net.train()
        for _ in range(self.epochs):
            perm = rng.permutation(len(Xt))
            for i in range(0, len(perm), 256):
                b = torch.tensor(perm[i:i + 256])
                loss = lossf(self.net(Xt[b] + 0.01 * torch.randn_like(Xt[b])), Yt[b])
                opt.zero_grad()
                loss.backward()
                opt.step()
        self.net.eval()
        return self

    def predict_proba(self, X):
        import torch
        with torch.no_grad():
            if X.ndim == 3:
                return np.mean([torch.sigmoid(self.net(torch.tensor(X[:, k], dtype=torch.float32))).numpy()
                                for k in range(X.shape[1])], 0)
            return torch.sigmoid(self.net(torch.tensor(X, dtype=torch.float32))).numpy()


class PresenceLogreg:
    def __init__(self, C: float = 1.0):
        self.C = C

    def fit(self, X, Y):
        from sklearn.linear_model import LogisticRegression
        Xf = X.reshape(-1, X.shape[-1])
        self.m = [LogisticRegression(C=self.C, max_iter=3000).fit(Xf, np.repeat(Y[:, k], X.shape[1]))
                  for k in range(Y.shape[1])]
        return self

    def predict_proba(self, X):
        return np.stack([np.mean([m.predict_proba(X[:, c])[:, 1] for c in range(X.shape[1])], 0) for m in self.m], 1)


def presence_matrix(chains: list[dict]) -> np.ndarray:
    return np.array([[s in CT.slots_of(c) for s in OPTIONAL] for c in chains], float)


def presence_scores(P: np.ndarray, Y: np.ndarray) -> dict:
    """Per optional slot: accuracy at 0.5 (Wilson 95 %), balanced accuracy, and the majority-class baseline."""
    out = {}
    for k, s in enumerate(OPTIONAL):
        pred, y = P[:, k] >= 0.5, Y[:, k] >= 0.5
        hit = int((pred == y).sum())
        n = len(y)
        tpr = float(pred[y].mean()) if y.any() else math.nan
        tnr = float((~pred[~y]).mean()) if (~y).any() else math.nan
        out[s] = {"n": n, "acc": hit / n if n else math.nan, "acc_ci": CT.wilson(hit, n),
                  "balanced": (tpr + tnr) / 2, "tpr": tpr, "tnr": tnr,
                  "majority": float(max(y.mean(), 1 - y.mean())) if n else math.nan, "n_present": int(y.sum())}
    return out


# ------------------------------------------------------------------------------------------ evaluation

BREAKDOWNS = ("drive present", "delay or reverb after", "any block after (besides cab)", "time effect after",
              "amp gain knob >= 64", "preamp model")


def evaluate(h, cal, mod_fam, models, knobs, test_chains, test_C, slot) -> dict:
    lab, X = slot_samples(test_chains, test_C, slot)
    if not lab:
        return {}
    mi = {m: i for i, m in enumerate(models)}
    y = np.array([mi[r["model"]] for r in lab])
    from .slot_dataset import knob_matrix
    K = knob_matrix(lab, models, knobs)
    lj, lf, mu = head_logp(h, cal, mod_fam, X)
    s = CT.item_scores(lj, lf, mu, y, mod_fam[y], K)
    r = {"all": CT.aggregate(s), "by_name": CT.knob_by_name(s, [knobs[m] for m in models], y)}
    ctx = [CT.contexts(c, slot) for c in test_chains if CT.block_of(c, slot) is not None]
    br = {}
    for key in BREAKDOWNS:
        v = np.array([c.get(key) for c in ctx], object)
        yes, no = v == True, v == False   # noqa: E712
        if yes.sum() and no.sum():
            br[key] = {"yes": CT.aggregate(s, yes, boot=300), "no": CT.aggregate(s, no, boot=300)}
    r["breakdown"] = br
    return r


def eval_single(h, cal, mod_fam, models, knobs, lab, X) -> dict:
    """The new head on single-block held-out clips (no chain): does chain training cost single-block accuracy?"""
    if not lab:
        return {}
    mi = {m: i for i, m in enumerate(models)}
    keep = [i for i, r in enumerate(lab) if r["model"] in mi]
    lab, X = [lab[i] for i in keep], X[keep]
    y = np.array([mi[r["model"]] for r in lab])
    from .slot_dataset import knob_matrix
    K = knob_matrix(lab, models, knobs)
    outs = [head_logp(h, cal, mod_fam, X[i:i + 4096]) for i in range(0, len(X), 4096)]
    lj, lf, mu = (np.concatenate([o[k] for o in outs]) for k in range(3))
    return CT.aggregate(CT.item_scores(lj, lf, mu, y, mod_fam[y], K), boot=0)


def load_previous(out: Path, res: dict) -> dict:
    """Earlier results.json of the same data (same chain, shard and test counts), else {}."""
    f = Path(out) / "results.json"
    if not f.is_file():
        return {}
    try:
        prev = json.loads(f.read_text())
    except ValueError:
        return {}
    if any(prev.get(k) != res[k] for k in ("n_chains", "n_shards", "n_test")):
        return {}
    return prev


def with_cab_onehot(chains: list[dict], X: np.ndarray, cab_models: list[str], *, scale: float = 1.0,
                    rng: np.random.Generator | None = None) -> np.ndarray:
    """Append a one-hot of each chain's cab model to every crop. `rng` draws a random cab instead (control)."""
    ci = {m: i for i, m in enumerate(cab_models)}
    oh = np.zeros((len(chains), len(cab_models)), np.float32)
    for i, c in enumerate(chains):
        b = CT.block_of(c, "cab")
        j = int(rng.integers(len(cab_models))) if rng is not None else ci.get(b["model"] if b else None, -1)
        if j >= 0:
            oh[i, j] = scale
    return np.concatenate([X, np.repeat(oh[:, None], X.shape[1], 1)], axis=2)


def oracle_cab(chains, C, test_chains, test_C, pools, *, heads=("logreg", "mlp"), epochs: int = 60, log=print,
               scale: float = 1.0) -> dict:
    """research-full-chain section 2(b): the amp head (chain-only arm, every chain) with the TRUE cab id appended to
    its input, scored with the true cab (oracle) and with a random cab (control). Zero renders."""
    from .slot_dataset import knob_matrix
    p = pools["amp"]
    models = list(p["models"])
    fams = sorted(set(p["families"][m] for m in models))
    fi = {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[p["families"][m]] for m in models])
    mi = {m: i for i, m in enumerate(models)}
    cabs = list(pools["cab"]["models"])
    tr = [i for i, c in enumerate(chains) if CT.block_of(c, "amp") is not None]
    te = [i for i, c in enumerate(test_chains) if CT.block_of(c, "amp") is not None]
    ch, Xc = [chains[i] for i in tr], C[tr]
    tch, Xt = [test_chains[i] for i in te], test_C[te]
    lab = [{"model": CT.block_of(c, "amp")["model"], "cc": CT.block_of(c, "amp")["cc"], "di_file": c["di_file"],
            "player": c.get("player", "")} for c in ch]
    tlab = [{"model": CT.block_of(c, "amp")["model"], "cc": CT.block_of(c, "amp")["cc"]} for c in tch]
    y, yt = np.array([mi[r["model"]] for r in lab]), np.array([mi[r["model"]] for r in tlab])
    K, Kt = knob_matrix(lab, models, p["knobs"]), knob_matrix(tlab, models, p["knobs"])
    files = np.array([r["di_file"] for r in lab])
    players = np.array([r["player"] for r in lab])
    Xtr = with_cab_onehot(ch, Xc, cabs, scale=scale)
    Xte = with_cab_onehot(tch, Xt, cabs, scale=scale)
    Xrand = with_cab_onehot(tch, Xt, cabs, scale=scale, rng=np.random.default_rng(215))
    out = {"n_train": len(ch), "n_test": len(tch), "n_cabs": len(cabs), "scale": scale}
    for kind in heads:
        t0 = time.time()
        h, cal = fit_calibrated(kind, Xtr, y, K, mod_fam, files, players, folds=5, epochs=epochs)
        r = {}
        for name, X in (("true_cab", Xte), ("random_cab", Xrand)):
            lj, lf, mu = head_logp(h, cal, mod_fam, X)
            r[name] = CT.aggregate(CT.item_scores(lj, lf, mu, yt, mod_fam[yt], Kt))
        r["fit_s"] = round(time.time() - t0, 1)
        out[kind] = r
        log(f"oracle amp+true-cab {kind}: n_train {len(ch)}; test top1 {r['true_cab']['top1']:.3f} top3 "
            f"{r['true_cab']['top3']:.3f} knob {r['true_cab']['knob']:.3f}; random cab top1 {r['random_cab']['top1']:.3f} "
            f"top3 {r['random_cab']['top3']:.3f}; {r['fit_s']:.0f} s")
    return out


# ------------------------------------------------------------------------------------------ run

def _single_loaders(root: Path = ROOT):
    """slot -> callable(split) -> (labels, crops) of the single-block datasets (slot-dataset-v1 + amp refill;
    drive-dataset-v1 new renders)."""
    sys.path.insert(0, str(root / "scripts"))
    import drive_dataset_render as DD

    def load_dirs(dirs, split):
        L, C = [], []
        for d in dirs:
            sdir = Path(d) / "shards"
            for f in sorted(sdir.glob("s*.json")):
                sid = f.stem
                try:
                    lab = [json.loads(x) for x in (sdir / f"{sid}.labels.jsonl").read_text().splitlines() if x.strip()]
                    sel = [i for i, r in enumerate(lab) if r.get("split") == split]
                    if not sel:
                        continue
                    c = np.load(sdir / f"{sid}.crops.npy")
                except (OSError, ValueError):
                    continue
                if len(c) != len(lab):
                    continue
                L += [{"model": lab[i]["model"], "cc": lab[i]["cc"], "di_file": lab[i]["di_file"],
                       "player": lab[i]["player"]} for i in sel]
                C.append(c[sel].astype(np.float32))
        return L, (np.concatenate(C) if C else np.zeros((0, 3, 512), np.float32))
    base = root / "cache/slot-dataset-v1"
    out = {"drive": lambda split: load_dirs([root / "cache/drive-dataset-v1"], split)}
    for s in ("amp", "cab", "mod", "delay", "reverb", "eq"):
        dirs = [base / s] + ([base / f"{s}-refill"] if (base / f"{s}-refill" / "shards").is_dir() else [])
        out[s] = (lambda dd: (lambda split: load_dirs(dd, split)))(dirs)
    _ = DD
    return out


def run(out: Path, test_dir: Path, models_dir: Path, *, pools: dict | None = None, ledger: bool = True,
        threads: int = 2, arms=("chain", "single+chain"), fractions=(0.5, 1.0), slots=CT.CHAIN_ORDER,
        heads=("logreg", "mlp"), epochs: int = 60, mixed_folds: int = 3, mixed_half: bool = False, single_loaders=None, log=print,
        t214_results: Path | None = None, resume: bool = True, oracle: bool = True) -> dict:
    """`resume`: a (slot, arm, fraction, head) result already in out/results.json for the same data is reused and not
    refitted (for fraction 1 only if its pickle is also on disk); presence results per fraction likewise. Nothing is
    re-rendered. `oracle`: the section 2(b) zero-render test (amp head with the true cab id appended)."""
    import pickle
    import warnings

    import torch
    torch.set_num_threads(int(threads))
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    from .slot_dataset import knob_matrix
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text

    out, models_dir = Path(out), Path(models_dir)
    t_start = time.time()
    if pools is None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import chain_transfer as S
        dpool, _ = S.drive_pool_knobs()
        pools = {"drive": dpool, **S.slot_pools()}
    plan = json.loads((out / "chains.json").read_text())
    shard_order = [s["id"] for s in plan["shards"]]
    chains, C = load_chain_shards(out)
    if not chains:
        raise RuntimeError("no finished chain shards")
    test_chains, test_C = load_test(test_dir)
    tfiles = {c["di_file"] for c in test_chains}
    assert not tfiles & {c["di_file"] for c in chains}, "test DI files in the chain training data"
    assert not any("/P3" in c["di_file"] for c in chains), "P3 in the training data"
    done = sorted({c["shard"] for c in chains}, key=shard_order.index)
    res = {"brief": "T215 chain-train-v1 pilot; measurement only, nothing adopted", "n_chains": len(chains),
           "n_shards": len(done), "n_test": len(test_chains), "fractions": {}, "slots": {}, "presence": {}}
    prev = load_previous(out, res) if resume else {}
    res["resumed"] = sorted(f"{s}/{k}" for s, v in prev.get("slots", {}).items() for k in v)
    if resume and prev:
        res["presence"] = dict(prev.get("presence", {}))
        res["slots"] = {s: dict(v) for s, v in prev.get("slots", {}).items()}
        if prev.get("oracle_amp_cab"):
            res["oracle_amp_cab"] = prev["oracle_amp_cab"]
        log(f"resume: {len(res['resumed'])} cached fits, presence for fractions {sorted(res['presence'])}")
    loaders = single_loaders if single_loaders is not None else _single_loaders()
    single_test_cache: dict[str, tuple] = {}
    for frac in fractions:
        k = max(1, math.ceil(frac * len(done)))
        keep = set(done[:k])
        sel = np.array([c["shard"] in keep for c in chains])
        ch, Cc = [c for c, s in zip(chains, sel) if s], C[sel]
        fk = f"{frac:g}"
        res["fractions"][fk] = {"chains": len(ch), "shards": k}
        log(f"fraction {fk}: {len(ch)} chains")
        # presence (chain clips only; both arms share it)
        Y, Yt = presence_matrix(ch), presence_matrix(test_chains)
        if fk not in res["presence"]:
            pr = {}
            for kind, mk in (("logreg", lambda: PresenceLogreg()), ("mlp", lambda: PresenceMLP(len(OPTIONAL)))):
                m = mk().fit(Cc, Y)
                pr[kind] = presence_scores(m.predict_proba(test_C), Yt)
            res["presence"][fk] = pr
        for slot in slots:
            p = pools[slot]
            models = list(p["models"])
            fams = sorted(set(p["families"][m] for m in models))
            fi = {f: i for i, f in enumerate(fams)}
            mod_fam = np.array([fi[p["families"][m]] for m in models])
            mi = {m: i for i, m in enumerate(models)}
            lab_c, X_c = slot_samples(ch, Cc, slot)
            for arm in arms:
                if arm == "single+chain" and frac < 1.0 and not mixed_half:
                    continue      # the half-size learning-curve point is chain-only by default (cost)
                lab, X = list(lab_c), X_c
                is_chain = np.ones(len(lab), bool)
                if arm == "single+chain":
                    ls, xs = loaders[slot]("train")
                    ok = [i for i, r in enumerate(ls) if r["model"] in mi]
                    ls, xs = [ls[i] for i in ok], xs[ok]
                    lab, X = ls + lab, np.concatenate([xs, X_c])
                    is_chain = np.concatenate([np.zeros(len(ls), bool), is_chain])
                y = np.array([mi[r["model"]] for r in lab])
                K = knob_matrix(lab, models, p["knobs"])
                files = np.array([r["di_file"] for r in lab])
                players = np.array([r["player"] for r in lab])
                key = f"{arm}/{fk}"
                for kind in heads:
                    pk = models_dir / arm.replace("+", "_") / f"{slot}_{kind}.pkl"
                    if f"{key}/{kind}" in res["slots"].get(slot, {}) and (frac < 1.0 or pk.is_file()):
                        a = res["slots"][slot][f"{key}/{kind}"]["all"]
                        log(f"{slot} {key} {kind}: cached; test top1 {a['top1']:.3f} top3 {a['top3']:.3f}")
                        continue
                    t0 = time.time()
                    h, cal = fit_calibrated(kind, X, y, K, mod_fam, files, players,
                                            folds=5 if arm == "chain" else mixed_folds, epochs=epochs,
                                            calib_mask=is_chain if arm == "single+chain" else None)
                    r = evaluate(h, cal, mod_fam, models, p["knobs"], test_chains, test_C, slot)
                    if slot not in single_test_cache:
                        single_test_cache[slot] = loaders[slot]("test")
                    r["single_block_test"] = eval_single(h, cal, mod_fam, models, p["knobs"], *single_test_cache[slot])
                    r["train_clips"], r["train_chain_clips"] = len(lab), int(is_chain.sum())
                    r["fit_s"] = round(time.time() - t0, 1)
                    res["slots"].setdefault(slot, {})[f"{key}/{kind}"] = r
                    a = r["all"]
                    log(f"{slot} {key} {kind}: n_train {len(lab)} (chain {int(is_chain.sum())}); test top1 "
                        f"{a['top1']:.3f} top3 {a['top3']:.3f} knob {a['knob']:.3f}; single-block test top1 "
                        f"{r['single_block_test'].get('top1', math.nan):.3f}; {r['fit_s']:.0f} s")
                    if frac == 1.0:
                        d = models_dir / arm.replace("+", "_")
                        d.mkdir(parents=True, exist_ok=True)
                        with open(d / f"{slot}_{kind}.pkl", "wb") as fh:
                            if kind == "mlp":
                                h.net = h.net.state_dict()
                            pickle.dump({"heads": h, "calibration": cal, "models": models, "families": fams,
                                         "family_of": p["families"], "knobs": p["knobs"], "slot": slot, "arm": arm,
                                         "note": "T215 pilot; measurement only; not adopted"}, fh)
                    atomic_write_json(out / "results.json", res, indent=1, default=float)
    if oracle and "amp" in slots and "cab" in pools and "oracle_amp_cab" not in res:
        res["oracle_amp_cab"] = oracle_cab(chains, C, test_chains, test_C, pools, heads=heads, epochs=epochs, log=log)
        atomic_write_json(out / "results.json", res, indent=1, default=float)
    res["elapsed_s"] = round(time.time() - t_start) + (prev.get("elapsed_s", 0) if resume else 0)
    t214 = json.loads(Path(t214_results or (Path(test_dir) / "results.json")).read_text())
    res["t214"] = {s: {"top1": v["heads"]["mlp"]["all"]["top1"], "top3": v["heads"]["mlp"]["all"]["top3"],
                       "fam1": v["heads"]["mlp"]["all"]["fam1"], "knob": v["heads"]["mlp"]["all"]["knob"],
                       "breakdown": {k: {a: {m: x[a][m] for m in ("top1", "top3", "n")} for a in ("yes", "no")}
                                     for k, x in v["heads"]["mlp"]["breakdown"].items()}}
                   for s, v in t214["slots"].items()}
    atomic_write_json(out / "results.json", res, indent=1, default=float)
    atomic_write_text(out / "results.md", results_md(res))
    if ledger:
        append_ledger(res)
    return res


def _p(x) -> str:
    return "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.2f}"


def _ci(r: dict, k: str) -> str:
    lo, hi = r.get(k + "_ci", (math.nan, math.nan))
    return f"{_p(r[k])} ({_p(lo)}-{_p(hi)})"


def results_md(res: dict) -> str:
    L = ["# chain-train-v1 (T215): results", "",
         f"Measurement only; nothing adopted; no pass bar. Training chains: {res['n_chains']} ({res['n_shards']} shards). "
         f"Test: the {res['n_test']} T214 chains (held-out DI files).", "",
         "## Model identification on the T214 test chains (MLP; top-1 / top-3 with 95 %; family; knobs within ±0.15)", "",
         "| Slot | T214 single-block head top-1 / top-3 | Chain-only half: top-1 / top-3 | Chain-only full: top-1 | top-3 | "
         "family | knobs | Single+chain full: top-1 | top-3 | family | knobs | Logreg chain / single+chain top-1 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for slot, v in res["slots"].items():
        t = res["t214"].get(slot, {})
        h = v.get("chain/0.5/mlp", {}).get("all", {})
        c = v.get("chain/1/mlp", {}).get("all", {})
        m = v.get("single+chain/1/mlp", {}).get("all", {})
        lc = v.get("chain/1/logreg", {}).get("all", {})
        lm = v.get("single+chain/1/logreg", {}).get("all", {})
        if not c:
            continue
        L.append(f"| {slot} | {_p(t.get('top1'))} / {_p(t.get('top3'))} | {_p(h.get('top1'))} / {_p(h.get('top3'))} "
                 f"| {_ci(c, 'top1')} | {_ci(c, 'top3')} | {_p(c['fam1'])} | {_ci(c, 'knob')} "
                 + (f"| {_ci(m, 'top1')} | {_ci(m, 'top3')} | {_p(m['fam1'])} | {_ci(m, 'knob')} " if m else "| | | | ")
                 + f"| {_p(lc.get('top1'))} / {_p(lm.get('top1'))} |")
    L += ["", "## The same heads on single-block held-out clips (no chain; top-1 / top-3)", "",
          "| Slot | Chain-only | Single+chain |", "|---|---|---|"]
    for slot, v in res["slots"].items():
        c = v.get("chain/1/mlp", {}).get("single_block_test", {})
        m = v.get("single+chain/1/mlp", {}).get("single_block_test", {})
        L.append(f"| {slot} | {_p(c.get('top1'))} / {_p(c.get('top3'))} | {_p(m.get('top1'))} / {_p(m.get('top3'))} |")
    L += ["", "## Presence of optional slots on the T214 test chains (accuracy at 0.5, 95 %; balanced accuracy; majority baseline)", ""]
    for fk, pr in res["presence"].items():
        L += [f"Training chains: fraction {fk}", "", "| Slot | n present / n | Logreg acc | Logreg balanced | MLP acc | MLP balanced | Majority |",
              "|---|---|---|---|---|---|---|"]
        for s in OPTIONAL:
            a, b = pr["logreg"][s], pr["mlp"][s]
            L.append(f"| {s} | {a['n_present']} / {a['n']} | {_ci(a, 'acc')} | {_p(a['balanced'])} | {_ci(b, 'acc')} "
                     f"| {_p(b['balanced'])} | {_p(a['majority'])} |")
        L.append("")
    L += ["## Breakdowns (MLP top-1 / top-3, n): T214 single-block head -> chain-only -> single+chain", ""]
    for slot, v in res["slots"].items():
        c = v.get("chain/1/mlp", {}).get("breakdown", {})
        m = v.get("single+chain/1/mlp", {}).get("breakdown", {})
        t = res["t214"].get(slot, {}).get("breakdown", {})
        if not c:
            continue
        L += [f"### {slot}", "", "| Context | T214 yes | T214 no | chain yes | chain no | single+chain yes | single+chain no |",
              "|---|---|---|---|---|---|---|"]
        for k in c:
            f = lambda d, a: (f"{_p(d[k][a]['top1'])} / {_p(d[k][a]['top3'])} ({d[k][a]['n']})" if k in d else "")  # noqa: E731
            L.append(f"| {k} | {f(t, 'yes')} | {f(t, 'no')} | {f(c, 'yes')} | {f(c, 'no')} | {f(m, 'yes')} | {f(m, 'no')} |")
        L.append("")
    o = res.get("oracle_amp_cab")
    if o:
        L += ["## Oracle: amp head with the TRUE cab id appended (research-full-chain 2(b); chain-only arm, every chain; "
              "top-1 / top-3 with 95 %; knobs)", "", "| Head | Unconditioned (chain/1) | True cab appended | Random cab appended (control) |",
              "|---|---|---|---|"]
        for kind in ("logreg", "mlp"):
            if kind not in o:
                continue
            u = res["slots"].get("amp", {}).get(f"chain/1/{kind}", {}).get("all", {})
            t, r = o[kind]["true_cab"], o[kind]["random_cab"]
            L.append(f"| {kind} | {_p(u.get('top1'))} / {_p(u.get('top3'))} | {_ci(t, 'top1')} / {_ci(t, 'top3')}; knobs {_p(t['knob'])} "
                     f"| {_ci(r, 'top1')} / {_ci(r, 'top3')}; knobs {_p(r['knob'])} |")
        L.append("")
    L.append(f"Train wall {res.get('elapsed_s', 0)} s.")
    return "\n".join(L) + "\n"


def append_ledger(res: dict, root: Path = ROOT) -> None:
    """One `benchmark / native / chain-train-v1` row per slot, metric `<slot>_mlp_model_top3` (chain-only arm, every
    chain); the single+chain and half-size numbers ride in the notes. Then one presence row per optional slot and
    one oracle row."""
    os.environ.setdefault("TONEMATCH_AGENT", "learning")

    def row(metric, val, notes):
        subprocess.run([sys.executable, str(root / "scripts/ledger_append.py"), "--kind", "benchmark", "--engine",
                        "native", "--dataset", "chain-train-v1", "--metric", metric, "--value", f"{val:.4f}",
                        "--renders", str(res["n_chains"]), "--wall-clock-s", str(res.get("elapsed_s", 0)),
                        "--notes", notes], check=False, cwd=str(root))

    def short(r):
        a = r.get("all") if r else None
        return f"top1 {a['top1']:.3f} top3 {a['top3']:.3f} knob {a['knob']:.3f}" if a else "not run"
    for slot, v in res["slots"].items():
        r = v.get("chain/1/mlp")
        if not r:
            continue
        a = r["all"]
        t = res["t214"].get(slot, {})
        row(f"{slot}_mlp_model_top3", a["top3"],
            f"T215 measurement only not adopted. {slot} MLP chain-only arm trained on {r['train_chain_clips']} chain clips; "
            f"scored on {a['n']} T214 test chains; top1 {a['top1']:.3f}; top3 {a['top3']:.3f} (95% {a['top3_ci'][0]:.2f}-"
            f"{a['top3_ci'][1]:.2f}); family {a['fam1']:.3f}; knob 0.15 {a['knob']:.3f}; single+chain arm {short(v.get('single+chain/1/mlp'))}"
            f" on {v.get('single+chain/1/mlp', {}).get('train_clips', 0)} clips; half chains {short(v.get('chain/0.5/mlp'))}; "
            f"logreg chain {short(v.get('chain/1/logreg'))}; T214 single-block head top1 {t.get('top1', math.nan):.3f} "
            f"top3 {t.get('top3', math.nan):.3f}; this head on single-block test top1 "
            f"{r['single_block_test'].get('top1', math.nan):.3f}; P3 unread")
    pr = res["presence"].get("1")
    if pr:
        for s_ in OPTIONAL:
            b = pr["mlp"][s_]
            row(f"presence_{s_}_mlp_acc", b["acc"],
                f"T215 measurement only not adopted. presence of {s_} (MLP, chain clips only, every chain) on {b['n']} "
                f"T214 test chains; balanced {b['balanced']:.3f}; majority baseline {b['majority']:.3f}; logreg acc "
                f"{pr['logreg'][s_]['acc']:.3f}")
    o = res.get("oracle_amp_cab", {})
    if o.get("mlp"):
        u = res["slots"].get("amp", {}).get("chain/1/mlp", {}).get("all", {})
        t, rc = o["mlp"]["true_cab"], o["mlp"]["random_cab"]
        row("amp_oracle_true_cab_mlp_model_top3", t["top3"],
            f"T215 zero-render oracle (research-full-chain 2(b)) measurement only not adopted. amp MLP chain-only with the "
            f"TRUE cab id appended; top1 {t['top1']:.3f} top3 {t['top3']:.3f} (95% {t['top3_ci'][0]:.2f}-{t['top3_ci'][1]:.2f}); "
            f"random cab control top1 {rc['top1']:.3f} top3 {rc['top3']:.3f}; unconditioned chain head top1 "
            f"{u.get('top1', math.nan):.3f} top3 {u.get('top3', math.nan):.3f}; logreg true cab top3 "
            f"{o.get('logreg', {}).get('true_cab', {}).get('top3', math.nan):.3f}")
