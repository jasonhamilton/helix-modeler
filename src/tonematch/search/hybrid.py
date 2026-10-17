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


def allocate(n_models: int, total: int) -> list[int]:
    """Draws per model in the given order: an even share, the remainder one each from the front."""
    base, extra = divmod(max(total, 0), max(n_models, 1))
    return [base + (1 if i < extra else 0) for i in range(n_models)]


def broad_stage(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget, weights, n_total: int,
                rng_seed: int = 0, fingerprint: bool = False, validator: Callable | None = None,
                score_kit=None) -> tuple[list[ModelScore], int, list[str]]:
    """Render `n_total` broad draws spread over `cands` (already in usage order). Returns the per-model
    scores (`start` is the best draw's values), the renders spent and notes."""
    counts = allocate(len(cands), n_total)
    jobs = []
    for i, (c, n) in enumerate(zip(cands, counts, strict=True)):
        rng = np.random.default_rng([rng_seed, 77, i])
        jobs.append(replace(c, settings=broad_settings(c, n, rng, fingerprint=fingerprint)))
    jobs = [j for j in jobs if j.settings]
    return screen(engine, score_kit or kit, section, jobs, weights, budget.clip_s, None, validator)


def choose_models_hybrid(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget, *,
                         meas: Measured | None = None, cal: dict | None = None,
                         ranges: dict[str, dict[str, tuple[float, float]]] | None = None,
                         f: float = 0.5, k: int = 5, eta: int = 2, sigma0: float = 0.3, min_per_model: float = 6.0,
                         refine_scale: float = 0.8, broad_kit=None, fingerprint: bool = False, usage: dict[str, int] | None = None,
                         validator: Callable | None = None, prior=None,
                         is_tv: Callable[[str], bool] | None = None, plan: Plan | None = None) -> ModelChoice:
    """Rank `cands` (every pool model, all categories) at `budget.max_renders` in total.

    `refine_scale` < 1 hands the refine stage less than the exact remainder: `halve` finishes the CMA-ES
    generation it is in and re-scores each warm start, so it overshoots its share by 10-20 %
    (measured: iteration 1 spent 313-339 of 300). 0.8 brings the total to about 300.

    `broad_kit` scores the broad draws (default `kit`). Pass the loss kit without the level guard: the
    guard adds 0.08 per dB beyond 0.25 dB, so a random Level/Output knob swamps the shape difference
    that tells the models apart (iteration 2 lost the true model in the broad stage on 3 of 16 items)."""
    from tonematch.search.loop import resolve_weights

    notes: list[str] = list(meas.notes) if meas is not None else []
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
    if meas is not None and cal:
        for c in cands:
            if not category_seeded(c.category, meas.probs):
                continue
            sd: dict[str, Seed] = seeds_for(c.model, c.category, cal, meas, (ranges or {}).get(c.model, {}))
            sc = seeded_candidate(c, sd) if sd else None
            if sc is not None:
                by_model[c.model] = sc
                seed_info[c.model] = {p: {"value": s.value, "from": s.quantity, "measured": s.measured}
                                      for p, s in sd.items()}
    notes.append(f"seeded {len(seed_info)} of {len(cands)} models from the measurement")
    working = [by_model.get(c.model, c) for c in cands]
    by_model = {c.model: c for c in working}

    n_broad = round(f * total)
    scores, n_spent, nn = broad_stage(section, engine, kit, working, budget, weights, n_broad, budget.seed,
                                      fingerprint, validator, broad_kit)
    notes += nn
    scores.sort(key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))
    hs, hn = halve(engine, kit, section, by_model, scores, budget, plan, max(total - n_spent, 0.0) * refine_scale, k, eta,
                   sigma0, min_per_model, prior, budget.seed)
    notes += hn
    refined = sorted((s for s in scores if s.refined is not None), key=lambda s: s.refined)
    others = sorted((s for s in scores if s.refined is None), key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))
    ch = ModelChoice(refined + others, n_spent + hs, n_spent, notes)
    ch.seeds = seed_info                                    # type: ignore[attr-defined]
    ch.measured = dict(meas.q) if meas is not None else {}  # type: ignore[attr-defined]
    ch.rungs = {"broad": n_spent, "refine": hs}             # type: ignore[attr-defined]
    return ch
