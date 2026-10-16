"""Turn evidence citations into links with hover text, in Markdown and HTML.

Cited things and where they link (all relative to the document's own folder):
  ledger rows   `2026-10-14T17:33:57+00:00` (or a bare `23:47` after one)  -> ledger/runs.csv?plain=1#L<line>
  decisions     `DECISIONS 2026-10-15 host memory`                          -> docs/DECISIONS.md#<heading slug>
  gates         `Gate 2`                                                    -> board/GATES.md#<heading slug>
  files         `scripts/x.py`, `docs/notes/y.md` (also in code spans)      -> the file, hover = what it is
  note section  `docs/notes/y.md 'Heading'`                                 -> the file at that heading
  tests         `tests/test_x.py::test_name` or a unique `test_name`        -> tests/test_x.py?plain=1#L<line>

Anything that looks like a ledger row, decision, gate or test but cannot be resolved stays plain
text and is returned in `unresolved` so the caller can list it as a discrepancy. Standard
library only. Idempotent: text inside existing links and code blocks is left alone.
"""

from __future__ import annotations

import ast
import csv
import html
import re
import subprocess
import urllib.parse
from pathlib import Path

TS_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00)?"
LEDGER_RE = re.compile(
    rf"(?P<ts>{TS_RE})(?P<more>(?:(?:,| and|, and) \d{{2}}:\d{{2}}(?::\d{{2}})?(?: x\d+)?)*)(?P<x> x\d+)?")
MORE_RE = re.compile(r"(?P<sep>,|, and| and) (?P<t>\d{2}:\d{2}(?::\d{2})?)(?P<x> x\d+)?")
DEC_RE = re.compile(r"DECISIONS (?P<d>\d{4}-\d{2}-\d{2})(?P<label>(?:,? [\w\-/'`]+){0,8})")
GATE_RE = re.compile(r"\bGate (?P<n>\d+)\b")
TEST_RE = re.compile(r"(?<![\w/.:-])test_[a-z0-9_]+(?![\w*/]|\.\w)")
PATH_RE = re.compile(
    r"(?<![\w/.@-])(?:docs|scripts|src|tests|contracts|board|reports|ledger|fixtures|\.claude)"
    r"/[\w./-]*\w(?:::test_\w+)?(?:(?:,| in)? (?:section )?['\"‘“](?P<sec>[^'\"’”]{3,80})['\"’”])?")
ALL_KINDS = frozenset({"ledger", "decision", "gate", "path", "test"})
UNLINKED_PREFIXES = ("cache/", "data/", "fixtures/golden/")
STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "is"}


def md_title(t: str) -> str:
    return " ".join(t.split()).replace("\\", "\\\\").replace('"', '\\"').replace("|", "\\|")


def slug_headings(text: str) -> list[tuple[str, str]]:
    """(heading text, GitHub anchor) for every heading outside code fences. Duplicates get -1, -2."""
    used: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    fence = False
    for line in text.splitlines():
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
            continue
        m = None if fence else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m[1]).replace("`", "")
        base = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        n = used.get(base, 0)
        used[base] = n + 1
        out.append((heading, base if n == 0 else f"{base}-{n}"))
    return out


def words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP}


def file_description(root: Path, rel: str) -> str:
    """What a file is: its first heading (Markdown) or docstring line (Python), else its path."""
    p = root / rel
    try:
        if p.suffix == ".md":
            h = slug_headings(p.read_text(encoding="utf-8"))
            return h[0][0] if h else rel
        if p.suffix == ".py":
            doc = ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))
            if doc:
                return doc.strip().splitlines()[0][:110]
    except (OSError, SyntaxError, UnicodeDecodeError):
        pass
    return rel


class Index:
    """What can be cited, read once from the repository."""

    def __init__(self, root: Path, root_rel: str, kinds: frozenset[str] = ALL_KINDS) -> None:
        self.kinds = kinds
        self.root = root.resolve()
        self.rel = root_rel.rstrip("/")
        self.ledger: dict[str, list[tuple[int, list[str]]]] = {}
        self.ledger_ts: list[str] = []
        self._load_ledger()
        self.decisions = self._headings("docs/DECISIONS.md")
        self.gates = self._headings("board/GATES.md")
        self.tests: dict[str, list[tuple[str, int]]] = {}
        self._load_tests()
        self._ignored: dict[str, bool] = {}
        self._heads: dict[str, list[tuple[str, str]]] = {}

    # ---- loading
    def _load_ledger(self) -> None:
        p = self.root / "ledger" / "runs.csv"
        if not p.exists():
            return
        with p.open(encoding="utf-8", newline="") as fh:
            rd = csv.reader(fh)
            prev = 0
            for i, row in enumerate(rd):
                start, prev = prev + 1, rd.line_num
                if i == 0 or not row:
                    continue
                self.ledger.setdefault(row[0], []).append((start, row))
        self.ledger_ts = list(self.ledger)

    def _headings(self, rel: str) -> list[tuple[str, str]]:
        p = self.root / rel
        return slug_headings(p.read_text(encoding="utf-8")) if p.exists() else []

    def _load_tests(self) -> None:
        d = self.root / "tests"
        for f in sorted(d.glob("*.py")) if d.exists() else []:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                m = re.match(r"\s*(?:async )?def (test_\w+)\(", line)
                if m:
                    self.tests.setdefault(m[1], []).append((f"tests/{f.name}", n))

    # ---- helpers
    def is_ignored(self, path: str) -> bool:
        if path not in self._ignored:
            try:
                r = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=self.root,
                                   capture_output=True, timeout=10, check=False)
                self._ignored[path] = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._ignored[path] = False
        return self._ignored[path]

    def note_headings(self, rel: str) -> list[tuple[str, str]]:
        if rel not in self._heads:
            self._heads[rel] = self._headings(rel)
        return self._heads[rel]

    def describe(self, rel: str) -> str:
        return file_description(self.root, rel)

    def file_ref(self, path: str) -> tuple[str, str] | None:
        clean = path.strip()
        if (not clean or clean.startswith(("/", "..")) or clean.startswith(UNLINKED_PREFIXES)):
            return None
        p = self.root / clean
        if not p.is_file() or self.is_ignored(clean):
            return None
        return f"{self.rel}/{urllib.parse.quote(clean)}", self.describe(clean)

    # ---- resolvers (each returns (href, title) or None)
    def ledger_ref(self, ts: str, prefix: bool = False) -> tuple[str, str, int] | None:
        keys = ([k for k in self.ledger_ts if k.startswith(ts)] if prefix
                else [k for k in self.ledger_ts if k == ts or k.startswith(ts)
                      or k.replace("+00:00", "") == ts])
        if not keys:
            return None
        rows = [r for k in keys for r in self.ledger[k]]
        line, row = rows[0]
        kind, engine, dataset, metric, value = row[3], row[4], row[5], row[6], row[7]
        title = f"{kind} · {engine} · {dataset} · {metric} = {value}"
        if len(rows) > 1:
            title += f" (first of {len(rows)} rows at this time)"
        return f"{self.rel}/ledger/runs.csv?plain=1#L{line}", title, len(rows)

    def decision_ref(self, date: str, label: str) -> tuple[str, str, str] | None:
        """(href, heading, kept label words) for 'DECISIONS <date> <label>'."""
        cands = [(h, s) for h, s in self.decisions if h.startswith(date)]
        if not cands:
            return None
        lw = [w for w in re.findall(r"[A-Za-z0-9]+", label)]
        lset = words(label)

        def score(h: str) -> tuple[float, float]:
            hwords = words(re.sub(r"^\d{4}-\d{2}-\d{2}", "", h))
            hit = lset & hwords
            weight = sum(3 if re.fullmatch(r"t\d{3}[a-z]?", w) else 1 for w in hit)
            return weight, len(hit) / max(len(hwords), 1)

        scored = sorted(((score(h), h, s) for h, s in cands), reverse=True)
        if len(cands) == 1 or (scored[0][0][0] >= 1 and scored[0][0] > scored[1][0]):
            best = scored[0]
        else:
            return None
        hw = words(re.sub(r"^\d{4}-\d{2}-\d{2}", "", best[1]))
        keep = 0
        for i, w in enumerate(lw):
            if w.lower() in hw:
                keep = i + 1
        return f"{self.rel}/docs/DECISIONS.md#{best[2]}", best[1], " ".join(lw[:keep])

    def gate_ref(self, n: str) -> tuple[str, str] | None:
        for h, s in self.gates:
            if re.match(rf"Gate {n}\b", h):
                return f"{self.rel}/board/GATES.md#{s}", h
        return None

    def test_ref(self, name: str, file: str | None = None) -> tuple[str, str] | None:
        hits = [h for h in self.tests.get(name, []) if file is None or h[0] == file]
        if len(hits) != 1:
            return None
        f, n = hits[0]
        return f"{self.rel}/{f}?plain=1#L{n}", name

    def section_ref(self, path: str, sec: str) -> tuple[str, str] | None:
        if not path.endswith(".md") or not (self.root / path).is_file():
            return None
        want = sec.strip().lower()
        for h, s in self.note_headings(path):
            if want in h.lower():
                return f"{self.rel}/{urllib.parse.quote(path)}#{s}", h
        return None


Span = tuple[int, int, str, str, str]  # start, end, text, href, title (+ suffix handled by caller)


def says_count(chunk: str, end: int) -> bool:
    """The text right after a timestamp already states a row count ('(4 rows', 'x4', '4 rows')."""
    return re.match(r"\s*[(\[]?\s*(?:x\d+|\d+ rows?\b|rows\b)", chunk[end:]) is not None


def find_spans(chunk: str, idx: Index, unresolved: set[str], allow_gate: bool = True,
               ) -> list[tuple[int, int, str, str, str, str]]:
    """Non-overlapping (start, end, shown text, href, title, trailing text) in plain prose."""
    spans: list[tuple[int, int, str, str, str, str]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b, *_ in spans)

    for m in LEDGER_RE.finditer(chunk) if "ledger" in idx.kinds else ():
        before = chunk[:m.start()]
        cited = re.search(r"ledger(?: rows?)?:? $", before) is not None
        ref = idx.ledger_ref(m["ts"])
        if ref is None:
            if cited:
                unresolved.add(f"ledger row {m['ts']} is cited but is not in ledger/runs.csv")
            continue
        href, title, n = ref
        if not free(m.start(), m.end()):
            continue
        tail = "" if (m["x"] or n == 1 or says_count(chunk, m.end("ts"))) else f" ({n} rows)"
        spans.append((m.start("ts"), m.end("ts"), m["ts"], href, title, tail))
        date = m["ts"][:10]
        for k in MORE_RE.finditer(m["more"]):
            full = f"{date}T{k['t']}"
            r2 = idx.ledger_ref(full, prefix=True)
            off = m.start("more") + k.start("t")
            if r2 is None:
                unresolved.add(f"ledger row {full} (cited as {k['t']}) is not in ledger/runs.csv")
                continue
            tail2 = ("" if (k["x"] or r2[2] == 1 or says_count(chunk, off + len(k["t"])))
                     else f" ({r2[2]} rows)")
            spans.append((off, off + len(k["t"]), k["t"], r2[0], r2[1], tail2))
    for m in DEC_RE.finditer(chunk) if "decision" in idx.kinds else ():
        r = idx.decision_ref(m["d"], m["label"])
        if r is None:
            unresolved.add(f"decision '{m.group(0).strip()}' matches no single heading in docs/DECISIONS.md")
            continue
        href, head, kept = r
        end = m.end("d")
        if kept:
            end = m.start("label") + m["label"].rindex(kept.split()[-1]) + len(kept.split()[-1])
        if free(m.start(), end):
            spans.append((m.start(), end, chunk[m.start():end], href, head, ""))
    if allow_gate and "gate" in idx.kinds:
        for m in GATE_RE.finditer(chunk):
            r = idx.gate_ref(m["n"])
            if r is None:
                unresolved.add(f"Gate {m['n']} is not a heading in board/GATES.md")
            elif free(m.start(), m.end()):
                spans.append((m.start(), m.end(), m.group(0), r[0], r[1], ""))
    for m in PATH_RE.finditer(chunk) if "path" in idx.kinds else ():
        raw = m.group(0)
        path = re.match(r"[\w./-]*\w", raw)[0]
        tm = re.match(r"::(test_\w+)", raw[len(path):])
        test = tm[1] if tm else ""
        if test:
            r = idx.test_ref(test, path)
            end = m.start() + len(path) + 2 + len(test)
            if r is None:
                unresolved.add(f"test {path}::{test} not found")
            elif free(m.start(), end):
                spans.append((m.start(), end, chunk[m.start():end], r[0], r[1], ""))
            continue
        end = m.start() + len(path)
        if m["sec"]:
            r = idx.section_ref(path, m["sec"])
            if r and free(m.start(), m.end()):
                spans.append((m.start(), m.end(), raw, r[0], r[1], ""))
                continue
        r = idx.file_ref(path)
        if r and free(m.start(), end):
            spans.append((m.start(), end, path, r[0], r[1], ""))
    for m in TEST_RE.finditer(chunk) if "test" in idx.kinds else ():
        if not free(m.start(), m.end()):
            continue
        r = idx.test_ref(m.group(0))
        if r:
            spans.append((m.start(), m.end(), m.group(0), r[0], r[1], ""))
        elif m.group(0) in idx.tests:
            unresolved.add(f"test {m.group(0)} exists in more than one test file; cite it as file::name")
        else:
            unresolved.add(f"test {m.group(0)} is cited but no such test function exists in tests/")
    return sorted(spans)


def code_ref(content: str, idx: Index) -> tuple[str, str] | None:
    """A whole code span that is a repo path, path::test or a unique test name."""
    c = content.strip()
    if not {"path", "test"} & idx.kinds:
        return None
    if "::" in c:
        f, _, t = c.partition("::")
        return idx.test_ref(t, f)
    if re.fullmatch(r"test_[a-z0-9_]+", c):
        return idx.test_ref(c)
    return idx.file_ref(c) if "/" in c else None


def _apply(chunk: str, spans: list, fmt) -> str:
    out, pos = [], 0
    for s, e, text, href, title, tail in spans:
        out.append(chunk[pos:s])
        out.append(fmt(text, href, title) + tail)
        pos = e
    out.append(chunk[pos:])
    return "".join(out)


def _md_fmt(text: str, href: str, title: str) -> str:
    return f'[{text}]({href} "{md_title(title)}")'


def _html_fmt(text: str, href: str, title: str) -> str:
    return f'<a href="{href}" title="{html.escape(title, quote=True)}">{text}</a>'


_MD_PROTECTED = re.compile(
    r"(?P<code>`+[^`]*`+)"
    r"|\[[^\]]*\]\((?:[^()\"]|\"[^\"]*\")*\)"
    r"|\[[^\]]*\]\[[^\]]*\]"
    r"|<a\b[^>]*>.*?</a>"
    r"|<[^>]+>"
    r"|https?://\S+")


def linkify_markdown(text: str, idx: Index, unresolved: set[str] | None = None) -> str:
    unres = unresolved if unresolved is not None else set()
    out: list[str] = []
    fence = False
    for line in text.splitlines(keepends=True):
        if re.match(r"\s*(```|~~~)", line):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        heading = line.startswith("#")
        pos, parts = 0, []

        def plain(chunk: str, heading: bool = heading) -> str:
            return _apply(chunk, find_spans(chunk, idx, unres, allow_gate=not heading), _md_fmt)

        for m in _MD_PROTECTED.finditer(line):
            parts.append(plain(line[pos:m.start()]))
            tok = m.group(0)
            if m["code"] and not heading:
                inner = tok.strip("`")
                r = code_ref(inner, idx)
                tok = _md_fmt(tok, r[0], r[1]) if r else tok
            parts.append(tok)
            pos = m.end()
        parts.append(plain(line[pos:]))
        out.append("".join(parts))
    return "".join(out)


_SKIP = ("a", "code", "pre", "script", "style", "title", "textarea")


def linkify_html(doc: str, idx: Index, unresolved: set[str] | None = None) -> str:
    unres = unresolved if unresolved is not None else set()
    toks = re.split(r"(<!--.*?-->|<[^>]*>)", doc, flags=re.DOTALL)
    out: list[str] = []
    depth = a_depth = head = 0
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.startswith("<") and tok.endswith(">"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", tok)
            if m:
                name, closing = m[2].lower(), bool(m[1])
                if (name == "code" and not closing and not a_depth and i + 2 < len(toks)
                        and toks[i + 2].lower() == "</code>"):
                    r = code_ref(html.unescape(toks[i + 1]), idx)
                    if r:
                        out.append(f'<a href="{r[0]}" title="{html.escape(r[1], quote=True)}">'
                                   f"{tok}{toks[i + 1]}{toks[i + 2]}</a>")
                        i += 3
                        continue
                step = -1 if closing else 1
                if name in _SKIP and not tok.endswith("/>"):
                    depth = max(0, depth + step)
                if name == "a":
                    a_depth = max(0, a_depth + step)
                if re.fullmatch(r"h[1-6]", name):
                    head = max(0, head + step)
            out.append(tok)
        elif depth or not tok:
            out.append(tok)
        else:
            out.append(_apply(tok, find_spans(tok, idx, unres, allow_gate=not head), _html_fmt))
        i += 1
    return "".join(out)


def collect_unresolved(obj: object, idx: Index) -> set[str]:
    """Unresolvable citations in every string of a report model section."""
    unres: set[str] = set()

    def walk(o: object) -> None:
        if isinstance(o, str):
            find_spans(o, idx, unres)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return unres
