"""local_site: link rewriting, broken-link detection, landing page, output confinement (T131)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import local_site as LS


def make_repo(tmp_path: Path, extra_link: str = "") -> Path:
    r = tmp_path
    (r / "board").mkdir()
    (r / "docs/notes").mkdir(parents=True)
    (r / "contracts").mkdir()
    (r / "ledger").mkdir()
    (r / "reports/status").mkdir(parents=True)
    (r / "reports/local/blocks").mkdir(parents=True)
    (r / "reports/local/ab/s1").mkdir(parents=True)
    (r / "reports/local/gate2").mkdir(parents=True)
    (r / "reports/local/preset-corpus").mkdir()
    (r / "ledger/runs.csv").write_text("a,b\n")
    (r / "README.md").write_text("# Readme\n\nThe project readme paragraph is here.\n")
    (r / "board/TASKS.md").write_text(
        '# Tasks\n\nThe task board lives here and is long enough.\n\n## Open items\n\n'
        '- <a id="t042"></a>T042 thing\n')
    (r / "docs/DECISIONS.md").write_text("# Decisions\n\nDecision log paragraph text here.\n\n## Same\n\n## Same\n")
    (r / "docs/notes/stompd.md").write_text(
        "# Stompd\n\nNotes about the helper process, long enough.\n\n"
        "[tasks](../../board/TASKS.md#t042) [dec](../DECISIONS.md#same-1) "
        "[ledger](../../ledger/runs.csv?plain=1#L12) [ext](https://example.com/a.md) "
        "[contract](../../contracts/board.md) [self](#stompd) " + extra_link + "\n")
    (r / "contracts/board.md").write_text("# Board contract\n\nContract paragraph with enough words.\n")
    (r / "reports/status/README.md").write_text("# Status\n\nStatus markdown rendering of the report.\n")
    (r / "reports/status/index.html").write_text("<html></html>")
    (r / "reports/local/blocks/index.html").write_text("<html></html>")
    (r / "reports/local/gate2/index.html").write_text("<html></html>")
    (r / "reports/local/ab/s1/index.html").write_text("<html></html>")
    (r / "reports/local/preset-corpus/README.md").write_text("# Corpus\n\nCorpus analysis paragraph here ok.\n")
    return r


def test_rewrite_md_to_html_keeps_anchor(tmp_path):
    r = make_repo(tmp_path)
    md = LS.discover(r)
    out = md["docs/notes/stompd.md"]
    got = LS.rewrite_href("../../board/TASKS.md#t042", "docs/notes/stompd.md", out, r, md)
    assert got == "../../board/TASKS.html#t042"
    assert LS.rewrite_href("../DECISIONS.md", "docs/notes/stompd.md", out, r, md) == "../DECISIONS.html"


def test_rewrite_repo_file_and_external(tmp_path):
    r = make_repo(tmp_path)
    md = LS.discover(r)
    out = md["docs/notes/stompd.md"]
    got = LS.rewrite_href("../../ledger/runs.csv?plain=1#L12", "docs/notes/stompd.md", out, r, md)
    assert got == "../../../../../ledger/runs.csv#L12"
    assert (out.parent / "../../../../../ledger/runs.csv").resolve().exists()
    assert LS.rewrite_href("https://example.com/a.md", "docs/notes/stompd.md", out, r, md) \
        == "https://example.com/a.md"
    assert LS.rewrite_href("#x", "docs/notes/stompd.md", out, r, md) == "#x"


def test_github_slugs_and_duplicates():
    assert LS.github_slug("2026-10-13 · Cycle 1 (mode 1)") == "2026-10-13--cycle-1-mode-1"
    _body, toc = LS.add_heading_ids("<h2>Same</h2><h2>Same</h2>")
    assert [t[1] for t in toc] == ["same", "same-1"]


def test_build_ok_no_broken_links(tmp_path):
    r = make_repo(tmp_path)
    LS.build(r)
    assert LS.build.last["broken"] == []
    assert (r / "reports/local/docs/board/TASKS.html").is_file()
    assert (r / "reports/local/docs/docs/notes/stompd.html").is_file()
    assert (r / "reports/local/docs/README.html").is_file()
    assert (r / "reports/local/docs/contracts/board.html").is_file()
    assert (r / "reports/local/preset-corpus/README.html").is_file()
    assert 'id="t042"' in (r / "reports/local/docs/board/TASKS.html").read_text()
    assert "broken links: 0" in (r / "reports/local/link_check.txt").read_text()


def test_broken_link_detected(tmp_path):
    r = make_repo(tmp_path, extra_link="[gone](../../scripts/nope.py) [gone2](../missing.md)")
    LS.build(r)
    broken = LS.build.last["broken"]
    assert len(broken) == 2
    assert any("nope.py" in b for b in broken)
    assert "broken links: 2" in (r / "reports/local/link_check.txt").read_text()
    assert LS.main(["build", "--root", str(r)]) == 1


def test_landing_lists_discovered_reports(tmp_path):
    r = make_repo(tmp_path)
    LS.build(r)
    page = (r / "reports/local/index.html").read_text()
    for needle in ('href="../status/index.html"', 'href="gate2/index.html"', 'href="blocks/index.html"',
                   'href="preset-corpus/README.html"', 'href="ab/s1/index.html"',
                   'href="docs/board/TASKS.html"', 'href="docs/docs/notes/stompd.html"',
                   'href="docs/contracts/board.html"', "Last updated"):
        assert needle in page, needle
    for group in ("Board", "Plan and decisions", "Notes", "Contracts"):
        assert f"<h3>{group}</h3>" in page


def test_writes_only_under_reports_local(tmp_path):
    r = make_repo(tmp_path)
    before = {p for p in r.rglob("*") if p.is_file() and LS.LOCAL.as_posix() not in p.relative_to(r).as_posix()}
    snap = {p: p.read_bytes() for p in before}
    LS.build(r)
    for p, data in snap.items():
        assert p.read_bytes() == data
    after = {p for p in r.rglob("*") if p.is_file()}
    for p in after - before:
        assert (r / "reports/local") in p.parents
    with pytest.raises(ValueError):
        LS._write(r, r / "docs/evil.html", "x")
    with pytest.raises(ValueError):
        LS._write(r, r / "reports/local/../status/evil.html", "x")
