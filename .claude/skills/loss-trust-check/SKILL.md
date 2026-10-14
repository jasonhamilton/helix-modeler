---
name: loss-trust-check
description: Admit or refuse a loss term: run it over the synthetic set with known answers and measure how often the true preset ranks first against distractors; a term joins the default weights only when it passes the threshold benchmark set. Use on every new or changed loss term, and when the ear disagrees with the metrics.
---

# loss-trust-check

Script: `scripts/loss_trust_check.py --term <name> [--all]` (owner: `tone-metrics`).

## Steps

1. Load `fixtures/synthetic/manifest.json` and the rendered set (regenerate with `synthetic-benchmark --generate` if missing).
2. For each item: the target is the wet render of the true preset mixed into its stems and re-separated (what the real pipeline sees); the candidates are the true preset rendered on a *different* DI clip plus N distractor presets (N from the manifest, default 15) rendered on that same clip.
3. Score every candidate with the term alone; record whether the true preset ranks first, and its rank.
4. `rank1_rate` over the set, plus the rate on the held-out-notes subset (same tone, different notes), which tests content invariance.
5. Threshold: the value `benchmark` set in `docs/DECISIONS.md` (until set, report only). Pass means the term may carry a non-zero default weight in `contracts/loss-kit.md`.

## Ledger

`--kind loss-trust --dataset synthetic-v<N> --metric rank1_rate --value <r> --notes "term=<name>"`, plus a row for the held-out-notes subset.

## When the ear disagreed

Add the failing song section to the synthetic set as a labelled case (the human's preferred candidate is the "true" one for that item), re-run for every admitted term, and report which term ranked the wrong candidate first.
