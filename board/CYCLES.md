# Cycle notes

Written by `plan-sync` at the end of every cycle: date, mode, what shipped (ledger rows), what is next.

## Cycle 0 — 2026-10-13 — planning

Shipped: the plan (`docs/PLAN.md`), the agent team (`docs/AGENT-TEAM.md`), this repo scaffold. Next: cycle 1 in mode 1; see `board/TASKS.md`.

## Cycle 1 — 2026-10-13 — mode 1, prove the architecture

Shipped: T005 `src/tonematch/catalog.py` + `scripts/catalog_import.py` (real import of HX Edit 3.51: 599 models, 583 on the Stomp; cache gitignored); T007 `src/tonematch/gpu.py`, `docs/DEV-ENV.md`; T008 `scripts/engine_smoke.py`, `fixtures/smoke/di_2s.wav` (synthetic placeholder); T009 `docs/notes/` (3 files); T011 `scripts/licence_check.py`, `reviews/licence-2026-10-13.md` (PASS). Spikes T002 `src/tonematch/engines/native*.py`, `scripts/native_spike.py` and T003 `scripts/stomp_spike_probe.py`, `docs/notes/T003-stomp-control-spike.md` are built but have no verdict: Native is not installed and no pedal batch has run. Whole-repo ruff clean, 34 tests pass.
Ledger (`ledger/runs.csv`): line 2 `engines_found=0` (smoke), line 3 `protocol_crates_build_macos` (offline build only), line 4 `native_available=0` (render-cost). None is Gate 1 evidence; no gate ticked.
Reviews: T007 blocked once, fixed; T002 blocked once (pedalboard import in a test), fixed; T003, T005, T008 allowed.
Next: T012-T016. Blocked on the human: Native 3.8x and HX Edit 3.82 install, golden exports (T010), the pedal batch, reading the installers' EULAs.
Living doc not synced (no Claude Docs link in PLAN.md checked this cycle).
