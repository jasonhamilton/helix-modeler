---
name: plan-sync
description: Write the outcome of a cycle back into the docs: decisions to docs/DECISIONS.md, risk and open-question changes to docs/PLAN.md, a cycle entry to board/CYCLES.md, done tasks archived, and (when reachable) the living Claude Docs plan updated. Use at the end of a cycle or after any decision.
---

# plan-sync

Run as `tone-matcher-lead` at the end of every cycle, or after any decision.

1. **Decisions.** Append to `docs/DECISIONS.md`: date, decision, why, who. Include contract changes (with the proposal that named who it breaks), chosen fallbacks, thresholds `benchmark` set, and anything the human decided.
2. **Risks.** In `docs/PLAN.md`, Risks table: retire a row whose spike resolved it (note the ledger row), add a row for any new risk the cycle surfaced, update a fallback that was exercised.
3. **Open questions.** Tick answered questions in `docs/PLAN.md` and record the answer in `docs/DECISIONS.md`; add new ones.
4. **Board.** In `board/TASKS.md`, done tasks get `[x]` and their ledger row id; move tasks done more than one cycle ago into `board/CYCLES.md`. Add the next cycle's tasks for the current mode. Then run `python scripts/board_anchors.py` (adds `<a id="tNNN"></a>` after the checkbox of every task line; `--check` exits 1 if any is missing).
5. **Cycle note.** Append to `board/CYCLES.md`: date, mode, what shipped (file paths and ledger rows), what is next, what is blocked on the human.
6. **Agent team.** If an agent's responsibilities changed, update `docs/AGENT-TEAM.md` and the agent's `.claude/agents/<name>.md` description together.
7. **Living doc.** If the Claude Docs tools are available in this session, mirror the changes to the living plan (link in `docs/PLAN.md`); otherwise note "living doc not synced" in the cycle entry.

Never delete history: the decision log and ledger are append-only; the cycle notes are the archive.
8. **Tone comparison doc.** If this cycle changed a comparison term or its weights, a trust-check result, how the search iterates, the hardware null test or parity findings, or a listening verdict contradicted the score, update `docs/TONE-COMPARISON.md` (and `docs/BENCHMARKING.md` when a test, threshold, gate or parity result changed) in the same change (and its "Last updated" date). If nothing relevant changed, leave it.
9. **Task links.** After the cycle note is written, run `python scripts/link_tasks.py`: it rewrites bare task ids in `docs/notes/*.md`, `docs/TONE-COMPARISON.md` and `board/CYCLES.md` into relative links with hover text (task names; ledger timestamps to their `runs.csv` line; `DECISIONS <date> <words>` to the decision heading) (idempotent; skips code and existing links; never touches `docs/DECISIONS.md`). It lists unknown ids; fix or report them. Rule: `contracts/board.md`.
