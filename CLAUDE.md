# CLAUDE.md

This repo builds the HX Stomp Tone Matcher. Read `docs/PLAN.md` for what and why, `docs/AGENT-TEAM.md` for who does what.

## Start of every session

1. Read `board/TASKS.md`, `board/GATES.md`, the last 20 rows of `ledger/runs.csv`, and `docs/DECISIONS.md`.
2. Unless the user asks for something specific, act as the lead: run the `cycle` skill.
3. When a request belongs to one step of the pipeline, hand it to that step's agent in `.claude/agents/`.

## Standing rules (every agent)

- **Contracts are in `contracts/`.** Implement them, do not change them. A change is a proposal to the lead naming who it breaks.
- **Every hand-off is a file plus a ledger row.** Append with `python scripts/ledger_append.py` (see `contracts/ledger-format.md`). A claim with no row did not happen.
- **No Line 6 assets in the repo.** Catalog JSON, firmware, HX Edit resources, Native binaries: never. Read them from the user's install at runtime. `fixtures/golden/*.hlx` are the user's own exports and stay local unless the human decides otherwise.
- **No firmware decompilation.** fretwire-style protocol interop only.
- **No song audio leaves the machine.**
- **Benchmark regression freezes merges.** Compare to the previous ledger row for the same `kind` and `dataset` before claiming improvement.
- **The ear is the final judge.** Prepare listening sessions; never score them yourself.
- **Hardware time is scarce.** Batch anything that needs the pedal and leave it on its original preset afterwards.
- Update `board/TASKS.md` when you start and finish a task. Write decisions to `docs/DECISIONS.md`, never only in chat.

## Environment

- Python 3.11+, `pip install -e .[dev]`. Audio at 48 kHz float32 throughout.
- Engine selection: `TONEMATCH_ENGINE=native|stomp|auto` (default `auto`: Native searches, Stomp verifies when both are present).
- Catalog cache: `cache/catalog/` (gitignored). Stems cache: `cache/stems/` (gitignored).
- Scripts named in a skill that do not exist yet are built by that skill's owning agent, to the contract, before the skill is run for the first time.
