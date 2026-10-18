"""Modulation specialist with a pool shortlist (T143).

The T116 specialist (tone-metrics) keeps a calibration table of 37 modulation models rendered at random
knobs and ranks them by distance to the target's phase-invariant summary. The dispatch used its hint and its
knob seeds but not that ranking: on synthetic-v2 (diagnosis only) the true modulation model was never
refined on 8 of 14 mod and drive misses, because the broad screen ranks a free-running LFO block by how well
one random phase happens to match.

`ModFamily` subclasses `ModSpecialist` and adds `shortlist(target, dry, models)`: the pool's modulation
models, nearest first by the same distance `seed()` uses. The dispatch passes it on (`Dispatch.shortlist`);
`family_policy.narrow` forces the first few into the refine set. The score is untouched (`score_weight` 0:
the mod family score is not admitted; a mod score term is a proposal to tone-metrics, DECISIONS T143).
"""
from __future__ import annotations

import numpy as np

from tonematch.metrics import mod_specialist as MS


class ModFamily(MS.ModSpecialist):
    def shortlist(self, target: np.ndarray, dry: np.ndarray, models: list[str]) -> list[str]:
        cal = self._cal
        if not cal:
            return []
        f, a = self.evidence(target, dry)
        v = MS.summary_vector(a, f)
        fits: dict[str, float] = {}
        for m in models:
            e = cal["models"].get(m)
            if e is None:
                continue
            X = e["_X"]
            if X.shape[1] != v.size:
                continue
            fits[m] = float(np.sqrt(np.sum((X - v[None, :]) ** 2, 1)).min())
        return sorted(fits, key=fits.get)


def make_mod_family(**kw) -> ModFamily:
    kw.setdefault("score_weight", 0.0)
    return ModFamily(**kw)
