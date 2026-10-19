"""tonematch.cli_result: the result-manifest helper (contracts/cli-result.md)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pytest

from tonematch import cli_result as CR

ROOT = CR.ROOT
KEYS = {"schema", "tool", "version", "status", "exit_code", "error", "started", "finished", "duration_s",
        "args", "inputs", "outputs", "metrics", "ledger_rows", "notes"}


def load(p):
    return json.loads(Path(p).read_text())


def test_schema_has_every_documented_key(tmp_path):
    p = tmp_path / "r.json"
    CR.write_result(p, tool="t", args=argparse.Namespace(a=1, result_json=str(p)), inputs={"x": ROOT / "docs" / "PLAN.md"},
                    outputs={"y": [ROOT / "cache" / "a.wav", ROOT / "cache" / "b.wav"]}, metrics={"n": 3},
                    ledger_rows=[{"kind": "smoke"}], notes="hi")
    d = load(p)
    assert set(d) == KEYS
    assert d["schema"] == CR.SCHEMA == "cli-result/v0"
    assert d["status"] == "ok" and d["exit_code"] == 0 and d["error"] is None
    assert d["args"] == {"a": 1}                       # --result-json itself is not echoed
    assert d["metrics"] == {"n": 3} and d["ledger_rows"] == [{"kind": "smoke"}] and d["notes"] == "hi"
    assert d["version"]


def test_paths_are_repo_relative_posix_and_outside_stays_absolute(tmp_path):
    p = tmp_path / "r.json"
    outside = tmp_path / "elsewhere" / "f.wav"
    CR.write_result(p, tool="t", inputs={"in": ROOT / "docs" / "PLAN.md", "rel": "docs/PLAN.md"},
                    outputs={"many": [ROOT / "cache" / "x" / "a.wav"], "out": outside, "skip": None})
    d = load(p)
    assert d["inputs"] == {"in": "docs/PLAN.md", "rel": "docs/PLAN.md"}
    assert d["outputs"]["many"] == ["cache/x/a.wav"]
    assert d["outputs"]["out"] == str(outside) and "skip" not in d["outputs"]
    assert "\\" not in json.dumps(d["inputs"])


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    p = tmp_path / "r.json"
    CR.write_result(p, tool="first")
    assert [x.name for x in tmp_path.iterdir()] == ["r.json"]
    real = os.replace

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        CR.write_result(p, tool="second")
    monkeypatch.setattr(os, "replace", real)
    assert load(p)["tool"] == "first"                  # the old manifest is intact
    assert [x.name for x in tmp_path.iterdir()] == ["r.json"]


def test_no_path_is_a_noop_and_bad_status_is_rejected(tmp_path):
    assert CR.write_result(None, tool="t") is None
    with pytest.raises(ValueError):
        CR.write_result(tmp_path / "r.json", tool="t", status="maybe")


def test_add_result_arg_defaults_off():
    ap = argparse.ArgumentParser()
    CR.add_result_arg(ap)
    assert ap.parse_args([]).result_json is None
    assert ap.parse_args(["--result-json", "x.json"]).result_json == "x.json"


def test_json_safe_values(tmp_path):
    p = tmp_path / "r.json"
    CR.write_result(p, tool="t", metrics={"nan": float("nan"), "np": np.float32(1.5), "i": np.int64(3),
                                          "path": Path("a/b"), "set": {1}})
    d = load(p)["metrics"]
    assert d["nan"] is None and d["np"] == 1.5 and d["i"] == 3 and d["path"] == "a/b" and d["set"] == [1]


def test_scope_ok_and_nonzero_exit_code(tmp_path):
    a = argparse.Namespace(result_json=str(tmp_path / "ok.json"), flag=True)
    with CR.scope(a, tool="t", inputs={"in": "docs/PLAN.md"}) as res:
        res.metrics["n"] = 1
        res.outputs["o"] = ROOT / "cache" / "o.wav"
        res.add_ledger_row(["--kind", "smoke", "--dataset", "d", "--metric", "m", "--value", "1.5"])
    d = load(tmp_path / "ok.json")
    assert d["status"] == "ok" and d["metrics"] == {"n": 1} and d["outputs"] == {"o": "cache/o.wav"}
    assert d["ledger_rows"] == [{"kind": "smoke", "dataset": "d", "metric": "m", "value": 1.5}]
    assert d["started"] and d["finished"] and d["duration_s"] >= 0
    a.result_json = str(tmp_path / "bad.json")
    with CR.scope(a, tool="t") as res:
        res.exit_code = 2
    d = load(tmp_path / "bad.json")
    assert d["status"] == "error" and d["exit_code"] == 2 and "2" in d["error"]


def test_scope_error_path_writes_then_reraises(tmp_path):
    a = argparse.Namespace(result_json=str(tmp_path / "e.json"))
    with pytest.raises(ValueError, match="bad thing"), CR.scope(a, tool="t") as res:
        res.metrics["before"] = 1
        raise ValueError("bad thing")
    d = load(tmp_path / "e.json")
    assert d["status"] == "error" and d["exit_code"] == 1
    assert d["error"] == "ValueError: bad thing" and d["metrics"] == {"before": 1}


def test_scope_systemexit_is_recorded_and_propagates(tmp_path):
    a = argparse.Namespace(result_json=str(tmp_path / "s.json"))
    with pytest.raises(SystemExit), CR.scope(a, tool="t"):
        raise SystemExit(2)
    d = load(tmp_path / "s.json")
    assert d["status"] == "error" and d["exit_code"] == 2


def test_scope_without_path_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with CR.scope(argparse.Namespace(result_json=None), tool="t"):
        pass
    assert list(tmp_path.iterdir()) == []


def test_run_returns_the_body_exit_code(tmp_path):
    a = argparse.Namespace(result_json=str(tmp_path / "r.json"))
    assert CR.run(a, tool="t", body=lambda res: 1) == 1
    assert load(tmp_path / "r.json")["status"] == "error"
    assert CR.run(a, tool="t", body=lambda res: None) == 0
    assert load(tmp_path / "r.json")["status"] == "ok"


def test_ledger_row_from_args():
    row = CR.ledger_row_from_args(["--kind", "smoke", "--engine", "native", "--value", "-12.5", "--wall-clock-s", "3"])
    assert row == {"kind": "smoke", "engine": "native", "value": -12.5, "wall_clock_s": "3"}


def test_absolute_repo_paths_in_args_are_made_relative(tmp_path):
    p = tmp_path / "r.json"
    CR.write_result(p, tool="t", args={"golden": str(ROOT / "fixtures" / "golden"), "other": str(tmp_path / "x"),
                                       "many": [ROOT / "docs" / "PLAN.md"], "n": 3})
    a = load(p)["args"]
    assert a["golden"] == "fixtures/golden" and a["many"] == ["docs/PLAN.md"]
    assert a["other"] == str(tmp_path / "x") and a["n"] == 3
