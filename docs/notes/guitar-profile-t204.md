# Guitar profile ([T204](../../board/TASKS.md#t204 "Guitar calibration (human 2026-10-22)")): per-guitar calibration

Human, 2026-10-22: "each guitar needs to be sampled/calibrated before starting the process." Built, **awaiting the human's session**. Nothing here is adopted; whether profiled rebuilds help matching is to be measured on Mother and judged by ear.

## The human's session checklist (about 10 minutes)

1. Guitar cable into the HX Stomp's **main input** (Main L). Stomp on USB. The preset's Input block must be the guitar input (Main L/R or Multi), not USB 5/6. Note the guitar's pickup selector and tone/volume knobs; keep them at your normal settings and the same for the whole session (the profile is per guitar and per setting).
2. Level check first (3 s, play normal then hard): `.venv/bin/python scripts/guitar_profile_record.py --guitar-id <name> --check`. Aim for a hard-pick peak between -12 and -3 dBFS; it warns on clipping or silence.
3. Run the session (it asks for Enter before each segment, counts down 3 s, then records and shows the peak; on a warning you can redo):
   `.venv/bin/python scripts/guitar_profile_record.py --guitar-id <name>`
   - a) each open string, 3 picks (6 x 9 s)
   - b) every fret from open to the 12th on each string, one note per pick (6 x 22 s)
   - c) E, A, D, G, C and E5, A5, D5 power chords, strummed twice (8 x 7 s)
   - d) 4 single notes each at soft, medium and hard picking (3 x 9 s)
   - e) palm-muted single notes and a short palm-muted riff (2 x 10 s)
   - f) optional bends, slides, harmonics (add `--with-optional`)
   - g) 30 s of free playing in your normal style
   `--list` prints the script and records nothing; `--segments a,b` or ids (`dyn_soft,free`) run a subset.
4. Build: `.venv/bin/python scripts/guitar_profile_build.py --guitar-id <name>`; then a rebuild: `.venv/bin/python scripts/rebuild_dry.py --profile <name>`.

Files: `data/guitars/<name>/session-<stamp>/*.wav` + `session.json`, then `profile.json`, `bank.json` (gitignored).

## Method

- **Recording.** Silence out, host input 5 (dry Main L) in, through `tonematch.reamp.duplex`, 48 kHz. Audio only; the control channel is never opened.
- **Level.** Loud-frame level ([T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") convention: 50 ms frames within 30 dB of the loudest) of the free-playing and run segments; peak and crest; soft/medium/hard note levels (RMS of the first 300 ms) from segment d and their spread as the dynamic range.
- **Notes.** Onset detection, then pitch: the prompted pitch for open strings, fret order for a 13-note run, otherwise YIN per onset, or Basic Pitch ([T203](../../board/TASKS.md#t203 "Install Spotify Basic Pitch locally") venv `cache/venvs/basic-pitch`, default settings) when it imports. Unsteady-pitch onsets (chords, noise) are skipped in free playing. Labels: pitch, string and fret where known, dynamic (nearest of the soft/medium/hard levels), technique (palm_mute, bend, slide, harmonic, open_string, normal). Stored as `BankNote` rows (player `OWN`) in `bank.json`.
- **Tone.** For plain notes, the 0.35 s after each onset, normalised to unit RMS, mean power spectrum; the same for Guitar-TECHS P1/P2 notes drawn with the same pitch histogram (3 bank notes per own note). dB difference in 11 half-octave bands (80 Hz to 8 kHz), mean over 125 Hz to 5 kHz removed (level is kept separate), capped at +-12 dB, joined linearly in log frequency, realised as a 2048-tap minimum-phase FIR.
- **Coverage.** `profile.json` lists the pitches with own notes and those that fall back to the bank (through the tone EQ).
- **Rebuild.** `rebuild_dry.py --profile <id>`: own note of the pitch (level nearest the velocity's target) else bank note through the EQ; velocity 30..110 maps linearly in dB onto soft..hard (without soft/hard: the [T201](../../board/TASKS.md#t201 "Rebuild a dry DI track for Mother from the Basic Pitch transcription…") rule); the whole is set to the profile's normal loud-frame level (peak-capped at -1 dBFS). Default behaviour without `--profile` is unchanged.

## Stand-in profile from the human's existing dry takes (no new recording)

Profile `human-standin` from all 14 dry takes of Mother and DS-Sample (free playing only: no soft/hard range, no strings, no palm mutes).

- **Level.** Pooled loud-frame level **-28.08 dBFS** against the -26.7 reference: 1.4 dB below it; peak -5.8 dBFS, crest 22.3 dB. Per take it is not one number: Mother -27.5 to -28.9 (one take -33.3), DS-Sample 12:23 and 12:27 sessions -26.6 to -27.6, but the DS-Sample 13:00 session **-37.1 to -37.6**, about 10 dB quieter (a different knob or pickup setting, or input gain; the pooled figure is pulled by it). This is the [T194](../../board/TASKS.md#t194 "Explain the Drive anomaly") input-level confound seen in the human's own takes.
- **Tone correction (bank to this guitar), dB at 80, 125, 200, 315, 500, 800 Hz, 1.25, 2, 3.15, 5, 8 kHz:** -11.4, -10.7, -7.0, -4.9, -0.6, +0.8, +5.6, +8.8, +7.3, +0.7, -9.4. The raw band differences before mean removal and capping are larger (+12 to +16 dB at 1.25 to 3.15 kHz), so the cap does bind at the top of the hump. Read as: this guitar is much more mid-forward (1 to 4 kHz) and lighter below 300 Hz than the Guitar-TECHS notes. The same shape appears separately in Mother (peak +10.5 dB at 2 kHz) and in the DS-Sample 12:2x takes (+9.1 dB at 2 kHz); the 13:00 DS-Sample takes (38 notes) have the hump a little lower (+7 dB at 1.25 kHz) and a darker top. Caveat: free playing is polyphonic, YIN or Basic Pitch pitches for chords are the bass or lead note, so the lowest and highest bands (edge-held) are the least reliable. The planned session (single notes, runs) makes this fit much cleaner.
- **Own notes from the stand-in.** 460 notes (pitches 37 to 90, many from chords) came out of the takes. They are not a real note bank: no string, dynamic or technique labels, chord-contaminated tails, and **Mother's own takes are the source for rebuilding Mother**, so do not read the profiled rebuild as a fair test.
- **Rebuilds of Mother (68 s), local only, for listening:**
  - `data/songs/mother-playalong/rebuilt-dry/rebuilt_dry_profiled.wav` (250 own notes, 47 bank notes through the EQ; loud level -28.08 dBFS, peak -8.5)
  - `rebuilt_dry_profiled_toneonly.wav` (all 297 notes from Guitar-TECHS through the EQ; same level, peak -7.3). This one is the fairer picture of "tone + level only".

## Honest limits

- Basic Pitch used at default settings; [T203](../../board/TASKS.md#t203 "Install Spotify Basic Pitch locally")'s tuned settings can be dropped in later (`basic_pitch_notes`).
- The tone fit trusts the bank's mean spectrum at the same pitches; a Guitar-TECHS guitar differing in pickup position shifts it. It corrects the average, not note-by-note.
- A segment-based bank holds each note in its segment file; rebuilds read those files, so keep the session folder.
