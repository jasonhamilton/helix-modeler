"""Step 1 harness: play the probe signals through the synthetic systems of `distortion_synth` and
score every `distortion_id` estimator against the known truth (T171). Research code; no score or
method here is adopted (DECISIONS 2026-10-18)."""
from __future__ import annotations

import numpy as np

from . import distortion_id as D
from . import distortion_synth as S

SR = S.SR
ESS_LEVELS = [-60.0, -42.0, -30.0, -24.0, -18.0, -12.0, -6.0, 0.0]   # dfp-v1 (note 5.1)
DFP_LEVELS = list(np.arange(-54.0, -3.0, 5.0))                      # -54 .. -4, 5 dB steps
PROBE_V1_LEVELS = list(np.arange(-45.0, 0.0, 6.0))                  # -45 .. -3, 6 dB steps
F1, F2 = 20.0, 20000.0
FGRID_ESS = 1000.0 * 2.0 ** (np.arange(-24, 25) / 6.0)              # 1/6 octave, 62.5 Hz..16 kHz
FGRID_ESS = FGRID_ESS[FGRID_ESS >= 40.0]
FGRID_STEADY = np.array([50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250,
                         1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000], float)
ORDERS = (1, 2, 3, 4, 5)
EXTRACT_ORDERS = tuple(range(1, 10))     # orders checked in the ESS extraction test
F0 = 1000.0
K_CURVE = int(0.45 * SR // F0)          # 21 harmonics of 1 kHz below 21.6 kHz


# ---------------------------------------------------------------------------- signals

def staircase(f0: float, levels, step_s: float = 0.3, phase0: float = 0.0):
    n = int(np.rint(step_s * SR))
    t = np.arange(n * len(levels)) / SR
    amp = np.repeat(10 ** (np.asarray(levels) / 20), n)
    return amp * np.cos(2 * np.pi * f0 * t + phase0), n


def staircase_harmonics(y, f0, levels, n, K, win=(0.1, 0.3)):
    a0, a1 = int(win[0] * SR), int(win[1] * SR)
    out = []
    for i in range(len(levels)):
        seg = y[i * n + a0:i * n + a1]
        # time origin of each window: shift phases so they refer to t = 0 of the staircase
        a = D.sine_harmonics(seg, f0, SR, K)
        t0 = (i * n + a0) / SR
        k = np.arange(K + 1)
        out.append(a * np.exp(-1j * 2 * np.pi * k * f0 * t0))
    return np.array(out)


def run_ess(system, T: float):
    x, L, _ = D.ess_sync(F1, F2, T, SR)
    tail = np.zeros(int(1.0 * SR))
    sweeps = {lv: system.process(np.concatenate([10 ** (lv / 20) * x, tail])) for lv in ESS_LEVELS}
    return x, L, sweeps


# ---------------------------------------------------------------------------- truth

def truth_table(system, fgrid, levels, orders=ORDERS):
    """True M_k(f, l) = |c_k(A |P(f)|)| |Q(k f)| for a W-H system."""
    Pm = np.abs(system.P(fgrid))
    M = {k: np.full((len(fgrid), len(levels)), np.nan) for k in orders}
    for i, f in enumerate(fgrid):
        for j, lv in enumerate(levels):
            c = D.chebyshev_truth(system.f, 10 ** (lv / 20) * Pm[i], max(orders), 4096)
            for k in orders:
                if k * f <= 0.45 * SR:
                    M[k][i, j] = D.db(c[k] * abs(system.Q([k * f])[0]))
    return M


def g0_of(fun, eps: float = 1e-7):
    return (fun(np.array([eps]))[0] - fun(np.array([-eps]))[0]) / (2 * eps)


def _stats(err):
    e = np.abs(np.asarray(err, float))
    e = e[np.isfinite(e)]
    if e.size == 0:
        return {"n": 0, "median": np.nan, "p95": np.nan, "max": np.nan}
    return {"n": int(e.size), "median": float(np.median(e)), "p95": float(np.percentile(e, 95)),
            "max": float(e.max())}


def _band_mask(truth_db, rel=-40.0):
    return np.isfinite(truth_db) & (truth_db >= np.nanmax(truth_db) + rel)


# ---------------------------------------------------------------------------- evaluation

def evaluate(system, fast: bool = False) -> dict:
    """All estimators on one system. `fast` skips the 1.5 s sweep and the steady multi-frequency
    route (used by the tests)."""
    r = {"name": system.name, "description": system.description, "is_wh": system.is_wh,
         "notes": list(system.notes)}
    has_truth = system.f is not None

    # --- ESS (6 s and 1.5 s)
    ess = {}
    for T in ((6.0,) if fast else (6.0, 1.5)):
        x, L, sweeps = run_ess(system, T)
        lev, M = D.ess_harmonic_table(sweeps, x, L, SR, F1, F2, FGRID_ESS,
                                      orders=max(EXTRACT_ORDERS))
        ess[T] = {"x": x, "L": L, "sweeps": sweeps, "M": M, "levels": lev}
    r["ess_L"] = {T: v["L"] for T, v in ess.items()}

    # timing rule, on the loudest sweep
    for T, v in ess.items():
        hr = D.ess_harmonics(v["sweeps"][0.0], v["x"], v["L"], SR, F1, F2, orders=9)
        r.setdefault("timing", {})[T] = {
            "predicted_gap_9_10_ms": 1e3 * v["L"] * np.log(10 / 9),
            "peak_offset_ms": {k: 1e3 * o for k, o in hr["peak_offset_s"].items()}}

    if has_truth:
        Mt = truth_table(system, FGRID_ESS, ESS_LEVELS, EXTRACT_ORDERS)
        r["truth_M"] = Mt
        for T, v in ess.items():
            per_k = {}
            for k in EXTRACT_ORDERS:
                t, e = Mt[k], v["M"][k]
                # only where the order carries energy: within 60 dB of its peak, above -100 dBFS
                # and not more than 80 dB under the fundamental
                m = np.isfinite(t) & np.isfinite(e) & (t >= np.nanmax(t) - 60) & (t > -100) \
                    & (t >= np.nanmax(Mt[1]) - 80)
                per_k[k] = _stats((e - t)[m])
            r.setdefault("ess_extraction", {})[T] = per_k

    # --- small-signal total response from the -60 dBFS sweep
    M6 = ess[6.0]["M"]
    ss_est = M6[1][:, 0] - ESS_LEVELS[0]
    r["small_signal_est_db"] = ss_est
    if has_truth:
        g0 = g0_of(system.f)
        ss_true = D.db(g0 * np.abs(system.P(FGRID_ESS)) * np.abs(system.Q(FGRID_ESS)))
        m = _band_mask(ss_true)
        r["small_signal_true_db"] = ss_true
        r["small_signal_err"] = _stats((ss_est - ss_true)[m])
        r["g0_db"] = float(D.db(g0))

    # --- pre/post split, ESS route
    sp = D.level_shift_split(ess[6.0]["levels"], M6, FGRID_ESS, f_ref=1000.0)
    r["split_ess"] = sp
    if system.P is not None:
        r["split_ess_err"] = split_errors(system, sp)

    # --- steady 1 kHz staircases (dfp grid and probe-v1 grid)
    y_in, n = staircase(F0, DFP_LEVELS)
    a_dfp = staircase_harmonics(system.process(y_in), F0, DFP_LEVELS, n, K_CURVE)
    y_in, n = staircase(F0, PROBE_V1_LEVELS)
    a_p1 = staircase_harmonics(system.process(y_in), F0, PROBE_V1_LEVELS, n, 9)
    fund_dfp = D.db(np.abs(a_dfp[:, 1]))
    fund_p1 = D.db(np.abs(a_p1[:, 1]))
    r["knee"] = {"dfp_5db": D.p1db_from_levels(DFP_LEVELS, fund_dfp),
                 "probe_v1_6db": D.p1db_from_levels(PROBE_V1_LEVELS, fund_p1),
                 "ess_8lev": D.p1db_from_levels(ESS_LEVELS, M6[1][list(FGRID_ESS).index(1000.0)])}
    if has_truth:
        r["knee"]["truth"] = D.describing_p1db(system.f, np.arange(-80, 6, 0.25))

    # --- static curve from one dfp level, three corrections. Two levels are reported: the top
    # (-4 dBFS) and the level nearest 10 dB above the knee ESTIMATED from the dfp run.
    Qhat = q_from_split(sp)
    kd = r["knee"]["dfp_5db"]
    j_k10 = int(np.argmin(np.abs(np.asarray(DFP_LEVELS) - ((kd if kd is not None else -54) + 10))))
    r["curve_levels"] = {}
    for tag, j in (("top", len(DFP_LEVELS) - 1), ("knee_plus_10", j_k10)):
        r["curve_levels"][tag] = curve_eval(system, a_dfp[j], DFP_LEVELS[j], Qhat)
    r["curve"] = r["curve_levels"]["top"]
    A = 10 ** (DFP_LEVELS[-1] / 20)
    a_top = a_dfp[-1]
    if has_truth:
        # hardness: Rapp on each side, fitted on the knee+10 dB curve (the top-level curve is
        # almost all ceiling), truth vs estimate on the same x range
        cv = r["curve_levels"]["knee_plus_10"]
        xs = cv["x"]
        ft = cv["truth"]["y"]
        hard = {}
        for nm, y in (("truth", ft), ("split_post", cv.get("split_post", {}).get("y")),
                      ("oracle_post", cv.get("oracle_post", {}).get("y"))):
            if y is None:
                continue
            pos = D.fit_rapp(xs[xs > 0], y[xs > 0])
            neg = D.fit_rapp(-xs[xs < 0], -y[xs < 0])
            hard[nm] = {"pos": pos, "neg": neg}
        r["hardness"] = hard
        # knee from the Rapp fit of the estimated curve (1 dB point of its describing function)
        g0, v, p, _ = hard["split_post"]["pos"]
        g0n, vn, pn, _ = hard["split_post"]["neg"]
        if np.isfinite([g0, v, p, g0n, vn, pn]).all():
            def frapp(u):
                u = np.asarray(u)
                return np.where(u >= 0, D.rapp(u, g0, v, p), -D.rapp(-u, g0n, vn, pn))
            r["knee"]["rapp_fit"] = D.describing_p1db(frapp, np.arange(-80, 6, 0.25))
    r["sym_output_dc_re_fund"] = float(a_top[0].real / (abs(a_top[1]) + 1e-30))
    r["sym_uncorrected_even_share_db"] = D.symmetry_stats(
        np.concatenate([[0.0], np.abs(a_top[1:])]), A)["even_share_db"]

    # --- steady multi-frequency route of the split (the note's literal derivation)
    if not fast:
        Ms = {k: np.full((FGRID_STEADY.size, len(DFP_LEVELS)), np.nan) for k in ORDERS}
        for i, f in enumerate(FGRID_STEADY):
            y_in, n = staircase(f, DFP_LEVELS)
            a = staircase_harmonics(system.process(y_in), f, DFP_LEVELS, n, max(ORDERS))
            for k in ORDERS:
                if k * f <= 0.45 * SR:
                    Ms[k][i] = D.db(np.abs(a[:, k]))
        sps = D.level_shift_split(DFP_LEVELS, Ms, FGRID_STEADY, f_ref=1000.0)
        r["split_steady"] = sps
        if system.P is not None:
            r["split_steady_err"] = split_errors(system, sps)

    # --- memory flags: hysteresis (220 Hz up vs down) and bias settling after a burst
    lv = DFP_LEVELS
    y_up, n = staircase(220.0, lv)
    y_dn, _ = staircase(220.0, lv[::-1])
    a_up = staircase_harmonics(system.process(y_up), 220.0, lv, n, 3)
    a_dn = staircase_harmonics(system.process(y_dn), 220.0, lv[::-1], n, 3)[::-1]
    hy = {}
    for k in (1, 3):
        u, d_ = D.db(a_up[:, k]), D.db(a_dn[:, k])
        m = (np.maximum(u, d_) >= D.db(a_up[:, 1]) - 80.0) & (np.maximum(u, d_) > -120.0)
        hy[f"H{k}"] = float(np.max(np.abs(u - d_)[m])) if m.any() else 0.0
    r["hysteresis_db"] = hy
    burst = np.concatenate([10 ** (-30 / 20) * np.ones(int(0.5 * SR)),
                            10 ** (-6 / 20) * np.ones(int(0.5 * SR)),
                            10 ** (-30 / 20) * np.ones(int(0.5 * SR))])
    t = np.arange(burst.size) / SR
    yb = system.process(burst * np.sin(2 * np.pi * 220.0 * t))
    st = D.settle_after_burst(yb, SR, 220.0, 1.0 + 1 / 220.0, 0.49)
    st["tau_true_s"] = system.tau_s
    r["settle"] = st
    return r


def curve_eval(system, a, level_db, Qhat) -> dict:
    """Chebyshev static curve from one steady 1 kHz level, three post-filter corrections, and its
    error against the true curve (dB, where |f| >= 0.1 max|f|)."""
    A = 10 ** (level_db / 20)
    xs = np.linspace(-A, A, 1601)
    out = {"A": A, "level_db": level_db, "x": xs}
    variants = {"uncorrected": None, "oracle_post": system.Q, "split_post": Qhat}
    for nm, Q in variants.items():
        if nm == "oracle_post" and Q is None:
            continue
        if nm == "split_post" and Q is None:
            continue
        c = D.chebyshev_coeffs(a, F0, post=Q)
        out[nm] = {"c": c, "y": D.chebyshev_curve(c, A, xs), "sym": D.symmetry_stats(c, A)}
    if system.f is not None:
        ft = system.f(xs)
        ct = D.chebyshev_truth(system.f, A, K_CURVE)
        ftr = D.chebyshev_curve(ct, A, xs)
        out["truth"] = {"y": ft, "c": ct, "sym": D.symmetry_stats(ct, A),
                        "dc_true": float(ct[0])}
        m = np.abs(ft) >= 0.1 * np.abs(ft).max()
        for nm in ("uncorrected", "oracle_post", "split_post"):
            if nm in out:
                out[nm]["err"] = _stats(D.db(out[nm]["y"][m]) - D.db(ft[m]))
                out[nm]["err_vs_truncated"] = _stats(D.db(out[nm]["y"][m]) - D.db(ftr[m]))
    return out


def q_from_split(sp, rel_floor_db: float = -60.0):
    """Minimum-phase post-filter estimate from the identifiable rows of a split."""
    m = sp["identifiable"] & np.isfinite(sp["Q_db"])
    if m.sum() < 3:
        return None
    return D.min_phase_from_db(sp["f"][m], np.maximum(sp["Q_db"][m], rel_floor_db), SR)


def split_errors(system, sp) -> dict:
    f = sp["f"]
    Pt = D.db(system.P(f))
    Qt = D.db(system.Q(f))
    ident = sp["identifiable"]
    out = {}
    for nm, t, e in (("P", Pt, sp["P_db"]), ("Q", Qt, sp["Q_db"])):
        band = _band_mask(t)
        both = band & ident & np.isfinite(e)
        out[nm] = {"in_band_points": int(band.sum()),
                   "identifiable_points": int(both.sum()),
                   "coverage": float(both.sum() / max(band.sum(), 1)),
                   "err_identifiable": _stats((e - t)[both]),
                   "err_all_fitted": _stats((e - t)[band & np.isfinite(e)])}
    prod_t = Pt + Qt
    band = _band_mask(prod_t)
    out["product"] = _stats((sp["product_db"] - prod_t)[band & np.isfinite(sp["product_db"])])
    out["residual_rms_db"] = sp["residual_rms_db"]
    return out


def run_all(fast: bool = False, names=None):
    out = []
    for s in S.build_systems():
        if names and s.name not in names:
            continue
        out.append(evaluate(s, fast=fast))
    return out
