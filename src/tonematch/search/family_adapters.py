"""Adapters that put each family's specialist behind the dispatch plug-in (T134, Gate 2 attempt 2).

`build_specialists(families)` returns `{family: specialist}` for `build_dispatch(..., specialists=...)`. The
adapters are thin: they construct the specialist with the policy the registered decisions require and change
nothing about how it measures, seeds or scores.

    gain      amp_specialist (T120). Adopted as measured: presence probabilities as category shares, no knob
              seeds, general score, and the GP-EI `refiner` hook (Dispatch.refiner) that replaces the halving.
    mod       mod_specialist (T116). Contributes `screen_hint` and `seed` only. `score_weight=0` is passed
              explicitly, so the mod_distance family score stays off in known-DI mode whatever the module's
              default says (DECISIONS 2026-10-17, lead ruling on T116: `FAMILY_SCORE_ADMITTED = False`).
    linear    linear_specialist (T117, adopted). Screen hint, seeds and the `refiner` hook (at most 8 renders on the
              target's own DI, general halving when it cannot settle); `score` stays None (family score not admitted).
    dynamics  stub (no specialist).
    time      time_specialist (T122), built with the run's `time_score_weight`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from tonematch.metrics import linear_specialist
from tonematch.search.specialists import FAMILIES, Specialist, StubSpecialist


def make_gain(gain_drive: bool = False, drive_seeds: bool = False, drive_shortlist: bool = False, **_: Any) -> Specialist:
    if gain_drive or drive_seeds or drive_shortlist:      # T143: drives are finalists of the GP-EI refiner too
        from tonematch.search.gain_family import make_gain_family
        return make_gain_family(drive_seeds=drive_seeds, drive_shortlist=drive_shortlist)
    from tonematch.search.amp_specialist import make_specialist
    return make_specialist()


def make_mod(mod_shortlist: bool = False, **_: Any) -> Specialist:
    if mod_shortlist:   # T143: adds the pool shortlist; same hint, seeds and (off) score
        from tonematch.search.mod_family import make_mod_family
        return make_mod_family()
    from tonematch.metrics.mod_specialist import make_specialist
    return make_specialist(score_weight=0.0)      # hint and seeds only; the family score is off (T116 ruling)


def make_time(time_score_weight: float = 0.25, **_: Any) -> Specialist:
    from tonematch.search.time_specialist import TimeSpecialist
    return TimeSpecialist(score_weight=time_score_weight)


def make_dynamics(**_: Any) -> Specialist:
    return StubSpecialist("dynamics")


# The linear slot (T117 adopted). Set to None to fall back to a stub.
LINEAR_ADAPTER: Callable[..., Specialist] | None = linear_specialist.make_specialist


def make_linear(**opts: Any) -> Specialist:
    return LINEAR_ADAPTER(**opts) if LINEAR_ADAPTER is not None else StubSpecialist("linear")


ADAPTERS: dict[str, Callable[..., Specialist]] = {
    "gain": make_gain, "mod": make_mod, "linear": make_linear, "dynamics": make_dynamics, "time": make_time,
}


def build_specialists(families: Iterable[str], **opts: Any) -> dict[str, Specialist]:
    return {f: ADAPTERS[f](**opts) for f in families if f in FAMILIES}
