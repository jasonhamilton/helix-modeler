"""Hybrid model choice (Gate 2): broad space-filling exploration, narrowing, then seeded refinement.

Random search finds the model because, with a trustworthy score, many settings per model reveal which
model can reach the target. Measured seeding (`measured_seed`) finds the settings because it starts
the refinement in the right place. This stage keeps both:

  broad     a fraction `f` of the budget is spent on ALL pool models: per model one anchor render (the
            measured seed where one exists, else the library defaults) and Latin-hypercube draws
            over the model's space (the seed band for seeded dimensions, the full range for the
            rest). Models are served in corpus-usage order; leftover renders go to the most-used
            models first. Every render goes through `model_choice.screen` (`base.render_many`).
  narrow    the top `k` models by their best broad score (ties: corpus usage).
  refine    `model_choice.halve` over the k models (successive halving, stage-wise CMA-ES, the seeded
            models inside their band), warm-started from each model's best broad draw.

Same total budget as `choose_models`. Everything here is a separable function: `latin_hypercube`,
`broad_settings`, `broad_stage`, `choose_models_hybrid`. Weights are the caller's.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from tonematch.search.measured_seed import (
    Measured,
    Seed,
    category_seeded,
    seeded_candidate,
    seeds_for,
)
from tonematch.search.model_choice import (
    ModelCandidate,
    ModelChoice,
    ModelScore,
    Setting,
    drop_time_variant,
    halve,
    screen,
)
from tonematch.search.types import Budget, Plan, Section


def latin_hypercube(n: int, d: int, rng: np.random.Generator) -> np.ndarray:
    """n points in [0,1]^d, one per stratum in every dimension, jittered inside the stratum."""
    if n <= 0:
        return np.zeros((0, d))
    u = (np.stack([rng.permutation(n) for _ in range(d)], axis=1) + rng.random((n, d))) / n
    return np.clip(u, 0.0, 1.0)


def broad_settings(c: ModelCandidate, n: int, rng: np.random.Generator, anchor: str = "start",
                   fingerprint: bool = False) -> list[Setting]:
    """`n` settings for one model: the anchor (`c.space.start`, which holds the seed for a seeded
    model) then, when `fingerprint`, the model's non-default fingerprint settings (centre and corners,
    seeded knobs held at the seed) while they last, then Latin-hypercube draws over `c.space`."""
    if n <= 0:
        return []
    sp = c.space
    out: list[Setting] = [Setting(anchor, {r: float(v) for r, v in zip(sp.refs, sp.start, strict=True)})]
    if fingerprint:
        for st in c.settings:
            if len(out) >= n:
                break
            if st.label not in ("default", "seed"):
                out.append(replace(st))
    m = n - len(out)
    if m > 0:
        u = latin_hypercube(m, len(sp.refs), rng)
        for i, row in enumerate(u):
            v = sp.from_unit(row)
            out.append(Setting(f"lhs{i}", {r: float(x) for r, x in zip(sp.refs, v, strict=True)}))
    return out[:n]


# Coarse grid (T176): Drive and Tone, centre first, so a truncated grid keeps the middle of the range.
COARSE_DRIVE = (0.5, 0.2, 0.8)
COARSE_TONE = (0.3, 0.7)


def coarse_grid(c: ModelCandidate) -> list[Setting]:
    """The coarse Drive x Tone grid of one model: about 3 x 2 settings, the other free knobs at the anchor
    (`space.start`, the seed where there is one). A model with only one of the two knobs gets that axis; a model
    with neither gets []. Values are fractions of each knob's range, snapped to the engine grid."""
    sp = c.space
    axes: list[tuple[int, tuple[float, ...]]] = []
    for j, (_, name) in enumerate(sp.refs):
        key = name.lower()
        if key == "drive":
            axes.append((j, COARSE_DRIVE))
        elif key == "tone":
            axes.append((j, COARSE_TONE))
    if not axes:
        return []
    u0 = sp.to_unit(sp.start)
    pts: list[tuple[float, dict[int, float]]] = [(0.0, {})]
    for j, levels in axes:
        pts = [(rank + i, {**d, j: lv}) for rank, d in pts for i, lv in enumerate(levels)]
    out = []
    for k, (_, d) in enumerate(sorted(pts, key=lambda t: t[0])):
        u = u0.copy()
        for j, lv in d.items():
            u[j] = lv
        v = sp.from_unit(u)
        out.append(Setting(f"grid{k}", {r: float(x) for r, x in zip(sp.refs, v, strict=True)}))
    return out


def coarse_settings(c: ModelCandidate, n: int, rng: np.random.Generator) -> list[Setting]:
    """`n` broad settings for one model in coarse mode: the anchor, then the Drive x Tone grid while it lasts
    (a model with no Drive or Tone knob falls back to Latin-hypercube draws), truncated to `n`."""
    grid = coarse_grid(c)
    if not grid:
        return broad_settings(c, n, rng)
    sp = c.space
    out = [Setting("start", {r: float(v) for r, v in zip(sp.refs, sp.start, strict=True)})] + grid
    return out[:n]


def coarse_size(c: ModelCandidate) -> int:
    """Renders the coarse grid of `c` takes, anchor included (0: the model has no Drive or Tone knob)."""
    return len(coarse_grid(c)) + 1 if coarse_grid(c) else 0


def with_anchor(c: ModelCandidate, vals: dict[str, float]) -> ModelCandidate:
    """`c` with its anchor (`space.start`) moved to `vals` (parameter units by name; other knobs stay at the
    library start; values are clipped to the range and snapped to the step grid)."""
    sp = c.space
    start = np.array([float(vals.get(r[1], v)) if r[0] == 0 else float(v)
                      for r, v in zip(sp.refs, sp.start, strict=True)])
    return replace(c, space=replace(sp, start=sp.from_unit(sp.to_unit(start))))


def allocate(n_models: int, total: int) -> list[int]:
    """Draws per model in the given order: an even share, the remainder one each from the front."""
    base, extra = divmod(max(total, 0), max(n_models, 1))
    return [base + (1 if i < extra else 0) for i in range(n_models)]


def broad_stage(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget, weights, n_total: int,
                rng_seed: int = 0, fingerprint: bool = False, validator: Callable | None = None,
                score_kit=None, counts: list[int] | None = None,
                coarse: bool = False) -> tuple[list[ModelScore], int, list[str]]:
    """Render `n_total` broad draws spread over `cands` (already in usage order; evenly unless `counts` says
    otherwise: the family dispatch). Returns the per-model scores (`start` is the best draw's values), the
    renders spent and notes."""
    counts = counts if counts is not None else allocate(len(cands), n_total)
    jobs = []
    for i, (c, n) in enumerate(zip(cands, counts, strict=True)):
        rng = np.random.default_rng([rng_seed, 77, i])
        jobs.append(replace(c, settings=coarse_settings(c, n, rng) if coarse
                            else broad_settings(c, n, rng, fingerprint=fingerprint)))
    jobs = [j for j in jobs if j.settings]
    return screen(engine, score_kit or kit, section, jobs, weights, budget.clip_s, None, validator)


def choose_models_hybrid(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget, *,
                         meas: Measured | None = None, cal: dict | None = None,
                         ranges: dict[str, dict[str, tuple[float, float]]] | None = None,
                         f: float = 0.5, k: int = 5, eta: int = 2, sigma0: float = 0.3, min_per_model: float = 6.0,
                         refine_scale: float = 0.8, broad_kit=None, fingerprint: bool = False, usage: dict[str, int] | None = None,
                         validator: Callable | None = None, prior=None,
                         is_tv: Callable[[str], bool] | None = None, plan: Plan | None = None,
                         dispatch=None, policy=None, predictor_seed: dict | None = None,
                         knob_tol: float = 0.0, breadth: float = 2.0) -> ModelChoice:
    """Rank `cands` (every pool model, all categories) at `budget.max_renders` in total.

    `dispatch` (`specialists.Dispatch`) is the family dispatch of the item: with families enabled, the broad
    draws are shared by the specialists' weights, a specialist's knob estimates replace the generic measured
    seeds of its models and its family score replaces the general kit. None or an empty dispatch is the
    frozen general method, draw for draw.

    `policy` (`family_policy.Policy`, T143): a category prior from the dispatch hints and a specialist shortlist,
    applied to which models are refined and to the final order. None or an inactive policy is the frozen method.

    `refine_scale` < 1 hands the refine stage less than the exact remainder: `halve` finishes the CMA-ES
    generation it is in and re-scores each warm start, so it overshoots its share by 10-20 %
    (measured: iteration 1 spent 313-339 of 300). 0.8 brings the total to about 300.

    `broad_kit` scores the broad draws (default `kit`). Pass the loss kit without the level guard: the
    guard adds 0.08 per dB beyond 0.25 dB, so a random Level/Output knob swamps the shape difference
    that tells the models apart (iteration 2 lost the true model in the broad stage on 3 of 16 items).

    `predictor_seed` (T162): `{model: {param name: value}}`, a first guess from a predictor. The named model
    keeps its unbanded space and its anchor (the first broad draw, `space.start`) is the guess, so the guess
    costs the one render the default anchor would have cost. It replaces any measured band for that model.
    None is the frozen method, draw for draw.

    `knob_tol` (T176, 0 = off = the frozen method): the ear cannot tell Drive or Tone steps of about 0.15 apart,
    so the search stops polishing a knob inside that and spends the renders on which model and circuit family.
    With `knob_tol` > 0: (1) the broad stage is a coarse Drive x Tone grid per model (about 3 x 2 plus the anchor)
    instead of Latin-hypercube draws, and what the grid does not use goes to the refine stage; (2) the refine set
    is `breadth` times wider (`k * breadth` models, the top families first); (3) every refinement stops once its
    knobs are inside `knob_tol` (`loop.refine_chain`), and what it saves is rolled into the later rungs.
    The renders actually used are in `ch.renders` and `ch.rungs`; `budget.max_renders` is a ceiling."""
    from tonematch.search.loop import resolve_weights

    disp = dispatch if (dispatch is not None and dispatch.families) else None
    if disp is not None and disp.wrap is not None:      # the family score, for the broad draws and the refine
        broad_kit = disp.wrap(broad_kit) if broad_kit is not None else None
        kit = disp.wrap(kit)
    notes: list[str] = list(meas.notes) if meas is not None else []
    if disp is not None:
        notes += disp.notes
    cands, dropped = drop_time_variant(cands, is_tv)
    if dropped:
        notes.append("time-variant models excluded: " + ", ".join(dropped))
    if not cands:
        raise ValueError("no candidate models")
    plan = plan or Plan("native", budget.max_renders, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    weights = resolve_weights(budget.weights)
    total = float(budget.max_renders)
    rank = usage or {c.model: i for i, c in enumerate(cands)}
    cands = sorted(cands, key=lambda c: rank.get(c.model, 10 ** 6))

    # measured seeds: a seeded model's space is banded around them and its anchor is the seed
    by_model: dict[str, ModelCandidate] = {}
    seed_info: dict[str, dict] = {}
    if (meas is not None and cal) or disp is not None:
        for c in cands:
            sd: dict[str, Seed] = {}
            if disp is not None and c.model in disp.seeds:
                sd = disp.seeds[c.model]
            elif meas is not None and cal and category_seeded(c.category, meas.probs):
                sd = seeds_for(c.model, c.category, cal, meas, (ranges or {}).get(c.model, {}))
            if not sd:
                continue
            sc = seeded_candidate(c, sd) if sd else None
            if sc is not None:
                by_model[c.model] = sc
                seed_info[c.model] = {p: {"value": s.value, "from": s.quantity, "measured": s.measured}
                                      for p, s in sd.items()}
    notes.append(f"seeded {len(seed_info)} of {len(cands)} models from the measurement")
    for m, vals in (predictor_seed or {}).items():
        base = next((c for c in cands if c.model == m), None)
        if base is not None:
            by_model[m] = with_anchor(base, vals)
            seed_info[m] = {"predictor_seed": dict(vals)}
    working = [by_model.get(c.model, c) for c in cands]
    by_model = {c.model: c for c in working}

    n_broad = round(f * total)
    counts = None
    if disp is not None:
        from tonematch.search.specialists import draw_counts
        counts = draw_counts([(c.model, c.category) for c in working], n_broad, disp)
        notes.append("broad draws by category: " + ", ".join(
            f"{cat} {sum(n for n, c in zip(counts, working, strict=True) if c.category == cat)}"
            for cat in dict.fromkeys(c.category for c in working)))
    coarse = knob_tol > 0
    if coarse:
        base = counts if counts is not None else allocate(len(working), n_broad)
        counts = [min(n, coarse_size(c)) if coarse_size(c) else n for n, c in zip(base, working, strict=True)]
        notes.append(f"knob tolerance {knob_tol:g}: coarse Drive x Tone grid, broad stage {sum(counts)} of {n_broad} renders")
    scores, n_spent, nn = broad_stage(section, engine, kit, working, budget, weights, n_broad, budget.seed,
                                      fingerprint, validator, broad_kit, counts, coarse=coarse)
    notes += nn
    scores.sort(key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))
    pol = policy if (policy is not None and policy.active and disp is not None) else None
    view = scores
    if pol is not None:         # T143: the refine set is shortlist + best by prior-adjusted screen
        from tonematch.search.family_policy import adjusted, narrow
        view = narrow(scores, k, disp, pol, rank)
        notes.append("family policy: refine set " + ", ".join(
            f"{s.model}({adjusted(s, s.screen, disp, pol):.3f})" for s in view))
        lead = min(view, key=lambda s: adjusted(s, s.screen, disp, pol), default=None)
    else:
        lead = next((s for s in scores if math.isfinite(s.screen)), None)    # scores are sorted by screen
    res = None
    refine_fn = disp.refiner_for(lead.category if lead is not None else None) if disp is not None else None
    if refine_fn is not None:   # the refine stage of the leader's family (T120 gain, T117 linear); None = it declines
        res = refine_fn(engine, kit, section, by_model, view, budget, plan, max(total - n_spent, 0.0), prior)
    if res is not None:
        hs, hn = res
    else:
        k_refine = max(k, int(math.ceil(k * breadth))) if coarse else k
        hs, hn = halve(engine, kit, section, by_model, view, budget, plan, max(total - n_spent, 0.0) * refine_scale,
                       k_refine, eta, sigma0, min_per_model, prior, budget.seed, knob_tol=knob_tol)
    notes += hn
    if pol is not None:
        from tonematch.search.family_policy import final_order
        refined, others = final_order(scores, disp, pol, rank)
    else:
        refined = sorted((s for s in scores if s.refined is not None), key=lambda s: s.refined)
        others = sorted((s for s in scores if s.refined is None), key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))
    ch = ModelChoice(refined + others, n_spent + hs, n_spent, notes)
    ch.seeds = seed_info                                    # type: ignore[attr-defined]
    ch.measured = dict(meas.q) if meas is not None else {}  # type: ignore[attr-defined]
    ch.rungs = {"broad": n_spent, "refine": hs}             # type: ignore[attr-defined]
    return ch
