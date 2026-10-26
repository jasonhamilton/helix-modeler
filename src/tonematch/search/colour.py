"""Colour pilot (T228): the light colouring stage after a drive block, set by direct measurement of the separated stem.

Chain: drive -> (delay and/or reverb) -> EQ. Nothing here ranks candidates by a sound-comparison score (the
T221 check: no score ranks delay/reverb or sets knobs under a mismatched dry). Three measurements, each with the
limits found on controls (the real stem with a known effect rendered onto it, docs/notes/colour-pilot-t228.md):

  delay   `delay_evidence`: the stem's onset envelope is deconvolved on the reference render's onset envelope
          (non-negative taps, 5 ms resolution). An echo is a tap at lag T >= 80 ms. Three gates, all needed:
          (A) alignment, the taps explain at least 30% of the stem's onset-envelope variance and the zero-lag tap is at least
          0.15, i.e. the two are the same performance (unrelated pairs: explained variance <= 0.12; the same performance
          with a light echo added: >= 0.40);
          (B) the best late tap stands out of the late taps (robust z >= 8) and weighs >= 5% of the zero-lag tap;
          (C) the echo shows in the real gaps: where the rebuilt dry is silent for longer than T + 40 ms, the stem
          rises at T after the gap start. A song with no such gap cannot confirm, and is then not accepted.
  reverb  `gap_tail`: in the real note gaps (rebuilt dry silent >= 250 ms), a tail is a stem level that keeps
          falling at >= 10 dB/s after the first 60 ms and stays within 35 dB of the loud level. A flat floor is
          string ring-out or separation leakage, not reverb; a song without such gaps is "not measurable".
  EQ      `eq_gains_from_ltas`: the ratio of the stem's long-term spectrum to the reference render's, per
          graphic-EQ band, mean removed, limited to +-6 dB, bands under 100 Hz ignored (the stem's bass is unreliable).

Pure numpy/scipy; the engine, the catalog and the song files are the caller's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import nnls
from scipy.signal import stft

SR = 48_000
HOP = 240                              # 5 ms onset-envelope hop
# Mix caps for "light" colour, from impulse renders on Native (note, section 3): Transistor Tape Mix .45 leaves the first echo about
# -11 dB (energy) under the direct sound; Room Mix .50 leaves the tail about -17 dB under it.
LIGHT_MIX_CAP = {"delay": 0.45, "reverb": 0.50}
LIGHT_FEEDBACK_CAP = 0.45
EQ_LIMIT_DB = 6.0
EQ_IGNORE_BELOW_HZ = 100.0
GRAPHIC10_CENTRES = (31.25, 62.5, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
GRAPHIC10_KNOBS = ("31p25Hz", "62p5Hz", "125Hz", "250Hz", "500Hz", "1kHz", "2kHz", "4kHz", "8kHz", "16kHz")

# decision thresholds (calibrated on the controls of the note)
ALIGN_R2_MIN = 0.30
ALIGN_MAIN_MIN = 0.15                   # and the zero-lag tap itself must carry weight: sparse unrelated bursts can be explained by late taps alone
Z_MIN = 8.0
TAP_MIN_REL = 0.05
LATE_FROM_S = 0.08
GAP_MIN_S = 0.25
GAP_SLOPE_DB_S = -10.0
REAL_GAP_MAX_DB = -12.0               # a gap whose stem level 60 to 100 ms in is above this (re loud) is not a real gap
GAP_LEVEL_MAX_DB = -35.0               # the tail must stay above -35 dB re the stem's loud level at 60 to 100 ms


# ------------------------------------------------------------------------------------------ helpers

def loud_db(x: np.ndarray, frame: int = 480) -> float:
    n = len(x) // frame
    e = 10 * np.log10((np.asarray(x[: n * frame], np.float64).reshape(n, frame) ** 2).mean(1) + 1e-14)
    return float(np.percentile(e, 90))


def frame_db(x: np.ndarray, frame: int = 480) -> np.ndarray:
    n = len(x) // frame
    return 10 * np.log10((np.asarray(x[: n * frame], np.float64).reshape(n, frame) ** 2).mean(1) + 1e-14)


def silent_runs(dry: np.ndarray, min_s: float = GAP_MIN_S, below_db: float = 50.0) -> list[tuple[float, float]]:
    """(start_s, length_s) of the runs where the dry track is silent (more than `below_db` under its loud level) for
    at least `min_s`. Frames are 10 ms."""
    d = frame_db(dry)
    sil = d < loud_db(dry) - below_db
    runs, i = [], 0
    while i < len(sil):
        if sil[i]:
            j = i
            while j < len(sil) and sil[j]:
                j += 1
            if (j - i) * 0.01 >= min_s:
                runs.append((i * 0.01, (j - i) * 0.01))
            i = j
        else:
            i += 1
    return runs


# ------------------------------------------------------------------------------------------ delay

def onset_envelope(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """Positive spectral flux of 8 log-spaced bands (100 Hz to 8 kHz), 5 ms hop: a note-attack curve that does not
    depend on the waveform, the pitch or the distortion."""
    f, _t, z = stft(np.asarray(x, np.float64), sr, nperseg=1024, noverlap=1024 - HOP, boundary=None)
    m = np.abs(z)
    edges = np.geomspace(100, 8000, 9)
    bands = np.stack([m[(f >= edges[i]) & (f < edges[i + 1])].sum(0) for i in range(8)])
    lg = np.log1p(bands / (np.percentile(bands, 50, axis=1, keepdims=True) + 1e-9))
    return np.maximum(np.diff(lg, axis=1, prepend=lg[:, :1]), 0).sum(0)


def echo_deconv(o_stem: np.ndarray, o_ref: np.ndarray, max_lag_s: float = 1.0, pre_s: float = 0.04,
                lam: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    """Non-negative taps h (lag in s, weight) with o_stem ~ sum_k h_k o_ref(t - lag_k), ridge `lam`."""
    n = min(len(o_stem), len(o_ref))
    o_s, o_r = np.asarray(o_stem[:n], float), np.asarray(o_ref[:n], float)
    hop_s = HOP / SR
    k0, kmax = int(pre_s / hop_s), int(max_lag_s / hop_s)
    lags = np.arange(-k0, kmax)
    a = np.zeros((n, len(lags)))
    for j, lag in enumerate(lags):
        if lag >= 0:
            a[lag:, j] = o_r[: n - lag]
        else:
            a[: n + lag, j] = o_r[-lag:]
    nr = np.linalg.norm(o_r) + 1e-9
    a, y = a / nr, o_s / nr
    aa = np.vstack([a, math.sqrt(lam) * np.eye(len(lags))])
    ya = np.concatenate([y, np.zeros(len(lags))])
    h, _ = nnls(aa, ya, maxiter=5000)
    return lags * hop_s, h


def explained_variance(o_stem: np.ndarray, o_ref: np.ndarray, t: np.ndarray, h: np.ndarray) -> float:
    """Share of the stem's onset-envelope variance that the taps explain (1 = the stem is the reference seen through h)."""
    n = min(len(o_stem), len(o_ref))
    y, r = np.asarray(o_stem[:n], float), np.asarray(o_ref[:n], float)
    hop = HOP / SR
    rec = np.zeros(n)
    for lag, w in zip(np.round(t / hop).astype(int), h, strict=True):
        if w <= 0:
            continue
        if lag >= 0:
            rec[lag:] += w * r[: n - lag]
        else:
            rec[: n + lag] += w * r[-lag:]
    return float(1.0 - np.sum((y - rec) ** 2) / max(np.sum((y - y.mean()) ** 2), 1e-12))


@dataclass
class DelayEvidence:
    aligned: bool
    main_tap: float
    lag_s: float | None
    rel_tap: float
    z: float
    gap_checked: int
    gap_confirmed: int
    present: bool
    reason: str
    taps: dict = field(default_factory=dict)         # tap weights near T and 2T, relative to the zero-lag tap
    r2: float = float("nan")                          # onset-envelope variance explained by the taps (gate A)


def late_peak(t: np.ndarray, h: np.ndarray) -> tuple[float, float, float, float]:
    """(main tap, lag of the best late tap, its weight relative to main (taps pooled over +-10 ms), robust z)."""
    main = float(h[(t > -0.04) & (t < 0.03)].sum())
    m = t > LATE_FROM_S
    k = np.convolve(h, np.ones(5), "same")[m]
    tl = t[m]
    med = float(np.median(k))
    mad = 1.4826 * float(np.median(np.abs(k - med))) + 1e-9
    i = int(np.argmax(k))
    return main, float(tl[i]), float(k[i] / max(main, 1e-9)), float((k[i] - med) / mad)


def tap_at(t: np.ndarray, h: np.ndarray, lag: float, half_s: float = 0.015) -> float:
    return float(h[(t > lag - half_s) & (t < lag + half_s)].sum())


def gap_echo_check(stem: np.ndarray, dry: np.ndarray, lag_s: float) -> tuple[int, int]:
    """(gaps long enough to test, gaps where the stem rises at `lag_s` after the gap start). A gap is a run where the
    rebuilt dry is silent for more than lag_s + 40 ms; the echo shows as the stem level near lag_s exceeding the
    median level of the gap's later frames by 3 dB."""
    s = frame_db(stem)
    ref = loud_db(stem)
    tested = confirmed = 0
    for start, length in silent_runs(dry, min_s=lag_s + 0.04):
        a = round(start / 0.01)
        b = a + int(length / 0.01)
        i = a + round(lag_s / 0.01)
        if i + 2 >= b or b > len(s):
            continue
        tested += 1
        floor = float(np.median(s[min(a + 4, b - 1):b]))
        peak = float(np.max(s[i - 1: i + 2]))
        if peak >= floor + 3.0 and peak - ref >= -40.0:
            confirmed += 1
    return tested, confirmed


def delay_evidence(stem: np.ndarray, ref: np.ndarray, dry: np.ndarray | None = None) -> DelayEvidence:
    o_s, o_r = onset_envelope(stem), onset_envelope(ref)
    t, h = echo_deconv(o_s, o_r)
    main, lag, rel, z = late_peak(t, h)
    r2 = explained_variance(o_s, o_r, t, h)
    aligned = r2 >= ALIGN_R2_MIN and main >= ALIGN_MAIN_MIN
    taps = {"T": tap_at(t, h, lag) / max(main, 1e-9), "2T": tap_at(t, h, 2 * lag) / max(main, 1e-9)}
    if not aligned:
        return DelayEvidence(False, main, None, rel, z, 0, 0, False,
                             f"not measurable: the stem's onsets do not line up with the reference (onset-envelope variance explained {r2:.2f}, need {ALIGN_R2_MIN}; zero-lag tap {main:.2f}, need {ALIGN_MAIN_MIN})",
                             taps, r2)
    if rel < TAP_MIN_REL or z < Z_MIN:
        return DelayEvidence(True, main, lag, rel, z, 0, 0, False,
                             f"no echo: best late tap {rel:.2f} of the zero-lag tap at {lag * 1000:.0f} ms, robust z {z:.1f}", taps, r2)
    tested, ok = (0, 0) if dry is None else gap_echo_check(stem, dry, lag)
    if tested == 0:
        return DelayEvidence(True, main, lag, rel, z, 0, 0, False,
                             f"candidate echo at {lag * 1000:.0f} ms (tap {rel:.2f}, z {z:.1f}) cannot be confirmed: no note gap longer than {lag + 0.04:.2f} s", taps, r2)
    if ok * 2 < tested:
        return DelayEvidence(True, main, lag, rel, z, tested, ok, False,
                             f"candidate echo at {lag * 1000:.0f} ms (tap {rel:.2f}, z {z:.1f}) rejected: the stem rises at that lag in {ok} of {tested} note gaps", taps, r2)
    return DelayEvidence(True, main, lag, rel, z, tested, ok, True,
                         f"echo at {lag * 1000:.0f} ms (tap {rel:.2f}, z {z:.1f}), confirmed in {ok} of {tested} note gaps", taps, r2)


# ------------------------------------------------------------------------------------------ reverb

@dataclass
class TailEvidence:
    gaps: int
    tails: int
    present: bool
    measurable: bool
    slope_db_s: float | None
    early_db: float | None
    rt60_s: float | None
    reason: str
    skipped_levels_db: list = field(default_factory=list)     # stem level (re loud) 60-100 ms into the transcription gaps that were not real gaps


def gap_tail(stem: np.ndarray, ref: np.ndarray, dry: np.ndarray) -> TailEvidence:
    """Tail test in the real note gaps (module docstring). The reference render must be silent in the gap too (a
    gap where the reference still sounds is not used)."""
    s, r = frame_db(stem), frame_db(ref)
    ls, lr = loud_db(stem), loud_db(ref)
    gaps = tails = not_real = 0
    slopes, earlies, skipped = [], [], []
    for start, length in silent_runs(dry, min_s=GAP_MIN_S):
        a = round(start / 0.01)
        b = min(a + int(length / 0.01), len(s), len(r))
        if b - a < 25 or np.max(r[a + 6:b]) - lr > -45.0:
            continue
        early = float(np.mean(s[a + 6: a + 10])) - ls                      # 60 to 100 ms into the gap
        if early > REAL_GAP_MAX_DB:                                        # the stem is still playing: the transcription's gap is not a gap
            not_real += 1
            skipped.append(early)
            continue
        gaps += 1
        x = np.arange(a + 10, b - 2) * 0.01
        slope = float(np.polyfit(x, s[a + 10: b - 2], 1)[0])
        slopes.append(slope)
        earlies.append(early)
        if early >= GAP_LEVEL_MAX_DB and slope <= GAP_SLOPE_DB_S:
            tails += 1
    if gaps == 0:
        extra = f" ({not_real} transcription gaps skipped: the stem is still sounding in them)" if not_real else ""
        return TailEvidence(0, 0, False, False, None, None, None,
                            f"not measurable: no real note gap of {GAP_MIN_S:.2f} s or more where the reference is silent{extra}",
                            skipped)
    sl, ea = float(np.median(slopes)), float(np.median(earlies))
    present = tails * 2 > gaps
    rt60 = (-60.0 / sl) if (present and sl < -1e-6) else None
    why = (f"tail in {tails} of {gaps} gaps (median slope {sl:.0f} dB/s, level {ea:.0f} dB re loud)" if present else
           f"no tail: {tails} of {gaps} gaps keep falling (median slope {sl:.0f} dB/s, level {ea:.0f} dB re loud); a flat floor is ring-out or leakage")
    return TailEvidence(gaps, tails, present, True, sl, ea, rt60, why, skipped)


# ------------------------------------------------------------------------------------------ EQ

def ltas_octave_db(x: np.ndarray, centres=GRAPHIC10_CENTRES, n_fft: int = 8192) -> np.ndarray:
    """Long-term average spectrum of the active frames, power-averaged in +-1/2 octave around each centre, dB."""
    x = np.asarray(x, np.float64)
    hop = n_fft // 4
    n = 1 + max(0, (len(x) - n_fft) // hop)
    win = np.hanning(n_fft)
    fr = np.stack([x[i * hop: i * hop + n_fft] * win for i in range(n)])
    p = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    rms = 10 * np.log10(p.sum(1) + 1e-18)
    act = rms >= np.percentile(rms, 95) - 35.0
    spec = p[act].mean(0)
    f = np.fft.rfftfreq(n_fft, 1 / SR)
    out = []
    for c in centres:
        sel = (f >= c / math.sqrt(2)) & (f < c * math.sqrt(2))
        out.append(10 * np.log10(spec[sel].mean() + 1e-18) if sel.any() else -200.0)
    return np.array(out)


def ltas_ratio_db(stem: np.ndarray, render: np.ndarray, centres=GRAPHIC10_CENTRES) -> np.ndarray:
    """stem / render, dB per band (positive = the stem has more there)."""
    return ltas_octave_db(stem, centres) - ltas_octave_db(render, centres)


EQ_CORE_HZ = (250.0, 4000.0)           # the level offset is the median ratio over these bands (the guitar's main range)


def _core_offset(r: np.ndarray, c: np.ndarray, core=EQ_CORE_HZ) -> float:
    sel = (c >= core[0]) & (c <= core[1])
    return float(np.median(r[sel])) if sel.any() else float(np.median(r))


def eq_gains_from_ltas(ratio_db: np.ndarray, centres=GRAPHIC10_CENTRES, limit_db: float = EQ_LIMIT_DB,
                       ignore_below_hz: float = EQ_IGNORE_BELOW_HZ, max_hz: float = 16_000.0) -> np.ndarray:
    """Band gains in dB for a graphic EQ that moves the render's spectrum toward the stem's: the ratio minus the level offset
    (the median over the core bands 250 Hz to 4 kHz: overall level is the loudness match's job, and a stem whose treble the
    separator rolled off must not drag the whole curve), smoothed 1-2-1 across neighbouring bands, limited to +-limit_db; bands under ignore_below_hz stay at 0 dB."""
    c = np.asarray(centres, float)
    r = np.asarray(ratio_db, float)
    use = (c >= ignore_below_hz) & (c <= max_hz)
    g = np.zeros(len(c))
    d = r[use] - _core_offset(r, c)
    if len(d) >= 3:                                  # 1-2-1 smoothing: a note-dependent spike in one band must not become a notch
        p = np.concatenate([[d[0]], d, [d[-1]]])
        d = 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]
    g[use] = d
    return np.clip(g, -limit_db, limit_db)


def eq_residual_db(ratio_db: np.ndarray, centres=GRAPHIC10_CENTRES, ignore_below_hz: float = EQ_IGNORE_BELOW_HZ) -> float:
    """RMS over the counted bands (>= ignore_below_hz) of the stem/render ratio minus the same core offset: how far the render's
    spectrum is from the stem's."""
    c = np.asarray(centres, float)
    r = np.asarray(ratio_db, float)
    sel = c >= ignore_below_hz
    return float(np.sqrt(np.mean((r[sel] - _core_offset(r, c)) ** 2)))


def graphic10_values(gains_db: np.ndarray) -> dict[str, float]:
    return {k: round(float(g), 3) for k, g in zip(GRAPHIC10_KNOBS, gains_db, strict=True)}


# ------------------------------------------------------------------------------------------ light bounds, knobs

def light_mix(mix: float, kind: str) -> tuple[float, bool]:
    """(Mix knob after the 'light' cap, whether the cap bound)."""
    cap = LIGHT_MIX_CAP[kind]
    return (cap, True) if mix > cap else (float(mix), False)


def invert_linear(target_stat: float, probe_stat: float, probe_knob: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Knob for a measured statistic, assuming the statistic scales with the knob through zero (one probe render)."""
    if probe_stat <= 1e-9:
        return probe_knob
    return float(np.clip(probe_knob * target_stat / probe_stat, lo, hi))


def snap_time(t_s: float, lo: float, hi: float, steps: int = 127) -> float:
    """A delay time on the CC grid of its template range."""
    t = min(max(t_s, lo), hi)
    return lo + round((t - lo) / (hi - lo) * steps) * (hi - lo) / steps


# ------------------------------------------------------------------------------------------ amp pool

AMP_EXCLUDE = ("Clean", "Jazz", "Princess", "Deluxe", "Twin", "Tweed", "SVT", "SVBeast", "Woody", "Vib", "Bass")


def amp_pool_by_usage(ranked: list, catalog: dict, n: int = 15, exclude=AMP_EXCLUDE) -> list[str]:
    """The `n` most-used amps of the user's preset corpus (`ranked` = [[model, count, _], ...]) that are in the catalog and
    not an obviously clean, bass or vintage-clean amp (by name). A pool restriction to keep the screen near 30 renders;
    it is not a ranking by sound."""
    out = []
    for m, *_ in ranked:
        d = catalog.get("models", {}).get(m)
        if not d or d.get("category") != "amp":
            continue
        if any(x.lower() in (m + " " + str(d.get("name", ""))).lower() for x in exclude):
            continue
        out.append(m)
        if len(out) >= n:
            break
    return out


def amp_gain_knob(names) -> str | None:
    """The knob a coarse amp screen moves: the first of Drive / Gain-like names (not ChVol)."""
    for n in names:
        low = n.lower()
        if ("drive" in low or "gain" in low) and "chvol" not in low:
            return n
    return None


def amp_screen_points(free, drives=(0.5, 0.85)) -> list[dict[str, float]]:
    """Coarse points for one amp: its gain knob at each fraction of the range, other knobs at the model default."""
    k = amp_gain_knob([f[0] for f in free])
    if k is None:
        return [{}]
    _n, lo, hi, _d = next(f for f in free if f[0] == k)
    return [{k: lo + f * (hi - lo)} for f in drives]


LEVEL_KNOBS = ("output", "master", "chvol", "level")


class LockedLevel:
    """A block view whose free knobs exclude the level knobs (Output, Master, ChVol, Level); they stay at the defaults."""

    def __init__(self, block, names=LEVEL_KNOBS):
        self._b = block
        self.free = [f for f in block.free if f[0].lower() not in names]
        self.locked = [f[0] for f in block.free if f[0].lower() in names]

    def with_values(self, values):
        return self._b.with_values({k: v for k, v in values.items() if k not in self.locked})

    def __getattr__(self, k):
        return getattr(self._b, k)
