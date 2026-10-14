# Gates

Only `benchmark` ticks an evidence item, with a ledger row id. Only the human ticks the sign-off line.

## Gate 1 — both engines driven by code (end of week 1)

- [ ] `engine-smoke` green on Native (ledger row: )
- [ ] `engine-smoke` green on the Stomp on at least one OS (ledger row: )
- [ ] A generated `.hlx` is accepted by HX Edit (no error -8309) and loads on the pedal (ledger row: )
- [ ] Engine interface v0 frozen in `contracts/engine-interface.md`, with the Native load route and the Stomp control route recorded in `docs/DECISIONS.md`
- [ ] Human sign-off: ______ (date)

## Gate 2 — recovers known presets (end of week 3)

- [ ] On synthetic-v1 the trusted loss ranks the true preset first at the threshold `benchmark` set (ledger row: )
- [ ] Block-type F1, model accuracy and parameter MAE meet the week-2 thresholds (ledger rows: )
- [ ] Separation stress curve recorded (ledger row: )
- [ ] Human sign-off: ______ (date)

## Gate 3 — beats the nearest preset on real songs (end of week 4)

- [ ] Blind A/B on the 20-song set: the matched preset is preferred over the nearest community preset on a majority of songs (ledger row: )
- [ ] Parity null residual recorded for firmware 3.80 (ledger row: )
- [ ] Engine comparison recorded: Native mode vs Stomp mode on the same songs (ledger row: )
- [ ] Human sign-off (the listener): ______ (date)
