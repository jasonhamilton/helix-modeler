"""The dfp-v1 pilot probe and its fingerprint (T173, step 2 of docs/notes/distortion-identification.md).

RESEARCH CODE. The human approved the pilot ("yes measure", DECISIONS 2026-10-18) with five method
choices: a 5 dB tone staircase across the clipping onset as the knee source; the static curve read
about 10 dB above the measured knee; the pre/post split by fundamental-only alignment (>= 50%
overlap, minimum-phase post-filter, harmonics at or above Nyquist excluded); the non-W-H flag from
the sweep odd-harmonic residual; the sweep length decided from measured Native ringing. Nothing
here is an admitted score, an adopted method, a class list or a bar.

Probe (one mono 48 kHz signal, levels dBFS peak):

    lead-in       0.5 s silence (output noise floor)
    sweeps        synchronized ESS 20 Hz-20 kHz at SWEEP_LEVELS (-78 small-signal, then every
                  6 dB from -66 to 0), each followed by a 1 s gap. 6 s in `build_probe("main")`,
                  1.5 s in `build_probe("short")` (the ringing variant, sweeps only)
    staircases    1 kHz and 220 Hz tones, STAIR_LEVELS (-71 to -1 in 5 dB steps, includes -6),
                  0.3 s per step, ascending
    two-tone      4 pairs x 4 levels (combined peak), 0.5 s each
    bursts        220 Hz and 1 kHz: -30 / -6 / -30 dBFS, 0.5 s each

`analyse(y, layout)` turns one render into a fingerprint: a dict of small arrays (stored as float16,
about 4 KB compressed per setting) and a dict of scalars. No raw audio is kept.
"""
from __future__ import annotations

import io
import json

import numpy as np

from . import distortion_id as D

SR = 48000
PROBE_VERSION = "dfp-v1-pilot"
F1, F2 = 20.0, 20000.0
SWEEP_LEVELS = [-78.0] + [float(v) for v in range(-66, 1, 6)]          # 13 levels
STAIR_LEVELS = [float(v) for v in range(-71, 0, 5)]                     # -71 .. -1, 15 levels
STAIR_LEVELS_V1 = [float(v) for v in range(-91, 0, 5)]                  # step 3: -91 .. -1, 19 levels
STAIR_FREQS = (1000.0, 220.0)
STAIR_STEP_S = 0.3
TWO_TONE_PAIRS = ((440.0, 554.0), (110.0, 165.0), (220.0, 330.0), (80.0, 2000.0))
TWO_TONE_LEVELS = (-30.0, -18.0, -12.0, -6.0)                          # combined peak
TWO_TONE_S = 0.5
BURST_FREQS = (220.0, 1000.0)
BURST_LEVELS = (-30.0, -6.0, -30.0)
BURST_STEP_S = 0.5
GAP_S = 1.0
SECTION_GAP_S = 0.5
FGRID = 1000.0 * 2.0 ** (np.arange(-24, 25) / 6.0)                     # 1/6 octave
FGRID = FGRID[FGRID >= 40.0]                                            # 49 points, 63 Hz .. 16 kHz
ORDERS = 9
SPLIT_ORDERS = (1, 2, 3, 4, 5)
K_CURVE = int(0.45 * SR // 1000.0)                                      # 21 harmonics of 1 kHz
HARM_LEVELS = (-30.0, -12.0, 0.0)                                       # stored H2..H5 levels
CURVE_POINTS = 65
SWEEP_T = {"main": 6.0, "short": 1.5, "v1": 6.0}
PROBE_VERSIONS = {"main": PROBE_VERSION, "short": PROBE_VERSION, "v1": "dfp-v1"}
ONSET_REL_DB = -60.0      # a harmonic "switches on" when H2 or H3 is within this of H1


# ------------------------------------------------------------------------------- probe

def _tone(f, level_db, n, phase=0.0):
    t = np.arange(n) / SR
    return 10 ** (level_db / 20) * np.cos(2 * np.pi * f * t + phase)


def build_probe(kind: str = "main"):
    """(signal float64, layout dict). `kind` "main" = the full pilot probe with 6 s sweeps;
    "short" = lead-in and 1.5 s sweeps only (the ringing comparison); "v1" = the step-3 probe
    (T174): "main" with the tone staircases starting at -91 dBFS (STAIR_LEVELS_V1, approved
    "about -90 dBFS")."""
    T = SWEEP_T[kind]
    x, L, T_act = D.ess_sync(F1, F2, T, SR)
    parts, segs, pos = [], [], 0

    def add(a, name, **meta):
        nonlocal pos
        parts.append(np.asarray(a, float))
        segs.append({"name": name, "start": pos, "n": len(a), **meta})
        pos += len(a)

    add(np.zeros(int(0.5 * SR)), "lead_in")
    gap = int(GAP_S * SR)
    for lv in SWEEP_LEVELS:
        add(np.concatenate([10 ** (lv / 20) * x, np.zeros(gap)]), "sweep", level_db=lv,
            sweep_n=len(x))
    stair_levels = stair_levels_for(kind)
    if kind in ("main", "v1"):
        n_step = int(STAIR_STEP_S * SR)
        for f in STAIR_FREQS:
            add(np.zeros(int(SECTION_GAP_S * SR)), "gap")
            t = np.arange(n_step * len(stair_levels)) / SR
            amp = np.repeat(10 ** (np.asarray(stair_levels) / 20), n_step)
            add(amp * np.cos(2 * np.pi * f * t), "stair", f0=f, step_n=n_step)
        n_tt = int(TWO_TONE_S * SR)
        add(np.zeros(int(SECTION_GAP_S * SR)), "gap")
        for f1, f2 in TWO_TONE_PAIRS:
            for lv in TWO_TONE_LEVELS:
                a = _tone(f1, lv - 6.0206, n_tt) + _tone(f2, lv - 6.0206, n_tt)
                add(a, "two_tone", f1=f1, f2=f2, level_db=lv)
        n_b = int(BURST_STEP_S * SR)
        for f in BURST_FREQS:
            add(np.zeros(int(SECTION_GAP_S * SR)), "gap")
            env = np.concatenate([np.full(n_b, 10 ** (lv / 20)) for lv in BURST_LEVELS])
            add(env * np.sin(2 * np.pi * f * np.arange(env.size) / SR), "burst", f0=f, step_n=n_b)
    add(np.zeros(int(SECTION_GAP_S * SR)), "tail")
    sig = np.concatenate(parts)
    layout = {"version": PROBE_VERSIONS[kind], "kind": kind, "sr": SR, "sweep_T": T_act, "sweep_L": L,
              "n": int(sig.size), "segments": segs, "stair_levels": list(stair_levels)}
    return sig, layout


def stair_levels_for(kind: str) -> list[float]:
    return STAIR_LEVELS_V1 if kind == "v1" else STAIR_LEVELS


def layout_stair_levels(layout) -> list[float]:
    """The staircase levels of a layout (pilot layouts carry none: STAIR_LEVELS)."""
    return [float(v) for v in layout.get("stair_levels") or STAIR_LEVELS]


def unit_sweep(layout):
    x, L, _ = D.ess_sync(F1, F2, SWEEP_T[layout["kind"]], SR)
    return x, L


# ------------------------------------------------------------------------------- sweeps

def sweep_records(y, layout):
    return {s["level_db"]: np.asarray(y[s["start"]:s["start"] + s["n"]], float)
            for s in layout["segments"] if s["name"] == "sweep"}


def sweep_tables(y, layout, orders: int = ORDERS):
    x, L = unit_sweep(layout)
    recs = sweep_records(y, layout)
    lev, M = D.ess_harmonic_table(recs, x, L, SR, F1, F2, FGRID, orders=orders)
    return lev, M


def leakage(y, layout, level_db: float, L_short: float, orders: int = 6, rel_floor_db: float = 40.0):
    """How much a shorter sweep's windows would be contaminated, measured on this (longer) record.

    With a sweep of time constant `L_short` the window boundary between orders k and k+1 sits half
    way between them, gap_k / 2 after order k+1 (gap_k = L_short ln((k+1)/k)). Measured here, where
    the orders are far apart: the part of order k+1's response later than gap_k / 2, plus the part
    of order k's response earlier than gap_k / 2 before it (each only up to half of THIS record's
    gap, so a neighbour is never counted). Positions are the predicted -L ln k plus the measured
    delay of order 1. Both parts are compared with order k's own response per frequency, on the
    fingerprint grid (bins k f, f in FGRID), so the deconvolution's band-edge ringing at 20 Hz and
    20 kHz does not count.

    Returns per k: `contamination_p95_db` / `_max_db` over the bins where order k is within
    `rel_floor_db` of its own peak and of order 1's peak (NaN if order k carries no energy),
    `rel_db` (order k's peak re order 1) and `gap_short_ms`. A contamination of c dB changes the
    measured level by at most 20 log10(1 + 10^(c/20)) dB: -20 -> 0.83, -30 -> 0.27, -40 -> 0.09."""
    x, L = unit_sweep(layout)
    rec = sweep_records(y, layout)[level_db]
    nfft = 1 << int(np.ceil(np.log2(len(rec) + int(L * np.log(orders + 2) * SR) + SR)))
    h = D.ess_deconvolve(rec, x, SR, F1, F2, nfft)
    f = np.fft.rfftfreq(nfft, 1 / SR)
    pos = {k: -D.harmonic_delay_s(L, k) * SR for k in range(1, orders + 2)}
    pos[0] = pos[1] + 0.6 * SR
    i1 = np.arange(int(pos[1] - 0.002 * SR), int(pos[1] + 0.05 * SR))
    d1 = int(i1[np.argmax(np.abs(h[i1 % nfft]))] - pos[1])
    c = {k: pos[k] + d1 for k in pos}

    def spec(a, b, ref):
        a, b = int(np.floor(a)), int(np.ceil(b))
        if b <= a:
            return np.zeros(f.size, complex)
        idx = np.arange(a, b)
        buf = np.zeros(nfft)
        buf[(idx - int(round(ref))) % nfft] = h[idx % nfft]
        return np.fft.rfft(buf)
    H = {k: spec(c[k] - 0.5 * (c[k] - c[k + 1]), c[k] + 0.5 * (c[k - 1] - c[k]), c[k])
         for k in range(1, orders + 1)}
    mag = {k: D.band_mag_db(f, H[k], np.minimum(k * FGRID, 0.45 * SR)) for k in H}
    out = {"rel_db": {}, "contamination_p95_db": {}, "contamination_max_db": {}, "gap_short_ms": {}}
    top1 = np.nanmax(mag[1])
    for k in range(1, orders + 1):
        out["rel_db"][k] = float(np.nanmax(mag[k]) - top1)
    for k in range(1, orders):
        gs = (D.harmonic_delay_s(L_short, k + 1) - D.harmonic_delay_s(L_short, k)) * SR
        gm = c[k] - c[k + 1]
        out["gap_short_ms"][k] = 1e3 * gs / SR
        tail = spec(c[k + 1] + 0.5 * gs, c[k + 1] + 0.5 * gm, c[k])
        pre = spec(c[k] - 0.5 * gm, c[k] - 0.5 * gs, c[k])
        leak = D.band_mag_db(f, tail + pre, np.minimum(k * FGRID, 0.45 * SR))
        m = (k * FGRID <= 0.45 * SR) & (mag[k] >= np.nanmax(mag[k]) - rel_floor_db) \
            & (mag[k] >= top1 - rel_floor_db)
        if not m.any():
            out["contamination_p95_db"][k] = np.nan
            out["contamination_max_db"][k] = np.nan
            continue
        d = (leak - mag[k])[m]
        out["contamination_p95_db"][k] = float(np.percentile(d, 95))
        out["contamination_max_db"][k] = float(d.max())
    return out


def gap_ms(L: float, k: int) -> float:
    """Time between orders k and k+1 (ms)."""
    return 1e3 * (D.harmonic_delay_s(L, k + 1) - D.harmonic_delay_s(L, k))


# ------------------------------------------------------------------------------- tones

def stair_harmonics(y, layout, f0: float, K: int):
    s = next(s for s in layout["segments"] if s["name"] == "stair" and s["f0"] == f0)
    n = s["step_n"]
    seg = np.asarray(y[s["start"]:s["start"] + s["n"]], float)
    a0, a1 = int(0.1 * SR), int(0.3 * SR)
    out = []
    for i in range(len(layout_stair_levels(layout))):
        a = D.sine_harmonics(seg[i * n + a0:i * n + a1], f0, SR, K)
        t0 = (i * n + a0) / SR
        out.append(a * np.exp(-1j * 2 * np.pi * np.arange(K + 1) * f0 * t0))
    return np.array(out)


def two_tone_products(f1: float, f2: float, max_order: int = 5):
    fs = {}
    for m in range(-max_order, max_order + 1):
        for n in range(-max_order, max_order + 1):
            if abs(m) + abs(n) == 0 or abs(m) + abs(n) > max_order:
                continue
            f = m * f1 + n * f2
            if 0 < f < 0.45 * SR:
                fs.setdefault(round(f, 6), (m, n))
    return fs


def two_tone_imd(seg, f1, f2):
    """IMD of a two-tone record: least-squares amplitudes at every m f1 + n f2 (|m| + |n| <= 5).
    Products that are harmonics of either tone are not counted. Returns dB relative to the summed
    power of the two tones: total IMD, the 2nd-order pair (f2 - f1, f1 + f2) and the 3rd-order pair
    (2 f1 - f2, 2 f2 - f1)."""
    seg = np.asarray(seg, float)[int(0.1 * SR):]
    t = np.arange(seg.size) / SR
    prods = two_tone_products(f1, f2)
    freqs = sorted(prods)
    B = np.concatenate([np.stack([np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)], 1)
                        for f in freqs] + [np.ones((t.size, 1))], 1)
    c, *_ = np.linalg.lstsq(B, seg, rcond=None)
    amp = {f: float(np.hypot(c[2 * i], c[2 * i + 1])) for i, f in enumerate(freqs)}

    def is_harm(f):
        return any(abs(f / g - round(f / g)) < 1e-9 for g in (f1, f2))
    p_t = amp[round(f1, 6)] ** 2 + amp[round(f2, 6)] ** 2 + D.EPS
    imd = sum(a ** 2 for f, a in amp.items() if not is_harm(f))

    def pair(fl):
        fl = [round(f, 6) for f in fl if 0 < f < 0.45 * SR]
        fl = [f for f in fl if f in amp and not is_harm(f)]
        return 10 * np.log10(sum(amp[f] ** 2 for f in fl) / p_t + 1e-15) if fl else np.nan
    return {"imd_db": float(10 * np.log10(imd / p_t + 1e-15)),
            "imd2_db": float(pair([f2 - f1, f1 + f2])),
            "imd3_db": float(pair([2 * f1 - f2, 2 * f2 - f1])),
            "tones_db": float(10 * np.log10(p_t))}


def burst_stats(y, layout, f0):
    s = next(s for s in layout["segments"] if s["name"] == "burst" and s["f0"] == f0)
    n = s["step_n"]
    seg = np.asarray(y[s["start"]:s["start"] + s["n"]], float)
    st = D.settle_after_burst(seg, SR, f0, 2 * n / SR + 1 / f0, n / SR - 2 / f0)
    # gain recovery: fundamental level per 10 ms frame in the last step vs its final value
    fr = int(0.01 * SR)
    last = seg[2 * n:3 * n]
    g = []
    for i in range(0, last.size - fr + 1, fr):
        a = D.sine_harmonics(last[i:i + fr], f0, SR, 1)
        g.append(D.db(abs(a[1] * np.exp(1j * 0))))
    g = np.array(g)
    ref = np.median(g[-10:])
    d = np.abs(g - ref)
    over = np.nonzero(d > 0.5)[0]
    st["gain_change_db"] = float(d[:5].max())
    st["gain_settle_s"] = float((over[-1] + 1) * fr / SR) if over.size else 0.0
    return st


# ------------------------------------------------------------------------------- fingerprint

def _q_from_split(sp, floor_db: float = -60.0):
    m = sp["identifiable"] & np.isfinite(sp["Q_db"])
    if m.sum() < 3:
        return None
    return D.min_phase_from_db(sp["f"][m], np.maximum(sp["Q_db"][m], floor_db), SR)


def corner_below(f, v, ref_band=(1000.0, 4000.0), drop=3.0):
    """Bass-cut corner: the highest frequency below `ref_band` where `v` first falls `drop` dB
    under its maximum over `ref_band` (walking down from the band). NaN if it never does."""
    f, v = np.asarray(f, float), np.asarray(v, float)
    band = (f >= ref_band[0]) & (f <= ref_band[1]) & np.isfinite(v)
    if not band.any():
        return np.nan
    top = np.nanmax(v[band])
    i0 = int(np.nonzero(band)[0][0])
    for i in range(i0, -1, -1):
        if np.isfinite(v[i]) and v[i] < top - drop:
            if i + 1 < f.size and np.isfinite(v[i + 1]) and v[i + 1] != v[i]:
                # interpolate in log f between i and i+1
                u = (top - drop - v[i]) / (v[i + 1] - v[i])
                return float(np.exp(np.log(f[i]) + u * (np.log(f[i + 1]) - np.log(f[i]))))
            return float(f[i])
    return np.nan


def corner_above(f, v, ref_band=(63.0, 500.0), drop=3.0):
    """Treble-cut corner: the lowest frequency above the maximum over `ref_band` where `v` falls
    `drop` dB under that maximum. NaN if it never does inside the grid."""
    f, v = np.asarray(f, float), np.asarray(v, float)
    band = (f >= ref_band[0]) & (f <= ref_band[1]) & np.isfinite(v)
    if not band.any():
        return np.nan
    top = np.nanmax(v[band])
    i0 = int(np.nonzero(band)[0][np.nanargmax(v[band])])
    for i in range(i0, f.size):
        if np.isfinite(v[i]) and v[i] < top - drop:
            if i > 0 and np.isfinite(v[i - 1]) and v[i - 1] != v[i]:
                u = (top - drop - v[i - 1]) / (v[i] - v[i - 1])
                return float(np.exp(np.log(f[i - 1]) + u * (np.log(f[i]) - np.log(f[i - 1]))))
            return float(f[i])
    return np.nan


def fit_first_order(f, v, kind: str, f_lo: float = 20.0, f_hi: float = 20000.0, n: int = 241):
    """Least-squares first-order fit in dB: v(f) = c + 20 log10 |H(f)|, H = (j f/fc) / (1 + j f/fc)
    for kind "hp", 1 / (1 + j f/fc) for "lp". Grid search of fc (log grid), c free. Returns
    (fc_hz, rms_db, n_points); NaN when fewer than 4 finite points."""
    f, v = np.asarray(f, float), np.asarray(v, float)
    m = np.isfinite(v)
    if m.sum() < 4:
        return (np.nan, np.nan, int(m.sum()))
    f, v = f[m], v[m]
    best = (np.inf, np.nan)
    for fc in np.geomspace(f_lo, f_hi, n):
        u = f / fc
        h = 20 * np.log10(u / np.sqrt(1 + u * u)) if kind == "hp" else -10 * np.log10(1 + u * u)
        r = v - h
        e = float(np.sqrt(np.mean((r - r.mean()) ** 2)))
        if e < best[0]:
            best = (e, fc)
    return (float(best[1]), best[0], int(m.sum()))


def knee_ref(levels_db, fund_out_db, g0_db: float, drop: float = 1.0):
    """1 dB compression point against an external small-signal gain `g0_db` (from the quietest
    sweep) instead of the first staircase step: None if never reached, -inf if the first step is
    already `drop` dB down (the knee is below the staircase)."""
    L = np.asarray(levels_db, float)
    g = np.asarray(fund_out_db, float) - L - g0_db
    below = np.nonzero(g <= -drop)[0]
    if below.size == 0:
        return None
    i = int(below[0])
    if i == 0:
        return -np.inf
    return float(L[i - 1] + (-drop - g[i - 1]) * (L[i] - L[i - 1]) / (g[i] - g[i - 1] + D.EPS))


def harmonic_onset(levels_db, stair_db, rel_db: float = ONSET_REL_DB) -> dict:
    """The input level where the 2nd or 3rd harmonic first comes within `rel_db` of the
    fundamental on the 1 kHz staircase (`stair_db`: rows H1..H9 in dB, columns levels).

    Native drives are often exactly linear up to a threshold (harmonics at the -130 to -160 dB
    float floor) and then switch on within one 5 dB step (T173). state: "within" (onset_dbfs is
    the first such step), "below" (already on at the first step: the onset is below the
    staircase; onset_dbfs = the first level), "never" (not reached; onset_dbfs None).
    `below_db`: the largest harmonic level re H1 below the onset (None if none)."""
    L = np.asarray(levels_db, float)
    st = np.asarray(stair_db, float)
    hrel = np.maximum(st[1], st[2]) - st[0]
    idx = np.nonzero(hrel > rel_db)[0]
    if idx.size == 0:
        return {"onset_dbfs": None, "state": "never", "below_db": float(np.nanmax(hrel))}
    i = int(idx[0])
    return {"onset_dbfs": float(L[i]), "state": "below" if i == 0 else "within",
            "below_db": float(np.nanmax(hrel[:i])) if i > 0 else None}


def mid_feature(f, ss_db, sat_db, min_db: float = 3.0) -> dict:
    """The mid feature of a setting: a hump in the small-signal response (peak 400 Hz-2.5 kHz
    above the lower of its 100 Hz and 10 kHz levels) or a scoop in the saturated response (dip
    250 Hz-4 kHz), whichever is larger; "flat" if neither reaches `min_db`. db is signed:
    positive for a hump, negative for a scoop."""
    h = hump(f, ss_db)
    sq = scoop(f, sat_db)
    hd = np.nanmin([h["above_100_db"], h["above_10k_db"]]) if np.isfinite(h["f_hz"]) else np.nan
    sd = sq["depth_db"] if sq["depth_db"] is not None else np.nan
    hd = float(hd) if np.isfinite(hd) else -np.inf
    sd = float(sd) if np.isfinite(sd) else -np.inf
    if max(hd, sd) < min_db:
        return {"kind": "flat", "f_hz": None, "db": 0.0}
    if hd >= sd:
        return {"kind": "hump", "f_hz": float(h["f_hz"]), "db": hd}
    return {"kind": "scoop", "f_hz": float(sq["f_hz"]), "db": -sd}


def scoop(f, v, lo=250.0, hi=4000.0):
    """Mid scoop: the minimum of `v` between `lo` and `hi`, its depth below the lower of the two
    maxima on either side (inside the 63 Hz-16 kHz grid). Depth <= 0 means no scoop."""
    f, v = np.asarray(f, float), np.asarray(v, float)
    ok = np.isfinite(v)
    m = ok & (f >= lo) & (f <= hi)
    if m.sum() < 3:
        return {"f_hz": np.nan, "depth_db": np.nan}
    i = int(np.nonzero(m)[0][np.argmin(v[m])])
    left = v[:i][ok[:i]]
    right = v[i + 1:][ok[i + 1:]]
    if left.size == 0 or right.size == 0:
        return {"f_hz": float(f[i]), "depth_db": 0.0}
    return {"f_hz": float(f[i]), "depth_db": float(min(left.max(), right.max()) - v[i])}


def hump(f, v, lo=400.0, hi=2500.0):
    """Mid hump: the maximum of `v` between `lo` and `hi`, and how far it stands above v(100 Hz)
    and above v(10 kHz)."""
    f, v = np.asarray(f, float), np.asarray(v, float)
    m = np.isfinite(v) & (f >= lo) & (f <= hi)
    if not m.any():
        return {"f_hz": np.nan, "above_100_db": np.nan, "above_10k_db": np.nan}
    i = int(np.nonzero(m)[0][np.argmax(v[m])])
    vi = lambda q: float(np.interp(np.log(q), np.log(f[np.isfinite(v)]), v[np.isfinite(v)]))
    return {"f_hz": float(f[i]), "above_100_db": float(v[i] - vi(100.0)),
            "above_10k_db": float(v[i] - vi(10000.0))}


def analyse(y, layout, *, full: bool = True) -> tuple[dict, dict]:
    """(arrays, scalars) of one render of the probe. `full=False` (the "short" probe) computes the
    sweep tables only."""
    y = np.asarray(y, float)
    arr, sc = {}, {}
    lead = next(s for s in layout["segments"] if s["name"] == "lead_in")
    nl = y[lead["start"] + int(0.1 * SR):lead["start"] + lead["n"]]
    sc["noise_floor_dbfs_rms"] = float(D.db(np.sqrt(np.mean(nl ** 2))))
    sc["peak_dbfs"] = float(D.db(np.max(np.abs(y))))

    lev, M = sweep_tables(y, layout)
    i1k = int(np.argmin(np.abs(np.log(FGRID / 1000.0))))
    arr["M1"] = M[1]                                                   # (49, 13), dB output
    arr["Mh"] = np.stack([M[k][:, [list(lev).index(v) for v in HARM_LEVELS]] for k in (2, 3, 4, 5)])
    arr["ss_db"] = M[1][:, 0] - lev[0]                                 # small-signal total response
    sc["g0_ss_1k_db"] = float(arr["ss_db"][i1k])
    g = M[1][i1k] - lev
    sc["sweep_gain_1k_db"] = [float(v) for v in g]
    sc["sweep_compression_at_minus66_db"] = float(g[0] - g[1])
    arr["sat_db"] = M[1][:, -1] - M[1][i1k, -1]                         # saturated response, 0 dBFS
    sc["L_s"] = float(unit_sweep(layout)[1])
    if not full:
        return arr, sc

    sp = D.level_shift_split(lev, {k: M[k] for k in SPLIT_ORDERS}, FGRID, f_ref=1000.0)
    ident = sp["identifiable"]
    arr["P_db"] = np.where(ident, sp["P_db"], np.nan)
    arr["Q_db"] = np.where(ident, sp["Q_db"], np.nan)
    sc["split_identifiable_fraction"] = float(np.mean(ident))
    sc["fund_residual_db"] = float(sp["residual_rms_db"])
    hr = sp["harmonic_residual_db"]
    sc["harmonic_residual_db"] = {str(k): (None if not np.isfinite(v) else float(v)) for k, v in hr.items()}
    odd = [v for k, v in hr.items() if k % 2 == 1 and np.isfinite(v)]
    sc["odd_harmonic_residual_db"] = float(max(odd)) if odd else None
    sc["P_corner_hz"] = corner_below(FGRID, arr["P_db"])
    sc["P_100_db"] = float(np.interp(np.log(100.0), np.log(FGRID[ident]), sp["P_db"][ident])) \
        if ident.sum() >= 2 and FGRID[ident].min() <= 100.0 else None
    sc["Q_100_db"] = float(np.interp(np.log(100.0), np.log(FGRID[ident]), sp["Q_db"][ident])) \
        if ident.sum() >= 2 and FGRID[ident].min() <= 100.0 else None
    sc["Q_corner_hz"] = corner_above(FGRID, arr["Q_db"], ref_band=(63.0, 16000.0))
    sc["ss_hump"] = hump(FGRID, arr["ss_db"])
    sc["ss_scoop"] = scoop(FGRID, arr["ss_db"])
    sc["sat_scoop"] = scoop(FGRID, arr["sat_db"])
    sc["Q_scoop"] = scoop(FGRID, arr["Q_db"])
    sc["ss_corner_above_hz"] = corner_above(FGRID, arr["ss_db"], ref_band=(63.0, 16000.0))
    sc["sat_corner_above_hz"] = corner_above(FGRID, arr["sat_db"], ref_band=(63.0, 16000.0))
    for nm, kind in (("P", "hp"), ("Q", "lp")):
        fc, rms, npt = fit_first_order(FGRID, arr[f"{nm}_db"], kind)
        sc[f"{nm}_{kind}1_fit"] = {"fc_hz": fc, "rms_db": rms, "n": npt}
    sc["mid_feature"] = mid_feature(FGRID, arr["ss_db"], arr["sat_db"])
    sc["ss_tilt_3k_200_db"] = float(np.interp(np.log(3000.0), np.log(FGRID), arr["ss_db"])
                                    - np.interp(np.log(200.0), np.log(FGRID), arr["ss_db"]))

    # staircases
    a1k = stair_harmonics(y, layout, 1000.0, K_CURVE)
    a220 = stair_harmonics(y, layout, 220.0, ORDERS)
    arr["stair1k_db"] = D.db(np.abs(a1k[:, 1:ORDERS + 1])).T             # (9, 15)
    arr["stair220_db"] = D.db(np.abs(a220[:, 1:ORDERS + 1])).T
    L = np.asarray(layout_stair_levels(layout))
    fund = D.db(np.abs(a1k[:, 1]))
    gain = fund - L
    sc["g0_stair_1k_db"] = float(gain[0])
    sc["stair_gain_1k_db"] = [float(v) for v in gain]
    sc["stair_compression_first_step_db"] = float(gain[0] - gain[1])
    kn = D.p1db_from_levels(L, fund)
    sc["knee_1k_dbfs"] = kn
    sc["knee_at_floor"] = bool(kn is not None and kn <= L[0] + 1e-9)
    kr = knee_ref(L, fund, sc["g0_ss_1k_db"])
    sc["knee_1k_ref_sweep_dbfs"] = kr if kr is None or np.isfinite(kr) else "below_staircase"
    kn220 = D.p1db_from_levels(L, D.db(np.abs(a220[:, 1])))
    sc["knee_220_dbfs"] = kn220
    on = harmonic_onset(L, arr["stair1k_db"])
    sc["harmonic_onset_dbfs"] = on["onset_dbfs"]
    sc["harmonic_onset_state"] = on["state"]
    sc["harmonics_below_onset_db"] = on["below_db"]
    thd = 10 * np.log10(np.sum(np.abs(a1k[:, 2:]) ** 2, 1) / (np.abs(a1k[:, 1]) ** 2 + D.EPS) + 1e-15)
    sc["thd_1k_db"] = [float(v) for v in thd]
    j6 = int(np.argmin(np.abs(L + 6.0)))
    sc["compression_at_minus6_db"] = float(gain[0] - gain[j6])
    sc["thd_1k_at_minus6_db"] = float(thd[j6])
    ev = np.sum(np.abs(a1k[:, 2::2]) ** 2, 1)
    od = np.sum(np.abs(a1k[:, 1::2]) ** 2, 1)
    sc["even_share_raw_db"] = [float(v) for v in 10 * np.log10(ev / (ev + od + D.EPS) + 1e-15)]

    # static curve about 10 dB above the measured knee, post-filter removed (min phase from split)
    Qhat = _q_from_split(sp)
    target = (kn if kn is not None else -16.0) + 10.0
    j = int(np.argmin(np.abs(L - target)))
    sc["curve_level_dbfs"] = float(L[j])
    sc["curve_post_corrected"] = Qhat is not None
    c = D.chebyshev_coeffs(a1k[j], 1000.0, post=Qhat)
    A = 10 ** (L[j] / 20)
    xs = np.linspace(-A, A, CURVE_POINTS)
    yc = D.chebyshev_curve(c, A, xs)
    arr["curve_y"] = yc
    arr["cheb_c"] = c
    sym = D.symmetry_stats(c, A)
    sc["even_share_db"] = float(sym["even_share_db"])
    sc["ceiling_ratio"] = float(sym["ceiling_ratio"]) if np.isfinite(sym["ceiling_ratio"]) else None
    sc["dc_re_fund"] = float(sym["dc_re_fund"]) if np.isfinite(sym["dc_re_fund"]) else None
    xd = np.linspace(-A, A, 1601)
    yd = D.chebyshev_curve(c, A, xd)
    hp = D.fit_rapp(xd[xd > 0], yd[xd > 0])
    hn = D.fit_rapp(-xd[xd < 0], -yd[xd < 0])
    sc["rapp_pos"] = {"g0": hp[0], "v": hp[1], "p": hp[2], "rms_db": hp[3]}
    sc["rapp_neg"] = {"g0": hn[0], "v": hn[1], "p": hn[2], "rms_db": hn[3]}
    # top level, uncorrected even share (what a reader of the raw harmonics would see)
    sc["even_share_raw_top_db"] = sc["even_share_raw_db"][-1]

    # two-tone
    tt = np.full((len(TWO_TONE_PAIRS), len(TWO_TONE_LEVELS), 3), np.nan)
    for s in layout["segments"]:
        if s["name"] != "two_tone":
            continue
        i = [p[0] for p in TWO_TONE_PAIRS].index(s["f1"])
        jj = TWO_TONE_LEVELS.index(s["level_db"])
        r = two_tone_imd(y[s["start"]:s["start"] + s["n"]], s["f1"], s["f2"])
        tt[i, jj] = [r["imd_db"], r["imd2_db"], r["imd3_db"]]
    arr["imd"] = tt
    sc["imd_440_554_at_minus12_db"] = float(tt[0, TWO_TONE_LEVELS.index(-12.0), 0])

    # bursts
    sc["burst"] = {str(int(f)): burst_stats(y, layout, f) for f in BURST_FREQS}
    sc["memory_flag"] = bool(any(v["memory_flag"] for v in sc["burst"].values()))
    return arr, sc


# ------------------------------------------------------------------------------- storage

def pack(arr: dict, sc: dict) -> bytes:
    """Compressed npz: arrays as float16, scalars as JSON (no audio)."""
    buf = io.BytesIO()
    out = {k: np.asarray(v, float).astype(np.float16) for k, v in arr.items()}
    out["scalars_json"] = np.frombuffer(json.dumps(_clean(sc)).encode(), dtype=np.uint8)
    np.savez_compressed(buf, **out)
    return buf.getvalue()


def unpack(b: bytes) -> tuple[dict, dict]:
    z = np.load(io.BytesIO(b))
    arr = {k: z[k].astype(float) for k in z.files if k != "scalars_json"}
    return arr, json.loads(bytes(z["scalars_json"]).decode())


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 5)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def table_diff(Ma: np.ndarray, Mb: np.ndarray, rel_db: float = 40.0) -> dict:
    """|Ma - Mb| in dB where both are finite and Ma is within `rel_db` of its peak."""
    m = np.isfinite(Ma) & np.isfinite(Mb) & (Ma >= np.nanmax(Ma) - rel_db)
    d = np.abs(Ma - Mb)[m]
    if d.size == 0:
        return {"n": 0, "median": np.nan, "p95": np.nan, "max": np.nan}
    return {"n": int(d.size), "median": float(np.median(d)), "p95": float(np.percentile(d, 95)),
            "max": float(d.max())}
