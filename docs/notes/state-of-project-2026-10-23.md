# State of the project, 2026-10-23 (lead)

Written for the human after the 2026-10-21 to 2026-10-23 dry-track round. It is an assessment, not a decision: every proposal below needs the human's approval (CLAUDE.md). Previous assessment: [2026-10-20](state-of-project-2026-10-20.md).

## Summary

The project moved from "can we name the pedal" to "can we match a whole song with a rebuilt dry guitar". The answer is: partly, and by ear it is better than before. The matcher no longer needs the human's dry takes: it rebuilds a dry guitar from the separated stem (Basic Pitch notes played back with real Guitar-TECHS notes, or with the human's own calibrated notes), searches all 60 drive models against it, and fine-tunes the knobs. On seven songs the human said "those actually all sound pretty good" and the fine-tuned versions "sound either the same or better". Overnight, training data for the other six slots (amp, cab, mod, delay, reverb, EQ) was rendered and trained. The heads are good on players they have seen and weak on a new player, the same pattern drive had before 150,000 renders fixed it. The biggest gap is unchanged in kind: every song match is one drive block standing in for a whole rig, and the slot heads have not yet been tested inside a chain ([T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") is running).

## What is working

| Evidence | Result | Strength | Source |
|---|---|---|---|
| Dry-track rebuild from Basic Pitch notes, all seven separated guitar stems | Runs on every song; alignment to the stem varies from 0.46 (Teen Spirit) to about 0 or below on three fast, dense riffs (the metric is blind there) | Weak as a measurement; the listening is the test | [T203](basic-pitch-t203.md), [T201](rebuilt-dry-t201.md) |
| Full DI-based drive search on the rebuilt dry, seven songs | Human, left/right against the separated guitar, LUFS-matched: "those actually all sound pretty good" | Informal, open listening, not blind | DECISIONS 2026-10-22 ([T207](../../board/TASKS.md#t207 "All seven songs (human 2026-10-22 \"render the dry tracks for all of…")) |
| Knob fine-tuning (model fixed) | AFx-Rep distance fell on 6 of 7 songs; human: "either the same or better" | Informal; distance and ear agree in direction | [T209](../../board/TASKS.md#t209 "Knob fine-tuning on the seven T207 picks") |
| Human's calibration profile (prs-7-tone) | 263 own notes, tone curve, normal loud level -36.1 dBFS against the -28.1 dBFS play-along level | A real measurement of one guitar | [T204](guitar-profile-t204.md) |
| Calibrated re-run on seven songs | Distance lower on 5 of 7 (clearly, by more than 0.04, on 3: Mother, Territorial, Teen Spirit); marginal on Seek & Destroy and Satisfaction; higher on DS-Sample and Cherub Rock | One run per song, no ear verdict yet | [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar"); ledger song-*-prs7 |
| Slot datasets for amp, cab, mod, delay, reverb, EQ | 240,541 single-block renders (amp includes the refill); model top-1 on held-out files of seen players (full size, MLP): amp 0.54 (224 models), cab 0.76, delay 0.56, mod 0.75, reverb 0.47, EQ 0.82 | Strong within player (thousands of test clips) | [results](slot-datasets-v1-results.md) |
| Amp silence bug | Cause found (a -90 dBFS render floor), 5,526 of 5,653 failed amp clips refilled at -240 dBFS; 127 digital-zero clips left out | Verified by repeat renders, details in the note | [slot-datasets-v1](slot-datasets-v1.md), [T212](../../board/TASKS.md#t212 "Diagnose the slot-dataset-v1 amp silence failures") |
| Earlier results still standing | Predictor-first drive matcher ([T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option")), AFx-Rep ranking ([T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")), DS-Sample chain tuning ([T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain")) | As in the 2026-10-20 note | [2026-10-20](state-of-project-2026-10-20.md) |

## What is not working or not proven

| Problem | Evidence | Why it matters |
|---|---|---|
| One drive block stands in for a whole rig | [T200](../../board/TASKS.md#t200 "Blind single-drive matches of five songs the human added to data/songs") blind verdicts: two "pretty off", one closest, two "somewhat close"; DS-Sample's true chain has an amp, cab and delay | The product matches songs, and songs are chains. Seven "pretty good" listens are drive-only |
| New-player generalisation of the slot heads is weak | Model top-1 on a player not in training (full, MLP): amp 0.19-0.31, cab 0.32-0.35, delay 0.25-0.32, mod 0.47-0.60, reverb 0.31-0.39, EQ 0.49-0.67 | The human is a new player. Drive had the same gap until [T191](../../board/TASKS.md#t191 "Unattended overnight drive dataset render") |
| Chain transfer unknown | The heads were trained on one block alone; [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") (about 300 chains) is running | A head that fails inside a chain cannot drive a chain matcher |
| Knobs are modest | Within +-0.15 of the true position for the true model: amp 0.45, cab 0.64, delay 0.43, mod 0.63; the lead reads a constant mid-range guess at about 0.3 (not re-measured here) | The model may be right with the wrong settings |
| Level is an open question | Calibrated level -36.1 dBFS against the -28.1 dBFS play-along level, 8 dB apart; the [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") run changed notes, tone and level together | The improvement in [T211](../../board/TASKS.md#t211 "All seven songs with the calibrated guitar") cannot be attributed to one change, and Drive reads level |
| Chord list helped decisiveness but not the sound | Mother with the six-chord list: runner-up margin 0.023 against 0.002 plain, but distance 0.706 against 0.629 (lower is closer) and the human preferred the plain version | A user-supplied chord list is optional, not a clear gain |
| Output and Master moved freely in the fine-tune | The distance has no level term; best.hlx carries what was found | Level can drift, so the presets need the Output/Master lock before use on stage |
| Never tested on a real pedal; no player but the human and two library players | As on 2026-10-20 | Gate 4 is still the honest test |
| Gates not formally run | Gate 2 unsigned; Gate 3 has no registered bars or sealed set | Results are measurements, not pass or fail |
| AFx-Rep licence | Training data includes non-commercial sources ([T190](afxrep-licence.md)) | Could block shipping |

## Proposed focus, ranked (needs the human's approval)

1. **Read [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")"), then decide the chain route.** If the heads hold up inside chains, a drive > amp > cab matcher on the fixed slot order is the next build. If they do not, train on chain outputs instead of single blocks.
2. **More players for the slot heads.** The cheapest fix for the new-player gap is what worked for drive: more DI variety (the human's calibrated notes, another licensed DI library).
3. **Level decision.** Choose one input level convention for the rebuilt dry (-36.1 dBFS calibrated, or -28.1 dBFS play-along), then run the seven songs once more with only that changed.
4. **Fine-tune as a default step, with Output and Master locked** (so loudness is set by LUFS, not by the search).
5. **Gate 4 with the human's real pedals** stays the first honest external test; ranking is unchanged from 2026-10-20.

**Proposed to stop:**
- chord-list rebuilds as a pipeline requirement (keep the option);
- more rebuild-alignment tuning (the metric is blind on dense riffs; the ear is the test);
- any new scoring method.

## For the human (decisions and actions)

- Level: -36.1 or -28.1 dBFS as the rebuild level.
- Fine-tune as the default matcher step, Output and Master locked: yes or no.
- A blind check of the seven (fine-tuned) matches, since the listening so far was open.
- Load the seven presets on the Stomp and play them (the human said they want to test them).
- Chord lists for other songs: optional.

Nothing in this note is adopted. Strengths are as stated in the sources; an informal open listen is not a blind test.

## Corrections 2026-10-23 (status-reporter, from the methodology audit section 6)

The text above is kept as written earlier the same day. These rows overstate or mislabel their evidence (see [methodology-audit-2026-10-23.md](methodology-audit-2026-10-23.md) sections 1, 2 and 6, and [project-analysis-2026-10-23.md](project-analysis-2026-10-23.md)). Evidence classes: A blind, sealed set, pre-registered bar; B blind, unsealed; C open listen by the human; D numbers only on an unadmitted metric or without a bar; E none.

| Row above | Said | Correction | Class |
|---|---|---|---|
| Slot datasets, "Strength" | "Strong within player (thousands of test clips)" | 18 held-out DI files from two players; the clips are cut from those files and are not independent. Effective n is 18. Trained, not verified: [T214](../../board/TASKS.md#t214 "Chain transfer test (human 2026-10-23 \"yes\")") (run after this note) shows the heads do not name their block inside a chain (MLP top-1 chain vs single: drive 0.02 vs 0.93, amp 0.01 vs 0.54, cab 0.25 vs 0.76, mod 0.21 vs 0.75, delay 0.10 vs 0.56, reverb 0.28 vs 0.47, EQ 0.12 vs 0.82; ledger [2026-10-23T15:49:38](../../ledger/runs.csv?plain=1#L2282 "benchmark · native · chain-transfer-v1 · drive_mlp_model_top3 = 0.0971 (first of 4 rows at this time)") (4 rows), [chain-transfer-t214.md](chain-transfer-t214.md)). | D |
| Calibrated re-run, "Distance lower on 5 of 7" | | Label: **AFx distance across different dry tracks; no ear verdict.** [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive") and [T041](../../board/TASKS.md#t041 "End-to-end on a real song") show the measure cannot order runs whose dry track differs; notes, tone and level changed together. | D |
| Knob fine-tuning, "distance fell on 6 of 7; same or better" | "distance and ear agree in direction" | The distance is an AFx-Rep distance used past its validation (same-performance model ordering only; invariance ratio 30 for small knob moves, [T175](../../board/TASKS.md#t175 "Different-guitar test of embed_afxrep, ltas_eqfit, ltas_tilt on drive")); the ear verdict is one open pass with Output/Master free. Contrary to the 2026-10-18 ruling that knobs are not set by a sound-comparison score; a test setting, not an approved method. | C + D |
| Full drive search on seven songs, "all sound pretty good" | "Informal, open listening, not blind" (as stated) | One listener, one pass, no alternative candidate heard. | C |
| Chord list, "distance 0.706 against 0.629" | | Label: **AFx distance across different dry tracks; no ear verdict** beyond the human's one open preference for the plain version. | C |
| Knobs are modest, "a constant mid-range guess at about 0.3 (not re-measured here)" | | Correct as written: **not re-measured**. [DECISIONS 2026-10-23 overnight slot training read-out](../../docs/DECISIONS.md#2026-10-23-lead-t213-overnight-slot-training-read-out-measurement-nothing-admitted "2026-10-23 (lead): T213 overnight slot training, read-out (measurement; nothing admitted)") and chain-transfer-t214.md repeat the 0.3 as fact; it is unmeasured. | E |
| "Earlier results still standing" ([T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option"), [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement"), [T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain")) | "As in the 2026-10-20 note" | See the corrections appended to [state-of-project-2026-10-20.md](state-of-project-2026-10-20.md): [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") is class D (unsealed, no bar); [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement") + v5 is one experiment heard; [T198](../../board/TASKS.md#t198 "Tune the DS-Sample reference chain") is 6 decided trials, 1 of 2 repeats consistent. | D / B |
| Gates, "Gate 2 unsigned" | | Also: the human judged the Gate 2 model "garbage" / unusable on 2026-10-18 and declared every lead-made method ruling behind it unapproved; [T152](../../board/TASKS.md#t152 "Gate 2 confirmation run on a fresh sealed synthetic-v4 with the…") (confirmation run) and [T155](../../board/TASKS.md#t155 "Test-validity checks") (harness sanity checks) are open. "3 of 3 evidenced, no check failing" describes the checklist, not the gate. | A on paper, disowned |
| Measured-pedal mode (in the 10-20 ranking, item 5 here) | | Built, simulation only, never run on a pedal. | D |
| [T206](../../board/TASKS.md#t206 "Re-run predictor-first drive match on Mother") (board): "unadmitted loss 9.53 vs 16.00 on the take" | | A provisional-loss comparison across different DIs, not a fidelity comparison. | D |

General: no result since 2026-10-14 rests on a blind test with a sealed set and a pre-registered bar; the human's test-retest consistency on repeated blind pairs is 7 of 12 (v2–v7). AFx-Rep final ranking (approved for shortlisting only) and fine-tuning with Output/Master free became `song_match.py` defaults by reuse without an itemised approval; until the human rules on the ten decisions in project-analysis-2026-10-23.md they are test settings. Reflected in `reports/status/` the same day.
