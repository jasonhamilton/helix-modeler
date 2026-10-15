---
name: status-report
description: Generate the by-feature project status report as one HTML page plus a GitHub-readable Markdown copy: an overview table (status, completion, functionality, evidence, owner, blockers) and a detailed section per feature (implemented, challenges, left to do, needs from the human). Use at the end of every cycle after plan-sync, and whenever someone asks where a feature stands.
---

# status-report

Run as `status-reporter` (or as `tone-matcher-lead` if that agent is unavailable) **at the end of every cycle, after `plan-sync`**. Output, both produced every cycle from the same registry and facts: `reports/status/index.html` (self-contained HTML) and `reports/status/README.md` (GitHub-flavoured Markdown, which GitHub renders when browsing the folder).

## Inputs (read, do not guess)

`board/TASKS.md`, `board/GATES.md`, `board/CYCLES.md`, `ledger/runs.csv`, `docs/DECISIONS.md`, `docs/PLAN.md` (Risks, Open questions), `docs/notes/*`, `contracts/*`, `reviews/*`, `git log`, and the code under `src/tonematch/` and `scripts/`.

## Steps

1. **Refresh the registry.** Open `docs/features.json`. For each feature update, from the sources above: `status`, `completion` (0 to 100) with `completion_reason`, `functionality` (one sentence on what it does today, and does not), `evidence` (the highest level actually supported, with the ledger timestamps or test files that support it), `implemented` (items with file paths), `challenges` (findings, failures, limits, with the source), `todo`, `needs_human`, `decisions` and `risks` (links to DECISIONS entries and PLAN rows), `tasks` (ids from the board), `ledger` (kind/engine/dataset filters used for the live row counts). Add a feature when a new area appears; never delete one, mark it `retired`.
2. **Check against the board.** If a task is marked done but the code or tests do not support it, or the board and registry disagree, list it under `discrepancies` in the registry and tell the lead.
3. **Render.** Run `python scripts/status_report.py --run-checks` (omit `--run-checks` to skip pytest/ruff/licence-check). It writes `reports/status/index.html` and `reports/status/README.md` and prints both paths (`--format html` or `--format md` limits it to one; the default and the cycle rule is both).
4. **Read it back.** Open the HTML source and the Markdown and confirm: the overview table has every non-retired feature and links work; no section is empty; statuses and evidence match the vocabulary; nothing claims more than its evidence. Fix the registry and re-render if not.
5. **Report.** Return one paragraph to the lead: features by status, what changed since the last report, the three biggest blockers, discrepancies found.

## Vocabularies (fixed)

- **status**: `not-started` (nothing built), `designing` (notes or plan only), `prototype` (code exists but unproven or partial), `working` (does its job and is tested, but only on synthetic/proxy data or one platform), `proven` (met its gate or a real-data measurement).
- **evidence**: `none`, `unit-tests`, `proxy`, `synthetic`, `hardware`, `real-songs`, `ear`. Several may apply; the page shows all that are supported and the highest.
- **functionality health** (colour on the page): `ok`, `partial`, `blocked`, `unknown`.

## Rules

- By feature, never by phase. Phases are context in a feature's history only.
- Completion is a reasoned judgement against the feature's own definition of done (`done_when` in the registry). It is not a task count; show both.
- Say what does not work. An unmeasured thing is "unmeasured", not "fine".
- Page is self-contained (inline CSS, no external requests, no JavaScript required), readable on a phone and printable, light and dark.
- No Line 6 content, no song audio, no secrets in the page. The Markdown is public on GitHub: refer to songs by file id only, list key names and structure only (no model catalog dumps, no ledger notes), and link only to files that are in the repo (cache/ and data/ are never linked).
- The Markdown uses only GitHub-supported syntax (tables, headings, lists, links), escapes pipes in table cells, and links with paths relative to `reports/status/`.
