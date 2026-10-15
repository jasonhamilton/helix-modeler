"""Render the by-feature project status page (skill `status-report`).

Reads docs/features.json (the curated registry) plus live facts: task counts from
board/TASKS.md, ledger rows from ledger/runs.csv, gate checkboxes from board/GATES.md, the
git commit count, and (with --run-checks) pytest, ruff and scripts/licence_check.py.
Writes, from the same registry and facts, one self-contained HTML file
(reports/status/index.html: inline CSS, no external requests) and a GitHub-flavoured Markdown
page (reports/status/README.md, which GitHub renders when browsing the folder). Choose with
--format html,md (default both). Standard library only. Exits 2 with a message if the registry
is invalid.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATUSES = ("not-started", "designing", "prototype", "working", "proven")
EVIDENCE = ("none", "unit-tests", "proxy", "synthetic", "hardware", "real-songs", "ear")
HEALTH = ("ok", "partial", "blocked", "unknown")
STATUS_GLYPH = {"not-started": "○", "designing": "◔", "prototype": "◑", "working": "◕", "proven": "●"}
HEALTH_GLYPH = {"ok": "✓", "partial": "◐", "blocked": "✕", "unknown": "?"}
LIST_FIELDS = ("owners", "tasks", "todo", "needs_human", "decisions", "risks", "key_files",
               "blockers")
REQUIRED = ("id", "name", "purpose", "done_when", "status", "completion", "completion_reason",
            "functionality", "health", "evidence")


# --------------------------------------------------------------------------- registry


def load_registry(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"status_report: registry not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"status_report: registry is not valid JSON: {exc}") from None


def validate(reg: dict) -> list[str]:
    """Return a list of human-readable problems; empty means the registry is usable."""
    errs: list[str] = []
    if not isinstance(reg, dict):
        return ["registry must be a JSON object"]
    for key in ("headline", "cycle", "generated_for"):
        if not isinstance(reg.get(key), str) or not reg.get(key):
            errs.append(f"top-level '{key}' must be a non-empty string")
    if not isinstance(reg.get("discrepancies", []), list):
        errs.append("top-level 'discrepancies' must be a list")
    feats = reg.get("features")
    if not isinstance(feats, list) or not feats:
        errs.append("'features' must be a non-empty list")
        return errs
    seen: set[str] = set()
    for i, f in enumerate(feats):
        tag = f"feature #{i + 1}"
        if not isinstance(f, dict):
            errs.append(f"{tag}: must be an object")
            continue
        fid = f.get("id")
        tag = f"feature '{fid}'" if isinstance(fid, str) and fid else tag
        for k in REQUIRED:
            if k not in f:
                errs.append(f"{tag}: missing '{k}'")
        if isinstance(fid, str):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fid):
                errs.append(f"{tag}: id must be lowercase letters, digits and hyphens")
            if fid in seen:
                errs.append(f"{tag}: duplicate id")
            seen.add(fid)
        if f.get("status") not in STATUSES:
            errs.append(f"{tag}: unknown status {f.get('status')!r} (allowed: {', '.join(STATUSES)})")
        if f.get("health") not in HEALTH:
            errs.append(f"{tag}: unknown health {f.get('health')!r} (allowed: {', '.join(HEALTH)})")
        c = f.get("completion")
        if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 100:
            errs.append(f"{tag}: completion must be an integer 0-100, got {c!r}")
        ev = f.get("evidence")
        if not isinstance(ev, list):
            errs.append(f"{tag}: evidence must be a list")
        else:
            for j, item in enumerate(ev):
                if not isinstance(item, dict) or item.get("level") not in EVIDENCE:
                    lvl = item.get("level") if isinstance(item, dict) else item
                    errs.append(f"{tag}: evidence[{j}] has unknown level {lvl!r} "
                                f"(allowed: {', '.join(EVIDENCE)})")
                elif not isinstance(item.get("refs", []), list):
                    errs.append(f"{tag}: evidence[{j}].refs must be a list")
        for k in LIST_FIELDS:
            if not isinstance(f.get(k, []), list):
                errs.append(f"{tag}: '{k}' must be a list")
        for k in ("implemented", "challenges", "ledger"):
            v = f.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
                errs.append(f"{tag}: '{k}' must be a list of objects")
    return errs


def top_evidence(feature: dict) -> str:
    levels = [e["level"] for e in feature.get("evidence", []) if e.get("level") != "none"]
    return max(levels, key=EVIDENCE.index) if levels else "none"


# --------------------------------------------------------------------------- live facts

TASK_RE = re.compile(r"^- \[(?P<m>[ x~])\] (?P<id>T\d+[a-z]?) ")


def parse_tasks(text: str) -> dict[str, str]:
    """task id -> ' ' (open), '~' (in progress) or 'x' (done)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = TASK_RE.match(line)
        if m:
            out[m["id"]] = m["m"]
    return out


def parse_gates(text: str) -> list[dict]:
    gates: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        h = re.match(r"^## (Gate \d+) — (.*)$", line)
        if h:
            cur = {"name": h[1], "title": h[2].strip(), "done": 0, "total": 0, "signed": False,
                   "items": []}
            gates.append(cur)
            continue
        m = re.match(r"^- \[(?P<m>[ x])\] (?P<t>.*)$", line)
        if m and cur is not None:
            ticked = m["m"] == "x"
            is_sign = m["t"].lstrip().lower().startswith("human sign-off")
            cur["total"] += 1
            cur["done"] += ticked
            if is_sign:
                cur["signed"] = ticked
                cur["sign_text"] = m["t"][:160]
            cur["items"].append((ticked, m["t"]))
    return gates


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ledger_matches(rows: list[dict], flt: dict) -> list[dict]:
    keys = {"kind": "kind", "engine": "engine", "dataset": "dataset", "agent": "agent"}
    return [r for r in rows
            if all(r.get(col) == flt[k] for k, col in keys.items() if k in flt)]


def git_info(root: Path) -> dict:
    def run(*args: str) -> str | None:
        try:
            p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                               timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    count = run("rev-list", "--count", "HEAD")
    last = run("log", "-1", "--format=%h %cs %s")
    dirty = run("status", "--short")
    return {"count": count, "last": last,
            "dirty": len(dirty.splitlines()) if dirty is not None else None}


def _run_check(cmd: list[str], root: Path, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout,
                           check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"could not run: {exc}"
    return p.returncode, (p.stdout + "\n" + p.stderr).strip()


def run_checks(root: Path) -> list[dict]:
    py = sys.executable
    out = []
    rc, txt = _run_check([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], root)
    summary = (txt.splitlines() or ["no output"])[-1]
    out.append({"name": "pytest", "ok": rc == 0, "detail": summary})
    rc, txt = _run_check([py, "-m", "ruff", "check", "."], root)
    out.append({"name": "ruff", "ok": rc == 0, "detail": (txt.splitlines() or [""])[-1]})
    lc = root / "scripts" / "licence_check.py"
    if lc.exists():
        rc, txt = _run_check([py, str(lc), "--no-write"], root)
        m = re.search(r"Overall:\s*\**(\w+)", txt)
        out.append({"name": "licence-check", "ok": rc == 0,
                    "detail": f"overall {m[1]}" if m else (txt.splitlines() or [""])[0]})
    else:
        out.append({"name": "licence-check", "ok": False, "detail": "script missing"})
    return out


def collect_facts(root: Path, checks: bool) -> dict:
    tasks_p, gates_p = root / "board" / "TASKS.md", root / "board" / "GATES.md"
    return {
        "tasks": parse_tasks(tasks_p.read_text(encoding="utf-8")) if tasks_p.exists() else {},
        "gates": parse_gates(gates_p.read_text(encoding="utf-8")) if gates_p.exists() else [],
        "ledger": read_ledger(root / "ledger" / "runs.csv"),
        "git": git_info(root),
        "checks": run_checks(root) if checks else None,
        "now": dt.datetime.now(dt.UTC),
        "root": root,
    }


# --------------------------------------------------------------------------- rendering


def e(x: object) -> str:
    return html.escape(str(x), quote=True)


def chip(kind: str, value: str, glyph: str = "") -> str:
    g = f'<span aria-hidden="true">{e(glyph)}</span> ' if glyph else ""
    return f'<span class="chip {e(kind)}-{e(value)}">{g}{e(value)}</span>'


def status_chip(s: str) -> str:
    return chip("st", s, STATUS_GLYPH.get(s, ""))


def health_chip(h: str) -> str:
    return chip("hl", h, HEALTH_GLYPH.get(h, ""))


def evidence_chips(f: dict) -> str:
    levels = sorted({x["level"] for x in f.get("evidence", []) if x["level"] != "none"},
                    key=EVIDENCE.index)
    if not levels:
        return chip("ev", "none")
    top = levels[-1]
    return " ".join(chip("ev", lv) + ('<span class="top" title="highest level">▲</span>'
                                      if lv == top and len(levels) > 1 else "") for lv in levels)


def task_progress(f: dict, tasks: dict[str, str]) -> tuple[int, int, int, list[str]]:
    ids = f.get("tasks", [])
    known = [t for t in ids if t in tasks]
    missing = [t for t in ids if t not in tasks]
    done = sum(1 for t in known if tasks[t] == "x")
    wip = sum(1 for t in known if tasks[t] == "~")
    return done, len(known), wip, missing


def ledger_for(f: dict, rows: list[dict]) -> list[dict]:
    seen: set[int] = set()
    hits: list[dict] = []
    for flt in f.get("ledger", []):
        for r in ledger_matches(rows, flt):
            if id(r) not in seen:
                seen.add(id(r))
                hits.append(r)
    return hits


def ul(items: list, empty: str = "None recorded.") -> str:
    if not items:
        return f'<p class="muted">{e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"


def code_list(paths: list[str]) -> str:
    return " ".join(f"<code>{e(p)}</code>" for p in paths)


def bar(pct: int) -> str:
    return (f'<div class="bar" role="img" aria-label="{pct} percent complete">'
            f'<span style="width:{pct}%"></span></div><span class="pct">{pct}%</span>')


def render_section(f: dict, facts: dict) -> str:
    done, total, wip, missing = task_progress(f, facts["tasks"])
    hits = ledger_for(f, facts["ledger"])
    p: list[str] = []
    p.append(f'<section id="{e(f["id"])}" class="feature" data-status="{e(f["status"])}">')
    p.append(f'<h3>{e(f["name"])} {status_chip(f["status"])} {health_chip(f["health"])}</h3>')
    p.append(f'<p><strong>Purpose.</strong> {e(f["purpose"])}</p>')
    p.append(f'<p><strong>Done when.</strong> {e(f["done_when"])}</p>')
    p.append(f'<div class="kv"><div><strong>Status</strong> {status_chip(f["status"])} '
             f'&nbsp; <strong>Completion</strong> {bar(f["completion"])}</div>'
             f'<p class="muted">Why {f["completion"]}%: {e(f["completion_reason"])}</p>'
             f'<p><strong>Tasks</strong> {done} of {total} done'
             f'{f", {wip} in progress" if wip else ""}'
             f'{" (unknown ids: " + e(", ".join(missing)) + ")" if missing else ""}'
             f' &nbsp; <span class="muted">{e(", ".join(f.get("tasks", [])))}</span></p>'
             f'<p><strong>Owners</strong> {e(", ".join(f.get("owners", [])) or "none")}</p></div>')
    p.append(f'<p><strong>Functionality today</strong> {health_chip(f["health"])} '
             f'{e(f["functionality"])}</p>')

    p.append("<h4>What is implemented</h4>")
    impl = f.get("implemented", [])
    if impl:
        p.append("<ul>" + "".join(
            f'<li>{e(i.get("item", ""))} {code_list(i.get("paths", []))}</li>' for i in impl)
                 + "</ul>")
    else:
        p.append('<p class="muted">Nothing implemented yet.</p>')

    p.append(f"<h4>Evidence</h4><p>{evidence_chips(f)}</p>")
    ev = [x for x in f.get("evidence", []) if x["level"] != "none" or x.get("refs")]
    if ev:
        p.append("<ul>" + "".join(
            f'<li>{chip("ev", x["level"])} {e("; ".join(x.get("refs", [])) or "no reference")}</li>'
            for x in ev) + "</ul>")
    if f.get("ledger"):
        flt = "; ".join(" ".join(f"{k}={v}" for k, v in d.items()) for d in f["ledger"])
        p.append(f'<p><strong>Live ledger rows</strong> {len(hits)} match '
                 f'<span class="muted">({e(flt)})</span></p>')
        if hits:
            p.append('<div class="scroll"><table class="mini"><thead><tr><th>timestamp</th>'
                     '<th>agent</th><th>kind</th><th>dataset</th><th>metric</th><th>value</th>'
                     '</tr></thead><tbody>')
            for r in hits[-3:][::-1]:
                p.append("<tr>" + "".join(
                    f"<td>{e(r.get(c, ''))}</td>" for c in
                    ("timestamp", "agent", "kind", "dataset", "metric", "value")) + "</tr>")
            p.append("</tbody></table></div>")
    else:
        p.append('<p class="muted">No ledger filter: no ledger rows apply to this feature.</p>')

    p.append("<h4>Challenges and findings</h4>")
    ch = f.get("challenges", [])
    if ch:
        p.append("<ul>" + "".join(
            f'<li>{e(c.get("finding", ""))} <span class="muted">[{e(c.get("source", ""))}]</span>'
            f"</li>" for c in ch) + "</ul>")
    else:
        p.append('<p class="muted">None recorded.</p>')
    p.append(f"<h4>Still to do</h4>{ul(f.get('todo', []))}")
    p.append(f"<h4>Needs the human</h4>{ul(f.get('needs_human', []), 'Nothing needed.')}")
    p.append(f"<h4>Decisions</h4>{ul(f.get('decisions', []))}")
    p.append(f"<h4>Risks</h4>{ul(f.get('risks', []))}")
    p.append(f"<h4>Key files</h4><p>{code_list(f.get('key_files', [])) or 'None.'}</p>")
    p.append('<p class="back"><a href="#overview">Back to the overview</a></p>')
    p.append("</section>")
    return "\n".join(p)


def render_overview_row(f: dict, facts: dict) -> str:
    done, total, wip, _ = task_progress(f, facts["tasks"])
    blockers = f.get("blockers") or f.get("needs_human") or []
    top = blockers[0] if blockers else "none recorded"
    owners = ", ".join(f.get("owners", [])) or "none"
    tasks_txt = f"{done}/{total}" + (f" (+{wip} in progress)" if wip else "")
    return (
        f'<tr data-status="{e(f["status"])}">'
        f'<td data-label="Feature"><a href="#{e(f["id"])}">{e(f["name"])}</a></td>'
        f'<td data-label="Status">{status_chip(f["status"])}</td>'
        f'<td data-label="Completion">{bar(f["completion"])}</td>'
        f'<td data-label="Functionality">{health_chip(f["health"])}'
        f'<div class="fn">{e(f["functionality"])}</div></td>'
        f'<td data-label="Evidence">{evidence_chips(f)}</td>'
        f'<td data-label="Owners">{e(owners)}</td>'
        f'<td data-label="Tasks">{e(tasks_txt)}</td>'
        f'<td data-label="Top blocker">{e(top)}</td></tr>')


CSS = """
:root{--bg:#fbfbfa;--fg:#1c1e21;--muted:#5b6068;--card:#ffffff;--line:#d5d8dc;--accent:#0b5cad;
--bar:#0072B2;--bar-bg:#e3e6ea;--ok:#005f46;--ok-bg:#d6efe6;--part:#7a4b00;--part-bg:#fbe8c4;
--bad:#8c2200;--bad-bg:#fbd9cc;--unk:#444a52;--unk-bg:#e6e8eb;--st:#eef2f7;--ev:#efe9f6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14161a;--fg:#e8eaed;
--muted:#a3a9b2;--card:#1c1f25;--line:#363b44;--accent:#7db7f0;--bar:#56b4e9;--bar-bg:#2e333b;
--ok:#7fe0bc;--ok-bg:#12382c;--part:#f2c46b;--part-bg:#40300f;--bad:#ffab8c;--bad-bg:#4a1f10;
--unk:#c3c8cf;--unk-bg:#2e333b;--st:#232a34;--ev:#2c2538}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
h1{margin:.2em 0;font-size:1.6rem}h2{margin-top:2em;border-bottom:2px solid var(--line)}
h3{margin:0 0 .5em;font-size:1.25rem}h4{margin:1.2em 0 .3em;font-size:1rem}
a{color:var(--accent)}code{font-size:.82em;background:var(--st);padding:1px 5px;border-radius:4px;
word-break:break-all}
.muted{color:var(--muted)}.sub{color:var(--muted);margin:0}
.headline{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--bar);
padding:12px 16px;border-radius:6px}
.gates{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.gate{flex:1 1 220px;background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:8px 12px}
.gate b{display:block}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid currentColor;
font-size:.8rem;font-weight:600;white-space:nowrap;margin:1px 0}
.st-not-started{color:var(--unk);background:var(--unk-bg)}.st-designing{color:var(--part);
background:var(--part-bg)}.st-prototype{color:var(--accent);background:var(--st)}
.st-working{color:var(--ok);background:var(--ok-bg)}.st-proven{color:var(--ok);
background:var(--ok-bg);border-width:2px}
.hl-ok{color:var(--ok);background:var(--ok-bg)}.hl-partial{color:var(--part);
background:var(--part-bg)}.hl-blocked{color:var(--bad);background:var(--bad-bg)}
.hl-unknown{color:var(--unk);background:var(--unk-bg)}
.ev-none{color:var(--bad);background:var(--bad-bg)}
.chip[class*="ev-"]:not(.ev-none){color:var(--fg);background:var(--ev)}
.top{color:var(--muted);font-size:.7rem}
.bar{display:inline-block;vertical-align:middle;width:110px;height:10px;background:var(--bar-bg);
border-radius:5px;overflow:hidden;border:1px solid var(--line)}
.bar span{display:block;height:100%;background:var(--bar)}.pct{margin-left:6px;font-weight:600}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid var(--line)}
th{background:var(--st);position:sticky;top:0}
.fn{font-size:.85rem;color:var(--muted);margin-top:4px}
.scroll{overflow-x:auto}table.mini{font-size:.8rem}table.mini td,table.mini th{padding:3px 6px}
section.feature{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 16px;margin:16px 0}
.kv p{margin:.3em 0}.back{font-size:.85rem}
.filter{margin:8px 0}select{font:inherit;padding:2px 6px}
ol.needs li{margin:.3em 0}
footer{margin-top:2em;padding-top:1em;border-top:2px solid var(--line);font-size:.9rem}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
@media (max-width:760px){
 main{padding:12px}
 table.ov,table.ov thead,table.ov tbody,table.ov tr,table.ov td{display:block;width:100%}
 table.ov thead{position:absolute;left:-9999px}
 table.ov tr{background:var(--card);border:1px solid var(--line);border-radius:8px;
 margin:0 0 12px;padding:6px 10px}
 table.ov td{border:0;padding:4px 0}
 table.ov td::before{content:attr(data-label);display:block;font-size:.72rem;
 text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
@media print{body{background:#fff;color:#000}.filter{display:none}
 section.feature{break-inside:avoid-page}a{color:#000}}
"""

JS = """
(function(){var s=document.getElementById('fsel');if(!s)return;
s.addEventListener('change',function(){var v=s.value;
document.querySelectorAll('[data-status]').forEach(function(n){
n.style.display=(!v||n.getAttribute('data-status')===v)?'':'none';});});})();
"""


def render(reg: dict, facts: dict) -> str:
    """Render the registry plus live facts to one HTML document (a string)."""
    feats = [f for f in reg["features"] if not f.get("retired")]
    now: dt.datetime = facts["now"]
    counts = {s: sum(1 for f in feats if f["status"] == s) for s in STATUSES}
    p: list[str] = []
    p.append('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             "<title>HX Stomp Tone Matcher status</title>"
             f"<style>{CSS}</style></head><body><main>")
    p.append("<h1>HX Stomp Tone Matcher: project status</h1>")
    p.append(f'<p class="sub">Generated {e(now.strftime("%Y-%m-%d %H:%M UTC"))} · '
             f'{e(reg["cycle"])} · for {e(reg["generated_for"])}</p>')
    p.append(f'<p class="headline">{e(reg["headline"])}</p>')

    p.append('<div class="gates" aria-label="Gates">')
    for g in facts["gates"]:
        state = "signed off" if g["signed"] else "not signed off"
        glyph = "✓" if g["signed"] else "○"
        p.append(f'<div class="gate"><b>{e(g["name"])}: {e(g["title"])}</b>'
                 f'{glyph} {e(state)} · {g["done"]} of {g["total"]} checklist lines ticked</div>')
    if not facts["gates"]:
        p.append('<div class="gate">board/GATES.md not found</div>')
    p.append("</div>")

    summary = ", ".join(f"{counts[s]} {s}" for s in STATUSES if counts[s])
    p.append(f'<h2 id="overview">Overview</h2><p class="muted">{len(feats)} features: '
             f"{e(summary)}. Completion is a judgement against each feature's own definition of "
             "done; task counts are shown beside it for comparison. Evidence is separate from "
             "status: nothing here has been judged by ear unless the ear chip says so.</p>")
    opts = "".join(f'<option value="{s}">{s}</option>' for s in STATUSES)
    p.append('<div class="filter"><label>Filter by status '
             f'<select id="fsel"><option value="">all</option>{opts}</select></label></div>')
    p.append('<table class="ov"><thead><tr><th>Feature</th><th>Status</th><th>Completion</th>'
             "<th>Functionality</th><th>Evidence</th><th>Owners</th><th>Tasks</th>"
             "<th>Top blocker</th></tr></thead><tbody>")
    p.extend(render_overview_row(f, facts) for f in feats)
    p.append("</tbody></table>")

    p.append("<h2>Features</h2>")
    p.extend(render_section(f, facts) for f in feats)

    p.append('<h2 id="discrepancies">Discrepancies</h2>')
    disc = list(reg.get("discrepancies", []))
    for f in feats:
        _, _, _, missing = task_progress(f, facts["tasks"])
        if missing:
            disc.append(f"Registry feature '{f['id']}' lists task ids not on the board: "
                        f"{', '.join(missing)}.")
    p.append(ul(disc, "None recorded."))

    p.append('<h2 id="needs">Needs from the human (all features, in priority order)</h2>')
    prio = [str(x) for x in reg.get("human_priority", [])]
    by_text: dict[str, list[str]] = {}
    for f in feats:
        for n in f.get("needs_human", []):
            by_text.setdefault(n, []).append(f["name"])
    order = [(", ".join(names), n) for n, names in by_text.items()]
    if prio:
        def rank(item: tuple[str, str]) -> int:
            for i, key in enumerate(prio):
                if key.lower() in item[1].lower():
                    return i
            return len(prio)
        order.sort(key=rank)
    if order:
        p.append('<ol class="needs">' + "".join(
            f"<li>{e(n)} <span class=\"muted\">({e(name)})</span></li>" for name, n in order)
                 + "</ol>")
    else:
        p.append('<p class="muted">Nothing needed.</p>')

    g = facts["git"]
    p.append("<footer><h2>Footer</h2>")
    p.append(f'<p>Generated {e(now.strftime("%Y-%m-%d %H:%M:%S UTC"))} for {e(reg["cycle"])}. '
             f"Ledger rows: {len(facts['ledger'])}. Git commits: {e(g['count'] or 'unknown')}"
             f"{', last: ' + e(g['last']) if g.get('last') else ''}"
             f"{', ' + str(g['dirty']) + ' uncommitted paths' if g.get('dirty') else ''}.</p>")
    if facts["checks"] is None:
        p.append('<p class="muted">Checks were not run for this page (use --run-checks).</p>')
    else:
        p.append("<ul>" + "".join(
            f'<li><span class="{"pass" if c["ok"] else "fail"}">'
            f'{"PASS" if c["ok"] else "FAIL"}</span> {e(c["name"])}: {e(c["detail"])}</li>'
            for c in facts["checks"]) + "</ul>")
    p.append("<p class=\"muted\">Gates: "
             + e("; ".join(f"{x['name']} {'signed' if x['signed'] else 'not signed'}"
                           for x in facts["gates"]) or "unknown") + ".</p>")
    p.append("</footer>")
    p.append(f"<script>{JS}</script></main></body></html>")
    return "\n".join(p)


# --------------------------------------------------------------------------- markdown

MD_UNLINKED_PREFIXES = ("cache/", "data/", "fixtures/golden/")
BAR_CELLS = 10


def mde(x: object) -> str:
    """Escape text for Markdown prose or a table cell (no raw HTML, no pipes, one line)."""
    t = " ".join(str(x).split())
    t = t.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in ("|", "`", "*", "[", "]"):
        t = t.replace(ch, "\\" + ch)
    return t


def md_code(x: object) -> str:
    """Inline code span that survives pipes and backticks."""
    t = " ".join(str(x).split()).replace("|", "&#124;")
    if "`" in t:
        return "`` " + t + " ``"
    return f"`{t}`"


def text_bar(pct: int) -> str:
    n = max(0, min(BAR_CELLS, round(pct * BAR_CELLS / 100)))
    return "█" * n + "░" * (BAR_CELLS - n) + f" {pct}%"


def gh_slug(heading: str, used: dict[str, int]) -> str:
    """The anchor GitHub gives a heading (lowercase, punctuation dropped, spaces to hyphens)."""
    base = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
    n = used.get(base, 0)
    used[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def git_ignored(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    try:
        p = subprocess.run(["git", "check-ignore", "--stdin"], cwd=root, input="\n".join(paths),
                           capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(p.stdout.split("\n")) if p.returncode in (0, 1) else set()


def md_path(path: str, root: Path | None, ignored: set[str]) -> str:
    """A repo path as a relative link from reports/status/ when the file is in the repo."""
    clean = path.strip()
    if (root is not None and clean and not clean.startswith(MD_UNLINKED_PREFIXES)
            and clean not in ignored and (root / clean).exists()):
        return f"[{md_code(clean)}](../../{urllib.parse.quote(clean)})"
    return md_code(clean)


def md_list(items: list, empty: str) -> list[str]:
    return [f"- {mde(i)}" for i in items] if items else [f"_{mde(empty)}_"]


def md_evidence(f: dict) -> str:
    levels = sorted({x["level"] for x in f.get("evidence", []) if x["level"] != "none"},
                    key=EVIDENCE.index)
    if not levels:
        return "none"
    return ", ".join(levels[:-1] + [f"**{levels[-1]}**"]) if len(levels) > 1 else levels[0]


def render_markdown(reg: dict, facts: dict) -> str:
    """Render the same content as `render` as GitHub-flavoured Markdown (a string)."""
    feats = [f for f in reg["features"] if not f.get("retired")]
    now: dt.datetime = facts["now"]
    root: Path | None = facts.get("root")
    all_paths = sorted({p for f in feats for i in f.get("implemented", [])
                        for p in i.get("paths", [])} | {p for f in feats
                                                         for p in f.get("key_files", [])})
    ignored = git_ignored(root, all_paths) if root is not None else set()
    used: dict[str, int] = {}
    anchors: dict[str, str] = {}
    for h in ("HX Stomp Tone Matcher: project status", "Gates", "Overview"):
        gh_slug(h, used)
    for f in feats:
        anchors[f["id"]] = gh_slug(f["name"], used)
    overview_anchor = "overview"
    counts = {s: sum(1 for f in feats if f["status"] == s) for s in STATUSES}

    o: list[str] = []
    o.append("# HX Stomp Tone Matcher: project status\n")
    o.append(f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · {mde(reg['cycle'])} · "
             f"for {mde(reg['generated_for'])}\n")
    o.append("_Generated by `scripts/status_report.py` from `docs/features.json` and the live "
             "board, ledger and gates; do not edit by hand. The same content as "
             "[index.html](index.html), which has colours and a status filter when opened "
             "locally._\n")
    o.append(f"> {mde(reg['headline'])}\n")

    o.append("## Gates\n")
    if facts["gates"]:
        for g in facts["gates"]:
            state = "signed off" if g["signed"] else "not signed off"
            o.append(f"- **{mde(g['name'])}: {mde(g['title'])}**: {state}, {g['done']} of "
                     f"{g['total']} checklist lines ticked")
    else:
        o.append("_board/GATES.md not found._")
    o.append("")

    summary = ", ".join(f"{counts[s]} {s}" for s in STATUSES if counts[s])
    o.append("## Overview\n")
    o.append(f"{len(feats)} features: {summary}. Completion is a judgement against each "
             "feature's own definition of done; task counts are shown beside it for "
             "comparison. Evidence is separate from status: nothing here has been judged by ear "
             "unless the evidence says `ear`.\n")
    o.append("| Feature | Status | Health | Completion | Evidence | Owners | Tasks | "
             "Top blocker |")
    o.append("|---|---|---|---|---|---|---|---|")
    for f in feats:
        done, total, wip, _ = task_progress(f, facts["tasks"])
        blockers = f.get("blockers") or f.get("needs_human") or []
        tasks_txt = f"{done}/{total}" + (f" (+{wip} in progress)" if wip else "")
        cells = [f"[{mde(f['name'])}](#{anchors[f['id']]})", f["status"], f["health"],
                 text_bar(f["completion"]), md_evidence(f),
                 mde(", ".join(f.get("owners", [])) or "none"), mde(tasks_txt),
                 mde(blockers[0] if blockers else "none recorded")]
        o.append("| " + " | ".join(cells) + " |")
    o.append("")

    for f in feats:
        done, total, wip, missing = task_progress(f, facts["tasks"])
        hits = ledger_for(f, facts["ledger"])
        o.append(f"## {mde(f['name'])}\n")
        o.append(f"**Status:** {f['status']} · **Health:** {f['health']} · "
                 f"**Completion:** {text_bar(f['completion'])} · "
                 f"**Evidence:** {md_evidence(f)}\n")
        o.append(f"**Purpose.** {mde(f['purpose'])}\n")
        o.append(f"**Done when.** {mde(f['done_when'])}\n")
        o.append(f"**Why {f['completion']}%.** {mde(f['completion_reason'])}\n")
        extra = f", {wip} in progress" if wip else ""
        unknown = f" (ids not on the board: {mde(', '.join(missing))})" if missing else ""
        o.append(f"**Tasks.** {done} of {total} done{extra}{unknown}"
                 + (f": {mde(', '.join(f.get('tasks', [])))}" if f.get("tasks") else "") + "  ")
        o.append(f"**Owners.** {mde(', '.join(f.get('owners', [])) or 'none')}\n")
        o.append(f"**Functionality today ({f['health']}).** {mde(f['functionality'])}\n")

        o.append("### What is implemented\n")
        impl = f.get("implemented", [])
        if impl:
            for i in impl:
                links = " ".join(md_path(p, root, ignored) for p in i.get("paths", []))
                o.append(f"- {mde(i.get('item', ''))} {links}".rstrip())
        else:
            o.append("_Nothing implemented yet._")
        o.append("")

        o.append("### Evidence\n")
        ev = [x for x in f.get("evidence", []) if x["level"] != "none" or x.get("refs")]
        if ev:
            for x in ev:
                refs = "; ".join(x.get("refs", [])) or "no reference"
                o.append(f"- **{x['level']}**: {mde(refs)}")
        else:
            o.append("- **none**: no evidence recorded")
        if f.get("ledger"):
            flt = "; ".join(" ".join(f"{k}={v}" for k, v in d.items()) for d in f["ledger"])
            o.append(f"\n**Live ledger rows:** {len(hits)} match ({md_code(flt)})")
            if hits:
                o.append("\n| timestamp | agent | kind | dataset | metric | value |")
                o.append("|---|---|---|---|---|---|")
                for r in hits[-3:][::-1]:
                    o.append("| " + " | ".join(mde(r.get(c, "")) for c in (
                        "timestamp", "agent", "kind", "dataset", "metric", "value")) + " |")
        else:
            o.append("\n_No ledger filter: no ledger rows apply to this feature._")
        o.append("")

        o.append("### Challenges and findings\n")
        ch = f.get("challenges", [])
        o.extend([f"- {mde(c.get('finding', ''))} _[{mde(c.get('source', ''))}]_" for c in ch]
                 or ["_None recorded._"])
        o.append("\n### Still to do\n")
        o.extend(md_list(f.get("todo", []), "None recorded."))
        o.append("\n### Needs the human\n")
        o.extend(md_list(f.get("needs_human", []), "Nothing needed."))
        o.append("\n### Decisions and risks\n")
        o.append("**Decisions**\n")
        o.extend(md_list(f.get("decisions", []), "None recorded."))
        o.append("\n**Risks**\n")
        o.extend(md_list(f.get("risks", []), "None recorded."))
        o.append("\n### Key files\n")
        kf = f.get("key_files", [])
        o.append(" · ".join(md_path(p, root, ignored) for p in kf) if kf else "None.")
        o.append(f"\n[Back to the overview](#{overview_anchor})\n")

    o.append("## Discrepancies\n")
    disc = list(reg.get("discrepancies", []))
    for f in feats:
        _, _, _, missing = task_progress(f, facts["tasks"])
        if missing:
            disc.append(f"Registry feature '{f['id']}' lists task ids not on the board: "
                        f"{', '.join(missing)}.")
    o.extend(md_list(disc, "None recorded."))
    o.append("")

    o.append("## Needs from the human (all features, in priority order)\n")
    prio = [str(x) for x in reg.get("human_priority", [])]
    by_text: dict[str, list[str]] = {}
    for f in feats:
        for n in f.get("needs_human", []):
            by_text.setdefault(n, []).append(f["name"])
    order = [(", ".join(names), n) for n, names in by_text.items()]
    if prio:
        def rank(item: tuple[str, str]) -> int:
            for i, key in enumerate(prio):
                if key.lower() in item[1].lower():
                    return i
            return len(prio)
        order.sort(key=rank)
    if order:
        o.extend(f"{k}. {mde(n)} _({mde(name)})_" for k, (name, n) in enumerate(order, 1))
    else:
        o.append("_Nothing needed._")
    o.append("")

    g = facts["git"]
    o.append("## Footer\n")
    git_txt = (f"Git commits: {mde(g['count'] or 'unknown')}"
               + (f", last: {mde(g['last'])}" if g.get("last") else "")
               + (f", {g['dirty']} uncommitted paths" if g.get("dirty") else ""))
    o.append(f"Generated {now.strftime('%Y-%m-%d %H:%M:%S UTC')} for {mde(reg['cycle'])}. "
             f"Ledger rows: {len(facts['ledger'])}. {git_txt}.\n")
    if facts["checks"] is None:
        o.append("_Checks were not run for this page (use --run-checks)._\n")
    else:
        o.extend(f"- **{'PASS' if c['ok'] else 'FAIL'}** {mde(c['name'])}: {mde(c['detail'])}"
                 for c in facts["checks"])
        o.append("")
    gates = "; ".join(f"{x['name']} {'signed' if x['signed'] else 'not signed'}"
                      for x in facts["gates"]) or "unknown"
    o.append(f"Gates: {mde(gates)}.")
    return "\n".join(o) + "\n"


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--registry", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="HTML output path (default reports/status/index.html)")
    ap.add_argument("--md-out", type=Path, default=None,
                    help="Markdown output path (default reports/status/README.md)")
    ap.add_argument("--format", default="html,md",
                    help="comma list of html, md (default: both)")
    ap.add_argument("--run-checks", action="store_true", help="run pytest, ruff, licence_check")
    a = ap.parse_args(argv)
    formats = [x.strip() for x in a.format.split(",") if x.strip()]
    bad = [x for x in formats if x not in ("html", "md")]
    if bad or not formats:
        ap.error(f"--format takes html, md or html,md (got {a.format!r})")
    root: Path = a.root
    reg = load_registry(a.registry or root / "docs" / "features.json")
    errs = validate(reg)
    if errs:
        print("status_report: registry is invalid:", file=sys.stderr)
        for m in errs:
            print(f"  - {m}", file=sys.stderr)
        return 2
    facts = collect_facts(root, a.run_checks)
    if "html" in formats:
        out = a.out or root / "reports" / "status" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(reg, facts), encoding="utf-8")
        print(out)
    if "md" in formats:
        mout = a.md_out or root / "reports" / "status" / "README.md"
        mout.parent.mkdir(parents=True, exist_ok=True)
        mout.write_text(render_markdown(reg, facts), encoding="utf-8")
        print(mout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
