# Board and decision log, v0

Owner: lead.

- `board/TASKS.md`: the work. One line per task: `- [ ] <id> · <owner agent> · <task> · blocked by <ids> · mode <n>`. Mark `[x]` when done and add the ledger row id in parentheses. Keep done tasks for one cycle, then move them to `board/CYCLES.md`. Every task line carries an HTML anchor right after the checkbox, `<a id="t042"></a>` (lowercase id), so reports can link to it; `scripts/board_anchors.py` adds missing anchors and is run by `plan-sync`. A task moved to `board/CYCLES.md` keeps its anchor there.
- **Task references in reports:** any report or document that mentions a task id (status reports, notes, the tone comparison document, cycle notes) writes it as a relative link to that task's anchor with the task's title as the hover text: Markdown `[T042](../../board/TASKS.md#t042 "Play-along user-DI capture")`, HTML `<a href="../../board/TASKS.md#t042" title="Play-along user-DI capture">T042</a>`. Paths are relative to the document's own folder.
- `board/GATES.md`: the three gates with their evidence checklists. Only `benchmark` ticks an evidence item; only the human ticks the sign-off line.
- `board/CYCLES.md`: one short entry per cycle: date, mode, what shipped (with ledger rows), what is next, written by `plan-sync`.
- `docs/DECISIONS.md`: append-only decision log with date, decision, why, who.

The board holds the work. The docs hold the why. Chat holds nothing that matters.
- **Evidence references in reports** follow the same pattern (`scripts/evidence_links.py`; relative links with hover text, both outputs): a ledger row links to `ledger/runs.csv?plain=1#L<line>` (hover `kind · engine · dataset · metric = value`; several rows at one timestamp link the first and say how many); `DECISIONS <date> <words>` links to the heading anchor in `docs/DECISIONS.md` (GitHub slug rules, `-1`, `-2` for duplicate headings; ambiguous citations stay plain text and are listed as discrepancies); `Gate N` links to `board/GATES.md`; a repo path links to the file with its first heading or docstring line as hover; a test links to `tests/<file>.py?plain=1#L<line>`. Cite a decision with its date plus two or three words of its heading, a test as `tests/test_x.py::test_name`.
