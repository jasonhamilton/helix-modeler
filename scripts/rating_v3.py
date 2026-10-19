"""Human rating session v3 (T170): "Hard pairs" (the human's choice, DECISIONS 2026-10-18).

Why v3: in v2 every existing score agreed with the human on 72-100% of the easy pairs and all the intervals
overlapped, so v2 could not tell the scores apart. v3 is made of trials that DISCRIMINATE:

  audio       only the human's own dry takes, 7 s excerpts of real playing (not the v2 windows where possible)
  targets     10: a hidden random single Helix drive setting each (TS, clean boost, Big Muff, DS-1, RAT, Klon, fuzz)
  pool        per target about 14 renders of the SAME excerpt: single-knob steps, near-twin models at matched
              settings, same-class models, different-class models at similar gain
  selection   every existing score (the v2 report list) is computed on every pool candidate against its target;
              pairs are chosen where the scores SPLIT (>= 30% of the scores on each side), plus some near-threshold
              pairs where they agree but the difference is small. Algorithm and the per-pair split are in the key.
  trials      40 regular + 3 sanity + 3 retests (sides swapped), -23 LUFS with one shared peak guard
  questions   A closer / B closer / too close, and 1-5 closeness ratings for BOTH candidates (prominent buttons)

Nothing here judges audio or admits a score: the human's answers are the data. Song audio is not used at all.
Used through scripts/ab_session.py (rating-build-v3, play, rating-score).
"""
from __future__ import annotations

import html
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

SR = RS.SR
EXCERPT_S = 7.0
LEAD_S = R2.LEAD_S
TAKE_PLAN = [1, 1, 1, 2, 2, 2, 3, 3, 3, 3]           # one excerpt per target (take numbers)
DRIVE_RANGE, TONE_RANGE = R2.DRIVE_RANGE, R2.TONE_RANGE
DRIVE_DELTAS = [-0.2, -0.1, -0.05, 0.05, 0.1, 0.2]
TONE_DELTAS = [-0.2, -0.1, 0.1, 0.2]
KNOB_LO, KNOB_HI = 0.02, 0.98
MIN_SHARE = 0.30                 # a disputed pair needs at least this share of the scores on each side
AGREE_MAX_SHARE = 0.15           # a near-threshold pair: at most this share on the minority side
N_DISPUTED, N_AGREE, N_RETESTS = 32, 8, 3
PER_TARGET, PER_TARGET_MAX = 4, 5
USE_CAP = 2                      # a candidate appears in at most this many chosen pairs of its target
NOISE_DB = -60.0
SPLIT_EXCLUDE = frozenset({"mrstft_v2+level_db"})   # = mrstft_v2 + 1e-7 * level_db: a copy of mrstft_v2 for voting
QUOTAS = {("disputed", "knob"): 12, ("disputed", "twin"): 10, ("disputed", "class"): 10,
          ("agree", "knob"): 3, ("agree", "twin"): 3, ("agree", "class"): 2}

SELECTION_RULE = (
    "For each target, all pairs (i, j) of pool candidates. Every score of the v2 report (except "
    "mrstft_v2+level_db, which is mrstft_v2 plus 1e-7 times level_db and would double-count it) votes for the "
    "candidate it ranks closer (lower distance); NaN and exact ties abstain. nA/nB = votes for each side, "
    "split = 4*pA*pB with pA = nA/(nA+nB) (1 = even split, 0 = unanimous), min_share = min(pA, pB). "
    "DISPUTED: min_share >= 0.30. AGREE (near-threshold): min_share <= 0.15 and the median over scores of "
    "|difference| / (that score's standard deviation across the target's pool) lies in the smallest fifth of "
    "such pairs. Pair type: 'class' if either candidate is a same-class or different-class model, else 'twin' if "
    "either is a near-twin model, else 'knob' (both single-knob steps of the target's own model). Greedy fill of "
    "per-(kind, type) quotas, round-robin over the least-used targets, choosing at random among the three best "
    "split candidates; a candidate is used at most twice per target; two pairs sharing a candidate whose other "
    "candidates are the same knob moved the same way are near-duplicates and never both chosen. Slots a quota "
    "could not fill are filled with the best remaining pair (marked relaxed).")

# model -> (drive knob, [tone knobs]); Level/Output/Volume is never moved (loudness is matched afterwards)
KNOBS3 = dict(R2.KNOBS)
KNOBS3.update({
    "HD2_DM4Screamer": ("Drive", ["Tone"]), "HD2_DistStuporOD": ("Drive", ["Tone"]),
    "HD2_DistTopSecretOD": ("Gain", []), "HD2_DistCompulsiveDrive": ("Gain", ["Tone"]),
    "HD2_DM4ClassicDistortion": ("Drive", ["Treble"]), "HD2_DM4FuzzPi": ("Drive", ["Treble"]),
    "HD2_DistBallisticFuzz": ("Sustain", ["Tone"]), "HD2_DistPocketFuzz": ("Drive", []),
    "HD2_DistValveDriver": ("Gain", ["Treble"]), "HD2_DM4JumboFuzz": ("Drive", ["Treble"]),
    "HD2_DM4FacialFuzz": ("Drive", ["Treble"]), "HD2_DM4Overdrive": ("Drive", ["Treble"]),
    "HD2_DistAlpacaRouge": ("Drive", ["HiCut"]),
})
D = "HD2_Dist"
# label, hidden-target model, twins (same confusable group), same-class models, different-class models
TARGETS = [
    ("TS-type (Scream 808)", D + "Scream808",
     [D + "Pillars", D + "DhyanaDrive", "HD2_DM4Screamer"], [D + "StuporOD", D + "CompulsiveDrive"],
     [D + "DeezOneVintage", D + "VerminDist"]),
    ("TS-type (Dhyana Drive)", D + "DhyanaDrive",
     [D + "Scream808", D + "Pillars", "HD2_DM4Screamer"], [D + "TopSecretOD", D + "PrizeDrive"],
     [D + "HedgehogD9", D + "DarkDoveFuzz"]),
    ("Klon-type (Minotaur)", D + "Minotaur",
     [D + "Teemah", D + "HeirApparent", D + "PrizeDrive"], [D + "Scream808", D + "ValveDriver"],
     [D + "KinkyBoost", D + "DeezOneVintage"]),
    ("clean boost (Kinky Boost)", D + "KinkyBoost",
     [D + "VitalBoost", D + "DerangedMaster"], [D + "Minotaur", D + "AlpacaRouge"],
     [D + "Scream808", D + "HedgehogD9"]),
    ("Big Muff (Triangle Fuzz)", D + "TriangleFuzz",
     [D + "DarkDoveFuzz", D + "RamsHead", "HD2_DM4FuzzPi"], [D + "BallisticFuzz", D + "PocketFuzz"],
     [D + "DeezOneVintage", D + "VerminDist"]),
    ("Big Muff (Dark Dove Fuzz)", D + "DarkDoveFuzz",
     [D + "TriangleFuzz", D + "RamsHead", D + "BallisticFuzz"], ["HD2_DM4FuzzPi", "HD2_DM4JumboFuzz"],
     [D + "Scream808", D + "HedgehogD9"]),
    ("DS-1 (Deez One Vintage)", D + "DeezOneVintage",
     [D + "DeezOneMod", D + "HedgehogD9"], ["HD2_DM4ClassicDistortion", D + "VerminDist"],
     [D + "TriangleFuzz", D + "Scream808"]),
    ("RAT (Vermin Dist)", D + "VerminDist",
     [D + "RatatouilleDist", "HD2_DM4ClassicDistortion"], [D + "DeezOneVintage", D + "HedgehogD9"],
     [D + "Scream808", D + "TriangleFuzz"]),
    ("Fuzz Face (Arbitrator Fuzz)", D + "ArbitratorFuzz",
     ["HD2_DM4FacialFuzz", "HD2_DM4JumboFuzz"], [D + "WringerFuzz", D + "PocketFuzz"],
     [D + "Scream808", D + "DeezOneVintage"]),
    ("DS-1 / SD-9 (Hedgehog D9)", D + "HedgehogD9",
     [D + "DeezOneVintage", D + "DeezOneMod"], [D + "VerminDist", "HD2_DM4Overdrive"],
     [D + "TriangleFuzz", D + "Minotaur"]),
]
EXCLUDED = R2.EXCLUDED


def all_models() -> set[str]:
    out = set()
    for _, tm, *groups in TARGETS:
        out.add(tm)
        for g in groups:
            out |= set(g)
    return out


# ---------------------------------------------------------------- pool of candidates

def _r2(v: float) -> float:
    return round(float(v), 2)


def random_setting(model: str, rng: np.random.Generator) -> dict[str, float]:
    drive, tones = KNOBS3[model]
    s = {drive: _r2(rng.uniform(*DRIVE_RANGE))}
    for t in tones:
        s[t] = _r2(rng.uniform(*TONE_RANGE))
    return s


def transfer_setting(src_model: str, src: dict[str, float], dst_model: str, defaults: dict[str, float]) -> dict[str, float]:
    """The same drive amount on another model (matched setting); tone knobs carried over by position, else default."""
    sd, st = KNOBS3[src_model]
    dd, dt_ = KNOBS3[dst_model]
    out = {dd: _r2(min(0.95, max(0.1, src[sd])))}
    for i, k in enumerate(dt_):
        out[k] = _r2(src[st[i]]) if i < len(st) else _r2(defaults.get(k, 0.5))
    return out


def make_pool(spec: tuple, rng: np.random.Generator, defaults: dict[str, dict[str, float]] | None = None,
              n_knob: int = 7) -> dict:
    """Hidden target setting and the candidate pool for one target. Candidate kinds:
    knob (one knob of the target's own model moved by a small step), twin (near-twin model, matched setting),
    same (another model of the same class), diff (different class, similar gain)."""
    defaults = defaults or {}
    label, tmodel, twins, sames, diffs = spec
    target = random_setting(tmodel, rng)
    drive, tones = KNOBS3[tmodel]
    steps = []
    for d in DRIVE_DELTAS:
        if KNOB_LO <= target[drive] + d <= KNOB_HI:
            steps.append((drive, d))
    tone_steps = []
    if tones:
        for d in TONE_DELTAS:
            k = tones[int(rng.integers(0, len(tones)))]
            if KNOB_LO <= target[k] + d <= KNOB_HI:
                tone_steps.append((k, d))
    n_tone = min(3, len(tone_steps)) if tones else 0
    n_drive = min(len(steps), n_knob - n_tone)
    # keep one of the two 0.05 steps, not both, when there are plenty of steps
    if n_drive < n_knob - n_tone:              # fewer valid steps than wanted: add the 0.15 steps
        steps += [(drive, d) for d in (-0.15, 0.15) if KNOB_LO <= target[drive] + d <= KNOB_HI]
        n_drive = min(len(steps), n_knob - n_tone)
    pick_d = [steps[i] for i in sorted(rng.permutation(len(steps))[:n_drive])]
    pick_t = [tone_steps[i] for i in sorted(rng.permutation(len(tone_steps))[:n_tone])] if n_tone else []
    cands = []
    for k, d in pick_d + pick_t:
        params = dict(target)
        params[k] = _r2(min(KNOB_HI, max(KNOB_LO, params[k] + d)))
        cands.append({"kind": "knob", "model": tmodel, "params": params, "knob": k, "delta": d,
                      "desc": f"{k} {d:+.2f}"})
    for kind, models, n in (("twin", twins, 3), ("same", sames, 2), ("diff", diffs, 2)):
        for m in [models[i] for i in rng.permutation(len(models))][:n]:
            cands.append({"kind": kind, "model": m, "params": transfer_setting(tmodel, target, m, defaults.get(m, {})),
                          "knob": None, "delta": None, "desc": "matched setting"})
    for i, c in enumerate(cands):
        c["cid"] = f"c{i + 1}"
    return {"label": label, "model": tmodel, "target": target, "cands": cands}


def cand_name(c: dict) -> str:
    kv = ",".join(f"{k}={v:g}" for k, v in c["params"].items())
    return f"{c['kind']}:{c['model'].replace('HD2_Dist', '').replace('HD2_', '')}({kv}) [{c['desc']}]"


# ---------------------------------------------------------------- excerpts

def pick_windows_avoid(x: np.ndarray, k: int, n: int, avoid: list[int], hop_s: float = 0.5) -> list[tuple[int, float]]:
    """Like rating_v2.pick_windows but never overlapping the `avoid` starts (windows used before)."""
    hop = int(hop_s * SR)
    cand = sorted(((R2.window_score(x, s, n), s) for s in range(0, len(x) - n + 1, hop)), reverse=True)
    out: list[tuple[int, float]] = []
    for sc, s in cand:
        if all(abs(s - o) >= n for o in avoid) and all(abs(s - o) >= n for o, _ in out):
            out.append((s, sc))
        if len(out) == k:
            break
    return out


def v2_windows(key_root: Path = RS.KEY_DIR, name: str = "human-ratings-v2") -> dict[int, list[int]]:
    """Take number -> sample starts of the v2 target excerpts (empty when the v2 key is not there)."""
    p = key_root / f"{name}.key.json"
    out: dict[int, list[int]] = {}
    if p.exists():
        for t in json.loads(p.read_text())["targets"].values():
            out.setdefault(int(t["take"]), []).append(round(t["excerpt_start_s"] * SR))
    return out


def choose_excerpts(takes_dir: Path, pattern: str, plan, n_s: float, rng, avoid: dict[int, list[int]]) -> list[dict]:
    n = int(n_s * SR)
    out = []
    reused = 0
    for take in sorted(set(plan)):
        p = takes_dir / pattern.format(n=take)
        x = RS.read_mono(p)
        want = plan.count(take)
        got = pick_windows_avoid(x, want, n, avoid.get(take, []))
        if len(got) < want:                       # not enough fresh windows: top up with any non-overlapping ones
            reused += want - len(got)
            more = pick_windows_avoid(x, want, n, [s for s, _ in got])
            got = (got + [g for g in more if g not in got])[:want]
        for s, sc in got:
            out.append({"take": take, "start": s, "path": str(p), "score": sc,
                        "fresh": all(abs(s - o) >= n for o in avoid.get(take, []))})
    if len(out) < len(plan):
        raise SystemExit(f"only {len(out)} usable excerpts for {len(plan)} targets")
    return [out[i] for i in rng.permutation(len(out))]


# ---------------------------------------------------------------- pair selection (the key step)

def split_names(score_dicts: list[dict]) -> list[str]:
    names = [n for n in RS.SCORE_ORDER if n not in SPLIT_EXCLUDE and any(n in s for s in score_dicts)]
    return names


def pair_type(ki: str, kj: str) -> str:
    if "same" in (ki, kj) or "diff" in (ki, kj):
        return "class"
    if "twin" in (ki, kj):
        return "twin"
    return "knob"


def pair_stats(si: dict, sj: dict, names: list[str], scale: dict[str, float]) -> dict:
    """Votes of the scores on one pair (i, j): 'i' = the score ranks i closer, 'j', or None (abstains)."""
    votes: dict[str, str | None] = {}
    z = []
    for n in names:
        a, b = si.get(n, float("nan")), sj.get(n, float("nan"))
        if not (np.isfinite(a) and np.isfinite(b)) or a == b:
            votes[n] = None
            continue
        votes[n] = "i" if a < b else "j"
        if scale.get(n, 0) > 0:
            z.append(abs(a - b) / scale[n])
    ni, nj = sum(v == "i" for v in votes.values()), sum(v == "j" for v in votes.values())
    tot = ni + nj
    pi = ni / tot if tot else 0.5
    return {"votes": votes, "n_i": ni, "n_j": nj, "split": 4 * pi * (1 - pi), "min_share": min(pi, 1 - pi),
            "z_median": float(np.median(z)) if z else float("nan"), "n_voting": tot}


def pool_scales(scores: list[dict], names: list[str]) -> dict[str, float]:
    out = {}
    for n in names:
        v = np.array([s.get(n, float("nan")) for s in scores], float)
        v = v[np.isfinite(v)]
        out[n] = float(v.std()) if len(v) > 1 else 0.0
    return out


def _close(a: dict, b: dict) -> bool:
    """Two candidates that would make near-duplicate pairs: the same single-knob move, same direction."""
    if a["kind"] == "knob" and b["kind"] == "knob":
        return a["knob"] == b["knob"] and a["delta"] * b["delta"] > 0
    return a["model"] == b["model"] and a["kind"] == b["kind"] and a["kind"] != "knob"


def near_dup(pa: tuple[dict, dict], pb: tuple[dict, dict]) -> bool:
    ids_a, ids_b = {pa[0]["cid"], pa[1]["cid"]}, {pb[0]["cid"], pb[1]["cid"]}
    if ids_a == ids_b:
        return True
    shared = ids_a & ids_b
    if len(shared) != 1:
        return False
    oa = pa[0] if pa[0]["cid"] not in shared else pa[1]
    ob = pb[0] if pb[0]["cid"] not in shared else pb[1]
    return _close(oa, ob)


def all_pairs(pools: list[dict], names_fn=split_names) -> list[dict]:
    """Every pair of every target with its split statistics. pools[t] = {"cands": [...], "scores": [...]}."""
    rows = []
    for t, p in enumerate(pools):
        names = names_fn(p["scores"])
        scale = pool_scales(p["scores"], names)
        c = p["cands"]
        for i in range(len(c)):
            for j in range(i + 1, len(c)):
                st = pair_stats(p["scores"][i], p["scores"][j], names, scale)
                rows.append({"t": t, "i": i, "j": j, "type": pair_type(c[i]["kind"], c[j]["kind"]), **st})
    return rows


def select_pairs(pools: list[dict], rng: np.random.Generator, n_disputed: int = N_DISPUTED, n_agree: int = N_AGREE,
                 quotas: dict | None = None, per_target: int = PER_TARGET, per_target_max: int = PER_TARGET_MAX,
                 min_share: float = MIN_SHARE, agree_max: float = AGREE_MAX_SHARE, use_cap: int = USE_CAP) -> list[dict]:
    """The disagreement criterion. Returns chosen rows (see all_pairs) with "kind" in disputed|agree|relaxed."""
    if quotas is None:
        quotas = dict(QUOTAS)
        # rescale when a caller asks for a different number of trials
        tot = sum(quotas.values())
        want = n_disputed + n_agree
        if want != tot:
            f = want / tot
            quotas = {k: max(0, round(v * f)) for k, v in quotas.items()}
    rows = all_pairs(pools)
    for r in rows:
        r["kind"] = None
    by_t: dict[int, list[dict]] = {}
    for r in rows:
        by_t.setdefault(r["t"], []).append(r)
    # near-threshold candidates: smallest fifth of z_median among the (nearly) unanimous pairs of the target
    agree_ok: set[int] = set()
    for t, rs in by_t.items():
        un = [r for r in rs if r["min_share"] <= agree_max and np.isfinite(r["z_median"]) and r["n_voting"] >= 5]
        if un:
            cut = sorted(r["z_median"] for r in un)[max(0, math.ceil(0.2 * len(un)) - 1)]
            agree_ok |= {id(r) for r in un if r["z_median"] <= cut}
    chosen: list[dict] = []
    cnt = {t: 0 for t in by_t}
    use: dict[tuple[int, int], int] = {}

    def valid(r, kind, typ, cap):
        if id(r) in {id(c) for c in chosen} or cnt[r["t"]] >= cap:
            return False
        if typ is not None and r["type"] != typ:
            return False
        if kind == "disputed" and r["min_share"] < min_share:
            return False
        if kind == "agree" and id(r) not in agree_ok:
            return False
        if use.get((r["t"], r["i"]), 0) >= use_cap or use.get((r["t"], r["j"]), 0) >= use_cap:
            return False
        c = pools[r["t"]]["cands"]
        pr = (c[r["i"]], c[r["j"]])
        for o in chosen:
            if o["t"] == r["t"] and near_dup(pr, (c[o["i"]], c[o["j"]])):
                return False
        return True

    def take(r, kind):
        r["kind"] = kind
        chosen.append(r)
        cnt[r["t"]] += 1
        for k in ("i", "j"):
            use[(r["t"], r[k])] = use.get((r["t"], r[k]), 0) + 1

    slots = [k for k, v in quotas.items() for _ in range(v)]
    slots = [slots[i] for i in rng.permutation(len(slots))]
    left = []
    for kind, typ in slots:
        order = sorted(by_t, key=lambda t: (cnt[t], rng.random()))
        done = False
        for t in order:
            if cnt[t] >= per_target:
                continue
            cand = [r for r in by_t[t] if valid(r, kind, typ, per_target)]
            if not cand:
                continue
            key = (lambda r: -r["split"]) if kind == "disputed" else (lambda r: r["z_median"])
            cand.sort(key=key)
            take(cand[int(rng.integers(0, min(3, len(cand))))], kind)
            done = True
            break
        if not done:
            left.append((kind, typ))
    for kind, typ in left:                 # fill: same kind, any type, up to the per-target maximum
        for t in sorted(by_t, key=lambda t: (cnt[t], rng.random())):
            cand = [r for r in by_t[t] if valid(r, kind, None, per_target_max)]
            if cand:
                cand.sort(key=(lambda r: -r["split"]) if kind == "disputed" else (lambda r: r["z_median"]))
                take(cand[0], kind)
                break
        else:
            for t in sorted(by_t, key=lambda t: (cnt[t], rng.random())):     # last resort: best split of any kind
                cand = [r for r in by_t[t] if valid(r, "relaxed", None, per_target_max)]
                if cand:
                    cand.sort(key=lambda r: -r["split"])
                    take(cand[0], "relaxed")
                    break
    return chosen


# ---------------------------------------------------------------- scoring the pool (all v2 scores)

class PoolScorer(RS.Scorer):
    """The v2 scores on in-memory clips (target, candidate) instead of the files of a built session."""

    def __init__(self, clips: dict, use_embed: bool = True, log=print):
        from tonematch.metrics import LossKit, afxrep
        from tonematch.metrics import nodi_candidates as N

        self.N, self.kit, self.log = N, LossKit(), log
        self.use_embed = use_embed and afxrep.available()
        self.afx = afxrep
        self.clips = clips
        cache: dict[int, tuple] = {}
        feats = {}
        for k, v in clips.items():
            if id(v) not in cache:
                cache[id(v)] = (N.texmod_features(v), N.frame_features(v), N.presence_vector(v))
            feats[k] = cache[id(v)]
        self.feats = feats
        uniq = list(cache.values())
        self.scales = N.fit_scales([f[0] for f in uniq], [f[1] for f in uniq], [f[2] for f in uniq])


def real_pool_scorer(use_embed: bool = True, log=print):
    """pool_scorer(entries) -> list of score dicts. entry = {id, T, C, t_db, c_db, di}."""
    def run(entries: list[dict]) -> list[dict]:
        clips = {}
        for e in entries:
            clips[(e["id"], "T")] = e["T"]
            clips[(e["id"], "A")] = e["C"]
        sc = PoolScorer(clips, use_embed, log)
        out = []
        for i, e in enumerate(entries):
            if i % 10 == 0:
                log(f"  scoring pool {i + 1}/{len(entries)}")
            out.append(sc.one({"id": e["id"], "A": {"orig_db": e["c_db"]}, "target_orig_db": e["t_db"], "di": e["di"]}, "A"))
        return out
    return run


# ---------------------------------------------------------------- the page (ratings prominent)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:15px system-ui;max-width:820px;margin:2em auto;padding:0 1em;color:#222}
.t{border:1px solid #bbb;border-radius:6px;padding:.8em 1em;margin:1em 0}.t.done{border-color:#4a8}
.t h3{margin:.1em 0 .4em}.row{margin:.35em 0}.row b{display:inline-block;width:5.5em}
audio{width:calc(100% - 6em);vertical-align:middle}
button{margin:.15em;padding:.35em .8em;font:inherit;cursor:pointer}button.on{background:#222;color:#fff}
.rate{margin:.5em 0;padding:.5em .7em;background:#f3f6fb;border-radius:6px}
.rate .lab{display:inline-block;width:11em;font-weight:600}
.rate button.r{width:3em;height:2.4em;font-size:17px;border:2px solid #7a93b8;border-radius:6px;background:#fff}
.rate button.r.on{background:#1d4e9e;border-color:#1d4e9e;color:#fff}
.ends{color:#666;font-size:12px;margin:0 0 0 11em}
textarea{width:100%}#bar{position:sticky;top:0;background:#fff;padding:.4em 0;border-bottom:1px solid #ccc}
#out{width:100%;height:6em}.hint{color:#666;font-size:13px}</style></head><body>
<div id="bar"><b>__TITLE__</b> &nbsp; chosen <span id="cnt">0</span> / <span id="tot"></span>
 &nbsp; rated <span id="rcnt">0</span> / <span id="rtot"></span>
 &nbsp; <button id="dl">Download answers (JSON)</button> <span id="msg"></span></div>
<p>__NOTE__</p>
<p>For each trial, listen to the <b>Target</b>, then to <b>A</b> and <b>B</b> (as often as you like; the
loudness of all three is matched). Many pairs are close on purpose, so a "too close to call" is a fine answer.
Judge the tone of the guitar (the distortion and the overall sound), not which you like more.</p>
<p><b>Please rate both A and B</b> from 1 (not close at all) to 5 (indistinguishable from the Target): one click each.
The ratings are what tell the measures apart, so they help a lot, but you can skip any of them.
Answers save in this browser as you go. When done, press <b>Download answers</b> and save the file as
<code>reports/local/ratings/__SESSION__.answers.json</code> in the project.</p>
<div id="trials"></div>
<h2>Finish</h2><p><button id="cp">Copy answers as text</button> <span class="hint">(if the download is blocked)</span></p>
<textarea id="out" readonly></textarea>
<script>
const SESSION=__SESSION_JS__, TRIALS=__TRIALS__, KEY="rating-"+SESSION;
let ans=JSON.parse(localStorage.getItem(KEY)||"{}");
const save=()=>localStorage.setItem(KEY,JSON.stringify(ans));
const slot=id=>ans[id]||(ans[id]={});
document.getElementById("tot").textContent=TRIALS.length;
document.getElementById("rtot").textContent=TRIALS.length*2;
function count(){document.getElementById("cnt").textContent=TRIALS.filter(t=>(ans[t.id]||{}).choice).length;
 let r=0;TRIALS.forEach(t=>{const s=ans[t.id]||{};if(s.rating_a)r++;if(s.rating_b)r++;});
 document.getElementById("rcnt").textContent=r;}
function pauseOthers(e){document.querySelectorAll("audio").forEach(a=>{if(a!==e.target)a.pause();});}
function ratingRow(s,field,label){const r=document.createElement("div");r.className="rate";
 const l=document.createElement("span");l.className="lab";l.textContent=label;r.appendChild(l);
 [1,2,3,4,5].forEach(i=>{const b=document.createElement("button");b.className="r"+(s[field]===i?" on":"");
  b.textContent=i;b.dataset.rating=field+":"+i;
  b.onclick=()=>{s[field]=s[field]===i?null:i;save();render();};r.appendChild(b);});
 return r;}
function render(){const root=document.getElementById("trials");root.innerHTML="";
 TRIALS.forEach(t=>{const d=document.createElement("div");const s=slot(t.id);
  d.className="t"+(s.choice?" done":"");
  let h="<h3>Trial "+t.n+" of "+TRIALS.length+"</h3>";
  for(const [lab,k] of [["Target","T"],["A","A"],["B","B"]])
   h+="<div class='row'><b>"+lab+"</b><audio controls preload='auto' src='"+t.files[k]+"'></audio></div>";
  d.innerHTML=h;d.querySelectorAll("audio").forEach(a=>a.addEventListener("play",pauseOthers));
  d.appendChild(ratingRow(s,"rating_a","How close is A?"));
  d.appendChild(ratingRow(s,"rating_b","How close is B?"));
  const e=document.createElement("div");e.className="ends";e.textContent="1 = not close at all ... 5 = indistinguishable from the Target";
  d.appendChild(e);
  const p=document.createElement("div");p.className="row";
  p.append("Which is closer to the Target? ");
  [["A","A is closer"],["B","B is closer"],["tie","Too close to call"]].forEach(([v,l])=>{
   const b=document.createElement("button");b.textContent=l;b.className=s.choice===v?"on":"";b.dataset.choice=v;
   b.onclick=()=>{s.choice=s.choice===v?null:v;save();render();};p.appendChild(b);});
  d.appendChild(p);
  const n=document.createElement("textarea");n.rows=1;n.placeholder="notes (optional)";n.value=s.notes||"";
  n.oninput=()=>{s.notes=n.value;save();};d.appendChild(n);root.appendChild(d);});count();}
function payload(){return JSON.stringify({session:SESSION,saved_at:new Date().toISOString(),
 answers:TRIALS.map(t=>({trial:t.id,n:t.n,choice:(ans[t.id]||{}).choice??null,rating_a:(ans[t.id]||{}).rating_a??null,
 rating_b:(ans[t.id]||{}).rating_b??null,notes:(ans[t.id]||{}).notes||""}))},null,1);}
document.getElementById("dl").onclick=()=>{const u=URL.createObjectURL(new Blob([payload()],{type:"application/json"}));
 const a=document.createElement("a");a.href=u;a.download=SESSION+".answers.json";a.click();
 document.getElementById("msg").textContent="downloaded (check your Downloads folder)";};
document.getElementById("cp").onclick=()=>{const s=payload();const o=document.getElementById("out");o.value=s;o.select();
 (navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(()=>document.getElementById("msg").textContent="copied",()=>{});};
render();
</script></body></html>"""


def render_page(name: str, trials: list[dict], note: str = "") -> str:
    return (PAGE.replace("__NOTE__", html.escape(note)).replace("__TITLE__", html.escape(f"Rating session {name}"))
            .replace("__SESSION_JS__", json.dumps(name)).replace("__SESSION__", html.escape(name))
            .replace("__TRIALS__", json.dumps(trials)))


# ---------------------------------------------------------------- build

def _order(plan: list[dict], rng) -> list[int]:
    """Random presentation order; retests after their originals; no two neighbours on the same target if avoidable."""
    order = [int(v) for v in rng.permutation(len(plan))]
    for _ in range(600):
        pos = {s: i for i, s in enumerate(order)}
        bad = [s for s, p in enumerate(plan) if p["kind"] == "repeat" and pos[s] < pos[p["orig"]]]
        adj = [i for i in range(len(order) - 1) if plan[order[i]]["t"] == plan[order[i + 1]]["t"]]
        late = [s for s, p in enumerate(plan) if p["kind"] == "repeat" and pos[s] - pos[p["orig"]] < 6]
        if not bad and not adj and not late:
            break
        if bad:
            s = bad[0]
            i, j = order.index(s), order.index(plan[s]["orig"])
            order[i], order[j] = order[j], order[i]
        elif late:                           # interleaved later: move the retest toward the end half
            s = late[0]
            i = order.index(s)
            j = int(rng.integers(max(i + 1, int(len(order) * 0.5)), len(order)))
            order[i], order[j] = order[j], order[i]
        else:
            i = adj[0]
            j = int(rng.integers(0, len(order)))
            order[i + 1], order[j] = order[j], order[i + 1]
    pos = {s: i for i, s in enumerate(order)}
    if any(p["kind"] == "repeat" and pos[s] < pos[p["orig"]] for s, p in enumerate(plan)):
        raise RuntimeError("could not order the retests after their originals")
    return order


def build_session(name: str, seed: int | None = None, render=None, pool_scorer=None,
                  takes_dir: Path = R2.TAKES_DIR, takes_pattern: str = R2.TAKES_GLOB, take_plan=TAKE_PLAN,
                  out_root: Path = RS.RATINGS_DIR, key_root: Path = RS.KEY_DIR, excerpt_s: float = EXCERPT_S,
                  defaults_fn=None, targets=TARGETS, n_disputed: int = N_DISPUTED, n_agree: int = N_AGREE,
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

    exc = choose_excerpts(takes_dir, takes_pattern, take_plan, excerpt_s, rng, v2_windows(key_root))
    if len(exc) < len(targets):
        raise SystemExit("fewer excerpts than targets")
    defaults = {m: defaults_fn(m) for m in all_models()}
    takes_x: dict[str, np.ndarray] = {}
    tg: list[dict] = []
    try:
        for ti, spec in enumerate(targets):
            e = exc[ti]
            x = takes_x.setdefault(e["path"], RS.read_mono(Path(e["path"])))
            s0 = e["start"]
            lead = min(int(LEAD_S * SR), s0)
            seg = x[s0 - lead:s0 + n_s]
            pool = make_pool(spec, rng, defaults)
            log(f"target {ti + 1}/{len(targets)} {spec[0]}: {len(pool['cands'])} candidates")

            def rend(model, params, seg=seg, lead=lead):
                try:
                    y = np.asarray(render(model, params, seg), np.float32)[lead:lead + n_s]
                except Exception as exc_:  # noqa: BLE001 - a failed render drops the candidate
                    log(f"  render error {model}: {type(exc_).__name__}: {exc_}")
                    return None
                if not np.all(np.isfinite(y)) or RS.rms_db(y) < -80:
                    log(f"  render failed or silent: {model}")
                    return None
                return y

            ty = rend(pool["model"], pool["target"])
            if ty is None:
                raise RuntimeError(f"target render for {spec[0]} failed")
            keep = []
            for c in pool["cands"]:
                y = rend(c["model"], c["params"])
                if y is None or np.array_equal(y, ty):
                    continue
                c["raw"] = y
                keep.append(c)
            if len(keep) < 10:
                raise RuntimeError(f"only {len(keep)} usable candidates for {spec[0]}")
            dry_ex = np.array(x[s0:s0 + n_s], np.float32)
            tg.append({"ti": ti, "label": spec[0], "model": pool["model"], "setting": pool["target"], "excerpt": e,
                       "raw": ty, "cands": keep, "dry": dry_ex})
    finally:
        if close is not None:
            close()

    # loudness: every pool clip at -23 LUFS, ONE shared peak guard (so the scores see exactly what is heard)
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

    # score the pool: every v2 score, every candidate against its target
    entries = [{"id": f"{t['ti']}/{c['cid']}", "T": lev[(t["ti"], "T")], "C": lev[(t["ti"], c["cid"])],
                "t_db": orig_db[(t["ti"], "T")], "c_db": orig_db[(t["ti"], c["cid"])], "di": di_file[t["ti"]]}
               for t in tg for c in t["cands"]]
    pool_scorer = pool_scorer or real_pool_scorer(log=log)
    flat = pool_scorer(entries)
    k = 0
    pools = []
    for t in tg:
        n = len(t["cands"])
        pools.append({"cands": t["cands"], "scores": flat[k:k + n]})
        k += n

    chosen = select_pairs(pools, rng, n_disputed, n_agree)
    names = {t: split_names(pools[t]["scores"]) for t in range(len(tg))}

    # trial plan
    plan: list[dict] = []
    for r in chosen:
        i, j = (r["i"], r["j"]) if rng.random() < 0.5 else (r["j"], r["i"])      # which candidate lands on side A
        plan.append({"kind": "regular", "t": r["t"], "A": tg[r["t"]]["cands"][i]["cid"], "B": tg[r["t"]]["cands"][j]["cid"],
                     "row": r, "a_is_i": i == r["i"]})
    # sanity: the target copy against a far candidate (different class, the largest mrstft distance), twice, and a near pair
    san_t = [int(v) for v in rng.permutation(len(tg))[:3]]
    for st in san_t[:2]:
        far = max((c for c in tg[st]["cands"] if c["kind"] in ("diff", "same")),
                  key=lambda c: pools[st]["scores"][tg[st]["cands"].index(c)].get("mrstft_v2", 0.0))
        plan.append({"kind": "sanity_target", "t": st, "A": "copy", "B": far["cid"], "expected": "copy"})
    plan.append({"kind": "sanity_near", "t": san_t[2], "A": "copy", "B": "noise"})
    for p in plan:
        if p["kind"] != "regular" and rng.random() < 0.5:
            p["A"], p["B"] = p["B"], p["A"]
    regs = [i for i, p in enumerate(plan) if p["kind"] == "regular"]
    rep_src: list[int] = []
    disp = [i for i in regs if plan[i]["row"]["kind"] == "disputed"] or regs
    for i in rng.permutation(len(disp)):
        if plan[disp[int(i)]]["t"] not in {plan[j]["t"] for j in rep_src}:
            rep_src.append(disp[int(i)])
        if len(rep_src) == n_retests:
            break
    for j in rep_src:
        o = plan[j]
        plan.append({"kind": "repeat", "t": o["t"], "A": o["B"], "B": o["A"], "orig": j, "row": o["row"],
                     "a_is_i": not o["a_is_i"]})

    order = _order(plan, rng)
    pos = {s: i for i, s in enumerate(order)}

    # noise clip for the near-identical sanity pair (-60 dB white noise on the target, re-levelled)
    noise_clip = {}
    for p in plan:
        if "noise" in (p["A"], p["B"]):
            T = faded[(p["t"], "T")]
            nz = rng.standard_normal(len(T)).astype(np.float32)
            y = T + nz * (float(np.sqrt(np.mean(T ** 2))) * 10 ** (NOISE_DB / 20) / float(np.std(nz)))
            g = gain[(p["t"], "T")]               # same gain as the target: the noise is 60 dB down
            noise_clip[p["t"]] = ((y * 10 ** (g / 20)).astype(np.float32), g, RS.rms_db(y))

    def clip(t: int, ref):
        if ref in ("copy", "copy2"):
            return lev[(t, "T")], gain[(t, "T")], orig_db[(t, "T")]
        if ref == "noise":
            return noise_clip[t][0], noise_clip[t][1], noise_clip[t][2]
        return lev[(t, ref)], gain[(t, ref)], orig_db[(t, ref)]

    def info(t: int, ref) -> dict:
        tt = tg[t]
        if ref in ("copy", "copy2"):
            return {"name": "target copy", "role": "target_copy", "cand_kind": "target", "model": tt["model"],
                    "params": tt["setting"], "cid": None}
        if ref == "noise":
            return {"name": f"target + noise ({NOISE_DB:g} dB)", "role": "near_identical", "cand_kind": "target",
                    "model": tt["model"], "params": tt["setting"], "cid": None}
        c = next(c for c in tt["cands"] if c["cid"] == ref)
        return {"name": cand_name(c), "role": "candidate", "cand_kind": c["kind"], "model": c["model"],
                "params": c["params"], "cid": ref, "knob": c["knob"], "delta": c["delta"]}

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
        for side, ref, d in (("A", p["A"], ai), ("B", p["B"], bi)):
            _, g, odb = clip(p["t"], ref)
            d["orig_db"], d["gain_db"] = odb, g
            d["source"] = f"target {p['t'] + 1} {d['cid'] or 'target'}"
        expected = None
        if p["kind"] == "sanity_target":
            expected = "A" if ai["role"] == "target_copy" else "B"
        sel = None
        if p["kind"] in ("regular", "repeat"):
            r = p["row"]
            vi = r["votes"]
            flip = (lambda v: None if v is None else ("A" if v == "i" else "B")) if p["a_is_i"] else \
                   (lambda v: None if v is None else ("B" if v == "i" else "A"))
            nA, nB = (r["n_i"], r["n_j"]) if p["a_is_i"] else (r["n_j"], r["n_i"])
            sel = {"kind": r["kind"], "pair_type": r["type"], "n_A": nA, "n_B": nB, "split": r["split"],
                   "min_share": r["min_share"], "z_median": r["z_median"],
                   "votes": {n: flip(v) for n, v in vi.items()}}
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": p["kind"], "family": f"target{p['t'] + 1} {t['label']}",
                        "target": p["t"] + 1, "excerpt_start_s": t["excerpt"]["start"] / SR, "excerpt_s": excerpt_s,
                        "A": ai, "B": bi, "files": files, "target_orig_db": orig_db[(p["t"], "T")],
                        "di": di_file[p["t"]], "expected": expected, "selection": sel,
                        "repeat_of": id_of[p["orig"]] if p["kind"] == "repeat" else None,
                        "loudness": {"gain_db": [gain[(p["t"], "T")], clip(p["t"], p["A"])[1], clip(p["t"], p["B"])[1]]}})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": name, "excerpt_s": excerpt_s, "trials": trials}, indent=1))
    note = ("All clips are the same guitar notes; only the Helix drive setting differs. Judge which of A and B has "
            "the tone closer to the Target.")
    (folder / "index.html").write_text(render_page(name, trials, note))
    tinfo = {str(t["ti"] + 1): {"label": t["label"], "model": t["model"], "params": t["setting"], "take": t["excerpt"]["take"],
                                "excerpt_start_s": t["excerpt"]["start"] / SR, "fresh_vs_v2": t["excerpt"].get("fresh"),
                                "orig_db": orig_db[(t["ti"], "T")], "di": di_file[t["ti"]],
                                "pool": [{"cid": c["cid"], "kind": c["kind"], "name": cand_name(c), "scores": s}
                                         for c, s in zip(t["cands"], pools[t["ti"]]["scores"], strict=True)]}
             for t in tg}
    keyf.write_text(json.dumps({
        "session": name, "version": 3, "seed": seed, "peak_guard_db": guard, "target_lufs": RS.TARGET_LUFS,
        "selection_rule": SELECTION_RULE, "min_share": MIN_SHARE, "agree_max_share": AGREE_MAX_SHARE,
        "quotas": {f"{k[0]}/{k[1]}": v for k, v in QUOTAS.items()}, "split_scores": names[0] if names else [],
        "targets": tinfo, "trials": ktrials}, indent=1, default=float))
    return folder, keyf


def folder_mb(*paths: Path) -> float:
    return R2.folder_mb(*paths)


# ---------------------------------------------------------------- analysis (extends the v2 report)

def _decided(a: dict | None) -> bool:
    return a is not None and a.get("choice") in ("A", "B")


def analyse(key: dict, byid: dict, sc: dict, pooled_records: list | None = None) -> dict:
    """The v3 extras. key: the hidden key; byid: trial id -> answer; sc: trial id -> {"A": scores, "B": scores}
    (regular and repeat trials); pooled_records: v2 records [(choice, sa, sb, family, rating_a, rating_b)] or None."""
    regs = [t for t in key["trials"] if t["kind"] in ("regular", "repeat") and t["id"] in sc]
    names = [n for n in RS.SCORE_ORDER if any(n in v["A"] for v in sc.values())]

    def rows_for(ts, n):
        return [(byid.get(t["id"], {}).get("choice"), sc[t["id"]]["A"].get(n, float("nan")),
                 sc[t["id"]]["B"].get(n, float("nan"))) for t in ts]

    def obs_for(ts, n):
        o = []
        for t in ts:
            a = byid.get(t["id"], {})
            o += [(t["family"], sc[t["id"]]["A"].get(n, float("nan")), a.get("rating_a")),
                  (t["family"], sc[t["id"]]["B"].get(n, float("nan")), a.get("rating_b"))]
        return o

    out: dict = {"disputed": {}, "near_threshold": {}, "all": {}}
    groups = {"disputed": [t for t in regs if t["selection"]["kind"] == "disputed"],
              "near_threshold": [t for t in regs if t["selection"]["kind"] == "agree"],
              "all": regs}
    for g, ts in groups.items():
        for n in names:
            r = RS.agreement(rows_for(ts, n))
            r["spearman"] = RS.spearman(obs_for(ts, n))
            out[g][n] = r
    out["n_trials"] = {g: len(ts) for g, ts in groups.items()}
    out["n_decided"] = {g: sum(_decided(byid.get(t["id"])) for t in ts) for g, ts in groups.items()}
    # which scores won each disputed trial
    won = []
    for t in groups["disputed"]:
        a = byid.get(t["id"], {})
        ch = a.get("choice")
        votes = t["selection"]["votes"]
        row = {"n": t["n"], "pair_type": t["selection"]["pair_type"], "choice": ch,
               "split": f"{t['selection']['n_A']}:{t['selection']['n_B']}"}
        if ch in ("A", "B"):
            row["agreed"] = [n for n, v in votes.items() if v == ch]
            row["disagreed"] = [n for n, v in votes.items() if v in ("A", "B") and v != ch]
        won.append(row)
    out["disputed_trials"] = won
    # what the human heard as close, by pair type
    pt: dict[str, dict] = {}
    for t in regs:
        a = byid.get(t["id"], {})
        d = pt.setdefault(t["selection"]["pair_type"], {"n": 0, "A": 0, "B": 0, "tie": 0, "none": 0, "ratings": [], "gaps": []})
        d["n"] += 1
        ch = a.get("choice")
        d[ch if ch in ("A", "B", "tie") else "none"] += 1
        ra, rb = a.get("rating_a"), a.get("rating_b")
        d["ratings"] += [float(x) for x in (ra, rb) if x is not None]
        if ra is not None and rb is not None:
            d["gaps"].append(abs(float(ra) - float(rb)))
    for d in pt.values():
        answered = d["A"] + d["B"] + d["tie"]
        d["tie_rate"] = d["tie"] / answered if answered else None
        d["mean_rating"] = float(np.mean(d["ratings"])) if d["ratings"] else None
        d["share_rated_4_5"] = float(np.mean([r >= 4 for r in d["ratings"]])) if d["ratings"] else None
        d["mean_abs_rating_gap"] = float(np.mean(d["gaps"])) if d["gaps"] else None
        d["n_ratings"] = len(d["ratings"])
        del d["ratings"], d["gaps"]
    out["by_pair_type"] = pt
    # pooled v2 + v3
    if pooled_records is not None:
        pooled = {}
        for n in names:
            rows3 = rows_for(regs, n)
            rows2 = [(c, sa.get(n, float("nan")), sb.get(n, float("nan"))) for c, sa, sb, *_ in pooled_records]
            r2, r3, rp = RS.agreement(rows2), RS.agreement(rows3), RS.agreement(rows2 + rows3)
            obs = obs_for(regs, n)
            for c, sa, sb, fam, ra, rb in pooled_records:
                obs += [("v2 " + fam, sa.get(n, float("nan")), ra), ("v2 " + fam, sb.get(n, float("nan")), rb)]
            rp["spearman"] = RS.spearman(obs)
            pooled[n] = {"v2": r2, "v3": r3, "pooled": rp}
        out["pooled"] = pooled
    return out


def fmt_extras(v3: dict) -> str:
    L = []

    def line(n, r):
        sp = r.get("spearman") or {"rho": None}
        spt = "n/a" if sp["rho"] is None else f"{sp['rho']:+.2f} (n={sp['n']})"
        if r["n"]:
            return (f"  {n:<20}{r['k']:>4}/{r['n']:<4}{r['rate']:>6.0%}  [{r['ci'][0]:.2f},{r['ci'][1]:.2f}]  "
                    f"p={r['p']:.3f}   rho {spt}")
        return f"  {n:<20}{'no data':>10}   rho {spt}"

    nd = v3["n_decided"]
    nt = v3["n_trials"]
    L.append(f"\nv3: agreement on DISPUTED trials only ({nd['disputed']} decided of {nt['disputed']}; "
             "the pairs where the scores split; chance = 50%):")
    for n, r in v3["disputed"].items():
        L.append(line(n, r))
    L.append(f"\nv3: agreement on NEAR-THRESHOLD trials (scores agree, small difference; {nd['near_threshold']} decided of "
             f"{nt['near_threshold']}):")
    for n, r in v3["near_threshold"].items():
        L.append(line(n, r))
    if v3.get("pooled"):
        L.append("\nPooled v2 + v3, all decided regular and repeat trials (agree/decided; Spearman on the pooled ratings):")
        L.append(f"  {'score':<20}{'v2':>10}{'v3':>10}{'pooled':>12}{'rate':>7}  {'95% CI':<14}  rho")
        for n, d in v3["pooled"].items():
            p = d["pooled"]
            sp = p["spearman"]
            spt = "n/a" if sp["rho"] is None else f"{sp['rho']:+.2f}"
            f2 = f"{d['v2']['k']}/{d['v2']['n']}"
            f3 = f"{d['v3']['k']}/{d['v3']['n']}"
            if p["n"]:
                L.append(f"  {n:<20}{f2:>10}{f3:>10}{str(p['k']) + '/' + str(p['n']):>12}{p['rate']:>7.0%}  "
                         f"[{p['ci'][0]:.2f},{p['ci'][1]:.2f}]  {spt}")
    L.append("\nWhat the human heard as close, by pair type (all regular and repeat trials):")
    L.append(f"  {'type':<7}{'trials':>7}{'A':>5}{'B':>5}{'tie':>5}{'tie rate':>10}{'mean 1-5':>10}{'rated 4-5':>11}{'|A-B| gap':>11}")
    for typ, d in sorted(v3["by_pair_type"].items()):
        f = lambda v, p="{:.2f}": "-" if v is None else p.format(v)
        L.append(f"  {typ:<7}{d['n']:>7}{d['A']:>5}{d['B']:>5}{d['tie']:>5}{f(d['tie_rate'], '{:.0%}'):>10}"
                 f"{f(d['mean_rating']):>10}{f(d['share_rated_4_5'], '{:.0%}'):>11}{f(d['mean_abs_rating_gap']):>11}")
    L.append("\nDisputed trials, which scores agreed with the human's choice (split = votes for A:B):")
    for r in v3["disputed_trials"]:
        if "agreed" in r:
            L.append(f"  trial {r['n']:>2} [{r['pair_type']}] split {r['split']:>5}  chose {r['choice']}: "
                     f"{len(r['agreed'])} agreed ({', '.join(r['agreed'])})")
        else:
            L.append(f"  trial {r['n']:>2} [{r['pair_type']}] split {r['split']:>5}  chose {r['choice']}")
    return "\n".join(L)


def load_v2_records(use_embed: bool = True, root: Path = RS.RATINGS_DIR, key_root: Path = RS.KEY_DIR, log=print) -> list | None:
    """Per decided-or-not regular/repeat trial of v2: (choice, scores A, scores B, family, rating_a, rating_b).
    Cached in reports/local/ratings/human-ratings-v2.trial_scores.json. None when v2 has no answers."""
    ans = RS.find_answers("human-ratings-v2", root)
    key_p = key_root / "human-ratings-v2.key.json"
    if ans is None or not key_p.exists():
        return None
    cache = root / "human-ratings-v2.trial_scores.json"
    key = json.loads(key_p.read_text())
    byid = {a["trial"]: a for a in json.loads(Path(ans).read_text())["answers"]}
    regs = [t for t in key["trials"] if t["kind"] in ("regular", "repeat")]
    if cache.exists():
        sc = json.loads(cache.read_text())
    else:
        scorer = RS.Scorer(root / "human-ratings-v2", key, use_embed=use_embed)
        sc = {}
        for i, t in enumerate(regs):
            log(f"  re-scoring v2 trial {t['n']} ({i + 1}/{len(regs)})")
            sc[t["id"]] = {"A": scorer.one(t, "A"), "B": scorer.one(t, "B")}
        cache.write_text(json.dumps(sc))
    return [(byid.get(t["id"], {}).get("choice"), sc[t["id"]]["A"], sc[t["id"]]["B"], t["family"],
             byid.get(t["id"], {}).get("rating_a"), byid.get(t["id"], {}).get("rating_b")) for t in regs]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="human-ratings-v3")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    f, k = build_session(a.name, seed=a.seed)
    print(f"page folder {f}, key {k}, {folder_mb(f, RS.KEY_DIR / (a.name + '.di')):.1f} MB")
