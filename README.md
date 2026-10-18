# HX Stomp Tone Matcher

**In one sentence:** you give the program a song, and it works out how the guitar in that song was made to sound, then writes a recipe your own gear can follow to sound the same.

**In plain words.** A guitar by itself makes a quiet, plain sound. The big, crunchy, shimmering or echoing guitar sounds on records come from a second step: the guitar's signal passes through a chain of small electronic effects (think of filters on a photo, but for sound) before it reaches the speaker. There are many of these effects and each has knobs, so copying a sound by ear means hours of turning knobs and guessing. This project does the guessing for you. It listens to the guitar in a song, tries many combinations of effects and knob settings on a computer, keeps whichever sounds closest, and saves the winner as a small file.

**Where the file goes.** The file is a "preset" for a device called the Line 6 HX Stomp: a small box a guitarist plugs a guitar into, which holds a whole chain of effects and can recall a saved setup at the press of a button. Load the preset and the box should make a similar sound. The program can test its guesses with the real box, or with a computer program from the same company that copies it (Helix Native).

**What you need to know, and what you do not.** You do not need to know music or electronics to read on. You do need a song file and, to finish the job, either the HX Stomp or Helix Native (both are Line 6 products you buy yourself). A graphics card helps speed up one step but is optional.

## Details

Give it a song. It isolates the guitar, works out the tone, and hands back a Line 6 HX Stomp preset (`.hlx`) with up to three snapshots, verified on the pedal.

The render engine is yours to choose: **Helix Native** (fast, offline, needs a licence) or the **HX Stomp itself** (real time, no extra purchase). At least one is required. A GPU is optional; it only speeds up separation and model training.

Status: Gate 1 passed (2026-10-14). Both render engines are driven from code (Helix Native on macOS; the HX Stomp over USB on macOS, firmware 3.50), `.hlx` presets round-trip, and the engine interface is frozen. The pieces of the matching loop exist and are tested on synthetic and proxy data only: separation, a reference-DI picker, a loss kit (no term admitted yet) and a search core. A first song has been matched end to end and checked by ear, but the tone comparison is not yet validated. See the [project status report](reports/status/README.md), regenerated at the end of every cycle.

Direction (human, 2026-10-17): testing on a guitar extracted from a song moves to a later gate. Before that, the matcher is validated on clean playing: a person plays a real guitar through real hardware pedals, one pedal at a time, and the model sees only the pedal's output (plus some separate, unrelated dry playing to learn what the raw guitar sounds like). It then proposes the pedal's settings, and the recorded dry signal is used only afterwards to score how close it got.

**Running many render hosts at once.** Each Helix Native instance uses about one processor core. A machine-wide scaler (`python -m tonematch.engines.host_budget status`) decides how many may run so the computer stays responsive; the first time you run it, `... setup` benchmarks your machine and lets you choose the minimum and maximum.

## Read first

- [`docs/PLAN.md`](docs/PLAN.md) — the project plan: pipeline, every step with its options, evaluation, roadmap, risks, legal, sources.
- [`docs/AGENT-TEAM.md`](docs/AGENT-TEAM.md) — the agents that build it, the skills they share, the contracts between them, the cycle and the priority modes.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the decision log.
- [`board/TASKS.md`](board/TASKS.md) and [`board/GATES.md`](board/GATES.md) — what is being worked on, and what each gate needs.
- [Project status report](reports/status/README.md) — project health, what works, the main blockers and what needs you.
- [How benchmarking works](docs/BENCHMARKING.md) — what the benchmark tests, how it reaches a verdict, and where each test stands.
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
