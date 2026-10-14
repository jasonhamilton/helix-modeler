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
4. For the user's own DI: record USB 5/6 (dry) and USB 1/2 (processed) at the same time while they play; both are saved.
5. Leave the pedal as found. The script never selects another preset; it undoes its own edit-buffer edits (Input routing, and for `--calibrate` the blocks it bypassed), newest first, and keeps going if one undo fails, reporting the failures. Nothing is saved.

## Ledger

`--kind render-cost --engine stomp --dataset <clip id> --metric seconds_per_render --value <t> --renders 1` (the script's `--ledger`). Calibration: `--kind render-cost --metric latency_samples` (not `smoke`; smoke rows are for `engine-smoke` only).
