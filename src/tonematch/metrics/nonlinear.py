"""Distortion parity diagnosis: when two renders of the same drive block fail a null, is the
difference tone (spectrum, harmonic structure), or only noise, aliasing, high-frequency filtering or
level?

    d = diagnose(stomp, native, di=dry, ref_a=stomp_block_input, ref_b=native_block_input)

Measured, all after aligning `a` onto `b` and one least-squares gain:

- `level_diff_db`: a minus b in RMS level (the spectra and harmonics are compared after an RMS match;
  the null after a least-squares gain).
- `null_db` full band, and `null_db_below_hz` with both renders low-passed at 5, 8 and 10 kHz: a
  null that passes once the top octave is removed means the difference lives up there (oversampling
  filters, aliasing), not in the body of the tone.
- `band_diff_db`: 1/3-octave long-term spectrum of a minus b (63 Hz to 16 kHz) and where the
  residual lives (`band_residual_db`, residual re b's band).
- Harmonic structure: on frames where the DI has a clear pitch, the level of harmonics 1 to 12
  relative to the fundamental, a minus b, median over frames; and the inharmonic share of 2-10 kHz
  (energy away from any harmonic, where aliasing lands), per render.
- Noise floors: the last 300 ms of each render (silence in, so what is left is the engine's noise).
- With the block inputs (`ref_a`, `ref_b`), the incoherent (nonlinear) part of each output per band,
  from the magnitude-squared coherence with its own input: how much distortion each engine adds, band
  by band, independent of the linear filtering.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from tonematch.metrics._common import EPS, as_mono
from tonematch.metrics._fx import (
    SR,
    align_gain,
    band_levels_db,
    db10,
    db20,
    lowpass,
    third_octave_centres,
)

BANDS = third_octave_centres(63.0, 16000.0)
CORE = (80.0, 8000.0)            # the band where a guitar tone's character is judged
N_HARM = 12


def _null(a: np.ndarray, b: np.ndarray) -> float:
    return float(db10(np.sum((a - b) ** 2) / (np.sum(b ** 2) + EPS)))


def f0_track(di: np.ndarray, *, sr: int = SR, n: int = 4096, hop: int = 2048, fmin: float = 70.0,
             fmax: float = 1000.0, min_conf: float = 0.85) -> tuple[np.ndarray, np.ndarray]:
    """(frame centres in samples, f0 Hz or nan) from the normalised autocorrelation (YIN-like)."""
    x = as_mono(di, "di")
    centres, f0s = [], []
    lmin, lmax = int(sr / fmax), int(sr / fmin)
    for s in range(0, x.size - n, hop):
        fr = x[s:s + n] * np.hanning(n)
        if np.sum(fr ** 2) < 1e-8:
            centres.append(s + n // 2)
            f0s.append(np.nan)
            continue
        ac = np.fft.irfft(np.abs(np.fft.rfft(fr, 2 * n)) ** 2)[:n]
        ac = ac / (ac[0] + EPS)
        seg = ac[lmin:lmax]
        k = int(np.argmax(seg))
        # prefer the first peak close to the best (avoid octave-down errors)
        cand = np.flatnonzero(seg >= 0.9 * seg[k])
        k = int(cand[0]) if cand.size else k
        while k + 1 < seg.size and seg[k + 1] > seg[k]:
            k += 1
        conf = float(seg[k])
        centres.append(s + n // 2)
        f0s.append(sr / (lmin + k) if conf >= min_conf else np.nan)
    return np.asarray(centres), np.asarray(f0s)


def harmonic_levels(x: np.ndarray, centres: np.ndarray, f0s: np.ndarray, *, sr: int = SR, n: int = 8192,
                    n_harm: int = N_HARM, tol: float = 0.03) -> tuple[np.ndarray, np.ndarray]:
    """Per frame: harmonic levels re harmonic 1 (dB, shape (frames, n_harm)) and the inharmonic share of
    2-10 kHz in dB. Frames without a pitch are nan."""
    f = np.fft.rfftfreq(n, 1 / sr)
    win = np.hanning(n)
    H = np.full((centres.size, n_harm), np.nan)
    inh = np.full(centres.size, np.nan)
    band = (f >= 2000) & (f <= 10000)
    for i, (c, f0) in enumerate(zip(centres, f0s, strict=True)):
        if not np.isfinite(f0) or c - n // 2 < 0 or c + n // 2 > x.size:
            continue
        P = np.abs(np.fft.rfft(x[c - n // 2: c + n // 2] * win)) ** 2
        lv = []
        near = np.zeros(f.size, bool)
        for k in range(1, int(10000 / f0) + 1):
            sel = (f >= k * f0 * (1 - tol)) & (f <= k * f0 * (1 + tol))
            near |= sel
            if k <= n_harm:
                lv.append(db10(P[sel].max() if sel.any() else 0.0))
        H[i, :len(lv)] = np.asarray(lv) - lv[0]
        tot = P[band].sum()
        inh[i] = db10(P[band & ~near].sum() / (tot + EPS)) if tot > 0 else np.nan
    return H, inh


def incoherent_bands_db(ref: np.ndarray, y: np.ndarray, *, sr: int = SR, n: int = 4096) -> np.ndarray:
    """Per 1/3-octave band: output power not explained linearly by the input, Pyy (1 - coherence), in dB
    re the output's total power."""
    m = min(ref.size, y.size)
    f, coh = signal.coherence(ref[:m], y[:m], fs=sr, nperseg=n)
    _, pyy = signal.welch(y[:m], fs=sr, nperseg=n)
    inc = pyy * (1 - coh)
    tot = pyy.sum() + EPS
    out = np.empty(BANDS.size)
    for i, c in enumerate(BANDS):
        sel = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        out[i] = db10(inc[sel].sum() / tot) if sel.any() else np.nan
    return out


def diagnose(a: np.ndarray, b: np.ndarray, *, di: np.ndarray | None = None, ref_a: np.ndarray | None = None,
             ref_b: np.ndarray | None = None, sr: int = SR, noise_tail_s: float = 0.3) -> dict:
    a, b = as_mono(a, "a"), as_mono(b, "b")
    m = min(a.size, b.size)
    a, b = a[:m], b[:m]
    am, lag, g = align_gain(b, a)
    # the null uses the least-squares gain; spectra and harmonics use an RMS level match, because the
    # least-squares gain shrinks when the waveforms decorrelate and would show up as a false tilt
    a_al = am / g
    rms_g = float(np.sqrt(np.sum(b ** 2) / (np.sum(a_al ** 2) + EPS)))
    ar = a_al * rms_g
    out: dict = {"lag_samples": lag, "level_diff_db": float(-db20(rms_g)), "ls_gain_db": float(db20(g)),
                 "null_db": _null(am, b)}
    out["null_db_below_hz"] = {str(int(fc)): _null(lowpass(am, fc, sr=sr), lowpass(b, fc, sr=sr)) for fc in (5000, 8000, 10000)}
    la, lb = band_levels_db(ar, BANDS, sr=sr), band_levels_db(b, BANDS, sr=sr)
    lr = band_levels_db(am - b, BANDS, sr=sr)
    diff = la - lb
    core = (BANDS >= CORE[0]) & (BANDS <= CORE[1])
    loud = lb >= lb.max() - 50.0
    out["bands_hz"] = BANDS.tolist()
    out["band_diff_db"] = diff.tolist()
    out["band_residual_db"] = (lr - lb).tolist()
    out["core_band_diff_max_abs_db"] = float(np.max(np.abs(diff[core & loud]))) if (core & loud).any() else float("nan")
    out["core_band_diff_rms_db"] = float(np.sqrt(np.mean(diff[core & loud] ** 2))) if (core & loud).any() else float("nan")
    above = BANDS > 10000
    out["hf_band_diff_max_abs_db"] = float(np.max(np.abs(diff[above & loud]))) if (above & loud).any() else float("nan")
    res = am - b
    e_res = band_levels_db(res, BANDS, sr=sr)
    p_res = 10 ** (e_res / 10)
    out["residual_share_above_10k"] = float(p_res[above].sum() / (p_res.sum() + EPS))
    out["residual_share_above_5k"] = float(p_res[BANDS > 5000].sum() / (p_res.sum() + EPS))
    k = int(noise_tail_s * sr)
    body = b[: m - int(2.0 * sr)] if m > 3 * sr else b
    out["noise_floor_dbfs"] = {"a": float(db20(np.sqrt(np.mean(a[-k:] ** 2)))), "b": float(db20(np.sqrt(np.mean(b[-k:] ** 2))))}
    out["residual_re_noise_db"] = float(db20(np.sqrt(np.mean(res[: body.size] ** 2))) - max(out["noise_floor_dbfs"].values()))
    if di is not None:
        c, f0 = f0_track(di, sr=sr)     # frames 85 ms long: the few samples of engine latency do not matter
        Ha, ia = harmonic_levels(ar, c, f0, sr=sr)
        Hb, ib = harmonic_levels(b, c, f0, sr=sr)
        ok = np.isfinite(Ha[:, 0]) & np.isfinite(Hb[:, 0])
        out["harmonic_frames"] = int(ok.sum())
        if ok.sum() >= 3:
            hd = np.nanmedian(Ha[ok] - Hb[ok], axis=0)
            out["harmonic_diff_db"] = hd.tolist()                       # harmonics 1..12 re harmonic 1
            out["harmonic_diff_max_abs_db"] = float(np.nanmax(np.abs(hd[1:10])))
            out["inharmonic_share_db"] = {"a": float(np.nanmedian(ia[ok])), "b": float(np.nanmedian(ib[ok]))}
    if ref_a is not None and ref_b is not None:
        ia_, ib_ = incoherent_bands_db(ref_a, a, sr=sr), incoherent_bands_db(ref_b, b, sr=sr)
        dd = ia_ - ib_
        sel = core & np.isfinite(dd) & (ib_ >= np.nanmax(ib_) - 40)
        out["incoherent_diff_db"] = dd.tolist()
        out["incoherent_core_max_abs_db"] = float(np.max(np.abs(dd[sel]))) if sel.any() else float("nan")
    return out


def classify(d: dict, *, band_tol_db: float = 1.0, harm_tol_db: float = 1.0, level_tol_db: float = 1.0,
             null_pass_db: float = -30.0) -> str:
    """One word for what a failed null is made of: 'pass' (null passes), 'tone' (a core-band or
    harmonic difference at or above the tolerance), 'hf-only' (passes once low-passed at 10 kHz),
    'noise' (residual within 6 dB of the noise floor), else 'waveform-only' (the null fails but every
    spectral and harmonic measure is inside its tolerance: fine structure, phase or sample-and-hold
    timing, not a different tone). A level difference beyond tolerance is appended ('+level')."""
    lvl = "+level" if abs(d["level_diff_db"]) > level_tol_db else ""
    if d["null_db"] <= null_pass_db:
        return "pass" + lvl
    harm = d.get("harmonic_diff_max_abs_db", 0.0)
    if d["core_band_diff_max_abs_db"] > band_tol_db or (np.isfinite(harm) and harm > harm_tol_db):
        return "tone" + lvl
    if d["null_db_below_hz"]["10000"] <= null_pass_db:
        return "hf-only" + lvl
    if d["residual_re_noise_db"] < 6.0:
        return "noise" + lvl
    return "waveform-only" + lvl
