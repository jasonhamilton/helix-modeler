# No-DI drive matcher, end to end, larger test ([T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement"))

*Written 2026-10-19 by search. Measurement only: nothing here is adopted and no default changed. Ledger: `search / native / nodi-drive-e2e-v2` (96 rows). Tools: `scripts/nodi_drive_e2e_v2.py`, `scripts/match_block.py --final-rank afxrep` (new, not the default). Builds on [T183](nodi-drive-e2e.md).*

## What this answers

[T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching") found that the matcher's last step (re-scoring the shortlist with the provisional score) throws away the good order that AFx-Rep (the audio-embedding distance) gives, and proposed ranking by AFx-Rep instead. It had only 24 targets. This run has **96**, three DI conditions, and tests the shortlist options too. The settings are still TEST SETTINGS (measured tone tolerances, 15 families, second-choice family under confidence 0.5); the human has not decided them.

## Set-up

- **96 targets.** A random drive model out of the 54 non-flagged (each model once, then 42 more different ones), random off-grid Drive and Tone, Level fixed. Seed 185. None of [T181](../../board/TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…")'s or [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching")'s settings or excerpts was reused (guard in the plan).
- **15 guitar excerpts of 5 s**, 5 each from Guitar-TECHS P1, P2 and the human's own dry takes. The song stem was not used. The human's takes all come from one session (the other takes were too quiet or too short), so "human" is one player on one day.
- **Three conditions.** *Blind*: the matcher's DI is another **player** (strictly, a change from [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching"), where another take of the same player could count). *Same-player*: same player, a different excerpt. *Same-DI*: the target's own DI, the upper bound; here the fingerprint inversion can run (it did on 93 of 96) and sets the knobs. In the other two the inversion declines, as in [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching"), and the knobs are the best of the 6 coarse-grid points.
- **Four arms per condition.** Final order: the provisional score (the current default) or AFx-Rep order. Shortlist: top 8 plus families plus second choice, or the plain AFx top 8.
- **Shared renders.** The coarse grid (336 renders) is the same in all four arms, so each (target, condition) was rendered once, the whole expanded shortlist scored once on the full excerpt, and the arms read off. I checked this against a real `match_block.py` run with `--final-rank afxrep` on one target: same models, same renders (372), scores equal to 3e-5. Renders per match below are what each arm would use alone (336 plus its own shortlist size).
- **Host limits.** 3 Native hosts, paused above load 9 (reached 9.8 while other agents ran, and the shards waited). One Native error ("returned silence") killed one shard; I made the runner treat that render as a very bad match instead and restarted it; this happened in 2 of 288 jobs and a handful of renders.

## Results (96 targets per cell; percent, then 95% Wilson interval)

"Model right" cells for Drive and Tone are out of the targets where the arm's top-1 model was right. "All knobs" is out of all 96: model right, Drive within 0.15, and Tone within its measured tolerance (when the model has a Tone knob).

### Blind (another player)

| Arm | True model in list | Model top-1 | Model top-3 | Family top-1 | Drive ok | Tone ok | All knobs | Renders |
|---|---|---|---|---|---|---|---|---|
| provisional, families + 2nd | 91 (95%, 88-98) | 10 (10%, 6-18) | 29 (30%, 22-40) | 26 (27%, 19-37) | 7/10 | 4/8 | 3 (3%) | 369 |
| **AFx order, families + 2nd** | 91 (95%) | **37 (39%, 29-49)** | **59 (61%, 51-71)** | **52 (54%, 44-64)** | 19/37 (51%) | 15/34 (44%) | 8 (8%, 4-16) | 369 |
| provisional, plain top 8 | 81 (84%, 76-90) | 17 (18%, 11-27) | 41 (43%, 33-53) | 30 (31%, 23-41) | 9/17 | 8/14 | 4 (4%) | 344 |
| AFx order, plain top 8 | 81 (84%) | 37 (39%) | 59 (61%) | 52 (54%) | 19/37 | 15/34 | 8 (8%) | 344 |

### Same player, different excerpt

| Arm | In list | Top-1 | Top-3 | Family top-1 | Drive ok | Tone ok | All knobs | Renders |
|---|---|---|---|---|---|---|---|---|
| provisional, families + 2nd | 92 (96%, 90-98) | 10 (10%, 6-18) | 32 (33%, 25-43) | 29 (30%, 22-40) | 5/10 | 5/8 | 3 (3%) | 370 |
| **AFx order, families + 2nd** | 92 (96%) | **57 (59%, 49-69)** | **70 (73%, 63-81)** | **65 (68%, 58-76)** | 37/57 (65%) | 22/48 (46%) | 20 (21%, 14-30) | 370 |
| provisional, plain top 8 | 84 (88%, 79-93) | 27 (28%, 20-38) | 57 (59%, 49-69) | 47 (49%, 39-59) | 15/27 | 10/23 | 6 (6%) | 344 |
| AFx order, plain top 8 | 84 (88%) | 57 (59%) | 70 (73%) | 65 (68%) | 37/57 | 22/48 | 20 (21%) | 344 |

### Same DI (upper bound; fingerprint inversion sets the knobs)

| Arm | In list | Top-1 | Top-3 | Family top-1 | Drive ok | Tone ok | All knobs | Renders |
|---|---|---|---|---|---|---|---|---|
| provisional, families + 2nd | 96 (100%) | 46 (48%, 38-58) | 58 (60%, 50-70) | 60 (62%, 53-72) | 30/46 (65%) | 25/38 (66%) | 24 (25%, 17-35) | 370 |
| **AFx order, families + 2nd** | 96 (100%) | **84 (88%, 79-93)** | **91 (95%, 88-98)** | **87 (91%, 83-95)** | 44/84 (52%) | 31/72 (43%) | 25 (26%, 18-36) | 370 |
| provisional, plain top 8 | 93 (97%, 91-99) | 52 (54%, 44-64) | 70 (73%, 63-81) | 65 (68%, 58-76) | 31/52 | 27/43 | 24 (25%) | 344 |
| AFx order, plain top 8 | 93 (97%) | 84 (88%) | 91 (95%) | 87 (91%) | 44/84 | 31/72 | 25 (26%) | 344 |

Plain top-8 and families give identical AFx-order results by construction: AFx order inside a longer list has the same first eight, so the families can only matter when the final order is the re-score. The paired test of those two arms is therefore empty.

### Paired comparisons (McNemar, exact, two-sided; "only A" = A right and B wrong)

| Condition | Comparison | Top-1 only A / only B (p) | Top-3 only A / only B (p) |
|---|---|---|---|
| Blind | AFx vs provisional, with families | 31 / 4 (p = 3e-6) | 36 / 6 (3e-6) |
| Blind | AFx vs provisional, plain top 8 | 28 / 8 (0.001) | 25 / 7 (0.002) |
| Blind | provisional with families vs plain top 8 | 0 / 7 (0.016) | 0 / 12 (0.0005) |
| Same player | AFx vs provisional, with families | 49 / 2 (1e-12) | 42 / 4 (5e-9) |
| Same player | AFx vs provisional, plain top 8 | 35 / 5 (1e-6) | 17 / 4 (0.007) |
| Same player | provisional with families vs plain top 8 | 0 / 17 (2e-5) | 1 / 26 (4e-7) |
| Same DI | AFx vs provisional, with families | 44 / 6 (3e-8) | 35 / 2 (1e-8) |
| Same DI | AFx vs provisional, plain top 8 | 38 / 6 (9e-7) | 23 / 2 (2e-5) |
| Same DI | provisional with families vs plain top 8 | 0 / 6 (0.03) | 0 / 12 (0.0005) |

## What it says

1. **AFx-Rep order beats the provisional score in every condition, by a wide margin.** Blind top-1 goes from 10 to 37 of 96 with families (18 to 37 against the plain top 8); with the same DI from 46 to 84. [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching")'s finding holds on four times the targets. The provisional re-score is worse than the shortlist order it is given.
2. **The families hurt when the provisional score does the final ranking** (the current default): more candidates for a noisy score to choose among, so top-1 drops (blind 17 to 10, same-player 27 to 10). With AFx order they cost nothing, but add nothing to the answer either: the same top-1 and top-3, only a bigger list (84% to 95% "true model in list" blind). So the list is useful only if something downstream searches it with a score that can be trusted, and none exists yet in the no-DI case.
3. **The second-choice family is a small add.** Blind, the true model is in the top 8 for 81 targets, in the top 8 plus families for 89, and with the second choice for 91 (2 targets). Same-player 84, 90, 92. Same DI 93, 95, 96. The shortlist grows from about 31 to 33 models.
4. **Blind (another player) is hard, even for AFx-Rep.** Top-1 39%, top-3 61%, family 54%, against 59%, 73%, 68% for the same player and 88%, 95%, 91% for the same DI. Different playing changes what the embedding sees. The true model sits at median AFx rank 3 blind and rank 1 for the other two conditions.
5. **Knobs are not good enough to claim.** When the model is right, Drive is within 0.15 about half the time (blind 51%, 19 of 37; same-player 65%; same-DI with inversion 52%) and Tone within tolerance 43-46%. Counting the model, both knobs: 8% blind, 21% same-player, 26% with the same DI. With the same DI the inversion was no better than the coarse grid with another excerpt of the same player (Drive 52% against 65%). [T183](../../board/TASKS.md#t183 "End-to-end check of no-DI drive matching")'s 15 of 17 on 24 targets did not hold up on 96. The coarse grid has 6 points, so these numbers are close to what a guess gives.
6. **Cost.** About 370 renders (336 for the grid plus the shortlist) per match, or 344 for the plain top 8: about a sixth of the old search. A match took roughly 90-120 s on one host.

## What I did not do, and caveats

- Nothing adopted. `--final-rank afxrep` exists and is off by default. Whether the flow should say "best = AFx order" is the human's decision; this data supports it for ranking models, not for the knobs.
- The 96 targets are drive models on the Native single-block library, rendered by the same engine the matcher uses: a best case. A real song adds a different engine (the Stomp), a band, a cab and amp.
- The human's DI is one session and one player; Guitar-TECHS adds two. "Another player" covers three players in total.
- Listening is not scored here. `reports/local/t185/predictions.json` has, per condition and arm, each target's top-3 predicted models and settings, with the target DI path, start sample and length, the lead-in, and the target's true settings, so benchmark ([T188](../../board/TASKS.md#t188 "Rating session v5: blind predictions re-rendered on the target's own…")) can re-render the predictions and the target on the target's own DI. The target audio was deleted (disk); [T188](../../board/TASKS.md#t188 "Rating session v5: blind predictions re-rendered on the target's own…") re-renders it.
- Disk: 4 MB of this run's files remain under `cache/matches/t185/` (the per-job JSON); free space was 6-9 GB during the run from other agents' jobs, not this one.
- Stale paths: `scripts/distortion_step4_analysis.py` and `scripts/nodi_drive_e2e.py` still point at `data/drive-families-v1.json` (moved by the lead). I changed the default in `src/tonematch/search/shortlist.py` to `cache/drive-maps/` because the shortlist silently ran without families by default; the other two are untouched.

## Files

- `scripts/nodi_drive_e2e_v2.py`, `scripts/match_block.py` (`--final-rank`), `tests/test_match_block.py` (3 new tests, 26 pass).
- `cache/matches/t185/` (plan, tolerance map, per-job JSON, logs), `reports/local/t185/{rows,summary,predictions}.json`.
