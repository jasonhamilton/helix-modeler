"""Term `envelope`: envelope and attack statistics (compression, gating, drive sustain). v1.

Two level envelopes in dB relative to the clip's loud frames (95th percentile), so level does not
matter: a coarse one (10 ms RMS, 5 ms hop) for level statistics and a fine one (2 ms RMS, 0.5 ms
hop) for attack and per-note shape.

Onsets come from spectral flux, not from level rises: the mean positive change, over 20 ms, of
quarter-octave band levels (80 Hz to 10 kHz, floored 60 dB under the loud level), minus a 0.4 s
running median, peak-picked above 0.3 of its 99.5th percentile, at least 60 ms apart. A level-rise
detector (v0) finds about half the notes of a clean DI and one to four in twelve seconds of a driven
tone, because saturation flattens the level; the flux sees the pitch change. Each onset is refined
on the fine envelope: the note's peak is the maximum from 20 ms before to 50 ms after it, its start
the minimum between there and the peak.

Statistics (the vector, in this order):

- `dyn_db`: 95th minus 10th percentile of the active (> -50 dB) coarse envelope;
- `median_db`: median of the active coarse envelope (how much of the time the tone sits near its
  peak level; saturation and compression raise it);
- `floor_db`: noise floor, the 5th percentile of the whole coarse envelope, clipped at -90 dB (a gate
  lowers it; hiss and separation bleed raise it). Block-minimum estimators were tried and did no
  better on the proxy;
- `attack_ms`: per note, time from the start to 50% of the rise in linear amplitude, with crossings
  interpolated between fine frames (sub-millisecond, not quantised to a hop);
- `decay_db_s`: per note, slope of a line fitted to the fine envelope from 30 ms after the peak to
  the next note or 500 ms, whichever comes first;
- `overshoot_db`: per note, the peak minus the mean level 30-60 ms after it (a compressor's
  transient spike, a picked note's initial burst);
- `sustain_db_s`: median 40 ms slope of the coarse envelope in frames within 3 dB of the loud level,
  away from onsets (-20 ms to +40 ms excluded): how flat the sustain is near full level.

A low-band (80-400 Hz) median level was tried and helped a little on the proxy, but it tracks the
register played (3-4 dB between note sets on one rhythm), so it is left out: the contract asks for
note invariance first.

Per-note statistics are medians over notes, so the notes played and their number matter little.
Distance: root mean square of the differences in noise units (`SCALES`, the standard deviation of
each statistic between a separated target and the same tone on other notes, measured on proxy seeds
0 and 1 only), times `WEIGHTS`. A statistic that one clip has and the other does not counts
`NAN_Z` noise units. The scales come from the numpy proxy, not from Helix (see docs/DECISIONS.md).

Mask: frames whose separator confidence is below 0.5 are left out of the target's level
statistics, notes whose peak lies in such a frame are skipped, and so are sustain frames.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from tonematch.metrics._common import (
    SAMPLE_RATE,
    as_mono,
    band_matrix,
    fractional_octave_bands,
    frame_rms,
    mask_frames,
    stft_power,
)

NAME = "envelope"
SR = SAMPLE_RATE
FLOOR_DB = -90.0
# coarse envelope: 10 ms RMS window, 5 ms hop
CWIN, CHOP = 480, 240
CFPS = SR / CHOP
# fine envelope: 2 ms RMS window, 0.5 ms hop
FWIN, FHOP = 96, 24
FFPS = SR / FHOP
# onset flux: STFT at the coarse hop, quarter-octave bands
NFFT = 1024
_CENTRES = fractional_octave_bands(80.0, 10_000.0, 4)
_BANDS = band_matrix(_CENTRES, NFFT, SR, 4)
FLUX_LAG = 4           # frames (20 ms); 2 gave precision 0.69 vs 0.85 against true onsets
FLUX_REL = 0.3         # peak threshold, fraction of the 99.5th percentile of the detrended flux
MIN_GAP_S = 0.06

FIELDS = ("dyn_db", "median_db", "floor_db", "attack_ms", "decay_db_s", "overshoot_db",
          "sustain_db_s")
# Noise standard deviations measured on proxy seeds 0 and 1 (80 items, separated target vs the same
# preset on other notes). NOT Helix. Re-measure on synthetic-v1.
SCALES = np.array([1.20, 0.60, 1.44, 2.92, 0.90, 0.53, 1.25])
WEIGHTS = np.ones(len(FIELDS))
NAN_Z = 2.0


@dataclass(frozen=True)
class EnvelopeFeatures:
    vec: np.ndarray       # values in FIELDS order; NaN where undefined (no usable notes)
    n_notes: int
    onsets_s: np.ndarray  # detected onset times in seconds

    def as_dict(self) -> dict[str, float]:
        return dict(zip(FIELDS, (float(v) for v in self.vec)))


def envelope_db(x: np.ndarray, win: int = CWIN, hop: int = CHOP) -> np.ndarray:
    db = 20 * np.log10(frame_rms(x, win, hop))
    return np.maximum(db - np.percentile(db, 95), FLOOR_DB)


def onset_flux(x: np.ndarray, power: np.ndarray | None = None) -> np.ndarray:
    """Per coarse frame: mean positive change of quarter-octave band levels over FLUX_LAG frames."""
    p = stft_power(x, NFFT, CHOP) if power is None else power
    lev = 10 * np.log10(p @ _BANDS.T + 1e-12)
    lev -= np.percentile(lev.max(axis=1), 95)
    lev = np.maximum(lev, -60.0)
    d = np.zeros(lev.shape[0])
    d[FLUX_LAG:] = np.maximum(lev[FLUX_LAG:] - lev[:-FLUX_LAG], 0.0).mean(axis=1)
    return d


def onsets(x: np.ndarray, power: np.ndarray | None = None) -> np.ndarray:
    """Onset positions in coarse frames (5 ms hop)."""
    f = onset_flux(x, power)
    e = f - ndimage.median_filter(f, size=int(0.4 * CFPS) | 1, mode="nearest")
    top = np.percentile(e, 99.5)
    peak = ndimage.maximum_filter1d(f, 2 * int(0.03 * CFPS) + 1)
    cand = np.flatnonzero((f == peak) & (e > FLUX_REL * top) & (e > 0))
    out: list[int] = []
    gap = int(MIN_GAP_S * CFPS)
    for c in cand:
        if not out or c - out[-1] >= gap:
            out.append(int(c))
    return np.asarray(out, dtype=int)


def _crossing(lin: np.ndarray, lo: int, hi: int, thr: float) -> float:
    """First fractional index in lin[lo:hi+1] reaching thr, linearly interpolated."""
    seg = lin[lo:hi + 1]
    k = np.flatnonzero(seg >= thr)
    if k.size == 0:
        return float(hi)
    i = int(k[0])
    if i == 0:
        return float(lo)
    a, b = seg[i - 1], seg[i]
    return lo + i - 1 + float((thr - a) / (b - a + 1e-12))


def _note_stats(fe: np.ndarray, ons: np.ndarray, ok_fine: np.ndarray | None):
    lin = 10 ** (fe / 20)
    r = FFPS / CFPS
    att, dec, ovs = [], [], []
    for k, o in enumerate(ons):
        of = int((o - FLUX_LAG) * r)
        nxt = int(ons[k + 1] * r) - int(0.01 * FFPS) if k + 1 < ons.size else fe.size
        lo = max(0, of - int(0.02 * FFPS))
        hi = min(fe.size - 1, of + int(0.05 * FFPS), nxt)
        if hi - lo < 4:
            continue
        p = lo + int(np.argmax(fe[lo:hi + 1]))
        if ok_fine is not None and not ok_fine[p]:
            continue
        st = lo + int(np.argmin(lin[lo:p + 1])) if p > lo else lo
        base, pk = lin[st], lin[p]
        att.append((_crossing(lin, st, p, base + 0.5 * (pk - base)) - st) * 1000.0 / FFPS)
        a, b = p + int(0.03 * FFPS), min(nxt, p + int(0.5 * FFPS))
        if b - a > int(0.08 * FFPS):
            t = np.arange(b - a) / FFPS
            dec.append(float(np.polyfit(t, fe[a:b], 1)[0]))
        a2, b2 = p + int(0.03 * FFPS), p + int(0.06 * FFPS)
        if b2 < nxt:
            ovs.append(float(fe[p] - np.mean(fe[a2:b2])))
    med = [float(np.median(v)) if v else np.nan for v in (att, dec, ovs)]
    return med, len(att)


def _sustain_slope(ce: np.ndarray, ons: np.ndarray, ok: np.ndarray | None) -> float:
    span = 8  # 40 ms
    keep = np.ones(ce.size, bool)
    for o in ons:
        keep[max(0, o - 4):o + 8] = False
    if ok is not None:
        keep &= ok[:ce.size]
    if ce.size <= span:
        return np.nan
    both = keep.copy()
    for s in range(1, span + 1):
        both[:ce.size - s] &= keep[s:]
    both[ce.size - span:] = False
    slope = np.zeros(ce.size)
    slope[:-span] = (ce[span:] - ce[:-span]) * CFPS / span
    mid = ce.copy()
    mid[:-span] = 0.5 * (ce[span:] + ce[:-span])
    sel = both & (mid >= -3.0) & (mid < 1.0)
    return float(np.median(slope[sel])) if sel.sum() >= 20 else np.nan


def _active_stats(env: np.ndarray, ok: np.ndarray | None) -> tuple[float, float, float]:
    sel = env if ok is None or not ok.any() else env[ok[:env.size]]
    act = sel[sel >= -50.0]
    if act.size == 0:
        act = sel
    return (float(np.percentile(act, 95) - np.percentile(act, 10)), float(np.percentile(act, 50)),
            float(np.percentile(sel, 5)))


def features(x: np.ndarray, mask: np.ndarray | None = None) -> EnvelopeFeatures:
    x = as_mono(x)
    ce = envelope_db(x)
    fe = envelope_db(x, FWIN, FHOP)
    power = stft_power(x, NFFT, CHOP)
    mc = mask_frames(mask, x.size, CWIN, CHOP)
    mf = mask_frames(mask, x.size, FWIN, FHOP)
    ok_c = None if mc is None else mc >= 0.5
    ok_f = None if mf is None else mf >= 0.5
    dyn, median, floor = _active_stats(ce, ok_c)
    ons = onsets(x, power)
    (att, dec, ovs), n = _note_stats(fe, ons, ok_f)
    sus = _sustain_slope(ce, ons, ok_c)
    vec = np.array([dyn, median, floor, att, dec, ovs, sus])
    return EnvelopeFeatures(vec=vec, n_notes=n, onsets_s=ons * CHOP / SR)


def distance(a: EnvelopeFeatures, b: EnvelopeFeatures) -> float:
    z = np.abs(a.vec - b.vec) / SCALES
    z = np.where(np.isnan(z), np.where(np.isnan(a.vec) & np.isnan(b.vec), 0.0, NAN_Z), z)
    return float(np.sqrt(np.sum(WEIGHTS * z * z) / WEIGHTS.sum()))


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    return distance(features(target, mask), features(candidate))
