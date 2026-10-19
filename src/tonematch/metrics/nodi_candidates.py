"""Candidate no-DI scores for the cross-DI invariance diagnostic (T161; docs/notes/no-di-invariance.md).

NOT loss-kit terms. None of these is a name in contracts/loss-kit.md, none is wired into `LossKit`, and
none is admitted. They exist so `scripts/no_di_invariance.py` can ask, for each idea on the
no-DI shortlist (docs/notes/no-di-survey-metrics.md), whether it tells Helix settings apart when the
target and the candidate were rendered from different guitar samples. Parameters were fixed in
docs/DECISIONS.md (2026-10-18, T161 registration) before any number was computed.

  ltas_eqfit     LTAS band difference after a bounded fit of offset + tilt (+-1.5 dB/oct) + a 3 kHz
                 high shelf (+-6 dB): the "nuisance-EQ profiling" idea (survey section 4, option 2).
  texmod         texture/modulation statistics (McDermott-Simoncelli style): subband-envelope moments,
                 cross-band envelope correlations, envelope modulation power per band.
  mmd_cep        maximum mean discrepancy between the two clips' frame distributions of pitch-light
                 features (MFCC 1-12 without c0, flatness, log centroid).
  presence_nodi  the no-DI presence features (`presence.features_no_di`) as a standardised vector.

Plus two helpers for building the cross-DI material without new renders: `PICKUP_FILTERS` (fixed
pickup-like EQs) and `lti_transfer` / `lti_apply` (a Welch transfer estimate of a linear block, to
re-play it on another DI).

Standardised scores (`texmod`, `mmd_cep`, `presence_nodi`) need a per-feature scale: `fit_scales`
takes the median absolute deviation over a label-free sample of clips of the same source.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal
from scipy.fft import dct
from scipy.optimize import lsq_linear

from tonematch.metrics import ltas
from tonematch.metrics._common import (
    EPS,
    SAMPLE_RATE,
    active_frames,
    as_mono,
    band_matrix,
    fractional_octave_bands,
    stft_power,
)

SR = SAMPLE_RATE
REL_FLOOR = 1e-10   # -100 dB below the clip's loudest bin: an absolute floor would make the scores level-dependent

# ------------------------------------------------------------------------------------- ltas_eqfit
EQFIT_TILT_MAX = 1.5      # dB per octave
EQFIT_SHELF_MAX = 6.0     # dB
EQFIT_SHELF_HZ = 3000.0
EQFIT_SHELF_WIDTH_OCT = 0.5

_U = np.log2(ltas._CENTRES / 1000.0)
_H = 1.0 / (1.0 + np.exp(-(np.log2(ltas._CENTRES / EQFIT_SHELF_HZ)) / EQFIT_SHELF_WIDTH_OCT))


def eqfit(a: ltas.LtasFeatures, b: ltas.LtasFeatures) -> tuple[float, float, float]:
    """(distance, fitted tilt dB/oct, fitted shelf dB). The fit explains `a - b` by offset + tilt +
    high shelf within the bounds; the distance is the weighted RMS of what is left, in dB."""
    d = a.bands_db - b.bands_db
    sw = np.sqrt(ltas._BW)
    A = np.stack([np.ones_like(_U), _U, _H], axis=1) * sw[:, None]
    lo = np.array([-np.inf, -EQFIT_TILT_MAX, -EQFIT_SHELF_MAX])
    hi = np.array([np.inf, EQFIT_TILT_MAX, EQFIT_SHELF_MAX])
    sol = lsq_linear(A, d * sw, bounds=(lo, hi), method="bvls")
    g, t, s = sol.x
    r = d - (g + t * _U + s * _H)
    return float(np.sqrt(ltas._BW @ r ** 2) / ltas.LTAS_SCALE_DB), float(t), float(s)


# ----------------------------------------------------------------------------------------- texmod
TEX_NFFT, TEX_HOP = 1024, 256
TEX_CENTRES = fractional_octave_bands(80.0, 12_000.0, 2)
_TEX_BANDS = band_matrix(TEX_CENTRES, TEX_NFFT, SR, 2)
MOD_CENTRES = np.array([0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0])
_FR = SR / TEX_HOP
TEX_GROUPS = ("mom", "corr", "modp")


def texmod_features(x: np.ndarray) -> dict[str, np.ndarray]:
    x = as_mono(x)
    p = stft_power(x, TEX_NFFT, TEX_HOP)
    bp = p @ _TEX_BANDS.T
    bp = np.maximum(bp, bp.max() * REL_FLOOR + 1e-30)             # (frames, bands); floor relative to the clip
    act = active_frames(x, TEX_NFFT, TEX_HOP)
    if act.sum() < 8:
        act = np.ones_like(act)
    c = bp[act] ** 0.15                                             # compressed envelope (amplitude^0.3)
    m = c.mean(axis=0)
    sd = c.std(axis=0) + EPS
    z = (c - m) / sd
    cv = sd / (m + EPS)
    skew = (z ** 3).mean(axis=0)
    kurt = np.clip((z ** 4).mean(axis=0) - 3.0, -3.0, 30.0)
    mom = np.concatenate([cv, skew, np.log1p(kurt + 3.0)])
    lg = np.log10(bp[act])
    cc = np.corrcoef(lg.T) if lg.shape[0] > 2 else np.eye(lg.shape[1])
    cc = np.nan_to_num(cc, nan=0.0)
    corr = cc[np.triu_indices(cc.shape[0], 1)]
    db = 10 * np.log10(bp)
    db = np.maximum(db, db.max() - 60.0)
    db = db - db.mean(axis=0)
    nper = min(512, db.shape[0])
    f, pw = signal.welch(db, fs=_FR, nperseg=nper, axis=0)        # (freqs, bands)
    tot = pw[(f >= 0.25) & (f <= 45.0)].sum(axis=0) + EPS
    mp = []
    for fc in MOD_CENTRES:
        sel = (f >= fc / np.sqrt(2)) & (f < fc * np.sqrt(2))
        mp.append(np.log10(pw[sel].sum(axis=0) / tot + 1e-6) if sel.any() else np.full(pw.shape[1], -6.0))
    modp = np.concatenate(mp)
    return {"mom": mom, "corr": corr, "modp": modp}


def texmod_distance(a: dict, b: dict, scales: dict[str, np.ndarray]) -> float:
    return float(np.mean([np.sqrt(np.mean(((a[g] - b[g]) / scales[g]) ** 2)) for g in TEX_GROUPS]))


# ---------------------------------------------------------------------------------------- mmd_cep
MMD_NFFT, MMD_HOP = 2048, 512
MMD_MAX_FRAMES = 256
MMD_DIMS = 14


def _mel_matrix() -> np.ndarray:
    try:
        import librosa
        return librosa.filters.mel(sr=SR, n_fft=MMD_NFFT, n_mels=40, fmin=60.0, fmax=12_000.0)
    except ImportError:  # pragma: no cover - librosa is a dev dependency; fall back to 1/4-octave bands
        cen = fractional_octave_bands(60.0, 12_000.0, 4)[:40]
        return band_matrix(cen, MMD_NFFT, SR, 4)


_MEL = _mel_matrix()
_FREQS = np.fft.rfftfreq(MMD_NFFT, 1.0 / SR)
_BANDSEL = (_FREQS >= 60.0) & (_FREQS <= 12_000.0)


def frame_features(x: np.ndarray) -> np.ndarray:
    """(n_frames <= 256, 14) pitch-light frame features of the active frames, float32."""
    x = as_mono(x)
    p = stft_power(x, MMD_NFFT, MMD_HOP)
    act = active_frames(x, MMD_NFFT, MMD_HOP)
    p = p[act]
    mp = p @ _MEL.T
    mel = 10 * np.log10(np.maximum(mp, mp.max() * REL_FLOOR + 1e-30))   # floor relative: level-invariant
    cep = dct(mel, type=2, norm="ortho", axis=1)[:, 1:13]
    pb = p[:, _BANDSEL]
    pb = np.maximum(pb, pb.max() * REL_FLOOR + 1e-30)
    flat = 10 * np.log10(np.exp(np.mean(np.log(pb), axis=1)) / pb.mean(axis=1))
    cen = np.log2((pb * _FREQS[_BANDSEL]).sum(axis=1) / pb.sum(axis=1))
    f = np.concatenate([cep, flat[:, None], cen[:, None]], axis=1)
    if f.shape[0] > MMD_MAX_FRAMES:
        f = f[np.linspace(0, f.shape[0] - 1, MMD_MAX_FRAMES).round().astype(int)]
    return f.astype(np.float32)


def mmd(a: np.ndarray, b: np.ndarray, scale: np.ndarray, sigma: float) -> float:
    """sqrt of the biased MMD^2 with a Gaussian kernel of width `sigma` on standardised frames."""
    x = a / scale
    y = b / scale

    def k(u, v):
        d2 = (u * u).sum(1)[:, None] + (v * v).sum(1)[None, :] - 2 * u @ v.T
        return np.exp(-np.maximum(d2, 0.0) / (2 * sigma * sigma))

    m2 = k(x, x).mean() + k(y, y).mean() - 2 * k(x, y).mean()
    return float(np.sqrt(max(m2, 0.0)))


# ---------------------------------------------------------------------------------- presence_nodi

def presence_vector(x: np.ndarray) -> np.ndarray:
    from tonematch.metrics import presence
    f, _ = presence.features_no_di(x)
    return np.array([f[k] for k in presence.NO_DI_FEATURES], dtype=np.float64)


def vec_distance(a: np.ndarray, b: np.ndarray, scale: np.ndarray) -> float:
    """RMS standardised difference over the features defined in both clips (some presence features,
    e.g. a decay slope, are undefined on some clips and are NaN)."""
    z = ((np.asarray(a, float) - np.asarray(b, float)) / scale) ** 2
    ok = np.isfinite(z)
    return float(np.sqrt(z[ok].mean())) if ok.any() else float("nan")


# ----------------------------------------------------------------------------------------- scales

def mad_scale(rows: np.ndarray, floor: float = 1e-3) -> np.ndarray:
    """Per-column 1.4826 x median absolute deviation, floored (a constant column gets scale `floor`
    of the column's typical magnitude or 1e-3, so it cannot dominate)."""
    rows = np.asarray(rows, dtype=np.float64)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(rows, axis=0)
            s = 1.4826 * np.nanmedian(np.abs(rows - med), axis=0)
    med = np.nan_to_num(med, nan=0.0)
    fallback = np.maximum(np.abs(med) * 0.1, floor)
    return np.where(np.isfinite(s) & (s > floor), s, fallback)


@dataclass
class Scales:
    tex: dict[str, np.ndarray]
    frame: np.ndarray
    sigma: float
    pres: np.ndarray

    def to_json(self) -> dict:
        return {"tex": {k: v.tolist() for k, v in self.tex.items()}, "frame": self.frame.tolist(),
                "sigma": self.sigma, "pres": self.pres.tolist()}

    @classmethod
    def from_json(cls, d: dict) -> Scales:
        return cls({k: np.asarray(v) for k, v in d["tex"].items()}, np.asarray(d["frame"]), float(d["sigma"]),
                   np.asarray(d["pres"]))


def fit_scales(tex: list[dict], frames: list[np.ndarray], pres: list[np.ndarray], seed: int = 161) -> Scales:
    """Label-free scales from a sample of clips of one source."""
    ts = {g: mad_scale(np.stack([t[g] for t in tex])) for g in TEX_GROUPS}
    allf = np.concatenate(frames, axis=0).astype(np.float64)
    fs = mad_scale(allf)
    rng = np.random.default_rng(seed)
    s = allf[rng.choice(allf.shape[0], size=min(2000, allf.shape[0]), replace=False)] / fs
    d2 = (s * s).sum(1)[:, None] + (s * s).sum(1)[None, :] - 2 * s @ s.T
    sigma = float(np.sqrt(np.median(d2[np.triu_indices(s.shape[0], 1)])))
    return Scales(ts, fs, max(sigma, 1e-3), mad_scale(np.stack(pres)))


# --------------------------------------------------------------------------------- pickup filters

def _biquad(kind: str, f0: float, q: float = 0.707, gain_db: float = 0.0, s: float = 0.5) -> np.ndarray:
    """RBJ audio-EQ-cookbook biquad as one SOS row."""
    a_ = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / SR
    cw, sw = np.cos(w0), np.sin(w0)
    if kind in ("lowshelf", "highshelf"):
        alpha = sw / 2 * np.sqrt((a_ + 1 / a_) * (1 / s - 1) + 2)
        sq = 2 * np.sqrt(a_) * alpha
        if kind == "lowshelf":
            b = [a_ * ((a_ + 1) - (a_ - 1) * cw + sq), 2 * a_ * ((a_ - 1) - (a_ + 1) * cw),
                 a_ * ((a_ + 1) - (a_ - 1) * cw - sq)]
            a = [(a_ + 1) + (a_ - 1) * cw + sq, -2 * ((a_ - 1) + (a_ + 1) * cw), (a_ + 1) + (a_ - 1) * cw - sq]
        else:
            b = [a_ * ((a_ + 1) + (a_ - 1) * cw + sq), -2 * a_ * ((a_ - 1) + (a_ + 1) * cw),
                 a_ * ((a_ + 1) + (a_ - 1) * cw - sq)]
            a = [(a_ + 1) - (a_ - 1) * cw + sq, 2 * ((a_ - 1) - (a_ + 1) * cw), (a_ + 1) - (a_ - 1) * cw - sq]
    else:
        alpha = sw / (2 * q)
        if kind == "peak":
            b = [1 + alpha * a_, -2 * cw, 1 - alpha * a_]
            a = [1 + alpha / a_, -2 * cw, 1 - alpha / a_]
        elif kind == "lowpass":
            b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        else:
            raise ValueError(kind)
    b, a = np.asarray(b) / a[0], np.asarray(a) / a[0]
    return np.concatenate([b, a])


# Fixed in the T161 registration (docs/DECISIONS.md 2026-10-18). Bounded, pickup-like, not fitted.
PICKUP_FILTERS: dict[str, np.ndarray] = {
    "tilt_up": np.stack([_biquad("lowshelf", 300.0, gain_db=-3.0), _biquad("highshelf", 1500.0, gain_db=3.0)]),
    "tilt_down": np.stack([_biquad("lowshelf", 300.0, gain_db=3.0), _biquad("highshelf", 1500.0, gain_db=-3.0)]),
    "humbucker_res": np.stack([_biquad("peak", 2200.0, q=1.5, gain_db=5.0), _biquad("lowpass", 5000.0, q=0.707)]),
    "singlecoil_res": np.stack([_biquad("peak", 4500.0, q=2.0, gain_db=4.0)]),
    "position_notch": np.stack([_biquad("peak", 1000.0, q=1.2, gain_db=-6.0)]),
}


def pickup(x: np.ndarray, name: str) -> np.ndarray:
    return signal.sosfilt(PICKUP_FILTERS[name], np.asarray(x, dtype=np.float64)).astype(np.float32)


# ----------------------------------------------------------------------------------- LTI transplant
LTI_TAPS = 16_384          # 0.34 s of impulse response; longer tails do not null and drop out
LTI_PRE = 64
LTI_NULL_MAX_DB = -25.0


def lti_transfer(x: np.ndarray, y: np.ndarray, n_taps: int = LTI_TAPS, pre: int = LTI_PRE) -> np.ndarray:
    """Least-squares FIR (`presence.wiener_fir`, n_taps long, starting `pre` samples before time zero)
    of the best linear fit y ~ h * x. A Welch transfer estimate was tried first and nulled only to
    about -20 dB on EQ renders whose coherence with the DI is 0.999; the time-domain fit reaches
    below -60 dB on the same renders."""
    from tonematch.metrics.presence import wiener_fir
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    n = min(x.size, y.size)
    return wiener_fir(x[:n], y[:n], n_taps, pre)


def lti_apply(x: np.ndarray, h: np.ndarray, pre: int = LTI_PRE) -> np.ndarray:
    x = np.asarray(x, np.float64)
    return signal.fftconvolve(x, h)[pre: pre + x.size].astype(np.float32)


def null_db(y: np.ndarray, yhat: np.ndarray) -> float:
    y = np.asarray(y, np.float64)
    r = y - np.asarray(yhat, np.float64)[: y.size]
    return float(10 * np.log10((r @ r + 1e-30) / (y @ y + 1e-30)))
