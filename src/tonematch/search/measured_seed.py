"""Measured seeding before screening (T113; known-DI mode).

The default-setting screen ranks time-based models badly: a delay at the wrong time, a reverb at the
wrong decay or a tremolo at the wrong rate is far from the target whatever model it is. But the
target and its DI are both known, so the time-like quantities can be MEASURED, not searched:

  measure(target, dry)            echo time / feedback / level, reverb RT60 / pre-delay / late level,
                                  LFO rate / depth, and the presence probabilities (T102, T066)
  calibrate_model(render, ...)    per model and knob: render the knob at a few values, measure the same
                                  quantity, keep the (quantity, knob) curve. Library preparation, done once
                                  on a DI that is in no benchmark item; cached as JSON.
  seeds_for(...)                  measurement -> knob value of one model by inverting that curve
  BandedSpace                     a ParamSpace whose seeded dimensions only span a narrow band (a fraction
                                  of the range) around the seed, so CMA-ES refines locally
  choose_models_seeded(...)       the model-choice stage with the seeds in front of the screen:

    A  one render per pool model: its catalog defaults (the old screen's first setting), or, for a model of a
       measured category, its seeds set (the seed replaces the default render);
    B  a second rung: every model gets the same number of its remaining fingerprint settings (centre, then
       corners; seeded knobs held at the seed), as many as the screen budget allows; leftover renders go to
       the top `rescreen_default` models by first score and the top `rescreen_seeded` by seeded score;
    C  successive halving over the best `k` (T050's `halve`), seeded models refined in their band.

The render budget is the same as `choose_models`: A + B take at most `screen_frac` of it, C the rest.
Pool order by corpus usage (`usage_rank`) breaks ties and orders rendering when B is cut short.
This module admits nothing; weights are the caller's.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from tonematch.search.model_choice import (
    ModelCandidate,
    ModelChoice,
    ModelScore,
    Setting,
    drop_time_variant,
    halve,
    screen,
)
from tonematch.search.space import ParamSpace
from tonematch.search.types import Budget, Plan, Section

SR = 48_000
TIGHT = 0.04        # band half-width (fraction of range) for time-like knobs: time, decay, rate, pre-delay
DECAY = 0.20       # reverb decay: seed error up to 0.14 of range on dev items (RT60 estimate depends on the DI and the mix), so a wide band
LOOSE = 0.12        # for level-like knobs (feedback, mix, depth): the calibration varies one knob at a time
MIN_PROB = 0.10     # a category is seeded when its presence probability is at least this (or unknown)
GRID = (0.1, 0.3, 0.5, 0.7, 0.9)

# Knob groups: (category, ((knob regex, quantity, band half-width), ...)). A group of two knobs is
# calibrated jointly on a grid (reverb Decay and Mix both move the measured decay and late level; delay
# Feedback and Mix both move the echo level), a group of one on a line.
GROUPS: list[tuple[str, tuple[tuple[str, str, float], ...]]] = [
    ("delay", ((r"^Time$", "delay.time_s", TIGHT),)),
    ("delay", ((r"^Feedback$", "delay.fb_db", LOOSE), (r"^Mix$", "delay.level_db", LOOSE))),
    ("reverb", ((r"^Decay$", "reverb.rt60_s", DECAY), (r"^Mix$", "reverb.late_db", LOOSE))),
    ("reverb", ((r"^Predelay$", "reverb.predelay_ms", TIGHT),)),
    ("mod", ((r"^(Speed|Rate)$", "mod.rate_hz", TIGHT),)),
    ("mod", ((r"^(Intensity|Depth)$", "mod.depth", LOOSE),)),
]
LOG_QUANTITIES = {"reverb.rt60_s"}      # compared on a log scale
QUANTITIES = ("delay.time_s", "delay.fb_db", "delay.level_db", "reverb.rt60_s", "reverb.predelay_ms",
              "reverb.late_db", "mod.rate_hz", "mod.amp_depth_db", "mod.pitch_cents")


# ------------------------------------------------------------------ measurement

@dataclass
class Measured:
    q: dict[str, float] = field(default_factory=dict)         # quantity -> value (NaN when not measurable)
    probs: dict[str, float] = field(default_factory=dict)     # category -> presence probability
    notes: list[str] = field(default_factory=list)

    def get(self, name: str) -> float:
        return float(self.q.get(name, float("nan")))


def _f(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return x if math.isfinite(x) else float("nan")


def measure(target: np.ndarray, dry: np.ndarray, *, with_probs: bool = True) -> Measured:
    """Time-like and level-like quantities of `target` against its DI `dry` (known-DI mode). Uses the
    presence detector's known-DI estimates (T102) and the pre-delay of metrics.reverb (T066)."""
    from tonematch.metrics import presence, reverb

    m = Measured()
    try:
        feats, est = presence.features_known_di(target, dry)
    except Exception as e:  # noqa: BLE001 - a failed measurement is no seed, not a crashed search
        m.notes.append(f"measure failed: {type(e).__name__}: {e}")
        return m
    d, r, md = est["delay"], est["reverb"], est["mod"]
    m.q.update({
        "delay.time_s": _f(d.get("time_s")), "delay.fb_db": _f(d.get("feedback_db")),
        "delay.level_db": _f(d.get("echo_level_db")),
        "reverb.rt60_s": _f(r.get("rt60_s")), "reverb.late_db": _f(feats.get("ir_late_db")),
        "mod.rate_hz": _f(md.get("rate_hz")), "mod.amp_depth_db": _f(md.get("amp_depth_db")),
        "mod.pitch_cents": _f(md.get("pitch_depth_cents")),
    })
    try:
        n = min(len(target), len(dry))
        m.q["reverb.predelay_ms"] = _f(reverb.predelay_ms(np.asarray(target, float)[:n], np.asarray(dry, float)[:n]))
    except Exception as e:  # noqa: BLE001
        m.q["reverb.predelay_ms"] = float("nan")
        m.notes.append(f"pre-delay not measured: {e}")
    if with_probs:
        try:
            m.probs = {k: float(v) for k, v in presence.detect(target, dry).probabilities().items()}
        except Exception as e:  # noqa: BLE001
            m.notes.append(f"presence probabilities unavailable: {e}")
    return m


# ------------------------------------------------------------------ calibration: knob <-> quantity

def groups_for(category: str, model: str, params) -> list[list[tuple[str, str, float]]]:
    """The knob groups of `model`: [(param, quantity, half-width), ...] per group, restricted to the
    knobs the model has (a two-knob group with one knob missing is a one-knob group)."""
    out = []
    for cat, items in GROUPS:
        if cat != category:
            continue
        g = []
        for rx, q, w in items:
            hit = next((p for p in params if re.match(rx, p)), None)
            if hit is not None:
                if q == "mod.depth":
                    q = "mod.amp_depth_db" if "Trem" in model else "mod.pitch_cents"
                g.append((hit, q, w))
        if g:
            out.append(g)
    return out


def calibrate_model(render: Callable[[dict[str, float]], np.ndarray], dry: np.ndarray, model: str, category: str,
                    ranges: dict[str, tuple[float, float]], defaults: dict[str, float],
                    grid: tuple[float, ...] = GRID) -> list[dict]:
    """Calibration of every knob group of `model`: render with the group's knobs on a grid of fractions
    of their ranges (others at `defaults`) and measure the group's quantities. `render(values)` returns
    the audio of the model with those knob values on `dry`. Returns one dict per group:
    {knobs, quantities, half_width, range, x: [[knob values]], q: [[measurements]], shape}."""
    out = []
    for g in groups_for(category, model, ranges):
        knobs = [p for p, _, _ in g]
        qs = [q for _, q, _ in g]
        rng = [list(map(float, ranges[p])) for p in knobs]
        pts = [[]]
        for r in rng:
            pts = [pt + [r[0] + u * (r[1] - r[0])] for pt in pts for u in grid]
        xs, ms = [], []
        for pt in pts:
            y = render({**defaults, **dict(zip(knobs, pt, strict=True))})
            m = measure(y, dry, with_probs=False)
            xs.append([float(v) for v in pt])
            ms.append([m.get(q) for q in qs])
        out.append({"knobs": knobs, "quantities": qs, "half_width": [w for _, _, w in g], "range": rng,
                    "x": xs, "q": ms, "shape": [len(grid)] * len(knobs)})
    return out


def _tq(q: str, v):
    v = np.asarray(v, float)
    return np.log(np.maximum(v, 1e-6)) if q in LOG_QUANTITIES else v


def invert(group: dict, values: list[float], min_rho: float = 0.7) -> list[float] | None:
    """Knob values of a calibrated group whose quantities equal `values` (the measurement). One knob:
    linear interpolation of the curve, clipped to the calibrated span; refused when the curve is not
    informative (under 3 finite points, flat, rank correlation under `min_rho`). Two knobs: the
    bilinear surface cell that best matches all quantities (each scaled by its spread over the grid).
    None when a quantity is not finite or the calibration is uninformative."""
    qn = group["quantities"]
    if len(values) != len(qn) or not all(math.isfinite(v) for v in values):
        return None
    X = np.array(group["x"], float)
    Q = np.array([[_tq(q, v) for q, v in zip(qn, row, strict=True)] for row in group["q"]], float)
    meas = np.array([_tq(q, v) for q, v in zip(qn, values, strict=True)], float)
    if X.shape[1] == 1:
        ok = np.isfinite(Q[:, 0])
        if ok.sum() < 3:
            return None
        qs, xs = Q[ok, 0], X[ok, 0]
        o = np.argsort(qs)
        qs, xs = qs[o], xs[o]
        if qs[-1] - qs[0] <= 1e-9:
            return None
        rq, rx = np.argsort(np.argsort(qs)).astype(float), np.argsort(np.argsort(xs)).astype(float)
        if np.std(rx) < 1e-9 or abs(float(np.corrcoef(rq, rx)[0, 1])) < min_rho:
            return None
        return [float(np.interp(meas[0], qs, xs))]
    n1, n2 = group["shape"]
    G = Q.reshape(n1, n2, -1)
    X1, X2 = X[:, 0].reshape(n1, n2), X[:, 1].reshape(n1, n2)
    spread = np.array([np.nanmax(G[..., k]) - np.nanmin(G[..., k]) if np.isfinite(G[..., k]).any() else 0.0
                       for k in range(G.shape[2])])
    use = spread > 1e-9
    if not use.any():
        return None
    s = np.linspace(0.0, 1.0, 21)
    S, T = np.meshgrid(s, s, indexing="ij")
    best, arg = np.inf, None
    for i in range(n1 - 1):
        for j in range(n2 - 1):
            c = G[i:i + 2, j:j + 2, :]
            if not np.isfinite(c).all():
                continue
            surf = ((1 - S)[..., None] * (1 - T)[..., None] * c[0, 0] + (1 - S)[..., None] * T[..., None] * c[0, 1]
                    + S[..., None] * (1 - T)[..., None] * c[1, 0] + S[..., None] * T[..., None] * c[1, 1])
            cost = np.sum(((surf - meas) / np.where(use, spread, 1.0)) ** 2 * use, axis=-1)
            k = np.unravel_index(int(np.argmin(cost)), cost.shape)
            if cost[k] < best:
                best, arg = float(cost[k]), (i, j, float(S[k]), float(T[k]))
    if arg is None:
        return None
    i, j, a, b = arg
    x1 = (1 - a) * X1[i, j] + a * X1[i + 1, j]
    x2 = (1 - b) * X2[i, j] + b * X2[i, j + 1]
    return [float(x1), float(x2)]


def save_calibration(cal: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(cal, indent=1))


def load_calibration(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class Seed:
    value: float
    half_width: float          # fraction of the knob's range
    quantity: str
    measured: float


def seeds_for(model: str, category: str, cal: dict, meas: Measured,
              ranges: dict[str, tuple[float, float]]) -> dict[str, Seed]:
    """Knob seeds of one model from the measurement and the model's calibration groups."""
    out = {}
    cg = cal.get(model)
    for group in cg if isinstance(cg, list) else []:
        if any(p not in ranges for p in group["knobs"]):
            continue
        vals = [meas.get(q) for q in group["quantities"]]
        v = invert(group, vals)
        if v is None:
            continue
        for p, x, q, w, m in zip(group["knobs"], v, group["quantities"], group["half_width"], vals, strict=True):
            lo, hi = ranges[p]
            out[p] = Seed(float(min(max(x, lo), hi)), float(w), q, float(m))
    return out


def category_seeded(category: str, probs: dict[str, float]) -> bool:
    """Seed a category when the presence model does not rule it out (or there is no presence model)."""
    return category in {c for c, _ in GROUPS} and (not probs or probs.get(category, 1.0) >= MIN_PROB)


# ------------------------------------------------------------------ banded parameter space

@dataclass
class BandedSpace(ParamSpace):
    """ParamSpace whose unit cube maps to [band_lo, band_hi] (inside [lo, hi]) per dimension. A seeded
    dimension has a narrow band; the others the full range. Values still snap to the full-range grid."""
    band_lo: np.ndarray | None = None
    band_hi: np.ndarray | None = None

    @classmethod
    def around(cls, space: ParamSpace, seeds: dict[int, tuple[float, float]]) -> BandedSpace:
        """`seeds`: dimension index -> (centre value, half-width as a fraction of the range)."""
        b_lo, b_hi = space.lo.copy(), space.hi.copy()
        for j, (c, w) in seeds.items():
            span = space.hi[j] - space.lo[j]
            b_lo[j], b_hi[j] = max(space.lo[j], c - w * span), min(space.hi[j], c + w * span)
        return cls(list(space.refs), space.start.copy(), space.lo.copy(), space.hi.copy(), list(space.categories),
                   space.steps, b_lo, b_hi)

    def to_unit(self, v):
        w = np.maximum(self.band_hi - self.band_lo, 1e-12)
        return np.clip((np.asarray(v, float) - self.band_lo) / w, 0, 1)

    def from_unit(self, u) -> np.ndarray:
        v = self.band_lo + np.clip(u, 0, 1) * (self.band_hi - self.band_lo)
        out = v.copy()
        for j, r in enumerate(self.refs):
            st = self._steps_for(r)
            if st and st > 1:
                span = max(self.hi[j] - self.lo[j], 1e-12)
                g = (v[j] - self.lo[j]) / span
                out[j] = self.lo[j] + np.round(g * (st - 1)) / (st - 1) * span
        return np.clip(out, self.lo, self.hi)


# ------------------------------------------------------------------ pool order by corpus usage

def usage_counts(path: str | Path) -> dict[str, float]:
    """model id -> corpus block count from model_usage.json (local report); empty when absent."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, float] = {}
    for cat in doc.values():
        for row in cat.get("ranked", []) if isinstance(cat, dict) else []:
            out[row[0]] = out.get(row[0], 0.0) + float(row[1])
    return out


def usage_rank(models: list[str], counts: dict[str, float]) -> dict[str, int]:
    """Rank of each model by corpus usage, most used first (0); unused models share the last ranks in
    the order given."""
    order = sorted(range(len(models)), key=lambda i: (-counts.get(models[i], 0.0), i))
    return {models[i]: r for r, i in enumerate(order)}


# ------------------------------------------------------------------ seeded candidates

def seeded_candidate(c: ModelCandidate, seeds: dict[str, Seed]) -> ModelCandidate | None:
    """`c` with a BandedSpace around the seeds and every setting overridden by them; None without seeds.
    The first setting is the seed alone (all other knobs at the library defaults)."""
    idx = {r[1]: j for j, r in enumerate(c.space.refs) if r[0] == 0}
    seeds = {p: s for p, s in seeds.items() if p in idx}
    if not seeds:
        return None
    sp = BandedSpace.around(c.space, {idx[p]: (s.value, s.half_width) for p, s in seeds.items()})
    over = {(0, p): s.value for p, s in seeds.items()}
    sp.start = sp.start.copy()
    for p, s in seeds.items():
        sp.start[idx[p]] = s.value
    settings = [Setting("seed", dict(over))]
    for st in c.settings:
        if st.label == "default":
            continue
        settings.append(Setting(st.label + "+seed", {**st.values, **over}))
    return ModelCandidate(c.model, c.category, c.preset, sp, settings)


def _by_screen(scores: list[ModelScore], rank: dict[str, int]) -> list[ModelScore]:
    return sorted(scores, key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))


def choose_models_seeded(section: Section, engine, kit, cands: list[ModelCandidate], budget: Budget,
                         meas: Measured, cal: dict, ranges: dict[str, dict[str, tuple[float, float]]], *,
                         k: int = 3, eta: int = 2, screen_frac: float = 0.5, rescreen_default: int = 12,
                         rescreen_seeded: int = 4, sigma0: float = 0.3, min_per_model: float = 6.0,
                         usage: dict[str, int] | None = None, validator: Callable | None = None,
                         prior=None, is_tv: Callable[[str], bool] | None = None,
                         plan: Plan | None = None) -> ModelChoice:
    """Model choice with measured seeds in front of the screen (module docstring: stages A, B, C).
    `ranges[model]` maps each free knob to its (lo, hi); `cal` is `load_calibration`; the budget is
    `budget.max_renders` in total, same as `choose_models`."""
    from tonematch.search.loop import resolve_weights

    notes: list[str] = list(meas.notes)
    cands, dropped = drop_time_variant(cands, is_tv)
    if dropped:
        notes.append("time-variant models excluded: " + ", ".join(dropped))
    if not cands:
        raise ValueError("no candidate models")
    plan = plan or Plan("native", budget.max_renders, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    weights = resolve_weights(budget.weights)
    total = float(budget.max_renders)
    rank = usage or {c.model: i for i, c in enumerate(cands)}
    cands = sorted(cands, key=lambda c: rank.get(c.model, 10 ** 6))      # render in corpus-usage order

    # seeds
    sc_of: dict[str, ModelCandidate] = {}
    seed_info: dict[str, dict] = {}
    for c in cands:
        if not category_seeded(c.category, meas.probs):
            continue
        sd = seeds_for(c.model, c.category, cal, meas, ranges.get(c.model, {}))
        sd_c = seeded_candidate(c, sd) if sd else None
        if sd_c is not None:
            sc_of[c.model] = sd_c
            seed_info[c.model] = {p: {"value": s.value, "from": s.quantity, "measured": s.measured} for p, s in sd.items()}
    notes.append(f"seeded {len(sc_of)} of {len(cands)} models from the measurement")

    # A: one render per model: the catalog defaults, or the seeds for a seeded model
    unseeded = [replace(c, settings=[Setting("default", {})]) for c in cands if c.model not in sc_of]
    s_def, n_def, nn = screen(engine, kit, section, unseeded, weights, budget.clip_s, 1, validator) \
        if unseeded else ([], 0, [])
    notes += nn
    seeded_list = list(sc_of.values())
    s_seed_list: list[ModelScore] = []
    n_seed = 0
    if seeded_list:
        first = [replace(c, settings=c.settings[:1]) for c in seeded_list]
        s_seed_list, n_seed, nn = screen(engine, kit, section, first, weights, budget.clip_s, 1, validator)
        notes += nn
    seed_score = {s.model: s for s in s_seed_list}
    default_score = {s.model: s for s in s_def}

    # B: the rest of the fingerprint settings (seeded knobs held at the seed). Every model gets the same
    # number, as many as the room allows; what is left over goes, one setting at a time, to the best
    # `rescreen_default` models by their first score and the best `rescreen_seeded` by their seeded score.
    spent = n_def + n_seed
    room = max(int(screen_frac * total) - spent, 0)
    first_score = {m: s.screen for m, s in {**default_score, **seed_score}.items()}
    prio = [m for m, _ in sorted(first_score.items(), key=lambda kv: (kv[1], rank.get(kv[0], 10 ** 6)))[:rescreen_default]]
    prio += [s.model for s in _by_screen(s_seed_list, rank)[:rescreen_seeded]]
    prio = list(dict.fromkeys(prio))
    rest_of: dict[str, list[Setting]] = {}
    for c in cands:
        src = sc_of.get(c.model, c)
        rest_of[c.model] = [st for st in src.settings if st.label not in ("default", "seed")]
    n_models = max(sum(1 for r in rest_of.values() if r), 1)
    per = min(max((len(r) for r in rest_of.values()), default=0), room // n_models)
    take = {m: per for m in rest_of}
    left = room - sum(min(per, len(r)) for r in rest_of.values())
    for m in prio + [c.model for c in cands]:
        if left <= 0:
            break
        if take[m] < len(rest_of[m]):
            take[m] += 1
            left -= 1
    jobs = [replace(sc_of.get(c.model, c), settings=rest_of[c.model][:take[c.model]]) for c in cands
            if rest_of[c.model][:take[c.model]]]
    extra_scores: dict[str, dict[str, float]] = {}
    extra_start: dict[str, tuple[float, str, dict]] = {}
    if jobs:
        s_b, n_b, nn = screen(engine, kit, section, jobs, weights, budget.clip_s, None, validator)
        notes += nn
        spent += n_b
        for s in s_b:
            extra_scores[s.model] = dict(s.scores)
            extra_start[s.model] = (s.screen, s.setting, dict(s.start))

    # merge: one ModelScore per model; the start comes from the seeded family when the model was seeded
    scores: list[ModelScore] = []
    for c in cands:
        m = c.model
        ms = ModelScore(m, c.category)
        d = default_score.get(m)
        if d is not None:
            ms.scores["default"] = d.screen
            ms.screen, ms.setting, ms.start = d.screen, "default", {}
        fam: list[tuple[float, str, dict]] = []
        if m in seed_score:
            sd = seed_score[m]
            ms.scores["seed"] = sd.screen
            fam.append((sd.screen, "seed", dict(sc_of[m].settings[0].values)))
        if m in extra_start:
            ms.scores.update(extra_scores[m])
            fam.append(extra_start[m])
        if fam:
            f, lab, st = min(fam, key=lambda t: t[0])
            if m in sc_of:
                ms.start = st      # refine from the best seeded setting, inside the band
                ms.setting = lab
                ms.screen = min(ms.screen, f)
            elif f < ms.screen:
                ms.screen, ms.setting, ms.start = f, lab, st
        scores.append(ms)
    scores.sort(key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))

    by_model = {c.model: sc_of.get(c.model, c) for c in cands}
    hs, hn = halve(engine, kit, section, by_model, scores, budget, plan, max(total - spent, 0.0), k, eta,
                   sigma0, min_per_model, prior, budget.seed)
    notes += hn
    refined = sorted((s for s in scores if s.refined is not None), key=lambda s: s.refined)
    others = sorted((s for s in scores if s.refined is None), key=lambda s: (s.screen, rank.get(s.model, 10 ** 6)))
    ch = ModelChoice(refined + others, spent + hs, spent, notes)
    ch.seeds = seed_info                                    # type: ignore[attr-defined]
    ch.measured = dict(meas.q)                              # type: ignore[attr-defined]
    ch.rungs = {"A_default": n_def, "A_seed": n_seed, "B": spent - n_def - n_seed, "C": hs}  # type: ignore[attr-defined]
    return ch

