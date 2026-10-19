# Predictor-led matching: design for review ([T165](../../board/TASKS.md#t165 "Predictor-led matching design"))

Owner: `learning`. Written 2026-10-18. Design only: no training run, no new Native renders, no sealed labels read (the labels of `synthetic-v1`, `-v2` and `-v3` were not opened; for every other set only manifests, DI paths and render counts were read). Basis: the human's decision of 2026-10-18 (DECISIONS, last entry: the end-user match runs on one ordinary computer, a predictor narrows the choices, a small render budget verifies; heavy compute is for training only), [no-di-survey-estimation.md](no-di-survey-estimation.md) options 1 and 2, [no-di-survey-metrics.md](no-di-survey-metrics.md), [seed-quality.md](seed-quality.md), Gate 3 in `board/GATES.md`, `docs/PLAN.md` "How it learns" and [T030](../../board/TASKS.md#t030 "Data generator; predictor v0").

## Summary

- **What it does.** A network looks at 3 to 10 s of wet guitar audio and returns the block category, a calibrated top-k list of Helix models, and settings for each listed model with error bars. Single block first (Gate 3), chains later.
- **What the user pays.** Under 1 s of predictor time on a laptop CPU (to be measured), then 10 to 50 Native renders on one host to verify and refine. That is about 15 to 55 s of wall time, against 1,719 renders and 23 minutes for the song match and about 300 renders per block in Gate 2.
- **Starting model.** A small CNN trained from scratch on log-mel input, with a category → model hierarchy and a settings head conditioned on the model. Frozen AFx-Rep heads are an optional comparison only.
- **First experiment.** Train once on renders already on disk (about 25,000 single-block renders on 92 Guitar-TECHS files of players P1 and P2), test on held-out DI files, and compare with presence-v1. **About 45 to 75 minutes on this Mac (MPS)**, run after [T164](../../board/TASKS.md#t164 "Play-along single-block match") frees the CPU. No new renders.
- **Two things need a human or a lead ruling first.**
  1. Disk: 9.3 GB is free. That is not enough to store the new training audio.
  2. A Gate 3 DI reserve has to be fixed before any training. I propose all of player P3.

## 1. Task definition

**Input.** Mono wet audio at 48 kHz, 3 to 10 s, loudness-normalised to the project reference (-26.7 dBFS active RMS). The gain that normalisation removes is kept as one scalar input. No dry signal of the performance is given, ever (Gate 3).

**Output: one `Seeds` object per clip.** (This is a proposed shape. No contract file defines Seeds; if search wants one, it goes to the lead as a proposal.)

| Field | Meaning |
|---|---|
| `category` | probability over {drive, amp, cab, comp, eq, mod, delay, reverb, none}, plus `other` (wah, filter, pitch, synth, volume) once those are trained |
| `candidates` | top-k models, each with calibrated `p`, settings `mu` and `sigma` per free knob (normalised 0 to 1 over the template's CC range), and the model's confusion cluster (section 7) |
| `entropy`, `ood` | entropy of the model posterior and an out-of-distribution score (energy score or ensemble disagreement) |
| `budget` | number of renders recommended for verify/refine (section 5) |

The existing search hook `hybrid.choose_models_hybrid(..., predictor_seed={model: {param: value}})` ([T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment")) accepts the top-k `mu` as is. No contract changes.

**Knobs it does not predict.** Output and Level knobs cannot be identified without the dry signal: the stem's level is arbitrary. They are set by the level match (`level_db`), not predicted. Drive amount and the player's input level are confounded (a hot pickup looks like more Drive). The settings head reports this as a wide `sigma` on gain knobs, not as a point estimate.

**Scope order.**
1. Single block on a DI, every category: Gate 3.
2. Amp plus cab, because songs nearly always have both.
3. Chains of up to about 6 blocks, as category presence per slot plus model plus settings (section 3.4).

## 2. Target user cost

Measured costs on this Mac (M1 Pro, 10 cores, 32 GB):

| Quantity | Value | Source |
|---|---|---|
| One render of a 10 s clip, one host, match path | 0.685 s | `cache/matches/Mother-0-68s/result.json` `t_render_probe_s` |
| One loss evaluation, same clip | 0.159 s | same file, `t_loss_probe_s` |
| Template load penalty per model change | 0.016 to 0.38 s | `cache/native/cost.json` `load_penalty_s` |
| Native host open | 6.7 to 7.6 s, once per session; about 1.8 GB RAM per host | `render_summary.json` `open_s`; `mem_mb_per_host` |
| Stomp (hardware only) | 2.7 s per render plus 0.66 s per model change | `docs/notes/synthetic-benchmark-design.md` |
| Song match today | 1,719 renders, 1,360 s | ledger [2026-10-18T19:00:26](../../ledger/runs.csv?plain=1#L1207 "search · native · mother-0-68s · provisional_loss_best = 3.2559"), dataset `mother-0-68s` |

**Proposed budget per block.**
- Predictor: under 1 s on a laptop CPU. A 1.5 M-parameter CNN costs roughly 0.3 GFLOP per 10 s clip; with 4 crops and a 3-member ensemble that should be a few hundred ms. This is an estimate; the pilot measures it with `torch.set_num_threads(1)`.
- Verify/refine: 10 to 50 renders on one host, at (0.685 + 0.159) s, about 0.84 s each, plus up to 5 model changes at most 0.4 s each.

| Case | Renders | Native, one host (incl. 7 s host open) | Stomp |
|---|---|---|---|
| Confident (top-1 p ≥ 0.8) | about 10 | about 17 s | about 30 s |
| Typical (top-3 to top-5 shortlist) | about 25 | about 30 s | about 75 s |
| Uncertain (flat posterior) | 50 cap, flagged "low confidence" | about 52 s | about 2.5 min |

**Why 10 to 50.**
- At least one render per shortlisted model (k ≤ 5) is needed to check it at all.
- Refining 2 to 4 free knobs locally follows Okita and Katayose's budget M = M0 · d^1.5 with M0 = 5 to 20. For d = 3 that gives 26 to 104; we take the low end because the predictor's `sigma` gives the start and the step size.
- 50 renders is 34 times fewer than the song match and 6 times fewer than Gate 2's single-block search. It still fits a Stomp-only user in under 3 minutes.
- More renders do not buy much while the blind score cannot rank candidates ([T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment"), section 5).

Two Native hosts nearly double throughput on this Mac (0.60 → 1.14 to 1.19 renders/s in `cost.json`). The default is still one host, because the user's machine is "ordinary" and each host takes about 1.8 GB.

## 3. Training data

### 3.1 What is on disk now (counts only)

Every candidate render in the trust sets is a labelled single block (model plus knob CC values), so all of them are training examples, not only the "true" ones. Library `block-library-v1.2` unless noted. DI clips are all Guitar-TECHS; players are counted by item or excerpt.

| Set | Renders | Models (by category) | DI excerpts and players | Format, size | Note |
|---|---|---|---|---|---|
| helix-trust-v2 | 3,840 (192 items × 20) | 442: amp 210, cab 87, drive 60, delay 44, comp 17, mod 11, eq 8, reverb 5. Train and test splits are model-disjoint except reverb | 192 excerpts, 104 files (52 train / 52 test, disjoint); P1 78, P2 100, P3 14 | npy float32 10 s, 6.9 GB | presence-v1 was trained and tested on it; consumed, not sealed |
| gain-trust-v1 | 3,264 (192 × 17) | amp 96, drive 60 (fit and test disjoint by model) | 192 excerpts; P1 80, P2 112 | npy, 5.8 GB | consumed ([T144](../../board/TASKS.md#t144 "Gain-stage score term for Gate 2 attempt 3")) |
| gain-trust-v1b | 1,530 (90 × 17) | amp 45, drive 30 | 90; P1 49, P2 40, P3 1 | npy, 2.2 GB | consumed ([T146](../../board/TASKS.md#t146 "Extended rule B (human 2026-10-18, follow-up to T144)")) |
| mod-trust-v1 | 1,239 | mod 37 (944 renders), plus one each of comp/delay/eq/reverb per item | 59; P1 27, P2 31, P3 1 | npy, 2.2 GB | consumed ([T116](../../board/TASKS.md#t116 "Modulation specialist")) |
| drive-specialist calibration | 12,000 (60 × 25 settings × 8 DIs) | drive 60 | 8 excerpts; P1 4, P2 4 | npy, 21 GB | the same 25 settings on every DI |
| mod-specialist calibration | 3,034 (37 × 41 × 2) | mod 37 | 2 excerpts, both P3 `directinput_04` | npy, 5.4 GB | lost if P3 is reserved (3.2) |
| time-specialist | 3,340 (62 model dirs, about 54 each) | delay about 40, mod about 15, reverb 5, other 1 to 2 | 3 excerpts, all P2 | FLAC 24-bit, 2.1 GB, `index.jsonl` per model with knobs | |
| helix-trust-v1 | 2,400 (40 × 20 × 3 DIs) | 43, 5 to 6 per category | 40 items × 3 DIs; P1, P2, P3 | npy, 4.3 GB | `block-library-v1.1` spot templates; leave out for library consistency |
| Dev search sets: synthetic-devfam (96), -devgm (72), -lindev (96), -devmod2 (60) | one target per item | | | 0.4 to 1.7 GB each | keep out of training so they stay usable as predict-then-search dev sets |
| Sealed/consumed gate sets: synthetic-v1, -v2, -v3 (96 items each) | not opened | | | | never used for training |

**Totals (v1.2 only):** 28,247 labelled renders, about 50 GB. 103 of the 104 Guitar-TECHS files appear somewhere. By player: P1 11,253, P2 15,763, P3 3,571 (3,034 of the P3 renders are mod-specialist on a single file).

**Per family (approximate, v1.2, all players):**

| Family | Renders | Notes |
|---|---|---|
| drive | about 14,900 | 60 of 61 library models, rich |
| mod | about 5,200 | about 40 of 56 |
| amp | about 3,300 | 224 models, but most have only about 4 renders outside gain-trust's 141 models |
| delay | about 2,900 | about 44 of 47 |
| reverb | about 720 | only the 5 Native stand-ins of 25 |
| cab | 556 | 87 models, about 6 each |
| comp | about 430 | 17 models |
| eq | about 390 | 8 models |

The data is good enough to learn category everywhere and model identity for drive, and partly for amp, mod and delay. It is too thin for model identity in cab, comp, eq, reverb and most amps.

Biases to remember:
- The trust sets cluster around their "true" settings (near misses move 1 or 2 knobs).
- drive-specialist repeats one grid of settings on only 8 excerpts.
- The time-specialist renders come from one player.

### 3.2 The Gate 3 DI reserve has to be fixed first

Gate 3's dry samples must come from DI material "held out of everything the matcher may render with or train on". Every Guitar-TECHS file has already been rendered somewhere, so the reserve has to be chosen now and the renders on it dropped from all training. **Proposal for `benchmark`, which owns the decision:**

- **R1 (recommended): all 12 P3 files** (`P3_music/directinput_01` to `_12`, about 8 min, 120 indexed 4 s clips). This is an unseen player and probably an unseen guitar (Guitar-TECHS guitar identity per player is not verified). Cost:
  - drop 3,571 existing renders from training;
  - the search's mod-specialist calibration and the measured-seed calibration ([T113](../../board/TASKS.md#t113 "Gate 2 step, raise single-block model accuracy")) used P3 `directinput_04`. Search must re-render them on a non-reserve DI before Gate 3, about 3,400 renders, about 10 min.
- R2: P3 except `directinput_04`. This keeps those calibrations, but player P3 is then seen through one file.
- Either way, `benchmark` may add 4 P1 and 4 P2 whole files as a same-player sub-condition.

Development evaluation never touches the reserve. Dev hold-out is by whole file among P1/P2 (3.3, 3.5).

### 3.3 New renders (after the pilot, and only if it passes)

**Set `predictor-single-v1`.**
- **Models.** Every renderable single-block template in `block-library-v1.2`: 604 models; the 3 time-variant blocks are flagged and kept. Order: amp 224, cab 87, drive 61, mod 56, delay 47, reverb 25, comp 17, eq 8. The 79 `other` models (wah, filter, pitch, synth, volume) come in a second pass.
- **Settings.**
  - Every free knob uniform over its verified CC range: integer CC 0 to 127, the full range rather than the 0.1 to 0.9 grid.
  - 30% of draws from the committed community-preset prior (`chain_prior`, `param_policy`), so common settings are dense.
  - Level and Output knobs randomised but not used as targets.
- **DI.**
  - Random 10 s excerpts from P1/P2 files, outside the Gate 3 reserve and outside the dev-test files.
  - Random DI gain over the range the trust sets used (about +2 to +15 dB).
  - 20% of draws with a pre-render pickup perturbation: ±3 dB tilt, or one resonance peak at 2 to 6 kHz. This is the C4 condition in the metrics survey, used here as augmentation.
- **Counts.**

  | Category | Models × renders per model | Renders |
  |---|---|---|
  | amp | 224 × 150 | 33,600 |
  | drive | 61 × 150 | 9,150 |
  | cab | 87 × 60 | 5,220 |
  | mod | 56 × 100 | 5,600 |
  | delay | 47 × 100 | 4,700 |
  | reverb | 25 × 100 | 2,500 |
  | comp | 17 × 100 | 1,700 |
  | eq | 8 × 150 | 1,200 |
  | none | 2,000 DI-only clips | 2,000 |
  | **Total** | | **about 65,700** |

  Amp and drive get the most data because they are the most confusable and matter most.
- **Time.** Measured bulk throughput on this Mac for 10 s clips, batched by model:

  | Run | Rate |
  |---|---|
  | helix-trust-v2 (2 to 4 hosts) | 11.3 renders/s (3,848 renders in 340 s) |
  | drive calibration (12,000 renders, 26 min) | 7.7 renders/s |
  | gain-trust (1 to 2 hosts) | 5.6 to 7.7 renders/s |

  At 6 to 8 renders/s, 65,700 renders take **2.3 to 3 hours**, overnight with margin. Render it grouped by model (one template load, then all settings across several DIs with `render_many`), resumable, with `proc_check` before and after.
- **Disk (blocking).** 65,700 × 10 s of float32 audio is about 126 GB; as 16-bit FLAC it is about 40 to 60 GB. 9.3 GB is free. Options, for the human:
  1. Store features only: 128-band log-mel, 20 ms hop, fp16, about 128 KB per 10 s clip, about 8.4 GB. Plus 16-bit FLAC for a 10% subset, so the front end can still change.
  2. Free space: for example, drop the 21 GB drive-specialist calibration after its renders are converted to features.
  3. Use an external disk.

  The recommendation is (1) plus (2): features are all the predictor needs, and the audio can be re-rendered from the stored labels.

### 3.4 The Native template limit, and chains

- Native renders only a GUI-captured template, one per chain structure, with 16 knobs and 16 switches. `block-library-v1.2` has one single-block template per model (604), so **every single block is renderable**. A captured template does not exist for an arbitrary multi-block chain.
- Chains can still be made without new captures by **block-serial rendering** ([T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")): block k runs on block k-1's output in its own instance. It nulls at -67 dB above 20 Hz on an 8-block chain after trimming 16 samples per instance. One known exception: Native's combined Amp+Cab block is not the same as an amp block plus a cab block.
- A chain then costs one render per block. Shared prefixes cut that: render a drive once and put several amps on its output. At about 2.5 renders per 4-block chain, 20,000 chains is about 50,000 renders, about 2 hours.
- For the Stomp-only path, chains need either the Stomp itself (2.7 s per render) or a published model (section 7, licence).

### 3.5 Splits

- **By whole DI file, never by excerpt.**
  - Train: P1/P2 files minus dev-test minus calibration.
  - Calibration (10% of train files): temperature scaling and the `sigma` scale.
  - Dev-test.
  - Gate 3 reserve (3.2).
- **Cross-player check:** train on P1 only and test on P2 dev files, then the reverse. This estimates the drop on a new guitar without spending the reserve.
- **For the pilot, the dev-test files are the helix-trust-v2 test file list minus P3,** so the presence-v1 comparison uses identical targets.

## 4. Model options and the recommendation

| Option | For | Against |
|---|---|---|
| **A. Small 2-D CNN on log-mel, from scratch (recommended)** | Comunità et al. reach 86 to 91% on 13 drive types with knob MAE mostly under 0.05 at this scale. In-domain data only. Trains in tens of minutes on MPS. Tens of ms on CPU. No third-party weights or licences | Must learn invariance to DI from our 3 players only |
| B. Frozen AFx-Rep embedding plus heads | Already in `cache/models/afx-rep`, about 0.065 s per 10 s clip; heads train in seconds | Weights tagged Apache-2.0 but trained on non-commercial sets (`afx-rep.md`). Rank-1 0.067 on our proxy (chance 0.0625). Its authors say it fails on guitar tone matching. 1.16 GB to ship |
| C. Transformer (AST-style) on log-mel or CQT | Better at long-range structure (delay repeats, LFO rate) | Needs more data than we have now; slower on CPU |
| D. Hand features (presence-v1) plus gradient boosting | Free; already computed | Already measured at no-DI F1 0.22 to 0.39 on EQ/mod/delay/reverb; this is the baseline to beat |
| E. Open-Amp or Fx-Encoder++ encoders | Contrastive and content-invariant (Open-Amp) | Open-Amp encoder checkpoint and licence unconfirmed; Fx-Encoder++ is CC BY-NC and not tested on guitar |

**Recommended architecture (A).**
- **Front end.** Log-mel, 128 bands, 30 Hz to 20 kHz, n_fft 2048, hop 480 (10 ms), log(x + 1e-5). Plus a linear-frequency band below 300 Hz if cab or EQ need it.
- **Trunk.** 6 conv blocks (3×3, BatchNorm, GELU; channels 32, 64, 128, 128, 256, 256; frequency pooling in every block, time pooling in the first four). Then mean and standard-deviation pooling over time, and an MLP to a 256-d embedding `z`. About 1.5 M parameters.
- **Heads.**
  1. **Category:** softmax over 9 classes.
  2. **Model, hierarchical:** p(model) = p(category) × p(model | category), with a masked softmax inside each category. Errors then stay inside the right category, and top-k is meaningful per category.
  3. **Settings, conditioned:** one MLP g(z, e_model, e_param) → (mu, log sigma) for every knob of the true model, with a Gaussian negative log-likelihood. Trained only on the true model's knobs; evaluated for each top-k candidate at inference.
     - Settings are conditioned on the model because near-synonymous amps reach the same sound at different gains: one shared regressor would average them.
     - Parameter-name embeddings share strength across models that have "Gain", "Tone" or "Mix".
     - A full posterior head (flow matching, MNPE) is option 5 of the survey, for later, if calibration is poor on confusable families.
- **Calibration.**
  - Temperature scaling for category and model on the calibration split.
  - One scale factor on `sigma` so that the 68% intervals cover 68% on calibration.
  - Later, a 3- to 5-member ensemble for the OOD score.
- **Compute.** torch 2.14.1 is installed in `.venv` with MPS available (`torch.backends.mps.is_available() == True`). torchaudio is not installed: use `torch.stft` plus librosa 1.0.0's mel filter matrix. Backend choice goes through `tonematch.gpu` (cuda, rocm, mps, cpu; never assume NVIDIA). sklearn 1.9.1 is there for baselines.

## 5. How the predictor is used at match time

1. **Predict.** Choose the most active 3 to 10 s window of the target. Run the predictor on 4 crops and average. Result: `Seeds`.
2. **Size the budget from uncertainty.**

   | Condition | Shortlist | Renders |
   |---|---|---|
   | Top-1 p ≥ 0.8 and median `sigma` ≤ 0.1 | top-1 only | 1 render at `mu` + up to 9 refine renders |
   | Otherwise | smallest k with cumulative p ≥ 0.9, k ≤ 5 | per model: 1 render at `mu` + 3 to 6 renders sampled from N(mu, sigma²), clipped |
   | Fallback | high OOD score or flat posterior | 50, report marked "low confidence" |

   The thresholds are set on the calibration split, not on any gate set.
3. **Verify, which is the hard part.** Each candidate is rendered on a reference DI, never the target's dry signal. The reference DI is the user's own guitar DI if one exists (Gate 4 style); otherwise a library clip close in register and style (`dry.pick_di`), outside the Gate 3 reserve. The render is then scored against the target with a **blind score**. [T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment") shows the current known-DI score used blind is at chance with or without a correct seed: a seed within 0.05 to 0.13 of the true knobs scored 0.8 to 1.2, the same band as wrong models. **With that score, verify adds nothing, and the predictor's top-1 alone is better than predictor plus search.**

**What is needed from [T161](../../board/TASKS.md#t161 "No-DI step 1"), for `tone-metrics`.**
1. A blind score with a cross-DI invariance ratio R well below 1 (suggested ≤ 0.5) per family, and high candidate-order agreement across DIs, measured on the registered train-split renders.
2. Its ability to separate the predictor's top-k, that is near-synonymous models and same model with knobs off by 0.1 to 0.25. That is the comparison verify makes; candidates in different categories are easy.
3. Its cost per render: it should stay at or under about 0.2 s, like `mrstft_v2`.
4. A difference threshold: how much better a candidate must score to count as better, given DI noise. This decides when verify stops.

**A candidate score the predictor itself supplies.** It is a proposal for `tone-metrics` and must pass the same check before use.
- Embed the target and each candidate render with the trained trunk and score with the distance in `z`, or the divergence between the two predicted posteriors.
- The trunk is trained with DI held out, so `z` is meant to ignore the DI. That is exactly the property R measures.
- Settings can then be refined by **iterative correction**: theta(t+1) = theta(t) + (theta_hat(target) − theta_hat(render(theta(t)))). This cancels the part of the predictor's settings bias that the render reproduces, usually in 2 to 4 renders.
- Weakness: the score shares the predictor's blind spots. It is checked on held-out DIs like any other term, and it never replaces the listening test.

**When verify cannot decide** (the score difference is below threshold), return the predictor's ranking and show the top 2 to 3 as alternatives in the listening pack. Do not spend the remaining budget.

## 6. The cheapest first experiment: `predictor-pilot-v0` (existing renders only)

**Registration first.** The splits, metrics and the continue rule below go into `docs/DECISIONS.md` before any training, after the lead and `benchmark` agree the reserve (3.2).

**Data.**
- v1.2 renders from section 3.1, excluding all P3 files (R1), helix-trust-v1, the dev search sets and the sealed sets. About 24,900 renders.
- Plus the DI clips themselves as `none`, about 500.
- Files split as in 3.5: train is helix-trust-v2's train file list (minus P3), test is its test file list (minus P3). Each other set's render goes to whichever side its DI file is on. This is roughly a 50/50 split, which costs training data but keeps one clean file split for every metric.

**Arms.**
1. **A-all:** the CNN of section 4, trained on every train-file render.
2. **A-strict:** the same CNN trained on helix-trust-v2 train renders only, the data presence-v1 had. This is the strictly comparable arm: test models unseen, as for presence-v1.
3. Optional **B:** AFx-Rep frozen plus the same heads. Embedding about 25,000 clips is about 30 min on MPS; run only if A-all is ambiguous.

**Training.**
- Random 4 to 8 s crops, AdamW, batch 64, about 30 epochs, cosine schedule.
- Features are computed once into RAM: about 3.5 GB at fp16 and 20 ms hop, so nothing new on disk.
- 10% of train files are held back for calibration.

**Metrics (dev-test files only).**
1. **Category.** Per-category one-vs-rest F1 on the helix-trust-v2 test targets minus P3, with presence-v1's transparent-target exclusion. Compared with presence-v1 no-DI **re-scored on the identical subset** from `cache/presence-v1/features-test.jsonl` and its frozen fit, which takes seconds.

   Reference values on the full test set (n = 1,950):

   | EQ | mod | delay | reverb | drive-or-amp | drive | amp | cab | comp |
   |---|---|---|---|---|---|---|---|---|
   | 0.22 | 0.28 | 0.39 | 0.31 | 0.85 | 0.50 | 0.65 | 0.60 | 0.29 |

   Also category top-1 against the majority class (amp, about 0.23) and against uniform (1/9).
2. **Model.** Top-1 and top-5 per category, on test renders whose model has at least 10 training renders, with coverage reported.

   | Category | Chance top-1 | Chance top-5 |
   |---|---|---|
   | drive (61 models) | 0.016 | 0.082 |
   | amp (224) | 0.004 | 0.022 |
   | all 604 models | 0.0017 | 0.0083 |

   Also against a frequency-prior baseline. Cluster top-1 (section 7) is reported next to the plain number.
3. **Settings.** MAE in normalised knob units for the true model, against the training-mean baseline (about 0.25 for uniform knobs). Also the coverage of the 68% and 95% intervals.
4. **Calibration.** ECE for category and model, before and after temperature scaling.
5. **Clip length.** All of the above at 3, 6 and 10 s.
6. **Inference time.** On CPU, with 1 thread.

**Continue rule (proposed; `benchmark` may change it before registration).** Scale to `predictor-single-v1` if A-strict raises the mean F1 over EQ, mod, delay and reverb by at least 0.15 above presence-v1 on the same subset, **and** A-all reaches model top-5 ≥ 0.5 on drive. If not, the learned predictor is not worth the overnight render; report why per family.

**Ledger.** Rows of kind `benchmark`, dataset `predictor-pilot-v0`, one per headline metric. Model card in `models/CARDS.md` (local data, Native-derived, not published).

**Estimated run time on this Mac.**
- Feature pass over about 50 GB of npy/FLAC: 5 to 10 min, IO-bound.
- A-all training: 20 to 40 min on MPS. This assumes 400 to 800 samples/s, which is not measured: the first 2 minutes measure it, and the run stops if the projection exceeds 2 hours.
- A-strict: 5 to 10 min (about 2,000 renders).
- Evaluation: 1 to 2 min.
- **Total: about 45 to 75 minutes**, plus about 30 min for the optional cross-player arm and 30 min for arm B.
- CPU: 2 to 3 data threads under `nice`. Start only after [T164](../../board/TASKS.md#t164 "Play-along single-block match") ends; the load was 47 on 10 cores at 12:13 today. Run `proc_check` before and after.

## 7. Risks

1. **Near-synonymous models.**
   - Helix has channel pairs (A30 Fawn Brt and Nrm, Brit J45 Brt and Nrm), siblings (Brit 2203 and 2204), pedal re-issues in DM4 and HD2 form, and about 87 cabs with similar mics. Some of these may not be identifiable from wet audio at all, because another model at other knobs gives the same sound.
   - Mitigation: build **confusion clusters** from the dev confusion matrix and from render distances (two models whose renders at fitted settings are closer than the DI-to-DI spread are one cluster). Report cluster top-1 next to model top-1. Settings are conditioned on the model so each mode keeps its own knobs.
   - Inside a cluster, choose by verify render if the blind score can separate them. If it cannot, choose by community-preset popularity and show the alternatives.
2. **Synthetic versus real guitars.**
   - Three players (two in training under R1), a few guitars, studio DI levels. Real users bring other pickups, input levels and, for songs, separated stems with bleed and artifacts.
   - Mitigation: random DI gain, pickup perturbation before rendering, the cross-player split, the user's own DI as an extra test later (local only), and separation-artifact augmentation (sep-stress-style residuals) before Gate 5 work.
   - Calibration learned on Guitar-TECHS will be over-confident out of domain; the OOD score and the 50-render cap guard against that.
3. **Licence of Native-derived weights.**
   - The human read the installer EULAs on 2026-10-15 and found no problem with local scripted use and **training on renders made with their own licence** (DECISIONS; `line6-eula-flags.md`).
   - Sharing trained weights with other users (the hardware-only path in `docs/PLAN.md` Legal) is a separate act the record does not explicitly cover. Every model card will say "trained on Helix Native 3.82 renders under the owner's licence; local use; not published", until the human rules on publishing or the model is retrained from Stomp renders.
   - Guitar-TECHS is CC BY 4.0: attribution goes in each card.
   - Arm B would add AFx-Rep's training-data terms. Arm A adds none.
4. **Single block is not a song.** A lone amp on a DI (no cab) sounds unlike any real recording. Gate 3 tests exactly this, so it is right for Gate 3 and not for songs; amp plus cab and chains (3.4) come next.
5. **Disk** (3.3) blocks the overnight set until the human chooses.
6. **The verify step depends on [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)").** Without a trusted blind score, the end-user routine is "predictor only", plus a listening pack of the top 2 to 3.
7. **Parity.** Native reverbs and mods do not null against the Stomp (ledger `parity-spot-fw380`). Predicted settings are Native settings; the existing parity check still applies before export.
8. **Bias of the existing data.** Trust-set near misses and the drive grid repeat settings, so the pilot's settings MAE may be optimistic. The new set uses uniform plus prior draws instead.

## Hand-offs

| To | What |
|---|---|
| `benchmark` | the Gate 3 DI reserve (3.2) and the continue rule (6) |
| `tone-metrics` | the four [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") needs, and the predictor-embedding score proposal (5) |
| `search` | the `Seeds` shape mapped onto `predictor_seed`, and re-rendering the P3-based calibrations under R1 |
| human | disk (3.3) and publishing Native-derived weights (7.3) |
