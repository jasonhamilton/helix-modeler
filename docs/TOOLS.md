# Command-line tools

Reference for the scripts in `scripts/` (129 Python files, 2 shell drivers, 1 README), written for a person who wants to run a tool on their own and for an agent that is handed the result. Written for [T172](../board/TASKS.md#t172 "Command-line tools reference + agent-readable results") (2026-10-18, `platform`). Every flag in an example was checked against the tool's `--help` (and `tests/test_tools_doc.py` re-checks them against the source).

How the 129 scripts split:

| Class | Count | Meaning |
|---|---|---|
| user-runnable | 69 | A person can run it for a useful result. Documented in full below. |
| internal | 19 | A helper that other tools call, a library with a thin command line, or bookkeeping. One line each at the end. |
| historical spike | 41 | A one-off experiment or live check, kept for the record. One line each at the end. |

Thirteen of the user-runnable tools write a result manifest with `--result-json PATH` (see "Handing results to an agent"). The others print to stdout and write their own files; each entry says which.

## Contents

- [Quick start](#quick-start)
- [Conventions that apply to every tool](#conventions-that-apply-to-every-tool)
- [Handing results to an agent](#handing-results-to-an-agent)
- 1 [Setup and health](#1-setup-and-health)
- 2 [Audio in and out, and the HX Stomp](#2-audio-in-and-out-and-the-hx-stomp)
- 3 [Songs and separation](#3-songs-and-separation)
- 4 [Matching](#4-matching)
- 5 [Benchmarking and evaluation](#5-benchmarking-and-evaluation) (5a suites and recovery, 5b loss-trust checks, 5c parity, 5d engine verification, 5e search diagnostics)
- 6 [Listening sessions](#6-listening-sessions)
- 7 [Presets and the catalog](#7-presets-and-the-catalog)
- 8 [Reports and the local site](#8-reports-and-the-local-site)
- 9 [Project bookkeeping](#9-project-bookkeeping)
- 10 [Library preparation](#10-library-preparation)
- 11 [Distortion research](#11-distortion-research)
- [Internal helpers](#internal-helpers) and [historical spikes](#historical-spikes)

## Quick start

Run from the repo root. Nothing here needs the pedal unless it says so.

```bash
# 1. Which engines does this machine have? (dry run, writes no ledger row)
.venv/bin/python scripts/engine_smoke.py --no-ledger

# 2. Any orphaned workers from an earlier job? (run before and after heavy work)
.venv/bin/python scripts/proc_check.py

# 3. Split a song into stems (local only; ~9 s for 68 s of audio on Apple MPS)
.venv/bin/python scripts/separate.py data/songs/Mother_0-68s.wav --dataset mother-0-68s

# 4. Check the HX Stomp inputs before a play-along (3 s, pedal connected, HX Edit closed)
.venv/bin/python scripts/playalong_record.py --check

# 5. Record three play-along takes, then pick the tightest against the separated stem
.venv/bin/python scripts/playalong_record.py --takes 3
.venv/bin/python scripts/align_takes.py data/songs/mother-playalong/takes/20261018-113158_take{1,2,3}_dry.wav

# 6. Match one song section on Helix Native (about 23 minutes, 6 hosts at most)
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/match_song.py \
  --stems-dir cache/stems/2ec11f9795be822e/htdemucs_6s --song Mother-0-68s --out cache/matches/Mother-0-68s \
  --user-di data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav \
  --user-di-json data/songs/mother-playalong/takes/20261018-113158_take1_align.json --target 28.3:38.3

# 7. Run the whole benchmark suite without the pedal and hand the result to an agent
nice -n 10 .venv/bin/python scripts/benchmark_suite.py --no-hardware --no-ledger

# 8. Validate a generated preset before loading it on the pedal
.venv/bin/python scripts/hlx_validate.py cache/matches/Mother-0-68s/best_snapshot1.hlx --result-json cache/results/validate.json
```

## Conventions that apply to every tool

**Interpreter and niceness.** Use `.venv/bin/python`. Heavy jobs run as `nice -n 10 .venv/bin/python ...`. Tools that render on Helix Native start a child process in the host venv; point it with `TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python` (the licence boundary keeps `pedalboard` out of the main venv).

**Environment variables.**

| Variable | Used by | Meaning |
|---|---|---|
| `TONEMATCH_ENGINE` | engine_smoke, benchmark_suite | `native`, `stomp` or `auto` (default). `auto`: Native searches, the Stomp verifies when both are present. |
| `TONEMATCH_NATIVE_PYTHON` | every Native tool | Python of the host venv that has `pedalboard`. Default: the current interpreter. |
| `TONEMATCH_STOMPD`, `TONEMATCH_STOMPD_COMPILE` | every Stomp tool that writes presets | Paths of the `stompd` control helper and its offline compiler (default `cache/stomp/bin/stompd{,-compile}`). Built outside the repo (`docs/notes/stompd.md`). |
| `TONEMATCH_AGENT` | everything that writes a ledger row | Name written in the `agent` column; default `human`. |
| `TONEMATCH_DEVICE` | separation, embeddings | Force the torch backend: `cpu`, `cuda`, `rocm`, `mps`, `directml`. |
| `TONEMATCH_STOMP_USB_RESET` | Stomp tools | `auto`, `always` or `off`: software USB reset after a pedal session. |
| `TONEMATCH_HOST_BUDGET`, `TONEMATCH_HOST_TARGET_LOAD`, `TONEMATCH_HOST_CORES`, `TONEMATCH_NATIVE_MAX_HOSTS`, `TONEMATCH_NATIVE_MANY_HOSTS` | Native pools | Host scaler tuning (see Load limit). Leave at defaults. |
| `TONEMATCH_CONTACT` | song_meta_probe | Contact address for the MusicBrainz User-Agent. |

**Load limit.** The machine-wide rule: the 1-minute load average never exceeds 10 on this 10-core Mac (the limit is the core count on another machine). A host scaler (`tonematch.engines.host_budget`) grants Native hosts a lease: at most 6 at once, one per core, with room under a target load of 9. Every script with its own guard now uses one: busy cores (`measure_demand`) behind a `BusyGate` (`LoadGuard` in `host_budget.py`; T193): it pauses above 0.95 x cores and resumes below 0.85 x cores, and the 1-minute load average is only a backstop above 1.2 x cores. Their `--max-load X` flag is the pause line in busy cores (default 0.95 x cores; the resume line keeps the 0.85/0.95 ratio). `host_budget_live.py` takes `--start-below` (default 0.85 x cores) and `--abort-load` (default 0.99 x cores) in busy cores; `time_specialist_fit.py` and `stomp_robust.py` wait the same way (no flag, default 0.95 x cores). Progress lines and stats that said `load1` / `load1_max` now say `busy` / `busy_max`. Check with `uptime` before you start; run heavy jobs under `nice -n 10`.

**Hardware.** "Pedal" tools need the HX Stomp on USB with HX Edit closed, the scratch preset 04C "Smoke Known" selected, and the helper env vars set. They write to the edit buffer only (never flash) and restore the original preset at the end. They take a shared lock (`tonematch.pedal_lock`) so two jobs never drive the pedal at once. Stomp reamping on macOS also needs Microphone permission for the terminal app (`docs/DEV-ENV.md`).

**Local only.** Song audio, stems, takes, cabs and renders of Line 6 models stay under `data/` and `cache/` (both gitignored) and `reports/local/`. No tool uploads anything. `song_meta_probe` is the only tool that can reach the network and it sends artist and title strings only, after an explicit opt-in.

**Ledger.** Tools that write ledger rows call `scripts/ledger_append.py` (kind, engine, dataset, metric, value; contracts/ledger-format.md). Rows are append-only. A tool with `--no-ledger` prints the rows instead. Compare a new row to the previous row of the same `kind` and `dataset` before claiming an improvement.

**Exit codes.** 0 = ok. 1 = a check failed or a regression was found. 2 = input missing or the needed engine absent. A few tools use 3 (catalog_import). The manifest's `exit_code` repeats the code.

**Disk.** The cache is large (75 GB at the last count) and the disk was at 99% (15 GB free). Check `df -h .` before separation, library or benchmark renders.

## Handing results to an agent

Thirteen user-runnable tools accept `--result-json PATH`. They write a small JSON manifest when they finish (atomically, so a reader never sees half a file). It lists what the tool did, the files it read and wrote, the headline numbers and the ledger rows it appended. An agent reads that one file instead of scraping stdout. The contract is `contracts/cli-result.md`; the helper is `src/tonematch/cli_result.py`.

Schema `cli-result/v0`:

| Field | Meaning |
|---|---|
| `schema`, `tool`, `version` | `cli-result/v0`; script name (a sub-command is appended: `synthetic_v1 run`); `tonematch.__version__`. |
| `status`, `exit_code`, `error` | `ok` only if the tool finished with exit code 0. A non-zero exit (including a check that ran and failed) is `error`; `error` holds `ExceptionType: text` for an exception or `exit code N`. |
| `started`, `finished`, `duration_s` | UTC ISO 8601 and seconds. |
| `args` | The parsed command line. |
| `inputs`, `outputs` | Name to path (or list of paths). Repo-relative with `/` inside the repo, absolute outside it. |
| `metrics` | Headline numbers, named by the tool (listed per tool below). |
| `ledger_rows` | Rows the run appended (or, with `--no-ledger`, would have; those carry `"written": false`). |
| `notes` | Why a tool stopped, dry-run remarks. |

On an exception the manifest is written with `status="error"` and the exception text, then the exception propagates. Example, from a real run of `proc_check`:

```json
{
  "schema": "cli-result/v0",
  "tool": "proc_check",
  "version": "0.0.1",
  "status": "ok",
  "exit_code": 0,
  "error": null,
  "started": "2026-10-18T20:20:41+00:00",
  "finished": "2026-10-18T20:20:41+00:00",
  "duration_s": 0.04,
  "args": {"kill": false},
  "inputs": {},
  "outputs": {},
  "metrics": {"orphans": 0, "zombies": 0, "orphan_pids": [], "zombie_pids": [], "orphan_load_points": 0.0},
  "ledger_rows": [],
  "notes": ""
}
```

A longer one, as `separate` writes it (values illustrative, field names real):

```json
{
  "schema": "cli-result/v0", "tool": "separate", "version": "0.0.1", "status": "ok", "exit_code": 0, "error": null,
  "started": "2026-10-18T18:22:41+00:00", "finished": "2026-10-18T18:22:51+00:00", "duration_s": 10.1,
  "args": {"song": "data/songs/Mother_0-68s.wav", "model": "htdemucs_6s", "dataset": "mother-0-68s", "no_ledger": false},
  "inputs": {"song": "data/songs/Mother_0-68s.wav"},
  "outputs": {"stems_dir": "cache/stems/2ec11f9795be822e/htdemucs_6s",
              "guitar_wav": "cache/stems/2ec11f9795be822e/htdemucs_6s/guitar.wav",
              "sections_json": "cache/stems/2ec11f9795be822e/htdemucs_6s/sections.json"},
  "metrics": {"model": "htdemucs_6s", "backend": "mps", "cached": false, "wall_clock_s": 9.343,
              "confidence_mean": 0.9205, "tempo_bpm": 144.0, "bars": 33, "n_snapshots": 2},
  "ledger_rows": [{"kind": "separate", "engine": "-", "dataset": "mother-0-68s", "metric": "wall_clock_s", "value": 9.34343}],
  "notes": ""
}
```

To use it: `... --result-json cache/results/separate.json`, then read the file. `status` tells you whether to trust the rest; `outputs` tells you where to look; `metrics` gives the numbers; `ledger_rows` tells you what is on the record. Wired tools: `separate`, `match_song`, `match_block`, `synthetic_v1` (`run`, `summarize`), `loss_trust_check`, `engine_smoke`, `playalong_record`, `align_takes`, `hlx_validate`, `licence_check`, `proc_check`, `no_di_invariance` (`run`, `report`) and `gate2_report build`.

Result manifest: **to add** (not wired yet, by design for [T172](../board/TASKS.md#t172 "Command-line tools reference + agent-readable results")) on `ab_session.py` (and `rating_session.py`, `rating_v2.py`), because `benchmark` is rebuilding the rating session; on the `stomp_*` tools (`stomp_live_preview.py`, `stomp_soak.py`, `stomp_verify.py` and the stomp spikes), because `stomp-engine` is fixing the USB freeze; and on `distortion_id_steps01.py`, because `tone-metrics` owns it. Every other user-runnable tool that does not say "wired" is simply not done yet; the wiring is the same few lines (`add_result_arg`, then `cli_result.run`).

---

## 1. Setup and health

### `engine_smoke.py` (wired)

**What it does.** Detects which engines are present (Helix Native, HX Stomp), loads the known preset `fixtures/smoke/known.hlx`, renders a 2 s DI and checks the output (right length, finite, peak between -30 and -1 dBFS, at least 20 dB above silence-through-preset, firmware and model list non-empty). On the Stomp it also checks that `close()` put the original preset back. The exit test for "ship for others": run it on a fresh install with either engine.

**Needs.** At least one engine. Native: `TONEMATCH_NATIVE_PYTHON`. Stomp: pedal on USB, HX Edit closed, `TONEMATCH_STOMPD`, scratch preset 04C. `fixtures/smoke/known.hlx` (build it with `make_smoke_hlx.py`; it is local only). Cost: seconds per engine, no CPU load worth naming.

**Inputs.** `--engine native|stomp|auto` (default `TONEMATCH_ENGINE`, else `auto`: every engine found). `--di auto|real|synthetic|PATH` (default `auto`: the Guitar-TECHS clip `fixtures/smoke/di_2s_real.wav`, synthetic fallback `di_2s.wav`; a custom clip must be 48 kHz float32 mono). `--no-ledger` prints the rows instead of appending them.

**Outputs.** stdout: one line per engine (`native: OK peak -13.5 dBFS, 0.168 s/render`). Ledger: `smoke` / `peak_dbfs` and `render-cost` / `seconds_per_render`, dataset `smoke-real-di` (real clip) or `smoke` (synthetic), engine `native` or `stomp`. Exit 0 pass, 1 failed check, 2 no engine found (no row).

**Examples.**

```bash
.venv/bin/python scripts/engine_smoke.py --no-ledger
TONEMATCH_ENGINE=native TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/engine_smoke.py --engine native --result-json cache/results/smoke.json
TONEMATCH_STOMPD=cache/stomp/bin/stompd TONEMATCH_STOMPD_COMPILE=cache/stomp/bin/stompd-compile TONEMATCH_AGENT=platform .venv/bin/python scripts/engine_smoke.py --engine stomp --di synthetic
```

**Safety.** The Stomp run loads 04C's edit buffer and restores it with a read-back check. No song audio.

**Machine-readable output.** `--result-json`. `metrics`: `engines_detected`, `engines_run`, `dataset`, `engines.<name>` = `{ok, peak_dbfs, seconds_per_render, wall_s}` or `{ok:false, failures}`. `ledger_rows` lists the rows (marked `written:false` with `--no-ledger`). `inputs`: `preset`, `di`.

### `proc_check.py` (wired)

**What it does.** Finds orphaned and zombie processes left by this repo's jobs: multiprocessing pool workers, resource trackers and Native hosts whose parent is gone (PPID 1) and whose working directory is this repo. A top-level script started with `nohup` is never flagged. `--kill` stops the orphans (SIGTERM, then SIGKILL after 5 s); zombies cannot be killed, the report names the parent that must reap them.

**Needs.** Nothing (reads `ps` and `lsof`). Run it before starting heavy work and after any run ends or is killed.

**Options.** `--kill`. No other input.

**Outputs.** stdout: `orphans: N (about X load points); zombies: M` plus one line each. Exit 0 clean, 1 something flagged (or an orphan survived `--kill`).

**Examples.**

```bash
.venv/bin/python scripts/proc_check.py
.venv/bin/python scripts/proc_check.py --kill
.venv/bin/python scripts/proc_check.py --result-json cache/results/proc_check.json
```

**Safety.** Kills only PIDs it flags. Never use a pattern `pkill` instead.

**Machine-readable output.** `--result-json`. `metrics`: `orphans`, `zombies`, `orphan_pids`, `zombie_pids`, `orphan_load_points`; after `--kill` also `killed`, `still_alive_pids`. Status is `error` (exit 1) whenever anything was flagged.

### `licence_check.py` (wired)

**What it does.** The reviewer's licence audit: dependency licences against the allowlist (`docs/licence-allowlist.json`), the `pedalboard` import boundary (only `engines/native_host.py` may import it), a scan for Line 6 assets in tracked files, committed golden presets, and dataset and model cards. Run it on every dependency change.

**Needs.** The installed environment (reads package metadata). No hardware. Seconds.

**Inputs.** `--root DIR` (default: this repo), `--date YYYY-MM-DD` (default today), `--no-write`.

**Outputs.** `reviews/licence-<date>.md` (unless `--no-write`), stdout `wrote ...` then `PASS` or `FAIL`. Exit 1 on any FAIL.

**Examples.**

```bash
.venv/bin/python scripts/licence_check.py
.venv/bin/python scripts/licence_check.py --no-write --result-json cache/results/licence.json
.venv/bin/python scripts/licence_check.py --root . --date 2026-10-18
```

**Safety.** Read-only apart from the report.

**Machine-readable output.** `--result-json`. `metrics`: `overall` (PASS/FAIL), `sections` (name to PASS/WARN/FAIL), `n_dependencies`. `outputs.report_md` when written.

### `catalog_import.py`

**What it does.** Reads the model catalog from the user's own HX Edit install (models, parameters, minimum firmware, DSP costs) into `cache/catalog/` so every tool can validate presets. Line 6 data never enters the repo.

**Needs.** HX Edit installed. Auto-detects the standard macOS, Windows and Wine locations.

**Inputs.** `--hxedit-path PATH` (app bundle or install folder), `--cache-dir DIR` (default `cache/catalog`), `--golden-dir DIR`, `--no-crosscheck` (skip resolving the names in `fixtures/golden/*.hlx`).

**Outputs.** `cache/catalog/<version>.json`, `current.json`, `dsp-costs.json`, `IMPORTS.log`. stdout: version, model and parameter counts per category, cross-check result. Exit 0 ok, 2 HX Edit not found, 3 cross-check found unresolved names.

**Examples.**

```bash
.venv/bin/python scripts/catalog_import.py
.venv/bin/python scripts/catalog_import.py --hxedit-path "/Applications/Line6/HX Edit.app"
.venv/bin/python scripts/catalog_import.py --no-crosscheck --cache-dir cache/catalog
```

**Safety.** Reads the HX Edit install; writes only under the cache. **Machine-readable output.** Not wired; read `cache/catalog/current.json`.

### `make_smoke_hlx.py`

**What it does.** Builds `fixtures/smoke/known.hlx` (one amp, one cab, one bypassed delay, named "Smoke Known") from your own HX Stomp export in `fixtures/golden/`. This is the scratch preset 04C that Stomp tools use and the preset `engine_smoke` loads.

**Needs.** `fixtures/golden/HXStomp.hlx` (your own export). **Inputs.** `--golden FILE`, `--out FILE`. **Outputs.** The `.hlx` (gitignored: it embeds Line 6 model data).

```bash
.venv/bin/python scripts/make_smoke_hlx.py
.venv/bin/python scripts/make_smoke_hlx.py --golden fixtures/golden/HXStomp.hlx --out fixtures/smoke/known.hlx
```

**Safety.** Local file only; never commit it. **Machine-readable output.** Not wired.

### `make_smoke_di.py`

**What it does.** Regenerates `fixtures/smoke/di_2s.wav`, the synthetic Karplus-Strong chord arpeggio (seed 8, 2 s, 48 kHz float32 mono, peak about -12 dBFS) used as the fallback smoke input. No arguments. Deterministic. **Safety.** Overwrites the committed fixture with identical bytes. **Machine-readable output.** Not wired.

```bash
.venv/bin/python scripts/make_smoke_di.py
```

### `native_hc_once.py`

**What it does.** A one-time human step. Opens Helix Native's editor in a host child process and drops `known.hlx` on it so Native asks "Change HC mode?". You tick "Don't ask me again", keep "Change the current HC mode to HX Stomp and load the preset", click OK, close the window. The script then saves the clean HX Stomp start state to `cache/native/start_stomp.state` and reports Native's `ChangeHWModeForFile` preference before and after.

**Needs.** Helix Native, a logged-in GUI session, `known.hlx`, `TONEMATCH_NATIVE_PYTHON`. macOS. **Inputs.** None (only `--help`). **Safety.** Touches Native's preference once, by your hand. Run it again only if Native forgets the answer.

```bash
cd ~/Documents/code/helix_pedal && TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/native_hc_once.py
```

**Machine-readable output.** Not wired.

---

## 2. Audio in and out, and the HX Stomp

All tools here need the HX Stomp on USB, HX Edit closed, and (macOS) Microphone permission for your terminal.

### `reamp_capture.py`

**What it does.** Sends a dry clip through the HX Stomp over USB and captures the result (trimmed by the calibrated round trip and level-matched). `--calibrate` measures the round-trip latency three times plus a level reference. `--user-di` is the play-along mode: it plays a song section with a click count-in on host USB out 1/2 while one duplex stream records your dry guitar (USB in 5/6) and the processed Path A (USB in 1/2), per take, with a JSON of alignment, levels, clipping and silence.

**Needs.** Pedal connected, Input block on USB 5/6 (the helper routes it and puts it back), calibration in `cache/stomp/latency.json` (made by `--calibrate`). `TONEMATCH_STOMPD` for firmware read and routing; `--no-bridge` skips the control interface (then pass `--firmware`).

**Inputs and options.** Positional `dry` (WAV) with `--out wet.wav`. `--calibrate`. `--user-di` with `--play SONG`, `--section START:END`, `--target START:END`, `--song-id`, `--countin N` clicks, `--bpm`, `--sections sections.json`, `--takes N`, `--gap S`, `--no-prompt`, `--song-gain-db`, `--click-dbfs`, `--extra-latency SAMPLES`, `--seconds N` (free play without `--play`), `--dry-run` (decode, cut and plan; no audio device). Also `--tail`, `--repeats`, `--dataset`, `--calibration`, `--usb-reset auto|always|off`, `--ledger`.

**Outputs.** Reamp: `--out` file. Play-along: `cache/user_di/<song-id>/take<k>_{dry,wet}.wav` plus a JSON per take. Calibration: `cache/stomp/latency.json`. With `--ledger`, a row per capture. It never saves anything to the pedal's flash: with the helper it reads the firmware and sets the Input routing in the edit buffer for the run (and for `--calibrate` bypasses the blocks), then puts back exactly what it changed.

**Examples.**

```bash
.venv/bin/python scripts/reamp_capture.py --calibrate --ledger
.venv/bin/python scripts/reamp_capture.py fixtures/smoke/di_2s_real.wav --out cache/reamp/wet.wav
.venv/bin/python scripts/reamp_capture.py --user-di --play data/songs/Mother.mp3 --section 32.45:46.45 --target 36.45:46.45 --countin 4 --takes 3 --dry-run
```

**Safety.** The song stays local. **Machine-readable output.** Not wired; read the take JSONs and `latency.json`. (For the Mother pack, `playalong_record.py` replaced this flow.)

### `playalong_record.py` (wired)

**What it does.** Plays a backing track through the Stomp (host out 1/2) and records your dry guitar (USB in 5) and the processed sound (USB in 1/2) while you play along. One full-duplex stream per take at 48 kHz; the recording is shifted by the calibrated round trip (1207 samples) so it lines up with the played file. Warns about silence, clipping and an unrouted input.

**Needs.** Pedal connected, Input routed to USB 5/6, a calibration (`cache/stomp/latency.json`), a play-along pack (default `data/songs/mother-playalong/`, made by the lead). Audio device only; no control commands. Seconds to minutes (the length of the backing track times the take count).

**Inputs.** `--pack DIR`, `--backing FILE` (default `1_backing_no_guitar_with_countin.wav`), `--with-guitar` (play the full mix instead), `--no-click`, `--takes N` (3), `--seconds N` (first N seconds only), `--level-db` (backing gain, -6), `--countdown S` (10 s before each take), `--check` (3 s of silence out, report the input levels).

**Outputs.** In `<pack>/takes/`: `<stamp>_take<k>_dry.wav`, `<stamp>_take<k>_processed.wav` (float32), `<stamp>_session.json` (per take: peaks, latency, warnings). stdout: peaks and warnings.

**Examples.**

```bash
.venv/bin/python scripts/playalong_record.py --check
.venv/bin/python scripts/playalong_record.py --takes 3 --with-guitar --no-click
.venv/bin/python scripts/playalong_record.py --seconds 8 --takes 2 --countdown 3 --result-json cache/results/playalong.json
```

**Safety.** The backing track is the user's song; takes stay in `data/`. The pedal is not written.

**Machine-readable output.** `--result-json`. `metrics.mode` is `check` (then `dry_peak_dbfs`, `processed_peak_dbfs`, `warnings`) or `record` (`takes`, `seconds`, `latency_samples`, `dry_peak_dbfs` list, `warnings`). `outputs`: `takes_dir`, `session_json`, `dry_wavs`, `processed_wavs`.

### `align_takes.py` (wired)

**What it does.** Lines up play-along takes with the separated guitar stem and picks the tightest. The take and the stem are different guitars, so waveforms do not correlate; note onsets do. Both are reduced to a 10 ms onset-strength envelope; the tool finds the global lag, then the lag and correlation in sliding 6 s windows where the stem has guitar. Score = median window correlation minus a penalty for lag spread. Writes `alignment.take_sample0_song_s`, which `match_song.py --user-di-json` and `match_block.py --di-json` consume.

**Needs.** Takes (`*_dry.wav`) and the stem. Seconds. **Inputs.** Positional `takes` (one or more WAVs), `--stem WAV` (default `data/songs/mother-playalong/target_guitar_stem.wav`), `--take-start-song-s S` (song time at which the played file started; 0 for the no-click full mix).

**Outputs.** `<take>_align.json` next to each take (global lag, windows, `median_corr`, `lag_iqr_ms`, `score`, `alignment`). stdout: a ranking table and `best: <take>`.

**Examples.**

```bash
.venv/bin/python scripts/align_takes.py data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav data/songs/mother-playalong/takes/20261018-113158_take2_dry.wav
.venv/bin/python scripts/align_takes.py data/songs/mother-playalong/takes/20261018-113158_take3_dry.wav --take-start-song-s 0 --result-json cache/results/align.json
.venv/bin/python scripts/align_takes.py takes/new_dry.wav --stem cache/stems/2ec11f9795be822e/htdemucs_6s/guitar.wav
```

Reference values (Mother, 2026-10-18): take1 score 0.344, take2 0.343, take3 0.312.

**Safety.** Offline, local. **Machine-readable output.** `--result-json`. `metrics`: `best_take`, `best_score`, `takes.<name>` = `{score, median_corr, lag_iqr_ms, global_lag_s, windows}`; `outputs.align_json` is the list of per-take files.

### `pedal_probe.py` (result manifest: to add; owned by stomp-engine)

**What it does.** [T187](../board/TASKS.md#t187 "Measured-pedal mode preparation"), measured-pedal mode, **prepared but not approved**. It fingerprints a real drive pedal with the dfp-v1 probe through the HX Stomp's FX loop (host USB out 5/6 -> Input on USB 5/6 -> FX Loop 1 -> Send L -> pedal -> Return L -> Path A -> USB in 1/2) and matches it against the 54-model Native library (dfp-v1 + tone ends).
- `checklist` prints the human's steps (cables, knob notes, what changes on the pedal: only the edit buffer).
- `template` builds the routing preset offline from the 04C backup and, with `--compile`, tries `stompd compile` offline. The v0 compiler refuses Send/Return blocks, so the tool loads the template **in place**: the device's own `model` command turns 04C's delay slot into FX Loop 1, the amp is bypassed, the Input is routed to USB 5/6 (`StompEngine(whole_preset="off")`), and `close()` puts the buffer back.
- `loopback --human [--l2]`: L1, a cable from Send L to Return L, at Send -6 dB (a unity loop puts the probe's 0 dBFS sweeps at full scale). It measures the latency (3 impulses), the loop gain, its response, noise floor and linearity. `--l2` then asks for the cable from Send L to the guitar input and measures the input scale, which sets the Send so the pedal sees the probe at Native's level.
- `capture --human --pedal NAME --setting TEXT --knob k=clock ... [--repeats N] [--noise-take]` plays the 115 s probe, trims and corrects it with the latest loopback, writes the fingerprint and prints the nearest models, families and knob readings.
- `identify FP.npz [--noise NOISE.npz]` matches a stored fingerprint offline.
- `selftest [--identify]` runs the whole pipeline on a fake loop and a toy pedal: no hardware.

**Needs.** Hardware steps: the pedal on USB, HX Edit closed, 04C selected, `cache/stomp/latency.json` from this firmware, `TONEMATCH_STOMPD`, the human at the pedal (`--human`). Offline steps: the catalog cache and the dfp-v1 fingerprints.

**Outputs.** `cache/pedal-probe/` (`template.hlx`, `loopback/<UTC>.json` + its fingerprint, `runs/` engine session backups) and `cache/distortion-fp/pedals/<pedal>/<setting>.npz` + `index.json` (dfp-v1 format, tagged `measured-pedal`, so the Helix library loader never mixes them in). No audio is kept.

```bash
.venv/bin/python scripts/pedal_probe.py checklist
.venv/bin/python scripts/pedal_probe.py template --compile
.venv/bin/python scripts/pedal_probe.py selftest --identify
.venv/bin/python scripts/pedal_probe.py loopback --human --l2
.venv/bin/python scripts/pedal_probe.py capture --human --pedal "My TS" --setting "D 2:00 T 12:00 L 10:00" --knob drive=2:00 --noise-take
.venv/bin/python scripts/pedal_probe.py identify cache/distortion-fp/pedals/My_TS/D_2_00_T_12_00_L_10_00.npz
```

**Safety.** Hardware steps refuse to run without `--human`; they take the pedal lock, write the edit buffer only and restore it (`restore_report`). Nothing is saved on the pedal. Turn the Stomp's volume knob down: the probe reaches 0 dBFS on the main outputs. Design and simulation: `docs/notes/measured-pedal-mode.md`.

### `stomp_live_preview.py` (result manifest: to add; owned by stomp-engine)

**What it does.** Live preview of a running search on the pedal. It watches a search's progress directory (`current_best.json` plus `current_best.hlx`, written atomically by `match_block.py --progress-dir`) and puts each new best preset into the Stomp's **edit buffer** so you can hear it with your own guitar on Main L/R. Loads at most one preset per `--throttle-s` (default 5 s), latest wins.

**Needs.** Pedal, HX Edit closed, scratch preset 04C selected (checked read-only before it starts), `TONEMATCH_STOMPD` and `TONEMATCH_STOMPD_COMPILE`. Holds the shared pedal lock for the whole run.

**Inputs.** `--dir DIR` (required), `--throttle-s`, `--poll-s`, `--once` (load the current best, hold until Ctrl-C), `--restore-on-exit/--no-restore-on-exit` (default on), `--run-dir`, `--ledger-first-load`.

**Outputs.** `cache/stomp/hw/<stamp>-live-preview/` with `preview.log`, `status.json`, `summary.json` and backups. Optional ledger row `smoke` / `first_load_ok`.

**Examples.**

```bash
.venv/bin/python scripts/stomp_live_preview.py --dir cache/matches/Mother-0-68s-drive
.venv/bin/python scripts/stomp_live_preview.py --dir cache/matches/Mother-0-68s-drive --once
.venv/bin/python scripts/stomp_live_preview.py --dir cache/matches/Mother-0-68s-drive --throttle-s 10 --ledger-first-load
```

**Safety.** Edit buffer only; never saved to flash. On exit, Ctrl-C, SIGTERM or SIGHUP the original preset is restored and byte-verified. **Known issue ([T166](../board/TASKS.md#t166 "Live preview on the Stomp"), [T168](../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue")):** a USB wedge happened twice on a new-model load; the pedal then refuses new sessions until its 9V adapter is pulled. Do not run it until stomp-engine reports the fix. **Machine-readable output.** To add; read `summary.json` in the run directory.

### `stomp_soak.py` (result manifest: to add; owned by stomp-engine)

**What it does.** Reproduction and soak test for the control-channel wedge ([T168](../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue")). Loads alternating new-model single drive blocks into the 04C edit buffer, idles 1 to 60 s between loads (optionally with an audio stream through the pedal) and stops at the first sign of trouble. It never retries into a pedal that stopped answering.

**Needs.** As `stomp_live_preview.py`. Minutes to hours.

**Inputs.** `--sequence load|capture`, `--loads N`, `--idle-min`, `--idle-max`, `--audio`, `--seed`, `--helper PATH`, `--helper-env K=V` (repeatable, for diagnosis), `--trace/--no-trace`, `--run-dir`, `--base-hlx`, `--ledger`, `--label`.

**Outputs.** `cache/stomp/hw/<stamp>-soak/`: `soak.jsonl` (one row per step), `summary.json`, `trace.log`. With `--ledger`, `smoke` rows dataset `stomp-soak`.

**Examples.**

```bash
.venv/bin/python scripts/stomp_soak.py --loads 200 --idle-min 1 --idle-max 60
.venv/bin/python scripts/stomp_soak.py --loads 60 --idle-min 1 --idle-max 20 --audio
.venv/bin/python scripts/stomp_soak.py --sequence capture --loads 20 --helper-env STOMPD_PUMP=0
```

**Safety.** This test can wedge the pedal on purpose; run it only when stomp-engine asks. Edit buffer only, original preset restored and verified. **Machine-readable output.** To add.

---

## 3. Songs and separation

### `separate.py` (wired)

**What it does.** Splits a song into stems (guitar, others, and the full set for the six-stem model), builds a time-frequency confidence mask of how exposed the guitar is, and segments the song into bars and snapshots (tempo, sections, exposed windows). Everything is cached by the song's content hash. You can supply your own guitar stem with `--stem` (separation is skipped; the mix is still required).

**Needs.** The separation extras (torch and `demucs`). A GPU is optional: the backend is detected (CUDA, Apple MPS, ROCm, DirectML, else CPU) or forced with `--backend` or `TONEMATCH_DEVICE`. Measured: 68 s of audio, `htdemucs_6s`, Apple MPS, 9.3 s (ledger `separate` / `mother-0-68s`); a 4-minute song is about 20 to 40 s on MPS (planning estimate; CPU is several times slower). Disk: a few hundred MB of WAV per song under `cache/stems/`. The roformer models need `TONEMATCH_ROFORMER_LICENCE_ACK` and a checkpoint (`TONEMATCH_ROFORMER_CKPT`).

**Inputs.** Positional `song` (any format soundfile reads). `--stem WAV`, `--model htdemucs_6s|bs-roformer|mel-roformer|mdx23c` (default `htdemucs_6s`), `--backend cpu|mps|cuda|rocm|directml`, `--override "1-8:0,9-16:1"` (bar to snapshot, 1-based inclusive; a user override is never overwritten by a plain re-run), `--dataset ID` (ledger id; default file stem plus hash), `--no-cache`, `--no-ledger`.

**Outputs.** `cache/stems/<hash>/<model>/`: `mix.wav`, `guitar.wav`, `others.wav` (and `bass`, `drums`, `vocals`, `piano`, `other` for six stems), `confidence.npy`, `sections.json`, `manifest.json`. stdout: cache hit or separated, model, backend, wall clock, confidence, tempo, a bar-to-snapshot table. Ledger (kind `separate`, not written on a cache hit): `wall_clock_s` and `confidence_mean` (a heuristic proxy). Exit 2 if no separator is available.

**Examples.**

```bash
.venv/bin/python scripts/separate.py data/songs/Mother_0-68s.wav --dataset mother-0-68s
.venv/bin/python scripts/separate.py data/songs/Mother.mp3 --backend cpu --no-ledger --result-json cache/results/separate.json
.venv/bin/python scripts/separate.py data/songs/Mother.mp3 --stem data/stems/my_guitar.wav --override "1-8:0,9-16:1"
```

**Safety.** No audio leaves the machine; hosted separation APIs are deliberately not used. **Machine-readable output.** `--result-json`. `metrics`: `model`, `backend`, `cached`, `wall_clock_s`, `song_hash`, `user_supplied_stem`, `confidence_mean`, `exposed_fraction`, `tempo_bpm`, `bars`, `n_snapshots`, `exposed_windows`. `outputs`: `stems_dir`, `guitar_wav`, `others_wav`, `mix_wav`, `confidence_npy`, `sections_json`, `manifest_json`. The `stems_dir` goes straight into `match_song.py --stems-dir`.

### `song_meta_probe.py`

**What it does.** A prototype that asks MusicBrainz for a song's genre tags and year from artist and title strings alone (design: `docs/notes/song-context.md`). One search call per song, 1 request per second, descriptive User-Agent, results cached in `cache/song_meta/`.

**Privacy.** The default is a dry run that makes no network call and prints the URLs it would request. A live call needs both `--live` and `--accept-privacy`, plus a contact address in `TONEMATCH_CONTACT` or `--contact`. Only artist and title strings leave the machine, never audio, file names or paths. MusicBrainz tags are CC BY-NC-SA: do not commit or redistribute the cache.

**Inputs.** `--song ARTIST TITLE` (repeatable; default three built-in examples), `--dry-run`, `--live`, `--accept-privacy`, `--contact`.

```bash
.venv/bin/python scripts/song_meta_probe.py --song "Metallica" "Master of Puppets"
.venv/bin/python scripts/song_meta_probe.py --song "Metallica" "Master of Puppets" --live --accept-privacy --contact you@example.com
```

**Machine-readable output.** Not wired; read `cache/song_meta/`.

---

## 4. Matching

Both tools search on Helix Native, then write the best presets as `.hlx`, validate them and build a listening pack. Every loss number is the **provisional, unadmitted** loss (equal weights on `ltas_tilt`, `crest_flatness_hnr`, `envelope`, plus a level guard): lower is better, the numbers rank candidates against each other and are not a fidelity score. The ear decides.

### `match_song.py` (wired)

**What it does.** End-to-end match of one section of a song: picks the most guitar-dominant ~10 s of a snapshot (or the window you give), picks DI clips (or uses yours), searches a beam of Native chains (K, A, B0, B, A30, C, H templates; CMA-ES), refines the best, checks the top 3 on the full excerpt with stems and mask, writes the best preset as `.hlx` (firmware 3.50, HX Stomp), validates it and builds `report.md` and a listening pack.

**Needs.** Stems from `separate.py`, Native with captured templates, `TONEMATCH_NATIVE_PYTHON`. Heavy: up to 6 Native hosts through the scaler, one core each. Measured on the Mother section: 1719 renders in 1360 s, 0 host restarts (ledger `search` / `mother-0-68s`). The Native host leaks memory per render; `--rss-limit-mb` (9000) restarts it.

**Inputs.** `--stems-dir DIR` (a `separate.py` output), `--song NAME`, `--out DIR` (default `cache/matches/DS-Sample`). Target: `--snapshot N` and `--dur S` (automatic pick), or `--target START:END` in song seconds. DI: `--user-di WAV` (true level, not normalised), `--user-di-json align.json` (from `align_takes.py`), `--user-di-extra WAV` (repeatable, for the DI-sensitivity check), else `--di-library`. Search: `--wall-min` (25), `--reserve-s` (150), `--beam`, `--chains K,A,...`, `--max-scores N` (trial budget independent of engine speed), `--clip-policy full|loudest3|loudest5|beam3-refine-full|spread`, `--seed`, `--prior-weight`, `--level-tol-db`, `--level-slope`, `--snap0-renders` (0 skips the snapshot 0 refine), `--model-choice`. Engine: `--engine native|stomp` (stomp renders on the pedal; see its safety rules), `--native-only` (leave Stomp-parity-frozen parameters free). Utility: `--dry-run`, `--rewrite-hlx RUN_DIR`, `--note`, `--preset-name`.

**Outputs.** In `--out`: `best_snapshot1.hlx` (+ `.validate.md`), `best_snapshot0.hlx`, `rank1_*.hlx` / `rank2_*.hlx`, `report.md`, `result.json` (finals, losses, render counts, host restarts), `progress.json`, `run.log`, `listen/` (target stem, match, start-unmatched, A/B, DI; loudness-matched with `gains.json`). No ledger rows by itself; the lead records the headline (`search` / `provisional_loss_best`).

**Examples.**

```bash
# the Mother play-along match (T163)
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/match_song.py --stems-dir cache/stems/2ec11f9795be822e/htdemucs_6s --song Mother-0-68s --out cache/matches/Mother-0-68s --user-di data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav --user-di-json data/songs/mother-playalong/takes/20261018-113158_take1_align.json --target 28.3:38.3 --wall-min 25
# what would it do? (renders nothing)
.venv/bin/python scripts/match_song.py --stems-dir cache/stems/2ec11f9795be822e/htdemucs_6s --song Mother-0-68s --out cache/matches/dry --target 28.3:38.3 --chains A,H --dry-run --result-json cache/results/match_song_dry.json
# re-write the top-3 presets of a finished run
.venv/bin/python scripts/match_song.py --rewrite-hlx cache/matches/Mother-0-68s
```

**Safety.** Local only. The Stomp is not touched unless you pass `--engine stomp`. **Machine-readable output.** `--result-json`. `metrics`: `label` ("unadmitted loss"), `renders`, `wall_s`, `host_restarts`, `start_loss`, `dry_loss`, `best_chain`, `best_loss`, `top` (three chains with losses); a dry run gives `dry_run: true`. `outputs`: `out_dir`, `run_log`, `best_snapshot1_hlx`, `best_snapshot0_hlx`, `report_md`, `result_json`, `listen`.

### `match_block.py` (wired)

**What it does.** Single-block match: searches every model of one category (default `drive`) of the block library, with its settings, against a window of a target stem, rendering through your own DI. Built for "match this one distortion". Hybrid search (broad draws, top-k, successive halving with CMA-ES) over shard workers, one Native host each, then a final check on the full window.

**Needs.** As `match_song.py`. The play-along DI is aligned by onsets, not by sample, so the sample-aligned `mrstft_v2` is not used to rank (it is reported for reference only).

**Inputs.** `--target-window START:END` (required), `--di WAV` (required, true level), `--out DIR` (required), `--target-stems DIR`, `--di-json align.json`, `--category` (drive), `--song`, `--wall-min` (25), `--workers auto|N`, `--k` (models refined per shard), `--hybrid-f`, `--hybrid-scale`, `--specialists [FAMILY ...]` (default `gain`), `--search-clip S`, `--final-top N`, `--level-tol-db`, `--level-slope`, `--amp-cab-loss`, `--knob-tol T` (default 0.15 for `drive`, 0 for other categories; 0 gives the old T164 behaviour: stop refining a knob once it is within T on a 0..1 scale, because the human could not hear Drive or Tone steps of 0.05 to 0.2, and spend the renders on model choice instead: first a coarse grid of about 3 Drive x 2 Tone settings on every model, then the top models, twice as many as `--k`, are refined and each refinement stops once its search step on the knobs, or its last two improvements, are inside T; the render budget (set by `--wall-min`) is a ceiling, so a match ends sooner), `--knob-tol-drive T` (Drive tolerance, 0.15), `--knob-tol-tone T` (Tone/Treble/Filter/Contour tolerance, default 0.1, and 0.05 within 0.1 of either end of the knob, where the knob does most of its work), `--knob-tol-map PATH` (per-model tone/drive knob names and tolerances; read from `reports/local/distortion-id/tone-ends/knob_sensitivity.json` or `cache/drive-maps/knob_sensitivity.json` when one exists) — these build a per-knob tolerance (`search/knob_tol.py`) and `--knob-tol X` still means the old single tolerance (T178); `--shortlist afxrep` (T178, for no-DI matching: rank all models by AFx-Rep distance between the target and each model's coarse renders, keep `--shortlist-n` (8) plus their measured family from `cache/drive-maps/drive-families-v1.json` (or `--families`) when that file exists; knobs then come from the fingerprint inversion if `tonematch.metrics.distortion_step4.estimate_knobs` exists, else from `--aligned-take` (the take is sample-aligned, so the shortlist is searched with `mrstft_v2`), else from the coarse Drive x Tone grid alone, reported as LOW CONFIDENCE: a sound score never counts as knob accuracy; default `none` keeps the old behaviour), `--preset-name`, `--preroll`, `--seed`, `--progress-dir DIR` (writes `current_best.hlx/.json` atomically whenever the best improves, for `stomp_live_preview.py`), `--dry-run`.

**Outputs.** In `--out`: `best.hlx`, `rank2.hlx`, `rank3.hlx` (+ `.validate.md`), `report.md`, `result.json`, `shard<i>.json`, `run.log`, `listen/`.

**Examples.**

```bash
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/match_block.py --target-stems cache/stems/2ec11f9795be822e/htdemucs_6s --target-window 28.3:38.3 --di data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav --di-json data/songs/mother-playalong/takes/20261018-113158_take1_align.json --category drive --song Mother-0-68s --out cache/matches/Mother-0-68s-drive --progress-dir cache/matches/Mother-0-68s-drive
.venv/bin/python scripts/match_block.py --target-window 28.3:38.3 --di data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav --out cache/matches/dry --dry-run
.venv/bin/python scripts/match_block.py --target-window 28.3:38.3 --di data/songs/mother-playalong/takes/20261018-113158_take1_dry.wav --category amp --workers 2 --wall-min 10 --out cache/matches/Mother-amp --result-json cache/results/match_block.json
```

**Safety.** Local only; no pedal. **Machine-readable output.** `--result-json`, written by the parent process only (shard workers write none). `metrics`: `label`, `renders`, `wall_s`, `workers`, `n_models`, `best_model`, `best_loss`, `top` (five). `outputs`: `out_dir`, `run_log`, `best_hlx`, `rank2_hlx`, `rank3_hlx`, `report_md`, `result_json`, `listen`.

### `search_smoke.py`

**What it does.** A smoke demo, not a benchmark: searches Drive on the captured `known.hlx` chain (Native, knob 01). The target is a Native render at Drive 0.75; the search starts at the file's Drive and uses a spectral stub loss, about 16 renders. Shows the engine and search plumbing work. Writes no ledger row. No arguments.

```bash
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python .venv/bin/python scripts/search_smoke.py
```

**Machine-readable output.** Not wired.

---

## 5. Benchmarking and evaluation

Rules for every tool here: a result is compared to the previous ledger row of the same `kind` and `dataset` before it is called an improvement; a benchmark regression freezes merges; a sealed or SPENT set (`synthetic-v1`, `-v2`, `-v3`) is never re-run for a claim; the ear is the final judge (these tools never score a listening session).

### 5a. Suites and recovery

#### `benchmark_suite.py`

**What it does.** Runs every benchmark test in a fixed order, one child process at a time, times each one, and records the run: engine smoke (Native, Stomp), hardware parity, loss-trust check (proxy), synthetic recovery (CI subset), separation stress, block-probe validate, unit tests (`pytest -q`) and lint (`ruff`). Statuses: `pass`, `fail`, `skipped` (chosen or unable), `not available` (what it needs does not exist yet; neither a pass nor a failure).

**Needs.** The pedal for the Stomp tests (HX Edit closed; the suite pre-checks and never interrupts another agent's job); Native for the rest. Last full run: 348 s (2026-10-15). Heavy, so run under `nice`.

**Inputs.** `--only ID [ID ...]` (smoke_native, smoke_stomp, parity, loss_trust, synthetic_recovery, separation_stress, block_probe, unit_tests, lint), `--skip ID ...`, `--no-hardware` (every pedal test becomes `skipped, no pedal`), `--no-ledger` (also none from the tests), `--list` (print the ids in run order), `--render-doc` (only refresh the generated section of `docs/BENCHMARKING.md` from `reports/benchmark/latest.json`).

**Outputs.** `reports/benchmark/history/<UTC stamp>.json` (small, committed) and `reports/benchmark/latest.json`; ledger row `benchmark` / `suite` / `suite_wall_clock_s` plus the rows of each test; the generated section of `docs/BENCHMARKING.md`. Exit 0 when nothing failed, 1 when a test failed.

**Examples.**

```bash
.venv/bin/python scripts/benchmark_suite.py --list
nice -n 10 .venv/bin/python scripts/benchmark_suite.py --no-hardware --no-ledger
nice -n 10 .venv/bin/python scripts/benchmark_suite.py --only unit_tests lint --no-ledger
```

**Safety.** The hardware rules live in the scripts it calls (edit buffer only, 04C restored and byte-diffed). No song audio. **Machine-readable output.** Not wired; `reports/benchmark/latest.json` is the structured record (per-test status, key result, run time, ledger rows).

#### `synthetic_v1.py` (`run` and `summarize` wired; `generate`)

**What it does.** The synthetic recovery set and its harness (Gate 2). Known one-block presets from the Native block library are rendered through clean guitar clips; the matcher is given only the sound and a pool of candidate models and must name the category, the model and the settings. `generate` draws a set, `run` runs the matcher on it, `summarize` merges the item files into metrics.

**Needs.** Native with the block library, `TONEMATCH_NATIVE_PYTHON`. `run` is heavy: Native hosts through the scaler, `--budget` renders per item (default 150), 96 items in a full set. The summary's `full_run_estimate_s` extrapolates the cost. `generate` is offline.

**`generate` inputs.** `--seed`, `--items-per-category` (12), `--categories drive delay reverb comp eq mod amp cab` (default all), `--avoid-sets SET ...` (DI excerpts no item may overlap), `--no-check-targets`, `--set NAME` (default `synthetic-v1`; the set lives in `cache/NAME/`).

**`run` inputs.** Item choice: `--items N` (first N in category round-robin), `--ids i003 i019`, `--shard k/n`. Search: `--budget`, `--beam`, `--weights provisional|FILE` (admitted weights, e.g. `data/weights/gate2-known-di-v3.json`), `--hybrid` with `--hybrid-f`, `--hybrid-k`, `--hybrid-scale`, `--hybrid-level-free`, `--hybrid-fingerprint`; `--specialists all|gain|linear|dynamics|time|mod`, `--model-choice`, `--seed-measured`, `--known-di`, `--no-rank1`. Run control: `--workers auto|N` (elastic supervisor sized by the host scaler), `--priority high|normal`, `--redo`, `--tag TAG` (run folder name), `--baseline middle|random` (a baseline instead of the search), `--save-audio` (target and recovered WAVs on the new notes, for the blind listen), `--ledger`, `--note`, `--set NAME`. Diagnostic knobs from T143/T162 (`--family-prior`, `--prior-gate`, `--keep-top`, `--shortlist`, `--rank-slack`, `--drive-seeds`, `--drive-shortlist`, `--gain-drive`, `--seed-corrupt P,SIGMA`, `--seed-stratify`, `--di-mismatch`) are listed by `run --help`.

**`summarize` inputs.** `--tag`, `--baseline middle|random` (merge baseline shards), `--items`, `--ids`, `--weights`, `--budget`, `--ledger`, `--note`, `--set`.

**Outputs.** `cache/<set>/`: `manifest.json`, `labels.json` (model ids; local), `di/`, and per run `runs/<tag>/i###.json` (one per item), `summary.json` (or `summary_<baseline>_<dataset>.json`). stdout: the summary JSON. With `--ledger`, rows `benchmark` / `native` / dataset `synthetic-v1` (or `<set>-n<k>[-shard...]` for a partial run) for `block_f1`, `model_acc`, `param_mae`, `param_mae_level`, `rank1_rate`, `rank1_rate_audible`, `loss_heldout`, `heldout_ratio`. A regression against the previous row prints to stderr and exits 1. A run with provisional weights is labelled "provisional, not gate evidence".

**Examples.**

```bash
.venv/bin/python scripts/synthetic_v1.py generate --set synthetic-devfam --categories drive amp --items-per-category 8 --avoid-sets synthetic-v1 synthetic-v2
nice -n 10 .venv/bin/python scripts/synthetic_v1.py run --set synthetic-devfam --items 16 --budget 150 --hybrid --workers auto --tag dev16 --weights data/weights/gate2-known-di-v3.json
.venv/bin/python scripts/synthetic_v1.py summarize --set synthetic-devfam --tag dev16 --weights data/weights/gate2-known-di-v3.json --budget 150 --result-json cache/results/synthetic_summary.json
```

**Safety.** Offline apart from Native renders. Do not run against SPENT sets for a claim. `run --workers` starts worker processes: check `proc_check.py` afterwards.

**Machine-readable output.** `--result-json` on `run` and `summarize` (not `generate`). `metrics`: `dataset`, `weights`, `admitted`, `known_di`, `budget`, `renders`, `wall_s`, `gate_evidence`, `items`, `set`, and the ledger metrics above. `outputs`: `run_dir`, `summary_json`. `ledger_rows`: the rows written by `--ledger`. Elastic workers (`--as-worker`) never write a manifest; the supervisor does.

#### `sep_stress.py`

**What it does.** The separation stress curve (dataset `sep-stress-v1`): Guitar-TECHS DI excerpts through a numpy drive and cab stand-in, mixed with procedurally synthesised accompaniment (no third-party audio) at five densities and several levels, separated with `htdemucs_6s` (and any other installed separator), scored against the known stem. Measured: 39 mixes at 3.1 s each on MPS.

**Inputs.** `--clips 02,03,08`, `--secs 20`, `--models htdemucs_6s`, `--no-ledger`. **Outputs.** `cache/sep-stress-v1/`; ledger `stress` / `sep-stress-v1` (SDR per density, `tone_shift_*`, `seconds_per_mix`).

```bash
nice -n 10 .venv/bin/python scripts/sep_stress.py --clips 02,03 --secs 10 --no-ledger
.venv/bin/python scripts/sep_stress.py --clips 02,03,08 --secs 20 --models htdemucs_6s
```

**Safety.** No song audio. **Machine-readable output.** Not wired.

#### `seed_quality_report.py` and `seed_quality_grid.sh`

**What they do.** [T162](../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment") diagnostic: the shell driver runs the registered grid of corrupted-seed experiments (`synthetic_v1.py run` cells under `cache/synthetic-devfam/runs/t162{a,b}_*`, resumable: a cell with a `summary.json` is skipped); the report aggregates the cells, takes paired differences against the unseeded hybrid at the same budget and prints a markdown table. Diagnostic only, not gate evidence. The grid is very heavy (Native for hours); run it only when the lead has scheduled it.

**Inputs.** `seed_quality_report.py {a,b}` with `--ledger` and `--n N` (first N items per cell). `scripts/seed_quality_grid.sh a|b` (env `ITEMS`, `PILOT`). **Outputs.** `reports/t162/cells_<cond>.json`; with `--ledger`, `search` rows.

```bash
.venv/bin/python scripts/seed_quality_report.py b --n 12
ITEMS=12 PILOT=1 scripts/seed_quality_grid.sh b
```

**Machine-readable output.** Not wired.

#### `no_di_invariance.py` (wired)

**What it does.** No-DI step 1 ([T161](../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)")): which candidate scores can still tell Helix settings apart when the target and the candidate renders come from different guitar samples. Uses cached renders only (no Native). A diagnostic; it admits nothing. Write-up: `docs/notes/no-di-invariance.md`.

**Inputs.** `run`: `--jobs N` (default 5, at most 6 are used), `--sources V1 DRIVE MOD V2`, `--limit N` (groups per source; smoke runs), `--no-afx`, `--out FILE`. `report`: `--inp FILE`, `--ledger`.

**Outputs.** `cache/no-di-invariance/distances.json`, `summary.json`, `tables.md`. With `--ledger`: `loss-trust` rows `invariance_ratio_<score>` and `rank1_rate_<score>`, dataset `helix-trust-v2-xdi`. [T161](../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") was stopped by the human on 2026-10-18 mid-run; no results exist yet.

```bash
nice -n 10 .venv/bin/python scripts/no_di_invariance.py run --sources V1 --limit 2 --jobs 2
.venv/bin/python scripts/no_di_invariance.py report --result-json cache/results/no_di_report.json
```

**Machine-readable output.** `--result-json` on `run` and `report`. `report` gives `metrics.headline.<score>` = `{invariance_ratio, rank1, pooled_rank1, n}` and `outputs.summary_json`, `tables_md`; `run` gives `metrics.meta` and `outputs.distances_json`. With `report --ledger` the rows appear in `ledger_rows`.

#### `drive_xdi_scores.py`

**What it does.** The different-guitar test of the drive scores ([T175](../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive")): do `embed_afxrep`, `ltas_eqfit` and `ltas_tilt` (with `mrstft_v2` as control) still pick the right drive setting when the target and the candidates are played on different DIs, by the same player or by another player on another guitar. Cached drive-specialist renders only (no Native), helix-trust-v2 train drive models only. A measurement; it admits nothing. Write-up: `docs/notes/drive-xdi-scores.md`.

**Inputs.** `plan` (no options). `run`: `--jobs N` (default and maximum 4), `--blocks N` (preset blocks per DI, default 4). `report`: `--ledger`.

**Outputs.** `cache/drive-xdi/plan.json`, `features.pkl`, `mrstft.json`, `run_meta.json`, `summary.json`, `tables.md`. With `--ledger`: `loss-trust` rows `rank1_rate_<score>`, `pairwise_win_<score>`, `invariance_ratio_twin_<score>`, dataset `helix-trust-v2-xdi-drive`. A full run takes about 30 minutes at 4 workers.

```bash
nice -n 10 .venv/bin/python scripts/drive_xdi_scores.py run --jobs 4
.venv/bin/python scripts/drive_xdi_scores.py report --ledger
```

**Machine-readable output.** Not wired.

### 5b. Loss-trust checks

A loss term is trusted only if it ranks the true setup first among decoys. All of these write `loss-trust` ledger rows with `--ledger`.

#### `loss_trust_check.py` (wired)

**What it does.** For each item, scores the true preset and N distractors against a target with one loss term alone and records whether the truth ranks first (rank-1), the mean rank, pairwise win rates and per-category results. `--proxy` is offline on numpy stand-in effects (not Helix, not admission evidence); `--helix` scores the rendered Helix Native trust set (`helix-trust-v1`, or `helix-trust-v2` with `--split`).

**Needs.** Proxy: nothing but CPU (about 87 s for 40 items by 15 decoys). Helix: the set rendered by `helix_trust_set.py` or `helix_trust_v2.py`. Embedding terms (`embed_afxrep`) need the `embed` extra and a checkpoint, and cap `--jobs` at a small number.

**Inputs.** `--term T [T ...]` or `--all` (every numpy DSP term), choosing from `ltas_tilt`, `crest_flatness_hnr`, `envelope`, `mrstft`, `mrstft_v2`, `level_db`, `embed_afxrep`. `--proxy` / `--helix`, `--helix-dir`, `--split train|test` (tune on train only; test is scored once), `--mrstft-v2-config FILE`, `--items`, `--distractors`, `--seed N [N ...]` (seeds 0 and 1 were used to tune the term scales; never evaluate on those), `--mask`, `--seconds`, `--bleed-db`, `--no-sep`, `--jobs`, `--out FILE`, `--ledger`, `--ledger-subsets`, `--note`.

**Outputs.** JSON report (default `cache/loss-trust/proxy-dsp-v0<tag>.json`, or `report[-split].json` in the Helix set folder); a table on stdout; with `--ledger`, `loss-trust` rows (`rank1_rate`, `rank1_rate_audible`, `seconds_per_test`, ...), dataset `proxy-dsp-v0` or `helix-trust-v1/v2`.

**Examples.**

```bash
nice -n 10 .venv/bin/python scripts/loss_trust_check.py --all --proxy --items 40 --distractors 15 --seed 9301 9302 --jobs 4
.venv/bin/python scripts/loss_trust_check.py --term level_db envelope --proxy --items 5 --distractors 4 --seconds 3 --jobs 1 --out cache/loss-trust/quick.json --result-json cache/results/lt.json
nice -n 10 .venv/bin/python scripts/loss_trust_check.py --term mrstft_v2 --helix --split test --helix-dir cache/loss-trust/helix-trust-v2 --ledger
```

**Machine-readable output.** `--result-json`. `metrics`: `dataset`, `proxy`, `items`, `distractors`, `chance_rank1_rate`, `wall_clock_s`, `rank1_rate.<subset>.<term>`; `outputs.report_json`; `ledger_rows` with `--ledger`.

#### `helix_trust_set.py` and `helix_trust_v2.py`

**What they do.** Build and render the Helix trust sets that `loss_trust_check.py --helix` scores: real Native renders of known chains (`helix-trust-v1`: the K chain with one block changed, 8 categories, DI A same notes and DI B held-out) and the known-DI set (`helix-trust-v2`: single blocks of the block library, disjoint train/test models and DI recordings). Each item: the true preset, 9 near misses, neighbours and random blocks to make 19 distractors.

**Sub-commands.** v1: `gen` (`--seed`, `--items`, `--distractors`, `--seconds`), `add-di` (DI C: another passage of A's recording), `render` (`--hosts` at most 3, `--chunk`, `--only`). v2: `gen` (`--seed`, `--force`), `render` (`--hosts` at most 4, `--chunk`, `--only`); `--out DIR`. Render is resumable and refuses if the machine would exceed 6 `native_host` processes. Output: `cache/loss-trust/helix-trust-v1|v2/` (local, holds model ids).

```bash
.venv/bin/python scripts/helix_trust_v2.py gen
nice -n 10 .venv/bin/python scripts/helix_trust_v2.py render --hosts 3
```

**Machine-readable output.** Not wired; `render_summary.json` in the set folder has per-chunk timings.

#### `gain_trust.py`

**What it does.** The gain-stage loss-trust sets `gain-trust-v1` and `gain-trust-v1b` (amp and drive against 16 gain-stage neighbours) and the score check. `gen` (offline), `render` (Native, at most 2 hosts), `features` and `nulls` (offline, cached), `eval` (rule-B rank-1 per family for named scores), `diag-v2` (diagnosis only; synthetic-v2 is spent).

**Inputs.** `gen --dataset gain-trust-v1|gain-trust-v1b [--seed] [--force]`; `render --dataset ... [--hosts] [--chunk]`; `features|nulls [--root] [--procs]`; `eval [--rule extended|direct] [--datasets ...] [--split] [--score ...] [--categories ...] [--show-fails] [--json FILE]`. Output: `cache/loss-trust/<dataset>/` and `features-<dataset>.json`.

```bash
nice -n 10 .venv/bin/python scripts/gain_trust.py eval --rule extended --datasets gain-trust-v1b --score mrstft_v2 --json cache/results/gain_eval.json
```

**Machine-readable output.** Not wired; `eval --json FILE` writes the table.

#### `mod_trust_check.py`, `time_specialist_trust.py`, `presence_eval.py`

**What they do.** The same question for specialists. `mod_trust_check.py` (`mod-trust-v1`): does the modulation specialist's distance rank the true modulation preset first when the target's LFO is at a different phase; `gen` (offline, refuses to redraw a registered set), `render` (Native, `--hosts` at most 2, `--chunk`), `score` (`--jobs`, `--report`, `--ledger`; rows `loss-trust` and `benchmark`, dataset `mod-trust-v1`). `time_specialist_trust.py`: rank-1 of the true delay or reverb preset with and without the time-family term; `render [--set] [--categories ...] [--truth-only]`, `score [--set] [--weights] [--weights-grid ...] [--ledger] [--ledger-weights ...]`. `presence_eval.py` (`presence-v1`): is a block present, per category, from cached helix-trust-v2 renders; no Native, no pedal; `extract --split train|test [--workers] [--limit]`, `fit` (train only), `score --split test [--ledger]`, `reverb-synth --split ... [--n-items] [--ledger]`, `chains`, `gen-chains [--n]`. The test split is scored once.

```bash
.venv/bin/python scripts/mod_trust_check.py score --jobs 4 --report cache/mod-trust-v1/report.json
.venv/bin/python scripts/time_specialist_trust.py score --set synthetic-devfam --weights 0.25
.venv/bin/python scripts/presence_eval.py extract --split train --workers 4 --limit 8
```

**Machine-readable output.** Not wired (they write `report.json` in their cache folder).

#### `mrstft_tune.py`

**What it does.** Tunes the `mrstft_v2` loss on the TRAIN split of helix-trust-v2. There is deliberately no way to point it at the test split. A stage file `{"name": ..., "configs": [{...}]}` lists configurations; for each it reports rule-B rank-1 on the aligned and separated targets and the selection objective registered in DECISIONS ([T096](../board/TASKS.md#t096 "Gate 2 option A + rule B")).

**Inputs.** `--stage FILE`, `--set-dir DIR`, `--jobs`, `--items N` (first N train items; smoke), `--out FILE`, `--ledger`, `--ledger-report FILE` (write rows for a finished tune report, then exit), `--note`. Output: a tune JSON; `loss-trust` rows with `--ledger`.

```bash
nice -n 10 .venv/bin/python scripts/mrstft_tune.py --stage stage1.json --jobs 4 --items 8 --out cache/loss-trust/tune-smoke.json
```

**Machine-readable output.** Not wired.

### 5c. Parity: the Stomp against Native

These render the same preset and DI on both engines and compare them. Hardware part: one batched Stomp session (shared pedal lock, scratch slot, restore with read-back and a byte diff). Run the pedal part from your own Terminal (Microphone permission; Native's window opens briefly for each new chain); HX Edit must be quit.

#### `parity_null.py`

**What it does.** The null test: the same `.hlx` and DI through the Stomp and Native, aligned, level-matched and subtracted. `gen` writes the two extra presets (C clean, H high gain) and cuts the 10 s DI with a silent tail; `check` parses and validates (and with `--base` compiles each preset offline and checks every parameter); `run` renders on both engines and analyses.

**Inputs.** `check`/`run`: `--presets NAME|PATH ...` (names in `cache/stomp/inject/`), `--di FILE`; `check --base BACKUP`; `run --out-dir`, `--engines stomp native`, `--reuse`, `--stomp-gap-s`, `--no-ledger`, `--usb-reset auto|always|off`.

**Outputs.** `cache/parity/<stamp>/`: `<name>_stereo.wav` (left Stomp, right Native, aligned and level-matched), `<name>_residual.wav`, `<name>.json`, `summary.json`. Ledger `parity` / `both` / `parity-v0`: `null_residual_db` (lower is closer; pass at or below -30 dB), gain, delay-tail level. Known non-identities are stated in each summary (firmware 3.50 vs Native 3.82, the Stomp output stage, tempo-synced delays).

```bash
.venv/bin/python scripts/parity_null.py gen
.venv/bin/python scripts/parity_null.py check --presets A B0 C
.venv/bin/python scripts/parity_null.py run --presets A C --engines native --no-ledger
```

**Safety.** The pedal part restores 04C and byte-diffs it. **Machine-readable output.** Not wired; `summary.json` in the out dir.

#### `parity_spot.py`, `parity_timefx.py`, `parity_timefx_stimuli.py`

**What they do.** `parity_spot.py` ([T060](../board/TASKS.md#t060 "Parity spot checks: at least 5 models per block category on Stomp")): at least 5 models per block category, K chain with one block changed, rendered on Native and on the Stomp from the same `.hlx` and DI, compared by null (delays also by echo level and timing). Stages `gen` (offline; firmware 3.80 presets), `native` (`--only`, `--hosts`), `stomp` (`--only`, `--lock-wait-s`; ONE batched pedal session) and `analyse` (`--ledger`; rows `parity-spot-fw380`). `parity_timefx.py` ([T066](../board/TASKS.md#t066 "Parity metrics for time-based effects")): scores the saved [T060](../board/TASKS.md#t060 "Parity spot checks: at least 5 models per block category on Stomp") renders with delay, reverb, modulation and nonlinear measurements instead of a null (no pedal, no Native): `--ledger`, `--only Del_ Rev_ Mod_ Dis_`, `--rows-only`; output `cache/parity/spot380/timefx.json`, rows dataset `parity-spot-fw380-timefx`. `parity_timefx_stimuli.py` ([T066b](../board/TASKS.md#t066b "Parity probe session closing the T066 open questions")): purpose-made stimuli (noise burst, sustained sines, a guitar note) for reverbs and modulation: `gen`, `native` (`--only`, `--stim`, `--hosts`), `stomp` (`--only`, `--stim`, `--lock-wait-s`), `analyse` (`--ledger`, `--long`, `--task`); output `cache/parity/timefx-stimuli/`.

```bash
.venv/bin/python scripts/parity_spot.py gen
nice -n 10 .venv/bin/python scripts/parity_spot.py native --only Del_ --hosts 2
.venv/bin/python scripts/parity_timefx.py --only Del_ Rev_
```

**Safety.** The `stomp` stages follow the pedal rules above. **Machine-readable output.** Not wired.

### 5d. Engine verification suites

#### `native_verify.py`

**What it does.** Verifies Helix Native end to end ([T103](../board/TASKS.md#t103 "Native verification suite scripts/native_verify.py")): every setting we send lands exactly and renders are consistent. `audit` (random model and settings on each template's CC grid against a drop of the same values), `baked` (baked values equal the presets'), `landing` (where missed CC steps land), `repeat` (the same setting 5 times on one host plus 3 more hosts; golden chains), `canary` (24 host opens against a reference), `sentinel` (forced drift is detected and corrected). Pass/fail rules were fixed in DECISIONS before the first run. At most 6 hosts (audit 3, repeat 4, canary 4). No pedal.

**Inputs.** Global `--no-ledger`; `audit [--seed] [--per-cat] [--extra] [--no-baked] [--no-diagnose]`, `landing [--cases model:param:step,...]`, `repeat [--seed] [--warmup 0] [--legacy] [--hosts]`, `canary [--hosts]`, `sentinel [--every]`. **Outputs.** `cache/native/verify/<check>.json`; ledger `parity` / `smoke`, dataset `native-verify-v1` (e.g. `repeat_pass`, `repeat_worst_null_db`).

```bash
TONEMATCH_AGENT=native-engine nice -n 10 .venv/bin/python scripts/native_verify.py --no-ledger audit --per-cat 1
TONEMATCH_AGENT=native-engine nice -n 10 .venv/bin/python scripts/native_verify.py repeat --hosts 4
```

**Safety.** Native only; every host is closed on exit. **Machine-readable output.** Not wired.

#### `stomp_verify.py` (result manifest: to add; owned by stomp-engine)

**What it does.** The Stomp side of `native-verify-v1` ([T108](../board/TASKS.md#t108 "HX Stomp verification suite scripts/stomp_verify.py")): 40 drawn (model, settings) items checked in one batched pedal session, then the original preset is restored and judged. `gen` (offline: draws items, compiles and decodes each onto a 04C backup), `run` (the pedal; `--seed`, `--select-scratch`, `--limit N` shakedown, `--checks 1,2,3,4,diag`), `analyse --run DIR` (offline verdict), `ledger --run DIR [--dry-run] [--label] [--only c1,c2,c3,c4,restore]`. Ledger dataset `stomp-verify-v1`.

```bash
TONEMATCH_AGENT=stomp-engine TONEMATCH_STOMPD=cache/stomp/bin/stompd TONEMATCH_STOMPD_COMPILE=cache/stomp/bin/stompd-compile .venv/bin/python scripts/stomp_verify.py run --limit 2
.venv/bin/python scripts/stomp_verify.py analyse --run cache/stomp/hw/<stamp>-verify
```

**Safety.** Pedal rules above; wait for stomp-engine's USB fix ([T168](../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue")) before running `run`. **Machine-readable output.** To add; `verdict.json` in the run directory.

#### `block_probe.py`

**What it does.** Characterises one block with a controlled probe signal (`probe-v1`, 47.25 s), offline. It never renders: an engine renders the probe, and this tool analyses the wet file. `make` writes the probe and its layout; `analyse WET.wav` produces a fingerprint; `compare A.json B.json` diffs two fingerprints (exit 1 if any feature differs beyond tolerance); `validate` runs the probe through known numpy/scipy blocks and prints the accuracy table (63 of 63 checks on 14 blocks in the last suite run; exit 1 on a failure).

**Inputs.** `make [--out DIR] [--no-di] [--di-root DIR]`; `analyse WET [--layout] [--latency N] [--channel mid|left|right] [--subject key=value ...] [--out FP.json] [--di-root]`; `compare A B [--all] [--json]`; `validate [--ledger] [--json OUT]`.

```bash
.venv/bin/python scripts/block_probe.py validate --json cache/results/probe_validate.json
.venv/bin/python scripts/block_probe.py make --out cache/probe
.venv/bin/python scripts/block_probe.py compare cache/probe/fingerprints/block-library-v1.2/a.json cache/probe/fingerprints/block-library-v1.2/b.json
```

**Machine-readable output.** Not wired (`validate --json`, `compare --json` print structured results).

### 5e. Engine timing and speed

`native_pool_bench.py`, `native_render_probe.py` and `native_mem_probe.py` (re-runnable Native diagnostics) are listed under historical spikes. The measured costs live in the ledger and in `docs/BENCHMARKING.md` ("Time per sample"); refresh that table with `engine_timing.py` (section 8).

---

## 6. Listening sessions

### `ab_session.py` (result manifest: to add; `benchmark` is rebuilding the rating session)

**What it does.** Blind listening sessions. It prepares them and records your answers; it never judges the audio. Sub-commands: `build` (make a blind A/B or ABX page from pairs of WAV files: shared excerpt, loudness match by LUFS, a seeded key kept outside the page folder), `score` (join the exported answers with the key: per-pair preference, binomial test, ABX accuracy; ledger `ab`), `gate2-demo` (a small session from `synthetic_v1` runs that saved audio), `rating-build` / `rating-build-v2` (the [T170](../board/TASKS.md#t170 "Human rating session") rating session: target plus two candidates, "which is closer?"), `play` (play T, A, B of one rating trial on the Stomp's USB outputs 1/2, audio only), `rating-score` (score your answers against the existing loss terms). Gate 4 (real pedals) and Gate 5 (real songs) require a session; Gate 2 and 3 are optional.

**Needs.** Offline for everything except `play`, which needs the pedal's audio interface (no control commands). `rating-score` runs the embedding term unless `--no-embed`.

**Inputs.** `build --name NAME --pair LABEL=X.wav,Y.wav [--pair ...] [--seed] [--excerpt-s] [--abx N]`; `score --key FILE --answers FILE|- [--no-ledger]`; `gate2-demo [--runs] [--seed]`; `rating-build-v2 [--name] [--seed] [--withdraw-v1]`; `play SESSION --trial N [--gain-db] [--only TA|B|TAB] [--gap-s]`; `rating-score SESSION [--answers FILE] [--no-ledger] [--no-embed]`.

**Outputs.** `reports/local/ab/<session>/index.html` (open as a file) and `reports/local/ab/<session>.key.json` (do not open it before you finish); rating sessions under `reports/local/ratings/`; ledger `ab` rows from `score`. In every pair, X is the thing under test and Y what it is compared with.

**Examples.**

```bash
.venv/bin/python scripts/ab_session.py build --name mother-ab --pair "drive=cache/matches/Mother-0-68s/listen/match.wav,cache/matches/Mother-0-68s/listen/target_stem.wav" --abx 2
.venv/bin/python scripts/ab_session.py score --key reports/local/ab/mother-ab.key.json --answers ~/Downloads/answers.json --no-ledger
.venv/bin/python scripts/ab_session.py play human-ratings-v2 --trial 3 --only TAB
```

**Safety.** Local only; nothing leaves `reports/local`. The human's answers are the data; no agent scores a session. The current session: `http://127.0.0.1:8000/local/ratings/human-ratings-v2/index.html` ([T170](../board/TASKS.md#t170 "Human rating session")), protocol `docs/notes/human-ratings-v2.md`. **Machine-readable output.** To add; `score` writes the answers join into the ledger and the key stays separate.

---

## 7. Presets and the catalog

### `hlx_validate.py` (wired)

**What it does.** Validates a generated `.hlx` before it goes anywhere near HX Edit or the pedal: device and firmware, every model and parameter against the catalog, snapshots, amp-cab pairing, block count and DSP budget, and a structural diff against the golden exports in `fixtures/golden/`.

**Needs.** `cache/catalog/` from `catalog_import.py` (without it, the catalog checks are skipped), the golden exports for the diff. Seconds.

**Inputs.** Positional `preset`. `--golden DIR` (default `fixtures/golden`), `--firmware 0x3800000` (expected `device_version`), `--cache-dir DIR`, `--keep-snapshot-values` (accept snapshot-controlled parameters that vary), `--no-report`.

**Outputs.** `<preset>.validate.md` next to the preset (unless `--no-report`); the findings on stdout, ending `result: ok (0 fail, N warn)` or `FAIL`. Exit 0 pass or warn, 1 any fail, 2 unreadable input.

**Examples.**

```bash
.venv/bin/python scripts/hlx_validate.py cache/matches/Mother-0-68s/best_snapshot1.hlx
.venv/bin/python scripts/hlx_validate.py cache/matches/Mother-0-68s-drive/best.hlx --firmware 0x3800000 --no-report --result-json cache/results/validate.json
.venv/bin/python scripts/hlx_validate.py fixtures/smoke/known.hlx --golden fixtures/golden
```

**Safety.** Read-only apart from the report. **Machine-readable output.** `--result-json`. `metrics`: `result` (ok/FAIL), `n_fail`, `n_warn`, `device_version`, `blocks`, `findings` (list of strings); `outputs.report_md`; `inputs.preset`. A failing preset gives `status: "error"`, `exit_code: 1`; an unreadable one `exit_code: 2`.

### `setlist_survey.py`

**What it does.** Coverage and statistics over a whole HX Stomp setlist (`.hls`): `coverage` compiles each slot offline (needs `TONEMATCH_STOMPD_COMPILE`) to count which block types and layouts the tooling handles; `stats` prints block and model statistics (`--per-model`). Aggregate counts only on stdout; preset bodies and model lists go to `cache/setlist/`.

**Inputs.** `coverage [--hls FILE] [--base BIN] [--no-compile] [--force-build]`, `stats [--hls FILE] [--per-model]`. **Safety.** The `.hls` is Line 6 content and stays local.

```bash
.venv/bin/python scripts/setlist_survey.py stats --hls fixtures/golden/HXStompSetlist.hls
.venv/bin/python scripts/setlist_survey.py coverage --hls fixtures/golden/HXStompSetlist.hls --no-compile
```

**Machine-readable output.** Not wired.

### `chain_patterns.py`

**What it does.** Learns category-order patterns (which block types follow which) from real presets, offline. Default sources: your setlist and `fixtures/golden/`. Corpus mode (`--corpus DIR`, [T104](../board/TASKS.md#t104 "Song and artist context as a prior")) scans a curated folder tree (artist / style / effect), drops duplicates, folds in folder metadata and writes a report.

**Inputs.** `--hls FILE`, `--hlx-dir PATH`, `--no-default`, `--out FILE` (default `data/chain_prior/category_prior.json`), `--corpus DIR`, `--quick`, `--no-catalog`. **Outputs.** `data/chain_prior/category_prior.json` (committed; category level only), `cache/chain-patterns/model_frequencies.json` (local), and in corpus mode `data/chain_prior/corpus_patterns.json` and `reports/local/preset-corpus/`.

```bash
.venv/bin/python scripts/chain_patterns.py --no-default --hlx-dir fixtures/golden --out cache/chain-patterns/test_prior.json
.venv/bin/python scripts/chain_patterns.py --corpus data/presets --quick
```

**Safety.** Line 6 content stays local; only category-level aggregates are committed. **Machine-readable output.** Not wired.

---

## 8. Reports and the local site

### `gate2_report.py` (`build` wired)

**What it does.** A plain-language local HTML report on the Gate 2 methods (attempts 1 to 3): what each method did on the synthetic sets, against the middle-of-every-knob and random baselines, with loudness-matched audio players where runs saved audio. `render-examples` re-renders a few targets and guesses with Native for the players (refuses while another job holds Native).

**Inputs.** `build [--root DIR] [--out DIR] [--no-audio]` (default command). `render-examples --run TAG [--attempt v1|v2|v3] [--items i001,i002] [--methods ours,middle]`.

**Outputs.** `reports/local/gate2/index.html` plus copied audio. **Examples.**

```bash
.venv/bin/python scripts/gate2_report.py build --no-audio
.venv/bin/python scripts/gate2_report.py build --out reports/local/gate2 --result-json cache/results/gate2_report.json
.venv/bin/python scripts/gate2_report.py render-examples --attempt v3 --run gate2_v3_attempt3 --items i039,i095
```

**Machine-readable output.** `--result-json` on `build`. `metrics`: `audio_files`, `audio`; `outputs`: `index_html`, `out_dir`; `inputs.ledger`.

### `status_report.py`

**What it does.** Renders the project status report from `docs/features.json`, `docs/status-curated.json`, `board/GATES.md`, `board/TASKS.md`, the ledger and git; with `--run-checks`, also pytest, ruff and `licence_check`. Standard library only. Exits 2 when the registry or the curated file is invalid or contradicts the gate checklists.

**Inputs.** `--root`, `--registry`, `--curated`, `--out FILE` (default `reports/status/index.html`), `--md-out FILE` (`reports/status/README.md`), `--format html,md`, `--run-checks`, `--no-snapshot` (skip `reports/status/history/<date>.json`), `--json FILE` (also write the report model).

```bash
.venv/bin/python scripts/status_report.py
.venv/bin/python scripts/status_report.py --format md --no-snapshot --json cache/results/status_model.json
```

**Machine-readable output.** Not wired; `--json FILE` writes the whole report model (the structured form of the report).

### `local_site.py`

**What it does.** Builds a local, offline HTML site from the project's Markdown (board, docs, contracts, the root README, status report, local analysis READMEs): rewrites links so they work from disk, writes a landing page `reports/local/index.html` and a link report `reports/local/link_check.txt`. Writes only under `reports/local/` (gitignored). Uses the `markdown` package if installed, else a built-in converter.

```bash
.venv/bin/python scripts/local_site.py build
```

**Inputs.** `build [--root DIR]`. **Machine-readable output.** Not wired; read `link_check.txt`.

### `block_coverage_report.py`

**What it does.** Stomp and Helix Native side by side, per GUI category and per tested model: catalog, presets that were rendered or written, parity results, search results, ledger evidence links. Everything is derived from what is on disk. Names every Line 6 model, so the whole output is local.

**Inputs.** `--root`, `--out DIR` (must stay under `reports/local`; default `reports/local/blocks`), `--format html,md`, `--json FILE` (a small JSON summary under `reports/local`). **Outputs.** `reports/local/blocks/index.html` and `README.md`.

```bash
.venv/bin/python scripts/block_coverage_report.py --format md --json reports/local/blocks/summary.json
```

**Machine-readable output.** `--json FILE` (a small summary); not wired for `--result-json`.

### `engine_timing.py`

**What it does.** Reads the ledger and shows how long each activity takes on each engine (measured per sample, estimated totals, "not measured" where there is no record). The status report uses it; run it directly to refresh the "Time per sample" table in `docs/BENCHMARKING.md`.

**Inputs.** `--render-doc` (rewrite the table between its markers), `--root`. Without `--render-doc` it only reads.

```bash
.venv/bin/python scripts/engine_timing.py --render-doc
```

**Machine-readable output.** Not wired.

---

## 9. Project bookkeeping

### `ledger_append.py`

**What it does.** Appends one row to `ledger/runs.csv` (`contracts/ledger-format.md`). It fills `timestamp`, `commit` (short git sha, `-dirty` suffix, or `nogit`) and `agent` (from `TONEMATCH_AGENT`, default `human`). Rows are never edited or deleted; a correction is a new row.

**Inputs.** `--kind` (one of `ab`, `benchmark`, `engine-compare`, `gate`, `loss-trust`, `parity`, `render-cost`, `search`, `separate`, `smoke`, `stress`), `--engine` (default `-`), `--dataset`, `--metric`, `--value` (number) and optional `--renders`, `--wall-clock-s`, `--notes`. A `gate` row is written only by `benchmark`, after the human's sign-off where the gate requires one.

```bash
TONEMATCH_AGENT=platform .venv/bin/python scripts/ledger_append.py --kind smoke --engine - --dataset cli-tools --metric tools_documented --value 58 --notes "T172"
```

**Outputs.** One CSV row, also echoed to stdout. **Machine-readable output.** The row itself; the wired tools echo the rows they append in `ledger_rows`.

### `link_tasks.py`

**What it does.** Rewrites bare task ids (`T042`), ledger timestamps and DECISIONS citations in hand-written Markdown into links with hover text. Idempotent; skips code blocks and existing links. Default files: `docs/notes/*.md`, `docs/TONE-COMPARISON.md`, `board/CYCLES.md`. Run it after you edit docs.

**Inputs.** `files ...`, `--root DIR`, `--check` (report without writing).

```bash
.venv/bin/python scripts/link_tasks.py
.venv/bin/python scripts/link_tasks.py --check docs/TOOLS.md
```

**Machine-readable output.** Not wired.

### `board_anchors.py`

**What it does.** Gives every task line in `board/TASKS.md` and `board/CYCLES.md` a GitHub anchor (`<a id="t042"></a>`) so reports can link to it.

**Inputs.** `--root DIR`, `--check` (exit 1 if any task line lacks its anchor).

```bash
.venv/bin/python scripts/board_anchors.py --check
```

**Machine-readable output.** Not wired.

---

## 10. Library preparation

One-off preparation of the lookup tables and renders the specialists use. Local output only (`cache/`, `data/`): the renders are of Line 6 models. All Native tools respect the host scaler and refuse when the machine already runs 6 hosts. Re-run after the Native version or the block library changes.

| Tool | What it prepares | Command shape | Writes |
|---|---|---|---|
| `native_block_library.py` | The Native single-block library ([T048](../board/TASKS.md#t048 "[breadth first, category order")): every Stomp-compatible model dropped alone with its params and bypass on MIDI CC, every CC checked, replayed in a fresh host. Needs a logged-in GUI session. | `capture [--sessions N] [--max-hosts N] [--limit N] [--tiers "drive,delay,reverb;comp,eq,mod"] [--force] [--models a,b] [--narrowed]`; `smoke [--n N] [--seed] [--max-hosts]` | `cache/native/library/block-library-v1.x/` |
| `native_block_fingerprints.py` | Block-probe fingerprints of every library template at a coarse grid of settings (starting points for search). | `run [--hosts 3] [--max-hosts] [--limit N] [--models a,b] [--batch N] [--analysis-workers N] [--force]` | `cache/probe/fingerprints/<library>/` |
| `linear_tables.py` | Response tables for the linear specialist (per cab and EQ model, a multisine probe at a grid of knob settings). One Native host. | `[--models M ...] [--pool-of SET ...] [--only-pools] [--redo] [--limit N] [--lti-only]` | `cache/linear-tables/<library version>/` |
| `seed_calibrate.py` | Measured-seed curves for delay, reverb and modulation (echo time, feedback, decay, pre-delay, LFO rate and depth). One Native host. | `[--models M ...] [--out FILE] [--set SET] [--merge]` | `cache/measured-seed/calibration.json` |
| `time_specialist_fit.py` | Renders for the time-based specialist and its per-model regressors and classifier. | `render [--set SET] [--models ...] [--all delay reverb] [--mods] [--all-reverbs] [--draws N] [--max-machine-hosts N]`; `fit-seeds`; `fit-classifier` | `cache/time-specialist/`, `data/time_specialist/classifier.json` |
| `drive_calibrate.py` | Calibration table for the drive path: renders, then the table. | `render [--hosts] [--chunk] [--models ...]`; `build [--jobs] [--out]` | `cache/drive-specialist/` |
| `mod_calibrate.py` | The same for modulation models. | `render [--hosts] [--chunk] [--models ...]`; `build [--jobs] [--out]` | `cache/mod-specialist/` |
| `ir_library_analyze.py` | Offline analysis of your impulse-response library against the Helix cab models (inventory, analyze, helix, cluster, estimate, summary; no Native, no pedal, no IR audio copied). | `{all,inventory,analyze,helix,cluster,estimate,summary} [--n-irs N]` | `reports/local/ir-library/work/` |

```bash
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python nice -n 10 .venv/bin/python scripts/native_block_library.py smoke --n 20
nice -n 10 .venv/bin/python scripts/native_block_fingerprints.py run --hosts 3 --limit 5
.venv/bin/python scripts/ir_library_analyze.py inventory
```

**Safety.** `ir_library_analyze.py` reads `data/ir/` (your files, gitignored); only smoothed 55-point magnitude responses are written. **Machine-readable output.** Not wired for any of them.

---

## 11. Distortion research

### `distortion_id_steps01.py` (result manifest: to add; owned by tone-metrics)

**What it does.** [T171](../board/TASKS.md#t171 "Distortion identification steps 0 and 1"), steps 0 and 1 of `docs/notes/distortion-identification.md`: mines the stored coarse-v1 drive fingerprints for model separability (leave-one-setting-out identification, clustering against the circuit groups, where the [T164](../board/TASKS.md#t164 "Play-along single-block match") top matches sit), and validates the distortion-id estimators on synthetic systems with known truth. Zero renders. Nothing is adopted; the placeholder checks are reported as measured.

**Inputs.** `--step 0|1|all`, `--out DIR`. **Outputs.** `reports/local/distortion-id/results_step{0,1}.json` and SVG figures.

```bash
nice -n 10 .venv/bin/python scripts/distortion_id_steps01.py --step all
```

**Machine-readable output.** To add (read `results_step*.json`). Later `distortion_id_*` tools get a section here when they exist.

### `distortion_pilot_step2.py` (result manifest: to add; owned by tone-metrics)

**What it does.** [T173](../board/TASKS.md#t173 "Distortion measurement pilot, step 2"), step 2 of `docs/notes/distortion-identification.md`: renders the dfp-v1 pilot probe (sweeps at 13 levels, 1 kHz and 220 Hz tone staircases from -71 dBFS, two-tone, bursts) through 6 Helix drive models on Native (single-block library templates, at most `--hosts` hosts through the host scaler), stores one small fingerprint per setting and no audio, and reports the pilot's pass/fail checks as measured. Nothing is adopted. `plan` lists the renders without starting Native; `recheck` re-renders the 12 ringing-subset settings (leakage measure, output noise, cross-session repeat); `summarise` rebuilds the summary and figures from the stored fingerprints.

**Inputs.** `{plan,run,recheck,summarise}`, `--hosts N` (default 3), `--out DIR`. **Outputs.** `cache/distortion-fp/dfp-v1-pilot/<model>/<setting>.npz` (about 6.6 KB each) and `index.json`; `reports/local/distortion-id/pilot/summary.json`, `recheck.json` and three SVG figures per model.

```bash
.venv/bin/python scripts/distortion_pilot_step2.py plan
nice -n 10 .venv/bin/python scripts/distortion_pilot_step2.py run --hosts 3
.venv/bin/python scripts/distortion_pilot_step2.py summarise
```

**Safety.** Native only, no pedal, no song audio (the probe is synthetic). Run `scripts/proc_check.py` before and after. **Machine-readable output.** To add (read `summary.json`).

### `distortion_step3.py` (result manifest: to add; owned by tone-metrics)

**What it does.** [T174](../board/TASKS.md#t174 "Distortion measurement step 3"), step 3 of `docs/notes/distortion-identification.md`: fingerprints every drive model of `match_block.category_pool('drive')` (60) at 12 settings each with the dfp-v1 probe (6 s sweeps, tone staircases from -91 dBFS), on Native single-block templates, at most `--hosts` hosts (capped at 5) through the host scaler and at most `--workers` analysis processes (capped at 4). Stores fingerprints only, no audio. `tonecheck` checks that Scream 808's Tone reaches Native (CC route against a controller-free drop, and the Tone 0 vs 1 spectra); `plan` lists the settings and which knob is "drive" and "tone" per model without starting Native; `analyse` runs leave-one-setting-out identification, clusters against the circuit labels and builds the fingerprint cards and figures; `ledger` appends the `benchmark / native / distortion-fp-v1` rows. Nothing is adopted. `run` resumes: settings already in `index.json` are skipped.

**Inputs.** `{tonecheck,plan,run,analyse,ledger}`, `--hosts N` (default 5), `--workers N` (default 4), `--batch N` (default 24), `--limit N` (shakedown), `--max-load X` (busy cores; default 0.95 x cores: pause rendering above it, resume below 0.85 x cores; T193), `--out DIR`. **Outputs.** `cache/distortion-fp/dfp-v1/<model>/<setting>.npz` (about 6.7 KB each) and `index.json`; `reports/local/distortion-id/step3/summary.json`, `cards.md`, `tonecheck.json`, summary SVGs and two SVGs per model under `models/`.

```bash
.venv/bin/python scripts/distortion_step3.py plan
nice -n 10 .venv/bin/python scripts/distortion_step3.py tonecheck
nice -n 10 .venv/bin/python scripts/distortion_step3.py run --hosts 5 --workers 4
.venv/bin/python scripts/distortion_step3.py analyse
TONEMATCH_AGENT=tone-metrics .venv/bin/python scripts/distortion_step3.py ledger
```

### `drive_tone_families.py` (result manifest: to add; owned by tone-metrics)

**What it does.** [T179](../board/TASKS.md#t179 "Tone-knob ends") and [T180](../board/TASKS.md#t180 "Circuit families from the measured fingerprints"), measurement only. `run` renders tone 0 / 0.1 / 0.9 / 1 at Drive 0.5 and 0.85 for every two-knob drive (52 models, 416 settings, plus 2 step-3 settings again as controls) with the dfp-v1 probe, at most 4 hosts through the host scaler (capped at 4) and at most 3 analysis processes; it never starts a batch while the 1-minute load is above `--max-load` (default 9). `tone` joins them with the step-3 tone 0.2 / 0.5 / 0.8 points and measures how much of each tone knob's effect sits in [0, 0.1], [0.1, 0.9] and [0.9, 1], with a shape label; it writes the map search reads. `families` clusters the 60 models from the step-3 fingerprints (level-free card and curves, consensus of Ward partitions over resamples) and writes the model-to-family map. `ledger` appends the `benchmark / native / distortion-fp-v1-tone-ends` and `benchmark / - / drive-families-v1` rows. Nothing is adopted. `run` resumes.

**Inputs.** `{plan,run,tone,families,ledger}`, `--hosts N` (default 4), `--workers N` (default 2), `--batch N` (default 8), `--limit N`, `--max-load X` (busy cores, default 0.95 x cores), `--k N` (force the family count), `--w-card X` (card weight, default 0.5), `--boot N` (resamples, default 200). **Outputs.** `cache/distortion-fp/dfp-v1-tone-ends/`; `reports/local/distortion-id/tone-ends/` (`summary.json`, `knob_sensitivity.json`, SVGs); `cache/drive-maps/knob_sensitivity.json` (copy, model ids and numbers only); `reports/local/distortion-id/families/` (`summary.json`, SVGs); `cache/drive-maps/drive-families-v1.json`.

```bash
.venv/bin/python scripts/drive_tone_families.py plan
nice -n 10 .venv/bin/python scripts/drive_tone_families.py run --hosts 4 --workers 3
.venv/bin/python scripts/drive_tone_families.py tone
.venv/bin/python scripts/drive_tone_families.py families
TONEMATCH_AGENT=tone-metrics .venv/bin/python scripts/drive_tone_families.py ledger
```

**Safety.** Native only, no pedal, no song audio (the probe is synthetic; `tonecheck` uses the repo's smoke DI clip, and its drop files are deleted after the drop). Run `scripts/proc_check.py` before and after. **Machine-readable output.** To add (read `summary.json`).

### `distortion_step4.py` (result manifest: to add; owned by tone-metrics)

**What it does.** [T181](../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes"), step 4 of `docs/notes/distortion-identification.md`, measurement only. It tries to name a drive model and its Drive and Tone from real guitar DI at random settings that were never measured, using the dfp-v1 fingerprints.
- `plan` picks 8 DI excerpts of 6 s: 4 Guitar-TECHS (P1 and P2) and 4 of the human's own dry takes, none used in a rating session. It also picks 120 targets: 54 models, with Drive and Tone at random CC steps off every measured grid.
- `render` renders on Native, through at most 3 hosts leased from the host scaler, and pauses between batches above `--max-load` busy cores (default 0.95 x cores; resume below 0.85 x cores):
  - `targets`: 120 renders, kept in a small cache that `clean` deletes;
  - `truth`: the dfp-v1 probe at each target's own setting, fingerprint only, a diagnostic;
  - `gallery`: 54 models × 12 step-3 settings × 8 excerpts. Features are taken on the fly (AFx-Rep embedding on the CPU, output-only features, `mrstft_v2` against the targets that use the render), and no audio is kept.
- `analyse`:
  - condition A (DI known): a three-stage twin fitted to each (DI, output) pair, read by the step-3 analyser and matched to every model's knob map, calibrated on the dev split;
  - condition B (output only): output-only features against the gallery on the other excerpts, alone and after the AFx-Rep shortlist;
  - the baselines (chance, `mrstft_v2` nearest render, AFx-Rep alone).
- `ledger` appends the `benchmark / native / distortion-step4-dryknown` and `distortion-step4-blind` rows.
- Nothing is adopted. `render` resumes. `analyse` writes `cache/distortion-step4/calibration.npz`, which `tonematch.metrics.distortion_step4.estimate_knobs` (the T178 hook) reads.

**Inputs.**
- `{plan,render,analyse,ledger,clean}`;
- `--what targets,truth,gallery`;
- `--hosts N` (default 3, capped at 3), `--workers N` (default 3, capped at 4), `--batch N` (default 24), `--max-load X` (busy cores, default 0.95 x cores), `--out DIR`.
- Set `TONEMATCH_AFXREP_DEVICE=cpu`: the GPU failed under three worker processes.

**Outputs.**
- `cache/distortion-step4/`: `plan.json`, `targets/` (deleted by `clean`), `truth/`, `gallery/`, `twins/`, `target_features.json`, `calibration.npz`, `runs.json`;
- `reports/local/distortion-id/step4/summary.json` and `rows.json`.

```bash
nice -n 10 .venv/bin/python scripts/distortion_step4.py plan
TONEMATCH_AFXREP_DEVICE=cpu nice -n 10 .venv/bin/python scripts/distortion_step4.py render --what targets,truth,gallery --hosts 3 --workers 3
TONEMATCH_AFXREP_DEVICE=cpu nice -n 10 .venv/bin/python scripts/distortion_step4.py analyse --workers 3
TONEMATCH_AGENT=tone-metrics .venv/bin/python scripts/distortion_step4.py ledger
.venv/bin/python scripts/distortion_step4.py clean
```

**Safety.**
- Native only, no pedal.
- The human's takes are read locally, and only fingerprints and features are kept. The target clips are deleted by `clean`.
- No song audio is used.
- Run `scripts/proc_check.py` before and after.

**Machine-readable output.** To add (read `summary.json`).

---

### `pedal_probe_sim.py` (result manifest: to add; owned by stomp-engine)

**What it does.** [T187](../board/TASKS.md#t187 "Measured-pedal mode preparation"), measurement only. A Native simulation of measured-pedal mode's real-world nuisances on drive identification, with 54 step-4 targets (one per model, hidden off-grid Drive/Tone) standing in for real pedals; 810 new probe renders.
- `plan` writes the conditions: input side (rendered: probe level offsets -12 to +12 dB, a pickup-like load of 10 k to 1 M ohm, interface tilt +-2 dB/decade), output side (on the base render: noise -90/-80/-70 dBFS, tilt with and without the simulated loopback correction, 2 to 1207 samples of latency, return gain with clipping) and three combinations.
- `run` renders 15 probe variants per target through at most 2 Native hosts and fingerprints every condition in up to 4 processes. It pauses above `--max-load` (busy cores, default 0.95 x cores) and while the disk has less than `--min-free-gb` free (it then closes its hosts). Resumes.
- `analyse` matches every fingerprint against the library with the fixes (none, input-level search, noise mask, noise floor) and runs the leave-one-model-out test (a pedal not in the library). `ledger` appends `benchmark / native / pedal-probe-sim-v1`.

**Outputs.** `cache/pedal-probe-sim/` (plan, `fp/<target>/<condition>.npz`, noise takes, runs) and `reports/local/pedal-probe-sim/` (`summary.json`, `rows.json`, `loo_rows.json`, `table.md`). No audio kept.

```bash
.venv/bin/python scripts/pedal_probe_sim.py plan
TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python nice -n 10 .venv/bin/python scripts/pedal_probe_sim.py run --hosts 2 --workers 4 --min-free-gb 8
nice -n 10 .venv/bin/python scripts/pedal_probe_sim.py analyse --workers 4
TONEMATCH_AGENT=stomp-engine .venv/bin/python scripts/pedal_probe_sim.py ledger
```

**Safety.** Native only, no pedal, no song audio. Run `scripts/proc_check.py` before and after.

---

### `nodi_drive_e2e.py` (result manifest: to add; owned by search)

**What it does.** [T183](../board/TASKS.md#t183 "End-to-end check of no-DI drive matching"), measurement only. It runs the no-DI drive matcher (`match_block.py --shortlist afxrep`) end to end on 24 held-out random single-drive targets rendered from real guitar DI excerpts, with the matcher rendering through a different DI (another player or another take).
- `plan` picks 8 DI excerpts of 5 s (Guitar-TECHS P1 and P2, the human's dry takes), 24 models out of the 54 non-flagged, random off-grid Drive and Tone, and a 6-target subset for the old full search. Its seed (183) is separate from T181's, and it avoids T181's excerpts and settings. It also writes the explicit tolerance map (`suggested_tol` from `cache/drive-maps/knob_sensitivity.json`).
- `render` renders the targets on one Native host and the DI excerpts as wavs (pauses above `--max-load` busy cores).
- `match --path coarse_grid|inversion|inversion_samedi|full` runs `match_block.py` once per target, `--procs` at a time. `inversion_samedi` hands the matcher the target's own DI (the upper bound for the fingerprint inversion). `full` is the old search at a wall cap (`--wall-min`) on the 6-target subset.
- `analyse` writes `reports/local/t183/rows.json` and `summary.json`; `ledger` appends the `search / native / nodi-drive-e2e-v1` rows; `clean` deletes the target audio.
- The families, the second-choice family and the tolerances are TEST SETTINGS here, not adopted defaults.

**Inputs.** `{plan,render,match,analyse,ledger,clean}`; `--path`, `--procs` (default 2), `--subset`, `--only u00,u01`, `--wall-min` (default 6), `--max-load` (busy cores, default 0.95 x cores).

**Outputs.** `cache/matches/t183/` (plan, per-target `result.json`, logs; the audio folder is deleted by `clean`) and `reports/local/t183/`.

```bash
.venv/bin/python scripts/nodi_drive_e2e.py plan
nice -n 10 .venv/bin/python scripts/nodi_drive_e2e.py render
nice -n 10 .venv/bin/python scripts/nodi_drive_e2e.py match --path coarse_grid --procs 2
.venv/bin/python scripts/nodi_drive_e2e.py analyse
.venv/bin/python scripts/nodi_drive_e2e.py clean
```

### `match_block.py --final-rank` and `nodi_drive_e2e_v2.py` (T185, measurement only)

**`--final-rank provisional|afxrep`** (match_block.py, with `--shortlist afxrep`). Default `provisional`: the final models are ordered by the unadmitted re-score, as before. `afxrep` orders them by AFx-Rep distance (the T183 proposal); the re-score is still computed and kept in each row of `result.json` (`loss`, `provisional_rank`). Not a default; the human decides. Tests in `tests/test_match_block.py`. The default families file is now `cache/drive-maps/drive-families-v1.json` (the old `data/` path was stale after the lead's move).

**`nodi_drive_e2e_v2.py`.** 96 held-out random drive targets (seed 185; T181 and T183 settings and excerpts excluded) from 15 real DI excerpts (5 each: Guitar-TECHS P1, P2, the human's takes). Three conditions: `blind` (the matcher's DI is another player), `same_player` (same player, other excerpt), `same_di` (the target's own DI, upper bound; the fingerprint inversion can run). Four arms: final order (provisional | AFx-Rep) x shortlist (top 8 + families + second choice | plain top 8). One job per (target, condition) renders the shared coarse grid once and scores the whole expanded shortlist once; the arms are read off afterwards.

- `plan`, `render` (target audio on one host), `run --shard i/n` (one process, one Native host; pauses above `--max-load` busy cores (default 0.95 x cores); `--stop-file`), `analyse` (tables, Wilson CIs, McNemar; writes `reports/local/t185/{rows,summary,predictions}.json`), `ledger` (`search / native / nodi-drive-e2e-v2`), `clean` (deletes the target audio).
- Set `TONEMATCH_AFXREP_DEVICE=cpu` (the script sets it by default).

```bash
.venv/bin/python scripts/nodi_drive_e2e_v2.py plan && .venv/bin/python scripts/nodi_drive_e2e_v2.py render
for s in 0 1 2; do .venv/bin/python scripts/nodi_drive_e2e_v2.py run --shard $s/3 & done
.venv/bin/python scripts/nodi_drive_e2e_v2.py analyse && .venv/bin/python scripts/nodi_drive_e2e_v2.py clean
```

**Safety.** Native only, no pedal, no song audio. At most 2 Native hosts. The target clips are deleted by `clean`. Run `scripts/proc_check.py` before and after.

### `match_block.py --shortlist predictor` and `predictor_first_t196.py` (T196, search; measurement only, NOT the default)

**`--shortlist predictor`** (match_block.py). The predictor-first drive matcher, an option next to `--shortlist afxrep`; the default stays `none`. It embeds the target once (three 3.33 s AFx-Rep crops, as the head was trained), runs the T191 head (`cache/models/predictor-dataset-v1/heads_main_v2data_mlp_multi.pkl`, MLP, multi-crop; `--predictor-heads PATH`), keeps the models that cover `--predictor-cum` (0.95) of the calibrated probability, at most `--predictor-max-models` (6), and renders about 5 candidates per model: the predicted Drive/Tone mean and +-1 sigma on Drive and on Tone (a Drive-only model gets the mean and +-1, +-2 sigma). The final order is the AFx-Rep distance of each model's best candidate (it forces `--final-rank afxrep`). The report and `result.json` also carry the predictor's own pick and knobs (no renders), its probabilities and every candidate's distance. No shard search runs. Expect 6 to 30 renders and 2 to 15 s per match, against about 370 and 150 s for `--shortlist afxrep`. Drive estimates are level-confounded (docs/notes/drive-anomaly-t194.md): treat them as provisional. The stage is `src/tonematch/search/predictor_first.py`; tests in `tests/test_predictor_first.py` (fake head, fake engine).

```bash
.venv/bin/python scripts/match_block.py --target-wav T.wav --dry-wav DI.wav --shortlist predictor --out cache/matches/pf --lean
```

**`predictor_first_t196.py`.** The measurement: 72 targets of 10 s (seed 196; T181/T183/T185 settings excluded): 24 from Guitar-TECHS P1 and 24 from P2 (only files the head never trained on) and 24 from the human's own takes; two matcher-DI conditions each (blind, same_player); both arms run in one job on one host. `plan`, `render`, `run --shard i/n` (busy-core guard; `--stop-file`), `analyse` (tables, Wilson intervals, McNemar, Drive error by DI level, `reports/local/t196/`), `ledger`, `clean` (deletes the target and DI audio). Results: docs/notes/predictor-first-t196.md.

### `drive_dataset_render.py` and `overnight_drive.sh` (T191, learning; measurement only)

Unattended single-drive dataset render on Helix Native plus a retrain of the T189 predictor heads. Every searchable drive model (minus the six flagged fuzzes) x random knob settings (CC grid, level knobs at default) x random 10 s excerpts with real playing from the Guitar-TECHS P1/P2 DI files; P3 is never read. Split by whole DI file: `test` (about 20 % of the files, never used by T186/T189), `v1test` (the T186/T189 test files, evaluation only), `cal`, `train`. Features only (frozen AFx-Rep `mid`: 6 s centre crop + three 3.33 s crops, float16); the audio is dropped.

- `plan` (seeded; `--target` cap, default 60,000; `--hours`, `--hosts`, `--sec-per-clip` size the plan; `--limit` for a shakedown) writes `cache/drive-dataset-v1/plan.json`.
- `render` is an elastic job: a supervisor asks the host scaler for at most 4 hosts (`--hosts`); each worker claims a 100-clip shard, pauses while the busy cores (CPU utilisation x cores, smoothed) are above `--max-load` (default 0.95 x cores, 9.5 on 10; resumes below 0.85 x cores; the 1-minute load above 1.2 x cores is a backstop; T192), stops cleanly when free disk is below `--min-free-gb` (8.5) or `--hours` elapse. Shards in `shards/` (`<id>.single.npy`, `<id>.crops.npy`, `<id>.labels.jsonl`, then `<id>.json` last); a rerun resumes. A failed or silent render is retried once, then skipped and logged in the shard JSON. SIGTERM: the current shard finishes and the hosts close.
- `train` retrains logistic + MLP heads (multi-crop) on T186 train (`v1data`) and on T186 train + new train (`v2data`); evaluates on the T186/T189 test split, the new held-out files and new renders on the v1 test files, within player and across players. Writes `summary.json`, `summary.md`, checkpoints in `cache/models/predictor-dataset-v1/`, and `benchmark / - / predictor-dataset-v1` ledger rows (`--no-ledger` to skip; a `--limit` run never writes them).
- `all`: plan if needed, render, train, then `proc_check.py --kill`. `scripts/overnight_drive.sh` starts `all` with nohup, nice 10 and `caffeinate -i`, logging to `cache/drive-dataset-v1/run.log`, and prints the PID.

```bash
scripts/overnight_drive.sh
cat cache/drive-dataset-v1/progress.json
.venv/bin/python scripts/drive_dataset_render.py all --out /tmp/dd-shake --limit 40 --hours 0.1 --old-limit 1500 --epochs 5 --seeds 1
.venv/bin/python scripts/drive_dataset_render.py train --no-ledger
```

**Safety.** Native only, no pedal, no song audio, P3 never read. At most 4 hosts, load guard 0.95 x cores busy, disk floor 8.5 GB. Stop with `kill <PID>` (never a pattern kill); run `scripts/proc_check.py` afterwards. Docs: `docs/notes/drive-dataset-v1.md`.

## Internal helpers

Not meant to be run on their own for a result. Libraries with a thin or no command line, comparison helpers for one dev run, or bookkeeping. Several have no `--help` (`parity_snap.py`, `rating_session.py`, `task_links.py`, `evidence_links.py`, `corpus_report.py`, `devmod2_pair.py`, `devmod2_make.py`), and `devmod2_make.py` runs its work at import: do not call these to see what they do.

| Script | What it is |
|---|---|
| `distortion_step4_analysis.py` | Library for `distortion_step4.py analyse` / `ledger` (T181): twin fitting in worker processes, the dev calibration, baselines, condition B, the summary and the ledger rows. |
| `parity_snap.py` | Library: writes the CC-snapped knob values into a Stomp `.hlx` so the 128-step CC grid is never scored as an engine difference (used by `parity_spot`, `parity_timefx`, `parity_timefx_stimuli`). |
| `task_links.py` | Library behind `link_tasks.py`: turns task ids into links to their board anchors. |
| `evidence_links.py` | Library: turns ledger-row and DECISIONS citations into links with hover text, in Markdown and HTML (status report, local site). |
| `rating_session.py` | Library for the withdrawn rating session v1; used through `ab_session.py rating-build`. |
| `rating_v2.py` | Library for human-ratings-v2 (hidden Helix settings over your own take); used through `ab_session.py rating-build-v2`. |
| `rating_v3.py` | Library for human-ratings-v3 (hard pairs where the scores disagree, over your own take); used through `ab_session.py rating-build-v3`. |
| `rating_v4.py` | Library for human-ratings-v4 (near-twin models vs large knob moves, over your own take); used through `ab_session.py rating-build-v4`. |
| `rating_v5.py` | Library for human-ratings-v5 (T185 blind predictions re-rendered on the target's own DI: AFx-ordered pick vs provisional-ordered pick); used through `ab_session.py rating-build-v5`. |
| `synthetic_benchmark.py` | Manifest, label generator, metrics and cost estimate behind the `synthetic-benchmark` skill; the runnable harness is `synthetic_v1.py`. |
| `corpus_report.py` | Library for `chain_patterns.py --corpus`: corpus statistics and the README under `reports/local/preset-corpus/`. |
| `drive_seed_eval.py` | Offline check of the drive calibration table on a dev set's saved targets (no renders). |
| `gm_compare.py` | Compares run directories on `synthetic-devgm` (counts by family and half). |
| `gm_replay.py` | Replays the final order of a `synthetic-devgm` run under other policy settings (no renders). |
| `hybrid_compare.py` | Compares `synthetic-v1` dev16 run directories (model, category, parameter MAE, held-out loss, rank-1). Dev numbers, not gate evidence. |
| `devmod2_make.py` | Builds `synthetic-devmod2` ([T146](../board/TASKS.md#t146 "Extended rule B (human 2026-10-18, follow-up to T144)")). Runs at import: not a tool to call blindly. |
| `devmod2_pair.py` | Paired summary of the [T147](../board/TASKS.md#t147 "Fresh paired check of the mod family prior only") mod-prior check on `synthetic-devmod2`. |
| `time_specialist_compare.py` | Compares two `synthetic-devfam` runs (general hybrid against the time specialist) on delay and reverb items. |
| `linear_eval.py` | Linear-specialist evaluation ([T117](../board/TASKS.md#t117 "Linear specialist for cab and EQ"), trust bar `linear-v1`): renders on Native with one host per item of a synthetic set, then scores; tied to its dev set. |

## Historical spikes

One-off experiments and live checks, kept for the record. Their findings are in `docs/DECISIONS.md` and `docs/notes/`. Most of the Native probes can be re-run as diagnostics (all have `--help` except `amp_refine_study.py`, which runs at import), but they target specific tasks, drop files or firmware and are not part of any workflow.

| Script | What it was |
|---|---|
| `drive_anomaly_t194.py` | T194: retrain-only measurement that traced the drive-dataset Drive regression to DI input level (docs/notes/drive-anomaly-t194.md). |
| `ds_sample_reference_chain.py` | T197: loads the DS-Sample tutorial reference chain onto the Stomp edit buffer, reamps a take through it (`capture`), and restores 04C (`restore`). Capture re-writes the stored chain, so it overwrites hand edits on the pedal. |
| `ds_sample_tune_t198.py` | T198: Native template capture of the reference chain, three-arm tuning (mrstft_v2 above 250 Hz, full range, AFx-Rep), Stomp confirmation, rating session v6. |
| `ds_sample_tune_t199.py` | T199: refine version c with the cab High Cut freed; Stomp confirmation; rating session v7. |
| `encoder_swap_t195.py` | T195: PANNs and DAC against AFx-Rep as the drive-predictor encoder on a drive-dataset-v1 subset (render, embed, train). |
| `predictor_pilot_v0.py` | T186 drive-only predictor pilot: a small log-mel CNN trained on drive renders on disk, held-out DI files; measurement only (docs/notes/predictor-pilot-v0.md). |
| `predictor_pilot_v0_afx.py` | T186 paired baseline: frozen AFx-Rep embeddings plus a logistic-regression head on the same split. |
| `predictor_pilot_v0_paired.py` | T186 paired comparison of the CNN and the AFx-Rep head per test file, with bootstrap intervals. |
| `predictor_pilot_v1_embed.py` | T189: frozen AFx-Rep embeddings for predictor-pilot-v1 (6 s centre crop, reused from T186, plus three 3.33 s crops per 10 s clip); CPU, 2 threads, resumable; about 54 min for 14,631 clips. |
| `predictor_pilot_v1.py` | T189 drive-only predictor pilot v1: small heads (hierarchical logistic regression + ridge, or an MLP) on frozen AFx-Rep, single vs multi-crop, multi-file calibration, within- and cross-player arms; measurement only (docs/notes/predictor-pilot-v1.md). |
| `native_spike.py` | T002/T013: can a whole preset be written into Helix Native's saved state from code. |
| `native_drop_load.py` | [T018n](../board/TASKS.md#t018n "Hands-off Native loading") prototype: hands-off chain capture by in-process file drop (macOS). |
| `native_capture_bench.py` | Times Native chain captures step by step (one host per file against one editor session). |
| `native_capture_files.py` | Captures Native templates for specific `.hlx` files so a parity run renders exactly the file the Stomp got. |
| `native_capture_frozen.py` | Captures the `match_song` template sets at each engine policy's frozen values ([T055](../board/TASKS.md#t055 "Native phase 3: parallel render pool for search") follow-up). |
| `native_bypass_probe.py` | Block bypass over MIDI CC under the host's state reset. |
| `native_bypass_patterns.py` | Bypass patterns over MIDI CC against drop-captured truth on an 8-block chain. |
| `native_cc_grid_probe.py` | Which value Native gives a parameter at MIDI CC c of 127. |
| `native_cc_range_probe.py` | Whether Native honours a CC assignment's own `@min/@max` ([T072](../board/TASKS.md#t072 "Narrower CC ranges for time-like and rate parameters in Native…")). |
| `native_fullrange_check.py` | Live check for values outside the narrowed CC ranges ([T098](../board/TASKS.md#t098 "E3 engine push: values outside the narrowed CC ranges load…")). |
| `native_reset_truth.py` | Independent check of the two Native reset paths on random settings. |
| `native_snapshot_check.py` | Which values Native renders when a dropped `.hlx`'s snapshot controller values disagree with its block values ([T051](../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…") follow-up). |
| `native_push_checks.py` | Engine-push checks on one Native host ([T082](../board/TASKS.md#t082 "N6 engine push: (a) exact 0.0 command lands at lo,")): tail pre-roll and related. |
| `native_render_many_check.py` | `render_many` equals serial renders on real Native ([T080](../board/TASKS.md#t080 "N3 engine push: NativeEngine.render_many(dry, variants) on…")). |
| `native_nan_check.py` | [T132](../board/TASKS.md#t132 "Native NaN after search") live check: a NaN-poisoned host is caught and cleaned. |
| `native_pool_bench.py` | Tests per second of `NativeRenderPool` for a list of host counts. Re-runnable diagnostic. |
| `native_render_probe.py` | Where Native render time goes on a captured chain. Re-runnable diagnostic. |
| `native_mem_probe.py` | Native host memory after each kind of operation. Re-runnable diagnostic. |
| `host_budget_live.py` | [T133](../board/TASKS.md#t133 "Machine-wide Native host scaler") live check of the host scaler with a CPU burner. |
| `chain_render_check.py` | [T121](../board/TASKS.md#t121 "Gate 3 groundwork: wire the chain builder") live check: the chain builder's Native renderer against one instance. |
| `blockserial_spike.py` | [T045](../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…"): does a chain of single-block instances equal the same chain in one instance. |
| `amp_refine_study.py` | [T120](../board/TASKS.md#t120 "Amp specialist (human 2026-10-17 \"a specialist per effect family\"") study: refine methods compared at an equal render budget. Runs at import. |
| `stomp_spike_probe.py` | [T003](../board/TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change"): finds the Stomp's audio and MIDI ports, plays a probe, sends a CC or Program Change. |
| `stomp_inject.py` | [T018s](../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"): whole-preset injection into the edit buffer, offline `gen` and `compile`, then `run`. |
| `stomp_fw380.py` | HX Stomp firmware 3.80 re-checks ([T059](../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session") follow-up), edit buffer only. |
| `stomp_hw.py` | 2026-10-15 hardware checks: `restore04c`, byte-diff of the edit buffer, renders. |
| `stomp_speed.py` | [T061](../board/TASKS.md#t061 "Stomp speed improvements and assignment-free base") speed measurements, before and after. |
| `stomp_push_session.py` | [T095](../board/TASKS.md#t095 "Engine-push pedal session"): one Stomp session for the push evidence (S1, S2, S4, S5). |
| `stomp_robust.py` | [T108](../board/TASKS.md#t108 "HX Stomp verification suite scripts/stomp_verify.py") follow-ups: device-refused models, in-place time-parameter glide, stream slips. |
| `stomp_twopath.py` | [T063](../board/TASKS.md#t063 "Two-path (Path A + B) render gap on the HX Stomp fw 3.80"): two-path (A + B) render gap on firmware 3.80. |
| `stomp_twopath_random.py` | [T071](../board/TASKS.md#t071 "Two-path parity on the Stomp"): two-path parity on a random chain. |

All `stomp_*` spikes drive the pedal and follow the pedal rules; do not run them while stomp-engine is fixing the USB freeze ([T168](../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue")). Their result manifest is "to add" only if one is promoted to a tool.

## Checking this document

`tests/test_tools_doc.py` checks, without running any tool, that every file in `scripts/` is named here, that the class counts above add up, and that every `--flag` used in an example command exists in that script's source. Update the document when a script is added or a flag changes.
