# Setup guide (for agents)

Written for [T218](../board/TASKS.md#t218 "Setup guides for humans and agents") on 2026-10-23. Read `CLAUDE.md` first; this page adds the environment checks and the single-block song pipeline. The human version, with downloads, licences and the first-song walkthrough, is [SETUP.md](SETUP.md). Commands and flags are in [TOOLS.md](TOOLS.md).

## 1. Before any work

Run these from the repo root, in this order. All are read-only.

```bash
scripts/setup.sh --check                        # what is present and missing, per environment, download and Line 6 install
.venv/bin/python scripts/proc_check.py          # orphaned workers from earlier jobs (exit 1 = something flagged)
uptime                                          # busy cores; heavy jobs run under `nice -n 10`
.venv/bin/python -c "from tonematch.gpu import detect_backend as d; print(d())"   # torch backend: mps, cuda, rocm, directml, cpu
```

Then read `board/TASKS.md`, `board/GATES.md`, the last 20 rows of `ledger/runs.csv` and `docs/DECISIONS.md`, as `CLAUDE.md` says.

Interpreting `setup.sh --check`:

- Missing `.venv`, `hostvenv`, `basic-pitch`: `scripts/setup.sh --only venv,hostvenv,bp` is safe to run (no consent needed, no Line 6 file involved).
- Missing downloads (Guitar-TECHS, AFx-Rep, demucs weights): **do not download them yourself.** They need the human's typed consent, with sizes, source and licence shown (AFx-Rep carries a non-commercial training-data caveat). Report them as missing and ask the human to run `scripts/setup.sh`. Never pass `--consent` to the script: that flag is for the human to type.
- Missing catalog, `start_stomp.state`, `known.hlx`, golden export: these come from the human's own Line 6 installs and a manual click (`native_hc_once.py`). Report, do not work around. Never create, copy or commit a Line 6 file.
- `stompd` missing: say so and run only what does not need it (section 4).

Environment variables you will need: `source cache/setup/env.sh` (sets `TONEMATCH_NATIVE_PYTHON`, and `TONEMATCH_STOMPD` when the helper exists), `TONEMATCH_AGENT=<your name>` for ledger rows, `TONEMATCH_ENGINE=native|stomp|auto`, `TONEMATCH_DEVICE` to force the torch backend.

Sandboxed shells: PortAudio cannot open a stream (error -9986), so recording (`guitar_profile_record.py`, `playalong_record.py`, Stomp reamp) is the human's. `stompd` USB control may also be blocked. Everything else is offline and fine.

## 2. The single-block song pipeline

```
guitar calibration (human, once)   data/guitars/<guitar-id>/profile.json, bank.json
song file (human)                  data/songs/<file>
        -> separate.py             cache/stems/<hash>/htdemucs_6s/
        -> transcribe_stems.py     data/songs/<id>-rebuilt/transcription.mid     (Basic Pitch venv)
        -> rebuild_dry.py          data/songs/<id>-rebuilt/profiled-<guitar-id>/ (synthetic dry track)
        -> match_block.py          cache/matches/...                              (Native, drive category)
        -> finetune_knobs.py       knob fine-tune (only with --finetune)
        -> package                 best.hlx, index.html, summary.json
```

`scripts/song_match.py` runs the whole chain, resumes by step and writes one run folder (the `song-match` skill, `.claude/skills/song-match/SKILL.md`, is the procedure once it lands). Use it, not the pieces, unless you are debugging a stage.

```bash
source cache/setup/env.sh
# plan only, renders nothing, shows which steps are already done
.venv/bin/python scripts/song_match.py --song <id|file|stems hash> --guitar-id <guitar-id> --window START:END --dry-run --result-json cache/results/song_match_plan.json
# the run
nice -n 10 .venv/bin/python scripts/song_match.py --song <id|file> --guitar-id <guitar-id> --window START:END --result-json cache/results/song_match.json
```

Flags: `--song`, `--guitar-id`, `--window`, `--level-db`, `--chords`, `--finetune`, `--load-stomp`, `--dry-run`, `--result-json`, plus `--id`, `--workers`, `--out`, `--force`. Note which are method choices (section 3): the window pick, `--chords`, `--finetune` and the fine-tune settings are not yours to change by default (fine-tuning is off unless asked; when on, Output/Master are locked). Defaults live in `data/approved-methods.json`, pinned by `tests/test_approved_methods.py`.

Stage scripts, for debugging only: `separate.py`, `transcribe_stems.py run|posteriors --songs <id> [--stem PATH]`, `rebuild_dry.py --song-id ID [--profile GUITAR] [--chords ...]` (full song length by default), `align_takes.py --song-id ID` or `--stem`, `playalong_record.py --song-id ID` or `--pack DIR` (human, Stomp).

**Read results from the manifest, not stdout.** `--result-json` gives `status`, `outputs` (`out_dir`, `best_hlx`, `listen_page`, `summary_json`, `summary_md`) and `metrics` (`song_id`, `window`, `model`, `values`, `dist_before`, `dist_after`, `renders`, `validate_exit`). Contract: `contracts/cli-result.md`.

## 3. Standing rules that apply here

From `CLAUDE.md`; this list is the part that touches setup and the pipeline.

- **The human approves methods before they take effect.** Which score, loss or weights are admitted; which search or prediction method is adopted; test-set design; pass bars; any gate change. You may propose with evidence and a recommendation. The Basic Pitch settings (`CHOSEN` in `transcribe_stems.py`), the rebuild's constants, the AFx-Rep shortlist and the knob fine-tune are proposals that the human has been running, not adopted methods. Record approvals in `docs/DECISIONS.md` with the human's words. Do not adopt on your own authority because a script defaults to it.
- **Approvals are recorded one item per line, in the human's words; a "yes" to a list is a bundle, recorded as provisional** (the human's decision 8, DECISIONS 2026-10-25 "yes go": "one line per item in the human's words from now on; bundles recorded as provisional").
- **Contracts are not changed by you.** Write a proposal to the lead naming who it breaks.
- **Ledger.** Every hand-off is a file plus a row. `TONEMATCH_AGENT=<name> python scripts/ledger_append.py --kind search --engine native --dataset <id> --metric <name> --value <n> --notes "..."` (format: `contracts/ledger-format.md`). A claim with no row did not happen. `song_match.py` writes none by itself; the headline is yours to record. Compare to the previous row of the same `kind` and `dataset` before claiming improvement; a benchmark regression freezes merges.
- **Leave no orphans.** `proc_check.py` before heavy work and after any run ends or is killed; `--kill` for flagged PIDs only. Never `pkill` by pattern.
- **CPU guard.** The machine-wide rule is busy cores (utilisation times cores), not load average; a job pauses above 0.95 x cores and resumes below 0.85 x cores, and the host scaler (`python -m tonematch.engines.host_budget status`) allows at most 6 Native hosts, `song_match.py` uses 3 workers. Check `uptime`, run under `nice -n 10`, never raise the host cap on your own, and do not start a second heavy job beside a running match.
- **No song audio leaves the machine.** No upload, no hosted separation API, no committing or publishing songs, stems, rebuilt dry tracks, takes, profiles or listening pages. Everything lives in gitignored `data/` and `cache/`. Never paste audio paths of the human's songs into public places (issues, PRs, external services).
- **No Line 6 assets in the repo.** Catalog JSON, firmware, HX Edit resources, Native binaries, templates, the block library, exported presets (`fixtures/golden/*.hlx`, `fixtures/smoke/known.hlx`) are read from the user's install at runtime and stay local. No firmware decompilation.
- **Listening is the human's.** Prepare the listening page; never score it yourself and never claim a tone "sounds right". Report the distance numbers as rankings, labelled unadmitted (`label: "unadmitted loss"` in the match manifests).
- **Hardware time is scarce.** The Stomp is for the human's calibration and your batched, announced runs. Leave it on its original preset afterwards; quit HX Edit first; one session at a time.
- **Blind tests only.** No known-dry-track control arms in no-DI or Gate 3 experiments.
- **Licences.** After any dependency change run `.venv/bin/python scripts/licence_check.py` and hand `reviewer` the result. Do not add a dependency to `pyproject.toml` without a ruling; the allowlist is `docs/licence-allowlist.json` and an entry needs a ruling in `docs/DECISIONS.md`. Basic Pitch and `pedalboard` stay out of the main venv's declared dependencies (external tool and GPL boundary).
- **Board and decisions.** Mark your task in `board/TASKS.md` when you start and finish (`python scripts/board_anchors.py` adds anchors). Decisions go to `docs/DECISIONS.md`, never only in chat.

## 4. What to run without Native, the Stomp or the downloads

| Missing | Still possible |
|---|---|
| Native | `pytest -q`, `separate.py`, `transcribe_stems.py`, `rebuild_dry.py`, `licence_check.py`, `hlx_validate.py`, `song_match.py --dry-run` |
| Stomp / `stompd` | Everything except `--load-stomp`, the Stomp engine, recording and parity runs |
| AFx-Rep | Search that does not shortlist by embedding; `song_match.py` needs it, so report that |
| Guitar-TECHS | Nothing in the rebuild: it is the note bank. Stop and ask |
| A guitar profile | `song_match.py` without `--guitar-id` uses the library bank (say so in the report) |

Tests: `.venv/bin/python -m pytest -q`. `tests/test_tools_doc.py` must pass whenever a script or an example flag in `docs/TOOLS.md` changes.

## 5. Where outputs land (all gitignored unless noted)

| Output | Location |
|---|---|
| Stems, section map, confidence | `cache/stems/<hash>/htdemucs_6s/` |
| Transcription, rebuilt dry tracks | `data/songs/<id>-rebuilt/` (`profiled-<guitar-id>/`, `playlevel*/`, `chords/`) |
| Guitar sessions and profiles | `data/guitars/<guitar-id>/` |
| Play-along packs and takes | `data/songs/<id>-playalong/` |
| A song-match run | `cache/song-match/<id>-<guitar-id or plain>/`: `best.hlx`, `best.validate.md`, `index.html`, `listen/`, `summary.json`, `summary.md`, `steps.json`, `logs/` |
| Search and fine-tune working folders | `cache/matches/<id>-dryrebuild*/`, `cache/matches/<id>-finetune*/` |
| Tool result manifests | wherever you pass `--result-json` (use `cache/results/`) |
| Models and weights | `cache/models/` (AFx-Rep, `hf/` demucs), never committed |
| Local reports | `reports/local/` (never committed) |
| Ledger, board, decisions (tracked) | `ledger/runs.csv`, `board/TASKS.md`, `docs/DECISIONS.md` |

## 6. How to report

1. Say what you ran (command lines, one per line), on what machine (`detect_backend`, engine), and the run folder.
2. Give the numbers from `summary.json` or the result manifest, labelled as unadmitted rankings: model, knob values, `dist_before` and `dist_after`, renders, wall time, `validate_exit`. Say whether the search was `low_confidence`.
3. Append the ledger row(s) and name the row timestamp in your report. A claim with no row did not happen.
4. List what is missing or failed, with the exact message, and what only the human can do (a download consent, a click, a recording, the listening).
5. Hand the listening page path to the human. Do not describe the tone as good or bad.
6. Run `proc_check.py` once more and say it was clean.
7. Mark the task in `board/TASKS.md`; put any decision or proposal in `docs/DECISIONS.md` or in a note to the lead.

## 7. Skills and agents to use

- `song-match` skill (`.claude/skills/song-match/SKILL.md`, search; being written): the song to preset procedure.
- `cycle` skill: when the human gives no specific request, act as the lead and run it. `plan-sync`: at the end of a cycle, write the outcome back to `docs/DECISIONS.md`, `docs/PLAN.md`, `board/CYCLES.md`.
- `engine-smoke`: the exit test for a fresh install. `licence-check`: after dependency changes. `catalog-import`, `separate`, `hlx-validate`, `status-report` as named in `.claude/skills/`.
- Hand a request to the step's agent in `.claude/agents/`: `platform` (environments, CI, this guide), `separation`, `dry-signal` (transcription, rebuild, calibration), `search`, `native-engine`, `stomp-engine`, `reviewer`.
