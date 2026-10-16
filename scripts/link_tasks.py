"""Rewrite bare task ids, ledger timestamps and DECISIONS citations in hand-written Markdown docs
into links with hover text.

Default files: docs/notes/*.md, docs/TONE-COMPARISON.md, board/CYCLES.md. Idempotent; skips code
blocks, inline code, existing links and urls. docs/DECISIONS.md (append-only) is never touched.
`--check` changes nothing and exits 1 if any file would change. Unknown ids are listed and left
as plain text. Standard library only. Run by plan-sync.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evidence_links as ev
import task_links as tl

ROOT = Path(__file__).resolve().parents[1]
NEVER = {"DECISIONS.md"}


def default_files(root: Path) -> list[Path]:
    files = sorted((root / "docs" / "notes").glob("*.md"))
    files += [root / "docs" / "TONE-COMPARISON.md", root / "board" / "CYCLES.md"]
    return [f for f in files if f.exists()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("files", nargs="*", type=Path)
    a = ap.parse_args(argv)
    index = tl.parse_board(a.root)
    files = a.files or default_files(a.root)
    unknown: set[str] = set()
    changed: list[Path] = []
    for f in files:
        if f.name in NEVER:
            continue
        text = f.read_text(encoding="utf-8")
        board_rel = tl.rel_prefix(a.root, f.resolve().parent)
        eidx = ev.Index(a.root, board_rel[:-len("/board")], frozenset({"ledger", "decision"}))
        new = ev.linkify_markdown(text, eidx, unknown)
        new = tl.linkify_markdown(new, index, board_rel, unknown)
        if new != text:
            changed.append(f)
            if not a.check:
                f.write_text(new, encoding="utf-8")
    for f in changed:
        print(("would change " if a.check else "linked ") + str(f))
    if unknown:
        print("unknown task ids / unresolved citations (left as text): " + ", ".join(sorted(unknown)))
    return 1 if (a.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
