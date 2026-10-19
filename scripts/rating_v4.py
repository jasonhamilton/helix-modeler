"""Human rating session v4 (T177): near-twin models versus large knob moves (DECISIONS 2026-10-18, "go ahead").

v3 showed the human's ear: small knob steps are mostly inaudible (71% ties), near-twin models are always told apart.
v4 asks the two things that remain: how much does a twin swap cost, how much does a large knob move cost, and which
does the ear prefer when they are set against each other.

  audio     only the human's own dry takes, 7 s excerpts not used in v2 or v3 where possible
  targets   8: one hidden drive setting each, from different confusable groups (distortion-circuits.md 1.4)
  pool      per target: 2 near-twin models at the matched setting, 2 large moves of the target's own model
            (+-0.3 to 0.5 on Drive, and on Tone where the model has one), 1 far model (different class)
  trials    16 regular (8 twin-vs-move, 4 twin-vs-twin, 4 move-vs-move) + 2 sanity + 2 retests = 20
  ratings   1-5 for A and for B (prominent buttons), plus A closer / B closer / too close

No score picks the pairs; the pairs are fixed by design. Nothing here judges audio or admits a score.
Used through scripts/ab_session.py (rating-build-v4, play, rating-score).
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
EXCERPT_S = 7.0
LEAD_S = R2.LEAD_S
TAKE_PLAN = [1, 1, 2, 2, 2, 3, 3, 3]
MOVES = (0.3, 0.4, 0.5)
KNOB_LO, KNOB_HI = 0.03, 0.97
NOISE_DB = -60.0
N_RETESTS = 2
R3.KNOBS3.setdefault("HD2_DistKWB", ("Gain", ["Treble"]))
D = "HD2_Dist"
# label, target model, twins (same confusable group), far model (different class)
TARGETS = [
    ("A TS lineage (Scream 808)", D + "Scream808", ["HD2_DM4Screamer", D + "Pillars"], D + "DeezOneVintage"),
    ("B Klon / Bluesbreaker (Minotaur)", D + "Minotaur", [D + "Teemah", D + "HeirApparent"], D + "TriangleFuzz"),
    ("C Distortion+ / OD-250 (Top Secret OD)", D + "TopSecretOD", ["HD2_DM4Overdrive", D + "KWB"], D + "ArbitratorFuzz"),
    ("D RAT (Vermin Dist)", D + "VerminDist", [D + "RatatouilleDist", "HD2_DM4ClassicDistortion"], D + "Scream808"),
    ("E DS-1 (Deez One Vintage)", D + "DeezOneVintage", [D + "DeezOneMod", D + "HedgehogD9"], D + "Minotaur"),
    ("G Big Muff (Triangle Fuzz)", D + "TriangleFuzz", [D + "DarkDoveFuzz", D + "RamsHead"], D + "Scream808"),
    ("H Fuzz Face (Arbitrator Fuzz)", D + "ArbitratorFuzz", ["HD2_DM4FacialFuzz", "HD2_DM4JumboFuzz"], D + "Minotaur"),
    ("K Boosts (Kinky Boost)", D + "KinkyBoost", [D + "VitalBoost", D + "DerangedMaster"], D + "HedgehogD9"),
]
PROTOCOL = (
    "Fixed by design, no score involved. Per target 8 candidates are not needed: 2 near-twin models of the same "
    "confusable group at the matched setting (same Drive, tone knobs carried over by position), 2 large moves of the "
    "target's own model (Drive by 0.3, 0.4 or 0.5; Tone by the same where the model has one, else Drive the other way), "
    "1 far model of a different class (sanity only). Trials: every target gets one twin-vs-move trial; four random "
    "targets get a twin-vs-twin trial, the other four a move-vs-move trial. Which candidate lands on A is random.")


def _r2(v: float) -> float:
    return round(float(v), 2)


def pick_move(value: float, rng, prefer: int | None = None) -> float:
    """A signed delta of size 0.3, 0.4 or 0.5 that keeps value+delta inside the knob range."""
    signs = [prefer, -prefer] if prefer else [int(rng.choice([-1, 1]))]
    if not prefer:
        signs.append(-signs[0])
    for s in signs:
        sizes = [m for m in MOVES if KNOB_LO <= value + s * m <= KNOB_HI]
        if sizes:
            return s * float(sizes[int(rng.integers(0, len(sizes)))])
    raise ValueError(f"no large move fits value {value}")


def make_pool(spec: tuple, rng, defaults: dict) -> dict:
    label, tmodel, twins, far = spec
    drive, tones = R3.KNOBS3[tmodel]
    target = {drive: _r2(rng.uniform(0.35, 0.65))}
    for t in tones:
        target[t] = _r2(rng.uniform(0.3, 0.7))
    cands = []

    def move(knob, delta):
        p = dict(target)
        p[knob] = _r2(min(KNOB_HI, max(KNOB_LO, p[knob] + delta)))
        return {"kind": "move", "model": tmodel, "params": p, "knob": knob, "delta": delta,
                "desc": f"{knob} {delta:+.2f}", "move_knob": "drive" if knob == drive else "tone"}

    d1 = pick_move(target[drive], rng)
    cands.append(move(drive, d1))
    if tones:
        cands.append(move(tones[0], pick_move(target[tones[0]], rng)))
    else:
        cands.append(move(drive, pick_move(target[drive], rng, prefer=-int(np.sign(d1)))))
    for m in twins:
        cands.append({"kind": "twin", "model": m, "params": R3.transfer_setting(tmodel, target, m, defaults.get(m, {})),
                      "knob": None, "delta": None, "desc": "matched setting"})
    cands.append({"kind": "far", "model": far, "params": R3.transfer_setting(tmodel, target, far, defaults.get(far, {})),
                  "knob": None, "delta": None, "desc": "matched setting"})
    for i, c in enumerate(cands):
        c["cid"] = f"c{i + 1}"
    return {"label": label, "model": tmodel, "target": target, "cands": cands}


def used_windows(key_root: Path = RS.KEY_DIR) -> dict[int, list[int]]:
    out = R3.v2_windows(key_root)
    p = key_root / "human-ratings-v3.key.json"
    if p.exists():
        for t in json.loads(p.read_text())["targets"].values():
            out.setdefault(int(t["take"]), []).append(round(t["excerpt_start_s"] * SR))
    return out


def build_session(name: str, seed: int | None = None, render=None, takes_dir: Path = R2.TAKES_DIR,
                  takes_pattern: str = R2.TAKES_GLOB, take_plan=TAKE_PLAN, out_root: Path = RS.RATINGS_DIR,
                  key_root: Path = RS.KEY_DIR, excerpt_s: float = EXCERPT_S, defaults_fn=None, targets=TARGETS,
                  n_retests: int = N_RETESTS, log=print) -> tuple[Path, Path]:
    seed = secrets.randbelow(2**31) if seed is None else seed
    rng = np.random.default_rng(seed)
    close = None
    if render is None:
        render, close = R2.native_renderer(log)
        defaults_fn = render.defaults
    defaults_fn = defaults_fn or (lambda m: {})
    n_s = int(excerpt_s * SR)
    folder, keyf = out_root / name, key_root / f"{name}.key.json"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    key_root.mkdir(parents=True, exist_ok=True)
    didir = key_root / f"{name}.di"
    if didir.exists():
        shutil.rmtree(didir)
    didir.mkdir()

    exc = R3.choose_excerpts(takes_dir, takes_pattern, take_plan, excerpt_s, rng, used_windows(key_root))
    models = set()
    for _, tm, tw, fr in targets:
        models |= {tm, fr, *tw}
    defaults = {m: defaults_fn(m) for m in models}
    takes_x: dict[str, np.ndarray] = {}
    tg: list[dict] = []
    n_renders = 0
    try:
        for ti, spec in enumerate(targets):
            e = exc[ti]
            x = takes_x.setdefault(e["path"], RS.read_mono(Path(e["path"])))
            s0 = e["start"]
            lead = min(int(LEAD_S * SR), s0)
            seg = x[s0 - lead:s0 + n_s]
            pool = make_pool(spec, rng, defaults)

            def rend(model, params, seg=seg, lead=lead):
                nonlocal n_renders
                n_renders += 1
                y = np.asarray(render(model, params, seg), np.float32)[lead:lead + n_s]
                if not np.all(np.isfinite(y)) or RS.rms_db(y) < -80:
                    raise RuntimeError(f"render failed or silent: {model} {params}")
                return y

            ty = rend(pool["model"], pool["target"])
            for c in pool["cands"]:
                c["raw"] = rend(c["model"], c["params"])
                if np.array_equal(c["raw"], ty):
                    raise RuntimeError(f"candidate identical to the target: {c['model']}")
            log(f"target {ti + 1}/{len(targets)} {spec[0]}: {len(pool['cands'])} candidates ({n_renders} renders so far)")
            tg.append({"ti": ti, "label": spec[0], "model": pool["model"], "setting": pool["target"], "excerpt": e,
                       "raw": ty, "cands": pool["cands"], "dry": np.array(x[s0:s0 + n_s], np.float32)})
    finally:
        if close is not None:
            close()

    faded = {}
    for t in tg:
        faded[(t["ti"], "T")] = RS.cut(t["raw"], 0, len(t["raw"]))
        for c in t["cands"]:
            faded[(t["ti"], c["cid"])] = RS.cut(c["raw"], 0, len(c["raw"]))
    lin = {k: RS.lufs(v) for k, v in faded.items()}
    g0 = {k: (RS.TARGET_LUFS - lin[k] if lin[k] > -69.9 else 0.0) for k in faded}
    worst = max(float(np.max(np.abs(faded[k]))) * 10 ** (g0[k] / 20) for k in faded)
    guard = 20 * math.log10(RS.PEAK_CEIL / worst) if worst > RS.PEAK_CEIL else 0.0
    gain = {k: g0[k] + guard for k in faded}
    lev = {k: (faded[k] * 10 ** (gain[k] / 20)).astype(np.float32) for k in faded}
    orig_db = {k: RS.rms_db(v) for k, v in faded.items()}
    di_file = {}
    for t in tg:
        p = didir / f"target{t['ti'] + 1}_di.wav"
        sf.write(str(p), t["dry"], SR, subtype="FLOAT")
        di_file[t["ti"]] = str(p)

    def of(ti, kind, k=0):
        return [c for c in tg[ti]["cands"] if c["kind"] == kind][k]["cid"]

    # trial plan
    n_t = len(tg)
    tt_targets = set(int(v) for v in rng.permutation(n_t)[: n_t // 2])       # twin-vs-twin; the others move-vs-move
    plan: list[dict] = []
    for ti in range(n_t):
        tw, mv = of(ti, "twin", int(rng.integers(0, 2))), of(ti, "move", int(rng.integers(0, 2)))
        plan.append({"kind": "regular", "t": ti, "A": tw, "B": mv, "ptype": "twin_vs_move"})
        if ti in tt_targets:
            plan.append({"kind": "regular", "t": ti, "A": of(ti, "twin", 0), "B": of(ti, "twin", 1), "ptype": "twin_vs_twin"})
        else:
            plan.append({"kind": "regular", "t": ti, "A": of(ti, "move", 0), "B": of(ti, "move", 1), "ptype": "move_vs_move"})
    san = [int(v) for v in rng.permutation(n_t)[:2]]
    plan.append({"kind": "sanity_target", "t": san[0], "A": "copy", "B": of(san[0], "far"), "expected": "copy"})
    plan.append({"kind": "sanity_near", "t": san[1], "A": "copy", "B": "noise"})
    for p in plan:
        if rng.random() < 0.5:
            p["A"], p["B"] = p["B"], p["A"]
    tvm = [i for i, p in enumerate(plan) if p.get("ptype") == "twin_vs_move"]
    rep_src: list[int] = []
    for i in rng.permutation(len(tvm)):
        if plan[tvm[int(i)]]["t"] not in {plan[j]["t"] for j in rep_src}:
            rep_src.append(tvm[int(i)])
        if len(rep_src) == n_retests:
            break
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
            g = gain[(p["t"], "T")]
            noise_clip[p["t"]] = ((y * 10 ** (g / 20)).astype(np.float32), g, RS.rms_db(y))

    def clip(t, ref):
        if ref == "copy":
            return lev[(t, "T")], gain[(t, "T")], orig_db[(t, "T")]
        if ref == "noise":
            return noise_clip[t]
        return lev[(t, ref)], gain[(t, ref)], orig_db[(t, ref)]

    def info(t, ref):
        tt = tg[t]
        if ref == "copy":
            return {"name": "target copy", "role": "target_copy", "cand_kind": "target", "model": tt["model"],
                    "params": tt["setting"], "cid": None}
        if ref == "noise":
            return {"name": f"target + noise ({NOISE_DB:g} dB)", "role": "near_identical", "cand_kind": "target",
                    "model": tt["model"], "params": tt["setting"], "cid": None}
        c = next(c for c in tt["cands"] if c["cid"] == ref)
        return {"name": R3.cand_name(c), "role": "candidate", "cand_kind": c["kind"], "model": c["model"],
                "params": c["params"], "cid": ref, "knob": c["knob"], "delta": c["delta"],
                "move_knob": c.get("move_knob")}

    trials, ktrials, id_of = [], [], {}
    for rank, si in enumerate(sorted(range(len(plan)), key=lambda i: pos[i]), start=1):
        p = plan[si]
        t = tg[p["t"]]
        tag = secrets.token_hex(4)
        files = {"T": f"{tag}_t.wav", "A": f"{tag}_a.wav", "B": f"{tag}_b.wav"}
        sides = {"T": lev[(p["t"], "T")], "A": clip(p["t"], p["A"])[0], "B": clip(p["t"], p["B"])[0]}
        for r in "TAB":
            RS.write_clip(folder / files[r], sides[r])
        ai, bi = info(p["t"], p["A"]), info(p["t"], p["B"])
        for ref, d in ((p["A"], ai), (p["B"], bi)):
            _, g, odb = clip(p["t"], ref)
            d["orig_db"], d["gain_db"] = odb, g
            d["source"] = f"target {p['t'] + 1} {d['cid'] or 'target'}"
        expected = None
        if p["kind"] == "sanity_target":
            expected = "A" if ai["role"] == "target_copy" else "B"
        sel = {"kind": "design", "pair_type": p["ptype"]} if p.get("ptype") else None
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": p["kind"], "family": f"target{p['t'] + 1} {t['label']}",
                        "target": p["t"] + 1, "excerpt_start_s": t["excerpt"]["start"] / SR, "excerpt_s": excerpt_s,
                        "A": ai, "B": bi, "files": files, "target_orig_db": orig_db[(p["t"], "T")],
                        "di": di_file[p["t"]], "expected": expected, "selection": sel,
                        "repeat_of": id_of[p["orig"]] if p["kind"] == "repeat" else None,
                        "loudness": {"gain_db": [gain[(p["t"], "T")], clip(p["t"], p["A"])[1], clip(p["t"], p["B"])[1]]}})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": name, "excerpt_s": excerpt_s, "trials": trials}, indent=1))
    note = ("All clips are the same guitar notes through one Helix drive block; the setting or the model differs. "
            "Judge which of A and B is closer to the Target in tone (the distortion and the overall sound). "
            "Rate both A and B 1 to 5 with the big buttons.")
    (folder / "index.html").write_text(R3.render_page(name, trials, note))
    tinfo = {str(t["ti"] + 1): {"label": t["label"], "model": t["model"], "params": t["setting"], "take": t["excerpt"]["take"],
                                "excerpt_start_s": t["excerpt"]["start"] / SR, "fresh_vs_v2_v3": t["excerpt"].get("fresh"),
                                "orig_db": orig_db[(t["ti"], "T")], "di": di_file[t["ti"]],
                                "pool": [{"cid": c["cid"], "kind": c["kind"], "name": R3.cand_name(c)} for c in t["cands"]]}
             for t in tg}
    keyf.write_text(json.dumps({"session": name, "version": 4, "seed": seed, "peak_guard_db": guard,
                                "target_lufs": RS.TARGET_LUFS, "protocol": PROTOCOL, "n_renders": n_renders,
                                "targets": tinfo, "trials": ktrials}, indent=1, default=float))
    return folder, keyf


# ---------------------------------------------------------------- analysis

def _mean(v):
    return float(np.mean(v)) if v else None


def analyse(key: dict, byid: dict, sc: dict) -> dict:
    """Pair-type table, rating cost of a twin swap and of a large move, per-score agreement by pair type."""
    from rating_session import SCORE_ORDER, agreement
    regs = [t for t in key["trials"] if t["kind"] == "regular"]
    allr = [t for t in key["trials"] if t["kind"] in ("regular", "repeat")]
    pt: dict[str, dict] = {}
    for t in allr:
        a = byid.get(t["id"], {})
        d = pt.setdefault(t["selection"]["pair_type"], {"n": 0, "A": 0, "B": 0, "tie": 0, "none": 0, "r": [], "gap": [],
                                                       "pick_twin": 0, "pick_move": 0})
        d["n"] += 1
        ch = a.get("choice")
        d[ch if ch in ("A", "B", "tie") else "none"] += 1
        ra, rb = a.get("rating_a"), a.get("rating_b")
        d["r"] += [float(x) for x in (ra, rb) if x is not None]
        if ra is not None and rb is not None:
            d["gap"].append(abs(ra - rb))
        if t["selection"]["pair_type"] == "twin_vs_move" and ch in ("A", "B"):
            d["pick_twin" if t[ch]["cand_kind"] == "twin" else "pick_move"] += 1
    for d in pt.values():
        ans = d["A"] + d["B"] + d["tie"]
        d["tie_rate"] = d["tie"] / ans if ans else None
        d["mean_rating"], d["mean_gap"] = _mean(d.pop("r")), _mean(d.pop("gap"))
    # ratings by what the candidate is (regular trials only: the retests would count a clip twice)
    byk: dict[str, list] = {}
    anchors = []
    for t in key["trials"]:
        a = byid.get(t["id"], {})
        for side, f in (("A", "rating_a"), ("B", "rating_b")):
            r = a.get(f)
            if r is None:
                continue
            info = t[side]
            if t["kind"] == "regular":
                if info["cand_kind"] == "twin":
                    byk.setdefault("twin swap", []).append(float(r))
                elif info["cand_kind"] == "move":
                    byk.setdefault(f"large move, {info['move_knob']}", []).append(float(r))
                    byk.setdefault("large move, any", []).append(float(r))
                    byk.setdefault(f"large move |{abs(info['delta']):.1f}|", []).append(float(r))
            elif t["kind"] == "sanity_target" and info["role"] == "target_copy":
                anchors.append(float(r))
            elif t["kind"] == "sanity_target":
                byk.setdefault("far model (sanity)", []).append(float(r))
            elif t["kind"] == "sanity_near":
                anchors.append(float(r))
    anchor = _mean(anchors)
    cost = {}
    for k, v in sorted(byk.items()):
        m = _mean(v)
        cost[k] = {"n": len(v), "mean": m, "sd": float(np.std(v)) if len(v) > 1 else None, "cost_vs_5": 5.0 - m,
                   "cost_vs_anchor": None if anchor is None else anchor - m}
    twin_by_group: dict[str, list] = {}
    for t in regs:
        a = byid.get(t["id"], {})
        for side, f in (("A", "rating_a"), ("B", "rating_b")):
            if t[side]["cand_kind"] == "twin" and a.get(f) is not None:
                twin_by_group.setdefault(t["family"], []).append(float(a[f]))
    # per-score agreement by pair type
    names = [n for n in SCORE_ORDER if any(n in v["A"] for v in sc.values())]
    agree = {}
    for typ in sorted(pt):
        ts = [t for t in allr if t["selection"]["pair_type"] == typ and t["id"] in sc]
        agree[typ] = {n: agreement([(byid.get(t["id"], {}).get("choice"), sc[t["id"]]["A"].get(n, float("nan")),
                                     sc[t["id"]]["B"].get(n, float("nan"))) for t in ts]) for n in names}
    return {"by_pair_type": pt, "cost": cost, "anchor": anchor, "n_anchor": len(anchors),
            "twin_by_family": {k: {"n": len(v), "mean": _mean(v)} for k, v in sorted(twin_by_group.items())},
            "agree_by_type": agree}


def fmt_extras(v4: dict) -> str:
    L = ["\nv4: pair-type table (regular + repeat trials; tie rate = share of answered trials called too close):"]
    L.append(f"  {'type':<14}{'trials':>7}{'A':>4}{'B':>4}{'tie':>4}{'tie rate':>10}{'mean 1-5':>10}{'|A-B| gap':>11}"
             f"{'chose twin':>12}{'chose move':>12}")
    f = lambda v, p="{:.2f}": "-" if v is None else p.format(v)
    for typ, d in sorted(v4["by_pair_type"].items()):
        L.append(f"  {typ:<14}{d['n']:>7}{d['A']:>4}{d['B']:>4}{d['tie']:>4}{f(d['tie_rate'], '{:.0%}'):>10}"
                 f"{f(d['mean_rating']):>10}{f(d['mean_gap']):>11}"
                 f"{d['pick_twin'] if typ == 'twin_vs_move' else '-':>12}{d['pick_move'] if typ == 'twin_vs_move' else '-':>12}")
    L.append(f"\nWhat each kind of change costs in rating points (1-5; regular trials; anchor = mean rating of the "
             f"exact target copy in the sanity trials = {f(v4['anchor'])}, n={v4['n_anchor']}):")
    L.append(f"  {'candidate':<26}{'n':>4}{'mean':>7}{'sd':>6}{'cost vs 5':>11}{'cost vs anchor':>16}")
    for k, d in v4["cost"].items():
        L.append(f"  {k:<26}{d['n']:>4}{d['mean']:>7.2f}{f(d['sd']):>6}{d['cost_vs_5']:>11.2f}{f(d['cost_vs_anchor']):>16}")
    L.append("\nTwin-swap rating by confusable group (mean 1-5):")
    for k, d in v4["twin_by_family"].items():
        L.append(f"  {k:<44}{d['mean']:.2f}  (n={d['n']})")
    L.append("\nAgreement of each score with the human's choice, by pair type (agree/decided):")
    typs = list(v4["agree_by_type"])
    L.append(f"  {'score':<20}" + "".join(f"{t:>16}" for t in typs))
    for n in next(iter(v4["agree_by_type"].values())):
        L.append(f"  {n:<20}" + "".join(
            f"{(str(v4['agree_by_type'][t][n]['k']) + '/' + str(v4['agree_by_type'][t][n]['n'])) if v4['agree_by_type'][t][n]['n'] else '-':>16}"
            for t in typs))
    return "\n".join(L)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="human-ratings-v4")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    f, k = build_session(a.name, seed=a.seed)
    print(f"page folder {f}, key {k}")
