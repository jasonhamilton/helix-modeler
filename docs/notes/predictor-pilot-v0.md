# predictor-pilot-v0: drive-only blind predictor on renders already on disk ([T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only"))

Owner: `learning`. Run 2026-10-18, 21:46 to 23:09, during the 12-hour unattended run. **This is a measurement only. Nothing ships and nothing is adopted.** It is the first experiment of [predictor-design.md](predictor-design.md) section 6, cut down to drive blocks because the human made distortion the only priority. The split and metrics were written into `docs/DECISIONS.md` before training started.

## Summary

- **The small CNN learns drive models from wet audio alone, but frozen AFx-Rep with a simple trained head does better on the same test.**
  - Test set: 4,337 renders on 29 held-out DI files. Neither method saw these files.
  - CNN: right model first 0.44, true model in the top 5 0.76, right family 0.59. Chance is 0.017, 0.083 and 0.067.
  - AFx-Rep plus logistic regression, trained on the same training files: 0.56, 0.90 and 0.65.
  - The CNN is 0.13 worse on top-1 (95% interval over test files, -0.25 to -0.09).
- **The CNN falls apart when the player changes.** Trained on player P2 only and tested on P1, it gets 0.13 top-1, 0.31 top-5 and 0.20 family. AFx-Rep under the same split still gets 0.42, 0.79 and 0.51. The CNN learned the few DI recordings it was trained on, not the pedals. It saw only 5 to 8 DI excerpts per player.
- **Knobs: the CNN is better than guessing, and a little better than AFx-Rep on Drive.** These are scored with the true model given.
  - Drive within ±0.15: CNN 0.64, AFx-Rep nearest neighbours 0.54, constant training mean 0.35.
  - Tone within ±0.10: CNN 0.47, AFx-Rep 0.49, constant 0.22.
  - With the CNN's own top-1 model (what a user would get): Drive 0.50 and Tone 0.41.
- **The uncertainty is not usable yet.**
  - The raw Drive error bars are about 3 times too narrow: the 68% interval covers only 36% of cases.
  - The scale fitted on the one calibration DI overcorrects, so the 68% interval then covers 94% of cases. The median error bar becomes ±0.35, which is most of the knob.
  - Tone after scaling is close to nominal: 0.67 at 68% and 0.90 at 95%.
  - Model confidence is useful for ranking calls. Calls made with confidence 0.5 or more (17% of clips) are right 85 to 90% of the time.
- **Speed.** 0.036 s per 10 s clip on one CPU thread. Training took 23 minutes on MPS.

**What this means.** For drives, a from-scratch CNN on today's data is not the starting guess to build on. Frozen AFx-Rep with a trained head is better and much more robust to a new player. The proposal (not adopted) is to make AFx-Rep plus heads the next pilot arm. A from-scratch network needs many more DI recordings, which means new renders (see "For the human").

## 1. Data

All of it is cached Native 3.82 single drive-block renders, block-library-v1.2, on Guitar-TECHS DI. There were no new renders.

| Source | Renders used | Notes |
|---|---|---|
| drive-specialist calibration | 12,000 (60 models × 25 settings × 8 DI excerpts) | The same 25 settings on every DI |
| helix-trust-v2, train split only | 211 | Drive-category candidates only |
| gain-trust-v1 (fit and test) | 1,592 | Drive-category candidates |
| gain-trust-v1b | 837 | Drive-category candidates |
| **Total** | **14,640** (9 digitally silent renders dropped) | 60 drive models, 15 families |

**Not read:**
- synthetic-v1, -v2 and -v3 (sealed);
- the helix-trust-v2 test split;
- every P3 file, kept untouched as the proposed Gate 3 reserve (there are no P3 drive renders in these sets anyway);
- the human's own DI takes.

The [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…") and [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") caches hold fingerprints and AFx embeddings, not audio, so there was nothing in them to train on.

**Labels.**
- Knob position is CC/127.
- Which knob is "Drive" and which is "Tone" comes from `cache/drive-maps/knob_sensitivity.json` ([T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models")). 8 of the 60 models have no tone knob; their Tone is not scored.
- Families come from `cache/drive-maps/drive-families-v1.json` ([T180](../../board/TASKS.md#t180 "Circuit families from the measured fingerprints, circuit names as…"), 15 families).

## 2. Split (by whole DI file)

| Side | DI files | Renders | What is in it |
|---|---|---|---|
| Train | 38 | 8,711 | 5 drive-specialist DIs (P1 `A`, P1 `Vibrato`, P2 `Set1_m7`, P2 `PinchHarmonics`, P2 `PalmMute`), plus trust-set files |
| Calibration | 5 | 1,583 | drive-specialist P1 `Bb`, plus every 10th other train file |
| Test | 29 | 4,337 | drive-specialist P2 `Drop3_m7b5` and P1 `allsinglenotes`, plus every file holding gain-trust test renders except `PalmMute` |

- No file is on two sides.
- Both players appear on both sides, so the main test is held-out *recordings* of known players.
- A held-out *player* is the separate cross-player arm: train on P2 train files only (5,426 renders), test on the P1 test files (2,313).

The test has two parts, reported separately:
- **drive-specialist test (3,000):** a new DI, but the same 25 settings the model saw on other DIs. Knob numbers here are flattered.
- **trust test (1,337):** a new DI and settings never seen in training. This is the honest knob test.

## 3. Model

- **Input.** Log-mel, 128 bands, 30 Hz to 20 kHz, n_fft 2048, 20 ms hop, per-clip RMS normalisation, and each crop's mean log level removed. Level is therefore not a cue.
- **Network.** 6 convolution blocks (32, 64, 128, 128, 256, 256 channels), mean and standard-deviation pooling over time, and a 256-d embedding. 1.49 M parameters.
- **Heads.**
  - Family: 15 classes.
  - Model, hierarchical: p(model) = p(family) × p(model | family), with a softmax over the family's members.
  - Drive and Tone: a mean and a log-sigma, conditioned on a model embedding, trained with a Gaussian likelihood.
- **Training.** Random 6 s crops, a ±3 dB spectral tilt as augmentation, AdamW, one-cycle schedule, batch 64, 30 epochs. 187 samples/s on MPS, 1,399 s.
- **Calibration, on the calibration split only.**
  - Temperature for the model posterior: 2.6.
  - One sigma scale per knob, so the 68% interval covers 68% there: Drive ×5.40, Tone ×1.75.
- **Speed probe first.** The projected total was 48 minutes, well under the 2-hour stop.

## 4. Results on the held-out DI files

The CNN is scored on a 6 s centre crop, the same window AFx-Rep sees. The full 10 s clip is slightly better (top-1 0.47) and a 3 s crop is worse (0.38).

### 4.1 Model and family

| | Model top-1 | Model top-5 | Family top-1 |
|---|---|---|---|
| Chance, uniform | 0.017 | 0.083 | 0.067 |
| Chance, most frequent class in train | 0.012 | 0.058 | 0.170 |
| **CNN, all test** | **0.436** | **0.764** | **0.588** (top-3: 0.881) |
| CNN, drive-specialist test | 0.468 | 0.805 | 0.627 |
| CNN, trust test | 0.362 | 0.674 | 0.500 |
| CNN, P1 / P2 test files | 0.420 / 0.453 | 0.768 / 0.760 | 0.583 / 0.593 |
| **AFx-Rep + logistic regression, same split (paired)** | **0.563** | **0.899** | **0.648** |
| AFx-Rep, trust test | 0.552 | 0.882 | 0.624 |
| AFx-Rep, nearest model centroid | 0.408 | 0.757 | 0.316 |
| Average of CNN and AFx-Rep probabilities | 0.608 | 0.903 | |
| **Cross-player: CNN (train P2, test P1)** | **0.130** | **0.310** | **0.200** |
| **Cross-player: AFx-Rep + logistic regression** | **0.421** | **0.786** | **0.511** |

- **CNN minus AFx-Rep, bootstrapped over the 29 test files:**
  - top-1: -0.128 (95% CI -0.254 to -0.093);
  - top-5: -0.135 (95% CI -0.198 to -0.107).
- **Earlier AFx-Rep numbers, for context.** These are not the same targets, so they are not paired comparisons.
  - [T175](../../board/TASKS.md#t175 "Different-guitar test on drive"): right model first among candidate types 0.67 on another guitar (chance 0.30); rank-1 among all candidates 0.13 (chance 0.012).
  - [T181](distortion-step4.md): AFx-Rep gallery from the output alone, top-1 0.44 (other excerpts) and 0.39 (other players), top-5 0.84 and 0.76, over 54 models.
  - [T183](nodi-drive-e2e.md): AFx-Rep top-1 11 of 24 blind (0.46).
  - Today's paired AFx-Rep head (0.56 / 0.90) is higher than those because it learns from 8,711 labelled training clips, not one gallery render per setting.
- **By family (CNN, top-1).**
  - Well identified: the single-model families F04 (0.98), F15 (0.97) and F14 (0.85), and F03 (0.70), F09 (0.74) and F10 (0.71).
  - Weakest: F06 (0.12, 9 models, screamer-like) and F11 (0.21).
  - The commonest confusions are between the two bass DI models, Ratatouille and Vermin, Vital Boost and Kinky Boost, and Scream-type and Teemah. They are the expected near-twins.
- **Confidence.**
  - Temperature scaling cuts ECE from 0.23 to 0.11.
  - On test the model is now slightly under-confident: mean confidence 0.33 against accuracy 0.44.
  - Accuracy by confidence band: 0.23 below 0.25, 0.48 at 0.25 to 0.5, 0.84 at 0.5 to 0.75, 0.90 above 0.75.

### 4.2 Drive and Tone

| | Drive within ±0.15 | Tone within ±0.10 | Drive MAE | Tone MAE |
|---|---|---|---|---|
| Constant (training mean) | 0.35 | 0.22 | 0.222 | 0.222 |
| **CNN, true model given** | **0.636** | **0.468** | 0.137 | 0.144 |
| CNN, true model given, trust test only | 0.559 | 0.422 | | |
| CNN, its own top-1 model | 0.503 | 0.407 | | |
| CNN, top-1 model, only when it is right | 0.666 | 0.537 | | |
| AFx-Rep, 5 nearest training clips of the true model | 0.543 (trust 0.524) | 0.489 (trust 0.432) | 0.169 | 0.138 |
| CNN cross-player, true model given | 0.455 | 0.334 | | |

"True model given" isolates the knob head. In use the model is not given, so the "own top-1" row is the realistic one. For comparison, [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…") fingerprint inversion with the same DI reached Drive 0.69 and Tone 0.46 (a different test, and it needs the DI).

### 4.3 Calibration of the knob uncertainty (true model given)

| | Drive 68% interval | Drive 95% interval | Tone 68% interval | Tone 95% interval |
|---|---|---|---|---|
| Raw sigma | 0.36 | 0.62 | 0.42 | 0.72 |
| After the calibration scale | 0.94 | 0.99 | 0.67 | 0.90 |
| After scaling, CNN's own top-1 model | 0.82 | 0.93 | 0.54 | 0.77 |

- **Raw sigmas are over-confident.** The network fits its training DIs (training loss 0.06 at the end) and believes its knob reads too much.
- **The Drive scale was fitted on one DI file and overcorrects.** P1 `Bb` is harder than the test average, so the scaled Drive bars (median ±0.35) are too wide to guide a search.
- **Tone after scaling is near nominal.**
- **Fix for next time (proposal):** calibrate on several whole files, a few per player, and per family rather than one global scale.

## 5. Limits of this pilot

- **Very few DI recordings.** 5 drive-specialist excerpts give 86% of the training clips. That is the likely reason for the cross-player collapse. More of the same 25 settings would not fix it; more DI recordings would.
- **Settings are biased.** The drive-specialist test repeats the training settings on another DI, and the trust sets cluster near their true setting. The trust-test row is the fair knob number.
- **There are only two players** once P3 is reserved. There is no truly new guitar in the test; the cross-player arm is the closest stand-in.
- **These are clean single drive blocks on a DI**, with no amp, cab or separation artifacts. The real use (a drive inside a song) is harder.
- **One training seed, no ensemble.** The 95% intervals cover file-to-file variation, not seed variation.

## 6. Model card stub (`predictor-pilot-v0`, local only, not published)

| Field | Value |
|---|---|
| Checkpoints | `cache/models/predictor-pilot-v0/all/model.pt` (main) and `xplayer/model.pt` (cross-player arm), 6 MB each, gitignored |
| Task | Drive model (60), family (15), Drive and Tone knob position from about 6 s of wet single-drive audio, no dry signal |
| Data | 14,640 Helix Native 3.82 renders (block-library-v1.2) made on the owner's licence, on Guitar-TECHS DI (CC BY 4.0, Guitar-TECHS authors; attribution required), players P1 and P2 only. P3 is unused. No sealed set, no helix-trust-v2 test split, no user audio |
| Licence note | Trained on Helix Native renders under the owner's licence. The human found local scripted use and training on own-licence renders acceptable (2026-10-15, `line6-eula-flags.md`). **Local use only; not published.** Sharing the weights needs a separate ruling, or a retrain from Stomp renders. The CNN uses no third-party weights. The AFx-Rep baseline adds AFx-Rep's terms (weights tagged Apache-2.0, trained on non-commercial datasets; see `afx-rep.md`) |
| Evaluation | Section 4. Held-out DI files: top-1 0.436, top-5 0.764, family 0.588, Drive ±0.15 0.636, Tone ±0.10 0.468 (true model given). Cross-player: top-1 0.130 |
| Known failure | Does not transfer to an unseen player. Raw knob sigmas are over-confident. Weak on family F06 |
| Ledger | `benchmark / - / predictor-pilot-v0`, 2026-10-18 rows by `learning` |
| Status | Measurement. Not adopted; not used by search |

The same card is in `models/CARDS.md`.

## 7. Files

- Code:
  - `src/tonematch/learning/drive_pilot.py`: index, split, front end, network;
  - `scripts/predictor_pilot_v0.py`: probe, features, train and eval;
  - `scripts/predictor_pilot_v0_afx.py`: AFx-Rep baseline;
  - `scripts/predictor_pilot_v0_paired.py`: paired bootstrap and AFx-Rep cross-player.
- Results:
  - `cache/models/predictor-pilot-v0/{all,xplayer}/result.json`;
  - `afx/result.json`;
  - `paired.json`;
  - `probe.json`;
  - `index.json` (the per-render split).
- **Feature cache.** It was 1.9 GB and was deleted when free disk fell to 7.0 GB because other agents were rendering. Every later step rebuilt the features in RAM, which takes about 40 s.
- **AFx-Rep embeddings.** `afx/emb.npy`, 30 MB. They were computed on the CPU (the device check fell back), 2,068 s with 2 threads.

## 8. For the human

These are proposals; none is adopted.

1. **Which predictor to build next for drives.**
   - (a) **Frozen AFx-Rep plus trained heads** for model, family and knobs. Recommended: it is better on every model metric here and survives a player change.
   - (b) Keep the from-scratch CNN, but only after the data has many more DI recordings.
   - (c) Average the two: top-1 0.61, but top-5 is no better than AFx-Rep alone.
   - AFx-Rep's training-data terms apply to (a) and (c) if weights are ever shared.
2. **An overnight render for the drive predictor.** This is the `predictor-single-v1` drive part. It is needed for (b) and would also help (a).
   - About 9,000 renders: 61 models × 150 random settings, spread over many P1/P2 files rather than 8 excerpts, plus a pickup tilt.
   - At the 6 to 8 renders/s recorded in predictor-design.md, that is about 20 to 25 minutes on Native. It is short; the cost is DI variety, not render time. The rate must still be re-measured, with other agents rendering at the same time.
   - Disk: store log-mel features only (about 1.2 GB), not audio. Free disk was 7.0 to 9.7 GB during this run because of other agents' renders, so even that needs the 8 GB floor watched.
3. **The Gate 3 DI reserve.** Holding out all of P3 (proposal R1) leaves only two players, and this pilot shows that the player is the hard part. Use P3 for a true unseen-player test inside Gate 3, as reserved. Keep it out of every dev run, as was done here.
4. **Publishing.** Nothing here is publishable as is: it is Native-derived and local only.
