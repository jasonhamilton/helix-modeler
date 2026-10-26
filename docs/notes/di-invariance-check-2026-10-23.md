# DI-invariance check: can any score rank settings when the dry track is not the target's? ([T221](../../board/TASKS.md#t221 "DI-invariance ratio check"))

Owner: `tone-metrics`. Written 2026-10-23. Experiment 3 of `docs/notes/research-dry-signal-2026-10-23.md` section 7. **A measurement, not an admission**: no weight, score or method changes because of it. No new renders; cached renders only; helix-trust-v2 train-split models only (as the [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") and [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") registrations).

## The question

In real use the song's guitar was played by someone else, on another guitar, playing other notes. Every search render goes through *our* dry recording. So for a score to be useful without the target's own dry signal, the distance it reports must be moved less by the dry-recording change than by a wrong setting. Two numbers test that:

- **R, the invariance ratio.** For a target (a setting P played on recording A): the distance from the target to *the same setting played on recording B*, divided by the median distance from the target to a *wrong setting played on recording A*. Below 1, the recording change is smaller than a typical wrong setting and the score can still see the setting through it. At or above 1, the recording change swamps the setting.
- **Order correlation (Spearman rho).** Rank one candidate list against the target twice: with the candidates played on A, and played on B. 1 means the search would walk the same path on either recording; 0 means the order on B says nothing about the order on A.

Two conditions: **same player** (another recording or passage by the same player on the same guitar, so other notes) and **other player** (another player on another guitar). Five scores: the learned effects embedding `embed_afxrep`, the spectrum shape `ltas_tilt`, the level-envelope statistics `envelope`, the crest / flatness / harmonic-to-noise statistics `crest_flatness_hnr`, and the fine-detail known-DI score `mrstft_v2` as the reference that is expected to fail.

## Short answer

- **Only `embed_afxrep` can rank across a dry-recording change, and only between models, not between knob settings.** Other player: R 1.02 overall (so the guitar change equals a typical wrong setting), but 0.82 against other models and 2.8 against knob moves of the same model; order correlation 0.79. Same player: R 0.60, order correlation 0.83. It is the only score whose candidate order survives (0.79 to 0.83; the next best, `mrstft_v2`, is 0.51 to 0.59, and it is not a no-DI score).
- **`ltas_tilt`, `envelope` and `crest_flatness_hnr` cannot rank under DI mismatch.** With another player R is 1.3, 1.9 and 2.0, with order correlations 0.42, 0.26 and 0.30. The two envelope-style scores are worse than the spectrum score: they move more with the playing than with the effect. With the same player `crest_flatness_hnr` sits at R 1.0 and `envelope` at 1.2; still not under 1.
- **`mrstft_v2` fails as expected**: R 2.3 (other player), 2.2 (same player); it is a known-DI score.
- **Modulation (tremolo and vibrato, same player, other passage) is the one place where two scores are clearly invariant**: `embed_afxrep` R 0.05, order 0.99, right setting first 76% of the time; `envelope` R 0.49, order 0.74. A tremolo's level wobble is a strong, note-independent signature; this does not transfer to drives (envelope R 1.4 to 1.8 there).
- **The same player playing other notes is already most of the damage.** For every score the same-player R is within a factor of about 1.5 of the other-player R; for the embedding the gap is 0.60 against 1.02.

This confirms the [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") reading on a second and third data source (helix-trust-v1 across eight block families, mod-specialist renders) and adds the two scores [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") did not test: `envelope` and `crest_flatness_hnr` are not DI-invariant. For the dry-signal question it means: a search that compares renders of a generic dry recording against the song's guitar can at best choose models (with the embedding), never knobs; the knob step needs the target's own dry signal (the human's take), a rebuilt dry that stands in for it, or a predictor that reads the target directly.

## The numbers

n is the number of (target, other recording) pairs; intervals are 95%, from resampling the models (the targets of one model are not independent). "R knob" uses only wrong settings of the same model as the denominator; "R model" only settings of other models. Chance rank-1 is 1 over the number of candidates.

### Pooled over sources (DRIVE + V1; MOD is same-player only and listed below)

| Score | Condition | n | models | R | 95% | R knob | R model | share r < 1 | order rho | 95% | rank-1 | chance |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `embed_afxrep` | same player | 585 | 54 | **0.60** | 0.41-0.85 | 1.65 | 0.50 | 0.66 | **0.83** | 0.80-0.87 | 0.27 | 0.015 |
| | other player | 621 | 43 | **1.02** | 0.81-1.31 | 2.81 | **0.82** | 0.49 | **0.79** | 0.76-0.82 | 0.13 | 0.013 |
| `ltas_tilt` | same player | 585 | 54 | 1.56 | 1.18-2.30 | 2.38 | 1.45 | 0.34 | 0.45 | 0.40-0.51 | 0.05 | 0.015 |
| | other player | 621 | 43 | 1.32 | 1.03-1.68 | 1.86 | 1.18 | 0.37 | 0.42 | 0.35-0.49 | 0.04 | 0.013 |
| `envelope` | same player | 585 | 54 | 1.23 | 0.97-1.47 | 1.68 | 1.09 | 0.40 | 0.44 | 0.38-0.52 | 0.05 | 0.015 |
| | other player | 621 | 43 | 1.86 | 1.67-2.13 | 2.70 | 1.62 | 0.16 | 0.26 | 0.19-0.33 | 0.01 | 0.013 |
| `crest_flatness_hnr` | same player | 585 | 54 | 1.02 | 0.82-1.20 | 1.52 | 0.87 | 0.49 | 0.53 | 0.48-0.58 | 0.08 | 0.015 |
| | other player | 621 | 43 | 1.96 | 1.69-2.29 | 3.04 | 1.67 | 0.17 | 0.30 | 0.24-0.37 | 0.02 | 0.013 |
| `mrstft_v2` (reference) | same player | 585 | 54 | 2.22 | 1.83-2.67 | 3.11 | 1.96 | 0.05 | 0.59 | 0.54-0.64 | 0.17 | 0.015 |
| | other player | 621 | 43 | 2.30 | 1.98-2.76 | 3.49 | 2.09 | 0.02 | 0.51 | 0.44-0.57 | 0.05 | 0.013 |

The "same player" rows pool V1's other-passage condition (35 pairs) with the 450 drive pairs and MOD (100); "other player" pools V1's 21 other-player pairs with the 600 drive pairs.

### By source

| Source / condition | Score | n | R | 95% | R knob | R model | order rho | rho between two other DIs | rank-1 |
|---|---|---|---|---|---|---|---|---|---|
| DRIVE / same player (3 other recordings of the target's player; 25 drive models, 150 targets, ~86 candidates) | `embed_afxrep` | 450 | 0.78 | 0.62-0.99 | 2.06 | 0.65 | 0.80 | 0.79 | 0.18 |
| | `ltas_tilt` | 450 | 1.21 | 0.99-1.53 | 1.69 | 1.07 | 0.45 | 0.51 | 0.05 |
| | `envelope` | 450 | 1.38 | 1.19-1.57 | 1.88 | 1.21 | 0.39 | 0.38 | 0.02 |
| | `crest_flatness_hnr` | 450 | 0.84 | 0.69-1.03 | 1.30 | 0.74 | 0.54 | 0.55 | 0.08 |
| | `mrstft_v2` | 450 | 1.94 | 1.67-2.41 | 3.02 | 1.74 | 0.61 | 0.69 | 0.09 |
| DRIVE / other player (4 recordings of the other player) | `embed_afxrep` | 600 | 1.01 | 0.80-1.26 | 2.77 | 0.82 | 0.79 | 0.80 | 0.13 |
| | `ltas_tilt` | 600 | 1.31 | 1.01-1.62 | 1.78 | 1.18 | 0.42 | 0.51 | 0.03 |
| | `envelope` | 600 | 1.83 | 1.64-2.09 | 2.65 | 1.64 | 0.26 | 0.38 | 0.01 |
| | `crest_flatness_hnr` | 600 | 1.94 | 1.63-2.29 | 2.91 | 1.67 | 0.30 | 0.66 | 0.02 |
| | `mrstft_v2` | 600 | 2.27 | 1.96-2.64 | 3.41 | 2.09 | 0.51 | 0.68 | 0.04 |
| V1 / same player (helix-trust-v1, 28 items over amp, cab, drive, delay, reverb, dynamics, EQ, modulation; another passage of the same recording, plus 7 items whose second recording is the same player; 20 candidates) | `embed_afxrep` | 35 | 1.58 | 0.58-3.62 | 14.5 | 0.59 | 0.82 | 0.72 | 0.11 |
| | `ltas_tilt` | 35 | 2.25 | 1.18-6.34 | 7.5 | 1.24 | 0.57 | 0.34 | 0.03 |
| | `envelope` | 35 | 2.12 | 1.32-2.81 | 4.2 | 1.32 | 0.34 | 0.35 | 0.03 |
| | `crest_flatness_hnr` | 35 | 1.74 | 1.45-2.74 | 6.3 | 1.09 | 0.59 | 0.76 | 0.11 |
| | `mrstft_v2` | 35 | 4.53 | 2.89-6.98 | 11.3 | 2.80 | 0.51 | 0.51 | 0.09 |
| V1 / other player (21 items whose second recording is the other player) | `embed_afxrep` | 21 | 3.36 | 0.71-6.27 | 39.5 | 1.02 | 0.66 | - | 0.10 |
| | `ltas_tilt` | 21 | 3.61 | 1.29-11.0 | 12.5 | 1.43 | 0.38 | - | 0.10 |
| | `envelope` | 21 | 2.46 | 1.81-3.52 | 6.5 | 1.58 | 0.21 | - | 0.00 |
| | `crest_flatness_hnr` | 21 | 4.83 | 2.45-6.49 | 12.9 | 2.45 | 0.34 | - | 0.00 |
| | `mrstft_v2` | 21 | 5.04 | 2.94-6.70 | 16.5 | 2.55 | 0.46 | - | 0.05 |
| MOD / same player (mod-specialist renders: 5 train tremolo and vibrato models, 41 settings each, two passages of one P3 recording; 100 targets, ~61 candidates) | `embed_afxrep` | 100 | **0.05** | 0.03-0.08 | 0.05 | 0.03 | **0.99** | - | 0.76 |
| | `ltas_tilt` | 100 | 4.74 | 2.43-6.66 | 4.95 | 3.97 | 0.43 | - | 0.05 |
| | `envelope` | 100 | **0.49** | 0.41-1.15 | 0.49 | 0.46 | 0.74 | - | 0.22 |
| | `crest_flatness_hnr` | 100 | 2.53 | 2.01-4.13 | 2.56 | 2.06 | 0.45 | - | 0.06 |
| | `mrstft_v2` | 100 | 2.90 | 2.34-3.70 | 2.94 | 2.62 | 0.56 | - | 0.56 |

V1's candidates are mostly near knob moves of the true model (12 of 20), so its overall R is dominated by the knob denominator and is higher than DRIVE's for every score; its "R model" column is the comparable one (embedding 0.59 same player, 1.02 other player).

### V1 by block family (both conditions pooled; n / R / order rho; tiny counts, 4 to 10 pairs each)

| Family | `embed_afxrep` | `ltas_tilt` | `envelope` | `crest_flatness_hnr` | `mrstft_v2` |
|---|---|---|---|---|---|
| amp | 6 / 0.99 / 0.72 | 6 / 1.14 / 0.63 | 6 / 1.11 / 0.35 | 6 / 0.68 / 0.81 | 6 / 2.56 / 0.89 |
| cab | 6 / 0.37 / 0.93 | 6 / 0.83 / 0.62 | 6 / 2.08 / 0.46 | 6 / 1.91 / 0.23 | 6 / 3.14 / 0.55 |
| distortion | 8 / 0.37 / 0.91 | 8 / 1.18 / 0.81 | 8 / 1.64 / 0.24 | 8 / 1.00 / 0.77 | 8 / 3.00 / 0.65 |
| EQ | 4 / 0.75 / 0.95 | 4 / 0.64 / 0.72 | 4 / 2.79 / 0.43 | 4 / 1.70 / 0.79 | 4 / 1.44 / 0.80 |
| modulation | 6 / 1.12 / 0.91 | 6 / 7.52 / 0.44 | 6 / 1.51 / 0.23 | 6 / 6.63 / 0.28 | 6 / 4.92 / 0.78 |
| delay | 6 / 15.2 / 0.44 | 6 / 10.6 / 0.37 | 6 / 3.04 / 0.19 | 6 / 4.60 / 0.33 | 6 / 6.44 / 0.54 |
| reverb | 10 / 4.39 / 0.65 | 10 / 8.97 / 0.40 | 10 / 2.10 / 0.27 | 10 / 4.97 / 0.47 | 10 / 5.15 / 0.28 |
| dynamics | 10 / 6.75 / 0.69 | 10 / 10.8 / 0.22 | 10 / 4.38 / 0.26 | 10 / 2.78 / 0.39 | 10 / 16.4 / -0.01 |

Delay, reverb and dynamics blocks are invisible to every score across a recording change (R 2 to 16): those effects change the sound less than the notes do. Cab, EQ and distortion are where the embedding holds (R 0.4 to 0.75, order 0.9 or better). These are 4 to 10 pairs per cell and only indicate where a larger test would look.

## Reading

No score we have can rank *settings* when the dry recording differs from the target's; the recording change is as big as or bigger than a wrong setting for all five, and for the three statistics scores (`ltas_tilt`, `envelope`, `crest_flatness_hnr`) the candidate order on another recording is close to unrelated to the order on the target's own (0.26 to 0.45). The one score that keeps its order, `embed_afxrep` (0.79 to 0.83), does so because it separates *models* through the recording change (R 0.5 to 0.8 against other models) while being blind to knob moves (R 1.7 to 2.8 against knob moves, up to 40 on the knob-heavy V1 set). This is the same split [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") found on drive, now seen on a second data source across eight families and on modulation, where the embedding is almost perfectly invariant for tremolo and vibrato. The practical consequence for the dry-signal work: comparing renders of a generic dry pick against the song's guitar cannot set knobs for any block type by any of these scores, and a sound-comparison search without the target's own (or a faithful rebuilt) dry signal should be limited to model choice by the embedding; the knob question goes to the predictor or to a dry signal that matches the target's playing. Adding `envelope` or `crest_flatness_hnr` to a no-DI score would add noise, not information.

## What was compared, exactly

- **Distances.** `embed_afxrep`: 1 minus the cosine of the AFx-Rep mid vectors (as [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive"); the side vector is not used). `ltas_tilt`, `envelope`, `crest_flatness_hnr`: the kit modules' own `distance` on their features. `mrstft_v2`: the kit term. Every clip was set to the same loudness first (active level -23 dB, the [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") rule), so no score sees a level difference.
- **DRIVE.** The [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") plan and its cached AFx-Rep, spectrum and `mrstft_v2` results (`cache/drive-xdi`), with envelope and crest features computed now on the same 5,000 level-matched clips. 25 train drive models, 6 settings each as targets, 8 Guitar-TECHS recordings (4 of player P1, 4 of P2), about 86 candidates per target (knob moves of the true model, near-twin models, same-class and different-class models at random settings).
- **V1.** `helix-trust-v1`: 40 items, of which the 28 whose model is not a helix-trust-v2 test-only model. Each has the true preset and 19 distractors (near knob moves, a same-family model, random other-family presets) rendered on three recordings: A (the target's), B (another recording: 21 by the other player, 7 by the same) and C (another passage of A's recording).
- **MOD.** `cache/mod-specialist/cal_renders`: the 5 helix-trust-v2 train mod models (three tremolos, a pattern tremolo, a vibrato), 41 settings each on two passages of Guitar-TECHS P3 `directinput_04`. 10 target settings per model on each passage; candidates: every other setting of the model plus 5 random settings of each of the other 4 models.
- **Not usable.** The time-specialist renders (`cache/time-specialist/renders`) hold one knob setting per file (a Latin-hypercube fit set), so no setting exists on two recordings. [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") and [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") kept their per-job JSON but no audio. No render was made.
- **Statistics.** R per (target, other recording) is the target-to-own-setting distance divided by the median target-to-distractor distance on the target's recording; the tables report the median over pairs. Order rho is Spearman between the candidate distances on the target's recording and on the other one, including the true preset (distance 0 to itself for `mrstft_v2`). Intervals: 1,000 resamples of the models. Rank-1 is given for context only ([T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") is the rank-1 study on drive).

## Limits

- Helix against Helix; two players for DRIVE, three recordings for V1 (P1, P2, one P3 item), one player for MOD.
- The MOD source is five tremolo and vibrato models only, because only those are train-split models with calibration renders; chorus, flanger and phaser were not measured here (V1 has three modulation items; [T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist")'s modulation specialist covers the rest with the DI).
- V1's distractors are knob-heavy and small (20), so its overall R is dominated by the knob denominator; compare its "R model" column with DRIVE's.
- Pooled rows mix sources with different candidate mixes; the per-source tables are the primary reading.
- No pass bar was set; nothing passes or fails. This is the diagnostic the survey note proposed ("terms with R near or above 1 drop out before any rank-1 test spends renders").

## Files and record

- Script `scripts/di_invariance_check.py` (`run --jobs 3`, `report [--ledger]`), tests `tests/test_di_invariance_check.py` (statistics on synthetic distances; no renders, no model).
- Run: 278 target groups, 7,090 clips, 116,488 fine-detail pairs (102,624 of them read from the [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") cache), 493 s at 3 workers with nice 10 beside the chain-training pilot; 0 renders; `proc_check` clean before and after.
- Local outputs: `cache/di-invariance-check/{distances.json,summary.json,tables.md,run_meta.json,run.log}`.
- Ledger: `kind=benchmark`, dataset `di-invariance-check`, 2026-10-23, 10 rows: `invariance_ratio_<score>` (other-player R, pooled DRIVE + V1) and `order_spearman_<score>` (other-player order correlation) for the five scores; same-player values in the notes field.
