# Scripts

Small utilities the skills call. Most are built during the spikes by the agent that owns the skill; until then a skill says "build it to the contract first". Present today:

- `ledger_append.py` — append one row to `ledger/runs.csv` (see `contracts/ledger-format.md`).
- `stomp_spike_probe.py` (stomp-engine, T003) — hardware spike probe: find the Stomp's audio and MIDI ports, play a synthetic probe to USB 5/6 and record USB 1/2, compare two captures, send a CC or Program Change. Runbook in `docs/notes/T003-stomp-control-spike.md`.
- `reamp_capture.py` (stomp-engine, T004): skill `reamp-capture`. `--calibrate` measures the impulse latency and level gain and writes `cache/stomp/latency.json`; `<dry.wav> --out <wet.wav>` reamps through the Stomp (USB 5/6 in, USB 1/2 out), trimmed and level-matched; `--user-di` records dry and processed together. Uses the `stompd` control helper through `TONEMATCH_STOMPD` when it is present, and puts back every edit-buffer change it makes.
- `native_spike.py` (native-engine, T002) — host Helix Native, dump and scan its saved state for a preset document, prove a state round trip or probe the 16+16 automation params, measure render cost; `--capture-template` saves a chain template for the automation route. Exit 2 if Native is not installed. Runbook in `docs/notes/native-spike.md`.

- `loss_trust_check.py` (tone-metrics, T022): skill `loss-trust-check`. `--proxy --all [--ledger]` ranks the true preset against distractors per loss term on offline numpy DSP proxy data (dataset `proxy-dsp-v0`, NOT Helix, not admission evidence); without `--proxy` it needs `fixtures/synthetic/manifest.json` and exits 2 until benchmark builds it.
- `status_report.py` (status-reporter): skill `status-report`. Reads `docs/features.json` plus the board, gates, ledger and git, and writes the self-contained `reports/status/index.html` (`--run-checks` also runs pytest, ruff and `licence_check.py --no-write`). Exits 2 with a message if the registry uses an unknown status, health or evidence word. Standard library only.

- `engine_smoke.py` (benchmark, T008): skill `engine-smoke`. Detects an engine, renders `fixtures/smoke/di_2s.wav` and checks level, silence, NaN and length; `TONEMATCH_ENGINE=native|stomp|auto`; exits 2 with no row if no engine is present.
- `catalog_import.py` (preset, T005): reads the model definition files from the user's own HX Edit install into `cache/catalog/`; nothing is copied into the repo.
- `hlx_validate.py` (preset, T006): parses and validates an `.hlx` (blocks, DSP cost, amp/cab pairing, device version); also runs the byte-equal round trip.
- `make_smoke_hlx.py`, `make_smoke_di.py` (preset, benchmark): generate the local smoke preset and the synthetic smoke DI under `fixtures/smoke/`.
- `native_render_probe.py` (native-engine, T026): measures Native render cost (reset and flush modes, clip length, pool scaling); writes ledger `render-cost` rows with `--ledger`. Findings in `docs/notes/native-spike.md`.
- `separate.py` (separation, T020): skill `separate`. htdemucs_6s by default with backend detection; the RoFormer path is opt-in and needs `TONEMATCH_ROFORMER_LICENCE_ACK=1`.
- `search_smoke.py` (search, T023): Native smoke demo of `match()`; a timing and plumbing check, not a benchmark.
- `synthetic_benchmark.py` (benchmark, T024): generates the labelled synthetic-v1 manifest (labels only, written under gitignored `cache/synthetic/`); it cannot render the set until the human captures three Native chains.
- `licence_check.py` (reviewer, T011/T016a): scans dependency licences, the pedalboard import boundary, Line 6 assets and committed golden presets; allowlist in `docs/licence-allowlist.json`; writes `reviews/licence-<date>.md` (`--no-write` to skip).

Expected soon (owner in parentheses): `parity_null.py` (benchmark), `ab_session.py` (benchmark).
