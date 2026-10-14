#!/usr/bin/env python3
"""licence-check (owner: reviewer). See .claude/skills/licence-check/SKILL.md.

Sections: dependency licences, pedalboard import boundary, Line 6 asset scan, committed golden
presets, datasets and model cards. Writes reviews/licence-<date>.md. Exit 1 on any FAIL.

  python scripts/licence_check.py [--root DIR] [--date YYYY-MM-DD] [--no-write]
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

PEDALBOARD_HOST = "src/tonematch/engines/native_host.py"
BOUNDARY_DECLARED_IN = "src/tonematch/engines/native.py"
SKIP_DIRS = {".venv", ".git", "cache", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}
COPYLEFT = ("gpl", "agpl", "lgpl", "unknown", "general public")
# Weak copyleft that is flagged (WARN when allowlisted) alongside the GPL family.
WEAK_COPYLEFT = re.compile(r"\bmpl\b|mozilla public", re.IGNORECASE)
# Non-commercial / research-only terms: a distinct category that blocks bundled or commercial release.
NONCOMMERCIAL = re.compile(
    r"\bcc[ _-]?by[ _-]?nc|\bby[ _-]nc\b|non[ -]?commercial|research[ -]only|only for research",
    re.IGNORECASE)
NC_REASON = "blocks bundled/commercial release"
BANNED_SUFFIXES = {".hxf", ".hxb", ".hxs", ".hxl", ".vst3", ".component", ".dll", ".dylib", ".so",
                   ".exe", ".dmg", ".pkg", ".msi"}
FIRMWARE_HINTS = ("firmware", "hxstomp.bin", ".fw")
HLX_ALLOWED = ("fixtures/golden/", "fixtures/smoke/known.hlx")
LINE6_MARKERS = ("HD2_", "\"@model\"", "HX_ModelCatalog")
MARKER_SUFFIXES = {".json", ".models", ".hlx"}
OWN_NAMES = {"tonematch"}
ALLOWLIST = "docs/licence-allowlist.json"
# (regex on the licence file header, name). Order matters: LGPL/AGPL before GPL, Apache before MIT.
LICENCE_TEXT = (
    (r"Attribution-NonCommercial", "CC-BY-NC"),
    (r"GNU AFFERO GENERAL PUBLIC", "AGPL"), (r"GNU LESSER GENERAL PUBLIC", "LGPL"),
    (r"GNU GENERAL PUBLIC", "GPL"), (r"Apache License", "Apache-2.0"),
    (r"Mozilla Public License", "MPL-2.0"), (r"Permission is hereby granted, free of charge", "MIT"),
    (r"Redistribution and use in source and binary forms", "BSD"),
    (r"Permission to use, copy, modify, and/or distribute", "ISC"),
)


def classify_licence_text(text: str) -> str | None:
    """Classify a licence file from its header (first 3000 chars). None when unrecognised."""
    head = re.sub(r"\s+", " ", text[:3000])
    for pat, name in LICENCE_TEXT:
        if re.search(pat, head, re.IGNORECASE):
            return name
    return None


def licence_from_files(d) -> str | None:
    """Fallback for empty or UNKNOWN metadata: read the dist-info licence file(s)."""
    for f in d.files or []:
        low = f.name.lower()
        if low.startswith(("licen", "copying", "notice")) and "dist-info" in str(f):
            try:
                found = classify_licence_text(Path(str(d.locate_file(f))).read_text(
                    encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if found:
                return f"{found} (from {f.name})"
    return None


def is_noncommercial(lic: str) -> bool:
    return bool(NONCOMMERCIAL.search(lic))


def load_allowlist(root: Path) -> dict[str, dict]:
    """name (lower) -> {match, reason, ref, needs_boundary?}. Only reviewed, DECISIONS-backed entries."""
    path = root / ALLOWLIST
    if not path.exists():
        return {}
    return {e["name"].lower(): e for e in json.loads(path.read_text(encoding="utf-8"))["allow"]}


def walk(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if p.is_file() or (p.is_dir() and p.suffix in {".vst3", ".component"}):
            yield p, rel.as_posix()


def dependency_rows() -> list[tuple[str, str, str]]:
    rows = []
    for d in metadata.distributions():
        name = d.metadata["Name"] or "?"
        if name.lower() in OWN_NAMES:
            continue
        lic = (d.metadata.get("License-Expression") or d.metadata.get("License") or "").strip()
        if not lic or len(lic) > 80:
            cls = [c.split("::")[-1].strip() for c in d.metadata.get_all("Classifier") or []
                   if c.startswith("License ::")]
            lic = "; ".join(cls) or lic.splitlines()[0][:60] if lic else "; ".join(cls)
        if not lic or "unknown" in lic.lower():
            lic = licence_from_files(d) or lic or "UNKNOWN"
        rows.append((name, d.version, lic))
    return sorted(rows, key=lambda r: r[0].lower())


def pedalboard_imports(root: Path) -> list[tuple[str, int]]:
    """(relative path, line) of every real import of pedalboard in any .py file (ast, not grep)."""
    hits = []
    for p, rel in walk(root):
        if p.suffix != ".py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            names: list[str] = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                names = [n.module]
            elif isinstance(n, ast.Call) and n.args:
                fn = n.func
                fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                a0 = n.args[0]
                if (fname in {"importorskip", "import_module", "__import__"}
                        and isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                    names = [a0.value]
            if any(x == "pedalboard" or x.startswith("pedalboard.") for x in names):
                hits.append((rel, n.lineno))
    return hits


def section_boundary(root: Path, deps) -> tuple[str, list[str]]:
    notes, bad = [], []
    for rel, line in pedalboard_imports(root):
        if rel != PEDALBOARD_HOST:
            bad.append(f"{rel}:{line} imports pedalboard (only {PEDALBOARD_HOST} may)")
    host = root / PEDALBOARD_HOST
    if not host.exists() or not pedalboard_imports(root):
        notes.append(f"no import in {PEDALBOARD_HOST} yet (nothing to check)")
    decl = root / BOUNDARY_DECLARED_IN
    if decl.exists() and "LICENCE BOUNDARY" not in decl.read_text(encoding="utf-8"):
        bad.append(f"{BOUNDARY_DECLARED_IN} does not declare the LICENCE BOUNDARY")
    if any(n.lower() == "pedalboard" for n, _, _ in deps):
        notes.append("pedalboard is installed in this environment (allowed; host process only)")
    return ("FAIL" if bad else "PASS"), bad + notes


def tracked_files(root: Path) -> set[str] | None:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return set(out.split("\n")) - {""}


def section_assets(root: Path) -> tuple[str, list[str]]:
    fail, warn = [], []
    for p, rel in walk(root):
        low = rel.lower()
        if p.suffix.lower() in BANNED_SUFFIXES:
            fail.append(f"{rel}: Line 6 / binary asset type {p.suffix}")
        elif (any(h in p.name.lower() for h in FIRMWARE_HINTS)
              and p.suffix.lower() in {".bin", ".fw", ".img", ".hex", ".dfu"}):
            fail.append(f"{rel}: looks like a firmware image")
        elif p.suffix.lower() == ".hlx" and not low.startswith(HLX_ALLOWED):
            warn.append(f"{rel}: .hlx outside fixtures/golden/ and fixtures/smoke/known.hlx")
        elif p.suffix.lower() in MARKER_SUFFIXES and p.is_file() and p.stat().st_size < 5_000_000:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(m in text for m in LINE6_MARKERS) and not low.startswith(HLX_ALLOWED):
                fail.append(f"{rel}: carries Line 6 model ids outside cache/")
    return ("FAIL" if fail else "PASS"), fail + warn


def section_golden(root: Path) -> tuple[str, list[str]]:
    golden = [rel for p, rel in walk(root) if rel.startswith("fixtures/golden/")
              and p.suffix.lower() == ".hlx"]
    gi = root / ".gitignore"
    ignored = gi.exists() and "fixtures/golden/*.hlx" in gi.read_text(encoding="utf-8")
    tracked = tracked_files(root)
    notes = []
    fail = False
    if not ignored:
        fail = True
        notes.append(".gitignore lacks fixtures/golden/*.hlx")
    if tracked is not None:
        for t in sorted(tracked):
            if t.startswith("fixtures/golden/") and t.lower().endswith(".hlx"):
                fail = True
                notes.append(f"{t} is tracked by git (needs a human decision in docs/DECISIONS.md)")
    else:
        notes.append("not a git repo: tracking cannot be checked; relying on .gitignore")
    notes.append(f"{len(golden)} local golden .hlx file(s), gitignored: {ignored}")
    return ("FAIL" if fail else "PASS"), notes


def section_data(root: Path) -> tuple[str, list[str]]:
    notes = []
    for d in ("data", "models/CARDS.md", "fixtures/synthetic/manifest.json"):
        notes.append(f"{d}: {'present, review by hand' if (root / d).exists() else 'absent (nothing to check)'}")
    return "PASS", notes


def build_report(root: Path, date: str) -> tuple[str, bool]:
    deps = dependency_rows()
    boundary, bnotes = section_boundary(root, deps)
    sections = {
        "Pedalboard import boundary (main process, tests and scripts included)": (boundary, bnotes),
        "Line 6 asset scan": section_assets(root),
        "Committed golden presets": section_golden(root),
        "Datasets and model cards": section_data(root),
    }
    flagged = [r for r in deps if any(c in r[2].lower() for c in COPYLEFT)
               or WEAK_COPYLEFT.search(r[2]) or is_noncommercial(r[2])]
    dep_status = "PASS"
    dep_notes = []
    allow = load_allowlist(root)
    for n, v, lic in flagged:
        entry = allow.get(n.lower())
        nc = is_noncommercial(lic)
        if nc and not (entry and entry["match"].lower() in lic.lower()):
            dep_status = "FAIL"
            dep_notes.append(f"{n} {v}: {lic} (NON-COMMERCIAL, unlisted: {NC_REASON}; record a ruling in "
                             f"docs/DECISIONS.md and add it to {ALLOWLIST})")
        elif nc:
            if dep_status == "PASS":
                dep_status = "WARN"
            dep_notes.append(f"WARN NON-COMMERCIAL {n} {v}: {lic}. {NC_REASON}. {entry['reason']} "
                             f"({entry['ref']})")
        elif entry and entry["match"].lower() in lic.lower():
            if entry.get("needs_boundary") and boundary != "PASS":
                dep_status = "FAIL"
                dep_notes.append(f"{n} {v}: {lic} (allowlisted but the process boundary FAILS)")
            else:
                if dep_status == "PASS":
                    dep_status = "WARN"
                dep_notes.append(f"WARN {n} {v}: {lic}. {entry['reason']} ({entry['ref']})")
        else:
            dep_status = "FAIL"
            dep_notes.append(f"{n} {v}: {lic} (NEW, unreviewed: record a ruling in docs/DECISIONS.md "
                             f"and add it to {ALLOWLIST})")
    out = [f"# licence-check {date}", ""]
    ordered = {"Dependency licences": (dep_status, dep_notes), **sections}
    overall_fail = any(s == "FAIL" for s, _ in ordered.values())
    out.append(f"Overall: **{'FAIL' if overall_fail else 'PASS'}**")
    for title, (status, notes) in ordered.items():
        out += ["", f"## {title}: {status}", ""] + [f"- {n}" for n in notes]
    out += ["", "## All dependencies", "", "| Name | Version | Licence |", "|---|---|---|"]
    out += [f"| {n} | {v} | {lic} |" for n, v, lic in deps]
    return "\n".join(out) + "\n", overall_fail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    text, failed = build_report(a.root, a.date)
    if not a.no_write:
        path = a.root / "reviews" / f"licence-{a.date}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
