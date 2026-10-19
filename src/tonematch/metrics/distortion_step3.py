"""Step 3 of docs/notes/distortion-identification.md (T174): the dfp-v1 fingerprints of every drive.

RESEARCH CODE. Approved by the human ("go ahead", DECISIONS 2026-10-18): 6 s sweeps, the staircase
from about -90 dBFS, Kinky Boost as it is, the non-W-H flag dropped, unmapped switches at default,
all drive models at 12 settings each. Nothing here is an admitted score, an adopted method, a
class list or a bar.

What lives here (the script `scripts/distortion_step3.py` drives it):

- `KNOBS` / `knob_semantics`: which knob of each model is "drive" and which is "tone", read from
  docs/notes/distortion-circuits.md (1.2, 1.3). Level knobs and every other knob stay at the
  library default; unmapped switches at the catalog default.
- `settings_for`: the 12 settings (grid Drive 0.2/0.5/0.85 x tone 0.2/0.8, then the tone-centre
  line, the Drive ends and one off-grid point; one-knob models get 12 Drive points).
- `fingerprint_job`: one render -> one stored fingerprint (worker process, no audio kept).
- `features`, `loso`, `cluster_report`, `cards`: the step-3 analysis.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import distortion_probe as P
from . import drive_fp_mining as DM

# ------------------------------------------------------------------------------------- knobs

# model -> (drive knob, tone knob or None, note). From distortion-circuits.md 1.2 / 1.3: "drive" is
# the knob that sets how hard the clipper is driven (Gain / Drive / Sustain / Fuzz / Dist / Grit);
# "tone" the main EQ control (Tone, Filter, Treble, HiCut, Bright, Spectrum, Contour, Notch Gain).
KNOBS: dict[str, tuple[str, str | None, str]] = {
    "HD2_DM4BuzzSaw": ("Drive", "Treble", "legacy 3-band stack; Bass/Mid at default"),
    "HD2_DM4ClassicDistortion": ("Drive", "Filter", "RAT Filter; Bass/Treble at default"),
    "HD2_DM4ColorDrive": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4FacialFuzz": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4FuzzPi": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4HeavyDistortion": ("Drive", "Treble", "MT-2 type; Bass/Mid at default"),
    "HD2_DM4JetFuzz": ("Drive", "Tone", "phaser Rate/Feedback at default (may be time-varying)"),
    "HD2_DM4JumboFuzz": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4Line6Distortion": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4Line6Drive": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4OctaveFuzz": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4Overdrive": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DM4Screamer": ("Drive", "Tone", "TS Tone; Bass/Treble at default"),
    "HD2_DM4SubOctFuzz": ("Drive", "Treble", "Sub and Bass at default"),
    "HD2_DM4TubeDrive": ("Drive", "Treble", "3-band stack; Bass/Mid at default"),
    "HD2_DistAlpacaRouge": ("Drive", "HiCut", "Volume is the level"),
    "HD2_DistAmpegScramblerOD": ("Drive", "Treble", "Blend at default"),
    "HD2_DistArbitratorFuzz": ("Fuzz", None, "one knob"),
    "HD2_DistBallisticFuzz": ("Sustain", "Tone", ""),
    "HD2_DistClawthornDrive": ("ODGain", "ODTone", "OD channel; Fuzz/FuzzOct/ODLowBoost switches unmapped, "
                               "at default; fuzz-channel knobs at default"),
    "HD2_DistCompulsiveDrive": ("Gain", "Tone", "LPHP and Version switches unmapped, at default"),
    "HD2_DistDarkDoveFuzz": ("Sustain", "Tone", ""),
    "HD2_DistDeezOneMod": ("Drive", "Tone", "Clipping switch unmapped, at default"),
    "HD2_DistDeezOneVintage": ("Drive", "Tone", ""),
    "HD2_DistDerangedMaster": ("Drive", "Treble", "Bass at default"),
    "HD2_DistDhyanaDrive": ("Gain", "Tone", "Voice at default"),
    "HD2_DistHedgehogD9": ("Gain", "Tone", ""),
    "HD2_DistHeirApparent": ("Gain", "Tone", "Presence at default; Clipping/GainMod/Voltage unmapped"),
    "HD2_DistHorizonDrive": ("Drive", "Bright", "Gate at default; Attack/Gate_Range unmapped"),
    "HD2_DistIndustrialFuzz": ("Drive", None, "no tone knob; Compress/Gate/Stability at default; "
                               "Oscillator unmapped"),
    "HD2_DistKWB": ("Gain", "Treble", "Treble -12..+12 dB; Asym/Bass at default; Push/PullDiode unmapped"),
    "HD2_DistKinkyBoost": ("Drive", None, "one knob; Boost/Bright switches unmapped, at default"),
    "HD2_DistLegendaryDrive": ("Drive", "Treble", "Bass/Middle/Presence at default; Volume is the level"),
    "HD2_DistMegaphone": ("Grit", "Tone", "Focus/Space/Mix at default"),
    "HD2_DistMinotaur": ("Gain", "Tone", ""),
    "HD2_DistObsidian7000": ("Drive", "Treble", "Treble -20..+20 dB; Blend/Bass/mids at default; "
                             "Master is the level; 5 switches unmapped"),
    "HD2_DistPillars": ("Gain", "Tone", "Mode switch unmapped, at default"),
    "HD2_DistPocketFuzz": ("Drive", None, "one knob"),
    "HD2_DistPrizeDrive": ("Drive", "Spectrum", "Bass Cut/Voltage unmapped, at default"),
    "HD2_DistRamsHead": ("Sustain", "Tone", ""),
    "HD2_DistRatatouilleDist": ("Gain", "Filter", ""),
    "HD2_DistRegalBassDI": ("Volume", "Treble", "no gain knob: Volume (the preamp's only gain control) "
                            "is measured in the drive slot; Bass at default; Low Cut unmapped"),
    "HD2_DistScream808": ("Gain", "Tone", ""),
    "HD2_DistStuporOD": ("Drive", "Tone", ""),
    "HD2_DistSwedishChainsaw": ("Drive", "Treble", "Bass at default"),
    "HD2_DistTeemah": ("Gain", "Treble", "Bass at default; Clipping unmapped"),
    "HD2_DistThrifterFuzz": ("Drive", "Notch Gain", "Notch Gain -10..+10 dB at the default Notch Freq; "
                             "Attack at default; Thick unmapped"),
    "HD2_DistToneSovereign": ("Gain 1", "Tone 1", "channel 1; channel 2 and Presence at default; "
                              "Clipping/GainMod/Voltage unmapped"),
    "HD2_DistTopSecretOD": ("Gain", None, "one knob"),
    "HD2_DistTriangleFuzz": ("Sustain", "Tone", ""),
    "HD2_DistTycoctaviaFuzz": ("Fuzz", None, "one knob"),
    "HD2_DistValveDriver": ("Gain", "Treble", "Bass at default"),
    "HD2_DistVerminDist": ("Gain", "Filter", ""),
    "HD2_DistVitalBoost": ("Boost", None, "one knob"),
    "HD2_DistVitalDist": ("Gain", "Filter", "Octave at default; Clipping unmapped"),
    "HD2_DistWringerFuzz": ("Gain", "Treble", "Bass at default; FuzzType unmapped"),
    "HD2_DistXenomorphFuzz": ("Gain", "Tone", "oscillator knobs at default; Clipping/WaveShape unmapped"),
    "HD2_DistZeroAmpBassDI": ("Drive", "Treble", "Bass/Blend/Presence at default"),
    "KillerZ": ("Dist", "Contour", "MT-2 type; Gain (probably the level) and Mid Amt/Freq at default"),
    "Line6BronzeMaster": ("Drive", None, "mixture (a blend, not a tone) at default; tonality unmapped"),
}

LEVEL_NAMES = ("Level", "Output", "Volume", "Master", "Level 1", "Level 2", "ODLevel", "FuzzLevel", "Mix")

# CC steps (0..127): 25 / 64 / 108 = 0.20 / 0.50 / 0.85 (drive); 25 / 102 = 0.20 / 0.80 (tone)
GRID = [(d, t) for d in (25, 64, 108) for t in (25, 102)]
EXTRA = [((25, 64), "centre"), ((64, 64), "centre"), ((108, 64), "centre"),
         ((6, 64), "end"), ((127, 64), "end"), ((83, 44), "off")]
ONE_KNOB = [0, 12, 25, 38, 51, 64, 76, 89, 102, 108, 118, 127]
REPEATS = (("HD2_DistScream808", "Gain@64,Tone@64"), ("HD2_DistArbitratorFuzz", "Fuzz@108"))


def knob_semantics(model: str, params: dict, tmap: dict) -> dict:
    """The drive / tone knobs of `model` and what stays fixed. Raises if a chosen knob is not
    CC-mapped on the template (it could not be set)."""
    drive, tone, note = KNOBS[model]
    for k in (drive, tone):
        if k is not None and f"0:{k}" not in tmap:
            raise KeyError(f"{model}: knob {k!r} not mapped on the template")
    names = [k for k in params if not k.startswith("@")]
    unmapped = [k for k in names if f"0:{k}" not in tmap]
    level = [k for k in names if k in LEVEL_NAMES and k not in (drive, tone)]
    other = [k for k in names if k not in (drive, tone) and k not in unmapped and k not in level]
    return {"drive": drive, "tone": tone, "note": note, "level_fixed": level, "other_at_default": other,
            "unmapped_at_default": unmapped, "one_knob": tone is None}


def setting_name(drive: str, tone: str | None, dcc: int, tcc: int | None = None) -> str:
    return f"{drive}@{dcc}" + ("" if tone is None else f",{tone}@{tcc}")


def settings_for(drive: str, tone: str | None) -> list[tuple[str, dict, str]]:
    """12 (name, {knob: cc}, kind) per model. kind: grid / centre / end / off, or "drive" for
    the 12 Drive points of a one-knob model."""
    if tone is None:
        return [(setting_name(drive, None, c), {drive: c}, "drive") for c in ONE_KNOB]
    out = [(setting_name(drive, tone, d, t), {drive: d, tone: t}, "grid") for d, t in GRID]
    out += [(setting_name(drive, tone, d, t), {drive: d, tone: t}, kind) for (d, t), kind in EXTRA]
    return out


# ------------------------------------------------------------------------------------- worker

_LAYOUT = None
DROPPED_SCALARS = ("odd_harmonic_residual_db", "harmonic_residual_db")   # the non-W-H flag, dropped


def fingerprint_job(y: np.ndarray, path: str) -> dict:
    """Worker: analyse one render of the "v1" probe and write its fingerprint (no audio)."""
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = P.build_probe("v1")[1]
    t0 = time.perf_counter()
    arr, sc = P.analyse(np.asarray(y, float), _LAYOUT)
    for k in DROPPED_SCALARS:
        sc.pop(k, None)
    sc["analysis_s"] = round(time.perf_counter() - t0, 2)
    b = P.pack(arr, sc)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b)
    return {"file": str(p), "bytes": len(b), "analysis_s": sc["analysis_s"]}


# ------------------------------------------------------------------------------------- tone check

def tone_reach_summary(rows: list[dict], refs: list[dict], f) -> dict:
    """Scream 808 Tone 0 vs 1 at each Gain, on the CC route and the file-truth route."""
    f = np.asarray(f, float)
    band = (f >= 100) & (f <= 10000)

    def eff(a, b, key):
        d = np.asarray(b[key]) - np.asarray(a[key])
        d = d[band & np.isfinite(d)]
        i1k = int(np.argmin(np.abs(np.log(f[band] / 1000))))
        dn = d - d[i1k]
        return {"max_abs_db": float(np.max(np.abs(d))), "shape_max_abs_db": float(np.max(np.abs(dn))),
                "at_1k_db": float(d[i1k])}
    out = {"per_gain": {}, "rows": [{k: r[k] for k in r if k not in ("cc_route", "truth")} for r in rows]}
    worst = None
    for r in rows:
        if "truth_null_db" in r:
            worst = r["truth_null_db"] if worst is None else max(worst, r["truth_null_db"])
    tmax = []
    for g in sorted({r["gain_cc"] for r in rows}):
        lo = next(r for r in rows if r["gain_cc"] == g and r["tone_cc"] == 0)
        hi = next(r for r in rows if r["gain_cc"] == g and r["tone_cc"] == 127)
        e = {k: eff(lo["cc_route"], hi["cc_route"], k) for k in ("ss_db", "hot_db", "di_ltas_db")}
        res = {"cc_route": e}
        if "truth" in lo and "truth" in hi:
            res["truth"] = {k: eff(lo["truth"], hi["truth"], k) for k in ("ss_db", "hot_db", "di_ltas_db")}
            tmax.append(res["truth"]["ss_db"]["max_abs_db"])
        out["per_gain"][str(g)] = res
    out["worst_truth_null_db"] = worst
    out["truth_tone_effect_max_db"] = max(tmax) if tmax else None
    rr = {}
    for m in sorted({r["model"] for r in refs}):
        lo = next(r for r in refs if r["model"] == m and r["tone_cc"] == 0)
        hi = next(r for r in refs if r["model"] == m and r["tone_cc"] == 127)
        rr[m] = {k: eff(lo, hi, k) for k in ("ss_db", "hot_db", "di_ltas_db")}
    out["refs"] = rr
    out["reaches"] = bool(worst is not None and worst <= -100.0)
    return out


# ------------------------------------------------------------------------------------- features

def _num(v, default=np.nan):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None else default


FIT_RMS_DB = 3.0


def card_values(sc: dict, arr: dict, levels) -> dict:
    """The six card quantities (and level) of one setting, with their states."""
    L = np.asarray(levels, float)
    kn = sc.get("knee_1k_dbfs")
    first_comp = float(sc["g0_ss_1k_db"] - (arr["stair1k_db"][0][0] - L[0]))
    if first_comp >= 1.0:
        knee, ks = float(L[0]), "below"
    elif kn is None:
        knee, ks = None, "none"
    else:
        knee, ks = float(kn), "within"
    on = P.harmonic_onset(L, arr["stair1k_db"])
    pfit, qfit = sc.get("P_hp1_fit", {}), sc.get("Q_lp1_fit", {})
    mf = sc.get("mid_feature") or P.mid_feature(P.FGRID, arr["ss_db"], arr["sat_db"])
    pf, pr, qf, qr = (_num(pfit.get("fc_hz")), _num(pfit.get("rms_db")), _num(qfit.get("fc_hz")),
                      _num(qfit.get("rms_db")))
    # a corner counts as "clear" when the first-order fit holds (rms <= FIT_RMS_DB) and the corner
    # lies inside the measured band (63 Hz-16 kHz); otherwise the card shows none
    pre_clear = bool(np.isfinite(pf) and pr <= FIT_RMS_DB and 63.0 <= pf <= 16000.0)
    post_clear = bool(np.isfinite(qf) and qr <= FIT_RMS_DB and 63.0 <= qf <= 16000.0)
    return {"knee_dbfs": knee, "knee_state": ks, "onset_dbfs": on["onset_dbfs"], "onset_state": on["state"],
            "even_share_db": _num(sc.get("even_share_db")), "pre_hp_fit_hz": pf, "pre_hp_rms_db": pr,
            "pre_hp_hz": pf if pre_clear else None, "post_lp_fit_hz": qf, "post_lp_rms_db": qr,
            "post_lp_hz": qf if post_clear else None, "mid_kind": mf["kind"], "mid_f_hz": mf["f_hz"],
            "mid_db": _num(mf["db"], 0.0), "g0_db": _num(sc.get("g0_ss_1k_db")),
            "compression_m6_db": _num(sc.get("compression_at_minus6_db")),
            "thd_m6_db": _num(sc.get("thd_1k_at_minus6_db")), "memory": bool(sc.get("memory_flag")),
            "split_fraction": _num(sc.get("split_identifiable_fraction"))}


def card_features(c: dict, level: bool = True) -> dict:
    """Numeric features from a card (the "card" feature set)."""
    f = {"knee": c["knee_dbfs"] if c["knee_dbfs"] is not None else 0.0,
         "knee_none": 1.0 if c["knee_state"] == "none" else 0.0,
         "knee_below": 1.0 if c["knee_state"] == "below" else 0.0,
         "onset": c["onset_dbfs"] if c["onset_dbfs"] is not None else 0.0,
         "onset_never": 1.0 if c["onset_state"] == "never" else 0.0,
         "onset_below": 1.0 if c["onset_state"] == "below" else 0.0,
         "even": max(c["even_share_db"], -60.0) if np.isfinite(c["even_share_db"]) else -60.0,
         "log_pre": np.log(np.clip(c["pre_hp_fit_hz"], 20, 20000)) if np.isfinite(c["pre_hp_fit_hz"]) else np.nan,
         "log_post": np.log(np.clip(c["post_lp_fit_hz"], 20, 20000)) if np.isfinite(c["post_lp_fit_hz"]) else np.nan,
         "mid_db": c["mid_db"],
         "mid_logf": np.log(c["mid_f_hz"]) if c["mid_f_hz"] else np.log(1000.0)}
    if level:
        f["g0"] = c["g0_db"]
    return f


def _flat_scalars(sc: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in sc.items():
        if k in ("analysis_s", "L_s", "peak_dbfs", "noise_floor_dbfs_rms", "repeat_null") or k in DROPPED_SCALARS:
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat_scalars(v, key + "."))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    out[f"{key}[{i}]"] = float(x)
        elif isinstance(v, bool):
            out[key] = float(v)
        elif isinstance(v, (int, float)):
            out[key] = float(v)
        elif isinstance(v, str):
            out[f"{key}={v}"] = 1.0
    return out


SCALAR_LEVEL_KEYS = ("g0_", "gain_1k", "sweep_gain", "stair_gain", "tones_db", "rapp_pos.g0", "rapp_neg.g0",
                     "rapp_pos.v", "rapp_neg.v")


def scalar_features(sc: dict, level: bool = True) -> dict:
    f = _flat_scalars(sc)
    f = {k: (np.sign(v) * np.log1p(abs(v)) if ("_hz" in k or "rapp" in k and ".p" in k) else v) for k, v in f.items()}
    if not level:
        f = {k: v for k, v in f.items() if not any(q in k for q in SCALAR_LEVEL_KEYS)}
    return f


def curve_blocks(arr: dict, sc: dict, level: bool = True) -> dict[str, np.ndarray]:
    """The stored curves as feature blocks (dB, harmonics relative to the fundamental and clipped
    at -100 dB so the -130 to -160 dB float floors do not count as signal). `level=False` removes
    the setting's own 1 kHz small-signal gain from every absolute output level."""
    off = 0.0 if level else _num(sc.get("g0_ss_1k_db"), 0.0)
    lev = list(P.SWEEP_LEVELS)
    M1 = np.asarray(arr["M1"], float)
    top = M1[:, [lev.index(v) for v in P.HARM_LEVELS]]
    Mh = np.maximum(np.asarray(arr["Mh"], float) - top[None], -100.0)
    st = np.asarray(arr["stair1k_db"], float)
    st2 = np.asarray(arr["stair220_db"], float)
    y = np.asarray(arr["curve_y"], float)
    return {"M1": (M1 - off).ravel(), "Mh": Mh.ravel(),
            "stair1k_h1": st[0] - off, "stair1k_rel": np.maximum(st[1:] - st[0], -100.0).ravel(),
            "stair220_h1": st2[0] - off, "stair220_rel": np.maximum(st2[1:] - st2[0], -100.0).ravel(),
            "ss": np.asarray(arr["ss_db"], float) - off, "sat": np.asarray(arr["sat_db"], float),
            "P": np.asarray(arr["P_db"], float), "Q": np.asarray(arr["Q_db"], float),
            "imd": np.maximum(np.asarray(arr["imd"], float), -100.0).ravel(),
            "curve": y / (np.nanmax(np.abs(y)) + 1e-30)}


def standardise(rows: list[dict]) -> tuple[np.ndarray, list[str]]:
    names = sorted(set().union(*rows))
    X = np.array([[r.get(n, np.nan) for n in names] for r in rows], float)
    X[~np.isfinite(X)] = np.nan
    with np.errstate(all="ignore"):
        allnan = np.isnan(X).all(axis=0)
        X[:, allnan] = 0.0
        med = np.nanmedian(X, axis=0)
    X = np.where(np.isnan(X), med, X)
    sd = X.std(axis=0)
    keep = sd > 1e-9
    Z = (X[:, keep] - X[:, keep].mean(axis=0)) / sd[keep]
    return Z, [n for n, k in zip(names, keep) if k]


def block_matrix(blocks: list[dict[str, np.ndarray]], weighted: bool) -> tuple[np.ndarray, int]:
    """Z-scored curve blocks side by side; `weighted`: each block scaled to the same total weight."""
    parts = []
    for name in sorted(blocks[0]):
        X = np.stack([b[name] for b in blocks]).astype(float)
        Z, _ = standardise([dict(enumerate(r)) for r in X])
        if Z.shape[1] == 0:
            continue
        parts.append(Z / np.sqrt(Z.shape[1]) if weighted else Z)
    Z = np.concatenate(parts, axis=1)
    return Z, Z.shape[1]


def pairwise(Z: np.ndarray) -> np.ndarray:
    sq = np.sum(Z ** 2, axis=1)
    D2 = np.maximum(sq[:, None] + sq[None, :] - 2 * Z @ Z.T, 0.0)
    return np.sqrt(D2 / max(Z.shape[1], 1))


# ------------------------------------------------------------------------------------- identification

def loso(models: list[str], Dm: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Leave-one-setting-out: for each setting, the distance to every model is its nearest OTHER
    setting of that model; the rank of the true model (1 = top). Returns (ranks, nearest wrong
    model index per query, model list)."""
    ms = sorted(set(models))
    mi = np.array([ms.index(m) for m in models])
    n = len(models)
    D = Dm.copy()
    np.fill_diagonal(D, np.inf)
    per = np.full((n, len(ms)), np.inf)
    for k in range(len(ms)):
        cols = np.nonzero(mi == k)[0]
        per[:, k] = D[:, cols].min(axis=1)
    true = per[np.arange(n), mi]
    ranks = 1 + np.sum(per < true[:, None], axis=1)
    per_wrong = per.copy()
    per_wrong[np.arange(n), mi] = np.inf
    return ranks, np.argmin(per_wrong, axis=1), ms


def rates(ranks, models, kinds, ks=(1, 5)) -> dict:
    out = {}
    q = list(zip(models, [""] * len(models)))
    for k in ks:
        out[f"top{k}"] = list(DM.bootstrap_rate(np.asarray(ranks), q, k, n_boot=1000))
    out["by_kind"] = {}
    for kd in sorted(set(kinds)):
        sel = np.array([x == kd for x in kinds])
        out["by_kind"][kd] = {"n": int(sel.sum()), "top1": float(np.mean(np.asarray(ranks)[sel] <= 1)),
                              "top5": float(np.mean(np.asarray(ranks)[sel] <= 5))}
    out["n"] = len(ranks)
    out["median_rank"] = float(np.median(ranks))
    return out


def cluster_report(models: list[str], Z: np.ndarray, seed: int = 0) -> dict:
    """Ward clusters of the model means against the reference labels (ARI, random 95th pct)."""
    ms = sorted(set(models))
    M = np.array([Z[[i for i, m in enumerate(models) if m == mm]].mean(axis=0) for mm in ms])
    lab = {"groups17": [DM.LABELS[m][0] for m in ms], "family": [DM.LABELS[m][1] for m in ms],
           "class6": [DM.LABELS[m][2] for m in ms]}
    rows = {}
    for name, labels in lab.items():
        k = len(set(labels)) if name != "groups17" else 17
        cl, _ = DM.cluster(M, k)
        sizes = np.repeat(np.arange(len(np.bincount(cl))), np.bincount(cl))
        a = DM.ari(labels, cl)
        n95, nmean = DM.ari_null(labels, k, len(ms), n_perm=1000, seed=seed, sizes=sizes)
        rows[f"{name}_k{k}"] = {"ari": a, "null95": n95, "null_mean": nmean, "k": k,
                                "clusters": {str(c): [m for m, x in zip(ms, cl) if x == c] for c in sorted(set(cl))}}
    return {"rows": rows, "models": ms}


def wrong_same_label(models, nearest_wrong, ranks, ms) -> dict:
    """When top-1 is wrong: how often the chosen model shares the true model's group / class,
    against chance (a random other model)."""
    out = {}
    wrong = [i for i, r in enumerate(ranks) if r > 1]
    for j, name in ((0, "group"), (2, "class6"), (1, "family")):
        same = [DM.LABELS[models[i]][j] == DM.LABELS[ms[nearest_wrong[i]]][j] for i in wrong]
        chance = np.mean([np.mean([DM.LABELS[m][j] == DM.LABELS[o][j] for o in ms if o != m]) for m in ms])
        out[name] = {"rate": float(np.mean(same)) if same else None, "chance": float(chance), "n_wrong": len(wrong)}
    return out


# ------------------------------------------------------------------------------------- cards

def _log2r(a, b):
    return float(np.log2(b / a)) if a and b and np.isfinite(a) and np.isfinite(b) and a > 0 and b > 0 else None


def _diff(a, b):
    return float(b - a) if a is not None and b is not None and np.isfinite(a) and np.isfinite(b) else None


CARD_KEYS = (("knee_dbfs", "dB"), ("onset_dbfs", "dB"), ("even_share_db", "dB"), ("pre_hp_hz", "oct"),
             ("post_lp_hz", "oct"), ("mid_db", "dB"), ("g0_db", "dB"))


def model_card(model: str, sem: dict, settings: list[dict]) -> dict:
    """settings: [{"cc": {...}, "kind": ..., "card": {...}}] for one model."""
    drive, tone = sem["drive"], sem["tone"]

    def get(dcc, tcc=None):
        for s in settings:
            if s["cc"].get(drive) == dcc and (tone is None or s["cc"].get(tone) == tcc):
                return s["card"]
        return None
    centre = get(64, None if tone is None else 64)
    eff = {}
    for key, unit in CARD_KEYS:
        f = _log2r if unit == "oct" else _diff
        if tone is None:
            dlo, dhi = get(25), get(108)
            de = f(dlo[key], dhi[key]) if dlo and dhi else None
            te = None
        else:
            ds = [f(get(25, t)[key], get(108, t)[key]) for t in (25, 102) if get(25, t) and get(108, t)]
            ds = [d for d in ds if d is not None]
            de = float(np.mean(ds)) if ds else None
            ts = [f(get(d, 25)[key], get(d, 102)[key]) for d in (25, 64, 108) if get(d, 25) and get(d, 102)]
            ts = [t for t in ts if t is not None]
            te = float(np.mean(ts)) if ts else None
        vals = [s["card"][key] for s in settings if s["card"][key] is not None and np.isfinite(s["card"][key])]
        eff[key] = {"centre": None if centre is None else centre[key], "min": min(vals) if vals else None,
                    "max": max(vals) if vals else None, "drive_effect": de, "tone_effect": te, "unit": unit}
    states = {k: sorted({s["card"][k] for s in settings}) for k in ("knee_state", "onset_state", "mid_kind")}
    mids = [s["card"] for s in settings if s["card"]["mid_kind"] != "flat"]
    return {"model": model, "drive": drive, "tone": tone, "note": sem.get("note", ""), "values": eff,
            "states": states, "memory_settings": sum(s["card"]["memory"] for s in settings),
            "mid_f_range_hz": [min(m["mid_f_hz"] for m in mids), max(m["mid_f_hz"] for m in mids)] if mids else None,
            "n": len(settings), "labels": list(DM.LABELS.get(model, ("?", "?", "?")))}


# ------------------------------------------------------------------------------------- driver

def load_all(root: Path, idx: dict):
    recs = []
    for s in idx["settings"]:
        if "file" not in s:
            continue
        arr, sc = P.unpack((root / s["file"]).read_bytes())
        recs.append({**s, "arr": arr, "sc": sc})
    return recs


def analyse_all(root: Path, idx: dict, workers: int = 1) -> dict:
    recs_all = load_all(root, idx)
    reps = [r for r in recs_all if r.get("tag") == "__rep"]
    recs = [r for r in recs_all if not r.get("tag")]
    levels = P.STAIR_LEVELS_V1
    for r in recs:
        r["card"] = card_values(r["sc"], r["arr"], levels)
    models = [r["model"] for r in recs]
    kinds = [r["kind"] for r in recs]
    sem = idx.get("semantics", {})
    out = {"n_fingerprints": len(recs), "n_models": len(set(models)),
           "probe_s": round(P.build_probe("v1")[0].size / P.SR, 1),
           "bytes_mean": float(np.mean([r["bytes"] for r in recs])) if recs else None}
    # repeats: fingerprint identity (the raw-render identity is recorded at render time)
    rep = []
    for r in reps:
        o = next(x for x in recs if x["model"] == r["model"] and x["setting"] == r["setting"])
        same = all(np.array_equal(np.nan_to_num(o["arr"][k], nan=9e3), np.nan_to_num(r["arr"][k], nan=9e3))
                   for k in o["arr"])
        rep.append({"model": r["model"], "setting": r["setting"], "fingerprint_identical": bool(same),
                    "render_bit_identical": r.get("repeat_bit_identical")})
    out["repeats"] = {"rows": rep, "n": len(rep),
                      "identical": int(sum(bool(x["render_bit_identical"]) and x["fingerprint_identical"] for x in rep))}
    # feature sets
    fsets = {}
    for lvl, tag in ((True, ""), (False, "_nolevel")):
        Zc, nc = standardise([card_features(r["card"], level=lvl) for r in recs])
        fsets[f"card{tag}"] = (Zc, len(nc))
        Zs, ns = standardise([scalar_features(r["sc"], level=lvl) for r in recs])
        fsets[f"scalars{tag}"] = (Zs, len(ns))
        blocks = [curve_blocks(r["arr"], r["sc"], level=lvl) for r in recs]
        Zp, npl = block_matrix(blocks, weighted=False)
        fsets[f"curves{tag}"] = (Zp, npl)
        Zw, nw = block_matrix(blocks, weighted=True)
        fsets[f"curves_blockw{tag}"] = (Zw, nw)
    out["loso"], out["wrong_same_label"] = {}, {}
    dists = {}
    for name, (Z, dims) in fsets.items():
        Dm = pairwise(Z)
        dists[name] = Dm
        ranks, nw_, ms = loso(models, Dm)
        out["loso"][name] = {**rates(ranks, models, kinds), "dims": dims}
        out["wrong_same_label"][name] = wrong_same_label(models, nw_, ranks, ms)
        if name == "curves_blockw_nolevel":
            out["confusions"] = confusion_list(models, nw_, ranks, ms, [r["setting"] for r in recs])
    # like-for-like with coarse-v1 (2 x 2 corners plus the centre of the 2 main knobs, CC 24/103/64
    # there): only the corners (CC 25/108 x 25/102) and the centre (64, 64) here; one-knob models
    # their 25 / 64 / 108 points
    sub = np.array([coarse_like(r["cc"], sem.get(r["model"], {})) for r in recs])
    out["loso_coarse_like"] = {}
    ms_sub = [m for m, k in zip(models, sub) if k]
    for name in ("card", "scalars", "curves", "card_nolevel", "scalars_nolevel", "curves_nolevel",
                 "curves_blockw_nolevel"):
        Dm = dists[name][np.ix_(sub, sub)]
        ranks, _, _ = loso(ms_sub, Dm)
        out["loso_coarse_like"][name] = {**rates(ranks, ms_sub, ["coarse"] * len(ms_sub)), "dims": fsets[name][1]}
    out["coarse_v1"] = {"note6": 0.34, "scalars": 0.59, "curves": 0.75,
                        "source": "docs/notes/distortion-id-steps01.md step 0 (288 held-out settings)"}
    # clusters
    out["clusters"] = {"rows": {}}
    for name in ("card_nolevel", "curves_blockw_nolevel", "curves_blockw"):
        cr = cluster_report(models, fsets[name][0])
        for k, v in cr["rows"].items():
            out["clusters"]["rows"][f"{name}:{k}"] = v
    # model-level distance ranks of the known same-pedal pairs
    out["same_pedal_pairs"] = pair_percentiles(models, dists["curves_blockw_nolevel"])
    # cards
    out["cards"] = {}
    for m in sorted(set(models)):
        ss = [{"cc": r["cc"], "kind": r["kind"], "card": r["card"], "setting": r["setting"]}
              for r in recs if r["model"] == m]
        out["cards"][m] = model_card(m, sem.get(m, {"drive": next(iter(ss[0]["cc"])), "tone": None}), ss)
    out["monotone"] = monotone_report(recs, sem)
    out["valid_fraction"] = len(recs) / max(1, sum(1 for s in idx["settings"] if not s.get("tag")))
    out["settings"] = [{"model": r["model"], "setting": r["setting"], "kind": r["kind"], "card": r["card"]}
                       for r in recs]
    out["analysis_s"] = {"mean": float(np.mean([r["analysis_s"] for r in recs])),
                         "max": float(np.max([r["analysis_s"] for r in recs]))}
    return out


def monotone_report(recs: list[dict], sem: dict) -> dict:
    """Step-3 placeholder check "knob maps monotone where expected": per Drive series (Drive 0.2 /
    0.5 / 0.85 at each tone; one-knob models over all 12 points), does g0 rise (> 0.5 dB a step,
    as in the pilot) and do the knee and the harmonic onset not rise (within 1 dB)?"""
    out = {"g0_rises": [0, 0], "knee_falls": [0, 0], "onset_falls": [0, 0], "not_g0": [], "not_knee": [],
           "not_onset": []}
    for m in sorted({r["model"] for r in recs}):
        s = sem.get(m) or {}
        d, t = s.get("drive"), s.get("tone")
        rs = [r for r in recs if r["model"] == m]
        if t is None:
            series = [sorted(rs, key=lambda r: r["cc"][d])]
        else:
            series = [sorted([r for r in rs if r["cc"].get(t) == tc and r["cc"][d] in (25, 64, 108)],
                             key=lambda r: r["cc"][d]) for tc in (25, 102)]
        for ser in series:
            if len(ser) < 3:
                continue
            g = [r["card"]["g0_db"] for r in ser]
            ok = all(b > a + 0.5 for a, b in zip(g, g[1:])) if t is not None else (g[-1] > g[0] + 0.5)
            out["g0_rises"][0] += ok
            out["g0_rises"][1] += 1
            if not ok:
                out["not_g0"].append(f"{m} tone@{ser[0]['cc'].get(t)}")
            for key, lab in (("knee_dbfs", "knee"), ("onset_dbfs", "onset")):
                v = [r["card"][key] for r in ser]
                if any(x is None for x in v):
                    continue
                okk = all(b <= a + 1.0 for a, b in zip(v, v[1:]))
                out[f"{lab}_falls"][0] += okk
                out[f"{lab}_falls"][1] += 1
                if not okk:
                    out[f"not_{lab}"].append(f"{m} tone@{ser[0]['cc'].get(t)}")
    return out


def coarse_like(cc: dict, sem: dict) -> bool:
    d, t = sem.get("drive"), sem.get("tone")
    if d is None:
        return False
    if t is None:
        return cc.get(d) in (25, 64, 108)
    return (cc.get(d), cc.get(t)) in {(25, 25), (25, 102), (108, 25), (108, 102), (64, 64)}


SAME_PEDAL = (("HD2_DistTriangleFuzz", "HD2_DM4FuzzPi"), ("HD2_DistArbitratorFuzz", "HD2_DM4FacialFuzz"),
              ("HD2_DistTycoctaviaFuzz", "HD2_DM4OctaveFuzz"), ("HD2_DistValveDriver", "HD2_DM4TubeDrive"),
              ("HD2_DM4HeavyDistortion", "KillerZ"), ("HD2_DistVerminDist", "HD2_DistRatatouilleDist"),
              ("HD2_DistVerminDist", "HD2_DM4ClassicDistortion"), ("HD2_DistScream808", "HD2_DM4Screamer"),
              ("HD2_DistDeezOneVintage", "HD2_DistDeezOneMod"))


def model_distance(models, Dm):
    ms = sorted(set(models))
    mi = np.array([ms.index(m) for m in models])
    K = np.zeros((len(ms), len(ms)))
    for a in range(len(ms)):
        for b in range(len(ms)):
            K[a, b] = np.median(Dm[np.ix_(mi == a, mi == b)].min(axis=1)) if a != b else 0.0
    K = 0.5 * (K + K.T)
    return ms, K


def pair_percentiles(models, Dm) -> dict:
    ms, K = model_distance(models, Dm)
    iu = np.triu_indices(len(ms), 1)
    allv = K[iu]
    out = {}
    for a, b in SAME_PEDAL:
        if a in ms and b in ms:
            v = K[ms.index(a), ms.index(b)]
            out[f"{a}|{b}"] = {"distance": float(v), "percentile": float(100 * np.mean(allv < v))}
    return out


def confusion_list(models, nearest_wrong, ranks, ms, settings) -> list[dict]:
    """Most frequent (true model -> chosen model) pairs among top-1 misses."""
    from collections import Counter

    c = Counter((models[i], ms[nearest_wrong[i]]) for i, r in enumerate(ranks) if r > 1)
    return [{"true": a, "chosen": b, "n": n, "same_group": DM.LABELS[a][0] == DM.LABELS[b][0]}
            for (a, b), n in c.most_common(25)]


# ------------------------------------------------------------------------------------- output

def short(m: str) -> str:
    return m.replace("HD2_Dist", "").replace("HD2_DM4", "DM4 ")


def _fmt(v, unit="dB", nd=1):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    if unit == "hz":
        return f"{v:,.0f}"
    return f"{v:+.{nd}f}" if unit in ("ddb", "oct") else f"{v:.{nd}f}"


def cards_markdown(res: dict) -> str:
    """All fingerprint cards as one local markdown table set (reports/local/.../cards.md)."""
    lines = ["# dfp-v1 fingerprint cards (T174, local)", "",
             "Centre setting value, then how Drive (0.2 -> 0.85) and tone (0.2 -> 0.8) move it. "
             "Frequencies move in octaves (+1 = doubles). '-' = not measurable.", ""]
    hdr = ("| Model | drive / tone | knee dBFS (D, T) | onset dBFS (D, T) | even share dB (D, T) | "
           "pre high-pass Hz (D oct, T oct) | post low-pass Hz (D oct, T oct) | mid dB (D, T) | g0 dB (D, T) | states |")
    lines += [hdr, "|" + "---|" * 10]
    for m, c in sorted(res["cards"].items(), key=lambda kv: (kv[1]["labels"][0], kv[0])):
        v = c["values"]

        def cell(k, unit):
            x = v[k]
            u = "hz" if unit == "oct" else "dB"
            du = "oct" if unit == "oct" else "ddb"
            return f"{_fmt(x['centre'], u)} ({_fmt(x['drive_effect'], du)}, {_fmt(x['tone_effect'], du)})"
        st = c["states"]
        lines.append(f"| {short(m)} [{c['labels'][0]}] | {c['drive']} / {c['tone'] or '-'} | {cell('knee_dbfs', 'dB')} | "
                     f"{cell('onset_dbfs', 'dB')} | {cell('even_share_db', 'dB')} | {cell('pre_hp_hz', 'oct')} | "
                     f"{cell('post_lp_hz', 'oct')} | {cell('mid_db', 'dB')} | {cell('g0_db', 'dB')} | "
                     f"knee {','.join(st['knee_state'])}; onset {','.join(st['onset_state'])}; "
                     f"mid {','.join(st['mid_kind'])}; memory {c['memory_settings']}/{c['n']} |")
    return "\n".join(lines) + "\n"


def write_figures(root: Path, idx: dict, res: dict, out: Path) -> None:
    import sys
    sys.path.insert(0, str(root / "scripts"))
    import distortion_id_steps01 as S01

    COL = S01.COLORS
    out.mkdir(parents=True, exist_ok=True)
    recs = [r for r in load_all(root, idx) if not r.get("tag")]
    sem = idx.get("semantics", {})
    L = np.asarray(P.STAIR_LEVELS_V1)
    f = P.FGRID
    mdir = out / "models"
    mdir.mkdir(exist_ok=True)
    for m in sorted({r["model"] for r in recs}):
        rs = [r for r in recs if r["model"] == m]
        s = sem.get(m, {})
        dk, tk = s.get("drive"), s.get("tone")
        sel = [r for r in rs if tk is None or r["cc"].get(tk) == 64 or r["kind"] == "grid" and r["cc"].get(tk) == 25]
        ser = []
        for i, r in enumerate(sorted(sel, key=lambda r: (r["cc"].get(tk, 0) if tk else 0, r["cc"][dk]))):
            st = r["arr"]["stair1k_db"]
            g = st[0] - L
            c = COL[i % len(COL)]
            ser.append((L, g - g[0], f"{r['setting']} H1 gain", c, False))
            ser.append((L, np.maximum(st[1], st[2]) - st[0], f"{r['setting']} max(H2,H3) re H1", c, True))
        S01.svg_lines(mdir / f"{m}_harmonics.svg", ser, f"{short(m)}: 1 kHz staircase, gain change and harmonics",
                      "input level (dBFS peak)", "dB", ylim=(-100, 5), w=900)
        ser = []
        eqsel = [r for r in rs if r["cc"].get(dk) == 64] if tk else [r for r in rs if r["cc"][dk] in (25, 64, 108)]
        for i, r in enumerate(eqsel):
            c = COL[i % len(COL)]
            ss = r["arr"]["ss_db"]
            ser.append((f, ss - np.interp(0.0, np.log(f / 1000.0), ss), f"{r['setting']} small-signal", c, False))
            ser.append((f, r["arr"]["P_db"], f"{r['setting']} pre", c, True))
            ser.append((f, r["arr"]["Q_db"], f"{r['setting']} post", c, True))
        S01.svg_lines(mdir / f"{m}_eq.svg", ser, f"{short(m)}: small-signal response and pre/post split "
                      "(0 dB at 1 kHz)", "frequency (Hz)", "dB", logx=True, ylim=(-45, 25), w=900)
    # summary scatter: knee vs onset at the centre setting, coloured by the provisional class
    cards = res["cards"]
    ms = sorted(cards)
    xy, lab, cat = [], [], []
    for m in ms:
        v = cards[m]["values"]
        k, o = v["knee_dbfs"]["centre"], v["onset_dbfs"]["centre"]
        if k is None or o is None:
            continue
        xy.append((k, o))
        lab.append(short(m))
        cat.append(cards[m]["labels"][2])
    if xy:
        S01.svg_scatter(out / "knee_vs_onset.svg", np.array(xy, float), lab, cat,
                        "Centre setting: knee (x, dBFS) vs harmonic onset (y, dBFS); colour = provisional class")
    xy, lab, cat = [], [], []
    for m in ms:
        v = cards[m]["values"]
        e, q = v["even_share_db"]["centre"], v["post_lp_hz"]["centre"]
        if e is None or q is None or not np.isfinite(e) or not np.isfinite(q):
            continue
        xy.append((max(e, -60.0), np.log2(q / 1000.0)))
        lab.append(short(m))
        cat.append(cards[m]["labels"][2])
    if xy:
        S01.svg_scatter(out / "even_vs_postlp.svg", np.array(xy, float), lab, cat,
                        "Centre setting: even share (x, dB, floor -60) vs post low-pass (y, octaves re 1 kHz)")
    # model distance heatmap ordered by group
    Z = None
    blocks = [curve_blocks(r["arr"], r["sc"], level=False) for r in recs]
    Z, _ = block_matrix(blocks, weighted=True)
    msd, K = model_distance([r["model"] for r in recs], pairwise(Z))
    order = sorted(range(len(msd)), key=lambda i: (DM.LABELS[msd[i]][0], msd[i]))
    S01.svg_heatmap(out / "model_distance.svg", K[np.ix_(order, order)], [short(msd[i]) for i in order],
                    "Model distance (curves, level-free, block-weighted), ordered by confusable group",
                    groups=[DM.LABELS[msd[i]][0] for i in order])
    # LOSO rate per feature set
    ser = []
    names = list(res["loso"])
    xs = np.arange(len(names))
    ser.append((xs, [res["loso"][n]["top1"][0] for n in names], "top-1", COL[0], False))
    ser.append((xs, [res["loso"][n]["top5"][0] for n in names], "top-5", COL[1], False))
    S01.svg_lines(out / "loso_rates.svg", ser, "Leave-one-setting-out model identification per feature set: "
                  + ", ".join(f"{i}={n}" for i, n in enumerate(names)), "feature set (index)", "rate",
                  ylim=(0, 1), w=1100)
    (out / "figures.json").write_text(json.dumps({"models_dir": str(mdir),
                                                  "feature_sets": names}, indent=1))
