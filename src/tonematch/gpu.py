"""Compute backend detection. Stub: no hard dependency on torch or onnxruntime.

Project convention: audio is 48 kHz float32 throughout. Python 3.11+.
Order of preference: cuda, rocm, mps, directml, cpu.
"""
from __future__ import annotations

import importlib.util
import os
import sys

SAMPLE_RATE = 48_000
DTYPE = "float32"
BACKENDS = ("cuda", "rocm", "mps", "directml", "cpu")


def _torch_backend() -> str | None:
    if importlib.util.find_spec("torch") is None:
        return None
    try:
        import torch

        if torch.cuda.is_available():
            return "rocm" if getattr(torch.version, "hip", None) else "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except (ImportError, RuntimeError, OSError, AttributeError):
        return None  # broken torch install: treat as absent
    return None


def _directml_available() -> bool:
    if sys.platform != "win32" or importlib.util.find_spec("onnxruntime") is None:
        return False
    try:
        import onnxruntime as ort

        return "DmlExecutionProvider" in ort.get_available_providers()
    except (ImportError, RuntimeError, OSError, AttributeError):
        return False  # broken onnxruntime install: treat as absent


def detect_backend() -> str:
    """Return one of BACKENDS. TONEMATCH_DEVICE overrides; invalid values raise ValueError."""
    forced = os.environ.get("TONEMATCH_DEVICE")
    if forced:
        name = forced.strip().lower()
        if name not in BACKENDS:
            raise ValueError(f"TONEMATCH_DEVICE={forced!r}; expected one of {BACKENDS}")
        return name
    found = _torch_backend()
    if found:
        return found
    return "directml" if _directml_available() else "cpu"
