# Scripts

Small utilities the skills call. Most are built during the spikes by the agent that owns the skill; until then a skill says "build it to the contract first". Present today:

- `ledger_append.py` — append one row to `ledger/runs.csv` (see `contracts/ledger-format.md`).
- `stomp_spike_probe.py` (stomp-engine, T003) — hardware spike probe: find the Stomp's audio and MIDI ports, play a synthetic probe to USB 5/6 and record USB 1/2, compare two captures, send a CC or Program Change. Runbook in `docs/notes/T003-stomp-control-spike.md`.
- `reamp_capture.py` (stomp-engine, T004): skill `reamp-capture`. `--calibrate` measures the impulse latency and level gain and writes `cache/stomp/latency.json`; `<dry.wav> --out <wet.wav>` reamps through the Stomp (USB 5/6 in, USB 1/2 out), trimmed and level-matched; `--user-di` records dry and processed together. Uses the `stompd` control helper through `TONEMATCH_STOMPD` when it is present, and puts back every edit-buffer change it makes.
- `native_spike.py` (native-engine, T002) — host Helix Native, dump and scan its saved state for a preset document, prove a state round trip or probe the 16+16 automation params, measure render cost; `--capture-template` saves a chain template for the automation route. Exit 2 if Native is not installed. Runbook in `docs/notes/native-spike.md`.

- `loss_trust_check.py` (tone-metrics, T022): skill `loss-trust-check`. `--proxy --all [--ledger]` ranks the true preset against distractors per loss term on offline numpy DSP proxy data (dataset `proxy-dsp-v0`, NOT Helix, not admission evidence); without `--proxy` it needs `fixtures/synthetic/manifest.json` and exits 2 until benchmark builds it.

Expected soon (owner in parentheses): `engine_smoke.py` (benchmark), `catalog_import.py` (preset), `hlx_validate.py` (preset), `separate.py` (separation), `synthetic_benchmark.py` (benchmark), `parity_null.py` (benchmark), `ab_session.py` (benchmark), `licence_check.py` (reviewer).
