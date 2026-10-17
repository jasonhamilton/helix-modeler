"""Term `mrstft_v2`: multi-resolution STFT distance for the KNOWN-DI case (T096). NEEDS AN ALIGNED DI.

A versioned successor of `mrstft` (v1 stays unchanged, so v1 ledger rows stay comparable). Like v1 it
compares the candidate rendered from the same DI clip as the target, frame by frame, and it is not
content-invariant. Unlike v1 every choice is a field of `Config`, so `scripts/mrstft_tune.py` can tune
it on the train split of `helix-trust-v2`; `DEFAULT` is the configuration chosen there (see
docs/DECISIONS.md, T096) and `V1` reproduces `mrstft` exactly.

Per resolution (FFT size n, hop n // hop_div, periodic Hann):
  1. power spectrogram of each signal, after the level step (below);
  2. optional A-weighting-like tilt (per bin) and optional mel or ERB band summation;
  3. optional silence gate: frames where BOTH signals are more than `silence_db` below the target's
     loud (95th percentile) frames are dropped; a confidence mask drops target frames as in v1;
  4. on magnitudes: w_sc x spectral convergence ||T - C|| / ||T|| + w_log x mean |log T - log C|
     (floor `floor_db` below the target's peak, or v1's absolute 1e-5) + w_lin x sum |T - C| / sum T.
The total is the mean over resolutions.

Level step (never absolute level: the stem's level in the mix is arbitrary and loudness is the search
level guard's job, so this term must not pull the level toward the target while the guard pulls it
toward its reference):
  unit_rms    both signals scaled to unit RMS over the whole clip (v1);
  active_rms  the same, RMS over the active (non-silent) frames only;
  median_log  a robust gain match per resolution: the candidate is scaled by the median log-ratio of
              the two magnitude spectrograms over the target's active, above-floor cells, so a
              difference confined to part of the clip (a delay tail, a gated note) does not leak into
              the rest through the normalisation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache

import numpy as np

from tonematch.metrics._common import (
    EPS,
    SAMPLE_RATE,
    active_frames,
    as_mono,
    frame,
    mask_frames,
    stft_power,
)

NAME = "mrstft_v2"
LEVELS = ("unit_rms", "active_rms", "median_log")
BANDS = ("linear", "mel", "erb")
V1_LOG_FLOOR = 1e-5


@dataclass(frozen=True)
class Config:
    fft_sizes: tuple[int, ...] = (512, 2048, 8192)
    hop_div: int = 4
    level: str = "unit_rms"
    bands: str = "linear"
    n_bands: int = 96
    a_weight: bool = False
    w_sc: float = 1.0
    w_log: float = 1.0
    w_lin: float = 0.0
    floor_db: float | None = None      # None: v1's absolute floor 1e-5 (after unit-RMS scaling)
    silence_db: float | None = None    # None: keep every frame
    f_lo: float = 20.0
    f_hi: float = 20_000.0

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        if self.bands not in BANDS:
            raise ValueError(f"bands must be one of {BANDS}")
        if not self.fft_sizes or any(n < 16 or n & (n - 1) for n in self.fft_sizes):
            raise ValueError("fft_sizes must be powers of two >= 16")
        if self.hop_div < 1:
            raise ValueError("hop_div must be >= 1")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["fft_sizes"] = list(self.fft_sizes)
        return d

    def with_(self, **kw) -> Config:
        return replace(self, **kw)


V1 = Config()

# Frozen configurations, by version. Never edit a frozen entry: a new choice is a new version, so
# every ledger row that names a version stays reproducible.
#   "2.0": chosen on the helix-trust-v2 TRAIN split by the registered staged search and selection rule
#          (scripts/mrstft_tune.py stages 1-5, T096; docs/DECISIONS.md 2026-10-16): FFT 512/2048/8192 at
#          hop 1/4 (v1's), whole-clip unit RMS (v1's), 64 ERB bands, linear-magnitude L1 only, no silence
#          gate (no effect on these clips). Train rule-B rank-1 0.990 aligned / 0.948 sep (v1: 0.740 / 0.854).
FROZEN: dict[str, Config] = {
    "1": V1,
    "2.0": Config(fft_sizes=(512, 2048, 8192), hop_div=4, level="unit_rms", bands="erb", n_bands=64,
                  a_weight=False, w_sc=0.0, w_log=0.0, w_lin=1.0, floor_db=None, silence_db=None,
                  f_lo=20.0, f_hi=20_000.0),
}
VERSION = "2.0"
DEFAULT = FROZEN[VERSION]


# --------------------------------------------------------------------------- weighting

def a_weight_db(f: np.ndarray) -> np.ndarray:
    """IEC 61672 A-weighting in dB (0 dB at 1 kHz)."""
    f2 = np.maximum(np.asarray(f, dtype=np.float64), 1e-3) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
                                     * (f2 + 12194.0 ** 2))
    return 20 * np.log10(ra) + 2.0


def _warp(f, kind):
    f = np.asarray(f, dtype=np.float64)
    if kind == "mel":
        return 2595.0 * np.log10(1 + f / 700.0)
    return 21.4 * np.log10(1 + 0.00437 * f)          # ERB-number scale (Glasberg and Moore)


def _unwarp(z, kind):
    z = np.asarray(z, dtype=np.float64)
    if kind == "mel":
        return 700.0 * (10 ** (z / 2595.0) - 1)
    return (10 ** (z / 21.4) - 1) / 0.00437


@lru_cache(maxsize=64)
def weighting(n_fft: int, bands: str, n_bands: int, a_weight: bool, f_lo: float, f_hi: float,
              sr: int = SAMPLE_RATE):
    """What is applied to a power spectrogram (frames x bins): None for plain bins, a per-bin vector
    for the A-weighting alone, or a sparse (n_bins, n_out) matrix of triangular band filters (with
    the A-weighting folded in). Bands too narrow for this FFT size to hold a bin are dropped, so small
    FFTs get fewer bands."""
    from scipy import sparse

    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    aw = 10 ** (a_weight_db(freqs) / 10) if a_weight else None
    if bands == "linear":
        return aw
    edges = _unwarp(np.linspace(_warp(f_lo, bands), _warp(min(f_hi, sr / 2), bands), n_bands + 2), bands)
    rows = []
    for k in range(n_bands):
        lo, c, hi = edges[k], edges[k + 1], edges[k + 2]
        up = (freqs - lo) / max(c - lo, EPS)
        down = (hi - freqs) / max(hi - c, EPS)
        w = np.clip(np.minimum(up, down), 0.0, None)
        if w.sum() <= 0:
            continue
        rows.append(w if aw is None else w * aw)
    return sparse.csr_matrix(np.asarray(rows).T)


def apply_weighting(p: np.ndarray, w) -> np.ndarray:
    if w is None:
        return p
    if isinstance(w, np.ndarray):
        return p * w
    return np.asarray(w.T.dot(p.T).T)


# --------------------------------------------------------------------------- pieces

def hop_for(n_fft: int, cfg: Config) -> int:
    return max(1, n_fft // cfg.hop_div)


def spectrum(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """(n_frames, n_fft // 2 + 1) power spectrogram (float32 to keep the tuning cache small)."""
    return stft_power(x, n_fft, hop).astype(np.float32)


def level_scale(x: np.ndarray, level: str) -> float:
    """Time-domain level used to normalise a signal before the STFT (1.0 for `median_log`, which
    matches the gain per resolution instead)."""
    if level == "unit_rms":
        return float(np.sqrt(np.mean(x ** 2)) + EPS)
    if level == "active_rms":
        n, hop = 2048, 512
        act = active_frames(x, n, hop)
        fr = frame(x, n, hop)[act]
        return float(np.sqrt(np.mean(fr ** 2)) + EPS)
    return float(np.sqrt(np.mean(x ** 2)) + EPS)      # median_log: unit RMS first, then matched


def compare(pt: np.ndarray, pc: np.ndarray, n_fft: int, cfg: Config,
            mf: np.ndarray | None = None) -> dict[str, float]:
    """Distance components for one resolution. `pt`, `pc`: power spectrograms of the level-scaled
    target and candidate (same frames). `mf`: per-frame mask confidence or None."""
    n = min(pt.shape[0], pc.shape[0])
    pt = np.asarray(pt[:n], dtype=np.float64)
    pc = np.asarray(pc[:n], dtype=np.float64)
    w = weighting(n_fft, cfg.bands, cfg.n_bands, cfg.a_weight, cfg.f_lo, cfg.f_hi)
    pt, pc = apply_weighting(pt, w), apply_weighting(pc, w)
    keep = np.ones(n, bool)
    if mf is not None and (mf[:n] >= 0.5).any():
        keep &= mf[:n] >= 0.5
    st, sc = np.sqrt(pt), np.sqrt(pc)
    if cfg.floor_db is None:
        floor = V1_LOG_FLOOR
    else:
        floor = float(np.max(st)) * 10 ** (cfg.floor_db / 20) + EPS
    if cfg.level == "median_log":
        lt = 10 * np.log10(pt.sum(axis=1) + EPS)
        act = lt >= np.percentile(lt, 95) - 35.0
        cells = (st >= floor) & act[:, None] & keep[:, None]
        if cells.any():
            g = float(np.median(np.log(sc[cells] + floor) - np.log(st[cells] + floor)))
            sc = sc * np.exp(-g)
    if cfg.silence_db is not None:
        lt = 10 * np.log10(np.sum(st ** 2, axis=1) + EPS)
        lc = 10 * np.log10(np.sum(sc ** 2, axis=1) + EPS)
        thr = np.percentile(lt, 95) + cfg.silence_db
        quiet = (lt < thr) & (lc < thr)
        if (keep & ~quiet).any():
            keep &= ~quiet
    if not keep.all():
        st, sc = st[keep], sc[keep]
    out = {"sc": float(np.linalg.norm(st - sc) / (np.linalg.norm(st) + EPS))}
    out["log"] = float(np.mean(np.abs(np.log(st + floor) - np.log(sc + floor)))) if cfg.w_log else 0.0
    out["lin"] = float(np.sum(np.abs(st - sc)) / (np.sum(st) + EPS)) if cfg.w_lin else 0.0
    return out


def combine(parts: list[dict[str, float]], cfg: Config) -> float:
    return float(np.mean([cfg.w_sc * p["sc"] + cfg.w_log * p["log"] + cfg.w_lin * p["lin"] for p in parts]))


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None,
         config: Config | None = None) -> float:
    cfg = DEFAULT if config is None else config
    t = as_mono(target, "target")
    c = as_mono(candidate, "candidate")
    n = min(t.size, c.size)
    m = None if mask is None else np.asarray(mask, dtype=np.float64).reshape(-1)
    if m is not None and m.size != t.size:
        raise ValueError(f"mask: expected one value per target sample ({t.size}), got {m.size}")
    t, c = t[:n], c[:n]
    m = None if m is None else m[:n]
    t = t / level_scale(t, cfg.level)
    c = c / level_scale(c, cfg.level)
    parts = []
    for n_fft in cfg.fft_sizes:
        hop = hop_for(n_fft, cfg)
        parts.append(compare(spectrum(t, n_fft, hop), spectrum(c, n_fft, hop), n_fft, cfg,
                             mask_frames(m, n, n_fft, hop)))
    return combine(parts, cfg)
