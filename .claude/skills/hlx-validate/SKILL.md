---
name: hlx-validate
description: Validate a generated .hlx preset: parse it, diff it against the golden templates, check block count, DSP budget, snapshot model sharing, amp/cab pairing and device_version, and record the human's HX Edit import result (error -8309 means wrong device). Use on every generated preset before it is handed to anyone.
---

# hlx-validate

Script: `scripts/hlx_validate.py <preset.hlx> [--golden fixtures/golden/]` (owner: `preset`).

## Checks

1. Parses as JSON; `data.device == 2162694`; `data.device_version` matches the firmware in `cache/catalog/current.json` (or the value the person confirmed).
2. Every `tone.dsp0.blockN` has `@model` in the catalog, `@enabled`, `@position`, `@path`, `@stereo`, and parameters that exist for that model with values inside their ranges.
3. At most 8 blocks; DSP cost total within budget using `cache/catalog/dsp-costs.json` (while that table is incomplete, refuse known-heavy combinations and warn on unknown ones).
4. All snapshots in `tone.controller` reference the same models in the same slots; they differ only in bypass and values.
5. Amp and cab pairing: an amp+cab block, or an amp followed by a cab; in full-rig mode no cab without an amp.
6. Structural diff against each golden template: every top-level key and every per-block key present in the template is present in the generated file; report keys the generated file adds.
7. Export mode: if `meta` says effects-only, there is no amp or cab block and the stand-in is recorded.

## The human step

HX Edit's import is the free validator. Ask the person to import the file and load it on the pedal; error -8309 means wrong target device. Record their answer as a ledger row: `--kind smoke --engine stomp --dataset <preset name> --metric hxedit_import --value 1|0`.

## Output

A report with each check as pass, warn or fail, printed and saved next to the preset as `<name>.validate.md`. Exit 1 on any fail.
