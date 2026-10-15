"""Analyse a block's render of the probe set into a fingerprint (schema in docs/notes/block-probe.md).

`analyse(wet, probe)` takes the block's output for the probe (mono or stereo, 48 kHz, same start as
the probe; any latency up to MAX_LATENCY_S is found from the end sync marker, or pass `latency=`)
and returns a JSON-ready dict. Every measurement compares output with the KNOWN input, so it says
what the block does, not what was played.

Methods, by section:

  alignment      cross-correlation with the end chirp; earliest peak within 6 dB of the best
  linear         Farina ESS deconvolution, linear IR windowed and divided by the same pipeline run
                 on the dry sweep (self-calibrating), power-averaged over +-1/24 octave on a
                 1/6-octave grid 31 Hz-16 kHz, one curve per sweep level
  harmonics      least-squares fit of DC + H1..H9 on the last 0.14 s of each stepped-sine level:
                 gain, H2..H9 re fundamental, THD, odd/even power ratio
  static_curve   two-slope fit of output vs input level (dB) at 440 Hz: knee, slopes, ratio; 1 dB
                 compression point
  transfer_curve memoryless curve y = f(x) binned from the loudest 110 Hz step (best lag of +-16
                 samples), with its R^2 (low R^2 = the block is not memoryless, curve is indicative)
  imd            two-tone least squares: IMD2 (f2-f1, f1+f2), IMD3 (2f1-f2, 2f2-f1) for 440+554;
                 total non-harmonic products on the 55 Hz grid for the 3:2 power-chord pair
  dynamics       complex demodulation at 1 kHz of wet / dry (same filter on both): gain vs time;
                 attack and release time constants from a log-linear fit of the deviation from the
                 settled gain; level-dependent gain; gain-reduction envelope at fixed times; burst
                 overshoot; gate threshold from the -30..-80 dBFS staircase
  delay          echo peaks in the impulse response's 1 ms RMS envelope: time (linear fit of echo
                 positions), feedback (log-linear fit of echo energies), echo level re direct,
                 and how much of the tail is discrete echoes (delay vs reverb)
  reverb         Schroeder backward integration of the late tail (direct excluded, noise floor
                 subtracted): T20, T30, RT60, EDT, wet level
  modulation     demodulated 1 kHz carrier, wet / dry: amplitude (dB) and phase tracks; LFO rate from
                 the periodogram, refined by least squares; depth as amplitude dB p-p, delay ms p-p,
                 FM cents
  di             per real-DI excerpt: RMS gain, crest in/out, magnitude-squared coherence and
                 effective response per octave, nonlinear (incoherent) fraction of output power
"""
from __future__ import annotations

import datetime as dt

import numpy as np
from scipy import signal as sps
from scipy.ndimage import uniform_filter1d

from . import signals as S

EPS = 1e-20
H_ORDERS = tuple(range(2, 10))
FR_GRID = tuple(float(1000.0 * 2 ** (k / 6)) for k in range(-30, 25))   # 31.25 Hz .. 16 kHz
OCT_BANDS = (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)
GR_TIMES_MS = (1, 3, 10, 30, 100, 300, 900)


def _db(x, floor: float = -200.0):
    return np.maximum(20.0 * np.log10(np.maximum(np.abs(x), EPS)), floor)


def _pdb(p, floor: float = -200.0):
    return np.maximum(10.0 * np.log10(np.maximum(p, EPS)), floor)


def _mono(x: np.ndarray, channel: str = "mid") -> np.ndarray:
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 2:
        if a.shape[0] == 2 and a.shape[1] > 2:
            a = a.T
        if channel == "left":
            a = a[:, 0]
        elif channel == "right":
            a = a[:, -1]
        else:
            a = a.mean(axis=1)
    if a.ndim != 1:
        raise ValueError(f"expected (n,) or (n, 2) audio, got {np.shape(x)}")
    if not np.all(np.isfinite(a)):
        raise ValueError("wet render contains NaN or inf")
    return a


def _ls(y: np.ndarray, freqs, sr: int = S.SAMPLE_RATE) -> tuple[np.ndarray, float]:
    """Least-squares complex amplitudes (peak) of sinusoids at `freqs` plus DC."""
    t = np.arange(y.size) / sr
    cols = [np.ones_like(t)]
    for f in freqs:
        cols += [np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)]
    A = np.stack(cols, axis=1)
    c, *_ = np.linalg.lstsq(A, y, rcond=None)
    amps = c[1::2] - 1j * c[2::2]
    return amps, float(c[0])


def _demod(x: np.ndarray, f: float, lp_hz: float, sr: int = S.SAMPLE_RATE) -> np.ndarray:
    t = np.arange(x.size) / sr
    sos = sps.butter(4, lp_hz, fs=sr, output="sos")
    b = x * np.exp(-2j * np.pi * f * t)
    return 2.0 * (sps.sosfiltfilt(sos, b.real) + 1j * sps.sosfiltfilt(sos, b.imag))


def _r(v, nd: int = 4):
    """JSON-safe rounding (None for NaN/inf)."""
    if v is None:
        return None
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_r(u, nd) for u in v]
    v = float(v)
    if not np.isfinite(v):
        return None
    return round(v, nd)


# ----------------------------------------------------------------------------------------- alignment

def estimate_latency(wet: np.ndarray, probe: S.ProbeSet) -> tuple[int | None, float]:
    """Latency in samples from the end sync chirp, and the match strength (normalised xcorr)."""
    sr = probe.sample_rate
    seg = probe.seg("marker")
    m = S.marker(sr)
    hi = seg.start + m.size + int(S.MAX_LATENCY_S * sr)
    w = wet[seg.start:hi]
    if w.size < m.size + 1 or np.max(np.abs(w)) < 1e-9:
        return None, 0.0
    xc = sps.correlate(w, m, mode="valid")
    env = np.abs(sps.hilbert(xc))
    best = float(env.max())
    i0 = int(np.argmax(env >= 0.5 * best))                 # earliest strong arrival
    j = i0 + int(np.argmax(env[i0:i0 + int(0.002 * sr)]))
    k0 = max(0, j - 24)
    lag = k0 + int(np.argmax(np.abs(xc[k0:j + 25])))
    strength = best / (np.linalg.norm(m) * np.linalg.norm(w[lag:lag + m.size]) + EPS)
    return lag, float(min(strength, 1.0))


# ------------------------------------------------------------------------------------------- linear

def _ess_window(n_sweep: int, n_gap: int, sr: int) -> tuple[int, int, np.ndarray]:
    # The deconvolved sweep has long low-frequency pre-ringing (only half its energy lies within
    # 256 samples of the peak), so the window opens 125 ms early; the 2nd-harmonic IR sits
    # L ln 2 = 150 ms before the linear IR and stays outside.
    pre = int(0.125 * sr)
    post = n_gap - int(0.02 * sr)
    w = np.ones(pre + post)
    nt0 = pre // 4
    w[:nt0] = 0.5 - 0.5 * np.cos(np.pi * np.arange(nt0) / nt0)
    nt = int(0.2 * post)
    w[-nt:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(nt) / nt)
    return pre, post, w


def _band_avg(freqs: np.ndarray, power: np.ndarray, centres, half_oct: float) -> np.ndarray:
    out = []
    for c in centres:
        m = (freqs >= c * 2 ** -half_oct) & (freqs <= c * 2 ** half_oct)
        out.append(power[m].mean() if m.any() else np.nan)
    return np.array(out)


def analyse_linear(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> dict:
    sr = probe.sample_rate
    inv = S.ess_inverse(sr)
    n = inv.size
    segs = probe.of_kind("ess")
    n_gap = segs[0].length - n
    pre, post, win = _ess_window(n, n_gap, sr)
    nfft = 1 << int(np.ceil(np.log2(win.size)))
    ref = sps.fftconvolve(S.ess(0.0, sr), inv)[n - 1 - pre:n - 1 + post] * win
    R = np.fft.rfft(ref, nfft)
    f = np.fft.rfftfreq(nfft, 1 / sr)
    mags, gains_1k, peak_off, pol = [], [], [], []
    for seg in segs:
        A = S.db2amp(seg.params["level_dbfs"])
        y = wet[seg.slice(lat)]
        h = sps.fftconvolve(y, inv)[n - 1 - pre:n - 1 + post] / A
        H = np.fft.rfft(h * win, nfft)
        p = np.abs(H) ** 2 / np.maximum(np.abs(R) ** 2, EPS)
        mag = _pdb(_band_avg(f, p, FR_GRID, 1 / 24))
        mags.append(mag)
        gains_1k.append(mag[FR_GRID.index(1000.0)])
        k = int(np.argmax(np.abs(h)))
        peak_off.append(k - pre)
        pol.append(1 if h[k] >= 0 else -1)
    return {
        "freqs_hz": _r(FR_GRID, 2),
        "levels_dbfs": [s.params["level_dbfs"] for s in segs],
        "mag_db": [_r(m, 3) for m in mags],
        "gain_1k_db": _r(gains_1k, 3),
        "ir_peak_offset_samples": peak_off,
        "polarity": pol,
    }


# ---------------------------------------------------------------------------- harmonics, static curve

def _two_slope(x: np.ndarray, y: np.ndarray) -> dict:
    best = None
    for k in np.linspace(x[1], x[-2], 241):
        A = np.stack([np.ones_like(x), np.minimum(x - k, 0), np.maximum(x - k, 0)], axis=1)
        c, *_ = np.linalg.lstsq(A, y, rcond=None)
        sse = float(np.sum((A @ c - y) ** 2))
        if best is None or sse < best[0] - 1e-12:
            best = (sse, k, c)
    sse, k, c = best
    s1, s2 = float(c[1]), float(c[2])
    bend = s1 - s2 > 0.1
    return {"knee_dbfs": float(k) if bend else None, "slope_below": s1, "slope_above": s2,
            "ratio": (s1 / s2 if s2 > 0.02 else None) if bend else 1.0, "fit_rms_db":
            float(np.sqrt(sse / x.size))}


def _p1db(levels: np.ndarray, gains: np.ndarray, drop: float = 1.0) -> float | None:
    g0 = gains[0]
    below = np.nonzero(gains <= g0 - drop)[0]
    if below.size == 0:
        return None
    i = int(below[0])
    if i == 0:
        return float(levels[0])
    x0, x1, y0, y1 = levels[i - 1], levels[i], gains[i - 1] - g0, gains[i] - g0
    return float(x0 + (-drop - y0) * (x1 - x0) / (y1 - y0 + EPS))


def _transfer_curve(x: np.ndarray, wet: np.ndarray, a: int, b: int, lat: int, amp: float) -> dict:
    nb = 41
    edges = np.linspace(-amp, amp, nb + 1)
    centres = 0.5 * (edges[1:] + edges[:-1])
    xs = x[a:b]
    idx = np.clip(np.digitize(xs, edges) - 1, 0, nb - 1)
    cnt = np.bincount(idx, minlength=nb)
    best = None
    for d in range(-16, 17):
        ys = wet[a + lat + d:b + lat + d]
        curve = np.bincount(idx, weights=ys, minlength=nb) / np.maximum(cnt, 1)
        res = ys - curve[idx]
        r2 = 1.0 - float(np.sum(res ** 2)) / (float(np.sum((ys - ys.mean()) ** 2)) + EPS)
        if best is None or r2 > best[0]:
            best = (r2, d, curve)
    r2, d, curve = best
    valid = cnt > 0
    pos, neg = float(curve[valid].max()), float(-curve[valid].min())
    return {"x": _r(centres / amp, 4), "x_scale": _r(amp, 5), "y": _r(curve, 5), "r2": _r(r2, 4),
            "lag_samples": d, "pos_ceiling": _r(pos, 5), "neg_ceiling": _r(neg, 5),
            "symmetry": _r(pos / neg, 4) if neg > 1e-9 and pos > 1e-9 else None}


def analyse_harmonics(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> tuple[dict, dict, dict]:
    sr = probe.sample_rate
    per = []
    curve = None
    for seg in probe.of_kind("stepped_sine"):
        f = seg.params["freq_hz"]
        levels = np.array(seg.params["levels_dbfs"])
        n_step = round(seg.params["step_s"] * sr)
        orders = [k for k in range(1, 10) if k * f < 0.45 * sr]
        fund, gain, hdb, thd, oe, dc = [], [], [], [], [], []
        for i, L in enumerate(levels):
            a = seg.start + i * n_step + int(0.10 * sr)
            b = seg.start + i * n_step + int(0.24 * sr)
            amps, c0 = _ls(wet[a + lat:b + lat], [k * f for k in orders], sr)
            mag = np.abs(amps)
            h1 = mag[0]
            fund.append(_db(h1))
            gain.append(_db(h1) - L)
            rel = [(_db(mag[k - 1] / (h1 + EPS), -140.0) if k in orders else None) for k in H_ORDERS]
            hdb.append(rel)
            hp = np.array([mag[k - 1] ** 2 if k in orders else 0.0 for k in H_ORDERS])
            thd.append(100.0 * np.sqrt(hp.sum()) / (h1 + EPS))
            odd = hp[[k % 2 == 1 for k in H_ORDERS]].sum()
            even = hp[[k % 2 == 0 for k in H_ORDERS]].sum()
            oe.append(_pdb(odd / (even + EPS * 1e-6), -200.0) if odd + even > 1e-24 else None)
            dc.append(c0 / (h1 + EPS))
        gains = np.array(gain)
        static = _two_slope(levels, np.array(fund))
        static.update({"freq_hz": f, "small_signal_gain_db": float(gains[0]),
                       "p1db_in_dbfs": _p1db(levels, gains, 1.0),
                       "p3db_in_dbfs": _p1db(levels, gains, 3.0),
                       "out_max_dbfs": float(np.max(fund))})
        per.append({"freq_hz": f, "levels_dbfs": levels.tolist(), "fund_out_dbfs": _r(fund, 3),
                    "gain_db": _r(gains, 3), "h_orders": list(H_ORDERS),
                    "h_db": [_r(r, 2) for r in hdb], "thd_pct": _r(thd, 5),
                    "thd_db": _r(_db(np.array(thd) / 100.0, -140.0), 2),
                    "odd_even_db": _r(oe, 2), "dc_re_fund": _r(dc, 5),
                    "static": {k: _r(v, 3) if not isinstance(v, str) else v
                               for k, v in static.items()}})
        if f == 110.0:
            i = len(levels) - 1
            a = seg.start + i * n_step + int(0.10 * sr)
            b = seg.start + i * n_step + int(0.24 * sr)
            curve = _transfer_curve(probe.signal.astype(np.float64), wet, a, b, lat,
                                    S.db2amp(levels[i]))
            curve["level_dbfs"] = float(levels[i])
    static_440 = next(p["static"] for p in per if p["freq_hz"] == 440.0)
    return {"per_freq": per}, static_440, curve or {}


# --------------------------------------------------------------------------------------------- IMD

def analyse_imd(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> list[dict]:
    sr = probe.sample_rate
    out = []
    for seg in probe.of_kind("two_tone"):
        f1, f2, L = seg.params["f1"], seg.params["f2"], seg.params["level_dbfs_per_tone"]
        a, b = seg.start + int(0.08 * sr), seg.start + int(0.48 * sr)
        y = wet[a + lat:b + lat]
        row = {"name": seg.name, "f1": f1, "f2": f2, "level_dbfs_per_tone": L}
        if abs(f2 / f1 - 1.5) < 1e-9:                 # 3:2: everything lies on the f1/2 grid
            g = f1 / 2
            ks = [k for k in range(1, 200) if k * g < 6000]
            amps, _ = _ls(y, [k * g for k in ks], sr)
            p = np.abs(amps) ** 2
            fund = p[ks.index(2)] + p[ks.index(3)] + EPS
            nonh = sum(p[i] for i, k in enumerate(ks) if k % 2 and k % 3)
            row.update({"fund_dbfs": _r([_db(np.sqrt(p[ks.index(2)])),
                                         _db(np.sqrt(p[ks.index(3)]))], 2),
                        "imd2_db": None, "imd3_db": None,
                        "imd_total_db": _r(_pdb(nonh / fund, -160.0), 2)})
        else:
            prods = {"d2": f2 - f1, "s2": f1 + f2, "l3": 2 * f1 - f2, "u3": 2 * f2 - f1,
                     "l5": 3 * f1 - 2 * f2, "u5": 3 * f2 - 2 * f1}
            harm = [2 * f1, 3 * f1, 2 * f2, 3 * f2]
            fr = [f1, f2] + list(prods.values()) + harm
            amps, _ = _ls(y, fr, sr)
            p = np.abs(amps) ** 2
            fund = p[0] + p[1] + EPS
            pp = dict(zip(prods, p[2:2 + len(prods)]))
            row.update({"fund_dbfs": _r([_db(np.sqrt(p[0])), _db(np.sqrt(p[1]))], 2),
                        "imd2_db": _r(_pdb((pp["d2"] + pp["s2"]) / fund, -160.0), 2),
                        "imd3_db": _r(_pdb((pp["l3"] + pp["u3"]) / fund, -160.0), 2),
                        "imd_total_db": _r(_pdb(sum(pp.values()) / fund, -160.0), 2)})
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------- dynamics

def _gain_track(wet: np.ndarray, probe: S.ProbeSet, seg: S.Segment, lat: int,
                lp_hz: float = 300.0) -> np.ndarray:
    f = seg.params["freq_hz"]
    x = probe.signal[seg.slice()].astype(np.float64)
    y = wet[seg.slice(lat)]
    zx, zy = _demod(x, f, lp_hz), _demod(y, f, lp_hz)
    g = _db(np.abs(zy) / np.maximum(np.abs(zx), 1e-12), -200.0)
    g[np.abs(zx) < 1e-7] = np.nan
    return g


def _fit_tau(g: np.ndarray, t0: int, t1: int, settled: float, sr: int) -> float | None:
    """Time constant (s) of the approach of g[t0:t1] (dB) to `settled`."""
    dev = np.abs(g[t0:t1] - settled)
    if dev.size < 10 or not np.all(np.isfinite(dev)):
        return None
    d0 = float(np.max(dev[: int(0.005 * sr)]))
    if d0 < 0.5:
        return None
    i_end = np.nonzero(dev < 0.05 * d0)[0]
    stop = int(i_end[0]) if i_end.size else dev.size
    idx = np.nonzero((dev[:stop] < 0.8 * d0) & (dev[:stop] > 0.05 * d0))[0]
    if idx.size < 10:
        return None
    slope = np.polyfit(idx / sr, np.log(dev[idx]), 1)[0]
    return float(-1.0 / slope) if slope < 0 else None


def analyse_dynamics(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> dict:
    sr = probe.sample_rate
    seg = probe.seg("level_step")
    g = _gain_track(wet, probe, seg, lat)
    (L0, d0), (L1, d1), (L2, d2) = seg.params["steps"]
    tu, td, tend = int(d0 * sr), int((d0 + d1) * sr), int((d0 + d1 + d2) * sr)

    def med(a: float, b: float) -> float:
        return float(np.nanmedian(g[int(a * sr):int(b * sr)]))

    g_low = med(d0 - 0.35, d0 - 0.05)
    g_high = med(d0 + d1 - 0.35, d0 + d1 - 0.05)
    g_rel = med(d0 + d1 + d2 - 0.25, d0 + d1 + d2 - 0.05)
    skip = int(0.002 * sr)
    attack = _fit_tau(g, tu + skip, td, g_high, sr)
    release = _fit_tau(g, td + skip, tend - int(0.02 * sr), g_rel, sr)
    gr_up = [float(g[tu + int(ms * sr / 1000)] - g_low) for ms in GR_TIMES_MS]
    gr_dn = [float(g[td + int(ms * sr / 1000)] - g_low) for ms in GR_TIMES_MS]

    bseg = probe.seg("bursts")
    gb = _gain_track(wet, probe, bseg, lat)
    on, off = bseg.params["on_s"], bseg.params["off_s"]
    ov = []
    for k in range(bseg.params["count"]):
        o = int(k * (on + off) * sr)
        head = gb[o + int(0.001 * sr):o + int(0.02 * sr)]
        tail = gb[o + int((on - 0.015) * sr):o + int((on - 0.003) * sr)]
        ov.append(float(np.nanmax(head) - np.nanmedian(tail)))

    gseg = probe.seg("gate_stair")
    gg = _gain_track(wet, probe, gseg, lat, lp_hz=100.0)
    levels = gseg.params["levels_dbfs"]
    per = (gseg.params["on_s"] + gseg.params["off_s"]) * sr
    gate_gains = []
    for k in range(len(levels)):
        o = int(k * per)
        gate_gains.append(float(np.nanmedian(gg[o + int(0.08 * sr):o + int(0.22 * sr)])))
    gate_gains_a = np.array(gate_gains)
    ref = float(np.max(gate_gains_a[:2]))
    drop = ref - gate_gains_a
    closed = drop > 6.0
    thr = None
    if closed.any() and not closed[0]:
        i = int(np.argmax(closed))
        if closed[i:].all():
            thr = 0.5 * (levels[i - 1] + levels[i])
    return {
        "carrier_hz": seg.params["freq_hz"],
        "step_levels_dbfs": [L0, L1, L2],
        "gain_low_db": _r(g_low, 3), "gain_high_db": _r(g_high, 3),
        "level_dependent_gain_db": _r(g_high - g_low, 3),
        "attack_ms": _r(None if attack is None else 1000 * attack, 3),
        "release_ms": _r(None if release is None else 1000 * release, 3),
        "gr_times_ms": list(GR_TIMES_MS),
        "gr_after_up_db": _r(gr_up, 3), "gr_after_down_db": _r(gr_dn, 3),
        "burst_overshoot_db": _r(float(np.mean(ov)), 3),
        "gate_levels_dbfs": list(levels), "gate_gain_db": _r(gate_gains, 3),
        "gate_threshold_dbfs": _r(thr, 2),
    }


# ------------------------------------------------------------------------------------ delay, reverb

def analyse_time(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> tuple[dict, dict]:
    sr = probe.sample_rate
    seg = probe.seg("impulse")
    # open the window 10 ms early: a marker latency that includes some group delay can put the
    # direct sound a few samples BEFORE the nominal start (the probe has 0.5 s of silence there)
    pre = int(0.01 * sr)
    sl = seg.slice(lat)
    h = wet[sl.start - pre:sl.stop - pre] / S.db2amp(seg.params["level_dbfs"])
    e = np.sqrt(np.maximum(uniform_filter1d(h * h, int(0.001 * sr), mode="constant"), 0.0))
    emax = float(e.max()) + EPS
    w = int(0.0025 * sr)

    def energy(p: int) -> float:
        return float(np.sum(h[max(0, p - w):p + w] ** 2))

    def refine(c: int, r: int) -> int:
        """Peak of the raw |h| near the envelope maximum in [c - r, c + r] (the 1 ms envelope
        has a flat top, so its argmax alone is ambiguous by up to 1 ms)."""
        lo = max(0, c - r)
        j = lo + int(np.argmax(e[lo:c + r + 1]))
        lo2 = max(0, j - int(0.001 * sr))
        return lo2 + int(np.argmax(np.abs(h[lo2:j + int(0.001 * sr) + 1])))

    early = pre + int(0.02 * sr)
    has_direct = e[:early].max() >= 0.01 * emax
    p0 = refine(early // 2, early // 2) if has_direct else pre
    a0 = np.sqrt(energy(p0)) if has_direct else 0.0

    # ---- delay: discrete echoes at regular spacing
    delay = {"detected": False, "time_ms": None, "feedback": None, "echo_level_db": None,
             "n_echoes": 0, "discreteness": None}
    start = p0 + int(0.015 * sr)
    tail_e = float(np.sum(h[p0 + w:] ** 2)) + EPS
    if start < e.size - w:
        pk, _ = sps.find_peaks(e[start:], distance=int(0.01 * sr), height=emax * 1e-3)
        if pk.size:
            p1 = refine(start + int(pk[np.argmax(e[start + pk])]), int(0.002 * sr))
            D0 = p1 - p0
            pos, amps = [], []
            k = 1
            while p0 + k * D0 + w < h.size:
                j = refine(p0 + k * D0, int(0.003 * sr))
                a = np.sqrt(energy(j))
                if amps and a < 1e-4 * amps[0]:
                    break
                pos.append(j)
                amps.append(a)
                k += 1
            disc = float(sum(a * a for a in amps)) / tail_e
            delay["discreteness"] = _r(disc, 4)
            if disc >= 0.5 and amps:
                ks = np.arange(1, len(pos) + 1)
                if len(pos) >= 2:
                    D = float(np.polyfit(ks, np.array(pos, float) - p0, 1)[0])
                else:
                    D = float(pos[0] - p0)
                good = [i for i, a in enumerate(amps) if a > 1e-4 * amps[0]]
                if len(good) >= 2:
                    fb = float(np.exp(np.polyfit(ks[good], np.log(np.array(amps)[good]), 1)[0]))
                else:
                    fb = 0.0
                delay.update({"detected": True, "time_ms": _r(1000 * D / sr, 3),
                              "feedback": _r(fb, 4), "n_echoes": len(pos),
                              "echo_level_db": _r(_db(amps[0] / a0), 3) if a0 > 0 else None})

    # ---- reverb: Schroeder integration of the late tail
    ts = p0 + int(0.005 * sr) if has_direct else int(np.argmax(e > 0.1 * emax))
    tail = h[ts:]
    # noise floor: median of 10 ms block powers over the last 0.5 s (robust to a late echo)
    blk = int(0.01 * sr)
    last = h[-int(0.5 * sr):]
    noise = float(np.median((last[: last.size // blk * blk] ** 2).reshape(-1, blk).mean(axis=1)))
    env_db = _pdb(uniform_filter1d(tail * tail, blk))
    te = tail.size
    if noise > 0:
        above = np.nonzero(env_db >= _pdb(noise) + 6.0)[0]    # last time clearly above the floor
        te = max(int(above[-1]) + 1, int(0.02 * sr)) if above.size else int(0.02 * sr)
    p = np.maximum(tail[:te] ** 2 - noise, 0.0)
    edc = np.cumsum(p[::-1])[::-1]
    # Lundeby-style compensation: the energy after the truncation point te, extrapolated from the
    # late decay rate (fit of the smoothed envelope from 10 dB below its peak to 10 dB above the
    # noise floor), so a noise-limited tail does not bend the EDC down and shorten the RT.
    if te < tail.size and te > int(0.05 * sr):
        e10 = env_db[:te]
        hi_db = float(e10.max()) - 10.0
        lo_db = _pdb(noise) + 10.0
        i_pk = int(np.argmax(e10))
        gone = np.nonzero(e10[i_pk:] < lo_db)[0]
        stop = i_pk + (int(gone[0]) if gone.size else e10.size - i_pk)
        idx = i_pk + np.nonzero(e10[i_pk:stop] <= hi_db)[0]      # contiguous initial decay only
        if idx.size > int(0.05 * sr):
            k = np.polyfit(idx / sr, e10[idx], 1)[0]           # dB per second (negative)
            if k < -1.0:
                p_te = float(np.mean(p[max(0, te - blk):te]))
                comp = min(p_te * 10.0 / (-k * np.log(10.0)), 0.1 * float(edc[0]))
                edc = edc + comp
    reverb = {"rt60_s": None, "t20_s": None, "t30_s": None, "edt_s": None,
              "decay_range_db": None, "wet_level_db": None}
    if edc[0] > 0:
        edc_db = _pdb(edc / edc[0], -300.0)
        # usable decay range: early-tail level over the noise floor (capped at 120 dB)
        head = float(env_db[:te].max())
        floor_db = _pdb(noise) if noise > 0 else float(env_db[env_db > -199].min(initial=head))
        rng = float(min(head - floor_db, 120.0))
        reverb["decay_range_db"] = _r(rng, 2)

        def fit(lo: float, hi: float) -> float | None:
            m = (edc_db <= lo) & (edc_db >= hi)
            if m.sum() < 20 or -hi + 5.0 > rng:               # needs 5 dB beyond the fit range
                return None
            idx = np.nonzero(m)[0]
            s = np.polyfit(idx / sr, edc_db[idx], 1)[0]
            return float(-60.0 / s) if s < 0 else None

        t20, t30 = fit(-5, -25), fit(-5, -35)
        reverb.update({"t20_s": _r(t20, 4), "t30_s": _r(t30, 4),
                       "rt60_s": _r(t30 if t30 is not None else t20, 4)})
        full = h[max(0, p0 - int(0.001 * sr)):][: ts - p0 + te] ** 2
        fe = np.cumsum(full[::-1])[::-1]
        fe_db = _pdb(fe / (fe[0] + EPS), -300.0)
        below = np.nonzero(fe_db <= -10.0)[0]              # EDT: 6 x time to the -10 dB point
        if below.size and rng >= 15.0:
            reverb["edt_s"] = _r(6.0 * below[0] / sr, 4)
        if has_direct:
            reverb["wet_level_db"] = _r(_pdb(float(np.sum(tail ** 2)) / (energy(p0) + EPS)), 3)
    return delay, reverb


# -------------------------------------------------------------------------------------- modulation

def _sine_track_fit(v: np.ndarray, fs: float, fmax: float = 20.0) -> dict:
    """Best single sinusoid (plus linear trend) in a modulation track. Rates need at least two
    cycles in the track. `stationarity` is the ratio of the sinusoid amplitude fitted on the two
    halves (1 for a steady LFO; a decaying pattern such as echo build-up gives far from 1)."""
    t = np.arange(v.size) / fs
    fmin = 2.0 / (v.size / fs)
    vd = sps.detrend(v)
    nfft = 1 << 17
    P = np.abs(np.fft.rfft(vd * np.hanning(v.size), nfft)) ** 2
    fr = np.fft.rfftfreq(nfft, 1 / fs)
    m = (fr >= fmin) & (fr <= fmax)
    f0 = float(fr[m][np.argmax(P[m])])

    def fitf(f: float):
        A = np.stack([np.ones_like(t), t, np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)], 1)
        c, *_ = np.linalg.lstsq(A, v, rcond=None)
        return float(np.sum((A @ c - v) ** 2)), c

    grid = f0 + np.linspace(-0.05, 0.05, 41) * max(f0, 1.0)
    best = min(((fitf(f)[0], f) for f in grid if f >= fmin), key=lambda z: z[0])
    sse, c = fitf(best[1])
    trend = c[0] + c[1] * t
    resid_total = float(np.sum((v - trend) ** 2))
    r2 = 1.0 - sse / resid_total if resid_total > 1e-12 else 0.0
    d = v - trend
    half = v.size // 2
    amps = []
    for sl in (slice(0, half), slice(half, None)):
        tt = t[sl]
        A = np.stack([np.ones_like(tt), tt, np.cos(2 * np.pi * best[1] * tt),
                      np.sin(2 * np.pi * best[1] * tt)], 1)
        cc, *_ = np.linalg.lstsq(A, v[sl], rcond=None)
        amps.append(float(np.hypot(cc[2], cc[3])))
    stat = min(amps) / max(amps) if max(amps) > 1e-12 else 0.0
    return {"rate_hz": best[1], "amp": float(np.hypot(c[2], c[3])), "r2": r2,
            "pp": float(np.percentile(d, 99.5) - np.percentile(d, 0.5)), "stationarity": stat}


def analyse_modulation(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> dict:
    sr = probe.sample_rate
    seg = probe.seg("mod_sine")
    f = seg.params["freq_hz"]
    x = probe.signal[seg.slice()].astype(np.float64)
    y = wet[seg.slice(lat)]
    zx, zy = _demod(x, f, 100.0), _demod(y, f, 100.0)
    a, b = int(0.3 * sr), int((seg.params["dur_s"] - 0.3) * sr)
    dec = 48
    r = (zy / np.where(np.abs(zx) > 1e-9, zx, 1e-9))[a:b]
    if np.max(np.abs(r)) < 1e-6:
        return {"kind": "none", "rate_hz": None, "am_depth_db_pp": None, "delay_depth_ms_pp": None,
                "fm_depth_cents": None, "am_r2": None, "pm_r2": None, "note": "no carrier out"}
    am = _db(r)[::dec]
    ph = np.unwrap(np.angle(r))[::dec]
    fs = sr / dec
    fa, fp = _sine_track_fit(am, fs), _sine_track_fit(ph, fs)
    am_sig = fa["pp"] > 0.3 and fa["r2"] > 0.5 and fa["stationarity"] > 0.6
    delay_pp_ms = 1000.0 * fp["pp"] / (2 * np.pi * f)
    pm_sig = delay_pp_ms > 0.02 and fp["r2"] > 0.5 and fp["stationarity"] > 0.6
    kind = {(False, False): "none", (True, False): "amplitude", (False, True): "pitch",
            (True, True): "both"}[(am_sig, pm_sig)]
    rate = None
    if am_sig or pm_sig:
        cands = [z for z, s in ((fa, am_sig), (fp, pm_sig)) if s]
        rate = max(cands, key=lambda z: z["r2"])["rate_hz"]
    fm_cents = None
    if pm_sig:
        dev_hz = fp["amp"] * fp["rate_hz"]           # peak d(phi)/dt / (2 pi) for a sine track
        fm_cents = 1200.0 * np.log2(1.0 + dev_hz / f)
    return {"kind": kind, "rate_hz": _r(rate, 4),
            "am_depth_db_pp": _r(fa["pp"], 3), "delay_depth_ms_pp": _r(delay_pp_ms, 4),
            "fm_depth_cents": _r(fm_cents, 3), "am_r2": _r(fa["r2"], 3),
            "pm_r2": _r(fp["r2"], 3), "am_stationarity": _r(fa["stationarity"], 3),
            "pm_stationarity": _r(fp["stationarity"], 3), "am_rate_hz": _r(fa["rate_hz"], 4),
            "pm_rate_hz": _r(fp["rate_hz"], 4)}


# -------------------------------------------------------------------------------------------- DI

def analyse_di(wet: np.ndarray, probe: S.ProbeSet, lat: int) -> list[dict]:
    sr = probe.sample_rate
    out = []
    for seg in probe.of_kind("di"):
        n = round(seg.params["dur_s"] * sr)
        x = probe.signal[seg.start:seg.start + n].astype(np.float64)
        y = wet[seg.start + lat:seg.start + lat + n]
        fr, C = sps.coherence(x, y, fs=sr, nperseg=4096)
        _, Pxx = sps.welch(x, fs=sr, nperseg=4096)
        _, Pyy = sps.welch(y, fs=sr, nperseg=4096)
        _, Pxy = sps.csd(x, y, fs=sr, nperseg=4096)
        H2 = np.abs(Pxy) ** 2 / np.maximum(Pxx, EPS) ** 2
        band = (fr > 40) & (fr < 16000)
        nl = float(np.sum((1 - C[band]) * Pyy[band]) / (np.sum(Pyy[band]) + EPS))

        def crest(v: np.ndarray) -> float:
            return float(_db(np.max(np.abs(v)) / (np.sqrt(np.mean(v * v)) + EPS)))

        def centroid(P: np.ndarray, fr: np.ndarray = fr, band: np.ndarray = band) -> float:
            return float(np.sum(fr[band] * P[band]) / (np.sum(P[band]) + EPS))

        out.append({
            "name": seg.name, "path": seg.params["path"], "start_s": seg.params["start_s"],
            "rms_gain_db": _r(_db(np.sqrt(np.mean(y * y)) / (np.sqrt(np.mean(x * x)) + EPS)), 3),
            "crest_in_db": _r(crest(x), 3), "crest_out_db": _r(crest(y), 3),
            "centroid_in_hz": _r(centroid(Pxx), 1), "centroid_out_hz": _r(centroid(Pyy), 1),
            "bands_hz": list(OCT_BANDS),
            "coherence": _r(_band_avg(fr, C, OCT_BANDS, 0.5), 4),
            "response_db": _r(_pdb(_band_avg(fr, H2, OCT_BANDS, 0.5)), 3),
            "nonlinear_fraction_db": _r(_pdb(nl), 3),
        })
    return out


# ---------------------------------------------------------------------------------------- analyse

def analyse(wet: np.ndarray, probe: S.ProbeSet, *, latency: int | None = None,
            subject: dict | None = None, channel: str = "mid") -> dict:
    """Fingerprint of one block from its render of `probe`. See docs/notes/block-probe.md."""
    from .fingerprint import SCHEMA, SCHEMA_VERSION

    y = _mono(wet, channel)
    need = probe.signal.size
    if y.size < need:
        y = np.pad(y, (0, need - y.size))
    notes = []
    m_lat, strength = estimate_latency(y, probe)
    if latency is None:
        if m_lat is None:
            latency, src = 0, "default-0"
            notes.append("sync marker not found; latency assumed 0")
        else:
            latency, src = m_lat, "marker"
    else:
        src = "given"
    if strength < 0.5:
        notes.append(f"weak sync marker match ({strength:.2f}): latency may be off")
    y = np.pad(y, (0, latency + 1))
    harmonics, static, curve = analyse_harmonics(y, probe, latency)
    delay, reverb = analyse_time(y, probe, latency)
    fp = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "probe_version": probe.version,
        "probe_sha256": probe.layout()["sha256"],
        "sample_rate": probe.sample_rate,
        "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "subject": subject or {},
        "channel": channel,
        "alignment": {"latency_samples": int(latency), "source": src,
                      "marker_latency_samples": m_lat, "marker_match": _r(strength, 3)},
        "linear": analyse_linear(y, probe, latency),
        "harmonics": harmonics,
        "static_curve": static,
        "transfer_curve": curve,
        "imd": analyse_imd(y, probe, latency),
        "dynamics": analyse_dynamics(y, probe, latency),
        "delay": delay,
        "reverb": reverb,
        "modulation": analyse_modulation(y, probe, latency),
        "di": analyse_di(y, probe, latency),
        "notes": notes,
    }
    return fp
