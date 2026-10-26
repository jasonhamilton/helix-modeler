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

**Closed as superseded, 2026-10-25** (human bundle approval of the lead's decision 9; the human's 2026-10-18 verdict that the Gate 2 model is unusable stands; T152 and T155 cancelled). The checklist below is kept for the record and is not a pass.

Attempts: attempt 1 (`synthetic-v1`, `gate2_full_hybrid`, 2026-10-17) NOT PASSED, 5 of 7 checks. Attempt 2 (`synthetic-v2`, `gate2_v2_attempt2`, 2026-10-17) NOT PASSED, 6 of 7 checks (rank-1 0.9375 against 0.95). Attempt 3 (`synthetic-v3`, `gate2_v3_attempt3`, 2026-10-18): all 7 checks met under the frozen rule with rank-1 under the human's extended rule B (2026-10-18). Attempts 1 and 2 stay recorded under the old rule and are not re-scored. All three sets are SPENT.

- [x] On synthetic-v3 the trusted loss ranks the true preset first at the threshold `benchmark` set: rank-1 0.979 (94 of 96; misses i039, i095, both cab) against >= 0.95 under the extended rule B (ledger rows: 2026-10-18T15:46:15+00:00 benchmark/native/synthetic-v3 rank1_rate_audible 0.979167; weights admission rows 2026-10-18T13:41:39+00:00 loss-trust gain-trust-v1b rank1_rate_audible_ruleb_ext 0.9 and 0.9778, helix-trust-v2 0.9896 and 1.0; v1 attempt-1 and v2 attempt-2 rows are the earlier, not-passed attempts)
- [x] Block-type F1, model accuracy and parameter MAE meet the week-2 thresholds on synthetic-v3 and beat both baselines: category 0.885 (>= 0.85), model 0.771 (>= 0.60), settings error 0.101 on right-model items under the rule's definition (<= 0.15; ledger param_mae 0.135 also under), model vs random walk +0.208 CI [+0.094, +0.323], settings error vs random walk -0.083 CI [-0.109, -0.058] (ledger rows: 2026-10-18T15:46:15+00:00 block_f1 0.885417, model_acc 0.770833, param_mae 0.134751, heldout_ratio 0.900035; baselines 2026-10-18T16:31:24..25+00:00 v3_base_middle model_acc_cat_given 0.520833, model_acc_no_cat 0.375, block_f1_no_cat 0.666667, param_mae 0.206848; v3_base_random model_acc 0.5625, block_f1 0.770833, param_mae 0.224243; split-80 carries no information on v3 and is not counted)
- [x] Separation stress curve recorded (ledger row: 2026-10-16T15:22:49..51, dataset sep-stress-v1: htdemucs_6s SDR 21.2/15.9/15.4/11.2/7.3 dB for densities D1-D5; SYNTHETIC set (Guitar-TECHS DI through a numpy drive/cab, synthesised accompaniment), real-song stress not yet measured — ticked by tone-matcher-lead)
- [ ] Human sign-off: awaiting human sign-off (as of 2026-10-18). Not ticked. The gate ledger row is written only with the human's sign-off. Caveats to accept or not: known-DI case only; the rank-1 rule was amended by the human before v3; three sealed sets consumed; delay model accuracy is 5 of 12.

## Gate 3 — blind prediction of a single block without the dry track (new, human 2026-10-18)

Each test item is one Helix block with a random model and random settings, rendered in Native from a random guitar sample. The matcher is given ONLY the rendered output. It never sees the item's dry sample: that sample comes from DI material held out of everything the matcher may render with or train on, and is used only afterwards, to score. Work starts after the no-DI research (T158). `benchmark` sets the bars and registers them in `docs/DECISIONS.md` BEFORE the sealed set is generated.

- [x] Sealed blind set generated: one block per item, random model and settings, random held-out guitar sample, hashes recorded, labels and dry samples unread by the matcher - DONE 2026-10-25 by benchmark as `gate3-v1`: 120 items (60 product drive models x 2), Guitar-TECHS P3 DI (the reserved third player), seed 223001, sha256 manifest b69372d4b7c4126ad6dd47a0b7671228530fd3d37f7f178b3fa30fa595906b27, labels 88a8880c14947375f2fa21333d86298e491b2d649983b702b480898a848cdbaa, nothing scored, note docs/notes/gate3-v1.md (ledger row: 2026-10-25T18:58:08+00:00 benchmark/benchmark/gate3-v1 sealed_items 120)
- [ ] Bars and baselines registered before the run: category, model and settings-error thresholds; middle-of-every-knob and random-walk baselines run under the same no-dry-track condition PROPOSED by benchmark 2026-10-25, NOT APPROVED; the human has not set any bar. The lead puts them to the human before the set is scored. Set `gate3-v1`: single drive block, 60 product drive models x 2, Guitar-TECHS P3 DI (the reserved third player), 120 items. G3-1 model top-1 >= 0.50 (chance 0.017); G3-2 model top-3 >= 0.70; G3-3 on right-model items, Drive within +-0.15 >= 0.60 and the mean over all free non-level knobs within +-0.15 >= 0.50; G3-4 each of G3-1..3 beats every baseline (random model with middle-of-every-knob settings; the true model at middle-of-every-knob; the T185 AFx-ordered arm) by >= +0.15 paired, exact McNemar p < 0.05; G3-5 (ear, the later short blind listen) with >= 30 decided trials and >= 6 repeats, the prediction re-rendered on the held-out P3 dry is judged closer to the target than the best baseline's pick in >= 70% of decided trials (21 of 30 gives p = 0.043); the listener's test-retest is reported first. Block category is trivial for a drive-only set and is dropped; one scoring attempt per frozen method, a changed method needs a new set. Text in docs/DECISIONS.md 2026-10-25 (benchmark, T223). (ledger row: 2026-10-25 benchmark/benchmark/gate3-v1 bars_registered)
- [ ] Blind prediction meets the registered bars and beats both baselines on the sealed set (ledger rows: )
- [ ] Short blind listen: the prediction re-rendered from the held-out dry sample against the target (ledger row: )
- [ ] Human sign-off: ______ (date)

## Gate 4 — matches clean recordings of real pedals (new, human 2026-10-17; was Gate 3, renumbered 2026-10-18)

The matcher sees only the pedal's OUTPUT, plus a separate dry sample of the player's guitar (random playing, different notes). The dry take of the same performance is recorded but used only afterwards, to score. Real pedals need not match a Helix model exactly; success is sounding close.

- [ ] Dry reference captured: the player's guitar, random playing, over USB 5/6 or the interface; the only dry signal the matcher may use (ledger row: )
- [ ] Single-pedal set recorded: at least one pedal per available category (drive, delay, reverb, modulation, compressor, EQ or amp-in-a-box), several random settings each, with the matching dry take kept aside for scoring (ledger row: )
- [ ] Single-pedal match quality meets the bar `benchmark` sets BEFORE the run (score of the matched preset re-rendered from the held-out dry take vs the pedal recording, against baselines), plus a short blind listen (ledger rows: )
- [ ] Combinations: two- and three-pedal chains recorded and matched by the chain builder at the same bar (ledger rows: )
- [ ] Human sign-off (the player): ______ (date)

## Gate 5 — beats the nearest preset on real songs (was Gate 3, then Gate 4; renumbered 2026-10-17 and 2026-10-18)

- [ ] Blind A/B on the 20-song set: the matched preset is preferred over the nearest community preset on a majority of songs (ledger row: )
- [x] Parity null residual recorded for firmware 3.80 (ledger row: 2026-10-16T02:31:10, dataset parity-spot-fw380: amps -37.5..-47.5 dB, cabs -35.4..-37.4 dB, dynamics/EQ pass, distortion 2 of 5, reverb and modulation do not null, delays match in echo level; see docs/BENCHMARKING.md)
- [ ] Engine comparison recorded: Native mode vs Stomp mode on the same songs (ledger row: )
- [ ] Human sign-off (the listener): ______ (date)
