"""Render the project status report (skill `status-report`; owner's spec docs/STATUS-REPORT-SPEC.md).

Pipeline: project sources -> evidence collection -> one structured report model -> HTML renderer
and Markdown renderer. Sources: docs/features.json (the per-feature registry), docs/status-curated.json
(curated judgements: health, readiness, problems, human actions, milestones text, glossary),
board/GATES.md (milestones and acceptance criteria), board/TASKS.md, ledger/runs.csv, git, and with
--run-checks the test, lint and licence results. Writes reports/status/index.html (self-contained
dashboard, no JavaScript) and reports/status/README.md (standalone GitHub Markdown), plus a small
snapshot of the model in reports/status/history/<date>.json that the next report diffs against.
Standard library only. Exits 2 with a message if the registry or curated file is invalid or
contradicts the gate checklists.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_timing
import evidence_links
import task_links

ROOT = Path(__file__).resolve().parents[1]
MODEL_SCHEMA = "tonematch.status-report-model"
MODEL_VERSION = 1

# --------------------------------------------------------------------------- vocabularies

STATUSES = ("not-started", "designing", "prototype", "working", "proven")
EVIDENCE = ("none", "unit-tests", "proxy", "synthetic", "hardware", "real-songs", "ear")
HEALTH = ("ok", "partial", "blocked", "unknown")
HEALTH_STATUS = ("On Track", "At Risk", "Blocked", "Complete", "Insufficient Information")
TECH_PROGRESS = ("Advancing", "Steady", "Stalled", "Regressing", "Not Started")
STAGES = ("Foundation", "Prototype", "Integrated", "Validated", "Ready")
STAGE_MEANING = {
    "Foundation": "Core infrastructure and supporting components exist.",
    "Prototype": "Important functionality can be demonstrated in isolation.",
    "Integrated": "The major components work together.",
    "Validated": "The system meets measurable success criteria under representative conditions.",
    "Ready": "The agreed product requirements are satisfied; suitable for its intended use.",
}
PRIORITIES = ("Critical", "High", "Medium", "Low")
CLASSIFICATIONS = ("Active Blocker", "Confirmed Problem", "Risk", "Unknown/Unverified")
# "Superseded": closed by the human without a pass (e.g. Gate 2, 2026-10-25). Never counted as Complete,
# and skipped when choosing the next milestone.
MILESTONE_STATES = ("Complete", "In Progress", "At Risk", "Blocked", "Not Started", "Superseded")
COMPONENT_STATUS = ("Working", "Partially Working", "Not Working", "Not Started", "Blocked")
ACTION_GROUPS = (("blocking", "Blocking now"), ("soon", "Needed soon"), ("later", "Later / optional"))
CHANGE_KINDS = {"milestone": "Milestone", "improvement": "Improvement", "problem": "New problem",
                "resolved": "Resolved", "activity": "Activity without a readiness gain",
                "decision": "Decision that changed the plan"}
STATUS_GLYPH = {"not-started": "○", "designing": "◔", "prototype": "◑", "working": "◕", "proven": "●"}
HEALTH_GLYPH = {"On Track": "✓", "At Risk": "▲", "Blocked": "■", "Complete": "★",
                "Insufficient Information": "?"}
STATE_GLYPH = {"Complete": "✓", "In Progress": "◐", "At Risk": "▲", "Blocked": "■",
               "Not Started": "○", "Superseded": "–"}
COMP_GLYPH = {"Working": "✓", "Partially Working": "◐", "Not Working": "✕", "Not Started": "○",
              "Blocked": "■"}
LIST_FIELDS = ("owners", "tasks", "todo", "needs_human", "decisions", "risks", "key_files",
               "blockers")
REQUIRED = ("id", "name", "purpose", "done_when", "status", "functionality", "health", "evidence")
MD_UNLINKED_PREFIXES = ("cache/", "data/", "fixtures/golden/")
BANNED_IN_SUMMARY = (
    (re.compile(r"\.(py|json|md|csv|wav|hlx)\b"), "a file name"),
    (re.compile(r"\bT\d{3}[a-z]?\b"), "a task id"),
    (re.compile(r"\b(native-engine|stomp-engine|tone-metrics|tone-matcher-lead|status-reporter|"
                r"benchmark agent|search agent)\b"), "an agent name"),
)


def prio_rank(p: str) -> int:
    return PRIORITIES.index(p)


# --------------------------------------------------------------------------- loading


def load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"status_report: {what} not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"status_report: {what} is not valid JSON: {exc}") from None


def load_registry(path: Path) -> dict:
    return load_json(path, "registry")


def validate(reg: dict) -> list[str]:
    """Problems with the feature registry; empty means usable."""
    errs: list[str] = []
    if not isinstance(reg, dict):
        return ["registry must be a JSON object"]
    for key in ("cycle", "generated_for"):
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
        for k in ("completion", "completion_reason"):
            if k in f:
                errs.append(f"{tag}: '{k}' is not allowed: completion percentages are not reported "
                            "(use the task counts, the evidence level and the milestone criteria)")
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

TASK_RE = re.compile(r"^- \[(?P<m>[ x~])\] (?:<a id=\"[^\"]*\"></a>)?(?P<id>T\d+[a-z]?) ")


def parse_tasks(text: str) -> dict[str, str]:
    """task id -> ' ' (open), '~' (in progress) or 'x' (done)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = TASK_RE.match(line)
        if m:
            out[m["id"]] = m["m"]
    return out


def parse_gates(text: str) -> list[dict]:
    """Gates with their checklist: criteria (evidence lines) and the human sign-off line."""
    gates: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        h = re.match(r"^## (Gate \d+) — (.*)$", line)
        if h:
            cur = {"name": h[1], "title": h[2].strip(), "done": 0, "total": 0, "signed": False,
                   "criteria": [], "sign_text": ""}
            gates.append(cur)
            continue
        m = re.match(r"^- \[(?P<m>[ x])\] (?P<t>.*)$", line)
        if m and cur is not None:
            ticked = m["m"] == "x"
            # Quoted gate text may carry a bar such as "70%"; the report never shows a bare
            # percentage (it could be read as a completion figure), so spell it out.
            text = re.sub(r"(\d)\s?%", r"\1 percent", m["t"])
            if text.lstrip().lower().startswith("human sign-off"):
                cur["signed"] = ticked
                cur["sign_text"] = text
            else:
                cur["criteria"].append({"done": ticked, "text": text})
                cur["total"] += 1
                cur["done"] += ticked
    return gates


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ledger_matches(rows: list[dict], flt: dict) -> list[dict]:
    keys = {"kind": "kind", "engine": "engine", "dataset": "dataset", "agent": "agent"}
    return [r for r in rows if all(r.get(col) == flt[k] for k, col in keys.items() if k in flt)]


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
    recent = run("log", "-8", "--format=%h %cs %s")
    dirty = run("status", "--short")
    return {"count": count, "last": last,
            "recent": recent.splitlines() if recent else [],
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
    out.append({"name": "pytest", "ok": rc == 0, "detail": (txt.splitlines() or ["no output"])[-1]})
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
        "task_index": task_links.parse_board(root),
        "git": git_info(root),
        "checks": run_checks(root) if checks else None,
        "now": dt.datetime.now(dt.UTC),
        "root": root,
    }


# --------------------------------------------------------------------------- curated judgements


def norm_title(t: str) -> list[str]:
    stop = {"the", "a", "an", "to", "of", "and", "or", "for", "on", "in", "is", "be", "with"}
    return [w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in stop]


def find_duplicate_actions(actions: list[dict]) -> list[tuple[str, str]]:
    """Pairs of action ids that read as the same action: same id, same normalised title, or
    nearly the same words (Jaccard >= 0.8)."""
    dups: list[tuple[str, str]] = []
    for i, a in enumerate(actions):
        wa = set(norm_title(a.get("title", "")))
        for b in actions[i + 1:]:
            wb = set(norm_title(b.get("title", "")))
            same = a.get("id") == b.get("id")
            if wa and wb and len(wa & wb) / len(wa | wb) >= 0.8:
                same = True
            if same:
                dups.append((a.get("id", "?"), b.get("id", "?")))
    return dups


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def plain_language_problems(text: str, glossary_terms: set[str]) -> list[str]:
    """Things an executive text must not contain: file names, task ids, agent names and
    capitalised abbreviations that the glossary does not define."""
    probs = [f"contains {what}: {m.group(0)!r}" for rx, what in BANNED_IN_SUMMARY
             for m in rx.finditer(text)]
    known = {tok for term in glossary_terms for tok in re.findall(r"[A-Z]{2,}", term)}
    for tok in sorted(set(re.findall(r"\b[A-Z]{2,}\b", text)) - known):
        probs.append(f"unexplained abbreviation {tok!r} (add it to the glossary)")
    return probs


def covers(action: dict, need: str) -> bool:
    return any(c.lower() in need.lower() for c in action.get("covers", []))


def validate_engines(eng: object) -> list[str]:
    """The optional 'engines' block of the curated file (capability verdicts and timing wording)."""
    if eng is None:
        return []
    if not isinstance(eng, dict):
        return ["curated: engines must be an object"]
    errs: list[str] = []
    caps = eng.get("capabilities")
    if not isinstance(caps, list) or not caps:
        errs.append("curated: engines.capabilities must be a non-empty list")
        caps = []
    for c in caps:
        name = c.get("capability") if isinstance(c, dict) else None
        if not name:
            errs.append("curated: an engines capability has no 'capability' name")
            continue
        cols = ("both",) if c.get("both") else ("stomp", "native")
        for col in cols:
            if not (isinstance(c.get(col), dict) and c[col].get("verdict")):
                errs.append(f"curated: engines capability {name!r} needs a '{col}' verdict")
        for col in cols:
            v = (c.get(col) or {}).get("verdict", "")
            for m in re.finditer(r"\{t:(\w+):(\w+)\}", v):
                if m[1] not in engine_timing.PLACEHOLDER_KEYS or m[2] not in ("stomp", "native", "any"):
                    errs.append(f"curated: engines capability {name!r} uses unknown placeholder {m[0]}")
    if not eng.get("units"):
        errs.append("curated: engines.units (plain-English units note) is required")
    labels = eng.get("activities", {})
    for key in engine_timing.ALL_ACTIVITY_KEYS:
        if key not in labels or not labels[key].get("label"):
            errs.append(f"curated: engines.activities needs a label for {key!r}")
    ids = [j.get("id") for j in eng.get("jobs", [])]
    for jid in engine_timing.JOB_IDS:
        if jid not in ids:
            errs.append(f"curated: engines.jobs needs an entry for {jid!r}")
    for j in eng.get("jobs", []):
        if not (j.get("name") and j.get("purpose")):
            errs.append(f"curated: engines job {j.get('id')!r} needs a name and a purpose")
    return errs


def validate_curated(cur: dict, reg: dict, facts: dict) -> list[str]:
    """Contradictions and gaps in the curated file, checked against the registry and the live
    gate checklists, ledger and repository. Empty means the model can be built."""
    errs: list[str] = []
    if not isinstance(cur, dict):
        return ["curated file must be a JSON object"]
    for k in ("project", "assessments", "summary", "biggest_problem", "milestones", "issues",
              "actions", "capabilities", "recent_changes", "components", "glossary", "evidence"):
        if k not in cur:
            errs.append(f"curated: missing '{k}'")
    if errs:
        return errs
    proj = cur["project"]
    for k in ("name", "goal"):
        if not isinstance(proj.get(k), str) or not proj.get(k):
            errs.append(f"curated: project.{k} must be a non-empty string")
    a = cur["assessments"]
    for k in ("health", "technical", "readiness"):
        if not isinstance(a.get(k), dict) or not a[k].get("rationale"):
            errs.append(f"curated: assessments.{k} needs a 'rationale'")
    if a.get("health", {}).get("value") not in HEALTH_STATUS:
        errs.append(f"curated: health {a.get('health', {}).get('value')!r} not in "
                    f"{', '.join(HEALTH_STATUS)}")
    if a.get("technical", {}).get("value") not in TECH_PROGRESS:
        errs.append(f"curated: technical progress {a.get('technical', {}).get('value')!r} not in "
                    f"{', '.join(TECH_PROGRESS)}")
    stage = a.get("readiness", {}).get("stage")
    if stage not in STAGES:
        errs.append(f"curated: readiness stage {stage!r} not in {', '.join(STAGES)}")
    if not a.get("readiness", {}).get("blocker_to_next") and stage != "Ready":
        errs.append("curated: readiness needs 'blocker_to_next' (what prevents the next stage)")
    for k in ("implemented", "integrated", "tested", "verified", "unfinished"):
        if not a.get("technical", {}).get("breakdown", {}).get(k):
            errs.append(f"curated: assessments.technical.breakdown.{k} is required")

    gates = {g["name"]: g for g in facts["gates"]}
    if set(cur["milestones"]) != set(gates):
        errs.append(f"curated: milestones {sorted(cur['milestones'])} must match the gates in "
                    f"board/GATES.md {sorted(gates)}")
    for name, m in cur["milestones"].items():
        g = gates.get(name)
        if g is None:
            continue
        for k in ("tier", "goal", "demonstrated", "remaining", "next_action"):
            if k not in m:
                errs.append(f"curated: milestone {name} missing '{k}'")
        plain = m.get("criteria_plain")
        if plain is not None and len(plain) != len(g["criteria"]):
            errs.append(f"curated: milestone {name} has {len(plain)} plain criteria but the gate "
                        f"checklist has {len(g['criteria'])}; update criteria_plain")
        st = m.get("state")
        if st is not None and st not in MILESTONE_STATES:
            errs.append(f"curated: milestone {name} state {st!r} not in {', '.join(MILESTONE_STATES)}")
        if st == "Complete" and not g["signed"]:
            errs.append(f"curated: milestone {name} claims Complete but the gate is not signed off")
        if g["signed"] and st not in (None, "Complete"):
            errs.append(f"curated: milestone {name} is signed off, so its state is Complete")

    evid_ids = {e.get("id") for e in cur["evidence"]}
    ledger_ts = {r.get("timestamp") for r in facts["ledger"]}
    root: Path | None = facts.get("root")
    for e in cur["evidence"]:
        if e.get("kind") == "ledger" and e.get("ref") not in ledger_ts:
            errs.append(f"curated: evidence {e.get('id')} cites ledger row {e.get('ref')} "
                        "which is not in ledger/runs.csv")
        elif e.get("kind") == "file" and root is not None and not (root / e.get("ref", "")).exists():
            errs.append(f"curated: evidence {e.get('id')} cites {e.get('ref')} which does not exist")
        elif e.get("kind") not in ("ledger", "file"):
            errs.append(f"curated: evidence {e.get('id')} has unknown kind {e.get('kind')!r}")

    def check_refs(owner: str, ids: list[str]) -> None:
        for i in ids:
            if i not in evid_ids:
                errs.append(f"curated: {owner} cites unknown evidence id {i!r}")

    issues = cur["issues"]
    iid = [i.get("id") for i in issues]
    if len(set(iid)) != len(iid):
        errs.append("curated: duplicate issue ids")
    for i in issues:
        tag = f"issue {i.get('id')}"
        if i.get("priority") not in PRIORITIES:
            errs.append(f"curated: {tag} priority {i.get('priority')!r} not in {', '.join(PRIORITIES)}")
        if i.get("classification") not in CLASSIFICATIONS:
            errs.append(f"curated: {tag} classification {i.get('classification')!r} not in "
                        f"{', '.join(CLASSIFICATIONS)}")
        for k in ("title", "problem", "impact", "cause", "resolution", "owner", "next_step"):
            if not i.get(k):
                errs.append(f"curated: {tag} missing '{k}'")
        check_refs(tag, i.get("evidence", []))
    execs = [i for i in issues if i.get("executive")]
    if not 3 <= len(execs) <= 5:
        errs.append(f"curated: the executive issue list must hold 3-5 issues, has {len(execs)}")
    bp = cur["biggest_problem"].get("issue")
    top = next((i for i in issues if i.get("id") == bp), None)
    if top is None:
        errs.append(f"curated: biggest_problem.issue {bp!r} is not an issue id")
    elif issues and prio_rank(top.get("priority", "Low")) > min(
            prio_rank(i.get("priority", "Low")) for i in issues):
        errs.append("curated: the biggest problem must be one of the highest-priority issues")
    elif not top.get("executive"):
        errs.append("curated: the biggest problem must be in the executive issue list")
    if not cur["biggest_problem"].get("why"):
        errs.append("curated: biggest_problem.why is required")

    health = a.get("health", {}).get("value")
    if health in ("On Track", "Complete") and any(
            i.get("priority") == "Critical" or i.get("classification") == "Active Blocker"
            for i in issues):
        errs.append(f"curated: health {health!r} contradicts a Critical issue or Active Blocker")
    if health == "Complete" and not all(g["signed"] for g in facts["gates"]):
        errs.append("curated: health 'Complete' needs every gate signed off")
    if stage in ("Validated", "Ready") and not gates.get("Gate 2", {}).get("signed"):
        errs.append(f"curated: readiness {stage!r} needs Gate 2 signed off")
    if stage == "Ready" and not all(g["signed"] for g in facts["gates"]):
        errs.append("curated: readiness 'Ready' needs every gate signed off")

    glossary_terms = {g.get("term", "") for g in cur["glossary"]}
    if not glossary_terms or "" in glossary_terms:
        errs.append("curated: glossary entries need a term and a definition")
    exec_texts = {"summary": cur["summary"],
                  "health rationale": a.get("health", {}).get("rationale", ""),
                  "technical rationale": a.get("technical", {}).get("rationale", ""),
                  "readiness rationale": a.get("readiness", {}).get("rationale", ""),
                  "biggest problem": (top or {}).get("problem", "") + " " +
                  cur["biggest_problem"].get("why", "")}
    n = words(cur["summary"])
    if not 60 <= n <= 170:
        errs.append(f"curated: summary has {n} words; keep it to about 100-150")
    for label, text in exec_texts.items():
        for p in plain_language_problems(text, glossary_terms):
            errs.append(f"curated: {label} {p}")

    actions = cur["actions"]
    groups = {g for g, _ in ACTION_GROUPS}
    for ac in actions:
        tag = f"action {ac.get('id')}"
        if ac.get("group") not in groups:
            errs.append(f"curated: {tag} group {ac.get('group')!r} not in {sorted(groups)}")
        if ac.get("priority") not in PRIORITIES:
            errs.append(f"curated: {tag} priority {ac.get('priority')!r} invalid")
        for k in ("title", "why", "unblocks", "recommended", "defer", "covers"):
            if not ac.get(k):
                errs.append(f"curated: {tag} missing '{k}'")
    for x, y in find_duplicate_actions(actions):
        errs.append(f"curated: actions {x!r} and {y!r} read as duplicates; merge them")
    owned = cur.get("agent_owned", [])
    for f in reg["features"]:
        for need in f.get("needs_human", []):
            if not any(covers(ac, need) for ac in actions) and not any(
                    s.lower() in need.lower() for s in owned):
                errs.append(f"curated: feature {f['id']} lists a human need not covered by any "
                            f"action or marked agent_owned: {need[:90]!r}")

    caps = cur["capabilities"]
    for grp in ("verified", "unproven", "missing"):
        for c in caps.get(grp, []):
            if not c.get("capability") or not c.get("reality"):
                errs.append(f"curated: capabilities.{grp} entries need capability and reality")
            check_refs(f"capability {c.get('capability')}", c.get("evidence", []))
    for c in cur["recent_changes"]:
        if c.get("kind") not in CHANGE_KINDS:
            errs.append(f"curated: recent change kind {c.get('kind')!r} not in {sorted(CHANGE_KINDS)}")
        if not c.get("evidence"):
            errs.append(f"curated: recent change without evidence: {c.get('text', '')[:60]!r}")
        check_refs("recent change", c.get("evidence", []))
    for m in cur["milestones"].values():
        check_refs("milestone", m.get("evidence", []))
    for key in ("in_progress", "up_next"):
        for w in cur.get(key, []):
            if not (w.get("area") and w.get("text")):
                errs.append(f"curated: {key} entries need an area and a text: {str(w)[:60]!r}")
            if not w.get("evidence"):
                errs.append(f"curated: {key} work without evidence: {w.get('area', '')!r}")
            check_refs(key, w.get("evidence", []))

    errs += validate_engines(cur.get("engines"))
    ec = cur.get("evidence_classes")
    if ec is not None:
        if not isinstance(ec, dict) or not isinstance(ec.get("legend"), list) \
                or not isinstance(ec.get("claims"), list):
            errs.append("curated: evidence_classes needs 'legend' and 'claims' lists")
        else:
            letters = []
            for lg in ec["legend"]:
                if not (lg.get("class") and lg.get("meaning")):
                    errs.append("curated: evidence_classes.legend entries need class and meaning")
                letters.append(lg.get("class"))
            if len(set(letters)) != len(letters):
                errs.append("curated: evidence_classes.legend has a repeated class")
            for cl in ec["claims"]:
                tag = f"evidence class claim {str(cl.get('claim', ''))[:50]!r}"
                for k in ("claim", "class", "n", "limit"):
                    if not cl.get(k):
                        errs.append(f"curated: {tag} missing '{k}'")
                if cl.get("class") not in letters:
                    errs.append(f"curated: {tag} class {cl.get('class')!r} is not in the legend")
                if not cl.get("evidence"):
                    errs.append(f"curated: {tag} has no evidence")
                check_refs(tag, cl.get("evidence", []))
    feats = {f["id"]: f for f in reg["features"] if not f.get("retired")}
    placed: dict[str, str] = {}
    cids = [c.get("id") for c in cur["components"]]
    if len(set(cids)) != len(cids):
        errs.append("curated: duplicate component ids")
    for c in cur["components"]:
        tag = f"component {c.get('id')}"
        if c.get("status") not in COMPONENT_STATUS:
            errs.append(f"curated: {tag} status {c.get('status')!r} not in {', '.join(COMPONENT_STATUS)}")
        for k in ("name", "purpose", "works", "missing", "problem", "next", "confidence"):
            if not c.get(k):
                errs.append(f"curated: {tag} missing '{k}'")
        sts = [feats[f]["status"] for f in c.get("features", []) if f in feats]
        for fid in c.get("features", []):
            if fid not in feats:
                errs.append(f"curated: {tag} lists unknown feature {fid!r}")
            elif fid in placed:
                errs.append(f"curated: feature {fid} is in two components")
            placed[fid] = c.get("id", "")
        if c.get("status") == "Working" and not any(s in ("working", "proven") for s in sts):
            errs.append(f"curated: {tag} is Working but none of its features is working or proven")
        if c.get("status") == "Not Started" and any(s != "not-started" for s in sts):
            errs.append(f"curated: {tag} is Not Started but a feature has begun")
    for fid in feats:
        if fid not in placed:
            errs.append(f"curated: feature {fid} is in no component")
    for i in issues:
        for cid in i.get("components", []):
            if cid not in cids:
                errs.append(f"curated: issue {i.get('id')} names unknown component {cid!r}")
    return errs


# --------------------------------------------------------------------------- the model


def task_progress(f: dict, tasks: dict[str, str]) -> dict:
    ids = f.get("tasks", [])
    known = [t for t in ids if t in tasks]
    return {"ids": ids, "done": sum(1 for t in known if tasks[t] == "x"), "total": len(known),
            "wip": sum(1 for t in known if tasks[t] == "~"),
            "missing": [t for t in ids if t not in tasks]}


def ledger_for(f: dict, rows: list[dict]) -> list[dict]:
    seen: set[int] = set()
    hits: list[dict] = []
    for flt in f.get("ledger", []):
        for r in ledger_matches(rows, flt):
            if id(r) not in seen:
                seen.add(id(r))
                hits.append(r)
    return hits


def evidence_label(level: str, ear_qualifier: str) -> str:
    return f"ear ({ear_qualifier})" if level == "ear" and ear_qualifier else level


def feature_detail(f: dict, facts: dict, ear_q: str) -> dict:
    hits = ledger_for(f, facts["ledger"])
    levels = sorted({x["level"] for x in f.get("evidence", []) if x["level"] != "none"},
                    key=EVIDENCE.index)
    flt = "; ".join(" ".join(f"{k}={v}" for k, v in d.items()) for d in f.get("ledger", []))
    return {
        "id": f["id"], "name": f["name"], "purpose": f["purpose"], "done_when": f["done_when"],
        "status": f["status"], "health": f["health"], "functionality": f["functionality"],
        "progress_note": f.get("progress_note", ""), "owners": f.get("owners", []),
        "tasks": task_progress(f, facts["tasks"]),
        "evidence_levels": levels, "top_evidence": top_evidence(f),
        "evidence_labels": [evidence_label(lv, ear_q) for lv in levels],
        "evidence": [{"level": x["level"], "label": evidence_label(x["level"], ear_q),
                      "refs": x.get("refs", [])}
                     for x in f.get("evidence", []) if x["level"] != "none" or x.get("refs")],
        "implemented": f.get("implemented", []), "challenges": f.get("challenges", []),
        "todo": f.get("todo", []), "needs_human": f.get("needs_human", []),
        "decisions": f.get("decisions", []), "risks": f.get("risks", []),
        "blockers": f.get("blockers", []), "key_files": f.get("key_files", []),
        "ledger_filter": flt, "ledger_count": len(hits),
        "ledger_recent": [{k: r.get(k, "") for k in ("timestamp", "agent", "kind", "dataset",
                                                     "metric", "value")} for r in hits[-3:][::-1]],
    }


def milestone_state(g: dict, c: dict) -> str:
    if g["signed"]:
        return "Complete"
    if c.get("state"):
        return c["state"]
    return "In Progress" if g["done"] else "Not Started"


def build_model(reg: dict, cur: dict, facts: dict, baseline: dict | None = None) -> dict:
    """The one structured report model both renderers consume (JSON-serialisable)."""
    now: dt.datetime = facts["now"]
    today = now.strftime("%Y-%m-%d")
    ear_q = cur.get("ear_qualifier", "") if not any(
        g["name"] == "Gate 5" and g["signed"] for g in facts["gates"]) else ""
    feats = [f for f in reg["features"] if not f.get("retired")]
    fdetail = {f["id"]: feature_detail(f, facts, ear_q) for f in feats}
    evid = {e["id"]: e for e in cur["evidence"]}
    ledger_by_ts = {r["timestamp"]: r for r in facts["ledger"]}

    def ev_ref(eid: str) -> dict:
        e = evid[eid]
        out = {"id": eid, "label": e["label"], "kind": e["kind"], "ref": e["ref"]}
        if e["kind"] == "ledger":
            r = ledger_by_ts[e["ref"]]
            n = sum(1 for x in facts["ledger"] if x["timestamp"] == e["ref"])
            out["summary"] = (" / ".join(x for x in (r["kind"], r["engine"], r["dataset"])
                                         if x and x != "-")
                              + (f", {n} rows at that time" if n > 1 else ""))
        return out

    used_ev: list[str] = []

    def refs(ids: list[str]) -> list[str]:
        for i in ids:
            if i not in used_ev:
                used_ev.append(i)
        return list(ids)

    milestones = []
    for g in facts["gates"]:
        c = cur["milestones"][g["name"]]
        plain = c.get("criteria_plain") or [x["text"] for x in g["criteria"]]
        milestones.append({
            "id": g["name"].lower().replace(" ", "-"), "name": g["name"], "title": g["title"],
            "tier": c["tier"], "goal": c["goal"], "state": milestone_state(g, c),
            "criteria": [{"text": t, "done": x["done"], "raw": x["text"]}
                         for t, x in zip(plain, g["criteria"], strict=True)],
            "criteria_done": g["done"], "criteria_total": g["total"], "signed": g["signed"],
            "demonstrated": c["demonstrated"], "remaining": c["remaining"],
            "blocking": c.get("blocking", ""), "next_action": c["next_action"],
            "evidence": refs(c.get("evidence", [])),
        })
    nxt = next((m for m in milestones if m["state"] not in ("Complete", "Superseded")), None)
    next_milestone = None
    if nxt:
        next_milestone = {
            "id": nxt["id"], "name": nxt["name"], "goal": nxt["goal"], "state": nxt["state"],
            "readiness": f"{nxt['criteria_done']} of {nxt['criteria_total']} acceptance criteria "
                         f"evidenced; owner sign-off {'given' if nxt['signed'] else 'pending'}",
            "needs": nxt["remaining"], "blocking": nxt["blocking"],
            "next_action": nxt["next_action"]}

    issues = sorted(
        [dict(i, evidence=refs(i.get("evidence", []))) for i in cur["issues"]],
        key=lambda i: (prio_rank(i["priority"]), CLASSIFICATIONS.index(i["classification"])))
    issue_by_id = {i["id"]: i for i in issues}
    bp_issue = issue_by_id[cur["biggest_problem"]["issue"]]
    biggest = {"id": bp_issue["id"], "title": bp_issue["title"], "what": bp_issue["problem"],
               "why": cur["biggest_problem"]["why"], "prevents": bp_issue["impact"],
               "resolution": bp_issue["resolution"], "next_step": bp_issue["next_step"],
               "priority": bp_issue["priority"], "classification": bp_issue["classification"]}

    actions = []
    for gk, glabel in ACTION_GROUPS:
        grp = sorted([a for a in cur["actions"] if a["group"] == gk],
                     key=lambda a: prio_rank(a["priority"]))
        for a in grp:
            actions.append(dict(
                {k: a[k] for k in ("id", "title", "why", "unblocks", "recommended", "defer",
                                   "priority")},
                group=gk, group_label=glabel,
                sources=sorted({f["name"] for f in feats for n in f.get("needs_human", [])
                                if covers(a, n)})))
    owned = cur.get("agent_owned", [])
    lead_items = sorted({n for f in feats for n in f.get("needs_human", [])
                         if any(s.lower() in n.lower() for s in owned)})

    components = []
    for c in cur["components"]:
        fl = [fdetail[f] for f in c["features"] if f in fdetail]
        top = max((f["top_evidence"] for f in fl), key=EVIDENCE.index, default="none")
        components.append({
            "id": c["id"], "name": c["name"], "purpose": c["purpose"], "status": c["status"],
            "works": c["works"], "missing": c["missing"], "problem": c["problem"],
            "next": c["next"], "confidence": c["confidence"],
            "strongest_evidence": evidence_label(top, ear_q),
            "features": [{"id": f["id"], "name": f["name"], "status": f["status"]} for f in fl]})

    caps = {grp: [dict(c, evidence=refs(c.get("evidence", []))) for c in cur["capabilities"][grp]]
            for grp in ("verified", "unproven", "missing")}

    recent = [dict(c, evidence=refs(c["evidence"])) for c in cur["recent_changes"]]
    recent.sort(key=lambda c: c["date"], reverse=True)
    ec = cur.get("evidence_classes")
    evidence_classes = None
    if ec:
        evidence_classes = {
            "about": ec.get("about", ""),
            "legend": [{"class": x["class"], "meaning": x["meaning"]} for x in ec["legend"]],
            "claims": [dict(c, evidence=refs(c.get("evidence", []))) for c in ec["claims"]]}
    in_progress = [dict(w, evidence=refs(w["evidence"])) for w in cur.get("in_progress", [])]
    up_next = [dict(w, evidence=refs(w["evidence"])) for w in cur.get("up_next", [])]

    rows = facts["ledger"]
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r.get("kind", "")] = kinds.get(r.get("kind", ""), 0) + 1
    task_done = sum(1 for v in facts["tasks"].values() if v == "x")
    task_wip = sum(1 for v in facts["tasks"].values() if v == "~")

    disc = list(reg.get("discrepancies", []))
    for f in feats:
        tp = fdetail[f["id"]]["tasks"]
        if tp["missing"]:
            disc.append(f"Registry feature '{f['id']}' lists task ids not on the board: "
                        f"{', '.join(tp['missing'])}.")

    model = {
        "schema": MODEL_SCHEMA, "version": MODEL_VERSION,
        "project": cur["project"],
        "report": {"date": today, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
                   "cycle": reg["cycle"], "generated_for": reg["generated_for"],
                   "period_from": baseline["date"] if baseline else cur["period_start"],
                   "period_to": today, "baseline_date": baseline["date"] if baseline else None},
        "assessments": {
            "health": cur["assessments"]["health"],
            "technical": cur["assessments"]["technical"],
            "readiness": dict(cur["assessments"]["readiness"],
                              stage_index=STAGES.index(cur["assessments"]["readiness"]["stage"]) + 1,
                              stages=[{"name": s, "meaning": STAGE_MEANING[s]} for s in STAGES])},
        "summary": cur["summary"],
        "biggest_problem": biggest,
        "next_milestone": next_milestone,
        "milestones": milestones,
        "issues": issues,
        "actions": actions,
        "capabilities": caps,
        "changes": {"baseline_date": baseline["date"] if baseline else None, "diff": [],
                    "recent": recent, "in_progress": in_progress, "up_next": up_next},
        "components": components,
        "engines": (engine_timing.build(cur["engines"], facts["ledger"], facts.get("root"))
                    if cur.get("engines") else None),
        "glossary": sorted(cur["glossary"], key=lambda g: g["term"].lower()),
        "evidence_classes": evidence_classes,
        "evidence": [ev_ref(i) for i in used_ev],
        "details": {
            "features": [fdetail[f["id"]] for f in feats],
            "gates": [{"name": g["name"], "title": g["title"], "signed": g["signed"],
                       "done": g["done"], "total": g["total"],
                       "criteria": g["criteria"], "sign_text": g["sign_text"]}
                      for g in facts["gates"]],
            "tasks": {"done": task_done, "wip": task_wip, "total": len(facts["tasks"])},
            "ledger": {"rows": len(rows), "kinds": dict(sorted(kinds.items()))},
            "discrepancies": disc, "lead_items": lead_items,
            "git": facts["git"], "checks": facts["checks"],
            "gate_status": "; ".join(f"{g['name']} {'signed' if g['signed'] else 'not signed'}"
                                     for g in facts["gates"]) or "unknown",
            "quality_notes": [
                f"Action '{a['id']}' matches no feature need in the registry (stale?)."
                for a in cur["actions"]
                if not any(covers(a, n) for f in feats for n in f.get("needs_human", []))
                and not a.get("standalone")],
        },
    }
    model["changes"]["diff"] = diff_models(baseline, snapshot_of(model)) if baseline else []
    return model


# --------------------------------------------------------------------------- history snapshot


def snapshot_of(model: dict) -> dict:
    """The small, deterministic slice of the model the next report diffs against."""
    a = model["assessments"]
    return {
        "schema": "tonematch.status-snapshot", "version": 1, "date": model["report"]["date"],
        "health": a["health"]["value"], "technical": a["technical"]["value"],
        "readiness": a["readiness"]["stage"],
        "biggest_problem": model["biggest_problem"]["title"],
        "milestones": {m["name"]: {"state": m["state"], "done": m["criteria_done"],
                                   "total": m["criteria_total"], "signed": m["signed"]}
                       for m in model["milestones"]},
        "issues": {i["id"]: {"title": i["title"], "priority": i["priority"],
                             "classification": i["classification"]} for i in model["issues"]},
        "actions": {x["id"]: {"title": x["title"], "group": x["group"]} for x in model["actions"]},
        "components": {c["id"]: {"name": c["name"], "status": c["status"]}
                       for c in model["components"]},
        "features": {f["id"]: f["status"] for f in model["details"]["features"]},
        "capabilities": {k: sorted(c["capability"] for c in v)
                         for k, v in model["capabilities"].items()},
        "tasks_done": model["details"]["tasks"]["done"],
        "ledger_rows": model["details"]["ledger"]["rows"],
    }


def diff_models(prev: dict, now: dict) -> list[dict]:
    """Human-readable differences between two snapshots (each {kind, text})."""
    out: list[dict] = []

    def add(kind: str, text: str) -> None:
        out.append({"kind": kind, "text": text})

    for key, label in (("health", "Project Health"), ("technical", "Technical Progress"),
                       ("readiness", "Product Readiness")):
        if prev.get(key) != now[key]:
            add("changed", f"{label} changed from {prev.get(key)} to {now[key]}.")
    if prev.get("biggest_problem") != now["biggest_problem"]:
        add("changed", f"The biggest problem changed from \"{prev.get('biggest_problem')}\" to "
                       f"\"{now['biggest_problem']}\".")
    for name, m in now["milestones"].items():
        p = prev.get("milestones", {}).get(name)
        if p is None:
            add("changed", f"New milestone: {name}.")
            continue
        if p["state"] != m["state"]:
            add("changed", f"{name} moved from {p['state']} to {m['state']}.")
        elif (p["done"], p["total"]) != (m["done"], m["total"]):
            add("changed", f"{name}: acceptance criteria evidenced went from {p['done']} of "
                           f"{p['total']} to {m['done']} of {m['total']}.")
    for iid, i in now["issues"].items():
        p = prev.get("issues", {}).get(iid)
        if p is None:
            add("problem", f"New issue: {i['title']} ({i['priority']}).")
        elif p["priority"] != i["priority"] or p["classification"] != i["classification"]:
            add("changed", f"Issue \"{i['title']}\" is now {i['priority']} / {i['classification']} "
                           f"(was {p['priority']} / {p['classification']}).")
    for iid, i in prev.get("issues", {}).items():
        if iid not in now["issues"]:
            add("resolved", f"Issue no longer listed: {i['title']}.")
    for aid, x in now["actions"].items():
        p = prev.get("actions", {}).get(aid)
        if p is None:
            add("changed", f"New human action: {x['title'].rstrip('.')}.")
        elif p["group"] != x["group"]:
            add("changed", f"Human action \"{x['title']}\" moved from {p['group']} to {x['group']}.")
    for aid, x in prev.get("actions", {}).items():
        if aid not in now["actions"]:
            add("resolved", f"Human action removed: {x['title']}.")
    for cid, c in now["components"].items():
        p = prev.get("components", {}).get(cid)
        if p and p["status"] != c["status"]:
            add("changed", f"Component \"{c['name']}\" moved from {p['status']} to {c['status']}.")
    for fid, st in now["features"].items():
        p = prev.get("features", {}).get(fid)
        if p and p != st:
            add("changed", f"Feature {fid} status moved from {p} to {st}.")
    for grp in ("verified", "unproven", "missing"):
        for cap in now["capabilities"][grp]:
            where = next((g for g in ("verified", "unproven", "missing")
                          if cap in prev.get("capabilities", {}).get(g, [])), None)
            if where and where != grp:
                add("changed", f"Capability \"{cap}\" moved from {where} to {grp}.")
    if now["tasks_done"] != prev.get("tasks_done"):
        add("activity", f"Board tasks done: {prev.get('tasks_done')} to {now['tasks_done']} "
                        "(bookkeeping, not progress by itself).")
    if now["ledger_rows"] != prev.get("ledger_rows"):
        add("activity", f"Ledger rows: {prev.get('ledger_rows')} to {now['ledger_rows']}.")
    return out


def snapshot_date(path: Path) -> str:
    """The report date a history file belongs to: '2026-10-23.json' and the later same-day
    revisions '2026-10-23-r2.json', '2026-10-23-r3.json' all belong to 2026-10-23."""
    return path.stem.split("-r")[0]


def snapshot_order(path: Path) -> tuple[str, int]:
    """Sort key: (date, revision); the plain file is revision 1."""
    parts = path.stem.split("-r")
    rev = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    return parts[0], rev


def load_baseline(history: Path, today: str) -> dict | None:
    """Newest snapshot dated before today (the last revision of that day), or None."""
    if not history.is_dir():
        return None
    best: dict | None = None
    for p in sorted(history.glob("*.json"), key=snapshot_order):
        if snapshot_date(p) >= today:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("schema") == "tonematch.status-snapshot" and d.get("date") == snapshot_date(p):
            best = d
    return best


def write_snapshot(history: Path, model: dict) -> Path:
    """Append a snapshot; history is never overwritten or deleted. A second run on the same day
    whose snapshot differs is written as <date>-r2.json (then -r3, ...); an identical snapshot
    is left as it is."""
    history.mkdir(parents=True, exist_ok=True)
    date = model["report"]["date"]
    text = json.dumps(snapshot_of(model), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path = history / f"{date}.json"
    n = 1
    while path.exists():
        if path.read_text(encoding="utf-8") == text:
            return path
        n += 1
        path = history / f"{date}-r{n}.json"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- HTML renderer


def e(x: object) -> str:
    return html.escape(str(x), quote=True)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Glossary:
    """Wraps the first use of each glossary term in <abbr title=...> in escaped text."""

    def __init__(self, entries: list[dict]):
        self.defs = {g["term"]: g["definition"] for g in entries}
        terms = sorted(self.defs, key=len, reverse=True)
        self.rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in terms) +
                             r")(?![\w-])") if terms else None
        self.seen: set[str] = set()

    def __call__(self, text: str) -> str:
        if self.rx is None:
            return e(text)
        out, pos = [], 0
        for m in self.rx.finditer(text):
            out.append(e(text[pos:m.start()]))
            term = m.group(1)
            if term in self.seen:
                out.append(e(term))
            else:
                self.seen.add(term)
                out.append(f'<abbr tabindex="0" title="{e(self.defs[term])}">{e(term)}</abbr>')
            pos = m.end()
        out.append(e(text[pos:]))
        return "".join(out)


def badge(kind: str, label: str, glyph: str = "") -> str:
    g = f'<span aria-hidden="true">{e(glyph)}</span> ' if glyph else ""
    return f'<span class="badge b-{e(slug(kind))}-{e(slug(label))}">{g}{e(label)}</span>'


def ev_badges(labels: list[str]) -> str:
    if not labels:
        return badge("ev", "none")
    return " ".join(badge("ev", lv.split(" (")[0]) + (
        f' <span class="muted">{e(lv[lv.index("("):])}</span>' if "(" in lv else "")
        for lv in labels)


def code_list(paths: list[str]) -> str:
    return " ".join(f"<code>{e(p)}</code>" for p in paths)


def ul(items: list, empty: str = "None recorded.") -> str:
    if not items:
        return f'<p class="muted">{e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"


def ev_links(model: dict, ids: list[str]) -> str:
    if not ids:
        return ""
    by_id = {x["id"]: x for x in model["evidence"]}
    return ('<p class="evrefs muted">Evidence: ' + ", ".join(
        f'<a href="#ev-{e(i)}">{e(by_id[i]["label"])}</a>' for i in ids if i in by_id) + "</p>")


CSS = """
:root{--bg:#f6f7f9;--fg:#1b1f24;--muted:#555d68;--card:#fff;--line:#d3d8de;--accent:#0b5cad;
--ok:#0d5f3f;--ok-bg:#d7efe3;--warn:#7a4b00;--warn-bg:#fbe9c6;--bad:#8c2200;--bad-bg:#fbdacd;
--neu:#3f4650;--neu-bg:#e7eaee;--info:#0b4f8f;--info-bg:#dce9f7}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--fg:#e9ebee;--muted:#a6adb7;--card:#1b1e24;
--line:#363c45;--accent:#86bdf2;--ok:#86e3bf;--ok-bg:#12372b;--warn:#f3c978;--warn-bg:#40300f;
--bad:#ffb094;--bad-bg:#4b2012;--neu:#c6cbd2;--neu-bg:#2c3139;--info:#9ccaf6;--info-bg:#17304b}}
*{box-sizing:border-box}
body{overflow-wrap:anywhere;margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}code{font-size:.82em;background:var(--neu-bg);padding:1px 5px;border-radius:4px;
word-break:break-all}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px 48px}
header.top{background:var(--card);border-bottom:1px solid var(--line);padding:16px 0 8px}
header.top h1{margin:0;font-size:1.55rem}
.meta{color:var(--muted);margin:.2em 0 .6em;font-size:.92rem}
nav.toc{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:.92rem;padding-bottom:6px}
h2{margin:2.2em 0 .6em;font-size:1.3rem;border-bottom:2px solid var(--line);padding-bottom:4px}
h3{margin:0 0 .4em;font-size:1.08rem}h4{margin:1em 0 .2em;font-size:.95rem}
.muted{color:var(--muted)}.goal{font-size:1.05rem;margin:1em 0}
.trio{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-top:5px solid var(--neu);
border-radius:8px;padding:10px 14px}
.kpi .lbl{display:block;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted)}
.kpi .val{display:block;font-size:1.35rem;margin:.1em 0 .2em}
.kpi p{margin:.2em 0;font-size:.92rem}
.k-at-risk{border-top-color:var(--warn)}.k-blocked{border-top-color:var(--bad)}
.k-on-track,.k-complete,.k-advancing{border-top-color:var(--ok)}
.ladder{display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:12px 0}
.ladder li{flex:1 1 150px;border:1px solid var(--line);border-radius:6px;padding:6px 10px;
background:var(--card);font-size:.85rem;color:var(--muted)}
.ladder li.reached{color:var(--fg);background:var(--ok-bg)}
.ladder li.current{border:2px solid var(--ok);font-weight:600;color:var(--fg)}
.summary{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px}
.bigproblem{background:var(--bad-bg);border:2px solid var(--bad);border-radius:8px;
padding:12px 16px;margin:16px 0}
.bigproblem h2{border:0;margin:0 0 .3em;font-size:1.15rem;color:var(--bad)}
.bigproblem dl,.issue dl{margin:.3em 0;display:grid;grid-template-columns:max-content 1fr;
gap:2px 12px}
.bigproblem dt,.issue dt{font-weight:600}.bigproblem dd,.issue dd{margin:0}
.next{background:var(--info-bg);border:1px solid var(--info);border-radius:8px;padding:10px 16px}
.next h3{color:var(--info)}
.badge{display:inline-block;padding:0 8px;border-radius:999px;border:1px solid currentColor;
font-size:.78rem;font-weight:600;white-space:nowrap;color:var(--neu);background:var(--neu-bg)}
.b-state-complete,.b-comp-working,.b-st-working,.b-st-proven,.b-pri-low{color:var(--ok);
background:var(--ok-bg)}
.b-state-at-risk,.b-comp-partially-working,.b-pri-medium,.b-st-prototype,.b-cls-risk,
.b-state-in-progress{color:var(--warn);background:var(--warn-bg)}
.b-state-blocked,.b-comp-blocked,.b-comp-not-working,.b-pri-critical,.b-pri-high,
.b-cls-active-blocker,.b-ev-none{color:var(--bad);background:var(--bad-bg)}
.timeline{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.timeline>li{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--neu);
border-radius:8px;padding:10px 14px}
.timeline>li.s-complete{border-left-color:var(--ok)}
.timeline>li.s-at-risk,.timeline>li.s-in-progress{border-left-color:var(--warn)}
.timeline>li.s-blocked{border-left-color:var(--bad)}
.timeline>li.upnext{border-width:2px 2px 2px 8px;box-shadow:0 1px 6px rgba(0,0,0,.15)}
@media (min-width:860px){.timeline{grid-template-columns:1fr 1.5fr 1fr;align-items:start}}
.crit{list-style:none;padding:0;margin:.4em 0;font-size:.9rem}
.crit li{padding-left:1.6em;text-indent:-1.6em;margin:2px 0}
.meter{display:flex;gap:3px;margin:.3em 0}.meter span{flex:1;height:8px;border-radius:2px;
background:var(--neu-bg);border:1px solid var(--line)}.meter span.on{background:var(--ok);
border-color:var(--ok)}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}
summary:focus-visible,a:focus-visible,abbr:focus-visible{outline:3px solid var(--accent);
outline-offset:2px}
.issue,.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 16px;margin:10px 0}
.issue.p-critical,.issue.p-high{border-left:6px solid var(--bad)}
.issue.p-medium{border-left:6px solid var(--warn)}
.issue.p-low{border-left:6px solid var(--neu)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:12px}
.cols .card{margin:0}.cols ul{padding-left:1.1em;margin:.3em 0}.cols li{margin:.35em 0}
.cols h3{font-size:1rem}
.cap-ok h3{color:var(--ok)}.cap-un h3{color:var(--warn)}.cap-no h3{color:var(--bad)}
.actions li{margin:.7em 0}.actions ul{padding-left:1.2em}
abbr{text-decoration:underline dotted;cursor:help}
.comps{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.comps .card{margin:0}.comps p{margin:.3em 0;font-size:.93rem}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:5px 8px;border-bottom:1px solid var(--line);
font-size:.88rem}
.scroll{overflow-x:auto}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
footer{margin-top:2em;padding-top:1em;border-top:2px solid var(--line);font-size:.9rem}
.sr{position:absolute;left:-9999px}
@media (max-width:640px){.bigproblem dl,.issue dl{grid-template-columns:1fr}
.bigproblem dt,.issue dt{margin-top:4px}}
@media print{body{background:#fff;color:#000}details{display:block}
.card,.issue{break-inside:avoid}}
"""


_METRIC_AFTER_TS = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00) (?P<metric>[a-z][a-z0-9_]*)\b(?: row (?P<line>\d+))?")
_ROWS_LIST = re.compile(r"(?<=ledger rows )(\d+(?:, \d+)*)")


def link_ledger_metrics(page: str, idx, fmt) -> str:
    """Several ledger rows can share one timestamp (a batch of rows). The generic linker opens the first
    of them, which sent the Stomp 10 s render figure to the 3 s row. Where a timestamp is followed by the
    metric name and that pair names exactly one row, link that row; "row N" after the metric names the
    line outright, and "ledger rows N, M" links each line."""
    def row_link(text: str, line: int, row: list[str]) -> str:
        title = f"{row[3]} · {row[4]} · {row[5]} · {row[6]} = {row[7]}"
        return fmt(text, f"{idx.rel}/ledger/runs.csv?plain=1#L{line}", title)

    by_line = {line: row for rows in idx.ledger.values() for line, row in rows}

    def sub(m: re.Match) -> str:
        rows = idx.ledger.get(m["ts"], [])
        if m["line"]:
            hit = [(line, row) for line, row in rows if line == int(m["line"]) and row[6] == m["metric"]]
        else:
            hit = [(line, row) for line, row in rows if row[6] == m["metric"]] if len(rows) > 1 else []
        if len(hit) != 1:
            return m.group(0)
        return row_link(m["ts"], hit[0][0], hit[0][1]) + " " + m["metric"] + (f" row {m['line']}" if m["line"] else "")

    def rows_sub(m: re.Match) -> str:
        nums = m.group(0).split(", ")
        if any(int(n) not in by_line for n in nums):
            return m.group(0)
        return ", ".join(row_link(n, int(n), by_line[int(n)]) for n in nums)

    return _ROWS_LIST.sub(rows_sub, _METRIC_AFTER_TS.sub(sub, page))


def render_html(model: dict, board_rel: str = "../../board", root: Path | None = None) -> str:
    """Render the report model as one self-contained HTML document (no JavaScript)."""
    gl = Glossary(model["glossary"])
    a = model["assessments"]
    rep = model["report"]
    d = model["details"]
    p: list[str] = []
    p.append('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<meta name="color-scheme" content="light dark">'
             f"<title>{e(model['project']['name'])}: project status</title>"
             f"<style>{CSS}</style></head><body>")
    p.append('<header class="top"><div class="wrap">'
             f"<h1>{e(model['project']['name'])}: project status</h1>"
             f'<p class="meta">Report date {e(rep["date"])} · reporting period {e(rep["period_from"])}'
             f' to {e(rep["period_to"])} · for {e(rep["generated_for"])}</p>'
             '<nav class="toc" aria-label="Sections">'
             '<a href="#dashboard">Dashboard</a><a href="#problems">Problems</a>'
             '<a href="#milestones">Milestones</a><a href="#works">What works</a>'
             '<a href="#actions">Human actions</a><a href="#changes">What changed</a>'
             '<a href="#components">Components</a>'
             + ('<a href="#engines">Engines</a>' if model.get("engines") else "")
             + '<a href="#technical">Technical details</a>'
             '<a href="#glossary">Glossary</a></nav></div></header><main class="wrap">')

    # --- executive dashboard
    p.append('<section id="dashboard" aria-labelledby="h-dash"><h2 id="h-dash">Executive dashboard</h2>')
    p.append(f'<p class="goal"><strong>What we are building.</strong> {gl(model["project"]["goal"])}</p>')
    h, t, r = a["health"], a["technical"], a["readiness"]
    p.append('<div class="trio">')
    p.append(f'<div class="kpi k-{slug(h["value"])}"><span class="lbl">Project Health</span>'
             f'<strong class="val"><span aria-hidden="true">{e(HEALTH_GLYPH[h["value"]])}</span> '
             f'{e(h["value"])}</strong><p>{gl(h["rationale"])}</p></div>')
    p.append(f'<div class="kpi k-{slug(t["value"])}"><span class="lbl">Technical Progress</span>'
             f'<strong class="val">{e(t["value"])}</strong><p>{gl(t["rationale"])}</p></div>')
    p.append(f'<div class="kpi"><span class="lbl">Product Readiness</span>'
             f'<strong class="val">{e(r["stage"])} (stage {r["stage_index"]} of {len(r["stages"])})'
             f'</strong><p>{gl(r["rationale"])}</p><p><strong>Next stage blocked by:</strong> '
             f'{gl(r["blocker_to_next"])}</p></div></div>')
    p.append('<ol class="ladder" aria-label="Readiness stages, highest demonstrated is marked">')
    for i, s in enumerate(r["stages"], 1):
        cls = "current" if i == r["stage_index"] else ("reached" if i < r["stage_index"] else "")
        mark = " (highest demonstrated)" if i == r["stage_index"] else ""
        p.append(f'<li class="{cls}"><strong>{i}. {e(s["name"])}</strong>{e(mark)}<br>'
                 f'{e(s["meaning"])}</li>')
    p.append("</ol>")
    p.append(f'<div class="summary"><h3>Summary</h3><p>{gl(model["summary"])}</p></div>')
    bp = model["biggest_problem"]
    p.append('<section class="bigproblem" role="region" aria-labelledby="h-big">'
             f'<h2 id="h-big">Biggest problem: {e(bp["title"])}</h2>'
             f'<p>{badge("pri", bp["priority"])} {badge("cls", bp["classification"])}</p><dl>'
             f'<dt>What is wrong</dt><dd>{gl(bp["what"])}</dd>'
             f'<dt>Why it matters</dt><dd>{gl(bp["why"])}</dd>'
             f'<dt>What it prevents</dt><dd>{gl(bp["prevents"])}</dd>'
             f'<dt>What resolves it</dt><dd>{gl(bp["resolution"])}</dd>'
             f'<dt>Next step</dt><dd>{gl(bp["next_step"])}</dd></dl></section>')
    nm = model["next_milestone"]
    if nm:
        p.append('<div class="next"><h3>Next milestone: '
                 f'{e(nm["name"])} {badge("state", nm["state"], STATE_GLYPH[nm["state"]])}</h3>'
                 f'<p>{gl(nm["goal"])}</p><p><strong>Readiness:</strong> {e(nm["readiness"])}.</p>'
                 f'<p><strong>What must happen:</strong> {gl(nm["needs"])}</p>'
                 + (f'<p><strong>Blocked by:</strong> {gl(nm["blocking"])}</p>' if nm["blocking"]
                    else "")
                 + f'<p><strong>Next action:</strong> {gl(nm["next_action"])}</p></div>')
    else:
        p.append('<div class="next"><h3>No open milestone: every gate is complete or closed as superseded.</h3></div>')
    p.append("</section>")

    # --- problems
    p.append('<section id="problems"><h2>Key problems and blockers</h2>')
    p.append('<p class="muted">Ranked by consequence for the goal, not by task count. '
             "Missing evidence is labelled Unknown/Unverified and is not counted as a failure.</p>")
    for i in [x for x in model["issues"] if x["executive"]]:
        p.append(_issue_html(i, gl, model, open_=True))
    rest = [x for x in model["issues"] if not x["executive"]]
    if rest:
        p.append(f"<details><summary>More issues ({len(rest)}, lower priority)</summary>")
        p.extend(_issue_html(i, gl, model, open_=True) for i in rest)
        p.append("</details>")
    p.append("</section>")

    # --- milestones
    p.append('<section id="milestones"><h2>Milestone progress</h2>')
    p.append('<p class="muted">Each milestone is a gate in the project plan with a written '
             "checklist and a sign-off by the owner. Nothing here is a schedule: no deadlines are "
             "set. The next milestone is shown in full.</p><ol class=\"timeline\">")
    next_id = nm["id"] if nm else None
    for m in model["milestones"]:
        up = m["id"] == next_id
        p.append(f'<li id="m-{e(m["id"])}" class="s-{slug(m["state"])}{" upnext" if up else ""}">'
                 f'<h3>{e(m["name"])}: {e(m["title"])}</h3><p>{badge("state", m["state"], STATE_GLYPH[m["state"]])} '
                 f'<span class="muted">{e(m["tier"])} milestone{", up next" if up else ""}</span></p>'
                 f'<div class="meter" role="img" aria-label="{m["criteria_done"]} of '
                 f'{m["criteria_total"]} criteria evidenced">'
                 + "".join(f'<span class="{"on" if c["done"] else ""}"></span>' for c in m["criteria"])
                 + f'</div><p>{gl(m["goal"])}</p>')
        body = ['<ul class="crit">' + "".join(
            f'<li><span aria-hidden="true">{"✓" if c["done"] else "○"}</span> '
            f'<span class="sr">{"Met:" if c["done"] else "Not yet:"}</span> {gl(c["text"])}</li>'
            for c in m["criteria"]) + "</ul>",
            (f'<p class="muted">{m["criteria_done"]} of {m["criteria_total"]} criteria '
             f'evidenced. Owner sign-off: {"given" if m["signed"] else "not yet"}.</p>'),
            f'<p><strong>Demonstrated.</strong> {gl(m["demonstrated"])}</p>',
            f'<p><strong>Remaining.</strong> {gl(m["remaining"])}</p>']
        if m["blocking"]:
            body.append(f'<p><strong>Blocking.</strong> {gl(m["blocking"])}</p>')
        body.append(f'<p><strong>Next action.</strong> {gl(m["next_action"])}</p>')
        body.append(ev_links(model, m["evidence"]))
        if up:
            p.append("".join(body))
        else:
            p.append("<details><summary>Criteria and status</summary>" + "".join(body) + "</details>")
        p.append("</li>")
    p.append("</ol></section>")

    # --- what works
    caps = model["capabilities"]
    p.append('<section id="works"><h2>What works and what does not</h2><div class="cols">')
    for key, title, cls in (("verified", "Working and verified", "cap-ok"),
                            ("unproven", "Implemented but not yet proven", "cap-un"),
                            ("missing", "Missing or not working", "cap-no")):
        p.append(f'<div class="card {cls}"><h3>{title}</h3><ul>' + "".join(
            f'<li><strong>{gl(c["capability"])}</strong><br><span class="muted">{gl(c["reality"])}'
            f"</span></li>" for c in caps[key]) + "</ul></div>")
    p.append("</div></section>")

    # --- actions
    p.append('<section id="actions"><h2>Human decisions and actions</h2>'
             '<p class="muted">One list, each action once. Routine work owned by the agents is not '
             "listed here.</p>")
    for gk, glabel in ACTION_GROUPS:
        items = [x for x in model["actions"] if x["group"] == gk]
        p.append(f"<h3>{e(glabel)}</h3>")
        if not items:
            p.append('<p class="muted">None today: no human action is currently holding up the '
                     "agents' work.</p>")
            continue
        p.append('<ul class="actions">')
        for x in items:
            p.append(f'<li id="a-{e(x["id"])}"><strong>{gl(x["title"])}</strong> '
                     f'{badge("pri", x["priority"])}<ul>'
                     f'<li><em>Why:</em> {gl(x["why"])}</li>'
                     f'<li><em>Unblocks:</em> {gl(x["unblocks"])}</li>'
                     f'<li><em>Recommended:</em> {gl(x["recommended"])}</li>'
                     f'<li><em>If deferred:</em> {gl(x["defer"])}</li></ul></li>')
        p.append("</ul>")
    p.append("</section>")

    # --- changes
    ch = model["changes"]
    p.append('<section id="changes"><h2>What changed since the last report</h2>')
    if ch["baseline_date"]:
        p.append(f'<p class="muted">Compared with the report snapshot of {e(ch["baseline_date"])}.</p>')
        if ch["diff"]:
            p.append("<ul>" + "".join(f"<li>{e(x['text'])}</li>" for x in ch["diff"]) + "</ul>")
        else:
            p.append("<p>No change in the assessments, milestones, issues, actions or components.</p>")
    else:
        p.append('<p class="muted">No earlier report snapshot exists, so there is no automatic '
                 "comparison yet. This report saved one; the next report will diff against it. "
                 "Until then the list below is taken from the project's own records (ledger, "
                 f"decision log, board) for the period from {e(rep['period_from'])}.</p>")
    gen = [c for c in ch["recent"] if c["kind"] != "activity"]
    act = [c for c in ch["recent"] if c["kind"] == "activity"]
    p.append("<h3>Progress and findings</h3><ul>" + "".join(
        f'<li>{e(c["date"])} · <strong>{e(CHANGE_KINDS[c["kind"]])}.</strong> {gl(c["text"])}'
        f'{ev_links(model, c["evidence"])}</li>' for c in gen) + "</ul>")
    if ch.get("in_progress"):
        p.append("<h3>In progress now</h3><ul>" + "".join(
            f'<li><strong>{e(w["area"])}.</strong> {gl(w["text"])}{ev_links(model, w["evidence"])}</li>'
            for w in ch["in_progress"]) + "</ul>")
    elif ch.get("up_next"):
        p.append('<p class="muted">Nothing is in progress right now.</p>')
    if ch.get("up_next"):
        p.append("<h3>Up next</h3><ul>" + "".join(
            f'<li><strong>{e(w["area"])}.</strong> {gl(w["text"])}{ev_links(model, w["evidence"])}</li>'
            for w in ch["up_next"]) + "</ul>")
    if act:
        p.append("<h3>Activity that did not advance readiness</h3><ul>" + "".join(
            f'<li>{e(c["date"])} · {gl(c["text"])}</li>' for c in act) + "</ul>")
    p.append("</section>")

    # --- components
    p.append('<section id="components"><h2>Component status</h2><div class="comps">')
    for c in model["components"]:
        p.append(f'<article class="card" id="c-{e(c["id"])}"><h3>{e(c["name"])}</h3>'
                 f'<p>{badge("comp", c["status"], COMP_GLYPH[c["status"]])}</p>'
                 f'<p class="muted">{gl(c["purpose"])}</p>'
                 f'<p><strong>What works.</strong> {gl(c["works"])}</p>'
                 f'<p><strong>What is missing.</strong> {gl(c["missing"])}</p>'
                 f'<p><strong>Primary problem.</strong> {gl(c["problem"])}</p>'
                 f'<p><strong>Next step.</strong> {gl(c["next"])}</p>'
                 f'<p><strong>Evidence confidence.</strong> {gl(c["confidence"])} '
                 f'<span class="muted">(strongest level: {e(c["strongest_evidence"])})</span></p>'
                 '<p class="muted">Detail: ' + ", ".join(
                     f'<a href="#f-{e(f["id"])}">{e(f["name"])}</a>' for f in c["features"])
                 + "</p></article>")
    p.append("</div></section>")

    # --- engines
    p.append(_engines_html(model, gl))

    # --- technical details
    p.append('<section id="technical"><h2>Technical details and evidence</h2>'
             '<p class="muted">Supporting material for the conclusions above: per-feature detail, '
             "gate checklists, evidence references, the board and ledger. Internal task ids, agent "
             "names and file paths live here.</p>")
    p.append("<h3>Features</h3>")
    for f in d["features"]:
        p.append(_feature_html(f))
    p.append("<h3>Gate checklists</h3>")
    for g in d["gates"]:
        p.append(f"<details><summary>{e(g['name'])}: {e(g['title'])} "
                 f"({g['done']} of {g['total']} criteria ticked, sign-off "
                 f"{'given' if g['signed'] else 'pending'})</summary><ul>" + "".join(
                     f'<li>{"✓" if c["done"] else "○"} {e(c["text"])}</li>' for c in g["criteria"])
                 + f"<li>{e(g['sign_text'] or 'Human sign-off: not recorded')}</li></ul></details>")
    ec = model.get("evidence_classes")
    if ec:
        p.append('<h3 id="evidence-classes">Evidence classes of the headline claims</h3>')
        if ec.get("about"):
            p.append(f'<p class="muted">{gl(ec["about"])}</p>')
        p.append("<dl>" + "".join(
            f"<dt><strong>Class {e(x['class'])}</strong></dt><dd>{e(x['meaning'])}</dd>"
            for x in ec["legend"]) + "</dl>")
        p.append('<div class="scroll"><table><thead><tr><th>Claim</th><th>Class</th><th>n</th>'
                 "<th>Limits</th><th>Evidence</th></tr></thead><tbody>")
        for c in ec["claims"]:
            p.append(f"<tr><td>{gl(c['claim'])}</td><td><strong>{e(c['class'])}</strong></td>"
                     f"<td>{e(c['n'])}</td><td>{gl(c['limit'])}</td>"
                     f"<td>{ev_links(model, c['evidence'])}</td></tr>")
        p.append("</tbody></table></div>")
    p.append('<h3 id="evidence">Evidence references</h3><div class="scroll"><table><thead><tr>'
             "<th>Reference</th><th>What it shows</th><th>Source</th></tr></thead><tbody>")
    for x in model["evidence"]:
        src = (f"ledger row {x['ref']}" + (f" ({x['summary']})" if x.get("summary") else "")
               if x["kind"] == "ledger" else x["ref"])
        p.append(f'<tr id="ev-{e(x["id"])}"><td><code>{e(x["id"])}</code></td><td>{e(x["label"])}</td>'
                 f"<td>{e(src)}</td></tr>")
    p.append("</tbody></table></div>")
    p.append("<h3>Open items for the lead (agent-owned, not human actions)</h3>"
             + ul(d["lead_items"], "None."))
    p.append("<h3>Discrepancies between sources</h3>" + ul(d["discrepancies"]))
    if d["quality_notes"]:
        p.append("<h3>Report quality notes</h3>" + ul(d["quality_notes"]))
    p.append("<h3>Ledger and board</h3>"
             f"<p>{d['ledger']['rows']} ledger rows ("
             + e(", ".join(f"{k}: {v}" for k, v in d["ledger"]["kinds"].items()))
             + f"). Board: {d['tasks']['done']} tasks done, {d['tasks']['wip']} in progress, "
               f"{d['tasks']['total']} listed.</p>")
    g = d["git"]
    p.append("<h3>Recent commits</h3>" + ul(g.get("recent", []), "Git history unavailable."))
    p.append("</section>")

    p.append('<section id="glossary"><h2>Glossary</h2><dl>' + "".join(
        f"<dt><strong>{e(x['term'])}</strong></dt><dd>{e(x['definition'])}</dd>"
        for x in model["glossary"]) + "</dl></section>")

    p.append("<footer><h2>Report information</h2>"
             f"<p>Generated {e(rep['generated_at'])} for {e(rep['cycle'])}. Git commits: "
             f"{e(g['count'] or 'unknown')}"
             f"{', last: ' + e(g['last']) if g.get('last') else ''}"
             f"{', ' + str(g['dirty']) + ' uncommitted paths' if g.get('dirty') else ''}.</p>")
    if d["checks"] is None:
        p.append('<p class="muted">Checks were not run for this report (use --run-checks).</p>')
    else:
        p.append("<ul>" + "".join(
            f'<li><span class="{"pass" if c["ok"] else "fail"}">{"PASS" if c["ok"] else "FAIL"}'
            f"</span> {e(c['name'])}: {e(c['detail'])}</li>" for c in d["checks"]) + "</ul>")
    p.append(f'<p class="muted">Gates: {e(d["gate_status"])}. Generated by '
             "scripts/status_report.py from one report model; the Markdown report "
             "(README.md in this folder) carries the same conclusions.</p></footer>")
    p.append("</main></body></html>")
    page = "\n".join(p)
    if root is not None:
        idx = evidence_links.Index(root, board_rel[:-len("/board")])
        page = evidence_links.linkify_html(link_ledger_metrics(page, idx, evidence_links._html_fmt), idx)
    return task_links.linkify_html(page, model.get("task_index", {}), board_rel)


def _issue_html(i: dict, gl: Glossary, model: dict, open_: bool) -> str:
    return (f'<article class="issue p-{slug(i["priority"])}" id="i-{e(i["id"])}">'
            f'<h3>{e(i["title"])}</h3><p>{badge("pri", i["priority"])} '
            f'{badge("cls", i["classification"])}</p><dl>'
            f'<dt>Problem</dt><dd>{gl(i["problem"])}</dd>'
            f'<dt>Impact</dt><dd>{gl(i["impact"])}</dd>'
            f'<dt>Cause</dt><dd>{gl(i["cause"])}</dd>'
            f'<dt>Resolution</dt><dd>{gl(i["resolution"])}</dd>'
            f'<dt>Owner</dt><dd>{e(i["owner"])}</dd>'
            f'<dt>Next step</dt><dd>{gl(i["next_step"])}</dd></dl>'
            f'{ev_links(model, i["evidence"])}</article>')


def _feature_html(f: dict) -> str:
    tp = f["tasks"]
    parts = [(f'<details id="f-{e(f["id"])}"><summary>{e(f["name"])} · '
              f'{badge("st", f["status"], STATUS_GLYPH[f["status"]])} '
              f'{ev_badges(f["evidence_labels"])}</summary>'),
             f'<p><strong>Purpose.</strong> {e(f["purpose"])}</p>',
             f'<p><strong>Done when.</strong> {e(f["done_when"])}</p>',
             f'<p><strong>Functionality today ({e(f["health"])}).</strong> {e(f["functionality"])}</p>']
    if f["progress_note"]:
        parts.append(f'<p><strong>Progress note.</strong> {e(f["progress_note"])}</p>')
    parts.append(f'<p><strong>Board tasks.</strong> {tp["done"]} of {tp["total"]} done'
                 + (f", {tp['wip']} in progress" if tp["wip"] else "")
                 + (f" (not on the board: {e(', '.join(tp['missing']))})" if tp["missing"] else "")
                 + f' <span class="muted">{e(", ".join(tp["ids"]))}</span>. '
                   f'<strong>Owners.</strong> {e(", ".join(f["owners"]) or "none")}</p>')
    parts.append("<h4>Implemented</h4>" + ("<ul>" + "".join(
        f'<li>{e(i.get("item", ""))} {code_list(i.get("paths", []))}</li>' for i in f["implemented"])
        + "</ul>" if f["implemented"] else '<p class="muted">Nothing implemented yet.</p>'))
    parts.append("<h4>Evidence</h4>" + ("<ul>" + "".join(
        f'<li>{badge("ev", x["level"])} {e("; ".join(x["refs"]) or "no reference")}</li>'
        for x in f["evidence"]) + "</ul>" if f["evidence"]
        else '<p class="muted">No evidence recorded.</p>'))
    if f["ledger_filter"]:
        parts.append(f'<p><strong>Live ledger rows.</strong> {f["ledger_count"]} match '
                     f'<span class="muted">({e(f["ledger_filter"])})</span></p>')
        if f["ledger_recent"]:
            parts.append('<div class="scroll"><table><thead><tr><th>timestamp</th><th>agent</th>'
                         "<th>kind</th><th>dataset</th><th>metric</th><th>value</th></tr></thead><tbody>"
                         + "".join("<tr>" + "".join(f"<td>{e(r[c])}</td>" for c in (
                             "timestamp", "agent", "kind", "dataset", "metric", "value")) + "</tr>"
                             for r in f["ledger_recent"]) + "</tbody></table></div>")
    parts.append("<h4>Challenges and findings</h4>" + ("<ul>" + "".join(
        f'<li>{e(c.get("finding", ""))} <span class="muted">[{e(c.get("source", ""))}]</span></li>'
        for c in f["challenges"]) + "</ul>" if f["challenges"]
        else '<p class="muted">None recorded.</p>'))
    for title, key, empty in (("Still to do", "todo", "None recorded."),
                              ("Open blockers", "blockers", "None recorded."),
                              ("Decisions", "decisions", "None recorded."),
                              ("Risks", "risks", "None recorded.")):
        parts.append(f"<h4>{title}</h4>{ul(f[key], empty)}")
    parts.append(f"<h4>Key files</h4><p>{code_list(f['key_files']) or 'None.'}</p></details>")
    return "".join(parts)


# --------------------------------------------------------------------------- engines section (shared)

ENG_NAMES = {"stomp": "HX Stomp (the pedal)", "native": "Helix Native (software)"}


def eng_cap_text(c: dict | None) -> str:
    if not c:
        return ""
    return c["verdict"] + (" Verified on fw 3.50, recheck on 3.80 pending." if c.get("fw350") else "")


def eng_est_cell(c: dict, which: str) -> str:
    t = engine_timing.est_text(c, which)
    if t != engine_timing.NOT_MEASURED and c.get("assume_n"):
        t += " (note " + ", ".join(str(n) for n in c["assume_n"]) + ")"
    return t


def eng_job_cells(j: dict) -> dict:
    """Display strings for one real-world job row, shared by both renderers."""
    nm = engine_timing.NOT_MEASURED

    def one(v: float | None) -> str:
        if v is None or j["serial_s"] is None:
            return nm
        pre = "at least " if j["lower"] else ""
        return f"{pre}{engine_timing.fmt_dur(v)} (est., from {j['date']})" if j.get("date") else (
            f"{pre}{engine_timing.fmt_dur(v)} (est.)")
    par = one(j["parallel_s"])
    if j["parallel_s"] is not None and j["serial_s"] is not None and j.get("par_note"):
        par += f"; {j['par_note']}"
    dom = ""
    if j.get("dominant"):
        dom = (f"{engine_timing.DOM_LABEL.get(j['dominant'], j['dominant'])} "
               f"({engine_timing.fmt_dur(j['dominant_s'])})")
    if j.get("extra"):
        dom = (dom + "; " if dom else "") + j["extra"]
    return {"serial": one(j["serial_s"]), "parallel": par, "dominant": dom or nm if j["serial_s"] is None
            else dom}


def _eng_label(en: dict, key: str) -> tuple[str, str]:
    lab = en["labels"].get(key) or {"label": key, "means": ""}
    return lab["label"], lab.get("means", "")


def _engines_html(model: dict, gl) -> str:
    en = model.get("engines")
    if not en:
        return ""
    p = ['<section id="engines"><h2>Engines: what each can do and how fast</h2>',
         f'<p class="muted">{gl(en["units"])}</p>',
         "<h3>What each engine can do</h3>",
         ('<div class="scroll"><table><thead><tr><th scope="col">Capability</th>'
          f'<th scope="col">{e(ENG_NAMES["stomp"])}</th><th scope="col">{e(ENG_NAMES["native"])}</th>'
          "</tr></thead><tbody>")]
    for c in en["capabilities"]:
        if c["both"]:
            p.append(f'<tr><th scope="row">{gl(c["capability"])}</th><td colspan="2">'
                     f'{gl(eng_cap_text(c["both"]))}</td></tr>')
        else:
            p.append(f'<tr><th scope="row">{gl(c["capability"])}</th>'
                     f'<td>{gl(eng_cap_text(c["stomp"]))}</td><td>{gl(eng_cap_text(c["native"]))}</td></tr>')
    p.append("</tbody></table></div>")
    p.append("<h3>Measured time per test</h3>"
             '<p class="muted">Each figure is the newest ledger row for that activity, with its date '
             "and a link to the row. Pedal results from the older firmware are marked until they are "
             "rechecked on 3.80; rows measured on 3.80 say so. Native pool figures use 6 hosts, the cap "
             "the human approved on 2026-10-16 (docs/DECISIONS.md); results before that date are kept as "
             "history. The table regenerates from the ledger every run.</p>"
             '<div class="scroll"><table><thead><tr><th scope="col">Activity</th>'
             f'<th scope="col">{e(ENG_NAMES["stomp"])}</th><th scope="col">{e(ENG_NAMES["native"])}</th>'
             '<th scope="col">Either engine / both</th></tr></thead><tbody>')
    for m in en["measured"]:
        p.append(f'<tr><th scope="row">{gl(m["label"])}<br><span class="muted">{gl(m["means"])}</span></th>'
                 + "".join(f"<td>{gl(engine_timing.cell_text(m[c]))}</td>" for c in ("stomp", "native", "any"))
                 + "</tr>")
    p.append("</tbody></table></div>")
    p.append(f"<h3>Estimated time for {en['n']} tests</h3>"
             f'<p class="muted">Each estimate is {en["n"]} times the measured time above. Where a speed-up '
             "from running things in parallel has been measured, the second column shows it (Native: several "
             "engine hosts at once; Stomp: two-path passes, one pass rendering both Path A and Path B), with the "
             "assumption it rests on listed below the table. Numbers marked \"not measured\" have no "
             "measurement behind them.</p>"
             '<div class="scroll"><table><thead><tr><th scope="col">Activity</th>'
             '<th scope="col">Stomp, one after another</th><th scope="col">Stomp, two-path passes</th>'
             '<th scope="col">Native, one after another</th><th scope="col">Native, in parallel</th>'
             '<th scope="col">Both or neither, one after another</th></tr></thead><tbody>')
    for r in en["estimates"]:
        lab, means = _eng_label(en, r["key"])
        head = f"<strong>{gl(lab)}</strong>" if r["kind"] == "combined" else gl(lab)
        p.append(f'<tr><th scope="row">{head}<br><span class="muted">{gl(means)}</span></th>'
                 f'<td>{gl(eng_est_cell(r["stomp"], "serial"))}</td><td>{gl(eng_est_cell(r["stomp"], "parallel"))}</td>'
                 f'<td>{gl(eng_est_cell(r["native"], "serial"))}</td><td>{gl(eng_est_cell(r["native"], "parallel"))}</td>'
                 f'<td>{gl(eng_est_cell(r["any"], "serial"))}</td></tr>')
    p.append("</tbody></table></div>")
    if en["assumptions"]:
        p.append('<p class="muted">Assumptions behind the estimates:</p><ol>' + "".join(
            f"<li>{gl(a['text'])}</li>" for a in en["assumptions"]) + "</ol>")
    p.append("<h3>Real-world jobs: how long the things we actually do take</h3>"
             '<p class="muted">Typical sizes come from the repository (cited in each row); times are those '
             "sizes multiplied by the measured per-unit times above. Estimates, not measurements, and "
             "they regenerate from the ledger and notes every run.</p>"
             '<div class="scroll"><table><thead><tr><th scope="col">Job and what it is for</th>'
             '<th scope="col">Engine</th><th scope="col">Typical size and where it comes from</th>'
             '<th scope="col">Per-unit time used</th><th scope="col">One after another</th>'
             '<th scope="col">In parallel</th><th scope="col">What dominates</th></tr></thead><tbody>')
    for j in en["jobs"]:
        c = eng_job_cells(j)
        p.append(f'<tr><th scope="row">{gl(j["name"])}<br><span class="muted">{gl(j["purpose"])}</span></th>'
                 f'<td>{gl(j["engine"])}</td><td>{gl(j["size"])}<br><span class="muted">Source: {e(j["source"])}</span></td>'
                 f'<td>{gl(j["unit"])}</td><td>{gl(c["serial"])}</td><td>{gl(c["parallel"])}</td>'
                 f'<td>{gl(c["dominant"])}</td></tr>')
    p.append("</tbody></table></div>")
    p.append(f'<p class="summary"><strong>Takeaway.</strong> {gl(en["takeaway"])}</p></section>')
    return "".join(p)


def _engines_md(doc: MdDoc, model: dict) -> None:
    en = model.get("engines")
    if not en:
        return
    doc.h(2, "Engines: what each can do and how fast", "engines")
    doc.add(mde(en["units"]), "")
    doc.h(3, "What each engine can do", "eng-can")
    doc.add(f"| Capability | {ENG_NAMES['stomp']} | {ENG_NAMES['native']} |", "|---|---|---|")
    for c in en["capabilities"]:
        if c["both"]:
            doc.add(f"| {mde(c['capability'])} | {mde(eng_cap_text(c['both']))} (both engines) | |")
        else:
            doc.add(f"| {mde(c['capability'])} | {mde(eng_cap_text(c['stomp']))} | "
                    f"{mde(eng_cap_text(c['native']))} |")
    doc.h(3, "Measured time per test", "eng-measured")
    doc.add("Each figure is the newest ledger row for that activity, with its date and a link to the row. "
            "Pedal results from the older firmware are marked until they are rechecked on 3.80; rows measured "
            "on 3.80 say so. Native pool figures use 6 hosts, the cap the human approved on 2026-10-16 "
            "(docs/DECISIONS.md); results before that date are kept as history. "
            "The table regenerates from the ledger every run.", "",
            f"| Activity | {ENG_NAMES['stomp']} | {ENG_NAMES['native']} | Either engine / both |",
            "|---|---|---|---|")
    for m in en["measured"]:
        doc.add(f"| {mde(m['label'])} | " + " | ".join(
            mde(engine_timing.cell_text(m[c])) for c in ("stomp", "native", "any")) + " |")
    doc.h(3, f"Estimated time for {en['n']} tests", "eng-estimates")
    doc.add(f"Each estimate is {en['n']} times the measured time above. Where a speed-up from running "
            "things in parallel has been measured, the second column shows it (Native: several engine hosts at once; "
            "Stomp: two-path passes, one pass rendering both Path A and Path B), with the assumption it "
            "rests on listed below the table. Numbers marked \"not measured\" have no measurement behind them.",
            "", "| Activity | Stomp, one after another | Stomp, two-path passes | Native, one after another | "
            "Native, in parallel | Both or neither, one after another |", "|---|---|---|---|---|---|")
    for r in en["estimates"]:
        lab, _ = _eng_label(en, r["key"])
        head = f"**{mde(lab)}**" if r["kind"] == "combined" else mde(lab)
        doc.add(f"| {head} | {mde(eng_est_cell(r['stomp'], 'serial'))} | {mde(eng_est_cell(r['stomp'], 'parallel'))} | "
                f"{mde(eng_est_cell(r['native'], 'serial'))} | {mde(eng_est_cell(r['native'], 'parallel'))} | "
                f"{mde(eng_est_cell(r['any'], 'serial'))} |")
    if en["assumptions"]:
        doc.add("", "Assumptions behind the estimates:", "")
        doc.add(*[f"{a['n']}. {mde(a['text'])}" for a in en["assumptions"]])
    doc.h(3, "Real-world jobs: how long the things we actually do take", "eng-jobs")
    doc.add("Typical sizes come from the repository (cited in each row); times are those sizes multiplied "
            "by the measured per-unit times above. Estimates, not measurements, and they regenerate from the "
            "ledger and notes every run.", "",
            "| Job and what it is for | Engine | Typical size (source) | Per-unit time used | One after another "
            "| In parallel | What dominates |", "|---|---|---|---|---|---|---|")
    for j in en["jobs"]:
        c = eng_job_cells(j)
        doc.add(f"| {mde(j['name'])}: {mde(j['purpose'])} | {mde(j['engine'])} | {mde(j['size'])} "
                f"(source: {mde(j['source'])}) | {mde(j['unit'])} | {mde(c['serial'])} | {mde(c['parallel'])} "
                f"| {mde(c['dominant'])} |")
    doc.add("", f"**Takeaway.** {mde(en['takeaway'])}")


# --------------------------------------------------------------------------- Markdown renderer


def mde(x: object) -> str:
    """Escape text for Markdown prose or a table cell (no raw HTML, no pipes, one line)."""
    t = " ".join(str(x).split())
    t = t.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in ("|", "`", "*", "[", "]"):
        t = t.replace(ch, "\\" + ch)
    return t


def md_code(x: object) -> str:
    t = " ".join(str(x).split()).replace("|", "&#124;")
    return f"`` {t} ``" if "`" in t else f"`{t}`"


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
        title = evidence_links.md_title(evidence_links.file_description(root, clean))
        return f'[{md_code(clean)}](../../{urllib.parse.quote(clean)} "{title}")'
    return md_code(clean)


class MdDoc:
    """Collects lines and headings so that links to headings use GitHub's real anchors."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.used: dict[str, int] = {}
        self.anchor: dict[str, str] = {}

    def h(self, level: int, text: str, key: str | None = None) -> None:
        self.anchor[key or text] = gh_slug(text, self.used)
        self.lines += ["", f"{'#' * level} {text}", ""]

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def text(self) -> str:
        body = "\n".join(self.lines)
        body = re.sub(r"@@a:([^@]+)@@", lambda m: "#" + self.anchor.get(m[1], m[1]), body)
        return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"


def md_refs(model: dict, ids: list[str]) -> str:
    by_id = {x["id"]: x for x in model["evidence"]}
    return "; ".join(by_id[i]["label"] for i in ids if i in by_id)


def render_markdown(model: dict, root: Path | None = None, board_rel: str = "../../board") -> str:
    """Render the report model as standalone GitHub-flavoured Markdown."""
    a, rep, d = model["assessments"], model["report"], model["details"]
    all_paths = sorted({p for f in d["features"] for i in f["implemented"]
                        for p in i.get("paths", [])} | {p for f in d["features"]
                                                         for p in f["key_files"]})
    ignored = git_ignored(root, all_paths) if root is not None else set()
    doc = MdDoc()
    doc.add(f"# {mde(model['project']['name'])}: project status", "")
    doc.add(f"Report date **{rep['date']}** · reporting period {rep['period_from']} to "
            f"{rep['period_to']} · for {mde(rep['generated_for'])}  ")
    doc.add("_Generated by `scripts/status_report.py` from one report model; do not edit by hand. "
            "The same conclusions appear in [index.html](index.html) (open locally for the "
            "visual dashboard)._")
    doc.add("", "@@TOC@@")

    # --- executive dashboard
    doc.h(2, "Executive dashboard", "dash")
    doc.add(f"**What we are building.** {mde(model['project']['goal'])}", "")
    h, t, r = a["health"], a["technical"], a["readiness"]
    doc.add(f"**Project Health:** {HEALTH_GLYPH[h['value']]} {h['value']}  ",
            f"Reason: {mde(h['rationale'])}", "")
    doc.add(f"**Technical Progress:** {t['value']}  ", f"Reason: {mde(t['rationale'])}", "")
    doc.add(f"**Product Readiness:** {r['stage']} (stage {r['stage_index']} of {len(r['stages'])})  ",
            f"Reason: {mde(r['rationale'])}  ",
            f"Blocking the next stage: {mde(r['blocker_to_next'])}", "")
    doc.add("Readiness stages (the highest demonstrated is in bold): " + " → ".join(
        f"**{s['name']}**" if i == r["stage_index"] else s["name"]
        for i, s in enumerate(r["stages"], 1)), "")
    doc.add(f"**Summary:** {mde(model['summary'])}", "")
    bp = model["biggest_problem"]
    doc.add(f"> **Biggest problem: {mde(bp['title'])}** ({bp['priority']}, {bp['classification']})",
            ">",
            f"> **What is wrong.** {mde(bp['what'])}  ",
            f"> **Why it matters.** {mde(bp['why'])}  ",
            f"> **What it prevents.** {mde(bp['prevents'])}  ",
            f"> **What resolves it.** {mde(bp['resolution'])}  ",
            f"> **Next step.** {mde(bp['next_step'])}", "")
    nm = model["next_milestone"]
    if nm:
        doc.add(f"**Next milestone: {mde(nm['name'])}** ({nm['state']}). {mde(nm['goal'])}  ",
                f"Readiness: {mde(nm['readiness'])}.  ",
                f"What must happen: {mde(nm['needs'])}  ",
                *([f"Blocked by: {mde(nm['blocking'])}  "] if nm["blocking"] else []),
                f"Next action: {mde(nm['next_action'])}", "")
    else:
        doc.add("**No open milestone: every gate is complete or closed as superseded.**", "")

    # --- problems
    doc.h(2, "Key problems and blockers", "problems")
    doc.add("Ranked by consequence for the goal. Missing evidence is labelled "
            "Unknown/Unverified and is not counted as a failure.", "")
    doc.add("| Priority | Problem | Classification |", "|---|---|---|")
    for i in model["issues"]:
        if i["executive"]:
            doc.add(f"| {i['priority']} | [{mde(i['title'])}](@@a:issue-{i['id']}@@) | "
                    f"{i['classification']} |")
    for i in model["issues"]:
        if i["executive"]:
            _md_issue(doc, i, model, 3)
    rest = [i for i in model["issues"] if not i["executive"]]
    if rest:
        doc.h(3, "Lower-priority issues", "lowissues")
        doc.add("| Priority | Problem | Classification |", "|---|---|---|")
        for i in rest:
            doc.add(f"| {i['priority']} | [{mde(i['title'])}](@@a:issue-{i['id']}@@) | "
                    f"{i['classification']} |")
        for i in rest:
            _md_issue(doc, i, model, 4)

    # --- milestones
    doc.h(2, "Milestone progress", "milestones")
    doc.add("Each milestone is a gate in the project plan with a written checklist and the "
            "owner's sign-off. No deadlines are set, so none are claimed.", "")
    doc.add("| Milestone | Level | State | Criteria evidenced | Owner sign-off |", "|---|---|---|---|---|")
    for m in model["milestones"]:
        up = " (next)" if nm and m["id"] == nm["id"] else ""
        doc.add(f"| [{mde(m['name'])}](@@a:{m['id']}@@){up} | {m['tier']} | **{m['state']}** | "
                f"{m['criteria_done']} of {m['criteria_total']} | "
                f"{'given' if m['signed'] else 'pending'} |")
    for m in sorted(model["milestones"], key=lambda m: nm is None or m["id"] != nm["id"]):
        doc.h(3, f"{m['name']}: {m['title']}" + (" (next milestone)" if nm and m["id"] == nm["id"]
                                                   else ""), m["id"])
        doc.add(f"**State: {m['state']}** · {m['tier']} milestone. {mde(m['goal'])}", "")
        doc.add(*[f"- {'[x]' if c['done'] else '[ ]'} {mde(c['text'])}" for c in m["criteria"]], "")
        doc.add(f"**Demonstrated.** {mde(m['demonstrated'])}  ",
                f"**Remaining.** {mde(m['remaining'])}  ",
                *([f"**Blocking.** {mde(m['blocking'])}  "] if m["blocking"] else []),
                f"**Next action.** {mde(m['next_action'])}  ")
        if m["evidence"]:
            doc.add(f"Evidence: {mde(md_refs(model, m['evidence']))}.")

    # --- what works
    doc.h(2, "What works and what does not", "works")
    caps = model["capabilities"]
    for key, title in (("verified", "Working and verified"),
                       ("unproven", "Implemented but not yet proven"),
                       ("missing", "Missing or not working")):
        doc.h(3, title, "cap-" + key)
        doc.add("| Capability | Current reality |", "|---|---|")
        for c in caps[key]:
            doc.add(f"| {mde(c['capability'])} | {mde(c['reality'])} |")

    # --- actions
    doc.h(2, "Human decisions and actions", "actions")
    doc.add("One list, each action once. Routine work owned by the agents is not listed here.")
    for gk, glabel in ACTION_GROUPS:
        doc.h(3, glabel, "act-" + gk)
        items = [x for x in model["actions"] if x["group"] == gk]
        if not items:
            doc.add("_None today: no human action is currently holding up the agents' work._")
        for x in items:
            doc.add(f"- **{mde(x['title'])}** ({x['priority']})  ",
                    f"  Why: {mde(x['why'])}  ",
                    f"  Unblocks: {mde(x['unblocks'])}  ",
                    f"  Recommended: {mde(x['recommended'])}  ",
                    f"  If deferred: {mde(x['defer'])}")

    # --- changes
    ch = model["changes"]
    doc.h(2, "What changed since the last report", "changes")
    if ch["baseline_date"]:
        doc.add(f"Compared with the report snapshot of {ch['baseline_date']}.", "")
        doc.add(*([f"- {mde(x['text'])}" for x in ch["diff"]] or
                  ["No change in the assessments, milestones, issues, actions or components."]))
    else:
        doc.add("_No earlier report snapshot exists, so there is no automatic comparison yet. "
                "This report saved one; the next report will diff against it. Until then the "
                "lists below come from the project's own records (ledger, decision log, board) "
                f"for the period from {rep['period_from']}._")
    gen = [c for c in ch["recent"] if c["kind"] != "activity"]
    act = [c for c in ch["recent"] if c["kind"] == "activity"]
    doc.h(3, "Progress and findings", "progress")
    for c in gen:
        doc.add(f"- {c['date']} · **{CHANGE_KINDS[c['kind']]}.** {mde(c['text'])} "
                f"_(Evidence: {mde(md_refs(model, c['evidence']))})_")
    if ch.get("in_progress"):
        doc.h(3, "In progress now", "in-progress")
        for w in ch["in_progress"]:
            doc.add(f"- **{mde(w['area'])}.** {mde(w['text'])} "
                    f"_(Evidence: {mde(md_refs(model, w['evidence']))})_")
    elif ch.get("up_next"):
        doc.add("", "_Nothing is in progress right now._")
    if ch.get("up_next"):
        doc.h(3, "Up next", "up-next")
        for w in ch["up_next"]:
            doc.add(f"- **{mde(w['area'])}.** {mde(w['text'])} "
                    f"_(Evidence: {mde(md_refs(model, w['evidence']))})_")
    if act:
        doc.h(3, "Activity that did not advance readiness", "activity")
        for c in act:
            doc.add(f"- {c['date']} · {mde(c['text'])}")

    # --- components
    doc.h(2, "Component status", "components")
    doc.add("| Component | Status | Strongest evidence |", "|---|---|---|")
    for c in model["components"]:
        doc.add(f"| [{mde(c['name'])}](@@a:comp-{c['id']}@@) | **{c['status']}** | "
                f"{mde(c['strongest_evidence'])} |")
    for c in model["components"]:
        doc.h(3, c["name"], "comp-" + c["id"])
        doc.add(f"**{c['status']}.** {mde(c['purpose'])}", "",
                f"- **What works.** {mde(c['works'])}",
                f"- **What is missing.** {mde(c['missing'])}",
                f"- **Primary problem.** {mde(c['problem'])}",
                f"- **Next step.** {mde(c['next'])}",
                f"- **Evidence confidence.** {mde(c['confidence'])}",
                "- **Detail.** " + ", ".join(
                    f"[{mde(f['name'])}](@@a:feat-{f['id']}@@)" for f in c["features"]))

    # --- engines
    _engines_md(doc, model)

    # --- technical details
    doc.h(2, "Technical details and evidence", "technical")
    doc.add("Supporting material: per-feature detail, gate checklists, evidence references, the "
            "board and ledger. Internal task ids, agent names and file paths live here.")
    doc.h(3, "Feature details", "features")
    doc.add("| Feature | Status | Strongest evidence | Board tasks |", "|---|---|---|---|")
    for f in d["features"]:
        tp = f["tasks"]
        doc.add(f"| [{mde(f['name'])}](@@a:feat-{f['id']}@@) | {f['status']} | "
                f"{mde(f['evidence_labels'][-1] if f['evidence_labels'] else 'none')} | "
                f"{tp['done']}/{tp['total']}"
                f"{' (+' + str(tp['wip']) + ' in progress)' if tp['wip'] else ''} |")
    for f in d["features"]:
        _md_feature(doc, f, root, ignored)
    doc.h(3, "Gate checklists", "gates")
    for g in d["gates"]:
        doc.add(f"**{mde(g['name'])}: {mde(g['title'])}** ({g['done']} of {g['total']} criteria "
                f"ticked, sign-off {'given' if g['signed'] else 'pending'})", "")
        doc.add(*[f"- {'[x]' if c['done'] else '[ ]'} {mde(c['text'])}" for c in g["criteria"]], "")
    ec = model.get("evidence_classes")
    if ec:
        doc.h(3, "Evidence classes of the headline claims", "evidence-classes")
        if ec.get("about"):
            doc.add(mde(ec["about"]), "")
        doc.add(*[f"- **Class {mde(x['class'])}**: {mde(x['meaning'])}" for x in ec["legend"]], "")
        doc.add("| Claim | Class | n | Limits | Evidence |", "|---|---|---|---|---|")
        for c in ec["claims"]:
            doc.add(f"| {mde(c['claim'])} | **{mde(c['class'])}** | {mde(c['n'])} | "
                    f"{mde(c['limit'])} | {md_refs(model, c['evidence'])} |")
        doc.add("")
    doc.h(3, "Evidence references", "evidence")
    doc.add("| Reference | What it shows | Source |", "|---|---|---|")
    for x in model["evidence"]:
        if x["kind"] == "ledger":
            src = f"ledger row {x['ref']}" + (f" ({x['summary']})" if x.get("summary") else "")
            src = mde(src)
        else:
            src = md_path(x["ref"], root, ignored)
        doc.add(f"| {md_code(x['id'])} | {mde(x['label'])} | {src} |")
    doc.h(3, "Open items for the lead (agent-owned, not human actions)", "lead")
    doc.add(*([f"- {mde(i)}" for i in d["lead_items"]] or ["_None._"]))
    doc.h(3, "Discrepancies between sources", "disc")
    doc.add(*([f"- {mde(i)}" for i in d["discrepancies"]] or ["_None recorded._"]))
    if d["quality_notes"]:
        doc.h(3, "Report quality notes", "quality")
        doc.add(*[f"- {mde(i)}" for i in d["quality_notes"]])
    doc.h(3, "Ledger and board", "ledgerboard")
    doc.add(f"{d['ledger']['rows']} ledger rows ("
            + mde(", ".join(f"{k}: {v}" for k, v in d["ledger"]["kinds"].items()))
            + f"). Board: {d['tasks']['done']} tasks done, {d['tasks']['wip']} in progress, "
              f"{d['tasks']['total']} listed.")
    doc.h(3, "Recent commits", "commits")
    doc.add(*([f"- {mde(c)}" for c in d["git"].get("recent", [])] or ["_Git history unavailable._"]))

    # --- glossary
    doc.h(2, "Glossary", "glossary")
    doc.add("| Term | Meaning |", "|---|---|")
    for x in model["glossary"]:
        doc.add(f"| {mde(x['term'])} | {mde(x['definition'])} |")

    # --- footer
    g = d["git"]
    doc.h(2, "Report information", "info")
    doc.add(f"Generated {rep['generated_at']} for {mde(rep['cycle'])}. Git commits: "
            f"{mde(g['count'] or 'unknown')}"
            + (f", last: {mde(g['last'])}" if g.get("last") else "")
            + (f", {g['dirty']} uncommitted paths" if g.get("dirty") else "") + ".", "")
    if d["checks"] is None:
        doc.add("_Checks were not run for this report (use --run-checks)._")
    else:
        doc.add(*[f"- **{'PASS' if c['ok'] else 'FAIL'}** {mde(c['name'])}: {mde(c['detail'])}"
                  for c in d["checks"]])
    doc.add("", f"Gates: {mde(d['gate_status'])}.")

    toc = ["**Contents:** " + " · ".join(
        f"[{label}](@@a:{key}@@)" for key, label in (
            ("dash", "Dashboard"), ("problems", "Problems"), ("milestones", "Milestones"),
            ("works", "What works"), ("actions", "Human actions"), ("changes", "What changed"),
            ("components", "Components"), *((("engines", "Engines"),) if model.get("engines") else ()),
            ("technical", "Technical details"), ("glossary", "Glossary")))]
    doc.lines = [ln if ln != "@@TOC@@" else toc[0] for ln in doc.lines]
    page = doc.text()
    if root is not None:
        idx = evidence_links.Index(root, board_rel[:-len("/board")])
        page = evidence_links.linkify_markdown(link_ledger_metrics(page, idx, evidence_links._md_fmt), idx)
    return task_links.linkify_markdown(page, model.get("task_index", {}), board_rel)


def _md_issue(doc: MdDoc, i: dict, model: dict, level: int) -> None:
    doc.h(level, i["title"], "issue-" + i["id"])
    doc.add(f"**{i['priority']}** · {i['classification']}", "",
            f"- **Problem.** {mde(i['problem'])}",
            f"- **Impact.** {mde(i['impact'])}",
            f"- **Cause.** {mde(i['cause'])}",
            f"- **Resolution.** {mde(i['resolution'])}",
            f"- **Owner.** {mde(i['owner'])}",
            f"- **Next step.** {mde(i['next_step'])}")
    if i["evidence"]:
        doc.add(f"- **Evidence.** {mde(md_refs(model, i['evidence']))}")


def _md_feature(doc: MdDoc, f: dict, root: Path | None, ignored: set[str]) -> None:
    tp = f["tasks"]
    doc.h(4, f["name"], "feat-" + f["id"])
    doc.add(f"**Status:** {f['status']} · **Health:** {f['health']} · **Evidence:** "
            f"{mde(', '.join(f['evidence_labels']) or 'none')}", "",
            f"**Purpose.** {mde(f['purpose'])}  ",
            f"**Done when.** {mde(f['done_when'])}  ",
            f"**Functionality today.** {mde(f['functionality'])}  ",
            *([f"**Progress note.** {mde(f['progress_note'])}  "] if f["progress_note"] else []),
            f"**Board tasks.** {tp['done']} of {tp['total']} done"
            + (f", {tp['wip']} in progress" if tp["wip"] else "")
            + (f" (not on the board: {mde(', '.join(tp['missing']))})" if tp["missing"] else "")
            + (f": {mde(', '.join(tp['ids']))}" if tp["ids"] else "") + "  ",
            f"**Owners.** {mde(', '.join(f['owners']) or 'none')}", "")
    doc.add("**Implemented**", "")
    doc.add(*([f"- {mde(i.get('item', ''))} " + " ".join(
        md_path(p, root, ignored) for p in i.get("paths", [])) for i in f["implemented"]]
              or ["_Nothing implemented yet._"]), "")
    doc.add("**Evidence**", "")
    doc.add(*([f"- **{x['label']}**: {mde('; '.join(x['refs']) or 'no reference')}"
               for x in f["evidence"]] or ["- **none**: no evidence recorded"]))
    if f["ledger_filter"]:
        doc.add("", f"**Live ledger rows:** {f['ledger_count']} match ({md_code(f['ledger_filter'])})")
        if f["ledger_recent"]:
            doc.add("", "| timestamp | agent | kind | dataset | metric | value |", "|---|---|---|---|---|---|")
            for r in f["ledger_recent"]:
                doc.add("| " + " | ".join(mde(r[c]) for c in (
                    "timestamp", "agent", "kind", "dataset", "metric", "value")) + " |")
    doc.add("", "**Challenges and findings**", "")
    doc.add(*([f"- {mde(c.get('finding', ''))} _[{mde(c.get('source', ''))}]_"
               for c in f["challenges"]] or ["_None recorded._"]))
    for title, key in (("Still to do", "todo"), ("Open blockers", "blockers"),
                       ("Decisions", "decisions"), ("Risks", "risks")):
        doc.add("", f"**{title}**", "")
        doc.add(*([f"- {mde(x)}" for x in f[key]] or ["_None recorded._"]))
    kf = f["key_files"]
    doc.add("", "**Key files.** " + (" · ".join(md_path(p, root, ignored) for p in kf)
                                      if kf else "None."))


# --------------------------------------------------------------------------- main


def make_model(root: Path, reg_path: Path | None, cur_path: Path | None, checks: bool,
               facts: dict | None = None) -> tuple[dict, list[str]]:
    """Load, validate and build; returns (model, problems). problems non-empty means no model."""
    reg = load_registry(reg_path or root / "docs" / "features.json")
    errs = validate(reg)
    if errs:
        return {}, ["registry: " + m for m in errs]
    cur = load_json(cur_path or root / "docs" / "status-curated.json", "curated status file")
    facts = facts or collect_facts(root, checks)
    errs = validate_curated(cur, reg, facts)
    if errs:
        return {}, errs
    today = facts["now"].strftime("%Y-%m-%d")
    baseline = load_baseline(root / "reports" / "status" / "history", today)
    model = build_model(reg, cur, facts, baseline)
    model["task_index"] = facts.get("task_index", {})
    if model["task_index"]:
        mentioned = set(task_links.ID_RE.findall(json.dumps(model["details"], default=str)))
        for tid in sorted(mentioned - set(model["task_index"])):
            model["details"]["discrepancies"].append(
                f"{tid} is mentioned in this report but is not on board/TASKS.md or board/CYCLES.md, "
                "so it is shown without a link.")
    idx = evidence_links.Index(root, "..")
    for msg in sorted(evidence_links.collect_unresolved(
            {k: v for k, v in model.items() if k not in ("task_index",)}, idx)):
        model["details"]["discrepancies"].append(msg + "; shown as plain text.")
    return model, []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--registry", type=Path, default=None)
    ap.add_argument("--curated", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="HTML output path (default reports/status/index.html)")
    ap.add_argument("--md-out", type=Path, default=None,
                    help="Markdown output path (default reports/status/README.md)")
    ap.add_argument("--format", default="html,md", help="comma list of html, md (default: both)")
    ap.add_argument("--run-checks", action="store_true", help="run pytest, ruff, licence_check")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="do not write reports/status/history/<date>.json")
    ap.add_argument("--json", type=Path, default=None, help="also write the report model here")
    a = ap.parse_args(argv)
    formats = [x.strip() for x in a.format.split(",") if x.strip()]
    if not formats or [x for x in formats if x not in ("html", "md")]:
        ap.error(f"--format takes html, md or html,md (got {a.format!r})")
    root: Path = a.root
    model, errs = make_model(root, a.registry, a.curated, a.run_checks)
    if errs:
        print("status_report: cannot build the report:", file=sys.stderr)
        for m in errs:
            print(f"  - {m}", file=sys.stderr)
        return 2
    if "html" in formats:
        out = a.out or root / "reports" / "status" / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(model, task_links.rel_prefix(root, out.resolve().parent), root),
                       encoding="utf-8")
        print(out)
    if "md" in formats:
        mout = a.md_out or root / "reports" / "status" / "README.md"
        mout.parent.mkdir(parents=True, exist_ok=True)
        mout.write_text(render_markdown(model, root, task_links.rel_prefix(root, mout.resolve().parent)),
                        encoding="utf-8")
        print(mout)
    try:
        if engine_timing.update_benchmarking_doc(root):
            print("docs/BENCHMARKING.md: time-per-sample block refreshed")
    except (OSError, ValueError) as exc:
        print(f"status_report: could not refresh the time-per-sample table: {exc}", file=sys.stderr)
    try:   # local Gate 2 report (gitignored); a failure here never breaks the status report
        import gate2_report

        print(gate2_report.build(root))
    except Exception as exc:  # noqa: BLE001
        print(f"status_report: could not refresh the local Gate 2 report: {exc}", file=sys.stderr)
    try:   # local offline site of reports and docs (gitignored); a failure never breaks the status report
        import local_site

        print(local_site.build(root))
    except Exception as exc:  # noqa: BLE001
        print(f"status_report: could not refresh the local docs site: {exc}", file=sys.stderr)
    if a.json:
        a.json.write_text(json.dumps(model, indent=1, ensure_ascii=False, default=str) + "\n",
                          encoding="utf-8")
    if not a.no_snapshot:
        print(write_snapshot(root / "reports" / "status" / "history", model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
