# Human ratings, session v3: hard pairs ([T170](../../board/TASKS.md#t170 "Human rating session"))

Owner: `benchmark`. Protocol written 2026-10-18, **before the human answered anything**. The design is the human's choice ("Hard pairs", DECISIONS 2026-10-18, "rating session v3 uses hard pairs"). This is a **measurement**: nothing below admits or rejects any score, loss, weights file or method. Only the human's ears are scored against; nobody else scores them.

## Why v3

In v2 ([results](human-ratings-v2.md)) every existing score agreed with the human on 72 to 100% of the 18 decided pairs and all the 95% intervals overlapped, so v2 could not tell good scores from bad ones: the pairs were easy and every score got them right. v3 is built from the opposite kind of trial, the ones where the scores **disagree with each other**. A score that follows the ear wins those; one that does not, loses them.

## What the human does

Open the page: **46 trials**. In each:

1. Listen to the **Target**, then **A** and **B**. All three are the same guitar notes (the human's own dry take, a 7 s excerpt) through one Helix drive block. Only the drive setting differs. Loudness of all three is matched.
2. **Rate how close A is to the Target, and how close B is, 1 to 5** (1 = not close at all, 5 = indistinguishable). They are big one-click buttons, placed first on the card. They are encouraged, not forced: any can be skipped. The ratings are what tell the measures apart.
3. Press **A is closer**, **B is closer** or **Too close to call**. Many pairs are close on purpose, so "too close" is a fine answer. Judge the tone (the distortion and the overall sound), not which you like more.
4. Optional notes.

## How the clips are made

- **Source audio.** Only the human's own dry takes (`data/songs/mother-playalong/takes/20261018-113158_take{1,2,3}_dry.wav`), 10 excerpts of 7 s of real playing (same window ranking as v2: mostly active, many onsets). Windows overlapping a v2 target excerpt are excluded; all 10 are fresh. Take 1 gives three, take 2 three, take 3 four.
- **Targets (10).** Each is a single Helix drive block with a **hidden random setting** (drive knob 0.2 to 0.9, tone knobs 0.2 to 0.8, Level never moved), rendered on Native from its excerpt. Chosen leaning to where v2 was weak or ambiguous: two TS-type (Scream 808, Dhyana Drive), Klon-type (Minotaur), clean boost (Kinky Boost), two Big Muffs (Triangle Fuzz, Dark Dove Fuzz), two DS-1 family (Deez One Vintage, Hedgehog D9), RAT (Vermin Dist), Fuzz Face (Arbitrator Fuzz). Time-variant and novelty models (the v2 exclusion list) are never used. The true model and setting of each target are in the hidden key.
- **Candidate pool per target** (13 or 14 renders of the SAME excerpt, 135 in all, plus the 10 targets = 145 renders on ONE Native host):

| Kind | What | Count |
|---|---|---|
| knob | the target's own model with **one knob** moved: drive knob by -0.2, -0.1, +-0.05, +0.1, +0.2 (0.15 steps fill in when a step would leave 0.02 to 0.98), tone knob by +-0.1, +-0.2. Level held, so loudness matching handles it. | 5 to 7 |
| twin | a near-twin model of the same confusable group (distortion-circuits.md 1.4) at the **matched setting** (same drive amount, tone knobs by position) | 2 to 3 |
| same | another model of the same circuit class at the matched setting | 2 |
| diff | a model of a different circuit class at the matched setting (similar gain) | 2 |

## The pair selection rule (the key step)

Written here before any answer; also stored word for word in the key (`selection_rule`).

1. **Score everything.** Every score of the v2 report is computed for every candidate against its target, on the loudness-matched clips the human will hear: mrstft_v2, level_db (from the original levels), provisional_kit and its three terms (ltas_tilt, crest_flatness_hnr, envelope), embed_afxrep, the four nl_* measures, ltas_eqfit, texmod, mmd_cep, presence_nodi. `mrstft_v2+level_db` is mrstft_v2 plus 1e-7 times level_db, so it would double-count mrstft_v2; it takes no vote in the split (it is still scored in the report). That leaves **15 voting scores**. Note provisional_kit is the sum of its three terms, so those four are not independent.
2. **Vote.** For a pair (i, j) of candidates, each score votes for the candidate it ranks closer (lower distance). NaN and exact ties abstain. `nA`, `nB` = votes per side; `pA = nA/(nA+nB)`; **split = 4 pA pB** (1 = even split, 0 = unanimous); `min_share = min(pA, pB)`.
3. **Disputed pair:** `min_share >= 0.30` (at least 30% of the voting scores on each side).
4. **Near-threshold pair (the scores agree but the difference is small):** `min_share <= 0.15`, and the median over the scores of `|difference| / (that score's standard deviation over the target's pool)` lies in the smallest fifth of such pairs in that target.
5. **Pair type:** `class` if either candidate is a same-class or different-class model; else `twin` if either is a near-twin model; else `knob` (both are single-knob steps of the target's own model).
6. **Fill.** Quotas (kind/type): disputed knob 12, twin 10, class 10; near-threshold knob 3, twin 3, class 2: **40 regular trials**. Greedy, round-robin over the least-used targets, choosing at random among the three best-split candidates (for near-threshold, the smallest difference). A candidate is used in at most two chosen pairs of its target; two pairs that share a candidate and whose other candidates are the same knob moved the same way are near-duplicates and are never both chosen; at most 4 trials per target (5 if a quota cannot otherwise be filled). Slots a quota could not fill take the best remaining pair of the same kind in any type, and failing that the best split of any kind (marked `relaxed`; none were needed in this build).
7. **Side.** Which candidate lands on A is random. The key stores, per trial, each score's vote **already mapped to A or B**, the vote counts, the split and the difference size, so the analysis can show which scores won the disputed trials.

Scale caveat: the pool scores that drive the selection use feature scales fitted over the whole pool; `rating-score` recomputes every score from the served 16-bit clips with scales fitted over the trial clips, so a score near a vote boundary can flip. The report uses the recomputed scores; the key's votes are the selection-time record.

## The trials (46)

| Group | Trials | What |
|---|---|---|
| Regular | 40 | 32 disputed + 8 near-threshold; pair types knob 16, twin 12, class 12; 4 per target |
| Sanity | 3 | Two: the Target copy against a far (same-class or different-class) candidate; the human should pick the exact copy. One: the Target copy against the Target plus -60 dB white noise (should be "too close"). |
| Retest | 3 | Three disputed regular trials on different targets again, sides swapped, same clips, shown at least 6 places after their originals. |

- **Loudness.** Every clip is set to -23 LUFS (BS.1770) with one shared peak guard for the whole session (-0.23 dB). The key records each clip's original RMS level and gain.
- **Blind.** File names are random; the page and `trials.json` carry only opaque ids. The key (true model and setting per target, the whole scored pool, every trial's candidates, votes and role, the seed) is `cache/ratings/human-ratings-v3.key.json`, outside the served folder. The dry excerpts the scores need are in `cache/ratings/human-ratings-v3.di/`.
- **Disk.** 89 MB page folder + 13 MB dry excerpts = 106 MB. No render cache is kept.

## Where things are

| Item | Place |
|---|---|
| Page | http://127.0.0.1:8000/local/ratings/human-ratings-v3/index.html (served from `reports/`; the local server binds 127.0.0.1:8000 only; the file `reports/local/ratings/human-ratings-v3/index.html` also opens directly) |
| Answers | press **Download answers**, save as `reports/local/ratings/human-ratings-v3.answers.json` (the scorer also finds `human-ratings-v3.answers*.json` in `~/Downloads`) |
| Hidden key | `cache/ratings/human-ratings-v3.key.json` (do not open before finishing) |
| Play on the Stomp | `python scripts/ab_session.py play human-ratings-v3 --trial N [--gain-db X] [--only TAB]` (Target, A, B to the Stomp's USB outputs 1 and 2; **audio only, never the control channel**, so it is safe beside a stomp-engine soak) |
| Score | `python scripts/ab_session.py rating-score human-ratings-v3` |
| Rebuild | `python scripts/ab_session.py rating-build-v3 [--seed N]` (code `scripts/rating_v3.py`, tests `tests/test_rating_v3.py`); the build used seed 170 |

## What is measured afterwards

`rating-score human-ratings-v3` joins the answers with the key and prints the v2 report (sanity, retests, agreement of every score with the human's choice on all decided trials, Spearman against the ratings z-scored within each target) plus:

1. **Agreement on DISPUTED trials only** (the 32 trials where the scores split), per score, exact 95% interval, p against 0.5, and Spearman against the 1-5 ratings on those trials. The same for the 8 near-threshold trials.
2. **Which scores won each disputed trial** (the list of scores that agreed with the human's choice).
3. **Pooled v2 + v3 table:** v2 is re-scored once (cached in `reports/local/ratings/human-ratings-v2.trial_scores.json`) and its decided regular and repeat trials are pooled with v3's, per score, with the Spearman on the pooled ratings.
4. **By pair type (knob step, twin model, class):** how the human answered (A, B, too close), the tie rate, the mean 1-5 rating, the share of ratings that were 4 or 5, and the mean gap between A's and B's ratings: what the human heard as close.
5. Ledger rows: `kind=ab`, dataset `ab-human-ratings-v3`: `agree_<score>` (all decided), `agree_disputed_<score>`, `agree_nearthr_<score>`, `sanity_correct`, `retest_consistent`.

It stays a measurement: nothing is admitted or rejected.

## Build summary (2026-10-18, seed 170)

- 46 trials: 40 regular, 2 sanity-target, 1 sanity-near, 3 retests. Ledger: `kind=ab dataset=ab-human-ratings-v3 metric=trials_built value=46`.
- Pair types: knob 16, twin 12, class 12. Kinds: 32 disputed, 8 near-threshold, none relaxed.
- **How disputed:** among the 32 disputed pairs the minority side holds a median 43% of the voting scores (range 33% to 50%); the commonest vote splits are 9:5, 6:8 and 7:7 of 14 or 15 voting scores. The 8 near-threshold pairs are 12 to 15 votes against 0 to 2.
- 145 renders on one Native host, `proc_check` clean before and after, load stayed under 3.

Caveats that go with the numbers:

- About 32 disputed trials and many ties: the intervals will still be wide. A rate near 50% means "no evidence either way".
- The 15 voting scores are not independent (the kit and its terms; the four nl_* measures). A pair split 8:7 can have a single underlying family on each side.
- Helix judged against Helix, as in Gate 2 (accepted by choosing this option). It tests which scores follow the ear on tone, not real pedals.
- One setting per target, one excerpt per target.

## Results (answered 2026-10-18T20:58Z; scored by `ab_session.py rating-score human-ratings-v3`; ledger ab-human-ratings-v3)

A measurement only: nothing is admitted or rejected.

- **Answers:** all 46 trials, with 1–5 ratings on every trial. 28 decided A/B on regular and repeat trials; 15 ties.
- **Sanity:**
  - The target copy was picked in 2 of 2 trials; the near-identical pair was called a tie.
  - The repeats were consistent in 1 of 3; both changes were on close pairs.
- **Disputed trials (25 decided, chance 50%):**
  - embed_afxrep 18/25 (72%, p = 0.043, the only one below 0.05; uncorrected for 16 scores);
  - ltas_eqfit 17/25 (68%) and ltas_tilt 16/25 (64%);
  - mrstft_v2 10/25 (40%);
  - presence_nodi 8/25 (32%);
  - nl_harmonic_* 9/25 (36%).
- **Pooled v2 + v3 (46 decided):**
  - embed_afxrep 36/46 (78%, 95% CI 0.64–0.89);
  - ltas_tilt and ltas_eqfit 34/46 (74%);
  - mrstft_v2 27/46 (59%).
- **Spearman with the 1–5 ratings:** +0.4 to +0.58 for most scores (n = 86), so scores do follow the ratings overall. They fail on the close calls.
- **By pair type:**
  - Small single-knob steps (±0.05–0.2): 71% ties, mean rating 4.85. Mostly inaudible.
  - Near-twin models: 0% ties, rating gap 1.17. Always distinguished.
  - Different class: 21% ties, mean rating 3.25.
- **Caveats:**
  - The pairs were selected by these same scores, and the scores are not independent.
  - n is small, and 16 scores were compared, so the p values are uncorrected.
