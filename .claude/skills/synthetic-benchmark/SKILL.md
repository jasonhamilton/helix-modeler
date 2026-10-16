---
name: synthetic-benchmark
description: Run the synthetic recovery benchmark with known answers: load or regenerate the set (DI clips rendered through random plausible presets, mixed with multitrack stems), run the search at a fixed small budget, compute block-type F1, model accuracy and parameter MAE, write a ledger row, and flag a regression. Use on every merge and in CI.
---

# synthetic-benchmark

Script: `scripts/synthetic_v1.py generate|run [--items N] [--budget R] [--weights FILE] [--shard k/n] [--ledger]` (owner: `benchmark`, T073; `synthetic_benchmark.py --ci` only points to it). synthetic-v1 is 96 one-block presets from the Native block library; see docs/BENCHMARKING.md "Synthetic recovery set". The generation steps below describe the earlier chain design and are superseded by that section.

## Generate (`--generate`, or when `fixtures/synthetic/` is missing)

1. From `fixtures/synthetic/manifest.json`: seed, number of items, DI clips (from `dry-signal`), the distribution of plausible chains (fit on community presets), mix stems and density levels (for the stress set), and the engine used to render (Native, or the Stomp overnight).
2. Render each item's wet guitar, mix it with stems at the manifest's density, and store the true preset as the label. Keep a held-out-notes variant per item.
3. Record the generation as a ledger row (`--kind benchmark --metric items --value N --notes generated`).

## Run

1. Separate each item (`separate`), run `search.match` with the fixed template at `--budget` renders (CI default small, nightly larger), on the engine in `TONEMATCH_ENGINE`.
2. Metrics: `block_f1` (which categories are enabled), `model_acc` (right model per enabled slot), `param_mae` (normalized), `loss_heldout` (the kit's score on held-out notes), `rank1_rate` for the full kit.
3. With `--seeded`, run cold and predictor-seeded and report renders-to-threshold for both (mode 4 evidence).
4. Compare each metric to the previous ledger row with the same `dataset`; print a regression warning for any drop beyond the tolerance in the manifest.

## Ledger

One row per metric: `--kind benchmark --engine <e> --dataset synthetic-v<N> --metric <m> --value <v> --renders <budget> --wall-clock-s <t>`. The stress variant uses `--kind stress --dataset stress-v<N>` with the density in notes.

## CI

`--ci` runs a 20-item subset at a small budget and exits 1 on regression.
