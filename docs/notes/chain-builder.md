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

## On real Native ([T121](../../board/TASKS.md#t121), `src/tonematch/engines/native_chain.py`)
`build_chain_native(target, dry, pool, kit, ...)` runs presence (unless `probs=` is given), hands the
estimates to the library, uses `NativeChainRenderer` and (by default) `preset_validator(..., engine="native")`.
Pass a `NativeRenderPool` for parallel renders; a `NativeEngine` works too (one `render_many` per input).
`build_chain` gained one keyword for this, `renderer_factory` (default `ChainRenderer`, so nothing else changes).

- **Trim, read this first.** `NativeEngine.render` already trims Native's 16-sample pass-through latency
  (engine contract), which is exactly [T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")'s per-instance trim. `ChainRenderer`/`render_chain` here apply
  `trim=16` on top, so on a real engine every block would land 16 samples early (48 on a 3-block chain).
  `native_chain` applies `trim - engine_trim(engine)`: 0 on Native, 16 on the raw fakes in the tests.
  Use `NativeChainRenderer` or `native_chain.render_chain` with any contract engine.
- **`NativeBlockLibrary`** (the `BlockLibrary`): `preset` is `block_preset` + values, CC-driven values
  snapped to the 128-step grid (`snap=False` keeps them). `free_params`: CC-verified continuous params on the
  template's CC range, start at the library value, minus frozen params, `CC_UNRELIABLE` and
  `exclude_params` (default `FIXED_PARAMS` = match_song's never-freed set, Level included; `exclude_params=()`
  frees Level). `shortlist`: fingerprint match then corpus usage (`model_usage.json`, local). The match is a
  bucket (within a quarter octave / within an octave or not comparable / further) for delay time, reverb RT60
  and mod sub-type + rate against the fingerprint settings' span; gain, EQ, cab and comp rank by usage only
  (no calibrated estimate-to-fingerprint mapping yet, tone-metrics). Out: no template, time-variant,
  unverifiable (incl. FX Loop / Send / Return), stereo-only (catalog `mono` false: 27, incl. the legacy
  Plate/Room/Hall/Spring reverbs and Ping Pong, Dual and Multitap delays), `...WithPan` twins, IR blocks
  (`allow_ir=True` keeps them). `excluded` gives the reason per model. Library v1.2 after exclusions: drive 61,
  amp 111, preamp 113, cab 89, comp 19, eq 8, mod 50, delay 40, reverb 13, pitch 29, filter 15, wah 11.
- **Rendering.** Same prefix cache, keys and budget as `ChainRenderer`; at each depth every pending block,
  whatever its input, goes out in one engine call (pool: one `render_batch`, jobs carry their input and are
  grouped by template; engine: one `render_many` per distinct input). Intermediate audio stays in this
  process (the cache), not inside the host: each block is another template (a state load per block either
  way), and a host-side chain would skip `NativeEngine.render`'s sentinel count, NaN/silence checks and memory
  watchdog. NativeEngine itself is unchanged.
- **Live check** (`scripts/chain_render_check.py`, 1 host, refuses when running hosts + 1 > 6; ledger
  [2026-10-17T20:52:21](../../ledger/runs.csv?plain=1#L895 "smoke · native · chain-render-v1 · blockserial_hp20_null_db = -67.04 (first of 5 rows at this time)") (5 rows), `smoke` / `chain-render-v1`, 5 rows). [T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…") method and bar (-60 dB above 20 Hz):
  C8p3 (comp, drive, amp) -67.0 dB, C8p4 (+ cab) -67.3 dB, lag 0 on both (the trim is counted once), against
  [T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")'s whole-chain captures; prefix-cached vs plain `render_chain` -331/-337 dB. A 4-block chain with no
  whole-chain template (Essex A15, 1x12 Blue Bell, 70s Chorus, Ganymede at library starts) repeats at -333 dB;
  0.47 s per block render on a 12 s clip. Library templates vs [T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…")'s exact-value captures: -29.9 dB on C8p4,
  all from the CC grid: the plugin plays the grid value (value as given vs snapped -323 to -337 dB per
  block), and each block's grid step costs -33 (comp: Level 5.4 -> 5.2 dB) to -49 dB (cab). So a chain the
  search finds is rendered at its snapped values, and `assemble_preset` writes those, which the Stomp plays.
- **Still open:** a whole-chain capture at grid values with a time-based block (mod, delay, reverb) for a
  library-template equivalence check; the [T045](../../board/TASKS.md#t045 "Spike: block-serial rendering — one block per Native instance chained…") captures cover comp/drive/amp/cab only.

## What the real run still needs
- **native-engine:** done in [T121](../../board/TASKS.md#t121 "Gate 3 groundwork: wire the chain builder") (above).
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
