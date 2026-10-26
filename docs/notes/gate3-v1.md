# Gate 3 sealed blind set `gate3-v1` ([T223](../../board/TASKS.md#t223 "Blind validation of AFx-Rep against separated stems and Gate 3 bars + sealed P3 set"))

*Written 2026-10-25 by benchmark. The set is sealed; nobody has scored it. The bars below are **proposed by benchmark, not approved**: the lead puts them to the human before the set is scored.*

## What it is

120 single drive-block items (the current product): each of the 60 product drive models (`match_block.category_pool('drive')`) twice, with random uniform settings on the CC grid (level-type knobs at their default), played through Helix Native 3.82 from a random 10 s active excerpt of **Guitar-TECHS P3** (the reserved third player; a different person and guitar from P1 and P2). The matcher gets only the rendered 10 s output (`cache/gate3-v1/targets/g3-NNN.wav`). The labels (model, settings, P3 file and start) and the dry excerpts are in `cache/gate3-v1/sealed/`, unread by the matcher and by benchmark since generation. Six of the 60 models are not in the [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") dataset pool (the flagged fuzzes), so they are flagged in the labels and a predictor trained on that dataset can miss them by construction; the report will give the 54-model subset as well.

- **Seed** 223001. **sha256** `manifest.json` b69372d4b7c4126ad6dd47a0b7671228530fd3d37f7f178b3fa30fa595906b27; `sealed/labels.json` 88a8880c14947375f2fa21333d86298e491b2d649983b702b480898a848cdbaa. Ledger `benchmark / gate3-v1 / sealed_items` = 120 ([2026-10-25T18:58:08](../../ledger/runs.csv?plain=1#L2316 "benchmark · - · gate3-v1 · sealed_items = 120.0")). Any change voids the seal; `python scripts/gate3_set.py verify` re-checks every target file against the manifest.
- **Silent renders:** 0 redrawn.
- **P3 files used:** directinput_01, 04, 05, 06, 07, 09, 10, 11, 12. **Held back as the reserve for a second set:** 02, 03, 08.
- **Limit, stated plainly:** the nine files hold only about 6.5 minutes of usable playing, which is about 31 non-overlapping 10 s windows. 120 items therefore share notes: two items on one file start at least 5 s apart for 43 items and 1.5 to 4 s apart for the rest (counts in the manifest), always with a different model or setting. Item outcomes are not fully independent; the effective number of distinct playing passages is nearer 30 to 40 than 120.

## P3 is read: what happened before, what this adds

The task brief called this the first time P3 is read. It is not quite: `scripts/sep_stress.py` (separation, 2026-10-16, ledger `sep-stress-v1`) used P3 clips 02, 03 and 08 as the guitar in the separation stress test (a numpy stand-in drive, synthetic accompaniment); no matcher, predictor or training saw them, and they are the held-back reserve here. The DI manifest builder also computed descriptors for P3 clips. This task read P3 audio to count usable excerpts (no labels) and to render the set. P3 is kept out of every training path: the drive-dataset-v1 and slot-dataset eligible-file lists exclude it, `dry_rebuild` refuses it, and `tests/test_gate3_set.py::test_p3_not_in_any_training_plan` checks that no plan.json under cache/ names a P3 file.

## Proposed bars (registered in [DECISIONS 2026-10-25, before the set was drawn](../../docs/DECISIONS.md#2026-10-25-benchmark-t223-gate-3-bars-proposed-registered-before-any-set-is-drawn-not-approved "2026-10-25 (benchmark, T223): Gate 3 bars PROPOSED, registered before any set is drawn (not approved)"))

| Id | Bar | Anchor (measured, not a target) |
|---|---|---|
| G3-1 | model top-1 >= 0.50 | chance 0.017; [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") blind 0.39; retrained heads on an unseen player 0.33 to 0.75 |
| G3-2 | model top-3 >= 0.70 | [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") blind 0.61, same player 0.73 |
| G3-3a | Drive within +-0.15 on right-model items >= 0.60 | [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") blind 0.51, same player 0.65 |
| G3-3b | mean share of all free non-level knobs within +-0.15 on right-model items >= 0.50 | constant middle guess about 0.3 |
| G3-4 | each of the above beats every baseline by >= +0.15, paired, exact McNemar p < 0.05 | baselines: random model at middle knobs; true model at middle knobs; the [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") AFx-ordered arm |
| G3-5 | ear: >= 30 decided trials, >= 6 repeats, the prediction judged closer than the best baseline's pick in >= 70% | 21 of 30 gives p = 0.043 |

Size: 120 items give a 95% interval of about +-0.09 on a rate near 0.5 (wider with the shared notes), and well over the 50 pairs the audit asks for a 0.15 difference. The ear step needs a draw of about 40 items to reach 30 decided trials; that draw is made at scoring time. Block category is trivial for a drive-only set and is dropped. One scoring attempt per frozen method; a changed method needs a new set.

## Scoring (not run)

`python scripts/gate3_set.py score --predictions <json> [--reference <json>] --launched-by <the lead's launch reference> [--bars-approved <DECISIONS ref>]`. It refuses to run without `--launched-by`. Predictions: `{"method": name, "items": {"g3-001": {"ranked": [model, ...], "values": {knob: value}}}}` with `values` the settings of the first-ranked model in the block's native units. Without `--bars-approved` the output says the bars are proposed, not approved. Tested on made-up data only (`tests/test_gate3_set.py`).
