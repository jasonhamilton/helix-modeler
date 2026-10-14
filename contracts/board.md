# Board and decision log, v0

Owner: lead.

- `board/TASKS.md`: the work. One line per task: `- [ ] <id> · <owner agent> · <task> · blocked by <ids> · mode <n>`. Mark `[x]` when done and add the ledger row id in parentheses. Keep done tasks for one cycle, then move them to `board/CYCLES.md`.
- `board/GATES.md`: the three gates with their evidence checklists. Only `benchmark` ticks an evidence item; only the human ticks the sign-off line.
- `board/CYCLES.md`: one short entry per cycle: date, mode, what shipped (with ledger rows), what is next, written by `plan-sync`.
- `docs/DECISIONS.md`: append-only decision log with date, decision, why, who.

The board holds the work. The docs hold the why. Chat holds nothing that matters.
