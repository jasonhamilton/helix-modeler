"""Distortion stage identification with the dry signal known (T171, step 1 of
docs/notes/distortion-identification.md section 8). numpy/scipy only. RESEARCH CODE: nothing here
is an admitted score or an adopted method; the human approves any use (DECISIONS 2026-10-18).

Model: Wiener-Hammerstein (W-H) = pre-filter P -> memoryless curve f -> post-filter Q.
Scale convention (note section 3.3): |P(1 kHz)| = |Q(1 kHz)| = 1, all gain lives in f, and levels
are input-referred dBFS peak at 1 kHz.

Estimators
- `ess_sync` / `ess_harmonics`: synchronized exponential sine sweep (Novak) and the harmonic-order
  responses H_k, cut out of one deconvolved record at -L ln k (Farina's timing rule).
- `sine_harmonics`: complex harmonic amplitudes of a steady sine (least squares).
- `chebyshev_curve`: static curve f(x) = sum c_k T_k(x / A) from one steady sine, with the
  post-filter removed (magnitude and minimum phase).
- `p1db_from_levels`, `fit_rapp`: knee (1 dB compression point) and hardness.
- `symmetry_stats`: even-harmonic share, ceiling ratio, DC.
- `level_shift_split`: the note's untested pre/post split: harmonic-vs-level curves at frequency f
  are the reference curves shifted along the level axis by |P(f)| (horizontal) and up by |Q(kf)|
  (vertical). The residual of the alignment measures how far the device is from W-H.
- `settle_time_constant`: bias settling after a loud burst (memory flag).
"""
from __future__ import annotations

import numpy as np
from scipy import interpolate, optimize

EPS = 1e-30


def db(x, floor: float = -300.0):
    return np.maximum(20.0 * np.log10(np.maximum(np.abs(x), EPS)), floor)


# ----------------------------------------------------------------------------------- sweeps

def ess_sync(f1: float, f2: float, T: float, sr: int, fade_s: float = 0.02):
    """Synchronized ESS (Novak 2015): L = round(f1 T / ln(f2/f1)) / f1, so that harmonic k of the
    sweep is the sweep itself advanced by L ln k. Returns (x, L, T_actual)."""
    L = round(f1 * T / np.log(f2 / f1)) / f1
    T_act = L * np.log(f2 / f1)
    t = np.arange(int(np.rint(T_act * sr))) / sr
    x = np.sin(2 * np.pi * f1 * L * (np.exp(t / L) - 1.0))
    nf = int(fade_s * sr)
    if nf > 0:
        x[:nf] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(nf) / nf)
        no = max(nf // 4, 1)    # short fade-out: the top of the sweep is above the analysis band
        x[-no:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(no) / no)
    return x, L, T_act


def harmonic_delay_s(L: float, k: int) -> float:
    """Farina/Novak timing rule: order k appears L ln k seconds before the linear response."""
    return L * np.log(k)


def ess_deconvolve(y: np.ndarray, x: np.ndarray, sr: int, f1: float, f2: float, nfft: int):
    """Circular deconvolution h = IFFT(Y / X), regularised outside [f1, f2]."""
    X = np.fft.rfft(x, nfft)
    Y = np.fft.rfft(y, nfft)
    f = np.fft.rfftfreq(nfft, 1 / sr)
    band = (f >= f1) & (f <= f2)
    reg = np.where(band, 1e-12, 1.0) * np.max(np.abs(X)) ** 2
    return np.fft.irfft(Y * np.conj(X) / (np.abs(X) ** 2 + reg), nfft)


def ess_harmonics(y: np.ndarray, x: np.ndarray, L: float, sr: int, f1: float, f2: float,
                  orders: int = 9, lin_len_s: float = 0.3, taper: float = 0.1):
    """Harmonic-order responses H_k (complex spectra on the rfft grid of `nfft`) from one sweep.
    Window for order k runs between the midpoints to its neighbours (order 1 gets `lin_len_s`).
    Returns dict(f=freqs, H={k: spectrum}, peak_offset_s={k: measured - predicted}, nfft)."""
    nfft = 1 << int(np.ceil(np.log2(len(y) + int(L * np.log(orders + 1) * sr) + sr)))
    h = ess_deconvolve(y, x, sr, f1, f2, nfft)
    pos = {k: -harmonic_delay_s(L, k) * sr for k in range(1, orders + 2)}
    H, peak = {}, {}
    f = np.fft.rfftfreq(nfft, 1 / sr)
    for k in range(1, orders + 1):
        lo = 0.5 * (pos[k] + pos[k + 1])
        hi = pos[k] + lin_len_s * sr if k == 1 else 0.5 * (pos[k] + pos[k - 1])
        i0, i1 = int(np.floor(lo)), int(np.ceil(hi))
        idx = np.arange(i0, i1)
        seg = h[idx % nfft].copy()
        n = seg.size
        nt = max(int(taper * n), 2)
        w = np.ones(n)
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(nt) / nt)
        w[:nt] = ramp
        w[-nt:] = ramp[::-1]
        seg *= w
        c = int(np.rint(pos[k]))
        peak[k] = (idx[int(np.argmax(np.abs(seg)))] - pos[k]) / sr
        buf = np.zeros(nfft)
        buf[(idx - c) % nfft] = seg      # re-reference time origin to the predicted position
        H[k] = np.fft.rfft(buf)
    return {"f": f, "H": H, "peak_offset_s": peak, "nfft": nfft}


def band_mag_db(f: np.ndarray, H: np.ndarray, centres, frac_oct: float = 1 / 12):
    """Power-average |H| in dB over a band of width `frac_oct` around each centre."""
    out = []
    p = np.abs(H) ** 2
    for c in np.atleast_1d(centres):
        m = (f >= c * 2 ** (-frac_oct / 2)) & (f <= c * 2 ** (frac_oct / 2))
        out.append(10 * np.log10(max(p[m].mean(), EPS)) if m.any() else np.nan)
    return np.array(out)


def ess_harmonic_table(sweeps: dict, x: np.ndarray, L: float, sr: int, f1: float, f2: float,
                       fgrid, orders: int = 5, max_out_hz: float | None = None):
    """M[k][i_f, j_level] = |H_k(k f)| in dB: the output amplitude of harmonic k when a sine of
    frequency f and amplitude A (the sweep level) is played. `sweeps` maps level dBFS -> output
    of the system for the sweep A * x (deconvolved with the unit sweep x)."""
    levels = sorted(sweeps)
    max_out = max_out_hz or 0.45 * sr
    M = {k: np.full((len(fgrid), len(levels)), np.nan) for k in range(1, orders + 1)}
    for j, lv in enumerate(levels):
        r = ess_harmonics(sweeps[lv], x, L, sr, f1, f2, orders=max(orders, 2))
        for k in range(1, orders + 1):
            kf = np.asarray(fgrid) * k
            ok = (kf <= min(f2 * 0.95, max_out)) & (kf >= k * f1 * 1.5)
            vals = band_mag_db(r["f"], r["H"][k], kf)
            M[k][ok, j] = vals[ok]
    return np.array(levels, float), M


# ----------------------------------------------------------------------------- steady sines

def sine_harmonics(y: np.ndarray, f0: float, sr: int, K: int):
    """Least-squares complex amplitudes a_k (k = 0..K) with y ~ Re sum a_k exp(j k w t);
    a_0 is the DC (real). Harmonics at or above 0.48 sr are not fitted (they alias onto lower ones and would steal
    their energy) and come back as 0."""
    t = np.arange(y.size) / sr
    Kf = min(K, int(np.floor(0.48 * sr / f0)))
    cols = [np.ones_like(t)]
    for k in range(1, Kf + 1):
        cols += [np.cos(2 * np.pi * k * f0 * t), np.sin(2 * np.pi * k * f0 * t)]
    B = np.stack(cols, axis=1)
    c, *_ = np.linalg.lstsq(B, y, rcond=None)
    a = np.zeros(K + 1, complex)
    a[0] = c[0]
    a[1:Kf + 1] = c[1::2] - 1j * c[2::2]
    return a


def min_phase_from_db(freqs_hz, mag_db, sr: int, nfft: int = 1 << 15):
    """Minimum-phase complex response (cepstral method) from a magnitude on a log grid;
    returns a callable F(f) -> complex."""
    f = np.fft.rfftfreq(nfft, 1 / sr)
    lf = np.log(np.maximum(f, 1e-3))
    xs = np.log(np.asarray(freqs_hz, float))
    ys = np.asarray(mag_db, float)
    m = np.isfinite(ys)
    xs, ys = xs[m], ys[m]
    v = np.interp(lf, xs, ys)
    # above the last point continue the slope of the last octave (flat would bend the phase)
    top = xs >= xs[-1] - np.log(2)
    slope = np.polyfit(xs[top], ys[top], 1)[0] if top.sum() >= 2 else 0.0
    hi = lf > xs[-1]
    v[hi] = ys[-1] + slope * (lf[hi] - xs[-1])
    logmag = v * np.log(10) / 20
    c = np.fft.irfft(logmag, nfft)
    fold = np.zeros(nfft)
    fold[0] = c[0]
    fold[1:nfft // 2] = 2 * c[1:nfft // 2]
    fold[nfft // 2] = c[nfft // 2]
    Hm = np.exp(np.fft.rfft(fold))

    def F(q):
        q = np.atleast_1d(q)
        return np.interp(q, f, Hm.real) + 1j * np.interp(q, f, Hm.imag)
    return F


def chebyshev_coeffs(a: np.ndarray, f0: float, post=None, drop_below_db: float = -40.0,
                     post_ref_db: float = 0.0):
    """Clipper Chebyshev coefficients c_k from measured complex harmonics a_k at f0.
    a_k = c_k Q(k f0) exp(j k phi): phi is removed via the fundamental (c_1 > 0 assumed).
    `post` is a callable complex Q(f) (None = no correction). Harmonics where |Q| is below
    `drop_below_db` (re `post_ref_db`) are dropped. Returns real c_0..c_K."""
    K = a.size - 1
    k = np.arange(1, K + 1)
    if post is not None:
        Qk = post(k * f0)
        Q0 = post(np.array([1e-3]))[0]
    else:
        Qk = np.ones(K, complex)
        Q0 = 1.0 + 0j
    phi = np.angle(a[1] / Qk[0])
    c = np.zeros(K + 1)
    corr = a[1:] / Qk * np.exp(-1j * k * phi)
    keep = db(Qk) - post_ref_db >= drop_below_db
    c[1:] = np.where(keep, corr.real, 0.0)
    c[0] = (a[0] / Q0).real if abs(Q0) > 10 ** (drop_below_db / 20) else np.nan
    return c


def chebyshev_curve(c: np.ndarray, A: float, x: np.ndarray):
    """f(x) = c_0 + sum_k c_k T_k(x / A), x in [-A, A] (input as A cos)."""
    u = np.clip(np.asarray(x) / A, -1, 1)
    c = np.nan_to_num(c, nan=0.0)
    return np.polynomial.chebyshev.chebval(u, c)


def chebyshev_truth(fun, A: float, K: int, n: int = 8192):
    """Exact c_k of f(A cos theta) (FFT over one period)."""
    th = 2 * np.pi * np.arange(n) / n
    v = np.fft.rfft(fun(A * np.cos(th))) / n
    c = np.zeros(K + 1)
    c[0] = v[0].real
    c[1:] = 2 * v[1:K + 1].real
    return c


# ----------------------------------------------------------------------------- knee, hardness

def p1db_from_levels(levels_db, fund_out_db, drop: float = 1.0):
    """Input level where the fundamental gain is `drop` dB below its value at the lowest level
    (linear interpolation). None if never reached; the lowest level if already reached there."""
    L = np.asarray(levels_db, float)
    g = np.asarray(fund_out_db, float) - L
    g0 = g[0]
    below = np.nonzero(g <= g0 - drop)[0]
    if below.size == 0:
        return None
    i = int(below[0])
    if i == 0:
        return float(L[0])
    y0, y1 = g[i - 1] - g0, g[i] - g0
    return float(L[i - 1] + (-drop - y0) * (L[i] - L[i - 1]) / (y1 - y0 + EPS))


def describing_p1db(fun, levels_db, drop: float = 1.0):
    """Truth P1dB from a known curve: fundamental of f(A cos) over a dense level grid."""
    L = np.asarray(levels_db, float)
    out = [db(chebyshev_truth(fun, 10 ** (lv / 20), 1, 4096)[1]) for lv in L]
    return p1db_from_levels(L, np.array(out), drop)


def rapp(x, g0, v, p):
    gx = g0 * np.asarray(x)
    return gx / (1.0 + np.abs(gx / v) ** p) ** (1.0 / p)


def fit_rapp(x, y):
    """Fit f(x) = g0 x / (1 + |g0 x / v|^p)^(1/p) on one side (x > 0, y > 0), least squares in dB.
    Returns (g0, v, p, rms_db)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    m = (x > 0) & (y > 0)
    x, y = x[m], y[m]
    if x.size < 5:
        return (np.nan, np.nan, np.nan, np.nan)
    g_init = y[: max(3, x.size // 20)].sum() / x[: max(3, x.size // 20)].sum()

    def res(q):
        g0, v, p = np.exp(q)
        return db(rapp(x, g0, v, p)) - db(y)
    best = None
    for p0 in (1.0, 2.5, 8.0):
        q0 = np.log([max(g_init, 1e-6), max(y.max(), 1e-9), p0])
        try:
            r = optimize.least_squares(res, q0, bounds=(np.log([1e-6, 1e-9, 0.2]),
                                                        np.log([1e9, 1e3, 200.0])))
        except ValueError:
            continue
        if best is None or r.cost < best.cost:
            best = r
    g0, v, p = np.exp(best.x)
    return (float(g0), float(v), float(p), float(np.sqrt(np.mean(best.fun ** 2))))


# ----------------------------------------------------------------------------- symmetry

def symmetry_stats(c: np.ndarray, A: float):
    """Even-harmonic share sum(c_even^2)/sum(c_k^2) (k >= 1), ceiling ratio max f / |min f| on
    [-A, A], and DC c_0 relative to c_1."""
    ck = np.nan_to_num(c[1:])
    k = np.arange(1, ck.size + 1)
    tot = float(np.sum(ck ** 2)) + EPS
    even = float(np.sum(ck[k % 2 == 0] ** 2))
    xs = np.linspace(-A, A, 2001)
    fx = chebyshev_curve(c, A, xs)
    pos, neg = float(fx.max()), float(-fx.min())
    return {"even_share": even / tot,
            "even_share_db": 10 * np.log10(even / tot + 1e-15),
            "ceiling_ratio": pos / neg if neg > 0 and pos > 0 else np.nan,
            "dc_re_fund": float(c[0] / (abs(c[1]) + EPS)) if np.isfinite(c[0]) else np.nan}


# ----------------------------------------------------------------------------- pre/post split

def _pchip(xs, ys):
    o = np.argsort(xs)
    xs, ys = np.asarray(xs)[o], np.asarray(ys)[o]
    u, i = np.unique(xs, return_index=True)
    return interpolate.PchipInterpolator(u, ys[i], extrapolate=False)


def level_shift_split(levels_db, M: dict, fgrid, f_ref: float = 1000.0, orders=(1,),
                      check_orders=(2, 3, 4, 5),
                      floor_db: float = 70.0, s_range: float = 45.0, s_step: float = 0.05,
                      iters: int = 3, min_points: int = 4, width_db: float = 1.0,
                      min_overlap: float = 0.5):
    """The level-shift pre/post split (note section 3.3, [P]).

    Model: M_k(f, l) [dB] = R_k(l + s(f)) + t_k(f), with s(f) = 20log|P(f)| (horizontal shift),
    t_1(f) = 20log|Q(f)| (vertical shift of the fundamental), R_k the reference curve of order k
    (from f_ref, where s = t = 0 by convention). After a first pass the reference curves are
    re-built from every identifiable frequency (consensus), then the shifts are re-fitted.
    Points more than `floor_db` below each order's maximum are ignored.

    The shift is fitted on `orders` (default the fundamental only: in the synthetic test the
    harmonic curves, which have notches where c_k changes sign, made the fit worse). The
    harmonics in `check_orders` are then used as a W-H consistency check: with the shift fixed,
    each order's curve must also line up (free vertical offset, i.e. Q at k f); their median
    absolute misfit is `harmonic_residual_db`.

    Returns dict with P_db, Q_db (per fgrid, NaN where no fit), width_db (half-width of the
    profile-cost minimum: large = shift not identifiable), identifiable mask, residual_rms_db
    (fundamental, all points of identifiable frequencies), resid_per_f, harmonic_residual_db."""
    L = np.asarray(levels_db, float)
    fgrid = np.asarray(fgrid, float)
    iref = int(np.argmin(np.abs(np.log(fgrid / f_ref))))
    orders = [k for k in orders if k in M]
    peak = {k: np.nanmax(M[k]) for k in orders}
    valid = {k: np.isfinite(M[k]) & (M[k] >= peak[k] - floor_db) for k in orders}
    svals = np.arange(-s_range, s_range + s_step / 2, s_step)

    # initial reference: f_ref row
    ref = {}
    for k in orders:
        m = valid[k][iref]
        ref[k] = _pchip(L[m], M[k][iref, m]) if m.sum() >= 2 else None

    def fit_row(i):
        J = np.full(svals.size, np.inf)
        npts = np.zeros(svals.size, int)
        offs = np.full((svals.size, len(orders)), np.nan)
        n_avail = sum(int(valid[k][i].sum()) for k in orders)
        for si, s in enumerate(svals):
            tot, n = 0.0, 0
            for ki, k in enumerate(orders):
                if ref[k] is None:
                    continue
                m = valid[k][i]
                if not m.any():
                    continue
                pred = ref[k](L[m] + s)
                ok = np.isfinite(pred)
                if ok.sum() < (3 if k == 1 else 2):
                    continue
                d = M[k][i, m][ok] - pred[ok]
                t = d.mean()
                offs[si, ki] = t
                tot += float(np.sum((d - t) ** 2))
                n += int(ok.sum())
            if np.isfinite(offs[si, 0]):
                J[si] = tot
                npts[si] = n
        # eligible shifts: the fundamental overlaps the reference on >= 3 levels and at least
        # `min_overlap` of this row's points are used (stops tiny overlaps from winning)
        elig = np.isfinite(J) & (npts >= max(min_points, int(np.ceil(min_overlap * n_avail))))
        if not elig.any():
            return None
        Jmean = np.where(elig, J / np.maximum(npts, 1), np.inf)
        si = int(np.argmin(Jmean))
        sigma2 = max(Jmean[si], 0.05 ** 2)
        inside = elig & ((Jmean - Jmean[si]) * npts[si] / sigma2 <= 4.0)
        w = float(svals[inside].max() - svals[inside].min()) / 2 if inside.any() else np.inf
        # a minimum on the edge of the eligible region is not trustworthy
        edge = (si == 0) or (si == svals.size - 1) or not elig[si - 1] or not elig[si + 1]
        return {"s": float(svals[si]), "t": offs[si], "cost": float(J[si]), "n": int(npts[si]),
                "width": w if not edge else np.inf}

    for it in range(iters):
        rows = [fit_row(i) for i in range(fgrid.size)]
        ident = np.array([r is not None and r["width"] <= width_db for r in rows])
        ident[iref] = True
        if it == iters - 1:
            break
        # consensus reference from identifiable rows, anchored at f_ref (s = t = 0 there)
        new = {}
        for ki, k in enumerate(orders):
            xs, ys = [], []
            for i, r in enumerate(rows):
                if r is None or not ident[i]:
                    continue
                s, t = (0.0, 0.0) if i == iref else (r["s"], r["t"][ki])
                if not np.isfinite(t):
                    continue
                m = valid[k][i]
                xs.append(L[m] + s)
                ys.append(M[k][i, m] - t)
            if xs:
                xs, ys = np.concatenate(xs), np.concatenate(ys)
                # bin-average onto a 1 dB grid, then smooth interpolant
                b = np.round(xs)
                ub = np.unique(b)
                yb = np.array([ys[b == q].mean() for q in ub])
                new[k] = _pchip(ub, yb) if ub.size >= 2 else ref[k]
            else:
                new[k] = ref[k]
        ref = new

    P = np.full(fgrid.size, np.nan)
    Q = np.full(fgrid.size, np.nan)
    W = np.full(fgrid.size, np.inf)
    R = np.full(fgrid.size, np.nan)
    tot, ntot = 0.0, 0
    for i, r in enumerate(rows):
        if r is None:
            continue
        P[i], Q[i], W[i] = r["s"], r["t"][0], r["width"]
        R[i] = np.sqrt(r["cost"] / max(r["n"], 1))
        if ident[i]:
            tot += r["cost"]
            ntot += r["n"]
    P[iref], Q[iref], W[iref] = 0.0, 0.0, 0.0
    harm = _harmonic_consistency(L, M, fgrid, iref, P, ident, check_orders, floor_db)
    return {"f": fgrid, "P_db": P, "Q_db": Q, "width_db": W, "identifiable": ident,
            "residual_rms_db": float(np.sqrt(tot / max(ntot, 1))), "resid_per_f": R,
            "product_db": P + Q, "harmonic_residual_db": harm}


def _harmonic_consistency(L, M, fgrid, iref, P, ident, check_orders, floor_db, iters: int = 3):
    """Median |misfit| (dB) of the harmonic-vs-level curves once every identifiable row is moved
    by its fitted shift P(f). Per order: a consensus curve (median on a 1 dB grid), started from
    the reference row and re-built from all rows; one free vertical offset per row (Q at k f)."""
    out = {}
    rows = [i for i in range(fgrid.size) if ident[i] and np.isfinite(P[i])]
    for k in check_orders:
        if k not in M or not np.isfinite(np.nanmax(M[k])):
            continue
        peak = np.nanmax(M[k])
        if peak < np.nanmax(M[1]) - 40.0:      # order too weak to judge (or absent)
            out[k] = np.nan
            continue
        valid = np.isfinite(M[k]) & (M[k] >= peak - floor_db)
        use = [i for i in rows if valid[i].sum() >= 3]
        if iref not in use or len(use) < 3:
            out[k] = np.nan
            continue
        m0 = valid[iref]
        cons = _pchip(L[m0] + P[iref], M[k][iref, m0])
        off = {iref: 0.0}
        for _ in range(iters):
            for j in use:
                pred = cons(L[valid[j]] + P[j])
                ok = np.isfinite(pred)
                if ok.sum() >= 2:
                    off[j] = float(np.mean(M[k][j, valid[j]][ok] - pred[ok]))
            xs = np.concatenate([L[valid[j]] + P[j] for j in off])
            ys = np.concatenate([M[k][j, valid[j]] - off[j] for j in off])
            b = np.round(xs)
            ub = np.unique(b)
            cons = _pchip(ub, np.array([np.median(ys[b == q]) for q in ub]))
        res = []
        for j in off:
            pred = cons(L[valid[j]] + P[j])
            ok = np.isfinite(pred)
            res.append((M[k][j, valid[j]] - off[j] - pred)[ok])
        out[k] = float(np.median(np.abs(np.concatenate(res)))) if res else np.nan
    return out


# ----------------------------------------------------------------------------- memory flags

def settle_after_burst(y: np.ndarray, sr: int, f0: float, t_start_s: float, dur_s: float,
                       early_s: float = 0.05):
    """Bias settling after a loud burst. Per cycle of f0: peak asymmetry (max + min) / (|max| +
    |min|) of the output. Returns peak_change (largest |asymmetry - final| in the first
    `early_s`), settle_s (last time |asymmetry - final| > 10% of peak_change, or 0) and
    memory_flag (peak_change > 0.2). A W-H system with filters only rings for a few ms."""
    n = int(np.rint(sr / f0))
    a = int(t_start_s * sr)
    cyc = int(dur_s * sr) // n
    seg = y[a:a + cyc * n].reshape(cyc, n)
    hi, lo = seg.max(axis=1), seg.min(axis=1)
    asym = (hi + lo) / (np.abs(hi) + np.abs(lo) + EPS)
    t = (np.arange(cyc) + 0.5) * n / sr
    d = np.abs(asym - asym[-max(cyc // 5, 1):].mean())
    early = t <= early_s
    peak = float(d[early].max()) if early.any() else float(d[0])
    over = np.nonzero(d > 0.1 * peak)[0]
    settle = float(t[over[-1]]) if (over.size and peak > 1e-3) else 0.0
    return {"peak_change": peak, "settle_s": settle, "memory_flag": bool(peak > 0.2)}
