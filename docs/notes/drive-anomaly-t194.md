# Drive anomaly on the old test renders ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly"))

Owner: learning. Date: 2026-10-20. This is a measurement only. Nothing is adopted. No new Native renders were made.

## The question

In [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"), the predictor trained with the 105k new renders (`v2data`) got better at the model and at Tone everywhere. On the OLD T186/T189 test renders, though, it got worse at Drive:
- within player, Drive within ±0.15 fell from 0.64–0.67 to 0.52–0.53;
- P1→P2, it fell from 0.47 (logistic) / 0.37 (MLP) to 0.27–0.29.

On the new renders, Drive improved.

## Answer in one paragraph

**The two render pipelines feed the drive block at different input levels.**
- **Old renders** (drive-specialist, gain-trust, helix-trust-v2) loudness-normalise every DI excerpt to -26.7 dBFS loud-frame level before Native (`helix_trust_set.load_excerpt`, used by `drive_calibrate.load_dis`).
- **New renders** (`drive_dataset_render.py`) feed the DI at its raw file level (`read_mono_file`), with no gain.

The raw Guitar-TECHS excerpts are a median of 7 dB (P1) and 8.6 dB (P2) quieter than the reference. The spread runs from 12 dB louder to 20 dB quieter. A drive distorts according to input level and the Drive knob together. So the same Drive label sounds different in the two sets: old clips sound "more driven" for their label.

A head trained mostly on new renders therefore reads old clips too high: +0.09 within player, +0.26 P1→P2. The head trained on old renders reads new clips too low by the mirror amount (-0.09 to -0.12). The label definition itself is the same in both.

**P1→P2 also has a second effect, present before any new data.** Even the old-data head over-reads P2 by +0.20 at matched level. The new data narrow the intervals (half-width 0.27 → 0.11), and the calibration pool (P1 files only) cannot see the P2 bias. That narrowing is why coverage collapses there.

## Evidence

Numbers below come from the saved [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") main checkpoints (MLP unless stated), re-scored on the same clips. Arms marked "retrain" come from `scripts/drive_anomaly_t194.py` (one MLP seed, calibration on the calibration files only, same recipe for every arm). Its `v1` and `v2` arms reproduce [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render"): 0.667 / 0.534 Drive ±0.15 on the old test.

### 1. Signed Drive error (predicted minus true, true model)

| Test clips | Old-data head | New-data head (v2data) |
|---|---|---|
| Old test, drive-specialist (3,000 clips) | -0.013 | **+0.098** |
| Old test, gain-trust v1 / v1b | -0.07 / +0.04 | +0.04 / +0.12 |
| New renders, new held-out files | **-0.123** | -0.019 |
| New renders, [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") test files | **-0.094** | -0.005 |

Each head is unbiased on its own level and biased in opposite directions on the other. This is a level shift, not noise.

### 2. Dose-response inside the new renders alone

The new renders were bucketed by how far each DI excerpt sat from the old reference level. "Gain to reference" is the gain the old pipeline would have applied.

| Gain to reference | Clips | Old-data head bias | New-data head bias |
|---|---|---|---|
| ≤ 1 dB | 864 | -0.017 | **+0.063** (mid Drive: +0.096) |
| 1–3 dB | 2,100 | -0.025 | +0.046 |
| 5–7 dB | 7,532 | -0.100 | +0.001 |
| 9–11 dB | 6,494 | -0.117 | -0.029 |
| ≥ 13 dB | 4,047 | -0.176 | -0.059 |

- The slope is about -0.01 Drive per dB for the old-data head and -0.008 per dB for the new-data head.
- New clips whose DI happens to be near the reference level reproduce the old-test bias. Mid Drive (0.3–0.7): +0.096 here against +0.125 on the old drive-specialist test; the old-data head reads -0.001 in both.
- This uses only one pipeline. It leaves about 0.03 at most for every other pipeline difference combined: pre-roll, excerpt choice, settings design, model mix.

### 3. The causal check: retrain on existing features

| Retrain arm (within player) | New train clips | Old test Drive ±0.15 | Old test bias | Old test cov 68 | New held-out Drive ±0.15 | New held-out bias | Model top-1 old / new |
|---|---|---|---|---|---|---|---|
| v1 (old only) | 0 | 0.667 | -0.013 | 0.72 | 0.478 | -0.123 | 0.72 / 0.55 |
| v2 (all new) | 105,181 | 0.534 | +0.094 | 0.52 | 0.731 | -0.019 | 0.89 / 0.93 |
| v2rand: random new subset | 15,494 | 0.588 | +0.067 | 0.62 | 0.673 | -0.027 | 0.87 / 0.87 |
| v2lev: new clips with DI within 3 dB of reference | 15,494 | **0.688** | -0.005 | 0.77 | 0.486 | -0.137 | 0.87 / 0.72 |
| v2cond: all new, input gain given to the head | 105,181 | **0.703** | +0.002 | 0.64 | **0.771** | -0.005 | 0.91 / 0.93 |

- **Same size, different level.** v2lev and v2rand have the same number of new clips. Choosing them by input level removes the old-test bias and gives the best Drive on old renders. It also makes the new renders worse, with the mirror-image bias.
- **Telling the head the input level** (v2cond: the gain in dB / 10, appended to each crop embedding; 0 for old clips) fixes both sides at once:
  - old test 0.534 → 0.703;
  - new held-out 0.731 → 0.771;
  - bias flat across every level bucket (-0.019 to +0.003);
  - model top-1 unchanged or better.
- Its 68% coverage is 0.64–0.68, slightly narrow with the calibration-files-only recipe used here.

### 4. P1→P2

| Retrain arm (train P1, test P2) | Old test Drive ±0.15 | Old test bias | Half-width 68 | Cov 68 | New held-out Drive ±0.15 |
|---|---|---|---|---|---|
| v1 | 0.373 | +0.198 | 0.27 | 0.61 | 0.530 |
| v2 | 0.288 | +0.263 | 0.11 | 0.24 | 0.650 |
| v2lev | 0.339 | +0.245 | 0.17 | 0.40 | 0.570 |
| v2cond | 0.323 | +0.223 | 0.11 | 0.25 | 0.639 |

- A head trained on P1 over-reads P2 by about +0.20 even when both are at the same normalised level. The level mismatch adds about +0.06 on top. Level conditioning removes about two thirds of that +0.06 (to +0.22) but not the +0.20.
- The new data make the head confident (half-width 0.27 → 0.11). The calibration pool holds only P1 files, so nothing inflates the intervals for a new player. That is the coverage collapse (0.61 → 0.24).
- Inside the new renders, the P2 bias is again level-dependent: +0.25 for P2 clips within 1 dB of reference, about 0 for the quietest.
- The old P2 test is 74% one recording: 1,500 of 2,024 clips come from `P2_chords/directinput_Drop3_m7b5`, with 25 fixed settings per model. Its cross-player figure is close to a single-file result.

## The candidate causes, one by one

| # | Cause | Verdict | Evidence |
|---|---|---|---|
| 1 | Different Drive label definitions | **Ruled out** | Both sets use the same knob per model (`drive_pilot._knob_names`, [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") map) and the same label, CC/127 on the integer grid. Both map CC to value with `synthetic_v1.grid_value` over identical free-parameter ranges (54 of 54 models identical). The only difference is the setting range: old random settings use CC 6–121 plus the default; new use 0–127. The old range lies inside the new. |
| 2 | Level/Output varied in old renders | **Ruled out** | In the old drive-specialist test, the bias is flat across the Output knob: +0.105 / +0.093 / +0.092 / +0.127 by quarter (950 clips with a level knob). Old clips with Output near the default still show +0.074. AFx-Rep peak-normalises its input, so a linear output gain cancels. |
| 3 | Peak normalisation or level in the embedding input | **Not the cause as stated** | Both pipelines use the same `afxrep.Embedder` (peak-normalised). The level that matters sits *before* the drive block (cause 6), where no normalisation can undo it. |
| 4 | Distribution shift, 25 fixed settings | **Minor; inflates the old baseline** | Biases at the Drive extremes (regression to the middle) appear in both sets and for both heads. The level effect is an extra +0.10 to +0.14 shift in every Drive bin. The old-data head benefits on the old test from seeing those exact 25 settings in training (0.70 on drive-specialist clips against 0.52 and 0.67 on the gain-trust v1 and v1b clips, whose settings were not in training), so its old-test baseline is optimistic. |
| 5 | Calibration-pool mixing | **A consequence, not the cause** | The v2data pool is mostly new renders; its Drive scale (0.90) gives near-nominal coverage on new renders (0.70–0.74). Removing the mean bias alone lifts old-test coverage from 0.53 to 0.65. The rest is the narrower intervals the larger dataset earns in-domain. |
| 6 | DI excerpt / pre-roll differ | **Root cause: the DI level.** Pre-roll not measured | Old excerpts are loudness-normalised to -26.7 dBFS; new ones are raw (median 7–9 dB quieter; table 2). Pre-roll differs: old 0.1 s of silence (`NativeRenderPool` default), new 3 s (`make_engine(144000)`). Measuring its effect would need renders. The within-pipeline level test leaves at most about 0.03 for it and every other difference combined. |

Also checked: the six fuzzes excluded from the new data ([T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…")) are not where the drop is. Their old-test bias is +0.05; the other models' is +0.10.

## Proposed fixes (for the human; nothing adopted)

1. **Harmonise the input level in the data generator.** Every training render should have a known DI level. Two ways; the human chooses:
   - **(a) Normalise.** Normalise each excerpt to -26.7 dBFS loud level, as the drive-specialist, gain-trust and synthetic sets do. This is a small change in `drive_dataset_render.py` (call `helix_trust_set.load_excerpt`'s gain rule). It keeps every set comparable, but a real user's DI is not at that level.
   - **(b) Record the gain.** Keep raw or randomised levels, and record the input gain per clip as a label.

   The 150k drive-dataset-v1 features can be kept as they are: the per-clip gain is now computed (`cache/drive-anomaly-t194/input_levels.json`, from the DI files alone).
2. **Give the predictor the DI input level** whenever the DI is available (the matcher has the user's DI). Measured above: within player, Drive ±0.15 is 0.70 on old renders and 0.77 on new, with no level bias. No re-render or relabelling is needed.
   - **Without a DI** (no-DI mode), Drive and input level are not separable from the sound alone. The head should then report Drive "at the reference input level", or predict the input gain jointly with Drive.
3. **Do not mix level conventions in one head without a level input.** v2 against v2lev shows that either convention alone is biased on the other.
4. **For a new player**, level does not fix the +0.20 P2 bias. The options:
   - more players and DI styles (as [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") already said);
   - a calibration pool that includes held-out-player folds, so intervals widen for unseen players;
   - reporting the P2 result per file, since one recording dominates the old P2 test.

Relabelling the new Drive values to "equivalent at -26.7 dBFS" would need a per-model input-gain law and is not recommended over option 2.

## Files

- `scripts/drive_anomaly_t194.py`: the retrain measurement (5 arms × main and P1→P2). Output: `cache/drive-anomaly-t194/result.json`, `run.log`.
- `cache/drive-anomaly-t194/input_levels.json`: per new clip, the raw loud-frame dBFS, peak, and gain to the old reference.
- `cache/drive-anomaly-t194/analysis/`: the re-scoring of the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") checkpoints (`preds.npz`, `meta.json`) and the analysis scripts behind tables 1–2 and the cause table.
- The [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") summaries and v2data checkpoints were not touched.

Load: one process, 2 torch threads, under `LoadGuard`. Total fit time 41 min. `proc_check` was clean before and after.
