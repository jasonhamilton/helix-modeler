"""Task anchors on the board, task links in reports and hand-written docs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ba, tl, lt, sr = (_load(n) for n in ("board_anchors", "task_links", "link_tasks", "status_report"))

TASKS = """# Task board
- [x] T042 · lead · Play-along user-DI capture. More words here · blocked by T001 · mode 2
- [ ] T018n · native-engine · Hands-off Native loading (human approved): evaluate things · mode 2
- [~] T020a · x · A "quoted" | piped name · mode 1
"""
CYCLES = "- [x] T001 · lead · Freeze engine interface v0 (`contracts/x.md`) · mode 1\n"


def make_root(tmp_path: Path) -> Path:
    (tmp_path / "board").mkdir()
    (tmp_path / "board" / "TASKS.md").write_text(TASKS, encoding="utf-8")
    (tmp_path / "board" / "CYCLES.md").write_text(CYCLES, encoding="utf-8")
    return tmp_path


def test_anchors_inserted_idempotent_and_checkbox_first(tmp_path):
    root = make_root(tmp_path)
    assert ba.main(["--root", str(root), "--check"]) == 1
    assert ba.main(["--root", str(root)]) == 0
    text = (root / "board" / "TASKS.md").read_text(encoding="utf-8")
    assert '- [x] <a id="t042"></a>T042 · lead' in text
    assert '- [ ] <a id="t018n"></a>T018n ·' in text
    assert '- [~] <a id="t020a"></a>T020a ·' in text
    assert '<a id="t001"></a>T001' in (root / "board" / "CYCLES.md").read_text(encoding="utf-8")
    assert ba.main(["--root", str(root), "--check"]) == 0
    ba.main(["--root", str(root)])
    assert (root / "board" / "TASKS.md").read_text(encoding="utf-8") == text


def test_real_board_has_every_anchor_and_parsers_read_it():
    assert ba.main(["--root", str(REPO), "--check"]) == 0
    text = (REPO / "board" / "TASKS.md").read_text(encoding="utf-8")
    parsed = sr.parse_tasks(text)
    assert len(parsed) == sum(1 for ln in text.splitlines() if ln.startswith("- ["))
    assert parsed["T001"] == "x"
    assert set(tl.parse_board(REPO)) >= set(parsed)


def test_parse_board_titles_and_archive_fallback(tmp_path):
    root = make_root(tmp_path)
    ba.main(["--root", str(root)])
    idx = tl.parse_board(root)
    assert idx["T042"] == {"title": "Play-along user-DI capture", "file": "TASKS.md"}
    assert idx["T018n"]["title"] == "Hands-off Native loading"
    assert idx["T001"] == {"title": "Freeze engine interface v0", "file": "CYCLES.md"}
    assert sr.parse_tasks((root / "board" / "TASKS.md").read_text(encoding="utf-8"))["T020a"] == "~"


def test_markdown_and_html_links(tmp_path):
    root = make_root(tmp_path)
    idx = tl.parse_board(root)
    rel = tl.rel_prefix(root, root / "reports" / "status")
    assert rel == "../../board"
    assert tl.link_md("T042", idx, rel) == \
        '[T042](../../board/TASKS.md#t042 "Play-along user-DI capture")'
    assert tl.link_html("T042", idx, rel) == \
        '<a href="../../board/TASKS.md#t042" title="Play-along user-DI capture">T042</a>'
    assert tl.link_md("T001", idx, rel).startswith("[T001](../../board/CYCLES.md#t001 ")
    assert '\\"quoted\\" \\| piped' in tl.link_md("T020a", idx, rel)
    assert 'title="A &quot;quoted&quot; | piped name"' in tl.link_html("T020a", idx, rel)
    assert tl.rel_prefix(root, root / "docs" / "notes") == "../../board"
    assert tl.rel_prefix(root, root / "docs") == "../board"


def test_linkify_skips_code_links_files_and_reports_unknown():
    idx = {"T042": {"title": "Name", "file": "TASKS.md"}}
    unknown: set[str] = set()
    md = ("T042 and T999 | `T042` | [T042](x.md) | docs/notes/T042-spike.md\n"
          "```\nT042\n```\n")
    out = tl.linkify_markdown(md, idx, "../board", unknown)
    first = out.splitlines()[0]
    assert first.startswith('[T042](../board/TASKS.md#t042 "Name") and T999 | `T042` | [T042](x.md)')
    assert first.endswith("docs/notes/T042-spike.md")
    assert "```\nT042\n```" in out
    assert unknown == {"T999"}
    assert tl.linkify_markdown(out, idx, "../board") == out
    html_in = '<p>T042 <a href="#">T042</a> <code>T042</code> T999</p><title>T042</title>'
    h = tl.linkify_html(html_in, idx, "../board", set())
    assert h.count('title="Name"') == 1 and h.count(">T042<") == 4
    assert tl.linkify_html(h, idx, "../board") == h


def test_link_tasks_cli_rewrites_idempotently_and_spares_decisions(tmp_path):
    root = make_root(tmp_path)
    ba.main(["--root", str(root)])
    (root / "docs" / "notes").mkdir(parents=True)
    note = root / "docs" / "notes" / "n.md"
    note.write_text("See T042 and T777.\n", encoding="utf-8")
    dec = root / "docs" / "DECISIONS.md"
    dec.write_text("T042 decided.\n", encoding="utf-8")
    assert lt.main(["--root", str(root), "--check"]) == 1
    assert lt.main(["--root", str(root)]) == 0
    assert note.read_text(encoding="utf-8") == \
        'See [T042](../../board/TASKS.md#t042 "Play-along user-DI capture") and T777.\n'
    assert dec.read_text(encoding="utf-8") == "T042 decided.\n"
    assert lt.main(["--root", str(root), "--check"]) == 0


def test_real_docs_are_linked():
    assert lt.main(["--root", str(REPO), "--check"]) == 0
