"""Candidate reverb signals for the reverb-model pilot (T230, docs/notes/reverb-pilot-t230.md). Measurement only.

The pilot asks whether any signal computable from the separated stem predicts the human's ranking of reverb
candidates (Room / Plate / Hall x Mix x Decay) rendered after the drive match and the fitted EQ. Every candidate of a
song shares the same dry track, drive and EQ, so a distance changes across a song's candidates only through the
reverb. None of these is a LossKit term and none is admitted; they are compared with the human's ranks by the
pre-registered analysis in `scripts/reverb_pilot_t230.py`.

  tail_stats / tail_distance          (ii) note-tail statistics in the real note gaps (the T228 gap rule,
                                      `tonematch.search.colour.gap_tail`): decay slope and early tail level
  regressor_vector / regressor_distance
                                      (iii) the time specialist's fitted Room, Plate and Hall regressors read on the
                                      stem and on the candidate (reference = the reverb block's input, the EQ render)
  dip_stats / dip_distance            (iv) envelope dip fill: how far the quiet moments between notes sit under the
                                      loud ones while the dry is playing (reverb fills them)
  ir_rt60 / pick_decays               decay calibration: one impulse render per (model, Decay) gives the RT60;
                                      three Decay values per model are chosen at common RT60 targets
  draw_design                         the seeded per-song choice of the candidates the human hears

Signal (i), the AFx-Rep distance, is `tonematch.metrics.afxrep`. Pure numpy/scipy here.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

SR = 48_000

# ------------------------------------------------------------------------------------------ (ii) note tails

TAIL_SLOPE_SCALE_DB_S = 30.0       # 30 dB/s of decay-slope difference counts as much as
TAIL_LEVEL_SCALE_DB = 6.0          # 6 dB of early-tail-level difference


@dataclass
class TailStats:
    gaps: int
    slope_db_s: float | None        # median level slope in the gaps, after the first 100 ms (dB/s; negative = falling)
    early_db: float | None          # median level 60 to 100 ms into the gap, re the signal's loud level (dB)
    reason: str

    @property
    def measurable(self) -> bool:
        return self.gaps > 0 and self.slope_db_s is not None and self.early_db is not None


def tail_stats(x: np.ndarray, ref: np.ndarray, dry: np.ndarray) -> TailStats:
    """The T228 gap measurement of `x`: gaps are where the rebuilt dry track is silent for 250 ms or more and the
    reference render (the reverb block's input) is silent too; a gap where `x` is still within 12 dB of its loud
    level is not a real gap and is skipped."""
    from tonematch.search.colour import gap_tail

    t = gap_tail(x, ref, dry)
    return TailStats(t.gaps, t.slope_db_s, t.early_db, t.reason)


def tail_distance(stem: TailStats, cand: TailStats, slope_scale: float = TAIL_SLOPE_SCALE_DB_S,
                  level_scale: float = TAIL_LEVEL_SCALE_DB) -> float:
    """|slope difference| / slope_scale + |early level difference| / level_scale; NaN when either side has no real gap."""
    if not (stem.measurable and cand.measurable):
        return float("nan")
    return abs(stem.slope_db_s - cand.slope_db_s) / slope_scale + abs(stem.early_db - cand.early_db) / level_scale


# ------------------------------------------------------------------------------------------ (iii) regressors

REGRESSOR_MODELS = ("HD2_ReverbRoom", "HD2_ReverbPlate", "HD2_ReverbHall")
REGRESSOR_KNOBS = ("Decay", "Mix")


def regressor_vector(x: np.ndarray, ref: np.ndarray, bank: dict, models=REGRESSOR_MODELS) -> dict[str, float]:
    """Each model's fitted regressor (time specialist seed bank) read on `x` with `ref` as the block input:
    {"<model>.Decay": normalised 0..1, "<model>.Mix": ...}. Six numbers: the pattern across the three regressors
    carries the type, each pair the decay and mix as that type would explain them."""
    from tonematch.search.time_specialist import analyse

    ev = analyse(np.asarray(x, np.float32), np.asarray(ref, np.float32))
    out = {}
    for m in models:
        sm = bank[m]
        p = sm.predict(ev.probe)
        for k in REGRESSOR_KNOBS:
            out[f"{m}.{k}"] = float(p[sm.knobs.index(k)])
    return out


def regressor_scales(bank: dict, models=REGRESSOR_MODELS) -> dict[str, float]:
    """Each coordinate's leave-one-out error (normalised units): a difference is counted in units of what the
    regressor itself cannot resolve."""
    return {f"{m}.{k}": float(bank[m].loo[bank[m].knobs.index(k)]) for m in models for k in REGRESSOR_KNOBS}


def regressor_distance(vs: dict[str, float], vc: dict[str, float], scales: dict[str, float]) -> float:
    """Root-mean-square over the six coordinates of (stem - candidate) / leave-one-out error."""
    ks = sorted(scales)
    return float(math.sqrt(np.mean([((vs[k] - vc[k]) / max(scales[k], 1e-6)) ** 2 for k in ks])))


# ------------------------------------------------------------------------------------------ (iv) dip fill

DIP_BAND_HZ = (300.0, 4000.0)       # the guitar's main range: the separator's bass and missing treble stay out
DIP_PERCENTILES = (10.0, 25.0, 50.0)
DIP_REF_PERCENTILE = 95.0
DIP_FRAME = 480                     # 10 ms
DIP_FLOOR_DB = -60.0                # frame levels are floored here (digital silence must not dominate a percentile)
DIP_ACTIVE_DB = 40.0                # a frame counts when the rebuilt dry is within 40 dB of its loud level ...
DIP_ACTIVE_PAD_S = 0.10             # ... or within 100 ms of such a frame


def _frames_db(x: np.ndarray, frame: int = DIP_FRAME) -> np.ndarray:
    n = len(x) // frame
    return 10 * np.log10((np.asarray(x[: n * frame], np.float64).reshape(n, frame) ** 2).mean(1) + 1e-14)


def active_mask(dry: np.ndarray, sr: int = SR) -> np.ndarray:
    """10 ms frames where the rebuilt dry track is playing (within DIP_ACTIVE_DB of its 95th-percentile level),
    widened by DIP_ACTIVE_PAD_S each side. Long rests of the dry are left out of the dip statistics: there a render
    is digitally silent while the stem keeps its leakage floor, which says nothing about the reverb."""
    from scipy.ndimage import binary_dilation

    e = _frames_db(dry)
    m = e >= np.percentile(e, DIP_REF_PERCENTILE) - DIP_ACTIVE_DB
    pad = round(DIP_ACTIVE_PAD_S * sr / DIP_FRAME)
    return binary_dilation(m, iterations=pad) if pad > 0 else m


def dip_stats(x: np.ndarray, mask: np.ndarray | None = None, sr: int = SR) -> np.ndarray:
    """10 ms frame levels of `x` band-passed to 300 Hz to 4 kHz, re their 95th percentile, floored at -60 dB; the 10th,
    25th and 50th percentiles over the frames of `mask` (all frames if None). dB, all <= 0. Reverb raises the low
    percentiles: the dips between notes fill in. Level-invariant."""
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, DIP_BAND_HZ, btype="bandpass", fs=sr, output="sos")
    e = _frames_db(sosfiltfilt(sos, np.asarray(x, np.float64)))
    if mask is not None:
        m = np.asarray(mask, bool)[: len(e)]
        e = e[: len(m)][m]
    e = np.maximum(e - np.percentile(e, DIP_REF_PERCENTILE), DIP_FLOOR_DB)
    return np.array([np.percentile(e, p) for p in DIP_PERCENTILES])


def dip_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference of the three percentile levels (dB)."""
    return float(np.mean(np.abs(np.asarray(a) - np.asarray(b))))


# ------------------------------------------------------------------------------------------ decay calibration

def ir_rt60(y: np.ndarray, t0_s: float, sr: int = SR, band_hz: tuple[float, float] = (250.0, 4000.0),
            skip_s: float = 0.010) -> dict:
    """RT60 of an impulse render `y` (the impulse at `t0_s`): band-passed to `band_hz`, Schroeder-integrated from
    `skip_s` after the impulse; T30 when the curve reaches -35 dB, else T20. Also EDT. Seconds; NaN if neither."""
    from scipy.signal import butter, sosfiltfilt

    from tonematch.metrics.reverb import _slope_time, schroeder_db

    sos = butter(4, band_hz, btype="bandpass", fs=sr, output="sos")
    z = sosfiltfilt(sos, np.asarray(y, np.float64))[int((t0_s + skip_s) * sr):]
    curve, rng = schroeder_db(z ** 2, sr=sr, block_s=0.005)
    t30 = _slope_time(curve, -5.0, -35.0, 0.005)
    t20 = _slope_time(curve, -5.0, -25.0, 0.005)
    edt = _slope_time(curve, 0.0, -10.0, 0.005)
    rt, how = (t30, "T30") if math.isfinite(t30) else ((t20, "T20") if math.isfinite(t20) else (float("nan"), "none"))
    return {"rt60_s": float(rt), "method": how, "edt_s": float(edt), "range_db": float(rng)}


def pick_decays(calib: dict[str, list[tuple[float, float]]], n: int = 3, inset: float = 0.10) -> dict:
    """Three (n) Decay knob values per model at common RT60 targets.

    `calib` = {model: [(decay, rt60_s), ...]}. The common range is [max of the models' smallest RT60, min of their
    largest]; the targets are log-spaced across it, `inset` (a fraction of the log range) in from each end. Each
    model's Decay is found by linear interpolation of log RT60 on Decay over its monotone envelope. Returns
    {"targets_s": [...], "decays": {model: [...]}, "range_s": (lo, hi)}."""
    curves = {}
    for m, pts in calib.items():
        p = sorted((float(d), float(r)) for d, r in pts if math.isfinite(r) and r > 0)
        d = np.array([a for a, _ in p])
        r = np.maximum.accumulate(np.array([b for _, b in p]))      # monotone envelope
        curves[m] = (d, np.log(r))
    lo = max(float(np.exp(c[1].min())) for c in curves.values())
    hi = min(float(np.exp(c[1].max())) for c in curves.values())
    if not hi > lo:
        raise ValueError(f"no common RT60 range across models (lo {lo:.3f} s, hi {hi:.3f} s)")
    llo, lhi = math.log(lo), math.log(hi)
    span = lhi - llo
    tl = np.linspace(llo + inset * span, lhi - inset * span, n)
    out = {}
    for m, (d, lr) in curves.items():
        vals = []
        for t in tl:
            # first crossing of the monotone envelope
            j = int(np.searchsorted(lr, t))
            j = min(max(j, 1), len(d) - 1)
            a, b = lr[j - 1], lr[j]
            f = 0.0 if b <= a else float(np.clip((t - a) / (b - a), 0.0, 1.0))
            vals.append(round(float(d[j - 1] + f * (d[j] - d[j - 1])), 3))
        out[m] = vals
    return {"targets_s": [float(np.exp(t)) for t in tl], "decays": out, "range_s": (lo, hi)}


# ------------------------------------------------------------------------------------------ design

def admissible_sets(cells: list[tuple[str, int, int]], k: int = 4, type_counts: dict[str, int] | None = None) -> list[tuple]:
    """All k-subsets of grid cells (type, mix level, decay level) that cover every mix level and every decay level
    present in the grid and match `type_counts` exactly (e.g. {"Hall": 2, "Room": 1, "Plate": 1})."""
    mixes = sorted({c[1] for c in cells})
    decs = sorted({c[2] for c in cells})
    out = []
    for s in itertools.combinations(cells, k):
        if {c[1] for c in s} != set(mixes) or {c[2] for c in s} != set(decs):
            continue
        if type_counts is not None:
            cnt: dict[str, int] = {}
            for c in s:
                cnt[c[0]] = cnt.get(c[0], 0) + 1
            if cnt != type_counts:
                continue
        out.append(s)
    return out


def draw_design(rng: np.random.Generator, cells: list[tuple[str, int, int]], k: int = 4,
                type_counts: dict[str, int] | None = None) -> list[tuple[str, int, int]]:
    """One admissible set, uniformly at random, in a random order."""
    sets = admissible_sets(cells, k, type_counts)
    if not sets:
        raise ValueError("no admissible design")
    s = list(sets[int(rng.integers(0, len(sets)))])
    return [s[i] for i in rng.permutation(len(s))]
