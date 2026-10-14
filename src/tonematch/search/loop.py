"""The matching loop: budget planning, CMA-ES refinement per chain, successive halving, chain grouping.

Stages are separable functions: `plan_budget`, `refine_chain` (stage-wise CMA-ES), `match`
(beam over candidate chains, then final check). Seeding (predictor, retrieval) lives upstream and
hands `match` a list of candidate Presets; `seed_fn` is the optional hook.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable

import numpy as np

from tonematch.engines.base import EngineError, UnsupportedModelChange
from tonematch.search.cmaes import CMAES
from tonematch.search.space import ParamSpace, Prior
from tonematch.search.types import Budget, LossKitLike, Plan, RankedPreset, Section

BAD = 1e9
# Native's reset render costs ~the same for any clip length; Stomp renders in real time.
FIXED_COST_ENGINES = {"native"}


def resolve_weights(w):
    """'provisional' -> tonematch.metrics.PROVISIONAL_WEIGHTS (default weights are all 0: flat objective)."""
    if w == "provisional":
        try:
            from tonematch.metrics import PROVISIONAL_WEIGHTS
        except ImportError:
            return None
        return dict(PROVISIONAL_WEIGHTS)
    return w


def plan_budget(engine, budget: Budget) -> Plan:
    """Turn engine.cost() and the Budget into a render cap and strategy. Handles load_penalty_s = NaN."""
    c = engine.cost()
    caps = engine.capabilities()
    name = caps.engine
    assumed = not math.isfinite(c.load_penalty_s)
    penalty = c.seconds_per_render if assumed else c.load_penalty_s  # conservative guess when unmeasured
    par = max(1, int(c.parallelism))
    notes = []
    halving = name not in FIXED_COST_ENGINES and budget.short_s < budget.clip_s
    short_frac = budget.short_s / budget.clip_s if halving else 1.0
    spr = c.seconds_per_render * budget.clip_s / 2.0 if name not in FIXED_COST_ENGINES else c.seconds_per_render
    cap = budget.max_renders
    if budget.wall_s is not None:
        cap = min(cap, int(budget.wall_s * par / max(spr, 1e-9)))
        notes.append(f"wall_s={budget.wall_s} caps renders at {cap}")
    cap = max(cap, 1)
    pop = 4 + int(3 * math.log(max(2, 8)))
    if par > 1:
        pop = int(math.ceil(pop / par) * par)  # fill the engine's parallel lanes
    if assumed:
        notes.append("load_penalty_s is NaN: assumed one render time per model change")
    return Plan(name, cap, pop, halving, short_frac, par, spr, penalty, assumed,
                est_wall_s=cap * spr / par, notes=notes)


class Evaluator:
    """Applies parameter vectors to a loaded engine and scores the render. Counts renders in full-render units."""

    def __init__(self, engine, losskit: LossKitLike, section: Section, space: ParamSpace, plan: Plan,
                 budget: Budget, prior: Prior | None = None, deadline: float | None = None):
        self.engine, self.kit, self.sec, self.space = engine, losskit, section, space
        self.plan, self.prior, self.deadline = plan, prior, deadline
        sr = 48_000
        self.n_full = min(len(section.dry), len(section.target), int(budget.clip_s * sr))
        self.n_short = min(self.n_full, int(budget.short_s * sr))
        self.clip_s = self.n_full / sr
        self.weights = resolve_weights(budget.weights)
        self.spent = 0.0   # full-render equivalents
        self.calls = 0
        self._applied: dict | None = None

    def exhausted(self, cap: float) -> bool:
        return self.spent >= cap or (self.deadline is not None and time.perf_counter() > self.deadline)

    def reset_applied(self) -> None:
        self._applied = None

    def score(self, values: np.ndarray, short: bool = False, final: bool = False):
        ch = self.space.as_changes(values)
        diff = ch if self._applied is None else {r: v for r, v in ch.items() if self._applied.get(r) != v}
        n = self.n_short if short else self.n_full
        try:
            if diff:
                self.engine.set_params(diff)
                self._applied = {**(self._applied or {}), **ch}
            y = self.engine.render(self.sec.dry[:n])
            kw = {"stems": self.sec.stems, "mask": self.sec.mask} if final else {}
            if self.weights is not None:
                kw["weights"] = self.weights
            sc = self.kit.score(self.sec.target[:n], np.asarray(y)[:n], **kw)
            total = float(sc.total)
            terms = dict(sc.terms)
        except EngineError:
            total, terms = BAD, {}
        self.calls += 1
        self.spent += self.plan.short_frac if short and self.plan.halving else 1.0
        if not math.isfinite(total):
            total = BAD
        if self.prior is not None:
            total += self.prior.penalty(self.space.as_changes(values))
        return total, terms


def refine_chain(ev: Evaluator, x0_values: np.ndarray, render_cap: float, sigma0: float = 0.25,
                 seed: int = 0, eta: int = 3) -> tuple[np.ndarray, float, dict, list[dict]]:
    """Stage-wise CMA-ES: gain/EQ group first, then time-based group. Budget split by dimension.

    Returns (best values, best score, best terms, per-stage log). Spends at most ~render_cap
    full-render equivalents on top of what `ev.spent` already holds.
    """
    space, plan = ev.space, ev.plan
    start_spent = ev.spent
    best = np.asarray(x0_values, float).copy()
    ev.reset_applied()
    best_f, best_terms = ev.score(best)
    log: list[dict] = []
    stages = space.stages()
    for si, idx in enumerate(stages):
        share = render_cap * len(idx) / space.n
        stage_end = ev.spent + share if si < len(stages) - 1 else start_spent + render_cap
        n = len(idx)
        u0 = space.to_unit(best)[idx]
        es = CMAES(u0, sigma0=sigma0, popsize=max(plan.popsize if n > 8 else 4 + int(3 * math.log(max(n, 2))),
                                                  plan.parallelism), seed=seed + si)
        gens, stage_start = 0, ev.spent
        stage_best_f = best_f
        while not ev.exhausted(stage_end):
            xs = es.ask()
            vecs = []
            for x in xs:
                u = space.to_unit(best).copy()
                u[idx] = es.clip(x)
                vecs.append(space.from_unit(u))
            if plan.halving:
                short = [ev.score(v, short=True)[0] for v in vecs]
                keep = max(1, math.ceil(len(vecs) / eta))
                top = list(np.argsort(short, kind="stable")[:keep])
                full = {int(i): ev.score(vecs[i]) for i in top}
                # promoted candidates rank by full score, the rest after them by short score
                order_f = np.empty(len(vecs))
                prom = sorted(full, key=lambda i: full[i][0])
                for r, i in enumerate(prom):
                    order_f[i] = r
                rest = [i for i in np.argsort(short, kind="stable") if int(i) not in full]
                for r, i in enumerate(rest):
                    order_f[i] = len(prom) + r
                scores = {i: full[i] for i in full}
            else:
                res = [ev.score(v) for v in vecs]
                order_f = np.array([r[0] for r in res])
                scores = {i: res[i] for i in range(len(vecs))}
            es.tell(xs, order_f)
            gens += 1
            for i, (f, t) in scores.items():
                if f < best_f:
                    best_f, best_terms, best = f, t, vecs[i]
        log.append({"stage": si, "params": [space.refs[j] for j in idx], "generations": gens,
                    "renders": round(ev.spent - stage_start, 2), "score_in": stage_best_f, "score_out": best_f})
    return best, best_f, best_terms, log


def _key(preset) -> tuple:
    return tuple((s.category, s.model_id) for s in preset.slots)


def _apply_values(preset, space: ParamSpace, values) -> object:
    p = copy.deepcopy(preset)
    for (i, k), v in zip(space.refs, values, strict=True):
        p.slots[i].params[k] = float(v)
    return p


def _violations(validator, preset) -> list[str]:
    if validator is None:
        return []
    return [str(v) for v in validator(preset) if getattr(v, "severity", "fail") == "fail"]


def default_validator() -> Callable | None:
    """preset.validate against the user's catalog, or None when no catalog cache exists."""
    try:
        from tonematch.catalog import load_catalog
        from tonematch.preset import validate

        cat = load_catalog()
    except Exception:  # noqa: BLE001 - no catalog cache: caller records "unvalidated"
        return None
    return lambda p: validate(p, cat)


def _steps(engine):
    return engine.capabilities().extras.get("param_steps")


def match(section: Section, engine, losskit: LossKitLike, budget: Budget | None = None, *,
          candidates: list, free: dict[tuple, list] | None = None, prior: Prior | None = None,
          validator: Callable | None = None, beam: int = 2, beam_frac: float = 0.35,
          bounds: dict | None = None) -> list[RankedPreset]:
    """Rank candidate chains by tuning each briefly, refining the best `beam`, then a final check.

    candidates: whole-chain Presets from the seed stages (predictor, retrieval). Trials are grouped
    by chain, so `engine.load` is called once per chain per phase. A chain the engine cannot load
    (UnsupportedModelChange) is skipped and noted.
    """
    budget = budget or Budget()
    plan = plan_budget(engine, budget)
    t0 = time.perf_counter()
    deadline = t0 + budget.wall_s if budget.wall_s else None
    steps = _steps(engine)
    uniq: dict[tuple, object] = {}
    for p in candidates:
        uniq.setdefault(_key(p), p)  # one trial group per chain
    skipped: list[str] = []
    live: list[dict] = []
    for key, p in uniq.items():
        bad = _violations(validator, p)
        if bad:
            skipped.append(f"{key}: invalid ({'; '.join(bad)})")
            continue
        space = ParamSpace.from_preset(p, (free or {}).get(key), bounds, steps)
        live.append({"key": key, "preset": p, "space": space, "values": space.from_unit(space.to_unit(space.start)),
                     "f": math.inf, "terms": {}, "log": [], "spent": 0.0, "calls": 0})
    final_reserve = budget.top_k_final
    pool = max(plan.max_renders - final_reserve, len(live))
    ev_total = 0.0
    calls = 0

    def run(group: list[dict], per_chain: float, sigma: float, seed_off: int) -> None:
        nonlocal ev_total, calls
        for g in group:  # one load per chain: trials are grouped
            try:
                engine.load(g["preset"])
            except UnsupportedModelChange as e:
                g["skip"] = str(e)
                continue
            ev = Evaluator(engine, losskit, section, g["space"], plan, budget, prior, deadline)
            v, f, t, lg = refine_chain(ev, g["values"], per_chain, sigma, budget.seed + seed_off)
            g.update(values=v, f=f, terms=t)
            g["log"] += lg
            g["spent"] += ev.spent
            g["calls"] += ev.calls
            ev_total += ev.spent
            calls += ev.calls
            g["preset"] = _apply_values(g["preset"], g["space"], v)

    if len(live) > 1:
        run(live, pool * beam_frac / len(live), 0.3, 0)
        ok = sorted((g for g in live if "skip" not in g), key=lambda g: g["f"])
        survivors = ok[:max(1, beam)]
        run(survivors, (pool - ev_total) / len(survivors), 0.12, 100)
    else:
        run(live, float(pool), 0.25, 0)
        survivors = [g for g in live if "skip" not in g]
    for g in live:
        if "skip" in g:
            skipped.append(f"{g['key']}: {g['skip']}")

    # Final check: top few, full clip, with stems / mask when the section has them.
    ranked: list[RankedPreset] = []
    for g in sorted(survivors, key=lambda g: g["f"])[:budget.top_k_final]:
        violations = _violations(validator, g["preset"])
        if violations:
            skipped.append(f"{g['key']}: final preset invalid ({'; '.join(violations)})")
            continue
        engine.load(g["preset"])
        ev = Evaluator(engine, losskit, section, g["space"], plan, budget, prior)
        f, terms = ev.score(g["values"], final=True)
        calls += ev.calls
        ranked.append(RankedPreset(
            preset=g["preset"], score=f, terms=terms, chain=g["key"],
            values=g["space"].as_changes(g["values"]), engine=plan.engine, renders=g["calls"] + ev.calls,
            wall_s=time.perf_counter() - t0, stages=g["log"], final_checked=True, clip_s=ev.clip_s,
            loss_label="admitted loss" if budget.admitted else "unadmitted loss",
            notes=[*plan.notes, *([f"validator: {'catalog' if validator else 'none (unvalidated)'}"])]))
    ranked.sort(key=lambda r: r.score)
    for r in ranked:
        r.notes.extend(f"skipped {s}" for s in skipped)
    return ranked
