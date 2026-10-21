# [T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain"): tuning the DS-Sample tutorial chain three ways, then by ear

Owner: search. Date: 2026-10-20. Status: built, awaiting the human's ratings (session human-ratings-v6). Nothing is adopted, admitted or frozen; the human's approvals are in DECISIONS (2026-10-20, "tune the DS-Sample reference chain three ways").

## Protocol (written before any answers)

- **Target:** the htdemucs_6s guitar stem of DS-Sample, song 0:30.1 to 0:50.0 (19.9 s, mono mid of `data/songs/ds-sample-playalong/target_guitar_stem.wav`).
- **DI:** the human's take2 (`20261020-130031_take2_dry.wav`), `take_sample0_song_s` 30.04, lag IQR 10 ms (aligned well enough for the aligned score).
- **Chain:** Scream 808 > Brit 2204 + 4x12 Greenback 25 > Simple Delay (26 ms, Feedback 0, Mix 0.5) > 10 Band Graphic > Hall (Mix 0.4, Decay 0.52), the [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp") chain.
- **Knobs tuned (approved):** Brit 2204 Drive, Bass, Mid, Treble, Presence; all ten 10 Band Graphic bands, at most +-12 dB; Scream 808 Gain and Tone. Fixed: delay, reverb, cab, 808 Level, amp ChVol and Master. 17 dimensions.
- **Three arms, each from the tutorial values, CMA-ES (`tonematch.search.cmaes`), population 20, sigma 0.12 of the range, 421 Native renders each (1 start + 21 generations), seed 0:**
  - **a:** `mrstft_v2` (frozen 2.0) on the aligned pair with both signals high-passed at 250 Hz (4th order, zero phase) and the ERB bands starting at 250 Hz, so nothing below about 250 Hz is scored. The three EQ bands below 250 Hz (31, 62.5, 125 Hz) are held at the tutorial values in this arm, because nothing scores them.
  - **b:** `mrstft_v2` (frozen 2.0), full range.
  - **c:** AFx-Rep distance (`embed_afxrep`, cosine distance of the embeddings of the whole 19.9 s clips).
- **Level guard:** `mrstft_v2` and AFx-Rep are level-blind (unit RMS, peak normalised). Each candidate's K-weighted loudness (LUFS) is compared with the tutorial render; beyond 3 dB the loss grows by 0.05 per extra dB. No arm ended near that edge (largest drift +2.89 dB, arm c). Loudness is matched again, properly, in the listening files.
- **Hosts:** at most 4 Native hosts, busy-core `LoadGuard` (`tonematch.engines.host_budget`). Arm b waited about 17 minutes while other jobs held the machine above 9.5 busy cores, so its wall time (1,330 s) is mostly waiting; the renders themselves took about 4 minutes, like the other arms.
- **Stomp confirmation:** the tutorial, each arm's best, and the tutorial a second time (repeat floor) were rendered on the real HX Stomp through take2 (edit buffer only, never flashed, the T168-fixed engine, Input on USB 5/6 for the renders).
- **Listening session `human-ratings-v6`:** Target against candidates, every pair of the four candidates once (6 pairs), plus 2 of them again with the sides swapped (8 trials). Each trial: Target, A, B, all about 20 s. The human rates A and B 1 to 5 and says which is closer. **Every clip, the target included, is set to -23 LUFS (BS.1770 K-weighted integrated loudness)**, with one shared peak guard (none was needed: the loudest peak is 0.37). It is not matched by RMS. The key (which candidate is A and B) is in `cache/ratings/human-ratings-v6.key.json` and stays hidden. No control arms (no known-dry or target-copy trial), by the standing rule for blind tests. No score picks pairs or judges answers.
- **Not in this session:** the human's current pedal settings. They were read back before anything was written and are identical to the tutorial (58 values checked, 0 mismatches; the tuned knobs differ by 0), so there is no extra candidate.

## Step 1: the Native template, and one bug found on the way

- **Capture worked** with the existing route (`scripts/native_capture_files.py`, drop-load, `--preroll-s 3`): 11.3 s capture, repeat null -321 dB, fresh-process replay -321 dB. The only unmapped values are the Simple Delay's `TempoSync1` and `SyncSelect1` (fixed switches, not tuned).
- **Bug: the first capture rendered without the cab.** The [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp") file has the amp as block type 1 with a linked cab. The Stomp accepts that and renders the cab (its output rolls off steeply above 6 kHz). Native drops the link silently: every cab control (Low Cut, High Cut, Distance, Level) was a dead CC, bit-identical renders, and Native sat 0.64 from the Stomp in mrstft_v2 instead of 0.35, with +34 dB in the 8-16 kHz band. Reference files that work on Native (known.hlx, the inject files) all use amp type 3. The tuned template is captured from a copy with the amp as type 3 and the cab's `@type` and `@path` removed (`cache/matches/DS-Sample-live/tuned/work/reference_chain_native.hlx`). The Stomp keeps getting the [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp") file unchanged. **Flag for native-engine and anyone who builds amp + cab chains programmatically: amp @type 1 + a separate cab loads on Native without the cab.**
- **CC mapping, every tuned knob (`cccheck`):** all 17 knobs have a CC and are verified in the template (808 Gain cc_9, Tone cc_10; Brit Drive cc_12, Bass 13, Mid 14, Treble 15, Presence 16; EQ 31 Hz cc_35 up to 16 kHz cc_44; range [0,1] for knobs, [-15,15] dB for the EQ). Moving each knob by about a quarter of its range changes the sound (null +2.3 to -40 dB; Drive reads above 0 dB because more drive is a different waveform, not a scaled one). The check "CC move equals a fresh load of the same value" reads -200 dB for all, but that is circular: `load` sets values by CC too. The independent check is the Stomp: see below.
- **Parity at the tutorial values, Native against the Stomp reference (`parity.json`):**

| Measure | Native against Stomp | Reading |
|---|---|---|
| mrstft_v2 distance (frozen 2.0) | 0.345 | Native to target 0.976, Stomp to target 0.986: the engines are about 3 times closer to each other than either is to the stem. The Stomp's own repeat floor is 0.0001. |
| Loudness | +0.56 LUFS (Native louder); per second -1.2 to +1.7 dB | within the 1 dB level JND on average, not every second |
| Band level, 250 Hz to 20 kHz (8 bands, equal total power) | -0.3 to +1.2 dB | inside the 1 dB parity tolerance for 7 of 8 bands; 250-500 Hz is +1.2 |
| Band 62.5-125 Hz / 125-250 Hz | -2.9 / -0.8 dB | outside tolerance at 62.5-125 Hz |
| Band 20-62.5 Hz | +6.1 dB | little energy there (the cab is cutting it at 80 Hz); read with care |
| Waveform null | about 0 dB at any lag | not a waveform match: an amp with a different phase, Sag and delay/reverb alignment. Spectral parity only. |

Per-band errors are for the whole 19.9 s clip. The same comparison at the three arms' settings (after the Stomp renders) reads 0.312, 0.271, 0.270 and level 0.3 to 0.4 LUFS, so the parity does not get worse away from the tutorial values, and the arms' predicted effect on loudness (-1.98, +0.82, +2.89 dB) matches the Stomp (-1.73, +0.97, +3.16 dB).

## Step 2: what each arm found

The scores are loss values (lower is closer). "Native" is the search render; "Stomp" is the real pedal through take2. Same settings.

| | tutorial | a (above 250 Hz) | b (full range) | c (AFx-Rep) |
|---|---|---|---|---|
| Renders / wall time | | 421 / 263 s | 421 / 1,330 s (17 min waiting on the load guard) | 421 / 328 s |
| Own score, Native | 0.7937 / 0.9768 / 0.5674 | **0.6474** | **0.6334** | **0.1869** |
| mrstft_v2 above 250 Hz, Stomp | 0.7700 | **0.6448** | 0.6439 | 0.6686 |
| mrstft_v2 full range, Stomp | 0.9860 | 0.7168 | **0.6346** | 0.6594 |
| AFx-Rep distance, Stomp | 0.5798 | 0.2912 | 0.2757 | **0.2081** |
| Loudness vs tutorial, Stomp (K-weighted) | 0 | -1.73 dB | +0.97 dB | +3.16 dB |

- Each arm is best at its own score on the pedal. The three tuned chains are close to each other in `mrstft_v2` (0.63 to 0.67 against 0.99 for the tutorial) and arm b is second on the other two scores. A gap of 0.01 in `mrstft_v2` is not known to be audible; no score is admitted here, so these numbers say nothing about which will sound best.
- Arm c ended 2.89 dB louder than the tutorial on Native (the guard's free band is 3 dB) and 3.16 dB louder on the pedal. The listening files remove that difference.

### Settings found (tutorial in the first column)

| Knob | tutorial | a | b | c |
|---|---|---|---|---|
| Scream 808 Gain | 0.500 | 0.071 | 0.331 | 0.047 |
| Scream 808 Tone | 0.500 | 0.221 | 0.000 | 0.134 |
| Brit 2204 Drive | 0.300 | 0.370 | 0.575 | 0.244 |
| Brit 2204 Bass | 0.700 | 0.362 | 1.000 | 1.000 |
| Brit 2204 Mid | 0.500 | 0.701 | 0.614 | 0.378 |
| Brit 2204 Treble | 0.400 | 0.677 | 0.953 | 1.000 |
| Brit 2204 Presence | 0.710 | 0.843 | 1.000 | 1.000 |
| EQ 31.25 Hz (dB) | 0 | 0.1 (held) | -7.0 | -11.9 |
| EQ 62.5 Hz | +1.25 | +1.3 (held) | -6.3 | +11.9 |
| EQ 125 Hz | +3.75 | +3.7 (held) | -11.9 | -11.9 |
| EQ 250 Hz | +2.25 | -11.9 | -11.9 | -11.9 |
| EQ 500 Hz | 0 | +4.8 | +11.9 | +11.2 |
| EQ 1 kHz | 0 | -8.4 | -6.0 | -2.2 |
| EQ 2 kHz | 0 | +11.9 | +11.9 | +11.9 |
| EQ 4 kHz | 0 | +1.5 | +5.3 | +11.0 |
| EQ 8 kHz | -1.55 | -11.5 | -11.9 | -11.9 |
| EQ 16 kHz | -3.0 | +5.3 | -10.5 | +8.6 |

Values are on the Native CC grid (127 steps), so the Stomp received exactly what Native played. The "held" EQ bands in arm a are the tutorial's values (0.1 is 0 snapped to the grid). Within 1 dB of the +-12 dB limit sit 3 (a), 5 (b) and 8 (c) of the 10 EQ bands: the scores keep asking for more than the allowed range, mostly for 250 Hz and 8 kHz cuts and a 2 kHz boost. That is the sign of a spectral gap the knobs cannot close, not of a precise optimum. Arm c also boosts 62.5 Hz by +11.9 dB and cuts 31 and 125 Hz by -11.9 dB; AFx-Rep is not a spectrum score, and this looks like a weakness the score has, not a tone anyone chose. The ear decides.

All four settings are saved as validated presets (`hlx_validate` 0 fails, `preset.validate` 0 fails): `cache/matches/DS-Sample-live/tuned/ds_tutorial.hlx`, `ds_a.hlx`, `ds_b.hlx`, `ds_c.hlx` (plus `.validate.md`).

## Tonal balance against the target (Stomp renders, 19.9 s)

Level of each octave-ish band relative to the stem, after the clip is set to the stem's K-weighted loudness (dB; plus means more energy than the stem):

| Band (Hz) | tutorial | a | b | c |
|---|---|---|---|---|
| 20-62.5 | -18.4 | -18.8 | -30.5 | -19.4 |
| 62.5-125 | +19.3 | +16.4 | +0.2 | +1.6 |
| 125-250 | +18.6 | +12.1 | +2.1 | +0.5 |
| 250-500 | +3.5 | -1.2 | 0.0 | -2.9 |
| 500-1k | -4.0 | -3.2 | -2.0 | -3.1 |
| 1-2k | -7.7 | -2.0 | -2.4 | -2.0 |
| 2-4k | -10.0 | -0.3 | +0.1 | +0.6 |
| 4-8k | -3.6 | -0.4 | +0.7 | +3.2 |
| 8-16k | -6.5 | -9.9 | -15.4 | -14.0 |
| 16-20k | -32.0 | -23.4 | -35.0 | -25.4 |

- **The tutorial chain** has about +19 dB more energy at 62-250 Hz than the stem and 8 to 10 dB less at 1-4 kHz (at equal loudness). With equal total power instead of equal loudness the same chain reads +17.7 / +16.9 dB at 62-250 Hz and -9.4 / -11.6 dB at 1-2 / 2-4 kHz. The lead's figures (about 24 dB and 2.5 to 5 dB) used other band edges and normalisation, so they do not map one to one; the direction and the gap agree: the low end is far too strong and the 1-4 kHz presence is too weak. Part of this is probably separation: the separator strips the guitar's bass.
- **The 20-62 Hz band** is 18 to 30 dB below the stem in every candidate, including the tutorial. The chain makes very little there (the cab's Low Cut at 80 Hz is fixed in all arms), and what the stem holds is likely separation residue. Not chased.
- **Arms b and c** match the stem within 3 dB between 62 Hz and 8 kHz (using boosts and cuts to the +-12 dB limit). **Arm a**, which cannot see below 250 Hz, left the 62-250 Hz excess nearly as large as the tutorial's (+16 / +12 dB), as intended, but fixed 1-8 kHz to within 2 dB.
- **8-20 kHz** is 6 to 15 dB below the stem in all four, the arms worse than the tutorial (a: -9.9, b: -15.4, c: -14.0 at 8-16 kHz). The cab's High Cut (8 kHz, fixed) is the cap; the stem keeps separation hiss up to 16 kHz. If the ear misses the air in the arms, the cab High Cut is the next knob.

## What is not shown, and limits

- One seed per arm. Arm a's and arm c's best kept improving until the budget ended (the last generation still set a new best), so the numbers are budget-limited, not converged.
- No parameter prior was used (the plan's MAP idea); the +-12 dB box and CMA's step size are the only guards, and many EQ bands sit on the box.
- The level guard was not tested by an arm hitting it.
- Parity: spectral only. 62.5-125 Hz is 2.9 dB off between engines; the arms' 62.5-125 Hz choices (b, c) rest on a Native that is within 0.7 dB there at their settings, and the Stomp agrees with the arms' own scores to 0.003 (a) and 0.001 (b) in mrstft_v2 and to 0.021 (c) in AFx distance.
- The Stomp repeat floor is tight: tutorial against its repeat 0.0001 mrstft_v2, null -64.8 dB. The tutorial render matches [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp")'s capture at 0.005 mrstft_v2 (null -21.7 dB; cause not investigated, not audible in a repeat).
- Pedal: 5 renders, 0 warnings, 0 mismatches. Session-close control ack true (the T059-type warning of [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp") did not recur). The tutorial chain is back in the 04C edit buffer with the Input on Main L/R (readback ok, 58 values); **04C was not restored** (the human's rule), so `scripts/ds_sample_reference_chain.py restore` is still the way back; its `session.json` is untouched.

## Files

- Script: `scripts/ds_sample_tune_t198.py` (`parity`, `cccheck`, `search --arm`, `report`, `stomp`, `analyze`, `session`); tests `tests/test_ds_sample_tune_t198.py`.
- Results: `cache/matches/DS-Sample-live/tuned/` (`work/search_{a,b,c}_seed0.json`, `work/parity.json`, `work/cccheck.json`, `work/report_native.json`, `work/analysis.json`, `stomp/*_raw.wav`, `stomp_confirm_summary.json`, `stomp_edit_buffer_before.json`, `ds_*.hlx`).
- Session: `reports/local/ratings/human-ratings-v6/` (page), `cache/ratings/human-ratings-v6.key.json` (hidden key). Play a trial on the Stomp outputs: `python scripts/ab_session.py play human-ratings-v6 --trial N`. The page: http://127.0.0.1:8000/local/ratings/human-ratings-v6/index.html
- Ledger: `search / native / ds-sample-chain-tune`, `ab / ab-human-ratings-v6 / trials_built`.
- Song audio stays local (`cache/` and `reports/local/` are gitignored).

---

# [T199](../../board/TASKS.md#t199 "Refine DS-Sample version c with the cab High Cut freed"): version c refined with the cab High Cut freed

Date 2026-10-20. Status: built, awaiting the human's ratings (session human-ratings-v7). Nothing adopted. Request (human): "refine c further with the cab high cut freed". Script: `scripts/ds_sample_tune_t199.py` (reuses the [T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain") script).

## Protocol (written before any answers)

- **Start:** c (`ds_c.hlx`, CC-snapped). **Knobs:** the 17 [T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain") knobs plus the 4x12 Greenback 25 High Cut over its catalog range (500 to 20,100 Hz), 18 in all. Same +-12 dB EQ limits and the same LUFS guard (free band 3 dB around the tutorial render, 0.05 per extra dB).
- **Score:** AFx-Rep distance, c's own score. CMA-ES, population 20, sigma 0.08 of the range, 401 Native renders per seed, 2 seeds (0 and 1), at most 4 hosts, busy-core LoadGuard.
- **Stomp:** edit buffer only, never flashed, T168-fixed engine. The buffer was read and saved first.
- **Session v7:** Target against the pair c and c2 (the buffer equalled c, so no human-edit candidate), plus one retest with the sides swapped (2 trials). Each clip, target included, is at -23 LUFS (K-weighted), not RMS. The key stays hidden. The human rates A and B 1 to 5 and says which is closer.

## Results

- **Convergence.** Native AFx-Rep distance 0.1869 (c) to 0.1743 (seed 0) and 0.1704 (seed 1). Both seeds were still creeping down in the last generation, so neither is converged; the gain is small (about 0.017). The seeds agree on the High Cut: 12,075 Hz and 11,766 Hz. A one-knob scan of c (only the High Cut moved) gives 0.1869 at 8 kHz, 0.1804 at 10 kHz, 0.1793 at 12 and 14 kHz, 0.181 at 16 kHz: the score is flat above 10 kHz, so the exact value is not pinned down. Seed 1 is c2.
- **c2 against c (High Cut 8,000 to 11,766 Hz).** 808 Gain 0.047 to 0.000, 808 Tone 0.134 to 0.433, Brit Drive 0.244 to 0.236, Mid 0.378 to 0.323 (Bass, Treble, Presence stay at 1.0). EQ: 125 Hz -11.9 to -11.0, 500 Hz +11.2 to +9.3, 1 kHz -2.2 to -3.2, 2 kHz +11.9 to +10.3, 4 kHz +11.0 to +8.9, 16 kHz +8.6 to +10.3; 31, 62.5, 250 and 8 kHz unchanged (+-11.9). Bands 31, 62.5, 125, 250 and 8 kHz still sit at or within 1 dB of the limit. The loudness is lower: c2 is 2.0 dB quieter than c on the Stomp (Native predicted 2.0).
- **Stomp (take2), AFx-Rep distance:** c 0.2076, c2 0.1968 (the same gain direction as Native, 0.1869 to 0.1704). mrstft_v2 above 250 Hz 0.6687 to 0.6638; full range 0.6595 to 0.6618 (no change worth a note). Repeat floor: c against its repeat 0.0012 mrstft_v2, null -49 dB.
- **Native against Stomp at c2:** mrstft_v2 0.275 (c 0.270); bands within 1 dB from 62 Hz to 20 kHz except 125-250 Hz -0.9; loudness +0.30 dB. The High Cut maps the same on both engines.
- **Tonal balance against the stem (Stomp, equal LUFS, dB, plus = more than the stem):**

| Band (Hz) | c | c2 |
|---|---|---|
| 20-62.5 | -19.4 | -17.1 |
| 62.5-125 | +1.6 | +4.3 |
| 125-250 | +0.5 | +2.7 |
| 250-500 | -2.9 | -2.5 |
| 500-1k | -3.1 | -3.1 |
| 1-2k | -2.0 | -2.1 |
| 2-4k | +0.6 | +0.5 |
| 4-8k | +3.2 | +3.1 |
| 8-16k | -14.0 | -7.2 |
| 16-20k | -25.4 | -13.8 |

  Freeing the High Cut closes about half the gap above 8 kHz (8-16 kHz: 14.0 to 7.2 dB under the stem; 16-20 kHz: 25.4 to 13.8 dB under), but it is still below the stem; the stem's top is mostly separation hiss. The low end rose by 2 to 3 dB at 62-250 Hz because of the equal-loudness reference.
- **Pedal:** edit buffer read and saved before any write; it equalled c (0 mismatches), so no extra candidate. 3 renders (c, c2, c repeat), no warning, close ack true. c is loaded in the 04C edit buffer with the Input on Main L/R; 04C not restored.

## Files

`cache/matches/DS-Sample-live/tuned/ds_c2.hlx` (validated: `hlx_validate` 0 fails, `preset.validate` 0 fails), `work/t199_*.json` (scan, both seeds, best, native report, analysis), `stomp/t199_*_raw.wav`, `t199_stomp_confirm_summary.json`. Session `reports/local/ratings/human-ratings-v7/`, key `cache/ratings/human-ratings-v7.key.json` (hidden): http://127.0.0.1:8000/local/ratings/human-ratings-v7/index.html. Ledger: `search / native / ds-sample-chain-tune-c2`, `ab / ab-human-ratings-v7 / trials_built`.
