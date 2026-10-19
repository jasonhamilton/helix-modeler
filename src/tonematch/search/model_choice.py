"""Model choice per slot (T050): fingerprint-informed screening, top K, successive halving.

Stage 1, screen. Every candidate model is rendered through the target's DI at its fingerprint
settings (the block library's `coarse-v1` grid: the default plus the corners and centre of the
model's two primary parameters, `native_library.fingerprint_set`), through `base.render_many`, and
each render is scored against the target. A model's screen score is its best setting; that setting
is its warm start. Time-variant models are left out (`is_time_variant`).

Stage 2, halving. The best `k` models are refined by CMA-ES from their warm start in rungs. Each
rung gives the survivors an equal share of that rung's renders, then keeps the best `1/eta`. The
last rung has one model. Renders are counted in full-render units, the same as `Evaluator.spent`.

Everything is a separable function: `fingerprint_settings`, `screen`, `halve`, `choose_models`.
Weights are a parameter (default `PROVISIONAL_WEIGHTS`, not admitted); this module admits nothing.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from tonematch.engines.base import EngineError, ParamRef, render_many
from tonematch.search.loop import BAD, Evaluator, refine_chain
from tonematch.search.space import ParamSpace
from tonematch.search.types import Budget, Plan, Section

SR = 48_000
CHUNK = 24  # variants per render_many call: bounds the memory of queued jobs


@dataclass
class Setting:
    label: str
    values: dict[ParamRef, float]   # parameter values this fingerprint setting pins (empty = defaults)


@dataclass
class ModelCandidate:
    """One model a slot may take: its base preset, the free parameters and the fingerprint settings."""
    model: str
    category: str
    preset: object                  # Preset with the model at its library defaults
    space: ParamSpace
    settings: list[Setting] = field(default_factory=list)

    def preset_at(self, values: dict[ParamRef, float]):
        """The base preset with the free parameters at `space.start`, then `values` on top."""
        p = copy.deepcopy(self.preset)
        for (i, k), v in zip(self.space.refs, self.space.start, strict=True):
            p.slots[i].params[k] = float(v)
        for (i, k), v in values.items():
            p.slots[i].params[k] = float(v)
        return p

    def start_vector(self, values: dict[ParamRef, float]) -> np.ndarray:
        """`space.start` with `values` laid on the free parameters, snapped to the engine grid."""
        x = np.array(self.space.start, float)
        for j, r in enumerate(self.space.refs):
            if r in values:
                x[j] = values[r]
        return self.space.from_unit(self.space.to_unit(x))


@dataclass
class ModelScore:
    model: str
    category: str
    screen: float = math.inf        # best fingerprint-setting score
    setting: str = "default"        # the setting that gave it
    start: dict[ParamRef, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)   # every setting's score
    refined: float | None = None
    values: np.ndarray | None = None
    rungs: list[dict] = field(default_factory=list)


@dataclass
class ModelChoice:
    ranked: list[ModelScore]        # refined models first (best first), then the rest by screen score
    renders: float
    screen_renders: int
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> ModelScore:
        return self.ranked[0]


# ------------------------------------------------------------------ fingerprint settings

def setting_order(labels: list[str]) -> list[int]:
    """Indices of the fingerprint settings in the order they are worth rendering when the budget
    cannot afford all of them: the default, the centre (all CCs equal 64), then the corners."""
    def rank(i: int) -> tuple:
        s = labels[i]
        if s == "default":
            return (0, i)
        if s and all(p.endswith("@64") for p in s.split(",")):
            return (1, i)
        return (2, i)
    return sorted(range(len(labels)), key=rank)


def fingerprint_settings(model_id: str, fp_set: dict | None = None, cap: int | None = None,
                         slot: int = 0) -> list[Setting]:
    """The model's fingerprint settings as parameter values on `slot`, best-first (`setting_order`).
    `fp_set` is `native_library.fingerprint_set(model_id)`; with none the model has only its
    default setting. Settings the probe rendered silent (`fingerprint: null`) are kept: they are
    still valid renders of the block."""
    if not fp_set or not fp_set.get("settings"):
        return [Setting("default", {})]
    sets = fp_set["settings"]
    out = []
    for i in setting_order([s.get("setting", "default") for s in sets]):
        s = sets[i]
        out.append(Setting(s.get("setting", "default"),
                           {(slot, n): float(v) for n, v in (s.get("params") or {}).items()}))
    return out[:cap] if cap else out


def drop_time_variant(cands: list[ModelCandidate], is_tv: Callable[[str], bool] | None = None
                      ) -> tuple[list[ModelCandidate], list[str]]:
    """Candidates without time-variant models (`native_library.is_time_variant`), and the dropped ids."""
    if is_tv is None:
        from tonematch.engines.native_library import is_time_variant as is_tv
    keep = [c for c in cands if not is_tv(c.model)]
    return keep, [c.model for c in cands if is_tv(c.model)]


# ------------------------------------------------------------------ stage 1: screen

def _score_one(kit, target, y, weights) -> float:
    try:
        t = float(kit.score(target, np.asarray(y, np.float32)[:len(target)], weights=weights).total)
    except Exception:  # noqa: BLE001 - a failed score is a bad candidate, not a crashed search
        return BAD
    return t if math.isfinite(t) else BAD


def screen(engine, kit, section: Section, cands: list[ModelCandidate], weights, clip_s: float,
           settings_per_model: int | None = None, validator: Callable | None = None,
           chunk: int = CHUNK) -> tuple[list[ModelScore], int, list[str]]:
    """Render the DI through each candidate at its first `settings_per_model` fingerprint settings
    (all when None) and score them. Returns the per-model scores, the renders spent and notes.
    Invalid presets (`validator(preset)` returns violations) are not rendered."""
    n = min(len(section.dry), len(section.target), int(clip_s * SR))
    dry, target = section.dry[:n], section.target[:n]
    jobs: list[tuple[int, Setting, object]] = []
    notes: list[str] = []
    for ci, c in enumerate(cands):
        for s in (c.settings or [Setting("default", {})])[:settings_per_model]:
            p = c.preset_at(s.values)
            bad = validator(p) if validator else []
            if bad:
                notes.append(f"{c.model}@{s.label}: invalid, not rendered ({'; '.join(map(str, bad))})")
                continue
            jobs.append((ci, s, p))
    out = [ModelScore(c.model, c.category) for c in cands]
    spent = 0
    for a in range(0, len(jobs), chunk):
        part = jobs[a:a + chunk]
        try:
            ys = render_many(engine, dry, [p for _, _, p in part])
            spent += len(part)
        except EngineError as e:
            notes.append(f"render_many failed ({e}); rendering the chunk one by one")
            ys = []
            for _, _, p in part:
                try:
                    engine.load(p)
                    ys.append(engine.render(dry))
                except EngineError:
                    ys.append(None)
                spent += 1
        for (ci, s, _), y in zip(part, ys, strict=True):
            f = BAD if y is None else _score_one(kit, target, y, weights)
            ms = out[ci]
            ms.scores[s.label] = f
            if f < ms.screen:
                ms.screen, ms.setting, ms.start = f, s.label, dict(s.values)
    return out, spent, notes


# ------------------------------------------------------------------ stage 2: halving

def rung_plan(k: int, eta: int = 2) -> list[int]:
    """Survivors per rung: k, ceil(k/eta), ... down to 1."""
    sizes = [max(1, k)]
    while sizes[-1] > 1:
        sizes.append(max(1, math.ceil(sizes[-1] / eta)))
    return sizes


def halve(engine, kit, section: Section, cands: dict[str, ModelCandidate], scores: list[ModelScore],
          budget: Budget, plan: Plan, renders: float, k: int = 6, eta: int = 2, sigma0: float = 0.3,
          min_per_model: float = 6.0, prior=None, seed: int = 0, knob_tol: float = 0.0) -> tuple[float, list[str]]:
    """Successive halving over the best `k` screened models. `renders` is the total full-render
    budget for this stage. Each rung splits its share (`renders` over the rungs) equally among its
    survivors, refines each from its current best (the warm start in rung 0) and keeps the best
    1/eta. Mutates `scores` (refined, values, rungs). Returns (renders spent, notes).

    `knob_tol` > 0 (T176) lets each refinement stop once its knobs are within that tolerance
    (`loop.refine_chain`); what a refinement does not spend is left unspent (saved), not rolled into later rungs;
    0 is the old behaviour."""
    notes: list[str] = []
    n_early = n_ref = 0
    live = sorted((s for s in scores if math.isfinite(s.screen) and s.screen < BAD), key=lambda s: s.screen)[:k]
    if not live:
        return 0.0, ["no model survived the screen"]
    # Too little budget for k models: keep as many as can each get `min_per_model` in rung 0.
    n_rungs0 = len(rung_plan(len(live), eta))
    while len(live) > 1 and renders / n_rungs0 / len(live) < min_per_model:
        live = live[:-1]
        n_rungs0 = len(rung_plan(len(live), eta))
        notes.append(f"k cut to {len(live)}: budget {renders:.0f} renders leaves under {min_per_model:g} per model")
    sizes = rung_plan(len(live), eta)
    per_rung = renders / len(sizes)
    spent = 0.0
    cur: dict[str, tuple[float, np.ndarray]] = {}
    for ri, size in enumerate(sizes):
        live = live[:size]
        share = per_rung / len(live)
        for s in live:
            c = cands[s.model]
            x0 = cur[s.model][1] if s.model in cur else c.start_vector(s.start)
            # Rung 0 can be spent partly by the warm-start score itself; refine_chain scores x0 once.
            engine.load(c.preset_at(dict(zip(c.space.refs, x0, strict=True))))
            ev = Evaluator(engine, kit, section, c.space, plan, budget, prior)
            v, f, _, rlog = refine_chain(ev, x0, max(share - 1, 1.0), sigma0, seed + 31 * ri,
                                     tol=knob_tol.for_model(s.model) if hasattr(knob_tol, "for_model") else knob_tol)
            n_ref += 1
            n_early += int(any(st.get("stop") for st in rlog))
            spent += ev.spent
            if s.model not in cur or f <= cur[s.model][0]:
                cur[s.model] = (f, v)
            s.refined, s.values = cur[s.model][0], cur[s.model][1]
            s.rungs.append({"rung": ri, "survivors": len(live), "renders": round(ev.spent, 1), "score": float(f)})
        live = sorted(live, key=lambda s: s.refined)
    if knob_tol > 0:
        notes.append(f"knob tolerance {knob_tol:g}: {n_early} of {n_ref} refinements stopped early, "
                     f"{spent:.0f} of {renders:.0f} refine renders used")
    return spent, notes


# ------------------------------------------------------------------ the stage

def choose_models(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget, *,
                  k: int = 6, eta: int = 2, screen_frac: float = 0.5, settings_per_model: int | None = None,
                  sigma0: float = 0.3, min_per_model: float = 6.0, validator: Callable | None = None,
                  prior=None, is_tv: Callable[[str], bool] | None = None, plan: Plan | None = None) -> ModelChoice:
    """Rank `cands` for one slot and return them best first (refined models, then the rest).

    The budget is `budget.max_renders` in total, screen included. The screen takes at most
    `screen_frac` of it: when all fingerprint settings do not fit, each model gets fewer settings
    (default and centre first). `budget.weights` are the loss weights (default PROVISIONAL)."""
    from tonematch.search.loop import resolve_weights

    notes: list[str] = []
    cands, dropped = drop_time_variant(cands, is_tv)
    if dropped:
        notes.append("time-variant models excluded: " + ", ".join(dropped))
    if not cands:
        raise ValueError("no candidate models")
    plan = plan or Plan("native", budget.max_renders, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    weights = resolve_weights(budget.weights)
    total = float(budget.max_renders)
    avail = max(len(c.settings) or 1 for c in cands)
    if settings_per_model is None:
        afford = int(screen_frac * total // len(cands))
        settings_per_model = max(1, min(avail, afford))
    scores, n_screen, sn = screen(engine, kit, section, cands, weights, budget.clip_s, settings_per_model, validator)
    notes += sn
    by_model = {c.model: c for c in cands}
    spent, hn = halve(engine, kit, section, by_model, scores, budget, plan, max(total - n_screen, 0.0), k, eta,
                      sigma0, min_per_model, prior, budget.seed)
    notes += hn
    refined = sorted((s for s in scores if s.refined is not None), key=lambda s: s.refined)
    others = sorted((s for s in scores if s.refined is None), key=lambda s: s.screen)
    return ModelChoice(refined + others, n_screen + spent, n_screen, notes)


def candidate_from_block(model: str, category: str, preset, space: ParamSpace,
                         fp_set: dict | None, cap: int | None = None) -> ModelCandidate:
    """A candidate whose fingerprint settings come from `native_library.fingerprint_set` output."""
    return ModelCandidate(model, category, preset, space, fingerprint_settings(model, fp_set, cap))
