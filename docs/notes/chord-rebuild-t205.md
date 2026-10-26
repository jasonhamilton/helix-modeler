# [T205](../../board/TASKS.md#t205 "rebuild_dry --chords"): chord-list dry rebuild (`rebuild_dry.py --chords`)

Human approval 2026-10-22 ("yes lets try both"). Ported from the scratch prototype the human liked ("sounds good", 0-20 s, dry and through the Zero Amp Bass DI match).

**Method.** The user supplies chord names; nothing from tab sites is stored. `src/tonematch/chord_rebuild.py`:
- names to low-position voicings (root in E2..D#3, so the low E string, else the A string; power chord = root + fifth; G5 also its octave; maj = 1-3-5, min = 1-b3-5). The six Mother chords give B5 47+54, G5 43+50+55, A5 45+52, F#5 42+49, D5 50+57, C#5 49+56, as in the prototype. Unknown names raise a clear error.
- Viterbi over Basic Pitch frame posteriors (22050/256 = 86.13 fps, bin 0 = MIDI 21) with one state per chord plus silence. Score = mean posterior over the chord's pitches minus 0.5 x the max over other vocabulary pitches. Switch penalty 0.6, silence 0.05, minimum segment 0.06 s.
- Strikes: one at every segment start, plus onset-posterior peaks >= 0.6 at least 0.45 s after the previous strike. Rendering is `dry_rebuild.render(prefer_position="low")`.
- These are named defaults, deliberately untuned; changing them is a method decision for the human.

**Posteriors.** `python scripts/transcribe_stems.py posteriors --songs mother --seconds 68` writes `cache/basic-pitch/<song>/posteriors.npz` (note, onset, fps, seconds); `rebuild_dry.py --chords` calls it through the Basic Pitch venv when the cache is missing or for another length.

**Output.** `data/songs/<song>-rebuilt/chords/` (or `--variant`): `rebuilt_dry_{ref,hot,veryhot}.wav`, the A/B files, `report.json` (with a `chord_rebuild` block), `chords.json`. Local only.

**Verified.** Mother, 68 s, six chords: 74 segments, 227 note-strikes (pitch-notes), cycles B5 G5 A5 ... The segment timeline equals the prototype's `chords-six/chords.json` exactly (74 of 74, identical), also from freshly recomputed posteriors, and `rebuilt_dry_ref.wav` is bit-identical to the prototype's.

**Gap.** `tests/test_tools_doc.py` fails on `rebuild_dry.py --profile` (documented for [T204](../../board/TASKS.md#t204 "Guitar calibration (human 2026-10-22)"), not present in `scripts/rebuild_dry.py` when this was built). Not touched here.
