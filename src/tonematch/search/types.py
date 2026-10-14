"""Data types for search. `Section` and `RankedPreset` are proposals for the lead (no contract names them yet)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from tonematch.engines.base import ParamRef


class ScoreLike(Protocol):
    total: float
    terms: dict
    measured: dict


class LossKitLike(Protocol):
    """contracts/loss-kit.md: LossKit.score(target, candidate, *, stems, mask, weights) -> Score."""

    def score(self, target: np.ndarray, candidate: np.ndarray, *, stems: Any = None, mask: Any = None,
              weights: dict | None = None) -> ScoreLike: ...


@dataclass
class Section:
    song: str
    name: str
    target: np.ndarray            # isolated guitar for the section, 48 kHz mono
    dry: np.ndarray               # DI the candidate is rendered from, same length
    stems: Any = None
    mask: Any = None


@dataclass
class Budget:
    max_renders: int = 200        # hard cap on engine.render calls (all stages)
    wall_s: float | None = None   # optional wall-clock cap; planning divides by cost()
    clip_s: float = 10.0          # full excerpt length (loss terms want >= 10 s); actual = min(this, section)
    short_s: float = 3.0          # successive-halving first rung (Stomp)
    halving_eta: int = 3          # keep 1/eta of each rung
    seed: int = 0
    top_k_final: int = 3          # candidates re-scored at the final check
    weights: dict | str | None = "provisional"  # loss weights; "provisional" = metrics.PROVISIONAL_WEIGHTS
    admitted: bool = False        # True only once loss-trust-check admits the weighted terms


@dataclass
class Plan:
    engine: str
    max_renders: int
    popsize: int
    halving: bool
    short_frac: float             # cost of a short render relative to a full one
    parallelism: int
    seconds_per_render: float
    load_penalty_s: float
    load_penalty_assumed: bool    # True when cost() returned NaN
    est_wall_s: float
    notes: list[str] = field(default_factory=list)


@dataclass
class RankedPreset:
    preset: Any
    score: float
    terms: dict
    chain: tuple
    values: dict[ParamRef, float]
    engine: str
    renders: int
    wall_s: float
    stages: list[dict] = field(default_factory=list)
    final_checked: bool = False
    clip_s: float = 0.0           # seconds of audio each score used
    loss_label: str = "unadmitted loss"
    notes: list[str] = field(default_factory=list)
