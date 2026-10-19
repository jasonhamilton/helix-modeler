# predictor-pilot-v1: frozen AFx-Rep plus small trained heads, drive only ([T189](../../board/TASKS.md#t189 "Drive predictor pilot v1"))

Owner: `learning`. Run 2026-10-19, 00:30 to 02:11 local, during the 12-hour unattended run. **This is a measurement only. Nothing ships, nothing is adopted, and no head is picked as "the" model: that is the human's choice.** The design was written into `docs/DECISIONS.md` before any head was fitted. It follows [predictor-pilot-v0](predictor-pilot-v0.md) ([T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk")), which found that frozen AFx-Rep with a logistic regression beat a from-scratch CNN.

## Summary

- **Looking at several parts of the clip is the big gain, not the head.**
  - The new "multi-crop" input embeds three 3.33 s pieces of the 10 s clip and averages the three predictions.
  - On held-out recordings of known players, right model first rises from 0.56 (v0's AFx-Rep head) to **0.68 with logistic regression** and **0.73 with a small MLP**.
  - True model in the top 5: 0.96 to 0.97. Right family: 0.74 to 0.76.
  - On a single crop, the MLP is a little worse than logistic regression (0.53 against 0.56).
  - It is the number of pieces that matters, not their length. One 3.33 s piece scores like the 6 s crop (0.56), two pieces score 0.60 to 0.64, and three score 0.68 to 0.73.
- **Most of that gain is on the easier half of the test.** The drive-specialist test files repeat the 25 settings the heads saw on other recordings. On the trust files, which have new settings as well as a new recording:
  - multi-crop is 0.60 (logistic) and 0.62 (MLP), against 0.55 for one crop;
  - the 95% intervals over files for that gain touch zero (logistic +0.05, from -0.01 to +0.13).
- **A new player is still the hard part, and the two directions differ.**
  - Trained on P2 and tested on P1: 0.42 (v0) becomes 0.48 (logistic, multi-crop) and **0.53** (MLP, multi-crop, 3 seeds 0.51 to 0.55). The MLP gain holds on the trust files (+0.10, 95% interval +0.00 to +0.22).
  - Trained on P1 and tested on P2: nothing helps. All heads score 0.36 to 0.42 against 0.40 for v0. P1 has fewer training clips (3,285 against 5,426).
- **The calibration flaw is fixed for Drive, but the bars are still wide.**
  - The scales are now fitted on 43 files, with each player and each file given equal weight, instead of one file.
  - The Drive 68% interval covers 0.74 to 0.77 (v0: 0.94), with a median half-width of ±0.17 to 0.20 (v0: ±0.35).
  - The 95% interval covers 0.95 to 0.98. Tone covers 0.68 to 0.70 at 68% and 0.93 to 0.94 at 95%.
  - Per-family scales add nothing over one scale per knob.
  - Across players, the MLP's bars are over-confident (Drive 68% covers 0.50 to 0.70). The logistic regression's are close to nominal (0.67 to 0.73).
- **Knobs improve a little and are still not good enough to claim.**
  - With the true model given (main test, multi-crop): Drive within ±0.15 is 0.64 (logistic) and 0.66 (MLP), against 0.64 for the v0 CNN and 0.35 for a constant guess. Tone within ±0.10 is 0.49 and 0.56, against 0.47 for v0 and 0.22 for a constant.
  - With the head's own top-1 model (what a user gets): Drive 0.55 to 0.58, Tone 0.41 to 0.47.
  - On the trust files only, Drive with the true model is about 0.58.

## 1. Data and split

They are identical to [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk"). The script reads [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk")'s per-render index (`cache/models/predictor-pilot-v0/index.json`).

- **Renders.** 14,640 cached single drive-block Helix Native 3.82 renders (block-library-v1.2) on Guitar-TECHS DI:
  - drive-specialist calibration, 12,000;
  - the helix-trust-v2 train split, 211;
  - gain-trust-v1, 1,592;
  - gain-trust-v1b, 837.
- **Embedded.** 14,631 renders: all except the 9 digitally silent ones [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") dropped.
- **No new renders.**
- **Not read:**
  - any P3 file;
  - synthetic-v1, -v2 and -v3 (sealed);
  - the helix-trust-v2 test split;
  - the human's own takes.
- **Split, by whole DI file.** No file is on two sides.

| Arm | Train | Calibration pool | Test |
|---|---|---|---|
| Main (held-out recordings of known players) | 38 files, 8,711 clips, P1 + P2 | out-of-fold over the 38 train files + 5 calibration files (1,583 clips): 43 files, both players | 29 files, 4,337 clips |
| P2 → P1 (held-out player) | 21 P2 files, 5,426 | out-of-fold over those + 2 P2 calibration files: 23 files | 14 P1 test files, 2,313 |
| P1 → P2 (held-out player) | 17 P1 files, 3,285 | out-of-fold over those + 3 P1 calibration files: 20 files | 15 P2 test files, 2,024 |

The test has two kinds of file, reported separately where it matters:
- **drive-specialist files** (1,500 clips each): one in each player's test, 3,000 of the 4,337 test clips in the main arm. They have a new recording but the 25 settings the heads saw on other recordings.
- **trust files:** a new recording and new settings. This is the honest test, especially for knobs.

## 2. What was built

**Features.** Frozen AFx-Rep `mid` embeddings (512-d, peak-normalised, CPU with 2 threads):
- **single:** the 6 s centre crop, reused from [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk"), plus the 1,583 calibration clips [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") had not embedded;
- **multi:** three non-overlapping 3.33 s crops covering the 10 s clip, about 54 minutes for 14,631 clips (64 in a speed probe, the rest in one run);
- **meanemb:** the mean of the three crop embeddings, used as one input;
- **controls:** **mid1** (the middle 3.33 s crop alone) and **multi2** (the first two crops).

**Heads.** Each predicts the family, the model inside its family, and Drive and Tone with a sigma for every model. Code is in `src/tonematch/learning/afx_heads.py`.

- **logreg.**
  - Family probability is the sum of a flat 60-model logistic regression (C = 10, as in [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk")).
  - The model is chosen by a separate logistic regression inside each family, so p(model) = p(family) × p(model | family).
  - Drive and Tone come from a ridge regression per model (alpha = 1, chosen by 3-fold group CV on train files only), with that model's residual standard deviation as the raw sigma.
- **logreg, separate family head.** A family logistic regression of its own instead of the summed posterior. It was tested and is clearly worse (see 3.1), so it is reported only on one crop.
- **MLP.**
  - Two layers of 256 with dropout 0.3, and small embedding noise.
  - A family head and a model head with a softmax inside the family.
  - A Gaussian Drive/Tone head that is fed a learned embedding of the model.
  - Trained with AdamW for 60 epochs on CPU. Three seeds; the seed range is shown.
- **Multi-crop training and prediction.** Each crop is a training example. At test time the log-probabilities, means and variances of the three crops are averaged.

**Calibration** (the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") fix). This step uses only the calibration pool in the table above.
- **Out-of-fold.** The heads are refitted in 5 folds grouped by file, and each train file is predicted by a model that did not see it. The calibration files are predicted by the final model.
- **Weights.** Each player weighs the same, and each file weighs the same within its player.
- **Fitted on that pool:**
  - one temperature for the model posterior (weighted log-likelihood);
  - one sigma scale per knob, set so the weighted 68% interval covers 68% with the true model given;
  - a per-family scale where a family has at least 4 files and 100 clips in the pool.
- **Cross-player arms.** They are calibrated on the training player only, which is the honest case.

## 3. Results

Numbers are on the test files of each arm. "true / own" means Drive or Tone scored with the true model given, then with the head's own top-1 model. A miss is counted when the predicted model has no Tone knob. Coverage is with the true model given and the global scale. MLP rows are the mean of 3 seeds.

### 3.1 Main arm: held-out recordings of known players (29 files, 4,337 clips)

| Head | Pooling | Model top-1 | Top-5 | Family top-1 | Drive ±0.15 true / own | Tone ±0.10 true / own | Drive cover 68 / 95 | Tone cover 68 / 95 | Drive 68% half-width | Model ECE raw / cal |
|---|---|---|---|---|---|---|---|---|---|---|
| [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") reference (flat logreg) | single | 0.56 | 0.90 | 0.65 | | | | | | |
| logreg | single | 0.56 | 0.91 | 0.65 | 0.57 / 0.49 | 0.45 / 0.36 | 0.74 / 0.98 | 0.69 / 0.94 | 0.20 | 0.02 / 0.03 |
| **logreg** | **multi** | **0.68** | **0.96** | **0.74** | 0.64 / 0.55 | 0.49 / 0.41 | 0.77 / 0.98 | 0.68 / 0.94 | 0.19 | 0.04 / 0.02 |
| logreg | meanemb | 0.62 | 0.93 | 0.69 | 0.60 / 0.51 | 0.47 / 0.37 | 0.76 / 0.98 | 0.68 / 0.95 | 0.19 | 0.05 / 0.06 |
| logreg, separate family head | single | 0.45 | 0.80 | 0.52 | 0.57 / 0.48 | 0.45 / 0.34 | 0.74 / 0.98 | 0.69 / 0.94 | 0.20 | 0.07 / 0.03 |
| MLP | single | 0.53 (0.52-0.53) | 0.88 | 0.61 | 0.58 / 0.49 | 0.43 / 0.34 | 0.73 / 0.94 | 0.70 / 0.93 | 0.19 | 0.10 / 0.07 |
| **MLP** | **multi** | **0.73 (0.72-0.74)** | **0.97** | **0.76** | 0.66 / 0.58 | 0.56 / 0.47 | 0.74 / 0.95 | 0.70 / 0.93 | 0.17 | 0.02 / 0.09 |
| MLP | meanemb | 0.61 (0.60-0.61) | 0.93 | 0.67 | 0.63 / 0.52 | 0.46 / 0.36 | 0.72 / 0.94 | 0.67 / 0.91 | 0.17 | 0.05 / 0.08 |
| logreg (control) | mid1, one 3.33 s crop | 0.56 | 0.90 | 0.64 | 0.57 / 0.49 | 0.47 / 0.36 | 0.76 / 0.98 | 0.71 / 0.94 | 0.21 | |
| logreg (control) | multi2, two crops | 0.60 | 0.92 | 0.68 | 0.62 / 0.50 | 0.48 / 0.37 | 0.76 / 0.98 | 0.72 / 0.95 | 0.19 | |
| MLP, 1 seed (control) | mid1 | 0.56 | 0.91 | 0.65 | 0.61 / 0.50 | 0.46 / 0.36 | 0.76 / 0.95 | 0.68 / 0.91 | 0.21 | |
| MLP, 1 seed (control) | multi2 | 0.64 | 0.95 | 0.70 | 0.63 / 0.54 | 0.51 / 0.42 | 0.77 / 0.96 | 0.67 / 0.92 | 0.20 | |

Chance is 0.017 for model top-1, 0.083 for top-5 and 0.067 for family. A constant knob guess gets Drive 0.35 and Tone 0.22.

**Split by kind of test file (main arm, top-1).**

| | Drive-specialist files (3,000, seen settings) | Trust files (1,337, new settings) |
|---|---|---|
| logreg single | 0.57 | 0.55 |
| logreg multi | 0.71 | 0.60 |
| MLP single | 0.53 | 0.53 |
| MLP multi | 0.78 | 0.62 |

- **By player (main arm).** Multi-crop gives top-1 0.69 / 0.67 (logistic) and 0.74 / 0.70 (MLP) on P1 / P2. Drive coverage at 68% is 0.76 to 0.77 for both players.
- **Paired against the [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") reference, top-1, with a 95% bootstrap interval over test files.** The bootstrap is stratified: specialist and trust files are resampled separately, because one specialist file holds 1,500 clips.
  - logreg multi: +0.113 (+0.101 to +0.136). Trust files alone: +0.051 (-0.013 to +0.133).
  - MLP multi, seed 0: +0.158 (+0.148 to +0.186). Trust files alone: +0.045 (-0.010 to +0.183).
  - MLP single: -0.035 (-0.054 to -0.013).

### 3.2 Held-out player

**P2 → P1** (train on P2, calibrate on P2, test on 14 P1 files, 2,313 clips):

| Head | Pooling | Model top-1 | Top-5 | Family | Drive ±0.15 true / own | Tone ±0.10 true / own | Drive cover 68 / 95 | Tone cover 68 / 95 |
|---|---|---|---|---|---|---|---|---|
| [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") reference | single | 0.42 | 0.79 | 0.51 | | | | |
| logreg | single | 0.42 | 0.78 | 0.51 | 0.52 / 0.45 | 0.44 / 0.32 | 0.73 / 0.97 | 0.72 / 0.95 |
| logreg | multi | 0.48 | 0.83 | 0.59 | 0.57 / 0.46 | 0.43 / 0.33 | 0.69 / 0.95 | 0.62 / 0.94 |
| MLP | single | 0.38 (0.38-0.39) | 0.78 | 0.52 | 0.54 / 0.46 | 0.38 / 0.29 | 0.59 / 0.85 | 0.59 / 0.90 |
| **MLP** | **multi** | **0.53 (0.51-0.55)** | **0.87** | **0.62** | 0.62 / 0.50 | 0.46 / 0.38 | 0.70 / 0.93 | 0.52 / 0.85 |

- MLP multi minus the reference: +0.109 (+0.087 to +0.134). On the trust files: +0.098 (+0.004 to +0.216), top-1 0.53.
- logreg multi: +0.062 (+0.049 to +0.077). On the trust files: +0.015 (-0.000 to +0.136).
- The [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") CNN got 0.13 here.

**P1 → P2** (train on P1, calibrate on P1, test on 15 P2 files, 2,024 clips):

| Head | Pooling | Model top-1 | Top-5 | Family | Drive ±0.15 true / own | Tone ±0.10 true / own | Drive cover 68 / 95 | Tone cover 68 / 95 |
|---|---|---|---|---|---|---|---|---|
| T186-style reference | single | 0.40 | 0.77 | 0.49 | | | | |
| logreg | single | 0.40 | 0.76 | 0.49 | 0.46 / 0.42 | 0.43 / 0.33 | 0.67 / 0.96 | 0.72 / 0.98 |
| logreg | multi | 0.41 | 0.79 | 0.53 | 0.47 / 0.43 | 0.49 / 0.36 | 0.73 / 0.97 | 0.72 / 0.94 |
| MLP | single | 0.37 (0.36-0.38) | 0.71 | 0.51 | 0.39 / 0.40 | 0.41 / 0.33 | 0.50 / 0.76 | 0.69 / 0.96 |
| MLP | multi | 0.38 (0.37-0.39) | 0.77 | 0.50 | 0.37 / 0.42 | 0.47 / 0.37 | 0.55 / 0.80 | 0.72 / 0.94 |

- No variant beats the reference reliably in this direction. The paired intervals all include zero, or are below it for the MLP.
- On the trust files, top-1 is only 0.24 to 0.35.
- One reading: the P1 training set is smaller and dominated by two specialist recordings (`A` and `Vibrato`). The heads learn those, not the pedals. The MLP's Drive bars are also badly over-confident here (68% covers 0.50 to 0.55).

### 3.3 Calibration

| | Drive 68% | Drive 95% | Drive median ± | Tone 68% | Tone 95% |
|---|---|---|---|---|---|
| [T186](../../board/TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk") CNN, one-file scale | 0.94 | 0.99 | 0.35 | 0.67 | 0.90 |
| v1 logreg multi, multi-file scale | 0.77 | 0.98 | 0.19 | 0.68 | 0.94 |
| v1 MLP multi, multi-file scale | 0.74 | 0.95 | 0.17 | 0.70 | 0.93 |
| v1 raw sigma (before scaling), logreg / MLP multi | 0.54 / 0.59 | | | 0.49 / 0.58 | |

- **Drive.** Still a little wide at 68% (0.74 to 0.77 against 0.68), and about right at 95%. The pool's out-of-fold heads are trained on 4/5 of the files, so they are a little worse than the final heads and the scale comes out a little large.
- **Per-family scales** were fitted for every family with enough data (all 15 for Drive in the main arm). They do not improve coverage (Drive 0.75 against 0.77 for logreg, 0.75 against 0.74 for the MLP), so one scale per knob is enough with today's data.
- **Model temperature.**
  - logreg is well calibrated already (ECE 0.02 to 0.04).
  - The MLP's out-of-fold temperature (1.5) makes the main-arm MLP under-confident on test (ECE 0.02 raw, 0.09 after). It is the same fold-weakness effect.
  - Cross-player, the temperature helps the MLP (0.20 to 0.09).
- **Trust files only (main arm).** Drive 68% covers 0.70 (logistic) and 0.62 (MLP), so the bars are tighter than nominal where the settings are new.

## 4. Comparison with earlier numbers

| Test | Right model first | In the top 5 (top 8, top 3) | Right family | Drive ok | Tone ok |
|---|---|---|---|---|---|
| **v1 main, logreg / MLP multi-crop** (4,337 clips, 29 held-out files, known players) | 0.68 / 0.73 | 0.96 / 0.97 | 0.74 / 0.76 | 0.55 / 0.58 own model | 0.41 / 0.47 own model |
| **v1 P2→P1, MLP multi** (held-out player) | 0.53 | 0.87 | 0.62 | 0.50 own | 0.38 own |
| **v1 P1→P2, logreg multi** (held-out player) | 0.41 | 0.79 | 0.53 | 0.43 own | 0.36 own |
| v0 CNN, main / cross-player | 0.44 / 0.13 | 0.76 / 0.31 | 0.59 / 0.20 | 0.50 own | 0.41 own |
| v0 AFx-Rep + logreg, main / cross-player | 0.56 / 0.42 | 0.90 / 0.79 | 0.65 / 0.51 | (knn5, true model 0.54) | (0.49) |
| [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") AFx-Rep shortlist, blind (24 targets) | 11/24 = 0.46 | top 8: 18/24 = 0.75 | 12/24 | 4 of 11 | 3 of 9 |
| [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") AFx-Rep order, blind = another player (96 targets) | 37/96 = 0.39 | top 3: 0.61 | 0.54 | 19/37 = 0.51 | 15/34 = 0.44 |
| [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") AFx-Rep order, same player, other excerpt | 0.59 | top 3: 0.73 | 0.68 | 0.65 | 0.46 |

These comparisons are not paired.
- **What [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") and [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") used.** They searched a 54-model gallery with AFx-Rep distance to renders on another DI. They also used 5 s excerpts, Tone tolerances measured per model, and (in [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")) the human's own takes as a third player.
- **The closest match to "same player, another excerpt"** is the v1 main arm. There, v1 heads are about 0.10 to 0.14 above [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")'s 0.59 top-1.
- **The closest match to [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") "blind"** is the v1 cross-player arms, at 0.41 to 0.53 against 0.39.
- **What the knob numbers count.** [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")'s Drive and Tone are out of the targets where the model was right. The v1 "own" numbers are out of all clips, with misses when the model was wrong. The v1 numbers are therefore not flattered, but they are also not the same quantity.
- **Cost.** A v1 prediction costs no renders: about 0.22 s of AFx-Rep on 2 CPU threads for three crops, plus a negligible head. [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") used about 370 renders per match.

## 5. Limits

- **Clean single drive blocks on a DI.** No amp, cab, band or separation artifacts.
- **Only two players.** P3 is reserved. One direction of the player transfer fails.
- **Drive-specialist files dominate both training (86%) and test (69% of the main test).** Their settings repeat, which flatters the main-arm numbers. Look at the trust-file rows first.
- **Seeds.** The MLP has 3 seeds and the logistic regression is deterministic. The bootstrap intervals cover file-to-file variation only.
- **Pooling rule.** The multi-crop pooling averages three crops in fixed positions. Random crops at training time, or more crops, were not tried.

## 6. Model card (`predictor-pilot-v1`, local only, not published)

The same card is in `models/CARDS.md`.

| Field | Value |
|---|---|
| Checkpoints | `cache/models/predictor-pilot-v1/heads_main_{logreg,mlp}_{single,multi,meanemb,mid1,multi2}.pkl`, `heads_main_logregfam_single.pkl` (main arm, seed 0; 0.4 to 1 MB each; heads, calibration, model and family names). Embeddings `emb_single.npy`, `emb_crops.npy` (120 MB). Gitignored |
| Task | Drive block only: family (15), model (60, within family), Drive and Tone knob position with a calibrated sigma, from about 10 s of wet single-drive audio, with no dry signal |
| Architecture | Frozen AFx-Rep (ST-ITO Cnn14, 512-d `mid`), plus either logistic regressions and per-model ridge, or an MLP (2 × 256) |
| Data | 14,640 Helix Native 3.82 single-drive renders (block-library-v1.2) made under the owner's licence, on Guitar-TECHS DI (CC BY 4.0; attribution to the Guitar-TECHS authors), players P1 and P2. Not used: P3, sealed synthetic sets, the helix-trust-v2 test split, user audio |
| Licence note | Trained on Helix Native renders under the owner's licence. The human found local scripted use and training on own-licence renders acceptable (2026-10-15, `line6-eula-flags.md`). **Local use only; not published.** Sharing needs a separate ruling or a retrain from Stomp renders. **AFx-Rep terms:** the weights are tagged Apache-2.0 by their author (HF `csteinmetz1/afx-rep`, revision 6582cb5). Their training data, read from `config.yaml` (see `docs/notes/afx-rep.md`), includes sets with non-commercial or research-only terms: MUSDB18 (educational, non-commercial), MTG-Jamendo (many tracks CC BY-NC), ENST-Drums (research licence) and others whose terms are unknown. Whether those terms carry over to the embedding and to heads trained on it is not settled. Any release needs the human's ruling, or a licence-clean encoder |
| Evaluation | Section 3. Main: top-1 0.68 (logreg multi) / 0.73 (MLP multi), top-5 0.96 / 0.97, family 0.74 / 0.76. Drive ±0.15 (true model) 0.64 / 0.66, Tone ±0.10 0.49 / 0.56. Drive 68% coverage 0.77 / 0.74. Cross-player top-1: P2→P1 0.48 / 0.53, P1→P2 0.41 / 0.38 |
| Known failure | P1→P2 transfer shows no gain over v0. The MLP's knob bars are over-confident for a new player. Most of the multi-crop gain is on repeated settings |
| Ledger | `benchmark / - / predictor-pilot-v1`, 23 rows at 2026-10-19T09:12 UTC, agent `learning` |
| Status | Measurement. Not adopted; not used by search |

## 7. Files

- **Code:**
  - `src/tonematch/learning/afx_heads.py`: heads, pooling, calibration and scoring;
  - `tests/test_afx_heads.py`: 4 tests;
  - `scripts/predictor_pilot_v1_embed.py`: embeddings;
  - `scripts/predictor_pilot_v1.py`: arms, variants and bootstrap.
- **Results** (all in `cache/models/predictor-pilot-v1/`):
  - `result.json`: all variants and seeds;
  - `result_control.json`: the mid1 and multi2 controls;
  - `result_paired.json`: single and multi, seed 0, per-clip hits and the stratified bootstrap;
  - `embed.out` and `heads.out`: logs.
- **Disk.** 124 MB written in total. Free disk stayed at 10 to 13 GB.
- **Load.** The 5-minute load was 3.9 at the start and stayed under 5.2.

## 8. For the human

These are choices; none is taken.

1. **Which head, if any, goes forward as the drive starting guess?**
   - (a) logreg multi-crop: deterministic, best-calibrated knobs, and robust in both player directions.
   - (b) MLP multi-crop: higher top-1 within players and for P2→P1, but over-confident knob bars for a new player, and 3 seeds differ by up to 0.04.
   - (c) Neither yet.
2. **Multi-crop as the standard input** for AFx-Rep in the predictor, and possibly for the AFx-Rep shortlist in `match_block` too. This was not tested in the matcher. It is a proposal for `search`.
3. **More DI recordings matter more than a better head.** P1→P2 shows it. The overnight drive render proposed in v0 (about 9,000 renders spread over many P1/P2 files, features only) is still the next data step.
4. **AFx-Rep licence.** Any shared model built on AFx-Rep inherits the open question about its training data.
