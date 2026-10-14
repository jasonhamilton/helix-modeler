---
name: reamp-capture
description: Send a dry clip through the HX Stomp over USB and capture the processed result: route the DI to USB 5/6 with the Input block set to USB 5/6, record USB 1/2, measure round-trip latency once with an impulse, trim and level-match. Use for hardware renders, parity tests, and capturing the user's own DI.
---

# reamp-capture

Script: `scripts/reamp_capture.py <dry.wav> --out <wet.wav> [--calibrate]` (owner: `stomp-engine`).

## One-time calibration (`--calibrate`, repeat after a driver or firmware update)

1. Ask the person to confirm the Stomp is connected, on firmware 3.80, and that the active preset's Input block is set to USB 5/6 (or load `fixtures/smoke/known.hlx`, which sets it).
2. Play a single-sample impulse to USB 5/6 while recording USB 1/2 at 48 kHz; the delay to the first non-zero sample is the round-trip latency. Repeat three times; they must agree within 2 samples. Save `cache/stomp/latency.json` with the value and the date.
3. Play a -18 dBFS 1 kHz tone through a known transparent preset; store the output level as the level reference.

## Capture

1. Load `cache/stomp/latency.json`; refuse to run without a calibration newer than the last firmware change.
2. Stream the dry clip to USB 5/6 and record USB 1/2 with 1 s of pre-roll and 2 s of tail (reverb and delay tails matter).
3. Trim by the measured latency, level-match to the reference, keep the tail, write the result.
4. For the user's own DI: record USB 5/6 (dry) and USB 1/2 (processed) at the same time while they play; both are saved.
5. Restore the pedal's original preset if one was changed.

## Ledger

`--kind render-cost --engine stomp --dataset <clip id> --metric seconds_per_render --value <t> --renders 1`.
