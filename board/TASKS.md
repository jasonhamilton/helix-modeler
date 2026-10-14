# Task board

Format: `- [ ] <id> · <owner> · <task> · blocked by <ids> · mode <n>`. See `contracts/board.md`.

## Cycle 1 — mode 1, prove the architecture

- [ ] T001 · tone-matcher-lead · Freeze engine interface v0 (`contracts/engine-interface.md`, `src/tonematch/engines/base.py`) after the two engine spikes report · blocked by T002, T003 · mode 1
- [ ] T002 · native-engine · Spike: can pedalboard write a whole preset into Helix Native's saved state? Inspect the state blob; if preset JSON is inside, prove a round-trip. If not, prove 16+16 automation or MIDI CC on one fixed chain. Report yes/no with evidence · blocked by T010 · mode 1 · started 2026-10-13 · harness done 2026-10-14, verdict PENDING: Helix Native is NOT installed on the dev Mac (ledger 2026-10-14T06:02:26+00:00 render-cost native_available=0). Host boundary, JUCE state codec, latency/cost/pool proven on stand-ins; run `python scripts/native_spike.py --gui --golden <hlx> --ledger` once Native 3.8x is installed (runbook docs/notes/native-spike.md)
- [ ] T003 · stomp-engine · Spike: fretwire on Linux drives a block, model and parameter change; attempt its protocol crates on Windows and macOS; prove MIDI CC on one parameter as the fallback; check coexistence with the Line 6 driver and HX Edit · mode 1 · started 2026-10-13 · offline half done 2026-10-14 (ledger 2026-10-14T06:01:52+00:00 protocol_crates_build_macos): macOS route = USB iface 0 via tonepush hx-usb (fretwire builds too); Windows likely MIDI CC fallback; HX Edit must be quit. Hardware batch H1-H11 in docs/notes/T003-stomp-control-spike.md pending, needs T010 (pedal + scratch slot named)
- [ ] T004 · stomp-engine · Measure USB round-trip latency with an impulse (USB 5/6 in, 1/2 out) and implement `reamp-capture` · blocked by T003 · mode 1
- [x] T005 · preset · `catalog-import`: locate the user's HX Edit install on each OS, read the model JSON, cache under `cache/catalog/` · mode 1 · done: src/tonematch/catalog.py, scripts/catalog_import.py, tests/test_catalog.py; real import 3.51 ok (599 models)
- [ ] T006 · preset · `.hlx` round-trip: parse a golden template into `Preset`, write it back byte-equivalent where it matters, `hlx-validate` passes · blocked by T010 · mode 1
- [x] T007 · platform · Dev environment notes for Windows, macOS, Linux; GPU backend detection stub (CUDA, MPS, ROCm, DirectML, CPU); Wine + yabridge note for Native on Linux · mode 1 · started: platform agent, cycle 1 · done: gpu.py stub + tests + docs/DEV-ENV.md; backend verified as cpu on arm64 macOS with no torch installed (other branches covered by monkeypatched tests only)
- [x] T008 · benchmark · `engine-smoke` script and the first ledger row; `fixtures/smoke/di_2s.wav` (a 2 s clean DI clip from a licence-clean source) · mode 1 · done: scripts/engine_smoke.py runs; no-engine path exits 2 with no row (earlier engines_found=0 row kept, append-only); DI is synthetic, real clip still needed (fixtures/smoke/PROVENANCE.md)
- [x] T009 · research · Pull the Native Pilot's Guide automation details and fretwire's protocol notes into `docs/notes/` with sources · mode 1 · done: docs/notes/{native-automation,fretwire-protocol,line6-eula-flags}.md; Pilot's Guide PDF unread; no ledger row
- [ ] T010 · human · Confirm Stomp firmware 3.80 and Native 3.82; connect the Stomp; export the two golden templates into `fixtures/golden/` · mode 1
- [x] T011 · reviewer · First `licence-check` run over `pyproject.toml` once the engine spikes add dependencies · blocked by T002, T003 · mode 1 · done: scripts/licence_check.py, tests/test_licence_check.py, reviews/licence-2026-10-13.md PASS (DeprecationWarning at licence_check.py:47 to fix)

## Cycle 2 — mode 1, still proving the architecture (blocked on the human)

- [ ] T012 · human · Install Helix Native 3.8x and HX Edit 3.82 (this Mac has HX Edit 3.51); read the EULA in the Native and HX Edit installers · mode 1
- [ ] T013 · native-engine · Run `scripts/native_spike.py --gui --golden ... --ledger` for the T002 verdict · blocked by T010, T012 · mode 1
- [ ] T014 · human · Pedal batch H1-H11 from `docs/notes/T003-stomp-control-spike.md`: name one scratch preset slot; HX Edit quit; leave the pedal on its original preset · blocked by T010 · mode 1
- [ ] T015 · benchmark · Re-run `engine-smoke` on each engine once one is present; Gate 1 ledger rows · blocked by T013, T014 · mode 1
- [ ] T016 · reviewer · Fix the DeprecationWarning in `scripts/licence_check.py`; add real GPL-boundary scan of installed extras · mode 1

## Parked (later modes)

- [ ] T020 · separation · `separate` skill with backend detection and `htdemucs_6s` first; RoFormer checkpoints behind a flag · mode 2
- [ ] T021 · dry-signal · Reference DI library index over EGDB; picker by register and style · mode 2
- [ ] T022 · tone-metrics · Loss kit v0 with the no-DI terms; `loss-trust-check` · mode 2
- [ ] T023 · search · CMA-ES over the fixed template using `cost()` for the budget; successive halving for Stomp mode · mode 2
- [ ] T024 · benchmark · Synthetic recovery set v1 and its thresholds · mode 2
- [ ] T030 · learning · Data generator: EGDB DI clips through random plausible chains on Native; predictor v0 · mode 4
