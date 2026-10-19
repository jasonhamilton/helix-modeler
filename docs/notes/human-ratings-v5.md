# Human ratings v5: do the blind predictions sound right? ([T188](../../board/TASKS.md#t188 "Rating session v5"))

*Written 2026-10-19 by benchmark, before any answers. Built and waiting for the human's ratings. Nothing is scored. Ledger: `benchmark / ab / ab-human-ratings-v5 / trials_built` = 28.*

## What the human is asked

[T185](nodi-drive-e2e-v2.md) measured the no-DI drive matcher by comparing model names and knob values. That says little about how the result sounds. In v5 the ear judges it directly. Every trial has the same performance in all three clips: the **Target** is the target's true drive model and settings played on the target's own guitar excerpt (5 s), and **A** and **B** are the matcher's predicted model and settings played on that same excerpt. Only the setting differs.

- **Page:** http://127.0.0.1:8000/local/ratings/human-ratings-v5/index.html
- **Play on the pedal outputs:** `python scripts/ab_session.py play human-ratings-v5 --trial N` (T, A, B in order; `--only TA` etc.)
- **Answers:** press Download on the page and save as `reports/local/ratings/human-ratings-v5.answers.json`.
- **Score later (not before the human is done):** `python scripts/ab_session.py rating-score human-ratings-v5`.

For each trial: rate A and B from 1 (not close) to 5 (indistinguishable from the Target), then pick A closer, B closer, or too close to call. Clips are matched at -23 LUFS (all three lowered together if a peak would clip), the side of each candidate is random, and the answer key is in `cache/ratings/human-ratings-v5.key.json`, outside the page folder.

## Design

- **Targets.** 24 of the 96 [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") targets, one trial each, drawn at random with a fixed seed (188188) and split by guitar source: blind 8 human takes + 10 Guitar-TECHS, same-player 2 human + 4 Guitar-TECHS. Only Guitar-TECHS DI and the human's own dry takes are used (the build refuses any other path). No song audio.
- **Blind condition (18 trials).** The matcher saw another player's DI. Pair: the AFx-ordered top-1 (arm `afx_fam2`) against the provisional-ordered top-1 (arm `prov_fam2`), the two arms of the shortlist-with-families setting. Where both arms picked the same model and settings, the AFx top-1 is paired with the AFx third-ranked alternative. In this draw 17 of 18 differ and 1 uses the alternative.
- **Same-player condition (6 trials).** Same rule; all 6 had different picks. These are not blind in the strict sense (the matcher saw the same player), so they are reported separately.
- **Sanity and retests.** One trial with an exact copy of the Target against a far model (expected: the copy); one with the copy against the copy plus -60 dB noise (expected: too close); two blind trials repeated later with the sides swapped. Total 28 trials: 24 + 2 + 2.
- **Renders.** 96 renders (per target: the Target, 2 picks, 1 far model; the far model is used in one sanity trial) on one Native host in 31 s; 61 MB of new files; no failure. Excerpt, 0.5 s lead-in and start come from `reports/local/t185/predictions.json`.
- **Presented knobs are the matcher's.** In the blind condition the knobs come from the coarse grid (6 points), so most picks have the wrong knobs even when the model is right. That is part of what is being judged.

## Reading it later (implemented, not run)

`scripts/rating_v5.py` (`analyse`, `fmt_extras`), called by `rating-score`:

- **How often the AFx pick was judged closer:** counts of AFx closer / other closer / tie and the AFx rate with an exact sign test, for all trials and split by condition, pair type and DI source. The same table counts how often the AFx pick got the higher 1-5 rating.
- **Mean rating per arm:** AFx, provisional, alternative; also split by whether the pick's model was the true model; next to the anchor (ratings of the exact copies).
- **Score agreement table:** every existing score against the human's A/B choice, overall, by condition and by pair type, with how often each score itself prefers the AFx pick.
- Sanity and retest consistency come from the standard report.

The ear is the judge. Nothing here is a score, and the result adopts nothing.
