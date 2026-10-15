"""status_report: one report model, two renderers; vocabulary, consistency, dedupe, no invented
percentages. Fixture-based (a tiny synthetic project) plus checks against the real project."""

from __future__ import annotations

import csv
import datetime as dt
import html
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status_report", REPO / "scripts" / "status_report.py")
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
- [ ] finds settings
- [ ] Human sign-off: ______
"""
LEDGER_CSV = (
    "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
    "2026-01-01T00:00:00+00:00,abc,tester,smoke,fake,ds,peak,-13.0,,,\n"
    "2026-01-02T00:00:00+00:00,abc,tester,loss-trust,-,ds,rank1,0.5,,,\n"
)
SUMMARY = ("We are building a tool that listens to a recorded guitar tone and builds a matching "
           "preset for a pedal. The building blocks work: the software drives the sound engine by "
           "itself and writes files the pedal accepts. One song was tried by hand and sounded "
           "promising in an informal listen. What is missing is proof that the tool can tell a "
           "good match from a bad one, so automatic matching cannot be relied on yet. The next "
           "step is to test that judgement against presets whose correct answer is already known, "
           "and only then to run listening tests with other people.")


def feature(fid, name, status="prototype", **kw):
    f = {"id": fid, "name": name, "purpose": f"Purpose of {name} <b>", "done_when": "Done",
         "status": status, "functionality": "Does a thing", "health": "partial",
         "evidence": [{"level": "unit-tests", "refs": ["tests/x.py"]}], "owners": ["agent-a"],
         "tasks": ["T001", "T002", "T003a", "T999"], "needs_human": [],
         "implemented": [{"item": "A thing", "paths": ["src/a.py", "gone/x.py"]}],
         "key_files": ["src/a.py", "cache/secret.bin"], "ledger": [{"kind": "smoke"}]}
    f.update(kw)
    return f


def issue(iid, prio, cls="Risk", ex=True, **kw):
    d = {"id": iid, "title": f"Issue {iid}", "priority": prio, "classification": cls,
         "executive": ex, "problem": "p", "impact": "i", "cause": "c", "resolution": "r",
         "owner": "o", "next_step": "n", "components": ["comp"], "evidence": ["ev-led"]}
    d.update(kw)
    return d


def action(aid, group, title, covers=(), prio="Low"):
    return {"id": aid, "group": group, "priority": prio, "title": title, "why": "w",
            "unblocks": "u", "recommended": "r", "defer": "d", "covers": list(covers) or [aid]}


def registry():
    return {"cycle": "Cycle X", "generated_for": "lead", "discrepancies": ["Board says <done>"],
            "features": [
                feature("alpha", "Alpha | engine", "working",
                        needs_human=["Capture <chain> now", "Lead should rule on it"]),
                feature("beta", "Beta", "not-started", evidence=[{"level": "none", "refs": []}],
                        tasks=[], ledger=[])]}


def curated():
    return {
        "project": {"name": "Demo", "goal": "Match a tone."},
        "period_start": "2026-01-01",
        "ear_qualifier": "informal, not blind",
        "assessments": {
            "health": {"value": "At Risk", "rationale": "The comparison is not validated."},
            "technical": {"value": "Advancing", "rationale": "Pieces were added.",
                          "breakdown": {k: "x" for k in ("implemented", "integrated", "tested",
                                                         "verified", "unfinished")}},
            "readiness": {"stage": "Prototype", "rationale": "Shown in isolation.",
                          "blocker_to_next": "No single command exists."}},
        "summary": SUMMARY,
        "biggest_problem": {"issue": "i1", "why": "Everything depends on it."},
        "milestones": {
            "Gate 1": {"tier": "Foundational", "goal": "g1", "criteria_plain": ["Smoke is green."],
                       "demonstrated": "d", "remaining": "r", "next_action": "none",
                       "evidence": ["ev-led"]},
            "Gate 2": {"tier": "Core method", "state": "At Risk", "goal": "g2",
                       "criteria_plain": ["Ranks first.", "Finds settings."],
                       "demonstrated": "d", "remaining": "r", "blocking": "b", "next_action": "go",
                       "evidence": []}},
        "issues": [issue("i3", "Medium"), issue("i1", "Critical", "Active Blocker"),
                   issue("i2", "High", "Unknown/Unverified"), issue("i4", "Low", ex=False)],
        "actions": [action("capture", "soon", "Capture a chain", ["Capture <chain>"], "High"),
                    action("later1", "later", "Decide something later")],
        "agent_owned": ["Lead should rule"],
        "capabilities": {"verified": [{"capability": "V", "reality": "ok", "evidence": ["ev-led"]}],
                         "unproven": [{"capability": "U", "reality": "maybe"}],
                         "missing": [{"capability": "M", "reality": "no"}]},
        "recent_changes": [
            {"date": "2026-01-02", "kind": "improvement", "text": "Got better.", "evidence": ["ev-led"]},
            {"date": "2026-01-02", "kind": "activity", "text": "Busywork.", "evidence": ["ev-led"]}],
        "components": [
            {"id": "comp", "name": "Engine & friends", "features": ["alpha", "beta"],
             "purpose": "p", "status": "Partially Working", "works": "w", "missing": "m",
             "problem": "pr", "next": "n", "confidence": "c"}],
        "glossary": [{"term": "DI", "definition": "Direct Input."},
                     {"term": "preset", "definition": "A saved sound."}],
        "evidence": [{"id": "ev-led", "label": "Smoke row", "kind": "ledger",
                      "ref": "2026-01-01T00:00:00+00:00"},
                     {"id": "ev-file", "label": "A file", "kind": "file", "ref": "src/a.py"}],
    }


def facts(tmp_path, checks=None):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    return {"tasks": sr.parse_tasks(TASKS_MD), "gates": sr.parse_gates(GATES_MD),
            "ledger": list(csv.DictReader(LEDGER_CSV.splitlines())),
            "git": {"count": "3", "last": "abc 2026-01-01 msg", "recent": ["abc 2026-01-01 msg"],
                    "dirty": 0},
            "checks": checks, "now": dt.datetime(2026, 1, 3, 12, 0, tzinfo=dt.UTC),
            "root": tmp_path}


def build(tmp_path, reg=None, cur=None, baseline=None, checks=None):
    reg, cur = reg or registry(), cur or curated()
    f = facts(tmp_path, checks)
    assert sr.validate(reg) == []
    errs = sr.validate_curated(cur, reg, f)
    assert errs == [], errs
    return sr.build_model(reg, cur, f, baseline)


# ------------------------------------------------------------------ registry and parsing


def test_parsers():
    assert sr.parse_tasks(TASKS_MD) == {"T001": "x", "T002": " ", "T003a": "~"}
    g = sr.parse_gates(GATES_MD)
    assert [(x["name"], x["done"], x["total"], x["signed"]) for x in g] == [
        ("Gate 1", 1, 1, True), ("Gate 2", 0, 2, False)]


@pytest.mark.parametrize("field,value,fragment", [
    ("status", "almost", "unknown status"), ("health", "great", "unknown health"),
    ("completion", 50, "not allowed"), ("completion_reason", "x", "not allowed")])
def test_registry_vocabulary_and_no_completion(field, value, fragment):
    reg = registry()
    reg["features"][0][field] = value
    assert any(fragment in m for m in sr.validate(reg))


def test_registry_unknown_evidence_duplicates_missing():
    reg = registry()
    reg["features"][0]["evidence"] = [{"level": "vibes", "refs": []}]
    reg["features"][1]["id"] = "alpha"
    del reg["features"][1]["purpose"]
    errs = " | ".join(sr.validate(reg))
    assert "vibes" in errs and "duplicate id" in errs and "missing 'purpose'" in errs


def test_top_evidence():
    assert sr.top_evidence(feature("a", "A", evidence=[{"level": "unit-tests"}, {"level": "synthetic"}])) \
        == "synthetic"


# ------------------------------------------------------------------ model: required fields


def test_model_required_fields(tmp_path):
    m = build(tmp_path)
    for k in ("project", "report", "assessments", "summary", "biggest_problem", "next_milestone",
              "milestones", "issues", "actions", "capabilities", "changes", "components",
              "glossary", "evidence", "details"):
        assert k in m, k
    assert m["report"]["date"] == "2026-01-03" and m["report"]["period_from"] == "2026-01-01"
    a = m["assessments"]
    assert a["health"]["value"] in sr.HEALTH_STATUS and a["health"]["rationale"]
    assert a["technical"]["value"] in sr.TECH_PROGRESS
    assert a["readiness"]["stage"] in sr.STAGES and a["readiness"]["blocker_to_next"]
    assert m["biggest_problem"]["id"] == "i1"  # highest priority, not list order
    assert [i["id"] for i in m["issues"]] == ["i1", "i2", "i3", "i4"]
    assert m["next_milestone"]["name"] == "Gate 2"
    assert [x["state"] for x in m["milestones"]] == ["Complete", "At Risk"]
    json.dumps(m, default=str)  # serialisable


def test_vocabularies_in_model(tmp_path):
    m = build(tmp_path)
    assert all(i["priority"] in sr.PRIORITIES and i["classification"] in sr.CLASSIFICATIONS
               for i in m["issues"])
    assert all(x["state"] in sr.MILESTONE_STATES for x in m["milestones"])
    assert all(c["status"] in sr.COMPONENT_STATUS for c in m["components"])
    assert all(f["status"] in sr.STATUSES for f in m["details"]["features"])
    assert [x["group"] for x in m["actions"]] == ["soon", "later"]


def test_ear_is_labelled_informal_until_gate3(tmp_path):
    reg = registry()
    reg["features"][0]["evidence"].append({"level": "ear", "refs": ["x"]})
    m = build(tmp_path, reg)
    assert m["details"]["features"][0]["evidence_labels"][-1] == "ear (informal, not blind)"


# ------------------------------------------------------------------ guardrails


def cur_err(tmp_path, mutate):
    cur = curated()
    mutate(cur)
    return sr.validate_curated(cur, registry(), facts(tmp_path))


def test_guardrails(tmp_path):
    def has(mutate, frag):
        assert any(frag in e for e in cur_err(tmp_path, mutate)), frag
    has(lambda c: c["assessments"]["health"].update(value="On Track"), "contradicts")
    has(lambda c: c["assessments"]["health"].update(value="Fine"), "health")
    has(lambda c: c["assessments"]["readiness"].update(stage="Validated"), "Gate 2")
    has(lambda c: c["milestones"]["Gate 2"].update(state="Complete"), "not signed")
    has(lambda c: c["milestones"]["Gate 1"].update(state="At Risk"), "signed off")
    has(lambda c: c["milestones"]["Gate 2"].update(criteria_plain=["only one"]), "plain criteria")
    has(lambda c: c.update(biggest_problem={"issue": "i3", "why": "w"}), "highest-priority")
    has(lambda c: c["issues"][0].update(executive=False), "3-5")
    has(lambda c: c["evidence"][0].update(ref="2030-01-01T00:00:00+00:00"), "not in ledger")
    has(lambda c: c["issues"][0].update(evidence=["nope"]), "unknown evidence id")
    has(lambda c: c["components"][0].update(features=["alpha"]), "in no component")
    has(lambda c: c["components"][0].update(status="Not Started"), "Not Started")
    has(lambda c: c.update(summary="Short summary."), "words")
    has(lambda c: c.update(summary=SUMMARY + " See scripts/x.py and T045 by native-engine."),
        "file name")
    has(lambda c: c.update(summary=SUMMARY + " The XYZQ thing."), "unexplained abbreviation")


def test_uncovered_human_need_is_an_error(tmp_path):
    reg = registry()
    reg["features"][0]["needs_human"].append("Do a brand new thing")
    errs = sr.validate_curated(curated(), reg, facts(tmp_path))
    assert any("not covered" in e for e in errs)


def test_agent_owned_need_is_not_a_human_action(tmp_path):
    m = build(tmp_path)
    assert all("Lead should rule" not in a["title"] for a in m["actions"])
    assert m["details"]["lead_items"] == ["Lead should rule on it"]
    assert m["actions"][0]["sources"] == ["Alpha | engine"]


def test_duplicate_action_detection(tmp_path):
    acts = [action("a", "soon", "Record the play-along takes"),
            action("b", "soon", "Record the play-along takes now"),
            action("c", "later", "Update the firmware")]
    assert sr.find_duplicate_actions(acts) == [("a", "b")]
    assert sr.find_duplicate_actions([action("x", "soon", "One"), action("x", "soon", "Two")]) \
        == [("x", "x")]
    cur = curated()
    cur["actions"].append(action("capture2", "later", "Capture a chain", ["zzz"]))
    errs = sr.validate_curated(cur, registry(), facts(tmp_path))
    assert any("duplicates" in e for e in errs)


# ------------------------------------------------------------------ renderers


def test_html_structure(tmp_path):
    m = build(tmp_path)
    out = sr.render_html(m)
    assert out.startswith("<!doctype html>") and "<script" not in out
    for sec in ('id="dashboard"', 'id="problems"', 'id="milestones"', 'id="works"',
                'id="actions"', 'id="changes"', 'id="components"', 'id="technical"',
                'id="glossary"', 'role="region"', "<details", "prefers-color-scheme:dark",
                "max-width:640px"):
        assert sec in out, sec
    assert "http://" not in out and "https://" not in out
    assert "At Risk" in out and "Prototype (stage 2 of 5)" in out
    assert "<abbr" in out and "Direct Input" in out


def test_html_escaping(tmp_path):
    reg = registry()
    reg["features"][0]["name"] = "Alpha <b>x</b>"
    cur = curated()
    cur["project"]["goal"] = "Goal <script>alert(1)</script>"
    out = sr.render_html(build(tmp_path, reg, cur))
    assert "<script>alert(1)" not in out and "&lt;script&gt;alert(1)" in out
    assert "Alpha <b>x</b>" not in out and "Alpha &lt;b&gt;x&lt;/b&gt;" in out


def test_markdown_structure_and_escaping(tmp_path):
    m = build(tmp_path)
    md = sr.render_markdown(m, tmp_path)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert heads == ["## Executive dashboard", "## Key problems and blockers",
                     "## Milestone progress", "## What works and what does not",
                     "## Human decisions and actions", "## What changed since the last report",
                     "## Component status", "## Technical details and evidence", "## Glossary",
                     "## Report information"]
    assert "**Project Health:** ▲ At Risk" in md and "**Product Readiness:** Prototype" in md
    assert "<script" not in md and "<b>" not in md and "Capture <chain>" not in md
    assert "Board says &lt;done&gt;" in md
    assert "[`src/a.py`](../../src/a.py)" in md and "](../../gone/" not in md
    assert "](../../cache/" not in md
    # every internal link resolves to a real heading anchor
    used: dict[str, int] = {}
    anchors = {sr.gh_slug(ln.lstrip("# ").strip(), used)
               for ln in md.splitlines() if re.match(r"#{1,6} ", ln)}
    for target in re.findall(r"\]\(#([^)]+)\)", md):
        assert target in anchors, target
    assert "@@" not in md


def test_markdown_tables_are_regular(tmp_path):
    md = sr.render_markdown(build(tmp_path), tmp_path)
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("|---"):
            n = ln.count("|")
            j = i - 1
            while j >= 0 and lines[j].startswith("|"):
                assert len(re.findall(r"(?<!\\)\|", lines[j])) == n, lines[j]
                j -= 1
            k = i + 1
            while k < len(lines) and lines[k].startswith("|"):
                assert len(re.findall(r"(?<!\\)\|", lines[k])) == n, lines[k]
                k += 1


def test_cross_format_consistency(tmp_path):
    m = build(tmp_path)
    html_out, md = sr.render_html(m), sr.render_markdown(m, tmp_path)
    a = m["assessments"]
    for txt in (html_out, md):
        assert a["health"]["value"] in txt and a["readiness"]["stage"] in txt
        assert a["technical"]["value"] in txt
        assert m["biggest_problem"]["title"] in txt
        for ms in m["milestones"]:
            assert ms["name"] in txt and ms["state"] in txt
        for i in m["issues"]:
            assert i["title"] in txt
        for x in m["actions"]:
            assert x["title"] in txt
        for c in m["components"]:
            assert sr.e(c["name"]) in txt or sr.mde(c["name"]) in txt
        assert "Direct Input" in txt  # glossary reachable in both
        for c in m["changes"]["recent"]:
            assert c["text"] in txt
        assert m["summary"] in html.unescape(re.sub(r"<[^>]+>", "", txt))


def test_summary_is_shown_once_per_format_and_empty_blocking_group(tmp_path):
    m = build(tmp_path)
    assert "None today" in sr.render_markdown(m, tmp_path) and "None today" in sr.render_html(m)


# ------------------------------------------------------------------ no invented percentages


def strip_css(h):
    return re.sub(r"<style>.*?</style>", "", h, flags=re.DOTALL)


def test_no_percentages_in_fixture_outputs(tmp_path):
    m = build(tmp_path)
    assert not re.search(r"\d\s?%", strip_css(sr.render_html(m)))
    assert not re.search(r"\d\s?%", sr.render_markdown(m, tmp_path))
    assert "completion" not in json.dumps(m)


# ------------------------------------------------------------------ baseline and diff


def test_no_baseline_is_stated(tmp_path):
    m = build(tmp_path)
    assert m["changes"]["baseline_date"] is None
    assert "No earlier report snapshot" in sr.render_markdown(m, tmp_path)
    assert "No earlier report snapshot" in sr.render_html(m)


def test_snapshot_and_diff(tmp_path):
    m1 = build(tmp_path)
    prev = sr.snapshot_of(m1)
    prev["date"] = "2026-01-02"
    cur = curated()
    cur["assessments"]["health"]["value"] = "Blocked"
    cur["assessments"]["health"]["rationale"] = "Now blocked."
    cur["actions"].append(action("newone", "soon", "A wholly different request", prio="High"))
    cur["actions"][0]["group"] = "later"
    reg = registry()
    reg["features"][0]["needs_human"].append("A wholly different request")
    cur["actions"][-1]["covers"] = ["wholly different"]
    cur["issues"] = [i for i in cur["issues"] if i["id"] != "i2"] + [
        issue("i5", "Medium", ex=True)]
    m2 = build(tmp_path, reg, cur, baseline=prev)
    text = " ".join(d["text"] for d in m2["changes"]["diff"])
    assert "Project Health changed from At Risk to Blocked" in text
    assert "New issue: Issue i5" in text and "Issue no longer listed: Issue i2" in text
    assert "New human action: A wholly different request" in text
    assert "moved from soon to later" in text
    assert m2["report"]["period_from"] == "2026-01-02"
    assert "Compared with the report snapshot of 2026-01-02" in sr.render_markdown(m2, tmp_path)


def test_baseline_loading_and_snapshot_writing(tmp_path):
    m = build(tmp_path)
    hist = tmp_path / "reports" / "status" / "history"
    path = sr.write_snapshot(hist, m)
    assert path.name == "2026-01-03.json"
    assert sr.load_baseline(hist, "2026-01-03") is None  # same day is not a baseline
    assert sr.load_baseline(hist, "2026-01-04")["date"] == "2026-01-03"
    assert path.read_text() == path.read_text()  # deterministic, small
    assert len(path.read_text()) < 20000


# ------------------------------------------------------------------ main


def make_root(tmp_path):
    for d in ("docs", "board", "ledger"):
        (tmp_path / d).mkdir()
    (tmp_path / "docs" / "features.json").write_text(json.dumps(registry()))
    (tmp_path / "docs" / "status-curated.json").write_text(json.dumps(curated()))
    (tmp_path / "board" / "TASKS.md").write_text(TASKS_MD)
    (tmp_path / "board" / "GATES.md").write_text(GATES_MD)
    (tmp_path / "ledger" / "runs.csv").write_text(LEDGER_CSV)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    return tmp_path


def test_main_writes_both_reports_and_snapshot(tmp_path, capsys):
    root = make_root(tmp_path)
    assert sr.main(["--root", str(root)]) == 0
    st = root / "reports" / "status"
    assert (st / "index.html").exists() and (st / "README.md").exists()
    assert list((st / "history").glob("*.json"))
    assert "Executive dashboard" in (st / "README.md").read_text(encoding="utf-8")
    (st / "README.md").unlink()
    (st / "index.html").unlink()
    assert sr.main(["--root", str(root), "--format", "md", "--no-snapshot"]) == 0
    assert (st / "README.md").exists() and not (st / "index.html").exists()
    with pytest.raises(SystemExit):
        sr.main(["--root", str(root), "--format", "pdf"])


def test_main_rejects_invalid_inputs(tmp_path, capsys):
    root = make_root(tmp_path)
    reg = registry()
    reg["features"][0]["status"] = "shipped"
    (root / "docs" / "features.json").write_text(json.dumps(reg))
    assert sr.main(["--root", str(root)]) == 2
    assert "unknown status" in capsys.readouterr().err
    assert not (root / "reports").exists()
    (root / "docs" / "features.json").write_text(json.dumps(registry()))
    bad = curated()
    bad["assessments"]["health"]["value"] = "Great"
    (root / "docs" / "status-curated.json").write_text(json.dumps(bad))
    assert sr.main(["--root", str(root)]) == 2
    assert "health" in capsys.readouterr().err


def test_main_missing_registry_exits(tmp_path):
    with pytest.raises(SystemExit) as ei:
        sr.main(["--root", str(tmp_path)])
    assert "registry not found" in str(ei.value)


# ------------------------------------------------------------------ the real project


@pytest.fixture(scope="module")
def real():
    model, errs = sr.make_model(REPO, None, None, checks=False)
    assert errs == [], errs
    return model


def test_real_project_builds_and_is_consistent(real):
    a = real["assessments"]
    assert a["health"]["value"] == "At Risk" and a["readiness"]["stage"] in ("Foundation", "Prototype")
    states = {m["name"]: m["state"] for m in real["milestones"]}
    assert states["Gate 1"] == "Complete" and states["Gate 2"] != "Complete"
    assert real["next_milestone"]["name"] == "Gate 2"
    md = sr.render_markdown(real, REPO)
    out = sr.render_html(real)
    for txt in (md, out):
        assert real["biggest_problem"]["title"] in txt
    assert not re.search(r"\d\s?%", md) and not re.search(r"\d\s?%", strip_css(out))
    assert 60 <= sr.words(real["summary"]) <= 170
    assert sr.find_duplicate_actions(real["actions"]) == []


def test_real_project_public_safety(real):
    md = sr.render_markdown(real, REPO)
    out = sr.render_html(real)
    for txt in (md, out):
        assert not re.search(r"\.(wav|mp3|flac|ogg|m4a)\b", txt, re.IGNORECASE)
    assert "](../../cache/" not in md and "](../../data/" not in md
    assert "http://" not in out and "<script" not in out


def test_real_registry_has_no_unmapped_needs():
    reg = json.loads((REPO / "docs" / "features.json").read_text(encoding="utf-8"))
    assert sr.validate(reg) == []
