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

## Cycle 2 — 2026-10-14 — mode 1, Gate 1 passed

Shipped: golden exports in; `src/tonematch/preset.py`, `scripts/hlx_validate.py`, `scripts/make_smoke_hlx.py`, `docs/notes/golden-diff.md` (T006, both goldens round-trip byte for byte; catalog re-imported against HX Edit 3.82: 677 models); Native spike and engine (`src/tonematch/engines/native*.py`, `scripts/native_spike.py`, `docs/notes/native-spike.md`): whole-preset state write rejected, chain-template replay works; Stomp engine (`src/tonematch/engines/stomp*.py`, `src/tonematch/reamp.py`, `scripts/reamp_capture.py`, `docs/notes/stompd.md`): hardware batch H1-H9 on firmware 3.50, pedal restored, no flash writes; engine interface v0 frozen. 75 tests pass, ruff clean, licence-check PASS.
Ledger: Native smoke 17:33:57, Stomp smoke 17:28:04 and render-costs, `pedal_load_ok` 17:51:18, plus the Gate 1 `kind=gate` row.
Reviews: T006, T003, T004 allowed (T004/T003 after bridge fixes); T002 and T007 each blocked once and fixed.
Gate 1 signed by Jason 2026-10-14. Caveats: Stomp tick on stomp-engine rows; load rows human-reported; firmware 3.50 and macOS only.
Next: T017-T019, T020a, mode 2 (separation, dry-signal, tone-metrics, search, synthetic benchmark). Blocked on the human: stompd run approval, EULA reading, go on T018.
Living doc not synced.

## Cycle 3 — 2026-10-14 — mode 2, make the loop work (mode 2 batch complete)

Shipped (commit 614bc18 and later): `separate` skill and `src/tonematch/separation/` (T020, synthetic mix only), `src/tonematch/dry.py` DI picker (T021, no real library yet), loss kit v0 `src/tonematch/metrics/` with envelope v1 (T022, T025; proxy results only, no term admitted), search core `src/tonematch/search/` (T023; fake engine plus one smoke demo), synthetic benchmark design and generator (T024; real manifest generated to gitignored cache/), RoFormer adapter gated (T020b; no licence granted for the only guitar-capable checkpoint), licence checker upgrades (allowlist, licence-file fallback, non-commercial detection), Native `cost()` cache fix and render-time measurements (T026), benchmark-owned Stomp smoke (T019), status reporting (`status-reporter` agent, `status-report` skill, `docs/features.json`, `scripts/status_report.py`, `reports/status/index.html`).
Ledger: separate rows (synthetic-sep-v0), loss-trust rows (proxy-dsp-v0), Native render-cost rows with load noted, Stomp smoke 18:09:13. No synthetic-v1, parity or ab rows.
Gate 2 has no evidence. No loss term is admitted. Nothing has been judged by ear.
Blocked on the human: three Native chain captures (A_clean, B_crunch, C_highgain), a reference DI set, the AFx-Rep checkpoint go, a real song for separation, EULA reading (T017), the T018 spike go, firmware decision (T020a), project licence.
Next: capture templates, then render synthetic-v1 and run the real Helix loss-trust check; freeze Gate 2 thresholds after baselines.
Living doc not synced.
