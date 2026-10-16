"""Give every task line on the board a GitHub anchor so reports can link to it.

Inserts `<a id="t042"></a>` (lowercase id) right after the checkbox of every task line in
board/TASKS.md and board/CYCLES.md that lacks one:

    - [ ] <a id="t042"></a>T042 · owner · task ...

The checkbox stays first, so GitHub still renders a task list. Idempotent. `--check` changes
nothing and exits 1 if any anchor is missing. Standard library only. Run by plan-sync.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD_FILES = ("TASKS.md", "CYCLES.md")
LINE_RE = re.compile(r"^(?P<box>\s*- \[[ xX~]\] )(?P<anchor><a id=\"[^\"]*\"></a>)?(?P<id>T\d+[a-z]?)(?=\s)")


def anchor_text(task_id: str) -> str:
    return f'<a id="{task_id.lower()}"></a>'


def add_anchors(text: str) -> tuple[str, list[str]]:
    """Return (new text, ids that were missing an anchor). A wrong anchor is corrected."""
    changed: list[str] = []
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = LINE_RE.match(line)
        if m and m["anchor"] != anchor_text(m["id"]):
            changed.append(m["id"])
            line = m["box"] + anchor_text(m["id"]) + line[m.end("anchor") if m["anchor"] else m.end("box"):]
        out.append(line)
    return "".join(out), changed


def process(root: Path, check: bool) -> list[str]:
    missing: list[str] = []
    for name in BOARD_FILES:
        p = root / "board" / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        new, changed = add_anchors(text)
        missing += [f"{name}: {t}" for t in changed]
        if changed and not check:
            p.write_text(new, encoding="utf-8")
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--check", action="store_true", help="exit 1 if any task line lacks its anchor")
    a = ap.parse_args(argv)
    missing = process(a.root, a.check)
    if a.check:
        for m in missing:
            print(f"missing anchor: {m}")
        return 1 if missing else 0
    print(f"anchors added: {len(missing)}" + (f" ({', '.join(missing)})" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
