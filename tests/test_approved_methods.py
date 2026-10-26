"""A method flag cannot become a default without an entry in data/approved-methods.json (audit 2026-10-23, rec. 8)."""
from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
AM = json.loads((ROOT / "data" / "approved-methods.json").read_text())
PINS = AM["pins"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_entries_carry_a_date_and_a_quote():
    ids = set()
    for m in AM["methods"]:
        assert re.fullmatch(r"20\d\d-\d\d-\d\d", m["decided"]) and m["quote"].strip() and m["decisions_entry"]
        assert m["status"] in {"approved", "interim", "provisional"}
        assert m["id"] not in ids
        ids.add(m["id"])
    assert {"afxrep-shortlist", "afxrep-final-ranking", "finetune-option", "rebuild-level-default", "provisional-loss",
            "drive-families", "fixed-slot-order"} <= ids
    assert "HD2_" not in json.dumps(AM) and "Line6" not in json.dumps(AM)


def test_song_match_defaults_match_the_file():
    SM = _load("song_match")
    p = PINS["song_match"]
    assert SM.SEARCH_FLAGS == p["search_flags"]
    assert SM.DEFAULT_LEVEL_DB == p["default_level_db"]
    assert SM.FINETUNE_DEFAULT is p["finetune_default"]
    assert SM.FINETUNE_LOCK_LEVEL_KNOBS is p["finetune_lock_level_knobs"]
    a = SM.parse(["--song", "x"])
    assert bool(a.finetune) is p["finetune_default"]
    assert not hasattr(a, "no_finetune") and not hasattr(a, "lock_level_knobs")


def test_match_block_defaults_match_the_file():
    MB = _load("match_block")
    p = PINS["match_block"]
    a = MB.parse(["--target-stems", "s", "--target-window", "0:10", "--di", "x.wav", "--out", "o"])
    assert a.final_rank == p["final_rank_argparse_default"] and a.families == p["families_argparse_default"]
    a.shortlist = "afxrep"
    assert MB.resolve_final_rank(a) == p["final_rank_with_afxrep_shortlist"]


def test_shortlist_families_default_matches_the_file():
    from tonematch.search import shortlist as SL
    assert SL.FAMILIES_DEFAULT_ON is PINS["shortlist"]["families_default_on"]
    assert inspect.signature(SL.shortlist).parameters["expand"].default is PINS["shortlist"]["families_default_on"]
