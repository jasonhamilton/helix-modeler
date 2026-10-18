"""Linear specialist for cab and EQ blocks (T117; human 2026-10-17 "a specialist per effect family").

Cabs and EQs are linear and time-invariant (T106: Helix cab coherence 0.9997). With the DI known, the
target's transfer response is computed, not searched: deconvolve the target against the DI, smooth it
to 1/12 octave, then

  (a) cab   match it to the nearest Helix cab model and setting by response;
  (b) EQ    fit each EQ model's knobs by weighted least squares over that model's response curves;
  (c) score a log-spectral response distance (dB), cheap and search-free.

The response curves come from a **response table** per model, rendered once per library version
through a periodic probe (`probe_signal`, a flat random-phase multisine, so one period's FFT ratio is
the exact response) and cached like `seed_calibrate` (`scripts/linear_tables.py`, one Native host,
`cache/linear-tables/<library version>/`). A table splits a model's knobs into **groups**: knobs that
interact (a parametric band's Freq, Gain and Q; a mic IR's Angle, Distance and Position) share a
grid, and groups that do not interact add in dB (measured: a cascade of filters adds exactly, cut
filters add to everything to within 0.1 dB). The groups are found from the renders, not assumed.

    R(u) = base + sum over groups g of [T_g(u_g) - base]

where T_g is the response with group g at u_g and every other knob at its default, interpolated
multilinearly in dB on the group's grid. A one-knob group is rendered at all 128 CC steps (exact); a
larger group's per-knob nodes are placed where a fine sweep shows linear interpolation in dB failing
(`adaptive_axis`; the mic-IR Angle knob is a switch between two IRs at the middle of its range, and
Position curves sharply above 0.5). `fit` minimises the weighted squared dB error by block coordinate
descent over the groups, every knob on the CC grid k/127, from the defaults and a few random starts.

`LinearSpecialist` is the family-dispatch face (docs/notes/family-specialists.md, "linear"):

  screen_hint(target, dry)   how linear the slot is (output power not explained by any LTI filter),
                             cab-versus-EQ probabilities, a model shortlist; no renders
  seed(target, dry)          every table model fitted, best first (model, CC knobs, values, fit dB)
  refine(render, ...)        optional: render the top seeds (on the target's DI by default), remove the
                             table's and the estimator's error at that point and re-fit (a few renders)
  score(target, cand, dry)   the family score: response distance in dB between target and candidate
  budget_share               the share of a slot's render budget it asks for

Not a LossKit term and not admitted (the contract's term list is unchanged): it is a seed and a
family score for the search. Known-DI mode only; with no DI, `transfer` has nothing to divide by.
All audio is mono float at 48 kHz.
"""
from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SR = 48_000
ROOT = Path(__file__).resolve().parents[3]
TABLE_DIR = ROOT / "cache" / "linear-tables"

#: Analysis grid: 1/12 octave, 31.25 Hz - 16 kHz, 109 points; each point averages +-1/24 octave.
GRID = 31.25 * 2.0 ** (np.arange(109) / 12.0)
HALF_OCT = 1 / 24
#: Bands that count in a fit and a score.
FIT_BAND = (40.0, 16000.0)
#: The probe: one period of PROBE_N samples, PROBE_PERIODS periods (the first is discarded).
PROBE_N = 16384
PROBE_PERIODS = 4
PROBE_RMS_DB = -20.0
#: Group grids. A one-knob group is rendered at all 128 CC steps (exact). A larger group gets per-knob
#: nodes from a sweep at every SWEEP_STEP-th CC step (the group's other knobs away from their default,
#: so a band's Freq sweep sees a peak), placed by `adaptive_axis` to AXIS_TOL_DB, at most
#: AXIS_MAX[size] nodes per knob, and the joint grid over those nodes.
SWEEP_STEP = 2
AXIS_TOL_DB = 0.03
AXIS_MAX = {2: 24, 3: 16}
AXIS_MAX_LARGE = 6
#: At most this many joint nodes per group (the knob with the most nodes gives one up until it fits).
MAX_GROUP_NODES = 1200
#: `lti_check`: a block whose response moves more than this with level or from period to period is
#: flagged non-LTI in its table's meta (the fit is then only an approximation).
LTI_TOL_DB = 0.5
#: Two knobs interact when their joint change differs from the sum of their single changes by more
#: than this (RMS dB over 60 Hz - 10 kHz) or INTERACT_MAX_DB at any point.
INTERACT_RMS_DB = 0.25
INTERACT_MAX_DB = 1.0
INTERACT_BAND = (60.0, 10000.0)
INTERACT_FLOOR_DB = 30.0
#: Models whose best fits are within TIE_DB of each other are told apart by fewer knobs.
TIE_DB = 0.10
STEPS = 127
#: Welch segment of `transfer` (0.68 s; T117 tuning half: 8192 left cab estimates 0.3-0.6 dB worse
#: and their incoherent power at -27 to -37 dB, 32768 at -36 to -51 dB).
NPERSEG = 32768
#: Random starts of the knob fit, besides the defaults (models with more than one group).
RESTARTS = 4
EPS = 1e-20


# --------------------------------------------------------------------------------------- probe
def probe_period(seed: int = 0, n: int = PROBE_N, rms_db: float = PROBE_RMS_DB) -> np.ndarray:
    """One period of a flat-magnitude random-phase multisine (every bin but DC and Nyquist)."""
    rng = np.random.default_rng([20261017, seed])
    k = np.arange(n // 2 + 1)
    mag = np.ones(k.size)
    mag[0] = mag[-1] = 0.0
    x = np.fft.irfft(mag * np.exp(2j * np.pi * rng.random(k.size)), n)
    return x * (10 ** (rms_db / 20) / np.sqrt(np.mean(x ** 2)))


def probe_signal(seed: int = 0, periods: int = PROBE_PERIODS) -> np.ndarray:
    """The probe clip: `periods` repeats of `probe_period` (float32, about 1.4 s)."""
    return np.tile(probe_period(seed), periods).astype(np.float32)


def _band_mean(freqs: np.ndarray, vals: np.ndarray, grid: np.ndarray = GRID, half_oct: float = HALF_OCT) -> np.ndarray:
    lo, hi = grid * 2.0 ** (-half_oct), grid * 2.0 ** half_oct
    c = np.concatenate([[0.0], np.cumsum(vals)])
    i0, i1 = np.searchsorted(freqs, lo, "left"), np.searchsorted(freqs, hi, "right")
    out = np.empty(grid.size)
    for j, (a, b) in enumerate(zip(i0, i1, strict=True)):
        out[j] = (c[b] - c[a]) / (b - a) if b > a else vals[int(np.argmin(np.abs(freqs - grid[j])))]
    return out


def probe_response(y: np.ndarray, seed: int = 0, periods: int = PROBE_PERIODS) -> np.ndarray:
    """Magnitude response (dB on GRID) from a render of `probe_signal(seed)`: the mean over periods
    2..P of FFT(y)/FFT(x) per bin, then the band mean of |H| (amplitude, as `transfer` averages)."""
    x1 = probe_period(seed)
    n = x1.size
    y = np.asarray(y, np.float64)
    ys = [y[i * n:(i + 1) * n] for i in range(1, periods) if (i + 1) * n <= y.size]
    if not ys:
        raise ValueError("render shorter than two probe periods")
    Y = np.mean([np.fft.rfft(s) for s in ys], axis=0)
    X = np.fft.rfft(x1)
    f = np.fft.rfftfreq(n, 1 / SR)
    h = np.abs(Y[1:-1] / X[1:-1])
    return 20 * np.log10(np.maximum(_band_mean(f[1:-1], h), EPS))


# --------------------------------------------------------------------------------------- target
@dataclass
class Transfer:
    """A measured response: dB on GRID, per-band weight (0..1), and how linear the pair is."""
    mag_db: np.ndarray
    weight: np.ndarray
    coherence: np.ndarray
    incoherent_db: float     # output power not explained by the best LTI filter, dB re the output (60 Hz-8 kHz)


def transfer(target: np.ndarray, dry: np.ndarray, nperseg: int = NPERSEG, min_input_db: float = -70.0) -> Transfer:
    """Transfer response of `target` against its DI `dry` (H1, |Sxy| / Sxx band-averaged on GRID).

    Weight = coherence squared inside FIT_BAND, zero where the DI's band power is more than
    `min_input_db` below its strongest band (nothing to divide by). `incoherent_db` is
    10 log10(1 - sum(coh * Pyy) / sum(Pyy)) over 60 Hz - 8 kHz bins: -36 dB or lower for every
    cab and EQ of the T117 tuning half on Native, far higher for drive, compression, modulation and long reverbs or echoes.
    """
    from scipy.signal import csd, welch

    x = np.asarray(dry, np.float64)
    y = np.asarray(target, np.float64)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    f, pxy = csd(x, y, fs=SR, nperseg=nperseg)
    _, pxx = welch(x, fs=SR, nperseg=nperseg)
    _, pyy = welch(y, fs=SR, nperseg=nperseg)
    tiny = np.finfo(np.float64).tiny
    axy = _band_mean(f, np.abs(pxy))
    sxx = _band_mean(f, pxx)
    mag = 20 * np.log10(np.maximum(axy, tiny) / np.maximum(sxx, tiny))
    coh_bin = np.abs(pxy) ** 2 / np.maximum(pxx * pyy, tiny)
    coh = np.clip(_band_mean(f, coh_bin * pxx) / np.maximum(sxx, tiny), 0.0, 1.0)
    sel = (f >= 60) & (f <= 8000)
    expl = float(np.sum(np.clip(coh_bin[sel], 0, 1) * pyy[sel]) / max(np.sum(pyy[sel]), tiny))
    inc_db = float(10 * np.log10(max(1.0 - expl, 1e-12)))
    level = 10 * np.log10(np.maximum(sxx, tiny))
    w = coh ** 2 * ((GRID >= FIT_BAND[0]) & (GRID <= FIT_BAND[1])) * (level >= level.max() + min_input_db)
    return Transfer(mag, w, coh, inc_db)


def response_distance(a: np.ndarray, b: np.ndarray, weight: np.ndarray | None = None,
                      gain_free: bool = False) -> float:
    """Weighted RMS dB difference of two responses on GRID (absolute gain kept unless `gain_free`)."""
    w = np.ones(GRID.size) * ((GRID >= FIT_BAND[0]) & (GRID <= FIT_BAND[1])) if weight is None else np.asarray(weight)
    d = np.asarray(a, float) - np.asarray(b, float)
    if gain_free:
        d = d - np.sum(w * d) / max(np.sum(w), EPS)
    return float(np.sqrt(np.sum(w * d ** 2) / max(np.sum(w), EPS)))


# --------------------------------------------------------------------------------------- tables
def adaptive_axis(us: np.ndarray, resp: np.ndarray, n_max: int, tol_db: float = AXIS_TOL_DB) -> np.ndarray:
    """Nodes (a subset of the sweep points `us`, sorted) such that linear interpolation in dB between
    them reproduces the sweep `resp` (len(us), G) to `tol_db` RMS over FIT_BAND, at most `n_max` nodes:
    the endpoints, then greedily the sweep point with the largest interpolation error. A step (the
    mic-IR Angle switch) gets a node on each side because both neighbours of the jump are sweep points."""
    sel = (GRID >= FIT_BAND[0]) & (GRID <= FIT_BAND[1])
    r = np.asarray(resp, float)[:, sel]
    chosen = [0, len(us) - 1]
    while len(chosen) < n_max:
        c = sorted(chosen)
        err = np.zeros(len(us))
        for a, b in itertools.pairwise(c):
            for i in range(a + 1, b):
                t = (us[i] - us[a]) / (us[b] - us[a])
                e = r[i] - ((1 - t) * r[a] + t * r[b])
                err[i] = np.sqrt(np.mean(e ** 2))
        if err.max() <= tol_db:
            break
        chosen.append(int(np.argmax(err)))
    return np.asarray(us)[sorted(chosen)]


@dataclass
class ResponseTable:
    """One model's cached response curves (see the module docstring)."""
    model: str
    category: str
    knobs: list[str]                       # free knob names, the table's order
    ranges: dict[str, tuple[float, float]]
    default_u: dict[str, float]            # catalog default, as a fraction of the range
    groups: list[list[str]]
    axes: list[list[np.ndarray]]           # per group, per knob: the unit grid
    base: np.ndarray                       # response at the defaults
    parts: list[np.ndarray]                # per group: (n1, ..., nd, G) responses, other knobs at default
    meta: dict = field(default_factory=dict)

    # ----- prediction
    def part(self, gi: int, u: np.ndarray) -> np.ndarray:
        """Group gi's response at unit points u (M, d) -> (M, G), multilinear in dB."""
        u = np.atleast_2d(np.asarray(u, float))
        tab, axes = self.parts[gi], self.axes[gi]
        d = len(axes)
        idx, frac = [], []
        for j in range(d):
            ax = axes[j]
            t = np.clip(u[:, j], ax[0], ax[-1])
            i = np.clip(np.searchsorted(ax, t, "right") - 1, 0, ax.size - 2)
            idx.append(i)
            frac.append((t - ax[i]) / (ax[i + 1] - ax[i]))
        out = np.zeros((u.shape[0], tab.shape[-1]))
        for corner in itertools.product((0, 1), repeat=d):
            wt = np.ones(u.shape[0])
            sl = []
            for j, c in enumerate(corner):
                wt = wt * (frac[j] if c else 1 - frac[j])
                sl.append(idx[j] + c)
            out += wt[:, None] * tab[tuple(sl)]
        return out

    def group_u(self, u: dict[str, float], gi: int) -> np.ndarray:
        return np.array([[u.get(k, self.default_u[k]) for k in self.groups[gi]]])

    def predict(self, u: dict[str, float]) -> np.ndarray:
        """Response (dB on GRID) at unit knob values `u` (missing knobs at their default)."""
        r = self.base.copy()
        for gi in range(len(self.groups)):
            r += self.part(gi, self.group_u(u, gi))[0] - self.base
        return r

    def values(self, u: dict[str, float]) -> dict[str, float]:
        return {k: float(self.ranges[k][0] + u[k] * (self.ranges[k][1] - self.ranges[k][0])) for k in u}

    # ----- storage
    def save(self, d: Path) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        arrays = {"base": self.base}
        for gi, p in enumerate(self.parts):
            arrays[f"part{gi}"] = p
        np.savez_compressed(d / f"{self.model}.npz", **arrays)
        doc = {"model": self.model, "category": self.category, "knobs": self.knobs,
               "ranges": {k: list(v) for k, v in self.ranges.items()}, "default_u": self.default_u,
               "groups": self.groups, "axes": [[a.tolist() for a in ax] for ax in self.axes], "meta": self.meta,
               "grid": "1/12 octave 31.25 Hz - 16 kHz (109)", "probe": {"n": PROBE_N, "periods": PROBE_PERIODS,
                                                                       "rms_db": PROBE_RMS_DB}}
        p = d / f"{self.model}.json"
        p.write_text(json.dumps(doc, indent=1))
        return p

    @classmethod
    def load(cls, d: Path, model: str) -> ResponseTable:
        doc = json.loads((d / f"{model}.json").read_text())
        z = np.load(d / f"{model}.npz")
        return cls(doc["model"], doc["category"], doc["knobs"], {k: tuple(v) for k, v in doc["ranges"].items()},
                   doc["default_u"], doc["groups"], [[np.asarray(a, float) for a in ax] for ax in doc["axes"]],
                   z["base"], [z[f"part{gi}"] for gi in range(len(doc["groups"]))], doc.get("meta", {}))


def table_dir(version: str | None = None) -> Path:
    if version is None:
        from tonematch.engines import native_library as NL

        version = NL.VERSION
    return TABLE_DIR / version


def load_tables(models: Sequence[str] | None = None, d: Path | None = None) -> dict[str, ResponseTable]:
    """The cached tables (all, or `models` that have one)."""
    d = d or table_dir()
    names = [p.stem for p in sorted(d.glob("*.json"))] if d.exists() else []
    if models is not None:
        names = [m for m in models if (d / f"{m}.json").exists()]
    return {m: ResponseTable.load(d, m) for m in names}


def _union_groups(knobs: list[str], linked: list[tuple[str, str]]) -> list[list[str]]:
    parent = {k: k for k in knobs}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for a, b in linked:
        parent[root(a)] = root(b)
    out: dict[str, list[str]] = {}
    for k in knobs:
        out.setdefault(root(k), []).append(k)
    return list(out.values())


def build_table(response: Callable[[dict[str, float]], np.ndarray], model: str, category: str,
                ranges: dict[str, tuple[float, float]], defaults: dict[str, float], n_check: int = 8,
                seed: int = 0, log: Callable[[str], None] | None = None) -> ResponseTable:
    """Render a model's response table. `response(values)` returns the probe response (dB on GRID) at
    physical knob values (missing knobs at their defaults); `ranges` and `defaults` are physical.

    1. base at the defaults; 2. each knob alone at 0.2 or 0.8 of its range (whichever is farther from
    the default) and every pair together: pairs whose joint change is not the sum of their singles
    are linked, linked knobs form a group; 3. each group's grid; 4. `n_check` random settings,
    rendered and predicted, give the table's own error (meta `check_rms_db`, `check_max_db`)."""
    knobs = sorted(ranges)

    def phys(k, u):
        lo, hi = ranges[k]
        return float(lo + u * (hi - lo))

    def unit(k, v):
        lo, hi = ranges[k]
        return float(np.clip((v - lo) / (hi - lo), 0.0, 1.0)) if hi > lo else 0.0

    du = {k: round(unit(k, defaults[k]) * STEPS) / STEPS for k in knobs}
    base_vals = {k: phys(k, du[k]) for k in knobs}
    renders = 0

    def R(u: dict[str, float]) -> np.ndarray:
        nonlocal renders
        renders += 1
        return np.asarray(response({**base_vals, **{k: phys(k, v) for k, v in u.items()}}), float)

    base = R({})
    probe_u = {k: (0.8 if du[k] < 0.5 else 0.2) for k in knobs}
    single = {k: R({k: probe_u[k]}) - base for k in knobs}
    sel = (GRID >= INTERACT_BAND[0]) & (GRID <= INTERACT_BAND[1])
    linked, inter = [], {}
    for a, b in itertools.combinations(knobs, 2):
        pair = R({a: probe_u[a], b: probe_u[b]})
        # only bands that are audible in all four renders count: deep in a cut filter's stop band (the
        # Parametric's LowCut x low band at -58 dB) the dB difference is large and meaningless
        floor = base.max() - INTERACT_FLOOR_DB
        live = sel & (np.minimum.reduce([base, base + single[a], base + single[b], pair]) >= floor)
        if not live.any():
            continue
        e = (pair - base - single[a] - single[b])[live]
        rms, mx = float(np.sqrt(np.mean(e ** 2))), float(np.abs(e).max())
        inter[f"{a}x{b}"] = [round(rms, 3), round(mx, 3)]
        if rms > INTERACT_RMS_DB or mx > INTERACT_MAX_DB:
            linked.append((a, b))
    groups = _union_groups(knobs, linked)
    axes, parts = [], []
    full = np.arange(STEPS + 1) / STEPS
    for g in groups:
        if len(g) == 1:
            ax = [full]
            tab = np.stack([R({g[0]: float(u)}) for u in full])
        else:
            sweep = np.unique(np.r_[np.arange(0, STEPS + 1, SWEEP_STEP), STEPS]) / STEPS
            curves = []
            for k in g:
                held = {o: probe_u[o] for o in g if o != k}
                curves.append(np.stack([R({**held, k: float(u)}) for u in sweep]))
            caps = [AXIS_MAX.get(len(g), AXIS_MAX_LARGE)] * len(g)
            ax = [adaptive_axis(sweep, c, n) for c, n in zip(curves, caps, strict=True)]
            while int(np.prod([a.size for a in ax])) > MAX_GROUP_NODES:
                j = int(np.argmax([a.size for a in ax]))
                caps[j] = ax[j].size - 1
                ax[j] = adaptive_axis(sweep, curves[j], caps[j])
            tab = np.empty(tuple(a.size for a in ax) + (GRID.size,))
            for node in itertools.product(*[range(a.size) for a in ax]):
                tab[node] = R({k: float(ax[j][node[j]]) for j, k in enumerate(g)})
        axes.append(ax)
        parts.append(tab)
        if log:
            log(f"{model}: group {g} nodes {[a.size for a in ax]} done ({renders} renders)")
    t = ResponseTable(model, category, knobs, {k: tuple(map(float, ranges[k])) for k in knobs}, du, groups,
                      axes, base, parts)
    rng = np.random.default_rng([seed, 7])
    errs = []
    for _ in range(n_check):
        u = {k: round(float(rng.uniform(0.1, 0.9)) * STEPS) / STEPS for k in knobs}
        e = (R(u) - t.predict(u))[(GRID >= FIT_BAND[0]) & (GRID <= FIT_BAND[1])]
        errs.append((float(np.sqrt(np.mean(e ** 2))), float(np.abs(e).max())))
    t.meta = {"renders": renders, "interaction": inter, "linked": [list(p) for p in linked],
              "check_rms_db": float(np.median([e[0] for e in errs])) if errs else None,
              "check_max_db": float(max(e[1] for e in errs)) if errs else None, "n_check": n_check}
    return t


def lti_check(render: Callable[[np.ndarray], np.ndarray], drop_db: float = 12.0) -> dict:
    """Is the block (at its loaded setting) linear and time-invariant? `render(x)` returns the block's
    output for x. Two renders: the probe at its level and `drop_db` lower (a linear block gives the
    same response), and the spread between the probe's periods (a time-invariant block repeats).
    Both as RMS dB over FIT_BAND; `lti` is both under LTI_TOL_DB."""
    sel = (GRID >= FIT_BAND[0]) & (GRID <= FIT_BAND[1])
    p = probe_signal()
    y1 = np.asarray(render(p), float)
    y2 = np.asarray(render((p * 10 ** (-drop_db / 20)).astype(np.float32)), float)
    level = float(np.sqrt(np.mean((probe_response(y1) - probe_response(y2) - drop_db)[sel] ** 2)))
    n = PROBE_N
    per = [probe_response(np.tile(y1[i * n:(i + 1) * n], 2), periods=2) for i in range(1, PROBE_PERIODS)]
    spread = float(np.sqrt(np.mean((np.max(per, axis=0) - np.min(per, axis=0))[sel] ** 2)))
    return {"lti_level_db": level, "lti_period_db": spread, "lti": bool(level < LTI_TOL_DB and spread < LTI_TOL_DB)}


# --------------------------------------------------------------------------------------- fit
@dataclass
class Fit:
    model: str
    category: str
    knobs: dict[str, int]          # CC steps 0..127
    values: dict[str, float]       # physical
    fit_db: float                  # weighted RMS dB residual (table prediction, or the render after refine)
    n_knobs: int
    predicted: np.ndarray
    rendered: bool = False         # fit_db is from a probe render, not the table


def _wrms(r: np.ndarray, w: np.ndarray) -> np.ndarray:
    return np.sqrt(np.sum(w * r ** 2, axis=-1) / max(float(np.sum(w)), EPS))


def _window(axis: np.ndarray, centre: float, half: int, step: int) -> np.ndarray:
    c = round(centre * STEPS)
    return np.arange(max(0, c - half), min(STEPS, c + half) + 1, step) / STEPS


def _best_group(t: ResponseTable, gi: int, need: np.ndarray, w: np.ndarray, gain_free: bool) -> np.ndarray:
    """The group setting whose part best matches `need` (dB on GRID): exhaustive on the CC grid for
    one or two knobs; for more, the grid nodes, then a window spanning the neighbouring nodes at
    step 4, then +-3 steps at step 1."""
    d = len(t.groups[gi])

    def score(us):
        r = need[None, :] - t.part(gi, us)
        if gain_free:
            r = r - (np.sum(w * r, axis=1) / max(float(np.sum(w)), EPS))[:, None]
        return _wrms(r, w)

    def best_of(dims):
        us = np.array(list(itertools.product(*dims)))
        sc = np.concatenate([score(us[i:i + 4096]) for i in range(0, len(us), 4096)])
        return us[int(np.argmin(sc))]
    full = np.arange(STEPS + 1) / STEPS
    if d <= 2:
        return best_of([full] * d)
    best = best_of(t.axes[gi])
    halves = []
    for j, ax in enumerate(t.axes[gi]):
        i = int(np.argmin(np.abs(ax - best[j])))
        lo, hi = ax[max(i - 1, 0)], ax[min(i + 1, ax.size - 1)]
        halves.append(int(np.ceil(max(best[j] - lo, hi - best[j]) * STEPS)))
    best = best_of([_window(ax, best[j], halves[j], 4) for j, ax in enumerate(t.axes[gi])])
    return best_of([_window(ax, best[j], 3, 1) for j, ax in enumerate(t.axes[gi])])


def _descend(t: ResponseTable, target_db: np.ndarray, weight: np.ndarray, u: dict[str, float], sweeps: int,
             gain_free: bool) -> tuple[dict[str, float], np.ndarray, float]:
    target_db = np.asarray(target_db, float)
    parts = [t.part(gi, t.group_u(u, gi))[0] - t.base for gi in range(len(t.groups))]
    for _ in range(sweeps):
        changed = False
        for gi, g in enumerate(t.groups):
            need = target_db - sum(p for j, p in enumerate(parts) if j != gi)
            b = _best_group(t, gi, need, weight, gain_free)
            new = {k: float(b[j]) for j, k in enumerate(g)}
            if any(abs(new[k] - u.get(k, -1)) > 1e-9 for k in g):
                changed = True
            u.update(new)
            parts[gi] = t.part(gi, t.group_u(u, gi))[0] - t.base
        if not changed:
            break
    pred = t.base + sum(parts) if parts else t.base.copy()
    r = target_db - pred
    if gain_free:
        r = r - np.sum(weight * r) / max(float(np.sum(weight)), EPS)
    return u, pred, float(_wrms(r, weight))


def fit(t: ResponseTable, target_db: np.ndarray, weight: np.ndarray, sweeps: int = 6,
        gain_free: bool = False, start: dict[str, float] | None = None, restarts: int = RESTARTS,
        seed: int = 0) -> Fit:
    """Least-squares knob fit of one model to a measured response: block coordinate descent over the
    groups (every knob on the CC grid) from `start` (default: the catalog defaults) and `restarts`
    seeded random starts when the model has more than one group; the lowest residual wins. Absolute
    gain counts unless `gain_free`."""
    starts = [dict(start or t.default_u)]
    if len(t.groups) > 1:
        rng = np.random.default_rng([seed, 11])
        starts += [{k: round(float(rng.uniform(0.05, 0.95)) * STEPS) / STEPS for k in t.knobs}
                   for _ in range(restarts)]
    best = None
    for s0 in starts:
        u, pred, d = _descend(t, target_db, weight, dict(s0), sweeps, gain_free)
        if best is None or d < best[2] - 1e-9:
            best = (u, pred, d)
    u, pred, d = best
    knobs = {k: round(u[k] * STEPS) for k in t.knobs}
    return Fit(t.model, t.category, knobs, t.values({k: knobs[k] / STEPS for k in t.knobs}), d, len(t.knobs), pred)


def rank(fits: list[Fit], tie_db: float = TIE_DB) -> list[Fit]:
    """Best first: lowest residual, but among fits within `tie_db` of the best, fewer knobs first."""
    fs = sorted(fits, key=lambda f: f.fit_db)
    if not fs:
        return fs
    best = fs[0].fit_db
    tied = sorted([f for f in fs if f.fit_db <= best + tie_db], key=lambda f: (f.n_knobs, f.fit_db))
    return tied + [f for f in fs if f.fit_db > best + tie_db]


# --------------------------------------------------------------------------------------- dispatch face
@dataclass
class ScreenHint:
    family: str
    family_prob: float            # how likely the slot is a single linear (cab or EQ) block
    incoherent_db: float
    category_probs: dict[str, float]
    shortlist: list[str]          # models best first
    fit_db: float                 # best model's table residual


class LinearSpecialist:
    """Family specialist for `linear` (cab, eq). See the module docstring and
    docs/notes/family-specialists.md ("linear")."""

    family = "linear"
    categories = ("cab", "eq")
    #: Share of a slot's render budget the specialist asks for: `refine` renders the top 4 seeds and
    #: corrects the best two up to twice, at most 8 renders, about 5 on the T117 tuning half, of 300.
    budget_share = 0.04
    #: incoherent_db at which family_prob is 0.5, and the logistic slope per dB (T117 registration).
    LINEAR_MID_DB = -33.0
    LINEAR_SLOPE = 0.5

    def __init__(self, tables: dict[str, ResponseTable] | None = None, models: Sequence[str] | None = None,
                 gain_free: bool = False, tie_db: float = TIE_DB):
        self.tables = tables if tables is not None else load_tables(models)
        if models is not None:
            self.tables = {m: t for m, t in self.tables.items() if m in set(models)}
        self.gain_free, self.tie_db = gain_free, tie_db

    # ----- no renders
    def measure(self, target: np.ndarray, dry: np.ndarray) -> Transfer:
        return transfer(target, dry)

    def fits(self, tr: Transfer, models: Sequence[str] | None = None) -> list[Fit]:
        ms = [m for m in (models or self.tables) if m in self.tables]
        return rank([fit(self.tables[m], tr.mag_db, tr.weight, gain_free=self.gain_free) for m in ms], self.tie_db)

    def seed(self, target: np.ndarray, dry: np.ndarray, models: Sequence[str] | None = None,
             k: int | None = None) -> list[Fit]:
        """Every table model (or `models`) fitted to the target's response, best first."""
        fs = self.fits(self.measure(target, dry), models)
        return fs[:k] if k else fs

    def screen_hint(self, target: np.ndarray, dry: np.ndarray, models: Sequence[str] | None = None,
                    k: int = 5) -> ScreenHint:
        tr = self.measure(target, dry)
        fs = self.fits(tr, models)
        p = float(1 / (1 + np.exp(self.LINEAR_SLOPE * (tr.incoherent_db - self.LINEAR_MID_DB))))
        best = {c: min((f.fit_db for f in fs if f.category == c), default=np.inf) for c in self.categories}
        # cab vs eq: a softmax over the best residual per category, 1 dB scale
        z = {c: np.exp(-(v - min(best.values()))) if np.isfinite(v) else 0.0 for c, v in best.items()}
        s = sum(z.values()) or 1.0
        return ScreenHint(self.family, p, tr.incoherent_db, {c: float(v / s) for c, v in z.items()},
                          [f.model for f in fs[:k]], float(fs[0].fit_db) if fs else float("nan"))

    def score(self, target: np.ndarray, candidate: np.ndarray, dry: np.ndarray,
              gain_free: bool | None = None) -> float:
        """Family score: response distance (dB) of the candidate's render against the target's, both on
        the same DI. Weighted by the target's coherence; 0 for the same response. `gain_free` (default
        the specialist's) ignores the mean level difference, for captures whose gain is unknown."""
        a, b = transfer(target, dry), transfer(candidate, dry)
        return response_distance(a.mag_db, b.mag_db, a.weight, self.gain_free if gain_free is None else gain_free)

    # ----- with renders
    def refine(self, render: Callable[[str, dict[str, float]], np.ndarray], target: np.ndarray,
               dry: np.ndarray, models: Sequence[str] | None = None, top: int = 4, corrections: int = 2,
               budget: int | None = None, on: str = "dry") -> tuple[list[Fit], int]:
        """Render the best `top` seeds, then for the best two re-fit with the error at that point removed
        (`corrections` rounds) and keep what the render confirms. Returns (fits best first, renders).

        `render(model, values)` returns the block's output for `dry` (on="dry", the default: candidate and
        target go through the same estimator on the same notes, so its bias cancels and the right
        setting scores 0) or for `probe_signal()` (on="probe": cheaper renders, exact responses, but the
        target's estimator bias stays in the comparison)."""
        tr = self.measure(target, dry)
        seeds = self.fits(tr, models)
        cap = budget if budget is not None else top + 2 * corrections

        def measured(model, values):
            y = render(model, values)
            return transfer(y, dry).mag_db if on == "dry" else probe_response(y)
        used = 0
        done: list[Fit] = []
        for f in seeds[:top]:
            if used >= cap:
                break
            r = measured(f.model, f.values)
            used += 1
            done.append(Fit(f.model, f.category, f.knobs, f.values,
                            response_distance(tr.mag_db, r, tr.weight, self.gain_free), f.n_knobs, r, True))
        done = rank(done, self.tie_db)
        for i in range(min(2, len(done))):
            cur = done[i]
            t = self.tables[cur.model]
            for _ in range(corrections):
                if used >= cap:
                    break
                u = {k: v / STEPS for k, v in cur.knobs.items()}
                corr = cur.predicted - t.predict(u)          # table and estimator error at this point
                nf = fit(t, tr.mag_db - corr, tr.weight, gain_free=self.gain_free, start=u)
                if nf.knobs == cur.knobs:
                    break
                r = measured(nf.model, nf.values)
                used += 1
                d = response_distance(tr.mag_db, r, tr.weight, self.gain_free)
                if d < cur.fit_db:
                    cur = Fit(nf.model, nf.category, nf.knobs, nf.values, d, nf.n_knobs, r, True)
                else:
                    break
            done[i] = cur
        rendered = rank(done, self.tie_db)
        rest = [f for f in seeds if f.model not in {d.model for d in rendered}]
        return rendered + rest, used


# --------------------------------------------------------------------------------------- dispatch adapter
class _CaptureKit:
    """A loss kit that keeps the last candidate it scored (the refiner needs the render itself)."""

    def __init__(self, kit):
        self.inner, self.last = kit, None

    def score(self, target, candidate, **kw):
        self.last = np.asarray(candidate)
        return self.inner.score(target, candidate, **kw)


class LinearFamily:
    """The linear specialist behind the search's family dispatch (`tonematch.search.specialists`, docs/notes/
    family-specialists.md): `screen_hint`, `seed` -> FamilySeed, `score` (None: the response distance is not
    admitted as a family term), `budget_share` (relative broad-stage weight, 1.0 = unchanged) and the optional
    `refiner` hook, which replaces the general halving by the table fit plus at most `max_renders` renders of the
    linear finalists, and stops there when the best render matches the target's response to `stop_db`;
    otherwise the general halving runs on the rest of the budget over every other model."""

    family = "linear"
    categories = ("cab", "eq")
    budget_share = 1.0
    SEED_HALF_WIDTH = 0.04     # fraction of a knob's range: the fit is on the CC grid, mic-IR knobs are the loosest

    def __init__(self, spec: LinearSpecialist | None = None, refine: bool = True, max_renders: int = 8,
                 stop_db: float = 0.05, top: int = 4, corrections: int = 2):
        self.spec = spec or LinearSpecialist()
        self.refine_on, self.max_renders, self.stop_db = refine, max_renders, stop_db
        self.top, self.corrections = top, corrections
        self._key, self._tr, self._fits = None, None, {}

    def _state(self, target, dry) -> tuple[Transfer, dict[str, Fit]]:
        key = (id(target), id(dry), len(target), len(dry))
        if key != self._key:
            self._key, self._tr, self._fits = key, self.spec.measure(target, dry), {}
        return self._tr, self._fits

    def _fit(self, model: str, target, dry) -> Fit | None:
        tr, fits = self._state(target, dry)
        if model not in self.spec.tables:
            return None
        if model not in fits:
            fits[model] = fit(self.spec.tables[model], tr.mag_db, tr.weight, gain_free=self.spec.gain_free)
        return fits[model]

    def screen_hint(self, target, dry) -> float:
        tr, _ = self._state(target, dry)
        return float(1 / (1 + np.exp(self.spec.LINEAR_SLOPE * (tr.incoherent_db - self.spec.LINEAR_MID_DB))))

    def seed(self, target, dry):
        from tonematch.search.measured_seed import Measured, Seed
        from tonematch.search.specialists import FamilySeed

        tr, _ = self._state(target, dry)
        h = self.screen_hint(target, dry)
        fs = [f for m in self.spec.tables if (f := self._fit(m, target, dry)) is not None]
        best = {c: min((f.fit_db for f in fs if f.category == c), default=np.inf) for c in self.categories}
        z = {c: np.exp(-(v - min(best.values()))) if np.isfinite(v) else 0.0 for c, v in best.items()}
        zs = sum(z.values()) or 1.0

        def for_model(model, category, ranges):
            f = self._fit(model, target, dry)
            if f is None:
                return {}
            return {k: Seed(float(v), self.SEED_HALF_WIDTH, "linear_fit", float(f.fit_db))
                    for k, v in f.values.items() if not ranges or k in ranges}
        top = rank(fs, self.spec.tie_db)[:3]
        return FamilySeed(self.family, Measured({"incoherent_db": tr.incoherent_db}),
                          {c: float(h * z[c] / zs) for c in self.categories}, for_model,
                          [f"linear: incoherent {tr.incoherent_db:.1f} dB, hint {h:.2f}, table fit "
                           + ", ".join(f"{f.model} {f.fit_db:.2f} dB" for f in top)],
                          {"incoherent_db": tr.incoherent_db, "fit_db": {f.model: f.fit_db for f in top}})

    def score(self, target, dry):
        return None

    def refiner(self, target, dry):
        if not self.refine_on or self.screen_hint(target, dry) < 0.5:
            return None
        spec = self

        def refine(engine, kit, section, by_model, scores, budget, plan, renders, prior=None):
            from tonematch.search.loop import Evaluator
            from tonematch.search.model_choice import halve

            lin = {s.model: s for s in scores if s.category in spec.categories and s.model in spec.spec.tables
                   and s.model in by_model}
            if not lin:
                return None
            tr, _ = spec._state(section.target, section.dry)
            cap = _CaptureKit(kit)
            spent, notes = 0.0, []
            cache: dict[tuple, tuple[float, float, np.ndarray, np.ndarray]] = {}

            def trial(f: Fit):
                key = (f.model, tuple(sorted(f.knobs.items())))
                if key in cache:
                    return cache[key]
                nonlocal spent
                c = by_model[f.model]
                vec = c.start_vector({(0, k): v for k, v in f.values.items()})
                engine.load(c.preset_at(dict(zip(c.space.refs, vec, strict=True))))
                ev = Evaluator(engine, cap, section, c.space, plan, budget, prior)
                total, _ = ev.score(vec)
                spent += ev.spent
                r = transfer(cap.last, section.dry).mag_db
                cache[key] = (response_distance(tr.mag_db, r, tr.weight, spec.spec.gain_free), total, vec, r)
                return cache[key]
            seeds = rank([f for m in lin if (f := spec._fit(m, section.target, section.dry)) is not None],
                         spec.spec.tie_db)
            done = []
            for f in seeds[:spec.top]:
                if spent >= min(spec.max_renders, renders):
                    break
                d, total, vec, r = trial(f)
                done.append([f, d, total, vec, r])
            done.sort(key=lambda x: x[1])
            for row in done[:2]:
                f, d, total, vec, r = row
                t = spec.spec.tables[f.model]
                for _ in range(spec.corrections):
                    if spent >= min(spec.max_renders, renders):
                        break
                    u = {k: v / STEPS for k, v in f.knobs.items()}
                    nf = fit(t, tr.mag_db - (r - t.predict(u)), tr.weight, gain_free=spec.spec.gain_free, start=u)
                    if nf.knobs == f.knobs:
                        break
                    nd, ntot, nvec, nr = trial(nf)
                    if nd >= d:
                        break
                    f, d, total, vec, r = nf, nd, ntot, nvec, nr
                row[:] = [f, d, total, vec, r]
            for f, d, total, vec, _ in done:
                s = lin[f.model]
                if s.refined is None or total < s.refined:
                    s.refined, s.values = float(total), np.asarray(vec, float)
                s.rungs.append({"linear_refine": True, "response_db": float(d), "loss": float(total)})
            done.sort(key=lambda x: x[1])
            notes.append(f"linear refiner: {spent:.0f} renders, " + ", ".join(f"{x[0].model} {x[1]:.3f} dB" for x in done))
            if done and done[0][1] <= spec.stop_db:
                notes.append(f"linear refiner: response matched to {done[0][1]:.3f} dB, general halving skipped")
                return spent, notes
            rest = [s for s in scores if s.model not in {x[0].model for x in done}]
            hs, hn = halve(engine, kit, section, by_model, rest, budget, plan, max(renders - spent, 0.0) * 0.9,
                           4, 2, 0.3, 6.0, prior, budget.seed)
            return spent + hs, notes + hn
        return refine


def make_specialist(**kw) -> LinearFamily:
    """The dispatch's entry point (`specialists.load_specialist`, or the linear slot of
    `search.family_adapters`). Options meant for other families (e.g. `time_score_weight`) are ignored."""
    import inspect

    known = set(inspect.signature(LinearFamily.__init__).parameters) - {"self"}
    return LinearFamily(**{k: v for k, v in kw.items() if k in known})


# --------------------------------------------------------------------------------------- module-level face
#: The family-dispatch interface as module functions (the modulation specialist's shape), over the
#: cached tables of the current library version.
BUDGET_SHARE = LinearSpecialist.budget_share
_DEFAULT: LinearSpecialist | None = None


def default_specialist() -> LinearSpecialist:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LinearSpecialist()
    return _DEFAULT


def seed(target: np.ndarray, dry: np.ndarray, models: Sequence[str] | None = None, k: int | None = None) -> list[Fit]:
    return default_specialist().seed(target, dry, models, k)


def score(target: np.ndarray, candidate: np.ndarray, dry: np.ndarray, gain_free: bool | None = None) -> float:
    return default_specialist().score(target, candidate, dry, gain_free)


def screen_hint(target: np.ndarray, dry: np.ndarray, models: Sequence[str] | None = None, k: int = 5) -> ScreenHint:
    return default_specialist().screen_hint(target, dry, models, k)


def budget_share() -> float:
    return BUDGET_SHARE
