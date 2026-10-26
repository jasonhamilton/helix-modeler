# Reverb estimator confirmation, [T232](../../board/TASKS.md#t232 "Pre-registered confirmation of the T231 reverb estimator on Mother…") (built; waiting for the human)

Owner: tone-metrics. Date: 2026-10-25. **Measurement only; nothing is adopted; nothing scored.** [T231](../../board/TASKS.md#t231 "Blind reverb estimator") built a blind reverb estimator and found, after the fact, that it agreed with the human's [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") reverb ranks on the songs where it could read a tail. A pattern found after the fact can be luck. This task tests it properly: the estimator names its pick in writing before the human listens, and the human ranks a fresh blind pack. Songs: Mother and Territorial Pissings, the two whose separated stems show a readable tail ([T231](../../board/TASKS.md#t231 "Blind reverb estimator")). By the human's ruling (2026-10-25, "do not focus on the separator") the stems are used as they are. Song audio stays local.

## What was registered, before any page existed

Registration sha256 **`de5c7f7a0a62db813a1f7ab0548344b3508c0456af6006377930258090fb6b6e`** (written 2026-10-25 23:22Z, file `cache/ratings/reverb-confirm-v1.prereg.json`, text also in `cache/reverb-confirm-t232/prereg.txt`). The hash is pinned in the script, shown on every page and in the ledger. The 29 readings and distances per song are sealed in `cache/reverb-confirm-t232/sealed/estimates.json` (sha256 `7eb2964318829e84...`). The fresh readings match [T231](../../board/TASKS.md#t231 "Blind reverb estimator")'s exactly (deviation 0).

| song | stem reading (full song) | named closest | named furthest |
|---|---|---|---|
| Mother (68 s stem) | decay 0.70 s, tail level -16.5 dB | **room-m35-short**: Room, Mix 0.35, short decay (reads 0.73 s, -16.4 dB; distance 0.12) | **plate-m50-long**: Plate, Mix 0.50, long decay (1.64 s, -10.8 dB; 3.98) |
| Territorial (143 s stem) | decay 0.90 s, tail level -13.6 dB | **room-m50-short**: Room, Mix 0.50, short decay (1.13 s, -13.4 dB; 0.79) | **room-m25-long**: Room, Mix 0.25, long decay (1.82 s, -24.7 dB; 4.55) |

How to read the numbers. "Decay" is how long the tail takes to die away by 60 dB. "Tail level" is how loud the tail is 0.1 s after a note stops, compared with the note (closer to 0 means more reverb). The distance is the fixed [T231](../../board/TASKS.md#t231 "Blind reverb estimator") rule: a 35% difference in decay counts the same as 5 dB of tail level. It was not tuned here.

Things the registration discloses, because they cut both ways:
- **Territorial's named furthest is a candidate the human already ranked joint best in [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")")** (value 2.0, the best on that page). The estimator says it is the worst of 29. If the human again ranks it near the top, that counts against the estimator.
- [T231](../../board/TASKS.md#t231 "Blind reverb estimator")'s after-the-fact check with this same rule and full-stem reference gave rho 0.77 on Mother and **-0.36 on Territorial**. Territorial is the harder test.
- Neither of Mother's named items was in the human's [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") pack.
- The estimator reads decay length and tail level only; it cannot tell Room from Plate from Hall ([T231](../../board/TASKS.md#t231 "Blind reverb estimator")). That both closest picks are Room says nothing about type.

## The analysis, as registered

1. **The listener first.** One item per song is repeated under another letter. Report the gap between its two ranks (chance 2.67). A gap of 3 or more marks the song as not reproducible; the song stays in the analysis.
2. **Primary 1, the named closest.** Its value is its average rank over the 7 clips, with the repeat averaged. Report it against the median of the six items. One-sided permutation test, 20,000 draws, the human's values shuffled within each song, pooled over both songs. The named furthest is the mirror test (secondary).
3. **Primary 2, the order.** Spearman rank correlation between the human's six values and the six distances, per song and pooled (the mean of the two). Same permutation draws, one-sided and two-sided. With two songs of six items, the one-sided 5% point of the pooled correlation is 0.51.
4. **Decided pairs.** Of the 15 pairs per song, those the human ranked equal are ties, excluded and reported. Agreement is the share of the rest that the distance orders the same way as the human.
5. **Proposed reading (a proposal for the human, not a bar):** "confirmed" if the named closest is in the top 2 of 7 on **both** songs, **and** the pooled correlation is above 0 with one-sided p below 0.05. If the human ranked at random, the first part alone would happen about 8% of the time. A self-check on made-up answers (in memory, not saved) showed the limit of the named-closest test: even a perfect result, the named closest ranked first and second on both pages, gives only p = 0.04. So that test can at best just reach 0.05 with two songs.

## The pack

Per song, 7 clips:
- the named closest and the named furthest;
- 4 others, drawn by `numpy default_rng(232)` from the remaining grid candidates. Every allowed set of four covers all three types, all three Mix values and all three decay lengths: 1,390 such sets on Mother, 1,355 on Territorial. This is a fresh draw, not the [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") letters;
- 1 repeat of one of the six.

Left ear is the song's guitar (the [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") 10 s stem excerpt); right ear is the candidate. Everything is loudness-matched to -23 LUFS with one common peak guard. Letters are random. The key is sealed at `cache/ratings/reverb-confirm-v1.key.json`, with its hash in `cache/reverb-confirm-t232/key.sha256`. There is an answer sheet per song. 0 new renders: every clip is audio already on disk.

## For the human (short)

1. Open http://127.0.0.1:8000/local/ratings/reverb-confirm-v1/index.html with headphones.
2. On each of the two pages, rank all seven clips 1 (closest to the song's guitar) to 7. Judge the space and ambience and the tone overall, not the notes. Ties are allowed. One clip is a repeat; rank it as if it were new.
3. On the index press **Download all answers**. Leave the file in Downloads (or move it to `reports/local/ratings/`).
4. Run `.venv/bin/python scripts/reverb_confirm_t232.py score`. It refuses to run if the registration, the sealed readings or the key changed since the build. Please do not open anything under `cache/ratings/` or `cache/reverb-confirm-t232/` before then. The named picks above are public by design; which letter each one is, is not.

## Files, ledger

- Script: `scripts/reverb_confirm_t232.py` (`register | build | ledger | score`). The estimator `src/tonematch/metrics/reverb_blind.py` is unchanged.
- Pages: `reports/local/ratings/reverb-confirm-v1/`. All pages and clips return HTTP 200 from the local server (checked).
- Ledger: `benchmark / - / reverb-confirm-v1 / trials_built = 2`, 0 renders, with the registration hash and the named picks.
- `proc_check` was clean before and after. No contract, loss, weight or default was changed; `data/approved-methods.json` was not touched. The human's [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") answers were read only for the registration's disclosure: [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") had already been scored, and [T231](../../board/TASKS.md#t231 "Blind reverb estimator") had used them.
