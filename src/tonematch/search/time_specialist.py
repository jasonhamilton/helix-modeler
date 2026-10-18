"""Time-based specialist (delay, reverb): the family dispatch's specialist for the `time` family
(docs/notes/family-specialists.md; human decision 2026-10-17 "a specialist per effect family").

What it supplies to the dispatch (`tonematch.search.specialists`):

  screen_hint(target, dry)   probability the target is a delay or a reverb (and, in `seed().category_probs`, how
                             that splits into delay, reverb and mod: a target that is neither carries "other")
  seed(target, dry)          knob estimates for every delay and reverb model of the pool, from per-model
                             regressors fitted on renders of that model with ALL knobs varied (library
                             preparation, scripts/time_specialist_fit.py), with a band width from each
                             regressor's own leave-one-out error
  score(target, dry)         the family score: the general score plus an echo-pattern and decay-curve distance
                             between the target's impulse response and the candidate's

How it tells the three apart (time_features.py): a delay leaves a few discrete peaks in the impulse response
with silence between them; a reverb a smooth decay whose echo density rises; a modulation leaves the linear
part unexplained. A fitted classifier (data/time_specialist/classifier.json) weighs those measurements; without
it a rule on the same measurements is used.

Measurements the seeds reuse: metrics.delay / reverb / presence (via `measured_seed.measure`) and the
calibration of `measured_seed` for models that have no fitted regressor.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tonematch.search.measured_seed import Measured, Seed, seeds_for
from tonematch.search.specialists import FamilySeed
from tonematch.search.time_features import (
    BLK_S,
    PROBE_KEYS,
    IRView,
    decay_curve,
    echo_pattern,
    ir_analysis,
    probe_features,
    shape_features,
)

ROOT = Path(__file__).resolve().parents[3]
CLASSIFIER_PATH = ROOT / "data" / "time_specialist" / "classifier.json"
SEEDS_PATH = ROOT / "cache" / "time-specialist" / "seeds.json"
CALIBRATION_PATH = ROOT / "cache" / "measured-seed" / "calibration.json"
CLASSES = ("delay", "reverb", "mod", "other")
SHAPE_KEYS = ("late_db", "span_s", "conc", "valley_db", "line_resid_db", "comb", "early_gap_db", "peaks_per_s",
              "slope_db_s", "dens_rise")
PRESENCE_KEYS = ("lin_resid_db", "nl_resid_db", "level_slope", "gain_swing_db", "gain_period_conf", "ir_late_db",
                 "ir_echo_prom_db", "ir_echo_level_db", "ir_tail_rt60_s", "ir_tail_smooth_db", "mod_rate_conf",
                 "mod_amp_depth_db", "mod_sweep_depth_db", "mod_pitch_cents", "e_late_db", "e_resid_db",
                 "e_echo_prom_db", "e_echo_level_db", "e_smooth_db", "e_rt60_s", "null_db")
PRESENCE_PROBE = ("ir_tail_rt60_s", "e_rt60_s", "e_late_db", "e_echo_prom_db", "e_echo_level_db", "ir_late_db", "e_smooth_db",
                  "ir_echo_level_db", "mod_rate_conf")
FEATURE_KEYS = SHAPE_KEYS + ("n_echo",) + PRESENCE_KEYS
ALL_PROBE_KEYS = PROBE_KEYS + tuple("p_" + k for k in PRESENCE_PROBE)


# ------------------------------------------------------------------------------------------ evidence

@dataclass
class Evidence:
    ir: IRView
    shape: dict[str, float]
    pattern: dict[str, list[float]]
    probe: dict[str, float]
    presence: dict[str, float]
    probs: dict[str, float] = field(default_factory=dict)

    def vector(self) -> dict[str, float]:
        f = {**self.shape, "n_echo": float(len(self.pattern["t"])), **self.presence}
        return {k: float(f.get(k, float("nan"))) for k in FEATURE_KEYS}


def analyse(target: np.ndarray, dry: np.ndarray, *, with_presence: bool = True) -> Evidence:
    ir = ir_analysis(target, dry)
    shape = shape_features(ir)
    pat = echo_pattern(ir)
    pres: dict[str, float] = {}
    if with_presence:
        from tonematch.metrics import presence

        try:
            feats, _ = presence.features_known_di(target, dry)
            pres = {k: float(feats[k]) for k in PRESENCE_KEYS if k in feats}
        except Exception:  # noqa: BLE001 - no presence features is a weaker classification, not a crash
            pres = {}
    probe = probe_features(ir, shape, pat)
    probe.update({"p_" + k: float(pres.get(k, float("nan"))) for k in PRESENCE_PROBE})
    return Evidence(ir, shape, pat, probe, pres)


# --------------------------------------------------------------------------------------- classifier

def rule_probs(f: dict[str, float]) -> dict[str, float]:
    """Hand rule on the same measurements (used when no fitted classifier exists). Soft, sums to 1."""
    def g(k, d):
        v = f.get(k, float("nan"))
        return d if not math.isfinite(v) else v

    late, conc, resid = g("late_db", -60.0), g("conc", 0.2), g("line_resid_db", 5.0)
    lin = g("lin_resid_db", -20.0)
    z_wet = 1 / (1 + math.exp(-(late + 25.0) / 4.0))            # is there a late response at all
    z_echo = 1 / (1 + math.exp(-((conc - 0.30) / 0.08 + (resid - 6.0) / 2.0)))   # discrete rather than smooth
    z_lin = 1 / (1 + math.exp(-(-12.0 - lin) / 3.0))            # the linear part explains the target
    p_delay = z_wet * z_lin * z_echo
    p_rev = z_wet * z_lin * (1 - z_echo)
    p_mod = (1 - z_lin)
    p_other = max(1e-3, 1 - p_delay - p_rev - p_mod)
    s = p_delay + p_rev + p_mod + p_other
    out = {"delay": p_delay / s, "reverb": p_rev / s, "mod": p_mod / s, "other": p_other / s}
    out["time"] = out["delay"] + out["reverb"]
    return out


@dataclass
class Classifier:
    """Multinomial logistic regression on standardised features (NaN -> training median, plus one missing flag)."""
    keys: tuple[str, ...]
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray            # (classes, 2 * len(keys))
    intercept: np.ndarray
    fam_coef: np.ndarray | None = None       # binary head: time family (delay or reverb) against everything else
    fam_intercept: float = 0.0

    def _x(self, f: dict[str, float]) -> np.ndarray:
        v = np.array([f.get(k, float("nan")) for k in self.keys], float)
        miss = ~np.isfinite(v)
        v = np.where(miss, self.median, v)
        return np.concatenate([(v - self.mean) / self.scale, miss.astype(float)])

    def proba(self, f: dict[str, float]) -> dict[str, float]:
        x = self._x(f)
        z = self.coef @ x + self.intercept
        z -= z.max()
        p = np.exp(z)
        p /= p.sum()
        out = {c: float(v) for c, v in zip(CLASSES, p, strict=True)}
        if self.fam_coef is not None:
            # the family question has its own head (it is the better detector of "is there a delay or reverb at all":
            # a modulated echo is a delay to this head and may be a mod to the four-way one). The four-way head
            # splits the family into delay / reverb and the rest into mod / other.
            pt = float(1.0 / (1.0 + np.exp(-(self.fam_coef @ x + self.fam_intercept))))
            d, r, m, o = (out[c] for c in CLASSES)
            ratio = d / (d + r) if d + r > 1e-9 else 0.5
            qm = m / (m + o) if m + o > 1e-9 else 0.5
            out = {"delay": pt * ratio, "reverb": pt * (1 - ratio), "mod": (1 - pt) * qm, "other": (1 - pt) * (1 - qm)}
        out["time"] = out["delay"] + out["reverb"]
        return out

    def to_json(self) -> dict:
        d = {"keys": list(self.keys), "median": self.median.tolist(), "mean": self.mean.tolist(),
             "scale": self.scale.tolist(), "coef": self.coef.tolist(), "intercept": self.intercept.tolist(),
             "classes": list(CLASSES)}
        if self.fam_coef is not None:
            d.update({"fam_coef": np.asarray(self.fam_coef).tolist(), "fam_intercept": float(self.fam_intercept)})
        return d

    @classmethod
    def from_json(cls, d: dict) -> Classifier:
        return cls(tuple(d["keys"]), np.array(d["median"]), np.array(d["mean"]), np.array(d["scale"]),
                   np.array(d["coef"]), np.array(d["intercept"]),
                   np.array(d["fam_coef"]) if "fam_coef" in d else None, float(d.get("fam_intercept", 0.0)))


def load_classifier(path: Path = CLASSIFIER_PATH) -> Classifier | None:
    try:
        return Classifier.from_json(json.loads(Path(path).read_text()))
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------------------- seed models

@dataclass
class SeedModel:
    """Posterior-mean regressor from probe features to the normalised (0..1) value of each knob of one model:
    a Gaussian process with a radial kernel plus a linear kernel (a delay time follows the echo time linearly
    and the radial part takes up the rest), on the features picked by forward selection. `loo` is the
    leave-one-out RMS error of each knob (normalised units), which sets the seed's band."""
    model: str
    category: str
    knobs: list[str]
    keys: list[str]                      # probe features used
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    length: float                        # radial kernel length scale (on standardised features)
    amp: float                           # radial kernel amplitude
    lin: float                           # linear kernel variance
    noise: float
    X: np.ndarray                        # (n, d) standardised training features
    alpha: np.ndarray                    # (n, knobs)
    ymean: np.ndarray
    loo: np.ndarray                      # (knobs,)
    spread: np.ndarray                   # training std of each knob (normalised)

    def _x(self, f: dict[str, float]) -> np.ndarray:
        v = np.array([f.get(k, float("nan")) for k in self.keys], float)
        v = np.where(np.isfinite(v), v, self.median)
        return (v - self.mean) / self.scale

    def predict(self, f: dict[str, float]) -> np.ndarray:
        x = self._x(f)
        d2 = ((self.X - x) ** 2).sum(1)
        k = self.amp * np.exp(-0.5 * d2 / self.length ** 2) + self.lin * (self.X @ x)
        return np.clip(self.ymean + k @ self.alpha, 0.0, 1.0)

    def to_json(self) -> dict:
        return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, d: dict) -> SeedModel:
        arr = ("median", "mean", "scale", "X", "alpha", "ymean", "loo", "spread")
        return cls(**{k: (np.array(v) if k in arr else v) for k, v in d.items()})


def _kernel(Zc: np.ndarray, ls: float, amp: float, lin: float) -> np.ndarray:
    d2 = ((Zc[:, None, :] - Zc[None, :, :]) ** 2).sum(-1)
    return amp * np.exp(-0.5 * d2 / ls ** 2) + lin * (Zc @ Zc.T)


def fit_seed_model(model: str, category: str, knobs: list[str], F: list[dict[str, float]], Y: np.ndarray,
                   keys: tuple[str, ...] = ALL_PROBE_KEYS, max_features: int = 6) -> SeedModel:
    """Fit one model's regressor. Features are chosen by forward selection on leave-one-out error (summed over
    knobs); the kernel's length, amplitude, linear variance and the noise are picked from a small grid by the same
    criterion. Pure numpy."""
    V = np.array([[f.get(k, float("nan")) for k in keys] for f in F], float)
    ok_cols = [j for j in range(V.shape[1]) if np.isfinite(V[:, j]).sum() >= max(10, 0.5 * len(V))]
    keys = tuple(keys[j] for j in ok_cols)
    V = V[:, ok_cols]
    med = np.nanmedian(V, axis=0)
    V = np.where(np.isfinite(V), V, med)
    mu, sd = V.mean(0), V.std(0) + 1e-9
    Z = (V - mu) / sd
    ym = Y.mean(0)
    Yc = Y - ym
    n = len(Z)

    def loo_err(cols: list[int], hp: tuple[float, float, float, float]) -> tuple[float, np.ndarray]:
        ls, amp, lin, noise = hp
        K = _kernel(Z[:, cols], ls * math.sqrt(len(cols)), amp, lin) + noise * np.eye(n)
        Ki = np.linalg.inv(K)
        r = (Ki @ Yc) / np.diag(Ki)[:, None]          # leave-one-out residuals
        return float(np.sqrt((r ** 2).mean())), np.sqrt((r ** 2).mean(0))

    grid = [(ls, amp, lin, nz) for ls in (0.7, 1.4, 2.5) for amp in (0.3, 1.0) for lin in (0.0, 0.1, 0.4) for nz in (0.03, 0.15)]
    chosen: list[int] = []
    best_e, best_hp = float(np.sqrt((Yc ** 2).mean())), (1.4, 0.3, 0.0, 0.15)
    for _ in range(max_features):
        cand = None
        for j in range(Z.shape[1]):
            if j in chosen:
                continue
            for hp in grid:
                e, _ = loo_err(chosen + [j], hp)
                if cand is None or e < cand[0]:
                    cand = (e, hp, j)
        if cand is None or cand[0] > best_e - 0.003:
            break
        best_e, best_hp = cand[0], cand[1]
        chosen.append(cand[2])
    if not chosen:
        chosen = [int(np.argmax(Z.std(0)))]
    _e, per = loo_err(chosen, best_hp)
    ls, amp, lin, noise = best_hp
    K = _kernel(Z[:, chosen], ls * math.sqrt(len(chosen)), amp, lin) + noise * np.eye(n)
    alpha = np.linalg.solve(K, Yc)
    return SeedModel(model, category, list(knobs), [keys[j] for j in chosen], med[chosen], mu[chosen], sd[chosen],
                     ls * math.sqrt(len(chosen)), amp, lin, noise, Z[:, chosen], alpha, ym, per, Y.std(0))


def load_seed_bank(path: Path = SEEDS_PATH) -> dict[str, SeedModel]:
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {m: SeedModel.from_json(v) for m, v in d.items() if not m.startswith("_")}


MIN_BAND, MAX_BAND = 0.04, 0.35        # half-width of a seed band (fraction of the knob's range)
INFORMATIVE = 0.80                     # a knob is seeded only when its LOO error is under this share of its spread


def seeds_from_bank(sm: SeedModel, probe: dict[str, float], ranges: dict[str, tuple[float, float]]) -> dict[str, Seed]:
    """Knob seeds of one model from its fitted regressor; knobs whose regression does not beat the spread are left
    free."""
    p = sm.predict(probe)
    out: dict[str, Seed] = {}
    for j, k in enumerate(sm.knobs):
        if k not in ranges or sm.loo[j] > INFORMATIVE * max(sm.spread[j], 1e-9):
            continue
        lo, hi = ranges[k]
        w = float(np.clip(2.0 * sm.loo[j], MIN_BAND, MAX_BAND))
        out[k] = Seed(float(lo + p[j] * (hi - lo)), w, "time_specialist", float(p[j]))
    return out


# ------------------------------------------------------------------------------------ family score

def _smooth_env(e_db: np.ndarray, floor_db: float, win: int) -> np.ndarray:
    p = 10 ** (np.maximum(e_db, floor_db) / 10)
    k = np.ones(win) / win
    return 10 * np.log10(np.convolve(p, k, "same") + 1e-18)


class TimeFamilyScore:
    """Distance between the target's and a candidate's impulse responses, in units where 1.0 is 20 dB of
    average envelope error: the late envelope at four smoothings (10, 40, 150, 400 ms: a timing error costs
    something at every scale and the coarse scale gives the search a basin) and the energy-decay curve per
    band. Level of the direct sound cancels (every envelope is re its direct block); the deconvolution floor
    clips both sides."""

    def __init__(self, target: np.ndarray, dry: np.ndarray, *, scales_ms=(10, 40, 150, 400), w_env: float = 1.0,
                 w_edc: float = 1.0, t_max_s: float = 2.0):
        self.dry = dry
        self.scales = tuple(max(int(s / (1000 * BLK_S)), 1) for s in scales_ms)
        self.w_env, self.w_edc = w_env, w_edc
        self.n = int(t_max_s / BLK_S)
        self.t = self._prep(ir_analysis(target, dry))

    def _prep(self, ir: IRView) -> dict:
        i0 = int(0.012 / BLK_S)
        e = ir.env_db[i0: self.n]
        clip = ir.floor_db + 3.0
        out = {"floor": ir.floor_db, "env": [_smooth_env(e, clip, w) for w in self.scales]}
        dc = decay_curve(ir)
        out["edc"] = {k: v for k, v in dc.items() if not k.endswith("_e")}
        return out

    def distance(self, cand: np.ndarray) -> float:
        try:
            c = self._prep(ir_analysis(cand, self.dry))
        except Exception:  # noqa: BLE001 - an unscoreable candidate is a bad one
            return 5.0
        floor = max(self.t["floor"], c["floor"]) + 3.0
        env = []
        for a, b in zip(self.t["env"], c["env"], strict=True):
            m = min(a.size, b.size)
            a, b = np.maximum(a[:m], floor), np.maximum(b[:m], floor)
            act = (a > floor + 1.0) | (b > floor + 1.0)
            env.append(float(np.mean(np.abs(a - b)[act])) if act.any() else 0.0)
        d_env = float(np.mean(env)) / 20.0
        edc = []
        for k, a in self.t["edc"].items():
            b = c["edc"].get(k)
            if b is None:
                continue
            m = min(a.size, b.size)
            a, b = np.maximum(a[:m], -50.0), np.maximum(b[:m], -50.0)
            act = (a > -45.0) | (b > -45.0)
            edc.append(float(np.mean(np.abs(a - b)[act])) if act.any() else 0.0)
        d_edc = float(np.mean(edc)) / 20.0 if edc else 0.0
        return self.w_env * d_env + self.w_edc * d_edc


@dataclass
class _Total:
    total: float
    terms: dict
    measured: dict


class FamilyKit:
    """`inner` (the general kit) plus `weight` times the time-family distance. Exposes `.inner` like the level-guard
    kit, so the hybrid's level-free broad scoring still finds the shape-only kit underneath."""

    def __init__(self, inner, fam: TimeFamilyScore, weight: float):
        self.inner, self.fam, self.weight = inner, fam, float(weight)

    def score(self, target, candidate, **kw):
        sc = self.inner.score(target, candidate, **kw)
        d = self.fam.distance(np.asarray(candidate, np.float32)[: len(self.fam.dry)])
        return _Total(float(sc.total) + self.weight * d, {**getattr(sc, "terms", {}), "time_family": d},
                      getattr(sc, "measured", {}))


# ------------------------------------------------------------------------------------ the specialist

class TimeSpecialist:
    family = "time"
    categories = ("delay", "reverb")
    budget_share = 1.0

    def __init__(self, classifier: Classifier | None | str = "default", bank: dict[str, SeedModel] | str = "default",
                 calibration: dict | str = "default", score_weight: float = 0.25):
        self._cls = load_classifier() if classifier == "default" else classifier
        self._bank = load_seed_bank() if bank == "default" else bank
        if calibration == "default":
            from tonematch.search.measured_seed import load_calibration
            calibration = load_calibration(CALIBRATION_PATH)
        self._cal = calibration or {}
        self.score_weight = score_weight
        self._last: tuple[tuple, Evidence] | None = None

    def evidence(self, target: np.ndarray, dry: np.ndarray) -> Evidence:
        key = (id(target), id(dry), len(target))
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        ev = analyse(target, dry)
        ev.probs = self._cls.proba(ev.vector()) if self._cls is not None else rule_probs(ev.vector())
        self._last = (key, ev)
        return ev

    def screen_hint(self, target: np.ndarray, dry: np.ndarray) -> float:
        return float(self.evidence(target, dry).probs["time"])

    def seed(self, target: np.ndarray, dry: np.ndarray) -> FamilySeed:
        ev = self.evidence(target, dry)
        meas = Measured()
        try:
            from tonematch.search.measured_seed import measure
            meas = measure(target, dry, with_probs=False)
        except Exception as e:  # noqa: BLE001
            meas.notes.append(f"generic measurement failed: {e}")
        meas.q.update({f"time.{k}": v for k, v in ev.probe.items()})

        def for_model(model: str, category: str, ranges: dict[str, tuple[float, float]]) -> dict[str, Seed]:
            sm = self._bank.get(model)
            if sm is not None:
                sd = seeds_from_bank(sm, ev.probe, ranges)
                if sd:
                    return sd
            if self._cal and model in self._cal:
                return seeds_for(model, category, self._cal, meas, ranges)
            return {}

        return FamilySeed(
            "time", meas, {"delay": ev.probs["delay"], "reverb": ev.probs["reverb"], "mod": ev.probs["mod"]},
            for_model, [(f"time specialist: p(delay) {ev.probs['delay']:.2f} reverb {ev.probs['reverb']:.2f} "
                        f"mod {ev.probs['mod']:.2f} other {ev.probs['other']:.2f}; echoes {len(ev.pattern['t'])}")],
            {"probs": dict(ev.probs), "echo_t": ev.pattern["t"][:8], "echo_db": [round(x, 1) for x in ev.pattern["level_db"][:8]]})

    def score(self, target: np.ndarray, dry: np.ndarray):
        if self.score_weight <= 0:
            return None
        fam = TimeFamilyScore(target, dry)
        return lambda kit: FamilyKit(kit, fam, self.score_weight)


def make_specialist(**kw) -> TimeSpecialist:
    return TimeSpecialist(**kw)
