# [T197](../../board/TASKS.md#t197 "DS-Sample reference chain on the Stomp"): DS-Sample reference chain on the HX Stomp

The human asked for this as a reference listening test. It comes from a tone tutorial's settings (DECISIONS 2026-10-20). No method is adopted.

## The mapping used

Each value below was read back from the pedal after the load and matches.

| # | Block | Values used | From the source |
|---|---|---|---|
| 1 | Scream 808 (`HD2_DistScream808`) | Gain 0.50, Tone 0.50, Level 0.70 | The model's drive knob is named Gain. "2 o'clock" on a 7-to-5 o'clock sweep is 7/10 of the travel, so 0.70. The lead's "about 0.67" is the catalog default. |
| 2 | Brit 2204 (`HD2_AmpBrit2204`) | Drive 0.30, Bass 0.70, Mid 0.50, Treble 0.40, ChVol 0.60, Master 0.70; Presence 0.71 (default); Sag 0.5, Hum 0.5, Ripple 0.5, Bias 0.7, BiasX 0.5 (defaults) | The 0–10 values divided by 10. Hum and Ripple are at the catalog default 0.5. The Stomp parameter policy freezes them at 0.0 for search; that is reported only, not applied, because this chain is not a search. |
| 3 | 4x12 Greenback 25, legacy cab (`HD2_Cab4x12Greenback25`), paired with the amp as Amp+Cab | Catalog defaults: mic 0, Distance 1.0, Low Cut 80 Hz, High Cut 8 kHz, ER 0, Level 0 dB | The legacy cab paired without trouble, so the CabMicIr variant was not needed. |
| 4 | Simple Delay (`HD2_DelaySimpleDelay`), mono | Time 0.026 s (26 ms), Tempo Sync off, Feedback 0, Mix 0.50, Level 0 dB | Mix is 50%. The source's "mix 100% / level 75%" is on a plugin whose mix means something else. Scale (0.75) is stereo-only, so the mono block has none. |
| 5 | 10 Band Graphic (`HD2_EQGraphic10Band`) | 62.5 Hz +1.25 dB, 125 Hz +3.75, 250 Hz +2.25, 8 kHz -1.55, 16 kHz -3.0; others 0; Level 0 | The catalog range is ±15 dB in dB, so the values are written as given. The source's "63 Hz" is the 62.5 Hz band. |
| 6 | Hall (`HD2_ReverbHall`), stereo | Decay 0.52, Mix 0.40, Predelay 22 ms, Low Cut 166 Hz, High Cut 2.8 kHz (defaults) | **It fit.** Decay is a 0..1 knob with no seconds scale in the catalog. There is one measurement on record, from the [T060](../../board/TASKS.md#t060 "Parity spot checks: at least 5 models per block category on Stomp") parity spot on this pedal (fw 3.80): Decay 0.68 gives an RT60 of about 2.3 s (T30, 250 Hz–2 kHz). The 0.52 setting assumes decay scales proportionally: 1.75 / 2.3 × 0.68. That is an approximation, so adjust it by ear. |

## DSP and layout

- Six blocks, with the cab inside the Amp+Cab block, so five Stomp cells of 8.
- Estimated DSP load is 52.3 (catalog load figures: 5.5 + 26.6 + 7.2 + 3.3 + 1.8 + 7.9). The engine's two-path working budget is 66. The pedal accepted the whole write and read it back, so the chain fits.
- `hlx_validate`: 0 fails. Its only warning is that the DSP budget is not enforced.

## Load (2026-10-20 19:51 UTC)

- The chain was written as one whole edit-buffer write (op 21) on 04C "Smoke Known". Nothing was written to flash.
- The load took 18.1 s. Most of that was four first-use device template captures: Brit 2204 with Greenback 25, Simple Delay, 10 Band Graphic and Hall.
- The full readback was ok. The Input reads 1 (Main L/R), so the human can play through it live.
- **Warning sign:** at session close the pedal did not acknowledge the release on the control channel (`acks.control: false`; data and events acked). This is the [T059](../../board/TASKS.md#t059 "Bug: pedal blocks stay locked after any control session") symptom. If the pedal's own knobs cannot edit the blocks, unplug and replug the USB cable (not the 9V).

## Reamp capture

Pending. The lead asked for it to run only after the human has finished playing. To run it:

```
TONEMATCH_AGENT=stomp-engine .venv/bin/python scripts/ds_sample_reference_chain.py capture
```

- **DI and excerpt:** the DI is take2. `take_sample0_song_s` is 30.03, so song 35–45 s is take samples 238560–718560. 2 s of silence is appended so the tail is recorded.
- **Routing:** the Input is set to USB 5/6 (value 6) and a single render is taken with the calibrated latency (1207 samples, `cache/stomp/latency.json`, fw 3.80). The Input then goes back to Main L/R and the chain stays loaded.
- **Pack:** the listening files go to `cache/matches/DS-Sample-live/reference-chain/`. They are `target_stem.wav` (stem 5–15 s, mono mid), `reference.wav`, `ab_L-target_R-reference.wav` and `di_dry.wav` (true level). Everything is RMS-matched to the target, with the gains in `gains.json`.

## Restoring 04C when the human is done

```
TONEMATCH_AGENT=stomp-engine .venv/bin/python scripts/ds_sample_reference_chain.py restore
```

This uses `StompEngine.close(verify_bytes=True)`, the same path the engine itself uses, against the session recorded at load (`session.json`: original preset, its chain, the open-time backup). It takes these steps:

1. Re-select 04C and compare the read-back chain with the one recorded at open.
2. If they differ, step to 04D and back.
3. Only if they still differ, write the open-time backup into the edit buffer.
4. Back the buffer up again and decode-compare it with the open-time backup.

It prints `restore_ok`, exits 0 on success and renames `session.json`. Offline tests in `tests/test_ds_sample_reference_chain.py` cover four cases: plain reselect, step away and back, backup write, and failure reported.

## Files

- Tool: `scripts/ds_sample_reference_chain.py` (`build`, `load`, `capture`, `restore`)
- Preset: `cache/matches/DS-Sample-live/reference-chain/reference_chain.hlx` (Input on Main L/R), `reference_chain.validate.md`, `build.json`
- Load run: `cache/matches/DS-Sample-live/reference-chain/load_summary.json`, `cache/stomp/hw/20261020T195116Z-t197-load/` (backup `20261020T195119Z_editbuffer.bin`)
- Ledger: `smoke / stomp / ds-sample-reference-chain` `load_readback_ok` = 1
