"""Turn task ids into links to their board anchors, with the task's name as hover text.

Markdown: [T042](../../board/TASKS.md#t042 "Play-along user-DI capture")
HTML:     <a href="../../board/TASKS.md#t042" title="Play-along user-DI capture">T042</a>

The title is the task's short name from its board line: the text after the owner, cut at the
first clause end. Tasks archived to board/CYCLES.md link there. Unknown ids stay plain text and
are collected so the caller can report them. Standard library only.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

LINE_RE = re.compile(r"^\s*- \[[ xX~]\] (?:<a id=\"[^\"]*\"></a>)?(?P<id>T\d+[a-z]?) · (?P<rest>.*)$")
# A task id in running text; not part of a file name (T003-stomp-spike.md) or a path.
ID_RE = re.compile(r"(?<![\w/.])T\d{3}[a-z]?(?!\w|-[a-z]|\.\w|/)")
MAX_TITLE = 70
MIN_TITLE = 20  # a clause break earlier than this leaves too short a name


def short_title(rest: str) -> str:
    """'owner · task text · blocked by ...' -> a short name for the task."""
    fields = rest.split(" · ")
    text = fields[1] if len(fields) > 1 else fields[0]
    text = text.replace("`", "")
    text = re.sub(r"^(\w+) \([^)]*\):", r"\1:", text)  # 'Spike (human approved):' -> 'Spike:'
    for cut in re.finditer(r"(\. |; |: |\? | \(|\.$|\?$)", text):
        if cut.start() >= MIN_TITLE:
            text = text[:cut.start()] + ("?" if cut.group(1).startswith("?") else "")
            break
    text = text.strip()
    if len(text) > MAX_TITLE:
        text = text[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(",:") + "…"
    return text


def parse_board(root: Path) -> dict[str, dict]:
    """task id -> {'title', 'file'}; board/TASKS.md wins, board/CYCLES.md fills in archived ids."""
    index: dict[str, dict] = {}
    for name in ("TASKS.md", "CYCLES.md"):
        p = root / "board" / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            m = LINE_RE.match(line)
            if m and m["id"] not in index:
                index[m["id"]] = {"title": short_title(m["rest"]), "file": name}
    return index


def rel_prefix(root: Path, from_dir: Path) -> str:
    """Relative path from a document's folder to the repository's board folder."""
    return os.path.relpath(root.resolve() / "board", from_dir.resolve()).replace(os.sep, "/")


def href(tid: str, index: dict, board_rel: str) -> str:
    return f"{board_rel}/{index[tid]['file']}#{tid.lower()}"


def link_md(tid: str, index: dict, board_rel: str) -> str:
    if tid not in index:
        return tid
    title = index[tid]["title"].replace("\\", "\\\\").replace('"', '\\"').replace("|", "\\|")
    return f'[{tid}]({href(tid, index, board_rel)} "{title}")'


def link_html(tid: str, index: dict, board_rel: str) -> str:
    if tid not in index:
        return tid
    return (f'<a href="{href(tid, index, board_rel)}" '
            f'title="{html.escape(index[tid]["title"], quote=True)}">{tid}</a>')


_MD_PROTECTED = re.compile(
    r"`+[^`]*`+"                                    # inline code
    r"|\[[^\]]*\]\((?:[^()\"]|\"(?:[^\"\\]|\\.)*\")*\)"   # existing inline link (title may hold \\")
    r"|\[[^\]]*\]\[[^\]]*\]"                        # reference link
    r"|<a\b[^>]*>.*?</a>"                           # html anchor
    r"|<[^>]+>"                                     # any other tag
    r"|https?://\S+")                               # bare url


def linkify_markdown(text: str, index: dict, board_rel: str, unknown: set[str] | None = None) -> str:
    """Replace bare task ids by links, leaving code, existing links and urls alone."""
    def sub_plain(chunk: str) -> str:
        def one(m: re.Match) -> str:
            if m.group(0) not in index:
                if unknown is not None:
                    unknown.add(m.group(0))
                return m.group(0)
            return link_md(m.group(0), index, board_rel)
        return ID_RE.sub(one, chunk)

    out: list[str] = []
    fence = False
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
            out.append(line)
            continue
        if fence or re.match(r"^(    |\t)", line) and not line.lstrip().startswith(("-", "*", "|")):
            out.append(line)
            continue
        pos, parts = 0, []
        for m in _MD_PROTECTED.finditer(line):
            parts.append(sub_plain(line[pos:m.start()]))
            parts.append(m.group(0))
            pos = m.end()
        parts.append(sub_plain(line[pos:]))
        out.append("".join(parts))
    return "".join(out)


_SKIP_TAGS = ("a", "code", "pre", "script", "style", "title", "textarea")


def linkify_html(doc: str, index: dict, board_rel: str, unknown: set[str] | None = None) -> str:
    """Link task ids in the text nodes of an HTML document (not in tags, links, code, scripts)."""
    out: list[str] = []
    depth = 0
    for tok in re.split(r"(<!--.*?-->|<[^>]*>)", doc, flags=re.DOTALL):
        if tok.startswith("<") and tok.endswith(">"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", tok)
            if m and m[2].lower() in _SKIP_TAGS and not tok.endswith("/>"):
                depth = max(0, depth + (-1 if m[1] else 1))
            out.append(tok)
        elif depth or not tok:
            out.append(tok)
        else:
            def one(m: re.Match) -> str:
                if m.group(0) not in index:
                    if unknown is not None:
                        unknown.add(m.group(0))
                    return m.group(0)
                return link_html(m.group(0), index, board_rel)
            out.append(ID_RE.sub(one, tok))
    return "".join(out)
