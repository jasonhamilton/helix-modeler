#!/usr/bin/env python3
"""T231: blind reverb estimator (human 2026-10-25 after T230: "how can you not detect reverb? try harder").
LOCAL ONLY: song audio stays under cache/ and reports/local/. Measurement only; nothing is adopted.

Estimators: src/tonematch/metrics/reverb_blind.py (sdd, ml, adc). Validation on known truth first (the 189 T230 reverb
candidates, the pre-reverb c renders, the rebuilt dry, the T229 stems, the T228 controls), then, only if that works, the
full-length separated stems.

    .venv/bin/python scripts/reverb_estimator_t231.py synth              # noise bursts with a known RT60 and DRR
    nice -n 10 .venv/bin/python scripts/reverb_estimator_t231.py validate   # 10 s material with known truth (0 renders)
    nice -n 10 .venv/bin/python scripts/reverb_estimator_t231.py songs      # full stems + full mixes, bootstrap CIs
    .venv/bin/python scripts/reverb_estimator_t231.py compare            # post hoc: estimates vs the human's T230 ranks
    .venv/bin/python scripts/reverb_estimator_t231.py ledger

Outputs: cache/reverb-estimator-t231/{synth,validate,songs,compare}.json and .txt.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
warnings.filterwarnings("ignore", category=RuntimeWarning)

from tonematch.metrics import reverb_blind as RB  # noqa: E402

OUT = ROOT / "cache" / "reverb-estimator-t231"
T230 = ROOT / "cache" / "reverb-pilot-t230"
T229 = ROOT / "cache" / "colour-t229"
T228 = ROOT / "cache" / "colour-pilot-t228"
STEMS = ROOT / "cache" / "stems"
KEY = ROOT / "cache" / "ratings" / "reverb-pilot-v1.key.json"
ANSWERS = ROOT / "reports" / "local" / "ratings" / "reverb-pilot-v1.answers.json"
SONGS = ("mother", "seek-destroy", "teen-spirit", "territorial", "satisfaction", "cherub-rock", "ds-sample")
T228_SONGS = ("mother", "seek-destroy", "teen-spirit")
TYPES = ("room", "plate", "hall")
MIXES = (0.25, 0.35, 0.50)
LEVELS = ("short", "medium", "long")
TARGET_S = {"short": 1.0518, "medium": 1.4814, "long": 2.0866}          # T230 calibration (key.json)
T229_RT = {"t229-r35": 0.7978, "t229-p35": 1.6267}                       # Room 0.40 / Plate 0.40 from calibration.json
T228_CTL = {"ctl_reverb_cap": ("room", 0.50, 0.50), "ctl_reverb_weak": ("room", 0.50, 0.35)}   # Room Decay 0.50, Mix
T228_CTL_RT = 0.95                                                       # Room Decay 0.50: between 0.80 (0.40) and 1.02 (0.55)
READINGS = ("sdd_p10_s", "sdd_peak_s", "ml_p10_s", "ml_peak_s", "adc_rt60_s")
# post hoc distance scales (set before the comparison, from the synthetic validation's own spread, see note)
SCALE_LOG_RT = 0.30
SCALE_EARLY_DB = 5.0


def log(m: str) -> None:
    print(m, flush=True)


def wav(p: Path) -> np.ndarray:
    return sf.read(str(p), dtype="float32")[0]


def song_hash(song: str) -> str:
    want = {"mother": "Mother_0-68s", "seek-destroy": "Seek", "teen-spirit": "Teen Spirit", "territorial": "Territorial",
            "satisfaction": "Satisfaction", "cherub-rock": "Cherub", "ds-sample": "DS-Sample"}[song]
    for d in sorted(STEMS.iterdir()):
        m = d / "htdemucs_6s" / "manifest.json"
        if m.exists() and want in json.load(open(m))["source"]:
            return d.name
    raise FileNotFoundError(song)


def window(song: str) -> tuple[float, float]:
    r = json.load(open(ROOT / "cache" / "matches" / f"{song}-finetune-prs7" / "result.json"))["window"]
    return float(r["start_s"]), float(r["end_s"])


def est_dict(x: np.ndarray, curve: bool = False) -> dict:
    return RB.estimate(x, keep_curve=curve).to_dict()


def _nan(v) -> float:
    return float(v) if v is not None and math.isfinite(float(v)) else float("nan")


def dump(name: str, obj, text: str | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=1, default=lambda o: float(o) if isinstance(o, np.floating) else str(o)))
    if text is not None:
        (OUT / f"{name}.txt").write_text(text)
        log(text)


# ------------------------------------------------------------------------------------------ synth

def cmd_synth(_a) -> None:
    rows = []
    for rt in (0.5, 1.0, 2.0):
        for drr in (10.0, 20.0, 30.0):
            for seed in (1, 2):
                e = RB.estimate(RB.synth_reverb_burst(rt, drr, seed=seed))
                rows.append({"rt60_s": rt, "drr_db": drr, "seed": seed, **{k: e.to_dict()[k] for k in READINGS},
                             "adc_early_db": e.adc_early_db, "sdd_n": e.sdd_n, "adc_events": e.adc_events})
    lines = ["Synthetic noise bursts (held band noise, cut, with an exponentially decaying tail): true RT60 / DRR vs readings",
             f"{'RT':>4} {'DRR':>4} seed | " + " ".join(f"{k[:-2]:>9}" for k in READINGS) + " | early_db  events sdd_n"]
    for r in rows:
        lines.append(f"{r['rt60_s']:4.1f} {r['drr_db']:4.0f} {r['seed']:4d} | " + " ".join(f"{r[k]:9.2f}" for k in READINGS)
                     + f" | {r['adc_early_db']:8.1f} {r['adc_events']:7d} {r['sdd_n']:5d}")
    for k in READINGS:
        err = [abs(r[k] / r["rt60_s"] - 1) for r in rows if math.isfinite(r[k])]
        lines.append(f"{k}: finite {len(err)}/{len(rows)}, median |rel err| {np.median(err) * 100 if err else float('nan'):.0f}%, "
                     f"max {max(err) * 100 if err else float('nan'):.0f}%")
    dump("synth", rows, "\n".join(lines))


# ------------------------------------------------------------------------------------------ validate

def validate_items(song: str) -> list[tuple[str, Path, dict]]:
    items = []
    items.append(("c", T229 / song / "audio" / "c.wav", {"kind": "pre-reverb", "type": None, "mix": 0.0, "rt": None}))
    items.append(("dry", T229 / song / "audio" / "dry.wav", {"kind": "dry", "type": None, "mix": 0.0, "rt": None}))
    items.append(("stem", T229 / song / "audio" / "stem.wav", {"kind": "stem", "type": None, "mix": None, "rt": None}))
    for t in TYPES:
        for m in MIXES:
            for lv in LEVELS:
                k = f"{t}-m{int(round(m * 100))}-{lv}"
                items.append((k, T230 / song / "audio" / f"{k}.wav", {"kind": "grid", "type": t, "mix": m, "rt": TARGET_S[lv], "level": lv}))
    items.append(("t229-r35", T229 / song / "audio" / "r35.wav", {"kind": "t229", "type": "room", "mix": 0.35, "rt": T229_RT["t229-r35"]}))
    items.append(("t229-p35", T229 / song / "audio" / "p35.wav", {"kind": "t229", "type": "plate", "mix": 0.35, "rt": T229_RT["t229-p35"]}))
    if song in T228_SONGS:
        for k, (t, _dec, m) in T228_CTL.items():
            items.append((k, T228 / song / "audio" / f"{k}.wav", {"kind": "control-on-stem", "type": t, "mix": m, "rt": T228_CTL_RT}))
    return items


def cmd_validate(a) -> None:
    t0 = time.time()
    res = {}
    for song in a.songs:
        res[song] = {}
        for k, p, truth in validate_items(song):
            if not p.exists():
                log(f"  missing {p}")
                continue
            e = est_dict(wav(p), curve=(k in ("c", "stem") or k.endswith("-m50-long")))
            res[song][k] = {"truth": truth, **e}
        log(f"{song}: {len(res[song])} items, {time.time() - t0:.0f} s")
    dump("validate", res, validate_text(res))


def _rho(x, y) -> float:
    from scipy.stats import spearmanr

    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or np.std(x[ok]) == 0 or np.std(y[ok]) == 0:
        return float("nan")
    return float(spearmanr(x[ok], y[ok]).correlation)


def validate_text(res: dict) -> str:
    L = ["Synthetic validation on known truth (10 s renders; 0 new renders). Decay targets short 1.05 / medium 1.48 / long 2.09 s.", ""]
    songs = list(res)
    # 1. decay: per song and reading, over the 27 grid items: Spearman(est, target RT), triples ordered right, MAE
    L.append("1. DECAY LENGTH, grid of 27 per song (Room/Plate/Hall x Mix x Decay)")
    L.append(f"{'song':13s} " + " ".join(f"{k[:-2]:>22s}" for k in READINGS))
    L.append(f"{'':13s} " + " ".join(f"{'rho / trip / MAE / n':>22s}" for _ in READINGS))
    summary = {k: {"rho": [], "trip": [], "mae": [], "fin": []} for k in READINGS}
    for s in songs:
        g = {k: v for k, v in res[s].items() if v["truth"]["kind"] == "grid"}
        cells = []
        for k in READINGS:
            est = np.array([_nan(v[k]) for v in g.values()])
            tru = np.array([v["truth"]["rt"] for v in g.values()])
            rho = _rho(est, tru)
            # triples: for each (type, mix) are the three levels ordered short < medium < long?
            trip_ok, trip_n = 0, 0
            for t in TYPES:
                for m in MIXES:
                    vals = [_nan(g[f"{t}-m{int(round(m * 100))}-{lv}"][k]) for lv in LEVELS]
                    if all(math.isfinite(v) for v in vals):
                        trip_n += 1
                        trip_ok += int(vals[0] < vals[1] < vals[2])
            ok = np.isfinite(est)
            mae = float(np.mean(np.abs(est[ok] - tru[ok]))) if ok.any() else float("nan")
            cells.append(f"{rho:5.2f} / {trip_ok}/{trip_n} / {mae:4.2f} / {ok.sum():2d}")
            summary[k]["rho"].append(rho); summary[k]["trip"].append((trip_ok, trip_n)); summary[k]["mae"].append(mae); summary[k]["fin"].append(int(ok.sum()))
        L.append(f"{s:13s} " + " ".join(f"{c:>22s}" for c in cells))
    cells = []
    for k in READINGS:
        r = np.array(summary[k]["rho"]); r = r[np.isfinite(r)]
        to = sum(a for a, _ in summary[k]["trip"]); tn = sum(b for _, b in summary[k]["trip"])
        m = np.array(summary[k]["mae"]); m = m[np.isfinite(m)]
        cells.append(f"{np.mean(r) if r.size else float('nan'):5.2f} / {to}/{tn} / {np.mean(m) if m.size else float('nan'):4.2f} / {sum(summary[k]['fin']):3d}")
    L.append(f"{'mean/total':13s} " + " ".join(f"{c:>22s}" for c in cells))
    L.append("(rho = Spearman of the reading against the target RT60 over the finite grid items; trip = (type, Mix) triples whose three")
    L.append(" decays come out in the right order, short < medium < long; MAE in seconds; n = finite readings of 27)")
    L.append("")
    # 2. the pre-reverb and dry: what do the readings say with no reverb?
    L.append("2. NO REVERB: the readings on the pre-reverb render c, the rebuilt dry, and the separated stem (unknown)")
    L.append(f"{'song':13s} {'item':6s} " + " ".join(f"{k[:-2]:>9s}" for k in READINGS) + "   early_db events")
    for s in songs:
        for it in ("c", "dry", "stem"):
            v = res[s].get(it)
            if v:
                L.append(f"{s:13s} {it:6s} " + " ".join(f"{_nan(v[k]):9.2f}" for k in READINGS) + f"   {_nan(v['adc_early_db']):8.1f} {v['adc_events']:6d}")
    L.append("")
    # 3. mix: early_db vs Mix within each (type, decay) triple; and vs decay within (type, mix)
    L.append("3. MIX LEVEL: adc early tail level (dB re the held level; higher = more reverb) by Mix, per song (mean over type and decay)")
    L.append(f"{'song':13s} {'Mix 0.25':>9s} {'0.35':>9s} {'0.50':>9s}  rho(early, Mix)  triples rising  | c  dry  stem")
    mono_all = [0, 0]
    for s in songs:
        g = {k: v for k, v in res[s].items() if v["truth"]["kind"] == "grid"}
        by = {m: [_nan(v["adc_early_db"]) for v in g.values() if v["truth"]["mix"] == m] for m in MIXES}
        rho = _rho([_nan(v["adc_early_db"]) for v in g.values()], [v["truth"]["mix"] for v in g.values()])
        ok = n = 0
        for t in TYPES:
            for lv in LEVELS:
                vals = [_nan(g[f"{t}-m{int(round(m * 100))}-{lv}"]["adc_early_db"]) for m in MIXES]
                if all(math.isfinite(v) for v in vals):
                    n += 1; ok += int(vals[0] < vals[1] < vals[2])
        mono_all[0] += ok; mono_all[1] += n
        others = "  ".join(f"{_nan(res[s][it]['adc_early_db']) if it in res[s] else float('nan'):5.1f}" for it in ("c", "dry", "stem"))
        L.append(f"{s:13s} " + " ".join(f"{np.nanmean(by[m]) if np.isfinite(by[m]).any() else float('nan'):9.1f}" for m in MIXES)
                 + f"  {rho:15.2f}  {ok:2d}/{n:<2d}           | {others}")
    L.append(f"triples (type, decay) where early_db rises with Mix 0.25 < 0.35 < 0.50: {mono_all[0]}/{mono_all[1]}")
    L.append("")
    # 4. type: features by type, leave-one-song-out nearest centroid
    L.append("4. TYPE (Room / Plate / Hall): candidate features by type (mean over songs, Mix and decay) and a leave-one-song-out nearest-centroid test")
    feats = ("rt_ratio_low_high", "early_tilt_db", "adc_early_db", "sdd_peak_s")
    rowsf = []
    for s in songs:
        for k, v in res[s].items():
            if v["truth"]["kind"] == "grid":
                rowsf.append((s, v["truth"]["type"], v["truth"]["rt"], v["truth"]["mix"], [_nan(v[f]) for f in feats]))
    for f_i, f in enumerate(feats):
        by = {t: [r[4][f_i] for r in rowsf if r[1] == t] for t in TYPES}
        L.append(f"  {f:20s} " + "  ".join(f"{t}: {np.nanmean(by[t]) if np.isfinite(by[t]).any() else float('nan'):6.2f} (finite {int(np.isfinite(by[t]).sum())}/{len(by[t])})" for t in TYPES))
    # LOSO on standardised finite features; "decay-and-mix-matched" so the test is about type: use all features as given
    X = np.array([r[4] for r in rowsf]); y = np.array([r[1] for r in rowsf]); sg = np.array([r[0] for r in rowsf])
    use = np.isfinite(X).all(axis=1)
    acc = []
    if use.sum() >= 10:
        Xs = (X[use] - np.nanmean(X[use], 0)) / (np.nanstd(X[use], 0) + 1e-9)
        ys, ss = y[use], sg[use]
        for s in np.unique(ss):
            tr, te = ss != s, ss == s
            cents = {t: Xs[tr][ys[tr] == t].mean(0) for t in TYPES if (ys[tr] == t).any()}
            for xi, yi in zip(Xs[te], ys[te], strict=True):
                pred = min(cents, key=lambda t: np.linalg.norm(xi - cents[t]))
                acc.append(int(pred == yi))
    L.append(f"  leave-one-song-out nearest centroid on {use.sum()} items with all four features finite: accuracy "
             f"{np.mean(acc) if acc else float('nan'):.2f} (chance 0.33)")
    L.append("")
    # 5. controls on the real stem (T228)
    L.append("5. KNOWN REVERB ON THE REAL STEM (T228 controls: Room Decay 0.50 = about 0.95 s, Mix 0.50 'cap' and 0.35 'weak') vs the plain stem")
    L.append(f"{'song':13s} {'item':17s} " + " ".join(f"{k[:-2]:>9s}" for k in READINGS) + "   early_db events")
    for s in T228_SONGS:
        if s not in res:
            continue
        for it in ("stem", "ctl_reverb_weak", "ctl_reverb_cap"):
            v = res[s].get(it)
            if v:
                L.append(f"{s:13s} {it:17s} " + " ".join(f"{_nan(v[k]):9.2f}" for k in READINGS) + f"   {_nan(v['adc_early_db']):8.1f} {v['adc_events']:6d}")
    return "\n".join(L)


# ------------------------------------------------------------------------------------------ songs

def cmd_songs(a) -> None:
    t0 = time.time()
    res = {}
    for song in a.songs:
        h = song_hash(song)
        d = STEMS / h / "htdemucs_6s"
        w0, w1 = window(song)
        res[song] = {"hash": h, "window_s": [w0, w1]}
        for name, fn in (("stem_full", "guitar.wav"), ("mix_full", "mix.wav")):
            x = wav(d / fn)
            if x.ndim > 1:
                x = x.mean(axis=1)
            A = RB._analyse(x)
            e = RB._fill(RB.ReverbEstimate(duration_s=len(x) / RB.SR), A, keep_curve=True).to_dict()
            e["ci"] = RB.bootstrap(x, n=a.boot, seed=231, A=A)
            res[song][name] = e
            log(f"{song} {name}: {len(x) / RB.SR:.0f} s, adc rt {e['adc_rt60_s']:.2f} early {e['adc_early_db']:.1f} ev {e['adc_events']}, "
                f"sdd peak {e['sdd_peak_s']:.2f} n {e['sdd_n']}, {time.time() - t0:.0f} s")
        # the 10 s excerpt of the stem (the T229 stem.wav) with CI too
        x = wav(T229 / song / "audio" / "stem.wav")
        A = RB._analyse(x)
        e = RB._fill(RB.ReverbEstimate(duration_s=len(x) / RB.SR), A).to_dict()
        e["ci"] = RB.bootstrap(x, n=a.boot, seed=231, A=A)
        res[song]["stem_excerpt"] = e
    dump("songs", res, songs_text(res))


def _ci(e: dict, k: str) -> str:
    lo, hi = e["ci"][k]
    return f"{_nan(e[k]):5.2f} [{lo:4.2f}, {hi:4.2f}]" if k != "adc_early_db" else f"{_nan(e[k]):5.1f} [{lo:5.1f}, {hi:5.1f}]"


def songs_text(res: dict) -> str:
    L = ["Per-song estimates on the FULL separated guitar stem, the full mix, and the 10 s excerpt; [16th, 84th percentile] block bootstrap", ""]
    L.append(f"{'song':13s} {'signal':13s} {'len s':>6s} {'adc RT60 s':>22s} {'adc early dB':>24s} {'sdd peak s':>22s} {'ml peak s':>22s} events sdd_n")
    for s, r in res.items():
        for name in ("stem_full", "mix_full", "stem_excerpt"):
            e = r[name]
            L.append(f"{s:13s} {name:13s} {e['duration_s']:6.0f} {_ci(e, 'adc_rt60_s'):>22s} {_ci(e, 'adc_early_db'):>24s} "
                     f"{_ci(e, 'sdd_peak_s'):>22s} {_ci(e, 'ml_peak_s'):>22s} {e['adc_events']:6d} {e['sdd_n']:5d}")
    return "\n".join(L)


# ------------------------------------------------------------------------------------------ compare

def _t230():
    spec = importlib.util.spec_from_file_location("t230", ROOT / "scripts" / "reverb_pilot_t230.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def distance(c: dict, s: dict) -> float:
    rc, rs = _nan(c["adc_rt60_s"]), _nan(s["adc_rt60_s"])
    ec, es = _nan(c["adc_early_db"]), _nan(s["adc_early_db"])
    if not (math.isfinite(rc) and math.isfinite(rs) and math.isfinite(ec) and math.isfinite(es)):
        return float("nan")
    return abs(math.log(rc / rs)) / SCALE_LOG_RT + abs(ec - es) / SCALE_EARLY_DB


def cmd_compare(_a) -> None:
    t230 = _t230()
    key = json.load(open(KEY))
    answers = json.load(open(ANSWERS))["songs"]
    val = json.load(open(OUT / "validate.json"))
    songs = json.load(open(OUT / "songs.json")) if (OUT / "songs.json").exists() else {}
    out = {"scale_log_rt": SCALE_LOG_RT, "scale_early_db": SCALE_EARLY_DB, "songs": {}}
    L = ["POST HOC (not pre-registered): the human's T230 ranks vs the blind estimates. 1 = closest to the song's guitar.", ""]
    rhos = {"excerpt": [], "full": []}
    for s in SONGS:
        des = key["songs"][s]
        hv = t230.human_values(des, answers.get(s, {}).get("ranks", {}))
        if hv is None:
            L.append(f"{s}: no ranks (excluded by the human)")
            continue
        h, rep = hv
        cands = des["candidates"]
        ce = {k: val[s][k] for k in cands}
        refs = {"excerpt": val[s]["stem"]}
        if s in songs:
            refs["full"] = songs[s]["stem_full"]
        rec = {"human": dict(zip(cands, h.tolist(), strict=True)), "estimates": {k: {"adc_rt60_s": _nan(ce[k]["adc_rt60_s"]), "adc_early_db": _nan(ce[k]["adc_early_db"]),
                                                                                "type": ce[k]["truth"]["type"], "mix": ce[k]["truth"]["mix"], "rt": ce[k]["truth"]["rt"]} for k in cands}}
        L.append(f"{s}  (repeat {rep['item']} ranked {rep['raw']})")
        L.append(f"  {'candidate':18s} {'human':>5s} {'type':>5s} {'Mix':>4s} {'RT':>5s} | {'est RT':>6s} {'early':>6s} | " + " ".join(f"{'d_' + r:>9s}" for r in refs))
        for r, ref in refs.items():
            L.append(f"  stem ({r}): est RT {_nan(ref['adc_rt60_s']):.2f} s, early {_nan(ref['adc_early_db']):.1f} dB, events {ref['adc_events']}")
        order = np.argsort(h)
        for i in order:
            k = cands[i]
            ds = {r: distance(ce[k], ref) for r, ref in refs.items()}
            L.append(f"  {k:18s} {h[i]:5.1f} {ce[k]['truth']['type']:>5s} {ce[k]['truth']['mix']:4.2f} {ce[k]['truth']['rt']:5.2f} | "
                     f"{_nan(ce[k]['adc_rt60_s']):6.2f} {_nan(ce[k]['adc_early_db']):6.1f} | " + " ".join(f"{ds[r]:9.2f}" for r in refs))
        rec["distances"] = {}
        for r, ref in refs.items():
            d = np.array([distance(ce[k], ref) for k in cands])
            if np.isfinite(d).any():
                d = np.where(np.isfinite(d), d, np.nanmax(d) + 1.0)        # as T230 amendment 1
            rho = t230._rho(h, d) if np.isfinite(d).all() else float("nan")
            rec["distances"][r] = {"d": d.tolist(), "rho": rho}
            rhos[r].append(rho)
            closest_est = cands[int(np.argmin(d))] if np.isfinite(d).all() else None
            L.append(f"  rho(human, d_{r}) = {rho:.2f}; estimate's closest = {closest_est}; human's closest = {cands[int(np.argmin(h))]}")
        L.append("")
        out["songs"][s] = rec
    for r, v in rhos.items():
        v = np.array(v, float); v = v[np.isfinite(v)]
        L.append(f"mean rho over songs, reference = stem {r}: {np.mean(v) if v.size else float('nan'):.2f} over {v.size} songs, positive on {(v > 0).sum()}")
        out[f"mean_rho_{r}"] = float(np.mean(v)) if v.size else float("nan")
    L.append("(T230's four pre-registered signals: mean rho 0.05, 0.04, -0.09, -0.40; chance 0; six candidates per song, so one song alone")
    L.append(" needs |rho| about 0.83 to mean anything)")
    dump("compare", out, "\n".join(L))


# ------------------------------------------------------------------------------------------ ledger

def cmd_ledger(_a) -> None:
    env = dict(os.environ, TONEMATCH_AGENT="tone-metrics")
    rows = []
    syn = json.load(open(OUT / "synth.json"))
    for k in ("adc_rt60_s", "sdd_peak_s"):
        err = [abs(r[k] / r["rt60_s"] - 1) for r in syn if math.isfinite(r[k])]
        rows.append((f"synth_{k[:-2]}_within15pct", float(np.mean([e <= 0.15 for e in err])) if err else 0.0, f"{len(err)}/{len(syn)} finite; noise bursts RT 0.5/1/2 s DRR 10/20/30 dB"))
    val = json.load(open(OUT / "validate.json"))
    for k in ("adc_rt60_s", "sdd_peak_s", "ml_peak_s"):
        ok = n = 0
        for s in val:
            for t in TYPES:
                for m in MIXES:
                    vals = [_nan(val[s].get(f"{t}-m{int(round(m * 100))}-{lv}", {}).get(k)) for lv in LEVELS]
                    if all(math.isfinite(v) for v in vals):
                        n += 1; ok += int(vals[0] < vals[1] < vals[2])
        rows.append((f"decay_rank_acc_{k[:-2]}", ok / n if n else 0.0, f"{ok}/{n} (type Mix) triples ordered short<medium<long of {7 * 9}; 189 T230 candidates"))
    ok = n = 0
    for s in val:
        for t in TYPES:
            for lv in LEVELS:
                vals = [_nan(val[s].get(f"{t}-m{int(round(m * 100))}-{lv}", {}).get("adc_early_db")) for m in MIXES]
                if all(math.isfinite(v) for v in vals):
                    n += 1; ok += int(vals[0] < vals[1] < vals[2])
    rows.append(("mix_monotone_adc_early", ok / n if n else 0.0, f"{ok}/{n} (type decay) triples with early_db rising with Mix of {7 * 9}"))
    if (OUT / "songs.json").exists():
        sg = json.load(open(OUT / "songs.json"))
        for s, r in sg.items():
            e = r["stem_full"]
            rows.append((f"stem_full_adc_rt60_s_{s}", _nan(e["adc_rt60_s"]), f"full stem {e['duration_s']:.0f} s; CI16-84 {e['ci']['adc_rt60_s'][0]:.2f}-{e['ci']['adc_rt60_s'][1]:.2f}; early {_nan(e['adc_early_db']):.1f} dB; events {e['adc_events']}"))
    if (OUT / "compare.json").exists():
        cp = json.load(open(OUT / "compare.json"))
        for r in ("excerpt", "full"):
            if f"mean_rho_{r}" in cp:
                rows.append((f"posthoc_mean_rho_{r}", cp[f"mean_rho_{r}"], "post hoc Spearman of the human's T230 ranks vs distance in (log RT early_db); not pre-registered"))
    for metric, value, note in rows:
        v = value if math.isfinite(value) else -1.0
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "-", "--dataset",
                        "reverb-estimator-v1", "--metric", metric, "--value", f"{v:.4f}", "--renders", "0", "--notes", note.replace(",", ";")],
                       check=True, env=env, cwd=ROOT)
        log(f"ledger: {metric} = {v:.4f}  ({note})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("synth").set_defaults(fn=cmd_synth)
    p = sub.add_parser("validate"); p.add_argument("--songs", nargs="+", default=list(SONGS)); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("songs"); p.add_argument("--songs", nargs="+", default=list(SONGS)); p.add_argument("--boot", type=int, default=200); p.set_defaults(fn=cmd_songs)
    sub.add_parser("compare").set_defaults(fn=cmd_compare)
    sub.add_parser("ledger").set_defaults(fn=cmd_ledger)
    a = ap.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
