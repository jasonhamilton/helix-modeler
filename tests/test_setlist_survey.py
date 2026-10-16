"""Survey script on a synthetic setlist (no Line 6 data)."""
import importlib.util
import json
from pathlib import Path

from test_preset import CAT, synth

from tonematch.engines.native_setlist import write_hls

SPEC = importlib.util.spec_from_file_location(
    "setlist_survey", Path(__file__).resolve().parents[1] / "scripts" / "setlist_survey.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)


def _hls(tmp_path):
    p = synth()["data"]
    p2 = synth()["data"]
    p2["tone"]["dsp0"]["block1"]["@path"] = 1
    return write_hls([p, p2], "synthetic", tmp_path / "s.hls")


def test_coverage_and_stats_on_synthetic_setlist(tmp_path, monkeypatch, capsys):
    cat = json.loads(json.dumps(CAT))
    for m in cat["models"].values():
        m["source_file"] = "x.models"
    monkeypatch.setattr(S.CAT, "load_catalog", lambda *_a, **_k: cat)
    monkeypatch.setattr(S, "OUT", tmp_path / "out")
    hls = _hls(tmp_path)
    assert S.main(["coverage", "--hls", str(hls), "--no-compile"]) == 0
    rows = json.loads((tmp_path / "out" / "coverage.json").read_text())
    assert len(rows) == 2 and all(r["roundtrip"] == "ok" for r in rows)
    assert "two-path" in rows[1]["tags"]
    assert S.main(["stats", "--hls", str(hls)]) == 0
    st = json.loads((tmp_path / "out" / "prior_stats.json").read_text())
    assert st["presets"] == 2 and st["share_presets_with_category"]["amp"] == 1.0
