# Dry signal: reference DI library (option B), T021

Code: `src/tonematch/dry.py`. Tests: `tests/test_dry.py` (synthetic clips only). Audio 48 kHz float32 mono; other rates are resampled on `load()`, stereo is averaged.

## Manifest format (`<library>/manifest.json`)

JSON object `{"version": 1, "clips": [...]}`. One entry per excerpt (default 4 s segments, no overlap; segments under 2 s or quieter than -55 dBFS RMS are dropped). Entry fields:

- `path`: file path relative to the library root; `start_s`, `dur_s`: excerpt window in seconds.
- `pitch_lo`, `pitch_med`, `pitch_hi`: MIDI note of the 10th, 50th, 90th percentile of voiced-frame pitch (autocorrelation, 2048-sample frames; polyphony reads as the strongest period, so treat it as a register estimate, not a transcription).
- `density`: onsets per second (positive 6 dB envelope jumps).
- `style`: `muted` (energy falls more than 18 dB within 0.2 s of onsets), else `chords` (median autocorrelation clarity below 0.6), else `sustained` (under 1 onset/s and slow decay), else `single`.
- `clarity`, `rms_dbfs`, `source`, `licence` (free text, set at index time), `synthetic` (true only for the fallback clip).

The manifest is reused unless `index_library(..., rebuild=True)`. Heuristics are crude by design; the picker only needs a rough match, and the `tone-metrics` content-invariant terms absorb the rest.

## Picker

`pick_di(register, style, density=None, n=3, max_dur_s=None, library=None)`: `register` is (low, high) MIDI or one note; score = centre and span distance in semitones, +12 for a style mismatch, +2 per onset/s of density error. Returns distinct source files first, so the n picks differ in guitar and pickup (a DI mismatch biases EQ and gain; several DIs average it). With no library (or an empty one) it returns the synthetic `fixtures/smoke/di_2s.wav` with a `UserWarning` and `synthetic=True`. The real clip is still needed for tone judgements (fixtures/smoke/PROVENANCE.md).

## Sources and licences (checked 2026-10-14)

| Source | DI? | Licence | Where | Note |
|---|---|---|---|---|
| Guitar-TECHS (NIME 2025) | Yes, 48 kHz float32 WAV, synced with amp mic and MIDI | CC BY 4.0 | https://guitar-techs.github.io/ , paper https://arxiv.org/abs/2501.03720 | About 5 h 12 min; byte size not stated in the paper. Three players, varied techniques/chords/scales/excerpts. Best fit. |
| EGDB original (Chen et al., ISMIR 2022) | Yes (DI plus 4 amp renders) | Not stated on the project page | https://ss12f32v.github.io/Guitar-Transcription/ (Google Drive link) | About 118 min x 6 timbres; 240 tablatures. Licence unclear: ask the authors before training or redistributing. Fine for private local picking. |
| EGDB rendered with BIAS FX2 (EGDB-PG) | Rendered amp audio; DI not confirmed | CC BY 4.0 | https://zenodo.org/records/12658984 (DOI 10.5281/zenodo.12658984), file EGDB_BIAS_FX2_random.zip, 1.0 GB | Licence clean, but likely wet. Useful for learning (Step 4), not as DI. Paper https://arxiv.org/abs/2504.07406 |
| Own recordings | Yes | Own | | Always licence-clean; a minute of clean DI at each register is enough. |

CC BY needs attribution if clips are shared; local use only needs the note. Nothing here is bundled in the repo.

## Licence upkeep for a downloaded set

A dataset placed in `data/di/` must keep its CC BY 4.0 licence text and attribution (authors, title, URL) alongside the audio, for example as `data/di/LICENCE-Guitar-TECHS.txt`, and get a `licence-check` run before any clip leaves the machine or is used for shared training. `data/di/**` is gitignored except `data/di/README.md`; every extension `dry.py` indexes (.wav, .flac, .aiff, .aif) is ignored repo-wide.

## Guitar-TECHS downloaded and indexed (2026-10-14, human approved)

- Source: Zenodo https://zenodo.org/records/14963133 (9 zips, 4.1 GB total, md5 from the Zenodo API all verified; zips deleted after extraction). Licence field `cc-by-4.0`; project page wording: "All data is licensed under a Creative Commons Attribution 4.0 License (CC BY 4.0)." Matches the assumption. Text and attribution line: `data/di/LICENCE-Guitar-TECHS.txt`.
- Only `*/audio/directinput/*` and `*/midi/*` extracted: 104 DI WAVs, 3.8 GB, 5.02 h, under `data/di/Guitar-TECHS/`. DI is NOT 48 kHz float32 everywhere: 24-bit PCM; 58 stereo (dual mono, L-R = 0), 44 mono, and 2 files at 44.1 kHz (P1_techniques Bendings, Vibrato; `dry.load` resamples). All read without error. Zenodo notes up to 100 ms misalignment between DI and mic/MIDI.
- Index (`manifest.json`, 4 s excerpts, 46 s to build): 4444 clips; players P1 2104, P2 2220, P3 120; styles sustained 1970, chords 1581, single 843, muted 50; median pitch (octave bins from C) 36-47: 744, 48-59: 1822, 60-71: 1470, 72-83: 376, 84+: 29, 0: 3.
- Caveats: the style heuristic is crude (a palm-mute file shows as `sustained`, only 50 clips are `muted`; onset density reads 0 on many chord clips). The pick for muted riffs is therefore weak; prefer P2_techniques PalmMute by name until the classifier is improved. Three players only, so "several DIs per tone" is limited to guitar/pickup variety across P1/P2/P3.
- Real fixture: `fixtures/smoke/di_2s_real.wav` (see PROVENANCE.md).

## Option A: the user's own DI, played along (accuracy mode, T042, stomp-engine)

PLAN option A ("User records own DI"; open question PLAN line 401 still stands: opt-in, needs the player's effort). Capture is `scripts/reamp_capture.py --user-di --play ...` (skill `reamp-capture`, section "Play-along user DI"); code in `src/tonematch/user_di.py`; fake-device tests in `tests/test_user_di.py`. Built and tested offline on 2026-10-15. **It has not been run on hardware yet** (the guitar was not plugged in).

- What the player hears: a count-in on the song's own beat grid (from the separator's `sections.json`; DS-Sample is 67.1 bpm, 0.894 s per beat), then the song section on the Stomp's outputs and phones (host USB out 1/2, unprocessed), mixed with their own processed guitar.
- What is recorded, in one duplex stream: dry Main L (USB in 5, mono, saved as `take<k>_dry.wav`) and processed Path A (USB in 1/2, stereo, `take<k>_wet.wav`). Both are trimmed by the calibrated round trip (1206 samples on fw 3.50, PortAudio `low`), so take sample 0 is the section start on the song timeline. That is the same timeline as the stems, because the song is decoded with `separation.audio.load_audio`. The residual dry-to-wet lag is measured per take.
- Expected accuracy of the alignment: within about a millisecond of the true song time (the converters are outside the calibrated loop), plus whatever timing the player brings. The player's timing is not corrected. For a loss computed on aligned windows, use the target window (`levels.target_window_samples`) and tolerate a few ms of lag.
- The pairing for search: the take's dry over the target window is the DI for that section. The take's wet is the person's current preset on the same performance, which is useful as a sanity reference and for A/B listening. It is not the target.
- Data handling: the song and anything derived from it stay on the machine, under gitignored `cache/user_di/<song-id>/`. Every take's JSON carries the source path, a sha256 prefix of the file, the section, the target, the alignment, the levels and the warnings.
- Checks per take: the dry is digital zero (the guitar is not plugged in; exit 3), very quiet, or clipped; the wet clips, or is silent while the dry has signal; Main R is as loud as Main L; the song bleeds into either recording.

First command for DS-Sample, target 36.45-46.45 s with a 4 s lead-in (section 32.45-46.45 s, 2 s tail), 3 takes:

```sh
TONEMATCH_STOMPD=cache/stomp/bin/stompd .venv/bin/python scripts/reamp_capture.py --user-di \
  --play data/songs/DS-Sample.mp3 --section 32.45:46.45 --target 36.45:46.45 --countin 4 --takes 3
```
