"""Modulation specialist (T116; human decision 2026-10-17, "a specialist per effect family").

For a known-DI, single-block target that may be a modulation effect (tremolo, chorus, vibrato,
flanger, phaser, rotary):

    f = features(target, dry)               # ModFeatures, phase-invariant
    a = analyse(target, dry)                # ModAnalysis: is it modulation, sub-type, rate, depth, mix
    s = seed(target, dry, models=[...])     # per-model knob estimates for the search
    d = mod_distance(target, candidate, dry)   # phase-invariant distance, lower is better
    d = score(target, candidate, dry)       # the same, with the target's features cached

Why: a free-running LFO starts at a different point of its cycle in every recording, so a
frame-by-frame comparison (null, `mrstft_v2`) of two renders of the same preset can be far apart,
and a static block (an EQ, a compressor) can score better than the right modulation block at a
slightly wrong rate. Every feature here is a statistic over time (a distribution or a power
spectrum), so shifting the LFO's starting phase leaves it unchanged up to noise.

Everything is measured on the effect's transfer, the target divided by its DI (both are scaled to
unit RMS first, so level does not count), so the notes played mostly cancel. Features:

- **LFO-averaged spectrum** (`mean_db`): output over input power per 1/3-octave band (160 Hz to
  6.3 kHz) over the whole clip, its mean level removed.
- **Level trajectories** per band, 5 ms steps (21 ms frames): their distribution (quantiles about
  the median: depth and waveform shape, `lvl_q`), their modulation spectrum (how much the level
  moves at each rate from 0.2 to 20 Hz, `env_ms`, in 4 band groups), and the correlation between
  band groups (`corr`: +1 for a tremolo, below 0 for a harmonic tremolo, in between for a sweep).
- **Pitch trajectory**: the phase advance of the output against the input between frames
  (250 Hz to 2.5 kHz), as a pitch deviation in cents: its quantiles (`pitch_q`) and modulation
  spectrum (`pitch_ms`). Chorus and vibrato move it, tremolo does not.
- **Comb-filter track** (flanger, chorus): per 100 ms, the delay whose comb best explains the
  log transfer across frequency (a weighted cepstrum, 0.3 to 25 ms); its strength-weighted
  histogram (`comb_hist`). **Notch track** (phaser): the per-frame frequency of the deepest dip of
  the transfer (1/6 octave), as a histogram (`notch_hist`).
- **Static share** (`static`): |mean H|^2 / mean |H|^2 per band group above 700 Hz. A dry path
  is constant and survives the time average; a modulated wet path rotates and averages out, so
  this measures the dry/wet mix whatever the LFO phase.

Summary measurements (`ModAnalysis`): LFO rate (peak of the summed modulation spectra with a
sub-harmonic check), its confidence (share of modulation power near the peak), amplitude depth
(5th to 95th percentile of the broadband level, dB), pitch depth (half the 5th to 95th
percentile range, cents), wet mix (from the static share), comb delay range (ms), a modulation
score (is it modulation at all) and a sub-type with the rule that chose it.

Is it modulation at all: `presence.detect`'s known-DI mod probability decides (pass it as `presence_mod`;
`seed` runs it by default), and this module's own logistic score (`mod_score`, fitted on helix-trust-v2's
train renders) can add a target only when it is at least OWN_STRICT (0.9).

For the search's family dispatch (`tonematch.search.specialists`, T122) `make_specialist()` returns an
adapter with `screen_hint`, `seed` (a `FamilySeed` whose `for_model` gives per-knob `Seed`s) and `score`
(the general kit plus FAMILY_WEIGHT x mod_distance, returned only while FAMILY_SCORE_ADMITTED is true).

The per-model knob seed needs a calibration table (`scripts/mod_calibrate.py`: renders of every
modulation model at random settings on a calibration DI used by no benchmark item, local file
`cache/mod-specialist/calibration.json`). `seed` finds the calibration renders whose summary
features are nearest the target's and returns their knob values (distance-weighted); without the
table it returns the measurements only.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import SR, third_octave_centres

NAME = "mod_distance"
VERSION = "1.0"
SUBTYPES = ("tremolo", "chorus", "vibrato", "flanger", "phaser", "rotary")
CALIBRATION = Path(__file__).resolve().parents[3] / "cache" / "mod-specialist" / "calibration.json"

N_FFT, HOP = 1024, 240                     # 21 ms frames, 5 ms steps
FRAME_RATE = SR / HOP                      # 200 trajectory samples per second
BANDS = third_octave_centres(160.0, 6300.0)
GROUP_EDGES = (160.0, 400.0, 1000.0, 2500.0, 6400.0)   # 4 band groups
PITCH_BAND = (250.0, 2500.0)
STATIC_LO = 700.0
MS_RANGE = (0.2, 25.0)                     # modulation-spectrum range, Hz
MS_BINS = 68                               # log-spaced, about 7.5 % apart
RATE_RANGE = (0.15, 22.0)
LVL_QUANTILES = (2, 5, 10, 25, 75, 90, 95, 98)
PITCH_QUANTILES = (2, 5, 10, 25, 50, 75, 90, 95, 98)
COMB_FFT, COMB_HOP, COMB_AVG = 4096, 1200, 4   # 85 ms frames, 25 ms steps, 4 averaged (100 ms)
COMB_TAU = np.geomspace(0.3e-3, 25e-3, 48)
COMB_BINS = np.geomspace(0.3e-3, 25e-3, 13)
NOTCH_BANDS = 2.0 ** (np.arange(0, 6 * np.log2(8000 / 150) + 1) / 6) * 150.0
ACTIVE_DB = -40.0                          # frames this far below the band's loud frames are ignored
SKIP_S = 0.15                              # start-up of the block
LEVEL_FLOOR_DB = 24.0                      # level trajectories are clipped this far either side of the band's median

# distance weights: each component is scaled so that "clearly different" is about 1
# (set on the development items, T116; see docs/DECISIONS.md)
WEIGHTS = {"spec": 1.0, "lvl_q": 1.0, "env": 1.0, "corr": 0.5, "pitch_q": 1.0, "pitch_ms": 1.0,
           "comb": 0.5, "notch": 0.5, "static": 1.0}
SCALES = {"spec": 1.0, "lvl_q": 1.0, "env": 1.0, "corr": 1.0, "pitch_q": 2.0, "pitch_ms": 2.0,
          "comb": 1.0, "notch": 1.0, "static": 0.2}


# ---------------------------------------------------------------------------------------------- features

@dataclass
class ModFeatures:
    mean_db: np.ndarray            # (bands,) LFO-averaged transfer, dB, mean removed
    lvl_q: np.ndarray              # (bands, quantiles) level about its median, dB
    lvl_w: np.ndarray              # (bands,) share of active frames (weights for the band terms)
    env_ms: np.ndarray             # (groups, MS_BINS) level modulation amplitude, dB
    corr: np.ndarray               # (6,) correlations between band-group level trajectories
    pitch_q: np.ndarray            # (quantiles,) pitch deviation, cents
    pitch_ms: np.ndarray           # (MS_BINS,) pitch modulation amplitude, cents
    comb_hist: np.ndarray          # (len(COMB_BINS) - 1,) comb delay histogram, strength-weighted
    comb_strength: float
    notch_hist: np.ndarray         # (len(NOTCH_BANDS),) deepest-dip histogram
    static: np.ndarray             # (groups above STATIC_LO,) static (dry) share
    # dense spectra kept for the rate estimate (not used by the distance)
    ms_freqs: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    env_dense: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    pitch_dense: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    bb_level: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    pitch: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    pitch_w: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    comb_track: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    comb_track_w: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))


def _unit(x: np.ndarray) -> np.ndarray:
    return x / (np.sqrt(np.mean(x ** 2)) + EPS)


def _stft(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    win = np.hanning(n + 1)[:-1]
    nf = 1 + max(0, (x.size - n) // hop)
    idx = np.arange(n)[None, :] + hop * np.arange(nf)[:, None]
    return np.fft.rfft(x[idx] * win, axis=1)


def _band_sel(freqs: np.ndarray, centres: np.ndarray, frac: float = 1 / 6) -> list[np.ndarray]:
    return [np.flatnonzero((freqs >= c * 2 ** -frac) & (freqs < c * 2 ** frac)) for c in centres]


_MS_CACHE: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}


def _ms_setup(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(dense frequencies, binning matrix (MS_BINS, dense), bin centres) for a trajectory of n samples."""
    if n not in _MS_CACHE:
        nfft = 1 << int(np.ceil(np.log2(4 * n)))
        f = np.fft.rfftfreq(nfft, 1 / FRAME_RATE)
        keep = np.flatnonzero((f >= MS_RANGE[0] / 1.2) & (f <= MS_RANGE[1] * 1.2))
        c = np.geomspace(*MS_RANGE, MS_BINS)
        step = np.log(c[1] / c[0])
        d = np.abs(np.log(np.maximum(f[keep], 1e-6))[None, :] - np.log(c)[:, None]) / step
        B = np.maximum(1 - d, 0.0)                           # triangular, 50 % overlap
        _MS_CACHE[n] = (f, B, keep)
    return _MS_CACHE[n]


def _mod_spectrum(v: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Power spectrum of a gappy trajectory v (weights w in [0, 1]); returns (freqs, dense power over
    the kept range, binned amplitude). Scaled so a sinusoid of amplitude A gives about A in its bin."""
    f, B, keep = _ms_setup(v.size)
    nfft = 2 * (f.size - 1)
    sw = np.maximum(w, 0.0)
    mu = float(np.sum(sw * v) / (np.sum(sw) + EPS))
    win = np.hanning(v.size) * sw
    S = np.fft.rfft((v - mu) * win, nfft)
    p = (np.abs(S[keep]) ** 2) * (4.0 / (np.sum(win) ** 2 + EPS))   # sinusoid of amplitude A -> A^2 peak
    # binned: the peak power over each bin's span (a bin holds a sinusoid's peak whatever its exact rate)
    amp = np.sqrt(np.max(B * p[None, :], axis=1))
    return f[keep], p, amp


def _wquantiles(v: np.ndarray, w: np.ndarray, qs) -> np.ndarray:
    o = np.argsort(v)
    cw = np.cumsum(w[o])
    if cw[-1] <= 0:
        return np.zeros(len(qs))
    cw /= cw[-1]
    return np.interp(np.asarray(qs) / 100.0, cw, v[o])


def _corr(a: np.ndarray, b: np.ndarray, w: np.ndarray) -> float:
    sw = w / (w.sum() + EPS)
    a = a - np.sum(sw * a)
    b = b - np.sum(sw * b)
    den = np.sqrt(np.sum(sw * a * a) * np.sum(sw * b * b))
    return float(np.sum(sw * a * b) / den) if den > 1e-12 else 0.0


def _highpass_traj(v: np.ndarray, w: np.ndarray, win_s: float = 6.0) -> np.ndarray:
    """Remove what moves slower than about 1/win_s Hz (note decays) with a weighted running mean."""
    k = max(int(win_s * FRAME_RATE) | 1, 3)
    k = max(min(k, v.size - 1 + v.size % 2), 1)      # odd and no longer than the trajectory (short clips)
    ker = np.hanning(k + 2)[1:-1]
    num = np.convolve(v * w, ker, mode="same")
    den = np.convolve(w, ker, mode="same")
    return v - num / (den + EPS)


def features(target: np.ndarray, dry: np.ndarray, *, sr: int = SR) -> ModFeatures:
    if sr != SR:
        raise ValueError(f"mod_specialist works at {SR} Hz, got {sr}")
    y, x = as_mono(target, "target"), as_mono(dry, "dry")
    n = min(y.size, x.size)
    if n < N_FFT * 8:
        raise ValueError("clip too short for the modulation measurements (needs about 0.2 s)")
    y, x = _unit(y[:n]), _unit(x[:n])
    s0 = int(SKIP_S * SR)
    y, x = y[s0:], x[s0:]
    X, Y = _stft(x, N_FFT, HOP), _stft(y, N_FFT, HOP)
    fr = np.fft.rfftfreq(N_FFT, 1 / SR)
    Sxx, Syy = np.abs(X) ** 2, np.abs(Y) ** 2
    Syx = Y * np.conj(X)
    nb = len(BANDS)
    sel = _band_sel(fr, BANDS)
    T = X.shape[0]
    lvl = np.zeros((nb, T))
    wts = np.zeros((nb, T))
    Hb = np.zeros((nb, T), complex)
    mean_db = np.zeros(nb)
    for b, idx in enumerate(sel):
        sx, sy = Sxx[:, idx].sum(1), Syy[:, idx].sum(1)
        ref = np.percentile(sx, 95) + EPS
        act = sx >= ref * 10 ** (ACTIVE_DB / 10)
        wts[b] = act * np.minimum(sx / ref, 1.0) ** 0.5
        r = 10 * np.log10((sy + EPS * ref) / (sx + EPS * ref))
        med = np.median(r[act]) if act.any() else 0.0
        lvl[b] = np.clip(r, med - LEVEL_FLOOR_DB, med + LEVEL_FLOOR_DB)    # a gated or muted step is "off", however deep
        Hb[b] = Syx[:, idx].sum(1) / (sx + EPS * ref)
        # the mean of the per-frame power ratio, loud frames counting alike: how the notes' energy falls on
        # the LFO cycle must not matter (an energy-weighted mean changes with the LFO's starting phase)
        mean_db[b] = 10 * np.log10(np.sum(wts[b] * 10 ** (lvl[b] / 10)) / (wts[b].sum() + EPS) + EPS)
    mean_db -= mean_db.mean()
    lvl_q = np.zeros((nb, len(LVL_QUANTILES)))
    lvl_hp = np.zeros_like(lvl)
    for b in range(nb):
        lvl_hp[b] = _highpass_traj(lvl[b], wts[b])
        w = wts[b]
        if w.sum() > 10:
            lvl_q[b] = _wquantiles(lvl_hp[b], w, LVL_QUANTILES) - _wquantiles(lvl_hp[b], w, (50,))[0]
    lvl_w = (wts > 0).mean(1)
    # band groups: power-weighted mean level trajectory
    groups = [np.flatnonzero((BANDS >= lo) & (BANDS < hi)) for lo, hi in itertools.pairwise(GROUP_EDGES)]
    g_lvl, g_w = [], []
    for g in groups:
        w = wts[g]
        g_w.append(np.clip(w.sum(0) / len(g), 0, 1))
        g_lvl.append(np.sum(lvl_hp[g] * w, 0) / (w.sum(0) + EPS))
    env_ms, env_dense = [], []
    freqs = np.zeros(0)
    for v, w in zip(g_lvl, g_w, strict=True):
        freqs, p, amp = _mod_spectrum(v, w)
        env_ms.append(amp)
        env_dense.append(p)
    env_ms = np.asarray(env_ms)
    env_dense = np.asarray(env_dense)
    corr = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            corr.append(_corr(g_lvl[i], g_lvl[j], np.minimum(g_w[i], g_w[j])))
    corr = np.asarray(corr)
    wall = np.mean(g_w, 0)
    bb = np.sum(np.asarray(g_lvl) * np.asarray(g_w), 0) / (np.sum(g_w, 0) + EPS)
    # pitch: phase advance of Y against X between frames, per band, as a delay velocity
    C = (Y[1:] * np.conj(Y[:-1])) * np.conj(X[1:] * np.conj(X[:-1]))
    pb = [b for b in range(nb) if PITCH_BAND[0] <= BANDS[b] <= PITCH_BAND[1]]
    num = np.zeros(T - 1)
    den = np.zeros(T - 1)
    for b in pb:
        s = C[:, sel[b]].sum(1)
        mag = np.abs(s)
        dphi = np.angle(s)
        vel = -dphi / (2 * np.pi * BANDS[b]) * FRAME_RATE       # -d'(t), s per s
        wb = np.sqrt(mag)
        num += wb * vel
        den += wb
    vel = num / (den + EPS)
    pw = den / (np.percentile(den, 95) + EPS)
    pw = np.where(pw >= 10 ** (ACTIVE_DB / 20), np.minimum(pw, 1.0), 0.0)
    cents = 1200 * np.log2(np.clip(1 + vel, 0.5, 2.0))
    cents_hp = _highpass_traj(cents, pw, 4.0)
    pitch_q = _wquantiles(cents_hp, pw, PITCH_QUANTILES) if pw.sum() > 10 else np.zeros(len(PITCH_QUANTILES))
    _, pitch_dense, pitch_ms = _mod_spectrum(cents_hp, pw)
    # static share: |mean H|^2 / mean |H|^2 per group above STATIC_LO (frames weighted by activity)
    static = []
    for g in groups:
        g = [b for b in g if BANDS[b] >= STATIC_LO]
        if not g:
            continue
        vals = []
        for b in g:
            w = wts[b]
            if w.sum() < 10:
                continue
            h = Hb[b]
            m1 = np.abs(np.sum(w * h) / w.sum()) ** 2
            m2 = np.sum(w * np.abs(h) ** 2) / w.sum()
            vals.append(m1 / (m2 + EPS))
        static.append(float(np.mean(vals)) if vals else 1.0)
    static = np.asarray(static)
    comb_hist, comb_strength, notch_hist, ctrack, ctw = _comb_notch(x, y)
    return ModFeatures(mean_db=mean_db, lvl_q=lvl_q, lvl_w=lvl_w, env_ms=env_ms, corr=corr, pitch_q=pitch_q,
                       pitch_ms=pitch_ms, comb_hist=comb_hist, comb_strength=comb_strength,
                       notch_hist=notch_hist, static=static, ms_freqs=freqs, env_dense=env_dense,
                       pitch_dense=pitch_dense, bb_level=np.stack([bb, wall]), pitch=cents_hp, pitch_w=pw,
                       comb_track=ctrack, comb_track_w=ctw)


_COMB_CACHE: dict[int, tuple] = {}


def _comb_setup():
    if "m" not in _COMB_CACHE:
        f = np.fft.rfftfreq(COMB_FFT, 1 / SR)
        k = np.flatnonzero((f >= 150) & (f <= 8000))
        E = np.exp(-2j * np.pi * f[k][:, None] * COMB_TAU[None, :])
        nsel = _band_sel(f, NOTCH_BANDS, 1 / 12)
        _COMB_CACHE["m"] = (f, k, E, nsel)
    return _COMB_CACHE["m"]


def _comb_notch(x: np.ndarray, y: np.ndarray):
    _, k, E, nsel = _comb_setup()
    X, Y = _stft(x, COMB_FFT, COMB_HOP), _stft(y, COMB_FFT, COMB_HOP)
    Sxx = np.abs(X) ** 2
    Syx = Y * np.conj(X)
    ker = np.ones(COMB_AVG)
    sxx = np.apply_along_axis(lambda v: np.convolve(v, ker, "same"), 0, Sxx)
    syx_r = np.apply_along_axis(lambda v: np.convolve(v, ker, "same"), 0, Syx.real)
    syx_i = np.apply_along_axis(lambda v: np.convolve(v, ker, "same"), 0, Syx.imag)
    H = (syx_r + 1j * syx_i) / (sxx + EPS + 1e-4 * sxx.max())
    logh = np.log(np.abs(H[:, k]) + 1e-4)
    w = sxx[:, k] / (sxx[:, k].max(1, keepdims=True) + EPS)
    w = np.sqrt(w)
    act = sxx[:, k].sum(1) >= np.percentile(sxx[:, k].sum(1), 95) * 1e-3
    # what moves: the log transfer less its time average (a static filter or comb leaves nothing)
    wa = w * act[:, None]
    logh = logh - np.sum(wa * logh, 0, keepdims=True) / (wa.sum(0, keepdims=True) + EPS)
    lw = logh - np.sum(w * logh, 1, keepdims=True) / (w.sum(1, keepdims=True) + EPS)
    c = np.abs((w * lw) @ E) / (w.sum(1, keepdims=True) + EPS)          # (frames, tau)
    best = np.argmax(c, 1)
    strength = c[np.arange(c.shape[0]), best] * act
    tau = COMB_TAU[best]
    hist, _ = np.histogram(tau, bins=COMB_BINS, weights=strength)
    tot = strength.sum() + EPS
    comb_strength = float(np.mean(strength[act])) if act.any() else 0.0
    hist = hist / tot * comb_strength
    # notch track: the deepest 1/6-octave dip (re the median over bands) per active frame
    nb = np.full((H.shape[0], len(NOTCH_BANDS)), np.nan)
    for j, idx in enumerate(nsel):
        if idx.size:
            num = np.abs(syx_r[:, idx].sum(1) + 1j * syx_i[:, idx].sum(1))
            nb[:, j] = 20 * np.log10(num / (sxx[:, idx].sum(1) + EPS) + 1e-6)
    ok = np.all(np.isfinite(nb), 0)
    nb = nb[:, ok]
    notch = np.zeros(len(NOTCH_BANDS))
    if nb.shape[1] >= 3 and act.any():
        dev = nb - np.mean(nb[act], 0, keepdims=True)          # against the time-averaged transfer
        dev = dev - np.median(dev, 1, keepdims=True)
        j = np.argmin(dev, 1)
        depth = np.clip(-dev[np.arange(dev.shape[0]), j], 0, 40) / 20.0
        h = np.bincount(j[act], weights=depth[act], minlength=nb.shape[1]) / act.sum()
        notch[np.flatnonzero(ok)] = h
    return hist, comb_strength, notch, tau, strength


# ---------------------------------------------------------------------------------------------- distance

def distance_parts(a: ModFeatures, b: ModFeatures) -> dict[str, float]:
    """Per-component distances, each divided by its scale (SCALES)."""
    w = np.sqrt(a.lvl_w * b.lvl_w) + EPS
    parts = {
        "spec": float(np.sum(w * np.abs(a.mean_db - b.mean_db)) / w.sum()),
        "lvl_q": float(np.sum(w[:, None] * np.abs(a.lvl_q - b.lvl_q)) / (w.sum() * a.lvl_q.shape[1])),
        "env": float(np.sum(np.abs(a.env_ms - b.env_ms)) / a.env_ms.shape[0] / 4.0),
        "corr": float(np.mean(np.abs(a.corr - b.corr)) * min(1.0, max(_depth(a), _depth(b)) / 1.0)),
        "pitch_q": float(np.mean(np.abs(a.pitch_q - b.pitch_q))),
        "pitch_ms": float(np.sum(np.abs(a.pitch_ms - b.pitch_ms)) / 4.0),
        "comb": float(np.sum(np.abs(a.comb_hist - b.comb_hist)) * 10.0),
        "notch": float(np.sum(np.abs(a.notch_hist - b.notch_hist))),
        "static": float(np.mean(np.abs(a.static - b.static))) if a.static.size == b.static.size else 1.0,
    }
    return {k: v / SCALES[k] for k, v in parts.items()}


def _depth(f: ModFeatures) -> float:
    """Broadband level swing, dB (5th to 95th percentile)."""
    return float(np.mean(f.lvl_q[:, LVL_QUANTILES.index(95)] - f.lvl_q[:, LVL_QUANTILES.index(5)]))


def distance(a: ModFeatures, b: ModFeatures, weights: dict[str, float] | None = None) -> float:
    w = WEIGHTS if weights is None else weights
    p = distance_parts(a, b)
    return float(sum(w.get(k, 0.0) * v for k, v in p.items()))


def mod_distance(target: np.ndarray, candidate: np.ndarray, dry: np.ndarray,
                 weights: dict[str, float] | None = None) -> float:
    """Phase-invariant distance between a target and a candidate rendered from the same DI `dry`."""
    return distance(features(target, dry), features(candidate, dry), weights)


_FEAT_CACHE: dict[str, ModFeatures] = {}


def _digest(*arrs: np.ndarray) -> str:
    h = hashlib.sha1()
    for a in arrs:
        a = np.ascontiguousarray(np.asarray(a, np.float32))
        h.update(str(a.shape).encode())
        h.update(a.tobytes())
    return h.hexdigest()


def cached_features(x: np.ndarray, dry: np.ndarray) -> ModFeatures:
    key = _digest(x, dry)
    if key not in _FEAT_CACHE:
        if len(_FEAT_CACHE) > 64:
            _FEAT_CACHE.clear()
        _FEAT_CACHE[key] = features(x, dry)
    return _FEAT_CACHE[key]


def score(target: np.ndarray, candidate: np.ndarray, dry: np.ndarray,
          weights: dict[str, float] | None = None) -> float:
    """`mod_distance` for the search: the target's features are computed once and cached."""
    return distance(cached_features(target, dry), features(candidate, dry), weights)


# ---------------------------------------------------------------------------------------------- analysis

@dataclass
class ModAnalysis:
    mod_score: float               # 0..1, this module's logistic detector (how clearly the transfer moves periodically)
    is_mod: bool                   # presence's mod probability >= 0.5, or mod_score >= OWN_STRICT
    subtype: str | None
    subtype_reason: str
    rate_hz: float
    rate_conf: float
    am_depth_db: float             # broadband level swing, 5th to 95th percentile
    pm_depth_cents: float          # half the 5th to 95th percentile pitch range
    wet_mix: float                 # 0 dry .. 1 fully wet, from the static share (comb and pitch effects)
    static_share: float
    comb_delay_ms: tuple[float, float]   # 10th, 90th percentile of the comb track (strength-weighted)
    comb_strength: float
    harmonic_anticorr: float       # correlation of the lowest and highest band groups (-1: harmonic tremolo)
    notch_spread: float            # octaves the deepest dip travels (10th to 90th percentile)
    env_peak: float                # level modulation: strongest bin over the median bin (amplitude ratio, log2)
    pitch_peak: float              # the same for the pitch modulation
    presence_mod: float | None = None   # `presence.detect`'s known-DI mod probability, when given

    def to_dict(self) -> dict:
        return asdict(self)


MOD_SCORE_MIN = 0.5
OWN_STRICT = 0.9        # this module's detector alone decides only above this (false positives 0 to 1 % on dev)


def _peak_rate(fr: np.ndarray, p: np.ndarray, sel: np.ndarray) -> tuple[float, float]:
    """Rate and periodicity of one modulation power spectrum: its peak, moved to half or a third of
    the frequency when that holds at least 0.6 of the peak's power (a square or pattern LFO has strong
    harmonics; the fundamental is the rate), refined by a parabola."""
    p = np.where(sel, p, 0.0)
    if p.max() <= 0:
        return float("nan"), 0.0
    k = int(np.argmax(p))
    fk = fr[k]
    for div in (2, 3):
        f2 = fk / div
        if f2 < RATE_RANGE[0]:
            break
        j = int(np.argmin(np.abs(fr - f2)))
        lo, hi = max(j - 3, 0), min(j + 4, fr.size)
        jj = lo + int(np.argmax(p[lo:hi]))
        if p[jj] >= 0.6 * p[k]:
            k, fk = jj, fr[jj]
            break
    if 0 < k < fr.size - 1:
        a, b, c = p[k - 1], p[k], p[k + 1]
        den = a - 2 * b + c
        if den != 0:
            fk = fr[k] + 0.5 * (a - c) / den * (fr[1] - fr[0])
    return float(fk), _periodicity(fr, p, sel, fk)


def _rate(f: ModFeatures) -> tuple[float, float]:
    """LFO rate: from the summed level modulation spectra and, when the pitch moves by 3 cents or more,
    from the pitch modulation spectrum; the more periodic of the two wins."""
    fr = f.ms_freqs
    sel = (fr >= RATE_RANGE[0]) & (fr <= RATE_RANGE[1])
    r_env = _peak_rate(fr, np.sum(f.env_dense, 0), sel)
    pm = 0.5 * (f.pitch_q[PITCH_QUANTILES.index(95)] - f.pitch_q[PITCH_QUANTILES.index(5)])
    if pm >= 3.0:
        r_p = _peak_rate(fr, f.pitch_dense, sel)
        if r_p[1] > r_env[1]:
            return r_p
    return r_env


def _periodicity(fr: np.ndarray, p: np.ndarray, sel: np.ndarray, f0: float) -> float:
    """Shape-free periodicity at rate f0: the share of the modulation power within +-6 % of f0 and its
    harmonics (to 8), above what a flat spectrum would put there (0: no more than chance, 1: all of it).
    A sweep crossing many bands, a square LFO and a pattern put much of their power in harmonics."""
    if not np.isfinite(f0) or f0 <= 0:
        return 0.0
    near = np.zeros_like(sel)
    for m in range(1, 9):
        if m * f0 > fr[sel].max():
            break
        near |= sel & (np.abs(fr - m * f0) <= max(0.06 * f0, 1.5 * (fr[1] - fr[0])))
    share = p[near].sum() / (p[sel].sum() + EPS)
    chance = near.sum() / max(sel.sum(), 1)
    return float(np.clip((share - chance) / (1 - chance + EPS), 0.0, 1.0))


def analyse(target: np.ndarray, dry: np.ndarray, feats: ModFeatures | None = None,
            presence_mod: float | None = None) -> ModAnalysis:
    """Measurements and the modulation decision. `presence_mod`: the known-DI mod probability from
    `presence.detect` (the stronger detector on dev: recall 0.97 / 0.65 on helix-trust-v2 train / test
    mod renders with 0 / 1 % false positives, against 0.81 / 0.53 with 16 / 14 % for this module's
    own); without it, the decision rests on `mod_score` alone at MOD_SCORE_MIN."""
    f = feats if feats is not None else features(target, dry)
    rate, conf = _rate(f)
    bb, ww = f.bb_level
    am = 0.0
    if ww.sum() > 10:
        q = _wquantiles(bb, ww, (5, 95))
        am = float(q[1] - q[0])
    pm = float(0.5 * (f.pitch_q[PITCH_QUANTILES.index(95)] - f.pitch_q[PITCH_QUANTILES.index(5)]))
    st = float(np.mean(f.static)) if f.static.size else 1.0
    st_c = min(max(st, 1e-4), 1 - 1e-4)
    mix = float(1.0 / (1.0 + np.sqrt(st_c / (1 - st_c))))
    if f.comb_track_w.sum() > 0:
        lo, hi = _wquantiles(f.comb_track * 1000, f.comb_track_w, (10, 90))
    else:
        lo = hi = float("nan")
    anti = float(f.corr[2]) if f.corr.size >= 3 else 0.0        # group 0 vs group 3
    nh = f.notch_hist
    if nh.sum() > 0:
        oct_ = np.log2(NOTCH_BANDS / NOTCH_BANDS[0])
        q = _wquantiles(oct_, nh, (10, 90))
        spread = float(q[1] - q[0])
    else:
        spread = 0.0
    ep = _peakiness(f.env_ms.max(0))
    pp = _peakiness(f.pitch_ms)
    ms = mod_probability({"rate_conf": conf, "am_depth_db": am, "pm_depth_cents": pm, "static_share": st,
                          "comb_strength": f.comb_strength, "env_peak": ep, "pitch_peak": pp})
    sub, why = subtype_rule(am_db=am, pm_cents=pm, static=st, anticorr=anti, comb_lo_ms=lo, comb_hi_ms=hi,
                            comb_strength=f.comb_strength, notch_spread=spread, rate_hz=rate)
    if presence_mod is None or not np.isfinite(presence_mod):
        is_mod = ms >= MOD_SCORE_MIN
    else:
        is_mod = bool(presence_mod >= 0.5 or ms >= OWN_STRICT)
    return ModAnalysis(mod_score=ms, is_mod=is_mod, subtype=sub,
                       subtype_reason=why, rate_hz=rate, rate_conf=conf, am_depth_db=am, pm_depth_cents=pm,
                       wet_mix=mix, static_share=st, comb_delay_ms=(float(lo), float(hi)),
                       comb_strength=f.comb_strength, harmonic_anticorr=anti, notch_spread=spread,
                       env_peak=ep, pitch_peak=pp,
                       presence_mod=None if presence_mod is None else float(presence_mod))


def _peakiness(amp: np.ndarray) -> float:
    if amp.size == 0 or not np.any(amp > 0):
        return 0.0
    return float(np.log2((amp.max() + 1e-6) / (np.median(amp) + 1e-6)))


DETECT_FEATURES = ("rate_conf", "log_am", "log_pm", "dyn_share", "comb_strength", "env_peak", "pitch_peak")
# logistic detector "is this modulation", fitted on the TRAIN split of helix-trust-v2 (mod renders against
# every other category's renders, T116); see docs/DECISIONS.md. Standardisation (mean, sd) then weights.
DETECT_MEAN = np.array([0.4192, 1.5462, 1.9148, 0.2416, 0.1889, 2.7184, 2.3998])
DETECT_SD = np.array([0.2356, 0.8088, 1.3812, 0.3046, 0.0749, 1.2177, 1.3199])
DETECT_W = np.array([0.232, 0.516, -0.209, -0.797, -0.305, 0.922, 1.434])
DETECT_B = 0.806


def detect_vector(m: dict) -> np.ndarray:
    return np.array([m["rate_conf"], np.log1p(max(m["am_depth_db"], 0.0)), np.log1p(max(m["pm_depth_cents"], 0.0)),
                     1.0 - m["static_share"], m["comb_strength"], m["env_peak"], m["pitch_peak"]], float)


def mod_probability(m: dict) -> float:
    z = (detect_vector(m) - DETECT_MEAN) / DETECT_SD
    return float(1.0 / (1.0 + np.exp(-(z @ DETECT_W + DETECT_B))))


def subtype_rule(*, am_db: float, pm_cents: float, static: float, anticorr: float, comb_lo_ms: float,
                 comb_hi_ms: float, comb_strength: float, notch_spread: float, rate_hz: float) -> tuple[str, str]:
    """Rule-based sub-type from the summary measurements (used when no calibration table is present,
    and reported next to the table's vote). Order: tremolo (level moves, pitch does not), then the
    pitch effects (vibrato without a dry path, chorus with one, flanger when the comb delay is short),
    then rotary (both, fast), else phaser (a sweeping dip, little pitch)."""
    if am_db >= 1.0 and pm_cents < 3.0:
        kind = "harmonic " if anticorr < -0.2 else ""
        return "tremolo", f"{kind}level swing {am_db:.1f} dB, pitch {pm_cents:.1f} cents"
    if pm_cents >= 3.0:
        if am_db >= 4.0 and rate_hz >= 3.0:
            return "rotary", f"level {am_db:.1f} dB and pitch {pm_cents:.1f} cents at {rate_hz:.1f} Hz"
        if static < 0.15:
            return "vibrato", f"pitch {pm_cents:.1f} cents, little dry path (static share {static:.2f})"
        if np.isfinite(comb_hi_ms) and comb_hi_ms <= 4.0 and comb_strength > 0.05:
            return "flanger", f"pitch {pm_cents:.1f} cents, comb delay {comb_lo_ms:.1f}-{comb_hi_ms:.1f} ms"
        return "chorus", f"pitch {pm_cents:.1f} cents with a dry path (static share {static:.2f})"
    if notch_spread >= 0.5:
        return "phaser", f"a dip sweeping {notch_spread:.1f} octaves, pitch {pm_cents:.1f} cents"
    if am_db >= 1.0:
        return "tremolo", f"level swing {am_db:.1f} dB"
    return "chorus", f"weak: level {am_db:.1f} dB, pitch {pm_cents:.1f} cents, static {static:.2f}"


def subtype_of_model(model_id: str) -> str | None:
    """Sub-type of a Helix modulation model id, from its name (no catalog file needed)."""
    m = model_id.lower()
    for key, sub in (("trem", "tremolo"), ("pattern", "tremolo"), ("panner", "tremolo"), ("vibrato", "vibrato"),
                     ("flang", "flanger"), ("phas", "phaser"), ("vibe", "phaser"), ("rotary", "rotary"),
                     ("chorus", "chorus"), ("dimension", "chorus"), ("double", "chorus")):
        if key in m:
            return sub
    return None


# ---------------------------------------------------------------------------------------------- seed

SUMMARY_KEYS = ("log_rate", "am", "pm", "mix", "anti", "spec")


def summary_vector(a: ModAnalysis, f: ModFeatures) -> np.ndarray:
    """Features the calibration table is searched on (DI-independent as far as possible)."""
    lr = np.log2(a.rate_hz) if np.isfinite(a.rate_hz) and a.rate_hz > 0 else 0.0
    spec = f.mean_db[::3] / 3.0
    return np.concatenate([[lr * 1.5 * min(1.0, a.mod_score / 0.5), a.am_depth_db / 2.0, a.pm_depth_cents / 3.0,
                            a.wet_mix * 3.0, a.harmonic_anticorr], spec])


def load_calibration(path: str | Path | None = None) -> dict | None:
    p = Path(path) if path is not None else CALIBRATION
    if not p.is_file():
        return None
    doc = json.loads(p.read_text())
    for e in doc.get("models", {}).values():
        e["_X"] = np.asarray([r["summary"] for r in e["rows"]], float)
    return doc


_CAL: dict = {}


def _calibration(path=None):
    key = str(path or CALIBRATION)
    if key not in _CAL:
        _CAL[key] = load_calibration(path)
    return _CAL[key]


def presence_mod_probability(target: np.ndarray, dry: np.ndarray) -> float | None:
    """`presence.detect`'s known-DI mod probability (about 1.3 s for 10 s), None without its model file."""
    from tonematch.metrics import presence

    try:
        p = presence.detect(target, di=dry).probabilities().get("mod")
    except (ValueError, np.linalg.LinAlgError, FileNotFoundError):
        return None
    return None if p is None else float(p)


def seed(target: np.ndarray, dry: np.ndarray, models: list[str] | None = None, *, k: int = 3,
         calibration: str | Path | dict | None = None, presence_mod: float | None | str = "auto") -> dict:
    """Measurements and per-model knob estimates for the search.

    Returns {"analysis": ModAnalysis.to_dict(), "models": {model: {"values": {knob: value},
    "fit": nearest calibration distance, "subtype": ...}}, "ranking": [models, nearest first],
    "subtype_vote": ...}. Knob values are in each knob's own units (the template range), the
    distance-weighted mean of the k nearest calibration renders of that model. `models` limits the
    models (default: every model in the table). `presence_mod`: "auto" runs `presence.detect`; pass a
    probability (or None) when the caller already has it."""
    f = features(target, dry)
    pm = presence_mod_probability(target, dry) if presence_mod == "auto" else presence_mod
    a = analyse(target, dry, f, presence_mod=pm)
    cal = calibration if isinstance(calibration, dict) else _calibration(calibration)
    out = {"analysis": a.to_dict(), "models": {}, "ranking": [], "subtype_vote": None, "version": VERSION}
    if not cal:
        return out
    v = summary_vector(a, f)
    fits = {}
    for m, e in cal["models"].items():
        if models is not None and m not in models:
            continue
        X = e["_X"]
        if X.shape[1] != v.size:
            continue
        d = np.sqrt(np.sum((X - v[None, :]) ** 2, 1))
        o = np.argsort(d)[:k]
        wk = 1.0 / (d[o] + 0.05)
        knobs = {}
        for name in e["rows"][0]["values"]:
            vals = np.asarray([e["rows"][i]["values"][name] for i in o], float)
            knobs[name] = float(np.sum(wk * vals) / wk.sum())
        fits[m] = float(d[o[0]])
        out["models"][m] = {"values": knobs, "fit": fits[m], "subtype": e.get("subtype") or subtype_of_model(m)}
    out["ranking"] = sorted(fits, key=fits.get)
    if out["ranking"]:
        votes: dict[str, float] = {}
        for m in out["ranking"][:3]:
            s = out["models"][m]["subtype"]
            if s:
                votes[s] = votes.get(s, 0.0) + 1.0 / (fits[m] + 0.05)
        out["subtype_vote"] = max(votes, key=votes.get) if votes else None
    return out


# ---------------------------------------------------------------------------------------------- dispatch adapter

# The family score the search's dispatch uses: the general kit's total plus FAMILY_WEIGHT x mod_distance, for every
# candidate. Returned by default only once mod-trust-v1 passes its registered family bar (docs/DECISIONS.md, T116).
FAMILY_WEIGHT = 0.2
FAMILY_SCORE_ADMITTED = False
SEED_K = 3
HALF_WIDTH = {"rate": (0.04, 0.20), "depth": (0.08, 0.30), "mix": (0.15, 0.30)}
# knobs of kind "other" (Headroom, Shape, DutyCycle, filter frequencies) are not seeded: on the dev items their
# neighbour estimate erred by 0.30 of the range on average, no better than the middle of the range
SEED_MAX_HALF = 0.35       # a knob whose neighbours disagree more than this (of its range) is left free


def knob_kind(name: str) -> str:
    n = name.lower()
    if n in ("speed", "rate", "fastspeed", "slowspeed", "pan speed"):
        return "rate"
    if n in ("depth", "intensity", "chorusintensity", "width", "range", "depth1", "depth2", "depth3"):
        return "depth"
    if n in ("mix", "blend"):
        return "mix"
    return "other"


def knob_seeds(target: np.ndarray, dry: np.ndarray, model: str, ranges: dict[str, tuple[float, float]], *,
               cal: dict | None = None, feats: ModFeatures | None = None, an: ModAnalysis | None = None,
               k: int = SEED_K) -> dict[str, tuple[float, float]]:
    """{knob: (value, half_width as a fraction of the range)} for one model from its k nearest calibration
    renders. The half-width is twice the neighbours' spread, clipped per knob kind; a knob whose spread is
    over SEED_MAX_HALF is not seeded."""
    cal = _calibration(None) if cal is None else cal
    if not cal or model not in cal.get("models", {}):
        return {}
    f = feats if feats is not None else features(target, dry)
    a = an if an is not None else analyse(target, dry, f)
    e = cal["models"][model]
    X = e["_X"] if "_X" in e else np.asarray([r["summary"] for r in e["rows"]], float)
    v = summary_vector(a, f)
    if X.shape[1] != v.size:
        return {}
    d = np.sqrt(np.sum((X - v[None, :]) ** 2, 1))
    o = np.argsort(d)[:k]
    wk = 1.0 / (d[o] + 0.05)
    out = {}
    for name, (lo, hi) in ranges.items():
        if hi <= lo or name not in e["rows"][0]["values"]:
            continue
        if knob_kind(name) == "rate":
            r = _rate_knob(e, name, a, lo, hi)
            if r is not None:
                out[name] = r
                continue
        if knob_kind(name) == "other":
            continue
        vals = (np.asarray([e["rows"][i]["values"][name] for i in o], float) - lo) / (hi - lo)
        mu = float(np.sum(wk * vals) / wk.sum())
        sd = float(np.sqrt(np.sum(wk * (vals - mu) ** 2) / wk.sum()))
        lo_h, hi_h = HALF_WIDTH[knob_kind(name)]
        hw = float(np.clip(2 * sd, lo_h, hi_h))
        if 2 * sd > SEED_MAX_HALF:
            continue
        out[name] = (lo + float(np.clip(mu, 0, 1)) * (hi - lo), hw)
    return out


RATE_K = 5
RATE_CONF_MIN = 0.3


def _rate_knob(e: dict, name: str, a: ModAnalysis, lo: float, hi: float) -> tuple[float, float] | None:
    """A rate knob from the measured rate: the calibration renders of the model whose measured rate is
    confidently known and nearest the target's (log scale), their knob values' weighted median; the band
    is twice their spread (4 to 20 % of the range). None when the target's rate is not confident."""
    if not (np.isfinite(a.rate_hz) and a.rate_hz > 0 and a.rate_conf >= RATE_CONF_MIN):
        return None
    rows = [r for r in e["rows"] if "analysis" in r and r["analysis"]["rate_conf"] >= RATE_CONF_MIN
            and r["analysis"]["rate_hz"] > 0]
    if len(rows) < RATE_K:
        return None
    lr = np.log(np.asarray([r["analysis"]["rate_hz"] for r in rows]))
    d = np.abs(lr - np.log(a.rate_hz))
    o = np.argsort(d)[:RATE_K]
    if d[o[0]] > np.log(1.25):           # no calibration render within 25 % of the measured rate
        return None
    u = (np.asarray([rows[i]["values"][name] for i in o], float) - lo) / (hi - lo)
    mu = float(np.median(u))
    hw = float(np.clip(2 * np.median(np.abs(u - mu)) + 0.02, *HALF_WIDTH["rate"]))
    return lo + float(np.clip(mu, 0, 1)) * (hi - lo), hw


class _Total:
    def __init__(self, total: float, terms: dict, measured: dict):
        self.total, self.terms, self.measured = total, terms, measured


class ModFamilyKit:
    """`inner` (the general kit) plus `weight` x mod_distance against the target's cached features. Exposes
    `.inner` like the level-guard kit."""

    def __init__(self, inner, target: np.ndarray, dry: np.ndarray, weight: float):
        self.inner, self.weight = inner, float(weight)
        self.dry = np.asarray(dry, np.float64)
        self.tf = features(target, dry)

    def score(self, target, candidate, **kw):
        sc = self.inner.score(target, candidate, **kw)
        try:
            d = distance(self.tf, features(np.asarray(candidate, np.float64)[: self.dry.size], self.dry))
        except ValueError:
            d = float("nan")
        d = d if np.isfinite(d) else 50.0
        return _Total(float(sc.total) + self.weight * d, {**getattr(sc, "terms", {}), NAME: d},
                      getattr(sc, "measured", {}))


class ModSpecialist:
    """Adapter for `tonematch.search.specialists` (the dispatch, T122)."""

    family = "mod"
    categories = ("mod",)
    budget_share = 1.0

    def __init__(self, calibration: str | Path | dict | None = None, score_weight: float | None = None,
                 presence: bool = True):
        self._cal = calibration if isinstance(calibration, dict) else _calibration(calibration)
        self.score_weight = (FAMILY_WEIGHT if FAMILY_SCORE_ADMITTED else 0.0) if score_weight is None else score_weight
        self.presence = presence
        self._last: tuple | None = None

    def evidence(self, target: np.ndarray, dry: np.ndarray) -> tuple[ModFeatures, ModAnalysis]:
        key = (id(target), id(dry), len(target))
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        f = features(target, dry)
        pm = presence_mod_probability(target, dry) if self.presence else None
        a = analyse(target, dry, f, presence_mod=pm)
        self._last = (key, (f, a))
        return f, a

    def screen_hint(self, target: np.ndarray, dry: np.ndarray) -> float:
        _, a = self.evidence(target, dry)
        p = a.presence_mod if a.presence_mod is not None else a.mod_score
        return float(max(p, a.mod_score) if a.mod_score >= OWN_STRICT else p)

    def seed(self, target: np.ndarray, dry: np.ndarray):
        from tonematch.search.measured_seed import Measured, Seed
        from tonematch.search.specialists import FamilySeed

        f, a = self.evidence(target, dry)
        meas = Measured(q={"mod.rate_hz": a.rate_hz, "mod.rate_conf": a.rate_conf, "mod.amp_depth_db": a.am_depth_db,
                           "mod.pitch_cents": a.pm_depth_cents, "mod.wet_mix": a.wet_mix, "mod.score": a.mod_score})
        cal = self._cal

        def for_model(model: str, category: str, ranges: dict[str, tuple[float, float]]) -> dict:
            if category != "mod":
                return {}
            ks = knob_seeds(target, dry, model, ranges, cal=cal, feats=f, an=a)
            return {n: Seed(v, hw, f"mod.knn.{knob_kind(n)}", v) for n, (v, hw) in ks.items()}

        hint = self.screen_hint(target, dry)
        return FamilySeed("mod", meas, {"mod": hint}, for_model,
                          [(f"mod specialist: p(mod) {hint:.2f} (presence {a.presence_mod}, own {a.mod_score:.2f}); "
                            f"{a.subtype} ({a.subtype_reason}); rate {a.rate_hz:.2f} Hz")],
                          {"analysis": a.to_dict(), "calibrated": bool(cal)})

    def score(self, target: np.ndarray, dry: np.ndarray):
        if self.score_weight <= 0:
            return None
        w = self.score_weight
        return lambda kit: ModFamilyKit(kit, target, dry, w)


def make_specialist(**kw) -> ModSpecialist:
    return ModSpecialist(**kw)
