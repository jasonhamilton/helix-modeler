"""Rank-1 with rule B, shared by `loss-trust-check` and the synthetic recovery (owner: tone-metrics).

Rule B (human decision, docs/DECISIONS.md 2026-10-16, "Gate 2 scoring options A and B"): a
distractor that nulls at -30 dB or lower against the true preset ON THE SAME NOTES is the same
sound. It counts as a tie, not a failure. Both rates are reported:

  * plain: the true preset ranks first only if every distractor scores strictly worse (a tie on the
    score counts against it, as before);
  * audible (rule B): the same, over the distractors whose null to the truth is above -30 dB. The
    "same sound" distractors are ties whatever they score. Admission uses this rate.

The null is the plain residual (no gain matching): 10 log10(sum (truth - y)^2 / sum truth^2), on the
two renders from the same DI clip. A distractor that only changes the level by more than about
0.3 dB is therefore audible (a level change is a real difference in a preset). Lower loss is better.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

SAME_SOUND_NULL_DB = -30.0


def null_db(truth: np.ndarray, y: np.ndarray) -> float:
    """Residual of `truth - y` relative to `truth`, in dB (more negative = closer). The shorter
    length wins. Not gain matched."""
    n = min(np.size(truth), np.size(y))
    a = np.asarray(truth, dtype=np.float64).reshape(-1)[:n]
    b = np.asarray(y, dtype=np.float64).reshape(-1)[:n]
    num = float(np.sum((a - b) ** 2))
    den = float(np.sum(a ** 2)) or 1e-30
    return float(10 * np.log10(max(num, 1e-30) / den))


def same_sound(null_dbs: Sequence[float], threshold_db: float = SAME_SOUND_NULL_DB) -> np.ndarray:
    """Boolean per distractor: True when it nulls at `threshold_db` or lower (rule B tie)."""
    return np.asarray(null_dbs, dtype=np.float64) <= threshold_db


@dataclass(frozen=True)
class Rank1:
    rank1: bool                  # plain: every distractor strictly worse than the truth
    rank1_audible: bool          # rule B: every AUDIBLE distractor strictly worse
    rank: int                    # 1-based plain rank of the truth (score ties count against it)
    rank_audible: int            # the same over audible distractors only
    n_same_sound: int            # distractors that are rule B ties
    margin: float                # (best distractor - truth) / median distractor; > 0 = first
    margin_audible: float        # the same over audible distractors (nan if none is audible)

    def as_dict(self) -> dict:
        return {"rank1": self.rank1, "rank1_audible": self.rank1_audible, "rank": self.rank,
                "rank_audible": self.rank_audible, "n_same_sound": self.n_same_sound,
                "margin": self.margin, "margin_audible": self.margin_audible}


def _margin(true_score: float, d: np.ndarray) -> float:
    if d.size == 0:
        return float("nan")
    med = float(np.median(np.abs(d))) or 1e-12
    return float((d.min() - true_score) / med)


def rank1_rule_b(true_score: float, distractor_scores: Sequence[float], distractor_null_db: Sequence[float],
                 threshold_db: float = SAME_SOUND_NULL_DB) -> Rank1:
    """Plain and rule B rank-1 of the true preset among its distractors (lower score is better).

    `distractor_null_db[k]` is distractor k's null to the truth on the same notes (`null_db`)."""
    d = np.asarray(distractor_scores, dtype=np.float64).reshape(-1)
    nd = np.asarray(distractor_null_db, dtype=np.float64).reshape(-1)
    if d.size != nd.size:
        raise ValueError(f"{d.size} distractor scores but {nd.size} nulls")
    t = float(true_score)
    tie = same_sound(nd, threshold_db)
    aud = d[~tie]
    return Rank1(rank1=bool(np.all(d > t)), rank1_audible=bool(np.all(aud > t)),
                 rank=int(1 + np.sum(d <= t)), rank_audible=int(1 + np.sum(aud <= t)),
                 n_same_sound=int(tie.sum()), margin=_margin(t, d), margin_audible=_margin(t, aud))


def rates(results: Sequence[Rank1]) -> dict:
    """Summary over items: plain and rule B rank-1 rates, mean ranks, median and p10 margins."""
    if not results:
        return {"items": 0}
    m = np.array([r.margin for r in results])
    ma = np.array([r.margin_audible for r in results])
    ma = ma[np.isfinite(ma)]
    return {"items": len(results),
            "rank1_rate": float(np.mean([r.rank1 for r in results])),
            "rank1_audible_only": float(np.mean([r.rank1_audible for r in results])),
            "mean_rank": float(np.mean([r.rank for r in results])),
            "mean_rank_audible": float(np.mean([r.rank_audible for r in results])),
            "same_sound_per_item": float(np.mean([r.n_same_sound for r in results])),
            "margin_median": float(np.median(m)), "margin_p10": float(np.percentile(m, 10)),
            "margin_audible_median": float(np.median(ma)) if ma.size else float("nan"),
            "margin_audible_p10": float(np.percentile(ma, 10)) if ma.size else float("nan")}
