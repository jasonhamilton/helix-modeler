"""Drive-pedal measurements against the known DI (T143).

A drive block is mostly memoryless: a tone shaping before and after a clipper, plus a level. Against the
DI that went in, its effect shows in four places that do not depend on which notes were played:

  tone     the level change per third-octave band, with the mean removed (the shape of the filtering)
  gain     the overall level change in dB
  squash   how much the gain falls from soft frames to loud frames, per band group (clipping and compression)
  fill     crest factor, spectral flatness and high-band share of the output against the input (harmonic fill)

`features(target, dry)` returns one vector of `NAMES`. Nothing here is a loss term and nothing is admitted:
the vector feeds a nearest-neighbour lookup in a calibration table (`drive_specialist`) for knob seeds.
"""
from __future__ import annotations

import numpy as np

SR = 48_000
N_FFT = 2048
HOP = 512
BANDS = 2.0 ** (np.arange(0, 7.01, 0.5)) * 62.5          # third-octave-ish centres, 62.5 Hz to 8 kHz
GROUPS = ((100.0, 500.0), (500.0, 2000.0), (2000.0, 8000.0))
NAMES = (["gain_db"] + [f"tone{round(c)}" for c in BANDS] + [f"squash{int(lo)}" for lo, _ in GROUPS]
         + ["crest_db", "flat_db", "hf_share_db", "floor_db"])
EPS = 1e-12


def _frames(x: np.ndarray) -> np.ndarray:
    n = 1 + max(len(x) - N_FFT, 0) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    w = np.hanning(N_FFT)
    return np.abs(np.fft.rfft(x[idx] * w, axis=1)) ** 2          # (frames, bins) power


def align(target: np.ndarray, dry: np.ndarray, max_lag: int = 96) -> tuple[np.ndarray, np.ndarray]:
    """Trim both to the same length and remove a small lag (the strongest cross-correlation peak)."""
    n = min(len(target), len(dry))
    t, d = np.asarray(target[:n], float), np.asarray(dry[:n], float)
    seg = slice(0, min(n, 2 * SR))
    best, lag = -1.0, 0
    for k in range(-max_lag, max_lag + 1):
        a = t[seg][max(k, 0):]
        b = d[seg][max(-k, 0):]
        m = min(len(a), len(b))
        c = abs(float(np.dot(a[:m], b[:m])))
        if c > best:
            best, lag = c, k
    if lag > 0:
        t, d = t[lag:], d[: n - lag]
    elif lag < 0:
        t, d = t[: n + lag], d[-lag:]
    return t, d


def _band_edges(freqs: np.ndarray) -> list[np.ndarray]:
    return [(freqs >= c / 2 ** 0.25) & (freqs < c * 2 ** 0.25) for c in BANDS]


def features(target: np.ndarray, dry: np.ndarray) -> np.ndarray:
    t, d = align(np.asarray(target, float), np.asarray(dry, float))
    pt, pd_ = _frames(t) + EPS, _frames(d) + EPS
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    rms_t, rms_d = np.sqrt(np.mean(t ** 2) + EPS), np.sqrt(np.mean(d ** 2) + EPS)
    gain_db = 20 * np.log10(rms_t / rms_d)
    # tone: band level change over the whole clip, mean-removed
    bands = _band_edges(freqs)
    lt = np.array([10 * np.log10(pt[:, b].sum() / pd_[:, b].sum()) if b.any() else 0.0 for b in bands])
    tone = lt - lt.mean()
    # squash: gain of the loud frames minus the gain of the soft frames, per group
    e_d = pd_.sum(1)
    soft, loud = e_d <= np.quantile(e_d, 0.4), e_d >= np.quantile(e_d, 0.8)
    sq = []
    for lo, hi in GROUPS:
        b = (freqs >= lo) & (freqs < hi)
        g_s = 10 * np.log10(pt[soft][:, b].sum() / pd_[soft][:, b].sum())
        g_l = 10 * np.log10(pt[loud][:, b].sum() / pd_[loud][:, b].sum())
        sq.append(g_l - g_s)
    # fill
    def crest(x):
        return 20 * np.log10(np.max(np.abs(x)) / (np.sqrt(np.mean(x ** 2)) + EPS) + EPS)

    def flat(p):
        return float(np.mean(10 * np.log10(np.exp(np.mean(np.log(p), 1)) / np.mean(p, 1))))

    hf = freqs >= 4000
    hf_t = 10 * np.log10(pt[:, hf].sum() / pt.sum())
    hf_d = 10 * np.log10(pd_[:, hf].sum() / pd_.sum())
    g_frame = 10 * np.log10(pt.sum(1) / pd_.sum(1))
    floor = float(np.quantile(g_frame, 0.1) - np.median(g_frame))
    return np.array([gain_db, *tone, *sq, crest(t) - crest(d), flat(pt) - flat(pd_), hf_t - hf_d, floor], float)
