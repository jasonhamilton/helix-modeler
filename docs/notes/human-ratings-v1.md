# Human ratings, session v1 ([T170](../../board/TASKS.md#t170 "Human rating session"))

**WITHDRAWN 2026-10-18, unanswered.** v1 compared the original guitarist with renders of the human's own playing, so it judged performance, not tone. Replaced by [human-ratings-v2](human-ratings-v2.md). Its page now holds a notice and its audio is deleted. What follows is kept as the record of the v1 design.

Owner: `benchmark`. Protocol written 2026-10-18, **before the human answered anything**. Approved by the human the same day (DECISIONS, "approved the rating session"). Under the standing rule of 2026-10-18, this note **measures and decides nothing**: no score, loss, weights file or method is admitted or rejected by anything below. Only the human's ears are scored against; nobody else scores them.

## Why

The scoring-models report ([scoring-models-report.md](scoring-models-report.md), proposal 1) found that no human rating of "which candidate sounds closer to the target" is stored anywhere in the repo. Every trust check so far asked whether a score finds the true preset on synthetic data. This session asks the question that matters: **does a score pick the same candidate a person picks?** Distortion is the only priority (DECISIONS 2026-10-18), so most trials are single drive blocks.

## What the human does

Open the page, 26 trials. In each trial:

1. Listen to the **Target** (the guitar from the song, or the DS sample).
2. Listen to **A** and **B**. Both are the same notes played through different Helix Native settings. The loudness of all three is matched.
3. Press **A is closer**, **B is closer** or **Too close to call**. Judge the tone of the guitar (the distortion and overall sound), not which one you like more.
4. Optionally give each of A and B a closeness rating, 1 (not close at all) to 5 (indistinguishable from the Target).

Every trial can also be played on the HX Stomp (`python scripts/ab_session.py play human-ratings-v1 --trial N`), which sends the Target, A and B to the Stomp's USB outputs 1 and 2. This is audio only: it never touches the Stomp's control channel.

## Where things are

| Item | Place |
|---|---|
| Page | http://localhost:8000/local/ratings/human-ratings-v1/index.html (served from `reports/`; file `reports/local/ratings/human-ratings-v1/index.html` also opens directly) |
| Answers | press **Download answers**, save as `reports/local/ratings/human-ratings-v1.answers.json` (the scorer also finds `human-ratings-v1.answers*.json` in `~/Downloads`) |
| Hidden key and DI excerpts | `cache/ratings/human-ratings-v1.key.json` and `.di/` (outside the served folder; do not open before finishing) |
| Play on the Stomp | `python scripts/ab_session.py play human-ratings-v1 --trial N [--gain-db X] [--only TAB]` |
| Score | `python scripts/ab_session.py rating-score human-ratings-v1` |

## The trials (26)

Built by `python scripts/ab_session.py rating-build --name human-ratings-v1` from audio already on disk: `cache/matches/*/listen` and 11 extra single-drive renders on one Native host (`cache/matches/Mother-0-68s-drive/listen_t170`; 10 drive models at their default settings plus the [T164](../../board/TASKS.md#t164 "Play-along single-block match") run's best preset so far, seq 23). No other render was made. All clips are 48 kHz.

| Group | Trials | Target | Candidates |
|---|---|---|---|
| Mother, single drive | 11 | the song's guitar stem, 28.3 to 38.3 s | 13 drive renders: the run's best presets (seq 12h, 14, 23) and 10 models at default settings |
| Mother, amp and cabinet | 4 | the same stem | the amp/cab match, its rank 2 and 3, the unmatched start |
| DS sample, amp and cabinet | 6 | the DS-Sample stem | match, rank 2, rank 3, unmatched start (and other runs' matches) |
| Sanity: target against an other | 2 | Mother | one side is the Target itself; the other is the clean DI, or the candidate that the provisional score ranks worst |
| Sanity: near-identical | 1 | DS sample | the Target against the Target plus noise 50 dB down |
| Retest | 2 | Mother, DS | one earlier trial again, sides swapped, a different excerpt, shown after the original |

- **Same excerpt in a trial.** Each trial cuts one 7 s window (random start inside the 10 s clip, 20 ms fades) from the Target and both candidates.
- **Loudness.** All three clips are set to -23 LUFS (BS.1770). If any would clip, all three are lowered together. The pairing of candidates is a seeded draw that favours pairs where `mrstft_v2` and the provisional kit disagree (at least 40% of each group) and uses nearly every candidate; the seed is in the key.
- **Blind.** Which candidate is A is random per trial. File names are random. The page and its `trials.json` carry only opaque ids; the key (which candidate is where, which trials are sanity or retest) is outside the served folder.

## What is measured afterwards

`rating-score` joins the answers with the key.

1. **The human's own consistency.**
   - Sanity: in the two "target against an other" trials, did the human pick the Target copy? In the near-identical trial, did the human call it too close?
   - Retest: for the two repeated trials, was the same candidate chosen both times?
2. **Agreement of each existing score with the human's A/B choice.** For each score (listed below), the fraction of *decided* regular and retest trials (not "too close", not sanity) where the score ranks the same candidate closer. Exact 95% binomial (Clopper-Pearson) interval and the two-sided exact p-value against 0.5. A score tie or a missing value drops the trial from that score's count (the count is shown).
3. **Rank correlation with the 1-5 ratings.** Spearman rho between each score (sign flipped, scores z-scored within each target so different targets are comparable) and the individual ratings, with a bootstrap 95% interval. **Positive means the score ranks like the human's ratings.**

Scores measured (all computed on the clips the human heard, target to candidate, no mask, lower is closer):

| Score | What it is |
|---|---|
| `mrstft_v2` | ear-scaled multi-resolution spectrogram distance |
| `mrstft_v2+level_db` | the same plus the level term at `match_block`'s 1e-7 tie weight. The level term uses the clips' ORIGINAL levels (before the listening files were levelled); where those cannot be recovered the trial drops out of this score and `level_db` |
| `level_db` | level difference alone |
| `provisional_kit` | `ltas_tilt` + `crest_flatness_hnr` + `envelope`, equal weights (what `match_song` and `match_block` search with) |
| `ltas_tilt`, `crest_flatness_hnr`, `envelope` | each of the three alone |
| `embed_afxrep` | AFx-Rep learned embedding distance |
| `nl_harmonic_max`, `nl_harmonic_mean`, `nl_band_rms`, `nl_inharmonic` | measures from `nonlinear.diagnose` used as scores: the largest and the mean difference in harmonic levels (harmonics 2 to 10, given the DI), the RMS difference in 1/3-octave band levels, and the difference in the share of non-harmonic energy |
| `ltas_eqfit`, `texmod`, `mmd_cep`, `presence_nodi` | the no-DI candidates of `nodi_candidates.py`; their standardising scales are fitted on this session's own clips (label-free) |

Caveats that go with the numbers:

- Only about 15 to 20 decided trials: the intervals will be wide. A rate near 50% means "no evidence either way", not "the score is bad".
- The excerpts differ between trials, so a candidate can be judged on different notes in different trials.
- The candidates are mostly poor matches by the human's own earlier ruling; the session ranks poor matches against each other. It says little about how a score behaves near a good match.
- The DS-Sample targets and the Mother stem are separated or sample audio; the Mother stem has separation artefacts.
- The 10 default-setting drive renders were not searched for this song; they only span a range of tones.
- Ledger rows: `kind=ab`, dataset `ab-human-ratings-v1`, one row per score named `agree_<score>` (value = agreement rate; the note carries n, the interval, p and the Spearman result), plus `sanity_correct` and `retest_consistent`.

## Results

Not yet answered. To be filled in after `rating-score` has run on the human's answers.
