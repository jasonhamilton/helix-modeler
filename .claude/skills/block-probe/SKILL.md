---
name: block-probe
description: Characterise one Helix block by controlled input vs output: render the fixed probe set (sine sweeps at 4 levels, stepped sines, two-tone, level steps, bursts, gate staircase, impulse, real DI) through that block alone, and analyse it into a fingerprint (frequency response, harmonics H2-H9 and THD vs level, clipping curve and symmetry, IMD, attack/release/sag/gate, delay, reverb, modulation). Compare two fingerprints feature by feature (Stomp vs Native, setting A vs B). Use for per-block parity audits, search starting points and block models.
---

# block-probe

Script: `scripts/block_probe.py` (offline part, owner `tone-metrics`; rendering, owner `native-engine`,
T047). Format and methods: `docs/notes/block-probe.md`.

## Steps

1. **Probe set.** `python scripts/block_probe.py make` writes `cache/probe/probe-v1.wav` (float32,
   48 kHz, 47.25 s) and `probe-v1.json` (the layout and its sha256). Without the local DI library it
   builds `probe-v1-nodi` (38.25 s) and says so; fingerprints from the two variants are comparable
   except for the `di` section. Never copy the DI audio into the repo.
2. **Render** (T047, `native-engine`, not built yet). Load a chain holding ONLY the block under test
   at the chosen settings (input and output blocks neutral, no other processing), render
   `probe-v1.wav` from the start with the usual pre-roll, and save the wet file under
   `cache/probe/renders/<engine>/<model>/<settings-hash>.wav`, full length, float, 48 kHz. Pass the
   engine's known latency if it has one. On the Stomp, batch every block in one session and leave
   the pedal on its original preset.
3. **Analyse.** `python scripts/block_probe.py analyse <wet.wav> --layout cache/probe/probe-v1.json
   --subject engine=native model=<id> settings=<hash> [--latency N] [--channel mid|left|right]`.
   Writes `<wet>.fingerprint.json` (schema `tonematch.block-fingerprint` v1). Read the `notes`: a weak
   sync marker means the latency may be wrong; pass `--latency`.
4. **Compare.** `python scripts/block_probe.py compare A.json B.json [--all] [--json]` lists the
   features beyond tolerance, worst first, with where (frequency, level, harmonic, band). Exit 1 if
   any differ. For parity, A = Native, B = Stomp, same model and settings.
5. **Check the analysis itself** after any change to `src/tonematch/probe/`:
   `python scripts/block_probe.py validate [--ledger]` runs the probe through known numpy/scipy blocks
   (EQ, tanh and asymmetric hard clip, compressors, gate, delays, Schroeder reverbs, tremolo,
   vibrato, an amp-like chain, two with a -90 dBFS noise floor) and prints measured vs true with
   tolerances. Every check must pass; `pytest tests/test_probe.py` runs the same.

## Read the fingerprint with care

- These are characterisations of one block. A whole-chain match is still judged on the chain and,
  finally, by ear.
- `transfer_curve` is meaningful only when its `r2` is near 1 (memoryless blocks).
- Time constants above about 250 ms (sag, optical release) are only partly resolved; LFOs below
  0.6 Hz are not resolved; reverb RT60 on hardware is usually T20 (noise-limited).
- Compare tolerances are provisional until T049 sets them from repeat-render and parity spreads.

## Ledger

`validate --ledger` appends `--kind smoke --engine - --dataset probe-synth-v1 --metric
probe_checks_pass_rate` (NOT Helix). Rendering runs (T047) log their own rows; per-block parity
(T049) logs `--kind parity --engine both --dataset probe-v1 --metric probe_features_differ` with the
model in the notes.
