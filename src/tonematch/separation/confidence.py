"""Artifact-confidence masks (time-frequency), a heuristic proxy, not a calibrated probability.

mask = sqrt(dominance * consistency), zeroed where the guitar stem is below the noise floor.
  dominance   guitar power / (guitar + others power): where the guitar is nearly alone, separation
              errors barely matter, so the bin is trustworthy.
  consistency 1 - |sum(stems) - mix| / |mix|: a separator whose stems do not add back to the mix
              is guessing there. Trivially 1 for user-supplied stems (others = mix - guitar).
Not measured against ground truth on real songs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import stft

from tonematch.separation.stems import Stems

N_FFT, HOP = 2048, 512


@dataclass
class Mask:
    values: np.ndarray        # [freq, time] float32 in 0..1
    sr: int
    n_fft: int
    hop: int
    energy_weighted_mean: float   # headline proxy, weighted by guitar-stem power
    exposed_fraction: float       # fraction of active frames where the guitar dominates (>0.8)
    method: str = "dominance*consistency v0"

    def summary(self) -> dict:
        return {"method": self.method, "energy_weighted_mean": self.energy_weighted_mean,
                "exposed_fraction": self.exposed_fraction}


def _spec(x: np.ndarray, sr: int) -> np.ndarray:
    return stft(x.mean(axis=0), fs=sr, nperseg=N_FFT, noverlap=N_FFT - HOP)[2]


def confidence_mask(stems: Stems) -> Mask:
    g, o, m = (_spec(a, stems.sr) for a in (stems.guitar, stems.others, stems.mix))
    pg, po = np.abs(g) ** 2, np.abs(o) ** 2
    dominance = pg / (pg + po + 1e-12)
    if stems.others_approximate:
        consistency = np.ones_like(dominance)
    else:
        resid = np.abs(g + o - m)
        consistency = 1.0 - np.clip(resid / (np.abs(m) + 1e-9), 0.0, 1.0)
    mask = np.sqrt(dominance * consistency)
    floor = pg.max() * 1e-9 if pg.max() > 0 else 1.0
    mask = np.where(pg > floor, mask, 0.0).astype(np.float32)
    w = pg / (pg.sum() + 1e-20)
    frame_power = pg.sum(axis=0)
    active = frame_power > frame_power.max() * 1e-4 if frame_power.max() > 0 else frame_power > 1
    frame_dom = (pg.sum(axis=0) / (pg.sum(axis=0) + po.sum(axis=0) + 1e-12))
    exposed = float(np.mean(frame_dom[active] > 0.8)) if active.any() else 0.0
    return Mask(mask, stems.sr, N_FFT, HOP, float((mask * w).sum()), exposed)
