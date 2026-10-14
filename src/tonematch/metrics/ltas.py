"""Term `ltas_tilt`: long-term average spectrum and spectral tilt (cab, EQ, overall voicing).

Features: the power spectrum averaged over active frames, smoothed into overlapping 1/3-octave
raised-cosine bands from 80 Hz to 12 kHz, in dB with the mean removed (level-invariant), plus the
spectral tilt (least-squares slope in dB per octave over the same bands).

Distance: weighted RMS band difference in dB (bands below 320 Hz down-weighted, see BAND_WEIGHTS) divided by `LTAS_SCALE_DB`, plus the tilt difference divided by
`TILT_SCALE_DB_OCT`, averaged. One unit is roughly "1 dB everywhere" or "0.5 dB/oct of tilt".
Content invariance comes from averaging over many frames and from bands wide enough (2/3 octave
support) to cover several harmonics of any guitar note.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tonematch.metrics._common import (
    EPS,
    SAMPLE_RATE,
    active_frames,
    as_mono,
    band_matrix,
    fractional_octave_bands,
    stft_power,
)

NAME = "ltas_tilt"
N_FFT = 4096
HOP = 1024
F_LO, F_HI, PER_OCT = 80.0, 12_000.0, 3
LTAS_SCALE_DB = 1.0
TILT_SCALE_DB_OCT = 0.5

_CENTRES = fractional_octave_bands(F_LO, F_HI, PER_OCT)
_BANDS = band_matrix(_CENTRES, N_FFT, SAMPLE_RATE, PER_OCT)
_LOG2F = np.log2(_CENTRES)
# Bands below ~320 Hz hold the fundamentals and move by 5 to 13 dB with the notes played (measured on
# proxy DI, see docs/DECISIONS.md), so they count less: weight 0.15 at 80 Hz rising to 1 at 320 Hz.
BAND_WEIGHTS = np.clip(np.log2(_CENTRES / F_LO) / 2.0, 0.15, 1.0)
_BW = BAND_WEIGHTS / BAND_WEIGHTS.sum()


@dataclass(frozen=True)
class LtasFeatures:
    bands_db: np.ndarray   # mean-removed band levels, dB
    tilt_db_oct: float


def features(x: np.ndarray, mask: np.ndarray | None = None) -> LtasFeatures:
    x = as_mono(x)
    p = stft_power(x, N_FFT, HOP)
    act = active_frames(x, N_FFT, HOP, mask=mask)
    ltas = p[act].mean(axis=0)
    db = 10 * np.log10(_BANDS @ ltas + EPS)
    tilt = float(np.polyfit(_LOG2F, db, 1, w=np.sqrt(BAND_WEIGHTS))[0])
    return LtasFeatures(bands_db=db - float(_BW @ db), tilt_db_oct=tilt)


def distance(a: LtasFeatures, b: LtasFeatures) -> float:
    d = a.bands_db - b.bands_db
    d = d - float(_BW @ d)
    rms = float(np.sqrt(_BW @ d ** 2))
    return 0.5 * (rms / LTAS_SCALE_DB + abs(a.tilt_db_oct - b.tilt_db_oct) / TILT_SCALE_DB_OCT)


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    return distance(features(target, mask), features(candidate))
