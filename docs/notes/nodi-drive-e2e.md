# No-DI drive matcher, end to end on real Native renders ([T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching"))

*Written 2026-10-18 by search. Measurement only: nothing here is adopted. Ledger: `search / native / nodi-drive-e2e-v1`. Tool: `scripts/nodi_drive_e2e.py`.*

## Read this first: three of the settings are TEST SETTINGS

The human has not answered three open questions. The lead asked for the testing to go on without them, so these runs used the pending recommendations as **test settings only. None of them is adopted as a default**, and the human decides after seeing this:

1. **Measured per-model tone tolerances**, taken from the `suggested_tol` block of `cache/drive-maps/knob_sensitivity.json`. They were applied explicitly for these runs.
2. **15 families** from `cache/drive-maps/drive-families-v1.json`.
3. **The second-choice family** is added for any shortlisted model whose family membership confidence is under 0.5.

## What was tested

- **24 targets.** A random drive model out of the 54 non-flagged ones (24 different models), random Drive and Tone settings that fall between the measured grid points, Level untouched. Seed 183, which is separate from [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…")'s seed; no [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…") setting was reused (checked by a guard in the plan).
- **Real guitar.** 8 excerpts of 5 seconds: Guitar-TECHS players P1 and P2 (4 clips) and the human's own dry takes (4 clips). Each target is rendered from one excerpt and the matcher gets another.
- **Blind condition.** The matcher renders through a different DI than the target did: another player or another take, always a different file. This is the case that matters, because with a real song the DI is never the target's own playing.
- **The matcher.** `match_block.py --shortlist afxrep` with 8 models from the AFx-Rep ranking, widened by their family and by the second-choice family. The knob path was (c), the coarse grid.
- **No song audio** was used. Target renders were deleted afterwards. Hosts: at most 2; the load, sampled between runs, stayed under 9 (8.4 at the highest).

## Results

All numbers are out of 24 targets unless said otherwise. Chance levels in brackets: the pool is 60 models, since the matcher does not know which 6 fuzzes were excluded from the targets.

### Blind condition (different DI), coarse grid

| Question | Result | Chance |
|---|---|---|
| True model in the AFx-Rep top 8 | **18 of 24 (75%)** | 13% |
| True model in the expanded shortlist (top 8 + families + second choice) | **23 of 24** | about 57% |
| True family in the expanded shortlist | **23 of 24** | about the same |
| AFx-Rep's own top-1 model | **11 of 24 (46%)** | 1.7% |
| AFx-Rep's own top-1 family | 12 of 24 | |
| **Final top-1 model (the flow's output)** | **2 of 24 (8%)** | 1.7% |
| Final top-3 model | 5 of 24 | |
| **Final top-1 family** | **3 of 24 (12.5%)** | |
| Drive within +-0.15, when the model is right | 1 of 2 | |
| Tone within its measured tolerance, when the model is right | 0 of 1 | |
| Renders per match | **370** (336 for the shortlist, one per shortlisted model for the check) | |
| Wall time per match | **106 s** (one Native host; about 95 s of it is the shortlist) | |

What this says:

- **The shortlist works, and is mostly the AFx-Rep ranking doing it.** The right model is in the top 8 three times in four, and the expanded shortlist is a wide net: it keeps 34 of 60 models (median), so 23 of 24 is only a little better than the 57% a random 34 would give. The families add little.
- **The second-choice family did nothing in this test.** It widened 15 of the 24 shortlists (2.8 more models on average) and rescued no target: the true model was never in the list only because of it.
- **The last step throws the shortlist away.** AFx-Rep puts the true model first 11 times. The flow then re-ranks the expanded shortlist with the unadmitted no-DI score and keeps the true model first only 2 times. With a different DI the score compares playing that is not the same, so its ranking is close to noise. This is the main finding: **the final order should be the AFx-Rep order, not the re-score** (proposal below).
- **The knobs are no better than a guess in the blind condition.** The coarse grid has 6 points (3 Drive by 2 Tone). Where AFx-Rep named the right model (11 times), its grid point had Drive within 0.15 4 times (chance about 30%) and Tone within tolerance 3 of 9 times (chance about 20%). Nothing in this flow knows the knobs when the DI is another performance; the report already labels the settings low confidence.

### Fingerprint inversion, path (a)

`tonematch.metrics.distortion_step4.estimate_knobs` exists ([T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…")), so path (a) was run too.

- **Blind condition: it declines.** [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…")'s code checks that the DI explains the target (twin fit) and, when it does not, returns nothing. With a different DI it returned nothing for every one of the 24 targets, so the flow fell back to the coarse grid and the results are **identical** to the coarse-grid rows above. This is the right behaviour, not a bug in the inversion; the bug was that the flow then said "inversion" (see Bugs).
- **Same-DI arm (an upper bound, not the blind case).** To see what the inversion is worth when it can run, I also gave the matcher each target's own DI excerpt, sample-aligned. That is more favourable than any real take, because the human's takes are only loosely aligned and the song is not a clean render.

| Same DI, inversion on | Result |
|---|---|
| True model in the AFx-Rep top 8 | **24 of 24** |
| AFx-Rep top-1 model | **23 of 24** |
| Final top-1 model / family | 17 of 24 / 18 of 24 |
| Final top-3 model | 18 of 24 |
| Drive within +-0.15, final model right | **15 of 17** |
| Tone within its measured tolerance, final model right | **9 of 15** |
| AFx top-1 pick: Drive / Tone | 18 of 23 / 11 of 20 |
| Renders, wall per match | 369, 117 s |

With the same DI the AFx-Rep ranking is very good (23 of 24 first), the inversion's Drive is usually right, and Tone is right about 60% of the time (tolerance 0.1, tighter near the ends). The re-score still costs accuracy (17 against 23 first). So the gap between the two arms is the DI: **it is the playing, not the method, that breaks the blind case.**

### Old full search, capped, on 6 of the targets

The 6-target subset was random, with seed 183. Budget: a 6-minute wall limit, 2 Native hosts, no shortlist, blind DI.

| | Old full search (capped) | Shortlist, coarse grid |
|---|---|---|
| Final top-1 model | 0 of 6 | 0 of 6 |
| Final top-3 | 2 of 6 | 0 of 6 |
| Final top-1 family | 0 of 6 | 0 of 6 |
| AFx-Rep top 8 / top-1 | n/a | 5 of 6 / 1 of 6 |
| Renders per match | about 2,300 | about 370 |
| Wall per match | about 216 s (2 hosts) | about 106 s (1 host) |

The subset happened to be hard for AFx-Rep (one target ranked 43rd of 60), so these six are not the average. Both are poor on them. The shortlist flow uses about a sixth of the renders. The old search got the true model into its top 3 twice; the shortlist never did on this subset (the true model was in the AFx top 8 five times, but the re-score moved it down). The old search's knob numbers are empty because it never named the true model first. This was a capped run (a full-budget run of 25 minutes was not made), so it says only that the old search is no better at this cap.

## Bugs found and fixed (tests in `tests/test_match_block.py`)

1. **Coarse grid moved the wrong knob, or none, on 10 of 60 models.** The grid picked Drive and Tone by guessing from names. Classic Distortion got Treble instead of Filter, KillerZ got Gain instead of Dist, and Clawthorn Drive, Tone Sovereign and Vital Boost got no knob at all (so they were only sampled at their defaults). `coarse_points` now takes the measured drive/tone knob names from the [T179](../../board/TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models") map and keeps the name rules as the fallback.
2. **The final check only scored the first 10 models of the shortlist**, so most family members were never scored and the family widening did nothing. With `--shortlist` the check now covers the whole expanded shortlist.
3. **Fingerprint inversion declining was silent**: the report said "knob path: inversion" while the settings were the coarse grid's. It now falls back, logs "declined", and records `inversion_declined`.
4. **Inversion positions were written as parameter values.** They are knob fractions 0..1; they are now scaled to each knob's range (matters for knobs like Treble -20 to 20).
5. **Renders were counted twice** (the shortlist's renders went through the same engine that was then counted again): reported 706 per match, true 370. The run's numbers above are corrected; the stored result files of this run still show the doubled count.
6. **The second-choice family was not implemented**; added as `--second-choice CONF`, with `--knob-path` to force a path, `--target-wav/--dry-wav` to match a plain pair of wavs, and `--lean` to skip the listening pack.

A tooling note: the ledger helper stopped on a division of 0 by 0 after writing the rows through `full_afx_top1_model`; that row (0 of 6, the full search has no AFx stage) is meaningless and should be ignored. The last row (`full_renders_per_match_median`) was appended by hand.

## What this means, and what I recommend

- **For the lead / human to decide:** the two shortlist settings are still undecided; this test gives little reason to prefer them. The families made the shortlist about 4 times longer than 8 models and gained about 5 targets over the top 8; the second-choice family gained none.
- **Proposal (for the lead, changes the flow):** with a non-aligned DI, rank the final models in the AFx-Rep order and drop the re-score from the ranking. On this test that moves top-1 from 2 of 24 to 11 of 24 blind and from 17 to 23 of 24 with the same DI. The re-score should still be shown for information. It is not done here because it changes what the flow reports as best; the task asked for bug fixes.
- **Knob setting needs a DI that is the same performance.** In a real song the take is only loosely aligned and the stem is full of other things, so path (a) is likely to decline there too. [T182](../../board/TASKS.md#t182 "Re-match the Mother 28.3-38.3 s guitar stem") will show.

## Files

- `cache/matches/t183/` (plan, per-target `result.json` and logs; the target and DI audio was deleted), `reports/local/t183/rows.json` and `summary.json`.
