"""T168: scripts/stomp_soak.py pure helpers. No pedal."""
from __future__ import annotations

import importlib.util
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stomp_soak", ROOT / "scripts" / "stomp_soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

CATALOG = {"models": {
    "D1": {"category": "drive", "stomp": True, "mono": True,
           "params": {"Gain": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.5},
                      "Level": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.6},
                      "Mode": {"kind": "stepped", "min": 0, "max": 2, "default": 1}}},
    "D2": {"category": "drive", "stomp": True, "mono": True, "params": {}},
    "D3": {"category": "drive", "stomp": True, "mono": True, "params": {}},
    "Dnew": {"category": "drive", "stomp": True, "mono": True, "stomp_min_firmware": "3.90", "params": {}},
    "Dstereo": {"category": "drive", "stomp": True, "mono": False, "params": {}},
    "R1": {"category": "reverb", "stomp": True, "mono": True, "params": {}},
}}


def test_drive_models_filters_category_availability_and_refusals():
    assert soak.drive_models(CATALOG, refused=set()) == ["D1", "D2", "D3"]
    assert soak.drive_models(CATALOG, refused={"D2"}) == ["D1", "D3"]


def test_every_step_is_a_model_change():
    seq = soak.model_sequence(["D1", "D2", "D3"], 200, random.Random(1))
    assert len(seq) == 200
    assert all(a != b for a, b in zip(seq, seq[1:]))
    assert set(seq) == {"D1", "D2", "D3"}


def test_model_sequence_with_two_models_alternates():
    seq = soak.model_sequence(["D1", "D2"], 9, random.Random(0))
    assert all(a != b for a, b in zip(seq, seq[1:]))


def test_drive_hlx_replaces_the_block_and_keeps_level_at_default():
    base = {"data": {"tone": {"dsp0": {"inputA": {"@input": 6}, "block0": {"@model": "Old", "X": 1},
                                       "block1": {"@model": "Gone"}}}}}
    h = soak.drive_hlx(base, "D1", CATALOG, random.Random(3))
    dsp = h["data"]["tone"]["dsp0"]
    assert dsp["block0"]["@model"] == "D1" and dsp["block0"]["@enabled"] is True
    assert "block1" not in dsp and dsp["inputA"] == {"@input": 6}
    assert 0.0 <= dsp["block0"]["Gain"] <= 1.0 and dsp["block0"]["Level"] == 0.6 and dsp["block0"]["Mode"] == 1
    assert base["data"]["tone"]["dsp0"]["block0"]["@model"] == "Old", "the base is not modified"


def test_chain_sig():
    ch = {"slots": [{"pos": 0, "kind": "Input", "enabled": True}, {"pos": 1, "kind": "Block", "model_id": "D1",
                                                                  "enabled": False}]}
    assert soak.chain_sig(ch) == [(0, "Input", None, True), (1, "Block", "D1", False)]
