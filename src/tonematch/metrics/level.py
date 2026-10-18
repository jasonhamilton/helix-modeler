"""Term `level_db` (proposed name, T144): absolute level difference in dB. KNOWN-DI MODE ONLY.

Why it exists (docs/DECISIONS.md 2026-10-17, T144). In Native, many gain-stage knobs are a pure output
gain: a preamp's Master, a drive's Output or Level, a clean boost's Boost. Two settings that differ only
in such a knob give the same waveform at two levels. `mrstft_v2` scales both signals to unit RMS (on
purpose: in the song case the stem's level is arbitrary), so it scores them exactly equal and the order
between them is floating-point noise. Gate 2's rank-1 counts a level change as audible (rule B), so
those ties are failures. This term breaks the tie: it is the only thing that differs between them.

    level_db(target, candidate) = | 20 log10( rms(candidate) / rms(target) ) |

over the common length, mask-weighted when a confidence mask is given. A silent signal is floored at
-200 dB, so the term is finite (at most `MAX_DB`).

Meant to be used as a tie-break with a small weight next to `mrstft_v2` (for example 1e-6 per dB: at
most 6e-6 for a 6 dB miss, against mrstft_v2 margins of 1e-4 and more on real tone differences), so
it orders only what mrstft_v2 cannot order.

Not content-invariant and not level-invariant by design: it is valid only when the candidate is
rendered from the same DI as the target (known-DI mode), like `mrstft_v2`. In the song case the
target's level is arbitrary and loudness belongs to the search's level guard; do not use it there.
Its invariances: the note content does not matter once the level is equal (a test checks a candidate
with the same level and other notes scores 0), and polarity and time shifts do not matter.
"""
from __future__ import annotations

import numpy as np

from tonematch.metrics._common import as_mono

NAME = "level_db"
VERSION = "1.0"
FLOOR_DB = -200.0
MAX_DB = 400.0


def rms_db(x: np.ndarray, mask: np.ndarray | None = None) -> float:
    """RMS level in dB re full scale 1.0, mask-weighted when a mask is given; floored at FLOOR_DB."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if mask is None:
        p = float(np.mean(a * a))
    else:
        m = np.clip(np.asarray(mask, dtype=np.float64).reshape(-1)[: a.size], 0.0, 1.0)
        s = float(m.sum())
        p = float(np.sum(m * a * a) / s) if s > 0 else float(np.mean(a * a))
    return max(10.0 * np.log10(p) if p > 0 else FLOOR_DB, FLOOR_DB)


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    """|level(candidate) - level(target)| in dB, over the common length (lower is better)."""
    t = as_mono(target, "target")
    c = as_mono(candidate, "candidate")
    n = min(t.size, c.size)
    m = None if mask is None else np.asarray(mask, dtype=np.float64).reshape(-1)
    if m is not None and m.size != t.size:
        raise ValueError(f"mask: expected one value per target sample ({t.size}), got {m.size}")
    m = None if m is None else m[:n]
    return float(min(abs(rms_db(c[:n], m) - rms_db(t[:n], m)), MAX_DB))
