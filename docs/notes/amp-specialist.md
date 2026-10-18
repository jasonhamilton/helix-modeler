# Amp specialist ([T120](../../board/TASKS.md#t120 "Amp specialist"))

Owner: search. Code: `src/tonematch/search/amp_specialist.py`; plugged into the dispatch of [family-specialists.md](family-specialists.md)
(`--specialists gain`). Tests: `tests/test_amp_specialist.py` (fakes). Study script: `scripts/amp_refine_study.py`.
Human decision 2026-10-17: a specialist per effect family. Amps were the third family on the lead's list (modulation, time-based, amp).

**Status: tuned on the dev family set (`synthetic-devfam`), same-budget before/after below. Not gate evidence, not adopted.** Adoption
needs the paired comparison on a fresh set (the lead's process note, DECISIONS 2026-10-17); `synthetic-v2` stays sealed.

## What was wrong (diagnosis, before any design)

1. **Gate 2 attempt 1, synthetic-v1 amps (diagnosis only, that set is seen).** 6 of 12 amp items right (random walk 8 of 12). In the broad
   stage the true amp ranked 6, 35, 9, 8, 1 and 3 on the six misses; each pool model had two or three draws in a 6 to 9 knob space.
   Two of the misses were the full-size amp Woody Blue taken for a drive pedal (presence said drive 0.9999).
2. **Devfam (the tuning set), general hybrid, 20 of 24 amp items.** The model is mostly found (17 of 20; the true amp led the broad stage
   by a wide margin on 11 of 12 of the first items) but the settings are not: parameter error 0.224, held-out loss median 0.103 and
   up to 0.43. So on this set the weakness is **refinement**, not model choice.
3. **Why the settings are poor** (true model given, `scripts/amp_refine_study.py`, devfam amps, 8 items, 150 renders):

   | refine | held-out loss mean | parameter error |
   |---|---|---|
   | CMA-ES from the best of 40 draws (what the halving does, with 1/3 of the budget) | 0.065 | 0.206 |
   | **Gaussian-process expected improvement** after the same 40 draws | **0.046** | **0.192** |
   | split: gain knobs, then tone stack, then all (idea 4; 3 items only, stopped) | 0.099 vs 0.064 for CMA-ES on the same 3 | - |

   And the general halving gives the right amp only about a third of the refine budget, because it refines four models.

## What was tried and dropped

- **Amp fingerprints (idea 1).** 33 numbers per render against the DI (tone-stack curve per third octave, how the transfer moves with
   input level per band group, overall gain, noise-floor lift), a Gaussian-process surrogate per model fitted offline on 250 renders of
   each of the six devfam amps (a calibration clip that no benchmark item uses: P3_music `directinput_04`, 513 s of Native), inverted for
   the target. Surrogate fidelity was fine (cross-validated error 0.05 to 0.2 of the pooled spread) but it did not carry to another clip:
   on the first item the distance to the true amp was 1.86 against 1.75 for a wrong one, and a per-item surrogate fitted on the item's own
   DI inverted to settings whose guarded loss was 0.22 to 1.3, worse than the best of the 40 draws on two of three items (the features
   do not pin the knobs that the loss does). Dropped; the module and its calibration file are gone.
- **Tone stack apart from gain (idea 4).** Gain knobs first, then Bass/Mid/Treble, then all together: no better than CMA-ES (above).
- **A gain-family score (idea 5).** Not tried: the refine study shows the existing score finds the right region once the search is
  good (held-out 0.01 to 0.05 on most items), so no trust bar was registered.
- **Within-family broad screen (idea 2).** On synthetic-devfam the true amp led the 6-amp pool at 3 level-free draws in 3 of 4 items
  (40-draw landscape over 4 items), so the broad stage is not the bottleneck there. It may be on synthetic-v1, where the pool is wider
  in kind (amps and full amps); the dispatch's probability-weighted draws already move draws to the family.

## Design

```
presence (drive, amp) --> category probabilities --> broad draws shared by the dispatch (family-specialists.md)
broad leader an amp model? --no--> general halving (the hybrid, unchanged)
                          --yes--> finalists: the leader + any amp model within 1.3x of its score (at most 2)
                                   one finalist : 30 space-filling points (+ its best broad draw), then expected improvement
                                   two finalists: 22 points each, the better one continues on the same data
```

- `seed(target, dry)` returns the drive and amp presence probabilities as `category_probs` (no knob seeds: see the fingerprint result).
  `screen_hint` is 1 - (1 - P(drive))(1 - P(amp)). `score` is None (the general score). `budget_share` is 1.0, because the draws follow
  the probabilities.
- `refiner(target, dry)` is the one hook added to the dispatch: `Dispatch.refiner` (specialists.py) and a call in
  `choose_models_hybrid` (hybrid.py). It receives the broad scores and the remaining renders and returns `(spent, notes)`, or None to
  decline (leader outside the family), in which case the general halving runs. Off unless a specialist supplies one; the neutral
  dispatch is unchanged and its test (`test_neutral_hybrid_equals_the_general_hybrid_draw_for_draw`) passes.
- Expected improvement: Matern-5/2 with a length scale per knob on log(loss + 0.02), 2,500 uniform and 1,500 local candidates per step,
  hyper-parameters re-optimised every fourth step.
- Spends the full remainder of the budget (no overshoot like CMA-ES, so `refine_scale` is not applied to it). The run JSON of each
  item carries the dispatch block; the stage renders are 120 broad + 180 refine, as for the general method.

## Result: devfam amps, general hybrid against hybrid + amp specialist, same budget (300 renders per item)

Admitted known-DI weights, level-free broad scoring, f 0.4, K 4. 20 of the 24 amp items: `i071`, `i079`, `i087` and `i095` cannot be
scored by the harness (Native returns NaN or inf on the held-out render of the true preset after the search; the true preset renders
fine in a fresh process; reported to the lead, it hits the general method too).

| | model | category | parameter error (right-model items) | held-out loss median | held-out loss mean | renders per item |
|---|---|---|---|---|---|---|
| general hybrid | 17/20 | 20/20 | 0.224 (17 items) | 0.103 | 0.150 | 320 |
| + amp specialist | 18/20 | 19/20 | **0.120** (18 items) | **0.043** | **0.053** | 333 |

Paired over items: held-out loss lower on 18 of 20 (mean difference -0.097, bootstrap 95 % interval -0.145 to -0.056); parameter error
lower on all 16 items both got right (mean -0.116, interval -0.153 to -0.081). The renders column counts every `engine.render` call
of the item (the search stages are 300 for both; the other 20 to 33 per item are outside the stages: target, held-out and engine checks. The specialist run counts 13 more on average; not traced further).

Misses of the specialist: `i067` (Grammatico Brt taken for US Deluxe Nrm, also missed by the general method) and `i091` (Grammatico Brt
lost to Tremolo, a near tie in the broad stage where presence gave amp only 0.40; the general method won it). Gained: `i031` and `i063`.

Caveats. This is the set the design was tuned on (the study, the lead ratio and the start size were chosen by looking at it; no
parameter was swept beyond the three listed), so the numbers are dev evidence. Model accuracy moved by one item and category accuracy
by one in the other direction, which is inside noise at n = 20; the large effect is on the settings. The four unscorable items are listed above, not hidden.

## Run it

```
python scripts/synthetic_v1.py run --set synthetic-devfam --hybrid --hybrid-f 0.4 --hybrid-k 4 --hybrid-scale 0.9 \
   --hybrid-level-free --budget 300 --weights data/weights/gate2-known-di-mrstft_v2-2.0.json --no-rank1 \
   --specialists gain --ids <amp item ids> --tag <run>
```

Ledger rows: kind `search`, dataset `synthetic-devfam`, metrics `*_amp20_general_hybrid` and `*_amp20_amp_specialist`
(model, category, parameter error, held-out loss median and mean), and `*_refine_study_{cma,bo}`. The refine-study rows came from a
scratch copy of `scripts/amp_refine_study.py` (the script was added after the rows).

## Open

- Drive pedals and full-size amps (Woody Blue): the gain family's other half is still the general method; the v1 failures there are
  presence and broad-stage failures (a drive-like target), not refine failures, and need a different fix.
- Near ties between an amp and a non-amp leader (`i091`): the specialist declines and the general halving runs. A mixed rung that keeps one
  amp finalist next to the other families' would cover it.
- The Native NaN on four devfam held-out renders (benchmark / native-engine).
