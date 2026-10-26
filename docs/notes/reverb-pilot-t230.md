# Reverb-model pilot, [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") ([T230](../../board/TASKS.md#t230 "Reverb-model pilot"))

Owner: tone-metrics. Date: 2026-10-25. **Measurement only; nothing is adopted.** Human approval: DECISIONS 2026-10-25, "reverb-model pilot [T230](../../board/TASKS.md#t230 "Reverb-model pilot (human 2026-10-25 \"yes\")") approved" (the human said "yes"). Local only: the pages hold song audio and stay under `reports/local/` and `cache/` (both gitignored).

**Please rank the seven pages before reading section 4 onward, and do not open anything under `cache/ratings/` or `cache/reverb-pilot-t230/sealed/` until you have downloaded your answers.**

## 1. What you do (about 20 to 30 minutes, headphones)

1. Open **http://127.0.0.1:8000/local/ratings/reverb-pilot-v1/index.html** (the local file server is running; the folder is `reports/local/ratings/reverb-pilot-v1/`). It links one page per song.
2. On each song's page there is the song's guitar alone, then seven clips A to G. Each clip is stereo: **left ear = the song's guitar** (separated from the record), **right ear = a candidate**: the drive match and fitted EQ you heard in [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") (variant c) with one reverb after it. All clips are loudness-matched (-23 LUFS).
3. Give every clip a rank, **1 = closest to the song's guitar, 7 = furthest**. Judge how close the space and ambience sound, and the tone overall, not the notes and not which you like best. Use the same number twice if you cannot tell two clips apart. **One clip on each page is repeated on purpose** under another letter; rank it as if it were new.
4. Answers save in the browser as you go. When all seven pages are done, press **Download all answers** on the index page (or Download answers on each page). Leave the file(s) in Downloads or move them to `reports/local/ratings/`.
5. Then run, once:

       .venv/bin/python scripts/reverb_pilot_t230.py score

   It prints the pre-registered analysis, your repeat consistency first, and writes `reports/local/ratings/reverb-pilot-v1.results.{json,txt}` and ledger rows. Nobody, me included, has scored or will score your answers for you.

Paper alternative: `reports/local/ratings/reverb-pilot-v1/<song>/answer_sheet.md`.

To hear a candidate on the Stomp: `cache/reverb-pilot-t230/load_list.md` lists a validated `best.hlx` for every candidate (27 grid settings per song, plus the [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") r35 and p35 presets). It names settings, not letters, so it does not tell you which clip is which, but it does tell you which settings exist; open it after you have ranked if you want to stay fully blind. **Nothing was loaded on the Stomp.**

## 2. Design

- **Songs:** the seven [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") songs: mother, seek-destroy, teen-spirit, territorial, satisfaction, cherub-rock, ds-sample.
- **Chain before the reverb:** [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") variant c (the fine-tuned drive match, then the fitted 10-band graphic EQ), audio `cache/colour-t229/<song>/audio/c.wav`. Target: the [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") `stem.wav` (separated guitar, 10 s; ds-sample 15 s).
- **Candidates per song (27):** Room, Plate, Hall x Mix 0.25, 0.35, 0.50 x Decay short, medium, long. Predelay 11 ms, LowCut 150 Hz, HighCut 3700 Hz (as [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs")). Rendered after c on one Native host (nice 10, LoadGuard before every render). Room, Plate and Hall are stereo-only on Native and are folded to mono, as in [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") and [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs").
- **Plus the two [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") settings**, r35 (Room, Decay 0.40, Mix 0.35) and p35 (Plate, Decay 0.40, Mix 0.35), as the exact [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") audio. Re-rendering r35 on Mother through this script's path gave the same audio bit for bit (maximum difference 0).
- **Pack per song (7 clips):** 4 grid candidates drawn by `numpy default_rng(230)` in song order, uniformly among the sets that hold exactly 2 Hall, 1 Room and 1 Plate and cover all three Mix values and all three Decay levels; plus r35 and p35 (so each page has two clips of each type); plus one of the six, drawn by the same generator, repeated under a new letter. Letters A to G are random per song.

### Decay values: the same measured tail length across models

The Decay knob means different things in each model, so one impulse was rendered through each model at seven Decay values (Mix 0.50, the fixed filters above) and the reverberation time measured (RT60: the time for the tail to fall by 60 dB, from the 250 Hz to 4 kHz band, Schroeder method, the -5 to -35 dB slope). Measured RT60 in seconds:

| Decay | 0.10 | 0.25 | 0.40 | 0.55 | 0.70 | 0.85 | 1.00 |
|---|---|---|---|---|---|---|---|
| Room | 0.52 | 0.62 | 0.80 | 1.02 | 1.29 | 1.62 | 2.27 |
| Plate | 0.97 | 1.12 | 1.63 | 2.53 | 5.12 | 10.98 | 17.69 |
| Hall | 0.73 | 0.92 | 1.18 | 1.60 | 2.38 | 5.00 | 26.5 |

The range all three can reach is 0.97 s (Plate's shortest) to 2.27 s (Room's longest). The three targets are spaced evenly on a log scale across it, 10% in from each end: **short 1.05 s, medium 1.48 s, long 2.09 s**. The Decay values that hit them (interpolated):

| | short (1.05 s) | medium (1.48 s) | long (2.09 s) |
|---|---|---|---|
| Room | 0.570 | 0.792 | 0.962 |
| Plate | 0.189 | 0.361 | 0.484 |
| Hall | 0.330 | 0.512 | 0.650 |

For comparison, the [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") settings at Decay 0.40 are Room 0.80 s (shorter than every Room in the grid: Plate cannot go below about 1 s, so a common grid has to start there) and Plate 1.63 s (between medium and long). The tail level at a given Mix still differs between models ([T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") note); Mix was not equalised, as the brief fixes the Mix values.

## 3. Renders (disclosed in full)

| what | renders |
|---|---|
| Decay calibration (impulses, 3 models x 7 Decay values) | 21 |
| Grid candidates (27 per song x 7 songs) | 189 |
| Reproduction check of [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") r35 on Mother | 1 |
| **Total** | **211** |

No other development renders: the r35 and p35 clips are the [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") audio, and the drive and EQ were not re-rendered (the grid renders start from `c.wav`). Wall time about 2 minutes of rendering. `proc_check` clean before and after every engine session.

## 4. Pre-registration (written before any candidate was rendered or any pack built)

- Registered text: `cache/ratings/reverb-pilot-v1.prereg.json`, written 2026-10-25T22:03:24Z, **sha256 `a7ba12a4fc3fb44a9d8e909fb7844faa1a3f3c8318f707ae481fe45c02c7b153`** (13 points: the question, songs, items, pack design, how ranks are read, the listener's repeat consistency first, the four signals, the primary test, decided pairs, descriptive factors, a proposed reading, size, blinding). It is also written into `reports/local/ratings/reverb-pilot-v1/trials.json` and the key.
- **Amendment 1**, written 2026-10-25T22:09:22Z, after the signals were computed and before any pack existed or anyone had listened: on a song where the note-tail signal is defined, a candidate whose reverb is so strong that none of its gaps counts as a real gap (it stays within 12 dB of its loud level) is treated as further from the stem than every measured candidate. Six of Mother's 29 items are like this and the registration had not said what happens to them. **Text with the amendment: sha256 `c13d90ac7b0617dfb11791ad079dd4ea1582e6e2f49fee750e532999ecd0a092`** (the one carried by the pages, the key and the ledger).
- The signal values were computed after registration and sealed: `cache/reverb-pilot-t230/sealed/signals.json`, sha256 `a46dc97176bd6fe45d5a7002e1989ab56acd765bd09bc0ca1d60970bfb236aa0`, recorded in `trials.json`; the scorer refuses to run if the file or the registration has changed. Key `cache/ratings/reverb-pilot-v1.key.json`, sha256 `2c2aab5e5b701c088ae0639039f63407320e1fa01e32e33d53dc96185c20bbd9`.

**The analysis in short.** Per song, your seven ranks become average ranks; the repeated clip's two values are averaged, giving one order of the six candidates. Reported first: how far apart you put the repeated clip (chance for two random ranks out of 7: 2.67 places on average; 3 or more places flags the song as not reproducible). Then, for each signal, the Spearman rank correlation between your order and the signal's distances on each song (1 = the same order, 0 = unrelated), the mean over songs, and a permutation test (your six values shuffled within each song 20,000 times) with a Holm correction for testing four signals. Pooled over all seven songs (primary) and over six without ds-sample. Decided pairs: of the 15 pairs per song, those you ranked differently, and how often the signal agrees; pairs you tied are left out and counted. Descriptive only: your ranks against Mix, Decay level and type, and where r35 and p35 land.

**Proposed reading, not a bar** (the human decides): a signal "agrees" if its pooled correlation is positive with a Holm-corrected one-sided p below 0.05 and it is positive on at least 5 of its songs. A small predictive model is proposed only for a signal that agrees; otherwise "Plate, Mix 0.35" stays the by-ear starting point the lead proposed.

**Size.** Six candidates per song is small: one song on its own needs a correlation of about 0.83 to mean anything, so per-song numbers are directional; the mean over seven songs is the test. One repeat per song only roughly shows how consistent the listener is, so a weak agreement can be listening noise.

## 5. The four candidate signals (distances; lower = closer to the song's guitar)

Every candidate of a song shares the same dry track, drive and EQ, so a signal changes across a song's candidates only through the reverb. Code: `src/tonematch/metrics/reverb_signals.py` (tests `tests/test_reverb_pilot.py`, 14), driver `scripts/reverb_pilot_t230.py`. Computed for all 29 items of every song before any pack was built.

1. **AFx-Rep distance** (the learned effects embedding the matcher already uses): 1 minus the cosine similarity of the stem's and the candidate's embeddings; no confidence mask.
2. **Note-tail statistics** (the [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") gap method): in the real note gaps (the rebuilt dry silent for 250 ms or more, the pre-reverb render silent too, and the stem below -12 dB re its loud level 60 to 100 ms into the gap), the median level slope after the first 100 ms and the median level 60 to 100 ms in; distance = slope difference / 30 dB per second + level difference / 6 dB. Defined only where the stem has real gaps: **mother (3 gaps), territorial (2), ds-sample (3)**; the other four songs have none (the guitar rings on, or the transcription's gap is not a real one), so this signal is left out there. On the three songs the stem's level in the gaps is flat or slowly falling (+14, +3, -4 dB/s), as [T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…") found: ring-out or separation leakage rather than a clear reverb tail.
3. **Time-specialist regressors.** The fitted Room, Plate and Hall regressors (trained on renders of each model with every knob varied) read the stem and the candidate, each against the pre-reverb render as the block input. Each gives a Decay and a Mix reading, six numbers in all; the pattern across the three carries the type. Distance = root mean square of the six differences, each divided by that regressor's own leave-one-out error (Decay 0.12 to 0.17, Mix 0.06 to 0.07 of the knob range). Caveat known before listening: for the stem, the pre-reverb render is not the stem's own dry signal ([T228](../../board/TASKS.md#t228 "Colour pilot (human 2026-10-25 \"focus on our distortion model… drive…"): such a deconvolution is not an impulse response), so the stem's readings may be noise.
4. **Envelope dip fill (my addition, chosen before listening).** Why: it is defined on every song (unlike 2), needs no sample alignment between the stem and our dry track (unlike 3), and measures what a reverb does audibly in dense playing: it fills the quiet moments between notes. Definition: 10 ms frame levels of the 300 Hz to 4 kHz band, relative to their 95th percentile, floored at -60 dB, over the frames where the rebuilt dry is playing (within 40 dB of its loud level, widened by 100 ms, so long rests are left out); the 10th, 25th and 50th percentiles; distance = mean absolute difference in dB. A first version without the "while playing" restriction was dropped before registration: in long rests our renders are digitally silent (-111 dB) while the stem keeps its leakage floor, which measured the dry track, not the reverb.

## 6. Files

| what | where |
|---|---|
| Index page | `reports/local/ratings/reverb-pilot-v1/index.html` (http://127.0.0.1:8000/local/ratings/reverb-pilot-v1/index.html) |
| One page per song | `reports/local/ratings/reverb-pilot-v1/<song>/index.html` (+ `clip_A..G.wav`, `song.wav`, `answer_sheet.md`) |
| Public session file (no labels) | `reports/local/ratings/reverb-pilot-v1/trials.json` |
| Sealed key and signals | `cache/ratings/reverb-pilot-v1.key.json`, `cache/reverb-pilot-t230/sealed/signals.json` |
| Registration | `cache/ratings/reverb-pilot-v1.prereg.json` (copy `cache/reverb-pilot-t230/prereg.json`) |
| Candidate audio | `cache/reverb-pilot-t230/<song>/audio/<type>-m<mix>-<decay>.wav` |
| Presets and load list | `cache/reverb-pilot-t230/<song>/hlx/<candidate>/best.hlx` (189, all `hlx_validate` exit 0; plus the 14 [T229](../../board/TASKS.md#t229 "Colour comparison on seven songs") r35/p35 presets referenced), `cache/reverb-pilot-t230/load_list.md` |
| Calibration | `cache/reverb-pilot-t230/calibration.json`, `render_counts.json` |

Ledger: `benchmark / native / reverb-pilot-v1 / trials_built` = 7 and `renders` = 211 ([2026-10-25T22:12:39](../../ledger/runs.csv?plain=1#L2421 "benchmark · native · reverb-pilot-v1 · trials_built = 7.0 (first of 2 rows at this time)") (2 rows)Z). The score step adds `ab / reverb-pilot-v1` rows from your answers.

## 7. Not done, caveats

- No listening and no scoring by me. No Stomp load. Nothing adopted; no contract, loss, weight or default changed; `data/approved-methods.json` untouched.
- The renders fold the stereo reverbs to mono; on the Stomp the reverb is stereo, so a preset may sound wider than its clip.
- ds-sample's target was left out of the [T223](../../board/TASKS.md#t223 "Blind validation of AFx-Rep against separated stems") session as not a usual separated stem; here it is one of the seven, and the pooled result is also reported without it.
