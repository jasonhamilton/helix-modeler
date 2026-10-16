"""Modulation measured on a sustained sine (T066b, benchmark): LFO rate, level swing and pitch swing of
the one tone, independent of where the LFO is in its cycle.

    p = sine_profile(render, ref, f0)       # SineModProfile
    d = compare(stomp_profile, native_profile)

`ref` is the same engine's render with the modulation block off (the block's input), `f0` the sine's
frequency. Both signals are mixed down by exp(-j 2 pi f0 t) and low-passed (60 Hz), so only the
neighbourhood of the tone is kept; the complex ratio z(t) = y / x is then the effect's transfer at f0
over time. From it:

- **Level swing**: 20 log10 |z|, its 2nd to 98th percentile range in dB (a tremolo swings it, a phaser or
  flanger sweeps a notch through f0, a chorus beats the wet copy against the dry one).
- **Pitch swing**: the instantaneous frequency of z (derivative of its unwrapped phase) re f0, in cents (samples within a null of z, where the phase jumps, are left out);
  the peak deviation is half the 2nd to 98th percentile range.
- **Rate**: the period of the unwrapped phase of z (autocorrelation, shape-free; a comb filter crosses
  the tone many times per LFO cycle, so the level alone would give a multiple of the rate); the level
  decides when the phase does not repeat (tremolo, flanger). `confidence` is the autocorrelation at the
  period (1: the trajectory repeats exactly); `cycles` the LFO cycles inside the analysed span.
- **Mean level**: mean of the level trajectory, dB (the effect's gain at f0 with the LFO averaged out).

Percentile ranges rather than sinusoid fits, so the numbers do not depend on the trajectory's shape or on
the LFO's phase; two renders of the same file with free-running LFOs give the same numbers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import butter, filtfilt

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import SR, db20

NAME = "mod_sine"
FS_TRAJ = 400.0              # trajectory sample rate, Hz
LOWPASS_HZ = 60.0
SKIP_START_S = 0.6           # settling of the amp, cab and the effect's own start-up
SKIP_END_S = 0.1
RATE_RANGE = (0.1, 12.0)
PEAK_MIN = 0.5               # an autocorrelation peak must reach this to be a period candidate
PHASE_CONF = 0.5             # the phase trajectory sets the rate when it repeats at least this well
NULL_REL = 0.25              # samples where |z| is under this share of its median are skipped for pitch (phase jumps)


@dataclass
class SineModProfile:
    f0_hz: float
    rate_hz: float
    confidence: float
    cycles: float
    level_swing_db: float
    pitch_swing_cents: float        # peak deviation
    mean_level_db: float
    span_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def _demod(x: np.ndarray, f0: float, sr: int) -> np.ndarray:
    t = np.arange(x.size) / sr
    z = x * np.exp(-2j * np.pi * f0 * t)
    b, a = butter(4, LOWPASS_HZ / (sr / 2))
    z = filtfilt(b, a, z.real) + 1j * filtfilt(b, a, z.imag)
    step = round(sr / FS_TRAJ)
    return z[::step]


def _rate(trajs: list[np.ndarray]) -> tuple[float, float]:
    """LFO rate (Hz) and confidence from the autocorrelation of the trajectories (each de-drifted and
    scaled to unit variance, autocorrelations averaged). Works whatever the trajectory's shape (a notch
    crossing the tone gives sharp, non-sinusoidal dips). Candidates are the autocorrelation peaks, shortest
    lag first, between 1/12 s and 0.75 of the span, that reach PEAK_MIN; the first whose
    whole multiples (2, 3 times the lag, while inside the span) also peak at 0.7 of its height is the period
    (a true period repeats; the jitter in a spiky trajectory does not). The period is then refined from the
    largest such multiple. Confidence is the autocorrelation at the period (1: the trajectory repeats exactly)."""
    n = min(len(v) for v in trajs)
    t = np.arange(n) / FS_TRAJ
    acf = np.zeros(n)
    for v in trajs:
        v = np.asarray(v[:n], np.float64)
        v = v - np.polyval(np.polyfit(t, v, 1), t)
        v = v / (np.std(v) + EPS)
        f = np.fft.rfft(v, 2 * n)
        a = np.fft.irfft(f * np.conj(f))[:n] / (n - np.arange(n)) * n
        acf += a / a[0]
    acf /= len(trajs)
    lo, hi = int(FS_TRAJ / RATE_RANGE[1]), int(0.75 * n)
    seg = acf[lo:hi]
    pk = np.flatnonzero((seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:])) + 1
    if pk.size == 0 or seg[pk].max() <= 0:
        return float("nan"), 0.0

    def peak_at(k: int, half: int) -> tuple[float, float]:
        i0, i1 = max(k - half, 1), min(k + half + 1, n - 1)
        j = int(np.argmax(acf[i0:i1])) + i0
        y0, y1, y2 = acf[j - 1], acf[j], acf[min(j + 1, n - 1)]
        den = y0 - 2 * y1 + y2
        return j + (0.5 * (y0 - y2) / den if den != 0 else 0.0), float(y1)

    cands = [int(k) + lo for k in pk if seg[k] >= PEAK_MIN] or [int(k) + lo for k in pk]
    chosen = None
    for c in cands:
        T, val = peak_at(c, max(c // 4, 2))
        ok, mult = True, [(1, T)]
        for m in (2, 3):
            if m * T >= 0.9 * n:
                break
            Tm, vm = peak_at(round(m * T), max(int(T // 4), 2))
            if vm < 0.7 * val or abs(Tm - m * T) > 0.1 * T:
                ok = False
                break
            mult.append((m, Tm))
        if ok:
            chosen = (val, mult)
            break
    if chosen is None:
        c = max(cands, key=lambda k: acf[k])
        T, val = peak_at(c, max(c // 4, 2))
        return float(FS_TRAJ / T), float(max(min(val, 1.0) * 0.5, 0.0))      # nothing repeats cleanly: half confidence
    val, mult = chosen
    m, Tm = mult[-1]
    T0 = Tm / m
    # a trace dominated by a harmonic repeats at a fraction of the LFO period but matches itself clearly better
    # at the whole period: take a multiple of the lag whose peak is clearly higher
    best_m, best_T, best_v = 1, T0, val
    for k in range(2, 9):
        if k * T0 > 0.5 * n:        # beyond half the span the unbiased autocorrelation is too noisy
            break
        Tk, vk = peak_at(round(k * T0), max(int(T0 // 4), 2))
        if vk > best_v + 0.15 and abs(Tk - k * T0) <= 0.1 * T0:
            best_m, best_T, best_v = k, Tk, vk
    if best_m > 1:
        return float(FS_TRAJ / best_T), float(max(min(best_v, 1.0), 0.0))
    return float(FS_TRAJ * m / Tm), float(max(min(val, 1.0), 0.0))


def sine_profile(y: np.ndarray, ref: np.ndarray, f0: float, *, sr: int = SR,
                 span_s: tuple[float, float] | None = None) -> SineModProfile:
    """`span_s`: analyse only this part of the clip (a note that sustains for 3.8 s, say); default: the whole
    clip less the first SKIP_START_S and the last SKIP_END_S seconds."""
    y, x = as_mono(y, "render"), as_mono(ref, "ref")
    m = min(y.size, x.size)
    zy, zx = _demod(y[:m], f0, sr), _demod(x[:m], f0, sr)
    i0, i1 = int(SKIP_START_S * FS_TRAJ), zy.size - int(SKIP_END_S * FS_TRAJ)
    if span_s is not None:
        i0, i1 = int(span_s[0] * FS_TRAJ), min(int(span_s[1] * FS_TRAJ), zy.size)
    zy, zx = zy[i0:i1], zx[i0:i1]
    ok = np.abs(zx) > np.abs(zx).max() * 0.05
    if ok.sum() < zy.size * 0.8:
        raise ValueError("the reference sine is not sustained over the analysed span")
    z = zy / (zx + EPS)
    level = db20(z)
    phase = np.unwrap(np.angle(z))
    dphi = np.gradient(phase) * FS_TRAJ / (2 * np.pi)      # Hz
    cents = 1200 * np.log2(np.maximum(1 + dphi / f0, 1e-6))
    span = zy.size / FS_TRAJ
    # Rate: the unwrapped phase repeats once per LFO cycle however many nulls the level passes (a comb
    # filter at 1 kHz crosses the tone many times per cycle); a tremolo has no phase swing, so its level decides.
    rate, conf = _rate([phase])
    if not conf >= PHASE_CONF:
        r2 = _rate([level])
        if r2[1] > conf:
            rate, conf = r2
    keep = np.abs(z) >= NULL_REL * np.median(np.abs(z))
    cents_k = cents[keep] if keep.sum() > 20 else cents
    p = lambda v, q: float(np.percentile(v, q))         
    return SineModProfile(
        f0_hz=float(f0), rate_hz=rate, confidence=conf, cycles=float(span * rate),
        level_swing_db=p(level, 98) - p(level, 2), pitch_swing_cents=(p(cents_k, 98) - p(cents_k, 2)) / 2,
        mean_level_db=float(np.mean(level)), span_s=float(span))


def compare(ref: SineModProfile, other: SineModProfile) -> dict:
    """`other` minus `ref`."""
    return {
        "rate_diff_pct": 100 * (other.rate_hz - ref.rate_hz) / ref.rate_hz if ref.rate_hz > 0 else float("nan"),
        "level_swing_diff_db": other.level_swing_db - ref.level_swing_db,
        "pitch_swing_diff_cents": other.pitch_swing_cents - ref.pitch_swing_cents,
        "pitch_swing_diff_pct": (100 * (other.pitch_swing_cents - ref.pitch_swing_cents) / ref.pitch_swing_cents
                                 if ref.pitch_swing_cents > 0 else float("nan")),
        "mean_level_diff_db": other.mean_level_db - ref.mean_level_db,
        "rate_ref_hz": ref.rate_hz, "rate_other_hz": other.rate_hz,
        "pitch_swing_ref_cents": ref.pitch_swing_cents, "level_swing_ref_db": ref.level_swing_db,
    }
