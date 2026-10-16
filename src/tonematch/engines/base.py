"""Engine interface, v0. The spec is contracts/engine-interface.md; this is its code form.

Implemented by tonematch.engines.native (Helix Native via a plugin host) and
tonematch.engines.stomp (the HX Stomp over USB). Consumed by search, benchmark, learning.
Steps 1 to 3 and 5 to 7 of the pipeline never know which engine is running.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

SAMPLE_RATE = 48_000

# 48 kHz float32 mono, shape (n,)
Audio = np.ndarray

# (slot index 0..7, parameter name from the catalog)
ParamRef = tuple[int, str]


class EngineError(RuntimeError):
    """The engine could not render, loaded an invalid preset, or returned silence or NaN."""


class UnsupportedModelChange(EngineError):
    """This engine cannot change models through load() on this route (e.g. Native via automation slots)."""


@dataclass(frozen=True)
class Cost:
    seconds_per_render: float  # measured on a 2 s clip
    parallelism: int           # renders that can run at once
    load_penalty_s: float      # extra settling after a model change


@dataclass(frozen=True)
class Capabilities:
    engine: str                     # "native" or "stomp"
    firmware_version: str           # e.g. "3.80"
    models: tuple[str, ...]         # catalog ids this engine can run
    snapshots: int = 3
    extras: dict = field(default_factory=dict)


@runtime_checkable
class Engine(Protocol):
    def load(self, preset: Preset) -> None: ...
    def set_params(self, changes: dict[ParamRef, float]) -> None: ...
    def render(self, dry: Audio) -> Audio: ...
    def cost(self) -> Cost: ...
    def capabilities(self) -> Capabilities: ...
    def close(self) -> None: ...


def serial_render_many(engine: Engine, dry: Audio, variants: list) -> list[Audio]:
    """The serial form of ``render_many`` (amendment 2026-10-16): for each variant, ``load`` it
    if it is a ``Preset``, else ``set_params`` it on the currently loaded preset (so consecutive
    dicts accumulate, exactly as calling the engine by hand); then ``render``. Results are in
    ``variants`` order; any ``EngineError`` (``UnsupportedModelChange`` included) is raised."""
    out: list[Audio] = []
    for v in variants:
        if isinstance(v, dict):
            engine.set_params(v)
        else:
            engine.load(v)
        out.append(engine.render(dry))
    return out


def render_many(engine: Engine, dry: Audio, variants: list) -> list[Audio]:
    """Render ``dry`` through every variant, in order (contracts/engine-interface.md, amendment
    2026-10-16). Callers always go through this helper: it uses the engine's own
    ``render_many`` when it has one (Native: the render pool; Stomp: two-path passes), else
    ``serial_render_many``. The engine's loaded state afterwards is unspecified: ``load`` again
    before relying on it."""
    own = getattr(type(engine), "render_many", None)
    if own is None:
        return serial_render_many(engine, dry, list(variants))
    return engine.render_many(dry, list(variants))


# Preset lives in tonematch.preset (owned by the preset agent, see contracts/preset-schema.md).
# It is referenced by name here to keep the engine package free of a hard import at type-check time.
try:  # pragma: no cover
    from tonematch.preset import Preset
except Exception:  # pragma: no cover  # noqa: BLE001 - any failure in the preset module must not break the interface
    Preset = object  # type: ignore[assignment,misc]
