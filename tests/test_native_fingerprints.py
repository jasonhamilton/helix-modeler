"""T047 block-probe fingerprints of the Native block library: grid choice, summary, loaders.
No Native: a fake template and a synthetic fingerprint."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

_spec = importlib.util.spec_from_file_location(
    "native_block_fingerprints", Path(__file__).resolve().parents[1] / "scripts" / "native_block_fingerprints.py")
fpmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpmod)

CAT = {"models": {"HD2_DelayX": {"category": "delay", "params": {
    "Mix": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.4},
    "Level": {"kind": "continuous", "min": -60.0, "max": 6.0, "default": 0.0},
    "Feedback": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.3},
    "Time": {"kind": "continuous", "min": 0.0, "max": 8.0, "default": 0.5},
    "TempoSync1": {"kind": "bool"}}}}}


def _tpl(verified):
    amap = {f"0:{k}": f"cc_{i}" for i, k in enumerate(("Mix", "Level", "Feedback", "Time"))}
    return nat.ChainTemplate((("delay", "HD2_DelayX"),), b"", amap, {"0:Time": [0.0, 1.27]},
                             verified=[f"0:{k}" for k in verified])


def test_primary_params_follow_category_priority_and_skip_levels():
    assert fpmod.primary_params("HD2_DelayX", CAT, _tpl(["Mix", "Level", "Feedback", "Time"])) == ["Time", "Feedback"]
    # Feedback baked (unverified): the next priority name, Mix
    assert fpmod.primary_params("HD2_DelayX", CAT, _tpl(["Mix", "Level", "Time"])) == ["Time", "Mix"]
    assert fpmod.primary_params("HD2_DelayX", CAT, _tpl(["Level"])) == []


def test_grid_is_default_corners_and_centre_on_the_template_cc_grid():
    g = fpmod.grid("HD2_DelayX", CAT, _tpl(["Mix", "Feedback", "Time"]))
    assert [s["setting"] for s in g] == ["default", "Time@24,Feedback@24", "Time@24,Feedback@103",
                                         "Time@103,Feedback@24", "Time@103,Feedback@103", "Time@64,Feedback@64"]
    assert g[1]["params"]["Time"] == nat.cc_value(0.0, 1.27, 24)  # the narrowed range, not 0-8 s
    assert g[4]["params"]["Feedback"] == nat.cc_value(0.0, 1.0, 103)
    one = fpmod.grid("HD2_DelayX", CAT, _tpl(["Time", "Level"]))
    assert [s["cc"] for s in one[1:]] == [{"Time": 24}, {"Time": 64}, {"Time": 103}]
    assert fpmod.grid("HD2_DelayX", CAT, _tpl([])) == [{"setting": "default", "params": {}, "cc": {}}]


def test_summary_reads_the_fingerprint_fields():
    fp = {"linear": {"freqs_hz": [250.0, 1000.0, 4000.0], "gain_1k_db": [0.0, 1.5, 0, 0],
                     "mag_db": [[0, 0, 0], [-1.0, 0.0, 2.0]]},
          "harmonics": {"per_freq": [{"freq_hz": 440.0, "levels_dbfs": [-45, -15, -9, -3],
                                      "thd_db": [-80, -60, -40.5, -20]}]},
          "static_curve": {"ratio": 2.0, "knee_dbfs": -20.0}, "dynamics": {"level_dependent_gain_db": -3.0},
          "delay": {"detected": True, "time_ms": 240.5, "feedback": 0.3}, "reverb": {"rt60_s": None},
          "modulation": {"kind": "none", "rate_hz": None}}
    s = fpmod.summary(fp)
    assert s["gain_1k_db"] == 1.5 and s["tilt_4k_250_db"] == 3.0 and s["thd_440_db"] == -40.5
    assert s["delay_ms"] == 240.5 and s["feedback"] == 0.3 and s["ratio"] == 2.0
    fp["delay"]["detected"] = False
    assert fpmod.summary(fp)["delay_ms"] is None
    json.dumps(fpmod.summary({}))  # every field optional


def test_library_fingerprint_loaders(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path / "cache"))
    assert lib.fingerprint_index() == {"models": {}} and lib.fingerprint_set("HD2_DelayX") is None
    d = lib.fingerprint_dir()
    assert d == tmp_path / "cache" / "probe" / "fingerprints" / lib.VERSION
    d.mkdir(parents=True)
    doc = {"schema": "tonematch.block-fingerprint-set", "schema_version": 1, "model": "HD2_DelayX",
           "category": "delay", "template": "t", "grid": fpmod.GRID,
           "settings": [{"setting": "default", "params": {}, "cc": {}, "summary": {"delay_ms": 500.0},
                         "fingerprint": {"schema": "tonematch.block-fingerprint"}}]}
    (d / "HD2_DelayX.json").write_text(json.dumps(doc))
    monkeypatch.setattr(fpmod, "out_dir", lib.fingerprint_dir)
    fpmod.write_index()
    idx = lib.fingerprint_index()
    assert idx["models"]["HD2_DelayX"]["settings"][0]["summary"] == {"delay_ms": 500.0}
    assert "fingerprint" not in idx["models"]["HD2_DelayX"]["settings"][0]
    assert lib.fingerprint_set("HD2_DelayX")["settings"][0]["fingerprint"]["schema"] == "tonematch.block-fingerprint"


@pytest.mark.parametrize("cat", ["drive", "delay", "reverb", "comp", "mod", "amp", "cab"])
def test_every_category_has_a_priority_entry(cat):
    assert cat in fpmod.PRIORITY
