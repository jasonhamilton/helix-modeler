# Project analysis, 2026-10-23 (lead, on Fable)

Written at the human's request ("evaluate our current methodology and progress… the different alternatives we've proposed… better methods… for the full chain… our dry sampling technique"). An assessment and a set of proposals; nothing here is adopted (CLAUDE.md). Three inputs, all run on Fable:

- [methodology-audit-2026-10-23.md](methodology-audit-2026-10-23.md): an independent, read-only audit of the primary records (DECISIONS, ledger, board, notes), with file:line citations.
- [research-full-chain-2026-10-23.md](research-full-chain-2026-10-23.md): blind chain estimation and advanced predictors, 2023–2026, tagged FACT / INTERPRETATION / UNVERIFIED.
- [research-dry-signal-2026-10-23.md](research-dry-signal-2026-10-23.md): the rebuilt-dry strategy against effect removal, better transcription, dry-free routes, separation and level.

Published page: https://claude.ai/artifact/2YAGoEnFog8L7GpM3V5McF. Pending at the time of writing: the [T215](../../board/TASKS.md#t215 "Chain training pilot") chain-training pilot and the DI-invariance check ([T221](../../board/TASKS.md#t221 "DI-invariance ratio check")).

## Verdict

1. **Single drive-block matching works by ear in open listening.** Rebuilt dry → all 60 drives rendered → AFx-Rep ranking → knob fine-tune: "pretty good" on all seven songs, "same or better" after fine-tuning. One listener, one pass, no alternative heard, not blind.
2. **No result since 2026-10-14 rests on a sealed blind test.** Gate 2 (the only pre-registered sealed gate) was disowned the day it passed and is unsigned; Gate 3 has no bars and no set. The human's test-retest consistency on repeated blind pairs is 7 of 12, so 6–25-trial sessions cannot resolve what they were read as resolving.
3. **AFx-Rep distance is used past its validation.** Validated for ordering models on the same performance (v3 18/25, v5 20/21). Now the objective for knob fine-tuning and for every "closer" claim between runs with different dry tracks, which [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") (invariance ratio 30 for small knob moves) says it cannot do; never rated against a separated stem.
4. **Full chains are the gap and single-block training does not transfer.** 240k single-block renders preceded the one-hour chain check; [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") then showed drive 0.02 / amp 0.01 top-1 inside chains. The literature says this is the documented behaviour of single-effect models; every published chain estimator trained on chain renders at 60–90× our pilot's scale.
5. **The rebuilt dry is an unmeasured hypothesis.** No published system searches with a transcribed-and-resynthesised guitar; the ablation "same search, generic library DI" was never run; the chord result (better dry, worse match) says the rebuild acts as a level-and-rhythm proxy, not a timbre proxy. The user's own take is the only input with a measured gain (83% vs 67% top-1).
6. **Governance improved in form, not substance.** Bundled one-word approvals; AFx-Rep final ranking (approved for shortlisting only) and fine-tuning with Output/Master free (against the 10-18 ruling on knobs) became defaults by reuse. The lead's failure, fixed by itemised approvals and a test pinning shipped defaults to an approved list.

## Evidence classes of the main claims (audit section 1)

| Claim | Class | n | Limit |
|---|---|---|---|
| AFx-Rep beats the provisional score (39% vs 10% blind top-1) | D + B | 96 + 21 | same engine, same players; v5 rated [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")'s own picks |
| Predictor-first 85% top-1 | D | 48 + 24 | unsealed set drawn after the method; own-take cells not significant |
| Seven songs "pretty good"; fine-tune "same or better" | C | 7 | open, one pass, no alternative, level knobs moved |
| Calibration "closer" (3 of 7 by > 0.04) | D | 7 | three factors changed; cross-dry-track distances; no ear verdict |
| Slot heads within player (amp 0.54, cab 0.76, EQ 0.82) | D | 18 files | two players; weak on a new player |
| Heads fail in chains (drive 0.02, amp 0.01) | D | 295 | sound negative result |
| Gate 2 "all 7 checks" | A | 96 | three sets consumed, rule amended, bar at random-walk level; disowned |

A = blind, sealed, pre-registered bar; B = blind unsealed; C = open listen; D = numbers on an unadmitted metric or without a bar.

## Alternatives (audit section 4, condensed)

Tested and in use: AFx-Rep distance; frozen AFx-Rep + heads. Tested, not adopted: level-conditioned head (fixes the Drive bias). Tested and rejected: provisional loss, mrstft, LTAS/texture/cepstral statistics, PANNs/DAC encoders, from-scratch CNN, circuit fingerprints and families (families still a default). Built, not verified: measured-pedal mode (simulation only), chord rebuild (rejected by ear on one song), guitar profile (no ear verdict), user-DI accuracy mode (the only condition with reliable knobs). Pilot running: chain-rendered training ([T215](../../board/TASKS.md#t215 "Chain training pilot")). Proposed only: song-like training data (mix + re-separate), separation-consistent re-score (in the contract, never run), content-invariant embedding on own renders, learned metric from the 159 rated trials, circuit-model fitting, note-by-note / sustain-only / multi-dry robust ranking / level-sweep scores, dry-recovery networks, linear-table chain rendering, artist-rig priors.

## Full-chain routes (research note section 5)

1. Chain-rendered training at scale with sequential decoding (cab → amp → drive, beam 3–5 per slot into the existing render-and-rank). Gate: [T215](../../board/TASKS.md#t215 "Chain training pilot") drive/amp top-3 > 5× chance on the 295-chain test; oracle cab conditioning adds ≥ 0.1 to amp top-3.
2. Retrieval from the 1,932 user presets rendered on 2–3 DIs (~15k block renders, ~2 h, local).
3. Contrastive content-invariant chain embedding on frozen features (CPU, no renders); gate: invariance ratio ≤ 0.5, cross-player top-1 +0.1.
4. Separation-aware training augmentation (render → mix into licence-clean or local backing → re-separate → features); gate: recover half the drop seen when clean-trained heads meet re-separated chains.
5. Bounded linear finishing block (cab cuts / EQ / IR fitted to the stem), blind A/B.
6. Neural proxies (NablAFx, MIT): one-amp test, Spearman ≥ 0.8 against Native, else drop.
7. Posterior heads, LLM/agentic, dry-recovery bridge: after 1–3.

Why the cab erases the single-block heads: identity learned from > 5 kHz texture that a cab removes, and a globally pooled log-mel embedding dominated by the envelope the cab sets (INTERPRETATION in the note).

## The dry signal (research note section 7)

Verdict: a defensible level-and-rhythm proxy, not a timbre proxy; contribution unmeasured. Ranked tests: (1) the un-run ablation, four arms per song (generic library DI; plain rebuild; rebuild with the stem's note envelope imposed; the human's own take), blind pack; (2) level-only ablation −36.1 vs −28.1 dBFS; (3) DI-invariance ratio on cached renders (started, [T221](../../board/TASKS.md#t221 "DI-invariance ratio check")); (4) separation-consistent re-score of the seven shortlists; (5) predictor trained on separated inputs; (6) transcription-upgrade check. Effect removal is not usable yet (RemFX 16–24 dB SI-SDR on acoustic single plug-ins; MSR 2025 guitars about 4 dB; Positive Grid data in-house). Separation: htdemucs_6s 5.2 dB guitar SDR vs 9.05 for a checkpoint with no licence grant; Banquet (MIT) is the clean step up. Level: play at the play-along level, carry level as metadata (NAM practice), warn on a > 3 dB disagreement.

## Recommended plan (proposals)

1. Validate AFx-Rep against separated stems blind: 5 songs × 5 candidates on the same rebuilt dry, ranked by the human with repeats; report rank agreement.
2. Run dry-signal tests 1 and 2 with a blind pack before more rebuild work.
3. Read [T215](../../board/TASKS.md#t215 "Chain training pilot") against a sealed bar: `benchmark` registers bars and seals a set with P3 before the numbers are read.
4. Contrastive score on own renders only if the invariance check shows AFx-Rep's ratio is poor.
5. Build the separation-consistent re-ranker (under an hour to test).
6. Keep the own-take accuracy mode; measure it blind against the rebuild on three songs.
7. Governance mechanics: itemised approvals; a test pinning `song_match.py` method flags to an approved-methods file only the human may change; remove the provisional loss and the drive families as defaults.

Stop or defer: new scoring methods; more open listens of drive-only matches; rebuild-alignment tuning; further single-block slot rendering until [T215](../../board/TASKS.md#t215 "Chain training pilot") reports; status-report growth that restates unadmitted numbers; the unlicensed separation checkpoint; a tab model before test 6.

## Decisions for the human (itemised)

1. AFx-Rep final ranking for song matches: interim default, or shortlisting only.
2. The blind validation design (plan item 1).
3. Knob fine-tuning on AFx-Rep distance: default, option, or stop until item 2 reports; Output/Master locked.
4. The dry-signal ablation including the envelope-hybrid arm.
5. Level convention: play-along (−28.1) with the profile warning, or calibration (−36.1).
6. Gate 3 bars and a sealed P3 set before [T215](../../board/TASKS.md#t215 "Chain training pilot") is read.
7. Separation-consistent re-score as a shortlist re-ranker.
8. Approval format: one line per item in the human's words.
9. Gate 2: close as superseded or schedule its confirmation.
10. Remove the provisional loss and the families as defaults.

## Corrections to the record (audit section 6)

Beyond the two the lead logged on 10-23: "two independent tests" for AFx-Rep ranking (one experiment heard); measured-pedal mode under "working" (simulation); "thousands of test clips" (18 files); "a whole chain can be tuned" (6 decided trials, 1 of 2 repeats consistent); "0.99 against a 0.90 bar" (unapproved score, chance on hard pairs); Gate 2 "3 of 3 evidenced" (omits the human's verdict and T152/T155); the drive "15 of 17" knob result (52% at n = 96). The status-reporter is correcting reports/status and the state-of-project notes; the `song-match` skill description was corrected the same day (it ran the AFx-Rep full search while labelled "predictor-first").

## Added the same day: the DI-invariance check ([T221](../../board/TASKS.md#t221 "DI-invariance ratio check"), measured, no new renders)

On 7,090 cached clips (278 targets, 116k pairs): no score can set knobs when the dry recording differs from the target's (other-player invariance ratio R: AFx-Rep 1.02, LTAS tilt 1.32, envelope 1.86, crest 1.96, mrstft 2.30; at or above 1 the recording change swamps a wrong setting). AFx-Rep is the only score whose candidate order survives (Spearman 0.79 other player, 0.83 same player other notes), by separating models through the recording change (R 0.82 vs other models) while blind to knob moves (R 2.8, up to 40 on knob-heavy sets). By family the embedding holds on cab, EQ and distortion; every score fails on delay, reverb and dynamics; tremolo/vibrato is the one family where envelope is invariant. Consequence: render-and-compare against any dry that is not the target's own can choose the model and cannot set the knobs; knobs need the target's own (or a faithful) dry, or a predictor reading the target directly. Reproduces [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive"). Details: [di-invariance-check-2026-10-23.md](di-invariance-check-2026-10-23.md).

## Added the same day: the chain-training pilot ([T215](../../board/TASKS.md#t215 "Chain training pilot"), finished)

4,472 chains, heads trained on finished-chain audio, scored on the [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") 295 chains. Top-3 chain-trained full / single+chain: drive .18/.15 (chance .06; before .10), amp .04/.04 (chance .013; before .02), cab .48/.60 (before .40; .75 with nothing after the cab), mod .67/.70, delay .22/.33, reverb .68/.64 (chance .60), EQ .74/.74. Knobs at the constant-guess level everywhere; presence +.02–.10 over majority. Oracle: giving the amp head the true cab adds 0.00 top-3. Against the survey's scaling bar (drive and amp > 5× chance, oracle ≥ +0.1): not met (3.2×, 3.3×, 0). Reading: 224-way amp identity from chain clips does not look learnable at any affordable scale; the pilot supports a cab shortlist feeding render-and-rank, with the amp by search, a coarser target, or the preset-retrieval prior (route 2). Plan reorder proposed: route 2 and a cab-led search ahead of a large chain render. Details: [chain-train-pilot-t215.md](chain-train-pilot-t215.md).
