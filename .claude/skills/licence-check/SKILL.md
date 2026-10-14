---
name: licence-check
description: Check licence hygiene: scan Python dependencies for licences, check the pedalboard GPLv3 boundary, grep the repo for Line 6 assets (catalog JSON, firmware, HX Edit resources, Native binaries, .hxf/.hxb bundles) and for committed golden presets, and report. Use on every dependency change and before any release.
---

# licence-check

Script: `scripts/licence_check.py` (owner: `reviewer`).

## Steps

1. `pip-licenses --format=markdown` over the current environment; list every dependency with its licence; flag GPL, AGPL, LGPL, unknown and "UNKNOWN".
2. pedalboard boundary: if pedalboard is installed, confirm `src/tonematch/engines/native.py` declares the process boundary and no other module imports pedalboard. Until the project licence is chosen, this is the rule.
3. Repo scan: fail on any `.hxf`, `.hxb`, firmware images, files that look like HX Edit resources (model JSON with Line 6 ids outside `cache/`), or Native binaries. Warn on `.hlx` files anywhere except `fixtures/golden/` (gitignored) and `fixtures/smoke/known.hlx`.
4. Datasets: list datasets referenced in `data/` and `fixtures/synthetic/manifest.json` with their licence note from `docs/notes/` (ask `research` for any missing).
5. Trained weights: for each model card in `models/CARDS.md`, confirm it says whether the data came from Native renders or hardware renders and what that implies.

## Output

`reviews/licence-<date>.md` with pass or fail per section. Exit 1 on any fail.
