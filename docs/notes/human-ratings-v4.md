# Human ratings, session v4: twin models against large knob moves ([T177](../../board/TASKS.md#t177 "Human rating session"))

Owner: `benchmark`. Protocol written 2026-10-18, **before the human answered anything**. The human approved a short v4 ("go ahead", DECISIONS 2026-10-18). This is a **measurement**: it admits or rejects no score, loss, weights file or method.

## Why v4

[v3](human-ratings-v3.md) showed what the ear separates: small single-knob steps (about 0.05 to 0.2) were called a tie 71% of the time (mean rating 4.85), while near-twin models were never tied (rating gap 1.17). v4 asks the two open questions:

1. **How many rating points does a twin swap cost, and how many does a large knob move cost?** (large = 0.3 to 0.5 on Drive or Tone.)
2. **Which does the ear prefer when they are set against each other?** That tells the drive search how to trade model choice against knob error.

## What the human does

Open the page: **20 trials**. In each, listen to the Target, A and B (same guitar notes, own dry take, 7 s, loudness matched at -23 LUFS), **rate A and B 1 to 5** with the big buttons (1 = not close at all, 5 = indistinguishable; optional), and press **A is closer**, **B is closer** or **Too close to call**. Judge the tone, not which you like.

## How the clips are made

- **Audio.** Only the human's own dry takes (`data/songs/mother-playalong/takes/20261018-113158_take{1,2,3}_dry.wav`), 8 excerpts of 7 s. Windows overlapping any v2 or v3 target excerpt are avoided; 5 of 8 are fresh, 3 (targets 1, 4, 7) overlap an earlier excerpt because the takes ran out of high-activity windows.
- **Targets (8), one hidden random drive setting each** (Drive 0.35 to 0.65, Tone 0.3 to 0.7, Level never moved), from different confusable groups ([distortion-circuits.md 1.4](distortion-circuits.md)): A Scream 808, B Minotaur, C Top Secret OD, D Vermin Dist, E Deez One Vintage, G Triangle Fuzz, H Arbitrator Fuzz, K Kinky Boost.
- **Candidates per target (5 renders + the target = 6, 48 renders in all, ONE Native host):**

| Kind | What |
|---|---|
| twin (2) | near-twin models of the same group at the **matched setting** (same Drive, tone knobs by position): e.g. Screamer and Pillars OD for Scream 808 |
| move (2) | the target's own model with a **large move**: Drive by 0.3, 0.4 or 0.5 (sign random, kept inside 0.03 to 0.97); the second move is Tone by 0.3 to 0.5 where the model has a tone knob, else Drive the other way |
| far (1) | a model of a different class at the matched setting; used only in the sanity trial |

- **No score picks the pairs.** The design is fixed (the v3 pair selection by score disagreement is not used).

## The trials (20)

| Group | n | What |
|---|---|---|
| twin-vs-move | 8 | one per target: a twin against a large move, at random sides. The pit |
| twin-vs-twin | 4 | two twins of the same target (random 4 targets): is one twin closer than another? |
| move-vs-move | 4 | the two large moves of the other 4 targets |
| Sanity | 2 | the Target copy against the far model (should be picked); the Target copy against the Target plus -60 dB noise (should be "too close") |
| Retest | 2 | two twin-vs-move trials on different targets again, sides swapped, at least 6 places later |

Blind: random file names; the key (true model and setting, role of each clip, seed) is `cache/ratings/human-ratings-v4.key.json`, outside the served folder. Loudness: all clips at -23 LUFS with one shared peak guard.

## Where things are

| Item | Place |
|---|---|
| Page | http://127.0.0.1:8000/local/ratings/human-ratings-v4/index.html (also opens as the file `reports/local/ratings/human-ratings-v4/index.html`) |
| Answers | **Download answers**, save as `reports/local/ratings/human-ratings-v4.answers.json` (the scorer also finds `human-ratings-v4.answers*.json` in `~/Downloads`) |
| Play on the Stomp | `python scripts/ab_session.py play human-ratings-v4 --trial N [--gain-db X] [--only TAB]` (audio only) |
| Score | `python scripts/ab_session.py rating-score human-ratings-v4` |
| Rebuild | `python scripts/ab_session.py rating-build-v4 [--seed N]` (code `scripts/rating_v4.py`); built with seed 177 |

## What is reported afterwards

`rating-score human-ratings-v4` prints the standard report (sanity, retests, **the full list of existing scores** against the human's decided choices with 95% intervals and Spearman against the 1 to 5 ratings) plus:

1. **Per pair type** (twin-vs-move, twin-vs-twin, move-vs-move): A, B and tie counts, tie rate, mean rating, mean |A-B| rating gap, and for twin-vs-move how often the twin and how often the move was chosen as closer.
2. **Rating cost**, mean over regular trials of 1 to 5 ratings by what the candidate is: twin swap, large move (Drive, Tone, and by size 0.3/0.4/0.5), far model. **Cost = 5 minus the mean rating**, and also the mean rating of the exact target copy (the anchor from the sanity trials) minus the mean. The report states the average rating points one twin swap costs and one large move costs, with n and sd.
3. Twin-swap rating per confusable group.
4. Agreement of each score with the human, split by pair type.

## Caveats (stated before the answers)

- At most 18 decided trials (16 regular + 2 retests): intervals will be wide; a rate near 50% means no evidence either way. 15 or more scores are compared, uncorrected.
- One setting and one excerpt per target; the large moves are random sign, so Drive moves up and down are mixed.
- Helix judged against Helix, as in v2 and v3.
- The rating cost compares candidates of different targets; groups with few twins (e.g. Fuzz Face, Boosts) are noisy.
- A large move that lands near a knob limit (e.g. Drive 0.05) may differ from a mid-range one.

## Build summary (2026-10-18, seed 177)

20 trials: 16 regular, 1 sanity-target, 1 sanity-near, 2 retests. 48 renders on one Native host (`proc_check` clean before and after, load stayed near 2.5). Ledger: `kind=ab dataset=ab-human-ratings-v4 metric=trials_built value=20`. The scorer was smoke-tested on random fake answers (not kept) so it will run when the real answers arrive.

Nothing has been scored. Nobody has seen the answer key.

## Results (answered 2026-10-18T23:46Z; scored by `ab_session.py rating-score human-ratings-v4`; ledger ab-human-ratings-v4)

A measurement only: nothing is admitted or rejected.

- **Answers:** all 20 trials, with ratings. 16 decided, 2 ties.
- **Sanity:** the target copy was picked (1/1); the near-identical pair was called a tie; the repeats were consistent in 1 of 2.
- **Twin vs large move (10 trials):** the twin model was chosen 7 times, the move 2 times, and 1 tie.
- **Rating cost against 5:**
  - large Tone move **1.33**;
  - large Drive move **0.40**;
  - twin swap 0.56;
  - far model (sanity) 2.00.
- **Twin swap by group:** Fuzz Face (Arbitrator) cost most, at 3.0, n = 1. TS, Distortion+ and DS-1 4.33; Big Muff 4.67.
- **Score agreement (16 decided):**
  - crest_flatness_hnr 14/16;
  - ltas_tilt and level_db 13/16;
  - provisional_kit 12/16;
  - embed_afxrep 11/16;
  - mrstft_v2 10/16;
  - envelope, texmod, nl_harmonic_* and presence_nodi at or below chance.
- **Reading:**
  - On this human's ear, Tone moves matter about 3× more than Drive moves of the same size.
  - A near-twin model at matched settings is closer than either.
  - The score that leads changes from session to session (AFx-Rep in v3, crest/tilt in v4), and n is small.
