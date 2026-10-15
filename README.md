# HX Stomp Tone Matcher

Give it a song. It isolates the guitar, works out the tone, and hands back a Line 6 HX Stomp preset (`.hlx`) with up to three snapshots, verified on the pedal.

The render engine is yours to choose: **Helix Native** (fast, offline, needs a licence) or the **HX Stomp itself** (real time, no extra purchase). At least one is required. A GPU is optional; it only speeds up separation and model training.

Status: Gate 1 passed (2026-10-14). Both render engines are driven from code (Helix Native on macOS; the HX Stomp over USB on macOS, firmware 3.50), `.hlx` presets round-trip, and the engine interface is frozen. The pieces of the matching loop exist and are tested on synthetic and proxy data only: separation, a reference-DI picker, a loss kit (no term admitted yet) and a search core. A first song has been matched end to end and checked by ear, but the tone comparison is not yet validated. See the [project status report](reports/status/README.md), regenerated at the end of every cycle.

## Read first

- [`docs/PLAN.md`](docs/PLAN.md) — the project plan: pipeline, every step with its options, evaluation, roadmap, risks, legal, sources.
- [`docs/AGENT-TEAM.md`](docs/AGENT-TEAM.md) — the agents that build it, the skills they share, the contracts between them, the cycle and the priority modes.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the decision log.
- [`board/TASKS.md`](board/TASKS.md) and [`board/GATES.md`](board/GATES.md) — what is being worked on, and what each gate needs.
- [Project status report](reports/status/README.md) — project health, what works, the main blockers and what needs you.
- [How the tone comparison works](docs/TONE-COMPARISON.md) — what is compared, how the search iterates, the hardware null test, and whether the score can be trusted yet.

## Layout

```
.claude/agents/     14 agent definitions (one lead, eight specialists, five guardians)
.claude/skills/     13 repeatable procedures, invoked as /<name>
contracts/          the five interfaces the agents agree on; the engine interface is also code
board/              task board, gate checklists, cycle notes
ledger/             runs.csv: one row per run, the only evidence a claim counts
docs/               the plan, the agent team, the decision log
fixtures/           golden templates and smoke clips (local only by default, see fixtures/README.md)
scripts/            small utilities the skills call; most are built during the spikes
src/tonematch/      the package: engines (Native, Stomp), presets and catalog, separation, DI picker, loss kit, search
tests/              pytest
```

## Working with Claude Code

Open the repo in Claude Code and say `/cycle`. The lead agent reads the board, the ledger and the gate checklists, picks the priority mode, and dispatches the specialists. Every other skill is also a slash command (`/engine-smoke`, `/hlx-validate`, ...). `CLAUDE.md` holds the standing rules.

## Hard rules

- No Line 6 files in the repo. The model catalog is read at runtime from your own HX Edit install. Native is optional and you bring your own licence.
- No firmware decompilation. Protocol interoperability (fretwire) is fine.
- No song audio is ever uploaded anywhere. Hosted separation APIs are deferred for that reason.
- No claim without a ledger row. No tone is done until it passes a blind listen.

## Licence

MIT (see [`LICENSE`](LICENSE)). The project's own code is MIT. Third-party components keep their own licences: the optional `native` extra installs pedalboard (GPLv3), which runs only in a separate child process and is never bundled; some optional separation dependencies are LGPL or non-commercial. See [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and `docs/DECISIONS.md`.
