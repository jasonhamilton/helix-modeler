"""Engine detection and selection. TONEMATCH_ENGINE=native|stomp|auto (default auto).

auto: Native searches and the Stomp verifies when both are present; whichever one is present otherwise.
The engine agents register their implementations here once the spikes land.
"""
from __future__ import annotations

import os

from .base import Engine, EngineError


def detect() -> dict[str, type[Engine]]:
    """Return the engine classes available on this machine, keyed by name."""
    found: dict[str, type[Engine]] = {}
    try:
        from .native import NativeEngine  # type: ignore[import-not-found]

        if NativeEngine.is_available():
            found["native"] = NativeEngine
    except Exception:  # noqa: BLE001, S110 - a broken engine module must not hide the other engine
        pass
    try:
        from .stomp import StompEngine  # type: ignore[import-not-found]

        if StompEngine.is_available():
            found["stomp"] = StompEngine
    except Exception:  # noqa: BLE001, S110 - a broken engine module must not hide the other engine
        pass
    return found


def select(role: str = "search") -> type[Engine]:
    """Pick an engine for a role ("search" or "verify") honouring TONEMATCH_ENGINE."""
    want = os.environ.get("TONEMATCH_ENGINE", "auto").lower()
    found = detect()
    if not found:
        raise EngineError("No engine found. Install Helix Native or connect an HX Stomp; at least one is required.")
    if want in found:
        return found[want]
    if want != "auto":
        raise EngineError(f"TONEMATCH_ENGINE={want} requested but only {sorted(found)} available.")
    if role == "verify" and "stomp" in found:
        return found["stomp"]
    return found.get("native") or found["stomp"]
