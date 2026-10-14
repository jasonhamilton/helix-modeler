"""Decode, resample to 48 kHz, loudness-normalize, hash. numpy/scipy/soundfile only."""
from __future__ import annotations

import hashlib
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import lfilter, resample_poly

from tonematch.gpu import SAMPLE_RATE

TARGET_LUFS = -23.0
# BS.1770 K-weighting at 48 kHz: high-shelf then high-pass.
_SHELF = ([1.53512485958697, -2.69169618940638, 1.19839281085285],
          [1.0, -1.69065929318241, 0.73248077421585])
_HP = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])


def file_hash(path: str | Path) -> str:
    """sha256 of the file bytes, first 16 hex chars. Names the cache directory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Resample [channels, samples] along the last axis."""
    if sr_in == sr_out:
        return x
    g = gcd(sr_in, sr_out)
    return resample_poly(x, sr_out // g, sr_in // g, axis=-1).astype(np.float32)


def to_stereo(x: np.ndarray) -> np.ndarray:
    if x.shape[0] == 1:
        return np.repeat(x, 2, axis=0)
    return x[:2]


def load_audio(path: str | Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode to float32 stereo [2, n] at `sr`."""
    data, file_sr = sf.read(str(path), dtype="float32", always_2d=True)
    return to_stereo(resample(np.ascontiguousarray(data.T), file_sr, sr)).astype(np.float32)


def integrated_loudness(x: np.ndarray) -> float:
    """BS.1770 gated integrated loudness in LUFS for 48 kHz audio [channels, n]."""
    y = lfilter(*_HP, lfilter(*_SHELF, x, axis=-1), axis=-1)
    block, hop = int(0.4 * SAMPLE_RATE), int(0.1 * SAMPLE_RATE)
    n = y.shape[-1]
    if n < block:
        z = np.mean(y**2, axis=-1).sum()
        return float(-0.691 + 10 * np.log10(max(z, 1e-12)))
    starts = np.arange(0, n - block + 1, hop)
    z = np.stack([np.mean(y[:, s:s + block] ** 2, axis=-1).sum() for s in starts])
    lk = -0.691 + 10 * np.log10(np.maximum(z, 1e-12))
    keep = lk > -70.0
    if not keep.any():
        return -70.0
    rel = -0.691 + 10 * np.log10(z[keep].mean()) - 10.0
    keep = lk > max(rel, -70.0)
    if not keep.any():
        return -70.0
    return float(-0.691 + 10 * np.log10(z[keep].mean()))


def normalize(x: np.ndarray, target: float = TARGET_LUFS) -> tuple[np.ndarray, float]:
    """Return (scaled copy, gain_db). Silence is left alone (gain 0)."""
    lufs = integrated_loudness(x)
    if lufs <= -69.9:
        return x, 0.0
    gain_db = target - lufs
    return (x * 10 ** (gain_db / 20)).astype(np.float32), float(gain_db)


def write_wav(path: str | Path, x: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    sf.write(str(path), np.ascontiguousarray(x.T), sr, subtype="FLOAT")


def align_to(ref: np.ndarray, other: np.ndarray, max_lag_s: float = 2.0) -> tuple[np.ndarray, int]:
    """Shift `other` [c, n] to line up with `ref` by FFT cross-correlation of the mono sums.

    Returns (other aligned and padded/trimmed to ref length, shift applied in samples; positive =
    other was moved later).
    """
    a, b = ref.mean(axis=0), other.mean(axis=0)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    m = int(max_lag_s * SAMPLE_RATE)
    cand = np.concatenate([cc[:m + 1], cc[-m:]])
    idx = np.concatenate([np.arange(m + 1), np.arange(-m, 0)])
    lag = int(idx[int(np.argmax(cand))])  # a[t + lag] ~ b[t]: move b later by lag
    out = np.zeros((other.shape[0], ref.shape[1]), dtype=np.float32)
    if lag >= 0:
        k = min(other.shape[1], ref.shape[1] - lag)
        if k > 0:
            out[:, lag:lag + k] = other[:, :k]
    else:
        k = min(other.shape[1] + lag, ref.shape[1])
        if k > 0:
            out[:, :k] = other[:, -lag:-lag + k]
    return out, lag
