"""Term `mrstft`: multi-resolution STFT distance (fine detail). NEEDS AN ALIGNED DI.

This is the only waveform-aligned term: the candidate must be rendered from the same performance as
the target (options A with alignment, or C), sample-aligned. It is not content-invariant and is not
meant to be.

For FFT sizes 512, 2048 and 8192 (hop 1/4, Hann): spectral convergence ||S_t - S_c||_F / ||S_t||_F
plus mean |log S_t - log S_c| on magnitudes, averaged over resolutions. Both signals are scaled to
unit RMS first (the stem's level in the mix is arbitrary); the shorter length wins. An optional
per-sample confidence mask drops target frames the separator is unsure of.
"""
from __future__ import annotations

import numpy as np

from tonematch.metrics._common import EPS, as_mono, mask_frames, stft_power

NAME = "mrstft"
FFT_SIZES = (512, 2048, 8192)
LOG_FLOOR = 1e-5


def _unit_rms(x: np.ndarray) -> np.ndarray:
    return x / (np.sqrt(np.mean(x ** 2)) + EPS)


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    t = as_mono(target, "target")
    c = as_mono(candidate, "candidate")
    n = min(t.size, c.size)
    m = None if mask is None else np.asarray(mask, dtype=np.float64).reshape(-1)
    if m is not None and m.size != t.size:
        raise ValueError(f"mask: expected one value per target sample ({t.size}), got {m.size}")
    t, c = _unit_rms(t[:n]), _unit_rms(c[:n])
    m = None if m is None else m[:n]
    total = 0.0
    for n_fft in FFT_SIZES:
        hop = n_fft // 4
        st = np.sqrt(stft_power(t, n_fft, hop))
        sc = np.sqrt(stft_power(c, n_fft, hop))
        mf = mask_frames(m, n, n_fft, hop)
        if mf is not None and (mf >= 0.5).any():
            keep = mf >= 0.5
            st, sc = st[keep], sc[keep]
        conv = np.linalg.norm(st - sc) / (np.linalg.norm(st) + EPS)
        logd = np.mean(np.abs(np.log(st + LOG_FLOOR) - np.log(sc + LOG_FLOOR)))
        total += conv + logd
    return float(total / len(FFT_SIZES))
