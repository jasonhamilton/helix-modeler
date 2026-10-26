"""Where the drive maps live (cache/drive-maps/), with a warned fallback to the legacy data/ location.

The maps name Line 6 model ids, so they stay under the gitignored cache/ (DECISIONS 2026-10-18; data/*.json
fails licence_check). Writers use `write_path`; readers use `read_path`.
"""
from __future__ import annotations

import warnings
from pathlib import Path

FAMILIES_NAME = "drive-families-v1.json"
SENSITIVITY_NAME = "knob_sensitivity.json"


def maps_dir(root: Path) -> Path:
    return Path(root) / "cache" / "drive-maps"


def write_path(root: Path, name: str) -> Path:
    """cache/drive-maps/<name>; the folder is created."""
    d = maps_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def read_path(root: Path, name: str) -> Path:
    """cache/drive-maps/<name> if present, else legacy data/<name> with a warning, else the cache path."""
    p = maps_dir(root) / name
    if p.is_file():
        return p
    legacy = Path(root) / "data" / name
    if legacy.is_file():
        warnings.warn(f"{legacy} is a legacy drive-map location; move it to {p}", stacklevel=2)
        return legacy
    return p
