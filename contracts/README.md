# Contracts

The five interfaces the agents agree on. Each has one owner. Nobody but the lead changes one, and only after a proposal that names who it breaks and a benchmark re-run.

| Contract | File | Owner | Version |
|---|---|---|---|
| Engine interface | `engine-interface.md`, `src/tonematch/engines/base.py` | lead | v0 (frozen 2026-10-14, Gate 1) |
| Preset schema | `preset-schema.md` | preset | v0 |
| Loss kit interface | `loss-kit.md` | tone-metrics | v0 |
| Ledger format | `ledger-format.md` | benchmark | v0 |
| Board and decision log | `board.md` | lead | v0 |
| CLI result manifest (additive, T172) | `cli-result.md`, `src/tonematch/cli_result.py` | platform (lead to ratify) | v0 |
