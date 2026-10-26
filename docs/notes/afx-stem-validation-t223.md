# AFx-Rep against separated stems: a blind ranking session ([T223](../../board/TASKS.md#t223 "Blind validation of AFx-Rep against separated stems and Gate 3 bars + sealed P3 set"))

*Written 2026-10-25 by benchmark. The pack is built and waiting for the human. Nothing is scored, and the analysis below was registered before any trial was shown. Ledger: `benchmark / ab / ab-afx-stem-validation-v1 / trials_built` = 41.*

## Why this exists

The methodology audit (`methodology-audit-2026-10-23.md`, section 2 and recommendation 2) found that AFx-Rep distance has never been checked by ear against a **separated stem**, which is the only target the product has. Every song match since 10-22 uses it to order the final candidates. This session asks the one question that decides how far to trust that: when five candidate drive models are played on the same rebuilt dry track, does the distance put them in the order the human hears them against the song's separated guitar? It adopts nothing and sets no bar.

## What the human does (one command)

1. Open **http://127.0.0.1:8000/local/ratings/afx-stem-validation-v1/index.html** (the repo's local file server already serves `reports/`; if it is not running, start it the way the earlier rating pages were served).
2. Work through the 41 trials in order. Answers save in the browser as you go, so you can stop and resume. Plan on about 35 minutes of listening.
   - **Ranking trial (8):** the Target and five clips A to E. Give each clip a rank, 1 = closest to the Target, 5 = furthest. Use the same number twice if you cannot tell two apart.
   - **A or B trial (33):** the Target and clips A and B. Press "A is closer", "B is closer" or "Too close to call".
   - Judge only the tone of the guitar (the distortion and overall sound) against the Target, not the notes and not what you like.
3. Some trials repeat on purpose (3 rankings and 6 A or B trials) and 2 are checks; answer each as if it were new.
4. Press **Download answers** and save the file as `reports/local/ratings/afx-stem-validation-v1.answers.json`.
5. Then run, once: `python scripts/rating_rank.py score afx-stem-validation-v1` (prints the pre-registered analysis, writes `reports/local/ratings/afx-stem-validation-v1.results.{json,txt}` and ledger rows).

To hear the clips on the pedal's outputs instead of the browser: `python scripts/rating_rank.py play afx-stem-validation-v1 --trial N` (Target then A to E, or Target, A, B; `--only TA` etc.). The pedal's own preset is not touched; this is audio playback only.

The answer key (which clip is which candidate, with its AFx distance) is `cache/ratings/afx-stem-validation-v1.key.json`, outside the page folder. Do not open it before scoring.

## What was built

- **Songs (5):** mother, cherub-rock, seek-destroy, teen-spirit, territorial. ds-sample is left out up front because its target is a Helix render, not a separated stem; one more of the remaining six (satisfaction) was dropped by a seeded draw (`default_rng(223223)`) made before any rendering or listening.
- **Candidates:** per song the first five of `result.json shortlist.ranked` of the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") run `cache/matches/<song>-dryrebuild-prs7`, each at the `shortlist.rows` coarse-grid settings.
- **Dry track and target:** the candidates are rendered on `listen/di_search.wav` of that run (bit-identical to the 10 s window of `data/songs/<song>-rebuilt/playlevel-prs7/rebuilt_dry_playlevel.wav`, checked), the Target is `listen/target_stem.wav` (the separated guitar, 10 s).
- **Renders:** 25 on one Native host, nice 10, LoadGuard. The [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") run kept level-matched files for its final top 3; the new renders of ranks 1 to 3 correlate with them at 0.9999995 or better in all five songs, so the pack shows exactly what the search ranked.
- **Loudness:** the Target and five candidates of a song are matched together to -23 LUFS (one common gain, peak guard); the clips are 10 s, with 20 ms fades.
- **Blind:** letters and sides are random in every trial and every repeat; the page and `trials.json` contain no model names, ranks or distances; the key is outside the folder.
- **Trials:** 5 rankings + 3 repeated rankings (new random letters); per song the four adjacent AFx ranks (1v2, 2v3, 3v4, 4v5) and the extreme pair 1v5 = 25 A or B trials; 6 repeated A or B trials (sides swapped, at most two per song); 2 checks (the exact Target against one low-ranked candidate; the Target against itself plus noise at -60 dB). Repeats come at least 6 places after the original; no two neighbours are the same song.
- **Size:** 135 MB of audio under `reports/local/ratings/afx-stem-validation-v1/`. No song audio leaves the machine.
- **Code:** `scripts/rating_rank.py` (build, play, score); tests `tests/test_rating_rank.py`.

## Pre-registered analysis (hash `c6e177455ce8e7e2ffcc4b4bd7380bf76591cdbf25a192979accd919d33cf687`)

The text below is written into `trials.json` and the key at build time. The analysis function is `rating_rank.analyse`; it has only been run on made-up answers in the tests.

1. Question: does the AFx-Rep distance (embed_afxrep, as ranked in each [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") result.json shortlist.ranked) order the top-5 candidates of a song the way the human hears them against that song's separated guitar stem? All five candidates are rendered on the same rebuilt dry track. Measurement only; no bar, nothing is admitted.
2. Songs: the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") dryrebuild-prs7 runs minus ds-sample (its target is a Helix render, not a separated stem), minus one more song drawn by numpy default_rng(223223) from the remaining six, in the order mother, cherub-rock, satisfaction, seek-destroy, teen-spirit, territorial. The draw was made before any rendering or listening.
3. Candidates: the first 5 entries of shortlist.ranked (distance ascending), each at the values of the matching shortlist.rows entry (the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") coarse-grid setting).
4. Primary 1 (ranking): per song, human rank (1 = closest; equal ranks get the average rank; a repeated ranking of the same song is averaged) against the AFx distance; Spearman rho with average ranks for ties. Pooled statistic = the mean of the per-song rhos (equal weight). A song whose human ranks are all equal is undecided and left out (reported). p-value: one-sided permutation test (20000 draws, seed 223) permuting the human ranks within each song; two-sided also shown. Also reported: how many songs have rho > 0, and how often the human's closest candidate is the AFx top-1 (chance 1 in 5).
5. Primary 2 (pairs): over the 25 regular pair trials (4 adjacent AFx ranks + the 1v5 pair per song), the share of decided trials in which the human chose the candidate with the lower AFx distance; exact two-sided binomial test against 0.5 and Clopper-Pearson 95% interval. Adjacent pairs and the 1v5 pairs are also reported separately. Repeat trials are not added to this count.
6. Test-retest (the listener, reported first): (a) pairs: the 6 repeats (sides swapped); consistent = the same candidate chosen both times, or too-close-to-call both times; also the rate among trials decided both times. (b) rankings: Spearman between the first and second ranking of each of the 3 repeated songs, and their mean.
7. Tie rule: 'too close to call' is excluded from every decided count and always reported with its count. A pair trial where the human called a tie once and chose a side once counts as inconsistent in (a). AFx distances are never treated as ties. Differences the listener cannot reproduce are noise: an agreement rate is read against the listener's own consistency rate (the ceiling), and a song whose repeated ranking has rho below 0.5 is flagged as not reproducible.
8. Size: 25 regular pair trials give a decided count of about 20; that detects an agreement rate of about 0.8 or more at 80% power, so anything weaker is directional only. The audit asks for 30+ decided trials for a firm conclusion; this session is below that and says so. Per-song rankings (5 songs) are directional by construction.
9. Sanity trials: (1) the exact target copy against one candidate; the copy is expected to be chosen. (2) the copy against the copy plus -60 dB noise; expected 'too close to call'. A failed sanity trial is reported and marks the session unreliable; it does not change the analysis above.
10. Blind: the human is not told which AFx rank any candidate has, the letters and sides are random per trial, all clips of a song are LUFS-matched to -23 together, and the key is kept outside the page folder.

## Reading it afterwards

The listener comes first: if the human's own test-retest is weak, the AFx comparison cannot be stronger than that. With about 20 decided A or B trials and five songs the result is directional by construction. The session adopts nothing; what AFx-Rep is used for in song matching is the human's decision.
