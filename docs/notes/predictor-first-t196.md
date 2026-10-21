# Predictor-first drive matcher vs the AFx-Rep shortlist ([T196](../../board/TASKS.md#t196 "Predictor-first drive matcher"))

*Written 2026-10-20 by search. Measurement only: nothing is adopted and the default is unchanged (`--shortlist none`). Ledger: `search / native / predictor-first-v1` (120 rows). Tools: `scripts/predictor_first_t196.py`, `match_block.py --shortlist predictor`, `src/tonematch/search/predictor_first.py`. Local results (no audio): `reports/local/t196/` (`rows.json`, `summary.json`, `predictions.json`, `input_levels.json`, `analyse.txt`).*

## What was built

`match_block.py --shortlist predictor` (a non-default option next to `--shortlist afxrep`):

1. Embed the target once (three 3.33 s AFx-Rep crops, as the head was trained) and run the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") head: `heads_main_v2data_mlp_multi.pkl`, with the [T189](../../board/TASKS.md#t189 "Drive predictor pilot v1") calibration (temperature 0.77; sigma scales 0.92 Drive, 0.79 Tone). It gives a probability per model and a Drive and Tone mean and sigma per model.
2. Keep the models that cover 95% of the probability, at most 6. The six fuzzes the dataset never contained are masked.
3. For each, render the mean, Drive +-1 sigma and Tone +-1 sigma (5 renders; a Drive-only model gets the mean and +-1, +-2 sigma; sigma never below 0.05; duplicates at the knob ends are dropped).
4. Order the models by the AFx-Rep distance of their best candidate (the T185/v5 ranking). The report also keeps the predictor's own pick and knobs, and the confirm-render pick. A provisional re-score of the winners runs as in the AFx path and is counted in the renders.

Tests: `tests/test_predictor_first.py` (7, fake head and engine), plus the 26 existing `test_match_block.py` tests still pass.

## Set-up

- **72 targets, seed 196**, each 10 s of real DI rendered through one random drive model (54 models, each at least once) at random off-grid Drive and Tone. No [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…"), [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") or [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") setting reused. Ten seconds, not the [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") five, because the head was trained on 10 s clips. No song audio.
  - 24 Guitar-TECHS P1 and 24 P2, from files the head never trained on (drive-dataset-v1 `test` and `v1test` splits). Known players, new recordings and new settings.
  - 24 from the human's own dry takes (`data/songs/mother-playalong/takes`; one session, four takes of 68 to 71 s). A player the head never saw. Because the takes are short, two human excerpts may overlap by up to 5 s (and earlier T183/T185 excerpts by up to 5 s); the settings are all new.
- **Two matcher-DI conditions per target.** Blind: another player (for a human target, a Guitar-TECHS DI). Same player: another, non-overlapping excerpt of the same player (for the human, another excerpt of the human's takes). The head sees only the target, so its model choice and its own knobs are the same in both; only the confirming renders and the AFx arm change.
- **Arms, same job on the same host.** `afx_top8`: the [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") shortlist (336-render grid), top 8, AFx final order. `afx_fam2` (top 8 + families + second choice) gives identical results under AFx order, as in [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement"). `pred_confirm`: the new path, models and knobs after the confirming renders. `pred_own`: the predictor's top model at its mean knobs, 0 renders. `pred_top1_confirm`: the predictor's top model with the best confirm candidate's knobs.
- Metrics as [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement"): model top-1 and top-3, family, Drive within 0.15 and Tone within its measured tolerance (these two are out of the targets where the top model was right), "all knobs" (right model, Drive and Tone both within) out of all targets. Wilson 95% intervals in the ledger and `summary.json`.
- Native 3 hosts, busy-core guard; other agents ran at the same time, so the wall times are what a loaded machine gave (the busy-core guard paused the shards several times).

## Results (each cell: k/n, percent)

### Guitar-TECHS targets, 48 per cell

| Condition | Arm | Model top-1 | Top-3 | Family | Drive +-0.15 | Tone | All knobs | Renders | Wall per match |
|---|---|---|---|---|---|---|---|---|---|
| Blind | AFx shortlist, AFx order | 21 (44%) | 32 (67%) | 25 (52%) | 14/21 | 7/16 | 10 (21%) | 344 | 155 s |
| Blind | **predictor-first** | **41 (85%)** | **48 (100%)** | **43 (90%)** | 26/41 (63%) | 24/35 (69%) | 19 (40%) | 10 | 4.4 s |
| Blind | predictor alone | 42 (88%) | 48 (100%) | 44 (92%) | 28/42 (67%) | 26/35 (74%) | 23 (48%) | 0 | 0.3 s |
| Same player | AFx shortlist, AFx order | 25 (52%) | 42 (88%) | 31 (65%) | 13/25 | 11/21 | 10 (21%) | 344 | 155 s |
| Same player | **predictor-first** | **46 (96%)** | **48 (100%)** | **46 (96%)** | 28/46 (61%) | 28/39 (72%) | 22 (46%) | 10 | 4.6 s |
| Same player | predictor alone | 42 (88%) | 48 (100%) | 44 (92%) | 28/42 (67%) | 26/35 (74%) | 23 (48%) | 0 | 0.3 s |

### The human's own takes, 24 per cell

| Condition | Arm | Model top-1 | Top-3 | Family | Drive +-0.15 | Tone | All knobs | Renders | Wall per match |
|---|---|---|---|---|---|---|---|---|---|
| Blind (Guitar-TECHS DI) | AFx shortlist, AFx order | 11 (46%) | 13 (54%) | 15 (62%) | 1/11 | 5/10 | 0 (0%) | 344 | 154 s |
| Blind | **predictor-first** | **16 (67%)** | **21 (88%)** | 18 (75%) | 6/16 (38%) | 11/15 | 4 (17%) | 16 | 7.3 s |
| Blind | predictor alone | 16 (67%) | 20 (83%) | 17 (71%) | 6/16 (38%) | 11/16 | 5 (21%) | 0 | 0.3 s |
| Same player (human DI) | AFx shortlist, AFx order | 20 (83%) | 23 (96%) | 21 (88%) | 12/20 (60%) | 8/19 | 4 (17%) | 344 | 154 s |
| Same player | predictor-first | 20 (83%) | 21 (88%) | 23 (96%) | 10/20 (50%) | 15/19 | 8 (33%) | 16 | 7.1 s |
| Same player | predictor alone | 16 (67%) | 20 (83%) | 17 (71%) | 6/16 (38%) | 11/16 | 5 (21%) | 0 | 0.3 s |

Across all 144 matches the new path used 12 renders on average (6 to 36; 6 when one model holds 95%: the 5 candidates plus 1 re-score), against 344.

### Paired tests (exact McNemar, two-sided; "only new" = predictor-first right and AFx wrong, then the reverse)

| Cell | Top-1 | Top-3 | Family | All knobs |
|---|---|---|---|---|
| Guitar-TECHS blind | 21 / 1, p 1e-5 | 16 / 0, p 3e-5 | 18 / 0, p 8e-6 | 12 / 3, p 0.035 |
| Guitar-TECHS same player | 23 / 2, p 2e-5 | 6 / 0, p 0.03 | 17 / 2, p 7e-4 | 15 / 3, p 0.008 |
| Human blind | 6 / 1, p 0.13 | 8 / 0, p 0.008 | 4 / 1, p 0.38 | 4 / 0, p 0.13 |
| Human same player | 2 / 2, p 1.0 | 1 / 3, p 0.63 | 2 / 0, p 0.5 | 6 / 2, p 0.29 |

Predictor-first against the predictor alone (does the confirm step help?): no test reaches p 0.05. It cost model top-1 on Guitar-TECHS blind (41 against 42) and gained on Guitar-TECHS same player (46 against 42, p 0.29) and the human's same-player DI (20 against 16, p 0.22). Drive and Tone accuracy do not improve with the confirming renders (all-knobs 40 to 46% against 48% for the predictor alone on Guitar-TECHS; the 5 candidates sit within 1 sigma of the mean, so a distance difference between them is small compared with the matcher-DI level and performance differences).

## What it shows

1. **Model choice.** On Guitar-TECHS held-out files the predictor-first path names the right model first 85 to 96% of the time against 44 to 52% for the AFx shortlist, with 10 renders against 344. The right model is among the top three every time (48 of 48 in each condition). The gains are large and every Guitar-TECHS paired test is far under 0.05. The head's model accuracy here matches its [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") figure on held-out recordings (0.88 against 0.93 on a different test set).
2. **A new player (the human).** Blind (matcher DI from other players) predictor-first is 16 of 24 against 11 of 24 (p 0.13 on top-1, p 0.008 on top-3). With the human's own DI as the matcher the AFx shortlist catches up (20 of 24 for both), and the predictor-first top-3 is no better (21 against 23). Three human targets (6 of 48 target-conditions, two models: HeirApparent, Screamer) were outside the 95% set, so no confirm render could rescue them.
3. **The confirm step.** It is worth it when the matcher's DI resembles the target's player (Guitar-TECHS same-player 96% against 88% for the head alone; human 83% against 67%), and it adds nothing or costs a little when the DI is another player. Its cost is 10 to 16 renders. With a blind DI the predictor alone is as good.
4. **Knobs are not better, and Drive is level-confounded ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly")).**
   - Within Guitar-TECHS, Drive is within 0.15 of the true setting for 61 to 67% of the right-model cases and Tone 69 to 74%, near the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") figures (0.71 and 0.67). The AFx shortlist is 52 to 67% and 44 to 52%.
   - "All knobs" (the stricter, out-of-all-targets figure) is 40 to 48% for the new path against 21% for the AFx shortlist on Guitar-TECHS (p 0.035 and 0.008 for the confirm path) and 17 to 33% against 0 to 17% for the human.
   - Drive by input level (loud-frame level of the target DI, relative to -26.7 dBFS, the level of the old renders; no normalisation applied, as asked). Rows where the arm's top model was right (target x condition rows):

| Group | Arm | Rows | Target DI level (median, range) | Mean signed Drive error (estimate minus true) | Mean absolute error | Within 0.15 |
|---|---|---|---|---|---|---|
| Guitar-TECHS | predictor alone | 84 | -8.3 dB (-17.7 to -2.1) | -0.01 | 0.12 | 56 of 84 |
| Guitar-TECHS | predictor-first | 87 | -8.4 dB | +0.02 | 0.14 | 54 of 87 |
| Guitar-TECHS | AFx shortlist | 46 | -7.7 dB | +0.01 | 0.17 | 27 of 46 |
| Human | predictor alone | 32 | -0.8 dB (-3.8 to -0.1) | **+0.19** | 0.25 | 12 of 32 |
| Human | predictor-first | 36 | -0.9 dB | **+0.20** | 0.24 | 16 of 36 |
| Human | AFx shortlist | 31 | -0.9 dB | **+0.15** | 0.25 | 13 of 31 |

   - Within Guitar-TECHS the signed error shows no trend with level (slope +0.001 per dB for the predictor alone, bins below -12, -12 to -6 and above -6 dB all near zero; the absolute error is 0.08, 0.12 and 0.15). The human's DI sits at the top of the range (7 dB above the Guitar-TECHS median) and every arm reads Drive too high there by 0.15 to 0.20. The AFx shortlist shows the same bias, so it is not only the head. The human's takes are a single level cluster, so this does not separate level from player. Treat Drive as provisional, as the lead said.
   - Model and Tone results stand as they are.
5. **Cost.** 12 renders on average and 5 s per match (4.1 s median, loaded machine) against 344 renders and 155 s: about 29 times fewer renders and 30 times less time. The head and embedding take 0.3 s on the CPU.

## Caveats

- Guitar-TECHS targets come from two players only, and the matcher DIs are Guitar-TECHS too; the head and the data share the instrument and the interface. The human's takes are one session and one guitar. 24 targets per cell give wide intervals (the human cells span 20 to 30 points).
- Targets are 10 s excerpts; a real section may differ. [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") used 5 s excerpts, so the AFx numbers here are not those of [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") (44% and 52% against 39% and 59%).
- The Native model is the same on both sides (Helix against Helix). Nothing was played through a real pedal.
- The predictor knows only the 54 non-fuzz drives and the drive category; it would need a different front end for chains.
- The target audio and the DI excerpts were deleted after use (`clean`).

## For the human to decide

Nothing here is adopted. If the human wants the predictor-first path to be the default, the evidence above is the comparison. The open items are the Drive level confound ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly")), the real-pedal test (measured-pedal mode needs the human at the pedal), and the AFx-Rep licence terms.
