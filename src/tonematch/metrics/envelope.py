"""Term `envelope`: envelope and attack statistics (compression, gating).

The RMS envelope (10 ms window, 5 ms hop) in dB relative to the clip's loud frames (95th
percentile), so level does not matter. Statistics:

- dynamic range: 95th minus 10th percentile of the active envelope (compression lowers it);
- floor: 5th percentile of the whole envelope, clipped at -90 dB (a gate pushes it down; separation
  bleed pushes it up, so it carries a small weight);
- per note (onsets = a rise of 6 dB within 15 ms, at least 60 ms apart): attack time to 90% of the
  peak in ms, decay slope over the 250 ms after the peak in dB/s, and sustain (level 150 ms after the
  peak, relative to the peak, in dB). Medians across notes.

Distance: weighted mean of absolute differences in scale units (`SCALES`, `WEIGHTS`). When one clip
has no detectable notes the per-note statistics count as a mismatch of 1 unit each.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tonematch.metrics._common import as_mono, frame_rms, mask_frames

NAME = "envelope"
WIN = 480
HOP = 240
FPS = 48_000 / HOP
FLOOR_DB = -90.0
# dyn, floor, attack_ms, decay_db_s, sustain_db
# Attack is quantised to the 5 ms hop and carried no signal on the proxy set, so it is weighted low.
SCALES = np.array([3.0, 6.0, 5.0, 15.0, 2.0])
WEIGHTS = np.array([1.0, 0.25, 0.25, 1.0, 1.0])


@dataclass(frozen=True)
class EnvelopeFeatures:
    vec: np.ndarray       # [dyn, floor, attack_ms, decay_db_s, sustain_db]; NaN where undefined
    n_notes: int


def envelope_db(x: np.ndarray) -> np.ndarray:
    db = 20 * np.log10(frame_rms(x, WIN, HOP))
    return np.maximum(db - np.percentile(db, 95), FLOOR_DB)


def onsets(env: np.ndarray, rise_db: float = 6.0, rise_frames: int = 3,
           min_gap_s: float = 0.06) -> np.ndarray:
    rise = np.full(env.size, -np.inf)
    rise[rise_frames:] = env[rise_frames:] - env[:-rise_frames]
    cand = np.flatnonzero(rise >= rise_db)
    out: list[int] = []
    gap = int(min_gap_s * FPS)
    for i in cand:
        if not out or i - out[-1] >= gap:
            out.append(int(i))
    return np.asarray(out, dtype=int)


def _note_stats(env: np.ndarray, ons: np.ndarray, ok: np.ndarray | None):
    att, dec, sus = [], [], []
    lin = 10 ** (env / 20)
    peak_w = int(0.06 * FPS)
    for k, o in enumerate(ons):
        nxt = int(ons[k + 1]) if k + 1 < ons.size else env.size
        lo = max(0, o - 6)
        p = lo + int(np.argmax(env[lo:min(o + peak_w, nxt, env.size)]))
        if ok is not None and not ok[p]:
            continue
        base = lin[lo:p + 1].min()
        thr = base + 0.9 * (lin[p] - base)
        first = lo + int(np.flatnonzero(lin[lo:p + 1] >= thr)[0])
        start = lo + int(np.argmin(lin[lo:p + 1]))
        att.append((first - start) * 1000.0 / FPS)
        end = min(p + int(0.25 * FPS), nxt)
        seg = env[p + 2:end]
        if seg.size >= int(0.08 * FPS):
            t = np.arange(seg.size) / FPS
            dec.append(float(np.polyfit(t, seg, 1)[0]))
        s = p + int(0.15 * FPS)
        if s < nxt:
            sus.append(float(env[s] - env[p]))
    med = [float(np.median(v)) if v else np.nan for v in (att, dec, sus)]
    return med, len(att)


def features(x: np.ndarray, mask: np.ndarray | None = None) -> EnvelopeFeatures:
    x = as_mono(x)
    env = envelope_db(x)
    mf = mask_frames(mask, x.size, WIN, HOP)
    ok = None if mf is None else mf >= 0.5
    sel = env if ok is None or not ok.any() else env[ok]
    act = sel[sel >= -50.0]
    if act.size == 0:
        act = sel
    dyn = float(np.percentile(act, 95) - np.percentile(act, 10))
    floor = float(np.percentile(sel, 5))
    (att, dec, sus), n = _note_stats(env, onsets(env), ok)
    return EnvelopeFeatures(vec=np.array([dyn, floor, att, dec, sus]), n_notes=n)


def distance(a: EnvelopeFeatures, b: EnvelopeFeatures) -> float:
    d = np.abs(a.vec - b.vec) / SCALES
    d = np.where(np.isnan(d), np.where(np.isnan(a.vec) & np.isnan(b.vec), 0.0, 1.0), d)
    return float(np.sum(WEIGHTS * d) / WEIGHTS.sum())


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    return distance(features(target, mask), features(candidate))
