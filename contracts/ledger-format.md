# Ledger format, v0

Owner: `benchmark`. Written by everyone, read by the lead and the reviewer.

`ledger/runs.csv`, append-only, one row per run. Append with `python scripts/ledger_append.py --kind ... --engine ... --dataset ... --metric ... --value ... [--renders N] [--wall-clock-s S] [--notes "..."]`. The script fills `timestamp`, `commit` and `agent` (from `TONEMATCH_AGENT`, default `human`).

| Column | Meaning |
|---|---|
| `timestamp` | ISO 8601, UTC |
| `commit` | short git sha of the working tree (`dirty` suffix if uncommitted) |
| `agent` | who ran it: an agent name from `.claude/agents/`, or `human` |
| `kind` | `smoke`, `render-cost`, `separate`, `loss-trust`, `benchmark`, `stress`, `parity`, `engine-compare`, `ab`, `gate`, `search` (search-stage studies such as clip policy and budgets) |
| `engine` | `native`, `stomp`, `both`, or `-` |
| `dataset` | `smoke`, `synthetic-v<N>`, `stress-v<N>`, `songs-20`, or a song id |
| `metric` | the metric name (`block_f1`, `model_acc`, `param_mae`, `rank1_rate`, `null_residual_db`, `ab_pref`, `peak_dbfs`, `seconds_per_render`, ...) |
| `value` | a number |
| `renders` | renders used, or empty |
| `wall_clock_s` | seconds, or empty |
| `notes` | free text, no commas unless quoted |

Rules: never edit or delete a row; a correction is a new row with a note. A gate row (`kind=gate`) is written only by `benchmark`, and only after the human's sign-off where the gate requires one.
