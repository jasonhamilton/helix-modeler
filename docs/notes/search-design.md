# Search design (T023)

Code: `src/tonematch/search/` (`cmaes.py`, `space.py`, `loop.py`, `report.py`, `types.py`, `stubs.py`). Tests: `tests/test_search.py`. Smoke demo: `scripts/search_smoke.py`.

## What is built

- **CMA-ES**: own numpy implementation (`cmaes.py`), box [0,1]^n, ask/tell, rank-only. The `cma` package (BSD-3) was not installed and was not needed; swap it in behind the same ask/tell if wanted.
- **Stage-wise refinement** (`refine_chain`): gain/EQ group (comp, drive, amp, cab, eq) first, then time-based (mod, delay, reverb); the render budget is split by dimension count.
- **Successive halving** (Stomp): each generation scores the whole population on the first `short_s` (1 s) of the excerpt, then the top 1/eta (eta=3) on the full clip. CMA gets ranks only: promoted candidates ranked by full score, the rest after them by short score.
- **Chain grouping** (`match`): candidates are de-duplicated by chain key `(category, model_id)` per slot; `engine.load` runs once per chain per phase (a short beam phase over all chains, then the refine phase on the best `beam`). A chain that raises `UnsupportedModelChange` is skipped and noted in the report. Loads are never per trial.
- **Prior**: `fit_prior(presets)` fits an independent Gaussian per (slot, param); MAP penalty is `weight * mean z^2` added to the loss. Off unless passed.
- **Step grid**: `capabilities().extras['param_steps']` (int, or dict per ParamRef) snaps every proposal to the grid before `set_params`. Absent means continuous (Native's extras do not carry it yet).
- **Validation**: a `validator(preset) -> violations` hook runs on every candidate before load and on every returned preset; default is `tonematch.preset.validate` against the cached catalog, and the report says "unvalidated" when there is no catalog.
- **Final check**: top `top_k_final` re-scored on the full clip with `stems`/`mask` passed to the loss kit. The hardware check through `benchmark` is not built here.
- **Not built**: predictor and retrieval seeding (`match` takes a candidate list), `Score.measured` seeding, Bayesian optimization, structure search.

## Budgets from `cost()`

Budget counts **full-render equivalents** (a 1 s Stomp render in halving counts 0.5). `plan_budget` caps by `Budget.max_renders` and, if given, `wall_s * parallelism / seconds_per_render`.

| Engine | cost() (measured) | Render model | Strategy | Budget |
|---|---|---|---|---|
| Native | 1.63 s/render for a 2 s clip, parallelism 6, load_penalty 0.016 s (empty-preset lower bound, `cache/native/cost.json`) | Reset render costs about the same for any clip length, so no short rung | Full-clip CMA-ES, population rounded up to a multiple of `parallelism` | 1000 renders = 1630 s sequential, about 5 min if six lanes run (needs `render_many`, deferred; the loop renders one at a time today, so budget by sequential time: 600 s is about 370 renders) |
| Stomp | 2.68 s/render, parallelism 1, load_penalty not measured (NaN) | Real time, so cost scales with clip length | Successive halving, 1 s rung then 2 s | 250 equivalents = about 11 min; 300 = about 13.5 min (inside the plan's 10 to 25 min) |

`load_penalty_s = NaN`: `plan_budget` assumes one render time per model change, sets `load_penalty_assumed`, and writes a note into the report.

Native's `set_params` only reaches knobs the captured template maps (today `known.hlx` maps `0:Drive` to `knob_01`); a free parameter without a map entry makes the render raise `EngineError`, which the evaluator scores as a large penalty. Pass `free=` to restrict the search to mapped parameters.

## Proof on the fake engine

`tests/test_search.py` builds a numpy drive/tone/level (and delay) chain behind the Engine protocol with a spectral LTAS stub loss. Recovery from 0.5 start: Tone to 0.001, Level to 0.02 to 0.04, Gain to 0.03 to 0.06 (Gain is the weakest axis under an LTAS loss) within 300 renders (Native-style) and 250 equivalents (Stomp-style halving). Stub loss only; says nothing about real tone-metrics terms.

## Proposals to the lead (contracts untouched)

1. `Section` (song, name, target, dry, stems, mask) and `RankedPreset` are defined in `search/types.py`; name them in a contract if other agents consume them.
2. The loss-kit contract names `tonematch.metrics.LossKit`; search codes against a `score(target, candidate, *, stems, mask, weights)` protocol and does not import it.
3. Native would profit from `render_many` (parallelism 6 is idle) and from `param_steps` in `capabilities().extras`.

## Smoke demo (not a benchmark, no ledger row)

`scripts/search_smoke.py`, 2026-10-14, Native on the captured `known.hlx` chain, one free parameter (`0:Drive`, `knob_01`), spectral stub loss, 16-render budget. Target rendered at Drive 0.75; the search started from the file's 0.61 and found 0.749 (stub score 0.0105, 20 render calls). Wall time was 207 s including host start, so about 10 s per render call, far above the 1.63 s `cost()` figure. Each `set_params` followed by a render on this chain appears to cost more than the cached empty-preset measurement (cost() is a lower bound, see DECISIONS); budget Native by measured wall time on the real chain, not by `cost()` alone.

## Loss weights and clip length (lead ruling from T022)

- `DEFAULT_WEIGHTS` in the real kit are all 0, so a call with no `weights` gives a flat objective. `Evaluator` now passes `weights` explicitly: `Budget.weights="provisional"` resolves to `tonematch.metrics.PROVISIONAL_WEIGHTS`. Every `RankedPreset` and report carries `loss_label = "unadmitted loss"` until the caller sets `Budget.admitted=True` after `loss-trust-check` admits the terms. A test shows the real kit's objective is not constant over the fake (numpy proxy) chain.
- `Budget.clip_s` default is now 10 s (short rung 3 s); the actual length is `min(clip_s, section length)`, recorded in `RankedPreset.clip_s` and printed in the report.
- Render cost at 10 s: Stomp renders in real time, so 2.68 s per 2 s becomes about 13.4 s per full render (5x). The 250 to 300 equivalent budget of the table above is then about 55 to 67 min, over the 10 to 25 min plan; to stay in 25 min use about 110 full 10 s renders, so Stomp should search on shorter clips (clip_s 2 to 4) and use 10 s only for the final check. Native's 1.63 s was measured on a 2 s clip and is treated as mostly fixed reset cost (unverified at 10 s; measure before budgeting). The earlier budget table assumed 2 s clips.

## Serial rendering and Native clip cost (native-engine re-measurement)

- `match()` renders serially through one engine, so `plan_budget` now plans with `parallelism = 1` whatever `cost()` reports (a note records the unused lanes), `est_wall_s = renders * seconds_per_render`, and the population is no longer rounded up to the parallelism. Using Native's six lanes would need each generation rendered through `NativeRenderPool`; not built.
- Native reset cost is about 1.8 s fixed plus 0.045 s per second of audio (2 s clip about 1.9 s). `plan_budget` scales seconds per render that way, so a 10 s clip is about 1.2x a 2 s clip: searching on full 10 s sections is fine on Native (reset dominates). On the Stomp a 10 s render costs about 13.4 s. The Native "1000 renders = 27 min" figure above assumed the older 1.63 s, so use about 1.9 to 2.3 s per render serially.
