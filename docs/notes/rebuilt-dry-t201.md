# Rebuilt dry track for Mother ([T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother"))

Owner: dry-signal. Date: 2026-10-22. For listening only. Nothing is adopted. Files are local (`data/songs/mother-playalong/rebuilt-dry/`, gitignored); never commit or upload them.

## What was built

A dry guitar track for the first 68 s of Mother, made of real guitar notes (no synth). The notes come from Guitar-TECHS (CC BY 4.0), pasted at the times the Basic Pitch transcription of the separated guitar stem gives.

Code: `src/tonematch/dry_rebuild.py`, `src/tonematch/midi_read.py` (a 90-line MIDI reader, no new dependency), `scripts/rebuild_dry.py`. Tests: `tests/test_dry_rebuild.py` (tiny fake bank).

## Method

1. **Note bank.** Every note of the P1 and P2 single-note takes (279 notes) and scale takes (7,117 notes): 7,396 notes, MIDI 40-86. P3 is refused in code. The chord takes are not in the bank (their notes overlap and cannot be cut out cleanly); the technique takes are not used for plain playing.
   - Each bank entry holds: file, player, pitch, onset, how much clean audio there is before the next note, and the string and fret. The MIDI track number is the string (1 = high e ... 6 = low E; open pitches 64, 59, 55, 50, 45, 40) and fret = pitch minus open pitch, which is consistent for every note (0-24).
   - Checks: pitch (YIN, 100-400 ms after the onset) against the MIDI pitch on 20 random notes: all 20 within 14 cents. Timing: **the Guitar-TECHS MIDI onsets are late against the audio**: audio onset minus MIDI onset has a median of -30 ms (5th to 95th percentile -60 to +40 ms). So the bank re-finds each onset in the audio (steepest 5 ms rise of the dB envelope within +-100 ms of the MIDI onset) and uses that. Raw MIDI times would have put every note about 30 ms off.
2. **Cleaning the transcription.** The file holds 307 notes (the brief said 298; the parser finds 307, with no zero-length or overlapping duplicates of the same note-on/off pair). Rules, applied in order:
   - **Below E2 (MIDI 40): 10 notes, all MIDI 35 (B1), all dropped.** The lowest string of a guitar in standard tuning is E2, so B1 cannot be played. In all 10 cases the B2 (47) above it is sounding at the same time, so these are the sub-octave ghost of a note we already play (bass bleed, or the transcriber's octave error). Transposing them up an octave would just double the B2. The rule in the code: drop if the octave-up pitch class is already sounding, otherwise transpose up. Here that never triggered.
   - Shorter than 40 ms or velocity below 20: none (shortest note 0.14 s, quietest velocity 45).
   - Same pitch struck again while the earlier note still rings (one string cannot do both): the earlier note is cut at the new onset. 9 notes.
   - Polyphony cap 6: never reached (max 5).
   - Result: 297 notes. Original timing kept.
3. **Rendering.** For each note: a bank note of the same pitch; one player for the whole take (P1 covers every needed pitch, so all 297 notes are P1); a source long enough for the transcribed length (all 297 had one); round-robin over the candidates of that pitch so the same sample does not repeat back to back. The segment starts 3 ms before the audio onset (2 ms fade-in, no click), is cut to the transcribed duration, and gets a 30 ms cosine release. Its gain is the bank note's level normalised to a common level, times MIDI velocity / 127. Overlaps are summed. 48 kHz float32 mono, 68 s.
   - No pitch shifting, so no missing pitches (all 297 found).
   - Not modelled: the same note on different strings, string noise, real muting, bends, slides. Every note is a plain single note, including the ones that were chord tones.

## Levels (loud frame = the [T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") definition: RMS of 50 ms frames within 30 dB of the loudest)

| File | Loud-frame level | Peak | Note |
|---|---|---|---|
| `rebuilt_dry_ref.wav` | -26.7 dBFS | -8.6 dBFS | reference level of the older renders |
| `rebuilt_dry_hot.wav` | -20.7 dBFS (+6) | -2.6 dBFS | |
| `rebuilt_dry_veryhot.wav` | **-19.1 dBFS (+7.6, not +12)** | -1.0 dBFS | **capped** |

The track has a high crest factor (peaks 18 dB above the loud level, from overlapping notes), so +12 dB would peak at +3.4 dBFS. The very-hot file is turned down until its peak is -1 dBFS, which leaves it only +7.6 dB over the reference. A truly hot pickup would clip the interface input or the pedal's input stage first. If the human wants a real +12 dB drive into the amp block, add a limiter or lower the crest factor first; that is a decision, not done here. The three files are NOT loudness-matched on purpose; that is the point of the three.

## Listening files

All in `data/songs/mother-playalong/rebuilt-dry/`.
- `ab_L-stem_R-rebuilt.wav`: left the separated guitar stem (cache/stems/.../guitar.wav, stereo mixed to mono), right the rebuilt dry, 0-68 s. Both set to -23 LUFS (stem +9.8 dB, rebuilt +4.9 dB).
- `ab_L-humantake_R-rebuilt.wav`: left the human's take 20261018-113158_take1_dry.wav placed with its `_align.json` (take sample 0 at song time 0.01 s), right the rebuilt dry, 0.01-68 s. Both -23 LUFS (take +5.7 dB, rebuilt +4.9 dB).
- The stem is a separated mix with other instruments leaking into it; the left channel of the first file is not a clean guitar.

## Alignment check (onset-envelope method of `align_takes.py`, against the separated stem)

| Signal | Score | Median correlation | Lag IQR |
|---|---|---|---|
| human take1 (113158) | 0.344 | 0.374 | 30 ms |
| **rebuilt dry** | **0.288** | **0.326** | **37.5 ms** |
| rebuilt dry shifted by 3.7 s (control) | 0.035 | 0.220 | 185 ms |

- The rebuilt dry does NOT beat the human take on this measure, although its notes sit where the transcription says. The measure compares onset envelopes of different audio, so it is limited by how the stem's attacks (separator smear, bleed, notes the transcriber missed, 307 notes in 68 s) differ from clean single-note attacks. A perfect copy of the notes would not score 1. Read it as "about as well aligned as a good human take, slightly looser", not as a quality score.
- The median correlation alone barely separates the control (0.22) from the real one (0.33); the lag spread does. The score (median minus spread) separates them (0.035 against 0.288).
- Median global lag 0 to +10 ms: no systematic offset.

## Playing technique: a feasibility read (no training)

**What Guitar-TECHS labels.** Five technique folders per player. MIDI notes per file (the file name is the label; the MIDI carries no per-note technique flag):

| Technique | P1 notes | P2 notes |
|---|---|---|
| Bendings | 205 | 113 |
| Harmonics | 36 | 26 |
| PalmMute | 148 | 134 |
| PinchHarmonics | 99 | 119 |
| Vibrato | 141 | 118 |

Each is a few minutes of audio (P1 258/96/552/288/528 s, P2 174/121/536/528/440 s). Some MIDI entries are junk (very short or velocity 10 or less: for example 95 of P2's 113 bend entries have velocity 20 or less, 50 of P1's 99 pinch-harmonic entries are 50 ms or shorter), so counts of usable notes are smaller. The plain-note takes are singlenotes and scales. There is no label for slides, hammer-ons, or chords played muted.

**Single notes against chords, from the transcription alone.** Yes, per attack. Grouping onsets within 50 ms into one event: 221 events in the raw transcription, of which 152 single notes, 53 dyads, 16 chords of 3 or more notes. After cleaning: 212 events: 144 single, 52 dyads, 16 chords of 3+. By time, 2 or more notes are ringing for most of the track (about 45 s of 68 s), because notes overlap while ringing. So "single-note moment" is decidable per attack but not per instant. Whether a dyad is a power chord or two separate notes is not decidable from MIDI alone. The transcription is Basic Pitch's guess from a separated stem, so the counts carry its errors.

**Palm muting from the separated stem with simple features.** Estimate: plausible for the sustained-versus-muted split, with a medium chance (I would say 60 to 70 percent) of getting a usable per-note flag, lower on heavy distortion where the sustain compresses everything. Evidence from a quick check on clean DI (not a build): decay time (time for the envelope to fall 10 dB after the peak) separates palm-muted notes from plain notes with an area under the curve of 0.86 (P1) and 0.97 (P2); the spectral centroid of the first 200 ms separates them with 0.82 for P1 but 0.46 (chance) for P2, so low-pass character is not reliable. The comparison is unfair in one way: the plain notes are long open sustains (3.6 s) while real riff notes are cut short (median 0.28 s in this transcription), so a short unmuted note will look muted. On the separated stem, the separator's smear and the amp's distortion will blur it further.

**Cheapest test (not run).** (a) Take the PalmMute and singlenote/scale DI notes, cut the plain ones to the same length distribution as palm-muted ones (0.1-0.4 s), compute decay and centroid and onset sharpness, and see whether the AUC holds; one hour on the CPU. (b) Render those same notes through one fixed high-gain chain on the Stomp or Native to see how much the distortion destroys the decay cue. (c) Apply the detector to the stem notes the transcription gives and let the human listen to a "muted" and an "open" group of 10 notes each. The ear decides; do not score it ourselves.

## Limits

- Every rebuilt note is a clean single-note pick on a clean guitar; the real part has chord voicings, muting, and the player's feel. This is a realistic dry level and timing reference, not the real performance.
- Transcription errors carry through: missing notes, wrong octaves, 307 notes in 68 s, no technique.
- Levels: the very-hot version is capped (see above).
- The two A/B files compare against a different guitar and a separated, smeared stem. Whether the Stomp's tone from the rebuilt dry resembles the song is for the ear.
- Guitar-TECHS credit: CC BY 4.0, keep the attribution if anything derived is ever shared (the rebuilt files are local only).
