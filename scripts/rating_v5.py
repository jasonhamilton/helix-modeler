"""Human rating session v5 (T188): do the blind predictions of T185 sound right?

The human hears the SAME performance in every clip: the Target is the target's true drive setting rendered on the
target's OWN DI excerpt (5 s), and A and B are the matcher's predicted model and settings rendered on that same DI.
Only the predicted setting differs from the target.

  pairs     blind condition: the AFx-ordered top-1 against the provisional-ordered top-1 (fam2 arms, the T185 default
            shortlist). Where both arms picked the same model and settings: the AFx top-1 against its third-ranked
            alternative. A few same-player-condition targets use the same rule.
  audio     only Guitar-TECHS DI and the human's own dry takes; no song audio, no stems.
  ratings   1-5 for A and for B (prominent), plus A closer / B closer / too close.
  levels    all clips -23 LUFS integrated (peak guard lowers all three alike).

No score picks the pairs. Nothing here judges audio or admits a score. Used through scripts/ab_session.py
(rating-build-v5, play, rating-score).
"""
from __future__ import annotations

import json
import math
import secrets
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import rating_session as RS
import rating_v2 as R2
import rating_v3 as R3

SR = RS.SR
PRED = ROOT / "reports" / "local" / "t185" / "predictions.json"
N_BLIND, N_SAME_PLAYER = 18, 6
SOURCE_QUOTA = {"blind": {"own": 8, "techs": 10}, "same_player": {"own": 2, "techs": 4}}
N_RETESTS = 2
NOISE_DB = -60.0
ARM_AFX, ARM_PROV = "afx_fam2", "prov_fam2"
ALT_RANK = 2                      # third-ranked prediction of the AFx arm
FAR = ["HD2_DistTriangleFuzz", "HD2_DistScream808"]
PROTOCOL = (
    "Per trial: Target = the target's true model and settings on its own DI excerpt; A and B = predicted model and "
    "settings on that same excerpt. Blind condition (matcher saw another player's DI): AFx-ordered top-1 against the "
    "provisional-ordered top-1 (arms afx_fam2 and prov_fam2). If both picked the same model and settings: the AFx "
    "top-1 against the AFx third-ranked alternative (a different model). 18 blind targets and 6 same-player targets "
    "are drawn at random from the 96 (stratified by DI source: own takes / Guitar-TECHS), one trial each. "
    "Plus 1 sanity trial (target copy against a far model), 1 near-identical sanity trial (copy against copy plus "
    "-60 dB noise), 2 retests (a regular trial again with the sides swapped). Side assignment random.")


def _k(p: dict) -> tuple:
    return (p["model"], tuple(sorted((a, round(float(b), 4)) for a, b in p["values"].items())))


def source_of(path: str) -> str:
    if "/data/di/Guitar-TECHS/" in "/" + path:
        return "techs"
    if "mother-playalong/takes/" in path and path.endswith("_dry.wav"):
        return "own"
    raise ValueError(f"not an allowed audio source (only Guitar-TECHS DI and the human's dry takes): {path}")


def pick_pair(preds: dict, cond: str, w: str) -> dict:
    """The two predictions to compare for target w in `cond`; ptype afx_vs_prov or afx_vs_alt."""
    a = preds[cond][ARM_AFX][w]["predicted"]
    p = preds[cond][ARM_PROV][w]["predicted"]
    if _k(a[0]) != _k(p[0]):
        return {"ptype": "afx_vs_prov", "afx": (a[0], 0), "other": (p[0], 0), "other_kind": "prov"}
    alt = a[ALT_RANK] if a[ALT_RANK]["model"] != a[0]["model"] else a[1]
    return {"ptype": "afx_vs_alt", "afx": (a[0], 0), "other": (alt, a.index(alt)), "other_kind": "alt"}


def candidate_targets(pred: dict) -> dict[str, list[str]]:
    """Targets by DI source whose audio is allowed and present."""
    out = {"own": [], "techs": []}
    for w, t in sorted(pred["targets"].items()):
        if Path(ROOT / t["target_di"]).exists():
            out[source_of(t["target_di"])].append(w)
    return out


def plan_targets(pred: dict, rng, ok=lambda cond, w, pair: True) -> list[dict]:
    """Walk the shuffled lists; keep the first quota-many that pass `ok` (render feasibility), distinct targets."""
    by_src = candidate_targets(pred)
    taken: set[str] = set()
    out: list[dict] = []
    for cond in ("blind", "same_player"):
        for src, n in SOURCE_QUOTA[cond].items():
            pool = [w for w in by_src[src] if w not in taken]
            got = 0
            for i in rng.permutation(len(pool)):
                w = pool[int(i)]
                pair = pick_pair(pred["predictions"], cond, w)
                if not ok(cond, w, pair):
                    continue
                taken.add(w)
                out.append({"cond": cond, "w": w, "src": src, **pair})
                got += 1
                if got == n:
                    break
            if got < n:
                raise RuntimeError(f"only {got} of {n} {cond}/{src} targets are usable")
    return out


def cand_name(c: dict) -> str:
    kv = ",".join(f"{k}={v:g}" for k, v in sorted(c["params"].items()))
    return f"{c['kind']}:{c['model'].replace('HD2_Dist', '').replace('HD2_', '')}({kv}) [{c['desc']}]"


def read_excerpt(t: dict) -> tuple[np.ndarray, int]:
    """(lead + excerpt, lead samples) of the target's DI, mono."""
    s0, n, lead = int(t["target_di_start"]), int(t["target_di_n"]), int(t["lead_in_samples"])
    lead = min(lead, s0)
    x, sr = sf.read(str(ROOT / t["target_di"]), start=s0 - lead, frames=n + lead, dtype="float32", always_2d=True)
    if sr != SR:
        raise ValueError(f"{t['target_di']}: expected {SR} Hz, got {sr}")
    return x.mean(axis=1).astype(np.float32), lead


def build_session(name: str, seed: int | None = None, render=None, pred_path: Path = PRED,
                  out_root: Path = RS.RATINGS_DIR, key_root: Path = RS.KEY_DIR, n_retests: int = N_RETESTS,
                  usable=None, log=print) -> tuple[Path, Path]:
    seed = secrets.randbelow(2**31) if seed is None else seed
    rng = np.random.default_rng(seed)
    pred = json.loads(pred_path.read_text())
    close = None
    if render is None:
        render, close = R2.native_renderer(log)
    if usable is None:
        from tonematch.engines import native_library as NL
        tv = NL.time_variant_models()
        from tonematch.catalog import load_catalog
        models = load_catalog()["models"]
        usable = lambda m: m not in tv and m not in R2.EXCLUDED and m in models
    ok = lambda cond, w, pair: (usable(pred["targets"][w]["model"]) and usable(pair["afx"][0]["model"])
                                and usable(pair["other"][0]["model"]))
    sel = plan_targets(pred, rng, ok)
    folder, keyf = out_root / name, key_root / f"{name}.key.json"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    key_root.mkdir(parents=True, exist_ok=True)
    didir = key_root / f"{name}.di"
    if didir.exists():
        shutil.rmtree(didir)
    didir.mkdir()

    n_renders = 0
    tg: list[dict] = []
    try:
        for ti, s in enumerate(sel):
            t = pred["targets"][s["w"]]
            seg, lead = read_excerpt(t)
            n_s = len(seg) - lead

            def rend(model, params, seg=seg, lead=lead, n_s=n_s):
                nonlocal n_renders
                n_renders += 1
                y = np.asarray(render(model, params, seg), np.float32)[lead:lead + n_s]
                if not np.all(np.isfinite(y)) or RS.rms_db(y) < -80:
                    raise RuntimeError(f"render failed or silent: {model} {params}")
                return y

            ty = rend(t["model"], t["settings"])
            cands = []
            for cid, (p, rank), kind in (("c1", s["afx"], "afx"), ("c2", s["other"], s["other_kind"])):
                c = {"cid": cid, "kind": kind, "model": p["model"], "params": {k: float(v) for k, v in p["values"].items()},
                     "rank": rank, "knob": None, "delta": None,
                     "desc": {"afx": "AFx order top-1", "prov": "provisional order top-1",
                              "alt": f"AFx order rank {rank + 1}"}[kind],
                     "model_right": p["model"] == t["model"]}
                c["raw"] = rend(c["model"], c["params"])
                cands.append(c)
            far = next(m for m in FAR if m != t["model"])
            far_c = {"cid": "c3", "kind": "far", "model": far, "params": {}, "rank": None, "knob": None, "delta": None,
                     "desc": "default setting", "model_right": False}
            far_c["params"] = {k: v for k, v in render.defaults(far).items()}
            far_c["raw"] = rend(far, far_c["params"])
            cands.append(far_c)
            log(f"target {ti + 1}/{len(sel)} {s['w']} {s['cond']} {t['model']}: {s['ptype']} ({n_renders} renders so far)")
            tg.append({"ti": ti, **s, "t": t, "raw": ty, "cands": cands, "dry": seg[lead:], "n_s": n_s})
    finally:
        if close is not None:
            close()

    faded = {}
    for g in tg:
        faded[(g["ti"], "T")] = RS.cut(g["raw"], 0, len(g["raw"]))
        for c in g["cands"]:
            faded[(g["ti"], c["cid"])] = RS.cut(c["raw"], 0, len(c["raw"]))
    lin = {k: RS.lufs(v) for k, v in faded.items()}
    g0 = {k: (RS.TARGET_LUFS - lin[k] if lin[k] > -69.9 else 0.0) for k in faded}
    worst = max(float(np.max(np.abs(faded[k]))) * 10 ** (g0[k] / 20) for k in faded)
    guard = 20 * math.log10(RS.PEAK_CEIL / worst) if worst > RS.PEAK_CEIL else 0.0
    gain = {k: g0[k] + guard for k in faded}
    lev = {k: (faded[k] * 10 ** (gain[k] / 20)).astype(np.float32) for k in faded}
    orig_db = {k: RS.rms_db(v) for k, v in faded.items()}
    di_file = {}
    for g in tg:
        p = didir / f"target{g['ti'] + 1}_di.wav"
        sf.write(str(p), g["dry"], SR, subtype="FLOAT")
        di_file[g["ti"]] = str(p)

    def cid_of(ti, kind):
        return next(c["cid"] for c in tg[ti]["cands"] if c["kind"] == kind)

    plan: list[dict] = []
    for ti, g in enumerate(tg):
        plan.append({"kind": "regular", "t": ti, "A": cid_of(ti, "afx"), "B": cid_of(ti, g["other_kind"]), "ptype": g["ptype"]})
    blind_idx = [i for i, g in enumerate(tg) if g["cond"] == "blind"]
    san = [int(blind_idx[int(v)]) for v in rng.permutation(len(blind_idx))[:2]]
    plan.append({"kind": "sanity_target", "t": san[0], "A": "copy", "B": cid_of(san[0], "far"), "expected": "copy"})
    plan.append({"kind": "sanity_near", "t": san[1], "A": "copy", "B": "noise"})
    for p in plan:
        if rng.random() < 0.5:
            p["A"], p["B"] = p["B"], p["A"]
    rep_src = [int(i) for i in rng.permutation(len(blind_idx))[:n_retests]]       # plan indices == target indices
    rep_src = [blind_idx[i] for i in rep_src]
    for j in rep_src:
        o = plan[j]
        plan.append({"kind": "repeat", "t": o["t"], "A": o["B"], "B": o["A"], "orig": j, "ptype": o["ptype"]})
    order = R3._order(plan, rng)
    pos = {s: i for i, s in enumerate(order)}

    noise_clip = {}
    for p in plan:
        if "noise" in (p["A"], p["B"]):
            T = faded[(p["t"], "T")]
            nz = rng.standard_normal(len(T)).astype(np.float32)
            y = T + nz * (float(np.sqrt(np.mean(T ** 2))) * 10 ** (NOISE_DB / 20) / float(np.std(nz)))
            gg = gain[(p["t"], "T")]
            noise_clip[p["t"]] = ((y * 10 ** (gg / 20)).astype(np.float32), gg, RS.rms_db(y))

    def clip(t, ref):
        if ref == "copy":
            return lev[(t, "T")], gain[(t, "T")], orig_db[(t, "T")]
        if ref == "noise":
            return noise_clip[t]
        return lev[(t, ref)], gain[(t, ref)], orig_db[(t, ref)]

    def info(t, ref):
        g = tg[t]
        if ref == "copy":
            return {"name": "target copy", "role": "target_copy", "cand_kind": "target", "model": g["t"]["model"],
                    "params": g["t"]["settings"], "cid": None}
        if ref == "noise":
            return {"name": f"target + noise ({NOISE_DB:g} dB)", "role": "near_identical", "cand_kind": "target",
                    "model": g["t"]["model"], "params": g["t"]["settings"], "cid": None}
        c = next(c for c in g["cands"] if c["cid"] == ref)
        return {"name": cand_name(c), "role": "candidate", "cand_kind": c["kind"], "model": c["model"],
                "params": c["params"], "cid": ref, "knob": None, "delta": None, "rank": c["rank"],
                "model_right": c["model_right"]}

    trials, ktrials, id_of = [], [], {}
    for rank, si in enumerate(sorted(range(len(plan)), key=lambda i: pos[i]), start=1):
        p = plan[si]
        g = tg[p["t"]]
        tag = secrets.token_hex(4)
        files = {"T": f"{tag}_t.wav", "A": f"{tag}_a.wav", "B": f"{tag}_b.wav"}
        sides = {"T": lev[(p["t"], "T")], "A": clip(p["t"], p["A"])[0], "B": clip(p["t"], p["B"])[0]}
        for r in "TAB":
            RS.write_clip(folder / files[r], sides[r])
        ai, bi = info(p["t"], p["A"]), info(p["t"], p["B"])
        for ref, d in ((p["A"], ai), (p["B"], bi)):
            _, gg, odb = clip(p["t"], ref)
            d["orig_db"], d["gain_db"] = odb, gg
            d["source"] = f"target {p['t'] + 1} {d['cid'] or 'target'}"
        expected = None
        if p["kind"] == "sanity_target":
            expected = "A" if ai["role"] == "target_copy" else "B"
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": p["kind"], "family": f"target{p['t'] + 1} {g['t']['model']}",
                        "target": p["t"] + 1, "condition": g["cond"], "di_source": g["src"], "w": g["w"],
                        "excerpt_start_s": g["t"]["target_di_start"] / SR, "excerpt_s": g["n_s"] / SR,
                        "A": ai, "B": bi, "files": files, "target_orig_db": orig_db[(p["t"], "T")],
                        "di": di_file[p["t"]], "expected": expected,
                        "selection": {"kind": "design", "pair_type": p["ptype"], "condition": g["cond"]} if p.get("ptype") else None,
                        "repeat_of": id_of[p["orig"]] if p["kind"] == "repeat" else None,
                        "loudness": {"gain_db": [gain[(p["t"], "T")], clip(p["t"], p["A"])[1], clip(p["t"], p["B"])[1]]}})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": name, "excerpt_s": tg[0]["n_s"] / SR, "trials": trials}, indent=1))
    note = ("In every trial the Target and both candidates are the same guitar notes through one Helix drive block; "
            "only the model or the setting differs. Judge which of A and B is closer to the Target in tone (the "
            "distortion and the overall sound). Rate both A and B 1 to 5 with the big buttons. Either may be identical "
            "to the Target, or neither close; use the whole 1 to 5 range.")
    (folder / "index.html").write_text(R3.render_page(name, trials, note))
    tinfo = {str(g["ti"] + 1): {"w": g["w"], "condition": g["cond"], "di_source": g["src"], "model": g["t"]["model"],
                                "params": g["t"]["settings"], "di_path": g["t"]["target_di"],
                                "start": g["t"]["target_di_start"], "ptype": g["ptype"],
                                "orig_db": orig_db[(g["ti"], "T")], "di": di_file[g["ti"]],
                                "pool": [{"cid": c["cid"], "kind": c["kind"], "name": cand_name(c)} for c in g["cands"]]}
             for g in tg}
    keyf.write_text(json.dumps({"session": name, "version": 5, "seed": seed, "peak_guard_db": guard,
                                "target_lufs": RS.TARGET_LUFS, "protocol": PROTOCOL, "n_renders": n_renders,
                                "targets": tinfo, "trials": ktrials}, indent=1, default=float))
    return folder, keyf


# ---------------------------------------------------------------- analysis

def _mean(v):
    return float(np.mean(v)) if v else None


def _sign_p(k: int, n: int) -> float | None:
    return RS.binom_two_sided(k, n) if n else None


def analyse(key: dict, byid: dict, sc: dict) -> dict:
    """Per-arm mean rating, how often the AFx pick was judged closer, per-condition and per-source breakdowns,
    agreement of every score with the human choice by pair type and condition."""
    from rating_session import SCORE_ORDER, agreement
    allr = [t for t in key["trials"] if t["kind"] in ("regular", "repeat")]
    regs = [t for t in key["trials"] if t["kind"] == "regular"]

    def side_of(t, kind):
        return next((s for s in "AB" if t[s]["cand_kind"] == kind), None)

    def groups(t):
        sel = t["selection"]
        return ["all", f"cond:{t['condition']}", f"type:{sel['pair_type']}", f"src:{t['di_source']}",
                f"cond:{t['condition']} / type:{sel['pair_type']}"]

    closer: dict[str, dict] = {}
    for t in allr:
        a = byid.get(t["id"], {})
        sa = side_of(t, "afx")
        ch = a.get("choice")
        ra, rb = a.get("rating_a"), a.get("rating_b")
        for g in groups(t):
            d = closer.setdefault(g, {"n": 0, "afx": 0, "other": 0, "tie": 0, "none": 0, "r_afx_higher": 0,
                                      "r_other_higher": 0, "r_equal": 0})
            d["n"] += 1
            if ch == "tie":
                d["tie"] += 1
            elif ch in ("A", "B"):
                d["afx" if ch == sa else "other"] += 1
            else:
                d["none"] += 1
            if ra is not None and rb is not None:
                x, y = (ra, rb) if sa == "A" else (rb, ra)
                d["r_afx_higher" if x > y else "r_other_higher" if x < y else "r_equal"] += 1
    for d in closer.values():
        n = d["afx"] + d["other"]
        d["afx_closer_rate"] = d["afx"] / n if n else None
        d["p"] = _sign_p(d["afx"], n)
        d["decided"] = n
        d["ci"] = RS.clopper_pearson(d["afx"], n) if n else None

    # mean rating per arm (regular trials only), by group
    arms: dict[str, dict[str, list]] = {}
    for t in regs:
        a = byid.get(t["id"], {})
        for side, f in (("A", "rating_a"), ("B", "rating_b")):
            r = a.get(f)
            if r is None:
                continue
            info = t[side]
            if info["role"] != "candidate":
                continue
            for g in groups(t):
                arms.setdefault(g, {}).setdefault(f"arm:{info['cand_kind']}", []).append(float(r))
                arms[g].setdefault(f"model {'right' if info['model_right'] else 'wrong'}", []).append(float(r))
    anchors = [float(byid.get(t["id"], {}).get(f)) for t in key["trials"] if t["kind"] in ("sanity_target", "sanity_near")
               for s, f in (("A", "rating_a"), ("B", "rating_b")) if t[s]["role"] in ("target_copy", "near_identical")
               and byid.get(t["id"], {}).get(f) is not None]
    mean_rating = {g: {k: {"n": len(v), "mean": _mean(v), "sd": float(np.std(v)) if len(v) > 1 else None}
                       for k, v in sorted(d.items())} for g, d in arms.items()}

    names = [n for n in SCORE_ORDER if any(n in v["A"] for v in sc.values())]
    agree: dict[str, dict] = {}
    sets = {"all": allr}
    for t in allr:
        sets.setdefault(f"cond:{t['condition']}", []).append(t)
        sets.setdefault(f"type:{t['selection']['pair_type']}", []).append(t)
    for g, ts in sets.items():
        ts = [t for t in ts if t["id"] in sc]
        agree[g] = {n: agreement([(byid.get(t["id"], {}).get("choice"), sc[t["id"]]["A"].get(n, float("nan")),
                                   sc[t["id"]]["B"].get(n, float("nan"))) for t in ts]) for n in names}
    # does each score prefer the AFx pick as often as the ear does?
    score_afx: dict[str, dict] = {}
    for n in names:
        k = m = 0
        for t in allr:
            if t["id"] not in sc:
                continue
            sa = side_of(t, "afx")
            so = "B" if sa == "A" else "A"
            va, vo = sc[t["id"]][sa].get(n, float("nan")), sc[t["id"]][so].get(n, float("nan"))
            if math.isnan(va) or math.isnan(vo) or va == vo:
                continue
            m += 1
            k += va < vo
        score_afx[n] = {"k": k, "n": m, "rate": k / m if m else None}
    return {"closer": closer, "mean_rating": mean_rating, "anchor": _mean(anchors), "n_anchor": len(anchors),
            "agree": agree, "score_prefers_afx": score_afx}


def fmt_extras(v5: dict) -> str:
    f = lambda v, p="{:.2f}": "-" if v is None else p.format(v)
    L = ["\nv5: was the AFx-ordered pick judged closer than the other pick? (regular + repeat trials)"]
    L.append(f"  {'group':<34}{'trials':>7}{'AFx closer':>11}{'other closer':>13}{'tie':>5}{'AFx rate':>10}{'p(=.5)':>8}"
             f"{'rated higher: AFx/other/equal':>32}")
    for g, d in closer_sorted(v5["closer"]):
        L.append(f"  {g:<34}{d['n']:>7}{d['afx']:>11}{d['other']:>13}{d['tie']:>5}{f(d['afx_closer_rate'], '{:.0%}'):>10}"
                 f"{f(d['p'], '{:.3f}'):>8}{d['r_afx_higher']:>12}/{d['r_other_higher']}/{d['r_equal']}")
    L.append(f"\nMean 1-5 rating per arm (regular trials; anchor = mean rating of the exact target copy and the "
             f"noise copy = {f(v5['anchor'])}, n={v5['n_anchor']}):")
    for g, d in v5["mean_rating"].items():
        L.append(f"  {g}: " + "; ".join(f"{k} {x['mean']:.2f} (n={x['n']})" for k, x in d.items()))
    L.append("\nAgreement of each score with the human's choice (agree/decided), by group; last column: how often the score "
             "itself prefers the AFx pick:")
    gs = list(v5["agree"])
    L.append(f"  {'score':<20}" + "".join(f"{g[:18]:>20}" for g in gs) + f"{'prefers AFx':>14}")
    for n in next(iter(v5["agree"].values())):
        row = "".join(f"{(str(v5['agree'][g][n]['k']) + '/' + str(v5['agree'][g][n]['n'])) if v5['agree'][g][n]['n'] else '-':>20}"
                      for g in gs)
        s = v5["score_prefers_afx"][n]
        L.append(f"  {n:<20}{row}{(str(s['k']) + '/' + str(s['n'])) if s['n'] else '-':>14}")
    return "\n".join(L)


def closer_sorted(c: dict):
    return sorted(c.items(), key=lambda kv: (kv[0] != "all", kv[0]))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="human-ratings-v5")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    f, k = build_session(a.name, seed=a.seed)
    print(f"page folder {f}, key {k}")
