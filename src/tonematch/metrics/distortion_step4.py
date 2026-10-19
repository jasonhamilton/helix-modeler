"""Step 4 of docs/notes/distortion-identification.md (T181): identify a drive model and its Drive / Tone
from real guitar takes at random held-out settings, using the dfp-v1 fingerprint cards and curves.

RESEARCH CODE. Approved by the human ("go ahead", DECISIONS 2026-10-18, item 3); measurement plan in
DECISIONS ("T181: step 4 measurement plan"). Measurement, not admission: nothing here is an admitted
score, an adopted method or a bar.

Condition A (the DI is known):
- `fit_twin(x, y)`: a three-stage twin of the (DI, output) pair, fitted with no probe signal:
  first-order pre high-pass (corner from a grid, chosen by the fit), memoryless static curve (cubic
  B-spline with log-spaced knots, penalised least squares), post FIR (least squares), alternating.
- `twin_fingerprint(twin)`: the dfp-v1 probe through the twin, read by the same analyser as the
  library (`distortion_probe.analyse`), so the estimate is in the library's own units.
- `FeatureSpace` / `KnobMap` / `rank_models`: distances to each model's knob map (its measured
  settings interpolated over Drive x Tone, thin-plate RBF), model ranking and knob inversion.

Condition B (output only): `blind_features(y, ref_ltas)`: even/odd balance of harmonics 4-12 on
sustained notes, the post-EQ against a clean-guitar reference spectrum, crest and compression.

`estimate_knobs(target, di, models)`: the interface `search/shortlist.py` (T178) looks for.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import interpolate, linalg, signal

from . import distortion_probe as P

SR = P.SR
EPS = 1e-30

# models whose card is "not meaningful at quiet input" (step 3, section 6.6): left out of step 4
EXCLUDED = ("HD2_DistXenomorphFuzz", "HD2_DistWringerFuzz", "HD2_DistTycoctaviaFuzz", "HD2_DistMegaphone",
            "HD2_DistIndustrialFuzz", "HD2_DM4JetFuzz")

PRE_CORNERS = (None, 60.0, 120.0, 200.0, 300.0, 450.0, 650.0, 900.0, 1300.0, 2000.0, 3000.0)
PRE_PREFER_FLAT_DB = 0.3       # a corner must beat "no pre high-pass" by this much residual to be chosen
KNOT_OCTAVES = 13              # spline breakpoints at +-2^-k, k = 0..12 (down to -72 dB re the input peak)
Q_TAPS = 1280
Q_PRE = 256                    # non-causal taps allowed in the post FIR (Native renders are latency-compensated;
                               # linear-phase oversampling and tone filters can put energy before lag 0)
ALS_ITERS = 3
RIDGE = 1e-7
WEIGHT_POWER = 1.0           # curve-fit weights 1 / envelope^2: a relative-error fit
WEIGHT_FLOOR_DB = -80.0       # envelope floor re its maximum (caps the weight of near-silent output). As measured
                               # in T181; -40 was no worse on the dev split and is safer with gated output
                               # (docs/notes/distortion-step4.md, limits), not switched so the numbers stay valid


# ------------------------------------------------------------------------------------ helpers

def db(x, floor: float = -300.0):
    return np.maximum(20 * np.log10(np.maximum(np.abs(x), EPS)), floor)


def align(x: np.ndarray, y: np.ndarray, max_lag: int = 2048) -> int:
    """Lag (samples) of y behind x from the cross-correlation peak (absolute value: polarity free)."""
    n = 1 << int(np.ceil(np.log2(x.size + y.size)))
    c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(x, n)), n)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.abs(np.concatenate([c[:max_lag + 1], c[-max_lag:]]))
    return int(lags[int(np.argmax(vals))])


def shift(y: np.ndarray, lag: int) -> np.ndarray:
    """y advanced by `lag` samples (zero-filled)."""
    if lag == 0:
        return y.copy()
    out = np.zeros_like(y)
    if lag > 0:
        out[:-lag] = y[lag:]
    else:
        out[-lag:] = y[:lag]
    return out


def pre_sos(fc: float | None, floor_db: float | None = None):
    """First-order high-pass at `fc` (bilinear), normalised to 0 dB at 1 kHz; None = flat. With `floor_db`
    a first-order shelf instead: the bass settles `floor_db` below the treble (a Tube-Screamer-style gain
    stage, whose clean path passes the bass at unity while the gain leg lifts the treble)."""
    if fc is None:
        return None
    w = 2 * np.pi * fc
    if floor_db is None:
        b, a = signal.bilinear([1.0, 0.0], [1.0, w], SR)
    else:
        k = 10 ** (floor_db / 20)
        b, a = signal.bilinear([1.0, k * w], [1.0, w], SR)
    sos = signal.tf2sos(b, a)
    g = abs(signal.sosfreqz(sos, worN=[1000.0], fs=SR)[1][0])
    sos[0, :3] /= g
    return sos


def apply_pre(x: np.ndarray, fc: float | None, floor_db: float | None = None) -> np.ndarray:
    s = pre_sos(fc, floor_db)
    return np.asarray(x, float) if s is None else signal.sosfilt(s, np.asarray(x, float))


def breakpoints(n_oct: int = KNOT_OCTAVES) -> np.ndarray:
    pos = 2.0 ** -np.arange(n_oct)[::-1]
    return np.concatenate([-pos[::-1], [0.0], pos])


def spline_knots(n_oct: int = KNOT_OCTAVES, k: int = 3) -> np.ndarray:
    b = breakpoints(n_oct)
    return np.concatenate([[b[0]] * k, b, [b[-1]] * k])


def design(s: np.ndarray, t: np.ndarray, k: int = 3):
    """Sparse B-spline design matrix for s in [-1, 1]."""
    return interpolate.BSpline.design_matrix(np.clip(s, -1.0, 1.0), t, k)


def fir_fit(v: np.ndarray, y: np.ndarray, taps: int = Q_TAPS, pre: int = Q_PRE, ridge: float = 1e-9) -> np.ndarray:
    """Least-squares FIR q (index 0 = lag -pre) with y[n] ~ sum_j q[j] v[n - j + pre]."""
    n = 1 << int(np.ceil(np.log2(v.size + taps)))
    V, Y = np.fft.rfft(v, n), np.fft.rfft(y, n)
    r = np.fft.irfft(V * np.conj(V), n)[:taps]
    cxy = np.fft.irfft(Y * np.conj(V), n)                      # cxy[j] = sum y[m] v[m - j]
    lags = np.arange(-pre, taps - pre)
    rhs = cxy[lags % n]
    r = r.copy()
    r[0] *= 1 + ridge
    return linalg.solve_toeplitz(r, rhs)


def fir_apply(q: np.ndarray, v: np.ndarray, pre: int = Q_PRE) -> np.ndarray:
    out = signal.fftconvolve(v, q)[: v.size + q.size]
    return out[pre:pre + v.size]


def fir_resp(q: np.ndarray, f, pre: int = Q_PRE) -> np.ndarray:
    w = 2 * np.pi * np.atleast_1d(np.asarray(f, float)) / SR
    j = np.arange(q.size) - pre
    return np.exp(-1j * np.outer(w, j)) @ q


# ------------------------------------------------------------------------------------ the twin

@dataclass
class Twin:
    pre_fc: float | None
    pre_lp: float | None
    U: float                       # input scale: s = u / U (u = pre-filtered DI, DI units at 1 kHz)
    coef: np.ndarray               # spline coefficients
    q: np.ndarray                  # post FIR, 0 dB at 1 kHz
    fit_db: float                  # residual energy re output, dB (unweighted)
    fit_w_db: float = 0.0          # the same, level-weighted (what the fit minimises)
    lag: int = 0
    residuals: dict = field(default_factory=dict)   # corner -> residual dB
    u_peak_dbfs: float = 0.0       # peak of the pre-filtered DI (dBFS at 1 kHz)
    pre_floor_db: float | None = None   # None: a high-pass; else a shelf with the bass this far down
    s_lin: float = 0.0             # |u| / U below this: linear plus a small even term (the data say little there)

    def _spl(self):
        return interpolate.BSpline(spline_knots(), self.coef, 3, extrapolate=False)

    def curve(self, u: np.ndarray) -> np.ndarray:
        """Static curve in DI units; beyond the observed range a straight continuation with the end
        slope, the slope clipped to [0, slope at 0] (saturating curves stay flat, clean ones go on)."""
        u = np.asarray(u, float)
        s = u / self.U
        spl = self._spl()
        out = np.asarray(spl(np.clip(s, -1.0, 1.0)), float)
        d = spl.derivative()
        g0 = float(d(0.0))
        if self.s_lin > 0:
            a = self.s_lin
            fp, fn, f0 = float(spl(a)), float(spl(-a)), float(spl(0.0))
            g, q2 = (fp - fn) / (2 * a), (fp + fn - 2 * f0) / (2 * a * a)
            m = np.abs(s) < a
            out[m] = f0 + g * s[m] + q2 * s[m] ** 2
            g0 = g
        for side, edge in ((1, 1.0), (-1, -1.0)):
            m = s * side > 1.0
            if m.any():
                sl = float(d(edge - side * 1e-9))
                sl = float(np.clip(sl, 0.0, max(g0, 0.0))) if g0 > 0 else float(np.clip(sl, min(g0, 0.0), 0.0))
                out[m] = float(spl(edge)) + sl * (s[m] - edge)
        return out

    def apply(self, x: np.ndarray) -> np.ndarray:
        u = apply_pre2(x, (self.pre_fc, self.pre_lp, self.pre_floor_db))
        return fir_apply(self.q, self.curve(u))

    def to_dict(self) -> dict:
        return {"pre_fc": self.pre_fc, "pre_lp": self.pre_lp, "fit_w_db": self.fit_w_db, "U": self.U, "coef": [float(v) for v in self.coef],
                "q": [float(v) for v in self.q], "fit_db": self.fit_db, "lag": self.lag,
                "residuals": {str(k): v for k, v in self.residuals.items()}, "u_peak_dbfs": self.u_peak_dbfs,
                "s_lin": self.s_lin, "pre_floor_db": self.pre_floor_db}

    @classmethod
    def from_dict(cls, d: dict) -> Twin:
        return cls(pre_fc=d["pre_fc"], pre_lp=d.get("pre_lp"), fit_w_db=d.get("fit_w_db", 0.0), U=d["U"], coef=np.asarray(d["coef"]), q=np.asarray(d["q"]),
                   fit_db=d["fit_db"], lag=d.get("lag", 0), residuals=d.get("residuals", {}),
                   u_peak_dbfs=d.get("u_peak_dbfs", 0.0), s_lin=d.get("s_lin", 0.0),
                   pre_floor_db=d.get("pre_floor_db"))


def envelope_weights(y: np.ndarray, power: float = 1.0, smooth_s: float = 0.01, floor_db: float | None = None) -> np.ndarray:
    """Per-sample weights 1 / envelope^(2 power): quiet passages count as much as loud ones in the curve
    fit (Native renders are noise-free, so quiet passages hold the small-signal truth)."""
    floor_db = WEIGHT_FLOOR_DB if floor_db is None else floor_db
    n = max(1, int(smooth_s * SR))
    e = np.sqrt(np.convolve(np.asarray(y, float) ** 2, np.ones(n) / n, mode="same"))
    e = np.maximum(e, np.max(e) * 10 ** (floor_db / 20) + EPS)
    w = e ** (-2.0 * power)
    return w / np.mean(w)


def _curve_fit(u, y, q, t, U, wts=None, ridge=RIDGE):
    """Spline coefficients with y ~ q * f(u) (weighted, penalised least squares on second differences)."""
    B = design(u / U, t).tocsc()
    K = B.shape[1]
    Z = np.empty((u.size, K))
    for kk in range(K):
        col = B[:, kk].toarray().ravel()
        Z[:, kk] = fir_apply(q, col)
    Zw = Z if wts is None else Z * wts[:, None]
    A = Zw.T @ Z
    rhs = Zw.T @ y
    D2 = np.diff(np.eye(K), 2, axis=0)
    lam = ridge * np.trace(A) / K
    return np.linalg.solve(A + lam * D2.T @ D2 + 1e-12 * np.trace(A) / K * np.eye(K), rhs)


def _residual_db(y, yh, wts=None):
    if wts is None:
        return float(10 * np.log10(np.sum((y - yh) ** 2) / (np.sum(y ** 2) + EPS) + 1e-30))
    return float(10 * np.log10(np.sum(wts * (y - yh) ** 2) / (np.sum(wts * y ** 2) + EPS) + 1e-30))


def _als(u, y, t, U, iters=ALS_ITERS, q0=None, wts=None):
    q = fir_fit(u, y) if q0 is None else q0
    coef = None
    for _ in range(iters):
        g = abs(fir_resp(q, [1000.0])[0]) + EPS
        q = q / g
        coef = _curve_fit(u, y, q, t, U, wts)
        v = np.nan_to_num(interpolate.BSpline(t, coef, 3, extrapolate=False)(np.clip(u / U, -1, 1)))
        q = fir_fit(v, y)
    g = abs(fir_resp(q, [1000.0])[0]) + EPS
    q = q / g
    coef = coef * g
    v = np.nan_to_num(interpolate.BSpline(t, coef, 3, extrapolate=False)(np.clip(u / U, -1, 1)))
    yh = fir_apply(q, v)
    return coef, q, _residual_db(y, yh, wts)


def apply_pre2(x, pre):
    """pre = (hp_fc or None, lp_fc or None[, shelf floor dB or None])."""
    hp, lp = pre[0], pre[1]
    u = apply_pre(x, hp, pre[2] if len(pre) > 2 else None)
    if lp is not None:
        b, a = signal.bilinear([2 * np.pi * lp], [1.0, 2 * np.pi * lp], SR)
        sos = signal.tf2sos(b, a)
        g = abs(signal.sosfreqz(sos, worN=[1000.0], fs=SR)[1][0])
        sos[0, :3] /= g
        u = signal.sosfilt(sos, u)
    return u


PRE_LP = (None, 6000.0, 3000.0, 1500.0)


PRE_SHELF_DB = (-30.0, -20.0, -12.0, -6.0)
PRE_SHELF_SCALE = (0.7, 1.0, 1.4)


def fit_twin(x: np.ndarray, y: np.ndarray, corners=PRE_CORNERS, iters: int = ALS_ITERS,
             weight_power: float = WEIGHT_POWER, lp_corners=PRE_LP, refine: bool = True,
             auto_lag: bool = False, shelves=()) -> Twin:
    """Fit the three-stage twin to a (DI, output) pair (same length, 48 kHz). The pre-filter is chosen by
    coordinate search: first-order high-pass corner on a grid (then refined on a log scale); then, if `shelves`
    (default none: no gain on the dev split), a shelf
    instead of the high-pass (bass `floor` dB below the treble, corner scaled 0.7 / 1 / 1.4); then an
    optional first-order low-pass. Each step is kept only if it lowers the weighted residual by
    PRE_PREFER_FLAT_DB."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    lag = align(x, y) if auto_lag else 0       # the cross-correlation peak moves with the filters' delay
    y = shift(y, lag)
    wts = envelope_weights(y, weight_power) if weight_power else None
    t = spline_knots()
    res, fits = {}, {}
    it_screen = max(1, iters - 1)

    def ev(pre, it=it_screen):
        if pre in res:
            return res[pre]
        u = apply_pre2(x, pre)
        U = float(np.max(np.abs(u))) * 1.0001 + EPS
        coef, q, r = _als(u, y, t, U, iters=it, wts=wts)
        res[pre], fits[pre] = r, (coef, q, U)
        return r

    def better(a, b):                       # is `a` a worthwhile step from `b`?
        return res[a] < res[b] - PRE_PREFER_FLAT_DB

    for fc in corners:
        ev((fc, None, None))
    best = (None, None, None)
    cand = min(res, key=lambda k: res[k])
    if better(cand, best):
        best = cand
    if refine and best[0] is not None:
        lo_hi = sorted(c for c in corners if c is not None)
        i = lo_hi.index(best[0])
        lo, hi = np.log(lo_hi[max(0, i - 1)]), np.log(lo_hi[min(len(lo_hi) - 1, i + 1)])
        for fc in np.exp(np.linspace(lo, hi, 7)[1:-1]):
            ev((float(round(fc, 1)), None, None))
        cand = min((k for k in res if k[0] is not None), key=lambda k: res[k])
        best = cand if res[cand] < res[best] else best
    if shelves and best[0] is not None:
        for fl in shelves:
            for sc_ in PRE_SHELF_SCALE:
                ev((float(round(best[0] * sc_, 1)), None, fl))
        cand = min((k for k in res if k[2] is not None), key=lambda k: res[k])
        if better(cand, best):
            best = cand
    for lp in lp_corners:
        if lp is not None:
            ev((best[0], lp, best[2]))
    cand = min((k for k in res if k[0] == best[0] and k[2] == best[2]), key=lambda k: res[k])
    if cand[1] is not None and better(cand, best):
        best = cand
    u = apply_pre2(x, best)
    U = fits[best][2]
    coef, q, r = _als(u, y, t, U, iters=iters, q0=fits[best][1], wts=wts)
    yh = fir_apply(q, np.nan_to_num(interpolate.BSpline(t, coef, 3, extrapolate=False)(np.clip(u / U, -1, 1))))
    return Twin(pre_fc=best[0], pre_lp=best[1], pre_floor_db=best[2], U=U, coef=coef, q=q,
                fit_db=_residual_db(y, yh), fit_w_db=r, lag=lag,
                residuals={f"{k[0]}|{k[1]}|{k[2]}": v for k, v in res.items()}, u_peak_dbfs=float(db(U)))


_PROBE = None


def probe():
    global _PROBE
    if _PROBE is None:
        _PROBE = P.build_probe("v1")
    return _PROBE


def twin_fingerprint(tw: Twin) -> tuple[dict, dict]:
    """(arrays, scalars) of the dfp-v1 probe played through the twin, read by the library analyser."""
    sig, lay = probe()
    y = tw.apply(sig)
    arr, sc = P.analyse(y, lay)
    for k in ("odd_harmonic_residual_db", "harmonic_residual_db"):
        sc.pop(k, None)
    return arr, sc


# ------------------------------------------------------------------------------------ direct measures

def residual_vs_level(x: np.ndarray, y: np.ndarray, tw: Twin, frame: int = 2048, hop: int = 1024,
                      edges=None) -> dict:
    """The twin's linear part alone (the curve's slope at 0) against the output, frame by frame, binned
    by the pre-filtered DI's frame peak (dBFS): how much of the output is NOT linear at each level."""
    x = np.asarray(x, float)
    y = shift(np.asarray(y, float)[:x.size], tw.lag)
    u = apply_pre2(x, (tw.pre_fc, tw.pre_lp, tw.pre_floor_db))
    spl = tw._spl()
    g0 = float(spl.derivative()(0.0)) / tw.U
    ylin = fir_apply(tw.q, g0 * u)
    edges = np.arange(-96.0, 1.0, 6.0) if edges is None else np.asarray(edges, float)
    nfr = 1 + (x.size - frame) // hop
    pk = np.array([np.max(np.abs(u[i * hop:i * hop + frame])) for i in range(nfr)])
    e = np.array([np.sum((y[i * hop:i * hop + frame] - ylin[i * hop:i * hop + frame]) ** 2) for i in range(nfr)])
    s = np.array([np.sum(y[i * hop:i * hop + frame] ** 2) for i in range(nfr)])
    L = db(pk)
    out = {"level_dbfs": [], "nonlinear_db": [], "frames": []}
    for lo, hi in itertools.pairwise(edges):
        m = (L >= lo) & (L < hi)
        if m.sum() < 3:
            continue
        out["level_dbfs"].append(float(0.5 * (lo + hi)))
        out["nonlinear_db"].append(float(10 * np.log10(e[m].sum() / (s[m].sum() + EPS) + 1e-30)))
        out["frames"].append(int(m.sum()))
    return out


# ------------------------------------------------------------------------------------ features

CARD_NAMES = ("knee", "knee_none", "knee_below", "onset", "onset_never", "onset_below", "even", "log_pre",
              "log_post", "mid_db", "mid_logf", "g0")


def card_vector(sc: dict, arr: dict, level: bool = False) -> dict[str, np.ndarray]:
    """The card as blocks of one value each (the step-3 "card" feature set)."""
    from . import distortion_step3 as S3
    c = S3.card_values(sc, arr, P.STAIR_LEVELS_V1)
    f = S3.card_features(c, level=level)
    return {f"card.{k}": np.array([v], float) for k, v in f.items()}


def curve_vector(arr: dict, sc: dict, level: bool = False, max_level_dbfs: float | None = None,
                 min_level_dbfs: float | None = None) -> dict[str, np.ndarray]:
    """The step-3 curve blocks; with `max_level_dbfs`, every value read at a probe level above it is NaN
    (masked: the twin only saw inputs up to the excerpt's peak)."""
    from . import distortion_step3 as S3
    b = S3.curve_blocks(arr, sc, level=level)
    if max_level_dbfs is None and min_level_dbfs is None:
        return b
    hi = np.inf if max_level_dbfs is None else max_level_dbfs
    lo = -np.inf if min_level_dbfs is None else min_level_dbfs
    out = lambda L: (np.asarray(L) > hi) | (np.asarray(L) < lo)
    b = {k: np.asarray(v, float).copy() for k, v in b.items()}
    sw = np.asarray(P.SWEEP_LEVELS)
    st = np.asarray(P.STAIR_LEVELS_V1)
    hl = np.asarray(P.HARM_LEVELS)
    M1 = b["M1"].reshape(len(P.FGRID), len(sw))
    M1[:, out(sw)] = np.nan
    b["M1"] = M1.ravel()
    Mh = b["Mh"].reshape(4, len(P.FGRID), len(hl))
    Mh[:, :, out(hl)] = np.nan
    b["Mh"] = Mh.ravel()
    for key in ("stair1k_h1", "stair220_h1"):
        b[key][out(st)] = np.nan
    for key in ("stair1k_rel", "stair220_rel"):
        v = b[key].reshape(-1, len(st))
        v[:, out(st)] = np.nan
        b[key] = v.ravel()
    if out([0.0])[0]:
        b["sat"][:] = np.nan
    tl = np.asarray(P.TWO_TONE_LEVELS)
    im = b["imd"].reshape(len(P.TWO_TONE_PAIRS), len(tl), 3)
    im[:, out(tl), :] = np.nan
    b["imd"] = im.ravel()
    return b


def feature_blocks(arr: dict, sc: dict, kind: str, level: bool = False, max_level_dbfs: float | None = None,
                   min_level_dbfs: float | None = None, blocks=None):
    """kind: "card", "curves" or "both"; `blocks` keeps only the named curve blocks."""
    cv = {}
    if kind in ("curves", "both"):
        cv = {f"curve.{k}": v for k, v in curve_vector(arr, sc, level, max_level_dbfs, min_level_dbfs).items()
              if blocks is None or k in blocks}
    if kind == "card":
        return card_vector(sc, arr, level=level)
    if kind == "curves":
        return cv
    if kind == "both":
        return {**card_vector(sc, arr, level=level), **cv}
    raise ValueError(kind)


class FeatureSpace:
    """Z-scoring fitted on a gallery of feature-block dicts; block weights so each block counts once
    (the step-3 "block-weighted" distance), card values each their own block."""

    def __init__(self, rows: list[dict[str, np.ndarray]], card_weight: float = 1.0):
        self.names = sorted(rows[0])
        self.sizes = [np.asarray(rows[0][k]).size for k in self.names]
        X = np.stack([np.concatenate([np.asarray(r[k], float).ravel() for k in self.names]) for r in rows])
        X[~np.isfinite(X)] = np.nan
        with np.errstate(all="ignore"):
            med = np.nanmedian(X, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)
        Xf = np.where(np.isnan(X), med, X)
        self.med, self.mean, self.sd = med, Xf.mean(axis=0), Xf.std(axis=0)
        self.keep = self.sd > 1e-9
        bw = []
        for k, n in zip(self.names, self.sizes):
            w = card_weight if k.startswith("card.") else 1.0
            bw += [w / n] * n
        self.w = np.asarray(bw)
        self.block_id = np.concatenate([[i] * n for i, n in enumerate(self.sizes)])
        self.Z = self.transform_matrix(X, fill=True)

    def vec(self, row: dict[str, np.ndarray]) -> np.ndarray:
        v = np.concatenate([np.asarray(row.get(k, np.full(n, np.nan)), float).ravel()
                            for k, n in zip(self.names, self.sizes)])
        v[~np.isfinite(v)] = np.nan
        return v

    def transform_matrix(self, X: np.ndarray, fill: bool) -> np.ndarray:
        Xf = np.where(np.isnan(X), self.med, X) if fill else X
        Z = (Xf - self.mean) / np.where(self.keep, self.sd, 1.0)
        Z[:, ~self.keep] = 0.0 if fill else np.nan
        return Z

    def transform(self, row: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """(z, weights) for a query; weights 0 where the query is masked, renormalised per block."""
        z = self.transform_matrix(self.vec(row)[None], fill=False)[0]
        ok = np.isfinite(z) & self.keep
        w = np.zeros_like(self.w)
        for b in np.unique(self.block_id):
            m = (self.block_id == b) & ok
            if m.any():
                w[m] = 1.0 / m.sum() * (self.w[self.block_id == b].sum())
        z = np.where(ok, z, 0.0)
        return z, w / max(w.sum(), EPS)


# ------------------------------------------------------------------------------------ knob maps

def tps_basis(pts: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Thin-plate (2-D) or cubic (1-D) RBF basis rows for query points `q` given centres `pts`, with the
    degree-1 polynomial tail: phi(q) [n_q x (n + d + 1)]."""
    d = np.sqrt(((q[:, None, :] - pts[None, :, :]) ** 2).sum(-1))
    if pts.shape[1] == 1:
        K = d ** 3
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            K = np.where(d > 0, d * d * np.log(d), 0.0)
    return np.concatenate([K, np.ones((q.shape[0], 1)), q], axis=1)


class KnobMap:
    """One model's feature vectors at its measured knob positions (0..1), interpolated by a thin-plate
    spline (cubic in 1-D) over Drive x Tone, evaluated on a regular grid."""

    def __init__(self, positions: np.ndarray, Z: np.ndarray, step: float = 0.02, smooth: float = 0.0):
        pts = np.asarray(positions, float)
        if pts.ndim == 1:
            pts = pts[:, None]
        self.dim = pts.shape[1]
        self.pts = pts
        n, d = pts.shape
        A = tps_basis(pts, pts)[:, :n] + smooth * np.eye(n)
        Pm = np.concatenate([np.ones((n, 1)), pts], axis=1)
        M = np.block([[A, Pm], [Pm.T, np.zeros((d + 1, d + 1))]])
        rhs = np.concatenate([Z, np.zeros((d + 1, Z.shape[1]))])
        self.W = np.linalg.lstsq(M, rhs, rcond=None)[0]
        g = np.round(np.arange(0.0, 1.0 + 1e-9, step), 6)
        self.grid = g[:, None] if d == 1 else np.stack(np.meshgrid(g, g, indexing="ij"), -1).reshape(-1, 2)
        self.B = tps_basis(pts, self.grid)
        self.Zg = self.B @ self.W                     # (n_grid x dims)

    def distances(self, z: np.ndarray, w: np.ndarray) -> np.ndarray:
        diff = self.Zg - z[None]
        return np.sqrt(np.maximum((diff * diff) @ w, 0.0))

    def best(self, z: np.ndarray, w: np.ndarray) -> tuple[float, np.ndarray]:
        dd = self.distances(z, w)
        i = int(np.argmin(dd))
        return float(dd[i]), self.grid[i].copy()


def nearest_setting(Zm: np.ndarray, positions: np.ndarray, z: np.ndarray, w: np.ndarray) -> tuple[float, np.ndarray]:
    diff = Zm - z[None]
    dd = np.sqrt(np.maximum((diff * diff) @ w, 0.0))
    i = int(np.argmin(dd))
    return float(dd[i]), np.atleast_1d(np.asarray(positions, float)[i]).copy()


def rank_models(maps: dict[str, KnobMap], z: np.ndarray, w: np.ndarray) -> list[tuple[str, float, np.ndarray]]:
    rows = [(m, *km.best(z, w)) for m, km in maps.items()]
    return sorted(rows, key=lambda r: r[1])


# ------------------------------------------------------------------------------------ blind features

LTAS_NFFT = 8192
EVEN_ORDERS = tuple(range(4, 13))


def ltas_db(y: np.ndarray, fgrid=None) -> np.ndarray:
    """Long-term average spectrum on the 1/6-octave grid, dB, 0 dB mean over 200 Hz-5 kHz."""
    from . import distortion_id as D
    fgrid = P.FGRID if fgrid is None else fgrid
    f, pxx = signal.welch(np.asarray(y, float), SR, nperseg=LTAS_NFFT, noverlap=LTAS_NFFT // 2)
    v = D.band_mag_db(f, np.sqrt(pxx), fgrid, 1 / 6)
    band = (np.asarray(fgrid) >= 200) & (np.asarray(fgrid) <= 5000)
    return v - np.nanmean(v[band])


def frame_levels(y: np.ndarray, n: int = 1024) -> np.ndarray:
    k = y.size // n
    f = np.asarray(y[:k * n], float).reshape(k, n)
    return 10 * np.log10(np.mean(f ** 2, axis=1) + 1e-20)


def f0_frames(y: np.ndarray, frame: int = 4096, hop: int = 2048, fmin: float = 70.0, fmax: float = 700.0):
    """Per-frame (start, f0, clarity) by normalised autocorrelation of the low-passed signal."""
    sos = signal.butter(4, 1500.0, "low", fs=SR, output="sos")
    z = signal.sosfiltfilt(sos, np.asarray(y, float))
    out = []
    lo, hi = int(SR / fmax), int(SR / fmin)
    for s0 in range(0, z.size - frame, hop):
        seg = z[s0:s0 + frame] * np.hanning(frame)
        n = 1 << int(np.ceil(np.log2(2 * frame)))
        r = np.fft.irfft(np.abs(np.fft.rfft(seg, n)) ** 2, n)[:frame]
        if r[0] <= 0:
            continue
        r = r / r[0]
        i = lo + int(np.argmax(r[lo:hi]))
        if 1 <= i < frame - 1:
            a, b, c = r[i - 1], r[i], r[i + 1]
            den = a - 2 * b + c
            di = 0.5 * (a - c) / den if den != 0 else 0.0
        else:
            di = 0.0
        out.append((s0, SR / (i + di), float(r[i])))
    return out


def harmonic_profile(y: np.ndarray, frames, frame: int = 4096, K: int = 12, min_clarity: float = 0.8,
                     min_db: float = -35.0):
    """Median harmonic levels (dB re H1, k = 1..K) over sustained, clearly pitched, loud frames."""
    from . import distortion_id as D
    lv = []
    yl = np.asarray(y, float)
    top = 10 * np.log10(np.mean(yl ** 2) + 1e-20) + 10
    for s0, f0, cl in frames:
        seg = yl[s0:s0 + frame]
        if cl < min_clarity or 10 * np.log10(np.mean(seg ** 2) + 1e-20) < top + min_db or f0 * K > 0.45 * SR:
            continue
        if seg.size < frame or not np.all(np.isfinite(seg)) or not np.isfinite(f0) or f0 <= 0:
            continue
        try:
            a = D.sine_harmonics(seg * 1.0, f0, SR, K)
        except (ValueError, np.linalg.LinAlgError):
            continue
        h = db(np.abs(a[1:K + 1]))
        lv.append(h - h[0])
    if not lv:
        return None, 0
    return np.median(np.array(lv), axis=0), len(lv)


def blind_features(y: np.ndarray, ref_ltas: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Output-only features (condition B), as blocks."""
    y = np.nan_to_num(np.asarray(y, float))
    out: dict[str, np.ndarray] = {}
    lt = ltas_db(y)
    out["post_eq"] = lt - (ref_ltas if ref_ltas is not None else 0.0)
    fl = frame_levels(y)
    act = fl > np.max(fl) - 40.0
    a = fl[act]
    rms = np.sqrt(np.mean(y ** 2) + 1e-20)
    out["crest_db"] = np.array([20 * np.log10(np.max(np.abs(y)) / rms + 1e-20)])
    fr = 2048
    k = y.size // fr
    seg = y[:k * fr].reshape(k, fr)
    segrms = np.sqrt(np.mean(seg ** 2, 1) + 1e-20)
    ok = 20 * np.log10(segrms) > 20 * np.log10(np.max(segrms)) - 30
    out["frame_crest_db"] = np.array([np.median(20 * np.log10(np.max(np.abs(seg[ok]), 1) / segrms[ok]))])
    out["level_spread_db"] = np.array([np.percentile(a, 95) - np.percentile(a, 10)])
    # note-decay slope: median slope (dB/s) of the frame level over 0.35 s after each local peak
    hop_s = 1024 / SR
    slopes = []
    for i in range(1, fl.size - 1):
        if fl[i] >= fl[i - 1] and fl[i] > fl[i + 1] and fl[i] > np.max(fl) - 25 and (i < 3 or fl[i] - fl[i - 3] > 4):
            j = min(fl.size, i + int(0.35 / hop_s))
            if j - i >= 6:
                t = np.arange(j - i) * hop_s
                slopes.append(np.polyfit(t, fl[i:j], 1)[0])
    out["decay_db_s"] = np.array([np.median(slopes) if slopes else np.nan])
    frames = f0_frames(y)
    prof, n = harmonic_profile(y, frames)
    if prof is None:
        out["even_odd_db"] = np.array([np.nan])
        out["harm_slope_db"] = np.array([np.nan])
        out["harm_profile"] = np.full(11, np.nan)
    else:
        p = 10 ** (prof / 10)
        ev = sum(p[k - 1] for k in EVEN_ORDERS if k % 2 == 0)
        od = sum(p[k - 1] for k in EVEN_ORDERS if k % 2 == 1)
        out["even_odd_db"] = np.array([10 * np.log10((ev + 1e-20) / (od + 1e-20))])
        ks = np.arange(2, prof.size + 1)
        out["harm_slope_db"] = np.array([np.polyfit(np.log2(ks), prof[1:], 1)[0]])
        out["harm_profile"] = prof[1:]
    out["n_sustained"] = np.array([float(n)])
    return out


BLIND_BLOCKS = ("post_eq", "crest_db", "frame_crest_db", "level_spread_db", "decay_db_s", "even_odd_db",
                "harm_slope_db", "harm_profile")


def blind_vector(feat: dict) -> dict[str, np.ndarray]:
    return {f"blind.{k}": np.asarray(feat[k], float) for k in BLIND_BLOCKS}


# ------------------------------------------------------------------------------------ library

def load_library(root: Path, dirs=("dfp-v1",), exclude=EXCLUDED) -> list[dict]:
    """Every stored dfp-v1 setting of the step-4 pool: {model, setting, cc, pos, arr, sc, source}."""
    recs = []
    for dname in dirs:
        idxp = root / "cache" / "distortion-fp" / dname / "index.json"
        if not idxp.is_file():
            continue
        idx = json.loads(idxp.read_text())
        sem = idx.get("semantics", {})
        for s in idx["settings"]:
            if "file" not in s or s.get("tag") or s["model"] in exclude:
                continue
            fp = root / s["file"]
            if not fp.is_file():
                continue
            arr, sc = P.unpack(fp.read_bytes())
            sm = sem.get(s["model"]) or {}
            dk, tk = sm.get("drive"), sm.get("tone")
            if dk is None:
                dk = next(iter(s["cc"]))
            pos = [s["cc"][dk] / 127.0] + ([s["cc"][tk] / 127.0] if tk else [])
            recs.append({"model": s["model"], "setting": s["setting"], "cc": s["cc"], "pos": pos,
                         "arr": arr, "sc": sc, "source": dname, "drive": dk, "tone": tk})
    # one record per (model, setting): a later dir never duplicates an earlier one
    seen, out = set(), []
    for r in recs:
        key = (r["model"], tuple(sorted(r["cc"].items())))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def build_maps(recs: list[dict], fs: FeatureSpace, step: float = 0.02) -> tuple[dict, dict]:
    """KnobMap per model and the raw (positions, Z) per model, over the FeatureSpace `fs` (same row order)."""
    maps, raw = {}, {}
    for m in sorted({r["model"] for r in recs}):
        ii = [i for i, r in enumerate(recs) if r["model"] == m]
        pos = np.array([recs[i]["pos"] for i in ii], float)
        Zm = fs.Z[ii]
        maps[m] = KnobMap(pos, Zm, step=step)
        raw[m] = (pos, Zm)
    return maps, raw


# ------------------------------------------------------------------------------------ search interface

_LIB_CACHE: dict = {}
MIN_FIT_DB = -3.0              # a twin that explains less than half the output's energy: the DI is not this
                               # output's input (another performance, or badly aligned): no estimate
CAL_FILE = Path("cache") / "distortion-step4" / "calibration.npz"


def _library_space(root: Path, kind: str = "curves", dirs=("dfp-v1",)):
    key = (str(root), kind, dirs)
    if key not in _LIB_CACHE:
        recs = load_library(root, dirs)
        if not recs:
            raise FileNotFoundError(f"no dfp-v1 fingerprints under {root / 'cache' / 'distortion-fp'}")
        fs = FeatureSpace([feature_blocks(r["arr"], r["sc"], kind) for r in recs])
        maps, _ = build_maps(recs, fs)
        knobs = {r["model"]: (r["drive"], r["tone"]) for r in recs}
        cal = None
        cp = root / CAL_FILE
        if cp.is_file():
            z = np.load(cp)
            if list(z["names"]) == fs.names and list(z["sizes"]) == fs.sizes and str(z["kind"]) == kind:
                cal = (z["bias"], z["rel"])
        _LIB_CACHE[key] = (fs, maps, knobs, cal)
    return _LIB_CACHE[key]


def estimate_knobs_with_confidence(target: np.ndarray, di: np.ndarray | None, models: list[str], *,
                                   root: Path | None = None, kind: str = "curves", align_take: bool = True) -> dict:
    """Fingerprint inversion (condition A of T181) with its evidence.

    Returns {"knobs": {model: {knob: position 0..1}}, "ranking": [(model, distance)] over `models`,
    "fit_db": the twin's residual (dB; lower is better), "lag": samples, "calibrated": bool,
    "confidence": "none" | "low" | "medium", "reason": str}.

    Confidence, from docs/notes/distortion-step4.md: "none" when there is no DI or the DI does not explain the
    output (fit worse than MIN_FIT_DB); "medium" when the fit is better than -15 dB; else "low". Even "medium"
    means only what the note measured (knobs on the right model within tolerance about 3 times in 4 for
    Drive, about 1 time in 2 for Tone); the model ranking is weaker than that, so use it with a shortlist."""
    out = {"knobs": {}, "ranking": [], "fit_db": None, "lag": 0, "calibrated": False, "confidence": "none",
           "reason": ""}
    if di is None:
        out["reason"] = "no DI: the blind knob map needs a render gallery (see the note, condition B)"
        return out
    root = root or Path(__file__).resolve().parents[3]
    fs, maps, knobs, cal = _library_space(root, kind)
    x, y = np.asarray(di, float).reshape(-1), np.asarray(target, float).reshape(-1)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    lag = 0
    if align_take:                       # a loosely aligned take: move it onto the target first
        lag = align(x, y, max_lag=int(0.05 * SR))
        if abs(lag) > 32:
            y = shift(y, lag)
            k = abs(lag) + 256                     # drop the zero-filled edge and a little more
            x, y = x[k:-k], y[k:-k]
        else:
            lag = 0
    tw = fit_twin(x, y)
    out.update(fit_db=float(tw.fit_db), lag=int(lag))
    if not np.isfinite(tw.fit_db) or tw.fit_db > MIN_FIT_DB:
        out["reason"] = f"the DI explains too little of the target (twin fit {tw.fit_db:.1f} dB)"
        return out
    arr, sc = twin_fingerprint(tw)
    z, w = fs.transform(feature_blocks(arr, sc, kind, max_level_dbfs=tw.u_peak_dbfs + 3.0))
    if cal is not None:
        b, rel = cal
        z = z - b * (w > 0)
        w = w * rel
        w = w / max(w.sum(), EPS)
        out["calibrated"] = True
    rows = []
    for m in models:
        if m not in maps:
            continue
        d, p = maps[m].best(z, w)
        dk, tk = knobs[m]
        kn = {dk: float(p[0])}
        if tk is not None and p.size > 1:
            kn[tk] = float(p[1])
        out["knobs"][m] = kn
        rows.append((m, float(d)))
    out["ranking"] = sorted(rows, key=lambda r: r[1])
    out["confidence"] = "medium" if tw.fit_db < -15.0 else "low"
    out["reason"] = f"twin fit {tw.fit_db:.1f} dB; pre high-pass {tw.pre_fc} Hz"
    return out


def estimate_knobs(target: np.ndarray, di: np.ndarray | None, models: list[str], *, root: Path | None = None,
                   kind: str = "curves") -> dict[str, dict[str, float]]:
    """T178 interface: {model: {knob: position 0..1}} by fingerprint inversion. {} when there is no DI or the
    DI does not explain the target. Positions are knob fractions (CC / 127), not parameter values.
    Models with no dfp-v1 fingerprints are left out."""
    return estimate_knobs_with_confidence(target, di, models, root=root, kind=kind)["knobs"]
