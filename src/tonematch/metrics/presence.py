"""Block-presence detector (T102): which effect categories a target guitar recording contains.

Human decision 2026-10-17 ("single blocks first; chains are built from analysis"): the chain builder
takes the categories measured here, arranges them with the chain-pattern prior
(`tonematch.chain_prior.top_structures`, which accepts a category -> probability dict) and searches
single blocks. This module measures; it does not choose models.

    rep = detect(target, di=dry)          # known-DI mode (a DI of the same performance)
    rep = detect(target)                  # no-DI mode (only what stays note-invariant)
    rep.probabilities()                   # {"drive": 0.93, "delay": 0.02, ...}
    rep.categories["delay"].estimate      # {"time_s": 0.352, "feedback_db": -6.1, ...}

Categories (the names of `chain_prior.CATEGORIES`): drive, amp, cab, comp, eq, mod, delay, reverb.
Each gets a probability from a small logistic model per category (one versus the rest, so a chain can
show several) over the measurements below, fitted on the TRAIN split of `presence-v1` (single Helix
blocks on clean DI) by `scripts/presence_eval.py` and stored in `data/presence/`. Without a fitted
model file, `detect` still returns every measurement and estimate, with probabilities None.

Known-DI measurements (target y against its DI x; both scaled to unit RMS, so level does not matter):

- **Linear part.** A 2.5 s impulse response h from regularised deconvolution of y by x
  (`_fx.impulse_response`). Its direct part (-2 ms to +30 ms) gives the magnitude response in
  1/3-octave bands: deviation from flat, tilt, high and low roll-off (EQ, cab). Its late part (from
  25 ms) gives the late-to-direct energy, discrete echo peaks (delay: time, feedback from the echo at
  twice the time) and the decay of the smooth tail (reverb: RT60 from a Schroeder curve, T20 or EDT).
- **What the linear part misses.** `lin_resid_db`: y minus h * x (80 Hz to 10 kHz), re y. Small for
  EQ, cab, delay and reverb; large for anything nonlinear or time-varying.
- **Frame gains.** On 20 ms frames, y against the linear prediction: a per-frame gain explains a
  compressor or a tremolo but not a distortion or a pitch wobble. `nl_resid_db` is what a per-frame
  gain cannot explain (drive, amp, chorus); `level_slope` the output level per dB of input level
  (1 linear, below 1 compressed); `gain_period_conf` and `gain_swing_db` the periodicity and swing of
  the gain once its dependence on the input level is regressed out (tremolo).
- **Modulation.** `modulation.mod_profile` (rate, rate confidence, level swing, notch sweep, pitch
  swing from the delay track, mean delay), which needs the block's input.
- **Dynamics and spectrum statistics.** Crest factor, spectral flatness and harmonic-to-noise ratio
  (`crest`), and envelope statistics (`envelope`), as target minus DI.

No-DI measurements (target only): the long-term spectrum (`ltas`: tilt, high and low roll-off,
roughness), `crest` and `envelope` statistics, echo peaks in the whitened autocorrelation (a delay
of T puts a peak at lag T whatever notes are played, but so do the DI's own repetitions), and the
periodicity of 1/3-octave band levels and of the spectral centroid (modulation). These cannot tell an
EQ from the guitar's own voicing; the evaluation reports how far they get.

Estimates (rule-based, independent of the logistic models): delay time and feedback, reverb RT60,
LFO rate and modulation sub-type (tremolo, chorus, vibrato, flanger, phaser; rules in
`mod_subtype`), amp and drive gain level (`gain_index`: what a per-frame gain cannot explain, in dB,
with labels clean / crunch / high), EQ tilt.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.linalg import solve_toeplitz
from scipy.ndimage import median_filter, uniform_filter1d
from scipy.signal import fftconvolve

from tonematch.metrics import crest, envelope, ltas, modulation
from tonematch.metrics._common import EPS, as_mono, frame
from tonematch.metrics._fx import (
    SR,
    db10,
    impulse_response,
    periodogram_peak,
    smooth_power,
    third_octave_centres,
)
from tonematch.metrics.reverb import schroeder_db

VERSION = "1.0"
CATEGORIES = ("drive", "amp", "cab", "comp", "eq", "mod", "delay", "reverb")
MOD_SUBTYPES = ("tremolo", "chorus", "vibrato", "flanger", "phaser")
MODES = ("known_di", "no_di")
MODEL_DIR = Path(__file__).resolve().parents[3] / "data" / "presence"

IR_LEN_S = 2.5
IR_PRE_S = 0.010
DIRECT_WIN_S = (-0.002, 0.030)
LATE_START_S = 0.025
MAG_BANDS = third_octave_centres(63.0, 12500.0)
FR_N, FR_HOP = 480, 240          # 10 ms frames, 5 ms hop (a 10 Hz tremolo moves 0.6 dB in one)
ECHO_RANGE_S = (0.030, 1.6)
GAIN_LABELS = ((-24.0, "clean"), (-12.0, "crunch"), (np.inf, "high"))   # gain_index upper bounds, dB


# ----------------------------------------------------------------------------------------- helpers

def _unit(x: np.ndarray) -> np.ndarray:
    return x / (np.sqrt(np.mean(x ** 2)) + EPS)


def _band(x: np.ndarray, lo: float = 80.0, hi: float = 10_000.0) -> np.ndarray:
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=SR, output="sos")
    return signal.sosfiltfilt(sos, x)


def _fin(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return f if np.isfinite(f) else float("nan")


def _band_db(power: np.ndarray, freqs: np.ndarray, centres: np.ndarray) -> np.ndarray:
    out = np.full(centres.size, np.nan)
    for i, c in enumerate(centres):
        sel = (freqs >= c / 2 ** (1 / 6)) & (freqs < c * 2 ** (1 / 6))
        if sel.any():
            out[i] = db10(power[sel].mean())
    return out


def _mean_bands(db: np.ndarray, lo: float, hi: float, centres: np.ndarray = MAG_BANDS) -> float:
    sel = (centres >= lo) & (centres <= hi) & np.isfinite(db)
    return float(np.mean(db[sel])) if sel.any() else float("nan")


def _local_peaks(e_db: np.ndarray, *, med_win: int, min_prom_db: float) -> tuple[np.ndarray, np.ndarray]:
    """Indices of local maxima of e_db standing `min_prom_db` above a running median, and their prominence."""
    base = median_filter(e_db, size=med_win, mode="nearest")
    prom = e_db - base
    pk, _ = signal.find_peaks(e_db, distance=max(med_win // 8, 1))
    pk = pk[prom[pk] >= min_prom_db]
    return pk, prom[pk]


def _echo_pick(times: np.ndarray, level_db: np.ndarray, prom_db: np.ndarray, *, within_db: float = 6.0) -> int | None:
    """The first echo: the earliest candidate within `within_db` of the loudest one (later echoes of a
    feedback train are quieter, so the loudest is usually echo 1; the earliest guards against echo 2)."""
    if times.size == 0:
        return None
    top = float(level_db.max())
    ok = np.flatnonzero(level_db >= top - within_db)
    return int(ok[np.argmin(times[ok])])


def _decay_rt60(e: np.ndarray, block_s: float = 0.01) -> tuple[float, float, str]:
    """(RT60 s, decay range dB, method) of an energy sequence: T20, else EDT scaled, else nan."""
    try:
        curve, rng = schroeder_db(e, sr=SR, block_s=block_s)
    except ValueError:
        return float("nan"), float("nan"), "none"

    def slope_time(hi: float, lo: float) -> float:
        sel = np.flatnonzero((curve <= hi) & (curve >= lo))
        if curve.min() > lo or sel.size < 3:
            return float("nan")
        cut = np.flatnonzero(curve < lo)
        sel = sel[sel <= cut[0]] if cut.size else sel
        if sel.size < 3:
            return float("nan")
        s = np.polyfit(sel * block_s, curve[sel], 1)[0]
        return float(-60.0 / s) if s < 0 else float("nan")

    t20 = slope_time(-5.0, -25.0)
    if np.isfinite(t20):
        return t20, float(rng), "T20"
    edt = slope_time(0.0, -10.0)
    return edt, float(rng), ("EDT" if np.isfinite(edt) else "none")


def _period_conf(t: np.ndarray, v: np.ndarray, w: np.ndarray | None = None,
                 f_lo: float = 0.4, f_hi: float = 12.0) -> tuple[float, float]:
    """(peak share of variance one sinusoid explains, its frequency Hz), 0.4-12 Hz."""
    if t.size < 20 or np.std(v) < 1e-9:
        return 0.0, float("nan")
    f, p = periodogram_peak(t, v, f_lo, f_hi, n=300, w=w)
    k = int(np.argmax(p))
    return float(p[k]), float(f[k])


def _detrend_running(v: np.ndarray, win: int) -> np.ndarray:
    return v - median_filter(v, size=max(win, 3), mode="nearest")


# ------------------------------------------------------------------------------- known-DI features

KNOWN_DI_FEATURES = (
    "null_db", "lin_resid_db", "nl_resid_db", "level_slope", "gain_swing_db", "gain_period_conf",
    "ir_late_db", "ir_echo_prom_db", "ir_echo_level_db", "ir_tail_rt60_s", "ir_tail_smooth_db",
    "mag_dev_db", "mag_max_db", "mag_tilt_db_oct", "mag_hf_db", "mag_lf_db", "mag_hf_slope_db",
    "mod_rate_conf", "mod_amp_depth_db", "mod_sweep_depth_db", "mod_pitch_cents", "mod_mean_delay_ms",
    "d_crest_db", "d_flat_db", "d_hnr_db", "d_dyn_db", "d_median_db", "d_overshoot_db", "d_attack_ms",
    "d_decay_db_s", "d_hf_share_db",
    "e_late_db", "e_resid_db", "e_echo_prom_db", "e_echo_level_db", "e_smooth_db", "e_rt60_s",
)


def wiener_fir(x: np.ndarray, y: np.ndarray, n_taps: int, pre: int, loading: float = 1e-4) -> np.ndarray:
    """Least-squares FIR h (n_taps long, starting `pre` samples before time zero) with
    y[n] ~ sum_k h[k] x[n - k + pre]: the normal equations on the autocorrelation of x (a Toeplitz
    solve). Unlike a full-length deconvolution it spreads nothing of what it cannot explain over
    lags it does not have."""
    n = 1 << int(np.ceil(np.log2(x.size + y.size)))
    X, Y = np.fft.rfft(x, n), np.fft.rfft(y, n)
    rxx = np.fft.irfft(np.abs(X) ** 2, n)[:n_taps]
    ryx = np.fft.irfft(Y * np.conj(X), n)
    lags = np.arange(n_taps) - pre
    rhs = ryx[lags % n]
    col = rxx.copy()
    col[0] *= 1.0 + loading
    return solve_toeplitz(col, rhs)


def _linear_part(y: np.ndarray, x: np.ndarray) -> dict:
    pre = int(IR_PRE_S * SR)
    h = impulse_response(x, y, n_out=int(IR_LEN_S * SR), pre=pre)
    lo, hi = pre - int(0.005 * SR), pre + int(0.010 * SR)
    d = lo + int(np.argmax(np.abs(h[lo:hi])))
    a, b = d + int(DIRECT_WIN_S[0] * SR), d + int(DIRECT_WIN_S[1] * SR)
    e_dir = float(np.sum(h[a:b] ** 2)) + EPS
    s_late = d + int(LATE_START_S * SR)
    e_late = float(np.sum(h[s_late:] ** 2))
    # the late part gated against the response's own noise floor (the negative-time part, which is
    # zero for a causal block): what a nonlinear or time-varying block leaves unexplained spreads over
    # every lag, and predicting with it would hide the per-frame gain of a compressor or tremolo
    floor = float(np.mean(h[: max(pre - int(0.003 * SR), 16)] ** 2)) + EPS
    env2 = smooth_power(h, int(0.002 * SR))
    keep = np.zeros(h.size, bool)
    keep[b:] = env2[b:] >= 4.0 * floor
    # the direct part as a least-squares FIR over the same window (cleaner than the deconvolution's)
    # h index i is lag i - pre, so the FIR's first tap (index a) is lag a - pre. The FIR is fitted to
    # what the gated late part leaves: fitted alone it would soak up echoes that sustained notes make
    # correlate with the direct sound.
    h_pred = h * keep
    y_rest = y - fftconvolve(x, h_pred)[pre: pre + y.size] if keep.any() else y
    hw = wiener_fir(x, y_rest, b - a, pre=pre - a)
    h_pred[a:b] = hw
    out: dict = {"h": h, "h_pred": h_pred, "direct_idx": d, "ir_late_db": float(db10(e_late / e_dir)),
                 "ir_floor_db": float(db10(floor * (b - a) / e_dir))}
    # magnitude response of the direct part (the least-squares FIR), tapered at its end
    seg = hw.copy()
    taper = int(0.010 * SR)
    seg[-taper:] *= np.hanning(2 * taper)[taper:]
    n_fft = 1 << 14
    P = np.abs(np.fft.rfft(seg, n_fft)) ** 2
    f = np.fft.rfftfreq(n_fft, 1 / SR)
    mag = _band_db(P, f, MAG_BANDS)
    ref = _mean_bands(mag, 250.0, 4000.0)
    mag = mag - ref
    core = (MAG_BANDS >= 100.0) & (MAG_BANDS <= 8000.0) & np.isfinite(mag)
    lf2 = np.log2(MAG_BANDS[core])
    tilt = float(np.polyfit(lf2, mag[core], 1)[0]) if core.sum() >= 3 else float("nan")
    resid_tilt = mag[core] - np.polyval(np.polyfit(lf2, mag[core], 1), lf2) if core.sum() >= 3 else mag[core]
    out.update({
        "mag_db": mag, "mag_dev_db": float(np.sqrt(np.mean(mag[core] ** 2))),
        "mag_max_db": float(np.max(np.abs(mag[core]))), "mag_tilt_db_oct": tilt,
        "mag_curv_db": float(np.sqrt(np.mean(resid_tilt ** 2))),
        "mag_hf_db": _mean_bands(mag, 6300.0, 10000.0), "mag_lf_db": _mean_bands(mag, 63.0, 125.0),
        "mag_hf_slope_db": _mean_bands(mag, 8000.0, 12500.0) - _mean_bands(mag, 2000.0, 4000.0),
    })
    # late part: 1 ms energy envelope in dB re the direct peak's
    blk = int(0.001 * SR)
    late = h[s_late:]
    nb = late.size // blk
    env = (late[: nb * blk] ** 2).reshape(nb, blk).sum(1)
    pk_dir = float(np.max(smooth_power(h[a:b], blk))) * blk + EPS
    e_db = db10(env / pk_dir)
    pk, prom = _local_peaks(e_db, med_win=41, min_prom_db=6.0)
    t_pk = (s_late - d + pk * blk + blk / 2) / SR
    sel = (t_pk >= ECHO_RANGE_S[0]) & (t_pk <= ECHO_RANGE_S[1])
    pk, prom, t_pk = pk[sel], prom[sel], t_pk[sel]
    audible = e_db[pk] >= -35.0 if pk.size else np.zeros(0, bool)
    out["ir_echo_prom_db"] = float(prom[audible].max()) if audible.any() else 0.0
    out["ir_echo_level_db"] = float(e_db[pk].max()) if pk.size else float(np.max(e_db)) if e_db.size else -120.0
    est: dict = {"time_s": float("nan"), "feedback_db": float("nan"), "echo_level_db": float("nan")}
    if pk.size:
        lvl = e_db[pk]
        # an echo 30 dB under the direct sound is not a delay setting, and one within 10 dB of the
        # response's own noise floor is what a nonlinear block leaves behind
        floor_blk = float(db10(floor * blk / pk_dir))
        strong = (prom >= 10.0) & (lvl >= -30.0) & (lvl >= floor_blk + 10.0)
        if strong.any():
            j = _echo_pick(t_pk[strong], lvl[strong], prom[strong])
            k = np.flatnonzero(strong)[j]
            # refine to the sample on a 0.5 ms smoothed envelope
            c = s_late + pk[k] * blk
            w0, w1 = max(c - 2 * blk, 0), min(c + 3 * blk, h.size)
            sp = smooth_power(h[w0:w1], blk // 2)
            t1 = (w0 + int(np.argmax(sp)) - d) / SR
            est["time_s"] = float(t1)
            est["echo_level_db"] = float(lvl[k])
            # the echo at twice the time: its level re echo 1 (the loop gain)
            near2 = np.flatnonzero(np.abs(t_pk - 2 * t1) <= 0.01 * t1 + 0.002)
            if near2.size:
                est["feedback_db"] = float(lvl[near2].max() - lvl[k])
    out["delay_est"] = est
    # the smooth tail: decay from LATE_START_S, and how smooth it is (spread of the envelope about its
    # running median: a reverb is smooth, a train of echoes is not)
    rt, rng, meth = _decay_rt60(late ** 2)
    out["ir_tail_rt60_s"] = rt
    out["reverb_est"] = {"rt60_s": rt, "decay_range_db": rng, "method": meth}
    act = e_db >= (e_db.max() - 30.0) if e_db.size else np.zeros(0, bool)
    if act.sum() >= 20:
        base = median_filter(e_db, size=41, mode="nearest")
        out["ir_tail_smooth_db"] = float(np.std((e_db - base)[act]))
    else:
        out["ir_tail_smooth_db"] = float("nan")
    return out


def _frame_gains(y: np.ndarray, yhat: np.ndarray) -> dict:
    Fy, Fp = frame(y, FR_N, FR_HOP), frame(yhat, FR_N, FR_HOP)
    ey, ep = np.sum(Fy ** 2, 1), np.sum(Fp ** 2, 1)
    lvl = db10(ep / FR_N)
    act = lvl >= lvl.max() - 35.0
    if act.sum() < 10:
        return {"nl_resid_db": float("nan"), "level_slope": float("nan"), "gain_swing_db": float("nan"),
                "gain_period_conf": 0.0, "gain_rate_hz": float("nan")}
    a = np.sum(Fy * Fp, 1) / (ep + EPS)
    res = np.sum((Fy - a[:, None] * Fp) ** 2, 1)
    nl = float(db10(res[act].sum() / (ey[act].sum() + EPS)))
    lin = lvl[act]
    lout = db10(ey[act] / FR_N)
    g = lout - lin
    t = (np.flatnonzero(act) * FR_HOP + FR_N / 2) / SR
    loud = lin >= lin.max() - 25.0
    slope = float(np.polyfit(lin[loud], lout[loud], 1)[0]) if loud.sum() >= 10 else float("nan")
    # gain not explained by the input level (a compressor's is), slow trends removed
    A = np.stack([np.ones_like(lin), lin, t - t.mean()], 1)
    coef, *_ = np.linalg.lstsq(A, g, rcond=None)
    gr = g - A @ coef
    conf, rate = _period_conf(t, gr)
    swing = float(np.percentile(gr, 95) - np.percentile(gr, 5))
    return {"nl_resid_db": nl, "level_slope": slope, "gain_swing_db": swing, "gain_period_conf": conf,
            "gain_rate_hz": rate}


def _stat_deltas(y: np.ndarray, x: np.ndarray) -> dict:
    cy, cx = crest.features(y).vec, crest.features(x).vec
    ey, ex = envelope.features(y).vec, envelope.features(x).vec
    fy, py = signal.welch(y, fs=SR, nperseg=4096)
    _, px = signal.welch(x, fs=SR, nperseg=4096)
    hf = fy >= 5000.0
    share = float(db10(py[hf].sum() / (py.sum() + EPS)) - db10(px[hf].sum() / (px.sum() + EPS)))
    return {"d_crest_db": float(cy[0] - cx[0]), "d_flat_db": float(cy[1] - cx[1]), "d_hnr_db": float(cy[2] - cx[2]),
            "d_dyn_db": _fin(ey[0] - ex[0]), "d_median_db": _fin(ey[1] - ex[1]), "d_attack_ms": _fin(ey[3] - ex[3]),
            "d_decay_db_s": _fin(ey[4] - ex[4]), "d_overshoot_db": _fin(ey[5] - ex[5]), "d_hf_share_db": share}


def _mod(y: np.ndarray, x: np.ndarray) -> dict:
    try:
        p = modulation.mod_profile(y, x)
    except (ValueError, np.linalg.LinAlgError, IndexError):
        return {k: float("nan") for k in ("mod_rate_hz", "mod_rate_conf", "mod_amp_depth_db", "mod_sweep_depth_db",
                                          "mod_pitch_cents", "mod_mean_delay_ms", "mod_delay_depth_ms")}
    return {"mod_rate_hz": _fin(p.rate_hz), "mod_rate_conf": _fin(p.rate_confidence),
            "mod_amp_depth_db": _fin(p.amp_depth_db), "mod_sweep_depth_db": _fin(p.sweep_depth_db),
            "mod_pitch_cents": _fin(p.pitch_depth_cents), "mod_mean_delay_ms": _fin(p.mean_delay_ms),
            "mod_delay_depth_ms": _fin(p.delay_depth_ms)}


E_HOP = 240                      # 5 ms
E_NFFT = 1024
E_MAX_S = 2.0
E_BANDS = (250.0, 500.0, 1000.0, 2000.0, 4000.0)
E_DIRECT = 6                     # lags 0..25 ms count as the direct sound (the frame is 21 ms long)
E_SMOOTH = 0.02                  # first-difference penalty re the DI band energy (0.02/0.2/1.0 tried on train, T102)


def _band_energy(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    P = np.abs(np.fft.rfft(frame(x, E_NFFT, E_HOP) * np.hanning(E_NFFT), axis=1)) ** 2
    f = np.fft.rfftfreq(E_NFFT, 1 / SR)
    return P, f


def energy_response(y: np.ndarray, x: np.ndarray) -> dict:
    """The block's response in the energy domain: per octave band (250 Hz to 4 kHz), the target's
    5 ms frame energy fitted as a non-negative sum of the DI's frame energy delayed by 0 to 2 s
    (non-negative least squares). Phase and modulation do not matter, so a modulated reverb tail or a
    chorused echo still fits; a delay leaves discrete peaks, a reverb a smooth decay, an EQ only lag 0.
    Returns the bands' median late-to-direct energy, the summed response's echo peak (prominence over
    a 200 ms running median, its lag), its decay time (RT60 from a Schroeder curve, T20 or EDT), its
    smoothness, and the median fit residual (what an energy-linear block cannot be: drive, comp,
    tremolo)."""
    from scipy.optimize import nnls
    Py, f = _band_energy(y)
    Px, _ = _band_energy(x)
    L = int(E_MAX_S * SR / E_HOP)
    gs, lates, resids = [], [], []
    for c in E_BANDS:
        sel = (f >= c / np.sqrt(2)) & (f < c * np.sqrt(2))
        ey, ex = Py[:, sel].sum(1), Px[:, sel].sum(1)
        ey = ey / (ey.mean() + EPS)
        ex = ex / (ex.mean() + EPS)
        T = ex.size
        A = np.zeros((T, L))
        for k in range(min(L, T)):
            A[k:, k] = ex[: T - k]
        # a light first-difference penalty past the direct sound: plain non-negative least squares
        # returns a few spikes, which would make a smooth reverb tail look like echoes
        lam = E_SMOOTH * float(np.sum(ex ** 2))
        D = np.zeros((L - E_DIRECT - 1, L))
        idx = np.arange(E_DIRECT, L - 1)
        D[idx - E_DIRECT, idx] = -1.0
        D[idx - E_DIRECT, idx + 1] = 1.0
        g, _ = nnls(np.vstack([A, np.sqrt(lam) * D]), np.concatenate([ey, np.zeros(D.shape[0])]), maxiter=3 * L)
        res = float(np.linalg.norm(A @ g - ey))
        d = g[:E_DIRECT].sum() + EPS
        gs.append(g / d)
        lates.append(float(db10(g[E_DIRECT:].sum() / d + 1e-6)))
        resids.append(float(db10(res ** 2 / (np.sum(ey ** 2) + EPS))))
    G = np.mean(np.asarray(gs), axis=0)
    lag = np.arange(L) * E_HOP / SR
    out = {"e_late_db": float(np.median(lates)), "e_resid_db": float(np.median(resids))}
    gl = G[E_DIRECT:]
    gdb = db10(np.maximum(gl, 1e-6))                    # floored 60 dB under the direct sound
    base = median_filter(gdb, size=41, mode="nearest")
    prom = gdb - base
    ok = gdb >= -30.0                                    # at most 30 dB under the direct sound
    if ok.any():
        k = int(np.argmax(np.where(ok, prom, -np.inf)))
        frac = 0.0
        if 0 < k < gl.size - 1 and gl[k] > 0:
            a_, b_, c_ = gl[k - 1], gl[k], gl[k + 1]
            den = a_ - 2 * b_ + c_
            frac = float(np.clip(0.5 * (a_ - c_) / den, -0.5, 0.5)) if den != 0 else 0.0
        out["e_echo_prom_db"] = float(prom[k])
        out["e_echo_level_db"] = float(gdb[k])
        out["e_echo_s"] = float(lag[E_DIRECT + k] + frac * E_HOP / SR)
        act = gdb >= gdb.max() - 30.0
        out["e_smooth_db"] = float(np.std(prom[act])) if act.sum() >= 10 else float("nan")
    else:
        out.update({"e_echo_prom_db": 0.0, "e_echo_level_db": -60.0, "e_echo_s": float("nan"), "e_smooth_db": float("nan")})
    # decay of the response after the direct sound (energy per lag, so no squaring)
    rt, _, meth = _decay_rt60(np.repeat(gl, E_HOP)) if gl.sum() > 1e-4 else (float("nan"), float("nan"), "none")
    out["e_rt60_s"] = rt
    out["e_rt60_method"] = meth
    return out


def null_db(target: np.ndarray, di: np.ndarray, max_lag: int = 480) -> float:
    """Target against its DI after a lag and least-squares gain: how much the chain changed the sound
    (dB re the DI; -30 dB or below counts as transparent, rule B of T096)."""
    from tonematch.metrics._fx import align_gain
    y, x = as_mono(target, "target"), as_mono(di, "di")
    m = min(y.size, x.size)
    ya, _, _ = align_gain(x[:m], y[:m], max_lag=max_lag)
    return float(db10(np.sum((ya - x[:m]) ** 2) / (np.sum(x[:m] ** 2) + EPS)))


RT60_SOURCE = "waveform"         # within 5 % on synthetic reverbs; the energy response is too spiky (T102)


def _delay_estimate(wave: dict, er: dict) -> dict:
    """Delay time: the waveform response's first echo (to the sample) when it has one, else the
    energy response's echo peak (5 ms frames, interpolated) when it stands 6 dB above its
    neighbourhood within 25 dB of the direct sound (a modulated or filtered echo smears the waveform
    peak, not the energy one). The energy response is spiky (a smooth reverb tail scores 15-35 dB
    there, a clean echo 45-50 dB, on train renders), hence the high bar. It takes the most prominent
    peak, which on a feedback train can be the second echo (a whole multiple of the time)."""
    out = dict(wave)
    te = er.get("e_echo_s", float("nan"))
    e_ok = er.get("e_echo_prom_db", 0.0) >= 40.0 and er.get("e_echo_level_db", -99.0) >= -25.0 and np.isfinite(te)
    out["time_s_waveform"] = wave["time_s"]
    out["time_s_energy"] = te if e_ok else float("nan")
    if np.isfinite(wave["time_s"]):
        out["source"] = "waveform"
    elif e_ok:
        out["time_s"], out["source"] = te, "energy"
    else:
        out["source"] = None
    return out


def _reverb_estimate(wave: dict, er: dict) -> dict:
    out = dict(wave)
    out["rt60_s_waveform"] = wave["rt60_s"]
    out["rt60_s_energy"] = er.get("e_rt60_s", float("nan"))
    out["rt60_s"] = out["rt60_s_energy"] if RT60_SOURCE == "energy" else out["rt60_s_waveform"]
    out["source"] = RT60_SOURCE
    return out


def features_known_di(target: np.ndarray, di: np.ndarray) -> tuple[dict, dict]:
    """(features, estimates) of a target against the DI of the same performance."""
    y, x = as_mono(target, "target"), as_mono(di, "di")
    m = min(y.size, x.size)
    y, x = _unit(y[:m]), _unit(x[:m])
    lp = _linear_part(y, x)
    pre = int(IR_PRE_S * SR)
    yhat = fftconvolve(x, lp["h_pred"])[pre: pre + m]
    yb, pb = _band(y), _band(yhat)
    gls = float(np.dot(yb, pb) / (np.dot(pb, pb) + EPS))
    lin_resid = float(db10(np.sum((yb - gls * pb) ** 2) / (np.sum(yb ** 2) + EPS)))
    fg = _frame_gains(yb, pb)
    md = _mod(y, x)
    er = energy_response(y, x)
    feats = {"null_db": null_db(y, x), "lin_resid_db": lin_resid}
    feats.update({k: er[k] for k in ("e_late_db", "e_resid_db", "e_echo_prom_db", "e_echo_level_db", "e_smooth_db", "e_rt60_s")})
    feats.update({k: fg[k] for k in ("nl_resid_db", "level_slope", "gain_swing_db", "gain_period_conf")})
    feats.update({k: lp[k] for k in ("ir_late_db", "ir_echo_prom_db", "ir_echo_level_db", "ir_tail_rt60_s",
                                     "ir_tail_smooth_db", "mag_dev_db", "mag_max_db", "mag_tilt_db_oct", "mag_hf_db",
                                     "mag_lf_db", "mag_hf_slope_db")})
    feats.update({k: md[k] for k in ("mod_rate_conf", "mod_amp_depth_db", "mod_sweep_depth_db", "mod_pitch_cents",
                                     "mod_mean_delay_ms")})
    feats.update(_stat_deltas(y, x))
    feats = {k: _fin(feats[k]) for k in KNOWN_DI_FEATURES}
    est = {
        "delay": _delay_estimate(lp["delay_est"], er),
        "reverb": _reverb_estimate(lp["reverb_est"], er),
        "mod": {"rate_hz": md["mod_rate_hz"] if feats["mod_rate_conf"] >= fg["gain_period_conf"] or not np.isfinite(fg["gain_rate_hz"]) else fg["gain_rate_hz"],
                "rate_hz_profile": md["mod_rate_hz"], "rate_hz_gain": fg["gain_rate_hz"],
                "amp_depth_db": md["mod_amp_depth_db"], "sweep_depth_db": md["mod_sweep_depth_db"],
                "pitch_depth_cents": md["mod_pitch_cents"], "mean_delay_ms": md["mod_mean_delay_ms"],
                "delay_depth_ms": md["mod_delay_depth_ms"]},
        "gain": {"gain_index_db": feats["nl_resid_db"]},
        "comp": {"level_slope": feats["level_slope"], "d_crest_db": feats["d_crest_db"]},
        "eq": {"tilt_db_oct": feats["mag_tilt_db_oct"], "dev_db": feats["mag_dev_db"],
               "bands_hz": [float(c) for c in MAG_BANDS], "bands_db": [_fin(v) for v in lp["mag_db"]]},
        "cab": {"hf_db": feats["mag_hf_db"], "lf_db": feats["mag_lf_db"]},
    }
    est["mod"]["subtype"], est["mod"]["subtype_reason"] = mod_subtype(est["mod"], lin_resid_db=lin_resid)
    est["gain"]["label"] = gain_label(feats["nl_resid_db"])
    return feats, est


# ---------------------------------------------------------------------------------- no-DI features

NO_DI_FEATURES = (
    "ltas_tilt_db_oct", "ltas_hf_db", "ltas_lf_db", "ltas_rough_db",
    "crest_db", "flat_db", "hnr_db", "crest_iqr_db", "flat_iqr_db", "hnr_iqr_db",
    "dyn_db", "median_db", "floor_db", "attack_ms", "decay_db_s", "overshoot_db", "sustain_db_s",
    "wac_prom_db", "wac_hf_prom_db", "band_period_conf", "band_period_agree", "centroid_period_conf",
    "hf_share_db",
)


def _echo_no_di(y: np.ndarray) -> dict:
    """Echo peaks without the DI: the whitened autocorrelation (magnitude divided by its 0.2 Hz-smoothed
    self, so only fine structure is left) over 30 ms to 1.6 s, full band (80 Hz-8 kHz) and high band
    (1.5-8 kHz, where re-struck chords are no longer phase-coherent but an echo still is). Score: the
    peak of the 1 ms envelope in dB above its 41 ms running median. The DI's own repetitions also
    score (10-20 dB on Guitar-TECHS clips), so this is weak evidence; T102 measured how weak."""
    n = 1 << int(np.ceil(np.log2(2 * y.size)))
    Y = np.fft.rfft(y, n)
    A = np.abs(Y)
    f = np.fft.rfftfreq(n, 1 / SR)
    As = uniform_filter1d(A, 201)
    q0, q1 = int(ECHO_RANGE_S[0] * SR), int(ECHO_RANGE_S[1] * SR)
    out = {}
    for name, lo, hi in (("wac", 80.0, 8000.0), ("wac_hf", 1500.0, 8000.0)):
        band = (f >= lo) & (f <= hi)
        W = np.where(band, A ** 2 / (As ** 2 + 1e-9 * As.max() ** 2), 0.0)
        r = np.fft.irfft(W, n)[: q1 + 1]
        r = r / (r[0] + EPS)
        blk = int(0.001 * SR)
        e = np.abs(r[q0:q1])
        nb = e.size // blk
        e = e[: nb * blk].reshape(nb, blk).max(1)
        ed = db10(e ** 2)
        prom = ed - median_filter(ed, size=41, mode="nearest")
        k = int(np.argmax(prom))
        out[f"{name}_prom_db"] = float(prom[k])
        out[f"{name}_peak_s"] = float((q0 + k * blk + int(np.argmax(np.abs(r[q0 + k * blk: q0 + (k + 1) * blk])))) / SR)
    return out


def _mod_no_di(y: np.ndarray) -> dict:
    n_fft, hop = 2048, 480
    P = np.abs(np.fft.rfft(frame(y, n_fft, hop) * np.hanning(n_fft), axis=1)) ** 2
    f = np.fft.rfftfreq(n_fft, 1 / SR)
    t = (np.arange(P.shape[0]) * hop + n_fft / 2) / SR
    tot = P[:, (f >= 100) & (f <= 8000)].sum(1)
    act = db10(tot) >= db10(tot).max() - 30.0
    if act.sum() < 40:
        return {"band_period_conf": 0.0, "band_period_agree": float("nan"), "centroid_period_conf": 0.0,
                "mod_rate_hz": float("nan")}
    confs, rates = [], []
    for c in third_octave_centres(250.0, 4000.0)[::2]:
        sel = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        lv = db10(P[:, sel].sum(1) / (tot + EPS))      # band share: note level changes cancel
        v = _detrend_running(lv, 101)                 # 1 s running median removes note decays
        cf, rt = _period_conf(t[act], v[act])
        confs.append(cf)
        rates.append(rt)
    confs, rates = np.asarray(confs), np.asarray(rates)
    top = np.argsort(-confs)[:3]
    agree = float(np.std(np.log2(rates[top]))) if np.all(np.isfinite(rates[top])) else float("nan")
    band = (f >= 100) & (f <= 8000)
    cen = np.log2((P[:, band] * f[band]).sum(1) / (P[:, band].sum(1) + EPS) + EPS)
    cc, crt = _period_conf(t[act], _detrend_running(cen, 101)[act])
    return {"band_period_conf": float(np.mean(confs[top])), "band_period_agree": agree,
            "centroid_period_conf": cc, "mod_rate_hz": float(np.median(rates[top])) if confs[top].mean() >= cc else crt}


def features_no_di(target: np.ndarray) -> tuple[dict, dict]:
    """(features, estimates) of a target with no DI."""
    y = _unit(as_mono(target, "target"))
    lf = ltas.features(y)
    cen = ltas._CENTRES
    bd = lf.bands_db
    ref = _mean_bands(bd, 500.0, 2000.0, cen)
    core = (cen >= 160) & (cen <= 8000)
    l2 = np.log2(cen[core])
    rough = float(np.std(bd[core] - np.polyval(np.polyfit(l2, bd[core], 2), l2)))
    cv = crest.features(y).vec
    ev = envelope.features(y).vec
    fy, py = signal.welch(y, fs=SR, nperseg=4096)
    echo = _echo_no_di(y)
    md = _mod_no_di(y)
    feats = {
        "ltas_tilt_db_oct": lf.tilt_db_oct, "ltas_hf_db": _mean_bands(bd, 6000.0, 12000.0, cen) - ref,
        "ltas_lf_db": _mean_bands(bd, 80.0, 160.0, cen) - ref, "ltas_rough_db": rough,
        "crest_db": cv[0], "flat_db": cv[1], "hnr_db": cv[2], "crest_iqr_db": cv[3], "flat_iqr_db": cv[4],
        "hnr_iqr_db": cv[5],
        "dyn_db": ev[0], "median_db": ev[1], "floor_db": ev[2], "attack_ms": ev[3], "decay_db_s": ev[4],
        "overshoot_db": ev[5], "sustain_db_s": ev[6],
        "wac_prom_db": echo["wac_prom_db"], "wac_hf_prom_db": echo["wac_hf_prom_db"],
        "band_period_conf": md["band_period_conf"], "band_period_agree": md["band_period_agree"],
        "centroid_period_conf": md["centroid_period_conf"],
        "hf_share_db": float(db10(py[fy >= 5000].sum() / (py.sum() + EPS))),
    }
    feats = {k: _fin(feats[k]) for k in NO_DI_FEATURES}
    t_echo = echo["wac_hf_peak_s"] if echo["wac_hf_prom_db"] >= echo["wac_prom_db"] else echo["wac_peak_s"]
    est = {"delay": {"time_s": float(t_echo), "time_s_full": echo["wac_peak_s"], "time_s_hf": echo["wac_hf_peak_s"]},
           "reverb": {"rt60_s": float("nan"), "decay_db_s": feats["decay_db_s"]},
           "mod": {"rate_hz": md["mod_rate_hz"], "subtype": None, "subtype_reason": "needs the DI"},
           "gain": {"flatness_db": feats["flat_db"], "crest_db": feats["crest_db"]},
           "comp": {"dyn_db": feats["dyn_db"]},
           "eq": {"tilt_db_oct": feats["ltas_tilt_db_oct"]},
           "cab": {"hf_db": feats["ltas_hf_db"]}}
    return feats, est


# ------------------------------------------------------------------------- rule-based estimates

def mod_subtype(m: dict, *, lin_resid_db: float) -> tuple[str | None, str]:
    """Modulation sub-type from the known-DI modulation measures (rules registered for T102). Applies
    when modulation is present; it describes, it does not detect.

    1. tremolo: level swing >= 2 dB, pitch swing under 8 cents, and the per-band sweep no more than
       2.5x the level swing plus 2 dB (every band moves together);
    2. chorus or vibrato: pitch swing >= 8 cents, or >= 3 cents with a level swing under 2 dB; vibrato
       when the linear part explains almost none of the output (lin_resid_db > -2 dB: no dry path),
       else chorus. The pitch comes from `modulation.mod_profile`'s delay tracker, which on clean DI
       finds a fully wet modulated delay but loses it when a dry path of similar level is mixed in, so
       most choruses show as notch sweep (rule 3) instead;
    3. phaser or flanger: notch sweep >= 6 dB; flanger when the tracked mean delay is between 0.2 and
       4 ms, else phaser (the delay tracker searches -2 to 25 ms and sits at its edge when it finds
       nothing);
    4. otherwise None."""
    amp, sw, pc, md = (m.get(k, float("nan")) for k in ("amp_depth_db", "sweep_depth_db", "pitch_depth_cents", "mean_delay_ms"))
    amp, sw, pc = (v if v is not None and np.isfinite(v) else 0.0 for v in (amp, sw, pc))
    md = md if md is not None and np.isfinite(md) else float("nan")
    if amp >= 2.0 and pc < 8.0 and sw <= 2.5 * amp + 2.0:
        return "tremolo", f"level swing {amp:.1f} dB, bands together, pitch {pc:.1f} cents"
    if pc >= 8.0 or (pc >= 3.0 and amp < 2.0):
        if np.isfinite(lin_resid_db) and lin_resid_db > -2.0:
            return "vibrato", f"pitch {pc:.1f} cents, no dry path (linear residual {lin_resid_db:.1f} dB)"
        return "chorus", f"pitch {pc:.1f} cents with a dry path (linear residual {lin_resid_db:.1f} dB)"
    if sw >= 6.0:
        if np.isfinite(md) and 0.2 <= md <= 4.0:
            return "flanger", f"notch sweep {sw:.1f} dB, delay {md:.1f} ms"
        return "phaser", f"notch sweep {sw:.1f} dB, no short delay tracked"
    return None, "no rule matched"


def gain_label(gain_index_db: float) -> str | None:
    if not np.isfinite(gain_index_db):
        return None
    for ub, name in GAIN_LABELS:
        if gain_index_db < ub:
            return name
    return GAIN_LABELS[-1][1]


# ----------------------------------------------------------------------- calibrated presence model

@dataclass
class PresenceModel:
    """One-versus-rest logistic models per category on robust-scaled features (median, IQR; z clipped
    to +-6; missing values at the median), optionally expanded with hinges at `knots` (an additive
    piecewise-linear model). Fitted with balanced class weights, so 0.5 means 'as likely present as
    absent' whatever the category's share in the training set."""
    mode: str
    features: list[str]
    center: list[float]
    scale: list[float]
    coef: dict[str, list[float]]
    intercept: dict[str, float]
    threshold: float = 0.5
    version: str = VERSION
    trained_on: str = ""
    knots: list[float] = field(default_factory=list)   # hinge knots in z units; [] = linear in z

    def z(self, feats: dict) -> np.ndarray:
        v = np.array([_fin(feats.get(k)) for k in self.features])
        z = (v - np.asarray(self.center)) / np.asarray(self.scale)
        return expand(np.clip(np.where(np.isfinite(z), z, 0.0), -6.0, 6.0), self.knots)

    def predict(self, feats: dict) -> dict[str, float]:
        z = self.z(feats)
        return {c: float(1.0 / (1.0 + np.exp(-(np.dot(self.coef[c], z) + self.intercept[c])))) for c in self.coef}

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> PresenceModel:
        return cls(**d)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_json(), indent=1) + "\n")


def expand(z: np.ndarray, knots: list[float] | tuple[float, ...]) -> np.ndarray:
    """z plus a hinge max(0, z - k) per knot, per feature (an additive piecewise-linear model)."""
    if not knots:
        return z
    parts = [z] + [np.maximum(z - k, 0.0) for k in knots]
    return np.concatenate(parts, axis=-1)


def _logistic(Z: np.ndarray, y: np.ndarray, *, l2: float, iters: int = 50) -> tuple[np.ndarray, float]:
    """Balanced-weight L2 logistic regression by Newton steps. Returns (coef, intercept)."""
    n, d = Z.shape
    pos = max(y.sum(), 1.0)
    neg = max(n - y.sum(), 1.0)
    w = np.where(y > 0, 0.5 * n / pos, 0.5 * n / neg)
    X = np.hstack([Z, np.ones((n, 1))])
    beta = np.zeros(d + 1)
    reg = np.full(d + 1, l2 * n)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ beta, -30, 30)))
        g = X.T @ (w * (p - y)) + reg * beta
        H = (X * (w * p * (1 - p))[:, None]).T @ X + np.diag(reg) + 1e-9 * np.eye(d + 1)
        step = np.linalg.solve(H, g)
        beta -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return beta[:-1], float(beta[-1])


def fit_model(rows: list[dict], labels: list[set[str]], mode: str, *, features: tuple[str, ...] | None = None,
              l2: float = 1e-3, categories: tuple[str, ...] = CATEGORIES, trained_on: str = "",
              knots: tuple[float, ...] = ()) -> PresenceModel:
    """Fit a PresenceModel on feature dicts `rows` with label sets (the categories present in each)."""
    names = list(features or (KNOWN_DI_FEATURES if mode == "known_di" else NO_DI_FEATURES))
    V = np.array([[_fin(r.get(k)) for k in names] for r in rows], dtype=float)
    center = np.nanmedian(V, axis=0)
    q75, q25 = np.nanpercentile(V, 75, axis=0), np.nanpercentile(V, 25, axis=0)
    scale = np.where((q75 - q25) > 1e-9, q75 - q25, 1.0)
    center = np.where(np.isfinite(center), center, 0.0)
    Z = expand(np.clip(np.nan_to_num((V - center) / scale, nan=0.0), -6.0, 6.0), list(knots))
    coef, icpt = {}, {}
    for c in categories:
        y = np.array([1.0 if c in lab else 0.0 for lab in labels])
        if y.sum() == 0 or y.sum() == y.size:
            continue
        b, b0 = _logistic(Z, y, l2=l2)
        coef[c], icpt[c] = b.tolist(), b0
    return PresenceModel(mode=mode, features=names, center=center.tolist(), scale=scale.tolist(), coef=coef,
                         intercept=icpt, trained_on=trained_on, knots=list(knots))


def model_path(mode: str, version: str = "presence-v1") -> Path:
    return MODEL_DIR / f"{version}-{mode}.json"


def load_model(mode: str, path: str | Path | None = None) -> PresenceModel | None:
    p = Path(path) if path else model_path(mode)
    if not p.exists():
        return None
    return PresenceModel.from_json(json.loads(p.read_text()))


# ---------------------------------------------------------------------------------------- report

@dataclass
class CategoryPresence:
    probability: float | None        # None when no fitted model is available
    present: bool | None
    estimate: dict = field(default_factory=dict)


@dataclass
class PresenceReport:
    mode: str
    categories: dict[str, CategoryPresence]
    features: dict[str, float]
    model: str | None

    def probabilities(self) -> dict[str, float]:
        """category -> probability (categories without a model left out): what `chain_prior.top_structures` takes."""
        return {c: p.probability for c, p in self.categories.items() if p.probability is not None}

    def present(self) -> list[str]:
        return [c for c, p in self.categories.items() if p.present]

    def to_dict(self) -> dict:
        return {"mode": self.mode, "model": self.model, "features": self.features,
                "categories": {c: asdict(p) for c, p in self.categories.items()}}


_EST_KEY = {"drive": "gain", "amp": "gain", "cab": "cab", "comp": "comp", "eq": "eq", "mod": "mod",
            "delay": "delay", "reverb": "reverb"}


def detect(target: np.ndarray, di: np.ndarray | None = None, *, model: PresenceModel | None | str = "default") -> PresenceReport:
    """Measure which categories are present. With `di` (the same performance's DI) the known-DI
    measurements are used, else the no-DI ones. `model`: a PresenceModel, None for measurements only,
    or "default" for the fitted file in data/presence/ when it exists."""
    mode = "known_di" if di is not None else "no_di"
    feats, est = features_known_di(target, di) if di is not None else features_no_di(target)
    m = load_model(mode) if isinstance(model, str) else model
    probs = m.predict(feats) if m is not None else {}
    thr = m.threshold if m is not None else 0.5
    cats = {}
    for c in CATEGORIES:
        p = probs.get(c)
        e = dict(est.get(_EST_KEY[c], {}))
        if c == "amp" and "gain_index_db" in e:
            e = {"gain_index_db": e["gain_index_db"], "gain_level": e.get("label")}
        cats[c] = CategoryPresence(probability=p, present=(p >= thr) if p is not None else None, estimate=e)
    return PresenceReport(mode=mode, categories=cats, features=feats,
                          model=(f"{m.trained_on} v{m.version}" if m is not None else None))
