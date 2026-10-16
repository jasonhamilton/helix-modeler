---
name: reamp-capture
description: Send a dry clip through the HX Stomp over USB and capture the processed result: route the DI to USB 5/6 with the Input block set to USB 5/6, record USB 1/2, measure round-trip latency once with an impulse, trim and level-match. Use for hardware renders, parity tests, and capturing the user's own DI.
---

# reamp-capture

Script: `scripts/reamp_capture.py <dry.wav> --out <wet.wav> [--calibrate]` (owner: `stomp-engine`).

## One-time calibration (`--calibrate`, repeat after a driver or firmware update)

1. Ask the person to confirm the Stomp is connected and HX Edit is quit (it holds the control interface). With the control helper (`TONEMATCH_STOMPD`; built outside this repo, see `docs/notes/stompd.md`) the script reads the firmware itself, sets the Input block to USB 5/6 and bypasses every block in the edit buffer (transparent chain), then puts back exactly what it changed. Without the helper, set that up by hand and pass `--firmware`. On the HX Stomp the Input block's USB 5/6 is **value 6** (measured on 3.50); the Floor menu's 15 is accepted and silent. `fixtures/smoke/known.hlx` has Input on Main L/R (1), not USB 5/6.
2. Play a single-sample impulse to USB 5/6 while recording USB 1/2 at 48 kHz (PortAudio latency `low`); the delay to the first non-zero sample is the round-trip latency. Repeat three times; they must agree within 2 samples (stream start jitters by 0 to 1 sample). Save `cache/stomp/latency.json` with the value, firmware, stream setting and date.
3. Play a -18 dBFS 1 kHz tone through the transparent chain; store the gain that brings it back to -18 dBFS as the fixed level calibration (0.0 dB on 3.50).

## Capture

1. Load `cache/stomp/latency.json`; refuse to run without a calibration newer than the last firmware change.
2. Stream the dry clip to USB 5/6 and record USB 1/2 with 1 s of pre-roll and 2 s of tail (reverb and delay tails matter).
3. Trim by the measured latency, level-match to the reference, keep the tail, write the result.
4. For the user's own DI: record USB 5/6 (dry) and USB 1/2 (processed) at the same time while they play; both are saved. See "Play-along user DI" below.
5. Leave the pedal as found. The script never selects another preset; it undoes its own edit-buffer edits (Input routing, and for `--calibrate` the blocks it bypassed), newest first, and keeps going if one undo fails, reporting the failures. Nothing is saved.
6. End the control session cleanly (T059). The helper sends HX Edit's session-close when it exits; without it the pedal keeps its blocks locked until USB is unplugged. `--usb-reset auto` (the default, also `TONEMATCH_STOMP_USB_RESET`) re-enumerates the pedal's USB only when the helper could not send that close; `always` resets after every session, `off` never. A reset never runs while audio streams. If the script says the pedal did not come back, ask the person to unplug and replug the USB cable (the 9V can stay in).

## Play-along user DI (`--user-di --play`, dry-signal accuracy mode A)

`python scripts/reamp_capture.py --user-di --play <song> [--section A:B] [--target A:B] --countin 4 --takes N [--out <dir>]`

1. Before running, check three things with the person: the guitar is in **Main L/Mono** (USB in 5 records it), the preset's Input block is on Main L/R or Guitar (not USB 5/6), and headphones or monitors are on the Stomp's outputs. HX Edit is quit if the control helper is used. On macOS the terminal needs Microphone permission, or every input reads as silence.
2. The song stays local. `--play` reads the person's own file (any format soundfile decodes, decoded like the separator so the timeline matches the stems). `--section 32.45:46.45` cuts it in song seconds. For a pre-cut file, pass `--song-offset <s>` to say where it starts in the song. Takes go under gitignored `cache/user_di/<song-id>/` (`--song-id` defaults to the file stem). An `--out` inside the repo but outside `cache/` is refused.
3. One full-duplex stream, with the same settings as the calibration (48 kHz, PortAudio `low`). Host USB out 1/2 carries the count-in clicks and then the section (`--song-gain-db`, default -6). The Stomp sends USB 1/2 playback straight to its outputs and phones, unprocessed. USB in 5 is the dry Main L, USB in 6 is Main R (levels only), and USB in 1/2 is the processed Path A (saved in stereo). The count-in follows the song's beat grid from `cache/stems/<hash>/*/sections.json` when the song has been separated (clicks on downbeats are accented); otherwise `--bpm` sets it, and the default is 120.
4. Alignment: each take is trimmed by `latency_samples` from `cache/stomp/latency.json` (+ `--extra-latency`), so take sample 0 is the section start on the song timeline. The JSON stores `alignment.take_sample0_song_s`, `song_offset_samples`, `latency_samples`, `recording_offset_samples` and the calibration date and firmware. The round trip covers USB out to DSP to USB in, and the player's path adds the converters, about a millisecond. `levels.dry_to_wet` reports the measured residual lag (reliable only when `corr` is high).
5. Each take writes `take<k>_dry.wav` (mono float32), `take<k>_wet.wav` (stereo) and `take<k>.json`. Take numbers continue from the existing files, and nothing is ever overwritten. The JSON holds levels over the whole take and the `--target` window (RMS, peak, clipped samples), plus warnings. The checks are: dry digital zero (guitar not plugged in, and the exit code is 3), very quiet dry, dry or wet clipping, a silent wet while the dry has signal (Input block on USB), Main R as loud as Main L (wrong jack), and the song bleeding into the dry or wet (check the Stomp's USB In 1/2 Destination).
6. The pedal is never written. With the control helper present the script only reads: it checks the firmware against the calibration, warns when the Input block is on USB, and confirms at the end that the chain is unchanged (`pedal.unchanged_after_session`). There is nothing to restore. `--dry-run` decodes, cuts and prints the plan without opening the audio device. Enter starts each take, unless `--no-prompt` is given (then there is a `--gap` pause between takes). Ctrl-C stops the outputs and discards the take in progress.
7. Without `--play`, `--user-di` records `--seconds` of free playing after the count-in.

No ledger row: a user-DI take is not a render, and no ledger `kind` fits.

## Ledger

`--kind render-cost --engine stomp --dataset <clip id> --metric seconds_per_render --value <t> --renders 1` (the script's `--ledger`). Calibration: `--kind render-cost --metric latency_samples` (not `smoke`; smoke rows are for `engine-smoke` only).
