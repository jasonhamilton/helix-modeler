"""Drive-map paths: writers go to cache/drive-maps/, readers fall back to legacy data/ with a warning."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tonematch import drive_maps as DM

ROOT = Path(__file__).resolve().parents[1]


def test_write_path_creates_cache_folder(tmp_path):
    p = DM.write_path(tmp_path, DM.FAMILIES_NAME)
    assert p == tmp_path / "cache" / "drive-maps" / "drive-families-v1.json"
    assert p.parent.is_dir()


def test_read_prefers_cache_then_warns_on_legacy(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / DM.SENSITIVITY_NAME).write_text("{}")
    with pytest.warns(UserWarning, match="legacy"):
        assert DM.read_path(tmp_path, DM.SENSITIVITY_NAME) == tmp_path / "data" / DM.SENSITIVITY_NAME
    new = DM.write_path(tmp_path, DM.SENSITIVITY_NAME)
    new.write_text("{}")
    assert DM.read_path(tmp_path, DM.SENSITIVITY_NAME) == new
    # nothing anywhere: the cache path (so the caller's "missing" message names the right place)
    assert DM.read_path(tmp_path / "empty", DM.FAMILIES_NAME).parts[-3:-1] == ("cache", "drive-maps")


def test_writer_script_targets_cache_drive_maps():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("dtf_script", ROOT / "scripts" / "drive_tone_families.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.DATA_T == ROOT / "cache" / "drive-maps" / "knob_sensitivity.json"
    assert m.DATA_F == ROOT / "cache" / "drive-maps" / "drive-families-v1.json"
