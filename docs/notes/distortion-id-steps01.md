# Distortion identification, steps 0 and 1: results ([T171](../../board/TASKS.md#t171 "Distortion identification steps 0 and 1"))

Owner: `tone-metrics`. Run 2026-10-18. Plan: [distortion-identification.md](distortion-identification.md) section 8, steps 0 and 1, approved by the human on 2026-10-18 ([DECISIONS](../DECISIONS.md), "approved the rating session, steps 0 and 1 ...").

**Nothing here is adopted.** These are measurements against the note's placeholder checks. No score, method, class list or bar changes. Zero Native renders, no song audio read.

## What was run

- **Step 0.** Re-read the drive fingerprints already on disk: probe-v1, coarse-v1 grid, Native 3.82, `cache/probe/fingerprints/block-library-v1.2/`. That is 60 drive models (Bitcrusher left out, as in the search pool) and 348 stored settings: the default plus the two main knobs at their corners and centre. No new renders.
- **Step 1.** Built nine made-up distortion circuits whose true insides are known. Played the note's test signals through them and checked each measuring method against the truth. Only numpy and scipy were used.
- **Code.**
  - `src/tonematch/metrics/distortion_id.py`: the measuring methods.
  - `src/tonematch/metrics/distortion_synth.py`: the made-up circuits.
  - `src/tonematch/metrics/distortion_id_eval.py`: the harness that scores the methods against the truth.
  - `src/tonematch/metrics/drive_fp_mining.py`: the Step 0 analysis.
  - `scripts/distortion_id_steps01.py`: runs both steps. Step 0 takes 4 s and Step 1 about 110 s on one core.
  - `tests/test_distortion_id.py`: 12 tests.
- **Outputs** (local only, gitignored): `reports/local/distortion-id/results_step0.json` and `results_step1.json`, plus SVG figures (listed at the end).

Words used below:

- **Knee:** the input level at which the pedal's output first falls 1 dB short of a straight line. This is the 1 dB compression point, measured with a 1 kHz tone, in dBFS (dB below digital full scale).
- **Pre-filter / post-filter:** the tone shaping before and after the clipping stage.
- **W-H (Wiener-Hammerstein):** the textbook three-stage model: pre-filter, then a clipper with no memory, then post-filter.

## Step 0: what the stored fingerprints can and cannot tell apart

### Telling models apart from a different knob setting

The test: hide one stored setting of one model. Ask which of the 60 models is nearest, using what remains (the hidden model keeps its other settings). The rank of the true model is recorded.

- The "default" setting is left out of the main numbers. For most models it sits almost exactly on the centre setting, and for 5 models it is identical to it. Keeping it would make the test easy.
- Brackets are 95% intervals from resampling models, not settings.

| Features used (all standardised) | How many | Top-1 | Top-5 | Top-1 without level features |
|---|---|---|---|---|
| Chance | | 1/60 = 0.017 | 5/60 = 0.083 | |
| The six scalars named in the note: gain at 1 kHz, THD at 440 Hz, tilt, ratio, knee, symmetry (plus a "no knee found" flag) | 7 | **0.34** [0.24, 0.44] | 0.56 [0.45, 0.65] | 0.30 |
| Every stored scalar: static curve, transfer curve, harmonics at 110/440/1760 Hz, intermodulation, dynamics, the three DI clips | 55 | **0.59** [0.50, 0.68] | 0.82 [0.76, 0.88] | 0.59 |
| Stored curves: linear response at 4 levels, harmonic levels and odd/even balance per level, DI band response | 419 | **0.75** [0.67, 0.83] | 0.96 [0.93, 0.98] | 0.74 |

(288 held-out settings in each row. With the duplicate defaults left in, the numbers are 0.48, 0.74 and 0.88.)

- **Placeholder check: "model id from a different knob setting at top-1 well above 1/61". Measured: met.**
  - The note's six scalars give 0.34, 20 times chance.
  - All stored scalars give 0.59.
  - The stored curves give 0.75.
- **Not because of loudness.** Helix sets each model's output level its own way, so loudness alone could tell models apart. Dropping every level-carrying feature barely changes the result (last column).
- **Limits of this test.**
  - The held-out setting is a corner of a coarse grid. Its neighbours, which differ in one knob, are still in the gallery. A random knob setting would be harder.
  - Every setting was rendered with the same probe and the same three DI clips, so this is the friendliest case (dry known, the same signals).

### Do the fingerprints group models by circuit?

- **Only weakly.** Each model's settings were averaged and clustered. The clusters were compared with three label sets from [distortion-circuits.md](distortion-circuits.md):
  - its 17 confusable groups (each of the six "O" singletons gets its own label);
  - its family column (boost, overdrive, distortion, fuzz, other);
  - a six-way circuit class: soft overdrive, hard distortion, asymmetric overdrive, fuzz, boost, and bass/other. This six-way class is my own reading of the clipping column, **not an approved class list**.
- The comparison uses the adjusted Rand index (ARI). 1 means identical groupings; 0 means no better than random grouping of the same sizes. The "random" line below is the 95th percentile of 1,000 random groupings.

| Clustering (Ward) | vs 17 groups | vs family | vs 6 classes |
|---|---|---|---|
| 6 clusters, model averages | 0.05 (random 0.03) | 0.06 (0.05) | **0.19** (0.05) |
| 17 clusters, model averages | 0.08 (0.04) | 0.10 (0.04) | 0.15 (0.05) |
| 17 clusters, centre setting only | **0.16** (0.04) | 0.08 (0.03) | 0.09 (0.04) |

- **The agreement is above random but small.** The six clusters mostly sort by how much gain a model has, not by circuit:
  - one large high-gain cluster holds the RAT, DS-1, Big Muff, Fuzz Face, MT-2 and HM-2 types together (27 models);
  - a low-gain cluster holds the Tube Screamer and Klon types plus the Rangemaster;
  - a third mixes mid-gain overdrives, the two clean boosts and seven of the fifteen DM4 legacy models.
- **When the nearest model is wrong** (119 of 288 held-out settings), it is in the same confusable group 20% of the time, against 5% by chance, and in the same six-way class 46% of the time, against 20% by chance. So circuit similarity shows, but weakly.
- **Same pedal, two entries.**
  - Close (within the nearest 5% of all model pairs): Triangle Fuzz and Fuzz Pi, Valve Driver and Tube Drive, Heavy Dist and Killer Z, Vermin and Classic Dist. Scream 808 and Screamer are at the 12th percentile.
  - Not close: Arbitrator and Facial Fuzz (median distance); Tycoctavia and Octave Fuzz (79th percentile); Vermin and Ratatouille (91st percentile; Ratatouille is far from every model).
  - Two possible reasons, neither checked: the legacy and current entries were fingerprinted at knob corners with different names, so "corresponding" settings differ; and Ratatouille's catalog lists knobs from several sub-models.

### Why the stored fingerprints are a weak base for circuit identification

The probe's lowest stepped-sine level is -45 dBFS, and many drives are already compressing there:

- **175 of 348** stored drive settings (50%) have their 440 Hz knee within 1.5 dB of that floor.
- **175 of 348** have no knee in the two-slope fit at all.

So for half the settings the stored knee is the bottom of the measuring range, not a measurement. The only nonlinear readings are at 110, 440 and 1760 Hz, and there is no pre/post split. This is the gap the note's section 2 describes, now measured.

### Where the [T164](../../board/TASKS.md#t164 "Play-along single-block match") top matches sit

The [T164](../../board/TASKS.md#t164 "Play-along single-block match") drive-only song search ranked these near the top: DM4 Sub Oct Fuzz, Horizon Drive, Obsidian 7000, Teemah and ZeroAmp Bass DI. It was stopped after the human heard them as "nothing alike" (logs in `cache/matches/Mother-0-68s-drive/`).

| Model | Group (circuits note) | Six-way class (mine) | Cluster of 17 | Cluster of 6 |
|---|---|---|---|---|
| DM4 Sub Oct Fuzz | J (octave) | fuzz | 14 | 5 (high gain) |
| Horizon Drive | A (Tube Screamer line) | soft overdrive | 5 | 2 (low gain) |
| Obsidian 7000 | L (bass drive) | other | 13 | 4 |
| Teemah | B (transparent) | soft overdrive | 4 | 2 (low gain) |
| ZeroAmp Bass DI | L (bass drive) | other | 1 | 1 |

- **The five matches fall in 5 of the 17 clusters and 4 of the 6.**
- **Their ten pairwise distances sit between the 26th and 74th percentile of all model pairs.** In other words, they are as far apart as models picked at random.
- So the old score's top picks were not homing in on any one kind of circuit. That fits the human hearing them as nothing alike.
- **The target's likely class is unknown.** No dry signal exists for the song guitar, so no fingerprint can be taken from it. Anything else would be a guess from listening, which is the human's call.

## Step 1: do the measuring methods recover known truth?

### The nine made-up circuits

Circuit values are illustrative, taken from the classic schematics quoted in the circuits note. Knees were placed between -48 and -16 dBFS, the range the Native drives show. The clipping parts run at 8 times the sample rate so that aliasing (high harmonics folding back into the audio band) does not spoil the truth. There is no added noise, so these are best-case numbers.

| Name | What it is | Knee (truth) |
|---|---|---|
| tanh | smooth soft clipper, no filters | -22.9 dBFS |
| hard_clip | flat-top clipper, no filters | -16.1 |
| asym_diode | diode clipper, 1 diode up and 2 down (exact diode equation) | -27.0 |
| ts_like | Tube Screamer-like: bass-cut gain stage (720 Hz) into a soft diode clipper, 723 Hz and 6 kHz low-pass after | -37.6 |
| rat_open / rat_dark | RAT-like: the RAT gain-leg network (60 Hz and 1.5 kHz corners, op-amp bandwidth limit), hard diodes to ground, then a low-pass at 16 kHz or 1.5 kHz | -47.6 |
| big_muff | two diode clippers with a filter between them, then the mid-scoop tone stack (723/1809 Hz blend). Only roughly W-H. | (two knees) |
| bias_drift | coupling capacitor in front of an asymmetric clipper; the bias shifts with loud playing and recovers with a 50 ms time constant; output capacitor removes DC. Not W-H (it has memory). | -26.6 (measured) |
| klon_blend | the non-W-H control: half clean signal plus a band-limited clipped path, Klon-style | -30.8 (measured) |

The test signals are the ones proposed for dfp-v1 in the note:

- 6 s synchronized sweeps at -60, -42, -30, -24, -18, -12, -6 and 0 dBFS, plus 1.5 s sweeps for comparison;
- steady tones at 1 kHz from -54 to -4 dBFS in 5 dB steps, plus the probe-v1 grid (-45 to -3 in 6 dB steps);
- the same 5 dB staircase at 23 third-octave frequencies (50 Hz to 8 kHz), for the split done with steady tones;
- a 220 Hz descending staircase (hysteresis check) and a -30/-6/-30 dBFS burst (bias settling).

### Results against the note's placeholder checks

| Check (placeholder) | Measured | Verdict |
|---|---|---|
| **Static curve error < 0.5 dB** | At the tone level about 10 dB above the measured knee, with the post-filter removed using the split's own estimate: worst-case 95th-percentile error 0.49 dB (ts_like; largest single point 0.59 dB). Every other W-H system is 0.14 dB or better. At the top level (-4 dBFS) this fails on four of six systems: 0.56 to 7.3 dB, because 21 harmonics of a 1 kHz tone cannot draw a near-square curve. Without removing the post-filter (what probe-v1's transfer curve does) the error is 5 to 7 dB on the two circuits with a strong low-pass after the clipper (ts_like, rat_dark). | **Met at a moderate drive level, not at full drive.** The level has to be chosen. |
| **Pre/post filter error < 1 dB where the response is above -40 dB** | Six W-H systems, 100% of the band identifiable. From sweeps: pre-filter worst 0.96 dB (ts_like, 95th percentile 0.83), post-filter worst 0.23 dB. From steady tones: pre-filter worst 0.39 dB, post-filter worst 0.08 dB. Big Muff, which is not W-H: only 10% of frequencies identifiable from sweeps, 2 to 3 dB off its outer filters; 78% from steady tones, 1.5 dB at the 95th percentile. | **Met for every W-H system.** The split did not fail. |
| **Knee within 1.5 dB** | With the 5 dB tone staircase: errors 0.3 to 1.0 dB on all six W-H systems (hard clip worst). With the probe-v1 grid (-45 to -3 dBFS, 6 dB steps): the RAT is 3.9 dB off because its knee (-47.6) is below the lowest level. With the eight sweep levels: ts_like 2.6 dB and RAT 8.5 dB off, because the levels jump from -60 to -42. From a fitted soft-clip curve (Rapp): within 0.7 dB except ts_like (2.1 dB). | **Met with the 5 dB tone staircase.** Not met with the probe-v1 grid or the sweep levels for high-gain circuits. |
| **Alignment residual large for a system that is not W-H** | Fundamental-only residual: W-H systems 0.002 to 0.05 dB; clean blend 0.40 (flagged, 8 times larger); bias drift 0.025 and Big Muff 0.05 (not flagged). Odd-harmonic residual from sweeps (3rd and 5th): W-H systems at most 0.10 dB; bias drift 1.53, Big Muff 0.73, clean blend 0.60 (all three flagged; the smallest is 6 times the largest W-H value). From steady tones the odd-harmonic residual does not separate them (W-H up to 0.20, non-W-H 0.10 to 0.33). | **Met with the sweep-based harmonic residual.** Not met with the fundamental alone or with steady tones. |

### The other methods

- **Sweep timing rule** (order k arrives L ln k seconds early, Farina and Novak; L is the sweep's time constant): **confirmed.**
  - Measured peaks were within 0.008 ms of the prediction for the memoryless clippers. For filtered circuits they were within 0.25 ms; the difference is the filters' own delay.
  - Orders 9 and 10 are 89.6 ms apart with a 6 s sweep and 21.1 ms with a 1.5 s sweep. The note's 91 and 23 ms are slightly changed because the synchronized sweep rounds L.
  - However, on these circuits the 1.5 s sweep separated orders 1 to 9 as well as the 6 s sweep did. The 95th-percentile error was 0.25 dB or less for every order except the hard clip (about 1.5 dB at both lengths, from aliasing and sharp nulls). The reason is that their filters ring for well under 20 ms. **Whether Native needs 6 s depends on how long its drives ring, which is not yet measured.**
- **Harmonic extraction from sweeps:** median error 0.01 dB. The 95th percentile is 0.2 dB or less, except the hard clip (1.3 to 1.5 dB).
- **Small-signal total response** (the quietest sweep, -60 dBFS): within 0.05 dB on every W-H system. Their knees are all at -48 dBFS or above. For a Native drive that already compresses at -60 dBFS this would not hold. The Step 0 floor finding says some may.
- **Symmetry:**
  - **Asymmetric diode, recovered exactly:** even-harmonic share -22.8 dB (truth -22.8), ratio of positive to negative ceiling 0.51 (truth 0.51), DC -0.24 of the fundamental (truth -0.24).
  - **Capacitor-coupled bias-drift case:** the output DC is lost (0.00), as the note warned. The even-harmonic share (-6 dB) and the ceiling ratio (2.57) still show the asymmetry.
  - **Symmetric circuits:** even share below -94 dB.
- **Hardness** (Rapp exponent: about 1 is soft, large is hard), fitted to the recovered curve against the true curve:
  - tanh 2.3 against 2.3;
  - asymmetric diode 3.5 / 4.6 against 3.2 / 4.9;
  - ts_like 2.7 against 3.5;
  - RAT 3.6 against 3.6;
  - hard clip 22 against 200. Both read as "very hard"; the recovered curve is limited by the 21 harmonics.
- **Memory flags:**
  - The burst test flags the bias-drift circuit: its asymmetry changes by 1.0 and settles in 0.15 s, about three time constants.
  - It flags none of the W-H circuits. The largest change there was 0.15 on the RAT, from its output capacitor, under the 0.2 flag line.
  - The up-versus-down staircase did **not** flag the bias drift: 0.15 dB, under the note's 0.5 dB line. For this case, the burst is the test that works.

### What I had to change from the note's derivation (flagged for approval, not adopted)

1. **The pre/post split aligns the fundamental only.** The note aligns every harmonic order. With all five orders the pre-filter error was up to 5 dB (95th percentile), because the higher harmonics' level curves have deep nulls where they change sign, and those nulls interpolate badly. The fundamental alone, re-fitted three times against a shared reference curve, gave the numbers above. The harmonics are then used only for the W-H consistency check.
2. **A shift is accepted only where at least half of that frequency's points overlap the reference.** The first version favoured "no shift" and misread the Tube Screamer's bass by up to 19 dB.
3. **The static curve needs the post-filter removed in phase as well as level.** I rebuilt the phase as minimum phase from the split's level estimate, continuing the last octave's slope above 16 kHz. This is accurate for minimum-phase filters, which all nine circuits are. Whether Native's filters are minimum phase is unknown.
4. **Tone-fitting fix.** Harmonics at or above half the sample rate are now excluded from the tone fit. At 8 kHz the 5th harmonic folded onto the fundamental and halved it.

### What this does and does not show

- **Shows:** with a known dry signal and a true three-stage circuit, the note's measuring methods recover:
  - the pre-filter and the post-filter separately, within 1 dB;
  - the clipping curve within 0.5 dB at a moderate drive level;
  - the knee within 1.5 dB, with the 5 dB tone staircase;
  - symmetry and hardness;
  - and the sweep-based harmonic residual tells the three non-W-H circuits from the W-H ones.
- **Does not show:**
  - anything about Native (these are noise-free toy circuits of my choosing);
  - real op-amp feedback clippers, whose pre-filter changes with Drive;
  - noise;
  - any blind (no dry signal) case.
  The thresholds I used (the 1 dB identifiability width, the 0.2 burst flag, the -40 dB harmonic floor) were chosen while building this, so they are tuned to these nine circuits.

## For the human to decide (proposals only)

1. **The sweep level set.**
   - The dfp-v1 sweep levels (-60, -42, -30, ...) jump 18 dB at the bottom. High-gain circuits have their knee in that gap, so the sweep-only knee fails (RAT 8.5 dB) and the Big Muff split becomes unidentifiable.
   - Option: sweeps every 6 dB from -66 dBFS, if Native's noise floor allows (unchecked).
   - Option: keep the 5 dB tone staircase as the knee source.
2. **Where to read the static curve.**
   - Option: at about 10 dB above the measured knee (met the 0.5 dB check).
   - Option: from a lower tone frequency, which gives more harmonics below Nyquist (not tested).
   - Not at full drive.
3. **The split method:** fundamental-only alignment plus a separate harmonic consistency check (above), instead of the note's all-orders alignment.
4. **Which non-W-H test to trust.** The fundamental residual alone misses memory and cascaded clippers. The sweep-based odd-harmonic residual caught all three non-W-H circuits here.
5. **Sweep length.** 1.5 s was enough on these circuits. Before choosing 6 s or 1.5 s, measure on a Native pilot (step 2) how long Native drives ring.
6. **Step 0's message.**
   - The stored summaries tell models apart from a neighbouring setting (top-1 0.34 to 0.75 against 0.017 by chance), but they barely group models by circuit (agreement 0.05 to 0.19).
   - Half the stored knees sit on the probe's -45 dBFS floor.
   - If the human wants circuit-level identification, this supports running the new probe (step 2, a 54-render pilot). Approving that is the human's call.
7. **The six-way circuit class** in `drive_fp_mining.LABELS` is my reading and needs the human's approval before anyone uses it.

## Figures (local, `reports/local/distortion-id/`)

- `step0_model_distance.svg`: distance between the 60 models, ordered by confusable group.
- `step0_pca_class6.svg`: models on the first two principal components (49% of the variance), coloured by the provisional class.
- `step0_loso_rank_hist.svg`: rank of the true model when one setting is held out.
- `step1_split_<system>.svg`: true and recovered pre- and post-filters, from sweeps and from steady tones.
- `step1_curve_<system>.svg`: true and recovered clipping curve at about 10 dB above the knee.
- `step1_residuals.svg`: the W-H residuals per circuit.

Re-run: `nice -n 10 .venv/bin/python scripts/distortion_id_steps01.py --step all`.
