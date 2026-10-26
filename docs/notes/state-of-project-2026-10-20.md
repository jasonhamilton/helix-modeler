# State of the project, 2026-10-20 (lead)

Written for the human after the 2026-10-18 to 2026-10-20 distortion-first round. It is an assessment, not a decision: every proposal below needs the human's approval (CLAUDE.md).

## Summary

There is real progress, but it is narrow. The drive-identification pipeline works well inside Helix, and three of the human's listens were positive. What is still unproven is the case the product exists for: an arbitrary real pedal or record, without the right reference chain. The approach that emerged is: a trained predictor names the pedal, AFx-Rep ranks a short list, and a handful of renders confirm. It is sound and cheap. It does well when the target is something it knows, and fails, or cannot know, when the target is a chain it has not learned, a pedal outside Helix, or a recording with an unknown input level.

## What is working

| Evidence | Result | Strength | Source |
|---|---|---|---|
| Predictor-first matcher, Helix drive targets, blind | Model top-1 85%, top-3 100%; about 12 renders and 5 s per match | Strong (48 targets, McNemar p 1e-5) | [T196](predictor-first-t196.md) |
| Same, the human's own takes, blind | Model top-1 67%, top-3 88% | Moderate (24 targets) | [T196](../../board/TASKS.md#t196 "Predictor-first drive matcher option") |
| AFx-Rep final ranking vs the provisional score | Blind top-1 39% vs 10% (96 targets); the human agreed 18 vs 3 (v5) | Strong: two independent tests | [T185](nodi-drive-e2e-v2.md), [v5](human-ratings-v5.md) |
| More DI variety in training (150k renders) | New-player model top-1 33–47% → 62–75% | Strong | [T191](drive-dataset-v1.md) |
| Mother, drive only (predictor-first) | Human: "it sounded pretty good" | One section, informal | DECISIONS 2026-10-20 |
| DS-Sample, tutorial chain tuned by AFx-Rep (version c) | Beat the tutorial blind (4.0 vs 2.5, v6) and live ("C is better") | One section, 8 trials | [T198](ds-sample-tuning-t198.md) |
| Measured-pedal mode (simulation) | Model 96%, Drive 85%, Tone 90% | Simulation only | [T187](measured-pedal-mode.md) |
| Infrastructure | Stomp USB freeze fixed and soak-tested; per-chain Native template capture; matching cost down about 30×; busy-core CPU monitor | Solid | [T168](../../board/TASKS.md#t168 "TOP PRIORITY (human 2026-10-18 \"fix the stomp freezing issue"), [T192](../../board/TASKS.md#t192 "CPU demand = busy cores")–[T193](../../board/TASKS.md#t193 "Switch the remaining scripts' own os.getloadavg() guards to…") |

## What is not working or not proven

| Problem | Evidence | Why it matters |
|---|---|---|
| Knobs are unreliable | Drive within ±0.15 in about 50–70% of cases; on the human's takes Drive reads 0.15–0.20 high | The model may be right while the settings are wrong |
| Playing level is confused with Drive | [T194](drive-anomaly-t194.md); not separable without a dry track | A hard limit for blind matching unless input level is measured |
| Real songs need chains | DS-Sample single drive blocks "both quite far off"; the right answer was 808 > amp > cab > delay > EQ | The predictor only knows single drive blocks |
| Separated stems are flawed targets | The stem lost about 20 dB of bass, and its top end is partly hiss | Tuning chases separation artefacts; c to c2 gave no audible gain (v7: 2 of 2 ties) |
| Never tested on a real pedal | Every number above is Helix against Helix | Gate 4 is the honest test |
| Few players | Two Guitar-TECHS players plus one session of the human | The new-player results are the weakest |
| Small listening samples | 2–28 trials per session | Directional, not conclusive |
| Gates not formally run | Gate 2 unsigned; Gate 3 has no registered bars or sealed set | Results exist without a formal pass/fail |
| AFx-Rep licence | Training data includes non-commercial sources ([T190](afxrep-licence.md)); PANNs/DAC swap ([T195](../../board/TASKS.md#t195 "Permissive encoders PANNs and DAC vs AFx-Rep on a drive-dataset-v1…")) still running | Could block shipping |

## Proposed focus, ranked (needs the human's approval)

1. **Measured-pedal mode with the human's real distortion pedals (Gate 4).** One cable setup; 2–3 pedals at a few settings; judged by numbers and by ear. It is the only route that set knobs reliably in simulation, it avoids the level and separation problems, and it is distortion-only.
2. **Formalise Gate 3 with the current method.** The human approves the bars; a sealed blind set is built with held-back P3; the predictor-first matcher is run once.
3. **Fix the input-level confound.** Give the predictor the measured input level when a dry take exists. The level is already computed for all 150k clips, so this needs retraining only.
4. **More players.** More of the human's takes in different styles, and another licensed DI library if available.
5. **Then chains:** drive > amp > cab first. It widens the distortion-only focus, so it needs the human's approval.

**Proposed to stop:**
- new scoring methods;
- further ratings of easy same-performance pairs;
- further DS-Sample refinement.

## Corrections 2026-10-23 (status-reporter, from the methodology audit section 6)

The text above is kept as written on 2026-10-20. The following rows of it overstate their evidence (see [methodology-audit-2026-10-23.md](methodology-audit-2026-10-23.md) sections 1 and 6, and [project-analysis-2026-10-23.md](project-analysis-2026-10-23.md)). Evidence classes: A blind, sealed set, pre-registered bar; B blind, unsealed; C open listen by the human; D numbers only on an unadmitted metric or without a bar; E none.

| Row above | Said | Correction | Class |
|---|---|---|---|
| AFx-Rep final ranking vs the provisional score | "Strong: two independent tests" ([T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement"), v5) | One experiment heard, not two independent tests: v5 rated [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")'s own picks on [T185](../../board/TASKS.md#t185 "Larger no-DI drive e2e measurement")'s targets. The direction (18 vs 3) is confirmed on 21 decided same-performance trials; the 39% vs 10% top-1 is on unadmitted metrics, same engine and players. | D + B |
| Measured-pedal mode (simulation), listed under "What is working" | "Model 96%, Drive 85%, Tone 90%" | Built, simulation only, never run on a pedal. It does not belong under "working"; it is an unverified build. | D |
| Predictor-first matcher, blind | "Strong (48 targets, McNemar p 1e-5)" | Unsealed set drawn by `search` after the method was built; no bar; Helix against Helix; the human's own-take cells are not significant (p 0.13 and 1.0). | D |
| DS-Sample tuned chain (version c) | "Beat the tutorial blind (4.0 vs 2.5, v6)" | 8 trials, 6 decided, 1 of 2 repeats consistent, one song, one window; arm c's EQ sits at the ±12 dB limit on 8 of 10 bands; c2 improved the score and was heard as identical (v7). Directional only; it does not show that a whole chain can be tuned by ear-checked search. | B + C |
| Mother, drive only | "it sounded pretty good" | As stated: one section, open, one pass, no alternative heard. | C |
| Knobs unreliable (lower table) | "Drive within ±0.15 in about 50–70% of cases" | The earlier "15 of 17" figure did not hold at n = 96 (52%); the v2 note carries the correction, the v1 note does not. | D |
| Infrastructure, "matching cost down about 30×" | | True for Helix single-drive targets (144 matches); not shown on a song. | D |

General: no claim in this note rests on a blind test with a sealed set and a pre-registered bar. Gate 2, the one gate run that way, was judged "garbage" / unusable by the human ([DECISIONS 2026-10-18 model and method decisions](../../docs/DECISIONS.md#2026-10-18-human-recorded-by-lead-model-and-method-decisions-require-the-humans-approval-the-current-model-is-judged-unusable "2026-10-18 (human, recorded by lead): model and method decisions require the human's approval; the current model is judged unusable")) and is unsigned with [T152](../../board/TASKS.md#t152 "Gate 2 confirmation run on a fresh sealed synthetic-v4 with the…") (confirmation run) and [T155](../../board/TASKS.md#t155 "Test-validity checks") (harness sanity checks) open. The admitted known-DI score (mrstft_v2 + level_db) is unapproved and at chance on hard same-performance pairs (v3: 10 of 25). The human's test-retest consistency on repeated blind pairs is 7 of 12 (v2–v7), so the 2–28-trial sessions quoted here are directional, as the note's own "small listening samples" row says. Reflected in `reports/status/` the same day.
