"""Time-domain analysis of a target against its DI for the time-based specialist: the impulse response h of
the block (regularised deconvolution, known-DI mode), its energy envelope, and what tells a delay from a
reverb from a modulation.

    ir = ir_analysis(target, dry)              # IRView: h, direct index, 2 ms envelope, floor
    f  = shape_features(ir)                    # delay vs reverb vs the rest
    e  = echo_pattern(ir)                      # discrete echoes: times, levels (any number, any spacing)
    c  = decay_curve(ir)                       # smooth tail: energy-decay curve per band

What each class leaves in h (the physics the discrimination rests on):

- delay: a few discrete peaks with near-silence between them. Most of the late energy sits in a handful of
  10 ms blocks (`conc`), the envelope's dips go to the floor (`valley_db`), the peaks repeat at a common
  spacing (`comb`), and the energy right after the direct sound is empty (`early_gap_db`).
- reverb: a smooth, roughly exponential decay with no dips: the envelope is close to a straight line in dB
  (`line_resid_db` small), the late energy is spread over many blocks (`conc` small), and the echo density
  rises with time, so the envelope has no gap after the direct sound unless there is a pre-delay.
- modulation: the linear part explains little (`lin_resid_db` from the presence module) and the response
  of a block that only wobbles has no late energy at all.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal
from scipy.ndimage import median_filter, uniform_filter1d

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import SR, db10, impulse_response

IR_LEN_S = 2.5
IR_PRE_S = 0.010
BLK_S = 0.002                    # envelope block
LATE_S = 0.025                   # the late part starts here (re the direct sound)
ABS_MIN_DB = -48.0                # an echo this far under the direct sound is below the deconvolution's usable range
BANDS_HZ = ((60.0, 400.0), (400.0, 2000.0), (2000.0, 12000.0))   # decay-curve bands


@dataclass
class IRView:
    h: np.ndarray                # impulse response, index `pre` is lag 0
    pre: int
    d: int                       # direct-sound index in h
    e_dir: float                 # direct-sound energy (sum of h^2 over -2..+10 ms)
    env_db: np.ndarray           # 2 ms block energy in dB re the direct sound's block, from the direct index
    floor_db: float              # the deconvolution's own floor, same scale
    t0: float                    # time of env_db[0] re the direct sound, s
    rms_in: float
    ok: bool = True


def _unit(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float64)
    return x / (np.sqrt(np.mean(x ** 2)) + EPS)


def ir_analysis(target: np.ndarray, dry: np.ndarray) -> IRView:
    y, x = as_mono(target, "target"), as_mono(dry, "dry")
    m = min(y.size, x.size)
    y, x = _unit(y[:m]), _unit(x[:m])
    pre = int(IR_PRE_S * SR)
    h = impulse_response(x, y, n_out=int(IR_LEN_S * SR), pre=pre)
    lo, hi = pre - int(0.005 * SR), pre + int(0.010 * SR)
    d = lo + int(np.argmax(np.abs(h[lo:hi])))
    a, b = d - int(0.002 * SR), d + int(0.010 * SR)
    e_dir = float(np.sum(h[a:b] ** 2)) + EPS
    blk = int(BLK_S * SR)
    seg = h[d:]
    nb = seg.size // blk
    eb = (seg[: nb * blk] ** 2).reshape(nb, blk).sum(1)
    ref = float(np.max(eb[: max(int(0.012 / BLK_S), 1)])) + EPS
    env = db10(eb / ref)
    neg = h[: max(pre - int(0.003 * SR), 64)]
    floor_db = float(db10((float(np.mean(neg ** 2)) * blk + EPS) / ref))
    i0 = int(LATE_S / BLK_S)
    if env.size > i0 + 50:
        # the negative-time part is short and carries the band-limiting's ringing; the quiet end of the late
        # part is the other estimate of the floor: take the lower
        floor_db = min(floor_db, float(np.percentile(env[i0:], 20)))
    return IRView(h, pre, d, e_dir, env, floor_db, 0.0, float(np.sqrt(np.mean(x ** 2))), True)


def _active(ir: IRView, margin_db: float = 6.0) -> tuple[int, int]:
    """Block range [i0, i1) of the late part that is above the floor (smoothed), from LATE_S to the last
    block that stays `margin_db` above the floor for 50 ms."""
    i0 = int(LATE_S / BLK_S)
    e = ir.env_db
    if e.size <= i0 + 10:
        return i0, i0
    sm = median_filter(e, size=25, mode="nearest")
    above = np.flatnonzero(sm[i0:] >= ir.floor_db + margin_db)
    if above.size == 0:
        return i0, i0
    return i0, i0 + int(above[-1]) + 1


def shape_features(ir: IRView) -> dict[str, float]:
    """Numbers that separate a train of echoes from a smooth tail. NaN when there is no late part."""
    nan = float("nan")
    out = {k: nan for k in ("late_db", "span_s", "conc", "valley_db", "line_resid_db", "comb", "early_gap_db",
                            "peaks_per_s", "slope_db_s", "dens_rise")}
    i0, i1 = _active(ir)
    e = ir.env_db
    p = 10 ** (e / 10)
    out["late_db"] = float(db10(p[int(LATE_S / BLK_S):].sum() / max(p[:int(0.012 / BLK_S)].sum(), 1e-12)))
    if i1 - i0 < 15:
        out["span_s"] = 0.0
        return out
    seg = e[i0:i1]
    ps = p[i0:i1]
    out["span_s"] = float((i1 - i0) * BLK_S)
    # 10 ms blocks: the share of the late energy that stands 8 dB over its own 150 ms neighbourhood (a train of
    # echoes is made of such blocks; a smooth decay, even a steep one, has almost none)
    b10 = 5
    n10 = ps.size // b10
    q = ps[: n10 * b10].reshape(n10, b10).sum(1)
    if n10 >= 15:
        qdb = db10(q + 1e-18)
        loc = median_filter(qdb, size=15, mode="nearest")
        out["conc"] = float(q[qdb > loc + 8.0].sum() / (q.sum() + 1e-18))
    # valleys: the 10th percentile of the 10 ms envelope against its 90th, in dB (a floor-limited delay has deep ones)
    qd = db10(q + 1e-18)
    if qd.size >= 5:
        out["valley_db"] = float(np.percentile(qd, 90) - max(np.percentile(qd, 10), ir.floor_db + db10(b10)))
    # straight-line fit of the (floor-clipped) dB envelope: a reverb tail is one
    t = np.arange(seg.size) * BLK_S
    sc = np.maximum(seg, ir.floor_db + 3.0)
    A = np.vstack([t, np.ones_like(t)]).T
    coef, *_ = np.linalg.lstsq(A, sc, rcond=None)
    out["line_resid_db"] = float(np.sqrt(np.mean((sc - A @ coef) ** 2)))
    out["slope_db_s"] = float(coef[0])
    # peaks: count per second of peaks standing 6 dB above a 100 ms running median
    median_filter(seg, size=min(51, seg.size // 2 * 2 + 1), mode="nearest")
    pk, _ = signal.find_peaks(seg, prominence=6.0, distance=5)
    out["peaks_per_s"] = float(pk.size / max(out["span_s"], 0.05))
    # comb: how much of the envelope's variance one spacing explains (autocorrelation peak of the 2 ms energy, 30 ms - 1.2 s)
    z = ps - ps.mean()
    if z.size > 40 and z.std() > 0:
        ac = np.correlate(z, z, "full")[z.size - 1:]
        ac = ac / (ac[0] + 1e-18)
        lo_l, hi_l = int(0.03 / BLK_S), min(int(1.2 / BLK_S), ac.size - 1)
        if hi_l > lo_l + 3:
            out["comb"] = float(ac[lo_l:hi_l].max())
    # the direct sound is followed by silence before the first echo (delay) or by diffuse energy (reverb)
    gap = e[int(0.012 / BLK_S): i0 + int(0.04 / BLK_S)]
    if gap.size:
        out["early_gap_db"] = float(np.median(gap) - (seg.max() if seg.size else 0.0))
    # density rise: slope of the local roughness (std of the 10 ms dB envelope about its running mean) with time
    qr = qd - uniform_filter1d(qd, 5, mode="nearest")
    if qr.size >= 12:
        h2 = qr.size // 2
        out["dens_rise"] = float(np.std(qr[:h2]) - np.std(qr[h2:]))
    return out


def echo_pattern(ir: IRView, max_echoes: int = 12, min_prom_db: float = 8.0, floor_margin_db: float = 8.0
                 ) -> dict[str, list[float]]:
    """Discrete echoes after the direct sound: time (s), level (dB re the direct sound's block), from the 2 ms
    envelope's peaks. Any number, any spacing (multi-tap, harmony, ping-pong, reverse included)."""
    e = ir.env_db
    i0 = int(LATE_S / BLK_S)
    if e.size <= i0 + 5:
        return {"t": [], "level_db": []}
    sm = 10 * np.log10(uniform_filter1d(10 ** (e / 10), 3, mode="nearest") + 1e-18)   # smooth in power, not in dB
    base = median_filter(sm, size=51, mode="nearest")
    pk, _pr = signal.find_peaks(sm[i0:], prominence=min_prom_db, distance=int(0.02 / BLK_S))
    pk = pk + i0
    keep = [(int(i), float(e[i])) for i in pk if e[i] >= max(ir.floor_db + floor_margin_db, ABS_MIN_DB) and sm[i] - base[i] >= min_prom_db]
    keep.sort(key=lambda a: -a[1])
    keep = sorted(keep[:max_echoes])
    # a feedback delay whose first repeats are weak can show echo 2 or 3 but not echo 1: look for the missing
    # ones at t/2, t/3, t/4 of the earliest echo with a lower bar (a bump 4 dB over its surroundings)
    if keep and keep[0][0] * BLK_S > 0.06:
        t0 = keep[0][0]
        for n in (2, 3, 4):
            c = round(t0 / n)
            if c * BLK_S < 0.03:
                break
            lo, hi = max(c - 3, i0), min(c + 4, e.size)
            if hi <= lo:
                continue
            j = lo + int(np.argmax(sm[lo:hi]))
            if sm[j] - base[j] >= 6.0 and e[j] >= keep[0][1] - 12.0:
                keep = sorted(set(keep + [(j, float(e[j]))]))
                break
    return {"t": [i * BLK_S for i, _ in keep], "level_db": [l for _, l in keep]}


def onsets(ir: IRView, min_rise_db: float = 6.0, max_n: int = 8) -> dict[str, list[float]]:
    """Rising edges of the late energy at 10 ms resolution: time (s), rise (dB over the median of the 20-100 ms
    before it) and level (dB re the direct block). An echo smeared by tape wow, drive or a modulated buffer is a
    plateau, not a peak (the deconvolution of a time-varying echo spreads it), but its front is still sharp and
    sits at the delay time."""
    e = ir.env_db
    nb = e.size // 5
    if nb < 30:
        return {"t": [], "rise_db": [], "level_db": []}
    q = 10 * np.log10((10 ** (e[: nb * 5] / 10)).reshape(nb, 5).sum(1) + 1e-18)
    out = []
    lo_q = max(ir.floor_db + 7.0, ABS_MIN_DB + 3.0)
    for i in range(4, nb - 3):
        prev = float(np.median(q[max(i - 10, 1): i - 1])) if i > 3 else q[1]
        rise = float(np.max(q[i: i + 3]) - prev)
        if rise >= min_rise_db and float(np.max(q[i: i + 3])) >= lo_q and i * 0.01 >= LATE_S + 0.005:
            out.append((i, rise, float(np.max(q[i: i + 3]))))
    # keep the local maxima of rise, at least 50 ms apart
    out.sort(key=lambda a: -a[1])
    kept: list[tuple[int, float, float]] = []
    for a in out:
        if all(abs(a[0] - b[0]) >= 5 for b in kept):
            kept.append(a)
        if len(kept) >= max_n:
            break
    kept.sort()
    return {"t": [a[0] * 0.01 + 0.005 for a in kept], "rise_db": [a[1] for a in kept], "level_db": [a[2] for a in kept]}


# ---------------------------------------------------------------------------- probe features (for the seeds)

def _band_energy_db(seg: np.ndarray, n_fft: int = 4096) -> np.ndarray:
    P = np.abs(np.fft.rfft(seg * np.hanning(seg.size) if seg.size > 8 else seg, n_fft)) ** 2
    f = np.fft.rfftfreq(n_fft, 1 / SR)
    return np.array([db10(P[(f >= lo) & (f < hi)].sum() + 1e-18) for lo, hi in BANDS_HZ])


def decay_curve(ir: IRView, tmax_s: float = 2.4) -> dict[str, np.ndarray]:
    """Energy-decay curve (backward integral of h^2 from the late part, dB re its start) per band, on a 20 ms
    grid, and the full-band one. Floor-corrected: the deconvolution's own floor is subtracted from the energy
    before integrating, and the integral stops where the energy meets the floor."""
    from scipy.signal import butter, sosfiltfilt

    h = ir.h[ir.d + int(LATE_S * SR): ir.d + int(tmax_s * SR)]
    out: dict[str, np.ndarray] = {}
    blk = int(0.02 * SR)
    nb = h.size // blk
    if nb < 5:
        return out
    for name, (lo, hi) in (("full", (60.0, 12000.0)), *((f"b{i}", b) for i, b in enumerate(BANDS_HZ))):
        sos = butter(4, [lo / (SR / 2), min(hi, 20000.0) / (SR / 2)], btype="band", output="sos")
        hb = sosfiltfilt(sos, h)
        e = (hb[: nb * blk] ** 2).reshape(nb, blk).sum(1)
        # noise floor: the median of the last tenth of the blocks where the tail has surely died, else 0
        nf = float(np.median(e[-max(nb // 10, 2):]))
        late_flat = float(np.median(e[-max(nb // 10, 2):]) / (np.median(e[: max(nb // 10, 2)]) + 1e-18))
        e2 = np.maximum(e - nf, 0.0) if late_flat > 0.01 else e
        c = np.cumsum(e2[::-1])[::-1]
        out[name] = db10(c / (c[0] + 1e-18) + 1e-12)
        out[name + "_e"] = e
    return out


def rt60_from_edc(curve_db: np.ndarray, block_s: float = 0.02) -> float:
    """RT60 from an energy-decay curve: T20 (-5 to -25 dB) scaled, else EDT (0 to -10 dB) scaled, else NaN."""
    for hi, lo in ((-5.0, -25.0), (0.0, -10.0)):
        sel = np.flatnonzero((curve_db <= hi) & (curve_db >= lo))
        if sel.size >= 3 and curve_db.min() <= lo:
            s = np.polyfit(sel * block_s, curve_db[sel], 1)[0]
            if s < 0:
                return float(-60.0 / s)
    return float("nan")


def tail_slopes(ir: IRView, tmax_s: float = 2.4) -> dict[str, float]:
    """Decay of the smooth tail from the FORWARD energy envelope (20 ms blocks, dB): a straight-line fit between 40 ms and the
    point where the band's energy reaches its floor (or the end of the response), per band and full-band. Backward
    integration (Schroeder) of a response that is cut off before the tail has died is biased to short decay times; a
    line through the part we can see is not. Returns rt60_* (s, from the slope), len_* (s, how long the tail stays
    above the floor), start_* (dB re the direct block, mean of 40-100 ms)."""
    from scipy.signal import butter, sosfiltfilt

    out: dict[str, float] = {}
    nan = float("nan")
    h = ir.h[ir.d - int(0.002 * SR): ir.d + int(tmax_s * SR)]
    blk = int(0.02 * SR)
    ref = float(np.sum(ir.h[ir.d - int(0.002 * SR): ir.d + int(0.010 * SR)] ** 2)) + 1e-18
    for name, (lo, hi) in (("full", (60.0, 12000.0)), *((f"b{i}", b) for i, b in enumerate(BANDS_HZ))):
        for k in ("rt60_", "len_", "start_"):
            out[k + name] = nan
        sos = butter(4, [lo / (SR / 2), min(hi, 20000.0) / (SR / 2)], btype="band", output="sos")
        hb = sosfiltfilt(sos, h)
        nb = hb.size // blk
        if nb < 8:
            continue
        e = db10((hb[: nb * blk] ** 2).reshape(nb, blk).sum(1) / ref + 1e-18)
        t = (np.arange(nb) + 0.5) * 0.02 - 0.002
        i0 = int(0.04 / 0.02)
        floor = float(np.percentile(e[i0:], 10))
        sm = 10 * np.log10(uniform_filter1d(10 ** (e / 10), 3, mode="nearest") + 1e-18)
        top = float(np.max(sm[i0: i0 + 8]))
        below = np.flatnonzero(sm[i0 + 1:] < max(floor + 4.0, top - 55.0))
        i1 = i0 + 1 + (int(below[0]) if below.size else nb - i0 - 1)
        out["start_" + name] = float(np.mean(e[i0: i0 + 3]))
        out["len_" + name] = float(t[min(i1, nb - 1)])
        if i1 - i0 >= 4:
            slope = float(np.polyfit(t[i0:i1], e[i0:i1], 1)[0])
            out["rt60_" + name] = float(min(-60.0 / slope, 20.0)) if slope < -0.5 else 20.0
    return out


PROBE_KEYS = (
    "on1", "on_rise_db", "on_level_db", "on_n", "on_fb_db", "t1", "dt_min", "dt_med", "t_strong", "n_echo", "lev1_db", "lev_slope_db", "lev21_db", "e1_lo_db", "e1_mid_db", "e1_hi_db",
    "late_db", "span_s", "rt60_full", "rt60_b0", "rt60_b1", "rt60_b2", "len_full", "len_b0", "len_b1", "len_b2",
    "start_full", "start_b0", "start_b1", "start_b2", "late_b0_db", "late_b1_db", "late_b2_db",
    "edc100", "edc300", "edc1000", "predelay_ms", "conc", "line_resid_db", "comb",
)


def probe_features(ir: IRView, shape: dict | None = None, pattern: dict | None = None) -> dict[str, float]:
    """Everything the per-model seed regressors read, one flat dict (NaN where it does not exist)."""
    nan = float("nan")
    shape = shape or shape_features(ir)
    pat = pattern or echo_pattern(ir)
    f: dict[str, float] = {k: nan for k in PROBE_KEYS}
    f.update({k: shape.get(k, nan) for k in ("late_db", "span_s", "conc", "line_resid_db", "comb")})
    ts, ls = pat["t"], pat["level_db"]
    f["n_echo"] = float(len(ts))
    if ts:
        top = max(ls)
        first = next(i for i, l in enumerate(ls) if l >= top - 12.0)
        f["t1"] = float(ts[first])
        f["t_strong"] = float(ts[int(np.argmax(ls))])
        gaps = [ts[first]] + [b - a for a, b in zip(ts[first:], ts[first + 1:], strict=False)]
        gaps = [g for g in gaps if g >= 0.03]
        f["dt_min"] = float(min(gaps)) if gaps else nan
        f["dt_med"] = float(np.median(gaps)) if gaps else nan
        f["lev1_db"] = float(ls[first])
        if len(ts) >= 2:
            k = np.arange(len(ls[first:]))
            f["lev_slope_db"] = float(np.polyfit(k, ls[first:], 1)[0]) if len(k) >= 2 else nan
            f["lev21_db"] = float(ls[first + 1] - ls[first]) if first + 1 < len(ls) else nan
        # tone of echo 1 against the direct sound, per band
        c = ir.d + round(ts[first] * SR)
        w = int(0.012 * SR)
        dir_b = _band_energy_db(ir.h[ir.d - int(0.002 * SR): ir.d + w])
        e1_b = _band_energy_db(ir.h[max(c - int(0.004 * SR), 0): c + w])
        f["e1_lo_db"], f["e1_mid_db"], f["e1_hi_db"] = (float(v) for v in (e1_b - dir_b))
    on = onsets(ir)
    f["on_n"] = float(len(on["t"]))
    if on["t"]:
        top = max(on["rise_db"])
        j = next(i for i, r in enumerate(on["rise_db"]) if r >= top - 6.0)
        f["on1"], f["on_rise_db"], f["on_level_db"] = float(on["t"][j]), float(on["rise_db"][j]), float(on["level_db"][j])
        near2 = [i for i, t in enumerate(on["t"]) if abs(t - 2 * f["on1"]) <= 0.08 * f["on1"] + 0.01]
        if near2:
            f["on_fb_db"] = float(on["level_db"][near2[0]] - on["level_db"][j])
    dc = decay_curve(ir)
    if dc:
        ts_ = tail_slopes(ir)
        for k_, v_ in ts_.items():
            if k_ in f:
                f[k_] = v_
        if "full" in dc:
            cv = dc["full"]
            for key, t in (("edc100", 0.1), ("edc300", 0.3), ("edc1000", 1.0)):
                i = int(t / 0.02)
                f[key] = float(cv[i]) if i < cv.size else nan
        dc.get("full_e")
        for name in ("b0", "b1", "b2"):
            if name + "_e" in dc and "full_e" in dc:
                f["late_" + name + "_db"] = float(db10(dc[name + "_e"].sum() / (dc["full_e"].sum() + 1e-18)))
    # pre-delay: onset of the late energy re the direct sound (ms), change point of the 2 ms envelope
    e = ir.env_db
    i0 = int(0.004 / BLK_S)
    lim = int(0.2 / BLK_S)
    if e.size > lim:
        c = np.concatenate([[0.0], np.cumsum(np.maximum(e[:lim], ir.floor_db))])
        best, arg = -np.inf, i0
        r = int(0.02 / BLK_S)
        for tau in range(i0 + 2, lim - r):
            left = (c[tau] - c[i0]) / (tau - i0)
            right = (c[tau + r] - c[tau]) / r
            if right - left > best:
                best, arg = right - left, tau
        f["predelay_ms"] = float(arg * BLK_S * 1000)
    return {k: f.get(k, nan) for k in PROBE_KEYS}
