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
