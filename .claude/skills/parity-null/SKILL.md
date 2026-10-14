---
name: parity-null
description: Measure the gap between Helix Native and the HX Stomp: reamp the same DI through both with the same preset, level-match, subtract, and record the null residual in dB. Use once per firmware or Native version and after any engine change.
---

# parity-null

Script: `scripts/parity_null.py --preset <file.hlx> --di <clip.wav>` (owner: `benchmark`; needs both engines present).

## Steps

1. Confirm both engines detect; record `capabilities()` of each (firmware, Native version).
2. Render the DI through the preset on Native and through the Stomp (`reamp-capture`), both latency-trimmed.
3. Align by cross-correlation (sub-sample if needed), level-match by RMS over the first 2 s, subtract.
4. Residual: RMS of the difference relative to the RMS of the Native render, in dB, overall and in octave bands; also the spectral tilt of the residual (tells whether the gap is a filter or a level).
5. Repeat on three presets: clean, crunch, high gain with delay and reverb. Report each.

## Ledger

`--kind parity --engine both --dataset <preset name> --metric null_residual_db --value <dB> --notes "fw=<v>;native=<v>"`. The lead records the per-firmware value in `docs/DECISIONS.md`; `search` uses it to decide whether a final hardware refinement is worth the renders.
