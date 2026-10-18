"""Family dispatch: one specialist per effect family (human decision, docs/DECISIONS.md 2026-10-17).

The general hybrid search treats every pool model alike. A specialist knows one family (docs/notes/
family-specialists.md) and can supply four things; every one is optional:

    seed(target, dry)          -> FamilySeed   what the family's own measurements say about the target
                                               (knob estimates per model, through `FamilySeed.for_model`)
    score(target, dry)         -> wrap | None  a family score: `wrap(kit)` returns a loss kit that scores with
                                               the family's terms added to the general kit's (None = general)
    budget_share               -> float        relative weight of the family's models in the broad stage
    screen_hint(target, dry)   -> float        probability the target belongs to the family (NaN = no opinion)

`Dispatch` combines the enabled specialists for one item (`build_dispatch`). `choose_models_hybrid`
reads it: per-model share of the broad draws, per-model seeds (replacing the generic measured seeds
where a specialist has them) and the family score. With no specialist enabled `build_dispatch` returns
the neutral dispatch (all shares equal, no seeds, no kit) and the search is the frozen general method.

Plug-in contract. A specialist is any object with the attributes of `Specialist`. A module that
supplies one exposes `SPECIALIST` (an instance) or `make_specialist()`; `load_specialist(family)` tries the
module named in `MODULES[family]` and falls back to a `StubSpecialist` (no opinion, no seeds, no kit),
so the dispatch runs before a specialist lands and picks it up when it does.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from tonematch.search.measured_seed import Measured, Seed

FAMILIES: dict[str, tuple[str, ...]] = {
    "gain": ("drive", "amp"),
    "linear": ("cab", "eq"),
    "dynamics": ("comp",),
    "time": ("delay", "reverb"),
    "mod": ("mod",),
}
FAMILY_OF: dict[str, str] = {c: f for f, cs in FAMILIES.items() for c in cs}
MODULES: dict[str, str] = {
    "time": "tonematch.search.time_specialist",
    "mod": "tonematch.metrics.mod_specialist",
    "gain": "tonematch.search.amp_specialist",
}
FLOOR = 0.25     # a model the specialists rule out still gets this share of an even split (never zero)
MIN_DRAWS = 3    # and at least this many broad draws (anchor + two), so a wrong hint cannot silence a model
SCORE_HINT = 0.5  # the family score replaces the general one when the specialist's hint is at least this


@dataclass
class FamilySeed:
    """What a specialist measured about one target."""
    family: str
    measured: Measured = field(default_factory=Measured)
    category_probs: dict[str, float] = field(default_factory=dict)   # category -> probability (may be empty)
    for_model: Callable[[str, str, dict[str, tuple[float, float]]], dict[str, Seed]] = \
        lambda model, category, ranges: {}
    notes: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)            # free-form numbers for the report


@runtime_checkable
class Specialist(Protocol):
    family: str
    categories: tuple[str, ...]
    budget_share: float

    def screen_hint(self, target: np.ndarray, dry: np.ndarray) -> float: ...
    def seed(self, target: np.ndarray, dry: np.ndarray) -> FamilySeed: ...
    def score(self, target: np.ndarray, dry: np.ndarray) -> Callable | None: ...


class StubSpecialist:
    """A family with no specialist yet: no opinion, no seeds, the general score."""

    budget_share = 1.0

    def __init__(self, family: str):
        self.family = family
        self.categories = FAMILIES[family]

    def screen_hint(self, target, dry) -> float:
        return float("nan")

    def seed(self, target, dry) -> FamilySeed:
        return FamilySeed(self.family, notes=[f"{self.family}: stub specialist, no seeds"])

    def score(self, target, dry):
        return None


def load_specialist(family: str) -> Specialist:
    """The family's specialist module if it exists, else a stub."""
    mod = MODULES.get(family)
    if mod:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            m = None
        if m is not None:
            if hasattr(m, "make_specialist"):
                return m.make_specialist()
            if hasattr(m, "SPECIALIST"):
                return m.SPECIALIST
    return StubSpecialist(family)


@dataclass
class Dispatch:
    families: tuple[str, ...] = ()
    hints: dict[str, float] = field(default_factory=dict)            # family -> probability (NaN dropped)
    category_probs: dict[str, float] = field(default_factory=dict)
    seeds: dict[str, dict[str, Seed]] = field(default_factory=dict)  # model -> knob seeds
    wrap: Callable | None = None                                     # wrap(kit) -> family-score kit, or None
    kit_family: str | None = None
    refiner: Callable | None = None                                  # refine(engine, kit, section, by_model, scores, budget, plan, renders, prior) -> (spent, notes) | None
    refiner_family: str | None = None
    refiners: dict[str, Callable] = field(default_factory=dict)      # family -> refine; the refine stage asks the family of the leader (T134b)
    shortlist: list[str] = field(default_factory=list)               # models a specialist wants refined, best first (T143; used by family_policy)
    detail: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def model_weights(self, models: list[tuple[str, str]]) -> list[float]:
        """Share of the broad draws for each (model, category): the category's probability (the specialist's
        own where it has one, else its family's split evenly over the pool's categories of that family,
        else the probability the specialists left over, split evenly) shared by the category's models, never
        below `FLOOR` of an even split."""
        n_cat: dict[str, int] = {}
        for _, c in models:
            n_cat[c] = n_cat.get(c, 0) + 1
        prob: dict[str, float] = {c: p for c, p in self.category_probs.items() if c in n_cat}
        for fam, h in self.hints.items():
            cs = [c for c in FAMILIES[fam] if c in n_cat and c not in prob]
            for c in cs:
                prob[c] = h / max(len([x for x in FAMILIES[fam] if x in n_cat]), 1)
        unc = [c for c in n_cat if c not in prob]
        rest = max(1.0 - sum(prob.values()), 0.0)
        for c in unc:
            prob[c] = rest / len(unc)
        even = 1.0 / max(len(models), 1)
        return [max(FLOOR * even, prob[c] / n_cat[c]) for _, c in models]


    def refiner_for(self, category: str | None) -> Callable | None:
        """The refine stage for a candidate of `category`: its family's refiner (amp and drive -> gain, cab and EQ
        -> linear), else None (the general halving). A hand-set `refiner` with no `refiners` keeps the old
        single-slot behaviour."""
        if self.refiners:
            return self.refiners.get(FAMILY_OF.get(category or "", ""))
        return self.refiner


def neutral() -> Dispatch:
    return Dispatch()


def build_dispatch(families: Iterable[str], target: np.ndarray, dry: np.ndarray, models: list[tuple[str, str]],
                   *, cal: dict | None = None, ranges: dict[str, dict[str, tuple[float, float]]] | None = None,
                   specialists: dict[str, Specialist] | None = None) -> Dispatch:
    """The dispatch for one item. `families` are the enabled ones (empty = the general method);
    `models` is [(model, category)] of the pool."""
    fams = tuple(f for f in families if f in FAMILIES)
    d = Dispatch(families=fams)
    if not fams:
        return d
    for f in fams:
        sp = (specialists or {}).get(f) or load_specialist(f)
        try:
            h = float(sp.screen_hint(target, dry))
        except Exception as e:  # noqa: BLE001 - a failed specialist is no opinion, not a crashed search
            h = float("nan")
            d.notes.append(f"{f}: screen_hint failed ({type(e).__name__}: {e})")
        if math.isfinite(h):
            d.hints[f] = h
        try:
            fs = sp.seed(target, dry)
        except Exception as e:  # noqa: BLE001
            d.notes.append(f"{f}: seed failed ({type(e).__name__}: {e})")
            fs = None
        if fs is not None:
            d.notes += fs.notes
            d.detail[f] = {"measured": dict(fs.measured.q), **fs.detail}
            d.category_probs.update(fs.category_probs)
            for m, c in models:
                if FAMILY_OF.get(c) == f:
                    sd = fs.for_model(m, c, (ranges or {}).get(m, {}))
                    if sd:
                        d.seeds[m] = sd
        if hasattr(sp, "shortlist"):   # optional hook (T143): the family's own ranking of the pool's models
            try:
                d.shortlist += [m for m in sp.shortlist(target, dry, [m for m, c in models if FAMILY_OF.get(c) == f])
                                if m not in d.shortlist]
            except Exception as e:  # noqa: BLE001
                d.notes.append(f"{f}: shortlist failed ({type(e).__name__}: {e})")
        if d.wrap is None and (not math.isfinite(h) or h >= SCORE_HINT):
            try:
                k = sp.score(target, dry)
            except Exception as e:  # noqa: BLE001
                k = None
                d.notes.append(f"{f}: score failed ({type(e).__name__}: {e})")
            if k is not None:
                d.wrap, d.kit_family = k, f
        if f not in d.refiners and hasattr(sp, "refiner"):   # optional hook (T120): the family's own refine stage
            try:
                r = sp.refiner(target, dry)
            except Exception as e:  # noqa: BLE001
                r = None
                d.notes.append(f"{f}: refiner failed ({type(e).__name__}: {e})")
            if r is not None:
                d.refiners[f] = r
                if d.refiner is None:
                    d.refiner, d.refiner_family = r, f
    d.notes.append("dispatch: families " + ", ".join(fams) + "; hints "
                   + ", ".join(f"{f} {v:.2f}" for f, v in d.hints.items())
                   + (f"; family score from {d.kit_family}" if d.wrap is not None else ""))
    return d


def draw_counts(models: list[tuple[str, str]], total: int, disp: Dispatch) -> list[int]:
    """Broad draws per model in the given order, proportional to the dispatch weights, at least
    `MIN_DRAWS` each while the total allows, summing to `total`."""
    n = len(models)
    if n == 0 or total <= 0:
        return [0] * n
    w = np.array(disp.model_weights(models), float)
    floor = min(MIN_DRAWS, total // n)
    rest = total - floor * n
    share = w / w.sum() * rest
    cnt = np.floor(share).astype(int)
    for i in np.argsort(-(share - cnt))[: rest - int(cnt.sum())]:
        cnt[i] += 1
    return [int(floor + c) for c in cnt]
