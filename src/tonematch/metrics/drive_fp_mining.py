"""Step 0 of docs/notes/distortion-identification.md section 8 (T171): mine the existing coarse-v1
drive fingerprints (probe-v1, block-library-v1.2) for model separability. numpy/scipy only.
Research code: nothing here is an adopted method or an approved class list.

Inputs are the user's local fingerprint JSONs under cache/probe/fingerprints/ (derived from the
user's Native install; never committed). Reference labels come from the tables in
docs/notes/distortion-circuits.md (Line 6's own "based on" list plus the research agent's circuit
reading): the 17 confusable groups (singletons "O" each get their own label), the family column,
and a six-way circuit class that is MY provisional reading of the clipping column (not approved).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.cluster import hierarchy

EXCLUDED = {"HD2_DistBitcrusher"}       # excluded from the search pool (synthetic_v1.eligible)

# model id -> (confusable group, family, provisional circuit class)
# group: parent letter of distortion-circuits.md 1.2/1.4 (A2/A3/"A/B" -> A, G2 -> G, J2 -> J);
# family: B boost, OD overdrive, D distortion, F fuzz, O other;
# class: boost, soft_od (symmetric soft / op-amp feedback), hard_dist (diodes to ground or
# high-gain hard), asym_od (asymmetric diode, MOSFET, BJT, tube), fuzz, other (bass DIs, lo-fi;
# circuit unverified).
LABELS: dict[str, tuple[str, str, str]] = {
    "HD2_DistScream808": ("A", "OD", "soft_od"),
    "HD2_DM4Screamer": ("A", "OD", "soft_od"),
    "HD2_DistStuporOD": ("A", "OD", "asym_od"),
    "HD2_DistPillars": ("A", "OD", "soft_od"),
    "HD2_DistDhyanaDrive": ("A", "OD", "soft_od"),
    "HD2_DistHorizonDrive": ("A", "OD", "soft_od"),
    "HD2_DistMinotaur": ("B", "OD", "soft_od"),
    "HD2_DistTeemah": ("B", "OD", "soft_od"),
    "HD2_DistHeirApparent": ("B", "OD", "soft_od"),
    "HD2_DistToneSovereign": ("B", "OD", "soft_od"),
    "HD2_DistPrizeDrive": ("B", "OD", "hard_dist"),
    "HD2_DistTopSecretOD": ("C", "OD", "hard_dist"),
    "HD2_DM4Overdrive": ("C", "OD", "hard_dist"),
    "HD2_DistKWB": ("C", "D", "asym_od"),
    "HD2_DistVerminDist": ("D", "D", "hard_dist"),
    "HD2_DistRatatouilleDist": ("D", "D", "hard_dist"),
    "HD2_DM4ClassicDistortion": ("D", "D", "hard_dist"),
    "HD2_DistVitalDist": ("D", "D", "hard_dist"),
    "HD2_DistHedgehogD9": ("E", "D", "hard_dist"),
    "HD2_DistDeezOneVintage": ("E", "D", "hard_dist"),
    "HD2_DistDeezOneMod": ("E", "D", "hard_dist"),
    "HD2_DistSwedishChainsaw": ("F", "D", "hard_dist"),
    "HD2_DM4HeavyDistortion": ("F", "D", "hard_dist"),
    "KillerZ": ("F", "D", "hard_dist"),
    "HD2_DistLegendaryDrive": ("F", "D", "hard_dist"),
    "HD2_DistTriangleFuzz": ("G", "F", "fuzz"),
    "HD2_DistRamsHead": ("G", "F", "fuzz"),
    "HD2_DistDarkDoveFuzz": ("G", "F", "fuzz"),
    "HD2_DM4FuzzPi": ("G", "F", "fuzz"),
    "HD2_DistBallisticFuzz": ("G", "F", "fuzz"),
    "HD2_DistPocketFuzz": ("G", "F", "fuzz"),
    "HD2_DistArbitratorFuzz": ("H", "F", "fuzz"),
    "HD2_DM4FacialFuzz": ("H", "F", "fuzz"),
    "HD2_DM4JumboFuzz": ("I", "F", "fuzz"),
    "HD2_DM4BuzzSaw": ("I", "F", "fuzz"),
    "HD2_DistWringerFuzz": ("I", "F", "fuzz"),
    "HD2_DistTycoctaviaFuzz": ("J", "F", "fuzz"),
    "HD2_DM4OctaveFuzz": ("J", "F", "fuzz"),
    "HD2_DM4SubOctFuzz": ("J", "F", "fuzz"),
    "HD2_DistKinkyBoost": ("K", "B", "boost"),
    "HD2_DistDerangedMaster": ("K", "B", "boost"),
    "HD2_DistVitalBoost": ("K", "B", "boost"),
    "HD2_DistAmpegScramblerOD": ("L", "OD", "other"),
    "HD2_DistZeroAmpBassDI": ("L", "OD", "other"),
    "HD2_DistRegalBassDI": ("L", "OD", "other"),
    "HD2_DistObsidian7000": ("L", "D", "other"),
    "Line6BronzeMaster": ("L", "D", "other"),
    "HD2_DM4ColorDrive": ("M", "OD", "asym_od"),
    "HD2_DM4Line6Drive": ("M", "OD", "asym_od"),
    "HD2_DistValveDriver": ("N", "OD", "asym_od"),
    "HD2_DM4TubeDrive": ("N", "OD", "asym_od"),
    "HD2_DistCompulsiveDrive": ("P", "OD", "asym_od"),
    "HD2_DistAlpacaRouge": ("Q", "OD", "soft_od"),
    "HD2_DistClawthornDrive": ("R", "OD", "fuzz"),
    "HD2_DistIndustrialFuzz": ("O:Industrial", "F", "fuzz"),
    "HD2_DistXenomorphFuzz": ("O:Xenomorph", "F", "fuzz"),
    "HD2_DistThrifterFuzz": ("O:Thrifter", "F", "fuzz"),
    "HD2_DM4Line6Distortion": ("O:L6Distortion", "D", "hard_dist"),
    "HD2_DistMegaphone": ("O:Megaphone", "O", "other"),
    "HD2_DM4JetFuzz": ("O:JetFuzz", "F", "fuzz"),
}

T164_TOP = ["HD2_DM4SubOctFuzz", "HD2_DistHorizonDrive", "HD2_DistObsidian7000",
            "HD2_DistTeemah", "HD2_DistZeroAmpBassDI"]


def _num(v, default=np.nan):
    return float(v) if isinstance(v, (int, float)) and v is not None else default


def load(dirpath: str | Path) -> list[dict]:
    """One record per (drive model, stored setting)."""
    out = []
    for p in sorted(Path(dirpath).glob("*.json")):
        if p.name == "index.json":
            continue
        d = json.loads(p.read_text())
        if d.get("category") != "drive" or d["model"] in EXCLUDED:
            continue
        for s in d["settings"]:
            out.append({"model": d["model"], "setting": s["setting"], "cc": s.get("cc", {}),
                        "fp": s["fingerprint"], "summary": s.get("summary", {})})
    return out


def _ratio(v):
    # None with a bend = slope above ~0 (a limiter): cap at 20; log scale
    return np.log(min(_num(v, 20.0), 20.0))


def features(rec: dict, kind: str) -> dict[str, float]:
    """Feature dict for one setting. kind: 'note6' (the six scalars named in the note),
    'scalars' (every stored scalar that describes the nonlinearity, the EQ or the level),
    'curves' (the stored curves: linear response, harmonic profiles vs level, DI band response)."""
    fp, sm = rec["fp"], rec["summary"]
    tc, sc = fp["transfer_curve"], fp["static_curve"]
    knee = sm.get("knee_dbfs")
    f = {"gain_1k_db": _num(sm.get("gain_1k_db")),
         "thd_440_db": _num(sm.get("thd_440_db")),
         "tilt_4k_250_db": _num(sm.get("tilt_4k_250_db")),
         "log_ratio": _ratio(sm.get("ratio")),
         "knee_dbfs": _num(knee, 0.0),          # no knee found in range -> above the range
         "no_knee": 1.0 if knee is None else 0.0,
         "symmetry_log": np.log(max(_num(tc.get("symmetry"), 1.0), 1e-3))}
    if kind == "note6":
        return f
    f["level_dependent_gain_db"] = _num(sm.get("level_dependent_gain_db"))
    for k in ("slope_below", "slope_above", "small_signal_gain_db", "p1db_in_dbfs",
              "p3db_in_dbfs", "out_max_dbfs", "fit_rms_db"):
        f[f"sc_{k}"] = _num(sc.get(k), -45.0 if "dbfs" in k else np.nan)
    for k in ("r2", "pos_ceiling", "neg_ceiling"):
        f[f"tc_{k}"] = _num(tc.get(k))
    for h in fp["harmonics"]["per_freq"]:
        st = h["static"]
        tag = int(h["freq_hz"])
        for k in ("small_signal_gain_db", "p1db_in_dbfs", "out_max_dbfs", "slope_above"):
            f[f"h{tag}_{k}"] = _num(st.get(k), -45.0 if "dbfs" in k else np.nan)
        dc = [abs(_num(x, 0.0)) for x in h["dc_re_fund"]]
        f[f"h{tag}_dc_max"] = float(np.log10(max(max(dc), 1e-6)))
    for im in fp["imd"]:
        for k in ("imd2_db", "imd3_db"):
            f[f"{im['name']}_{k}"] = _num(im.get(k))
    dy = fp["dynamics"]
    for k in ("gain_low_db", "gain_high_db"):
        f[f"dyn_{k}"] = _num(dy.get(k))
    for di in fp["di"]:
        for k in ("rms_gain_db", "crest_out_db", "nonlinear_fraction_db"):
            f[f"{di['name']}_{k}"] = _num(di.get(k))
        f[f"{di['name']}_log_centroid"] = np.log(max(_num(di.get("centroid_out_hz"), 1.0), 1.0))
    for i, g in enumerate(fp["linear"]["gain_1k_db"]):
        f[f"lin_gain_1k_{i}"] = _num(g)
    if kind == "scalars":
        return f
    lin = fp["linear"]
    for i, row in enumerate(lin["mag_db"]):
        for j in range(0, len(row), 3):
            f[f"lin_mag_{i}_{j}"] = _num(row[j])
    for h in fp["harmonics"]["per_freq"]:
        tag = int(h["freq_hz"])
        for j, v in enumerate(h["gain_db"]):
            f[f"h{tag}_gain_{j}"] = _num(v)
        for j, v in enumerate(h["thd_db"]):
            f[f"h{tag}_thd_{j}"] = _num(v)
        for j, v in enumerate(h["odd_even_db"]):
            f[f"h{tag}_oe_{j}"] = min(_num(v, 0.0), 60.0)
        for j, row in enumerate(h["h_db"]):
            for q, v in enumerate(row):
                f[f"h{tag}_h{q + 2}_{j}"] = max(_num(v, -140.0), -100.0)
    for di in fp["di"]:
        for j, v in enumerate(di["response_db"]):
            f[f"{di['name']}_resp_{j}"] = _num(v)
    return f


LEVEL_KEYS = ("gain_1k_db", "small_signal_gain_db", "out_max_dbfs", "rms_gain_db", "lin_gain_1k",
              "dyn_gain_", "_gain_", "pos_ceiling", "neg_ceiling")


def level_free(f: dict) -> dict:
    """Drop features that carry the block's absolute output level (Helix level-matches each
    model differently, which is not a circuit property); linear magnitudes become relative to
    their own mean."""
    out = {k: v for k, v in f.items() if not any(q in k for q in LEVEL_KEYS)
           and not k.startswith("lin_mag_")}
    lin = {k: v for k, v in f.items() if k.startswith("lin_mag_")}
    for i in range(4):
        row = {k: v for k, v in lin.items() if k.startswith(f"lin_mag_{i}_")}
        if row:
            mu = np.nanmean(list(row.values()))
            out.update({k: v - mu for k, v in row.items()})
    return out


def matrix(recs: list[dict], kind: str, no_level: bool = False):
    """Standardised feature matrix (z-score per column over all settings; missing -> column
    median; constant columns dropped). `no_level` applies `level_free`. Returns (Z, names)."""
    rows = [features(r, kind) for r in recs]
    if no_level:
        rows = [level_free(r) for r in rows]
    names = sorted(set().union(*rows))
    X = np.array([[row.get(n, np.nan) for n in names] for row in rows], float)
    with np.errstate(all="ignore"):
        allnan = ~np.isfinite(X).any(axis=0)
        X[:, allnan] = 0.0
        med = np.nanmedian(X, axis=0)
    X = np.where(np.isfinite(X), X, med)
    sd = X.std(axis=0)
    keep = sd > 1e-9
    Z = (X[:, keep] - X[:, keep].mean(axis=0)) / sd[keep]
    return Z, [n for n, k in zip(names, keep) if k]


def pairwise(Z: np.ndarray) -> np.ndarray:
    sq = np.sum(Z ** 2, axis=1)
    D2 = np.maximum(sq[:, None] + sq[None, :] - 2 * Z @ Z.T, 0.0)
    return np.sqrt(D2 / Z.shape[1])


def loso_identification(recs: list[dict], Dm: np.ndarray, skip_default: bool = False):
    """Leave-one-setting-out: each stored setting is a query; the gallery is every OTHER stored
    setting (the query's own model keeps its remaining settings). Model distance = nearest
    gallery setting of that model. Returns per-query true-model rank (1 = top) and model list."""
    models = sorted({r["model"] for r in recs})
    idx = {m: [i for i, r in enumerate(recs) if r["model"] == m
               and not (skip_default and r["setting"] == "default")] for m in models}
    ranks, queries = [], []
    for m in models:
        for i in idx[m]:
            dist = []
            for m2 in models:
                cand = [j for j in idx[m2] if j != i]
                dist.append(min(Dm[i, j] for j in cand) if cand else np.inf)
            dist = np.array(dist)
            true = dist[models.index(m)]
            ranks.append(int(1 + np.sum(dist < true)))
            queries.append((m, recs[i]["setting"]))
    return np.array(ranks), queries, models


def bootstrap_rate(ranks, queries, k: int, n_boot: int = 2000, seed: int = 0):
    """Top-k rate with a cluster bootstrap over models (settings of a model are correlated)."""
    rng = np.random.default_rng(seed)
    by = {}
    for r, (m, _) in zip(ranks, queries):
        by.setdefault(m, []).append(r <= k)
    ms = list(by)
    rate = float(np.mean([v for m in ms for v in by[m]]))
    bs = []
    for _ in range(n_boot):
        pick = rng.choice(len(ms), len(ms), replace=True)
        vals = [v for p in pick for v in by[ms[p]]]
        bs.append(np.mean(vals))
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return rate, float(lo), float(hi)


def ari(a, b) -> float:
    """Adjusted Rand index (Hubert and Arabie)."""
    a = np.asarray(a)
    b = np.asarray(b)
    _, ai = np.unique(a, return_inverse=True)
    _, bi = np.unique(b, return_inverse=True)
    C = np.zeros((ai.max() + 1, bi.max() + 1))
    np.add.at(C, (ai, bi), 1)

    def c2(x):
        return x * (x - 1) / 2.0
    n = a.size
    s_ij = c2(C).sum()
    s_a = c2(C.sum(axis=1)).sum()
    s_b = c2(C.sum(axis=0)).sum()
    exp = s_a * s_b / c2(n)
    mx = 0.5 * (s_a + s_b)
    return float((s_ij - exp) / (mx - exp)) if mx != exp else 0.0


def model_means(recs, Z):
    models = sorted({r["model"] for r in recs})
    M = np.array([Z[[i for i, r in enumerate(recs) if r["model"] == m]].mean(axis=0)
                  for m in models])
    return models, M


def cluster(M: np.ndarray, k: int, method: str = "ward"):
    L = hierarchy.linkage(M, method=method)
    return hierarchy.fcluster(L, k, criterion="maxclust"), L


def ari_null(labels, k_clusters, n_items, n_perm: int = 1000, seed: int = 0, sizes=None):
    """ARI of random partitions into the same cluster sizes against `labels` (95th percentile)."""
    rng = np.random.default_rng(seed)
    base = np.asarray(sizes)
    vals = []
    for _ in range(n_perm):
        vals.append(ari(labels, rng.permutation(base)))
    return float(np.percentile(vals, 95)), float(np.mean(vals))
