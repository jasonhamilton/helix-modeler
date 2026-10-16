# Gates

Only `benchmark` ticks an evidence item, with a ledger row id. Only the human ticks the sign-off line.

## Gate 1 — both engines driven by code (end of week 1)

- [x] `engine-smoke` green on Native (ledger row: 2026-10-14T17:33:57+00:00 smoke/native/smoke peak_dbfs -13.5; render-cost 1.883 s/render same timestamp; benchmark run, TONEMATCH_ENGINE=native, exit 0)
- [x] `engine-smoke` green on the Stomp on at least one OS (ledger row: 2026-10-14T17:28:04+00:00 smoke/stomp/smoke peak_dbfs -13.49, with 17:28:05 seconds_per_render 2.6669; macOS, fw 3.50, written by stomp-engine; pedal restored to 04C; benchmark re-run blocked, see report)
- [x] A generated `.hlx` is accepted by HX Edit (no error -8309) and loads on the pedal (ledger rows: 2026-10-14T17:38:00+00:00 hx_edit_import_ok and 2026-10-14T17:51:18+00:00 pedal_load_ok, known.hlx as 04C Smoke Known; both human-reported via the lead, not observed by benchmark)
- [x] Engine interface v0 frozen in `contracts/engine-interface.md`, with the Native load route and the Stomp control route recorded in `docs/DECISIONS.md` (verified by benchmark: contract header says frozen 2026-10-14 and has Resolved-at-Gate-1 section; DECISIONS 2026-10-14 entries for Native route and Stomp route; no ledger row, repo evidence only)
- [x] Human sign-off: Jason, 2026-10-14 (instructed to the lead in session; caveats accepted: Stomp tick rests on stomp-engine rows, hardware-load rows are human-reported, firmware 3.50 and macOS only; gate ledger row: 2026-10-14T18:04:42+00:00 gate/both/gate1 gate_passed 1)
- Note (benchmark, 2026-10-14): benchmark-owned Stomp engine-smoke re-run now exists (ledger rows 2026-10-14T18:09:13+00:00 smoke/stomp/smoke peak_dbfs -13.47 and render-cost seconds_per_render 2.6823; TONEMATCH_ENGINE=stomp, stompd from cache/stomp/bin, exit 0, restore check passed, pedal on 04C). It corroborates the stomp-engine rows at 17:28:04/17:28:05 (-13.49 dBFS, 2.6669 s); no regression. The sign-off and gate row are unchanged.

## Gate 2 — recovers known presets (end of week 3)

- [ ] On synthetic-v1 the trusted loss ranks the true preset first at the threshold `benchmark` set (ledger row: )
- [ ] Block-type F1, model accuracy and parameter MAE meet the week-2 thresholds (ledger rows: )
- [ ] Separation stress curve recorded (ledger row: )
- [ ] Human sign-off: ______ (date)

## Gate 3 — beats the nearest preset on real songs (end of week 4)

- [ ] Blind A/B on the 20-song set: the matched preset is preferred over the nearest community preset on a majority of songs (ledger row: )
- [x] Parity null residual recorded for firmware 3.80 (ledger row: 2026-10-16T02:31:10, dataset parity-spot-fw380: amps -37.5..-47.5 dB, cabs -35.4..-37.4 dB, dynamics/EQ pass, distortion 2 of 5, reverb and modulation do not null, delays match in echo level; see docs/BENCHMARKING.md)
- [ ] Engine comparison recorded: Native mode vs Stomp mode on the same songs (ledger row: )
- [ ] Human sign-off (the listener): ______ (date)
