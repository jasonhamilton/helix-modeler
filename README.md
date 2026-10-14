# HX Stomp Tone Matcher

Give it a song. It isolates the guitar, works out the tone, and hands back a Line 6 HX Stomp preset (`.hlx`) with up to three snapshots, verified on the pedal.

The render engine is yours to choose: **Helix Native** (fast, offline, needs a licence) or the **HX Stomp itself** (real time, no extra purchase). At least one is required. A GPU is optional; it only speeds up separation and model training.

Status: planning complete, week-1 spikes not yet started. Nothing here renders audio yet.

## Read first

- [`docs/PLAN.md`](docs/PLAN.md) — the project plan: pipeline, every step with its options, evaluation, roadmap, risks, legal, sources.
- [`docs/AGENT-TEAM.md`](docs/AGENT-TEAM.md) — the agents that build it, the skills they share, the contracts between them, the cycle and the priority modes.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the decision log.
- [`board/TASKS.md`](board/TASKS.md) and [`board/GATES.md`](board/GATES.md) — what is being worked on, and what each gate needs.

## Layout

```
.claude/agents/     13 agent definitions (one lead, eight specialists, four guardians)
.claude/skills/     12 repeatable procedures, invoked as /<name>
contracts/          the five interfaces the agents agree on; the engine interface is also code
board/              task board, gate checklists, cycle notes
ledger/             runs.csv: one row per run, the only evidence a claim counts
docs/               the plan, the agent team, the decision log
fixtures/           golden templates and smoke clips (local only by default, see fixtures/README.md)
scripts/            small utilities the skills call; most are built during the spikes
src/tonematch/      the package; today just the engine interface and a registry stub
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

Not yet chosen. The open question is GPLv3 for the whole project versus a permissive licence with the pedalboard host (GPLv3) isolated in a separate process. See `docs/PLAN.md`, Legal and licensing.
