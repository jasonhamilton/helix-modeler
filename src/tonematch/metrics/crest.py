"""Term `crest_flatness_hnr`: crest factor, spectral flatness and harmonic-to-noise ratio
(gain and distortion amount).

Per active frame (2048 samples, hop 512):
- crest factor, 20*log10(peak / RMS) in dB; clipping and saturation pull it down;
- spectral flatness, 10*log10(geometric / arithmetic mean power) over 150 Hz to 8 kHz; distortion
  fills the spectrum between harmonics and raises it;
- harmonic-to-noise ratio from the normalised autocorrelation peak r over lags 1/1000 to 1/70 s,
  10*log10(r / (1 - r)); intermodulation and noise lower it.

Features are the median and inter-quartile range of each across frames. Distance is a weighted L1
in "scale units" (`SCALES`, `WEIGHTS`). All three are level-invariant by construction.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tonematch.metrics._common import EPS, SAMPLE_RATE, active_frames, as_mono, frame

NAME = "crest_flatness_hnr"
N = 2048
HOP = 512
F_LO, F_HI = 150.0, 8_000.0
LAG_MIN = SAMPLE_RATE // 1000
LAG_MAX = SAMPLE_RATE // 70
# crest dB, flatness dB, HNR dB (medians), then the same for IQRs
# Weights from the proxy spread study (2026-10-14): flatness separates clean from driven by ~20 dB
# and moves ~5 dB with the notes; HNR and the IQRs move with chords versus single notes almost as
# much as with the tone; crest measured after the cab barely tracks drive (filters restore peaks).
# Revisit on synthetic-v1.
SCALES = np.array([1.0, 4.0, 4.0, 1.0, 4.0, 4.0])
WEIGHTS = np.array([0.5, 1.0, 0.25, 0.25, 0.25, 0.1])


@dataclass(frozen=True)
class CrestFeatures:
    vec: np.ndarray  # [crest_med, flat_med, hnr_med, crest_iqr, flat_iqr, hnr_iqr]


def _per_frame(x: np.ndarray, mask: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fr = frame(x, N, HOP)
    act = active_frames(x, N, HOP, mask=mask)
    fr = fr[act]
    fr = fr - fr.mean(axis=1, keepdims=True)
    rms = np.sqrt(np.mean(fr ** 2, axis=1) + EPS)
    crest = 20 * np.log10(np.max(np.abs(fr), axis=1) / rms + EPS)

    win = np.hanning(N)
    spec = np.fft.rfft(fr * win, n=2 * N, axis=1)
    pw = spec.real ** 2 + spec.imag ** 2
    freqs = np.fft.rfftfreq(2 * N, 1.0 / SAMPLE_RATE)
    band = (freqs >= F_LO) & (freqs <= F_HI)
    pb = pw[:, band] + EPS
    flat = 10 * np.log10(np.exp(np.mean(np.log(pb), axis=1)) / np.mean(pb, axis=1))

    # autocorrelation of the windowed frame, divided by the window's own autocorrelation
    ac = np.fft.irfft(pw, axis=1)[:, :N]
    wpw = np.abs(np.fft.rfft(win, n=2 * N)) ** 2
    wac = np.fft.irfft(wpw)[:N]
    ac = ac / (wac + EPS)
    ac = ac / (ac[:, :1] + EPS)
    r = np.clip(ac[:, LAG_MIN:LAG_MAX].max(axis=1), 1e-3, 0.999)
    hnr = 10 * np.log10(r / (1 - r))
    return crest, flat, hnr


def features(x: np.ndarray, mask: np.ndarray | None = None) -> CrestFeatures:
    x = as_mono(x)
    stats = []
    per = _per_frame(x, mask)
    for v in per:
        stats.append(np.median(v))
    for v in per:
        q75, q25 = np.percentile(v, [75, 25])
        stats.append(q75 - q25)
    return CrestFeatures(vec=np.asarray(stats))


def distance(a: CrestFeatures, b: CrestFeatures) -> float:
    return float(np.sum(WEIGHTS * np.abs(a.vec - b.vec) / SCALES) / WEIGHTS.sum())


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    return distance(features(target, mask), features(candidate))
