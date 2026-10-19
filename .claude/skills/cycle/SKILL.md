---
name: cycle
description: Run one iteration cycle of the HX Stomp Tone Matcher as the lead: read the board, ledger and gates, pick the priority mode, order the board, dispatch the owning agents, review, run the gate checks, sync the docs. Use at the start of a work session or when asked what to do next.
---

# cycle

Run as `tone-matcher-lead`. One cycle is two to three days in weeks 1 to 4, a week after.

## 1. Read state

- `board/TASKS.md`, `board/GATES.md`, `board/CYCLES.md` (last entry).
- Last 20 rows of `ledger/runs.csv`; note any `kind=benchmark` or `kind=parity` row lower than the previous row for the same dataset (that is a regression).
- `docs/DECISIONS.md` (last 10 entries) and the Risks and Open questions sections of `docs/PLAN.md`.

## 2. Pick the mode

From `board/GATES.md`: no gate passed → mode 1 (prove the architecture); Gate 1 passed → mode 2 (make the loop work); Gate 2 passed → mode 3 (beat the baseline: Gate 3 blind single block, then Gate 4 real pedals, then Gate 5 real songs; mode 3 holds until Gate 5 unless the human says otherwise); Gate 5 passed → mode 4 (make it learn) and mode 5 (ship for others) as the roadmap in `docs/AGENT-TEAM.md` says. (Renumbered 2026-10-18, T160: modes unchanged, mode 3 now spans Gates 3 to 5.) An override (gate blocker, regression, new firmware, ear disagrees) changes who goes first inside the mode, not the mode.

## 3. Order the board

Apply the priority rules: gate blockers, then regressions, then the mode's next roadmap item, then High-impact risks, then research. Tie-breaks: unblocks the most agents; cheaper spike; hardware tasks batched around the human. Rewrite the mode's section of `board/TASKS.md` in that order.

## 4. Dispatch

For each top task whose blockers are done, spawn the owning agent (the `Agent` tool, `subagent_type` = the name in `.claude/agents/`) with: the task line, the contracts it touches, the ledger kind it must write, and "report the file paths and ledger row". Independent tasks run in parallel. A task that touches a contract gets the contract owner's name in the prompt and the instruction to propose, not change. A `human` task becomes a short, specific request to the person, with the lead's recommendation.

## 5. Review and integrate

Send each returned change to `reviewer`. A block goes back to the owner once; a second block goes to the lead's judgement.

## 6. Gate

Ask `benchmark` to run the whole suite first: `python scripts/benchmark_suite.py` (add `--no-hardware` when the pedal is not free). It runs every test in a fixed order, times each, writes `reports/benchmark/latest.json` and a history file, appends the `suite_wall_clock_s` ledger row, and refreshes the "most recent benchmark run" section of `docs/BENCHMARKING.md` (`--render-doc` refreshes only that section). A test that passed last run and fails now is a regression: freeze merges and tell the owner. Then ask `benchmark` to run anything extra the mode needs: `engine-smoke` (mode 1), `synthetic-benchmark` (mode 2 onward, every merge), `parity-null` and `blind-ab-session` (mode 3), `synthetic-benchmark` with seeded-vs-cold comparison (mode 4), `engine-smoke` on fresh installs plus `licence-check` (mode 5). If an exit condition in `docs/AGENT-TEAM.md` is met, `benchmark` ticks the evidence in `board/GATES.md`; the human signs.

## 7. Sync

Run `plan-sync`.

## 8. Status report

Run `status-report` (agent `status-reporter`): refresh `docs/features.json` and regenerate `reports/status/index.html`, the by-feature progress page for the person. Then regenerate the block coverage report, `python scripts/block_coverage_report.py` (owner `benchmark`): the Stomp-versus-Native block table by GUI category, with a card per tested chain. It writes only `reports/local/blocks/index.html` and `README.md`, which are gitignored because they name Line 6 models: never commit them, never link them from a committed document, never create a public copy. Mention its path in the closing paragraph. Then end with one paragraph to the person: mode, what shipped (ledger rows), what is blocked on them, what is next, and the path to the status page with its three biggest blockers.
