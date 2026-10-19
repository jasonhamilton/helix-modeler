# Drive scores with a different guitar ([T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive"))

Owner: `tone-metrics`. Written 2026-10-18. This is a **measurement, not an admission**: no score weight changes because of it. The plan was written into `docs/DECISIONS.md` ("[T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive"): registration of the different-guitar test on drive") before any number was computed.

## The question

In the human's rating sessions (v2 and v3, pooled 46 decided pairs), three scores agreed with the ear most often: the learned effects embedding `embed_afxrep` (36 of 46), the spectrum-shape score with an EQ correction `ltas_eqfit` (34 of 46) and the plain spectrum-shape score `ltas_tilt` (34 of 46). In those sessions the target and both candidates were the **same guitar notes**. In real use they never are: the song's guitar is someone else playing other notes on another guitar. So the question is: **does any of these scores still pick the right drive setting when the target and the candidates are played on different guitar recordings?** `mrstft_v2`, the fine-detail score that needs the same notes, is the control. It is expected to fail.

## Short answer

- **`embed_afxrep` survives a guitar change for the choice of model, not for the knob settings.** With another player on another guitar it puts a setting of the right model first 67% of the time, against 30% by guessing. It beats a near-twin model 90% of the time and a different kind of pedal 93% of the time. It cannot tell knob settings apart across guitars. The guitar change moves it about as much as a big knob change (1.7 times as much), and 30 times as much as a small one.
- **`ltas_eqfit` and `ltas_tilt` do not survive.** Across guitars they put the right model first 30% and 34% of the time, which is what guessing gives (30%). They beat a random wrong candidate only about 7 times in 10. For them the guitar change is about as large as swapping to a pedal of a different kind. Their good agreement with the ear came from same-note comparisons and does not carry over.
- **The control `mrstft_v2` fails, as expected.** Right model first 33% of the time, again at guessing level.
- **The same player playing other notes is almost as hard as another player on another guitar.** All four scores lose most of their accuracy already when only the notes change. The guitar adds a little on top, most visibly for `embed_afxrep`: right model first 74% with the same player, 67% with another.

Nothing is adopted. This supports what [T176](../../board/TASKS.md#t176 "Drive matching: stop refining a knob within about +-0.15 on…") already does: spend renders on model and family choice, not on fine knob refinement, when the target's own dry signal is not available.

## What was compared

- **Sounds.** Helix Native drive blocks, already rendered for the drive specialist (`cache/drive-specialist`). Each preset was rendered through 8 guitar recordings of 10 seconds from the Guitar-TECHS library: 4 by player P1 (a Les Paul-type guitar, bridge pickup) and 4 by player P2 (a superstrat, neck pickup). The guitars and pickups are from the Guitar-TECHS paper. No new renders were made.
- **Models.** The 25 drive models of the training half of `helix-trust-v2`. Time-varying, novelty, octave and bass models were left out, as in the rating sessions. No test-half model and no sealed answer was read.
- **Targets.** 150 targets: 6 settings of each model, each on one of the 8 recordings.
- **Candidates.** For each target, about 86 candidates, all played on one *other* recording:
  - the true preset;
  - **small knob change**: the same model with no knob moved more than 0.175 of its range (about 3 per target);
  - **medium knob change**: 0.175 to 0.375 (about 6);
  - **large knob change**: over 0.375 (about 15);
  - **near twin**: every setting of the other models in the same look-alike group from `docs/notes/distortion-circuits.md`, for example Scream 808 against Stupor OD, or Triangle Fuzz against Bighorn and Ballistic Fuzz (about 32);
  - **same class**: two other models of the same kind (boost, overdrive, distortion or fuzz), 5 settings each;
  - **different class**: four models of other kinds, 5 settings each.
  Volume knobs were not counted as a knob change, and every clip was set to the same loudness first, as in the rating sessions.
- **Two kinds of recording change.** "Same player": the target's player and guitar, but another recording, so other notes (3 recordings). "Other guitar": the other player on the other guitar (4 recordings).

## The numbers

Each figure is over targets times recordings. The ranges in brackets are 95% intervals from resampling the 25 models, because the 6 targets of one model are not independent.

### How often the true preset comes first

Guessing gets rank-1 right about 1.2% of the time (1 in about 86) and "right model first" about 30% of the time.

| Score | Same player, other notes: true preset first | Other guitar: true preset first | Other guitar: true preset or a small knob step first | Other guitar: a setting of the right model first | Other guitar: beats a random wrong candidate |
|---|---|---|---|---|---|
| `embed_afxrep` | 0.18 (0.12-0.26) | **0.13** (0.08-0.18) | 0.23 (0.19-0.28) | **0.67** | **0.88** (0.85-0.90) |
| `ltas_eqfit` | 0.04 (0.02-0.07) | 0.05 (0.02-0.08) | 0.07 (0.04-0.10) | 0.30 | 0.71 (0.66-0.76) |
| `ltas_tilt` | 0.05 (0.02-0.08) | 0.03 (0.02-0.05) | 0.09 (0.07-0.12) | 0.34 | 0.67 (0.62-0.71) |
| `mrstft_v2` (control) | 0.09 (0.05-0.13) | 0.04 (0.02-0.07) | 0.08 (0.05-0.12) | 0.33 | 0.71 (0.66-0.75) |

"A setting of the right model first" is worked out from the table of which candidate came first when the truth did not. The registration did not name it as a separate measure.

### Against each kind of wrong candidate (other guitar)

How often the true preset beats one wrong candidate of that kind. 0.5 is a coin toss.

| Score | Small knob | Medium knob | Large knob | Near twin | Same class | Different class |
|---|---|---|---|---|---|---|
| `embed_afxrep` | 0.60 | 0.71 | 0.82 | **0.90** | **0.95** | **0.93** |
| `ltas_eqfit` | 0.51 | 0.63 | 0.70 | 0.71 | 0.75 | 0.75 |
| `ltas_tilt` | 0.54 | 0.62 | 0.66 | 0.66 | 0.72 | 0.70 |
| `mrstft_v2` | 0.43 | 0.61 | 0.71 | 0.71 | 0.76 | 0.75 |

The true preset beats **every** near twin (all 25 settings of each look-alike model) on 71% of targets with `embed_afxrep` (0.62 to 0.81), against 23% to 35% for the others.

### How much the guitar change moves each score (the invariance ratio)

The ratio is the distance from the target to the true preset played on another recording, divided by the typical distance to a wrong preset played on the target's own recording. Below 1 means the guitar change moves the score less than that kind of wrong preset does, so the score can still see the difference. The figure is the median, for the other-guitar case.

| Score | Small knob | Medium knob | Large knob | Near twin | Same class | Different class |
|---|---|---|---|---|---|---|
| `embed_afxrep` | 30 | 4.5 | 1.7 | **0.98** (0.70-1.27) | **0.79** | **0.68** |
| `ltas_eqfit` | 18 | 5.5 | 2.4 | 2.0 | 1.8 | 1.4 |
| `ltas_tilt` | 8.0 | 2.5 | 1.4 | 1.2 | 1.15 | 0.98 |
| `mrstft_v2` | 14 | 4.8 | 2.8 | 2.2 | 1.8 | 1.7 |

Only `embed_afxrep` has ratios at or below 1, and only against other models. Against knob changes of the same model, every score moves more with the guitar than with the knob. With the same player the pattern is the same, a little better for `embed_afxrep` (near twin 0.69, different class 0.56).

### Does the order of the candidates survive?

Rank correlation (Spearman; 1 means the same order) between the candidate order on another recording and the order on the target's own recording.

| Score | Same player | Other guitar | Between two other-guitar recordings |
|---|---|---|---|
| `embed_afxrep` | **0.80** | **0.79** | 0.80 |
| `ltas_eqfit` | 0.45 | 0.47 | 0.57 |
| `ltas_tilt` | 0.45 | 0.42 | 0.51 |
| `mrstft_v2` | 0.61 | 0.51 | 0.68 |

`embed_afxrep` keeps most of its order across a guitar change. The spectrum scores keep under half.

### By kind of pedal (other guitar)

| Score | Boost | Overdrive | Distortion | Fuzz |
|---|---|---|---|---|
| `embed_afxrep`: true first / beats a random wrong one | 0.04 / 0.82 | 0.11 / 0.88 | 0.18 / 0.87 | 0.14 / 0.90 |
| `ltas_eqfit` | 0.00 / 0.52 | 0.03 / 0.67 | 0.08 / 0.75 | 0.05 / 0.79 |
| `ltas_tilt` | 0.00 / 0.50 | 0.02 / 0.60 | 0.04 / 0.72 | 0.06 / 0.76 |

Boosts are hardest for every score: a boost barely changes the tone, so the guitar's own sound dominates. With a boost the spectrum scores are at a coin toss.

## What this means

1. **For choosing the model without the dry signal**, `embed_afxrep` is the only one of the three worth carrying forward. It tells look-alike models and different kinds of pedal apart across guitars about as well as with the same guitar.
2. **For setting the knobs without the dry signal**, none of these scores works. A large knob move is still smaller, to every score, than the change from one guitar to another. Settings need either the user's own lined-up take (then `mrstft_v2` works, as Gate 2 showed) or a different approach, such as the planned predictor (`docs/notes/predictor-design.md`).
3. **The rating agreement of `ltas_eqfit` and `ltas_tilt` does not transfer.** They followed the ear when the notes were the same. Across guitars they rank models no better than chance. They should not be used as no-DI scores on drive.
4. The human heard small knob steps as the same sound 71% of the time (rating v3). So failing on small knob steps costs little. Failing on large knob steps and on models (the spectrum scores) is what matters.

## Limits

- Helix judged against Helix. Two guitars only (P3 is held back). Ten-second clips.
- Near twins and other models are at their own cached random settings, not at a setting matched to the target as in rating v3. A twin at a matched setting is harder to tell apart, so the near-twin figures are likely optimistic.
- The "same player" recordings are different files (for example chords against scales), so "other notes" is already a large change.
- 150 targets from 25 models. The intervals are wide where the counts are small, for example boosts (18 targets from 3 models).
- Two thresholds were fixed beforehand and not tuned: the knob-move bins and the loudness match. No pass bar was set, so nothing passes or fails.

## Files and record

- Script `scripts/drive_xdi_scores.py` (`plan`, `run`, `report --ledger`), tests `tests/test_drive_xdi_scores.py`. Full tables: `cache/drive-xdi/tables.md` and `summary.json` (local).
- Run: 5,000 clips, 102,624 fine-detail pairs, 1,791 s at 4 workers with nice 10. No Native renders.
- Ledger: `kind=loss-trust`, dataset `helix-trust-v2-xdi-drive`, [2026-10-18T21:47:21](../../ledger/runs.csv?plain=1#L1308 "loss-trust · native · helix-trust-v2-xdi-drive · rank1_rate_embed_afxrep = 0.13 (first of 12 rows at this time)") (12 rows): `rank1_rate_<score>`, `pairwise_win_<score>` and `invariance_ratio_twin_<score>` for the four scores (other-guitar case; same-player values in the notes).
