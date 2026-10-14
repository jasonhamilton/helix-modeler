# Scripts

Small utilities the skills call. Most are built during the spikes by the agent that owns the skill; until then a skill says "build it to the contract first". Present today:

- `ledger_append.py` — append one row to `ledger/runs.csv` (see `contracts/ledger-format.md`).
- `stomp_spike_probe.py` (stomp-engine, T003) — hardware spike probe: find the Stomp's audio and MIDI ports, play a synthetic probe to USB 5/6 and record USB 1/2, compare two captures, send a CC or Program Change. Runbook in `docs/notes/T003-stomp-control-spike.md`.
- `native_spike.py` (native-engine, T002) — host Helix Native, dump and scan its saved state for a preset document, prove a state round trip or probe the 16+16 automation params, measure render cost; `--capture-template` saves a chain template for the automation route. Exit 2 if Native is not installed. Runbook in `docs/notes/native-spike.md`.

Expected soon (owner in parentheses): `engine_smoke.py` (benchmark), `catalog_import.py` (preset), `hlx_validate.py` (preset), `separate.py` (separation), `reamp_capture.py` (stomp-engine), `loss_trust_check.py` (tone-metrics), `synthetic_benchmark.py` (benchmark), `parity_null.py` (benchmark), `ab_session.py` (benchmark), `licence_check.py` (reviewer).
