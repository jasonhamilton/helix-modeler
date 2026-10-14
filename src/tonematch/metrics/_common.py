"""Shared signal helpers for the loss kit: framing, STFT power, activity gating, band matrices.

Everything is plain numpy/scipy, 48 kHz mono float. No term may depend on absolute level: the target is
a separated stem whose level in the song mix is arbitrary, so every statistic is taken relative to the
clip itself.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 48_000
EPS = 1e-12


def as_mono(x: np.ndarray, name: str = "audio") -> np.ndarray:
    """Validate and return a 1-D float64 copy. Stereo (n, 2) or (2, n) is averaged to mono."""
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 2:
        a = a.mean(axis=0 if a.shape[0] <= 2 < a.shape[1] else 1)
    if a.ndim != 1:
        raise ValueError(f"{name}: expected mono audio of shape (n,), got {np.shape(x)}")
    if a.size == 0:
        raise ValueError(f"{name}: empty audio")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name}: contains NaN or inf")
    return a


def frame(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    """(n_frames, n) view of x, zero-padded at the end so the last samples are covered."""
    if x.size < n:
        x = np.pad(x, (0, n - x.size))
    n_frames = 1 + int(np.ceil((x.size - n) / hop))
    need = (n_frames - 1) * hop + n
    if need > x.size:
        x = np.pad(x, (0, need - x.size))
    return np.lib.stride_tricks.sliding_window_view(x, n)[::hop][:n_frames]


def stft_power(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """(n_frames, n_fft//2+1) power spectrogram with a periodic Hann window."""
    win = np.hanning(n_fft + 1)[:-1]
    fr = frame(x, n_fft, hop) * win
    spec = np.fft.rfft(fr, axis=1)
    return (spec.real ** 2 + spec.imag ** 2) / (win ** 2).sum()


def frame_rms(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    fr = frame(x, n, hop)
    return np.sqrt(np.mean(fr ** 2, axis=1) + EPS)


def mask_frames(mask: np.ndarray | None, n_samples: int, n: int, hop: int) -> np.ndarray | None:
    """Per-frame mean of a per-sample confidence mask in [0, 1], aligned with `frame(x, n, hop)`."""
    if mask is None:
        return None
    m = np.asarray(mask, dtype=np.float64).reshape(-1)
    if m.size != n_samples:
        raise ValueError(f"mask: expected one value per target sample ({n_samples}), got {m.size}")
    return frame(np.clip(m, 0.0, 1.0), n, hop).mean(axis=1)


def active_frames(x: np.ndarray, n: int, hop: int, *, rel_db: float = -35.0,
                  mask: np.ndarray | None = None, mask_min: float = 0.5) -> np.ndarray:
    """Boolean per-frame activity: within `rel_db` of the loud (95th percentile) frames, and where the
    separator's confidence mask (if any) is at least `mask_min`. Always keeps at least one frame."""
    rms = frame_rms(x, n, hop)
    db = 20 * np.log10(rms)
    act = db >= np.percentile(db, 95) + rel_db
    mf = mask_frames(mask, x.size, n, hop)
    if mf is not None:
        act &= mf >= mask_min
    if not act.any():
        act[int(np.argmax(rms))] = True
    return act


def fractional_octave_bands(f_lo: float, f_hi: float, per_octave: int) -> np.ndarray:
    """Band centre frequencies, log-spaced, `per_octave` per octave from f_lo to f_hi inclusive-ish."""
    n = int(np.floor(per_octave * np.log2(f_hi / f_lo))) + 1
    return f_lo * 2.0 ** (np.arange(n) / per_octave)


def band_matrix(centres: np.ndarray, n_fft: int, sr: int, per_octave: int) -> np.ndarray:
    """(n_bands, n_bins) matrix of smooth (raised-cosine in log frequency) band weights, each row
    summing to 1, so `M @ power` is the band's mean power. Overlapping bands smooth out individual
    harmonics, which is what keeps the long-term spectrum from tracking the notes played."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    lf = np.log2(np.maximum(freqs, 1.0))
    width = 1.0 / per_octave  # half-width of the raised cosine, in octaves (adjacent bands overlap 50%)
    rows = []
    for c in centres:
        d = (lf - np.log2(c)) / width
        w = np.where(np.abs(d) < 1, 0.5 * (1 + np.cos(np.pi * d)), 0.0)
        s = w.sum()
        rows.append(w / s if s > 0 else w)
    return np.asarray(rows)
