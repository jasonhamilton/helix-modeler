"""Reverb measurement (plan Step 6, reverb; reserved term name `reverb_tail`): early decay time (EDT)
and reverberation time (RT60) per octave band, the pre-delay, the long-term spectrum of the tail, and
the tail level relative to the playing (a proxy for the mix).

    p = reverb_profile(render, ref=block_input)   # ReverbProfile
    d = compare(stomp_profile, native_profile)

Decay times use the interrupted-signal method (ISO 3382-1 allows interrupted noise): the input stops
at `t_stop`, the render's free decay from 10 ms later is band-filtered per octave (125 Hz to 8 kHz)
and backward-integrated (Schroeder). A noise floor, if the decay reaches one, is subtracted and the
integral truncated 5 dB above it, with the usual correction for the energy cut off. EDT is the slope
from 0 to -10 dB, scaled to 60 dB; RT60 is T30 (-5 to -35 dB) when the curve reaches -35 dB, else
T20 (-5 to -25 dB), else not reported. The free decay of a music input is not an impulse response,
so absolute values differ from a hall measurement; two engines given the same input are compared
like for like.

Pre-delay needs the block's input (`ref`, the same engine's render with the block off): the impulse
response is deconvolved and the wet onset found as the change point of its energy envelope between
2 ms and 200 ms after the direct sound. This is the delay to the first strong reflections (spring
models have one even at Predelay 0), measured to about 1 ms.

Nothing here depends on where a modulated reverb's LFO is, or on the absolute level.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.signal import fftconvolve

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import (
    OCTAVE_CENTRES,
    SR,
    band_levels_db,
    bandpass,
    db10,
    impulse_response,
    smooth_power,
    third_octave_centres,
)

NAME = "reverb_tail"
TAIL_START_S = 0.010       # skip the end of the dry sound and the cab's ring
TAIL_BANDS = third_octave_centres(100.0, 10_000.0)


@dataclass
class ReverbProfile:
    t_stop_s: float
    bands_hz: list[float] = field(default_factory=list)
    edt_s: list[float] = field(default_factory=list)
    rt60_s: list[float] = field(default_factory=list)
    rt60_method: list[str] = field(default_factory=list)   # "T30", "T20" or "none"
    decay_range_db: list[float] = field(default_factory=list)
    predelay_ms: float | None = None
    tail_bands_hz: list[float] = field(default_factory=list)
    tail_bands_db: list[float] = field(default_factory=list)   # mean removed: the shape only
    tail_level_db: float = float("nan")                        # first 50 ms of tail re the playing

    def to_dict(self) -> dict:
        return asdict(self)


def find_stop(ref: np.ndarray, *, sr: int = SR, rel_db: float = -50.0) -> float:
    """Last time the block's input is within `rel_db` of its loudest 10 ms (the end of the playing)."""
    p = smooth_power(as_mono(ref, "ref"), int(0.01 * sr))
    idx = np.flatnonzero(p >= p.max() * 10 ** (rel_db / 10))
    return float(idx[-1] / sr)


def schroeder_db(e: np.ndarray, *, sr: int = SR, block_s: float = 0.01) -> tuple[np.ndarray, float]:
    """Backward-integrated decay curve in dB (0 at the start) of an energy sequence `e`, with noise
    subtraction and truncation if a floor is reached. Returns (curve, dynamic range dB)."""
    b = max(int(block_s * sr), 1)
    nb = e.size // b
    if nb < 4:
        raise ValueError("tail too short")
    eb = e[: nb * b].reshape(nb, b).mean(axis=1)
    lev = db10(eb)
    last = max(nb // 10, 2)
    noise = float(eb[-last:].mean())
    late = lev[-max(nb // 4, 3):]
    flat = np.polyfit(np.arange(late.size) * block_s, late, 1)[0] > -10.0   # dB/s: no longer decaying
    tc = nb
    rng = float(lev[: max(nb // 50, 1)].mean() - lev.min())
    if flat and lev[0] - db10(noise) > 10.0:
        above = np.flatnonzero(lev <= db10(noise) + 5.0)
        tc = int(above[0]) if above.size else nb
        rng = float(lev[0] - db10(noise))
    else:
        noise = 0.0
    x = np.maximum(eb[:tc] - noise, 0.0)
    E = np.cumsum(x[::-1])[::-1]
    # correction for the energy beyond the truncation point, from the slope of the last 30 % above it
    k0 = int(0.7 * tc)
    if tc - k0 >= 3 and x[k0:tc].min() > 0:
        s = np.polyfit(np.arange(k0, tc) * block_s, db10(x[k0:tc]), 1)[0]
        if s < -1.0:
            tau = 10.0 / (np.log(10) * -s)   # energy time constant, s
            E = E + x[tc - 1] * tau / block_s
    curve = db10(E / max(E[0], EPS))
    return curve, rng


def _slope_time(curve: np.ndarray, hi_db: float, lo_db: float, block_s: float) -> float:
    sel = (curve <= hi_db) & (curve >= lo_db)
    if curve.min() > lo_db or sel.sum() < 3:
        return float("nan")
    idx = np.flatnonzero(sel)
    idx = idx[idx <= np.flatnonzero(curve < lo_db)[0]] if (curve < lo_db).any() else idx
    s = np.polyfit(idx * block_s, curve[idx], 1)[0]
    return float(-60.0 / s) if s < 0 else float("nan")


def decay_times(tail: np.ndarray, *, sr: int = SR, centres=OCTAVE_CENTRES, block_s: float = 0.01) -> dict:
    out = {"bands_hz": [], "edt_s": [], "rt60_s": [], "rt60_method": [], "decay_range_db": []}
    for c in centres:
        bp = bandpass(tail, c, sr=sr)
        curve, rng = schroeder_db(bp ** 2, sr=sr, block_s=block_s)
        edt = _slope_time(curve, 0.0, -10.0, block_s)
        t30 = _slope_time(curve, -5.0, -35.0, block_s)
        t20 = _slope_time(curve, -5.0, -25.0, block_s)
        if np.isfinite(t30):
            rt, m = t30, "T30"
        elif np.isfinite(t20):
            rt, m = t20, "T20"
        else:
            rt, m = float("nan"), "none"
        out["bands_hz"].append(float(c))
        out["edt_s"].append(edt)
        out["rt60_s"].append(rt)
        out["rt60_method"].append(m)
        out["decay_range_db"].append(float(rng))
    return out


def predelay_ms(y: np.ndarray, ref: np.ndarray, *, sr: int = SR, search_ms: tuple[float, float] = (2.0, 200.0),
                right_ms: float = 20.0) -> float:
    """Wet onset re the direct sound, from the deconvolved impulse response (change point of the
    1 ms energy envelope in dB: the split that maximises mean(right 20 ms) - mean(left))."""
    pre = int(0.01 * sr)
    h = impulse_response(ref, y, n_out=int(0.4 * sr), pre=pre, sr=sr)
    env = db10(smooth_power(h, int(0.001 * sr)))
    d = pre + int(np.argmax(env[pre - int(0.002 * sr): pre + int(0.005 * sr)])) - int(0.002 * sr)
    a, b = d + int(search_ms[0] * sr / 1000), d + int(search_ms[1] * sr / 1000)
    r = int(right_ms * sr / 1000)
    c = np.concatenate([[0.0], np.cumsum(env)])
    best, arg = -np.inf, a
    for tau in range(a + int(0.001 * sr), b, max(sr // 4000, 1)):
        left = (c[tau] - c[a]) / (tau - a)
        right = (c[tau + r] - c[tau]) / r
        if right - left > best:
            best, arg = right - left, tau
    return float(1000 * (arg - d) / sr)


def reverb_profile(y: np.ndarray, ref: np.ndarray | None = None, *, t_stop: float | None = None, sr: int = SR) -> ReverbProfile:
    y = as_mono(y, "render")
    if t_stop is None:
        if ref is None:
            raise ValueError("give t_stop or ref (the block's input) to find where the playing stops")
        t_stop = find_stop(ref, sr=sr)
    s0 = int((t_stop + TAIL_START_S) * sr)
    if s0 >= y.size - int(0.1 * sr):
        raise ValueError("less than 100 ms of tail after the playing stops")
    tail = y[s0:]
    prof = ReverbProfile(t_stop_s=float(t_stop))
    dt = decay_times(tail, sr=sr)
    prof.bands_hz, prof.edt_s, prof.rt60_s = dt["bands_hz"], dt["edt_s"], dt["rt60_s"]
    prof.rt60_method, prof.decay_range_db = dt["rt60_method"], dt["decay_range_db"]
    lv = band_levels_db(tail[: int(1.0 * sr)], TAIL_BANDS, sr=sr)
    prof.tail_bands_hz = [float(c) for c in TAIL_BANDS]
    prof.tail_bands_db = (lv - lv.mean()).tolist()
    body = y[: int(t_stop * sr)]
    pb = smooth_power(body, int(0.05 * sr))
    act = pb >= pb.max() * 10 ** (-35 / 10)
    prof.tail_level_db = float(db10(np.mean(tail[: int(0.05 * sr)] ** 2)) - db10(np.mean(body[act] ** 2) + EPS))
    if ref is not None:
        prof.predelay_ms = predelay_ms(y, as_mono(ref, "ref"), sr=sr)
    return prof


def synthetic_reverb(ref: np.ndarray, rt60_s: list[float], *, seed: int, predelay_s: float = 0.02,
                     wet_gain: float = 0.3, sr: int = SR) -> np.ndarray:
    """`ref` plus `ref` convolved with an exponentially decaying noise impulse response whose octave
    bands decay with `rt60_s` (one per OCTAVE_CENTRES). Two seeds give two reverbs that differ only by
    chance: the reference for what the measurement can resolve on this input."""
    rng = np.random.default_rng(seed)
    T = [t if np.isfinite(t) and t > 0 else 1.0 for t in rt60_s]
    n = int(min(max(T) * 1.2, 4.0) * sr)
    tt = np.arange(n) / sr
    h = np.zeros(n)
    for c, t in zip(OCTAVE_CENTRES, T, strict=True):
        h += bandpass(rng.standard_normal(n), c, sr=sr) * np.exp(-3 * np.log(10) * tt / t)
    h = np.concatenate([np.zeros(int(predelay_s * sr)), h])
    wet = fftconvolve(ref, h)[: ref.size]
    wet *= wet_gain * np.sqrt(np.mean(ref ** 2) / (np.mean(wet ** 2) + EPS))
    return ref + wet


def estimator_floor(ref: np.ndarray, rt60_s: list[float], *, t_stop: float, pairs: int = 5, sr: int = SR) -> dict:
    """Largest differences (over `pairs` pairs of seeds) between two statistically identical synthetic
    reverbs driven by the same input `ref`: per-band EDT and RT60 in %, tail spectrum in dB. A real
    difference smaller than this cannot be told from the chance in one free decay."""
    edt, rt, spec, lvl, pre = [], [], [], [], []
    for k in range(pairs):
        a = reverb_profile(synthetic_reverb(ref, rt60_s, seed=2 * k, sr=sr), ref, t_stop=t_stop, sr=sr)
        b = reverb_profile(synthetic_reverb(ref, rt60_s, seed=2 * k + 1, sr=sr), ref, t_stop=t_stop, sr=sr)
        c = compare(a, b)
        edt.append(np.abs(c["edt_diff_pct"]))
        rt.append(np.abs(c["rt60_diff_pct"]))
        spec.append(c["tail_spectrum_max_abs_db"])
        lvl.append(abs(c["tail_level_diff_db"]))
        pre.append(abs(c["predelay_diff_ms"]))
    return {"edt_pct": np.nanmax(edt, axis=0).tolist(), "rt60_pct": np.nanmax(rt, axis=0).tolist(),
            "tail_spectrum_db": float(np.max(spec)), "tail_level_db": float(np.max(lvl)),
            "predelay_ms": float(np.max(pre)), "pairs": pairs}


def _rel_pct(a: float, b: float) -> float:
    return float(100 * (b - a) / a) if np.isfinite(a) and np.isfinite(b) and a > 0 else float("nan")


def compare(ref: ReverbProfile, other: ReverbProfile) -> dict:
    """`other` minus `ref`: per-band EDT and RT60 differences in %, pre-delay in ms, tail spectrum
    shape (largest and RMS 1/3-octave difference after removing the mean), tail level in dB."""
    edt = [_rel_pct(a, b) for a, b in zip(ref.edt_s, other.edt_s, strict=True)]
    rt = [_rel_pct(a, b) for a, b in zip(ref.rt60_s, other.rt60_s, strict=True)]
    sd = np.asarray(other.tail_bands_db) - np.asarray(ref.tail_bands_db)

    def fin(v: list[float]) -> list[float]:
        return [x for x in v if np.isfinite(x)]

    return {
        "bands_hz": ref.bands_hz, "edt_diff_pct": edt, "rt60_diff_pct": rt,
        "max_abs_edt_diff_pct": float(max(map(abs, fin(edt)))) if fin(edt) else float("nan"),
        "max_abs_rt60_diff_pct": float(max(map(abs, fin(rt)))) if fin(rt) else float("nan"),
        "predelay_diff_ms": (other.predelay_ms - ref.predelay_ms) if ref.predelay_ms is not None and other.predelay_ms is not None else None,
        "tail_spectrum_max_abs_db": float(np.max(np.abs(sd))),
        "tail_spectrum_rms_db": float(np.sqrt(np.mean(sd ** 2))),
        "tail_spectrum_diff_db": sd.tolist(),
        "tail_level_diff_db": other.tail_level_db - ref.tail_level_db,
    }
