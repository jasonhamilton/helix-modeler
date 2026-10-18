"""Drive seeds and ranking from a calibration table (T143).

`scripts/drive_calibrate.py` renders every eligible drive model at random knob settings through a calibration
DI and stores the `drive_features` vector of each render. For a target, `DriveTable.lookup` finds the nearest
renders of each model in the standardised feature space and returns the distance-weighted knob values (with
their spread) and the model's best distance. That gives

  seeds     per model, per knob: value and half-width, for the dispatch (a seeded model's broad draws are banded
            around the seed, `measured_seed.seeded_candidate`)
  ranking   the models nearest the target first (the dispatch shortlist)

Nothing here is a loss term. Same lookup idea as the modulation specialist (T116).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
CALIBRATION = ROOT / "cache" / "drive-specialist" / "calibration.json"
K = 5
HALF = (0.10, 0.30)         # half-width of a seed band, as a fraction of the knob range (twice the spread, clipped)
MAX_SPREAD = 0.35           # a knob whose neighbours disagree more than this (twice the spread) is left free
LEVEL_KNOBS = {"output", "volume", "level", "master", "odlevel", "fuzzlevel", "level 1", "level 2"}


@dataclass
class DriveTable:
    models: dict[str, dict]
    mean: np.ndarray
    std: np.ndarray
    names: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None, exclude_di: int | None = None) -> DriveTable | None:
        p = Path(path) if path is not None else CALIBRATION
        if not p.is_file():
            return None
        doc = json.loads(p.read_text())
        models = {}
        for m, e in doc["models"].items():
            rows = [r for r in e["rows"] if exclude_di is None or r["di"] != exclude_di]
            if len(rows) < K:
                continue
            models[m] = {"X": np.asarray([r["summary"] for r in rows], float),
                         "V": {n: np.asarray([r["values"][n] for r in rows], float) for n in rows[0]["values"]},
                         "free": {f[0]: (f[1], f[2]) for f in e["free"]}}
        allx = np.vstack([e["X"] for e in models.values()])
        mean, std = allx.mean(0), allx.std(0) + 1e-6
        return cls(models, mean, std, list(doc.get("summary_keys", [])))

    def lookup(self, vec: np.ndarray, models: list[str] | None = None, k: int = K) -> dict[str, dict]:
        v = (np.asarray(vec, float) - self.mean) / self.std
        out = {}
        for m, e in self.models.items():
            if models is not None and m not in models:
                continue
            d = np.sqrt(np.sum(((e["X"] - self.mean) / self.std - v[None, :]) ** 2, 1))
            o = np.argsort(d)[:k]
            w = 1.0 / (d[o] + 0.1)
            knobs = {}
            for n, vals in e["V"].items():
                lo, hi = e["free"][n]
                u = (vals[o] - lo) / (hi - lo) if hi > lo else np.zeros(len(o))
                mu = float(np.sum(w * u) / w.sum())
                sd = float(np.sqrt(np.sum(w * (u - mu) ** 2) / w.sum()))
                knobs[n] = (mu, sd)
            out[m] = {"fit": float(d[o[0]]), "knobs": knobs}
        return out

    def ranking(self, vec: np.ndarray, models: list[str] | None = None) -> list[str]:
        r = self.lookup(vec, models)
        return sorted(r, key=lambda m: r[m]["fit"])


def knob_seeds(entry: dict, ranges: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """{knob: (value in knob units, half-width as a fraction of the range)} from a `lookup` entry."""
    out = {}
    for n, (mu, sd) in entry["knobs"].items():
        if n not in ranges or 2 * sd > MAX_SPREAD:
            continue
        lo, hi = ranges[n]
        if hi <= lo:
            continue
        out[n] = (lo + float(np.clip(mu, 0, 1)) * (hi - lo), float(np.clip(2 * sd, *HALF)))
    return out
