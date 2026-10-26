"""product-chain-v1 (T233, learning): training and scoring of the product-chain heads. Measurement only.

Heads, all on frozen AFx-Rep `mid` crop embeddings of the finished chain output (3 x 3.33 s), with the input level
appended to every crop (level-conditioned, T194 v2cond):
  drive     model (54) + family + every drawn knob: the T189 heads (`afx_heads.LinearHeads` "logreg", `MLPHeads`
            "mlp"), calibrated on out-of-fold predictions grouped by DI source (chain_train.fit_calibrated);
            one unconditioned pair (no level input) on the largest arm for comparison;
  reverb    presence (all chains), type Room/Plate/Hall, Mix, log RT60 (chains with a reverb);
  eq        presence (all chains), type Parametric/Graphic, the measured 8-band stage curve (chains with an EQ).
Colouring heads: `LinearAux` (logistic / ridge) and `SmallMLP`.

Arms: `lib` (Guitar-TECHS chains only) and `lib+prs` (plus the human's prs-7-tone chains); fractions: the first half
of the finished training shards in plan order (the plan is shuffled) and all of them. Evaluation sets: `lib-test`
(drive-dataset-v1 test-split files) and `prs-test` (the held-out part of the session).
"""
from __future__ import annotations

import json
import math
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import chain_transfer as CT
from . import product_chain as PC

ROOT = Path(__file__).resolve().parents[3]
EVAL_SETS = ("lib-test", "prs-test")
ARMS = ("lib", "lib+prs")
AUX_TASKS = {   # name: (task, n_out, which chains, target key)
    "reverb_present": ("bce", 1, "all", "reverb_present"),
    "reverb_type": ("ce", 3, "reverb", "reverb_type"),
    "reverb_mix": ("mse", 1, "reverb", "reverb_mix"),
    "reverb_logrt60": ("mse", 1, "reverb", "reverb_logrt60"),
    "eq_present": ("bce", 1, "all", "eq_present"),
    "eq_type": ("ce", 2, "eq", "eq_type"),
    "eq_curve": ("mse", len(PC.CURVE_BANDS), "eq", "eq_curve"),
}


# ------------------------------------------------------------------------------------------ data

def load_shards(out: Path) -> tuple[list[dict], np.ndarray]:
    """Every finished shard (train and eval), in plan order: (chain labels with "shard", (n, 3, 512) float32)."""
    out = Path(out)
    plan = json.loads((out / "chains.json").read_text())
    sdir = out / "shards"
    L, C = [], []
    for s in plan["shards"]:
        sid = s["id"]
        if not (sdir / f"{sid}.json").is_file():
            continue
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


def select(chains: list[dict], C: np.ndarray, mask) -> tuple[list[dict], np.ndarray]:
    idx = [i for i, c in enumerate(chains) if mask(c)]
    return [chains[i] for i in idx], C[idx] if idx else np.zeros((0,) + C.shape[1:], np.float32)


def levels(chains: list[dict]) -> np.ndarray:
    return np.array([c["di_level_db"] for c in chains], float)


def drive_labels(chains: list[dict]) -> list[dict]:
    return [{"model": PC.block_of(c, "drive")["model"], "cc": PC.block_of(c, "drive")["cc"], "di_file": c["di_file"],
             "player": c.get("player", "")} for c in chains]


# ------------------------------------------------------------------------------------------ drive

def drive_knob_names() -> dict[str, str]:
    """model -> its Drive-type knob (drive_pilot's T179 map, as T194); {} if the map is not on disk."""
    try:
        from .drive_pilot import _knob_names
        return {m: v[0] for m, v in _knob_names().items()}
    except (OSError, ValueError, KeyError):
        return {}


def drive_eval(h, cal, mod_fam, models, knobs, chains, X, drive_knob: dict[str, str] | None = None) -> dict:
    from .chain_train import head_logp
    from .slot_dataset import knob_matrix
    if not chains:
        return {}
    mi = {m: i for i, m in enumerate(models)}
    lab = drive_labels(chains)
    y = np.array([mi[r["model"]] for r in lab])
    K = knob_matrix(lab, models, knobs)
    outs = [head_logp(h, cal, mod_fam, X[i:i + 4096]) for i in range(0, len(X), 4096)]
    lj, lf, mu = (np.concatenate([o[k] for o in outs]) for k in range(3))
    s = CT.item_scores(lj, lf, mu, y, mod_fam[y], K)
    r = {"all": CT.aggregate(s, boot=300), "by_name": CT.knob_by_name(s, [knobs[m] for m in models], y)}
    br = {}
    for key, f in (("reverb after", lambda c: PC.block_of(c, "reverb") is not None),
                   ("eq after", lambda c: PC.block_of(c, "eq") is not None)):
        v = np.array([f(c) for c in chains])
        if v.any() and (~v).any():
            br[key] = {"yes": CT.aggregate(s, v, boot=0), "no": CT.aggregate(s, ~v, boot=0)}
    r["breakdown"] = br
    # the Drive-type knob (the model's first drawn gain knob, T194): signed error by input-level bucket
    lv = levels(chains)
    b = PC.level_buckets(lv)
    idx = np.arange(len(y))
    rows = {}
    for i in idx:
        names = knobs[models[y[i]]]
        dk = (drive_knob or {}).get(models[y[i]])
        j = names.index(dk) if dk in names else next((j for j, nm in enumerate(names) if nm in CT.GAIN_KNOBS), None)
        if j is None or not np.isfinite(K[i, j]):
            continue
        rows.setdefault(int(b[i]), []).append((float(mu[i, y[i], j] - K[i, j]), abs(mu[i, y[i], j] - K[i, j]) <= CT.KNOB_TOL))
    r["drive_knob_by_level"] = {str(k): {"n": len(v), "bias": float(np.nanmean([e for e, _ in v])),
                                         "within": float(np.mean([h_ for _, h_ in v]))} for k, v in sorted(rows.items())}
    return r


def fit_drive(kind, chains, X, pool, *, folds=3, epochs=60, log=print):
    from .chain_train import fit_calibrated
    from .slot_dataset import knob_matrix
    models = list(pool["models"])
    fams = sorted(set(pool["families"][m] for m in models))
    fi = {f: i for i, f in enumerate(fams)}
    mod_fam = np.array([fi[pool["families"][m]] for m in models])
    mi = {m: i for i, m in enumerate(models)}
    lab = drive_labels(chains)
    y = np.array([mi[r["model"]] for r in lab])
    K = knob_matrix(lab, models, pool["knobs"])
    files = np.array([r["di_file"] for r in lab])
    players = np.array([r["player"] for r in lab])
    h, cal = fit_calibrated(kind, X, y, K, mod_fam, files, players, folds=folds, epochs=epochs)
    return {"heads": h, "calibration": cal, "models": models, "families": fams, "family_of": pool["families"],
            "knobs": pool["knobs"], "mod_fam": mod_fam}


def load_pickle(path: Path) -> dict:
    """A saved fit; an MLP drive head's network is rebuilt from its state dict (input width from the first layer)."""
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    h = d.get("heads")
    if h is not None and isinstance(getattr(h, "net", None), dict):
        net = h._build(int(h.net["trunk.0.weight"].shape[1]), h.n_fam, h.M)
        net.load_state_dict(h.net)
        h.net = net.eval()
        h.device = "cpu"
    return d


def drive_predict(d: dict, X: np.ndarray) -> list[dict]:
    """Top-3 models with probabilities and the top-1 model's knob positions (0..1) for each row of X."""
    from .chain_train import head_logp
    lj, lf, mu = head_logp(d["heads"], d["calibration"], d["mod_fam"], X)
    out = []
    for i in range(len(X)):
        o = np.argsort(-lj[i])[:3]
        m0 = d["models"][o[0]]
        out.append({"top3": [[d["models"][j], float(np.exp(lj[i, j]))] for j in o],
                    "ranking": [d["models"][j] for j in np.argsort(-lj[i])],
                    "knobs": {nm: round(float(mu[i, o[0], k]), 3) for k, nm in enumerate(d["knobs"][m0])}})
    return out


# ------------------------------------------------------------------------------------------ colouring heads

def aux_data(task: str, chains: list[dict], X: np.ndarray):
    t, n_out, which, key = AUX_TASKS[task]
    T = PC.aux_targets(chains)
    y = T[key]
    if which == "all":
        sel = np.ones(len(chains), bool)
    else:
        sel = T[f"{which}_present"] >= 0.5
        if task == "eq_curve":
            sel &= np.isfinite(y).all(1)
    return X[sel], y[sel]


def aux_eval(task: str, m, chains, X) -> dict:
    t = AUX_TASKS[task][0]
    Xs, y = aux_data(task, chains, X)
    if not len(y):
        return {"n": 0}
    p = m.predict(Xs)
    if t == "bce":
        return PC.presence_score(p[:, 0], y)
    if t == "ce":
        return PC.type_score(p, y.astype(int))
    if task == "reverb_logrt60":
        r = PC.regression_score(np.exp(p[:, 0]), np.exp(y), tols=(0.2, 0.4))
        r["log_mae"] = float(np.abs(p[:, 0] - y).mean())
        return r
    if task == "reverb_mix":
        return PC.regression_score(p[:, 0], y, tols=(0.05, 0.10))
    return PC.regression_score(p, y, tols=())


# ------------------------------------------------------------------------------------------ run

def run(out: Path, models_dir: Path, pool: dict, *, arms=ARMS, fractions=(0.5, 1.0), heads=("logreg", "mlp"),
        threads: int = 2, folds: int = 3, epochs: int = 60, aux_epochs: int = 30, uncond: bool = True,
        resume: bool = True, log=print, ledger: bool = False) -> dict:
    """Fit every (arm, fraction, head) on the finished training shards and score on both evaluation sets.
    results.json is written after every fit; with `resume` a fit already in it (for the same shard set) is reused."""
    import torch
    import warnings
    torch.set_num_threads(int(threads))
    warnings.filterwarnings("ignore")
    from tonematch.engines.elastic import atomic_write_json, atomic_write_text
    out, models_dir = Path(out), Path(models_dir)
    t0 = time.time()
    plan = json.loads((out / "chains.json").read_text())
    train_order = [s["id"] for s in plan["shards"] if s.get("split", "train") == "train"]
    chains, C = load_shards(out)
    tr, Ctr = select(chains, C, lambda c: c["split"] == "train")
    tests = {e: select(chains, C, lambda c, e=e: c["split"] == e) for e in EVAL_SETS}
    if not tr:
        raise RuntimeError("no finished training shards")
    assert not any("/P3" in c["di_file"] for c in chains), "P3 in the data"
    assert not {c["di_file"] for c in tr} & {c["di_file"] for c in tests["lib-test"][0]}, "test DI in training"
    assert not {c["di_file"] for c in tr} & {c["di_file"] for c in tests["prs-test"][0]}, "held-out PRS in training"
    done = [s for s in train_order if any(c["shard"] == s for c in tr)]
    res = {"brief": "T233 product-chain-v1; measurement only, nothing adopted", "n_train": len(tr),
           "n_train_shards": len(done), "n_test": {e: len(v[0]) for e, v in tests.items()},
           "fractions": {}, "fits": {}}
    rp = out / "results.json"
    if resume and rp.is_file():
        try:
            prev = json.loads(rp.read_text())
            if prev.get("n_train") == res["n_train"] and prev.get("n_test") == res["n_test"]:
                res["fits"] = prev.get("fits", {})
                log(f"resume: {len(res['fits'])} fits reused")
        except ValueError:
            pass
    Xtest = {e: PC.with_level(v[1], levels(v[0])) for e, v in tests.items()}
    Xtest_u = {e: v[1] for e, v in tests.items()}
    dkn = drive_knob_names()

    def save(key, obj):
        p = models_dir / (key.replace("/", "__").replace("+", "_") + ".pkl")
        p.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(obj.get("heads"), "net") and not isinstance(obj["heads"].net, dict):
            obj["heads"].net = obj["heads"].net.state_dict()
        with open(p, "wb") as fh:
            pickle.dump(obj, fh)
        return str(p)

    def record(key, r):
        res["fits"][key] = r
        res["elapsed_s"] = round(time.time() - t0)
        atomic_write_json(rp, res, indent=1, default=float)

    for frac in fractions:
        k = max(1, math.ceil(frac * len(done)))
        keep = set(done[:k])
        fk = f"{frac:g}"
        for arm in arms:
            ch, Xc = select(tr, Ctr, lambda c: c["shard"] in keep and (arm == "lib+prs" or c["source"] == "lib"))
            res["fractions"][f"{arm}/{fk}"] = {"chains": len(ch), "shards": k,
                                                "prs_chains": sum(c["source"] == "prs" for c in ch)}
            log(f"{arm} fraction {fk}: {len(ch)} chains ({res['fractions'][f'{arm}/{fk}']['prs_chains']} PRS)")
            if not ch:
                continue
            Xl = PC.with_level(Xc, levels(ch))
            variants = [("cond", Xl, Xtest)]
            if uncond and arm == "lib+prs" and frac == 1.0:
                variants.append(("uncond", Xc, Xtest_u))
            for var, X, XT in variants:
                for kind in heads:
                    key = f"drive/{arm}/{fk}/{kind}/{var}"
                    if key in res["fits"]:
                        continue
                    t1 = time.time()
                    d = fit_drive(kind, ch, X, pool, folds=folds, epochs=epochs, log=log)
                    r = {e: drive_eval(d["heads"], d["calibration"], d["mod_fam"], d["models"], d["knobs"],
                                       tests[e][0], XT[e], dkn) for e in EVAL_SETS}
                    r["n_train"], r["fit_s"] = len(ch), round(time.time() - t1, 1)
                    if frac == 1.0:
                        d["level_conditioned"] = var == "cond"
                        d["note"] = "T233 product-chain-v1; measurement only; not adopted"
                        r["pickle"] = save(key, d)
                    record(key, r)
                    a, b = r["lib-test"].get("all", {}), r["prs-test"].get("all", {})
                    log(f"{key}: n {len(ch)}; lib-test top1 {a.get('top1', math.nan):.3f} top3 {a.get('top3', math.nan):.3f}"
                        f" knob {a.get('knob', math.nan):.3f}; prs-test top1 {b.get('top1', math.nan):.3f} top3 "
                        f"{b.get('top3', math.nan):.3f} knob {b.get('knob', math.nan):.3f}; {r['fit_s']:.0f} s")
            for task in AUX_TASKS:
                t, n_out, _, _ = AUX_TASKS[task]
                for kind in heads:
                    key = f"{task}/{arm}/{fk}/{kind}"
                    if key in res["fits"]:
                        continue
                    t1 = time.time()
                    Xs, y = aux_data(task, ch, Xl)
                    if len(y) < 10:
                        continue
                    m = PC.make_aux(kind, t, n_out, epochs=aux_epochs).fit(Xs, y)
                    r = {e: aux_eval(task, m, tests[e][0], Xtest[e]) for e in EVAL_SETS}
                    r["n_train"], r["fit_s"] = int(len(y)), round(time.time() - t1, 1)
                    if frac == 1.0:
                        p = models_dir / f"{task}__{arm.replace('+', '_')}__{kind}.pkl"
                        p.parent.mkdir(parents=True, exist_ok=True)
                        with open(p, "wb") as fh:
                            pickle.dump({"model": m, "task": task, "arm": arm, "kind": kind, "level_conditioned": True,
                                         "note": "T233 product-chain-v1; measurement only; not adopted"}, fh)
                        r["pickle"] = str(p)
                    record(key, r)
                    log(f"{key}: n {len(y)}; lib-test {short_aux(task, r['lib-test'])}; prs-test "
                        f"{short_aux(task, r['prs-test'])}; {r['fit_s']:.0f} s")
    res["elapsed_s"] = round(time.time() - t0)
    atomic_write_json(rp, res, indent=1, default=float)
    atomic_write_text(out / "results.md", results_md(res))
    if ledger:
        append_ledger(res)
    return res


def short_aux(task: str, r: dict) -> str:
    if not r or not r.get("n"):
        return "n 0"
    t = AUX_TASKS[task][0]
    if t == "bce":
        return f"acc {r['acc']:.3f} bal {r['balanced']:.3f} auc {r['auc']:.3f} (majority {r['majority']:.3f})"
    if t == "ce":
        return f"acc {r['acc']:.3f} (chance {r['chance']:.3f})"
    return f"mae {r['mae']:.3f} (const {r['mae_const']:.3f})"


# ------------------------------------------------------------------------------------------ report

def _p(x, nd=2) -> str:
    return "" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


def _ci(r: dict, k: str) -> str:
    lo, hi = r.get(k + "_ci", (math.nan, math.nan))
    return f"{_p(r.get(k))} ({_p(lo)}-{_p(hi)})"


def results_md(res: dict) -> str:
    F = res["fits"]
    L = ["# product-chain-v1 (T233): results", "",
         f"Measurement only; nothing adopted; no pass bar. Training chains: {res['n_train']} "
         f"({res['n_train_shards']} shards). Evaluation: lib-test {res['n_test']['lib-test']} chains (held-out "
         f"Guitar-TECHS files), prs-test {res['n_test']['prs-test']} chains (held-out part of the human's session).", "",
         "## Drive (level-conditioned unless marked): model top-1 / top-3 (95 %), family, knobs within +-0.15", "",
         "| Arm | Fraction | Head | Train chains | lib-test top-1 | top-3 | family | knobs | prs-test top-1 | top-3 | "
         "family | knobs |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, r in F.items():
        if not key.startswith("drive/"):
            continue
        _, arm, fk, kind, var = key.split("/")
        a, b = r.get("lib-test", {}).get("all", {}), r.get("prs-test", {}).get("all", {})
        L.append(f"| {arm} | {fk} | {kind}{'' if var == 'cond' else ' (no level input)'} | {r['n_train']} | "
                 f"{_ci(a, 'top1')} | {_ci(a, 'top3')} | {_p(a.get('fam1'))} | {_ci(a, 'knob')} | {_ci(b, 'top1')} | "
                 f"{_ci(b, 'top3')} | {_p(b.get('fam1'))} | {_ci(b, 'knob')} |")
    L += ["", "Chance: top-1 1/54 = 0.019, top-3 0.056.", "",
          "## Drive-type knob by input level (signed error, within +-0.15), lib+prs full, MLP", "",
          "| Head | Set | Level bucket (dBFS) | n | bias | within |", "|---|---|---|---|---|---|"]
    names = ["< -40", "-40..-35", "-35..-30", "-30..-25", ">= -25"]
    for key, r in F.items():
        if key.startswith("drive/lib+prs/1/mlp/"):
            for e in EVAL_SETS:
                for bk, v in r.get(e, {}).get("drive_knob_by_level", {}).items():
                    L.append(f"| {key.split('/', 3)[3]} | {e} | {names[int(bk)] if int(bk) < len(names) else bk} | "
                             f"{v['n']} | {_p(v['bias'], 3)} | {_p(v['within'])} |")
    L += ["", "## Colouring heads (level-conditioned)", "",
          "| Task | Arm | Fraction | Head | Train | lib-test | prs-test |", "|---|---|---|---|---|---|---|"]
    for key, r in F.items():
        task = key.split("/")[0]
        if task not in AUX_TASKS:
            continue
        _, arm, fk, kind = key.split("/")
        L.append(f"| {task} | {arm} | {fk} | {kind} | {r['n_train']} | {short_aux(task, r.get('lib-test'))} | "
                 f"{short_aux(task, r.get('prs-test'))} |")
    L += ["", "Presence: accuracy at 0.5, balanced accuracy, ROC AUC, majority-class baseline. Type: accuracy vs chance. "
          "Regressions: mean absolute error vs `const` = always predicting the evaluation set's own mean (a generous "
          "baseline); reverb_logrt60 MAE is in seconds of RT60, reverb_mix in Mix units, eq_curve in dB per band.",
          "", f"Train wall {res.get('elapsed_s', 0)} s.", ""]
    return "\n".join(L)


def append_ledger(res: dict, root: Path = ROOT, extra_rows: list[tuple] = ()) -> None:
    """`benchmark / native / product-chain-v1` rows for the main numbers (lib+prs full arm; the rest in notes)."""
    os.environ.setdefault("TONEMATCH_AGENT", "learning")
    F = res["fits"]

    def row(metric, val, notes):
        if val is None or not math.isfinite(float(val)):
            return
        subprocess.run([sys.executable, str(root / "scripts/ledger_append.py"), "--kind", "benchmark", "--engine",
                        "native", "--dataset", "product-chain-v1", "--metric", metric, "--value", f"{float(val):.4f}",
                        "--renders", str(res["n_train"] + sum(res["n_test"].values())),
                        "--wall-clock-s", str(res.get("elapsed_s", 0)), "--notes", notes], check=False, cwd=str(root))

    def g(key, e, k="top3"):
        return F.get(key, {}).get(e, {}).get("all", {}).get(k, math.nan)
    for e in EVAL_SETS:
        key = "drive/lib+prs/1/mlp/cond"
        r = F.get(key)
        if not r:
            continue
        a = r[e]["all"]
        row(f"drive_mlp_top3_{e}", a["top3"],
            f"T233 measurement only not adopted. drive MLP level-conditioned, lib+prs arm all chains ({r['n_train']}); "
            f"{e} n {a['n']}; top1 {a['top1']:.3f} top3 {a['top3']:.3f} (95% {a['top3_ci'][0]:.2f}-{a['top3_ci'][1]:.2f}) "
            f"family {a['fam1']:.3f} knob0.15 {a['knob']:.3f}; lib arm top3 {g('drive/lib/1/mlp/cond', e):.3f}; half "
            f"{g('drive/lib+prs/0.5/mlp/cond', e):.3f}; logreg {g('drive/lib+prs/1/logreg/cond', e):.3f}; no-level "
            f"{g('drive/lib+prs/1/mlp/uncond', e):.3f}; chance 0.056; P3 unread")
        row(f"drive_mlp_knob_{e}", a["knob"],
            f"T233 measurement only. drive knobs within 0.15 (true model), level-conditioned MLP lib+prs; {e}; no-level "
            f"head {g('drive/lib+prs/1/mlp/uncond', e, 'knob'):.3f}; lib arm {g('drive/lib/1/mlp/cond', e, 'knob'):.3f}")
        for task, metric in (("reverb_present", "balanced"), ("reverb_type", "acc"), ("reverb_mix", "mae"),
                             ("reverb_logrt60", "mae"), ("eq_present", "balanced"), ("eq_type", "acc"),
                             ("eq_curve", "mae")):
            best = F.get(f"{task}/lib+prs/1/mlp", {}).get(e, {})
            lr = F.get(f"{task}/lib+prs/1/logreg", {}).get(e, {})
            lib = F.get(f"{task}/lib/1/mlp", {}).get(e, {})
            if best.get("n"):
                row(f"{task}_mlp_{metric}_{e}", best.get(metric),
                    f"T233 measurement only. {task} MLP level-conditioned lib+prs all chains; {e}; {short_aux(task, best)}; "
                    f"logreg {short_aux(task, lr)}; lib arm {short_aux(task, lib)}")
    for metric, val, notes in extra_rows:
        row(metric, val, notes)
