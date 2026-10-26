# Five songs, blind single-drive matches ([T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs"))

*Written 2026-10-22 by search. Local only: song audio, stems, DI clips and listening packs stay in `cache/` (gitignored). No lyrics anywhere. Ledger: `search / native / song-<id>` (two rows per song: `best_loss`, `predictor_p_top1`). Nothing is adopted; the human said "I will not be providing dry tracks. Do your best".*

## Method (the same for every song)

1. **Separate**: `scripts/separate.py --dataset <id>` (htdemucs_6s, mps). Stems in `cache/stems/<hash>/htdemucs_6s`.
2. **Window**: `scripts/t200_windows.py`. Every 10 s window at 0.5 s steps. Keep windows that are mostly active (0.8 of frames), whose guitar-stem loudness (K-weighted LUFS) is within 2 dB of the song's 90th-percentile window, and whose crest factor (peak over RMS of the guitar stem, low means compressed or distorted) is at or below the song's median. Among those, the highest guitar-power-weighted separator confidence; ties within 0.01 go to the lower crest factor. No song section logic from `match_song` was needed because the ranking above already prefers the loud part.
3. **DI**: `dry.pick_di` on the target stem's register, style and density, as `match_song` does, with the first 12 picks loaded. **Deviation**: the first pick for Cherub Rock was a sparse palm-muted file with sound in only a quarter of the 10 s, a poor stand-in for a riff, so `scripts/t200_run.py` takes the first pick with sound in at least 0.8 of its half-second blocks. DI peak at -12.5 dBFS (the `match_song` convention). All DIs are Guitar-TECHS.
4. **Match**: `match_block.py --category drive --shortlist predictor --final-rank afxrep --workers 1 --pack-lufs`, `TONEMATCH_AFXREP_DEVICE=cpu`, one Native host, no Stomp. New flag `--pack-lufs` (default off) level-matches the listening pack by K-weighted LUFS to the target instead of RMS (`match_song.level_match_lufs`; `gains.json` records which).
5. **Preset**: `cache/matches/<id>/best.hlx` (and `rank2.hlx`, `rank3.hlx` when there are 2 or more models). All validate "ok (0 fail, 4 warn)": the warnings are the usual unbudgeted DSP estimate, snapshot bypass and golden-diff notes.

The final order is by AFx-Rep distance of each model's best confirming render (the [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") ranking), inside the predictor's 95% set. So "AFx-Rep's own top pick" is the first of that set; the predictor's top model (by probability) is named separately. They differ for one song (Territorial Pissings); in that case both are in the pack (rank 1 and rank 2).

## Per song

| id | window (song s) and why | separator conf. (song mean / window) | DI | predictor top models (p) | AFx-Rep order | pick and settings | renders / time |
|---|---|---|---|---|---|---|---|
| teen-spirit | 167.0-177.0; loudest-band, low crest 13.8 dB (song median 14.6), activity 1.0 | 0.835 / 0.90 | Set1_7 @32 s (pick_di rank 2) | HD2_DistZeroAmpBassDI 0.82, KillerZ 0.18 | ZeroAmpBassDI, KillerZ | **HD2_DistZeroAmpBassDI**: Drive 0.875, Treble 0.777, Bass 0.59, Blend 0.40, Presence 0.50 | 12 / 17 s |
| territorial | 9.5-19.5; confidence 0.96, the best of any loud window, crest 13.2 (median 14.5); 5 s of it inside a separator "exposed guitar" window | 0.791 / 0.96 | Drop3_m7b5 @108 s (rank 1) | DM4TubeDrive 0.38, DM4Line6Distortion 0.36, DM4ClassicDistortion 0.15, BronzeMaster 0.07 | DM4ClassicDistortion, DM4TubeDrive, BronzeMaster, DM4Line6Distortion | **HD2_DM4ClassicDistortion**: Drive 0.418, Filter 0.363, Bass 0.48, Treble 0.50, Output 0.36 | 24 / 19 s |
| seek-destroy | 0.5-10.5; confidence 0.99, almost all of it a separator "exposed guitar" window (9.5 s), crest 13.3 (median 14.1) | 0.933 / 0.99 | Set1_dim @136 s (rank 1) | DM4HeavyDistortion 0.99 | (one model) | **HD2_DM4HeavyDistortion**: Drive 0.567, Treble 0.745, Bass 0.73, Mid 0.31, Output 0.73 | 6 / 16 s |
| satisfaction | 0.5-10.5; the opening riff, confidence 0.94, crest 17.2 dB (median 17.3, so the guitar is less compressed than in the other songs) | 0.856 / 0.94 | Set1_min @40 s (rank 1) | DistZeroAmpBassDI 1.00 | (one model) | **HD2_DistZeroAmpBassDI**: Drive 0.791, Treble 0.882, Bass 0.59, Blend 0.40, Presence 0.50 | 6 / 14 s |
| cherub-rock | 189.0-199.0; loudest guitar of any window (-28.6 LUFS), crest 13.1 (median 13.7), confidence 0.94 | 0.859 / 0.94 | Set1_7 @32 s (rank 3; the first two were sparse) | KillerZ 0.69, DistZeroAmpBassDI 0.31 | KillerZ, DistZeroAmpBassDI | **KillerZ**: Dist 0.703, Contour 0.960, Gain 0.50, Mid Amt 0.55, Mid Freq 0.88 | 12 / 18 s |

Separator confidence is the heuristic dominance-times-consistency proxy (uncalibrated); the song mean is the ledger value, the window figure is the guitar-power-weighted mean over the chosen 10 s. Renders include the confirming renders and the final re-score; times are `match_block` wall-clock (the separation took 15 to 41 s per song, 133 s in all).

### Runner-up and files

| id | runner-up (AFx order) | provisional losses (pick / runner-up; unadmitted, lower is better) | listening pack (`cache/matches/<id>/listen/`) |
|---|---|---|---|
| teen-spirit | KillerZ: Dist 0.576, Contour 0.844 | 22.4 / 11.4 | target_stem, match, match_rank2, ab_L-target_R-match, ab_rank2.., di_dry |
| territorial | HD2_DM4TubeDrive (the predictor's top): Drive 0.658, Treble 0.722, Output 0.82; also rank 3 BronzeMaster (Drive 0.402, mixture 0.65) | 10.3 / 10.9 | same, plus match_rank3 |
| seek-destroy | none (single model) | 7.4 / - | same without rank 2 |
| satisfaction | none (single model) | 27.7 / - | same without rank 2 |
| cherub-rock | HD2_DistZeroAmpBassDI: Drive 0.889, Treble 0.790 | 9.6 / 20.6 | same |

All packs are LUFS-matched to the target stem (K-weighted, integrated; common gain keeps the peak at -1 dBFS; `gains.json` says "LUFS (K-weighted)"). Pick gains relative to the target in the pack, dB: teen-spirit -2.0, territorial -4.9, seek-destroy -4.4, satisfaction -0.5, cherub-rock +3.5. `best.hlx` and `rank2.hlx` are in the same directory; `di_search.wav` is the true-level DI (the only file for hardware, none was used).

Paths (all under `/Users/jason/Documents/code/helix_pedal/cache/matches/`): `teen-spirit/`, `territorial/`, `seek-destroy/`, `satisfaction/`, `cherub-rock/`, each with `report.md`, `result.json`, `best.hlx`, `listen/`.

## Caveats (every song)

- **The match is blind.** The DI is a Guitar-TECHS recording, not the player on the record. The knobs are low confidence (the [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") notes: Drive within 0.15 for about 60% of right-model cases on library targets, about 38% for a new player), and **Drive is level-confounded** ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly")): it depends on how loud the matcher's DI is, so a Drive value here is partly a statement about the DI.
- **The target is a separated stem of a mastered mix** (drums, bass and vocals bleed, mp3 source), already through an amp, cab, room and mix bus. One drive block with no cab cannot be the whole chain; the AFx-Rep ranking and the provisional loss say which block is closest, not that it is the rig.
- **The predictor was trained on 54 drive models and has never seen six fuzzes**, which are masked from its output. It is confident (p 1.00) on Satisfaction, a fuzz record, and picks a bass-DI model: treat that as "nearest model it knows", not as an identification. The Seek & Destroy and Satisfaction windows are riff intros with confidence 0.94 to 0.99 mostly because the guitar is exposed there, which says the target is clean to measure, not that the model is right.
- Territorial Pissings is the one real disagreement: the predictor is nearly split three ways (0.38, 0.36, 0.15), the AFx order puts the third-ranked model first, and the provisional loss prefers a fourth. The pack lets the human hear ranks 1 to 3.
- The first Cherub Rock run used a sparse DI and gave KillerZ with Dist 0.867 (different knobs, same model); it was overwritten by the run above. Knob sensitivity to the DI is large.
- The ear decides. No listening scores here.

## Plausibility against documented rigs (background, unverified, did not influence any pick)

| id | publicly documented rig (as reported) | the pick | comment |
|---|---|---|---|
| teen-spirit | Boss DS-1 (Tone 4, Level 10, Distortion 7) and an Electro-Harmonix Small Clone chorus into a Mesa/Boogie Studio preamp, Crown power amp and Marshall 4x12s; a Vox AC30 and Fender Bassman for layers ([Guitar World](https://www.guitarworld.com/amp/features/the-secrets-behind-kurt-cobains-guitar-tone-on-nirvanas-smells-like-teen-spirit), [Andertons](https://www.andertons.co.uk/browse/utility/kurt-cobains-guitar-rig/), [ask.audio](https://ask.audio/articles/nirvana-s-gear-in-the-nevermind-era)) | Zero Amp Bass DI (a preamp-driver style block), KillerZ second | A driven preamp front end is in the right family as a stand-in for pedal plus amp; the DS-1 itself is not among the picks. Not evidence either way. |
| territorial | Not looked up for this song; the band's rig on the same period is described above. No claim made. | a DM4-style distortion (Classic), Tube Drive and Line 6 Distortion nearly tied | Three-way split in the predictor, so no comparison is meaningful. |
| seek-destroy | Early Metallica used 100 W Marshall heads on this song; the Mesa/Boogie Mark IIC+ came with later records ([Marshall Forum thread](https://mail.marshallforum.com/threads/metallica-tone.4282), [Wikipedia: Mesa/Boogie Mark IIC+](https://en.wikipedia.org/wiki/Mesa/Boogie_Mark_IIC%2B)); forum sources, unverified | DM4 Heavy Distortion, p 0.99 | A high-gain distortion block is the expected kind. |
| satisfaction | Gibson Maestro FZ-1 Fuzz-Tone into the amp, Gibson Firebird ([Wikipedia: Maestro FZ-1](https://en.wikipedia.org/wiki/Maestro_FZ-1_Fuzz-Tone), [Guitar.com](https://www.guitar.com/features/opinion-analysis/fuzz-was-the-future-satisfaction-guaranteed/)) | Zero Amp Bass DI, p 1.00 | Not a fuzz. The predictor has no fuzz output for this set (six fuzzes masked, none in training), so the mismatch is expected and is a blind spot, not a finding. |
| cherub-rock | Electro-Harmonix Op-Amp Big Muff Pi into a Marshall JCM800 (low-sensitivity input) with a Strat ([Reverb](https://reverb.com/news/the-gear-of-the-smashing-pumpkins), search summary of Fractal Audio forum and Riff Magazine posts) | KillerZ (a Metal-Zone-type distortion) | A Muff-type fuzz is among the masked fuzzes. A heavy, scooped-mid distortion block is a reasonable neighbour; not a Muff. |

## Code and tests

`scripts/t200_windows.py`, `scripts/t200_run.py`, `--pack-lufs` in `scripts/match_block.py`, `level_match_lufs` and `write_pack(lufs=)` in `scripts/match_song.py`. The 69 existing tests in `test_match_block.py`, `test_predictor_first.py` and `test_match_song.py` pass; the new flag defaults off.


## Human verdicts (2026-10-22, played live on the Stomp after hearing each song section)

| Song | Pick | Verdict |
|---|---|---|
| teen-spirit | Zero Amp Bass DI | pretty off |
| territorial | DM4 Classic Distortion (the DS-1 model) | closest so far |
| seek-destroy | DM4 Heavy Distortion | pretty off |
| satisfaction | Zero Amp Bass DI | somewhat close |
| cherub-rock | KillerZ | somewhat close |

Informal and one listener. The only pick in the documented pedal family (territorial) was heard as closest.
