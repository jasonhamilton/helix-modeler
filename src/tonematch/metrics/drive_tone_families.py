"""T179 and T180: the tone-knob ends of every two-knob drive, and drive families from the fingerprints.

RESEARCH CODE, measurement only. Approved by the human ("go ahead", DECISIONS 2026-10-18, the five
post-v4 recommendations). Nothing here is an admitted score, an adopted method or a bar. The family
map is measurement-derived; the circuit names from docs/notes/distortion-circuits.md are labels only.

T179 (tone ends):
- `tone_end_settings`: tone CC 0 / 13 / 114 / 127 (0, 0.10, 0.90, 1.0) at Drive CC 64 and 108
  (0.50, 0.85); with step 3's tone 25 / 64 / 102 these make a 7-point tone line at each Drive.
- `response_block` / `step_effect`: the tone knob's effect between two points is the RMS dB
  difference of the fundamental response (100 Hz-10 kHz) at three input levels (-66, -30, 0 dBFS),
  with and without the overall level change.
- `tone_line_summary` / `knob_shape`: how much of the knob's effect happens in [0, 0.1], [0.1, 0.9]
  and [0.9, 1], the effect per 0.1 in each, and a shape label.

T180 (families):
- `model_features`: per model, the level-free card and the block-weighted level-free curves of its
  12 step-3 settings, z-scored.
- `cluster_models` / `stability`: Ward clusters of the model means; stability is the adjusted Rand
  index against the full-data clusters under resampling (settings bootstrap and feature dropout).
- `describe_family`: a name from measured character only.
"""
from __future__ import annotations

import numpy as np

from . import distortion_probe as P
from . import distortion_step3 as S3
from . import drive_fp_mining as DM

# ------------------------------------------------------------------------------------- T179 plan

DRIVE_CC = (64, 108)                       # Drive 0.50 / 0.85
END_CC = (0, 13, 114, 127)                 # tone 0 / 0.10 / 0.90 / 1.0 (the new renders)
STEP3_TONE_CC = (25, 64, 102)              # tone 0.2 / 0.5 / 0.8 (already in dfp-v1)
LINE_CC = (0, 13, 25, 64, 102, 114, 127)   # the full tone line at each Drive
CC_STEPS = 127

# "card not meaningful at quiet inputs" (docs/notes/distortion-step3.md section 6, item 6)
QUIET_UNRELIABLE = ("HD2_DistXenomorphFuzz", "HD2_DistWringerFuzz", "HD2_DistTycoctaviaFuzz",
                    "HD2_DistMegaphone", "HD2_DistIndustrialFuzz", "HD2_DM4JetFuzz")


def tone_end_settings(drive: str, tone: str | None) -> list[tuple[str, dict, str]]:
    """8 (name, {knob: cc}, kind) per two-knob model; none for a one-knob model."""
    if tone is None:
        return []
    return [(S3.setting_name(drive, tone, d, t), {drive: d, tone: t}, "tone_end")
            for d in DRIVE_CC for t in END_CC]


def pos(cc: int) -> float:
    """Normalised knob position Native plays for a CC step (linear, c / 127)."""
    return float(cc) / CC_STEPS


# ------------------------------------------------------------------------------------- T179 effect

EFFECT_LEVELS = (-66.0, -30.0, 0.0)        # input levels (dBFS peak) of the fundamental response
BAND = (100.0, 10000.0)
FLOOR_DB = -120.0                          # output floor: below this a model is treated as silent


def response_block(arr: dict) -> np.ndarray:
    """(levels x frequencies) output level in dB of the fundamental, 100 Hz-10 kHz, floored."""
    f = np.asarray(P.FGRID, float)
    band = (f >= BAND[0]) & (f <= BAND[1])
    lev = list(P.SWEEP_LEVELS)
    M1 = np.asarray(arr["M1"], float)[:, [lev.index(v) for v in EFFECT_LEVELS]].T
    M1 = np.where(np.isfinite(M1), M1, FLOOR_DB)
    return np.maximum(M1[:, band], FLOOR_DB)


def step_effect(a: np.ndarray, b: np.ndarray) -> dict:
    """Difference of two response blocks: `rms_db` (all of it), `shape_db` (each level's mean level
    change removed), `level_db` (the mean level change, RMS over the input levels), `max_db`."""
    d = np.asarray(b, float) - np.asarray(a, float)
    lv = d.mean(axis=1, keepdims=True)
    return {"rms_db": float(np.sqrt(np.mean(d ** 2))), "shape_db": float(np.sqrt(np.mean((d - lv) ** 2))),
            "level_db": float(np.sqrt(np.mean(lv ** 2))), "max_db": float(np.max(np.abs(d))),
            "per_level_rms_db": [float(np.sqrt(np.mean(r ** 2))) for r in d]}


REGIONS = (("bottom", 0, 13), ("middle", 13, 114), ("top", 114, 127))


def tone_line_summary(blocks: dict[int, np.ndarray], key: str = "rms_db") -> dict:
    """blocks: tone CC -> response block along one Drive line (LINE_CC). The knob's total effect is
    the path length (sum of the step effects); each region's share, its effect per 0.1 of travel,
    and the per-0.1 effect of each of the six steps."""
    ccs = sorted(blocks)
    steps = []
    for c0, c1 in zip(ccs, ccs[1:]):
        e = step_effect(blocks[c0], blocks[c1])
        steps.append({"from_cc": c0, "to_cc": c1, **e, "per_0p1": e[key] / ((pos(c1) - pos(c0)) * 10)})
    total = float(sum(s[key] for s in steps))
    out = {"total_db": total, "steps": steps, "end_to_end": step_effect(blocks[ccs[0]], blocks[ccs[-1]]),
           "key": key}
    for name, lo, hi in REGIONS:
        e = float(sum(s[key] for s in steps if s["from_cc"] >= lo and s["to_cc"] <= hi))
        out[name] = {"effect_db": e, "share": e / total if total > 0 else None,
                     "per_0p1_db": e / ((pos(hi) - pos(lo)) * 10)}
    return out


INERT_DB = 1.0        # a total tone effect under 1 dB RMS (path) is "little effect"
END_RATIO = 2.0       # an end acts at least twice as hard per 0.1 as the middle (or half as hard)
TAPER_RHO = 0.8       # a taper: the per-0.1 effect trends steadily along the knob (rank correlation) ...
TAPER_ENDS = 3.0      # ... and one end acts at least 3x as hard as the other
HALVES_RATIO = 2.0    # "linear" also needs the middle's halves (0.2-0.5, 0.5-0.8) within 2x of each other


def knob_shape(per_0p1_bottom: float, per_0p1_middle: float, per_0p1_top: float,
               per_0p1_steps: list[float], total_db: float) -> dict:
    """Shape label from the effect per 0.1 of travel, checked in this order:

    - little effect: the whole travel moves the response by under INERT_DB (RMS, path);
    - taper: the per-0.1 effect trends steadily along the knob (Spearman >= TAPER_RHO over the six
      steps) and one end acts TAPER_ENDS times as hard as the other; `ends` = the heavy end;
    - end-loaded: an end acts at least END_RATIO times as hard per 0.1 as the middle; `ends` =
      bottom / top / both;
    - middle-loaded: both ends act at most 1/END_RATIO as hard as the middle (the ends are slack);
    - linear: both ends within a factor END_RATIO of the middle and the middle's halves within
      HALVES_RATIO;
    - uneven: anything else (`ends` names a slack end if there is one).
    """
    mid = max(per_0p1_middle, 1e-9)
    rb, rt = per_0p1_bottom / mid, per_0p1_top / mid
    s = list(per_0p1_steps)
    half = (s[3] / max(s[2], 1e-9)) if len(s) == 6 else 1.0
    rho = _spearman(np.arange(len(s)), s) if len(s) >= 3 else 0.0
    out = {"bottom_vs_middle": float(rb), "top_vs_middle": float(rt), "middle_halves_ratio": float(half),
           "trend_rho": float(rho)}
    if total_db < INERT_DB:
        return {**out, "shape": "little effect", "ends": None}
    if abs(rho) >= TAPER_RHO and max(rb, rt) >= TAPER_ENDS * max(min(rb, rt), 1e-9):
        heavy = "top" if rt > rb else "bottom"
        if (heavy == "top") == (rho > 0):
            return {**out, "shape": "taper", "ends": heavy}
    if rb >= END_RATIO or rt >= END_RATIO:
        ends = "both" if (rb >= END_RATIO and rt >= END_RATIO) else ("top" if rt >= END_RATIO else "bottom")
        return {**out, "shape": "end-loaded", "ends": ends}
    if rb <= 1 / END_RATIO and rt <= 1 / END_RATIO:
        return {**out, "shape": "middle-loaded", "ends": "both slack"}
    if 1 / END_RATIO < rb < END_RATIO and 1 / END_RATIO < rt < END_RATIO and \
            1 / HALVES_RATIO < half < HALVES_RATIO:
        return {**out, "shape": "linear", "ends": None}
    slack = "bottom slack" if rb <= 1 / END_RATIO else ("top slack" if rt <= 1 / END_RATIO else None)
    return {**out, "shape": "uneven", "ends": slack}


def _spearman(x, y) -> float:
    from scipy.stats import rankdata

    x, y = np.asarray(x, float), np.asarray(y, float)
    if np.std(y) == 0:
        return 0.0
    return float(np.corrcoef(rankdata(x), rankdata(y))[0, 1])


def model_tone_report(model: str, sem: dict, lines: dict[int, dict[int, np.ndarray]],
                      tone_range=(0.0, 1.0)) -> dict:
    """lines: Drive CC -> {tone CC -> response block}. Both effect keys at each Drive, the shape
    per Drive and from the mean per-0.1 effects of the two Drives (the recommendation)."""
    rep = {"model": model, "drive": sem["drive"], "tone": sem["tone"], "range": [float(tone_range[0]),
           float(tone_range[1])], "by_drive": {}}
    for key in ("rms_db", "shape_db"):
        per = {}
        for d, bl in sorted(lines.items()):
            s = tone_line_summary(bl, key)
            s["shape"] = knob_shape(s["bottom"]["per_0p1_db"], s["middle"]["per_0p1_db"], s["top"]["per_0p1_db"],
                                    [x["per_0p1"] for x in s["steps"]], s["total_db"])
            per[str(d)] = s
        rep["by_drive"][key] = per
    # combined: mean over the Drives of the per-0.1 numbers (primary key rms_db: level included)
    comb = {}
    for key in ("rms_db", "shape_db"):
        per = rep["by_drive"][key]
        mean = lambda f: float(np.mean([f(v) for v in per.values()]))  # noqa: E731
        c = {r: {"per_0p1_db": mean(lambda v, r=r: v[r]["per_0p1_db"]), "share": mean(lambda v, r=r: v[r]["share"] or 0.0)}
             for r in ("bottom", "middle", "top")}
        steps = [float(np.mean([v["steps"][i]["per_0p1"] for v in per.values()])) for i in range(len(LINE_CC) - 1)]
        tot = mean(lambda v: v["total_db"])
        c["total_db"] = tot
        c["steps_per_0p1_db"] = steps
        c["shape"] = knob_shape(c["bottom"]["per_0p1_db"], c["middle"]["per_0p1_db"], c["top"]["per_0p1_db"],
                                steps, tot)
        c["drives_agree"] = len({v["shape"]["shape"] for v in per.values()}) == 1
        comb[key] = c
    rep["combined"] = comb
    rep["shape"] = comb["rms_db"]["shape"]["shape"]
    rep["ends"] = comb["rms_db"]["shape"]["ends"]
    rep["shape_level_free"] = comb["shape_db"]["shape"]["shape"]
    # where the knob acts: the 0-to-1 effect at each input level (equal = after the clipper)
    ee = [rep["by_drive"]["rms_db"][str(d)]["end_to_end"]["per_level_rms_db"] for d in sorted(lines)]
    rep["end_to_end_by_level_db"] = {str(int(lv)): float(np.mean([e[i] for e in ee])) for i, lv in enumerate(EFFECT_LEVELS)}
    return rep


def suggested_tol(entry: dict, tone_tol: float = 0.10, floor: float = 0.02) -> dict:
    """Equal-effect rule (a suggestion for search, not adopted): the approved Tone tolerance
    `tone_tol` is kept in the middle, and each end gets the knob step that moves the response by
    as much as `tone_tol` does in the middle, capped at `tone_tol` and floored at `floor`. The end
    zone is the measured 0.1 at each end."""
    e = entry["effect_per_0p1_db"]
    mid = e["middle"]

    def end(per):
        if per <= 0 or mid <= 0:
            return tone_tol
        return round(float(np.clip(tone_tol * mid / per, floor, tone_tol)), 3)
    return {"tone_tol": tone_tol, "tone_end_tol_bottom": end(e["bottom"]), "tone_end_tol_top": end(e["top"]),
            "tone_end_tol": min(end(e["bottom"]), end(e["top"])), "end_zone": 0.1,
            "rule": f"equal effect: an end step moves the response as much as {tone_tol} does in the middle "
                    f"(capped at {tone_tol}, floor {floor}); suggestion only"}


def search_entry(rep: dict) -> dict:
    """The compact per-knob entry for search (T178): range, effect per 0.1 at each end and in the
    middle (dB RMS, level included; and level-free), shares, shape. Measured, not a tolerance."""
    c, cs = rep["combined"]["rms_db"], rep["combined"]["shape_db"]

    def step_for(db, per):
        return None if per <= 0 else round(min(1.0, 0.1 * db / per), 3)
    return {"range": rep["range"], "units": "knob position 0..1 over the range (Native plays CC/127)",
            "effect_per_0p1_db": {r: round(c[r]["per_0p1_db"], 2) for r in ("bottom", "middle", "top")},
            "effect_per_0p1_db_level_free": {r: round(cs[r]["per_0p1_db"], 2) for r in ("bottom", "middle", "top")},
            "share": {r: round(c[r]["share"], 3) for r in ("bottom", "middle", "top")},
            "total_db": round(c["total_db"], 2),
            "shape": rep["shape"], "ends": rep["ends"], "shape_level_free": rep["shape_level_free"],
            "drives_agree": c["drives_agree"],
            "end_to_end_by_input_level_db": {k: round(v, 2) for k, v in rep["end_to_end_by_level_db"].items()},
            "level_dependent": bool(max(rep["end_to_end_by_level_db"].values())
                                    - min(rep["end_to_end_by_level_db"].values()) > 1.0),
            "knob_step_for_1db": {r: step_for(1.0, c[r]["per_0p1_db"]) for r in ("bottom", "middle", "top")},
            "regions": {"bottom": [0.0, round(pos(13), 4)], "middle": [round(pos(13), 4), round(pos(114), 4)],
                        "top": [round(pos(114), 4), 1.0]}}


# ------------------------------------------------------------------------------------- T180 features

def model_features(recs: list[dict]) -> dict:
    """recs: step-3 records (with "arr", "sc", "card"). Returns per-setting z-scored card (level-free)
    and block-weighted level-free curves, the block names, and the model of each row."""
    models = [r["model"] for r in recs]
    Zc, nc = S3.standardise([S3.card_features(r["card"], level=False) for r in recs])
    blocks = [S3.curve_blocks(r["arr"], r["sc"], level=False) for r in recs]
    Zp, _ = S3.block_matrix(blocks, weighted=True)
    return {"models": models, "card": Zc, "card_names": nc, "curves": Zp}


def combine(Zcard: np.ndarray, Zcurves: np.ndarray, w_card: float = 0.5) -> np.ndarray:
    """Card and curves side by side, each scaled to unit mean squared norm per row, weighted."""
    def unit(Z):
        return Z / np.sqrt(np.mean(np.sum(Z ** 2, axis=1)) + 1e-30)
    return np.concatenate([np.sqrt(w_card) * unit(Zcard), np.sqrt(1 - w_card) * unit(Zcurves)], axis=1)


def model_means(models: list[str], Z: np.ndarray, idx: list[np.ndarray] | None = None) -> tuple[list[str], np.ndarray]:
    ms = sorted(set(models))
    rows = {m: np.nonzero(np.asarray(models) == m)[0] for m in ms}
    if idx is not None:
        rows = {m: rows[m][i] for m, i in zip(ms, idx)}
    return ms, np.array([Z[rows[m]].mean(axis=0) for m in ms])


def cluster_models(M: np.ndarray, k: int) -> np.ndarray:
    cl, _ = DM.cluster(M, k, method="ward")
    return relabel(cl)


def relabel(cl) -> np.ndarray:
    """Cluster ids 1..k in order of first appearance (stable names across runs)."""
    cl = np.asarray(cl)
    order = {}
    for c in cl:
        order.setdefault(int(c), len(order) + 1)
    return np.array([order[int(c)] for c in cl])


def stability(models: list[str], Z: np.ndarray, ks, n_boot: int = 200, seed: int = 0,
              feature_keep: float = 0.8) -> dict:
    """For each k: ARI between the full-data clusters and clusters of (a) model means from a
    bootstrap of each model's settings, (b) model means on a random `feature_keep` share of the
    feature columns. Also the per-model co-assignment rate with its full-data family."""
    rng = np.random.default_rng(seed)
    ms, M = model_means(models, Z)
    counts = [int(np.sum(np.asarray(models) == m)) for m in ms]
    out = {}
    for k in ks:
        ref = cluster_models(M, k)
        same_ref = ref[:, None] == ref[None, :]
        a_set, a_feat, co = [], [], np.zeros(len(ms))
        for b in range(n_boot):
            idx = [rng.integers(0, n, n) for n in counts]
            _, Mb = model_means(models, Z, idx)
            cb = cluster_models(Mb, k)
            a_set.append(DM.ari(ref, cb))
            cols = rng.random(Z.shape[1]) < feature_keep
            cf = cluster_models(M[:, cols], k)
            a_feat.append(DM.ari(ref, cf))
            for c in (cb, cf):
                same = c[:, None] == c[None, :]
                # per model: share of its full-data family mates still with it (Jaccard of the pair sets)
                inter = np.sum(same & same_ref, axis=1)
                union = np.sum(same | same_ref, axis=1)
                co += inter / union
        co /= 2 * n_boot
        out[int(k)] = {"ari_settings_boot": [float(np.mean(a_set)), float(np.percentile(a_set, 5))],
                       "ari_feature_dropout": [float(np.mean(a_feat)), float(np.percentile(a_feat, 5))],
                       "stability": float(np.mean(a_set + a_feat)),
                       "member_stability": {m: float(v) for m, v in zip(ms, co)},
                       "silhouette": silhouette(M, ref),
                       "sizes": sorted(np.bincount(ref)[1:].tolist(), reverse=True)}
    return out


def resample_partitions(models: list[str], Z: np.ndarray, k: int, n: int, rng,
                        feature_keep: float = 0.8) -> np.ndarray:
    """n Ward partitions of the model means at k, each on a bootstrap of every model's settings
    AND a random `feature_keep` share of the feature columns. Returns (n, n_models)."""
    ms = sorted(set(models))
    counts = [int(np.sum(np.asarray(models) == m)) for m in ms]
    out = []
    for _ in range(n):
        idx = [rng.integers(0, c, c) for c in counts]
        _, Mb = model_means(models, Z, idx)
        cols = rng.random(Z.shape[1]) < feature_keep
        out.append(cluster_models(Mb[:, cols], k))
    return np.array(out)


def coassoc(parts: np.ndarray) -> np.ndarray:
    """Share of partitions in which each pair of models sits in the same cluster."""
    C = np.zeros((parts.shape[1], parts.shape[1]))
    for p in parts:
        C += p[:, None] == p[None, :]
    return C / len(parts)


def consensus_cut(C: np.ndarray, k: int) -> np.ndarray:
    """Average-linkage cut of 1 - C into k families (consensus clustering, Monti et al. 2003)."""
    from scipy.cluster import hierarchy
    from scipy.spatial.distance import squareform

    D = 1.0 - C
    np.fill_diagonal(D, 0.0)
    L = hierarchy.linkage(squareform(D, checks=False), method="average")
    return relabel(hierarchy.fcluster(L, k, criterion="maxclust"))


def pac(C: np.ndarray, lo: float = 0.1, hi: float = 0.9) -> float:
    """Proportion of ambiguous clustering: the share of model pairs that are together in between
    10% and 90% of the resamples. 0 = every pair always together or always apart."""
    iu = np.triu_indices(C.shape[0], 1)
    v = C[iu]
    return float(np.mean((v > lo) & (v < hi)))


def consensus_report(models: list[str], Z: np.ndarray, ks, n: int = 200, seed: int = 0) -> dict:
    """Per k: the consensus families from n resamples, PAC, the reproducibility (ARI between the
    consensus families of two independent halves of the resamples, and between the full consensus
    and each resample), and each model's confidence (mean co-assignment with its family mates)."""
    rng = np.random.default_rng(seed)
    ms = sorted(set(models))
    out = {}
    for k in ks:
        parts = resample_partitions(models, Z, k, n, rng)
        C = coassoc(parts)
        fam = consensus_cut(C, k)
        h1, h2 = consensus_cut(coassoc(parts[: n // 2]), k), consensus_cut(coassoc(parts[n // 2:]), k)
        per = [DM.ari(fam, p) for p in parts]
        conf = {}
        for i, m in enumerate(ms):
            mates = [j for j in range(len(ms)) if fam[j] == fam[i] and j != i]
            conf[m] = float(np.mean(C[i, mates])) if mates else float(np.mean(parts[:, i][:, None] != parts[:, [j for j in range(len(ms)) if j != i]]))
        out[int(k)] = {"families": fam, "coassoc": C, "pac": pac(C), "halves_ari": DM.ari(h1, h2),
                       "resample_ari": [float(np.mean(per)), float(np.percentile(per, 5))],
                       "confidence": conf, "sizes": sorted(np.bincount(fam)[1:].tolist(), reverse=True),
                       "singletons": int(np.sum(np.bincount(fam)[1:] == 1))}
    return out


def choose_k_consensus(rep: dict, ms: list[str], flagged=QUIET_UNRELIABLE, max_singletons: int = 2,
                       min_halves: float = 0.9) -> int:
    """The k with the lowest PAC among those whose two half-consensus families agree (ARI >=
    `min_halves`) and that leave at most `max_singletons` single-model families made of an
    unflagged model (the flagged quiet-input models may stand alone); ties to the smaller k.
    If no k qualifies, the k with the best half agreement."""
    def lone(k):
        fam = np.asarray(rep[k]["families"])
        return sum(1 for f in set(fam.tolist()) if np.sum(fam == f) == 1
                   and ms[int(np.nonzero(fam == f)[0][0])] not in flagged)
    ok = [k for k in rep if rep[k]["halves_ari"] >= min_halves and lone(k) <= max_singletons]
    if not ok:
        return max(rep, key=lambda k: rep[k]["halves_ari"])
    return min(ok, key=lambda k: (round(rep[k]["pac"], 3), k))


def silhouette(M: np.ndarray, cl) -> float:
    cl = np.asarray(cl)
    D = np.sqrt(np.maximum(np.sum(M ** 2, 1)[:, None] + np.sum(M ** 2, 1)[None] - 2 * M @ M.T, 0))
    s = []
    for i in range(len(cl)):
        same = (cl == cl[i])
        same[i] = False
        if not same.any():
            s.append(0.0)
            continue
        a = D[i, same].mean()
        b = min(D[i, cl == c].mean() for c in set(cl) if c != cl[i])
        s.append((b - a) / max(a, b))
    return float(np.mean(s))


def choose_k(stab: dict, min_stability: float = 0.75, slack: float = 0.05) -> int:
    """The largest k whose stability is at least `min_stability` and within `slack` of the best
    stability over k >= 4; if none reaches `min_stability`, the most stable k."""
    ks = sorted(stab)
    best = max(stab[k]["stability"] for k in ks)
    ok = [k for k in ks if stab[k]["stability"] >= min_stability and stab[k]["stability"] >= best - slack]
    return max(ok) if ok else max(ks, key=lambda k: stab[k]["stability"])


# ------------------------------------------------------------------------------------- T180 names

def _median(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    return float(np.median(xs)) if xs else None


def member_character(card: dict, sc_centre: dict) -> dict:
    """Measured character of one model from its step-3 card (`S3.model_card`) and the centre
    setting's scalars."""
    v = card["values"]
    p = rapp_smoothness(sc_centre)
    knee = v["knee_dbfs"]["centre"]
    onset = v["onset_dbfs"]["centre"]
    st = card["states"]
    return {"even_db": v["even_share_db"]["centre"], "knee_dbfs": knee, "onset_dbfs": onset,
            "onset_below_start": "below" in st["onset_state"] and onset is not None and onset <= -90,
            "knee_none": knee is None, "knee_at_floor": knee is not None and knee <= -90.5, "rapp_p": p,
            "pre_hp_hz": v["pre_hp_hz"]["centre"], "post_lp_hz": v["post_lp_hz"]["centre"],
            "mid_db": v["mid_db"]["centre"], "mid_kind": _mid_kind(card), "mid_f_hz": _mid_f(card),
            "drive_knee_move_db": v["knee_dbfs"]["drive_effect"], "drive_gain_move_db": v["g0_db"]["drive_effect"]}


RAPP_FIT_DB = 1.5        # a Rapp side counts when its fit is within 1.5 dB RMS ...
RAPP_P_RANGE = (0.3, 100.0)   # ... and its exponent is off the fit bounds (0.2, 200)


def rapp_smoothness(sc: dict) -> float | None:
    """Rapp smoothness exponent of the static curve (2 = rounded, 10+ = nearly a brick wall): the
    geometric mean of the sides whose fit holds; None if neither does."""
    ps = []
    for side in ("rapp_pos", "rapp_neg"):
        r = sc.get(side) or {}
        p, rms = r.get("p"), r.get("rms_db")
        if p is not None and rms is not None and rms <= RAPP_FIT_DB and RAPP_P_RANGE[0] < p < RAPP_P_RANGE[1]:
            ps.append(float(p))
    return float(np.exp(np.mean(np.log(ps)))) if ps else None


SOFT_P, HARD_P = 2.5, 5.0


def _mid_kind(card):
    ks = card["states"]["mid_kind"]
    return ks[0] if len(ks) == 1 else ("mixed:" + "/".join(ks))


def _mid_f(card):
    r = card.get("mid_f_range_hz")
    return float(np.sqrt(r[0] * r[1])) if r else None


def describe_family(chars: list[dict], centre_mid: list[tuple[str, float, float | None]]) -> dict:
    """A name from measured character only. chars: member_character per member; centre_mid:
    (kind, dB, Hz) of each member's centre-setting mid feature."""
    even = _median([c["even_db"] if c["even_db"] is not None else -150 for c in chars])
    sym = ("symmetric" if even is not None and even < -60 else
           "slightly asymmetric" if even is not None and even < -30 else "asymmetric")
    p = _median([c["rapp_p"] for c in chars])
    soft = None if p is None else ("soft" if p < SOFT_P else ("medium" if p < HARD_P else "hard"))
    knee = _median([c["knee_dbfs"] for c in chars])
    below = np.mean([bool(c["onset_below_start"]) for c in chars])
    no_knee = np.mean([bool(c["knee_none"]) for c in chars])
    if below >= 0.5:
        gain = "nonlinear at every level"
    elif no_knee >= 0.5:
        gain = "clean until near full scale"
    elif np.mean([bool(c.get("knee_at_floor")) for c in chars]) >= 0.5:
        gain = "compressing from the quietest input"
    elif knee is None:
        gain = "gain ?"
    else:
        gain = "low gain" if knee > -40 else ("medium gain" if knee > -60 else "high gain")
    kinds = [k for k, _, _ in centre_mid]
    hump = np.mean([k == "hump" for k in kinds])
    scoop = np.mean([k == "scoop" for k in kinds])
    fmid = _median([f for k, _, f in centre_mid if k in ("hump", "scoop") and f])
    dmid = _median([d for k, d, _ in centre_mid if k in ("hump", "scoop")])
    if hump >= 0.5:
        mid = f"mid-hump {fmid / 1000:.1f} kHz" if fmid else "mid-hump"
    elif scoop >= 0.5:
        mid = f"mid-scoop {fmid / 1000:.1f} kHz" if fmid else "mid-scoop"
    elif (1 - hump - scoop) >= 0.5:
        mid = "flat mids"
    else:
        mid = "mixed mids"
    pre = np.mean([c["pre_hp_hz"] is not None for c in chars])
    post = _median([c["post_lp_hz"] for c in chars])
    parts = [" ".join(x for x in (soft, sym) if x), gain, mid]
    if pre >= 0.5:
        parts.append("pre-HPF")
    if post is not None and np.mean([c["post_lp_hz"] is not None for c in chars]) >= 0.5:
        parts.append(f"post-LPF {post / 1000:.1f} kHz")
    after = np.mean([(c["drive_gain_move_db"] or 0) > 10 and abs(c["drive_knee_move_db"] or 0) < 5
                     for c in chars])
    if after >= 0.5:
        parts.append("drive after the clipper")
    return {"name": ", ".join(parts), "even_share_db_median": even, "rapp_p_median": p, "knee_dbfs_median": knee,
            "onset_below_start_share": float(below), "no_knee_share": float(no_knee), "hump_share": float(hump),
            "scoop_share": float(scoop), "mid_f_hz_median": fmid, "mid_db_median": dmid,
            "pre_hpf_share": float(pre), "post_lp_hz_median": post, "drive_after_clipper_share": float(after)}


def label_disagreements(ms: list[str], fam: dict[str, int]) -> dict:
    """Where the circuit-note labels (DM.LABELS: confusable group, family, provisional class) and
    the measured families disagree: groups split across families; families that mix label families;
    each model whose label family is not its measured family's majority label."""
    from collections import Counter

    groups = {}
    for m in ms:
        g = DM.LABELS[m][0]
        if not g.startswith("O:"):
            groups.setdefault(g, []).append(m)
    split = {g: {m: fam[m] for m in mm} for g, mm in groups.items() if len({fam[m] for m in mm}) > 1}
    kept = sorted(g for g, mm in groups.items() if len(mm) > 1 and len({fam[m] for m in mm}) == 1)
    mix, odd = {}, []
    for f in sorted(set(fam.values())):
        mem = [m for m in ms if fam[m] == f]
        for j, name in ((1, "family"), (2, "class6")):
            c = Counter(DM.LABELS[m][j] for m in mem)
            mix.setdefault(str(f), {})[name] = dict(c.most_common())
        maj = Counter(DM.LABELS[m][1] for m in mem).most_common(1)[0][0]
        for m in mem:
            if DM.LABELS[m][1] != maj and len(mem) > 1:
                odd.append({"model": m, "label_family": DM.LABELS[m][1], "family": f, "family_majority": maj})
    return {"groups_split": split, "groups_kept_together": kept, "label_mix": mix, "minority_members": odd,
            "ari": {name: DM.ari([DM.LABELS[m][j] for m in ms], [fam[m] for m in ms])
                    for j, name in ((0, "groups17"), (1, "family"), (2, "class6"))}}
