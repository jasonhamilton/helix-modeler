# Basic Pitch on the separated guitar stems: install, tuning, rebuilds ([T203](../../board/TASKS.md#t203 "Basic Pitch on all guitar stems"))

Owner: dry-signal. Date: 2026-10-22. A measurement for listening; nothing is adopted. All outputs are local (`data/songs/<id>-rebuilt/`, `cache/basic-pitch/`, both gitignored). No song audio left the machine.

## Install

- Spotify Basic Pitch 0.4.0 in its own venv: `cache/venvs/basic-pitch/` (Python 3.12, ONNX runtime 1.31). The project `.venv` is untouched.
- Recipe (the plain `pip install basic-pitch` fails here: pip hits "resolution-too-deep", and the Darwin pin wants `tensorflow-macos`, which we do not want):
  ```
  python3.12 -m venv cache/venvs/basic-pitch
  cache/venvs/basic-pitch/bin/pip install --no-deps basic-pitch
  cache/venvs/basic-pitch/bin/pip install onnxruntime numpy scipy librosa resampy pretty_midi mir_eval scikit-learn typing_extensions
  ```
  pip then warns that `tensorflow-macos` is missing and `resampy` is 0.4.3 (the pin is `<0.4.3`); both are harmless for the ONNX path (checked: inference and decoding run).
- Runtime choice: the package ships `nmp.onnx`; `scripts/transcribe_stems.py` loads that file explicitly. No TensorFlow, no CoreML. Speed: 68 s of audio in 1.8 s of network time on the M1 Pro CPU; decoding one setting of a 68 s stem takes 0.14 s.
- Network outputs (frame, onset, contour posteriors) are cached in `cache/basic-pitch/`, so a sweep never re-runs the network.

## Licence

- Basic Pitch: Apache License 2.0 (`LICENSE` in the package, Copyright 2022 Spotify AB; `NOTICE` lists Librosa ISC and mir_eval MIT parts). The ICASSP 2022 model weights (`saved_models/icassp_2022/`, including `nmp.onnx`) ship inside the same package and repository under that licence. PyPI metadata has no licence field; the LICENSE file is the evidence.
- Its venv contains: onnxruntime (MIT), numpy/scipy/scikit-learn/numba/llvmlite/joblib (BSD), librosa and resampy (ISC), pretty_midi, mir_eval, mido (MIT), protobuf (BSD), certifi (MPL-2.0, already a WARN in the allowlist), and `soxr` 1.1.0 (LGPL-2.1-or-later, pulled in by librosa; the same package and terms as the existing allowlist entry: unmodified, dynamically installed, never bundled).
- It is an external tool, not a project dependency: it is not in `pyproject.toml`, it is not imported by anything under `src/`, and `scripts/transcribe_stems.py` only calls it in a separate process or when launched by that venv's python. `.venv/bin/python scripts/licence_check.py` stays PASS (checked after the install).
- Before any bundled or frozen release, re-review soxr (LGPL) and the Apache NOTICE obligations.

## References (approximate)

Basic Pitch on the human's clean dry takes, run with range 75 to 1400 Hz and otherwise default settings, then shifted to song time with each take's `_align.json` (`take_sample0_song_s`):
- Mother: `20261018-113158_take1_dry.wav`, 533 reference notes over 0.01 to 68 s.
- DS-Sample: `20261020-130031_take2_dry.wav`, 140 reference notes over 30.04 to 50 s.

These are NOT ground truth. The human's takes are not note-perfect copies of the record, and Basic Pitch on clean DI still makes errors. A measure of how noisy the reference is: the same scoring between the human's own takes of the same part (take2 against take1 as reference and so on) gives F 0.55 to 0.58 on Mother and 0.51 to 0.56 on DS-Sample. So about 0.55 is the most any transcription of the record can score against these references, and scores near 0.30 are about half of that ceiling.

## Sweep

Scoring: note F-measure, onset within 50 ms, pitch exact (also the octave-tolerant variant, pitch class equal), precision and recall separately; offsets not scored. Stem notes are cropped to the span the take covers. Selection: the mean of the two songs' F. Core grid (960 settings): onset threshold 0.3 to 0.8 (6), frame threshold 0.2 to 0.5 (4), minimum note length 58 to 260 ms (5), range none / 75-1400 / 80-1000 / 82-700 Hz (4), melodia on/off. Then the post-filters on the 12 best core settings (1,728 more): drop below MIDI 40 or 45, drop above 76/84/88, drop notes below 0.3/0.5/0.7 of the median amplitude of the notes within 0.5 s, merge same-pitch notes with a gap of 0.05 or 0.12 s. `multiple_pitch_bends` only changes the pitch-bend events in the MIDI file, not which notes exist, so it cannot change the score; it is left off.

| Setting | Mean F | Mean P | Mean R | Octave-tolerant F | Mother F (notes) | DS-Sample F (notes) |
|---|---|---|---|---|---|---|
| Library defaults (0.5 / 0.3 / 127.7 ms / melodia on) | 0.262 | 0.361 | 0.205 | 0.291 | 0.266 (308) | 0.257 (78) |
| **Chosen** (onset 0.4, frame 0.3, min length 58 ms, melodia off, range 75-1400 Hz, quiet-neighbour filter 0.5) | **0.303** | 0.329 | 0.282 | 0.347 | 0.295 (497) | 0.311 (111) |
| Same, no quiet-neighbour filter | 0.302 | 0.326 | 0.283 | | 0.294 (509) | 0.311 (111) |
| Chosen with onset 0.5 (precision-leaning) | 0.290 | 0.386 | 0.232 | | 0.299 (311) | 0.282 (87) |
| Chosen with onset 0.3 | 0.280 | 0.262 | 0.306 | | 0.270 (754) | 0.290 (136) |
| Chosen with frame 0.4 | 0.256 | 0.336 | 0.210 | | 0.274 | 0.238 |
| Chosen with melodia on | 0.296 | 0.301 | 0.291 | | 0.292 | 0.300 |
| Chosen with min length 90 ms | 0.290 | 0.350 | 0.248 | | 0.276 | 0.303 |
| Best precision with F at least 0.27 (adds a 0.12 s re-strike merge) | 0.272 | 0.409 | 0.206 | 0.314 | 0.249 (222) | 0.293 (85) |

What the sweep says:
- The gain is small and flat: **F 0.262 to 0.303 (+0.04)**, and a wide plateau of settings sits at 0.29 to 0.30. The gain comes from recall (0.205 to 0.282), paid for with a little precision (0.361 to 0.329). The tuned setting emits 497 notes on Mother where the defaults emit 308.
- Stray notes (precision) are not fixed by any setting I tried. A higher onset threshold or a re-strike merge raises precision to 0.39 to 0.41 but costs F and recall. If the ear says strays matter more than missing notes, onset 0.5 with the rest of the chosen settings is the precision-leaning choice (P 0.386, F 0.290); that is a proposal, not run through the rebuild.
- The pitch-range and quiet-neighbour filters do nothing measurable on these stems (no notes outside the guitar range once the model's range is set; the quiet filter moves F by 0.001). The re-strike merge removes real repeated riff notes (Mother 497 to about 225 notes) and lowers F, so it is not used.
- **Leave-one-song-out does not confirm the gain.** The best setting on DS-Sample alone, applied to Mother, scores 0.202 against 0.266 for the defaults; the best on Mother alone, applied to DS-Sample, scores 0.270 against 0.258. With two songs, and references that agree with each other at 0.55, the +0.04 is within what the choice of reference could move. Sensitivity of the verdict to the reference settings (three variants of the reference decode): the tuned setting beats the defaults in every case (Mother +0.01 to +0.04, DS-Sample +0.05 to +0.07), so the direction holds; its size does not.
- Timing tolerance: at 30 ms the tuned and default F are 0.19/0.22 and 0.16/0.16 (Mother, DS-Sample); at 50 ms 0.295/0.311 and 0.266/0.257; at 100 ms 0.354/0.344 and 0.335/0.323. A large share of "misses" are timing offsets between a human's take and the record of 50 to 100 ms.

## Chosen settings

Recorded as `CHOSEN` in `scripts/transcribe_stems.py` and in `data/songs/<id>-rebuilt/transcription_settings.json`: `onset_threshold 0.4, frame_threshold 0.3, minimum_note_length 58 ms, melodia_trick false, minimum_frequency 75, maximum_frequency 1400, multiple_pitch_bends false`, filter `rel_amp 0.5` (neighbour window 0.5 s). Proposal only: the human has not approved any of it as a method. The 75-1400 Hz range is a guard that changes nothing measurable on these two stems.

## Rebuilds

`transcribe_stems.py run` for the seven stems, then `rebuild_dry.py --songs all` ([T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") bank and cleaning; full song length; three levels; a capped very-hot level, as in [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…")). Per song in `data/songs/<id>-rebuilt/`: `transcription.mid`, `rebuilt_dry_{ref,hot,veryhot}.wav`, `ab_L-stem_R-rebuilt.wav`, `report.json`; Mother and DS-Sample also `ab_L-humantake_R-rebuilt.wav`.

Alignment score (`align_takes` method: onset-envelope correlation of the rebuilt track against the separated guitar stem, whole song), with the control of the same audio rolled 3.7 s:

| Song | Length | Notes in MIDI | Score | Median corr | Lag IQR | Rolled control |
|---|---|---|---|---|---|---|
| Mother | 68 s | 497 | 0.286 | 0.322 | 35 ms | 0.057 |
| DS-Sample | 279 s | 2,398 | 0.435 | 0.445 | 10 ms | 0.069 |
| teen-spirit | 278 s | 3,173 | 0.457 | 0.457 | 0 ms | 0.266 |
| territorial | 143 s | 1,364 | -0.016 | 0.124 | 140 ms | -0.088 |
| seek-destroy | 415 s | 3,452 | -0.092 | 0.220 | 312 ms | -0.210 |
| satisfaction | 235 s | 2,576 | 0.195 | 0.205 | 10 ms | -0.074 |
| cherub-rock | 300 s | 3,858 | 0.033 | 0.107 | 75 ms | -0.159 |

Reading the table:
- Mother, 0.286, is the same as [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…")'s 0.288 with the default-settings MIDI, so the tuning did not change the rebuild's alignment to the stem (it added 190 notes). The human take1 scores 0.344 ([T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…")) or 0.357 (the same method on the take's own span).
- DS-Sample, 30 to 50 s window against the human take2: rebuilt 0.558, take 0.420 (6 windows; a short span).
- The score separates real from the control well only for Mother, DS-Sample and satisfaction. For teen-spirit the repetitive riff makes the rolled control score 0.266, so 0.457 means little. Territorial, seek-destroy and cherub-rock are fast and dense: the onset envelope of the distorted stem does not line up with isolated clean notes (a lag spread of 75 to 312 ms), and the scores are at or near chance. These are not evidence that the transcription is wrong or right; they say the metric is blind there. The ear has to judge.
- The [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") cleaning (polyphony cap 6, re-strike trim, sub-E2 drop) removed 0 to 7 notes per song, and no pitch was missing from the bank.

## Mother: facts from the lead (human's tab header and playing characteristics) and what I measured

The facts were passed on by the lead; I did not fetch any tab. Nothing from the tab's notes is used.
- **Pitch floor.** Tuning EADGBE, lowest pitch E2 (MIDI 40). With the chosen settings the lowest transcribed note is MIDI 38 (D2), one note, and the cleaning drops it; the old [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") drop of 10 B1 notes is consistent with E2 as the floor.
- **Key.** B natural minor (B C# D E F# G A) plus A#, G# and F as borrowed tones: 2 of 497 notes are flagged, both D#. Flagged only, none removed. Both flagged notes match nothing in the take1 reference (precision 0.0 on 2 notes; unflagged 0.307), which fits being errors, but 2 notes prove nothing. The note counts by pitch class are B 143, A 91, F# 82, G 82, E 57, D 39, C# 1, D# 2: the transcription is almost entirely in the key.
- **Tempo.** Onset autocorrelation on the guitar stem (16 s windows, 8 s hop) gives a lag of 0.94 s (two beats) for 0 to 32 s: 127.7 to 129 bpm. Later windows move to 130.4 (24 to 40 s), 136 (32 to 48 s), 141 (48 to 64 s); the full mix's drums give about 146 at 56 to 72 s. So both are right: the intro is 128 bpm, as the header says, and the song speeds up; `sections.json`'s single 144.01 is a whole-excerpt estimate dominated by the faster later part, and a fixed 144 grid is about 12 percent too fast at the start. The lead's later report from the tab agrees (128 intro, about 130 to 135 in verse 1, about 137 by the chorus).
- **Quantisation arm: dropped.** Because the tempo moves, a fixed grid is the wrong tool (the lead withdrew it). A grid that follows a beat track was not built.
- **Low-fret positions.** `rebuild_dry.py --prefer-position low` picks, for each pitch, the lowest fret that has a long enough bank note (open strings first). Mother with it (`data/songs/mother-rebuilt/low-position/`): alignment **0.320** against the stem (0.286 default positions; [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") 0.288; take1 0.344/0.357); on the take span 0.314 against 0.291 default. The change comes only from which bank samples are used (the transcription is identical), so what moved is attack character, not note content. On bars 15 to 20 (by the 144 bpm `sections.json` bar grid, 23.3 to 33.3 s, one 6 s alignment window only) rebuilt default 0.364, low-position 0.366, [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") 0.333, take1 0.384: too few windows to separate them.
- **Palm mutes.** The bank used is only the plain single-note and scale takes; no palm-mute samples are ever used, so the "no palm mute in intro and verse 1" point holds already.
- **Not done.** Power chords (root, fifth, octave) are rendered as separate single notes, since the bank has no chords. `song_facts` in `data/songs/mother-playalong/session.json` is NOT written: my attempt to write it with the rest of the facts command was refused by the permission system, so I left it for the lead to confirm. The facts are in this note.

## Limits

- Two songs, one reference each, references that agree with each other at about 0.55. Treat F 0.30 and the +0.04 as indicative.
- The stems are separator output from distorted mixes: bleed, smear and chords make Basic Pitch hear fewer true notes and many partials. Octave-tolerant F is 0.05 higher, so some misses are octave errors.
- Only the first 20 seconds of DS-Sample's 279 s are covered by a reference; the settings were not checked on the full-mix-like sections of the five other songs at all (no reference exists for them).
- The very-hot rebuilt files are capped to a peak of -1 dBFS (as in [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…")), so they are about +7 dB over the reference, not +12.
- The rebuild is single notes from one clean guitar. Whether it sounds like the part is for the ear; I scored nothing by listening.
