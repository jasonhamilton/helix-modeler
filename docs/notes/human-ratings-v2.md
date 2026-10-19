# Human ratings, session v2: hidden Helix setting ([T170](../../board/TASKS.md#t170 "Human rating session"))

Owner: `benchmark`. Protocol written 2026-10-18, **before the human answered anything**. The design is the human's choice ("Hidden Helix setting", DECISIONS 2026-10-18, "the rating session is rebuilt"). This is a **measurement**: nothing below admits or rejects any score, loss, weights file or method. Only the human's ears are scored against; nobody else scores them.

## Why v1 was withdrawn

v1 played the original song guitarist as the Target and renders of the human's own playing as candidates. A trial therefore judged the *performance*, not the *tone* ("the guitar playing in the sample audio is nothing like the original"). v1 is withdrawn unanswered. Its page folder holds only a one-line notice; its audio (it was song audio) is deleted. Its key stays in `cache/ratings/` (no human data in it).

## What the human does

Open the page, 29 trials. In each trial:

1. Listen to the **Target**.
2. Listen to **A** and **B**. All three are **the same guitar notes**, the human's own dry take, run through a Helix Native drive block. Only the drive setting differs, so only the tone differs. Loudness of all three is matched.
3. Press **A is closer**, **B is closer** or **Too close to call**. Judge the tone (the distortion and the overall sound), not which you like more.
4. Optionally rate each of A and B from 1 (not close at all) to 5 (indistinguishable from the Target).

## How the clips are made

- **Source audio.** Only the human's own dry takes (`data/songs/mother-playalong/takes/20261018-113158_take{1,2,3}_dry.wav`). No song audio is used anywhere in this session.
- **Excerpts.** 8 excerpts of 7 s (one per target, all different, no overlap within a take). Windows are ranked by how much of the window is active playing and by how many note onsets it has. Take 1 gives four, take 2 three, take 3 one.
- **Targets (8, one per circuit class).** Each is a single Helix drive block with a **hidden random setting**, rendered on Native from its excerpt. Drive/Gain/Sustain/Fuzz is drawn from 0.2 to 0.9 and tone controls from 0.2 to 0.8; Level is never moved.

| Class | Hidden-target model family (from [distortion-circuits.md](distortion-circuits.md)) |
|---|---|
| TS-type | Tube Screamer family |
| Klon-type | Klon / transparent family |
| RAT-type | RAT family |
| DS-1 / hard clip | DS-1 family |
| Big Muff | Big Muff family |
| germanium fuzz | Fuzz Face family |
| HM-2 / metal | HM-2 / metal family |
| clean boost | EP-booster-style clean boost |

  The exact model and setting of each target, and of every candidate, is in the hidden key. Models the circuits note lists as time-variant or novelty (bitcrusher, megaphone, Jet Fuzz, Xenomorph, Industrial Fuzz, Thrifter, the octave fuzzes, Clawthorn, the bass drives, Vital Dist, Tone Sovereign) are never used, nor any model the Native library flags as time-variant.
- **The ladder of candidates (rendered from the SAME excerpt as the target).**

| Rung | Candidate |
|---|---|
| 1 | the same model, every knob moved slightly (about 0.1) |
| 2 | the same model, every knob moved a lot (about 0.35) |
| 3 | another model of the same confusable group or circuit class, at the same drive amount |
| 4 | a model of a different circuit class, at the same drive amount (tone knobs left at their defaults; the boost class is used only for a boost target) |
| 5 | clean: the dry excerpt, no drive (for 6 of the 8 targets) |

  Each target also has a spare rung 3 and rung 4, used only if a render fails.
- **Renders.** 8 targets, each with a target, rungs 1 to 4 and two spares (56 renders), on ONE Native host, one load per render. The clean rung is the dry excerpt itself. `proc_check` before and after: 0 orphans.

## The trials (29)

| Group | Trials | What |
|---|---|---|
| Regular | 24 | 3 per target. Pairs of ladder rungs: five 1-2, five 2-3, four 3-4, two 4-5 (the adjacent pairs, 16) and three 1-3, three 2-4, two 3-5 (two apart, 8). A pair with rung 5 only goes to a target that has a clean rung. Which rung lands on side A is random. |
| Sanity | 3 | Two: the Target against the clean excerpt (the human should pick the exact copy of the Target). One: the Target against an exact second copy of itself (the human should call it too close). |
| Retest | 2 | Two regular trials again, sides swapped, same clips, shown after their originals. Different targets. |

- **Loudness.** Every clip is set to -23 LUFS (BS.1770) with one **shared peak guard** for the whole session (it turned out to be 0 dB: no clip needed lowering). The key records each clip's original RMS level and the gain applied, so `level_db` scores work on every trial. Original levels differ by up to 14 dB between a candidate and its target, so the level term is real.
- **Blind.** File names are random; the page and `trials.json` carry only opaque ids. The key (true model and setting per target, rung per candidate, sanity and retest roles, seed) is `cache/ratings/human-ratings-v2.key.json`, outside the served folder. The dry excerpts the scores need are in `cache/ratings/human-ratings-v2.di/`.
- **Disk.** 56 MB page folder + 10 MB dry excerpts = 66 MB. No render cache is kept.

## Where things are

| Item | Place |
|---|---|
| Page | http://127.0.0.1:8000/local/ratings/human-ratings-v2/index.html (served from `reports/`; the local server binds 127.0.0.1:8000 only; the file `reports/local/ratings/human-ratings-v2/index.html` also opens directly) |
| Answers | press **Download answers**, save as `reports/local/ratings/human-ratings-v2.answers.json` (the scorer also finds `human-ratings-v2.answers*.json` in `~/Downloads`) |
| Hidden key | `cache/ratings/human-ratings-v2.key.json` (do not open before finishing) |
| Play on the Stomp | `python scripts/ab_session.py play human-ratings-v2 --trial N [--gain-db X] [--only TAB]` (Target, A, B to the Stomp's USB outputs 1 and 2; **audio only, never the control channel**) |
| Score | `python scripts/ab_session.py rating-score human-ratings-v2` |
| Rebuild | `python scripts/ab_session.py rating-build-v2 [--seed N]` (code `scripts/rating_v2.py`, tests `tests/test_rating_v2.py`) |

## What is measured afterwards

`rating-score` joins the answers with the key.

1. **The human's own consistency.** Sanity trials (picked the exact copy over the clean one? called the two exact copies too close?) and retests (same candidate chosen both times?).
2. **Does the human's choice follow the ladder?** In each decided regular or retest trial, did the human pick the nearer rung (lower number) as closer? Reported overall, per pair of rungs (1 vs 2, 2 vs 3, ...), per circuit class, with the exact 95% binomial interval, plus the mean 1-5 rating by rung. This checks the ladder itself (do people hear rung 1 as closer than rung 3?). A rung order is not guaranteed to match hearing, especially 3 against 2 and 4 against 3, so a result below 100% is a finding about the ladder, not an error.
3. **Agreement of each existing score with the human's choice.** Same as v1: for each score, the share of decided trials where the score ranks the same candidate closer, exact 95% interval, two-sided p against 0.5, and Spearman against the 1-5 ratings (scores z-scored within each target's class). The list of scores is the v1 list (mrstft_v2, mrstft_v2+level_db, level_db, provisional_kit and its three terms, embed_afxrep, the four nonlinear measures, the four no-DI candidates). **Per circuit class**: the same agreement counts split by the eight classes (only 3 trials per class, so these are descriptive).
4. Ledger rows: `kind=ab`, dataset `ab-human-ratings-v2`: `agree_<score>`, `sanity_correct`, `retest_consistent`, `ladder_follow`.

Caveats that go with the numbers:

- About 24 decided trials at most: the intervals are wide. A rate near 50% means "no evidence either way".
- It is **Helix judged against Helix**, as in Gate 2 (accepted by choosing this option). It tells us which scores follow the human's ear on tone. It does not test real pedals.
- The ladder is built from knob distance and circuit class, not from hearing. Rungs 3 and 4 differ in model; two rungs can sound closer or further than their numbers say.
- One target per class, one excerpt per target, so a class result is one setting's result.
- Rung 5 (clean) is the dry take itself, not a Native render with the block off.
- Trials of different targets use different excerpts of the playing.

## Results

Answered 2026-10-18T20:24Z, all 29 trials, no 1–5 ratings. Scored by `ab_session.py rating-score human-ratings-v2`; ledger kind=ab, dataset=ab-human-ratings-v2 (20 rows). A measurement only: nothing is admitted or rejected.

- **Decided trials:** 18 A/B on regular and repeat trials; 8 "too close to call".
- **Sanity:**
  - The target copy was picked in 1 of 2 target-copy trials; the other was called a tie.
  - The near-identical pair was called a tie.
  - The repeats were consistent in 1 of 2.
- **Score agreement with the human:** every score agreed on 72–100% of decided trials, and all the 95% intervals overlap, so no score is separated from the others at n = 18.
  - presence_nodi 18/18.
  - provisional_kit, ltas_tilt, crest_flatness_hnr and embed_afxrep 17/18.
  - mrstft_v2 16/18.
  - nl_harmonic_* 13/18, the lowest.
- **The ladder:** the human picked the nearer rung on 13/18.
  - Rung 2 vs 3 went 0/4: a different model of the same circuit group at the same drive was heard as closer than the same model with the knobs moved by about 0.35.
- **Lead's reading, for the human to judge:**
  - With identical playing and clearly different tones, most scores agree with the ear. The earlier failures were dominated by performance and guitar mismatch, not by an inability to hear tone.
  - The ladder's knob-distance order does not match perception: knob settings move the sound more than model identity within a circuit group. That argues against model-id accuracy as a target.
