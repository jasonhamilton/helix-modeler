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
0.3 dB is audible under the plain null. Lower loss is better.

Rule B extended (human ruling, docs/DECISIONS.md 2026-10-18, "rule B extended"; T146): a distractor
is ALSO a tie when it nulls at -30 dB or lower after the optimal broadband gain match AND that gain
lies within the capture's own level-jitter range. The aligned capture (`aligned_capture`) applies a
random gain of up to +-`gain_db`, so a distractor that is the truth at another level within that
range cannot be ranked behind the truth by any score. The range is read from the capture's own
setting (`capture_gain_range_db`, the `gain_db` the capture was made with), never from a separate
constant; a target made without level jitter (clean, sep) has range 0, so only the direct null
counts there, exactly as before. Callers that do not pass the gain-matched null get the old rule.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

SAME_SOUND_NULL_DB = -30.0

# The aligned capture's setting (helix-trust-v2 `aligned`, Ruling 2 of 2026-10-16): residual
# fractional delay, gain jitter (uniform +-gain_db) and pink noise. The extended rule B reads its
# level range from the `gain_db` here or from the setting a set's manifest records.
ALIGNED_CAPTURE = {"frac_delay_samples": 0.5, "gain_db": 3.0, "noise_db_re_rms": -60.0}


def aligned_capture(x: np.ndarray, rng: np.random.Generator,
                    frac_delay_samples: float = ALIGNED_CAPTURE["frac_delay_samples"],
                    gain_db: float = ALIGNED_CAPTURE["gain_db"],
                    noise_db_re_rms: float = ALIGNED_CAPTURE["noise_db_re_rms"]) -> np.ndarray:
    """The true render as an aligned capture gives it (helix-trust-v2 `aligned`): a residual
    fractional delay uniform in +-frac_delay_samples (after integer alignment), a gain uniform in
    +-gain_db, and pink noise noise_db_re_rms below the render's RMS."""
    x = np.asarray(x, dtype=np.float64)
    d = float(rng.uniform(-frac_delay_samples, frac_delay_samples))
    g = float(rng.uniform(-gain_db, gain_db))
    pad = 4096
    xp = np.pad(x, (pad, pad))
    f = np.fft.rfftfreq(xp.size)
    y = np.fft.irfft(np.fft.rfft(xp) * np.exp(-2j * np.pi * f * d), n=xp.size)[pad:pad + x.size]
    w = np.fft.rfft(rng.standard_normal(x.size))
    fw = np.fft.rfftfreq(x.size, 1 / 48_000)
    w[1:] /= np.sqrt(fw[1:] / fw[1])
    w[0] = 0
    pink = np.fft.irfft(w, n=x.size)
    rms = float(np.sqrt(np.mean(x ** 2))) or 1e-12
    pink *= rms * 10 ** (noise_db_re_rms / 20) / (float(np.sqrt(np.mean(pink ** 2))) or 1e-12)
    return (10 ** (g / 20) * y + pink).astype(np.float32)


def capture_gain_range_db(aligned: dict | None = None) -> float:
    """The level range of the extended rule B: the `gain_db` jitter of the aligned capture with this
    setting (None = the default setting `ALIGNED_CAPTURE`)."""
    return float({**ALIGNED_CAPTURE, **(aligned or {})}["gain_db"])


def null_db(truth: np.ndarray, y: np.ndarray) -> float:
    """Residual of `truth - y` relative to `truth`, in dB (more negative = closer). The shorter
    length wins. Not gain matched."""
    n = min(np.size(truth), np.size(y))
    a = np.asarray(truth, dtype=np.float64).reshape(-1)[:n]
    b = np.asarray(y, dtype=np.float64).reshape(-1)[:n]
    num = float(np.sum((a - b) ** 2))
    den = float(np.sum(a ** 2)) or 1e-30
    return float(10 * np.log10(max(num, 1e-30) / den))


def gain_match(truth: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """(gain-matched null in dB, gain in dB) of `y` against `truth` after the optimal broadband gain.

    The gain is g = <truth, y> / <truth, truth>, so y ~ g * truth: +2 dB means the distractor is the
    truth 2 dB louder. The null is the residual of the least-squares fit c * y of the truth, relative
    to the truth (10 log10 (1 - rho^2), the same whichever way round the gain is fitted). A zero or
    negative gain (silence, polarity flip) is not a level match: gain -inf, null 0 dB."""
    n = min(np.size(truth), np.size(y))
    a = np.asarray(truth, dtype=np.float64).reshape(-1)[:n]
    b = np.asarray(y, dtype=np.float64).reshape(-1)[:n]
    aa, bb, ab = float(np.dot(a, a)), float(np.dot(b, b)), float(np.dot(a, b))
    if aa <= 0 or bb <= 0 or ab <= 0:
        return 0.0, float("-inf")
    resid = max(aa - ab * ab / bb, 0.0)
    return float(10 * np.log10(max(resid, 1e-30) / aa)), float(20 * np.log10(ab / aa))


def null_fields(truth: np.ndarray, y: np.ndarray) -> dict:
    """Everything the extended rule B needs about one distractor: direct null, gain-matched null and
    its gain (dB). Store these per distractor; `rank1_rule_b` takes the three lists."""
    gm, g = gain_match(truth, y)
    return {"null_db": null_db(truth, y), "gm_null_db": gm, "gm_gain_db": g}


def same_sound(null_dbs: Sequence[float], threshold_db: float = SAME_SOUND_NULL_DB,
               gm_null_dbs: Sequence[float] | None = None, gm_gain_dbs: Sequence[float] | None = None,
               level_range_db: float = 0.0) -> np.ndarray:
    """Boolean per distractor: True when it is a rule B tie.

    Direct: it nulls at `threshold_db` or lower (unchanged). Extended (when the gain-matched nulls
    and gains are given): it nulls at `threshold_db` or lower after the gain match, with |gain| at
    most `level_range_db` (the capture's jitter, `capture_gain_range_db`)."""
    tie = np.asarray(null_dbs, dtype=np.float64) <= threshold_db
    if gm_null_dbs is None or gm_gain_dbs is None or level_range_db <= 0:
        return tie
    gm = np.asarray(gm_null_dbs, dtype=np.float64).reshape(-1)
    g = np.asarray(gm_gain_dbs, dtype=np.float64).reshape(-1)
    if gm.size != tie.size or g.size != tie.size:
        raise ValueError(f"{tie.size} nulls but {gm.size} gain-matched nulls and {g.size} gains")
    return tie | ((gm <= threshold_db) & (np.abs(g) <= level_range_db + 1e-9))


@dataclass(frozen=True)
class Rank1:
    rank1: bool                  # plain: every distractor strictly worse than the truth
    rank1_audible: bool          # rule B: every AUDIBLE distractor strictly worse
    rank: int                    # 1-based plain rank of the truth (score ties count against it)
    rank_audible: int            # the same over audible distractors only
    n_same_sound: int            # distractors that are rule B ties (direct or level-only)
    margin: float                # (best distractor - truth) / median distractor; > 0 = first
    margin_audible: float        # the same over audible distractors (nan if none is audible)
    n_level_tie: int = 0         # of n_same_sound, the ties only the extended rule makes (level-only)

    def as_dict(self) -> dict:
        return {"rank1": self.rank1, "rank1_audible": self.rank1_audible, "rank": self.rank,
                "rank_audible": self.rank_audible, "n_same_sound": self.n_same_sound,
                "n_level_tie": self.n_level_tie,
                "margin": self.margin, "margin_audible": self.margin_audible}


def _margin(true_score: float, d: np.ndarray) -> float:
    if d.size == 0:
        return float("nan")
    med = float(np.median(np.abs(d))) or 1e-12
    return float((d.min() - true_score) / med)


def rank1_rule_b(true_score: float, distractor_scores: Sequence[float], distractor_null_db: Sequence[float],
                 threshold_db: float = SAME_SOUND_NULL_DB, *,
                 gm_null_db: Sequence[float] | None = None, gm_gain_db: Sequence[float] | None = None,
                 level_range_db: float = 0.0) -> Rank1:
    """Plain and rule B rank-1 of the true preset among its distractors (lower score is better).

    `distractor_null_db[k]` is distractor k's null to the truth on the same notes (`null_db`).
    Extended rule B: pass `gm_null_db` and `gm_gain_db` (from `gain_match` / `null_fields`) and the
    capture's `level_range_db` (`capture_gain_range_db(setting)`); without them the rule is the
    direct null alone, as before."""
    d = np.asarray(distractor_scores, dtype=np.float64).reshape(-1)
    nd = np.asarray(distractor_null_db, dtype=np.float64).reshape(-1)
    if d.size != nd.size:
        raise ValueError(f"{d.size} distractor scores but {nd.size} nulls")
    t = float(true_score)
    direct = same_sound(nd, threshold_db)
    tie = same_sound(nd, threshold_db, gm_null_db, gm_gain_db, level_range_db)
    aud = d[~tie]
    return Rank1(rank1=bool(np.all(d > t)), rank1_audible=bool(np.all(aud > t)),
                 rank=int(1 + np.sum(d <= t)), rank_audible=int(1 + np.sum(aud <= t)),
                 n_same_sound=int(tie.sum()), margin=_margin(t, d), margin_audible=_margin(t, aud),
                 n_level_tie=int((tie & ~direct).sum()))


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
            "level_tie_per_item": float(np.mean([r.n_level_tie for r in results])),
            "margin_median": float(np.median(m)), "margin_p10": float(np.percentile(m, 10)),
            "margin_audible_median": float(np.median(ma)) if ma.size else float("nan"),
            "margin_audible_p10": float(np.percentile(ma, 10)) if ma.size else float("nan")}
