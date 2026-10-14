"""status_report: structure, escaping and vocabulary validation on a tiny synthetic registry."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "status_report.py"
spec = importlib.util.spec_from_file_location("status_report", SCRIPT)
sr = importlib.util.module_from_spec(spec)
sys.modules["status_report"] = sr
spec.loader.exec_module(sr)

TASKS_MD = """## Cycle
- [x] T001 · lead · done thing · mode 1
- [ ] T002 · lead · open thing · mode 1
- [~] T003a · lead · in progress · mode 1
"""
GATES_MD = """## Gate 1 — both work
- [x] smoke green
- [x] Human sign-off: Someone, 2026-01-01
## Gate 2 — recovers
- [ ] ranks first
- [ ] Human sign-off: ______
"""
LEDGER_CSV = (
    "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
    "2026-01-01T00:00:00+00:00,abc,tester,smoke,fake,ds,peak,-13.0,,,\n"
    "2026-01-02T00:00:00+00:00,abc,tester,loss-trust,-,ds,rank1,0.5,,,\n"
)

FEATURE = {
    "id": "alpha",
    "name": "Alpha <b>engine</b>",
    "purpose": "Does \"alpha\" & more",
    "done_when": "It is done",
    "status": "prototype",
    "completion": 40,
    "completion_reason": "Half <built>",
    "functionality": "Renders a thing",
    "health": "partial",
    "evidence": [{"level": "unit-tests", "refs": ["tests/x.py"]},
                 {"level": "synthetic", "refs": ["ledger 2026-01-01"]}],
    "owners": ["agent-a"],
    "tasks": ["T001", "T002", "T003a", "T999"],
    "implemented": [{"item": "A thing", "paths": ["src/a.py"]}],
    "challenges": [{"finding": "It <fails>", "source": "notes.md"}],
    "todo": ["more"],
    "needs_human": ["Capture <chain>"],
    "ledger": [{"kind": "smoke"}],
}


def registry():
    beta = copy.deepcopy(FEATURE)
    beta.update(id="beta", name="Beta", status="not-started", health="unknown",
                evidence=[{"level": "none", "refs": []}], completion=0, tasks=[], ledger=[],
                needs_human=[])
    return {"headline": "Headline <script>alert(1)</script>", "cycle": "Cycle X",
            "generated_for": "lead", "discrepancies": ["Board says <done>"],
            "features": [copy.deepcopy(FEATURE), beta]}


def facts(checks=None):
    return {"tasks": sr.parse_tasks(TASKS_MD), "gates": sr.parse_gates(GATES_MD),
            "ledger": list(__import__("csv").DictReader(LEDGER_CSV.splitlines())),
            "git": {"count": "3", "last": "abc 2026-01-01 msg", "dirty": 0},
            "checks": checks, "now": dt.datetime(2026, 1, 3, 12, 0, tzinfo=dt.UTC)}


def test_valid_registry_has_no_errors():
    assert sr.validate(registry()) == []


def test_parsers():
    assert sr.parse_tasks(TASKS_MD) == {"T001": "x", "T002": " ", "T003a": "~"}
    g = sr.parse_gates(GATES_MD)
    assert [(x["name"], x["done"], x["total"], x["signed"]) for x in g] == [
        ("Gate 1", 2, 2, True), ("Gate 2", 0, 2, False)]


def test_structure():
    out = sr.render(registry(), facts())
    assert out.startswith("<!doctype html>")
    assert 'id="overview"' in out and 'id="discrepancies"' in out and 'id="needs"' in out
    for fid in ("alpha", "beta"):
        assert f'<section id="{fid}"' in out
        assert f'href="#{fid}"' in out
    for heading in ("What is implemented", "Evidence", "Challenges and findings", "Still to do",
                    "Needs the human", "Decisions", "Key files"):
        assert heading in out
    assert "prefers-color-scheme:dark" in out and "max-width:760px" in out
    assert "http://" not in out and "https://" not in out
    assert "Gate 1" in out and "signed off" in out and "not signed off" in out


def test_status_and_health_chips_have_text_labels():
    out = sr.render(registry(), facts())
    assert ">prototype<" in out.replace("</span>", "<") or "prototype</span>" in out
    assert "partial</span>" in out and "unknown</span>" in out
    assert "synthetic</span>" in out and "none</span>" in out


def test_task_counts_and_unknown_ids():
    out = sr.render(registry(), facts())
    assert "1/3 (+1 in progress)" in out  # T001 done, T003a in progress, T002 open
    assert "T999" in out  # unknown id surfaced in discrepancies


def test_ledger_filter_counts_live_rows():
    out = sr.render(registry(), facts())
    assert "1 match" in out and "peak" in out


def test_escaping():
    out = sr.render(registry(), facts())
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "Alpha <b>engine</b>" not in out and "Alpha &lt;b&gt;engine&lt;/b&gt;" in out
    assert "It <fails>" not in out and "It &lt;fails&gt;" in out
    assert "Capture &lt;chain&gt;" in out


def test_footer_checks():
    out = sr.render(registry(), facts([{"name": "pytest", "ok": True, "detail": "9 passed"},
                                       {"name": "ruff", "ok": False, "detail": "2 errors"}]))
    assert "PASS" in out and "FAIL" in out and "9 passed" in out
    assert "not run" in sr.render(registry(), facts(None))


@pytest.mark.parametrize("field,value,fragment", [
    ("status", "almost", "unknown status"),
    ("health", "great", "unknown health"),
    ("completion", 101, "completion"),
    ("completion", "50", "completion"),
])
def test_vocabulary_validation(field, value, fragment):
    reg = registry()
    reg["features"][0][field] = value
    errs = sr.validate(reg)
    assert any(fragment in m for m in errs)


def test_unknown_evidence_word_and_duplicates_and_missing():
    reg = registry()
    reg["features"][0]["evidence"] = [{"level": "vibes", "refs": []}]
    reg["features"][1]["id"] = "alpha"
    del reg["features"][1]["purpose"]
    errs = " | ".join(sr.validate(reg))
    assert "vibes" in errs and "duplicate id" in errs and "missing 'purpose'" in errs


def test_top_evidence():
    assert sr.top_evidence(FEATURE) == "synthetic"
    assert sr.top_evidence({"evidence": [{"level": "none"}]}) == "none"


def make_root(tmp_path, reg):
    (tmp_path / "docs").mkdir()
    (tmp_path / "board").mkdir()
    (tmp_path / "ledger").mkdir()
    (tmp_path / "docs" / "features.json").write_text(json.dumps(reg))
    (tmp_path / "board" / "TASKS.md").write_text(TASKS_MD)
    (tmp_path / "board" / "GATES.md").write_text(GATES_MD)
    (tmp_path / "ledger" / "runs.csv").write_text(LEDGER_CSV)
    return tmp_path


def test_main_writes_page(tmp_path, capsys):
    root = make_root(tmp_path, registry())
    assert sr.main(["--root", str(root)]) == 0
    page = root / "reports" / "status" / "index.html"
    assert page.exists() and str(page) in capsys.readouterr().out
    assert 'id="alpha"' in page.read_text()


def test_main_rejects_invalid_registry(tmp_path, capsys):
    reg = registry()
    reg["features"][0]["status"] = "shipped"
    root = make_root(tmp_path, reg)
    assert sr.main(["--root", str(root)]) == 2
    assert "unknown status" in capsys.readouterr().err
    assert not (root / "reports").exists()


def test_main_missing_registry_exits(tmp_path):
    with pytest.raises(SystemExit) as ei:
        sr.main(["--root", str(tmp_path)])
    assert "registry not found" in str(ei.value)


def test_real_registry_is_valid():
    path = Path(__file__).resolve().parents[1] / "docs" / "features.json"
    assert sr.validate(json.loads(path.read_text(encoding="utf-8"))) == []
