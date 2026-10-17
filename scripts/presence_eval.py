#!/usr/bin/env python3
"""Block-presence evaluation `presence-v1` (owner: tone-metrics, T102). Offline: no Native, no pedal.

Data: the helix-trust-v2 renders already on disk (Helix Native 3.82, block-library-v1.2): 192 items,
each a 10 s Guitar-TECHS DI clip with 20 single-block renders on it (one block alone, the DI in), plus
the DI itself as the "nothing present" case. Split as helix-trust-v2: train (96 items) and test
(96 items), disjoint DI recordings, and disjoint models except reverb (5 models, shared). Design,
split and pass bars are registered in docs/DECISIONS.md (2026-10-17, T102) before the test split is
scored.

  extract --split train|test   features of every render, both modes -> cache/presence-v1/features-<split>.jsonl
  fit                          fit the per-category logistic models on TRAIN only (l2 by grouped CV)
                               -> data/presence/presence-v1-<mode>.json, cache/presence-v1/fit.json
  score --split test [--ledger]  precision, recall, confusion, parameter errors -> cache/presence-v1/report-<split>.json
  reverb-synth --split test    RT60 error on synthetic reverbs of known decay on that split's DIs
  chains                       spot380 diagnostic: amp + cab + one block on the Stomp and Native (not a bar)
  gen-chains [--n 120]         write the PROPOSED multi-block set presence-v2 (manifest only; renders later)

Targets go through `loss_trust_check.aligned_capture` (residual fractional delay up to 0.5 samples,
gain within 3 dB, pink noise 60 dB down), seeded per item and render, as an aligned capture would
give them. A render whose gain-matched null against its own DI is -30 dB or lower is "transparent"
(rule B of T096: the same sound): left out of fitting and scoring, and counted.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tonematch.metrics import presence as PR

SR = 48_000
DATASET = "presence-v1"
SRC = ROOT / "cache" / "loss-trust" / "helix-trust-v2"
OUT = ROOT / "cache" / DATASET
SEED = 5102
TRANSPARENT_DB = -30.0
L2_GRID = (1e-4, 1e-3, 1e-2, 1e-1)
BASES = {"linear": (), "hinge": (-1.0, 0.0, 1.0)}     # registered amendment 2026-10-17, before test scoring
CV_FOLDS = 4
GAIN_KNOBS = ("Drive", "Gain", "Fuzz", "Sustain", "FuzzGain", "ODGain", "Distortion", "Overdrive")
REVERB_SYNTH_RT60 = (0.4, 0.8, 1.5, 2.5, 4.0)


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def _clean(v):
    if isinstance(v, float) and not np.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def manifest() -> dict:
    return json.loads((SRC / "manifest.json").read_text())


def param_value(free: list, knobs: dict, name: str) -> float | None:
    for n, lo, hi, *rest in free:
        if n == name:
            if name in knobs:
                return float(lo + knobs[name] / 127.0 * (hi - lo))
            return float(rest[0]) if rest else None
    return None


def param_range(free: list, name: str) -> tuple[float, float] | None:
    for n, lo, hi, *_ in free:
        if n == name:
            return float(lo), float(hi)
    return None


def truth(cand: dict, free_params: dict) -> dict:
    cat, model, knobs = cand["category"], cand["model"], cand.get("knobs", {})
    free = free_params.get(cat, {}).get(model, [])
    t: dict = {}
    if cat == "delay":
        t["time_s"] = param_value(free, knobs, "Time")
        sc = param_value(free, knobs, "Scale")
        if sc is not None and t["time_s"] is not None:
            t["time_scaled_s"] = t["time_s"] * sc      # a ping-pong's other side: Time x Scale
        t["mix"] = param_value(free, knobs, "Mix")
        t["feedback"] = param_value(free, knobs, "Feedback")
    if cat == "mod":
        rng = param_range(free, "Speed")
        if rng and rng[1] > 1.0 and "pattern" not in model.lower():   # pattern trems step, they do not sweep
            t["rate_hz"] = param_value(free, knobs, "Speed")
    if cat == "reverb":
        t["decay_knob"] = param_value(free, knobs, "Decay")
        t["mix"] = param_value(free, knobs, "Mix")
    if cat in ("drive", "amp"):
        for g in GAIN_KNOBS:
            r = param_range(free, g)
            if r:
                v = param_value(free, knobs, g)
                t["gain_knob"] = g
                t["gain_frac"] = (v - r[0]) / (r[1] - r[0]) if r[1] > r[0] else None
                break
    return t


def _item_rows(args) -> list[dict]:
    import soundfile as sf
    from loss_trust_check import aligned_capture
    it, free_params, done = args
    x, sr = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")
    assert sr == SR
    rows = []
    jobs = [(k, c) for k, c in enumerate(it["candidates"])] + [(99, None)]
    for k, cand in jobs:
        if (it["item"], k) in done:
            continue
        if cand is None:
            y = x
            cat, model, knobs, tr = "none", None, {}, {}
        else:
            y = np.load(SRC / "renders" / it["dir"] / f"c{k:02d}_A.npy")
            cat, model, knobs, tr = cand["category"], cand["model"], cand.get("knobs", {}), truth(cand, free_params)
        rng = np.random.default_rng([SEED, it["item"], k])
        tgt = aligned_capture(y, rng).astype(np.float64)
        t0 = time.time()
        raw_null = PR.null_db(y, x) if cand is not None else -200.0
        fk, ek = PR.features_known_di(tgt, x)
        fn, en = PR.features_no_di(tgt)
        rows.append(_clean({"item": it["item"], "k": k, "split": it["split"], "category": cat, "model": model,
                            "label": cand["label"] if cand else "di", "knobs": knobs, "truth": tr,
                            "raw_null_db": raw_null, "known_di": fk, "known_di_est": ek, "no_di": fn, "no_di_est": en,
                            "seconds": time.time() - t0}))
    return rows


def features_path(split: str) -> Path:
    return OUT / f"features-{split}.jsonl"


def load_rows(split: str) -> list[dict]:
    """Feature rows of a split, with `truth` recomputed from the manifest (so a change to `truth`
    applies to rows extracted earlier)."""
    p = features_path(split)
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []
    if rows:
        m = manifest()
        items = {it["item"]: it for it in m["items"]}
        for r in rows:
            if r["k"] != 99:
                r["truth"] = _clean(truth(items[r["item"]]["candidates"][r["k"]], m["free_params"]))
    return rows


def cmd_extract(a) -> int:
    m = manifest()
    OUT.mkdir(parents=True, exist_ok=True)
    done = {(r["item"], r["k"]) for r in load_rows(a.split)}
    items = [it for it in m["items"] if it["split"] == a.split]
    if a.limit:
        items = items[: a.limit]
    t0 = time.time()
    n = 0
    with open(features_path(a.split), "a") as fh, ProcessPoolExecutor(max_workers=a.workers) as ex:
        for rows in ex.map(_item_rows, [(it, m["free_params"], done) for it in items]):
            fh.writelines(json.dumps(r, default=_json_default) + "\n" for r in rows)
            fh.flush()
            n += len(rows)
            print(f"{n} rows, {time.time() - t0:.0f} s", flush=True)
    print(f"extract {a.split}: {n} new rows in {time.time() - t0:.0f} s")
    return 0


# ------------------------------------------------------------------------------------- scoring

def usable(rows: list[dict]) -> tuple[list[dict], int]:
    keep = [r for r in rows if r["category"] == "none" or r["raw_null_db"] is None or r["raw_null_db"] > TRANSPARENT_DB]
    return keep, len(rows) - len(keep)


def labels_of(rows: list[dict]) -> list[set[str]]:
    return [set() if r["category"] == "none" else {r["category"]} for r in rows]


def prf(pred: np.ndarray, true: np.ndarray) -> dict:
    tp = int(np.sum(pred & true))
    fp = int(np.sum(pred & ~true))
    fn = int(np.sum(~pred & true))
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * p * r / (p + r) if (tp + fp) and (tp + fn) and (p + r) > 0 else 0.0
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "fp": fp, "fn": fn, "positives": int(true.sum())}


def evaluate(rows: list[dict], model: PR.PresenceModel, mode: str) -> dict:
    probs = [model.predict(r[mode]) for r in rows]
    cats = [c for c in PR.CATEGORIES if c in model.coef]
    true_cat = np.array([r["category"] for r in rows])
    out: dict = {"n": len(rows), "per_category": {}, "flag_rate": {}, "top1": {}}
    for c in cats:
        pred = np.array([p[c] >= model.threshold for p in probs])
        out["per_category"][c] = prf(pred, true_cat == c)
    # drive and amp together (both nonlinear: "some gain stage")
    pred = np.array([max(p.get("drive", 0), p.get("amp", 0)) >= model.threshold for p in probs])
    out["per_category"]["drive_or_amp"] = prf(pred, np.isin(true_cat, ["drive", "amp"]))
    f1s = [out["per_category"][c]["f1"] for c in cats]
    out["macro_f1"] = float(np.mean(f1s))
    # confusion: for each true category, how often each category is flagged
    for t in [*cats, "none"]:
        sel = true_cat == t
        if sel.any():
            out["flag_rate"][t] = {c: float(np.mean([probs[i][c] >= model.threshold for i in np.flatnonzero(sel)])) for c in cats}
            top = [max(cats, key=lambda c, i=i: probs[i][c]) if max(probs[i].values()) >= model.threshold else "none"
                   for i in np.flatnonzero(sel)]
            out["top1"][t] = {c: float(np.mean([x == c for x in top])) for c in [*cats, "none"] if any(x == c for x in top)}
    out["top1_accuracy"] = float(np.mean([
        (max(cats, key=lambda c, i=i: probs[i][c]) if max(probs[i].values()) >= model.threshold else "none") == true_cat[i]
        for i in range(len(rows))]))
    return out


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    from scipy.stats import spearmanr
    if a.size < 4:
        return float("nan")
    return float(spearmanr(a, b).statistic)


def _near_multiple(ratio: np.ndarray, tol: float = 0.05) -> np.ndarray:
    """Estimate/true near a whole multiple (2, 3, ...) or a whole fraction (1/2, 1/3, ...)."""
    r = np.where(np.isfinite(ratio) & (ratio > 0), ratio, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        up = (r >= 1.5) & (np.abs(r - np.round(r)) <= tol * np.round(r))
        inv = 1.0 / r
        down = (inv >= 1.5) & (np.abs(inv - np.round(inv)) <= tol * np.round(inv))
    return np.nan_to_num(up | down, nan=0).astype(bool)


def parameter_errors(rows: list[dict], mode: str) -> dict:
    est_key = f"{mode}_est"
    out: dict = {}
    # delay time: renders whose model has a Time knob, true time >= 40 ms
    sel = [r for r in rows
           if r["category"] == "delay" and r["truth"].get("time_s") is not None and r["truth"]["time_s"] >= 0.04]
    d = [(r["truth"]["time_s"], r[est_key]["delay"]["time_s"], r["model"]) for r in sel]
    if d:
        tt = np.array([a for a, _, _ in d])
        ee = np.array([b if b is not None else np.nan for _, b, _ in d])
        rel_strict = np.abs(ee - tt) / tt
        # scale-aware: a model with a Scale knob may put its first echo at Time x Scale (ping-pong)
        ts = np.array([r["truth"].get("time_scaled_s") or np.nan for r in sel])
        ts = np.where(ts >= 0.02, ts, np.nan)
        rel_scaled = np.abs(ee - ts) / ts
        rel = np.fmin(rel_strict, np.where(np.isfinite(rel_scaled), rel_scaled, np.inf))
        found = np.isfinite(ee)
        relf = np.where(found, rel, np.inf)
        rel_strict_f = np.where(found, rel_strict, np.inf)
        ratio = ee / tt
        out["delay_time"] = {
            "n": int(tt.size), "found": float(found.mean()),
            "median_abs_err_pct": float(100 * np.median(relf)) if np.isfinite(np.median(relf)) else None,
            "within_5pct": float(np.mean(relf <= 0.05)), "within_10pct": float(np.mean(relf <= 0.10)),
            "within_10pct_strict": float(np.mean(rel_strict_f <= 0.10)),
            "multiple_or_fraction": float(np.mean(found & (relf > 0.10) & _near_multiple(ratio))),
            "by_model": {},
        }
        for mdl in sorted({m for _, _, m in d}):
            s = np.array([m == mdl for _, _, m in d])
            out["delay_time"]["by_model"][mdl] = {"n": int(s.sum()), "within_10pct": float(np.mean(relf[s] <= 0.10))}
    # LFO rate: mod models whose Speed range is in Hz
    q = [(r["truth"]["rate_hz"], r[est_key]["mod"]["rate_hz"], r["model"]) for r in rows
         if r["category"] == "mod" and r["truth"].get("rate_hz") is not None and r["truth"]["rate_hz"] >= 0.4]
    if q:
        tt = np.array([a for a, _, _ in q])
        ee = np.array([b if b is not None else np.nan for _, b, _ in q])
        rel = np.where(np.isfinite(ee), np.abs(ee - tt) / tt, np.inf)
        out["lfo_rate"] = {"n": int(tt.size), "median_abs_err_pct": float(100 * np.median(rel)),
                           "within_5pct": float(np.mean(rel <= 0.05)), "within_10pct": float(np.mean(rel <= 0.10)),
                           "by_model": {mdl: {"n": int(sum(m == mdl for *_, m in q)),
                                              "within_10pct": float(np.mean(rel[np.array([m == mdl for *_, m in q])] <= 0.10))}
                                        for mdl in sorted({m for *_, m in q})}}
    # reverb: no decay time in seconds on the knobs (Decay is 0..1): rank agreement within model
    rv = [(r["model"], r["truth"].get("decay_knob"), r[est_key]["reverb"].get("rt60_s")) for r in rows if r["category"] == "reverb"]
    if rv and mode == "known_di":
        per = {}
        for mdl in sorted({m for m, _, _ in rv}):
            s = [(k, e) for m, k, e in rv if m == mdl and k is not None and e is not None]
            if len(s) >= 4:
                per[mdl] = {"n": len(s), "spearman": _spearman(np.array([k for k, _ in s]), np.array([e for _, e in s]))}
        out["reverb_decay_rank"] = {"by_model": per,
                                    "median_spearman": float(np.median([v["spearman"] for v in per.values()])) if per else None,
                                    "found": float(np.mean([e is not None for _, _, e in rv]))}
    # gain index against the gain knob, within item and model (near misses that moved the gain knob)
    if mode == "known_di":
        agree = []
        by = {}
        for r in rows:
            if r["category"] in ("drive", "amp") and r["truth"].get("gain_frac") is not None:
                by.setdefault((r["item"], r["model"]), []).append((r["truth"]["gain_frac"], r["known_di"]["nl_resid_db"]))
        for v in by.values():
            for i in range(len(v)):
                for j in range(i + 1, len(v)):
                    (ga, ia), (gb, ib) = v[i], v[j]
                    if abs(ga - gb) >= 0.1 and ia is not None and ib is not None:
                        agree.append((ga - gb) * (ia - ib) > 0)
        out["gain_index_rank"] = {"pairs": len(agree), "agreement": float(np.mean(agree)) if agree else None}
    return out


def subtype_eval(rows: list[dict]) -> dict:
    """Modulation sub-type rule against the model's family (from its name), known-DI only."""
    def family(model: str) -> str | None:
        s = model.lower()
        for k in ("trem", "chorus", "flanger", "phaser", "vibrato"):
            if k in s:
                return {"trem": "tremolo"}.get(k, k)
        return None
    res = []
    for r in rows:
        if r["category"] != "mod":
            continue
        fam = family(r["model"])
        if fam:
            res.append((fam, r["known_di_est"]["mod"]["subtype"]))
    if not res:
        return {}
    out = {"n": len(res), "accuracy": float(np.mean([a == b for a, b in res])), "by_family": {}}
    for fam in sorted({a for a, _ in res}):
        got = [b for a, b in res if a == fam]
        out["by_family"][fam] = {"n": len(got), "accuracy": float(np.mean([b == fam for b in got])),
                                 "called": {k: [str(g) for g in got].count(k) for k in sorted({str(g) for g in got})}}
    return out


def cv_score(rows: list[dict], mode: str, l2: float, knots: tuple[float, ...] = ()) -> float:
    items = sorted({r["item"] for r in rows})
    rng = np.random.default_rng(SEED)
    perm = rng.permutation(items)
    folds = [set(perm[i::CV_FOLDS]) for i in range(CV_FOLDS)]
    f1 = []
    for f in folds:
        tr = [r for r in rows if r["item"] not in f]
        te = [r for r in rows if r["item"] in f]
        mdl = PR.fit_model([r[mode] for r in tr], labels_of(tr), mode, l2=l2, knots=knots)
        f1.append(evaluate(te, mdl, mode)["macro_f1"])
    return float(np.mean(f1))


def cmd_fit(a) -> int:
    rows, n_transp = usable(load_rows("train"))
    if not rows:
        print("no train features: run extract --split train first")
        return 2
    report = {"dataset": DATASET, "split": "train", "rows": len(rows), "transparent_excluded": n_transp, "modes": {}}
    for mode in PR.MODES:
        cv = {f"{b}/{l2}": cv_score(rows, mode, l2, BASES[b]) for b in BASES for l2 in L2_GRID}
        key = max(cv, key=lambda k: (cv[k], float(k.split("/")[1])))
        b, best = key.split("/")[0], float(key.split("/")[1])
        mdl = PR.fit_model([r[mode] for r in rows], labels_of(rows), mode, l2=best, knots=BASES[b],
                           trained_on=f"{DATASET} train ({len(rows)} renders, helix-trust-v2 train split)")
        mdl.save(PR.model_path(mode))
        train_eval = evaluate(rows, mdl, mode)
        report["modes"][mode] = {"cv_macro_f1": cv, "l2": best, "basis": b, "train_macro_f1_in_sample": train_eval["macro_f1"],
                                 "train_per_category": train_eval["per_category"], "model_file": str(PR.model_path(mode).relative_to(ROOT))}
        print(mode, "cv", {k: round(v, 3) for k, v in cv.items()}, "->", b, best, "in-sample macro F1", round(train_eval["macro_f1"], 3))
        print("   ", {c: (round(v["precision"], 2), round(v["recall"], 2)) for c, v in train_eval["per_category"].items()})
    report["train_parameters"] = {m: parameter_errors(rows, m) for m in PR.MODES}
    report["train_subtype"] = subtype_eval(rows)
    (OUT / "fit.json").write_text(json.dumps(report, indent=1, default=_json_default) + "\n")
    print(json.dumps({"params": report["train_parameters"], "subtype": report["train_subtype"]}, default=_json_default)[:3000])
    return 0


def ledger(metric: str, value: float, notes: str, renders: int | str = "", wall: float | str = "") -> None:
    if value is None or not np.isfinite(value):
        return
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "native",
                    "--dataset", DATASET, "--metric", metric, "--value", f"{value:.4f}", "--renders", str(renders),
                    "--wall-clock-s", str(wall), "--notes", notes], check=True, env=env)


def cmd_score(a) -> int:
    all_rows = load_rows(a.split)
    rows, n_transp = usable(all_rows)
    if not rows:
        print(f"no {a.split} features")
        return 2
    rep = {"dataset": DATASET, "split": a.split, "rows": len(rows), "transparent_excluded": n_transp, "modes": {}}
    for mode in PR.MODES:
        mdl = PR.load_model(mode)
        if mdl is None:
            print("no fitted model: run fit")
            return 2
        ev = evaluate(rows, mdl, mode)
        ev["parameters"] = parameter_errors(rows, mode)
        rep["modes"][mode] = ev
    rep["subtype_known_di"] = subtype_eval(rows)
    path = OUT / f"report-{a.split}.json"
    path.write_text(json.dumps(rep, indent=1, default=_json_default) + "\n")
    for mode, ev in rep["modes"].items():
        print(f"== {mode}: macro F1 {ev['macro_f1']:.3f}, top-1 {ev['top1_accuracy']:.3f}")
        for c, v in ev["per_category"].items():
            print(f"   {c:13s} P {v['precision']:.3f} R {v['recall']:.3f} F1 {v['f1']:.3f} (pos {v['positives']})")
        print("   params", json.dumps(ev["parameters"], default=_json_default)[:1500])
    print("subtype", json.dumps(rep["subtype_known_di"]))
    if a.ledger:
        n = len(all_rows)
        base = f"split={a.split} rows={len(rows)} transparent_excluded={n_transp} single Helix Native blocks on clean DI (helix-trust-v2 renders) threshold 0.5"
        for mode, ev in rep["modes"].items():
            ledger(f"macro_f1_{mode}", ev["macro_f1"], f"{base} mode={mode} top1={ev['top1_accuracy']:.3f}", renders=n)
            for c, v in ev["per_category"].items():
                ledger(f"f1_{c}_{mode}", v["f1"], f"{base} mode={mode} precision={v['precision']:.3f} recall={v['recall']:.3f} positives={v['positives']} fp={v['fp']}", renders=n)
            pe = ev["parameters"]
            if "delay_time" in pe:
                dt = pe["delay_time"]
                ledger(f"delay_time_within10_{mode}", dt["within_10pct"], f"{base} mode={mode} n={dt['n']} found={dt['found']:.3f} median_abs_err_pct={dt['median_abs_err_pct']} within5={dt['within_5pct']:.3f} within10_strict_time_only={dt['within_10pct_strict']:.3f} multiple_or_fraction={dt['multiple_or_fraction']:.3f} (Time or Time x Scale)", renders=n)
            if "lfo_rate" in pe:
                lr = pe["lfo_rate"]
                ledger(f"lfo_rate_within10_{mode}", lr["within_10pct"], f"{base} mode={mode} n={lr['n']} median_abs_err_pct={lr['median_abs_err_pct']:.2f} within5={lr['within_5pct']:.3f}", renders=n)
            if "reverb_decay_rank" in pe and pe["reverb_decay_rank"]["median_spearman"] is not None:
                rr = pe["reverb_decay_rank"]
                ledger(f"reverb_decay_spearman_{mode}", rr["median_spearman"], f"{base} mode={mode} RT60 estimate vs Decay knob within model; per-model median; found={rr['found']:.3f}", renders=n)
            if pe.get("gain_index_rank", {}).get("agreement") is not None:
                g = pe["gain_index_rank"]
                ledger(f"gain_index_rank_{mode}", g["agreement"], f"{base} mode={mode} pairs={g['pairs']} gain index orders same-model renders by their gain knob", renders=n)
        st = rep["subtype_known_di"]
        if st:
            ledger("mod_subtype_acc_known_di", st["accuracy"], f"{base} n={st['n']} by_family=" + "/".join(f"{k}={v['accuracy']:.2f}" for k, v in st["by_family"].items()), renders=n)
    return 0


def cmd_reverb_synth(a) -> int:
    """RT60 error on synthetic reverbs of known decay (equal in every octave) on the split's DIs."""
    import soundfile as sf
    from loss_trust_check import aligned_capture

    from tonematch.metrics.reverb import synthetic_reverb
    m = manifest()
    items = [it for it in m["items"] if it["split"] == a.split]
    res = []
    for it in items[:: max(1, len(items) // a.n_items)][: a.n_items]:
        x, _ = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")
        x = x.astype(np.float64)
        for j, rt in enumerate(REVERB_SYNTH_RT60):
            y = synthetic_reverb(x, [rt] * 7, seed=it["item"] * 10 + j, wet_gain=0.5)
            tgt = aligned_capture(y, np.random.default_rng([SEED, it["item"], 200 + j])).astype(np.float64)
            _, est = PR.features_known_di(tgt, x)
            res.append((rt, est["reverb"]["rt60_s"]))
    tt = np.array([a for a, _ in res])
    ee = np.array([b if b is not None and np.isfinite(b) else np.nan for _, b in res])
    rel = np.where(np.isfinite(ee), np.abs(ee - tt) / tt, np.inf)
    out = {"split": a.split, "n": len(res), "median_abs_err_pct": float(100 * np.median(rel)),
           "within_20pct": float(np.mean(rel <= 0.2)), "found": float(np.mean(np.isfinite(ee))),
           "by_rt60": {str(rt): {"median_est_s": float(np.nanmedian(ee[tt == rt])), "within_20pct": float(np.mean(rel[tt == rt] <= 0.2))}
                       for rt in REVERB_SYNTH_RT60}}
    (OUT / f"reverb-synth-{a.split}.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out))
    if a.ledger:
        ledger("reverb_rt60_within20_known_di", out["within_20pct"],
               f"split={a.split} synthetic exponential reverbs (RT60 {'/'.join(map(str, REVERB_SYNTH_RT60))} s; wet 0.5) on {a.n_items} helix-trust-v2 DIs; aligned capture; median_abs_err_pct={out['median_abs_err_pct']:.1f} found={out['found']:.3f}",
               renders=0)
    return 0


def cmd_chains(a) -> int:
    """spot380 (T060): amp (Essex A15) + cab + one block, Stomp and Native, 4 s guitar + 2 s silence.
    Diagnostic only: the models were fitted on single blocks, and here amp and cab are always on."""
    import soundfile as sf
    spot = ROOT / "cache" / "parity" / "spot380"
    man = json.loads((spot / "manifest.json").read_text())
    x, _ = sf.read(str(spot / "di_guitar.wav"), dtype="float32")
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(1)
    cat_map = {"Distortion": "drive", "Delay": "delay", "Reverb": "reverb", "Dynamics": "comp", "EQ": "eq",
               "Modulation": "mod", "Amp": "amp", "Cab": "cab", "ER": "cab"}
    mk, mn = PR.load_model("known_di"), PR.load_model("no_di")
    out = []
    for it in man["items"]:
        for eng in ("stomp", "native"):
            f = spot / "raw" / f"{it['name']}__guitar__{eng}.wav"
            if not f.exists():
                continue
            y, _ = sf.read(str(f), dtype="float32")
            y = y.astype(np.float64)
            y = y.mean(1) if y.ndim > 1 else y
            fk, ek = PR.features_known_di(y, x)
            fn, _ = PR.features_no_di(y)
            out.append({"name": it["name"], "engine": eng, "category": cat_map.get(it["category"], it["category"]),
                        "known_di": mk.predict(fk) if mk else None, "no_di": mn.predict(fn) if mn else None,
                        "subtype": ek["mod"]["subtype"], "delay_time_s": ek["delay"]["time_s"], "rt60_s": ek["reverb"]["rt60_s"],
                        "rate_hz": ek["mod"]["rate_hz"]})
    (OUT / "chains-spot380.json").write_text(json.dumps(_clean(out), indent=1, default=_json_default) + "\n")
    # extra block detected on top of amp + cab: the category with the highest probability other than amp and cab
    for mode in ("known_di", "no_di"):
        hits = []
        for r in out:
            if r[mode] is None or r["category"] in ("amp", "cab"):
                continue
            p = {c: v for c, v in r[mode].items() if c not in ("amp", "cab")}
            hits.append((r["category"], max(p, key=p.get), p.get(r["category"], 0.0) >= 0.5))
        if hits:
            print(mode, "extra-block top-1", f"{np.mean([a == b for a, b, _ in hits]):.3f}", "flagged", f"{np.mean([h for *_, h in hits]):.3f}", f"n={len(hits)}")
            for cat in sorted({h[0] for h in hits}):
                s = [h for h in hits if h[0] == cat]
                print(f"   {cat:7s} top1 {np.mean([a == b for a, b, _ in s]):.2f} flagged {np.mean([h for *_, h in s]):.2f} n={len(s)} called {[b for _, b, _ in s]}")
    return 0


CHAIN_SEED = 5103
CHAIN_CATS = ("drive", "amp", "cab", "comp", "eq", "mod", "delay", "reverb")


def cmd_gen_chains(a) -> int:
    """Write the manifest of `presence-v2` (PROPOSED, not rendered): multi-block chains for the lead to
    schedule on Native after native-verify-v1. Offline. Category sets are drawn from the committed
    chain prior's share of presets containing each category (amp and cab together), ordered by
    `chain_prior.top_structures`; models and knobs from helix-trust-v2's TEST split lists (CC grid at
    0.10-0.90 of each template range); DI clips from the test split's DI files. Render plan: block-serial
    (T045), each block on the previous block's output, every intermediate kept, so each block's own
    input is known (per-block isolation truth and a per-block transparency check)."""
    from tonematch import chain_prior as CP
    m = manifest()
    prior = CP.load_prior()
    share = {c: float(prior["share_present"].get(c, 0.0)) for c in CHAIN_CATS}
    rng = np.random.default_rng(CHAIN_SEED)
    models = m["splits"]["test"]["models"]
    dis = [it["di"]["A"] for it in m["items"] if it["split"] == "test"]
    chains = []
    for i in range(a.n):
        cats = {c for c in CHAIN_CATS if c not in ("cab",) and rng.random() < share[c]}
        if "amp" in cats and rng.random() < share["cab"] / max(share["amp"], 1e-9):
            cats.add("cab")
        if not cats:
            cats = {str(rng.choice(["drive", "delay", "reverb", "mod"]))}
        order = list(CP.top_structures(set(cats), k=1, prior=prior)[0][0])
        blocks = []
        for c in order:
            pool = models.get(c) or models.get("drive")
            mdl = str(rng.choice(pool))
            free = m["free_params"].get(c, {}).get(mdl, [])
            knobs = {n: int(np.clip(round(rng.uniform(0.10, 0.90) * 127), 0, 127)) for n, *_ in free}
            blocks.append({"category": c, "model": mdl, "knobs": knobs})
        chains.append({"chain": i, "categories": order, "blocks": blocks, "di": dis[int(rng.integers(len(dis)))]})
    out = ROOT / "cache" / "presence-v2"
    out.mkdir(parents=True, exist_ok=True)
    doc = {"dataset": "presence-v2", "status": "proposed, not rendered", "seed": CHAIN_SEED, "seconds": 10.0,
           "library_version": m.get("library_version"), "source_split": "helix-trust-v2 test (models and DI files)",
           "render": "block-serial on Native (T045): block k rendered on block k-1's output, all intermediates kept",
           "share_present": share, "chains": chains}
    (out / "manifest.json").write_text(json.dumps(doc, indent=1) + "\n")
    n_blocks = [len(c["blocks"]) for c in chains]
    from collections import Counter
    print(f"presence-v2 manifest: {len(chains)} chains, {sum(n_blocks)} block renders, blocks per chain {Counter(n_blocks)}")
    print("category counts", Counter(c for ch in chains for c in ch["categories"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--split", choices=("train", "test"), required=True)
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--limit", type=int, default=0)
    sub.add_parser("fit")
    s = sub.add_parser("score")
    s.add_argument("--split", choices=("train", "test"), required=True)
    s.add_argument("--ledger", action="store_true")
    r = sub.add_parser("reverb-synth")
    r.add_argument("--split", choices=("train", "test"), required=True)
    r.add_argument("--n-items", type=int, default=12)
    r.add_argument("--ledger", action="store_true")
    sub.add_parser("chains")
    g = sub.add_parser("gen-chains")
    g.add_argument("--n", type=int, default=120)
    a = p.parse_args(argv)
    return {"extract": cmd_extract, "fit": cmd_fit, "score": cmd_score, "reverb-synth": cmd_reverb_synth,
            "chains": cmd_chains, "gen-chains": cmd_gen_chains}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
