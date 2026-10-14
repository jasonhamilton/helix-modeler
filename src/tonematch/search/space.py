"""Parameter space: which (slot, param) are free, bounds, grid quantisation and the Gaussian prior."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tonematch.engines.base import ParamRef

# Stage order from the plan: gain and tone shaping first, then time-based effects.
STAGE_ORDER = (("comp", "drive", "amp", "cab", "eq"), ("mod", "delay", "reverb"))


@dataclass
class Prior:
    """Independent Gaussian over normalised parameter values. MAP: loss + weight * mean z^2."""
    mean: dict[ParamRef, float] = field(default_factory=dict)
    std: dict[ParamRef, float] = field(default_factory=dict)
    weight: float = 0.0

    def penalty(self, values: dict[ParamRef, float]) -> float:
        zs = [((v - self.mean[r]) / max(self.std.get(r, 0.3), 1e-3)) ** 2
              for r, v in values.items() if r in self.mean]
        return self.weight * float(np.mean(zs)) if zs else 0.0


def fit_prior(presets, weight: float = 0.01, min_std: float = 0.05) -> Prior:
    """Fit mean and std per (slot, param) over a library of presets sharing the template."""
    acc: dict[ParamRef, list[float]] = {}
    for p in presets:
        for i, s in enumerate(p.slots):
            for k, v in (s.params or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    acc.setdefault((i, k), []).append(float(v))
    return Prior({r: float(np.mean(v)) for r, v in acc.items()},
                 {r: max(float(np.std(v)), min_std) for r, v in acc.items()}, weight)


@dataclass
class ParamSpace:
    refs: list[ParamRef]
    start: np.ndarray                     # initial values in parameter units
    lo: np.ndarray
    hi: np.ndarray
    categories: list[str]                 # category of each ref's slot
    steps: int | dict | None = None       # engine's param_steps (int, per-param dict, or None)

    @classmethod
    def from_preset(cls, preset, free: list[ParamRef] | None = None,
                    bounds: dict[ParamRef, tuple[float, float]] | None = None, steps=None) -> ParamSpace:
        bounds = bounds or {}
        refs = free
        if refs is None:
            refs = [(i, k) for i, s in enumerate(preset.slots) for k, v in (s.params or {}).items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)]
        start = np.array([float(preset.slots[i].params.get(k, 0.5)) for i, k in refs])
        lo = np.array([bounds.get(r, (0.0, 1.0))[0] for r in refs])
        hi = np.array([bounds.get(r, (0.0, 1.0))[1] for r in refs])
        return cls(list(refs), start, lo, hi, [preset.slots[i].category for i, _ in refs], steps)

    @property
    def n(self) -> int:
        return len(self.refs)

    def to_unit(self, v):
        return np.clip((np.asarray(v, float) - self.lo) / np.maximum(self.hi - self.lo, 1e-12), 0, 1)

    def _steps_for(self, ref: ParamRef):
        s = self.steps
        return s.get(ref) if isinstance(s, dict) else s

    def from_unit(self, u) -> np.ndarray:
        """Unit cube to parameter units, snapped to the engine's step grid."""
        v = self.lo + np.clip(u, 0, 1) * (self.hi - self.lo)
        out = v.copy()
        for j, r in enumerate(self.refs):
            st = self._steps_for(r)
            if st and st > 1:
                g = (v[j] - self.lo[j]) / max(self.hi[j] - self.lo[j], 1e-12)
                out[j] = self.lo[j] + np.round(g * (st - 1)) / (st - 1) * (self.hi[j] - self.lo[j])
        return out

    def as_changes(self, values, idx=None) -> dict[ParamRef, float]:
        idx = range(self.n) if idx is None else idx
        return {self.refs[j]: float(values[j]) for j in idx}

    def stages(self) -> list[list[int]]:
        """Indices per refinement stage: gain/EQ group, then time-based group, then anything else."""
        seen, out = set(), []
        for cats in STAGE_ORDER:
            idx = [j for j, c in enumerate(self.categories) if c in cats]
            seen.update(idx)
            if idx:
                out.append(idx)
        rest = [j for j in range(self.n) if j not in seen]
        if rest:
            out.append(rest)
        return out
