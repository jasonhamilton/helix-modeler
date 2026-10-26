"""Note-list helpers for tuning a transcriber (T203): post-filters and note F-measure. Pure numpy.

A note is a tuple (onset_s, offset_s, pitch_midi, amplitude). Nothing here imports basic-pitch; that tool lives in
its own venv (cache/venvs/basic-pitch) and is called only by scripts/transcribe_stems.py.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

Note = tuple  # (onset, offset, pitch, amp)


def shift(notes: Sequence[Note], dt: float) -> list[Note]:
    return [(n[0] + dt, n[1] + dt, n[2], *n[3:]) for n in notes]


def crop(notes: Sequence[Note], lo: float, hi: float) -> list[Note]:
    """Notes whose onset lies in [lo, hi)."""
    return [n for n in notes if lo <= n[0] < hi]


def filter_notes(notes: Sequence[Note], min_pitch: int | None = None, max_pitch: int | None = None,
                 rel_amp: float | None = None, neighbour_s: float = 0.5, restrike_gap_s: float | None = None) -> list[Note]:
    """Post-filters. Order: pitch range, quiet-neighbour drop, re-strike merge.

    rel_amp: drop a note whose amplitude is below rel_amp * (median amplitude of the other notes whose onset is within
        neighbour_s). Notes with no neighbours are kept.
    restrike_gap_s: merge two notes of the same pitch when the gap between the first's end and the second's onset is
        at most this (a split sustain becomes one note; the louder amplitude is kept).
    """
    out = [n for n in notes if (min_pitch is None or n[2] >= min_pitch) and (max_pitch is None or n[2] <= max_pitch)]
    if rel_amp is not None and out:
        on = np.array([n[0] for n in out])
        am = np.array([n[3] for n in out])
        keep = []
        for i, n in enumerate(out):
            m = (np.abs(on - on[i]) <= neighbour_s)
            m[i] = False
            keep.append(not m.any() or am[i] >= rel_amp * float(np.median(am[m])))
        out = [n for n, k in zip(out, keep) if k]
    if restrike_gap_s is not None and out:
        out = sorted(out, key=lambda n: (n[2], n[0]))
        merged = [out[0]]
        for n in out[1:]:
            p = merged[-1]
            if n[2] == p[2] and n[0] - p[1] <= restrike_gap_s:
                merged[-1] = (p[0], max(p[1], n[1]), p[2], max(p[3], n[3]))
            else:
                merged.append(n)
        out = sorted(merged, key=lambda n: (n[0], n[2]))
    return out


def f_measure(ref: Sequence[Note], est: Sequence[Note], onset_tol: float = 0.05, octave_tolerant: bool = False) -> dict:
    """Note-onset F-measure: a reference note is matched by an estimated note of the same pitch (same pitch class when
    octave_tolerant) whose onset is within onset_tol; one-to-one, nearest onset first. Offsets are not scored."""
    r = sorted(ref, key=lambda n: n[0])
    e = sorted(est, key=lambda n: n[0])
    key = (lambda p: int(p) % 12) if octave_tolerant else (lambda p: int(p))
    cands = []
    eo = np.array([n[0] for n in e]) if e else np.zeros(0)
    for i, n in enumerate(r):
        lo, hi = np.searchsorted(eo, n[0] - onset_tol), np.searchsorted(eo, n[0] + onset_tol, side="right")
        for j in range(lo, hi):
            if key(e[j][2]) == key(n[2]):
                cands.append((abs(e[j][0] - n[0]), i, j))
    cands.sort()
    ur, ue, tp = set(), set(), 0
    for _, i, j in cands:
        if i not in ur and j not in ue:
            ur.add(i)
            ue.add(j)
            tp += 1
    p = tp / len(e) if e else 0.0
    rc = tp / len(r) if r else 0.0
    f = 2 * p * rc / (p + rc) if p + rc else 0.0
    return {"precision": p, "recall": rc, "f": f, "tp": tp, "n_ref": len(r), "n_est": len(e)}


def pooled(scores: Sequence[dict]) -> dict:
    """Combine per-song scores into one by summing counts (so a long song does not hide a short one's errors: each song
    is weighted by its own note counts, which is the usual micro-average)."""
    tp = sum(s["tp"] for s in scores)
    ne = sum(s["n_est"] for s in scores)
    nr = sum(s["n_ref"] for s in scores)
    p = tp / ne if ne else 0.0
    r = tp / nr if nr else 0.0
    return {"precision": p, "recall": r, "f": 2 * p * r / (p + r) if p + r else 0.0, "tp": tp, "n_ref": nr, "n_est": ne}


# --------------------------------------------------------------------------- song-fact diagnostics (Mother, T203)

def pc_histogram(notes: Sequence[Note]) -> dict[int, int]:
    h: dict[int, int] = {}
    for n in notes:
        h[int(n[2]) % 12] = h.get(int(n[2]) % 12, 0) + 1
    return dict(sorted(h.items()))


def flag_out_of_scale(notes: Sequence[Note], allowed_pcs: Sequence[int]) -> list[bool]:
    """True for each note whose pitch class is NOT in allowed_pcs. Flags only; nothing is removed."""
    ok = {int(p) % 12 for p in allowed_pcs}
    return [int(n[2]) % 12 not in ok for n in notes]
