"""Analysis for scripts/distortion_step4.py (T181). Measurement, not admission.

Condition A: per target, fit the three-stage twin to (DI, output), read its fingerprint with the dfp-v1
analyser, rank the 54 models by distance to their knob maps, invert Drive / Tone. Condition B: output-only
features against the render gallery on other excerpts, with and without the AFx-Rep shortlist. Baselines:
chance, mrstft_v2 nearest gallery render, AFx-Rep alone. Diagnostics: the twin's card against the truth
probe's card, and identification from the truth fingerprint (the knob-map interpolation alone).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import distortion_step4 as R

from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_step3 as S3
from tonematch.metrics import distortion_step4 as S4
from tonematch.metrics import drive_fp_mining as DM

SR = 48000
TWIN_DIR = Path(os.environ.get("STEP4_TWIN_DIR", str(R.CACHE / "twins")))
FAMILIES = ROOT / "data" / "drive-families-v1.json"
DRIVE_TOL, TONE_TOL = 0.15, 0.10
MASK_MARGIN_DB = 3.0


# ------------------------------------------------------------------------------------------- twins

def _init():
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"


def twin_job(tid: str, x: np.ndarray, y: np.ndarray) -> dict:
    t0 = time.perf_counter()
    tw = S4.fit_twin(x, y)
    t1 = time.perf_counter()
    arr, sc = S4.twin_fingerprint(tw)
    rv = S4.residual_vs_level(x, y, tw)
    TWIN_DIR.mkdir(parents=True, exist_ok=True)
    (TWIN_DIR / f"{tid}.npz").write_bytes(P.pack(arr, sc))
    meta = {"twin": tw.to_dict(), "residual_vs_level": rv, "fit_s": round(t1 - t0, 2),
            "analysis_s": round(time.perf_counter() - t1, 2)}
    (TWIN_DIR / f"{tid}.json").write_text(json.dumps(meta))
    return {"tid": tid, "fit_db": tw.fit_db, "fit_s": meta["fit_s"]}


def fit_all_twins(plan: dict, workers: int = 3, ids=None, force: bool = False, log=print) -> None:
    exc = {e["id"]: e for e in plan["excerpts"]}
    lead = int(R.LEAD_S * SR)
    di = {k: R.excerpt_audio(e)[lead:] for k, e in exc.items()}
    todo = [t for t in plan["targets"] if (ids is None or t["id"] in ids)
            and (force or not (TWIN_DIR / f"{t['id']}.json").exists())]
    if not todo:
        return
    with cf.ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"), initializer=_init) as ex:
        futs = [ex.submit(twin_job, t["id"], di[t["excerpt"]], np.load(R.TARGET_DIR / f"{t['id']}.npy"))
                for t in todo]
        for i, f in enumerate(cf.as_completed(futs)):
            r = f.result()
            log(json.dumps({"twin": i + 1, "of": len(todo), **r, "load1": round(os.getloadavg()[0], 2)}))


def load_twin(tid: str):
    arr, sc = P.unpack((TWIN_DIR / f"{tid}.npz").read_bytes())
    meta = json.loads((TWIN_DIR / f"{tid}.json").read_text())
    return arr, sc, meta


def load_truth(tid: str):
    return P.unpack((R.TRUTH_DIR / f"{tid}.npz").read_bytes())


# ------------------------------------------------------------------------------------------- labels

def families() -> tuple[dict[str, str], str]:
    """model -> family label; from cache/drive-maps/drive-families-v1.json (T180) when present, else the confusable
    groups of distortion-circuits.md (each "O" singleton its own)."""
    if FAMILIES.is_file():
        from tonematch.search.shortlist import load_families
        fam = load_families(FAMILIES)
        if fam:
            lab = {m: "|".join(sorted(v)) for m, v in fam.items()}
            return lab, "drive-families-v1"
    return {m: DM.LABELS[m][0] for m in DM.LABELS}, "confusable-groups"


# ------------------------------------------------------------------------------------------- metrics

def boot(vals_by_model: dict[str, list[float]], n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    ms = [m for m in vals_by_model if vals_by_model[m]]
    if not ms:
        return (float("nan"),) * 3
    allv = [v for m in ms for v in vals_by_model[m]]
    bs = []
    for _ in range(n):
        pick = rng.choice(len(ms), len(ms), replace=True)
        bs.append(np.mean([v for p in pick for v in vals_by_model[ms[p]]]))
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return float(np.mean(allv)), float(lo), float(hi)


def summarise(rows: list[dict], fam: dict[str, str]) -> dict:
    """rows: {tid, model, ranking: [models best first], knobs_true: [d, t?], truth: [d, t?],
    knobs_top: [..] or None}."""
    out = {"n": len(rows)}
    by = lambda key: {m: [r[key] for r in rows if r["model"] == m] for m in {r["model"] for r in rows}}
    for r in rows:
        rk = r["ranking"]
        r["rank"] = 1 + rk.index(r["model"])
        r["top1"] = float(r["rank"] == 1)
        r["top5"] = float(r["rank"] <= 5)
        r["fam_top1"] = float(fam.get(rk[0], rk[0]) == fam.get(r["model"], r["model"]))
        famm = [m for m in rk if fam.get(m, m) == fam.get(r["model"], r["model"])]
        r["in_family_top1"] = float(famm[0] == r["model"]) if famm else float("nan")
        r["family_size"] = len(famm)
        kt, tr = r.get("knobs_true"), r["truth"]
        if kt is not None:
            r["drive_err"] = float(abs(kt[0] - tr[0]))
            r["drive_ok"] = float(r["drive_err"] <= DRIVE_TOL + 1e-9)
            if len(tr) > 1:
                r["tone_err"] = float(abs(kt[1] - tr[1]))
                r["tone_ok"] = float(r["tone_err"] <= TONE_TOL + 1e-9)
                r["both_ok"] = float(r["drive_ok"] and r["tone_ok"])
            else:
                r["both_ok"] = r["drive_ok"]
        kp = r.get("knobs_top")
        if kp is not None and r["top1"]:
            ok = abs(kp[0] - tr[0]) <= DRIVE_TOL + 1e-9 and (len(tr) == 1 or abs(kp[1] - tr[1]) <= TONE_TOL + 1e-9)
            r["model_and_knobs_ok"] = float(ok)
        elif kp is not None:
            r["model_and_knobs_ok"] = 0.0
    for key in ("top1", "top5", "fam_top1", "in_family_top1", "drive_ok", "tone_ok", "both_ok", "model_and_knobs_ok"):
        vals = {m: [x for x in v if not np.isnan(x)] for m, v in
                {m: [r.get(key, float("nan")) for r in rows if r["model"] == m] for m in {r["model"] for r in rows}}.items()}
        if any(vals.values()):
            out[key] = boot(vals)
            out[f"{key}_n"] = sum(len(v) for v in vals.values())
    for key in ("drive_err", "tone_err"):
        v = [r[key] for r in rows if key in r]
        if v:
            out[f"median_{key}"] = float(np.median(v))
    out["median_rank"] = float(np.median([r["rank"] for r in rows]))
    del by
    return out


def chance(models: list[str], fam: dict[str, str]) -> dict:
    n = len(models)
    sizes = {}
    for m in models:
        sizes[fam.get(m, m)] = sizes.get(fam.get(m, m), 0) + 1
    fam_rate = float(np.mean([sizes[fam.get(m, m)] / n for m in models]))
    # random knob guess, uniform on [0, 1]: P(|g - t| <= tol) averaged over t uniform
    pk = lambda tol: 2 * tol - tol * tol
    return {"top1": 1 / n, "top5": 5 / n, "fam_top1": fam_rate, "drive_ok": pk(DRIVE_TOL), "tone_ok": pk(TONE_TOL),
            "both_ok": pk(DRIVE_TOL) * pk(TONE_TOL)}


# ------------------------------------------------------------------------------------------- condition A

def library_space(kind: str, dirs=("dfp-v1",), level: bool = False):
    recs = S4.load_library(ROOT, dirs)
    rows = [S4.feature_blocks(r["arr"], r["sc"], kind, level=level) for r in recs]
    fs = S4.FeatureSpace(rows)
    maps, raw = S4.build_maps(recs, fs)
    return recs, fs, maps, raw


def query_blocks(arr, sc, kind: str, level: bool, mask_db: float | None):
    return S4.feature_blocks(arr, sc, kind, level=level, max_level_dbfs=mask_db)


def condition_a_rows(plan, fs, maps, raw, kind, level=False, mask=True, source="twin", nearest=False) -> list[dict]:
    rows = []
    for t in plan["targets"]:
        if source == "twin":
            arr, sc, meta = load_twin(t["id"])
            mdb = meta["twin"]["u_peak_dbfs"] + MASK_MARGIN_DB if mask else None
        else:
            arr, sc = load_truth(t["id"])
            mdb = None
        z, w = fs.transform(query_blocks(arr, sc, kind, level, mdb))
        if nearest:
            res = sorted(((m, *S4.nearest_setting(raw[m][1], raw[m][0], z, w)) for m in maps), key=lambda r: r[1])
            kt = S4.nearest_setting(raw[t["model"]][1], raw[t["model"]][0], z, w)[1]
        else:
            res = S4.rank_models(maps, z, w)
            kt = maps[t["model"]].best(z, w)[1]
        truth = [t["cc"][t["drive"]] / 127.0] + ([t["cc"][t["tone"]] / 127.0] if t["tone"] else [])
        rows.append({"tid": t["id"], "model": t["model"], "excerpt": t["excerpt"],
                     "ranking": [r[0] for r in res], "dist": [r[1] for r in res[:10]],
                     "knobs_true": [float(v) for v in kt], "knobs_top": [float(v) for v in res[0][2]], "truth": truth})
    return rows


# ------------------------------------------------------------------------------------------- card errors

CARD_KEYS = ("knee_dbfs", "onset_dbfs", "even_share_db", "pre_hp_fit_hz", "post_lp_fit_hz", "mid_db", "g0_db")


def onset_at(stair_db, rel_db: float):
    o = P.harmonic_onset(P.STAIR_LEVELS_V1, stair_db, rel_db=rel_db)
    return o["onset_dbfs"], o["state"]


def card_errors(plan) -> dict:
    """The twin's card against the truth probe's card at the same (off-grid) setting."""
    per = {k: [] for k in CARD_KEYS + ("onset40_dbfs",)}
    states = {"knee": [0, 0], "onset": [0, 0], "onset40": [0, 0], "mid_kind": [0, 0]}
    for t in plan["targets"]:
        a1, s1, _ = load_twin(t["id"])
        a0, s0 = load_truth(t["id"])
        c1 = S3.card_values(s1, a1, P.STAIR_LEVELS_V1)
        c0 = S3.card_values(s0, a0, P.STAIR_LEVELS_V1)
        for k in CARD_KEYS:
            v0, v1 = c0[k], c1[k]
            if v0 is None or v1 is None or not (np.isfinite(v0) and np.isfinite(v1)):
                continue
            if k.endswith("_hz"):
                per[k].append(abs(np.log2(max(v1, 1.0) / max(v0, 1.0))))
            elif k == "even_share_db":
                per[k].append(abs(max(v1, -60.0) - max(v0, -60.0)))
            else:
                per[k].append(abs(v1 - v0))
        o0, st0 = onset_at(a0["stair1k_db"], -40.0)
        o1, st1 = onset_at(a1["stair1k_db"], -40.0)
        if o0 is not None and o1 is not None:
            per["onset40_dbfs"].append(abs(o1 - o0))
        states["onset40"][0] += st0 == st1
        states["onset40"][1] += 1
        states["knee"][0] += c0["knee_state"] == c1["knee_state"]
        states["knee"][1] += 1
        states["onset"][0] += c0["onset_state"] == c1["onset_state"]
        states["onset"][1] += 1
        states["mid_kind"][0] += c0["mid_kind"] == c1["mid_kind"]
        states["mid_kind"][1] += 1
    out = {}
    for k, v in per.items():
        if v:
            out[k] = {"n": len(v), "median": float(np.median(v)), "p75": float(np.percentile(v, 75)),
                      "unit": "octaves" if k.endswith("_hz") else "dB"}
    out["state_agreement"] = {k: v[0] / max(1, v[1]) for k, v in states.items()}
    return out


# ------------------------------------------------------------------------------------------- calibration (A)

def twin_query(t: dict, fs, kind: str, mask: bool = True):
    arr, sc, meta = load_twin(t["id"])
    mdb = meta["twin"]["u_peak_dbfs"] + MASK_MARGIN_DB if mask else None
    return fs.transform(S4.feature_blocks(arr, sc, kind, max_level_dbfs=mdb))


def truth_query(t: dict, fs, kind: str):
    a0, s0 = load_truth(t["id"])
    return fs.transform(S4.feature_blocks(a0, s0, kind))


def calibration(tw_z, tw_w, tr_z, ridge: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    """Per-dimension bias (mean twin - truth) and reliability 1 / (residual variance + ridge), from
    calibration targets (the dev split): what the twin gets systematically wrong, and how noisy it is."""
    Zt, Wt, Z0 = np.asarray(tw_z), np.asarray(tw_w), np.asarray(tr_z)
    D = Zt - Z0
    D[Wt <= 0] = np.nan
    with np.errstate(all="ignore"):
        b = np.nan_to_num(np.nanmean(D, axis=0))
        v = np.nanmean((D - b) ** 2, axis=0)
    v = np.where(np.isfinite(v), v, np.nanmax(v[np.isfinite(v)]) if np.isfinite(v).any() else 1.0)
    return b, 1.0 / (v + ridge)


def apply_cal(z, w, cal):
    if cal is None:
        return z, w
    b, rel = cal
    z = z - b * (w > 0)
    w = w * rel
    return z, w / max(w.sum(), 1e-30)


def rows_from_queries(targets, queries, maps, raw=None, nearest=False):
    rows = []
    for t, (z, w) in zip(targets, queries):
        if nearest:
            res = sorted(((m, *S4.nearest_setting(raw[m][1], raw[m][0], z, w)) for m in maps), key=lambda r: r[1])
            kt = S4.nearest_setting(raw[t["model"]][1], raw[t["model"]][0], z, w)[1]
        else:
            res = S4.rank_models(maps, z, w)
            kt = maps[t["model"]].best(z, w)[1]
        rows.append({"tid": t["id"], "model": t["model"], "excerpt": t["excerpt"], "ranking": [r[0] for r in res],
                     "dist": [float(r[1]) for r in res[:10]], "knobs_true": [float(v) for v in kt],
                     "knobs_top": [float(v) for v in res[0][2]], "truth": truth_pos(t)})
    return rows


def truth_pos(t):
    return [t["cc"][t["drive"]] / 127.0] + ([t["cc"][t["tone"]] / 127.0] if t["tone"] else [])


def condition_a(plan, dirs=("dfp-v1",), kind="curves", log=print) -> dict:
    """The dev-chosen fingerprint method (curves, masked above the excerpt peak, calibrated on dev) and its
    variants, plus the truth-fingerprint diagnostic."""
    tg = plan["targets"]
    dev = [t for t in tg if split_of(plan, t) == "dev"]
    test = [t for t in tg if split_of(plan, t) == "test"]
    out = {}
    for kd in (("curves", "card", "both") if dirs == ("dfp-v1",) else ("curves",)):
        _, fs, maps, raw = library_space(kd, dirs)
        q_tw = {t["id"]: twin_query(t, fs, kd) for t in tg}
        q_tr = {t["id"]: truth_query(t, fs, kd) for t in tg}
        # uncalibrated
        out[f"twin_{kd}"] = rows_from_queries(tg, [q_tw[t["id"]] for t in tg], maps)
        # calibrated: dev targets leave-one-out, test targets with the calibration of all dev targets
        cal_all = calibration([q_tw[t["id"]][0] for t in dev], [q_tw[t["id"]][1] for t in dev],
                              [q_tr[t["id"]][0] for t in dev])
        qs = []
        for t in tg:
            if t in dev:
                others = [d for d in dev if d["id"] != t["id"]]
                cal = calibration([q_tw[d["id"]][0] for d in others], [q_tw[d["id"]][1] for d in others],
                                  [q_tr[d["id"]][0] for d in others])
            else:
                cal = cal_all
            qs.append(apply_cal(*q_tw[t["id"]], cal))
        out[f"twin_{kd}_cal"] = rows_from_queries(tg, qs, maps)
        out[f"truth_{kd}"] = rows_from_queries(tg, [q_tr[t["id"]] for t in tg], maps)
        out[f"truth_{kd}_nearest"] = rows_from_queries(tg, [q_tr[t["id"]] for t in tg], maps, raw, nearest=True)
        if kd == "curves" and dirs == ("dfp-v1",):
            save_calibration(fs, cal_all, kd)
        log(json.dumps({"condition_a": kd, "dirs": dirs, "done": True}))
    del test
    return out


CAL_PATH = R.CACHE / "calibration.npz"


def save_calibration(fs, cal, kind):
    b, rel = cal
    np.savez(CAL_PATH, bias=b, rel=rel, names=np.array(fs.names), sizes=np.array(fs.sizes), kind=kind,
             mask_margin_db=MASK_MARGIN_DB)


def split_of(plan, t) -> str:
    return next(e["split"] for e in plan["excerpts"] if e["id"] == t["excerpt"])


# ------------------------------------------------------------------------------------------- gallery

def load_gallery(plan) -> list[dict]:
    rows = []
    _, sem = R.pool_models()
    for f in sorted(R.GALLERY_DIR.glob("part*.json")):
        rows.extend(json.loads(f.read_text()))
    seen, out = set(), []
    for r in rows:
        key = (r["excerpt"], r["model"], r["setting"])
        if key in seen:
            continue
        seen.add(key)
        s = sem[r["model"]]
        r["pos"] = [r["cc"][s["drive"]] / 127.0] + ([r["cc"][s["tone"]] / 127.0] if s["tone"] else [])
        r["afx"] = np.load(R.GALLERY_DIR / f"{r['excerpt']}__{r['model']}__{r['setting'].replace(',', '_')}.afx.npy")
        out.append(r)
    return out


def target_features(plan, log=print) -> dict:
    """AFx-Rep embedding and output-only features of every target, and the clean DI features of every
    excerpt (the reference for condition B). Cached."""
    p = R.CACHE / "target_features.json"
    if p.exists():
        d = json.loads(p.read_text())
        for v in d["targets"].values():
            v["afx"] = np.asarray(v["afx"])
        return d
    from tonematch.metrics import afxrep
    emb = afxrep.get_embedder()
    out = {"targets": {}, "di": {}}
    lead = int(R.LEAD_S * SR)
    for e in plan["excerpts"]:
        x = R.excerpt_audio(e)[lead:]
        out["di"][e["id"]] = {k: np.asarray(v, float).tolist() for k, v in S4.blind_features(x, None).items()}
    for t in plan["targets"]:
        y = np.load(R.TARGET_DIR / f"{t['id']}.npy")
        out["targets"][t["id"]] = {"afx": emb.embed(y).mid.astype(float).tolist(),
                                   "blind": {k: np.asarray(v, float).tolist() for k, v in S4.blind_features(y, None).items()}}
    p.write_text(json.dumps(out))
    log(json.dumps({"target_features": len(out["targets"])}))
    for v in out["targets"].values():
        v["afx"] = np.asarray(v["afx"])
    return out


def _grid_rank(per_setting: dict[tuple[str, str], float], pos: dict, models) -> tuple[list, dict]:
    """per (model, setting) distance -> ranking of models by their nearest setting, and that setting's pos."""
    best = {}
    for (m, s), d in per_setting.items():
        if m not in best or d < best[m][0]:
            best[m] = (d, pos[(m, s)])
    rk = sorted((m for m in models if m in best), key=lambda m: best[m][0])
    return rk, best


def baseline_rows(plan, gal, tf, metric: str, cond: str, players: str = "all") -> list[dict]:
    """mrstft_v2 / AFx-Rep nearest gallery render. cond A: the target's own excerpt; cond B: mrstft on the
    partner excerpt, AFx-Rep averaged over the other excerpts (`players`: all others or other players)."""
    exc = {e["id"]: e for e in plan["excerpts"]}
    pos = {(r["model"], r["setting"]): r["pos"] for r in gal}
    models = plan["models"]
    by_exc = {}
    for r in gal:
        by_exc.setdefault(r["excerpt"], []).append(r)
    rows = []
    for t in plan["targets"]:
        e = t["excerpt"]
        if metric == "mrstft":
            g = e if cond == "A" else R.PARTNER[e]
            key = "mr_a" if cond == "A" else "mr_b"
            per = {(r["model"], r["setting"]): r[key][t["id"]] for r in by_exc[g] if t["id"] in r[key]}
        else:
            ta = tf["targets"][t["id"]]["afx"]
            gs = [e] if cond == "A" else gallery_excerpts(exc, e, players)
            acc = {}
            for g in gs:
                for r in by_exc[g]:
                    acc.setdefault((r["model"], r["setting"]), []).append(max(0.0, 1.0 - float(np.dot(ta, r["afx"]))))
            per = {k: float(np.mean(v)) for k, v in acc.items()}
        rk, best = _grid_rank(per, pos, models)
        rows.append({"tid": t["id"], "model": t["model"], "excerpt": e, "ranking": rk,
                     "dist": [best[m][0] for m in rk[:10]], "knobs_true": list(best[t["model"]][1]),
                     "knobs_top": list(best[rk[0]][1]), "truth": truth_pos(t), "afx_model_dist": {m: best[m][0] for m in rk}})
    return rows


# ------------------------------------------------------------------------------------------- condition B

def gallery_excerpts(exc: dict, e: str, players: str) -> list[str]:
    """The excerpts a condition-B target may be compared on: never its own. "all": every other excerpt;
    "other": other players only (the song case: the guitar is unknown); "same": the same player only (the
    Gate 4 case: a separate dry sample of the player's own guitar, other notes)."""
    gs = [g for g in exc if g != e]
    if players == "other":
        gs = [g for g in gs if exc[g]["player"] != exc[e]["player"]]
    elif players == "same":
        gs = [g for g in gs if exc[g]["player"] == exc[e]["player"]]
    return gs


def blind_rel(feat: dict, ref: dict) -> dict:
    """Output-only features relative to a clean-guitar reference (feature minus the reference's)."""
    out = {}
    for k in S4.BLIND_BLOCKS:
        a, b = np.asarray(feat[k], float), np.asarray(ref[k], float)
        out[k] = a - b
    return out


def mean_feat(fs_: list[dict]) -> dict:
    out = {}
    for k in S4.BLIND_BLOCKS:
        with np.errstate(all="ignore"):
            out[k] = np.nanmean(np.stack([np.asarray(f[k], float) for f in fs_]), axis=0)
    return out


def condition_b(plan, gal, tf, mode: str = "rel", players: str = "all", shortlist=(5, 8), log=print) -> dict:
    """Output-only fingerprint features against the gallery on other excerpts; knob maps over the 12 grid
    settings in that feature space. mode "rel": every feature relative to clean guitar (gallery: the render
    minus its own DI; target: minus the mean of the other excerpts' DIs); "raw": as measured, except the
    post-EQ, which is always relative."""
    exc = {e["id"]: e for e in plan["excerpts"]}
    dif = tf["di"]
    afx_rows = {r["tid"]: r for r in baseline_rows(plan, gal, tf, "afx", "B", players)}
    by = {}
    for r in gal:
        by.setdefault((r["model"], r["setting"]), []).append(r)
    out = {f"fp_{mode}_{players}": []}
    for k in shortlist:
        out[f"afx{k}+fp_{mode}_{players}"] = []
    spaces = {}
    for t in plan["targets"]:
        e = t["excerpt"]
        gs = tuple(gallery_excerpts(exc, e, players))
        if gs not in spaces:
            keys = sorted(by)
            feats = []
            for key in keys:
                fl = []
                for r in by[key]:
                    if r["excerpt"] in gs:
                        f = {kk: np.asarray(v, float) for kk, v in r["blind"].items()}
                        fl.append(blind_rel(f, dif[r["excerpt"]]) if mode == "rel" else
                                  {**f, "post_eq": np.asarray(f["post_eq"]) - np.asarray(dif[r["excerpt"]]["post_eq"])})
                feats.append(S4.blind_vector(mean_feat(fl)))
            fs = S4.FeatureSpace(feats)
            recs = [{"model": k[0], "pos": by[k][0]["pos"]} for k in keys]
            maps, raw = S4.build_maps(recs, fs)
            spaces[gs] = (fs, maps, raw)
        fs, maps, raw = spaces[gs]
        ref = mean_feat([{kk: np.asarray(v, float) for kk, v in dif[g].items()} for g in gs])
        f = {kk: np.asarray(v, float) for kk, v in tf["targets"][t["id"]]["blind"].items()}
        q = blind_rel(f, ref) if mode == "rel" else {**f, "post_eq": f["post_eq"] - ref["post_eq"]}
        z, w = fs.transform(S4.blind_vector(q))
        res = S4.rank_models(maps, z, w)
        kt = maps[t["model"]].best(z, w)[1]
        base = {"tid": t["id"], "model": t["model"], "excerpt": e, "truth": truth_pos(t),
                "knobs_true": [float(v) for v in kt]}
        fpd = {m: (d, p) for m, d, p in res}
        out[f"fp_{mode}_{players}"].append({**base, "ranking": [r[0] for r in res],
                                            "knobs_top": [float(v) for v in res[0][2]]})
        arow = afx_rows[t["id"]]
        for k in shortlist:
            sl = arow["ranking"][:k]
            rr = sorted(sl, key=lambda m: fpd[m][0]) + arow["ranking"][k:]
            out[f"afx{k}+fp_{mode}_{players}"].append({**base, "ranking": rr,
                                                      "knobs_top": [float(v) for v in fpd[rr[0]][1]]})
    log(json.dumps({"condition_b": mode, "players": players}))
    return out


# ------------------------------------------------------------------------------------------- driver

def table(plan, rows_by_method: dict, fam) -> dict:
    out = {}
    for name, rows in rows_by_method.items():
        out[name] = {}
        for sp in ("dev", "test", "all"):
            rs = [dict(r) for r in rows if sp == "all" or split_of(plan, {"excerpt": r["excerpt"]}) == sp]
            if rs:
                out[name][sp] = summarise(rs, fam)
        test = [dict(r) for r in rows if split_of(plan, {"excerpt": r["excerpt"]}) == "test"]
        summarise(test, fam)
        by_src = {}
        for r in test:
            src = "human" if next(e["player"] for e in plan["excerpts"] if e["id"] == r["excerpt"]) == "human" else "guitar-techs"
            by_src.setdefault(src, []).append(r)
        out[name]["test_by_source"] = {k: {kk: (v[0] if isinstance(v, tuple) else v) for kk, v in summarise(rs, fam).items()
                                           if kk in ("top1", "top5", "fam_top1", "drive_ok", "tone_ok", "n")}
                                       for k, rs in by_src.items()}
    return out


def tone_ends_complete() -> tuple[bool, int]:
    p = ROOT / "cache" / "distortion-fp" / "dfp-v1-tone-ends" / "index.json"
    if not p.is_file():
        return False, 0
    idx = json.loads(p.read_text())
    n = sum(1 for s in idx.get("settings", []) if "file" in s and not s.get("tag") and s["model"] not in S4.EXCLUDED)
    models = {s["model"] for s in idx.get("settings", []) if "file" in s and s["model"] not in S4.EXCLUDED}
    return len(models) >= 54 - 6, n            # one-knob models have no tone ends


def run(a) -> dict:
    t0 = time.perf_counter()
    plan = R.load_plan()
    log = lambda s: print(s, flush=True)
    out = Path(a.out)
    only_b = getattr(a, "only", "") == "B" and (out / "rows.json").exists()
    fit_all_twins(plan, workers=a.workers, log=log)
    fam, fam_src = families()
    res = {"plan": {"targets": len(plan["targets"]), "models": len(plan["models"]),
                    "excerpts": [{k: e[k] for k in ("id", "group", "player", "split", "peak_dbfs", "rms_dbfs", "path", "start")}
                                 for e in plan["excerpts"]],
                    "excluded": plan["excluded_models"]},
           "families": fam_src, "chance": chance(plan["models"], fam)}
    te_ok, te_n = tone_ends_complete()
    res["tone_ends"] = {"complete": te_ok, "settings": te_n}
    gal = load_gallery(plan)
    tf = target_features(plan, log=log)
    if only_b:
        A = json.loads((out / "rows.json").read_text())["A"]
    else:
        A = condition_a(plan, log=log)
        if te_ok:
            A2 = condition_a(plan, dirs=("dfp-v1", "dfp-v1-tone-ends"), log=log)
            A.update({f"{k}+toneends": v for k, v in A2.items()})
        A["mrstft_v2"] = baseline_rows(plan, gal, tf, "mrstft", "A")
        A["afxrep"] = baseline_rows(plan, gal, tf, "afx", "A")
    B = {"mrstft_v2": baseline_rows(plan, gal, tf, "mrstft", "B"),
         "afxrep_all": baseline_rows(plan, gal, tf, "afx", "B", "all"),
         "afxrep_other": baseline_rows(plan, gal, tf, "afx", "B", "other"),
         "afxrep_same": baseline_rows(plan, gal, tf, "afx", "B", "same")}
    for mode in ("rel", "raw"):
        for players in ("all", "other", "same"):
            B.update(condition_b(plan, gal, tf, mode, players, log=log))
    for d in (A, B):
        for rows in d.values():
            for r in rows:
                r.pop("afx_model_dist", None)
    res["A"] = table(plan, A, fam)
    res["B"] = table(plan, B, fam)
    res["card_errors"] = {sp: card_errors({"targets": [t for t in plan["targets"] if sp == "all" or split_of(plan, t) == sp]})
                          for sp in ("dev", "test", "all")}
    res["twin_fit"] = twin_fit_summary(plan)
    res["renders"] = render_summary()
    res["gallery_rows"] = len(gal)
    res["wall_s"] = round(time.perf_counter() - t0, 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(P._clean(res), indent=1, default=float))
    (out / "rows.json").write_text(json.dumps({"A": A, "B": B}, default=float))
    return {k: res[k] for k in ("families", "tone_ends", "wall_s")}


def twin_fit_summary(plan) -> dict:
    fits = [load_twin(t["id"])[2] for t in plan["targets"]]
    f = np.array([m["twin"]["fit_db"] for m in fits])
    return {"median_fit_db": float(np.median(f)), "p25": float(np.percentile(f, 25)), "p75": float(np.percentile(f, 75)),
            "share_better_than_minus20": float(np.mean(f < -20)), "median_fit_s": float(np.median([m["fit_s"] for m in fits])),
            "median_analysis_s": float(np.median([m["analysis_s"] for m in fits])),
            "pre_highpass_chosen": int(sum(m["twin"]["pre_fc"] is not None for m in fits))}


def render_summary() -> dict:
    hist = json.loads((R.CACHE / "runs.json").read_text()) if (R.CACHE / "runs.json").exists() else []
    return {"runs": hist, "kept": {"targets": len(list(R.TARGET_DIR.glob("*.npy"))),
                                   "truth": len(list(R.TRUTH_DIR.glob("*.npz"))),
                                   "gallery": len(list(R.GALLERY_DIR.glob("*.afx.npy")))}}


# ------------------------------------------------------------------------------------------- ledger

def ledger(a) -> int:
    res = json.loads((Path(a.out) / "summary.json").read_text())
    rn = res["renders"]["kept"]
    n_r = rn["targets"] + rn["truth"] + rn["gallery"]
    wall = sum(r.get("wall_s", 0) for r in res["renders"]["runs"])
    base = ("T181 step 4, measurement not admission (plan in DECISIONS before computing); 54 drive models (6 'not "
            "meaningful at quiet input' left out), 120 targets at random off-grid Drive/Tone on 8 real-DI excerpts "
            "(4 Guitar-TECHS P1/P2, 4 human takes), dev 30 / test 90 by excerpt; families: " + res["families"] + "; ")
    rows = []
    pick = {"dryknown": [("twin_curves_cal", "fingerprint (twin, curves, dev-calibrated)"),
                         ("twin_curves", "fingerprint (twin, curves, uncalibrated)"),
                         ("twin_card", "fingerprint (twin, card)"),
                         ("truth_curves", "diagnostic: probe at the hidden setting (upper bound)"),
                         ("mrstft_v2", "baseline mrstft_v2 nearest grid render, same DI"),
                         ("afxrep", "baseline AFx-Rep nearest grid render, same DI")],
            "blind": [("fp_rel_all", "output-only fingerprint features"),
                      ("afx8+fp_rel_all", "AFx-Rep shortlist of 8 then output-only features"),
                      ("afx5+fp_rel_all", "AFx-Rep shortlist of 5 then output-only features"),
                      ("afxrep_all", "baseline AFx-Rep alone, other excerpts"),
                      ("afxrep_other", "baseline AFx-Rep alone, other players' excerpts only (song case)"),
                      ("afxrep_same", "AFx-Rep alone, same player's other excerpts only (Gate 4 case; variant added after "
                                      "registration)"),
                      ("fp_rel_same", "output-only fingerprint features, same player's other excerpts only (Gate 4 case; "
                                      "variant added after registration)"),
                      ("mrstft_v2", "baseline mrstft_v2, partner excerpt")]}
    for cond, items in pick.items():
        tab = res["A" if cond == "dryknown" else "B"]
        for key, label in items:
            if key not in tab or "test" not in tab[key]:
                continue
            s = tab[key]["test"]
            for m in ("top1", "top5", "fam_top1", "drive_ok", "tone_ok"):
                if m not in s:
                    continue
                v, lo, hi = s[m]
                rows.append((cond, f"{key}:{m}", v, s.get(f"{m}_n", s["n"]),
                             base + f"{label}; test split; 95% CI over models {lo:.2f}-{hi:.2f}; "
                             f"chance {res['chance'].get(m, float('nan')):.3f}; knob tolerances Drive 0.15 / Tone 0.10 "
                             "(knobs on the true model's map)"))
    for i, (cond, metric, value, n, notes) in enumerate(rows):
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "native",
               "--dataset", f"distortion-step4-{cond}", "--metric", metric, "--value", str(round(float(value), 6)),
               "--notes", notes]
        if i == 0:
            cmd += ["--renders", str(n_r), "--wall-clock-s", str(round(wall, 1))]
        subprocess.run(cmd, check=True, env={**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")})
    print(f"{len(rows)} ledger rows")
    return 0
