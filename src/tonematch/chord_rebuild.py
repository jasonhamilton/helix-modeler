"""Chord-list dry rebuild (T205): chord names -> low-position voicings; Viterbi over Basic Pitch frame posteriors
restricted to those chords plus silence; one strike per chord change plus strong onset re-strikes.

Pure numpy/scipy; the posteriors come from scripts/transcribe_stems.py (Basic Pitch lives in its own venv).
The constants below are the human-approved defaults (the six-chord Mother run); changing them is a method decision.
"""
from __future__ import annotations

import re

import numpy as np
from scipy.signal import find_peaks

from .midi_read import MidiNote

SWITCH_PENALTY = 0.6      # Viterbi cost of changing state
SILENCE = 0.05            # score of the silence state
MIN_SEGMENT_S = 0.06      # shorter chord segments are dropped
OTHER_WEIGHT = 0.5        # score = mean(chord pitches) - OTHER_WEIGHT * max(other vocabulary pitches)
STRIKE_HEIGHT = 0.6       # onset posterior needed for a re-strike
MIN_GAP_S = 0.45          # minimum time between strikes
BASIC_PITCH_LOW_MIDI = 21  # posterior bin 0
LOW_E = 40                # roots are placed in E2..D#3 (low E string, else the A string for B..D#)
OCTAVE_ROOTS = {"G"}      # G5 also gets its octave, as in the prototype the human liked

_NAME = re.compile(r"^([A-G])([#b]?)(5|maj|min|m|)$")
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_QUALITY = {"5": (0, 7), "maj": (0, 4, 7), "": (0, 4, 7), "m": (0, 3, 7), "min": (0, 3, 7)}


def voicing(name: str) -> list[int]:
    """MIDI pitches of a chord name at the lowest playable spot. Supported: X5 (power), X / Xmaj, Xm / Xmin, with # or b."""
    m = _NAME.match(name.strip())
    if not m:
        raise ValueError(f"unknown chord name {name!r}: use a root A-G with optional # or b, then 5, maj, m or min (e.g. B5, F#5, Em)")
    letter, acc, q = m.groups()
    pc = (_PC[letter] + {"#": 1, "b": -1, "": 0}[acc]) % 12
    root = LOW_E + (pc - LOW_E) % 12
    pitches = [root + i for i in _QUALITY[q]]
    if q == "5" and f"{letter}{acc}" in OCTAVE_ROOTS:
        pitches.append(root + 12)
    return pitches


def parse_chords(spec: str | list[str]) -> dict[str, list[int]]:
    names = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if s.strip()]
    if not names:
        raise ValueError("--chords needs at least one chord name")
    return {n: voicing(n) for n in dict.fromkeys(names)}


def _cols(mat: np.ndarray, pitches) -> np.ndarray:
    return mat[:, [p - BASIC_PITCH_LOW_MIDI for p in pitches]]


def viterbi_segments(note: np.ndarray, chords: dict[str, list[int]], fps: float, switch: float = SWITCH_PENALTY,
                     silence: float = SILENCE, min_segment_s: float = MIN_SEGMENT_S):
    """[(name, start_s, end_s)] from frame posteriors (frames x 88, bin 0 = MIDI 21)."""
    names = list(chords)
    vocab = sorted({p for v in chords.values() for p in v})
    T = len(note)
    E = np.zeros((T, len(names) + 1))
    for k, c in enumerate(names):
        others = [p for p in vocab if p not in chords[c]]
        out = _cols(note, others).max(1) if others else 0.0
        E[:, k] = _cols(note, chords[c]).mean(1) - OTHER_WEIGHT * out
    E[:, -1] = silence
    dp = E[0].copy()
    bp = np.zeros((T, E.shape[1]), int)
    pen = switch * (1 - np.eye(E.shape[1]))
    for t in range(1, T):
        cand = dp[:, None] - pen
        bp[t] = cand.argmax(0)
        dp = cand.max(0) + E[t]
    st = np.zeros(T, int)
    st[-1] = dp.argmax()
    for t in range(T - 1, 0, -1):
        st[t - 1] = bp[t, st[t]]
    segs, s0 = [], 0
    for t in range(1, T + 1):
        if t == T or st[t] != st[s0]:
            if st[s0] < len(names) and (t - s0) / fps >= min_segment_s:
                segs.append((names[st[s0]], s0 / fps, t / fps))
            s0 = t
    return segs


def strikes(onset: np.ndarray, chords: dict[str, list[int]], segs, fps: float, height: float = STRIKE_HEIGHT,
            min_gap_s: float = MIN_GAP_S):
    """Notes and a timeline [(name, start, end, n_strikes)]: a strike at each segment start, plus onset peaks
    >= height at least min_gap_s after the previous strike."""
    notes, timeline = [], []
    for c, a, b in segs:
        on = _cols(onset, chords[c]).max(1)[int(a * fps): int(b * fps) + 1]
        pk, _ = find_peaks(on, height=height, distance=max(1, int(min_gap_s * fps)))
        hits = [a]
        for p in pk:
            if a + p / fps - hits[-1] >= min_gap_s:
                hits.append(a + p / fps)
        for i, h in enumerate(hits):
            e = hits[i + 1] if i + 1 < len(hits) else b
            for p in chords[c]:
                notes.append(MidiNote(p, h, e, 100))
        timeline.append((c, round(a, 2), round(b, 2), len(hits)))
    return notes, timeline


def decode(note: np.ndarray, onset: np.ndarray, fps: float, chords: dict[str, list[int]]):
    """Posteriors + chord set -> (MidiNote list, timeline)."""
    segs = viterbi_segments(note, chords, fps)
    return strikes(onset, chords, segs, fps)
