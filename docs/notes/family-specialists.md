# Family specialists and the dispatch ([T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist"))

Owner: search. Code: `src/tonematch/search/specialists.py` (registry, `Dispatch`, stubs), `src/tonematch/search/hybrid.py` (the hook),
`scripts/synthetic_v1.py --specialists` (the switch). Human decision 2026-10-17: a specialist per effect family, not one model for all.

## Families

| family | categories | specialist | owner | module the dispatch looks for |
|---|---|---|---|---|
| gain | drive, amp | amp (drive: general) | search ([T120](../../board/TASKS.md#t120 "Amp specialist (human 2026-10-17 \"a specialist per effect family\"")) | `tonematch.search.amp_specialist` |
| linear | cab, eq | linear | tone-metrics ([T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ")) | `tonematch.metrics.linear_specialist` |
| dynamics | comp | none yet | | |
| time | delay, reverb | time | search ([T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist")) | `tonematch.search.time_specialist` |
| mod | mod | modulation | tone-metrics ([T116](../../board/TASKS.md#t116 "Modulation specialist")) | `tonematch.metrics.mod_specialist` |

`FAMILIES` / `FAMILY_OF` in `specialists.py` are the one place the mapping lives. `MODULES` names the module each family's specialist is
looked up in (`load_specialist`): a module exposes `make_specialist()` or `SPECIALIST`; if it is missing or does not import, a
`StubSpecialist` stands in (no opinion, no seeds, general score), so the dispatch runs now and picks the real one up when it lands.

## The interface (what a specialist object has)

```python
class Specialist:
    family: str                       # a key of FAMILIES
    categories: tuple[str, ...]       # FAMILIES[family]
    budget_share: float               # relative weight of the family's models in the broad stage (1.0 = no change)
    def screen_hint(self, target, dry) -> float          # probability the target belongs to the family; NaN = no opinion
    def seed(self, target, dry) -> FamilySeed            # what the family's own measurements say about this target
    def score(self, target, dry) -> Callable | None      # wrap(kit) -> kit  (a family score), or None = the general score
```

`FamilySeed` (in `specialists.py`):

- `measured`: a `measured_seed.Measured` (free-form quantities, for the report);
- `category_probs`: `{category: probability}` for the categories the specialist can tell apart (may be empty);
- `for_model(model, category, ranges) -> {knob: measured_seed.Seed}`: the knob estimates of one pool model of the family. `ranges` maps
  each free knob of the model to its (lo, hi). A `Seed(value, half_width, quantity, measured)` replaces the generic measured seed of
  that knob: the anchor render of the broad stage is placed at `value`, and the knob's space is banded to `value +- half_width * range`
  for the broad draws and the refinement. A knob without a seed stays free over its whole range. Return `{}` to leave a model unseeded;
- `notes`, `detail`: strings and numbers for the report.

The score: `score(target, dry)` returns a function `wrap(kit)`. The hybrid calls it on the item's kit (and on the level-free broad kit)
and uses the wrapped kits for the whole item, so every candidate (the specialist's family and the others) is compared on one scale. A
family score is therefore the general score plus a family term; it must expose `.inner` (the wrapped kit) and return an object with
`.total`, `.terms`, `.measured`. It is used when the specialist's `screen_hint` is at least `SCORE_HINT` (0.5) or NaN. **A family score
that is not yet admitted by a registered trust bar must not be returned by default.**

What `build_dispatch(families, target, dry, models, cal=, ranges=)` does with them, per item:

1. calls `screen_hint` and `seed` of every enabled family (a specialist that raises is no opinion, noted in the report);
2. broad-stage draws: each model's share is its category's probability (the specialist's own `category_probs`, else the family hint
   split evenly over the family's categories in the pool, else what the specialists left over split evenly over the categories nobody
   speaks for), divided over the category's models, never below `FLOOR` (0.25) of an even split, and at least `MIN_DRAWS` (3) draws.
   A wrong hint cannot silence a model;
3. seeds: a specialist's `for_model` seeds replace the generic measured seeds of that model;
4. the family score, as above.

With no family enabled (`--specialists` absent) the dispatch is the neutral one and the hybrid is the frozen general method, draw for draw
(`tests/test_specialists.py::test_neutral_hybrid_equals_the_general_hybrid_draw_for_draw`).

`scripts/synthetic_v1.py run --hybrid ... --specialists time mod` turns families on (`all` for every family; a family without a module
is a stub). The run JSON of each item carries `dispatch` (hints, category probabilities, which family score, the specialist's notes).

## Notes for the other specialists (tone-metrics: linear, modulation; search: amp)

The three modules written so far (`metrics/linear_specialist.py`, `metrics/mod_specialist.py`, `search/amp_specialist.py`) each have a
`seed(...)`, `score(...)`, `screen_hint(...)` of their own shape. To plug in unchanged, add a small adapter object exposing the four
members above (`make_specialist()` at module level returns it); nothing else in the dispatch needs to change. If a specialist spends
renders of its own (probe renders), say so in the report: the dispatch does not count them against the budget.

## The time specialist (delay, reverb)

Code: `src/tonematch/search/time_specialist.py`, `time_features.py`; library preparation `scripts/time_specialist_fit.py`.

**Telling the three apart.** The block's impulse response h comes from deconvolving the target by its DI (known-DI mode). In h:
a delay is a few discrete peaks (or, for a tape or modulated echo, a sharp front followed by a plateau) with little between them
(`conc`, the share of late energy in 10 ms blocks standing 8 dB over their own 150 ms neighbourhood, is high; `line_resid_db`, the scatter
about a straight line in dB, is large); a reverb is a smooth decay whose echo density rises (`conc` low, a straight line); a modulation
leaves the linear part unexplained (`lin_resid_db` of the presence module is high) and has no late energy of its own. Two logistic
heads on these and the presence features (`data/time_specialist/classifier.json`): a four-way one (delay, reverb, mod, other) and a
family one (delay or reverb against everything else), which decides `screen_hint` (a modulated echo is a delay to it and a mod to the
four-way head). The four-way head splits the family into delay and reverb. A hand rule on the same numbers is the fallback. Training
data: the helix-trust-v2 train split with its presence features (single blocks on 96 different DIs), 20 renders per delay, reverb and mod
model on the three calibration DIs, and the tuning half of the dev set's renders (DI-diverse; never its held-out half). Grouped
cross-validation (whole models, whole items held out): family head 0.96 recall and 0.96 rejection; four-way 0.88.

**Seeds.** For every delay and reverb model, a Gaussian-process regressor (radial plus linear kernel, forward-selected features) from the probe
features of a render to each knob (normalised 0 to 1). Probe features: the first echo time from the rising edge of the late energy
(`on1`; it is right on 54 to 58 of 60 draws for the BBD, digital and tube echoes, where the strongest peak is much less often, because a smeared echo has no
peak), the echo spacing, echo levels and tone, decay times and lengths per band from the forward energy envelope, pre-delay, and the presence
module's echo and RT60 estimates. It is fitted on 60 renders of that model with ALL knobs varied (Latin hypercube, `scripts/time_specialist_fit.py`) on three
calibration DIs that no benchmark set uses. A knob is seeded only if its leave-one-out error beats 80 % of its spread; the band is twice that
error (4 % to 35 % of the range). Result on the devfam delay and reverb pool (error in fraction of the knob range): Time 0.03 (Analog),
0.07 (Dig with Mod), 0.10 (Tube echo), 0.16 (Bubble echo), none for Swell and Poly Sustain; reverb Decay 0.12 to 0.17, Mix 0.04 to 0.07,
Pre-delay 0.04 to 0.11, Low cut 0.14 to 0.24, High cut none. The old one-knob-at-a-time calibration (other knobs at their defaults) failed
because in the benchmark every knob moves at once: a delay with Mix at its default of nearly nothing gave no echo to time, and a multi-voice
or modulated delay moved the measured echo. Models with several independent echo times (multitap, harmony, dual, criss-cross) are not
solved by this: their seeds are left free.

**Family score.** The general score plus `w` times a distance between the target's and the candidate's impulse responses: the late
energy envelope at 10, 40, 150 and 400 ms (a timing error costs at every scale; the coarse ones give the search a basin) and the
energy-decay curve per band. 1.0 is 20 dB of average envelope error. `w` = 0.25 is the largest weight that passed the trust bar (true
preset first against 24 neighbours: 1.000 audible rank-1 on the 46 scoreable devfam delay and reverb items, same as the plain score; 0.35 and
above fell under the bar), so the term is a landscape aid, not a better judge. Registered in DECISIONS (2026-10-17, [T122](../../board/TASKS.md#t122 "Family dispatch and time-based specialist")).

**Benchmark finding for the lead.** Some targets are not identifiable by sound. In synthetic-devfam, 4 of the 22 scoreable delay items
(all DelayPolySustain) render bit-identical to their DI (null -145 dB) and 4 more targets are within -30 dB of it (rule B's tie); a
model-accuracy figure on them is a coin flip (the arm that lists the pass-through model first in a tie wins). 2 delay items (Swell
Adriatic) render silent and cannot be scored. Report model accuracy on the audible items as well, and consider applying rule B to the
item itself.

## The modulation specialist (chorus, flanger, phaser, tremolo, vibrato, rotary)

Owner: tone-metrics ([T116](../../board/TASKS.md#t116 "Modulation specialist")). Code: `src/tonematch/metrics/mod_specialist.py`;
library preparation `scripts/mod_calibrate.py`; trust check `scripts/mod_trust_check.py` (`mod-trust-v1`, registered in DECISIONS
2026-10-17, [T116](../../board/TASKS.md#t116 "Modulation specialist")).

**Why a family of its own.** A modulation block's LFO free-runs: two recordings of one preset start at different points of the cycle,
so a frame-by-frame comparison (null, `mrstft_v2`) of the right preset can be far off, and a static EQ, compressor or reverb, which matches
the LFO-averaged sound, can beat the right modulation block at a slightly wrong rate or duty cycle. That is what Gate 2 attempt 1 did on
7 of its 12 mod items.

**What it measures** (target against its DI, both at unit RMS, so the notes and the level mostly cancel). Every feature is a statistic
over time, so moving the LFO's starting phase leaves it unchanged up to noise:

- the LFO-averaged transfer spectrum (1/3 octave, 160 Hz to 6.3 kHz, mean level removed);
- per band, the level trajectory in 5 ms steps: its spread (quantiles: depth and waveform shape), its modulation spectrum (0.2 to 25 Hz,
  4 band groups) and the correlation between band groups (+1 tremolo, below 0 harmonic tremolo);
- a pitch trajectory from the phase advance of output against input (250 Hz to 2.5 kHz), in cents: quantiles and modulation spectrum;
- the moving comb (weighted cepstrum of the log transfer less its time average, 0.3 to 25 ms) and the moving notch (deepest dip);
- the static share |mean H|^2 / mean |H|^2 above 700 Hz: a dry path survives the time average, a modulated wet path does not.

**Interface.**

```python
from tonematch.metrics import mod_specialist as MS
MS.features(target, dry) -> ModFeatures          # phase-invariant features
MS.analyse(target, dry, presence_mod=None) -> ModAnalysis   # rate_hz, rate_conf, am_depth_db, pm_depth_cents, wet_mix,
                                                 # comb_delay_ms, mod_score, is_mod, subtype (+ reason)
MS.seed(target, dry, models=None) -> dict        # {"analysis", "models": {model: {"values": {knob: value}, "fit", "subtype"}},
                                                 #  "ranking": models nearest first, "subtype_vote"}
MS.mod_distance(target, candidate, dry) -> float # phase-invariant distance, lower is better
MS.score(target, candidate, dry) -> float        # the same, target features cached (for many candidates)
MS.make_specialist() -> ModSpecialist            # the dispatch adapter (family "mod"): screen_hint, seed -> FamilySeed, score
```

- `screen_hint`: presence's known-DI mod probability (`presence.detect`, about 1.3 s per target), raised to the specialist's own score
  when that is at least 0.9. On helix-trust-v2 renders: recall 0.97 / 0.73 (train / test models), false positives 0.00 / 0.02.
- `seed(...).for_model(model, "mod", ranges)`: per knob, the distance-weighted mean of the 3 calibration renders of that model whose
  summary (log rate, level depth, pitch depth, wet mix, harmonic anti-correlation, coarse spectrum) is nearest the target's, with a
  band of twice their spread (rate 4 to 20 %, depth and mix 8 to 30 %, others 12 to 35 % of the range); a knob whose neighbours
  disagree by more than 35 % is left free. No calibration file, no seeds (the measurements are still reported).
- `score`: returns `wrap(kit)` = the general kit plus 0.2 x `mod_distance` for every candidate, **only when
  `FAMILY_SCORE_ADMITTED` is true**. It is False: in mod-trust-v1 that family score met the LFO-shifted bar (0.939) but fell
  0.03 below `mrstft_v2` with the LFO phase aligned (0.970 against 1.000; 0.02 allowed), so `score()` returns None and the
  dispatch keeps the general score. `make_specialist(score_weight=0.2)` forces it on for experiments.

**Trust result (`mod-trust-v1`, scored once; [DECISIONS 2026-10-17, T116](../../docs/DECISIONS.md#2026-10-17-tone-metrics-t116-modulation-specialist-trust-set-mod-trust-v1-registered-before-any-render-or-scoring "2026-10-17 (tone-metrics, T116): modulation specialist; trust set mod-trust-v1 registered BEFORE any render or scoring")).** Rank-1 under rule B on the 33 core items with the LFO
shifted: `mod_distance` 0.939 (bar 0.90, passed), `mrstft_v2` 0.273; with the LFO aligned 0.970 and 1.000. On the LFO-shifted
targets the seed's ranking holds the true model in its top 3 for 85 % of core items (first for 48 %); seeded knob errors (fraction of
the range) are rate 0.09, depth 0.08, mix 0.20; the measured rate is within 5 % on every Hz-knob tremolo. Flangers and phasers
(extended subset) are weaker: 0.808 rank-1, top-3 35 %.
- Cost: features about 0.1 s per 10 s render (one process); presence 1.3 s once per target.

**Calibration** (`scripts/mod_calibrate.py render`, then `build`): 37 modulation models (the 11 Native stand-ins of Gate 2 plus
choruses, flangers, phasers, vibes, vibratos and rotaries) at their defaults and 40 random settings (all knobs moved at once, CC grid
0.05 to 0.95) on two passages of the calibration DI (Guitar-TECHS P3_music directinput_04, used by no benchmark set): 3,034 renders,
11 minutes on one Native host. Local file `cache/mod-specialist/calibration.json` (holds model ids, gitignored). Re-run `build` after any
change to the features; re-run `render` for a new library version.

## The amp specialist and the `refiner` hook (gain family, [T120](../../board/TASKS.md#t120 "Amp specialist (human 2026-10-17 \"a specialist per effect family\""))

Code: `src/tonematch/search/amp_specialist.py` (`make_specialist()`), details and results in [amp-specialist.md](amp-specialist.md).
It follows the interface above (`seed` returns the drive and amp presence probabilities as `category_probs` and no knob seeds, `score`
is None, `screen_hint` is the chance of drive-or-amp) and adds one optional member:

```python
def refiner(self, target, dry) -> Callable | None     # refine(engine, kit, section, by_model, scores, budget, plan, renders, prior) -> (spent, notes) | None
```

`build_dispatch` stores the first specialist's refiner in `Dispatch.refiner` (with `refiner_family`); `choose_models_hybrid` calls it
with the broad scores and the whole remaining budget instead of `halve`. The refiner returns None to decline (the amp specialist does
when the broad leader is not an amp model), and the general halving then runs. A refiner owns its renders and must stay within
`renders`. Additive: a specialist without `refiner` and the neutral dispatch behave as before.

## The linear specialist (cab, EQ)

Owner: tone-metrics ([T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ")). Code: `src/tonematch/metrics/linear_specialist.py`;
library preparation `scripts/linear_tables.py`; evaluation `scripts/linear_eval.py` (trust bar `linear-v1`, [DECISIONS 2026-10-17, T117](../../docs/DECISIONS.md#2026-10-17-tone-metrics-t117-linear-specialist-trust-bar-linear-v1-registered-before-any-scoring "2026-10-17 (tone-metrics, T117): linear specialist trust bar linear-v1, registered BEFORE any scoring")).
Known-DI mode only.

**Why a family of its own.** A cab or EQ is linear and time-invariant, so with the DI known its whole effect is one frequency response,
and that can be computed instead of searched: the target's transfer response against its DI (H1 estimate, 0.68 s Welch segments,
1/12 octave from 31 Hz to 16 kHz, each band weighted by its coherence squared). Gate 2 attempt 1 lost 1 cab and 4 EQ items to models
of the same family whose sound is close; the response tells them apart.

**Response tables** (`scripts/linear_tables.py`, once per library version, one Native host, `cache/linear-tables/<version>/`, local):
per model, the response to a periodic multisine probe (1.4 s, synthetic) at a grid of knob settings. Knobs that interact are found
from the renders (pairwise test) and share a grid; groups add in dB. A one-knob group has all 128 CC steps; a larger group (mic-IR
Angle x Distance x Position, a parametric band's Freq x Gain x Q) gets per-knob nodes placed by a fine sweep. Table error against
fresh renders: under 0.05 dB RMS for legacy cabs and EQs, 0.04-0.17 dB for mic-IR cabs. Each table's meta also says whether the block
is LTI (`lti_check`: response unchanged at -12 dB input and from period to period); the Acoustic Guitar Sim is not.

**Interface.**

```python
from tonematch.metrics import linear_specialist as LS
LS.transfer(target, dry) -> Transfer                 # mag_db, weight, coherence, incoherent_db (power no LTI filter explains)
LS.LinearSpecialist(models=None)                     # over the cached tables
    .screen_hint(target, dry) -> ScreenHint          # family_prob, incoherent_db, category_probs {cab, eq}, shortlist, fit_db
    .seed(target, dry, models=None) -> [Fit]         # every model's least-squares knob fit, best first; no renders
    .refine(render, target, dry, models, budget=8)   # render the top seeds on the DI, remove table/estimator error, re-fit
    .score(target, candidate, dry) -> float          # response distance, dB (0 = same response)
LS.make_specialist() -> LinearFamily                 # the dispatch adapter (family "linear")
```

The adapter: `screen_hint` is the logistic of `incoherent_db` (midpoint -33 dB, 0.5 per dB); `seed(...).category_probs` splits the
hint between cab and EQ by each one's best fit; `for_model` returns the fitted value of every knob with a band of 4 % of its range;
`budget_share` 1.0 (the broad stage is unchanged); `score` returns None (bar 6 passed, so the lead may admit it as a family score; until
then the general score stands). The optional `refiner` takes over the refine stage when the hint is at least 0.5 and the pool has
tabled cab or EQ models: at most 8 renders of the best fits on the item's DI (scored with the item's kit, so its losses compare with
every other model's), then it stops if a render matches the target's response to 0.05 dB, else it hands the rest of the budget to the
general halving over the other models. Its renders go through the `Evaluator`, so they are counted. **For search (proposal, your
files untouched):** fill the slot with `LINEAR_ADAPTER = tonematch.metrics.linear_specialist.make_specialist` in `family_adapters.py`
(it ignores other families' options such as `time_score_weight`) and update `test_linear_slot_is_a_stub_until_filled`; then
`--specialists linear` and `all` use it. `tests/test_linear_specialist.py` checks the filled slot with a monkeypatch. **One snag for
`all`:** `build_dispatch` keeps the first family's `refiner`, and the gain adapter's `refiner()` always returns a function (it declines
only later, inside `refine`, when the broad leader is not an amp), so with `all` the linear refiner is never asked. The linear
`refiner()` returns None up front when its hint is below 0.5. Suggested fix: keep every family's refiner and try them in turn until
one returns a result.

**Result (`linear-v1`, dev set synthetic-lindev, 24 cab and EQ items, half held out; DECISIONS 2026-10-17).** Standalone specialist
against the Gate 2 hybrid at 300 renders: model 24/24 against 18/24, category 24/24 both, knob error 0.028 against 0.128, about 6
renders per item. Held-out 12: 12 against 8. Screen: 24/24 linear items called linear, 65/71 others called not linear; the misses are
near-dry settings (Mix 0.10-0.14, a compressor that barely compresses, a near-clean drive), which a linear block reproduces, so the
dispatcher should keep the other families in play (the refiner's fallback does).

## Adapters for `--specialists` ([T134](../../board/TASKS.md#t134 "Specialist adapters (blocking --specialists all for Gate 2 attempt 2)"))

Code: `src/tonematch/search/family_adapters.py` (`build_specialists(families, time_score_weight=)`), used by `scripts/synthetic_v1.py`
for every enabled family. One factory per family, none of them changes how a specialist measures, seeds or scores:

| family | adapter builds | policy |
|---|---|---|
| gain | `amp_specialist.make_specialist()` | as measured in [T120](../../board/TASKS.md#t120 "Amp specialist (human 2026-10-17 \"a specialist per effect family\""): category shares from presence, no knob seeds, general score, GP-EI `refiner` |
| mod | `mod_specialist.make_specialist(score_weight=0.0)` | screen hint and seeds only; `mod_distance` family score off in known-DI mode (lead ruling on [T116](../../board/TASKS.md#t116 "Modulation specialist")), even if the module's default flips |
| time | `TimeSpecialist(score_weight=...)` | the run's `--time-score-weight` |
| linear | stub | slot below |
| dynamics | stub | no specialist |

**Filling the linear slot.** `linear_specialist.make_specialist()` exists (tone-metrics, [T117](../../board/TASKS.md#t117 "Linear specialist for cab and EQ")) but is not wired: its `score` is None
and its optional `refiner` would take over the refine stage, a behaviour change the lead has not admitted. To wire it, set
`family_adapters.LINEAR_ADAPTER = lambda **_: linear_specialist.make_specialist()` (or make `make_linear` import it). The spec it must meet is
the four-member contract above; before it is switched on the lead needs (1) its decision on admitting the linear `refiner` and any
family score, (2) a dispatch comparison on synthetic-devfam cab and EQ items at equal budget. One test in
`tests/test_family_adapters.py` already runs a filled slot through `build_dispatch`.

Last updated: 2026-10-17.

Inside the hybrid (`--specialists linear`, slot filled at runtime, same 24 items): model 24/24, knob error 0.038, 195 search renders
per item on average: 15 items stopped after the broad stage plus about 6 linear renders (about 126), 9 fell back to the general
halving (about 305). The 120-render broad stage is most of what is left; for a slot the screen calls linear it could be skipped.
