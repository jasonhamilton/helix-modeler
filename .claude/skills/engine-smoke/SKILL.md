---
name: engine-smoke
description: Prove an engine works end to end: detect Helix Native or the HX Stomp, load a known preset, render a 2 s DI clip, check level, silence, NaN and length, and write a smoke row to the ledger. Use before any engine hand-off, after a fresh install, or when asked whether the engine is working.
---

# engine-smoke

Script: `scripts/engine_smoke.py` (owner: `benchmark`; the engine agents may build it first to `contracts/engine-interface.md`).

## Inputs

- `TONEMATCH_ENGINE=native|stomp|auto` (default auto; with both present, run once per engine).
- `fixtures/smoke/di_2s_real.wav` (2 s real clean guitar DI from Guitar-TECHS, CC BY 4.0, 48 kHz mono; default since T099) or, when it is missing or with `--di synthetic`, `fixtures/smoke/di_2s.wav` (synthetic), and `fixtures/smoke/known.hlx` (one amp, one cab, bypassed delay, generated from a golden template).

## Steps

1. `tonematch.engines.registry.detect()`; if nothing is found, stop with the message "No engine found. Install Helix Native or connect an HX Stomp; at least one is required." and do not write a row.
2. For each engine under test: `load(known.hlx)`, `render(di)`, then `cost()` and `capabilities()`.
3. Checks, all must pass: output length equals input length (after the engine's latency trim); no NaN or inf; peak between -30 and -1 dBFS; RMS at least 20 dB above the RMS of the tail 100 ms of digital silence rendered through the same preset; `capabilities().firmware_version` is non-empty and `models` is non-empty.
4. On the Stomp: confirm `close()` restored the pedal's original preset (read it back).
5. Write rows: `--kind smoke --engine <e> --dataset smoke-real-di --metric peak_dbfs --value <v>` (`--dataset smoke` for the synthetic clip) and `--kind render-cost --metric seconds_per_render --value <cost.seconds_per_render> --renders 1 --wall-clock-s <t>`.

## Output

Exit 0 with a one-line summary per engine; exit 1 and name the failing check otherwise. Never claim "works" without the row.
