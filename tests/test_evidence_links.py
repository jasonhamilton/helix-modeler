"""Evidence citations become links: ledger rows, decisions, gates, files, tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
spec = importlib.util.spec_from_file_location("evidence_links", REPO / "scripts" / "evidence_links.py")
ev = importlib.util.module_from_spec(spec)
sys.modules["evidence_links"] = ev
spec.loader.exec_module(ev)

LEDGER = (
    "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
    "2026-10-14T10:00:00+00:00,x,a,smoke,native,smoke,peak,-13.5,,,\"multi\nline note\"\n"
    "2026-10-14T10:05:00+00:00,x,a,parity,stomp,ds,null_db,-60.0,,,n\n"
    "2026-10-14T10:05:00+00:00,x,a,parity,stomp,ds,other,1.0,,,n\n"
)
DECISIONS = """# Decision log
## 2026-10-15 (native-engine, host memory)
## 2026-10-15 (lead, host memory)
## 2026-10-15 (native-engine, stale template)
## 2026-10-16 (a "quoted" heading, with `code`)
## 2026-10-16 (a "quoted" heading, with `code`)
"""
GATES = "# Gates\n## Gate 1 — engines (week 1)\n## Gate 2 — recovers presets\n"


def make_root(tmp: Path) -> Path:
    (tmp / "ledger").mkdir()
    (tmp / "ledger" / "runs.csv").write_text(LEDGER, encoding="utf-8")
    (tmp / "docs" / "notes").mkdir(parents=True)
    (tmp / "docs" / "DECISIONS.md").write_text(DECISIONS, encoding="utf-8")
    (tmp / "docs" / "notes" / "n.md").write_text("# The note\n\n## Setup steps\ntext\n", encoding="utf-8")
    (tmp / "board").mkdir()
    (tmp / "board" / "GATES.md").write_text(GATES, encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_a.py").write_text('"""A tests."""\n\n\ndef test_one():\n    pass\n\n\n'
                                             "def test_dup():\n    pass\n", encoding="utf-8")
    (tmp / "tests" / "test_b.py").write_text("def test_dup():\n    pass\n", encoding="utf-8")
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "tool.py").write_text('"""Does the tool thing.\n\nMore."""\n', encoding="utf-8")
    return tmp


def idx(tmp_path):
    return ev.Index(make_root(tmp_path), "../..")


def test_ledger_line_numbers_follow_physical_lines_and_multi_rows(tmp_path):
    i = idx(tmp_path)
    href, title, n = i.ledger_ref("2026-10-14T10:00:00+00:00")
    assert href == "../../ledger/runs.csv?plain=1#L2" and n == 1
    assert title == "smoke · native · smoke · peak = -13.5"
    href, title, n = i.ledger_ref("2026-10-14T10:05:00")
    assert href.endswith("#L4") and n == 2 and "first of 2 rows" in title  # row 1 spans lines 2-3
    assert i.ledger_ref("2030-01-01T00:00:00") is None


def test_ledger_citations_in_text_with_bare_times_and_unresolved(tmp_path):
    i = idx(tmp_path)
    unres: set[str] = set()
    out = ev.linkify_markdown("ledger 2026-10-14T10:00:00+00:00 and 10:05 done; "
                              "ledger 2026-12-01T00:00:00 gone; the time 2026-12-02T00:00:00 plain\n",
                              i, unres)
    assert '[2026-10-14T10:00:00+00:00](../../ledger/runs.csv?plain=1#L2 "smoke' in out
    assert '[10:05](../../ledger/runs.csv?plain=1#L4 "parity · stomp · ds · null_db = -60.0 (first of 2' in out
    assert "(2 rows)" in out
    assert "ledger 2026-12-01T00:00:00 gone" in out
    assert any("2026-12-01" in u for u in unres) and not any("2026-12-02" in u for u in unres)
    assert ev.linkify_markdown(out, i) == out


def test_slug_rules_and_duplicates():
    heads = ev.slug_headings(DECISIONS + "```\n## not a heading\n```\n")
    slugs = [s for _, s in heads]
    assert slugs[0] == "decision-log"
    assert slugs[1] == "2026-10-15-native-engine-host-memory"
    assert slugs[4] == "2026-10-16-a-quoted-heading-with-code"
    assert slugs[5] == "2026-10-16-a-quoted-heading-with-code-1"
    assert len(heads) == 6
    assert ev.slug_headings("## Gate 1 — engines (week 1)")[0][1] == "gate-1--engines-week-1"


def test_decisions_pick_best_heading_and_refuse_ties(tmp_path):
    i = idx(tmp_path)
    href, head, kept = i.decision_ref("2026-10-15", " stale template and more")
    assert href == "../../docs/DECISIONS.md#2026-10-15-native-engine-stale-template"
    assert head == "2026-10-15 (native-engine, stale template)" and kept == "stale template"
    assert i.decision_ref("2026-10-16", " quoted heading") is None  # two identical headings
    assert i.decision_ref("2026-10-15", "") is None
    unres: set[str] = set()
    out = ev.linkify_markdown("(DECISIONS 2026-10-15 stale template).", i, unres)
    assert '[DECISIONS 2026-10-15 stale template](' in out
    assert out.endswith(").")
    ev.linkify_markdown("DECISIONS 2026-10-16 quoted heading", i, unres)
    assert unres


def test_gates_files_sections_and_tests(tmp_path):
    i = idx(tmp_path)
    unres: set[str] = set()
    text = ("Gate 2 holds. See scripts/tool.py and docs/notes/n.md 'Setup steps'. "
            "tests/test_a.py::test_one, test_one, test_dup, test_missing.\n")
    out = ev.linkify_markdown(text, i, unres)
    assert '[Gate 2](../../board/GATES.md#gate-2--recovers-presets "Gate 2 — recovers presets")' in out
    assert '[scripts/tool.py](../../scripts/tool.py "Does the tool thing.")' in out
    assert "(../../docs/notes/n.md#setup-steps \"Setup steps\")" in out
    assert '[tests/test_a.py::test_one](../../tests/test_a.py?plain=1#L4 "test_one")' in out
    assert '[test_one](../../tests/test_a.py?plain=1#L4 "test_one")' in out
    assert "test_dup," in out and "test_missing." in out
    assert any("test_dup" in u and "more than one" in u for u in unres)
    assert any("test_missing" in u for u in unres)
    assert ev.linkify_markdown(out, i) == out


def test_code_spans_and_html(tmp_path):
    i = idx(tmp_path)
    md = ev.linkify_markdown("`scripts/tool.py` and `x = 1` and `test_one`\n```\nGate 1\n```\n", i)
    assert '[`scripts/tool.py`](../../scripts/tool.py "Does the tool thing.")' in md
    assert "`x = 1`" in md and "```\nGate 1\n```" in md
    doc = ('<h2>Gate 1</h2><p>Gate 1 <code>scripts/tool.py</code> 2026-10-14T10:00:00+00:00 '
           '<a href="#">Gate 2</a></p>')
    out = ev.linkify_html(doc, i)
    assert "<h2>Gate 1</h2>" in out
    assert '<a href="../../board/GATES.md#gate-1--engines-week-1" title="Gate 1 — engines (week 1)">Gate 1</a>' in out
    assert '<a href="../../scripts/tool.py" title="Does the tool thing."><code>scripts/tool.py</code></a>' in out
    assert 'href="../../ledger/runs.csv?plain=1#L2"' in out
    assert '<a href="#">Gate 2</a>' in out
    assert ev.linkify_html(out, i) == out


def test_hover_escaping(tmp_path):
    root = make_root(tmp_path)
    (root / "scripts" / "q.py").write_text('"""Say "hi" | <b> & go."""\n', encoding="utf-8")
    i = ev.Index(root, "../..")
    md = ev.linkify_markdown("scripts/q.py\n", i)
    assert 'Say \\"hi\\" \\| <b> & go.' in md
    h = ev.linkify_html("<p>scripts/q.py</p>", i)
    assert 'title="Say &quot;hi&quot; | &lt;b&gt; &amp; go."' in h


def test_real_repo_ledger_lines_match_timestamps():
    i = ev.Index(REPO, "../..")
    lines = (REPO / "ledger" / "runs.csv").read_text(encoding="utf-8").splitlines()
    for ts in i.ledger_ts[:: max(1, len(i.ledger_ts) // 15)]:
        href, _title, _n = i.ledger_ref(ts)
        n = int(href.rsplit("#L", 1)[1])
        assert lines[n - 1].startswith(ts)
