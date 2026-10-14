# Decision log

Append-only. One entry per decision: date, decision, why, who decided. The lead writes here at the end of every cycle (`plan-sync`). Decisions made before the repo existed are listed first.

## 2026-10-13 (planning, before the repo)

- **No firmware decompilation or emulation.** Months of work, legal risk (Davidson v. Internet Gateway; DMCA 1201(f) is narrow), and no gain over Native. Decompiling for *interoperability* (USB control protocol, preset format, what fretwire did) is defensible; decompiling the DSP code is not and is pointless. — Jason
- **Render engine is user-selectable**: Helix Native or the HX Stomp, behind one interface (`load`, `set_params`, `render`, `cost`, `capabilities`). With both present, Native searches and the Stomp verifies by default. — Jason
- **At least one engine is required.** A "neither engine, shipped models only" tier was rejected. — Jason
- **GPU optional, not NVIDIA-specific.** Used only for separation speed and for training the predictor and proxies. CUDA, Apple MPS, AMD ROCm, DirectML and CPU are all acceptable backends. — Jason
- **User-supplied stems are accepted** and skip separation; the original mix is always required too. — Jason
- **Hosted separation APIs deferred** because they send song audio to a third party. — Jason
- **Dry signal, v0 build**: B (reference DI library) as the default and E (blind estimation) as the starting guess. A (user-recorded DI) is a v1 accuracy mode. A+B hybrid and C (blind DI recovery) are research. D (transcribe and resynthesize) is low priority. — Jason
- **Comparison is layered**: artifact-robust embeddings for the bulk of the search, confidence-masked comparison, exposed-guitar sections, separation-consistent recombine-and-re-separate for final candidates, and a blind human listen as the final gate. — Jason
- **Two export modes**: full-rig (default, exports amp and cab) and effects-only (amp and cab held fixed as a stand-in, effects exported). — Jason
- **Fixed 8-slot template for v0**: compressor, drive, amp, cab, EQ, modulation, delay, reverb; unused slots bypassed. — Jason
- **No Line 6 data or binaries in the repo**; catalog imported at runtime from the user's HX Edit install (fretwire pattern). Pin original Helix Native 3.8x; avoid Stadium Native. — Jason
- **Neural proxies per block deferred** to after week 6. — Jason
- **An optional LLM "listen and suggest a starting chain" step was declined.** — Jason
- **Agent team adopted**: 13 agents, 12 skills, 5 contracts, a cycle with 5 priority modes. See `docs/AGENT-TEAM.md`. — Jason
- **Catalog source is the `*.models` files in HX Edit's Resources folder, not `HX_ModelCatalog.json`.** The latter lists only names and param labels; the former carry per-parameter min/max/default/kind, per-model DSP `load` (mono and stereo), and a per-device firmware filter. Logic lives in `src/tonematch/catalog.py`; `scripts/catalog_import.py` is the CLI. — preset (T005)

## 2026-10-13 (cycle 1, mode 1)

- **Stomp control route (proposed, unconfirmed until the pedal batch H1-H11 runs):** USB control via tonepush's crate (fretwire as cross-check) on macOS and Linux; MIDI CC fallback on Windows unless step H11 shows otherwise; HX Edit is quit during Stomp runs because it holds the control interface exclusively. Evidence: `docs/notes/T003-stomp-control-spike.md` (offline only, no device touched; ledger line 3 is a build check, not Gate 1 evidence). — lead, on `stomp-engine`'s proposal
- **Edit-buffer-only rule:** `load()` and `set_params()` must never write a flash slot; `close()` restores the original preset by re-selecting it. Wording change to `close()` in the contract is deferred to T001. — lead
- **Native route is undecided.** Helix Native is not installed on this Mac, so the T002 verdict (whole-preset write vs 16+16 automation vs MIDI CC) is pending. `NativeEngine` defaults to the 16+16 route until the spike proves a round trip. Ledger line 4 (`native_available=0`) is not evidence of anything else. — lead
- **Reference level read as a fixed calibration gain** (Native's digital output as reference), not per-clip normalisation; **latency is measured once per chain**, not once per engine. Interim interpretations by `native-engine`, accepted pending T001. Affects `stomp-engine`, `tone-metrics`, `benchmark`. — lead
- **Catalog proposals held for T001/T006:** seed `dsp-costs.json` from the catalog's per-model `load`; make the catalog key layout canonical in `preset-schema.md`; add `Preset.to_hlx_dict()`. HX Edit's DSP units and budget are unverified. — lead
- **pedalboard (GPLv3) stays behind a process boundary**: imported only in `engines/native_host.py`, never in the main process or in tests (`test_gpl_host_never_imported_in_this_process` guards it). Declared as the optional `native` extra. — lead, on `reviewer`'s block
- **Smoke-series hygiene:** the `engines_found=0` row (ledger line 2) is kept but is not evidence; `engine-smoke` with no engine now writes no row and exits 2. No `smoke`-kind rows for build-only evidence. — lead
- **Lint is a merge condition:** whole-repo `ruff` clean (deliberate broad catches carry a reasoned `noqa`). — lead
- **Version gap:** this Mac has HX Edit 3.51; targets are HX Edit 3.82, Native 3.8x, Stomp firmware 3.80. The 3.51 catalog may lack 3.8x models, so re-run `catalog-import` after upgrading. — lead
- **Licence flags (not legal advice):** the generic Line 6 EULA forbids reverse engineering beyond interop and hosted/cloud use, and is silent on training ML on Native renders; prefer hardware renders for anything published until the installers' EULAs are read. Native allows 4 authorised computers. See `docs/notes/line6-eula-flags.md`. — `research`, noted by lead
