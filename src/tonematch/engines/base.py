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


# Preset lives in tonematch.preset (owned by the preset agent, see contracts/preset-schema.md).
# It is referenced by name here to keep the engine package free of a hard import at type-check time.
try:  # pragma: no cover
    from tonematch.preset import Preset
except Exception:  # pragma: no cover  # noqa: BLE001 - any failure in the preset module must not break the interface
    Preset = object  # type: ignore[assignment,misc]
