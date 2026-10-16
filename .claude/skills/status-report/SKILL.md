---
name: status-report
description: Generate the human-centred project status report (HTML dashboard plus GitHub Markdown, from one report model): project health, technical progress, product readiness, biggest problem, milestones, prioritised issues, deduplicated human actions, what changed, component status, glossary and technical evidence. Use at the end of every cycle after plan-sync, and whenever someone asks where a feature stands.
---

# status-report

**Follow `docs/STATUS-REPORT-SPEC.md` (the owner's specification). Where this file and the spec differ, the spec wins.**

Run as `status-reporter` (or as `tone-matcher-lead` if that agent is unavailable) **at the end of every cycle, after `plan-sync`**. Output, both produced every cycle from the same registry and facts: `reports/status/index.html` (self-contained HTML) and `reports/status/README.md` (GitHub-flavoured Markdown, which GitHub renders when browsing the folder).

## Inputs (read, do not guess)

`board/TASKS.md`, `board/GATES.md`, `board/CYCLES.md`, `ledger/runs.csv`, `docs/DECISIONS.md`, `docs/PLAN.md` (Risks, Open questions), `docs/notes/*`, `contracts/*`, `reviews/*`, `git log`, and the code under `src/tonematch/` and `scripts/`.

## Steps

1. **Refresh the registry** (`docs/features.json`): per feature `status`, `functionality`, `evidence` (highest level actually supported, with ledger timestamps or test files), `implemented` (with paths), `challenges`, `todo`, `needs_human`, `decisions`, `risks`, `tasks`, `ledger` filters, `progress_note`. No completion percentage (the validator rejects `completion`). Never delete a feature; mark it `retired`.
2. **Refresh the curated judgements** (`docs/status-curated.json`) from current evidence: the three assessments (Project Health, Technical Progress, Product Readiness stage, each with a rationale and, for readiness, what blocks the next stage), the 100-150 word plain-English summary, the biggest problem, issues (priority, classification, impact, cause, resolution, owner, next step; 3-5 flagged executive), milestone text per gate (state is derived from `board/GATES.md`: Complete only when signed; only At Risk, Blocked or In Progress may be set by hand), the deduplicated human actions (Blocking now, Needed soon, Later), the capability lists, recent changes (each with evidence ids), component groupings and the glossary. Every claim cites an evidence id whose ledger timestamp or file exists. Explain abbreviations in the glossary; keep file names, task ids and agent names out of the summary and rationales. Do not ask the human for something that can wait, and do not list agent-owned items as human actions (list them under `agent_owned`).
3. **Check against the board.** Task marked done but the code or ledger does not support it, or sources disagree: add it to `discrepancies` and tell the lead.
4. **Render.** Run `python scripts/board_anchors.py --check` first (exit 1 means plan-sync has not anchored the board; run it without `--check`). The script links every task id in both outputs to its board anchor with the task name on hover (`scripts/task_links.py`, paths relative to each output's folder); ids not on the board stay plain and appear under Discrepancies. Write task ids as plain `T042` in `docs/features.json` and `docs/status-curated.json`, never as links. The script also links evidence in both outputs (`scripts/evidence_links.py`): ledger timestamps to their line in `ledger/runs.csv`, `DECISIONS <date> <words>` to the heading in `docs/DECISIONS.md`, `Gate N` to `board/GATES.md`, repo paths (with a file description as hover), and test names to their line. Write citations in curated text in those forms (a full timestamp; a decision's date plus two or three words of its heading; `tests/test_x.py::test_name`); unresolvable or ambiguous ones stay plain and appear under Discrepancies, so fix the citation rather than the report. `python scripts/status_report.py --run-checks` (add `--no-snapshot` for a trial run). It writes both reports and today's snapshot in `reports/status/history/`. The next run diffs against the newest earlier snapshot; with none it says so.
5. **Read both back as a human** (the spec's 30-second, 60-second, 2-minute tests): health, readiness and biggest problem visible at once; the actions list has no repeats; HTML and Markdown agree; nothing claims more than its evidence; glossary covers every abbreviation. Fix the JSON and re-render.
6. **Report.** One paragraph to the lead: the three assessments, what changed, the biggest problem, the human actions, discrepancies.

## Vocabularies (fixed)

- **status**: `not-started` (nothing built), `designing` (notes or plan only), `prototype` (code exists but unproven or partial), `working` (does its job and is tested, but only on synthetic/proxy data or one platform), `proven` (met its gate or a real-data measurement).
- **evidence**: `none`, `unit-tests`, `proxy`, `synthetic`, `hardware`, `real-songs`, `ear`. Several may apply; the page shows all that are supported and the highest.
- **functionality health** (per feature, technical details only): `ok`, `partial`, `blocked`, `unknown`.
- **Project Health**: On Track, At Risk, Blocked, Complete, Insufficient Information. **Technical Progress**: Advancing, Steady, Stalled, Regressing, Not Started. **Product Readiness** (highest demonstrated): Foundation, Prototype, Integrated, Validated (needs Gate 2 signed), Ready (needs all gates signed).
- **Issue classification**: Active Blocker, Confirmed Problem, Risk, Unknown/Unverified. **Component status**: Working, Partially Working, Not Working, Not Started, Blocked.

## Rules

- The executive view is organised by outcome and component; the technical details by feature. Never by phase. Phases are context in a feature's history only.
- No invented percentages. Progress is shown as milestone criteria evidenced (x of y), board task counts and evidence levels.
- Say what does not work. An unmeasured thing is "unmeasured", not "fine".
- Page is self-contained (inline CSS, no external requests, no JavaScript used), readable on a phone and printable, light and dark.
- No Line 6 content, no song audio, no secrets in the page. The Markdown is public on GitHub: refer to songs by file id only, list key names and structure only (no model catalog dumps, no ledger notes), and link only to files that are in the repo (cache/ and data/ are never linked).
- The Markdown uses only GitHub-supported syntax (tables, headings, lists, links), escapes pipes in table cells, and links with paths relative to `reports/status/`.
