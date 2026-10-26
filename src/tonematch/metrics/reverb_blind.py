"""Blind reverb estimation from a running signal (T231, docs/notes/reverb-estimator-t231.md). Measurement only.

No dry reference, no silent gaps: the estimators read the signal's own band-by-band decays. When a guitar moves to a new
note, the frequency bins its old note occupied are abandoned; without reverb they fall to the floor at once, with
reverb they fall at the reverb's own rate (the tail), from a level that says how strong the tail is (a direct-to-
reverberant ratio, the Mix). Three estimators share one short-time spectrogram (1024-point frames, 5 ms hop):

  sdd   spectral decay distribution (after Wen, Habets and Naylor 2008; the physics mapping instead of a trained one):
        a straight line is fitted to every bin's level over sliding 400 ms windows; windows that are a clean, falling
        line (R^2 >= 0.9, at least 6 dB of fall, above the bin's floor) are "free decays"; their decay rates give
        RT60 = -60 / slope. A reverb bounds how FAST a bin can fall, so the estimate is read from the fast side of the
        distribution: the 10th percentile of RT60 ("p10") and the first peak of the log-RT60 histogram ("peak").
  ml    the maximum-likelihood decay rate of Ratnam et al. 2003 (JASA 114:2877): in a window the tail is an
        exponentially damped Gaussian noise; the ML decay ratio r per frame is the root of
        sum(n r^-n e[n]) / sum(r^-n e[n]) = (N-1)/2 (the compensated energy has its centroid at the window centre),
        solved by bisection on every (bin, window) at once; the same order-statistics reading (p10, first peak).
  adc   abandonment-synchronous decay averaging (the offset mirror of onset-synchronous averaging): every time a bin
        falls by 10 dB or more within 150 ms from a held level, the following 1.5 s of that bin, relative to the held
        level and cut where the bin is re-excited, is one decay event; the median over events at each lag is a smeared
        decay curve. Its slope from 100 ms on gives RT60; its level 80 to 120 ms in ("early_db", re the held level)
        is the tail-strength reading that tracks the Mix (a direct-to-reverberant ratio with the sign flipped).

Every reading is also given per frequency group (low 150 to 700 Hz, high 1.2 to 3.6 kHz) for a type feature: the
ratio of low to high RT60 and the tilt of the early tail. Confidence intervals: a block bootstrap over 2 s stretches
of the signal (sdd, ml) or over events (adc).

    est = estimate(x)                       # ReverbEstimate (all three, plus bands)
    ci = bootstrap(x, n=200, seed=0)        # {name: (lo, hi)} 16th and 84th percentiles

Pure numpy/scipy. Nothing here is a LossKit term; nothing is admitted.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.signal import stft

SR = 48_000
N_FFT = 1024                        # 21 ms frames
HOP = 240                           # 5 ms
BAND_HZ = (150.0, 3600.0)           # the pedal reverbs here run LowCut 150 Hz, HighCut 3700 Hz; the separator keeps little above
LOW_HZ = (150.0, 700.0)
HIGH_HZ = (1200.0, 3600.0)
SMOOTH_S = 0.030
CLAMP_DB = 90.0                     # levels are floored this far under the peak (digital silence is not a decay)
CHUNK_S = 60.0                      # long signals are analysed in stretches of this length (memory)
BAND_OCT = 1 / 6                    # bins are summed into 1/6-octave bands (at least MIN_BINS bins wide) before any fit
MIN_BINS = 3

# sdd / ml
WIN_S = 0.25
WIN_HOP_S = 0.025
MIN_R2 = 0.90
MIN_DROP_DB = 5.0
HALF_SLOPE_RATIO = 2.0             # the two halves of a window must fall at rates within this factor (no knee)
FLOOR_MARGIN_DB = 6.0               # a window must sit this far above its bin's floor (5th percentile) ...
PEAK_MARGIN_DB = 75.0               # ... and above the spectrogram's peak minus this
RT_MIN_S, RT_MAX_S = 0.05, 12.0
P_FAST = 10.0                       # the fast-side percentile of the RT60 distribution
PEAK_FRAC = 0.30                    # first histogram peak from the fast side with at least this share of the max

# adc
ADC_HOLD_S = 0.10                   # held level = median of this much before the drop
ADC_AFTER_S = 0.15                  # the drop must be reached this soon after
ADC_DROP_DB = 10.0
ADC_SPAN_S = 1.5
ADC_REFIRE_DB = 6.0                 # re-excitation: the bin climbs this far above its running minimum
ADC_MIN_EVENTS = 20
ADC_FIT_FROM_S = 0.10
ADC_FIT_FLOOR_DB = -50.0
ADC_FIT_SPAN_DB = 20.0              # the slope is fitted from 100 ms until the curve has fallen this far below its early level
ADC_ANCHOR_DB = 3.0                 # the event starts at the last frame still within this of the held level
ADC_EARLY_S = (0.08, 0.12)
ADC_HOLD_MARGIN_DB = 15.0           # the held level must be this far above the bin's floor
ADC_HOLD_PEAK_DB = 50.0             # and above the spectrogram peak minus this
ADC_DEAD_S = 0.30                   # no second event in a bin for this long


@dataclass
class ReverbEstimate:
    sdd_p10_s: float = float("nan")
    sdd_peak_s: float = float("nan")
    sdd_n: int = 0
    ml_p10_s: float = float("nan")
    ml_peak_s: float = float("nan")
    ml_n: int = 0
    adc_rt60_s: float = float("nan")
    adc_early_db: float = float("nan")     # tail level 80..120 ms after abandonment, re the held level (dB, <= 0)
    adc_events: int = 0
    # per frequency group
    sdd_p10_low_s: float = float("nan")
    sdd_p10_high_s: float = float("nan")
    adc_rt60_low_s: float = float("nan")
    adc_rt60_high_s: float = float("nan")
    adc_early_low_db: float = float("nan")
    adc_early_high_db: float = float("nan")
    adc_curve_db: list[float] = field(default_factory=list)   # the averaged decay curve, 5 ms steps, for plots
    duration_s: float = 0.0

    @property
    def rt_ratio_low_high(self) -> float:
        return self.sdd_p10_low_s / self.sdd_p10_high_s

    @property
    def early_tilt_db(self) -> float:
        return self.adc_early_high_db - self.adc_early_low_db

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rt_ratio_low_high"] = float(self.rt_ratio_low_high)
        d["early_tilt_db"] = float(self.early_tilt_db)
        return d


# ------------------------------------------------------------------------------------------ spectrogram

def band_edges(f: np.ndarray, band_hz=BAND_HZ, oct_frac: float = BAND_OCT, min_bins: int = MIN_BINS) -> list[tuple[int, int]]:
    """Bin index ranges [lo, hi) of 1/`oct_frac`-octave bands inside `band_hz`, each at least `min_bins` wide."""
    df = float(f[1] - f[0])
    out, lo = [], band_hz[0]
    while lo < band_hz[1]:
        hi = min(lo * 2 ** oct_frac, band_hz[1])
        i0 = int(np.searchsorted(f, lo))
        i1 = max(int(np.searchsorted(f, hi)), i0 + min_bins)
        if i1 > len(f) or f[i0] > band_hz[1]:
            break
        out.append((i0, i1))
        lo = float(f[i1]) if i1 < len(f) else band_hz[1]
    return out


def band_levels(x: np.ndarray, sr: int = SR, n_fft: int = N_FFT, hop: int = HOP, band_hz=BAND_HZ,
                smooth_s: float = SMOOTH_S) -> tuple[np.ndarray, np.ndarray, float]:
    """Levels in dB [bands, frames] of 1/6-octave bands (summed bin power) inside `band_hz`, time-smoothed by a
    `smooth_s` moving average of the power; the band centre frequencies; frames per second."""
    x = np.asarray(x, np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    f, _, z = stft(x, fs=sr, nperseg=n_fft, noverlap=n_fft - hop, window="hann", boundary=None, padded=False)
    p = np.abs(z) ** 2
    edges = band_edges(f, band_hz)
    pb = np.stack([p[a:b].sum(axis=0) for a, b in edges])
    fc = np.array([math.sqrt(f[a] * f[b - 1]) if f[a] > 0 else f[b - 1] for a, b in edges])
    k = max(int(round(smooth_s * sr / hop)), 1)
    if k > 1:
        ker = np.ones(k) / k
        pb = np.apply_along_axis(lambda r: np.convolve(r, ker, mode="same"), 1, pb)
    L = 10 * np.log10(pb + 1e-20)
    return np.maximum(L, L.max() - CLAMP_DB), fc, sr / hop


def _windows(L: np.ndarray, fps: float, win_s: float = WIN_S, hop_s: float = WIN_HOP_S) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sliding windows of every bin: array [bins, n_win, N] of levels, plus the window start frames and a validity
    mask (above the bin's floor and the global floor)."""
    N = int(round(win_s * fps))
    h = max(int(round(hop_s * fps)), 1)
    nb, nt = L.shape
    if nt < N:
        raise ValueError("signal shorter than one analysis window")
    starts = np.arange(0, nt - N + 1, h)
    idx = starts[:, None] + np.arange(N)[None, :]
    W = L[:, idx]                                               # [bins, n_win, N]
    floor = np.percentile(L, 5.0, axis=1)[:, None]              # per-bin floor
    gmax = float(L.max())
    ok = (W.min(axis=2) >= floor + FLOOR_MARGIN_DB) & (W.min(axis=2) >= gmax - PEAK_MARGIN_DB)
    return W, starts, ok


# ------------------------------------------------------------------------------------------ sdd

def _linefit(W: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Slope (dB/s) and R^2 of a straight line through each window (last axis)."""
    N = W.shape[-1]
    t = (np.arange(N) - (N - 1) / 2) / fps
    Wc = W - W.mean(axis=-1, keepdims=True)
    slope = (Wc * t).sum(axis=-1) / (t ** 2).sum()
    ss_res = ((Wc - slope[..., None] * t) ** 2).sum(axis=-1)
    ss_tot = (Wc ** 2).sum(axis=-1)
    r2 = 1 - ss_res / np.maximum(ss_tot, 1e-12)
    return slope, r2


def _free_decay(W: np.ndarray, fps: float) -> np.ndarray:
    """Windows that are one clean falling line: slope < 0, R^2 >= MIN_R2, at least MIN_DROP_DB of fall, and the two
    halves falling at rates within HALF_SLOPE_RATIO of each other (a knee, direct sound giving way to tail, fails)."""
    slope, r2 = _linefit(W, fps)
    drop = slope * (W.shape[-1] / fps)
    h = W.shape[-1] // 2
    s1, _ = _linefit(W[..., :h], fps)
    s2, _ = _linefit(W[..., h : 2 * h], fps)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = s1 / s2
    knee_ok = (s1 < 0) & (s2 < 0) & (ratio <= HALF_SLOPE_RATIO) & (ratio >= 1 / HALF_SLOPE_RATIO)
    return (slope < 0) & (r2 >= MIN_R2) & (drop <= -MIN_DROP_DB) & knee_ok


def _read_distribution(rt: np.ndarray) -> tuple[float, float]:
    """(p10, first-peak) of an RT60 sample in seconds; NaN when too few values."""
    rt = rt[np.isfinite(rt)]
    if rt.size < 10:
        return float("nan"), float("nan")
    p10 = float(np.percentile(rt, P_FAST))
    lr = np.log10(np.clip(rt, RT_MIN_S, RT_MAX_S))
    edges = np.linspace(math.log10(RT_MIN_S), math.log10(RT_MAX_S), 49)      # 1/20 decade bins
    h, _ = np.histogram(lr, bins=edges)
    h = np.convolve(h.astype(float), [0.25, 0.5, 0.25], mode="same")
    thr = PEAK_FRAC * h.max()
    peak = float("nan")
    for i in range(1, len(h) - 1):
        if h[i] >= thr and h[i] >= h[i - 1] and h[i] >= h[i + 1]:
            peak = float(10 ** ((edges[i] + edges[i + 1]) / 2))
            break
    return p10, peak


def sdd_rates(L: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """RT60 (s) of every free-decay window and the window start frames (for block bootstrap)."""
    W, starts, ok = _windows(L, fps)
    good = ok & _free_decay(W, fps)
    slope, _ = _linefit(W, fps)
    rt = -60.0 / slope[good]
    st = np.broadcast_to(starts[None, :], good.shape)[good]
    return rt, st


# ------------------------------------------------------------------------------------------ ml (Ratnam 2003)

def ml_decay(W: np.ndarray, fps: float, iters: int = 40) -> np.ndarray:
    """Ratnam's ML decay rate per window (last axis holds frame energies in dB): RT60 in seconds. Solves
    sum(n r^-n e_n) / sum(r^-n e_n) = (N-1)/2 for the per-frame energy ratio r by bisection on log r."""
    e = 10 ** ((W - W.max(axis=-1, keepdims=True)) / 10)       # linear energy, scaled per window
    N = W.shape[-1]
    n = np.arange(N, dtype=np.float64)
    target = (N - 1) / 2
    # log r in [lo, hi]: lo = fastest decay (RT_MIN), hi = 0 (no decay)
    lo = np.full(W.shape[:-1], -60.0 / RT_MIN_S / fps * math.log(10) / 10)
    hi = np.zeros(W.shape[:-1])
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        w = e * np.exp(-mid[..., None] * n)
        c = (w * n).sum(-1) / np.maximum(w.sum(-1), 1e-300)
        # centroid falls as r rises (mid -> 0); if c > target the decay guess is too slow? No: w = e * r^-n;
        # a smaller r (more negative log r) boosts late frames -> centroid rises. c > target => r too small => raise lo.
        too_small = c > target
        lo = np.where(too_small, mid, lo)
        hi = np.where(too_small, hi, mid)
    logr = 0.5 * (lo + hi)                                      # per frame, natural log of energy ratio
    slope_db_s = 10 * logr * fps / math.log(10)
    with np.errstate(divide="ignore"):
        rt = -60.0 / slope_db_s
    return rt


def ml_rates(L: np.ndarray, fps: float) -> tuple[np.ndarray, np.ndarray]:
    W, starts, ok = _windows(L, fps)
    ok = ok & _free_decay(W, fps)                                         # the same free-decay selection as sdd
    rt = ml_decay(W[ok], fps) if ok.any() else np.zeros(0)
    good = np.isfinite(rt) & (rt > RT_MIN_S * 1.01) & (rt < RT_MAX_S)
    return rt[good], np.broadcast_to(starts[None, :], ok.shape)[ok][good]


# ------------------------------------------------------------------------------------------ adc

def adc_events(L: np.ndarray, fps: float) -> np.ndarray:
    """Decay events [n_events, span] in dB re the held level (NaN after re-excitation)."""
    nb, nt = L.shape
    hold = int(round(ADC_HOLD_S * fps))
    after = int(round(ADC_AFTER_S * fps))
    span = int(round(ADC_SPAN_S * fps))
    dead = int(round(ADC_DEAD_S * fps))
    floor = np.percentile(L, 5.0, axis=1)
    gmax = float(L.max())
    out = []
    for b in range(nb):
        row = L[b]
        if nt <= hold + after + 2:
            continue
        # held level before each frame: median of the previous `hold` frames
        from numpy.lib.stride_tricks import sliding_window_view
        held = np.median(sliding_window_view(row[: nt - 1], hold), axis=1)       # held[i] -> frames i..i+hold-1, drop at i+hold
        t = np.arange(hold, hold + len(held))
        lvl_after = np.full(len(held), np.nan)
        ok_t = t + 2 * after < nt
        lvl_after[ok_t] = row[t[ok_t] + after]
        stay = np.full(len(held), np.nan)                        # the bin must stay down: the next 150 ms too
        cs = np.concatenate([[0.0], np.cumsum(row)])
        stay[ok_t] = (cs[t[ok_t] + 2 * after] - cs[t[ok_t] + after]) / after
        cand = ((held >= floor[b] + ADC_HOLD_MARGIN_DB) & (held >= gmax - ADC_HOLD_PEAK_DB)
                & (lvl_after <= held - ADC_DROP_DB) & (stay <= held - ADC_DROP_DB))
        i = 0
        last_anchor = -dead
        while i < len(cand):
            if not cand[i]:
                i += 1
                continue
            t0 = t[i]
            # the real cut: first crossing of held - drop, anchored at the last frame still near the held level
            cross = t0 + int(np.argmax(row[t0 : t0 + 2 * after] <= held[i] - ADC_DROP_DB))
            near = np.flatnonzero(row[t0 : cross + 1] >= held[i] - ADC_ANCHOR_DB)
            ta = t0 + int(near[-1]) if near.size else t0
            if ta - last_anchor < dead:
                i += 1
                continue
            last_anchor = ta
            seg = np.full(span, np.nan)
            n = min(span, nt - ta)
            s = row[ta : ta + n] - held[i]
            # re-excitation cut
            run_min = np.minimum.accumulate(s)
            k = np.flatnonzero((s > run_min + ADC_REFIRE_DB) & (np.arange(n) >= int(0.05 * fps)))
            if k.size:
                s = s[: k[0]]
            seg[: len(s)] = s
            out.append(seg)
            i = max(i + 1, ta - hold + 1)
    return np.array(out) if out else np.zeros((0, span))


def adc_curve(ev: np.ndarray, min_events: int = ADC_MIN_EVENTS) -> np.ndarray:
    if ev.shape[0] < min_events:
        return np.full(ev.shape[1] if ev.ndim == 2 else 0, np.nan)
    cnt = np.isfinite(ev).sum(axis=0)
    with np.errstate(all="ignore"):
        c = np.nanmedian(ev, axis=0)
    c[cnt < min_events] = np.nan
    return c


def adc_read(curve: np.ndarray, fps: float) -> tuple[float, float]:
    """(RT60 s, early tail level dB) from an averaged decay curve."""
    if curve.size == 0 or not np.isfinite(curve[: int(0.2 * fps)]).any():
        return float("nan"), float("nan")
    i0, i1 = int(ADC_EARLY_S[0] * fps), int(ADC_EARLY_S[1] * fps)
    early = float(np.nanmean(curve[i0:i1])) if np.isfinite(curve[i0:i1]).any() else float("nan")
    f0 = int(ADC_FIT_FROM_S * fps)
    seg = curve[f0:]
    lim = max(ADC_FIT_FLOOR_DB, (early if math.isfinite(early) else 0.0) - ADC_FIT_SPAN_DB)
    bad = np.flatnonzero(~np.isfinite(seg) | (seg < lim))
    end = int(bad[0]) if bad.size else len(seg)
    if end < int(0.15 * fps):
        return float("nan"), early
    y = seg[:end]
    t = np.arange(end) / fps
    slope = float(np.polyfit(t, y, 1)[0])
    rt = -60.0 / slope if slope < 0 else float("inf")
    return float(rt), early


# ------------------------------------------------------------------------------------------ top level

def _group(L: np.ndarray, f: np.ndarray, hz: tuple[float, float]) -> np.ndarray:
    return L[(f >= hz[0]) & (f <= hz[1])]


@dataclass
class _Analysis:
    rt_sdd: np.ndarray
    st_sdd: np.ndarray
    rt_ml: np.ndarray
    st_ml: np.ndarray
    events: np.ndarray
    groups: dict
    n_frames: int
    fps: float


def _analyse(x: np.ndarray, sr: int = SR, chunk_s: float = CHUNK_S) -> _Analysis:
    """All three estimators' raw material, pooled over `chunk_s` stretches (1 s overlap) of the signal."""
    x = np.asarray(x, np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    n = len(x)
    step, ov = int(chunk_s * sr), int(1.0 * sr)
    starts = list(range(0, max(n - ov, 1), step)) if n > step + ov else [0]
    acc = {"rt_sdd": [], "st_sdd": [], "rt_ml": [], "st_ml": [], "ev": []}
    grp = {g: {"rt": [], "ev": []} for g in ("low", "high")}
    off, fps, nfr = 0, SR / HOP, 0
    for s0 in starts:
        seg = x[s0 : s0 + step + ov]
        if len(seg) < N_FFT + int(WIN_S * sr):
            continue
        L, f, fps = band_levels(seg, sr)
        rt, st = sdd_rates(L, fps)
        acc["rt_sdd"].append(rt); acc["st_sdd"].append(st + off)
        rt, st = ml_rates(L, fps)
        acc["rt_ml"].append(rt); acc["st_ml"].append(st + off)
        acc["ev"].append(adc_events(L, fps))
        for g, hz in (("low", LOW_HZ), ("high", HIGH_HZ)):
            Lg = _group(L, f, hz)
            if Lg.shape[0]:
                grp[g]["rt"].append(sdd_rates(Lg, fps)[0])
                grp[g]["ev"].append(adc_events(Lg, fps))
        off += L.shape[1]
        nfr = off
    cat = lambda k: np.concatenate(acc[k]) if acc[k] else np.zeros(0)
    span = int(round(ADC_SPAN_S * fps))
    ev = np.concatenate(acc["ev"]) if acc["ev"] else np.zeros((0, span))
    groups = {g: {"rt": np.concatenate(v["rt"]) if v["rt"] else np.zeros(0),
                  "ev": np.concatenate(v["ev"]) if v["ev"] else np.zeros((0, span))} for g, v in grp.items()}
    return _Analysis(cat("rt_sdd"), cat("st_sdd"), cat("rt_ml"), cat("st_ml"), ev, groups, nfr, fps)


def _fill(est: ReverbEstimate, A: _Analysis, keep_curve: bool = False) -> ReverbEstimate:
    est.sdd_p10_s, est.sdd_peak_s = _read_distribution(A.rt_sdd)
    est.sdd_n = int(A.rt_sdd.size)
    est.ml_p10_s, est.ml_peak_s = _read_distribution(A.rt_ml)
    est.ml_n = int(A.rt_ml.size)
    est.adc_events = int(A.events.shape[0])
    curve = adc_curve(A.events)
    est.adc_rt60_s, est.adc_early_db = adc_read(curve, A.fps)
    if keep_curve:
        est.adc_curve_db = [float(v) for v in curve]
    for g in ("low", "high"):
        setattr(est, f"sdd_p10_{g}_s", _read_distribution(A.groups[g]["rt"])[0])
        c = adc_curve(A.groups[g]["ev"], min_events=max(ADC_MIN_EVENTS // 2, 8))
        rt_g, e_g = adc_read(c, A.fps)
        setattr(est, f"adc_rt60_{g}_s", rt_g)
        setattr(est, f"adc_early_{g}_db", e_g)
    return est


def estimate(x: np.ndarray, sr: int = SR, keep_curve: bool = False) -> ReverbEstimate:
    A = _analyse(x, sr)
    return _fill(ReverbEstimate(duration_s=float(len(x) / sr)), A, keep_curve)


def bootstrap(x: np.ndarray, sr: int = SR, n: int = 200, seed: int = 0, block_s: float = 2.0,
              q: tuple[float, float] = (16.0, 84.0), A: _Analysis | None = None) -> dict[str, tuple[float, float]]:
    """Block bootstrap (sdd, ml: resample `block_s` stretches of window starts; adc: resample events) of the main
    readings. Returns {name: (lo, hi)} at the `q` percentiles."""
    rng = np.random.default_rng(seed)
    if A is None:
        A = _analyse(x, sr)
    fps = A.fps
    nblk = max(int(math.ceil(A.n_frames / (block_s * fps))), 1)
    blk, blkm = (A.st_sdd / (block_s * fps)).astype(int), (A.st_ml / (block_s * fps)).astype(int)
    res = {k: [] for k in ("sdd_p10_s", "sdd_peak_s", "ml_p10_s", "ml_peak_s", "adc_rt60_s", "adc_early_db")}
    for _ in range(n):
        pick = rng.integers(0, nblk, nblk)
        cnt = np.bincount(pick, minlength=nblk)
        r = np.repeat(A.rt_sdd, cnt[blk]) if A.rt_sdd.size else A.rt_sdd
        p10, pk = _read_distribution(r)
        res["sdd_p10_s"].append(p10); res["sdd_peak_s"].append(pk)
        r = np.repeat(A.rt_ml, cnt[blkm]) if A.rt_ml.size else A.rt_ml
        p10, pk = _read_distribution(r)
        res["ml_p10_s"].append(p10); res["ml_peak_s"].append(pk)
        if A.events.shape[0]:
            e = A.events[rng.integers(0, A.events.shape[0], A.events.shape[0])]
            a, b = adc_read(adc_curve(e), fps)
        else:
            a, b = float("nan"), float("nan")
        res["adc_rt60_s"].append(a); res["adc_early_db"].append(b)
    out = {}
    for k, v in res.items():
        v = np.array(v, float)
        v = v[np.isfinite(v)]
        out[k] = (float(np.percentile(v, q[0])), float(np.percentile(v, q[1]))) if v.size else (float("nan"), float("nan"))
    return out


# ------------------------------------------------------------------------------------------ synthetic test signals

def synth_reverb_burst(rt60_s: float, drr_db: float, sr: int = SR, seed: int = 0, n_bursts: int = 12,
                       burst_s: float = 0.35, gap_s: float = 0.9, band_hz=(200.0, 3000.0)) -> np.ndarray:
    """Band-limited noise bursts (held, then cut) convolved with an exponentially decaying noise tail of `rt60_s`
    whose energy sits `drr_db` under the direct sound. A toy of a guitar changing notes with a reverb after it."""
    from scipy.signal import butter, fftconvolve, sosfiltfilt

    rng = np.random.default_rng(seed)
    per = int((burst_s + gap_s) * sr)
    x = np.zeros(per * n_bursts + int(3 * sr))
    sos = butter(4, band_hz, btype="bandpass", fs=sr, output="sos")
    for i in range(n_bursts):
        b = sosfiltfilt(sos, rng.standard_normal(int(burst_s * sr)))
        x[i * per : i * per + len(b)] = b * 10 ** (rng.uniform(-6, 0) / 20)
    tl = int(min(2.5 * rt60_s, 8.0) * sr)
    t = np.arange(tl) / sr
    tail = rng.standard_normal(tl) * np.exp(-3 * math.log(10) * t / rt60_s)
    tail *= 10 ** (-drr_db / 20) / math.sqrt((tail ** 2).sum())
    ir = np.zeros(tl + 1)
    ir[0] = 1.0
    ir[1:] += tail
    return fftconvolve(x, ir)[: len(x)].astype(np.float32)
