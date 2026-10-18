"""local_site: a local, offline HTML site for the project's reports and Markdown docs (owner: status-report).

  python scripts/local_site.py build [--root DIR]

Renders every Markdown file under board/, docs/ and contracts/, the root README.md, the public
status report README and the local analysis READMEs (reports/local/*/README.md) to HTML, rewrites
their links so they work when opened from disk, writes a landing page at reports/local/index.html
and a link report at reports/local/link_check.txt.

Writes ONLY under reports/local/ (gitignored, never committed). Every write goes through
`_write`, which refuses any path outside that folder. Needs the `markdown` package (dev-only);
without it a small built-in converter is used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import posixpath
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:  # dev-only dependency
    import markdown as _markdown
except ImportError:  # pragma: no cover - exercised only without the package
    _markdown = None

LOCAL = Path("reports") / "local"
DOCS_OUT = LOCAL / "docs"
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
ATTR = re.compile(r'(<(?:a|img)\b[^>]*?\b(?:href|src)=")([^"]*)(")', re.IGNORECASE)
HEADING = re.compile(r"<h([1-6])>(.*?)</h\1>", re.DOTALL)
IDS = re.compile(r'\b(?:id|name)="([^"]+)"')

CSS = """:root{--bg:#f6f7f9;--fg:#1b1f24;--muted:#555d68;--card:#fff;--line:#d3d8de;--accent:#0b5cad;
--neu-bg:#e7eaee;--ok:#0d5f3f}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--fg:#e9ebee;--muted:#a6adb7;--card:#1b1e24;
--line:#363c45;--accent:#86bdf2;--neu-bg:#2c3139;--ok:#86e3bf}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
overflow-wrap:anywhere}
a{color:var(--accent)}
code{font-size:.85em;background:var(--neu-bg);padding:1px 5px;border-radius:4px}
pre{background:var(--neu-bg);padding:10px 12px;border-radius:6px;overflow:auto}pre code{padding:0;background:none}
header.top{background:var(--card);border-bottom:1px solid var(--line);padding:12px 20px}
header.top .crumb{font-size:.92rem}header.top .meta{color:var(--muted);font-size:.85rem;margin-top:4px}
.layout{display:flex;gap:24px;align-items:flex-start;max-width:1320px;margin:0 auto;padding:16px 20px 48px}
aside.side{flex:0 0 250px;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto;
font-size:.85rem;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
aside.side ul{list-style:none;margin:0;padding:0}aside.side li{margin:2px 0}
aside.side li.l3{padding-left:12px}aside.side li.l4{padding-left:24px}
main{flex:1 1 auto;min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 24px 24px}
main table{border-collapse:collapse;display:block;overflow:auto}
main th,main td{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
main h1,main h2{border-bottom:1px solid var(--line);padding-bottom:4px}
blockquote{margin:0;padding:0 14px;border-left:4px solid var(--line);color:var(--muted)}
.wrap{max-width:1020px;margin:0 auto;padding:0 20px 48px}
.wrap h2{margin:2em 0 .5em;border-bottom:2px solid var(--line);padding-bottom:4px}
.wrap h3{margin:1.3em 0 .3em;font-size:1.02rem;color:var(--muted)}
.wrap table{width:100%;border-collapse:collapse;background:var(--card)}
.wrap td,.wrap th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
.wrap td.t{white-space:nowrap;color:var(--muted);font-size:.82rem}
.muted{color:var(--muted)}
@media(max-width:800px){.layout{flex-direction:column}aside.side{position:static;max-height:none;flex:none;width:100%}}
"""


# ------------------------------------------------------------------ safety

def _write(root: Path, path: Path, text: str) -> None:
    """Write text, but only below <root>/reports/local."""
    base = (root / LOCAL).resolve()
    target = path.resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"local_site refuses to write outside {base}: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ discovery

def discover(root: Path) -> dict[str, Path]:
    """Map source path (posix, relative to root) -> output path (absolute) for every Markdown file."""
    out: dict[str, Path] = {}

    def add_docs(rel: str) -> None:
        out[rel] = root / DOCS_OUT / (rel[:-3] + ".html")

    for top in ("board", "docs", "contracts"):
        base = root / top
        if base.is_dir():
            for p in sorted(base.rglob("*.md")):
                add_docs(p.relative_to(root).as_posix())
    if (root / "README.md").is_file():
        add_docs("README.md")
    if (root / "reports/status/README.md").is_file():
        add_docs("reports/status/README.md")
    local = root / LOCAL
    if local.is_dir():
        for d in sorted(p for p in local.iterdir() if p.is_dir() and p.name != "docs"):
            readme = d / "README.md"
            if readme.is_file():
                rel = readme.relative_to(root).as_posix()
                out[rel] = d / "README.html"
    return out


# ------------------------------------------------------------------ markdown

def github_slug(text: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", text)).strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _prep(text: str) -> str:
    """GitHub lets a list interrupt a paragraph; python-markdown does not. Insert the blank line."""
    out: list[str] = []
    fence = False
    prev = ""
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif (not fence and LIST_ITEM.match(line) and prev.strip()
              and not LIST_ITEM.match(prev) and not prev.startswith((" ", "\t", ">", "|"))):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def _mini(text: str) -> str:  # pragma: no cover - fallback only
    parts, para, code = [], [], None
    inline = lambda s: re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(s, quote=False))
    inline_links = lambda s: re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", r'<a href="\2">\1</a>', s)
    for line in text.split("\n"):
        if line.startswith("```"):
            if code is None:
                code = []
            else:
                parts.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m or not line.strip():
            if para:
                parts.append("<p>" + inline_links(inline(" ".join(para))) + "</p>")
                para = []
            if m:
                n = len(m.group(1))
                parts.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        else:
            para.append(line.strip())
    if para:
        parts.append("<p>" + inline_links(inline(" ".join(para))) + "</p>")
    return "\n".join(parts)


def render_markdown(text: str) -> str:
    if _markdown is None:  # pragma: no cover
        return _mini(text)
    md = _markdown.Markdown(extensions=["tables", "fenced_code", "attr_list", "sane_lists"])
    return md.convert(_prep(text))


def add_heading_ids(body: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Give headings GitHub-style ids (duplicates get -1, -2). Returns html and (level, id, text)."""
    seen: dict[str, int] = {}
    toc: list[tuple[int, str, str]] = []

    def sub(m: re.Match) -> str:
        level, inner = int(m.group(1)), m.group(2)
        base = github_slug(inner)
        n = seen.get(base, 0)
        seen[base] = n + 1
        ident = base if n == 0 else f"{base}-{n}"
        toc.append((level, ident, html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()))
        return f'<h{level} id="{ident}">{inner}</h{level}>'

    return HEADING.sub(sub, body), toc


# ------------------------------------------------------------------ links

def rewrite_href(href: str, src_rel: str, out_path: Path, root: Path,
                 md_map: dict[str, Path]) -> str:
    """Return the offline-safe href for one link found in the Markdown source `src_rel`."""
    raw = html.unescape(href)
    if not raw or raw.startswith(("#", "//")) or SCHEME.match(raw):
        return href
    parts = urlsplit(raw)
    path, frag = unquote(parts.path), parts.fragment
    if not path:
        return href
    if path.startswith("/"):
        target = posixpath.normpath(path.lstrip("/"))
    else:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), path))
    if target.startswith("../") or target == "..":
        return href  # outside the repo: left alone, the link check reports it
    if target in md_map:
        dest = md_map[target]
    else:
        dest = root / target
    rel = os.path.relpath(dest, out_path.parent).replace(os.sep, "/")
    if path.endswith("/") and not rel.endswith("/"):
        rel += "/"
    new = quote(rel, safe="/%._-~")
    if frag:
        new += "#" + frag
    return html.escape(new, quote=True)


def rewrite_links(body: str, src_rel: str, out_path: Path, root: Path, md_map: dict[str, Path]) -> str:
    return ATTR.sub(lambda m: m.group(1) + rewrite_href(m.group(2), src_rel, out_path, root, md_map)
                    + m.group(3), body)


def check_links(root: Path, files: list[Path]) -> tuple[list[str], list[str], int]:
    """Check every relative href/src in the generated files. Returns (broken, anchor_warnings, links)."""
    broken: list[str] = []
    warn: list[str] = []
    n = 0
    id_cache: dict[Path, set[str]] = {}

    def ids(p: Path) -> set[str]:
        if p not in id_cache:
            try:
                id_cache[p] = set(IDS.findall(p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                id_cache[p] = set()
        return id_cache[p]

    for f in files:
        text = f.read_text(encoding="utf-8")
        shown = f.relative_to(root).as_posix()
        for m in ATTR.finditer(text):
            raw = html.unescape(m.group(2))
            if not raw or SCHEME.match(raw) or raw.startswith("//"):
                continue
            n += 1
            parts = urlsplit(raw)
            if not parts.path:
                if parts.fragment and parts.fragment not in ids(f) and not parts.fragment.startswith("L"):
                    warn.append(f"{shown}: #{parts.fragment} not on this page")
                continue
            if parts.path.startswith("/"):
                broken.append(f"{shown}: absolute path {raw}")
                continue
            target = (f.parent / unquote(parts.path)).resolve()
            if not target.exists():
                broken.append(f"{shown}: {raw}")
            elif (parts.fragment and target.suffix == ".html"
                  and not re.fullmatch(r"L\d+(-L\d+)?", parts.fragment)
                  and parts.fragment not in ids(target)):
                warn.append(f"{shown}: {raw} (no such anchor)")
    return broken, warn, n


# ------------------------------------------------------------------ pages

def _plain(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def describe(body: str) -> tuple[str, str]:
    """(title, one-line description) from the rendered body: first heading, first paragraph."""
    h = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", body, re.DOTALL)
    title = _plain(h.group(1)) if h else ""
    desc = ""
    for p in re.findall(r"<p>(.*?)</p>", body, re.DOTALL):
        t = _plain(p)
        if len(t) > 20:
            desc = t
            break
    if len(desc) > 190:
        desc = desc[:187].rsplit(" ", 1)[0] + "..."
    return title, desc


def page_html(*, title: str, crumbs: str, meta: str, toc: list[tuple[int, str, str]], body: str) -> str:
    side = ""
    items = [t for t in toc if t[0] <= 3 and t[2]]
    if len(items) > 1:
        side = ('<aside class="side"><strong>On this page</strong><ul>' + "".join(
            f'<li class="l{lv}"><a href="#{i}">{html.escape(tx)}</a></li>' for lv, i, tx in items)
            + "</ul></aside>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body>"
            f'<header class="top"><div class="crumb">{crumbs}</div><div class="meta">{meta}</div></header>'
            f'<div class="layout">{side}<main>{body}</main></div></body></html>\n')


def render_page(root: Path, src_rel: str, out_path: Path, md_map: dict[str, Path], stamp: str) -> dict:
    text = (root / src_rel).read_text(encoding="utf-8", errors="replace")
    body = render_markdown(text)
    body, toc = add_heading_ids(body)
    body = rewrite_links(body, src_rel, out_path, root, md_map)
    title, desc = describe(body)
    home = os.path.relpath(root / LOCAL / "index.html", out_path.parent).replace(os.sep, "/")
    src_href = html.escape(quote(os.path.relpath(root / src_rel, out_path.parent).replace(os.sep, "/"),
                                 safe="/%._-~"))
    crumbs = (f'<a href="{home}">Reports home</a> / ' + " / ".join(
        html.escape(p) for p in src_rel.split("/")))
    meta = (f'Source <a href="{src_href}"><code>{html.escape(src_rel)}</code></a> &middot; '
            f"generated {stamp} from {html.escape(src_rel)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write(root, out_path, page_html(title=title or src_rel, crumbs=crumbs, meta=meta, toc=toc, body=body))
    return {"src": src_rel, "out": out_path, "title": title or src_rel, "desc": desc}


def _mtime(p: Path) -> str:
    return dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _group(src: str) -> str:
    name = posixpath.basename(src)
    if src.startswith("board/"):
        return "Board"
    if src.startswith("contracts/"):
        return "Contracts"
    if src.startswith("docs/notes/"):
        return "Notes"
    if name in ("BENCHMARKING.md", "TONE-COMPARISON.md"):
        return "Benchmarking and comparison"
    if name in ("PLAN.md", "DECISIONS.md", "AGENT-TEAM.md", "STATUS-REPORT-SPEC.md", "DEV-ENV.md"):
        return "Plan and decisions"
    return "Other documents"


GROUP_ORDER = ["Board", "Plan and decisions", "Benchmarking and comparison", "Notes", "Contracts",
               "Other documents"]


def landing(root: Path, pages: list[dict], stamp: str) -> str:
    index = root / LOCAL / "index.html"

    def rel(p: Path) -> str:
        return html.escape(quote(os.path.relpath(p, index.parent).replace(os.sep, "/"), safe="/%._-~"))

    def row(label: str, p: Path, desc: str = "") -> str:
        d = f'<br><span class="muted">{html.escape(desc)}</span>' if desc else ""
        return (f'<tr><td><a href="{rel(p)}">{html.escape(label)}</a>{d}</td>'
                f'<td class="t">{_mtime(p)}</td></tr>')

    def table(rows: list[str]) -> str:
        return ("<table><tr><th>Report</th><th>File modified</th></tr>" + "".join(rows) + "</table>"
                if rows else '<p class="muted">Nothing found yet.</p>')

    by_src = {p["src"]: p for p in pages}
    sections: list[str] = []

    rows = []
    s = root / "reports/status/index.html"
    if s.is_file():
        rows.append(row("Project status report (HTML)", s, "The public dashboard: health, progress, readiness."))
    if "reports/status/README.md" in by_src:
        p = by_src["reports/status/README.md"]
        rows.append(row("Project status report (Markdown version, rendered)", p["out"], p["desc"]))
    sections.append("<h2>Status</h2>" + table(rows))

    g = root / LOCAL / "gate2/index.html"
    sections.append("<h2>Gate 2</h2>" + table(
        [row("Gate 2 methods report", g, "Plain-language report on the Gate 2 methods.")] if g.is_file() else []))

    rows = []
    local = root / LOCAL
    if local.is_dir():
        for d in sorted(p for p in local.iterdir() if p.is_dir()):
            if d.name in ("docs", "ab", "gate2"):
                continue
            if (d / "index.html").is_file():
                rows.append(row(f"{d.name} (report page)", d / "index.html"))
            src = f"{LOCAL.as_posix()}/{d.name}/README.md"
            if src in by_src:
                rows.append(row(f"{d.name} (README)", by_src[src]["out"], by_src[src]["desc"]))
    bench = root / "reports/benchmark"
    if bench.is_dir():
        for name in ("index.html",):
            if (bench / name).is_file():
                rows.append(row("benchmark (report page)", bench / name))
    sections.append("<h2>Analyses</h2>" + table(rows))

    groups: dict[str, list[dict]] = {}
    for p in pages:
        if p["src"].startswith(("reports/", "README.md")) and p["src"] != "README.md":
            continue
        groups.setdefault(_group(p["src"]), []).append(p)
    docs_html = ["<h2>Project documents</h2>"]
    for name in GROUP_ORDER:
        if name in groups:
            docs_html.append(f"<h3>{name}</h3>" + table(
                [row(f'{p["src"]} - {p["title"]}', p["out"], p["desc"]) for p in groups[name]]))
    sections.append("".join(docs_html))

    ab = sorted((root / LOCAL / "ab").glob("*/index.html")) if (root / LOCAL / "ab").is_dir() else []
    sections.append("<h2>A-B listening sessions</h2>" + table([row(p.parent.name, p) for p in ab]))

    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>HX Stomp Tone Matcher - local reports</title>'
            f"<style>{CSS}</style></head><body>"
            f'<header class="top"><strong>HX Stomp Tone Matcher - local reports and docs</strong>'
            f'<div class="meta">Last updated {stamp}. Local only (reports/local is gitignored). '
            f"Every link is relative: open this file straight from disk.</div></header>"
            f'<div class="wrap">{"".join(sections)}</div></body></html>\n')


# ------------------------------------------------------------------ build

def build(root: Path | None = None) -> str:
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    docs_dir = root / DOCS_OUT
    if docs_dir.is_dir():
        shutil.rmtree(docs_dir)  # only ever reports/local/docs
    md_map = discover(root)
    pages = [render_page(root, src, out, md_map, stamp) for src, out in md_map.items()]
    _write(root, root / LOCAL / "index.html", landing(root, pages, stamp))
    files = [p["out"] for p in pages] + [root / LOCAL / "index.html"]
    broken, warn, n = check_links(root, files)
    lines = [f"local_site link check {stamp}",
             f"pages rendered: {len(pages)} (+ landing page); relative links checked: {n}",
             f"broken links: {len(broken)}", *[f"  BROKEN {b}" for b in broken],
             f"anchor warnings (not failures): {len(warn)}", *[f"  warn {w}" for w in warn]]
    _write(root, root / LOCAL / "link_check.txt", "\n".join(lines) + "\n")
    build.last = {"pages": len(pages), "links": n, "broken": broken, "warnings": warn}  # type: ignore[attr-defined]
    return (f"{root / LOCAL / 'index.html'}: {len(pages)} pages, {n} links checked, "
            f"{len(broken)} broken, {len(warn)} anchor warnings (reports/local/link_check.txt)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["build"])
    ap.add_argument("--root", type=Path, default=None)
    a = ap.parse_args(argv)
    print(build(a.root))
    return 1 if build.last["broken"] else 0  # type: ignore[attr-defined]


if __name__ == "__main__":
    sys.exit(main())
