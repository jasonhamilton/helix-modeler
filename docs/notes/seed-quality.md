# How good must a predictor's first guess be? ([T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment"))

Status: PAUSED after a partial PILOT (n = 12, one budget, two cells). Diagnostic only, not gate evidence. Last updated 2026-10-18.

## Plain answer so far

The pilot cannot say how accurate a predictor must be, because the search was at chance with or without a seed. The current score (the Gate 2 known-dry-track score) was used blind for this control: the target was made from one guitar clip and every search render went through a different clip. Under that mismatch the score does not prefer the true model, so a good first guess has nothing to hold on to.

What the 12 items showed:

- Unseeded hybrid, 150 renders: right model on 2 of 12 (0.17), right category on 4 of 12 (0.33). Chance for the model is about 0.19.
- Seeded hybrid (right model about half the time by design, settings noise 0.1), 150 renders: right model 2 of 12 (0.17), right category 5 of 12 (0.42). The difference in model accuracy is exactly 0, and in category accuracy +1 item. At n = 12 neither is distinguishable from zero.
- By luck of the fixed draws the seed named the right model on only 3 of 12 items (0.25), not 6. Those three items:
  - i011 (amp): right with and without the seed.
  - i000 (tremolo): the seed was right and its model reached refinement, but a reverb scored 1.133 against the true model's 1.133 after refinement and won by a hair. Without the seed the true model refined to 1.695, so the seed did improve its own fit, and it still did not win.
  - i006 (echo): the seed was right and improved the model's screen loss (0.815 against 0.883), but a preamp at 0.771 still beat it and the echo was never refined.
- Why: even a seed within 0.05 to 0.13 of the true knobs scores 0.8 to 1.2 under the mismatched clip, while the true preset scores 0 on the matched clip. Other models reach the same band, so the ranking is decided by the score, not by the starting point.

So, for the plain question: with this score and no known dry track, a predictor that gets the model right 25 to 50 percent of the time saved zero renders in 12 items, and the failure was the score. The experiment has to be re-run once a blind score exists ([T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") and blind versions of the specialists). Nothing here argues against seeding; it shows the seed cannot help while the score cannot tell the true model apart.

## Why it stopped

The lead stopped the pilot (human approved, so all CPU goes to [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)")). Under `--di-mismatch` the loss and the specialists receive the search's own clip as the "dry" signal, so the cells measure a score that does not fit the blind task, not blind matching. The full grid (see DECISIONS 2026-10-18 [T162](../../board/TASKS.md#t162 "No-DI step 2: corrupted-seed experiment")) is deferred until [T161](../../board/TASKS.md#t161 "No-DI step 1 (human 2026-10-18 yes to steps 1 and 2)") picks a blind score and the specialists have blind versions.

## Pilot table (condition b only, n = 12, budget 150, first 12 devfam items, 3 each of mod, reverb, delay, amp)

| Arm | Seed model right | Model acc | Category acc | Settings MAE (right-model items) | Renders per item | Seconds per item |
|---|---|---|---|---|---|---|
| Unseeded hybrid | - | 0.17 (2/12) | 0.33 (4/12) | 0.124 (n = 2) | 157 | 83 |
| Seeded, p 0.5 asked, sigma 0.1 | 0.25 (3/12) realised | 0.17 (2/12) | 0.42 (5/12) | 0.188 (n = 2) | 158 | 79 |
| Random walk | not run | | | | | |
| Seeded, p 1.0, sigma 0.1 | not run | | | | | |
| p 0.75, 0.25, sigma 0 and 0.25, budgets 50 and 300 | not run | | | | | |

Paired difference seeded minus unseeded: model 0.00 (interval 0 to 0), category +0.08 (-0.00 to +0.25), MAE not estimable (2 items each). By family (seeded): mod 1/3, amp 1/3, reverb 0/3, delay 0/3. Cells and per-item numbers: `reports/t162/cells_b.json`. Ledger rows: `kind=search`, dataset `synthetic-devfam-seedcorrupt-diffdi`, metrics `model_acc_*` and `param_mae_*` (4 rows).

The known-DI arm (a) was dropped by the human ("Why are we running any tests with the dry sample known?"). Its partial data on disk (`t162a_*` run dirs) is not reported as a result.

## What was built

- `--seed-corrupt P,SIGMA` (hybrid only): the true answer, corrupted, becomes the first broad draw of its model (`predictor_seed` in `choose_models_hybrid`; default off = frozen method). Wrong model: another pool model of the same category. Fixed random draws per item so grids are nested.
- `--seed-stratify`: exactly round(P x items) right models instead of an independent draw per item. Added after the pilot showed the binomial luck (3 of 12 instead of 6); not yet used in a run.
- `--di-mismatch`: the target from the item's DI A, the search (and the random baseline) through a clip of a different recording.
- Driver `scripts/seed_quality_grid.sh`, aggregation `scripts/seed_quality_report.py`, 8 new tests in `tests/test_synthetic_v1.py`.

## To resume

Run the registered grid (blind cells only) with `--seed-stratify` once a blind score is chosen. At n = 12 only differences larger than about 0.3 in model accuracy can be seen; use 32 or more items.
