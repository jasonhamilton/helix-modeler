# Preset schema, v0

Owner: `preset`. Consumed by the engines, `search`, `benchmark`.

## The file

An `.hlx` preset is plain JSON. The parts this project reads and writes:

- `data.device`: `2162694` for the HX Stomp. `data.device_version`: must match the user's firmware (assumed 3.80).
- `data.meta`: name, author, and this project's provenance block (engine, budget, ledger row id) under a namespaced key.
- `data.tone.dsp0.blockN`: each block's `@model` (e.g. `HD2_AmpUSDoubleVib`), `@enabled`, `@position`, `@path`, `@stereo`, and its parameter values keyed by the catalog's parameter names.
- `data.tone.controller`: snapshot and controller assignments; snapshot names and footswitch assignments sit alongside.

Golden templates: one exported from Native in HX Stomp compatibility mode, one from HX Edit with the pedal attached, kept in `fixtures/golden/`. Every generated file is diffed against them by `hlx-validate`.

## The in-memory form

`tonematch.preset.Preset`: `slots` (ordered list of 8 `Slot(category, model_id | None, params: dict[str, float], enabled: bool)`), `snapshots` (3 × per-slot `enabled` and `params` overrides, plus a name), `export_mode` (`full_rig` | `effects_only`), `device_version`.

v0 template, in order: `comp`, `drive`, `amp`, `cab`, `eq`, `mod`, `delay`, `reverb`. An unused slot has `model_id=None` and `enabled=False`.

## The catalog

Read at runtime from the user's HX Edit install by `catalog-import`, cached under `cache/catalog/<version>.json`, never committed. Gives, per model: id, category, parameter names, ranges and defaults, and a DSP cost entry once the spikes measure it.

## Constraints every writer enforces

1. At most 8 blocks on one DSP, and the per-model DSP cost total within the budget (the DSP cost table is built during the spikes; until then, refuse known-heavy combinations listed in `cache/catalog/dsp-costs.json`).
2. All 3 snapshots share the same models in the same slots. Snapshots change only `enabled` and parameter values.
3. Amp and cab pairing: an `amp+cab` block, or an `amp` block followed by a `cab` block; never a cab without an amp in full-rig mode.
4. `effects_only` export drops the amp and cab slots and keeps every effect block; the dropped stand-in is recorded in `meta` so the result can be reproduced.
