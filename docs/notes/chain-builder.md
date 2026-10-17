# Chain builder ([T105](../../board/TASKS.md#t105 "Chain builder (human 2026-10-17 \"build a chain from the analysis\")"))

Human direction 2026-10-17: model single blocks, build the chain from the analysis. Code:
`src/tonematch/search/chain_builder.py`. Tests: `tests/test_chain_builder.py` (fake library, engine,
loss and structures; no Native).

## Pipeline
1. **Presence.** `presence_fn(target, di) -> (probs, estimates)`; default wraps `metrics.presence.detect`
   (`probabilities()`, per-category `estimate`). Pass `probs=` to skip it.
2. **Structures.** `chain_prior.top_structures(probs, k, context=...)`; context (decade, style) is passed
   through. Order of the returned list is the prior order and is reported unchanged. Structures over 8
   blocks, with no models for a category, or refused by `validator(chain)` are dropped before any render.
3. **Per structure.** `StructureSearch.step(state, renders)`: a coordinate pass (70 %) then a joint refine
   (30 %). Pass 0 renders each slot's shortlist models at library starts and keeps the best, then CMA-ES on
   that slot's knobs with the others fixed. Later passes re-screen the two best earlier runners-up with the
   tuned knobs carried over by name, then refine again. Joint refine: CMA-ES on every knob, small step.
4. **Halving.** Rung sizes k, k/eta, ..., 1; each rung splits its share equally among survivors; 15 % is
   reserved for the winner's last joint refine. States are resumable, so survivors continue, not restart.
5. **Output.** `ChainResult` (chain, score, structure, every structure's score, renders and drop rung, mode
   label) and `assemble_preset(chain, library)` -> `Preset` for `write_hlx` (cab has no `@position`, amp gets
   `@cab`). `preset_validator` runs `preset.validate` (block limit, models, ranges, DSP costs).

## Rendering
`render_chain(engine, presets, dry)`: one single-block template per slot, each through `base.render_many`,
output shifted by 16 samples ([T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")) before it feeds the next. `ChainRenderer` batches many chains: blocks
are grouped by their input signal (prefix), each group is one `render_many`, every prefix is cached, so a
move on slot i costs the blocks from i on. Cost unit: block renders (a 3-block chain from scratch is 3).
Joint moves are the expensive ones (every block re-renders); coordinate moves on late slots are cheap.

## Scoring
Known DI: `KNOWN_DI_WEIGHTS` plus the Ruling 3 level guard (tolerance 0.25 dB, slope 0.08, target's own
active level). No DI: `ScoreMode.song_mode(weights)`, default provisional, reference level optional; result
label says "PROVISIONAL". Nothing is admitted here.

## What the real run needs
- **native-engine:** a library adapter implementing `BlockLibrary` (`preset(model, values)` from
  `native_library.block_preset` + values; `shortlist(category, n)` by fingerprint match then usage
  frequency; `free_params(model)` from `param_policy`); a pool `render_many` that accepts one dry signal and
  several single-block presets; a real `render_chain` that keeps intermediate audio in the host. Stereo
  blocks and dual cab are not serialisable ([T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")): exclude them from shortlists. Time-variant models out.
- **tone-metrics:** `presence.detect` with a fitted model (`probabilities()` is empty without one) and
  stable estimate keys for delay time, mod rate, reverb decay for `seed_fn`; calibrated probabilities,
  since `top_structures` multiplies them (p<0.05 is ignored, p>=1 is forced).
- **preset:** corpus plus context in `chain_prior` (already called with `context=`).

## Known limits
- Coordinate descent on coupled knobs (drive Gain vs Level) converges slowly; on the fake 3-block target
  3000 block renders reach score 0.17-0.26 from 0.8 (truth 0). Budgets in the thousands are needed.
- Model screening at library defaults is noisy; the re-screen with carried knobs helps but is not proof.
- Measured parameters (delay time) are searched in a +-2 % band; a wrong estimate is not corrected.
- Cab IR choice is just another shortlist slot; amp+cab interplay is only seen via the joint refine.
