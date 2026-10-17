# Chain patterns ([T101](../../board/TASKS.md#t101 "Chain-pattern model for the chain builder"))

Aggregate only: no preset names, no model names. Code: `scripts/chain_patterns.py` (build),
`src/tonematch/chain_prior.py` (API). Committed output: `data/chain_prior/category_prior.json`.
Model-level frequencies are in `cache/chain-patterns/model_frequencies.json` (local, Line 6 derived).

Run: `python scripts/chain_patterns.py [--hlx-dir PATH ...] [--hls F ...] [--no-default]`.
`--hlx-dir` is recursive (.hlx and .hls) and repeatable; rerun after adding the human's own files.

## Method
Per preset: active blocks only (bypassed in the base state and in all snapshots are dropped),
ordered by `@position`; `cab0`/`cab1` follow the first/second amp; two-path presets are
`pre + (A | B) + post` with parallel branches. Categories come from the 3.82 catalog (name
heuristic when no catalog). Bigrams use START/END and branch-aware edges; the sequence model is
add-alpha smoothed (alpha 0.5) so unseen orders keep mass.

## Corpus
129 presets parsed (126 setlist + 3 golden .hlx), 128 with at least one active block, 42 two-path.
120 distinct category sequences, so exact sequences are sparse; the bigram/order statistics carry
the signal. 6-7 blocks is typical (45 and 42 presets), max 9 (cab counted as a block).

## Headline patterns
- Presence: amp 72 %, cab 71 %, reverb 71 %, drive 63 %, delay 58 %, comp 56 %, mod 42 %,
  volpan 34 %, eq 23 %, pitch 13 %, sendreturn 11 %, wah 10 %.
- Most common full sequence: volpan > drive > amp > cab > delay > reverb > comp (3 presets); every
  other sequence has at most 2. Typical shape: [wah/volpan/comp] > drive > amp > cab > [eq] > mod > delay > reverb.
- Order, first occurrence, among presets with both: drive before amp 100 % (55/55), amp before cab
  100 %, amp before reverb 94 % (64/68), amp before delay 89 % (40/45), amp before mod 58 % (21/36;
  mod is the category most often placed before the amp), delay before reverb 96 % (55/57),
  mod before delay 83 % (29/35), comp before drive 62 % (25/40).
- Reverb is last in most chains (median normalized position 1.0), delay 0.75, mod 0.5, amp 0.4,
  drive 0.2, comp 0.17, wah/volpan first.
- Counts: drive 0 / 1 / 2+ = 47 / 67 / 14 presets, delay 54 / 64 / 10, reverb 37 / 87 / 4,
  amp 36 / 85 / 7, cab 2+ in 26 (dual cab or amp plus cab block).


## Corpus update (T109): 1,932 curated presets

Category-level only. Names (models, presets, authors, artists) are in `reports/local/preset-corpus/` (gitignored).
Machine-readable aggregates: `data/chain_prior/corpus_patterns.json`; rebuilt prior: `data/chain_prior/category_prior.json`
(now with conditional sub-priors by decade, style, effect and group). Run: `python scripts/chain_patterns.py --corpus data/presets`
(about 1 minute; `--quick` fewer permutations). Library: `src/tonematch/corpus.py`, report driver `scripts/corpus_report.py`.

### Data quality
- 1,931 `.hlx` files; 3 are setlists saved with an `.hlx` extension (354 presets, 24 empty slots), expanded and counted as
  records; 1 is an encrypted container (not read, not decrypted) and 1 has a corrupt setlist payload. 207 exact duplicates
  (identical `tone`) removed, leaving 2,074; 2,068 have an active block and a Stomp-family device id. Another 123 share name
  and author with an earlier preset (variants), 163 share models and enabled pattern with different parameter values.
- Device: 2,002 HX Stomp, 70 HX Stomp XL, 2 other (a Helix-family id). Firmware 2.65 to 3.15 (every preset predates the
  3.8x the project assumes; 3.11 and 2.82 are the most common). All 3.82 catalog ids resolve (0 unknown models).
- All 2,072 Stomp-family presets fit one DSP and 8 blocks (224 use all 8; 24% are two-path). The 8-block limit counts every `blockN`
  key; a `cab0` key (1,032 presets) is the amp's own cab and does not count. Snapshots flagged valid: 3 in 1,056 presets, 1 in 817.
- Outliers (rule flags): 27 cab without an amp, 13 reverb first, 10 with four or more blocks of one category, 4 with no active block.

### Chain order (2,068 usable; 1,576 single-path used for order statistics)
Presence: amp 89 %, cab 86 %, reverb 72 %, drive 64 %, delay 61 %, comp 57 %, mod 35 %, eq 32 %, pitch 11 %, volpan 9 %.
Mean length 5.9 blocks (5.5 groups).

| position | most common categories (share) |
|---|---|
| first | comp 31, drive 30, amp 11, volpan 7 |
| second | drive 32, amp 30, cab 10, comp 7 |
| third | amp 32, cab 28, drive 9, mod 6 |
| fourth | cab 36, delay 14, amp 14, reverb 9 |
| last-2 | cab 29, amp 15, delay 14, mod 11 |
| last-1 | delay 31, cab 17, reverb 17, eq 10 |
| last | reverb 51, delay 13, comp 11, cab 9 |

Collapsing runs into groups barely moves the first positions (mean length 5.9 to 5.5); runs of 2+ occur for drive in 12 % of drive
groups, cab 10 % (dual cab), eq 8 %, mod 7 %, delay 5 %, reverb and comp 3 %, amp 1 %. With amp, preamp and cab folded into one
`amp_unit` the chain is 4.7 groups; the unit is first or second in 48 % of presets.

### Mixing and super-groups (contiguity versus the same presets shuffled)
| candidate | presets with 2+ members | contiguous | if shuffled | lift |
|---|---|---|---|---|
| amp + cab | 1,320 | 90 % | 33 % | 2.7 |
| drive + amp + cab ("gain staging") | 1,365 | 75 % | 26 % | 2.8 |
| delay + reverb ("ambience") | 755 | 86 % | 31 % | 2.8 |
| mod + delay + reverb ("time and space") | 969 | 72 % | 28 % | 2.6 |
| mod + delay | 366 | 58 % | 29 % | 2.0 |
| comp + drive | 570 | 59 % | 28 % | 2.1 |
| wah + volpan + pitch + filter + comp | 233 | 32 % | 28 % | 1.1 |

- **Fixed pairs**: amp before cab 99 %, drive before amp 98 %, delay before reverb 91 % (swap index 0.17), amp before reverb 97 %.
- **Exchangeable**: comp and drive are a coin flip (drive first 43 %, swap index 0.86); comp and pitch likewise. The front pedals
  behave as an unordered set ahead of the amp, not as a fixed sequence.
- **Floaters**: mod sits before the amp in 62 % and before the cab in 59 % of presets that have both (swap index 0.75 to 0.82), i.e.
  pedal-style or rack-style placement are about equally common. EQ has no stable home (swap index 0.37 to 0.52 against amp, cab,
  mod, delay and reverb).
- Clustering by position and neighbour profile joins eq with mod first, then comp with pitch and volpan, then drive; delay, cab,
  reverb and amp stay apart.
- **Proposed super-groups**: (1) front pedals {comp, drive, wah, volpan, pitch, filter} ahead of the amp, internal order free,
  (2) rig {drive?, amp, cab} fixed order, (3) ambience {delay, reverb} fixed order, widened to {mod, delay, reverb} with the order
  mod < delay < reverb (87 %, 93 %), (4) floating slots: eq and mod can sit on either side of the rig. The proposal is evidence
  from adjacency and contiguity, not a tested improvement of the search.

### Does folder metadata predict the chain?
Context sizes: group 1,824 presets; style 850 (29 labels); effect folder 228 (33); artist 564 (208 folders); decade only 26 (80s 14,
60s 6, 70s 4, 90s 2), too few to measure. Bias-corrected mutual information (observed minus label-shuffled) as a share of the feature's
entropy, and held-out log-loss gain in bits per preset with folds grouped by preset author:

| context | mean MI/H over features | mean held-out gain | largest held-out gains |
|---|---|---|---|
| group (artist / style / effect / misc) | 0.9 % | 0.001 | amp family 0.03 |
| style (genre folders) | 8.3 % | 0.017 | amp family 0.22, drive present 0.05, reverb present 0.05 |
| effect folder | 10.8 % | -0.010 | pitch present 0.07, amp family 0.06 |
| artist folder | 11.4 % | 0.017 | amp family 0.27, mod present 0.10 |
| preset author (not musical metadata) | 19.4 % | not applicable | n/a |

- Metadata predicts **what is in the chain** (amp family, drive, reverb, pitch) far more than **the order** or the block count. MI/H
  for the whole category sequence is 1 to 3 % for every folder context; block count is weakly predictable (MI/H 0.3 to 6 %, and no held-out gain for style, effect or artist).
- Who built the preset explains more than any musical label (author MI/H 19 %, with sequence MI/H 4.6 %). Random-fold gains
  are 1.5 to 9 times the author-grouped ones for the artist folder, so authors uploading families of near-identical presets inflate
  the apparent artist signal; only the author-grouped figures generalise.
- Reverse direction (logistic regression on chain features, author-grouped folds): the group is 49 % against a 48 % majority; the
  genre folder 27 % against 20 % across 15 classes.
- Conditional prior versus global (held-out bits per block): style +0.013 at its best backoff strength, group +0.004 to +0.007,
  effect and artist negative at every strength tried (10 to 300). The default backoff strength is 100. Conditioning by style
  is a marginal, not a material, improvement of the order model; use it only when the context is a genre label.
