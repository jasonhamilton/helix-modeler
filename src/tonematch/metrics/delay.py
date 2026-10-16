"""Delay measurement (plan Step 6, delay; reserved term name `delay_echo`): per-echo time and level,
the delay time, and the feedback decay slope, measured on one render of a short burst (or any
impulse-like input with silence after it).

    p = echo_profile(render)                  # EchoProfile
    d = compare(stomp_profile, native_profile)

Method. The direct sound is the earliest peak within 6 dB of the loudest; its span (down to -30 dB
of its peak, capped at 60 ms) is the template. A matched filter (the render correlated with the
template) peaks at every repeat of that template; the Hilbert envelope of the matched filter gives
echo times to a fraction of a sample, so a filtered (darker) echo is still timed at its centre of
energy rather than its first edge. Echo levels are energies in a template-length window at each
echo, in dB relative to the direct sound, so they ignore the absolute level. Echoes are indexed by
the nearest multiple of the first interval; the profile stops at the first missing index.

Time errors between two profiles are reported both in ms and as a percentage of the echo's own
time (echo k is at about k x the delay time), so a constant clock difference shows as a constant
percentage and a growing ms error. Neither the level nor the phase of the input matters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from scipy import signal

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import SR, db10, smooth_power

NAME = "delay_echo"


@dataclass
class EchoProfile:
    direct_s: float                       # time of the direct sound in the render
    times_s: list[float] = field(default_factory=list)    # echo k time re the direct sound, k = 1..n
    levels_db: list[float] = field(default_factory=list)  # echo k energy re the direct sound, dB
    delay_s: float = float("nan")          # least-squares slope of time vs index (through 0)
    slope_db_per_echo: float = float("nan")  # level fit over echoes 1..n (the feedback decay)
    feedback_gain: float = float("nan")    # 10 ** (slope / 20): the loop gain the slope implies
    template_ms: float = float("nan")

    def to_dict(self) -> dict:
        return asdict(self)


def _peak_interp(e: np.ndarray, k: int) -> float:
    if 0 < k < e.size - 1:
        a, b, c = e[k - 1], e[k], e[k + 1]
        den = a - 2 * b + c
        if den != 0:
            return k + 0.5 * (a - c) / den
    return float(k)


def echo_profile(y: np.ndarray, *, sr: int = SR, max_echoes: int = 10, floor_db: float = -50.0,
                 min_delay_s: float = 0.02, max_template_s: float = 0.06) -> EchoProfile:
    y = as_mono(y, "render")
    p = smooth_power(y, int(0.002 * sr))
    pk_all = float(p.max())
    if pk_all <= 0:
        raise ValueError("silent render")
    loud = np.flatnonzero(p >= pk_all * 10 ** (-6 / 10))
    first = int(loud[0])
    pk = first + int(np.argmax(p[first:first + int(max_template_s * sr)]))
    thr = p[pk] * 1e-3
    on = pk
    while on > 0 and p[on - 1] >= thr:
        on -= 1
    off = pk
    lim = min(p.size, pk + int(max_template_s * sr))
    while off < lim - 1 and p[off + 1] >= thr:
        off += 1
    w = max(int(0.002 * sr), off - on + int(0.003 * sr))
    w = min(w, int(max_template_s * sr))
    tpl = y[on:on + w]
    n = 1 << int(np.ceil(np.log2(y.size + w)))
    c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(tpl, n)), n)[: y.size]
    env = np.abs(signal.hilbert(c))
    d_idx = on + int(np.argmax(env[on: on + w]))
    d_pos = _peak_interp(env, d_idx)
    ref = env[d_idx]
    e_dir = float(np.sum(tpl ** 2)) + EPS

    guard = max(w, int(min_delay_s * sr))
    cand = env.copy()
    cand[: d_idx + guard] = 0.0
    found: list[float] = []
    for _ in range(4 * max_echoes):
        k = int(np.argmax(cand))
        if cand[k] < ref * 10 ** (floor_db / 20):
            break
        found.append(_peak_interp(env, k))
        cand[max(0, k - guard): k + guard] = 0.0
    found.sort()
    out = EchoProfile(direct_s=d_pos / sr, template_ms=1000 * w / sr)
    if not found:
        return out
    t1 = (found[0] - d_pos) / sr
    by_index: dict[int, tuple[float, float]] = {}
    for pos in found:
        t = (pos - d_pos) / sr
        idx = round(t / t1)
        if idx < 1 or abs(t - idx * t1) > 0.15 * t1:
            continue
        s = round(on + (pos - d_pos))
        e = float(np.sum(y[max(s, 0): s + w] ** 2))
        if idx not in by_index or e > by_index[idx][1]:
            by_index[idx] = (t, e)
    k = 1
    while k in by_index and k <= max_echoes:
        t, e = by_index[k]
        out.times_s.append(float(t))
        out.levels_db.append(float(db10(e / e_dir)))
        k += 1
    ks = np.arange(1, len(out.times_s) + 1, dtype=float)
    if ks.size:
        out.delay_s = float(np.dot(ks, out.times_s) / np.dot(ks, ks))
    if ks.size >= 2:
        out.slope_db_per_echo = float(np.polyfit(ks, out.levels_db, 1)[0])
        out.feedback_gain = float(10 ** (out.slope_db_per_echo / 20))
    return out


def compare(ref: EchoProfile, other: EchoProfile) -> dict:
    """`other` minus `ref`, echo by echo (matched by index). Time errors in ms and in % of the echo's
    time; the per-echo drift (ms per echo) is the slope of the ms error against the echo index."""
    m = min(len(ref.times_s), len(other.times_s))
    terr_ms = [1000 * (other.times_s[i] - ref.times_s[i]) for i in range(m)]
    terr_pct = [100 * (other.times_s[i] - ref.times_s[i]) / ref.times_s[i] for i in range(m)]
    ldiff = [other.levels_db[i] - ref.levels_db[i] for i in range(m)]
    ks = np.arange(1, m + 1, dtype=float)
    drift = float(np.dot(ks, terr_ms) / np.dot(ks, ks)) if m else float("nan")
    out = {
        "n_echoes_ref": len(ref.times_s), "n_echoes_other": len(other.times_s), "n_compared": m,
        "time_err_ms": terr_ms, "time_err_pct": terr_pct, "level_diff_db": ldiff,
        "drift_ms_per_echo": drift,
        "delay_err_ms": 1000 * (other.delay_s - ref.delay_s),
        "delay_err_pct": 100 * (other.delay_s - ref.delay_s) / ref.delay_s if np.isfinite(ref.delay_s) else float("nan"),
        "slope_diff_db_per_echo": other.slope_db_per_echo - ref.slope_db_per_echo,
        "max_abs_level_diff_db": float(max(np.abs(ldiff))) if m else float("nan"),
        "max_abs_time_err_ms": float(max(np.abs(terr_ms))) if m else float("nan"),
    }
    # cumulative timing error at the last echo still within 30 dB of the first (the audible train)
    aud = [i for i in range(m) if ref.levels_db[i] >= ref.levels_db[0] - 30.0] if m else []
    out["audible_echoes"] = len(aud)
    out["cum_time_err_ms_audible"] = float(abs(terr_ms[aud[-1]])) if aud else float("nan")
    return out
