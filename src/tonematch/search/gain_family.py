"""Gain family with a drive path (T143).

`AmpSpecialist` (T120) refines amps with Gaussian-process expected improvement but names a finalist only
when its category is amp or preamp. The pool's categories are GUI categories (drive, amp, ...), so a drive
pedal was never a finalist: Gate 2 attempt 2 (synthetic-v2, diagnosis only) sent a drive-led item to the
general halving (DM4 Heavy Distortion refined to 1.404 while the amp that beat it reached 0.454), and on two
others the true drive was ranked 1st and 2nd by the broad screen but the refiner worked on two amps. The
presence detector also calls DM4 distortions and overdrives amps (P(amp) 1.0), so which of drive and amp
the target is cannot be read off the detector.

`GainFamily` keeps everything of the amp specialist and changes `pick_finalists` only:

  * the finalists are the (up to three) best gain-family models by broad score, drive, amp or preamp alike, that
    score within `cross_ratio` (default 2.0) of the leader.

The finalists share the Gaussian-process refinement exactly as before (`refine_finalists`). Two finalists chosen by
the broad screen alone left the true amp out of the refine on a dev item whose four best models were within 40 %
of each other (a drive pedal and two amps among them), so the third place is kept.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tonematch.search.amp_specialist import AmpSpecialist
from tonematch.search.loop import BAD
from tonematch.search.measured_seed import Seed
from tonematch.search.model_choice import ModelScore

GAIN_CATEGORIES = ("drive", "amp", "preamp")


@dataclass
class GainFamily(AmpSpecialist):
    cross_ratio: float = 2.0
    max_finalists: int = 3
    table: object | None = None          # drive_specialist.DriveTable: knob seeds and a shortlist for drive models (T143)
    shortlist_on: bool = False           # offer the table's ranking of drive models to the policy's shortlist
    _last: tuple | None = None

    def _lookup(self, target, dry) -> dict:
        key = (id(target), id(dry), len(target))
        if self._last is None or self._last[0] != key:
            from tonematch.search import drive_features as DF
            self._last = (key, self.table.lookup(DF.features(target, dry)))
        return self._last[1]

    def seed(self, target, dry):
        fs = super().seed(target, dry)
        if self.table is None:
            return fs
        from tonematch.search import drive_specialist as DS
        res = self._lookup(target, dry)

        def for_model(model: str, category: str, ranges: dict) -> dict:
            if category != "drive" or model not in res:
                return {}
            return {n: Seed(v, hw, "drive.knn", v) for n, (v, hw) in DS.knob_seeds(res[model], ranges).items()}

        fs.for_model = for_model
        fs.notes.append("drive: knob seeds from the calibration table")
        return fs

    def shortlist(self, target, dry, models: list[str]) -> list[str]:
        if self.table is None or not self.shortlist_on:
            return []
        res = self._lookup(target, dry)
        return sorted((m for m in models if m in res), key=lambda m: res[m]["fit"])

    def pick_finalists(self, scores: list[ModelScore]) -> list[ModelScore]:
        # the dispatch routes here when the (prior-adjusted) leader is a gain model; a non-gain model may still lead
        # on the raw screen, so the leader is the best gain model of the list
        live = sorted((s for s in scores if math.isfinite(s.screen) and s.screen < BAD and s.category in GAIN_CATEGORIES),
                      key=lambda s: s.screen)
        if not live:
            return []
        base = max(live[0].screen, 1e-3)
        return [s for s in live if s.screen <= self.cross_ratio * base][: self.max_finalists]


def make_gain_family(drive_seeds: bool = False, drive_shortlist: bool = False, **kw) -> GainFamily:
    """`drive_seeds` loads the drive calibration table (cache/drive-specialist, `scripts/drive_calibrate.py`)."""
    if drive_seeds or drive_shortlist:
        from tonematch.search.drive_specialist import DriveTable
        kw["table"] = DriveTable.load()
        kw["shortlist_on"] = drive_shortlist
    return GainFamily(**kw)
