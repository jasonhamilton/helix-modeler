# Cycle notes

Written by `plan-sync` at the end of every cycle: date, mode, what shipped (ledger rows), what is next.

## Cycle 0 — 2026-10-13 — planning

Shipped: the plan (`docs/PLAN.md`), the agent team (`docs/AGENT-TEAM.md`), this repo scaffold. Next: cycle 1 in mode 1; see `board/TASKS.md`.

## Cycle 1 — 2026-10-13 — mode 1, prove the architecture

Shipped: [T005](./TASKS.md#t005 "catalog-import: locate the user's HX Edit install on each OS, read…") `src/tonematch/catalog.py` + `scripts/catalog_import.py` (real import of HX Edit 3.51: 599 models, 583 on the Stomp; cache gitignored); [T007](./TASKS.md#t007 "Dev environment notes for Windows, macOS, Linux") `src/tonematch/gpu.py`, `docs/DEV-ENV.md`; [T008](./TASKS.md#t008 "engine-smoke script and the first ledger row") `scripts/engine_smoke.py`, `fixtures/smoke/di_2s.wav` (synthetic placeholder); [T009](./TASKS.md#t009 "Pull the Native Pilot's Guide automation details and fretwire's…") `docs/notes/` (3 files); [T011](./TASKS.md#t011 "First licence-check run over pyproject.toml once the engine spikes…") `scripts/licence_check.py`, `reviews/licence-2026-10-13.md` (PASS). Spikes [T002](./TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…") `src/tonematch/engines/native*.py`, `scripts/native_spike.py` and [T003](./TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change") `scripts/stomp_spike_probe.py`, `docs/notes/T003-stomp-control-spike.md` are built but have no verdict: Native is not installed and no pedal batch has run. Whole-repo ruff clean, 34 tests pass.
Ledger (`ledger/runs.csv`): line 2 `engines_found=0` (smoke), line 3 `protocol_crates_build_macos` (offline build only), line 4 `native_available=0` (render-cost). None is Gate 1 evidence; no gate ticked.
Reviews: [T007](./TASKS.md#t007 "Dev environment notes for Windows, macOS, Linux") blocked once, fixed; [T002](./TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…") blocked once (pedalboard import in a test), fixed; [T003](./TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change"), [T005](./TASKS.md#t005 "catalog-import: locate the user's HX Edit install on each OS, read…"), [T008](./TASKS.md#t008 "engine-smoke script and the first ledger row") allowed.
Next: [T012](./TASKS.md#t012 "Install Helix Native 3.8x and HX Edit 3.82")-[T016](./TASKS.md#t016 "Fix the DeprecationWarning in scripts/licence_check.py"). Blocked on the human: Native 3.8x and HX Edit 3.82 install, golden exports ([T010](./TASKS.md#t010 "Confirm Stomp firmware 3.80 and Native 3.82")), the pedal batch, reading the installers' EULAs.
Living doc not synced (no Claude Docs link in PLAN.md checked this cycle).

## Cycle 2 — 2026-10-14 — mode 1, Gate 1 passed

Shipped: golden exports in; `src/tonematch/preset.py`, `scripts/hlx_validate.py`, `scripts/make_smoke_hlx.py`, `docs/notes/golden-diff.md` ([T006](./TASKS.md#t006 ".hlx round-trip: parse a golden template into Preset, write it back…"), both goldens round-trip byte for byte; catalog re-imported against HX Edit 3.82: 677 models); Native spike and engine (`src/tonematch/engines/native*.py`, `scripts/native_spike.py`, `docs/notes/native-spike.md`): whole-preset state write rejected, chain-template replay works; Stomp engine (`src/tonematch/engines/stomp*.py`, `src/tonematch/reamp.py`, `scripts/reamp_capture.py`, `docs/notes/stompd.md`): hardware batch H1-H9 on firmware 3.50, pedal restored, no flash writes; engine interface v0 frozen. 75 tests pass, ruff clean, licence-check PASS.
Ledger: Native smoke 17:33:57, Stomp smoke 17:28:04 and render-costs, `pedal_load_ok` 17:51:18, plus the Gate 1 `kind=gate` row.
Reviews: [T006](./TASKS.md#t006 ".hlx round-trip: parse a golden template into Preset, write it back…"), [T003](./TASKS.md#t003 "Spike: fretwire on Linux drives a block, model and parameter change"), [T004](./TASKS.md#t004 "Measure USB round-trip latency with an impulse") allowed (T004/T003 after bridge fixes); [T002](./TASKS.md#t002 "Spike: can pedalboard write a whole preset into Helix Native's saved…") and [T007](./TASKS.md#t007 "Dev environment notes for Windows, macOS, Linux") each blocked once and fixed.
Gate 1 signed by Jason 2026-10-14. Caveats: Stomp tick on stomp-engine rows; load rows human-reported; firmware 3.50 and macOS only.
Next: [T017](./TASKS.md#t017 "Read the EULA shipped with the Native and HX Edit installers")-[T019](./TASKS.md#t019 "Own Stomp engine-smoke row once the human approves running the local…"), [T020a](./TASKS.md#t020a "Stomp firmware 3.50 vs 3.80"), mode 2 (separation, dry-signal, tone-metrics, search, synthetic benchmark). Blocked on the human: stompd run approval, EULA reading, go on [T018](./TASKS.md#t018 "Spike: emit .hlx with knob/switch assignments pre-written so a new…").
Living doc not synced.

## Cycle 3 — 2026-10-14 — mode 2, make the loop work (mode 2 batch complete)

Shipped (commit 614bc18 and later): `separate` skill and `src/tonematch/separation/` ([T020](./TASKS.md#t020 "separate skill with backend detection and htdemucs_6s first"), synthetic mix only), `src/tonematch/dry.py` DI picker ([T021](./TASKS.md#t021 "Reference DI library index over EGDB"), no real library yet), loss kit v0 `src/tonematch/metrics/` with envelope v1 ([T022](./TASKS.md#t022 "Loss kit v0 with the no-DI terms"), [T025](./TASKS.md#t025 "Sharpen envelope (weakest proxy term, held-out rank-1 0.05)"); proxy results only, no term admitted), search core `src/tonematch/search/` ([T023](./TASKS.md#t023 "CMA-ES over the fixed template using cost() for the budget"); fake engine plus one smoke demo), synthetic benchmark design and generator ([T024](./TASKS.md#t024 "Synthetic recovery set v1 and its thresholds"); real manifest generated to gitignored cache/), RoFormer adapter gated ([T020b](./TASKS.md#t020b "RoFormer adapter licence read + install"); no licence granted for the only guitar-capable checkpoint), licence checker upgrades (allowlist, licence-file fallback, non-commercial detection), Native `cost()` cache fix and render-time measurements ([T026](./TASKS.md#t026 "Explain and re-measure the Native render-time gap")), benchmark-owned Stomp smoke ([T019](./TASKS.md#t019 "Own Stomp engine-smoke row once the human approves running the local…")), status reporting (`status-reporter` agent, `status-report` skill, `docs/features.json`, `scripts/status_report.py`, `reports/status/index.html`).
Ledger: separate rows (synthetic-sep-v0), loss-trust rows (proxy-dsp-v0), Native render-cost rows with load noted, Stomp smoke 18:09:13. No synthetic-v1, parity or ab rows.
Gate 2 has no evidence. No loss term is admitted. Nothing has been judged by ear.
Blocked on the human: three Native chain captures (A_clean, B_crunch, C_highgain), a reference DI set, the AFx-Rep checkpoint go, a real song for separation, EULA reading ([T017](./TASKS.md#t017 "Read the EULA shipped with the Native and HX Edit installers")), the [T018](./TASKS.md#t018 "Spike: emit .hlx with knob/switch assignments pre-written so a new…") spike go, firmware decision ([T020a](./TASKS.md#t020a "Stomp firmware 3.50 vs 3.80")), project licence.
Next: capture templates, then render synthetic-v1 and run the real Helix loss-trust check; freeze Gate 2 thresholds after baselines.
Living doc not synced.


## 2026-10-18 to 2026-10-19: distortion-first round (mode 2)

- **Human directions.** Distortion pedals only, from first principles. Model and method decisions go through the human (CLAUDE.md). New Gate 3, blind single-block; real pedals is now Gate 4 and real songs Gate 5.
- **Shipped:**
  - the Stomp USB freeze fix ([T168](./TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue"));
  - distortion research ([T167](./TASKS.md#t167 "TOP PRIORITY (human 2026-10-18)")) and the fingerprint probe on all 60 drives, with tone ends, measured families and the real-guitar test ([T171](./TASKS.md#t171 "Distortion identification steps 0 and 1")–[T174](./TASKS.md#t174 "Distortion measurement step 3"), [T179](./TASKS.md#t179 "Tone-knob ends at 0/0.1/0.9/1 on all drive models")–[T181](./TASKS.md#t181 "Step 4: identify model and settings from real guitar takes at random…"));
  - rating sessions v2–v5 ([T170](./TASKS.md#t170 "Human rating session"), [T177](./TASKS.md#t177 "Rating session v4: about 20 trials on near-twin models and large knob…"), [T188](./TASKS.md#t188 "Rating session v5: blind predictions re-rendered on the target's own…"));
  - no-DI drive matching measured at scale ([T183](./TASKS.md#t183 "End-to-end check of no-DI drive matching"), [T185](./TASKS.md#t185 "Larger no-DI drive e2e measurement")): AFx-Rep final ordering gives blind top-1 39% vs 10%, and the human's v5 ratings agree (18 vs 3);
  - predictor pilots v0 and v1 ([T186](./TASKS.md#t186 "Predictor pilot v0, drive only, on renders on disk"), [T189](./TASKS.md#t189 "Drive predictor pilot v1"));
  - measured-pedal mode prepared ([T187](./TASKS.md#t187 "Measured-pedal mode preparation"));
  - the AFx-Rep licence trail ([T190](./TASKS.md#t190 "AFx-Rep licence and alternatives, 12-hour run, research only"));
  - CLI tools reference and result manifests ([T172](./TASKS.md#t172 "Command-line tools reference + agent-readable results"));
  - the overnight dataset job ([T191](./TASKS.md#t191 "Unattended overnight drive dataset render")).
- **Blocked on the human:**
  - AFx-Rep final ranking as default;
  - a first measured-pedal session;
  - dropping families and the output-only fingerprint route;
  - testing PANNs/DAC;
  - the P3 reserve;
  - disk clean-up;
  - the [T191](./TASKS.md#t191 "Unattended overnight drive dataset render") batch renders, which the human runs.
- **Next:** read the [T191](./TASKS.md#t191 "Unattended overnight drive dataset render") summaries (50k vs 100k) and propose the predictor direction.
- **Living doc:** not synced.


## 2026-10-19 to 2026-10-20: predictor and real-song listening (mode 2)

- **Shipped:**
  - the overnight dataset job ([T191](./TASKS.md#t191 "Unattended overnight drive dataset render")), run in batches by the human: 150k drive renders;
  - the Drive-anomaly root cause, which is DI input level ([T194](./TASKS.md#t194 "Explain the Drive anomaly"));
  - the predictor-first matcher, about 12 renders per match ([T196](./TASKS.md#t196 "Predictor-first drive matcher option"));
  - the busy-core CPU monitor across all tools ([T192](./TASKS.md#t192 "CPU demand = busy cores"), [T193](./TASKS.md#t193 "Switch the remaining scripts' own os.getloadavg() guards to…"));
  - a reference chain for DS-Sample from a tutorial ([T197](./TASKS.md#t197 "DS-Sample reference chain on the Stomp")), tuned three ways ([T198](./TASKS.md#t198 "Tune the DS-Sample reference chain"), [T199](./TASKS.md#t199 "Refine DS-Sample version c with the cab High Cut freed"));
  - rating sessions v6 and v7.
- **The human's ear:**
  - Mother predictor-first match: "pretty good";
  - DS-Sample drive-only matches: "quite far off";
  - the tutorial chain: "pretty good";
  - tuned version c beat the tutorial blind and live;
  - c2 tied c.
- **Assessment:** docs/notes/state-of-project-2026-10-20.md.
- **Blocked on the human:**
  - measured-pedal session (Gate 4);
  - Gate 3 bars;
  - predictor-first as default;
  - input-level fix;
  - chains scope;
  - P3 reserve.
- **Running:** [T195](./TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep on a drive-dataset-v1…") encoder swap (train step).
- **Living doc:** not synced.
