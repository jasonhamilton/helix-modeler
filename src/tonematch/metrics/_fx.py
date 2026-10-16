"""Shared helpers for the time-based effect measurements (delay, reverb, modulation) and the
distortion diagnosis: smoothing, band filters, band levels, deconvolution, alignment, sinusoid fits.

Plain numpy/scipy, 48 kHz mono float64. These measure one render (or one render plus the block's
input) and are compared between engines or against a target; none of them needs the two renders
to be phase-locked.
"""
from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

from tonematch.metrics._common import EPS, SAMPLE_RATE, as_mono

SR = SAMPLE_RATE
OCTAVE_CENTRES = (125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)


def db10(p: float | np.ndarray) -> float | np.ndarray:
    return 10.0 * np.log10(np.maximum(p, EPS * EPS))


def db20(a: float | np.ndarray) -> float | np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(a), EPS))


def smooth_power(x: np.ndarray, win: int) -> np.ndarray:
    """Moving average of x**2 over `win` samples (centred)."""
    return uniform_filter1d(np.asarray(x, np.float64) ** 2, max(int(win), 1), mode="nearest")


def bandpass(x: np.ndarray, centre_hz: float, *, fraction: int = 1, sr: int = SR, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth band-pass of 1/`fraction` octave around `centre_hz`."""
    half = 2.0 ** (1.0 / (2 * fraction))
    lo, hi = centre_hz / half, min(centre_hz * half, 0.49 * sr)
    sos = signal.butter(order, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, np.float64))


def lowpass(x: np.ndarray, cutoff_hz: float, *, sr: int = SR, order: int = 8) -> np.ndarray:
    sos = signal.butter(order, cutoff_hz, btype="lowpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, np.float64))


def third_octave_centres(f_lo: float = 63.0, f_hi: float = 16000.0) -> np.ndarray:
    n = int(np.floor(3 * np.log2(f_hi / f_lo))) + 1
    return f_lo * 2.0 ** (np.arange(n) / 3.0)


def band_levels_db(x: np.ndarray, centres: np.ndarray, *, sr: int = SR, n_fft: int = 8192) -> np.ndarray:
    """Power in each 1/3-octave band (rectangular in frequency) from a Welch PSD, in dB."""
    x = np.asarray(x, np.float64)
    nper = min(n_fft, max(256, x.size))
    f, p = signal.welch(x, fs=sr, nperseg=nper, noverlap=nper // 2, window="hann", detrend=False)
    out = np.empty(len(centres))
    for i, c in enumerate(centres):
        sel = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        out[i] = db10(p[sel].sum() if sel.any() else 0.0)
    return out


def impulse_response(ref: np.ndarray, y: np.ndarray, *, n_out: int, pre: int = 0, rel_reg: float = 1e-3,
                     f_lo: float = 60.0, f_hi: float = 12_000.0, sr: int = SR) -> np.ndarray:
    """Regularised frequency-domain deconvolution of `y` by `ref` (the block's input), band-limited to
    [f_lo, f_hi]. The regulariser is `rel_reg` times the 1/3-octave-smoothed input power, so spectral
    holes between a guitar's harmonics are not blown up. Returns `pre` samples of negative time then
    `n_out` samples from time zero. The floor (about -40 to -50 dB re the direct sound on guitar
    input) is a method limit, not noise in the renders."""
    ref, y = as_mono(ref, "ref"), as_mono(y, "y")
    n = 1 << int(np.ceil(np.log2(max(ref.size, y.size) + n_out + pre)))
    X, Y = np.fft.rfft(ref, n), np.fft.rfft(y, n)
    P = np.abs(X) ** 2
    idx = np.arange(P.size)
    c = np.concatenate([[0.0], np.cumsum(P)])
    lo = np.maximum((idx / 2 ** (1 / 6)).astype(int), 0)
    hi = np.minimum((idx * 2 ** (1 / 6)).astype(int) + 1, P.size)
    Ps = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    H = Y * np.conj(X) / (P + rel_reg * Ps + 1e-30)
    f = np.fft.rfftfreq(n, 1 / sr)
    H[(f < f_lo) | (f > f_hi)] = 0.0
    h = np.fft.irfft(H, n)
    return np.concatenate([h[n - pre:], h[:n_out]]) if pre else h[:n_out]


def xcorr_lag(ref: np.ndarray, x: np.ndarray, max_lag: int) -> float:
    """Lag (samples, fractional by parabolic interpolation) that best aligns x onto ref: x[n + lag] ~ ref[n]."""
    n = 1 << int(np.ceil(np.log2(ref.size + x.size)))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(ref, n)), n)
    c = np.concatenate([c[n - max_lag:], c[:max_lag + 1]])
    k = int(np.argmax(c))
    frac = 0.0
    if 0 < k < c.size - 1:
        a, b, d = c[k - 1], c[k], c[k + 1]
        den = a - 2 * b + d
        frac = 0.5 * (a - d) / den if den != 0 else 0.0
    return k - max_lag + frac


def shift(x: np.ndarray, lag: float) -> np.ndarray:
    """y[n] = x[n + lag] (fractional, FFT phase ramp, zero-padded so nothing wraps)."""
    pad = int(np.ceil(abs(lag))) + 64
    n = 1 << int(np.ceil(np.log2(x.size + 2 * pad)))
    X = np.fft.rfft(np.concatenate([np.zeros(pad), x]), n)
    k = np.fft.rfftfreq(n)
    y = np.fft.irfft(X * np.exp(2j * np.pi * k * lag), n)
    return y[pad:pad + x.size]


def align_gain(ref: np.ndarray, x: np.ndarray, max_lag: int = 2400) -> tuple[np.ndarray, float, float]:
    """Move x onto ref (lag) and least-squares gain-match it. Returns (x aligned and scaled, lag, gain)."""
    m = min(ref.size, x.size)
    ref, x = np.asarray(ref[:m], np.float64), np.asarray(x[:m], np.float64)
    lag = xcorr_lag(ref, x, max_lag)
    xa = shift(x, lag)
    g = float(np.dot(ref, xa) / max(np.dot(xa, xa), EPS))
    return g * xa, lag, g


def fit_sinusoid(t: np.ndarray, y: np.ndarray, freq: float, w: np.ndarray | None = None) -> tuple[float, float, float]:
    """Weighted least squares y ~ c + a cos(2 pi f t) + b sin(2 pi f t). Returns (amplitude, phase rad, offset)."""
    A = np.stack([np.ones_like(t), np.cos(2 * np.pi * freq * t), np.sin(2 * np.pi * freq * t)], axis=1)
    sw = np.ones_like(t) if w is None else np.sqrt(np.maximum(w, 0))
    coef, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    c, a, b = coef
    return float(np.hypot(a, b)), float(np.arctan2(-b, a)), float(c)


def periodogram_peak(t: np.ndarray, y: np.ndarray, f_lo: float, f_hi: float, n: int = 2000,
                     w: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Weighted least-squares (Lomb-Scargle-like) power of a sinusoid at each trial frequency on an
    uneven time grid: the fraction of the (weighted, mean-removed) variance one sinusoid explains."""
    freqs = np.geomspace(f_lo, f_hi, n)
    sw = np.ones_like(t) if w is None else np.maximum(w, 0)
    yc = y - np.average(y, weights=sw)
    var = float(np.sum(sw * yc ** 2)) + EPS
    # weighted normal equations for [1, cos, sin] at every trial frequency at once
    ph = 2 * np.pi * freqs[:, None] * t[None, :]
    co, si = np.cos(ph), np.sin(ph)
    s1 = np.full(n, sw.sum())
    sc, ss = co @ sw, si @ sw
    scc, sss, scs = (co * co) @ sw, (si * si) @ sw, (co * si) @ sw
    A = np.stack([np.stack([s1, sc, ss], -1), np.stack([sc, scc, scs], -1), np.stack([ss, scs, sss], -1)], -2)
    b = np.stack([np.full(n, float(np.sum(sw * yc))), co @ (sw * yc), si @ (sw * yc)], -1)
    A = A + 1e-9 * np.eye(3) * s1[:, None, None]
    coef = np.linalg.solve(A, b[..., None])[..., 0]
    explained = np.einsum("ni,ni->n", coef, b)          # b . coef = variance the fit removes
    return freqs, explained / var
