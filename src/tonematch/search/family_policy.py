"""Family policy for the hybrid search (T143, Gate 2 attempt 3).

Why. Gate 2 attempt 2 (synthetic-v2, diagnosis only) lost mod and drive items in two ways the dispatch
could see and did not use:

  1. The family detectors were right and unused at the end. Three of the eight mod misses had P(mod) of
     1.0 and a graphic or shelving EQ still won, because a gentle EQ fits a weak modulation under the
     admitted score about as well as a mis-phased modulation block does. Two drive misses were taken for
     the EQ CaliQ with P(gain) 0.97 and 0.49.
  2. The true model was not in the refine set. For 8 of the 14 mod and drive misses the true model was
     never refined: the broad screen of a free-running LFO block (random phase, random rate) is a poor
     guide, and the refine set is the top K of that screen.

What it does (all off with `Policy()`, which is the frozen method):

  prior      each candidate's score gets `tau * -ln(clip(P(family), FLOOR, 1))` added, where P(family) is the
             dispatch hint of the candidate's family (gain, mod, time, linear; none for dynamics). It is a
             category prior in the MAP sense, applied to the narrowing and to the final order only. The loss
             terms, their weights and the stored `refined` values are untouched.
  shortlist  the first `shortlist_max` models a specialist names (`Dispatch.shortlist`, e.g. the nearest
             modulation models in the calibration table) are always in the refine set, when the family's hint
             is at least `shortlist_hint`.

Nothing here is a loss term. The prior reads detector probabilities the dispatch already computed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tonematch.search.model_choice import ModelScore
from tonematch.search.specialists import FAMILY_OF, Dispatch

FLOOR = 0.02


@dataclass(frozen=True)
class Policy:
    tau: float = 0.0              # strength of the category prior (0 = off)
    shortlist_max: int = 0        # models forced into the refine set from Dispatch.shortlist (0 = off)
    shortlist_hint: float = 0.3   # ... when the hint of their family is at least this
    gate: float = 0.0             # >0: the prior acts only when some family's hint is at least this, and then penalises a
                                  # candidate by tau * ln(best hint / its family's hint) (a high hint is trustworthy,
                                  # a low one is not: it is wrong for about one gain item in five and one mod item in three)
    gate_families: tuple = ("mod", "gain")
    keep_top: int = 0             # the best `keep_top` models by raw broad score are always in the refine set
    rank_categories: tuple = ("mod",)   # categories the rank swap applies to
    rank_slack: float = 0.0       # >0: a winning shortlisted model is swapped for the specialist's best-ranked model
                                  # of its category among refined ones within this factor of its loss (0 = off)

    @property
    def active(self) -> bool:
        return self.tau > 0 or self.shortlist_max > 0 or self.rank_slack > 0


def penalty(disp: Dispatch | None, category: str, tau: float, gate: float = 0.0,
            gate_families: tuple = ("mod", "gain")) -> float:
    """tau * -ln(P(family of `category`)); 0 when there is no hint for the family."""
    if disp is None or tau <= 0:
        return 0.0
    h = disp.hints.get(FAMILY_OF.get(category, ""))
    known = [v for v in disp.hints.values() if math.isfinite(v)]
    if h is None or not math.isfinite(h):
        # a family with no detector (dynamics): what the other families leave over, so that it neither gains an
        # advantage from having no opinion nor loses one it deserves
        h = 1.0 - max(known) if known else 1.0
    if gate > 0:
        # only the families whose high hint is trustworthy gate the others: a confident time or linear hint was wrong
        # on 5 of 36 dev items (a tremolo and four amps) and cost one of them its model
        top = max((v for f, v in disp.hints.items() if f in gate_families and math.isfinite(v)), default=0.0)
        if top < gate:
            return 0.0
        return tau * max(0.0, math.log(top / min(max(h, FLOOR), 1.0)))
    return tau * -math.log(min(max(h, FLOOR), 1.0))


def adjusted(s: ModelScore, value: float, disp: Dispatch | None, pol: Policy) -> float:
    return value + penalty(disp, s.category, pol.tau, pol.gate, pol.gate_families)


def narrow(scores: list[ModelScore], k: int, disp: Dispatch | None, pol: Policy,
           rank: dict[str, int] | None = None) -> list[ModelScore]:
    """The models to refine, `k` in all: the `keep_top` raw leaders, then the specialists' shortlisted models (the most
    confident family first, `shortlist_max` each, one slot always left), then the best by prior-adjusted broad score.
    `scores` are the broad results; models without a finite screen are never chosen."""
    rank = rank or {}
    live = [s for s in scores if math.isfinite(s.screen)]
    key = lambda s: (adjusted(s, s.screen, disp, pol), rank.get(s.model, 10 ** 6))
    live.sort(key=key)
    out: list[ModelScore] = []
    # the raw leaders stay: the prior must not push out a model the screen likes best
    out.extend(sorted(live, key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))[: pol.keep_top])
    if disp is not None and pol.shortlist_max > 0:
        by = {s.model: s for s in live}
        fams: dict[str, list[str]] = {}
        for m in list(getattr(disp, "shortlist", [])):          # the specialists' lists, grouped by family, in their order
            if m in by:
                fams.setdefault(FAMILY_OF.get(by[m].category, ""), []).append(m)
        # the most confident family first, up to shortlist_max each, always leaving one slot to the adjusted order
        for fam in sorted(fams, key=lambda f: -disp.hints.get(f, 0.0)):
            if disp.hints.get(fam, 0.0) < pol.shortlist_hint:
                continue
            for m in fams[fam][: pol.shortlist_max]:
                if len(out) < k - 1 and by[m] not in out:
                    out.append(by[m])
    for s in live:
        if len(out) >= k:
            break
        if s not in out:
            out.append(s)
    return out[:k]


def final_order(scores: list[ModelScore], disp: Dispatch | None, pol: Policy,
                rank: dict[str, int] | None = None) -> tuple[list[ModelScore], list[ModelScore]]:
    """(refined models best first, the rest by prior-adjusted screen), the order `ModelChoice` reports."""
    rank = rank or {}
    refined = sorted((s for s in scores if s.refined is not None),
                     key=lambda s: adjusted(s, s.refined, disp, pol))
    refined = within_category_rank(refined, disp, pol)
    others = sorted((s for s in scores if s.refined is None),
                    key=lambda s: (adjusted(s, s.screen, disp, pol), rank.get(s.model, 10 ** 6)))
    return refined, others


def within_category_rank(refined: list[ModelScore], disp: Dispatch | None, pol: Policy) -> list[ModelScore]:
    """When the winner is a model the specialist ranked (`Dispatch.shortlist`, e.g. the nearest modulation models
    in the T116 calibration table), put in front the best-ranked refined model of the same category whose loss is
    within `pol.rank_slack` times the winner's. The specialist's phase-invariant ranking names the right
    modulation model more often than the loss of a free-running LFO does (trust set mod-trust-v1: rank-1 0.94 for
    the distance against 0.27 for the loss); the category itself is still decided by the loss and the prior."""
    if not refined or disp is None or pol.rank_slack <= 0 or not getattr(disp, "shortlist", None):
        return refined
    pos = {m: i for i, m in enumerate(disp.shortlist)}
    top = refined[0]
    if top.model not in pos or top.category not in pol.rank_categories:
        return refined
    base = max(float(top.refined), 1e-3)
    grp = [s for s in refined if s.category == top.category and s.model in pos
           and float(s.refined) <= pol.rank_slack * base]
    pick = min(grp, key=lambda s: pos[s.model])
    if pick is top:
        return refined
    return [pick] + [s for s in refined if s is not pick]
