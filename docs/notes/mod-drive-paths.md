# Mod and drive paths for Gate 2 attempt 3 ([T143](../../board/TASKS.md#t143 "Mod and drive paths for Gate 2 attempt 3"))

Owner: search. Code: `src/tonematch/search/family_policy.py`, `mod_family.py`, `gain_family.py`, `drive_features.py`,
`drive_specialist.py`; `scripts/drive_calibrate.py`, `scripts/drive_seed_eval.py`, `scripts/gm_compare.py`, `scripts/gm_replay.py`; switches
`--family-prior --prior-gate --keep-top --shortlist --rank-slack --gain-drive --drive-seeds --drive-shortlist` in `scripts/synthetic_v1.py`
(all off by default: the frozen attempt-2 method is unchanged draw for draw). Tests: `tests/test_family_policy.py`, `tests/test_drive_specialist.py`.
Dev evidence on `synthetic-devgm`, not gate evidence. The admitted score is untouched; if tone-metrics changes it (the gain-stage term), re-run the comparison.

## Diagnosis of the synthetic-v2 mod and drive misses (read only)

| | items | what won instead | why |
|---|---|---|---|
| mod, 8 misses | 5 | an EQ (graphic, 3-band, shelving) | a gentle EQ fits a weak or mis-phased modulation about as well as the right modulation block does under `mrstft_v2`; on 3 of the 5 the mod detector said P(mod) 1.0 and the final pick ignored it |
| | 3 | another mod model (Tremolo for Pattern, Opto for Pattern, Bubble Vibrato for Tremolo) | the loss of a free-running LFO block is a poor guide between tremolos; the specialist's own ranking has the truth in its top 2 |
| drive, 6 misses | 2 | the EQ CaliQ | P(gain) 0.99 and 0.49; same reason as mod |
| | 3 | an amp (DM4 Heavy Distortion x2, DM4 Overdrive) | the presence detector calls DM4 drives amps (P(amp) 1.0); `AmpSpecialist.pick_finalists` accepted only amp categories, so the GP-EI refiner never worked on a drive; on i056 the true drive led the screen and went to the general halving (1.404 against 0.454 for the amp that beat it) |
| | 1 | another drive | LegendaryDrive against DM4 Heavy, refined 0.333 against 0.322 |

Where the loss sits: for 8 of the 14 mod and drive misses the true model was never refined (mod broad-screen rank 9, 28, 15, 4, 17;
drive 1, 2, 15). The seeds were fine (hint at least 0.45 on all 8 mod misses, rate within a few percent); the loss is the pool and the final order.

## What was built

1. **Family policy** (`family_policy.py`, `Policy`): a category prior `tau * ln(best hint / own hint)` that acts only when the mod or gain hint is at
   least `gate` (0.9), because a high hint is trustworthy and a low one is not (it missed 4 of 12 mod and 5 of 24 gain dev items); the raw best
   `keep_top` models by broad score always stay in the refine set; a specialist `shortlist` (mod: the pool's mod models nearest the target in the [T116](../../board/TASKS.md#t116 "Modulation specialist")
   calibration table) joins the refine set; and a rank swap (`rank_slack`): when the winner is a mod model, the specialist's best-ranked refined mod
   model within `rank_slack` times its loss is put first. No loss term, weight or stored `refined` value changes.
2. **Mod family** (`mod_family.py`): the [T116](../../board/TASKS.md#t116 "Modulation specialist") specialist plus `shortlist`. `score_weight` stays 0.
3. **Gain family** (`gain_family.py`): the amp specialist whose finalists are the best (up to 3) gain models of any category within 2x of the leader,
   so drives get the GP-EI refinement; optionally drive seeds and a drive shortlist from a calibration table.
4. **Drive seeds** (`drive_features.py`, `drive_specialist.py`, `scripts/drive_calibrate.py`): 23 numbers measured against the DI (level change, tone
   curve in 15 half-third-octave bands, loud-minus-soft gain in 3 band groups, crest factor, spectral flatness, high-band share, noise floor lift),
   looked up in a table of 60 drive models x 25 random settings x 8 DI excerpts (12,000 renders, 8 recordings, 4 playing styles, no excerpt used by any
   item). A first table made on one recording did not transfer (knob error 0.26, no better than guessing; ranking at chance); eight excerpts did.

## Evidence

Dev set `synthetic-devgm` (seed 20261021, 72 items, 24 each of mod, drive, amp; full 8-category pool of 47 models; tuning half i000-i035, held-out half
i036-i071; hashes and bars in [DECISIONS 2026-10-17 T143](../../docs/DECISIONS.md#2026-10-17-benchmark-t143-synthetic-v3-sealed-quirks-recorded-near-inaudible-proposal "2026-10-17 (benchmark, T143): synthetic-v3 sealed; quirks recorded; near-inaudible proposal")). Model correct / category correct, admitted weights, 300 renders:

| | tuning, attempt 2 | tuning, frozen H4 | held-out, attempt 2 | held-out, frozen H4 |
|---|---|---|---|---|
| mod (12) | 7 / 8 | 9 / 11 | 7 / 8 | 8 / 10 |
| drive (12) | 10 / 10 | 11 / 12 | 10 / 11 | 8 / 10 |
| amp (12) | 8 / 9 | 8 / 8 | 7 / 10 | 7 / 9 |
| all 36 | 25 / 27 | 28 / 31 | 24 / 29 | 23 / 29 |
| MAE right-model, renders | 0.142, 353 | 0.140, 348 | 0.120, 353 | 0.130, 352 |

**Held-out verdict: bars 1, 2 and 4 not met; not adopted** ([DECISIONS 2026-10-18](../../docs/DECISIONS.md#2026-10-18-search-t143-held-out-result-the-frozen-mod-and-drive-paths-did-not-meet-their-bars-not-adopted "2026-10-18 (search): T143 held-out result: the frozen mod and drive paths did NOT meet their bars; not adopted")). Post hoc over both halves: the mod prior moves mod category from 16 to 21 of 24
and mod model from 14 to 17, with the same sign on both halves; the rank swap and the drive path do not carry over (drive 20 to 19 over both halves, 10 to 8 on held-out).

## How to reproduce

```
python scripts/synthetic_v1.py generate --set synthetic-devgm --seed 20261021 --items-per-category 24 --categories mod drive amp --avoid-sets synthetic-v1 synthetic-v2 synthetic-devfam synthetic-lindev synthetic-v3
# then complete the manifest pool with the other five categories (build_pools(20261021)); see DECISIONS T143
python scripts/drive_calibrate.py render --hosts 2 && python scripts/drive_calibrate.py build     # drive table, ~25 min
python scripts/synthetic_v1.py run --set synthetic-devgm --ids i036 ... --hybrid --specialists all --hybrid-f 0.4 --hybrid-k 4 --hybrid-scale 0.9 \
   --hybrid-level-free --budget 300 --weights data/weights/gate2-known-di-mrstft_v2-2.0.json --family-prior 0.04 --prior-gate 0.9 --keep-top 2 \
   --shortlist 2 --shortlist-hint 0.5 --rank-slack 2.0 --gain-drive --drive-seeds --drive-shortlist --tag <tag>
python scripts/gm_compare.py <tag> ... --half held        # counts; scripts/gm_replay.py re-orders a finished run under other final-order settings
```

## Caveats

- The calibration excerpts (eight recordings) come from the same Guitar-TECHS DI library as every item, at positions no item uses; real DI from another guitar
  would be a harder transfer test.
- The mod rank swap and shortlist use the [T116](../../board/TASKS.md#t116 "Modulation specialist") calibration ranking as a model prior, not a loss term; a mod family score in known-DI mode remains tone-metrics' decision
  (proposal in DECISIONS [T143](../../board/TASKS.md#t143 "Mod and drive paths for Gate 2 attempt 3")).
- The synthetic_v1 harness's `param_mae` is unchanged; `scripts/gm_compare.py` recomputes a right-model MAE (level knobs excluded) from `pred_values`.
