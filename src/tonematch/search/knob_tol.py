"""Per-knob tolerance for the search (T178, builds on the scalar `knob_tol` of T176).

The ear cannot tell Drive steps of about 0.15 apart, Tone steps of about 0.1 (finer within 0.1 of either end,
where the knob does most of its work: Scream 808 Tone changes 1 dB over 0.2 to 0.8 and 9 dB at 10 kHz over 0.8
to 1.0, docs/notes/distortion-step3.md). `KnobTol` is a float (its value is the Drive tolerance, so every
`knob_tol > 0` test and `:g` format in the search keeps working) that also knows, per model and knob, how tight to be.

Which knob is the tone knob comes from the knob names (`TONE_NAMES`: Tone, Treble, Filter, Contour, Spectrum, Bass,
Presence, Tilt), or from a map file (`--knob-tol-map`) with, per model, any of
    {"drive_knobs": [...], "tone_knobs": [...], "drive_tol": x, "tone_tol": x, "tone_end_tol": x, "end_zone": x}
(the shape expected from T179's knob_sensitivity.json; keys not listed are ignored, so a richer file still loads).
Level-like knobs and every other knob use the Drive tolerance, as in T176.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

DRIVE_TOL = 0.15
TONE_TOL = 0.10
TONE_END_TOL = 0.05
END_ZONE = 0.10
TONE_NAMES = frozenset({"tone", "treble", "filter", "contour", "spectrum", "bass", "presence", "tilt"})
SENSITIVITY_PATHS = ("reports/local/distortion-id/tone-ends/knob_sensitivity.json", "cache/drive-maps/knob_sensitivity.json")


class KnobTol(float):
    """Drive tolerance as the float value; `.vector(...)` gives the per-knob tolerances for a stage."""

    tone: float
    tone_end: float
    end_zone: float
    per_model: dict
    model: str | None

    def __new__(cls, drive: float = DRIVE_TOL, tone: float = TONE_TOL, tone_end: float = TONE_END_TOL,
                end_zone: float = END_ZONE, per_model: dict | None = None, model: str | None = None):
        o = super().__new__(cls, max(float(drive), 0.0))
        o.tone, o.tone_end, o.end_zone = float(tone), float(tone_end), float(end_zone)
        o.per_model, o.model = dict(per_model or {}), model
        return o

    def for_model(self, model: str) -> KnobTol:
        return KnobTol(float(self), self.tone, self.tone_end, self.end_zone, self.per_model, model)

    def _entry(self) -> dict:
        return self.per_model.get(self.model, {}) if self.model else {}

    def is_tone(self, name: str) -> bool:
        e = self._entry()
        if "tone_knobs" in e:
            return name in e["tone_knobs"]
        if "drive_knobs" in e and name in e["drive_knobs"]:
            return False
        return name.lower() in TONE_NAMES

    def tolerance(self, name: str, unit_pos: float) -> float:
        """Tolerance (parameter units, taken as unit-range) of one knob at position `unit_pos` in 0..1."""
        e = self._entry()
        if not self.is_tone(name):
            return float(e.get("drive_tol", float(self)))
        t, te, z = float(e.get("tone_tol", self.tone)), float(e.get("tone_end_tol", self.tone_end)), \
            float(e.get("end_zone", self.end_zone))
        return min(t, te) if (unit_pos <= z or unit_pos >= 1.0 - z) else t

    def vector(self, space, idx, values) -> np.ndarray:
        """Tolerance of each stage knob `idx` of `space` at the current `values`; 0 on a Drive-less (tol off) object."""
        if float(self) <= 0:
            return np.zeros(len(idx))
        u = space.to_unit(np.asarray(values, float))
        return np.array([self.tolerance(space.refs[j][1], float(u[j])) for j in idx])


def load_sensitivity(path: str | Path | None = None) -> tuple[dict, str | None]:
    """The per-model map from `path`, or from the first default location that exists; ({}, None) if none."""
    cands = [Path(path)] if path else [Path(__file__).resolve().parents[3] / p for p in SENSITIVITY_PATHS]
    for p in cands:
        if p.is_file():
            raw = json.loads(p.read_text())
            raw = raw.get("models", raw) if isinstance(raw, dict) else {}
            return {m: v for m, v in raw.items() if isinstance(v, dict)}, str(p)
        if path:
            raise FileNotFoundError(f"knob tolerance map not found: {p}")
    return {}, None


def make_knob_tol(drive: float = DRIVE_TOL, tone: float | None = None, map_path: str | Path | None = None) -> KnobTol:
    pm, _ = load_sensitivity(map_path)
    return KnobTol(drive, TONE_TOL if tone is None else tone, min(TONE_END_TOL, TONE_TOL if tone is None else tone),
                   END_ZONE, pm)
