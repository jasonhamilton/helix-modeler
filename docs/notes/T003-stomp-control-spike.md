# [T003](../../board/TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change") spike: driving the HX Stomp from code (stomp-engine, 2026-10-13)

Status: **offline half done 2026-10-13; hardware batch H1-H9 run on macOS 2026-10-14 (results in "Hardware batch results" below).** H3a/H3b (HX Edit exclusivity) were skipped on instruction (HX Edit not launched); H10/H11 need Linux/Windows machines. Upstream code was read and built in a scratch directory; none of it, and no Line 6 file, is in this repo.

## [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"): autonomous whole-preset injection (2026-10-14, macOS, fw 3.50, HX Edit closed)

**Result: YES, control and audio (rendered 2026-10-14T23:44Z by the human from Terminal; see "[T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") rendered results" below; the first agent-run paragraphs here describe the control-only stage).** Code wrote generated `.hlx` presets (not a restore of the pedal's own buffer) into the edit buffer and read them back with no human step. 4 of 4 landed, including a new Amp+Cab model pair built from the file. Restore to 04C matched, and the buffer after the restore is byte-identical to the backup. The renders, and so the "expected direction" check and the settling time, were **not done**: the HX Stomp audio stream would not start from the agent's sandboxed shell (PortAudio -9986, AUHAL err 35, also with no control session open), and an unsandboxed retry was denied by the permission classifier. Ledger: [2026-10-14T23:18:39](../../ledger/runs.csv?plain=1#L62 "render-cost · stomp · smoke · whole_preset_write_s = 0.319 (first of 4 rows at this time)") (4 rows: `whole_preset_write_s`, `whole_preset_load_s`, `whole_preset_landed`, `reamp_stream_start_ok`=0).

How it works:

1. **Variants** (`scripts/stomp_inject.py gen`, written to `cache/stomp/inject/`, gitignored because they are derived from a Line 6 export). Each is `known.hlx` read with `tonematch.preset` at device_version 0x3500000 (3.50, as in the HX Edit golden). Every model is checked against the catalog's `stomp_min_firmware` (all have none, so all run on 3.50). Path A's input is set to `@input` 6 (USB 5/6) in all of them. K is known.hlx with only that change. A changes the amp: Drive 0.61 to 0.95, Master 1.0 to 0.7, Bass 0.5 to 0.65. B0 is Brit Plexi Brt + 4x12 Greenback25 at catalog defaults, `@mic` 6. B is B0 with the DL4 Ping Pong enabled in the block and in all 3 snapshots. `hlx_validate.py --firmware 0x3500000`: all 4 ok (0 fail; the usual WARN: DSP budget not enforced).
2. **Compile** (`stompd compile <hlx> <base> <out>`, offline, never opens USB; a separate build at `cache/stomp/bin/stompd-compile`, source kept locally in `cache/stomp/stompd-src/`, not in the repo per the human's stompd decision). The base is a backup of the current edit buffer, so the device's own container is kept: section table, metadata, settings, junctions, assignments, the snapshot section. Each `.hlx` block is placed by `@path`/`@position` through the base's layout. A block with the same model is edited in place, and every value keeps the wire variant the device used. A block with a new model is built: values typed by catalog kind (switch to bool, menu to int, knob to f32), the engine class and second count from `hx-catalog`, a cab's `@mic` and a delay or reverb's `@trails` added as the one value past the symbol list. Blocks the file does not name are emptied. Snapshot bypass states and the input routing come from the file. **Self-check:** with every slot forced to "built", known.hlx compiles to a document **byte-identical** to the device's own 04C buffer (2460 bytes). tonepush's own builder (`hx-catalog/src/build.rs`) says "the pedal will not take what this produces". It writes every value as f32. The difference here is typing values by kind and adding the extras, and the pedal accepted the result.
3. **Write** (op 21, `restore` in the approved `cache/stomp/bin/stompd`, EDIT BUFFER only; hx-usb's `write_preset` waits until the document reads back). The bridge now refuses this command for anything but the same session's own backup unless it is opened with `allow_document_write=True` (`stomp_bridge.DOCUMENT_WRITE`). No save, rename, restore-all, set-setting or IR command was sent; the USB helper binary is unchanged (sha256 30422bed...).

| Variant | Compile | Write (s) | Whole `load()` (s) | Read-back document vs written | Read-back chain vs `.hlx` |
|---|---|---|---|---|---|
| K | in place; input 1 to 6 | 0.313 | 0.446 | equal except the order of the 3 MIDI-CC assignment entries | equal (models, cab, enabled, every named value, input 6) |
| A | in place; Drive, Bass, Master | 0.324 | 0.487 | same | equal |
| B0 | **built** slot 2: model 20/45 to 15/66 (Plexi Brt / Greenback25) | 0.319 | 0.463 | same | equal |
| B | delay enabled; 3 snapshot states | 0.319 | 0.464 | the device also set the delay footswitch's LED colour (67840 to 458496) and state (false to true) | equal |

Whole `load()` = backup 0.03-0.06 s + compile 0.026-0.030 s + write 0.32 s + chain read and input check. The in-place `load()` with one Amp+Cab model change took 0.658 s ([T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse")). Restore: `select` 04C, chain signature equal on the first step, buffer read with a fresh session byte-equal to the backup. Run directory: `cache/stomp/inject/20261014T231801Z/` (summary.json, every written document, every read-back, chains). The first attempt (`.../20261014T231317Z/`) wrote K, which landed (verified offline after the run), then stopped at the first render with the audio error. It restored cleanly too.

Not done, and why:
- **Renders and the expected direction.** Prediction written before any render: A has a lower crest factor and higher spectral flatness than K, and a higher idle noise floor. B differs from B0 in the energy of the last 0.5 s of the clip (delay repeats). `scripts/stomp_inject.py run` (without `--no-render`) does this, with features, nulls and WAVs. It needs a shell where CoreAudio input works. Earlier Stomp renders on this Mac worked ([T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse"), [T019](../../board/TASKS.md#t019 "Own Stomp engine-smoke row once the human approves running the local…")), so the human's terminal is expected to work.
- **Settling time** after a whole write: the same run renders right after the write, again, and after 2 s, and nulls first-vs-settled against repeat-vs-settled. Unmeasured.
- Footswitch and controller sections of the `.hlx` are not compiled: the base's are kept, and the device updates a footswitch LED itself (B). A model change or emptied slot that carries controller assignments is refused, as are Send/Return blocks. Splits and joins keep the base's model and attach point. Only blocks on rows the base already has can be placed.

What the human must run to finish it (HX Edit quit, pedal on 04C):
`TONEMATCH_STOMPD=$PWD/cache/stomp/bin/stompd TONEMATCH_STOMPD_COMPILE=$PWD/cache/stomp/bin/stompd-compile TONEMATCH_AGENT=stomp-engine .venv/bin/python scripts/stomp_inject.py run`
(about 2 minutes; 4 whole-document writes plus 14 renders, then the restore and the byte diff).

### [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") follow-up: silent render after the whole write (human's Terminal run, 2026-10-14 23:33Z)

The human ran `run` from Terminal. Audio started there, so the earlier -9986 came from the sandbox. K compiled (input 1 to 6), was written (0.31 s) and read back as written, with an empty chain diff. Then the first render raised "the Stomp returned silence". The restore was clean (`cache/stomp/inject/20261014T233351Z/`).

Offline diagnosis, without touching the pedal: K's read-back Input body is `{5: 6, ...}`. A buffer captured at 17:19Z, after the [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") engine's live `route_key` (the routing that rendered fine), holds exactly the same `{5: 6}`. Output `{6: 1}`, settings key 6, the current snapshot and the Output values are identical to the 04C backup. So the document is the same as a working one. The difference is the path. T004/T019 sent a live `route_key` (op 42). After the whole write, `_load_whole` saw the chain already reading 6 and sent nothing. **Hypothesis:** op 21 stores the input routing, but the audio engine keeps routing until a live op-42 route. A settle delay is the alternative. **Fix (implemented, unverified):** `StompEngine.REASSERT_INPUT_AFTER_WHOLE = True` always re-sends `route_key` after a whole write. Test: `test_whole_write_resends_the_input_route_even_when_the_chain_reads_usb56`.

`run --diagnose` gathers the data to decide in one run. First a control: the original buffer with a live `route_key`, rendered. Then K is written whole without the re-send and rendered at 0 s, 0.5 s and 2.5 s, again after re-sending `route_key`, and (only if still silent) after a 1-to-6 toggle. Each probe records the RMS of USB 1/2, 3/4, 5/6 and 7/8, the routing the chain reports, and a WAV. Backups go to `diag_c0.bin`, `diag_k.bin` and `diag_k_after.bin`. Then K, A, B0 and B run normally with the fix. A render failure is recorded and the next variant runs; a control error stops the run. The restore to 04C and the byte diff always run.

**Outcome of the follow-up: the routing hypothesis was wrong.** Run `20261014T234106Z` (`--diagnose`) rendered the control, the ORIGINAL 04C buffer with a live `route_key` (the configuration [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") proved), and got exact digital zero (`diag_c0` peak 0.0). So the silence was macOS withholding audio input from Terminal (Microphone permission), not the document. That run then stopped on a script bug: a numpy bool in the diag print, since fixed and tested. No whole write was made, and the restore was clean. After the permission was granted, run `20261014T234400Z` had sound at k1, straight after the write with no route re-send and no settle (-23.27 dB, identical to the control). `REASSERT_INPUT_AFTER_WHOLE` stays: it is harmless (one op-42 command, k4 identical) but **not needed**. The Microphone requirement is now in `docs/DEV-ENV.md` and `docs/notes/stompd.md`. Ledger: `render_silence` rows for both runs (23:4x).

### [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") rendered results (run `cache/stomp/inject/20261014T234400Z/`, human's Terminal, analysis stomp-engine)

All 4 variants were written whole, read back as written, and rendered with audio. 19 renders in all (diagnose 5, then per variant r0/r1/r2, plus 2 idle). Restore: matched, and the post-restore buffer is byte-equal to the backup. Writes took 0.306-0.317 s, a whole `load()` 0.464-0.488 s.

Nulls on the r2 renders, recomputed with cross-correlation alignment (integer and fractional lag) and a least-squares gain. Analysis in `.../analysis/aligned.txt`:

| Pair | Lag | Gain | Null, aligned + gain | Unaligned (lead's) |
|---|---|---|---|---|
| K vs A (Drive/Master/Bass) | 0 | 0.942 | -7.4 dB | -7.4 |
| K vs B0 (new Amp+Cab pair) | +7 samples (frac -0.31) | 1.231 | **-3.3 dB** | -0.2 (misaligned: the Plexi path is 7 samples later) |
| B0 vs B (delay on) | 0 | 0.999 | -23.9 dB | -23.9 |
| repeat r0 vs r2, K / A / B0 / B | 0 | ~1.0 | -27.9 / -36.6 / -39.6 / -38.8 dB | same |

- **Settling:** no settle effect in A, B0 or B. The first render after the write nulls against the settled one about as well as a repeat does (-36.6 vs -33.4, -39.6 vs -43.7, -38.8 vs -32.9). K is the exception: first vs settled -27.9 dB against repeat -40.3 dB. That is one outlier with an unknown cause; the Essex model's Hum and Ripple are a guess. No settle delay is added.
- **B0 vs B is a real difference:** -23.9 dB is 15 dB above B's own repeat noise. The lead compared it with K's repeat noise, which is K's outlier.

Pre-registered predictions (written before any render):

| Prediction | Outcome |
|---|---|
| A crest factor lower than K | **Wrong.** 11.02 vs 9.71 dB (peak/RMS); 8.88 vs 8.56 with the 99.9th percentile. A plausible but untested reason: Master 1.0 to 0.7 removes power-amp compression while Drive 0.61 to 0.95 adds preamp gain. |
| A spectral flatness higher | Right: -19.76 vs -21.85 dB |
| A idle noise floor higher | Right: -80.31 vs -86.95 dBFS |
| B more energy than B0 in the last 0.5 s | **Not supported** (+1.08 vs +1.09 dB relative to clip RMS). Inconclusive rather than refuted: di_2s plays to its last sample, so repeats are masked, and the delay Mix is 0.23. |

Post-hoc and not pre-registered, so weaker evidence: the residual B - B0 (aligned, gain-matched) correlates with B0 lagged by 348.5 ms at |r| 0.309. Across lags of 50-1200 ms the median is 0.055, and the same test on B's repeat residual gives 0.032. This is consistent with delay repeats being present.

Ledger 2026-10-14T23:47Z (11 rows): `whole_preset_rendered` 4, `whole_preset_write_s` 0.309, `null_residual_db` x4, `prediction_correct` x4 (0,1,1,0), `delay_residual_corr` 0.309.

**Remaining for [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp"):**
- A delay test with a gap after the last note: a clip that stops at 1 s and runs 2 s of silence, pre-registered on tail energy.
- Repeat K's first-render outlier.
- Then the lead decides on the `whole_preset` default, which stays `off`.

## [T059](../../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session"): blocks locked after a session (2026-10-15, stomp-engine; phases 1 and 2 offline, pedal not touched)

**Cause (from published sources, interop only).** tonepush's session ends without a closing handshake, so the pedal keeps an "editor attached" state and locks block editing until USB is unplugged. HX Edit ends every session with one bare session-close frame per channel (type `0x02`, no body; events and data, then control), each answered by the pedal, before releasing the interface. fretwire documents this as "the panel lock fix", verified live. tonepush had the close once (with control first) and removed it in 9fd6fe2 (2026-08-05) after a second consecutive hardware run could not open the device. Details and evidence: `docs/notes/stompd.md`, "Session end".

**Fix (phase 2).** The helper now sends HX Edit's session-close at the end of every session, including when stdin closes or the session is lost. Its bookkeeping reproduces all 30 close frames of tonepush's 10 captured HX Edit quits exactly. The new build is `cache/stomp/bin/stompd`; the old one is kept as `stompd.pre-T059`. `StompBridge` records the helper's `closed` report (`release_report`). On a timeout it now closes the helper's stdin and waits 3 s before terminating it, so even a failed run sends the close.

**Fallback (phase 1).** `stompd reset` re-enumerates the pedal's USB. `StompBridge.close()` runs it per `TONEMATCH_STOMP_USB_RESET` (`auto`, the default: only when no session-close was sent; `always`; `off`). It never runs while `tonematch.reamp` streams audio, and never on a wedged pedal. `--usb-reset` is available on `reamp_capture.py`, `parity_null.py run` and `stomp_inject.py run`. **Risk:** tonepush says this exact call (nusb `Device::reset`) took an HX Stomp off the bus until a physical replug. So `auto` is the default rather than `always`, and the reset is untested on hardware. Tests: `tests/test_stomp_session_end.py` (21, fake helper), helper unit tests (3).

**Phase 3 (one short session, only on the lead's go; HX Edit quit, pedal on 04C, the human watching):**
1. `TONEMATCH_STOMPD=$PWD/cache/stomp/bin/stompd .venv/bin/python scripts/stomp_hw.py release-check` (open, `info`, close; USB reset off). The human then tries to edit a block. If it is locked: unplug and replug USB, and stop.
2. If it is editable: `... release-check --sessions 2 --chain`. This checks that a second session opens after a clean close (tonepush's reason for removing the close). It also reads the Input/Output routing read-only, and the human checks the blocks again.
3. `... stomp_hw.py verify fixtures/smoke/known.hlx` (read-only readback of 04C against its known export).
4. `cp cache/stomp/latency.json cache/stomp/latency_fw350.json`, then `... reamp_capture.py --calibrate --ledger --usb-reset off`. This sets Input to USB 5/6 (value 6, op 42 key 51) and bypasses blocks in the edit buffer only, then undoes both. A tone that comes back at about -18 dBFS confirms value 6 on 3.80, and the impulse gives the 3.80 latency.
5. Output routing key 52: nothing above writes the Output block (the engine never routes it). This step only reads it (2). An active check would write op 42 to the Output, which silenced the output on 3.50, so it is left out unless the lead asks for it.

## [T056](../../board/TASKS.md#t056 "HX Stomp priorities (human, 2026-10-15)"): HX Stomp priorities (human, 2026-10-15), stomp-engine

Every run below started and ended on 04C, with the edit buffer byte-equal to `cache/stomp/live/base04C.bin`; no flash write was made. Run directories: `cache/stomp/hw/<stamp>-<cmd>/` (from `scripts/stomp_hw.py`) and `cache/parity/<stamp>/`. Ledger 2026-10-15T21:1x: 14 rows labelled "[T053](../../board/TASKS.md#t053 "Native phase 1, bugs") item N", corrected to [T056](../../board/TASKS.md#t056 "HX Stomp priorities (human, 2026-10-15)") by the row at 21:16:16.

**Found in passing: 04C in flash changed between 18:19Z and 19:39Z on 2026-10-15.** It is now 2457 bytes; it was 2460. The only difference is the delay's CC-110 bypass assignment: LED colour 65280 → 0 and key 11 True → False. Our tools never save, so this was a save on the pedal (or by something outside our tools). The sound is unaffected. `base04C.bin` is the new reference.

### Item 1: wedge health check
`StompBridge` now runs `info` with a 3 s timeout right after opening, before anything else. A hang on open, or no answer, raises `PowerCycleNeeded` with one plain message: switch the pedal off and on (9V), wait for it to boot, run again; nothing was written. Every bridge user gets this (the engine, `reamp_capture.py`, `parity_null.py`, `stomp_inject.py`, `stomp_hw.py`). Tests: a fake helper that never answers `info`, and a healthy one.

### Item 2: delay read back Mix 0.72 / Feedback 0.52 / Time 0.47
- **Not reproduced.** Both the old `rank2_H_live.bin` and the fixed `H_check.bin`, written onto 04C, read back Time 0.375, Feedback 0.40, Mix 0.25 immediately and at 1, 3 and 6 s.
- **MIDI does not explain it.** CC 110 (the delay's controller and its bypass) moves only Mix (0.23..0.54) and Feedback (0.52..0.93), and toggles bypass. CC 69 snapshot switching recalls the snapshot's stored values, and edits are kept per snapshot.
- **[T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…")'s explanation is refuted.** The audible test (3 s pre-roll) nulls old against fixed document at -32.4 dB, against a repeat noise of -32.8/-33.9 dB. So the stale snapshot controller values are not applied when a document is written; they matter only on a snapshot recall. The [T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…") compiler fix (snapshot values from the file) is still right, because a recall would apply them.
- **Likely cause:** 0.47 / 0.52 / 0.72 are exactly the DL4 Ping Pong catalog defaults. That is what a `model` command produces: it resets the block to defaults (seen in H6). The only senders in our code are `StompEngine`'s in-place model change and `measure_load_penalty`; re-selecting the model on the pedal does the same. No buffer was captured between 12:17 and 12:39 local, so which one it was can't be decided.
- **Fixes:**
  - In `whole_preset="fallback"` mode, a model change is now one whole write, so the engine never sends `model` there.
  - `stomp_hw.py verify <hlx>` reads the current buffer without writing and diffs every parameter. Run it when a live session starts and when a listening judgement is recorded.
- **Found in passing:** with the old 0.25 s pre-roll, the same delay document rendered 8.5 dB apart across loads, and repeats were only -28 dB, because the previous render's echoes were still in the delay. Fixed: `StompEngine.pre_roll_s()` uses the time to -60 dB from Time and Feedback plus 0.5 s for an enabled delay (2 s Time assumed when tempo-synced), 4 s for a reverb, capped at 8 s.

### Item 3: K's noisier first render
**No settle effect.** Three K/A cycles of a whole write followed by 4 renders:
- The first render after the write has a median null of -30.9 dB against the cycle's last render; later renders -31.2 dB.
- The control with no write at all gives -30..-33 dB.
- Integer offsets are 0/±1 sample, with no fractional lag.
- The residual sits at the clip's note frequencies plus 120/240 Hz: a free-running Hum/Ripple modulation whose phase differs per render.

With Hum = Ripple = 0 (K0/A0), the median is -54.4 dB, with many pairs at -76..-89 dB. The first render is -50.7 dB (n = 6) against -55.3 dB (n = 12), with overlapping ranges, so not significant. **No fix to the write path is needed.** Comparisons on the Stomp that need better than -30 dB should use Hum = Ripple = 0. That is a proposal for the lead: it could be a Stomp entry in the per-engine policy, but it changes the exported tone slightly, so it is not added.

### Item 4: per-engine parameter policy
`src/tonematch/param_policy.json` and `tonematch.param_policy`:
- Actions: `freeze` (hold at a value) or `forbid`, per engine.
- Functions: `frozen_for`, `forbidden_for`, `violations`, `apply`.
- First rule: Stomp, cab EarlyReflections frozen at 0.0, with its evidence and "until a firmware re-measure".
- `scripts/match_song.py` `STOMP_FROZEN` now reads `param_policy.frozen_for("stomp")` (same value as before).
- `StompEngine.load()` records `policy_warnings` and never rewrites a preset.
- Packaged via `pyproject.toml` package-data. Tests: `tests/test_param_policy.py`.

### Item 6: delay-tail test (pre-registered 2026-10-15T21:07Z)
The pre-registration was written to this note at 21:07Z, before any render of these presets. A concurrent rewrite of the note (task-link insertion) dropped it at about 21:15Z. It is restored here verbatim:

> Presets (`cache/stomp/hw/presets/`, all Brit Plexi Brt + 4x12 Greenback25 from B, Hum = Ripple = 0, snapshot controllers synced): B00 delay off; BT0 delay on, TempoSync off, Time 0.375 s, Feedback 0.40, Mix 0.25; BS0 delay on, tempo-synced as in B (SyncSelect 6 at preset tempo 81; Feedback 0.52, Mix 0.23). DI `cache/parity/di_10s_tail.wav` (10 s of playing + 2 s of silence); tail = the last 2 s; `parity_null.py run`. P1: Stomp tail RMS BT0 ≥ B00 + 10 dB. P2: Stomp tail RMS BS0 ≥ B00 + 10 dB. P3: Stomp-vs-Native tail null of BT0 ≤ -15 dB. P4: tail null of BS0 > -5 dB (tempo; NOT blind, B gave +4.3 dB at 00:23Z). P5: BT0 tail RMS Stomp minus Native within ±2 dB. P6: B00 body null ≤ -30 dB (NOT blind, B0 -37.5 dB).

Results (`cache/parity/20261015T211012Z/`, `tail_analysis.txt`):

| | Prediction | Outcome |
|---|---|---|
| P1 | BT0 tail ≥ B00 + 10 dB | **FAIL, narrowly:** +9.66 dB (Native +9.34) |
| P2 | BS0 tail ≥ B00 + 10 dB | pass: +11.58 dB |
| P3 | BT0 tail null ≤ -15 dB | **FAIL:** +4.30 dB; body -16.2 dB (B00 -39.7) |
| P4 | BS0 tail null > -5 dB | pass: +4.34 dB. But P3 shows the stated reason (tempo) was wrong |
| P5 | BT0 tail RMS within 2 dB | pass: +1.34 dB matched (+1.99 raw) |
| P6 | B00 body null ≤ -30 dB | pass: -39.65 dB |

**Why P3 failed (post hoc; `cache/parity/20261015T211225Z/echo_times.txt`):** a 20 ms 440 Hz burst followed by 3.5 s of silence through BT0 shows identical echoes on both engines. Echo levels match within 0.2 dB and per-echo xcorr is 0.96-1.00. But Native's echoes drift about 71 samples (1.48 ms) per echo against the Stomp's: 1.4, 3.0, 4.4, 5.9 and 7.3 ms over five echoes. That is about a 0.8% delay-time difference between fw 3.50 and Native 3.82 at a fixed Time, so delay tails cannot null across the two engines. Compare tail levels and echo times instead.

**For native-engine:** Native's own BT0 capture was not deterministic (repeat null -24.5 dB, fresh replay -16.2 dB, where B00 and BS0 gave -314 dB).

### Item 8: Path A and Path B at once
- **Compiler:** `stompd-compile` (build sha 62b12a19; the previous build kept as `stompd-compile.pre-pathB`) now writes Path B's input (split slot, body 14, key 5) and output (join slot, body 16, key 6) from the file's `inputB.@input` / `outputB.@output`. It is byte-identical to the previous build on K, A, B0, B, C and H, and the force-build self-check still reproduces 04C.
- **Routing:** on the Stomp, Path B's **output value 6 = USB 3/4**. Values 1 and 5 merge Path B into USB 1/2; 0, 2, 3 and 4 give no USB 3/4.
- **Throughput:** a dual pass takes 2.670 s against 2.668 s for a single render, so **2.0x renders per second** (2 chains per pass). The dual document write takes 0.36-1.24 s.
- **But dual renders are not interchangeable with single-path renders.** Path A (K0) in the dual layout nulls at **-18.3 dB** against the same chain rendered alone, aligned and gain-matched, over 3 runs. Path B (B00) gives -22.0 dB.
  - This is not crosstalk: the same -18.3 dB appears with Path B bypassed.
  - It is not delay drift: per-window fractional alignment gives -18.5 dB.
  - It is not level or pan: band levels agree within 0.3 dB, and L = R.
  - It is repeatable: dual-vs-dual nulls at -40..-75 dB, against single-vs-single -54..-77 dB.
  - Cause not found.
- **Use:** dual passes are fine where renders are only compared with other dual renders (screening, benchmark sets rendered the same way). They are not fine for parity, or for the final verification of a single-path preset.
- **Gain:** Stomp verification and Stomp benchmark renders, about 2.67 s → 1.33 s per chain. The overnight library is a Native job and does not speed up.

### Item 7: whole-preset path and default (recommendation; the lead decides)
- **Done today:**
  - Health check before every run.
  - Delay/reverb flush pre-roll.
  - Path B endpoints compiled.
  - `fallback` now sends model changes as one whole write (no `model` resets).
  - Full-parameter readback ([T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…")) on every whole load.
- **Evidence:** about 40 whole writes today, 0.31 s each. Every one read back as written, with no wedge. No settle is needed: item 3's first-render medians are -30.9 vs -31.2 dB (Hum and Ripple on) and -50.7 vs -55.3 dB (off), the second not significant.
- **Recommendation:** make `whole_preset="fallback"` the default. In-place `set` commands stay for parameter-only changes, which cost milliseconds. Whole writes handle model and layout changes (0.46 s, and no default resets).
- **Not** `always`, because a whole load costs about 0.46 s against milliseconds for a `set`.
- **Open risks:**
  - op 21's lockup history (none seen in about 40 writes).
  - Compile refusals: model changes on slots with controller assignments, Send/Return.

## [T051](../../board/TASKS.md#t051 "Bug: rank2_H_live delay read back Mix 0.72 / Feedback 0.52 / Time…"): wrong delay values after a whole write (2026-10-15, offline only; the pedal was in live use)

**Report (lead).** rank2_H_live.hlx was compiled onto the 04C backup and written with `restore`. The pedal then read back delay Mix 0.72, Feedback 0.52 and Time 0.47. The .hlx says 0.25, 0.40 and 0.375 (TempoSync off).

**Decoded offline (`stompd decode`).**
- The compiled document's delay block is right: Time 0.375, Feedback 0.40, Mix 0.25, TempoSync off. The amp and cab are right too.
- Its snapshot section is wrong. In all 3 snapshots, `tone[10][10][n][2]` still holds the base's controller values: Feedback 0.52 and Mix 0.23. In 04C both are assigned to MIDI CC 110.
- The device recalls the current snapshot's controller values over the block values on load, so the delay ran Feedback 0.52. That is the value read back. The compiler never compiled `snapshotN.controllers`.
- **Mix 0.72 and Time 0.47 appear in neither document** and are not explained offline. With CC 110 at any value, Mix would be at most 0.54. Possible causes are a knob turned on the pedal during the live session or the lead's own edits, but neither is known. The hardware check below decides it.

**Fixes.**
1. Compiler: snapshot controller values and tempo now come from the file (`docs/notes/stompd.md`).
2. `tonematch.engines.stomp_readback`: a full-parameter diff that covers every parameter of every block, enabled state, models, stray enabled blocks, the input routing, and on a decoded document every snapshot controller value. Tolerances: exact for switches, menus and extras; 1e-5 of the range for knobs. `StompEngine` runs it twice and raises `ReadbackMismatch` on a failure. The first run is on the compiled document before the write (nothing is written on a mismatch). The second is on the read-back chain after the write; in-place loads get this one too.
3. The engine sets snapshot controller values to the slot values in the .hlx it writes, so it renders what `set_params` set.
4. `parity_null.extra_variants` H now keeps its snapshot values equal to the block. The old H.hlx said Mix/Feedback 0.25/0.40 in the block and 0.23/0.52 in every snapshot. H.hlx has been regenerated.

**Results that could be affected.** Offline audit: each preset compiled with the old compiler onto 04C, decoded, snapshot recall applied.
- Unaffected, because their delay Mix/Feedback equal 04C's 0.23/0.52 and their files agree with themselves: K, A, B0, B, C, all best_snapshot1, er025/050/100, v_*. This covers parity-v0 rows 2026-10-15T00:25 (K A B0 B), 02:32, 02:48, 03:18, 03:20 and 18:21, and the [T018s](../../board/TASKS.md#t018s "Spike: autonomous whole-preset injection on the HX Stomp") renders. Every amp and cab value would have landed.
- Affected:
  - The live session (ledger 19:20:34 `live_pref_delay_mix`): the delay was not the .hlx's.
  - H parity: never run on the Stomp. With the old compiler and the old H.hlx the Stomp would have run 0.23/0.52. With the old H.hlx even the fixed compiler follows the file's own snapshot values.
  - How Native treats a block-vs-snapshot conflict is unverified. Native-side results on the old H.hlx, if any, should be checked by native-engine.

**Hardware check for the lead (after the human is done).** Run `parity_null.py check --base <fresh 04C backup> --presets cache/stomp/live/rank2_H_live.hlx` (offline). Then write the newly compiled document with nobody touching the pedal and read back `chain` at once. Expect Time 0.375, Feedback 0.40, Mix 0.25. If Mix or Time still differ, the cause is outside the document.

## Hardware batch results (2026-10-14, macOS 27 arm64, HX Edit closed)

**Firmware: 3.50** (build `v3.15-207-g09aad57`, device id `0x00210006`, serial 3554786), read over USB in H2. Not 3.80: generated `.hlx` files for this pedal must carry device_version for 3.50 (the HX Edit golden says 3.50, consistent; the Native golden's 3.80 is not this pedal).

Tooling (scratchpad only, not in the repo): `stompd`, a long-lived JSON-lines helper (about 450 lines, written for this task) built as an example inside the scratch tonepush checkout at `53a6a65`. It holds one session, sends keepalives and drains notifications every second when idle, and exposes edit-buffer operations only. Save, rename, slot write, settings write and IR commands are not exposed. Python client: `src/tonematch/engines/stomp_bridge.py` (`TONEMATCH_STOMPD` points at the binary). Sessions opened in about 0.5 s; about 20 sessions were opened and closed this run without a wedge.

| # | Result | Evidence |
|---|---|---|
| H1 | PASS | `system_profiler SPUSBHostDataType`: HX Stomp, VID 0x0e41, PID 0x4246, 480 Mb/s; `fretwire detect`: "HX Stomp: present"; `tonepush list`: "HX Stomp (pid 0x4246) serial 3554786 - 126 presets" |
| H2 | PASS (firmware 3.50, not 3.80) | Original preset **04C "Smoke Known"** (setlist 0, index 11). Edit buffer backed up to `cache/stomp/spike/orig_editbuffer.bin` (2460 bytes) and chain to `cache/stomp/spike/orig_chain.json`. `read_preset_at` (op 4, read a stored slot) fails on 3.50: "preset blob was not an l6-helix document". |
| H3a/b | SKIPPED | HX Edit was not launched (instruction). Exclusivity still rests on upstream evidence only. |
| H3c | PASS | "HX Stomp" CoreAudio device has 8 in and 8 out at 48 kHz (no volume or mute controls; one stream each way); MIDI in and out "HX Stomp". Control `info` calls made during an 8 s render: no error, no dropout. USB control, USB audio and USB MIDI (H8, H8b) coexist. |
| H4 | PASS, after a fix | **Input "USB 5/6" is value 6 on the Stomp**, not the Floor menu's 15. tonepush's name lookup picks 15, which the device accepts and echoes back, but no audio arrives. A sweep of 0..16 found 6 = host 5/6 (5 to L, 6 to R) and 7 = host 7/8. Values 3, 5, 9, 10, 12 and 13 are refused. Record side: 1/2 = Path A, 3/4 = Path B, 5/6 = dry Main L/R in (digital zero with nothing plugged in). |
| H5 | PASS | Drive 6.1, 2.0, 8.0: probe renders changed (aligned null p0/p2 -15.0 dB, p1/p2 -6.0 dB, p0/p1 -6.8 dB); repeat at the same setting -30.8 dB. Without input, Drive and Ch Vol at 10 raised the idle noise floor from -86.99 to -71.65 dBFS, and putting them back gave -86.99 again. |
| H6 | PASS | Essex A15 to US Deluxe Nrm: model command 0.242 s; render null +10.9 dB (louder and brighter); repeat -48.4 dB. **`set_model` on an Amp+Cab drops the cab**; the engine uses `set_model_pair`. |
| H7 | PASS | Amp bypass: render changed (rms -5.6 dB, null -1.7 dB); re-enabled render nulls at -67.3 dB against the earlier one. Bypassed amp with idle input: -186.6 dBFS. |
| H8 | PASS | Drive assigned to MIDI (source 8) and CC 20; reads back as "MIDI CC", CC 20. CC 20 = 0 / 127 on channel 1 gives Drive 0.0 / 10.0 (read back over USB); renders 27 dB apart. Narrowed range Min 0.4 / Max 0.6: CC 0, 1, 64, 127 give 0.400, 0.4016, 0.5008, 0.600, so the step is 0.00157 of the range. |
| H8b | PASS | Program Change 12 moves the pedal to 05A, PC 11 back to 04C, with the USB control session open throughout. |
| H9 | PASS | Re-selecting 04C reloads it from flash; chain, routing, values and assignments match H2 exactly (`chain_signature`, `cache/stomp/spike/final_chain.json`). The original buffer had no unsaved edits. No flash write was made at any point in this batch. |

Protocol findings on firmware 3.50 (fed back into the helper; tonepush is tested on 3.80):

- **Output-block routing is confirmed under key 52, not 51.** tonepush's `set_routing` sends 51 for both blocks. On an Output block this left it routed to None and silent, and re-sending 51 did not undo it. `{98: 9, 52: 1}` on op 42 fixed it. The engine never routes the Output block.
- `request` does not check reply status, so a refused write looks like success. The helper's `set` and `route_key` check the status.
- An Amp+Cab's cab parameters are op 30 with key 26 = 1 (fretwire's `set_paired_value`). The cab's **mic** sits past its `Helix.sym` list (an "extra" value): key 29 = false, key 28 = 0, int wire type. `.hlx` calls it `@mic`. tonepush's `.hlx` import does not apply cab settings at all.
- A whole-buffer write (op 21, `write_preset`) restored the slots and the same assignments, in a different order. One op-21 write was made, to the edit buffer only, during the first engine dry run.

Audio and timing (`tonematch.reamp`, `cache/stomp/latency.json`):

- Round-trip latency (impulse, USB 5/6 to USB 1/2, transparent chain): **1206 samples (25.1 ms)** with PortAudio `latency='low'` (measurements 1207, 1206, 1206, 1206, 1206). With default buffering it is 12498 to 12499 (260 ms); blocksize 256 gives 1490 to 1491, blocksize 512 gives 8914 to 8915. The impulse comes back as a single sample at unity. **Stream-start jitter is 0 to 1 sample on every setting.** `render()` cancels it by aligning (±2 samples) to the chain's first render of the same clip.
- Level: -18 dBFS 1 kHz through the transparent chain comes back at -18.000 dBFS, so the calibration gain is 0.0 dB.
- Repeatability on known.hlx, di_2s, after alignment: -31 to -36 dB null between repeat renders. Probably sub-sample variation (the Essex model's Hum and Ripple may add some); fractional alignment is a possible improvement. `load()` rebuilding the chain from a different amp, then rendering, nulls at **-50.3 dB** against the flash-loaded preset.
- Cost (engine, 2 s clip): **2.667 s per render**, parallelism 1. `load_penalty_s` **0.658 s** (whole `load()` with one Amp+Cab model change; the model command alone is 0.12 s). No settle is needed: a render straight after `load()` nulls as well as a repeat does. A `load()` with nothing to change takes 0.081 s. The reamp-capture script's capture (1 s pre-roll, 2 s tail) takes 5.82 s for the 2 s clip.
- Not measured: parallel Path A/B renders (`parallelism=2`), user-DI capture (`--user-di` is written but not run on hardware, nothing plugged in).

Machine: macOS 27.0.1 (26A434), Apple Silicon arm64, Python 3.14.8. Rust 1.99.0 and 1.98.0, installed for this spike under the session scratchpad only (`RUSTUP_HOME`/`CARGO_HOME` pointed there; nothing added to `~/.cargo` or the shell profile).

## Summary: control route per OS (macOS confirmed on hardware 2026-10-14; Linux and Windows still proposed)

| OS | Route | Confidence | Why |
|---|---|---|---|
| **macOS** | **USB control (interface 0) through tonepush's `hx-usb` crate, behind the `stompd` helper. CONFIRMED: H1 to H9 pass, and `engine-smoke` is green on firmware 3.50.** MIDI CC (H8) and PC (H8b) work alongside it. | Confirmed | tonepush was developed and hardware-tested on exactly this setup (HX Stomp fw 3.80, macOS 27.0, Apple Silicon, HX Edit 3.82). On macOS interface 0 has no kernel driver bound. Audio and MIDI are class-compliant and live on other interfaces. Both projects build and pass their offline tests here. |
| **Linux** | USB control: fretwire (its home OS) or tonepush (also supports Linux, with a udev rule) | High | fretwire is verified on the HX Stomp on Linux. It needs the udev rule. Pin 48 kHz for audio. |
| **Windows** | **MIDI CC fallback** on templates; USB control is unlikely | Medium | fretwire states that the Line 6 driver owns interface 0 on Windows. `nusb` on Windows can only claim an interface bound to WinUSB. Rebinding with Zadig would break HX Edit and probably the Line 6 ASIO audio. tonepush ships Windows builds, but nothing in its tree or issues shows a Windows claim succeeding with the Line 6 driver installed. One hardware check settles it (H9). |

Coexistence, all OSes: **HX Edit and our controller cannot hold interface 0 at the same time.** HX Edit claims it exclusively; tonepush's claim error tells the user to quit HX Edit, and fretwire documents one session per device. So the rule is sequential use: quit HX Edit before a Stomp-mode run. USB audio (interfaces 1 to 3) and USB MIDI (interface 4) are separate interfaces. tonepush's MIDI-clock hardware test drives CoreMIDI while its control session is open, which is evidence that MIDI and control coexist on macOS. That is their test, not ours; H3 repeats it with audio streaming.

## Evidence (offline, reproducible)

| Check | Result |
|---|---|
| fretwire @ `fa27c819d00e` (2026-10-13), `cargo build --release -p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-cli` on macOS arm64 | Builds unmodified, 24 s |
| fretwire offline tests (`-p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-data`) | 281 passed, 0 failed |
| `fretwire detect` (enumerates via nusb, IOKit on macOS; claims nothing) | Runs: "no HX device found" (correct, nothing plugged in) |
| tonepush @ `53a6a65b5436` (2026-10-13), toolchain pinned 1.98.0, `cargo build --release -p hx-proto -p hx-usb -p hx-catalog -p tonepush-cli` | Builds, 27 s |
| tonepush offline tests, same crates | 280 passed, 0 failed, 27 ignored (the hardware tests) with `HX_EDIT_RESOURCES` pointing nowhere. With this Mac's HX Edit 3.51 resources found automatically, 1 fails: `hlx::tests::reads_a_preset_shipped_with_hx_edit` skips split/join in 3.51's `default_preset.hlx`. That is an `.hlx` import edge case with old resources, not a USB or macOS problem. |
| `tonepush list` | Runs: "no supported devices found" |
| Python audio/MIDI stack in a scratch venv: `sounddevice` 0.5.6 (PortAudio 19.7), `python-rtmidi` 1.5.8 (CoreMIDI API) | Both import and enumerate. `python-rtmidi` has **no wheel for Python 3.14** and fails to build from source on macOS (`sem_timedwait` in the JACK backend). It builds with `pip install python-rtmidi --config-settings=setup-args="-Djack=false"`. |
| `scripts/stomp_spike_probe.py selftest` | OK (probe signal -18.0 dBFS RMS, 96000 samples, compare logic) |
| `system_profiler SPUSBDataType` | No Line 6 device on the bus |

Found on this Mac: **HX Edit 3.51** at `/Applications/Line6/HX Edit.app` (bundles `libusb-1.0.0.dylib`), plus prefs from 2022 to 2023. It is too old for firmware 3.80; [T005](../../board/TASKS.md#t005 "catalog-import: locate the user's HX Edit install on each OS, read…") and [T010](../../board/TASKS.md#t010 "Confirm Stomp firmware 3.80 and Native 3.82") need HX Edit 3.82. No Line 6 kext, DriverKit extension or HAL plug-in is installed, which is consistent with the Stomp being class-compliant on macOS.

## What the source says (file references are upstream, at the commits above)

- **Device topology** (tonepush `PROTOCOL.md`, "Device topology"): interface 0 is vendor-specific, bulk `0x01` OUT / `0x81` IN (the editor channel); 1 to 3 are USB audio; 4 is USB MIDI; 5 is HID. "Interface 0 has no kernel driver bound to it, and HX Edit holds it exclusively while running." The editor protocol is not on MIDI.
- **Windows** (fretwire `crates/fretwire-usb/src/lib.rs` header; `docs/next-steps.md` Track 2; `STATUS.md`): "on Windows the Line 6 driver owns interface 0"; "Windows can't claim the interface". nusb docs: on Windows the interface must be bound to WinUSB.
- **Edit buffer vs flash** (tonepush `crates/hx-usb/src/commands.rs`): `set_param`, `set_model`, `set_model_pair`, `set_enabled`, `set_routing`, `write_preset` (a whole document) and the controller assignments all change **the edit buffer only**. Only `save_preset`, `rename_preset` and the IR, favourite and settings writes touch flash. This is how `load()`/`set_params()` can avoid writing the user's slots at all.
- **Whole-preset write** (`write_preset`, op 21): tonepush paces it on completion notification 20 and verifies by reading back; fretwire's CLI calls op 21 "the operation that has produced every device lockup on record". So prefer `set_model` plus `set_param` edits for `load()`, and use whole-document writes only to restore the original state.
- **MIDI CC assignment from code** (tonepush `assign_parameter` with source ordinal 8 = MIDI, then `set_assign_cc` op 64, then `set_assign_range` ops 65 and 66 for Min/Max): a CC template can be built in the edit buffer over USB. Narrowing Min/Max lets 128 CC steps cover a smaller range, so effective resolution is finer than 1/128.
- **Fragility** (tonepush `PROTOCOL.md`): handshake once per session; drain the IN endpoint between operations; wait for notification 20 on deferred ops; never call USB reset (the pedal drops off the bus). A wedged pedal needs the **9V adapter pulled**; a USB replug is not enough. The device keeps sequence state across connections, so tonepush claims, releases and claims again.
- **Line 6 EULA**: see `docs/notes/line6-eula-flags.md` (research, [T009](../../board/TASKS.md#t009 "Pull the Native Pilot's Guide automation details and fretwire's…")). This spike adds no protocol work and uses only the published results of fretwire and tonepush.

## Implications for `StompEngine` (input to [T001](../../board/TASKS.md#t001 "Freeze engine interface v0"); [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") builds it)

- **Bridge, not one-shot CLI calls.** The CLIs open a session, handshake and close on every command. That costs time per call and adds handshake churn, which is the main cause of wedges. Plan: one long-lived Rust helper (JSON lines over stdin/stdout) that holds one tonepush `hx-usb` session, sends a keepalive, and exposes `info`, `read_preset`, `select_preset`, `set_param`, `set_model`, `set_enabled`, `set_routing`, `write_preset` (restore only) and the assignment ops. Pin it to a git revision: `hx-proto` is on crates.io but `hx-usb` is not. tonepush is the first choice because its tested platform is ours; fretwire is the second, for cross-checking opcodes.
- **`load(preset)`**: select a base (the human-named scratch slot, or the current preset), then edit the buffer: `set_model`, `set_param` and `set_enabled` per slot, and `route 0` to USB 5/6. Never `save`.
- **`close()`**: on open, store the current setlist and index and `backup` the edit buffer document to `cache/stomp/`. On close, `select_preset(original)`. If the buffer was dirty on open, `write_preset(backup)`. Then read back and compare.
- **Parameter values**: the protocol takes values in the parameter's own units, not 0..1. The engine maps from the catalog's normalized range using the catalog import ([T005](../../board/TASKS.md#t005 "catalog-import: locate the user's HX Edit install on each OS, read…")).
- **MIDI route (Windows)**: the preset agent writes templates as `.hlx` with controller assignments. The human imports them into scratch slots once, through HX Edit. A Program Change selects a template; CCs sweep values. A model change means another template, which is a large `load_penalty_s`. A model with no template raises `UnsupportedModelChange`.

## Proposal to the lead: contract `engine-interface.md` (not changed here)

1. Allow `load()` on the Stomp MIDI route to raise `UnsupportedModelChange`, as the contract already allows for Native-by-automation. **Breaks:** `search` (it must group by chain for this engine too; it already must for Native) and `benchmark` (engine-smoke must load a template that exists).
2. Standardise two `Capabilities.extras` keys: `control_route` (`"usb"` | `"midi-cc"`) and `param_steps` (`None` = continuous, `128` = CC). **Breaks:** nobody, since it is additive. `search` should read `param_steps` for its step size.
3. Parallel Path A/B renders (`parallelism=2`) need either a `render_many(dry, presets)` method or two engines sharing one device. Defer until [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") measures whether it works. **Would break:** `search` and `learning`.
4. `close()`: "restores the pedal's original preset" should also say "including unsaved edit-buffer changes present at open". **Breaks:** `benchmark` (smoke check step 4 should compare the document, not only the index). *Implemented in StompEngine (restore_report, value signature with assignments in any order). `engine_smoke.py` now fails when `restore_report.match` is false; that is the skill's step 4, and no contract text was changed.*
5. (hardware, 2026-10-14) `preset-schema.md` assumes firmware 3.80. This pedal is on **3.50**. **Affects:** `preset` (device_version of generated files; models newer than 3.50 must be refused for this pedal), `benchmark` (Gate 3 "parity for firmware 3.80" would read "3.50" unless the human updates the pedal).
6. (hardware) On the Stomp, the Input block's "USB 5/6" is `@input` = **6**. **Affects:** `preset` (a reamp template should write 6; `known.hlx` has 1 = Main L/R). The engine routes the edit buffer to 6 itself, so nothing breaks today.
7. (hardware) v0 `load()` cannot add or remove blocks: a preset whose layout differs from the pedal's current one raises `UnsupportedModelChange`, the same as Native's automation route. **Affects:** `search` (group trials by chain layout, as for Native).

## Proposed DECISIONS.md entry (for the lead, after the hardware batch)

> Stomp control route: macOS and Linux use USB interface 0 through tonepush's `hx-usb`, pinned by git revision, behind a long-lived helper (fretwire is the cross-check). Windows uses the MIDI CC fallback on HX Edit-imported templates unless H9 shows interface 0 can be claimed with the Line 6 driver present. HX Edit must be closed during Stomp runs. The engine edits the edit buffer only and never saves. Why: [T003](../../board/TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change") evidence. Who: lead, on stomp-engine's report.

## Hardware batch for the human (one sitting, about 30 to 40 min on this Mac)

Preconditions: the Stomp is connected by USB with its 9V adapter; **HX Edit is quit**; the human **names one scratch preset slot** (for example `32C`) that may be overwritten in the edit buffer. Nothing in this list saves to flash. Never run `save`, `rename`, `restore-all`, `ir-*`, `set-setting`, or tonepush's `--ignored` device tests (some of them write slots). If the pedal stops answering, **pull the 9V adapter for a few seconds**; a USB replug does not clear it.

Set up once (stomp-engine can do this before the session):
`T=<scratch>/tonepush/target/release/tonepush`; `F=<scratch>/fretwire/target/release/fretwire`; `P="<venv>/bin/python scripts/stomp_spike_probe.py"`.

| # | Step | Command | Pass when |
|---|---|---|---|
| H1 | Enumeration | `system_profiler SPUSBDataType \| grep -A8 -i "HX Stomp"`; `$F detect`; `$T list` | VID 0x0E41, PID 0x4246 seen by all three |
| H2 | Read-only session, firmware, original state | `$T info`; `$T preset > cache/stomp/spike/orig.txt`; `$T chain >> cache/stomp/spike/orig.txt`; `$T backup cache/stomp/spike/orig_editbuffer.bin` | Firmware reads 3.80; the original preset index is recorded |
| H3a | HX Edit exclusivity | Open HX Edit, wait for it to connect, run `$T info` | Fails with the claim error (expected) |
| H3b | Release | Quit HX Edit, run `$T info` | Succeeds |
| H3c | Audio and MIDI alongside control | `$P devices` (expect a "HX Stomp" audio device and MIDI ports; `stomp_48k_ok: true`). Then start `$P render --tag coexist --seconds 8` and, while it plays, run `$T info` in a second terminal | No dropout or error in either; the WAV is not silent |
| H4 | Go to scratch, route input | `$T select <scratch>`; `$T route 0 "USB 5/6"`; `$T chain` | Input shows USB 5/6 |
| H5 | **Render confirms a parameter change** | `$P render --tag p0`; `$T set <amp block> Drive 2.0`; `$P render --tag p1`; `$T set <amp block> Drive 8.0`; `$P render --tag p2`; `$P compare p0 p2` | `compare` prints CHANGED; `chain` shows the values |
| H6 | **Render confirms a model change** | `$T model <amp block> "<another amp name>"`; `$P render --tag m1`; `$P compare p2 m1` | CHANGED; time the `model` command (gives `load_penalty_s`) |
| H7 | **Render confirms a block change** | `$T enable <amp block> off`; `$P render --tag b1`; `$P compare m1 b1`; `$T enable <amp block> on` | CHANGED |
| H8 | **MIDI CC fallback on one parameter** | `$T assign-param <amp block> <drive idx> 8`; `$T assign-cc <amp block> <drive idx> 20`; then `$P cc --cc 20 --value 0`, `$P render --tag c0`, `$P cc --cc 20 --value 127`, `$P render --tag c1`, `$P compare c0 c1`; `$T assignments <amp block> 6` | CHANGED; the assignment reads back as MIDI CC 20. Check first that CC 20 is not reserved in the 3.80 manual's MIDI table; use the MIDI channel set in the Stomp's global MIDI settings. |
| H8b | Program Change (Windows-style template select) | `$P pc --program <original index>` and confirm the pedal moves; `$P pc --program <scratch index>` | The pedal shows that preset |
| H9 | Restore | `$T select <original index>`; `$T chain` and diff against `orig.txt`. If the original buffer had unsaved edits, `$T restore cache/stomp/spike/orig_editbuffer.bin` | Same preset and values as H2 |
| H10 | Linux (only if a Linux box is available) | fretwire `install-udev`, then `detect`, `pull`, `goto`, `set`, `swap`, `bypass` on the scratch slot, plus `$P render` with the rate pinned at 48 kHz | Same pass rules as H5 to H7 |
| H11 | Windows (only if a Windows PC is available, with the Line 6 driver and HX Edit installed) | tonepush MSI, then `tonepush list` and `tonepush info`. **Do not install Zadig or WinUSB over the Line 6 driver.** Then H8 and H8b through the Line 6 MIDI port | Records whether interface 0 can be claimed; the MIDI path works |

When H1 to H9 pass on macOS, stomp-engine writes the ledger rows (`smoke` for control, `render-cost` with the measured seconds per render and `load_penalty_s`). Then the engine route is settled for Gate 1, "Stomp on at least one OS", and [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") can start.

## Reproduce the offline half

```sh
S=<scratchpad>; export RUSTUP_HOME=$S/rustup CARGO_HOME=$S/cargo PATH=$S/cargo/bin:$PATH
curl -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal
git clone https://github.com/john-baxter-dev/fretwire $S/fretwire   # fa27c819d00e
git clone https://github.com/crmne/tonepush $S/tonepush             # 53a6a65b5436
(cd $S/fretwire && cargo build --release -p fretwire-cli && cargo test --release -p fretwire-protocol -p fretwire-usb -p fretwire-core -p fretwire-data)
(cd $S/tonepush && cargo build --release -p tonepush-cli && HX_EDIT_RESOURCES=/nonexistent cargo test --release -p hx-proto -p hx-usb -p hx-catalog -p tonepush-cli)
```

## Licences (for reviewer, [T011](../../board/TASKS.md#t011 "First licence-check run over pyproject.toml once the engine spikes…"))

fretwire: MIT OR Apache-2.0. tonepush: MIT. sounddevice: MIT. python-rtmidi: MIT. No dependency was added to `pyproject.toml` in this spike; [T004](../../board/TASKS.md#t004 "Measure USB round-trip latency with an impulse") adds the `stomp` extra (`sounddevice`, `python-rtmidi`) and the pinned Rust helper.

## Sources

- [fretwire](https://github.com/john-baxter-dev/fretwire) (README, `crates/fretwire-usb/src/lib.rs`, `docs/next-steps.md`, `STATUS.md`)
- [tonepush](https://github.com/crmne/tonepush) (README, `PROTOCOL.md`, `crates/hx-usb/src/{lib,commands}.rs`, `crates/hx-usb/tests/device.rs`, `docs/_guide/using-the-cli.md`)
- [nusb docs: Windows needs WinUSB on the interface](https://docs.rs/nusb/latest/nusb/)
- [Line 6 forum: reserved MIDI CCs on HX Stomp](https://line6.com/support/topic/36546-reserved-midi-cc-and-external-midi-controllers-hx-stomp/) (the full list is in the 3.80 manual; check it before H8)
