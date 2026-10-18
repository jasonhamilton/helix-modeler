"""Amp specialist (amp and preamp family; DECISIONS 2026-10-17, "a specialist per effect family").

Why a specialist. Gate 2 attempt 1 found the right amp on 6 of 12 synthetic-v1 items; on the dev
family set (synthetic-devfam, 24 amp items) the general hybrid finds the model on 11 of 12 so far, but
the SETTINGS are poor: held-out loss 0.34 and 0.43 on two items, parameter error 0.22. Two causes were
measured (docs/notes/amp-specialist.md):

  1. The general halving spreads the refine budget over the top four models, so the model that
     matters gets about a third of it. In the broad stage the right amp led by a wide margin on 11 of
     12 items, so three of the four finalists were wasted renders.
  2. CMA-ES with about 100 renders is too few for 6 to 8 knobs that interact (Drive, Master, Sag and
     the tone stack). A Gaussian-process / expected-improvement search on the same 150 renders cut the
     held-out loss (true model fixed, 40 space-filling draws then 110 steps).

What it does (plugged into the family dispatch, `--specialists gain`):

  broad      the dispatch shares the broad draws by category probability; this specialist supplies the
             presence detector's drive and amp probabilities (`seed`), so an amp-like target gives the six
             amp models more draws and a target with no amp gives them the floor.
  finalists  `pick_finalists`: when the broad leader is an amp model, keep it and any amp model within
             `lead_ratio` of its score (at most `max_finalists`); otherwise the dispatch falls back to the
             general halving (this specialist does not touch other families).
  refine     `refine_finalists` with `BOSession`: space-filling start (`n_init`), then Gaussian-process
             expected improvement on log(loss + 0.02), Matern-5/2 with one length scale per knob. With two
             finalists each gets a short start and the better one gets the rest, keeping its data.

The refine stage is the one hook the dispatch did not have: `Dispatch.refiner` (specialists.py) and a
call in `choose_models_hybrid`, both additive and off unless a specialist returns a refiner. No new
loss term is registered here (any gain-family term needs a trust bar in DECISIONS first); the
specialist scores with the general kit.

Tried and dropped (kept out of this module): a per-model feature fingerprint (tone-stack curve, level
slope, noise floor lift) with a Gaussian-process surrogate fitted offline on a calibration clip and
inverted for the target. Across clips the features moved more than the models differ (the distance to
the true amp was not the smallest on the first item, and the inverted settings scored worse than the
best broad draw), so it neither identified the model nor seeded the settings.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from tonematch.search.loop import BAD, Evaluator
from tonematch.search.model_choice import ModelCandidate, ModelScore
from tonematch.search.types import Budget, Plan, Section

FAMILY_CATEGORIES = ("amp", "preamp")
U_LO, U_HI = 0.05, 0.95  # space-filling draws stay off the knob ends
LOG_EPS = 0.02  # BO works on log(loss + LOG_EPS): the useful range spans two decades


# ------------------------------------------------------------------ Bayesian optimisation session


def _lhs(n: int, d: int, rng: np.random.Generator) -> np.ndarray:
    from tonematch.search.hybrid import latin_hypercube

    return U_LO + (U_HI - U_LO) * latin_hypercube(n, d, rng)


def expected_improvement(mu: np.ndarray, sd: np.ndarray, best: float) -> np.ndarray:
    """EI for minimisation of a Gaussian predictive N(mu, sd^2) against the best value so far."""
    from scipy.stats import norm

    sd = np.maximum(sd, 1e-9)
    z = (best - mu) / sd
    return (best - mu) * norm.cdf(z) + sd * norm.pdf(z)


@dataclass
class BOSession:
    """Gaussian-process expected-improvement search on one model, in the unit cube of its space.

    The session keeps every (point, loss) so a second phase can continue from a first (the halving between
    two finalists). `ev` is an `Evaluator` bound to the model's space; `activate()` loads the model into
    the engine and resets the evaluator (called before each phase, so sessions can interleave)."""

    ev: Evaluator
    activate: Callable[[], None]
    rng: np.random.Generator
    n_cand: int = 2500
    n_local: int = 300
    X: list[np.ndarray] = field(default_factory=list)
    F: list[float] = field(default_factory=list)
    _gp: object | None = None
    _steps: int = 0

    @property
    def d(self) -> int:
        return len(self.ev.space.refs)

    def evaluate(self, u: np.ndarray) -> float:
        u = np.clip(np.asarray(u, float), 0.0, 1.0)
        f, _ = self.ev.score(self.ev.space.from_unit(u))
        f = float(f) if math.isfinite(f) else BAD
        self.X.append(u)
        self.F.append(min(f, BAD))
        return f

    def best(self) -> tuple[np.ndarray, float]:
        j = int(np.argmin(self.F))
        return self.X[j], self.F[j]

    def init(self, n: int, extra: list[np.ndarray] | None = None) -> None:
        """`n` space-filling points (plus any `extra` unit-cube points)."""
        self.activate()
        for u in extra or []:
            self.evaluate(u)
        for u in _lhs(n, self.d, self.rng):
            self.evaluate(u)

    def _fit(self):
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

        X, y = np.array(self.X), np.log(np.minimum(np.array(self.F), BAD) + LOG_EPS)
        if self._gp is not None and self._steps % 4:  # hyper-parameters re-optimised every 4th step
            gp = GaussianProcessRegressor(self._gp.kernel_, optimizer=None, normalize_y=True)
        else:
            k = ConstantKernel(1.0, (1e-2, 1e2)) * Matern(
                np.full(self.d, 0.4), (0.05, 5.0), nu=2.5
            ) + WhiteKernel(1e-3, (1e-6, 1e-1))
            gp = GaussianProcessRegressor(
                k, normalize_y=True, n_restarts_optimizer=0, random_state=0
            )
        with warnings.catch_warnings():  # bound-hitting length scales are expected with few points
            warnings.simplefilter("ignore", ConvergenceWarning)
            gp.fit(X, y)
        self._gp = gp
        return gp, y

    def step(self, n: int) -> None:
        """`n` more evaluations, each at the maximiser of expected improvement."""
        self.activate()
        for _ in range(n):
            if len(self.X) < 3:
                self.evaluate(_lhs(1, self.d, self.rng)[0])
                continue
            gp, y = self._fit()
            top = np.array(self.X)[np.argsort(y)[:5]]
            cand = np.vstack(
                [U_LO + (U_HI - U_LO) * self.rng.random((self.n_cand, self.d))]
                + [
                    np.clip(t + 0.08 * self.rng.standard_normal((self.n_local, self.d)), U_LO, U_HI)
                    for t in top
                ]
            )
            mu, sd = gp.predict(cand, return_std=True)
            self.evaluate(cand[int(np.argmax(expected_improvement(mu, sd, float(y.min()))))])
            self._steps += 1


# ------------------------------------------------------------------ the specialist


@dataclass
class AmpSpecialist:
    """Family specialist for the gain family's amp and preamp models, in the shape the family dispatch
    expects (docs/notes/family-specialists.md): `family`, `categories`, `budget_share`, `screen_hint`,
    `seed`, `score`, plus `refiner`, the one hook this specialist adds (see `Dispatch.refiner`)."""

    family: str = "gain"
    categories: tuple[str, ...] = ("drive", "amp")
    budget_share: float = (
        1.0  # relative weight in the broad stage; the category probabilities do the work
    )
    lead_ratio: float = 1.3  # a second finalist must score within this factor of the leader
    max_finalists: int = 2
    n_init: int = 30  # space-filling start of a lone finalist
    n_init_shared: int = 22  # ... per finalist when two share the budget
    probs_fn: Callable | None = (
        None  # (target, dry) -> {category: probability}; default: the presence detector
    )

    # -- the dispatch surface --------------------------------------------
    def _probs(self, target: np.ndarray, dry: np.ndarray) -> dict[str, float]:
        if self.probs_fn is not None:
            return {k: float(v) for k, v in self.probs_fn(target, dry).items()}
        from tonematch.metrics import presence

        return {k: float(v) for k, v in presence.detect(target, dry).probabilities().items()}

    def screen_hint(self, target: np.ndarray, dry: np.ndarray) -> float:
        """Probability the target is a gain-family block (drive or amp), from the presence detector."""
        p = self._probs(target, dry)
        return 1.0 - (1.0 - p.get("drive", 0.0)) * (1.0 - p.get("amp", 0.0))

    def seed(self, target: np.ndarray, dry: np.ndarray):
        """The category probabilities for drive and amp (the broad stage shares its draws by them). No knob
        seeds: a single measured quantity does not pin an amp's interacting gain knobs, and a per-model
        feature fingerprint did not transfer between clips (module docstring)."""
        from tonematch.search.specialists import FamilySeed

        p = self._probs(target, dry)
        return FamilySeed(
            self.family,
            category_probs={c: p[c] for c in ("drive", "amp") if c in p},
            notes=[
                "gain: presence probabilities "
                + ", ".join(f"{c} {p.get(c, float('nan')):.2f}" for c in ("drive", "amp"))
            ],
            detail={"probs": {c: p.get(c) for c in ("drive", "amp")}},
        )

    def score(self, target: np.ndarray, dry: np.ndarray):
        """The general score (None): no gain-family term is registered, it would need a trust bar first."""
        return None  # noqa: RET501

    def refiner(self, target: np.ndarray, dry: np.ndarray):
        """The refine stage: `refine(...)` or, for a leader outside the family, None (general halving)."""

        def refine(engine, kit, section, by_model, scores, budget, plan, renders, prior=None):
            finalists = self.pick_finalists(scores)
            if not finalists:
                return None
            spent, notes = refine_finalists(
                engine, kit, section, by_model, finalists, budget, plan, self, renders, prior
            )
            return spent, [f"amp specialist: finalists {[s.model for s in finalists]}"] + notes

        return refine

    # -- stages ----------------------------------------------------------
    def pick_finalists(self, scores: list[ModelScore]) -> list[ModelScore]:
        """The models to refine, best first: the broad leader if it is an amp model, plus amp models within
        `lead_ratio` of it (up to `max_finalists`). Empty when the leader is not an amp (the caller falls
        back to the general halving, so a drive or any other family is not touched)."""
        live = sorted(
            (s for s in scores if math.isfinite(s.screen) and s.screen < BAD),
            key=lambda s: s.screen,
        )
        if not live or live[0].category not in FAMILY_CATEGORIES:
            return []
        lead = live[0]
        out = [lead]
        for s in live[1:]:
            if len(out) >= self.max_finalists:
                break
            if s.category in FAMILY_CATEGORIES and s.screen <= self.lead_ratio * max(
                lead.screen, 1e-3
            ):
                out.append(s)
        return out


def make_specialist(**kw) -> AmpSpecialist:
    return AmpSpecialist(**kw)


def refine_finalists(
    engine,
    kit,
    section: Section,
    by_model: dict[str, ModelCandidate],
    finalists: list[ModelScore],
    budget: Budget,
    plan: Plan,
    spec: AmpSpecialist,
    renders: float,
    prior=None,
) -> tuple[float, list[str]]:
    """Bayesian-optimisation refinement of 1 to `max_finalists` models with `renders` in total.

    One finalist: `n_init` space-filling points (and its best broad draw, re-scored with the guard) then
    expected improvement for the rest. Two: each gets `n_init_shared` points, the better one (lower best
    loss) continues with the remaining budget on the same data. Mutates `finalists` (refined, values,
    rungs). Returns (renders spent, notes)."""
    notes: list[str] = []
    sessions: dict[str, BOSession] = {}
    spent = 0.0
    n_init = spec.n_init if len(finalists) == 1 else spec.n_init_shared
    for j, s in enumerate(finalists):
        c = by_model[s.model]
        ev = Evaluator(engine, kit, section, c.space, plan, budget, prior)

        def activate(c=c, ev=ev, s=s):
            engine.load(c.preset_at(dict(zip(c.space.refs, c.start_vector(s.start), strict=True))))
            ev.reset_applied()

        warm = c.space.to_unit(c.start_vector(s.start))
        sess = BOSession(ev, activate, np.random.default_rng([budget.seed, 91, j]))
        init = max(1, min(n_init, int(renders / max(len(finalists), 1)) - 1))
        sess.init(init, [warm])
        sessions[s.model] = sess
        spent += ev.spent
        u, f = sess.best()
        s.refined, s.values = f, c.space.from_unit(u)
        s.rungs.append(
            {
                "rung": 0,
                "survivors": len(finalists),
                "renders": round(ev.spent, 1),
                "score": float(f),
            }
        )
    order = sorted(finalists, key=lambda s: s.refined)
    win = order[0]
    sess = sessions[win.model]
    before = sess.ev.spent
    sess.step(int(max(renders - spent, 0.0)))
    spent += sess.ev.spent - before
    u, f = sess.best()
    win.refined, win.values = f, by_model[win.model].space.from_unit(u)
    win.rungs.append(
        {"rung": 1, "survivors": 1, "renders": round(sess.ev.spent - before, 1), "score": float(f)}
    )
    if len(order) > 1:
        notes.append(
            f"second finalist {order[1].model}: {order[1].refined:.3f} vs {win.refined:.3f} at the start"
        )
    return spent, notes


__all__ = [
    "AmpSpecialist",
    "BOSession",
    "expected_improvement",
    "make_specialist",
    "refine_finalists",
]
