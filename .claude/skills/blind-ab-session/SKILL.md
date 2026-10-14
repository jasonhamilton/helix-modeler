---
name: blind-ab-session
description: Prepare a blind listening session for the human and ingest their ratings: level-matched, trimmed, randomized A/B or MUSHRA clips of the matched preset against the reference and the baselines, an answer key kept out of the session folder, a rating sheet, and ledger rows from the results. Use for Gate 3 and later milestones; never score it yourself.
---

# blind-ab-session

Script: `scripts/ab_session.py build --song <id> --candidates <dir> [--mushra]` then `scripts/ab_session.py ingest sessions/<id>/ratings.csv` (owner: `benchmark`).

## Build

1. Inputs per song section: the reference (isolated guitar from the mix, or the user's stem), the matched preset's render, the nearest community preset's render, and the text-only AI preset's render, all through the same engine and the same DI.
2. Trim every clip to the same 6 to 10 s excerpt chosen by `separation`'s exposed-guitar windows; level-match by loudness; apply short fades.
3. Randomize the order and the labels (A/B, or MUSHRA with a hidden reference and anchor). Write the clips to `sessions/<id>/` with opaque names, and the answer key to `sessions/<id>.key.json` outside that folder.
4. Write `sessions/<id>/ratings.csv` with one row per trial for the person to fill in (preference or 0 to 100 score), and a one-paragraph `README.md` with the listening instructions.
5. Tell the person the session is ready. Do not listen, do not guess, do not describe the clips.

## Ingest

1. Join the ratings with the key; compute per-song preference of matched vs each baseline and MUSHRA means.
2. Ledger: `--kind ab --dataset songs-20 --metric ab_pref --value <share of songs where matched is preferred over the nearest preset>` plus one row per baseline and per song.
3. If the person preferred a baseline or the reference by a wide margin on a song, open a task for `tone-metrics` (the ear disagreed) with that song and section.
