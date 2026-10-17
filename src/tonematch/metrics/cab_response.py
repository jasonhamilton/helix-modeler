"""Cab and impulse-response (IR) frequency responses: measure, describe, compare, estimate.

Not a LossKit term (the contract's term list is unchanged). These are helpers for cab work:

- `ir_response(ir)` measures an IR's magnitude response on the block-probe fingerprint grid (1/6 octave,
  31.25 Hz - 16 kHz, power-averaged over +-1/24 octave, the same smoothing as `tonematch.probe`), so a
  user IR and a Helix cab fingerprint (`linear.mag_db`) can be compared directly.
- `band_levels(ir)` gives 1/3-octave band levels for reports.
- `response_features(grid, mag_db)` gives the low resonance, low and high roll-off, presence,
  brightness, tilt and centroid of a response.
- `response_distance(a, b)` is the RMS difference in dB over a band after the best gain, optionally after
  removing a tilt (detrend=1) or a tilt plus a curve (detrend=2), which is what a simple EQ could undo.
- `transfer_response(dry, wet)` estimates the response from an aligned input/output pair (H1 estimator:
  cross-spectrum over input power, band-averaged before the division), with the coherence per band.
  This is the "deconvolve the target against the amp-only render" estimator; it needs the same notes.
- `ltas_ratio_response(target, reference)` is the estimator for different notes: the ratio of long-term
  average spectra. It is biased by any difference in the two sources' spectra.
- `helix_length(ir, n)` mimics the Helix IR block's fixed length (2048 or 1024 samples at 48 kHz).

All audio is mono float at 48 kHz unless a sample rate is given.
"""
from __future__ import annotations

from math import gcd
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48_000
EPS = 1e-20

#: The block-probe fingerprint grid: 55 points, 1/6 octave, 31.25 Hz - 16 kHz.
FINGERPRINT_GRID = 31.25 * 2.0 ** (np.arange(55) / 6.0)
#: 1/3-octave band centres (base-2), 24.8 Hz - 20.2 kHz, 30 bands.
THIRD_OCTAVE = 1000.0 * 2.0 ** (np.arange(-16, 14) / 3.0)

REF_BAND = (500.0, 2000.0)
COMPARE_BAND = (100.0, 8000.0)


# --------------------------------------------------------------------------------------- loading
def resample(x: np.ndarray, sr_in: int, sr_out: int = SAMPLE_RATE) -> np.ndarray:
    if sr_in == sr_out:
        return x
    from scipy.signal import resample_poly

    g = gcd(int(sr_in), int(sr_out))
    return resample_poly(x, sr_out // g, sr_in // g)


def load_ir(path: str | Path, sr: int = SAMPLE_RATE) -> tuple[np.ndarray, dict]:
    """Read a WAV IR as mono float64 at `sr`. Stereo files use the LEFT channel, as HX Edit does."""
    import soundfile as sf

    data, file_sr = sf.read(str(path), dtype="float64", always_2d=True)
    info = {"file_sr": int(file_sr), "channels": int(data.shape[1]), "frames": int(data.shape[0])}
    x = data[:, 0]
    if data.shape[1] > 1:
        info["right_differs_db"] = _side_difference_db(data[:, 0], data[:, 1])
    return resample(x, file_sr, sr), info


def _side_difference_db(left: np.ndarray, right: np.ndarray) -> float:
    """Energy of (left - right) relative to left, in dB (-inf for identical channels)."""
    e = float(np.sum(left ** 2)) + EPS
    d = float(np.sum((left - right) ** 2))
    return float(10 * np.log10(d / e + EPS))


def helix_length(ir: np.ndarray, n: int = 2048, fade: int = 64) -> np.ndarray:
    """The IR as a fixed-length Helix IR block would hold it: truncated or zero-padded to `n` samples.

    A short half-Hann fade ends a truncated IR. Line 6 documents that the 1024 variant "fades out the IR
    halfway through"; the exact fade shape is not documented, so `fade` is an assumption.
    """
    x = np.asarray(ir, dtype=np.float64)
    if x.size <= n:
        return np.pad(x, (0, n - x.size))
    y = x[:n].copy()
    if fade > 0:
        y[-fade:] *= np.cos(np.linspace(0, np.pi / 2, fade)) ** 2
    return y


# --------------------------------------------------------------------------------------- spectra
def _power_spectrum(ir: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    n = max(1 << 16, 1 << int(np.ceil(np.log2(max(len(ir), 2)))))
    spec = np.fft.rfft(np.asarray(ir, dtype=np.float64), n)
    return np.fft.rfftfreq(n, 1.0 / sr), spec.real ** 2 + spec.imag ** 2


def _band_average(freqs: np.ndarray, power: np.ndarray, centres: np.ndarray, half_oct: float) -> np.ndarray:
    lo = centres * 2.0 ** (-half_oct)
    hi = centres * 2.0 ** half_oct
    c = np.concatenate([[0.0], np.cumsum(power)])
    i0 = np.searchsorted(freqs, lo, side="left")
    i1 = np.searchsorted(freqs, hi, side="right")
    out = np.empty(centres.size)
    for k, (a, b) in enumerate(zip(i0, i1)):
        if b <= a:  # band narrower than a bin: nearest bin
            j = int(np.argmin(np.abs(freqs - centres[k])))
            out[k] = power[j]
        else:
            out[k] = (c[b] - c[a]) / (b - a)
    return out


def ir_response(ir: np.ndarray, sr: int = SAMPLE_RATE, grid: np.ndarray = FINGERPRINT_GRID,
                half_oct: float = 1 / 24) -> np.ndarray:
    """Magnitude response in dB on `grid` (power-averaged over +-half_oct octave). Absolute gain kept."""
    f, p = _power_spectrum(ir, sr)
    return 10 * np.log10(_band_average(f, p, np.asarray(grid, float), half_oct) + EPS)


def band_levels(ir: np.ndarray, sr: int = SAMPLE_RATE, centres: np.ndarray = THIRD_OCTAVE) -> np.ndarray:
    """1/3-octave band levels in dB (band power average over +-1/6 octave)."""
    return ir_response(ir, sr, centres, half_oct=1 / 6)


def band_mean(grid: np.ndarray, mag_db: np.ndarray, band: tuple[float, float]) -> float:
    g = np.asarray(grid)
    sel = (g >= band[0]) & (g <= band[1])
    return float(np.mean(np.asarray(mag_db)[..., sel]))


def normalise(grid: np.ndarray, mag_db: np.ndarray, ref: tuple[float, float] = REF_BAND) -> np.ndarray:
    """Response relative to its mean level over `ref` (default 500 Hz - 2 kHz)."""
    m = np.asarray(mag_db, dtype=np.float64)
    g = np.asarray(grid)
    sel = (g >= ref[0]) & (g <= ref[1])
    return m - m[..., sel].mean(axis=-1, keepdims=True)


# --------------------------------------------------------------------------------------- features
def response_features(grid: np.ndarray, mag_db: np.ndarray) -> dict:
    """Describe one response (any grid at least as fine as 1/6 octave, 31 Hz - 16 kHz).

    Levels are relative to the 500 Hz - 2 kHz mean (the reference, 0 dB).

    - resonance_hz / resonance_db: highest point between 50 and 250 Hz (the cab's low resonance bump).
    - low_rolloff_hz: going down from the resonance, the first frequency 6 dB below the reference.
    - high_rolloff_hz: the highest frequency above 1 kHz still within 10 dB of the reference.
    - presence_db: mean 2-5 kHz; brightness_db: mean 5-10 kHz; both re the reference.
    - tilt_db_per_oct: least-squares slope 100 Hz - 5 kHz.
    - centroid_hz: power centroid 80 Hz - 12 kHz.
    """
    g = np.asarray(grid, dtype=np.float64)
    m = normalise(g, mag_db)
    lowsel = (g >= 50) & (g <= 250)
    i_res = int(np.flatnonzero(lowsel)[np.argmax(m[lowsel])])
    low = np.nan
    for i in range(i_res, -1, -1):
        if m[i] <= -6.0:
            low = float(_interp_cross(g, m, i, i + 1, -6.0))
            break
    above = np.flatnonzero((g >= 1000) & (m >= -10.0))
    high = float(g[above[-1]]) if above.size else np.nan
    if above.size and above[-1] + 1 < g.size:
        high = float(_interp_cross(g, m, above[-1] + 1, above[-1], -10.0))
    tsel = (g >= 100) & (g <= 5000)
    slope = np.polyfit(np.log2(g[tsel]), m[tsel], 1)[0]
    csel = (g >= 80) & (g <= 12000)
    pw = 10 ** (m[csel] / 10) * g[csel]  # log grid: weight each point by its bandwidth
    centroid = float(np.sum(pw * g[csel]) / np.sum(pw))
    return {
        "resonance_hz": float(g[i_res]),
        "resonance_db": float(m[i_res]),
        "low_rolloff_hz": low,
        "high_rolloff_hz": high,
        "presence_db": band_mean(g, m, (2000, 5000)),
        "brightness_db": band_mean(g, m, (5000, 10000)),
        "tilt_db_per_oct": float(slope),
        "centroid_hz": centroid,
    }


def _interp_cross(g: np.ndarray, m: np.ndarray, i_below: int, i_above: int, level: float) -> float:
    """Log-frequency interpolation of where m crosses `level` between two neighbouring grid points."""
    if not (0 <= i_above < g.size) or m[i_above] == m[i_below]:
        return float(g[i_below])
    t = (level - m[i_below]) / (m[i_above] - m[i_below])
    return float(2 ** (np.log2(g[i_below]) + t * (np.log2(g[i_above]) - np.log2(g[i_below]))))


# --------------------------------------------------------------------------------------- distance
def _residual(grid, a, b, band, detrend):
    g = np.asarray(grid, dtype=np.float64)
    sel = (g >= band[0]) & (g <= band[1])
    d = np.asarray(a, float)[..., sel] - np.asarray(b, float)[..., sel]
    x = np.log2(g[sel])
    basis = np.vander(x - x.mean(), detrend + 1)  # columns x^k .. 1
    coef, *_ = np.linalg.lstsq(basis, np.atleast_2d(d).T, rcond=None)
    return (np.atleast_2d(d).T - basis @ coef).T


def response_distance(grid: np.ndarray, a: np.ndarray, b: np.ndarray, band: tuple[float, float] = COMPARE_BAND,
                      detrend: int = 0) -> float | np.ndarray:
    """RMS dB difference over `band` after the best gain (detrend=0), tilt (1) or tilt + curve (2).

    `a` and `b` broadcast: (g,) vs (n, g) gives n distances.
    """
    r = _residual(grid, a, b, band, detrend)
    out = np.sqrt(np.mean(r ** 2, axis=-1))
    return float(out[0]) if np.ndim(a) == 1 and np.ndim(b) == 1 else out


def nearest(grid: np.ndarray, query: np.ndarray, bank: np.ndarray, k: int = 5,
            band: tuple[float, float] = COMPARE_BAND, detrend: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Indices and distances of the k bank rows closest to `query`."""
    d = np.atleast_1d(response_distance(grid, np.asarray(query)[None, :], np.asarray(bank), band, detrend))
    idx = np.argsort(d)[:k]
    return idx, d[idx]


# --------------------------------------------------------------------------------------- estimation
def transfer_response(dry: np.ndarray, wet: np.ndarray, sr: int = SAMPLE_RATE, grid: np.ndarray = FINGERPRINT_GRID,
                      nperseg: int = 8192, half_oct: float = 1 / 24) -> tuple[np.ndarray, np.ndarray]:
    """Response (dB) and coherence (0-1) per grid band from an aligned dry/wet pair (H1 estimator).

    Cross- and auto-spectra are band-averaged before the division, so notes that leave gaps between
    harmonics still give an estimate wherever the band holds some input energy. The pair must be aligned
    to within a small fraction of `nperseg`; a constant latency inside the segment only adds phase.
    """
    from scipy.signal import csd, welch

    x = np.asarray(dry, dtype=np.float64)
    y = np.asarray(wet, dtype=np.float64)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    f, pxy = csd(x, y, fs=sr, nperseg=nperseg)
    _, pxx = welch(x, fs=sr, nperseg=nperseg)
    _, pyy = welch(y, fs=sr, nperseg=nperseg)
    g = np.asarray(grid, float)
    # |Pxy| is averaged bin by bin: a latency rotates the phase across a band and would cancel in a
    # complex band sum. Coherence is per bin, then averaged with the input power as weight.
    # Denominators use the smallest positive float, not EPS: quiet renders have power densities near
    # 1e-11, whose product is below any fixed epsilon (that bug read a coherence of 0.003 as 1.0's).
    tiny = np.finfo(np.float64).tiny
    axy = _band_average(f, np.abs(pxy), g, half_oct)
    sxx = _band_average(f, pxx, g, half_oct)
    mag = 20 * np.log10(np.maximum(axy, tiny) / np.maximum(sxx, tiny))
    coh_bin = np.abs(pxy) ** 2 / np.maximum(pxx * pyy, tiny)
    coh = _band_average(f, coh_bin * pxx, g, half_oct) / np.maximum(sxx, tiny)
    return mag, np.clip(coh, 0.0, 1.0)


def ltas_ratio_response(target: np.ndarray, reference: np.ndarray, sr: int = SAMPLE_RATE,
                        grid: np.ndarray = FINGERPRINT_GRID, nperseg: int = 8192,
                        half_oct: float = 1 / 24) -> np.ndarray:
    """Response estimate (dB) as the ratio of long-term average spectra: target / reference.

    Works when the two clips play different notes, but any difference in the sources' average spectra
    (register, picking, the amp's own EQ) lands in the estimate.
    """
    from scipy.signal import welch

    f, pt = welch(np.asarray(target, float), fs=sr, nperseg=nperseg)
    _, pr = welch(np.asarray(reference, float), fs=sr, nperseg=nperseg)
    g = np.asarray(grid, float)
    tiny = np.finfo(np.float64).tiny
    return 10 * np.log10(np.maximum(_band_average(f, pt, g, half_oct), tiny)
                         / np.maximum(_band_average(f, pr, g, half_oct), tiny))


def fingerprint_response(fp_setting: dict, level_index: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """(grid, mag_db) from one setting of a block-library fingerprint set (`settings[i]`)."""
    lin = fp_setting["fingerprint"]["linear"]
    return np.asarray(lin["freqs_hz"], float), np.asarray(lin["mag_db"][level_index], float)
