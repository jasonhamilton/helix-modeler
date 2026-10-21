"""Predictor-first drive matcher (T196). NOT the default; measurement option `match_block.py --shortlist predictor`.

Instead of rendering a 336-point grid of every model (the AFx-Rep shortlist), embed the target once, run the trained
drive head (T191 drive-dataset-v1, MLP on multi-crop AFx-Rep) and ask it for:
  * a calibrated probability per model (temperature from the T189 calibration pool),
  * a Drive and a Tone mean and sigma per model (sigma scaled by the same calibration).
Then render a few confirming candidates only for the models that cover `cum` of the probability (at most `max_models`):
the mean, and +-1 sigma on Drive and on Tone (5 renders for a two-knob model; a Drive-only model gets the mean and
+-1, +-2 sigma). The final model order is the AFx-Rep distance of the best candidate of each model (the T185/v5
ranking); the predictor's own knobs (no renders at all) are kept next to the confirmed ones.

Everything here is pure given an `embed_crops` callable and an engine, so tests run with a fake engine and a fake head.
Knob positions are 0..1 over the Native range of the knob, the same convention as the dataset labels (CC / 127).
"""
from __future__ import annotations

import pickle
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
SR = 48_000
DEFAULT_HEADS = ROOT / "cache" / "models" / "predictor-dataset-v1" / "heads_main_v2data_mlp_multi.pkl"
N_CROPS, CROP_S = 3, 10.0 / 3.0
SIGMA_FLOOR = 0.05          # never let a candidate spread collapse below 5% of the knob range
DEFAULT_CUM, DEFAULT_MAX_MODELS = 0.95, 6


# ------------------------------------------------------------------------------------------- the head

@dataclass
class Prediction:
    models: list[str]            # head order
    p: np.ndarray                # (M,) calibrated probability, sums to 1
    mu: np.ndarray               # (M, 2) Drive/Tone mean (nan where the model has no such knob)
    sig: np.ndarray              # (M, 2) calibrated sigma
    temperature: float

    def top(self, k: int = 5) -> list[dict]:
        return [{"model": self.models[i], "p": float(self.p[i]),
                 "drive": _f(self.mu[i, 0]), "drive_sigma": _f(self.sig[i, 0]),
                 "tone": _f(self.mu[i, 1]), "tone_sigma": _f(self.sig[i, 1])} for i in np.argsort(-self.p)[:k]]


def _f(x) -> float | None:
    return None if not np.isfinite(x) else float(x)


class Head:
    """The pickled T191 head (`heads_main_v2data_mlp_multi.pkl`): MLP weights, calibration, the model list."""

    def __init__(self, heads, calibration, models: list[str], families: list[str]):
        self.heads, self.cal, self.models, self.families = heads, calibration, list(models), list(families)
        self.mod_fam = np.asarray(heads.mod_fam)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Head:
        d = pickle.load(open(path or DEFAULT_HEADS, "rb"))   # our own file from drive_dataset_render train
        h = d["heads"]
        if isinstance(getattr(h, "net", None), dict):        # saved as a state_dict
            import torch
            net = h._build(512, h.n_fam, h.M)
            net.load_state_dict(h.net)
            h.net = net.eval()
            h.device = "cpu"
        return cls(h, d["calibration"], d["models"], d["families"])

    def predict(self, crops: np.ndarray, allowed: Sequence[str] | None = None) -> Prediction:
        """crops: (n_crops, 512) AFx-Rep mid embeddings of ONE target. `allowed` masks models (e.g. the ones the
        dataset never contained); probabilities are renormalised over what is left."""
        from tonematch.learning import afx_heads as ah
        out = self.heads.predict(np.asarray(crops, np.float32)[None])
        T = self.cal.T if self.cal is not None else 1.0
        lj, _ = ah.joint_logp(out["fl"], out["lc"], self.mod_fam, T)
        lj = lj[0].copy()
        if allowed is not None:
            al = set(allowed)
            ok = np.array([m in al for m in self.models])
            lj[~ok] = -np.inf
        p = np.exp(lj - np.max(lj))
        p /= p.sum()
        scale = np.array(self.cal.scale if self.cal is not None else (1.0, 1.0))
        return Prediction(self.models, p, out["mu"][0].astype(float), out["sig"][0].astype(float) * scale[None, :], float(T))


def target_crops(x: np.ndarray, sr: int = SR, n: int = N_CROPS, crop_s: float = CROP_S) -> list[np.ndarray]:
    """`n` crops of `crop_s` s spread evenly over the target (a 10 s target gives the three contiguous crops the head
    was trained on). A target shorter than n crops is repeat-padded."""
    L = int(crop_s * sr)
    x = np.asarray(x, np.float32)
    if len(x) < L:
        x = np.pad(x, (0, L - len(x)), mode="wrap") if len(x) else np.zeros(L, np.float32)
    if len(x) >= n * L:                                       # a crop centred in each third
        third = len(x) // n
        starts = [k * third + (third - L) // 2 for k in range(n)]
    else:                                                     # shorter: overlapping crops from first to last
        starts = [int(v) for v in np.linspace(0, len(x) - L, n)]
    return [x[s:s + L] for s in starts]


def embed_crops_afxrep(x: np.ndarray) -> np.ndarray:
    """(3, 512) AFx-Rep mid embeddings of the target's crops (the shared embedder, as drive_dataset_render.embed_clip)."""
    from tonematch.metrics import afxrep
    feats = afxrep.get_embedder().embed_batch(target_crops(x))
    return np.stack([np.asarray(f.mid, np.float32) for f in feats])


# ------------------------------------------------------------------------------------------- selection and candidates

def select_models(pred: Prediction, allowed: Sequence[str] | None = None, cum: float = DEFAULT_CUM,
                  max_models: int = DEFAULT_MAX_MODELS) -> list[str]:
    """Most probable models until their probability reaches `cum` (at least 1, at most `max_models`)."""
    ok = None if allowed is None else set(allowed)
    out, tot = [], 0.0
    for i in np.argsort(-pred.p):
        m = pred.models[i]
        if ok is not None and m not in ok:
            continue
        out.append(m)
        tot += float(pred.p[i])
        if tot >= cum or len(out) >= max_models:
            break
    return out


def knob_points(mu: Sequence[float], sig: Sequence[float], floor: float = SIGMA_FLOOR) -> list[dict]:
    """Candidate knob positions around the prediction. Two-knob model: mean, Drive+-1 sigma, Tone+-1 sigma (5).
    Drive-only: mean, +-1, +-2 sigma (5). No knob: one point. Positions are clipped to 0..1 and de-duplicated."""
    d, t = mu
    sd, st = (max(float(s), floor) if np.isfinite(s) else floor for s in sig)
    has_d, has_t = np.isfinite(d), np.isfinite(t)
    c = lambda v: float(min(max(v, 0.0), 1.0))   # noqa: E731
    pts: list[tuple[str, float | None, float | None]] = []
    if has_d and has_t:
        pts = [("mean", c(d), c(t)), ("d-1s", c(d - sd), c(t)), ("d+1s", c(d + sd), c(t)),
               ("t-1s", c(d), c(t - st)), ("t+1s", c(d), c(t + st))]
    elif has_d:
        pts = [("mean", c(d), None), ("d-1s", c(d - sd), None), ("d+1s", c(d + sd), None),
               ("d-2s", c(d - 2 * sd), None), ("d+2s", c(d + 2 * sd), None)]
    elif has_t:
        pts = [("mean", None, c(t)), ("t-1s", None, c(t - st)), ("t+1s", None, c(t + st))]
    else:
        pts = [("default", None, None)]
    seen, out = set(), []
    for lab, dd, tt in pts:
        key = (None if dd is None else round(dd, 3), None if tt is None else round(tt, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": lab, "drive": dd, "tone": tt})
    return out


def values_for(block, knobs: dict, drive: float | None, tone: float | None) -> dict[str, float]:
    """Parameter values of `block` (its `free` list [(name, lo, hi, default)]) for knob positions 0..1; the other
    knobs stay at their default. `knobs` is the T179 entry {"drive_knobs": [...], "tone_knobs": [...]}."""
    free = {n: (lo, hi, d) for n, lo, hi, d in block.free}
    vals = {n: float(d) for n, (lo, hi, d) in free.items()}
    for key, pos in (("drive_knobs", drive), ("tone_knobs", tone)):
        if pos is None:
            continue
        name = next((k for k in (knobs or {}).get(key, []) if k in free), None)
        if name is None:
            continue
        lo, hi, _ = free[name]
        vals[name] = lo + pos * (hi - lo)
    return vals


# ------------------------------------------------------------------------------------------- the stage

def run_predictor_first(engine, blocks: dict, section, head: Head, knob_map: dict, *,
                        embed_crops: Callable[[np.ndarray], np.ndarray] = embed_crops_afxrep,
                        embed: Callable | None = None, dist: Callable | None = None,
                        cum: float = DEFAULT_CUM, max_models: int = DEFAULT_MAX_MODELS,
                        allowed: Sequence[str] | None = None, log=print) -> dict:
    """Stage 1+2 of the predictor-first matcher. Returns
      predicted   the head's top models with p and knob mean/sigma (no renders);
      predictor_own  {model, values} of its most probable model at the mean knobs;
      selected    the models that cover `cum` (at most `max_models`);
      candidates  every rendered candidate with its AFx-Rep distance;
      ranked      [{model, distance}] by the best candidate of each selected model (the final order);
      rows        match_block-style rows (best candidate per model, in `ranked` order);
      renders, embed_s, render_s, wall_s."""
    if embed is None or dist is None:
        from tonematch.metrics import afxrep
        embed, dist = embed or afxrep.features, dist or afxrep.distance
    t0 = time.time()
    names = set(blocks) if allowed is None else set(allowed) & set(blocks)
    crops = embed_crops(section.target)
    pred = head.predict(crops, allowed=names)
    selected = select_models(pred, names, cum, max_models)
    ft = embed(section.target)
    t_embed = time.time() - t0
    idx = {m: i for i, m in enumerate(pred.models)}
    cands, n = [], 0
    for m in selected:
        b, i = blocks[m], idx[m]
        for pt in knob_points(pred.mu[i], pred.sig[i]):
            vals = values_for(b, knob_map.get(m) or {}, pt["drive"], pt["tone"])
            engine.load(b.with_values(vals) if vals else b.preset)
            y = engine.render(section.dry)
            n += 1
            d = float(dist(ft, embed(y)))
            cands.append({"model": m, "label": pt["label"], "drive": pt["drive"], "tone": pt["tone"], "values": vals,
                          "distance": d if np.isfinite(d) else float("inf")})
    best = {}
    for c in cands:
        if c["model"] not in best or c["distance"] < best[c["model"]]["distance"]:
            best[c["model"]] = c
    ranked = sorted(({"model": m, "distance": c["distance"]} for m, c in best.items()), key=lambda r: r["distance"])
    rows = [{"model": r["model"], "values": best[r["model"]]["values"], "screen": r["distance"], "refined": None}
            for r in ranked]
    top = int(np.argmax(pred.p))
    own_m = pred.models[top]
    own = {"model": own_m, "p": float(pred.p[top]),
           "values": values_for(blocks[own_m], knob_map.get(own_m) or {}, _f(pred.mu[top, 0]), _f(pred.mu[top, 1])),
           "drive": _f(pred.mu[top, 0]), "tone": _f(pred.mu[top, 1])}
    shown = ", ".join("%s p=%.2f" % (r["model"], pred.p[idx[r["model"]]]) for r in ranked[:3])
    log(f"predictor-first: {len(selected)} models, {n} renders; AFx-Rep order: {shown}")
    return {"predicted": pred.top(8), "temperature": pred.temperature, "predictor_own": own, "selected": selected,
            "p_selected": {m: float(pred.p[idx[m]]) for m in selected},
            "sigma": {m: [_f(pred.sig[idx[m], 0]), _f(pred.sig[idx[m], 1])] for m in selected},
            "candidates": cands, "ranked": ranked, "rows": rows, "renders": n, "embed_s": t_embed,
            "wall_s": time.time() - t0, "top": [r["model"] for r in ranked], "models": [r["model"] for r in ranked]}
