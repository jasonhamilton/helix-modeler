"""Modulation measurement (plan Step 6, modulation; reserved term name `mod_periodicity`): LFO rate,
depth (amplitude and pitch), and the spectrum with the LFO averaged out. All phase-independent: two
renders whose LFOs start at different points of the cycle give the same numbers.

    p = mod_profile(render, ref=block_input)     # ModProfile
    d = compare(stomp_profile, native_profile, kind="tremolo" | "pitch" | "sweep")

Needs the block's input (`ref`: the same engine's render with the block off, or the DI when the
block is first). Dividing by the input removes the notes: what is left is the block's time-varying
transfer function, so the measurement does not depend on what was played, only on when the input
had energy (silent frames are ignored).

- **Gain trajectories.** Short-time cross-spectra (2048-point frames, 5 ms hop) give the transfer
  function H(t, f) = S_yx / S_xx, summed in 1/3-octave bands from 200 Hz to 5 kHz and over the whole
  range (broadband). Their level in dB over time is the amplitude modulation (tremolo: broadband;
  phaser, flanger, chorus: notches sweeping through the bands).
- **Delay trajectory.** Per 40 ms frame, the (partly whitened) cross-correlation of output with input
  at lags -2 to 25 ms, minus its average over all frames (which removes the dry path and any fixed
  filtering); the peak of what is left, followed from frame to frame, is the modulated delay d(t).
  On a pitched note the peak repeats every period of the note, so the rate of change d'(t) is used:
  it is the same on every repeat. A delay moving as +-A sin(2 pi f t) shifts pitch by up to
  2 pi f A, reported in cents.
- **Rate.** A least-squares sinusoid periodogram (works on gappy time grids) of every trajectory,
  normalised and averaged; the peak between 0.1 and 12 Hz is the LFO rate. Depths are amplitudes of
  sinusoids fitted at that rate.
- **Spectrum without the LFO.** Output power over input power per 1/3-octave band, averaged over a
  whole number of LFO cycles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.ndimage import maximum_filter1d

from tonematch.metrics._common import EPS, as_mono, frame
from tonematch.metrics._fx import SR, db10, fit_sinusoid, periodogram_peak, third_octave_centres

NAME = "mod_periodicity"
N_FFT, HOP = 2048, 240
GAIN_BANDS = third_octave_centres(200.0, 5000.0)
SPEC_BANDS = third_octave_centres(100.0, 10_000.0)
RATE_RANGE = (0.4, 12.0)            # a 4 s clip holds under 2 cycles below 0.4 Hz
TOP_K = 3
XC_FRAME, XC_HOP = 1920, 480          # 40 ms frames, 10 ms hop for the delay tracker
XC_LAGS = (-96, 1200)                 # -2 ms .. 25 ms
MAX_DELAY_SPEED = 0.2                 # s per s (2 ms per 10 ms hop); a 5 Hz LFO swinging +-5 ms reaches 0.16


@dataclass
class ModProfile:
    rate_hz: float
    rate_confidence: float                 # periodogram peak, 0..1 (share of variance one sinusoid explains)
    amp_depth_db: float                    # broadband peak-to-trough level swing at the rate
    sweep_depth_db: float                  # largest per-band peak-to-trough swing (notch sweep)
    delay_depth_ms: float                  # peak-to-peak swing of the modulated delay
    pitch_depth_cents: float               # peak frequency deviation, from the delay velocity
    pitch_depth_cents_pos: float           # from the delay position: informational, unreliable on pitched notes
    pitch_depth_cents_vel: float           # from the delay's rate of change (noisier, alias-proof)
    mean_delay_ms: float
    lfo_phase_rad: float                   # informational only: where the LFO was at t = 0
    alias_steps: int = 0                   # tracker steps at its speed limit (jumps between note-period aliases)
    cycles: float = float("nan")           # LFO cycles inside the active (playing) span
    spec_bands_hz: list[float] = field(default_factory=list)
    spec_db: list[float] = field(default_factory=list)   # mean output/input power per band, dB

    def to_dict(self) -> dict:
        return asdict(self)


def _detrend(t: np.ndarray, v: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Remove a weighted straight line (level and slow drift)."""
    sw = np.sqrt(np.maximum(w, 0)) + EPS
    A = np.stack([np.ones_like(t), t - t.mean()], axis=1)
    coef, *_ = np.linalg.lstsq(A * sw[:, None], v * sw, rcond=None)
    return v - A @ coef


def _stft(x: np.ndarray) -> np.ndarray:
    win = np.hanning(N_FFT + 1)[:-1]
    return np.fft.rfft(frame(x, N_FFT, HOP) * win, axis=1)


def _band_index(centres: np.ndarray, sr: int) -> list[np.ndarray]:
    f = np.fft.rfftfreq(N_FFT, 1 / sr)
    return [np.flatnonzero((f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))) for c in centres]


def _delay_track(y: np.ndarray, x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(frame times s, delay s, frame weight) of the modulated part of the output-input correlation."""
    lo, hi = XC_LAGS
    pad = max(-lo, hi)
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad + XC_FRAME)])
    starts = np.arange(0, y.size - XC_FRAME, XC_HOP)
    n = 1 << int(np.ceil(np.log2(XC_FRAME + hi - lo + 1)))
    rows, w = [], []
    for s in starts:
        yf = y[s:s + XC_FRAME]
        xs = xp[pad + s - hi: pad + s + XC_FRAME - lo]          # x[s - hi .. s + F - lo)
        C = np.conj(np.fft.rfft(yf, n)) * np.fft.rfft(xs, n)
        # partial phase transform: flattens the spectrum so the note's own periodicity does not
        # dominate the correlation; c[k] = sum_m yf[m] xs[m + k], lag l (y[m] ~ x[m - l]) is k = hi - l
        C = C / (np.abs(C) ** 0.75 + EPS + 1e-3 * np.abs(C).max() ** 0.75)
        c = np.fft.irfft(C, n)
        r = c[: hi - lo + 1][::-1]                               # lags lo..hi
        rows.append(r / (np.sqrt(np.sum(r ** 2)) + EPS))
        w.append(float(np.sum(x[max(s - hi, 0): s + XC_FRAME] ** 2)))
    R = np.asarray(rows)
    w = np.asarray(w)
    ok = w >= w.max() * 1e-3
    if ok.sum() < 3:
        return starts / sr, np.zeros(starts.size), np.zeros(starts.size)
    # the dry path and any fixed filtering are the same in every frame: remove their average
    resid = R - np.average(R[ok], axis=0, weights=w[ok])
    resid[~ok] = 0.0
    resid = resid / (resid.std(axis=1, keepdims=True) + EPS)
    # continuity: best path through the frames, the delay moving at most MAX_DELAY_SPEED
    jump = max(int(MAX_DELAY_SPEED * XC_HOP), 1)
    acc = resid[0].copy()
    back = np.zeros(resid.shape, dtype=np.int64)
    idx = np.arange(resid.shape[1])
    for i in range(1, resid.shape[0]):
        best = maximum_filter1d(acc, 2 * jump + 1, mode="nearest")
        # where the best predecessor is (argmax inside the window)
        win = np.lib.stride_tricks.sliding_window_view(np.pad(acc, jump, mode="edge"), 2 * jump + 1)
        back[i] = idx + np.argmax(win, axis=1) - jump
        acc = best + resid[i]
    path = np.empty(resid.shape[0], dtype=np.int64)
    path[-1] = int(np.argmax(acc))
    for i in range(resid.shape[0] - 1, 0, -1):
        path[i - 1] = np.clip(back[i, path[i]], 0, resid.shape[1] - 1)
    d = np.empty(starts.size)
    sal = np.empty(starts.size)
    for i, k in enumerate(path):
        row = resid[i]
        sal[i] = row[k]
        frac = 0.0
        if 0 < k < row.size - 1:
            a, b, c3 = row[k - 1], row[k], row[k + 1]
            den = a - 2 * b + c3
            frac = float(np.clip(0.5 * (a - c3) / den, -0.5, 0.5)) if den != 0 else 0.0
        d[i] = (lo + k + frac) / sr
    # keep frames where the moving peak stands out (z >= 3)
    keep = ok & (sal >= 3.0)
    return (starts + XC_FRAME / 2) / sr, d, np.where(keep, sal, 0.0)


def _delay_velocity(td: np.ndarray, d: np.ndarray, wd: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Rate of change of the tracked delay between consecutive kept frames (s per s). On a pitched note
    the correlation also peaks at the delay plus or minus whole periods of the note, and the tracker can
    sit on one of those for a while; they move exactly like the true delay, so the velocity is the same
    on every one of them. Steps between them (at the tracker's speed limit) are dropped."""
    jump = MAX_DELAY_SPEED * XC_HOP / sr                   # the tracker's limit, s per hop
    i = np.flatnonzero((wd[1:] > 0) & (wd[:-1] > 0))
    step = d[i + 1] - d[i]
    keep = np.abs(step) < 0.8 * jump
    n_alias_steps = int((~keep).sum())
    i, step = i[keep], step[keep]
    hop_s = XC_HOP / sr
    return (td[i] + td[i + 1]) / 2, step / hop_s, np.minimum(wd[i], wd[i + 1]), n_alias_steps


def mod_profile(y: np.ndarray, ref: np.ndarray, *, kind: str | None = None, sr: int = SR) -> ModProfile:
    """`kind` ("tremolo", "pitch", "sweep" or None) picks which trajectories set the rate: the broadband
    level for a tremolo, the delay track for chorus and vibrato, the band levels for phaser and flanger.
    None lets the most periodic trajectories decide."""
    y, x = as_mono(y, "render"), as_mono(ref, "ref")
    m = min(y.size, x.size)
    y, x = y[:m], x[:m]
    Y, X = _stft(y), _stft(x)
    t = (np.arange(Y.shape[0]) * HOP + N_FFT / 2) / sr
    Sxx, Syx, Syy = np.abs(X) ** 2, Y * np.conj(X), np.abs(Y) ** 2
    trajs: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    # broadband 200 Hz .. 5 kHz
    f = np.fft.rfftfreq(N_FFT, 1 / sr)
    bb = np.flatnonzero((f >= 200) & (f <= 5000))
    wb = Sxx[:, bb].sum(1)
    okb = wb >= wb.max() * 1e-3
    gb = db10(np.abs(Syx[:, bb].sum(1)) ** 2 / (wb ** 2 + EPS))
    trajs.append((t[okb], gb[okb], wb[okb]))
    band_g = []
    for idx in _band_index(GAIN_BANDS, sr):
        if idx.size == 0:
            continue
        w = Sxx[:, idx].sum(1)
        ok = w >= w.max() * 1e-3
        if ok.sum() < 20:
            continue
        g = db10(np.abs(Syx[:, idx].sum(1)) ** 2 / (w ** 2 + EPS))
        band_g.append((t[ok], g[ok], w[ok]))
    trajs += band_g
    td, d, wd = _delay_track(y, x, sr)
    tv, vel, wv, n_alias = _delay_velocity(td, d, wd, sr)
    okd = wd > 0
    has_vel = vel.size >= 20
    has_pos = okd.sum() >= 20
    if has_pos:
        trajs.append((td[okd], d[okd], wd[okd]))           # position: clean when no note-period alias
    if has_vel:
        trajs.append((tv, vel, wv))                        # velocity: the same on every alias
    # slow drifts (note decays the transfer estimate still sees, level changes) are not the LFO
    trajs = [(tt, _detrend(tt, vv, ww), ww) for tt, vv, ww in trajs]
    # rate: periodogram of every trajectory; the TOP_K most periodic ones are averaged (a tremolo
    # moves every band, a vibrato mainly the delay track, a phaser some bands), coarse then fine
    pgs = [periodogram_peak(tt, vv, *RATE_RANGE, n=400, w=np.sqrt(ww)) for tt, vv, ww in trajs]
    n_band = len(band_g)
    delay_idx = [i for i in (1 + n_band, 2 + n_band) if i < len(trajs)]
    # the delay velocity, which is the same on every note-period alias the tracker may sit on; the
    # position is reported too but drifts between aliases without any one large step (seen on the
    # synthetic vibrato and chorus tests), so it never sets the rate or the primary depth
    i_v = 1 + n_band + int(has_pos) if has_vel else None
    i_p = 1 + n_band if has_pos else None
    use_v = i_v is not None
    if kind == "tremolo":
        top = np.array([0])
    elif kind == "pitch" and delay_idx:
        top = np.array([i_v if use_v else i_p])
    elif kind == "sweep" and n_band:
        top = 1 + np.argsort([-pgs[1 + i][1].max() for i in range(n_band)])[:TOP_K]
    else:
        top = np.argsort([-pw.max() for _, pw in pgs])[:TOP_K]
    freqs = pgs[0][0]
    acc = np.mean([pgs[i][1] for i in top], axis=0)
    k = int(np.argmax(acc))
    lo_f, hi_f = freqs[max(k - 2, 0)], freqs[min(k + 2, freqs.size - 1)]
    fine = [periodogram_peak(trajs[i][0], trajs[i][1], lo_f, hi_f, n=200, w=np.sqrt(trajs[i][2])) for i in top]
    acc2 = np.mean([pw for _, pw in fine], axis=0)
    rate = float(fine[0][0][int(np.argmax(acc2))])
    conf = float(acc2.max())
    band_g = trajs[1:1 + n_band]
    amp, phase, _ = fit_sinusoid(trajs[0][0], trajs[0][1], rate, np.sqrt(trajs[0][2]))
    sweep = max((2 * fit_sinusoid(tt, vv, rate, np.sqrt(ww))[0] for tt, vv, ww in band_g), default=float("nan"))
    a_dv = a_dp = cents_vel = cents_pos = mean_d = float("nan")
    if i_v is not None:
        a_v, _, _ = fit_sinusoid(trajs[i_v][0], trajs[i_v][1], rate, trajs[i_v][2])   # peak velocity, s/s
        a_dv = a_v / (2 * np.pi * rate)
        cents_vel = float(1200 * np.log2(1 + a_v))
    if i_p is not None:
        a_dp, _, _ = fit_sinusoid(trajs[i_p][0], trajs[i_p][1], rate, trajs[i_p][2])
        cents_pos = float(1200 * np.log2(1 + 2 * np.pi * rate * a_dp))
        mean_d = float(np.median(d[okd]))       # informational: may sit on a whole-period alias
    a_d, cents = (a_dv, cents_vel) if use_v else (a_dp, cents_pos)
    # spectrum with the LFO averaged out: a whole number of cycles of the active frames
    act = np.flatnonzero(okb)
    t0 = t[act[0]]
    span = t[act[-1]] - t0
    cycles = np.floor(span * rate)
    keep = act if cycles < 1 else act[t[act] <= t0 + cycles / rate]
    spec = []
    for idx in _band_index(SPEC_BANDS, sr):
        spec.append(float(db10(Syy[np.ix_(keep, idx)].sum() / (Sxx[np.ix_(keep, idx)].sum() + EPS))) if idx.size else float("nan"))
    return ModProfile(rate_hz=rate, rate_confidence=conf, amp_depth_db=float(2 * amp), sweep_depth_db=float(sweep),
                      delay_depth_ms=float(2000 * a_d), pitch_depth_cents=cents, pitch_depth_cents_pos=cents_pos,
                      pitch_depth_cents_vel=cents_vel,
                      mean_delay_ms=float(1000 * mean_d),
                      lfo_phase_rad=phase, alias_steps=n_alias, cycles=float(span * rate), spec_bands_hz=[float(c) for c in SPEC_BANDS], spec_db=spec)


PRIMARY_DEPTH = {"tremolo": "amp_depth_db", "pitch": "pitch_depth_cents", "sweep": "sweep_depth_db"}


def compare(ref: ModProfile, other: ModProfile, kind: str = "pitch") -> dict:
    """`other` minus `ref`. `kind` picks the depth that matters for the effect (tremolo: amplitude,
    chorus and vibrato: pitch, phaser and flanger: notch sweep); all depths are reported."""
    sd = np.asarray(other.spec_db) - np.asarray(ref.spec_db)
    sd = sd[np.isfinite(sd)]
    lvl = float(np.mean(sd)) if sd.size else float("nan")
    shape = sd - lvl
    key = PRIMARY_DEPTH[kind]
    a, b = getattr(ref, key), getattr(other, key)
    out = {
        "kind": kind, "primary_depth": key,
        "rate_diff_pct": 100 * (other.rate_hz - ref.rate_hz) / ref.rate_hz,
        "rate_ref_hz": ref.rate_hz, "rate_other_hz": other.rate_hz,
        "amp_depth_diff_db": other.amp_depth_db - ref.amp_depth_db,
        "sweep_depth_diff_db": other.sweep_depth_db - ref.sweep_depth_db,
        "pitch_depth_diff_cents": other.pitch_depth_cents - ref.pitch_depth_cents,
        "pitch_depth_pos_diff_cents": other.pitch_depth_cents_pos - ref.pitch_depth_cents_pos,
        "pitch_depth_diff_pct": 100 * (other.pitch_depth_cents - ref.pitch_depth_cents) / ref.pitch_depth_cents
        if ref.pitch_depth_cents > 0 else float("nan"),
        "primary_depth_ref": a, "primary_depth_other": b,
        "spec_level_diff_db": lvl,
        "spec_shape_max_abs_db": float(np.max(np.abs(shape))) if shape.size else float("nan"),
        "spec_shape_rms_db": float(np.sqrt(np.mean(shape ** 2))) if shape.size else float("nan"),
        "lfo_phase_diff_rad": float(np.angle(np.exp(1j * (other.lfo_phase_rad - ref.lfo_phase_rad)))),
    }
    return out
