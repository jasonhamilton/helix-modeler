"""Beats, bars, sections and snapshot grouping (first cut; constant-tempo, 4/4 assumed).

Beats and section boundaries come from the full mix. A tone embedding is computed per 2 s guitar
window (1 s hop), silent windows are skipped, the rest are clustered into at most 3 groups, and
each bar gets a snapshot index. The bar-to-snapshot map is the thing the user may override
(`SectionMap.override`). Dependencies: numpy and scipy only. Not validated on real songs.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.signal import find_peaks, stft

from tonematch.separation.stems import Stems

WIN_S, HOP_S, MAX_SNAPSHOTS, BEATS_PER_BAR = 2.0, 1.0, 3, 4
SILENCE_DB_BELOW_P95 = 40.0
_BAND_EDGES = np.geomspace(80.0, 12000.0, 25)


@dataclass
class SectionMap:
    tempo_bpm: float
    beat_times: list[float]
    bar_starts: list[float]              # seconds; bar i spans bar_starts[i]..bar_starts[i+1]
    section_boundaries: list[int]        # bar indices (0-based) where a new section starts
    bar_snapshot: list[int]              # snapshot index per bar (the user-facing map)
    n_snapshots: int
    window_times: list[float] = field(default_factory=list)     # centres of the used windows
    window_labels: list[int] = field(default_factory=list)
    exposed_windows: list[tuple[float, float]] = field(default_factory=list)
    overridden: bool = False
    notes: str = ""

    def override(self, spans: str | dict[tuple[int, int], int]) -> SectionMap:
        """Return a copy with user-set snapshots. `spans` is "1-8:0,9-16:1" (1-based bars,
        inclusive) or {(first, last): snapshot}."""
        if isinstance(spans, str):
            parsed = {}
            for part in filter(None, (p.strip() for p in spans.split(","))):
                rng, snap = part.split(":")
                a, _, b = rng.partition("-")
                parsed[(int(a), int(b or a))] = int(snap)
            spans = parsed
        bars = list(self.bar_snapshot)
        for (a, b), snap in spans.items():
            if not (1 <= a <= b <= len(bars)):
                raise ValueError(f"bar span {a}-{b} outside 1..{len(bars)}")
            if not (0 <= snap < MAX_SNAPSHOTS):
                raise ValueError(f"snapshot {snap} outside 0..{MAX_SNAPSHOTS - 1}")
            bars[a - 1:b] = [snap] * (b - a + 1)
        out = SectionMap(**{**asdict(self), "bar_snapshot": bars,
                            "n_snapshots": len(set(bars)), "overridden": True})
        return out

    def table(self) -> str:
        """Human-readable bar -> snapshot runs."""
        rows, start = [], 0
        for i in range(1, len(self.bar_snapshot) + 1):
            if i == len(self.bar_snapshot) or self.bar_snapshot[i] != self.bar_snapshot[start]:
                t0 = self.bar_starts[start]
                rows.append(f"bars {start + 1}-{i}  ({t0:.1f}s)  snapshot {self.bar_snapshot[start]}")
                start = i
        return "\n".join(rows)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> SectionMap:
        d = json.loads(Path(path).read_text())
        d["exposed_windows"] = [tuple(w) for w in d["exposed_windows"]]
        return cls(**d)


def _band_energy(x: np.ndarray, sr: int, nperseg: int = 2048, hop: int = 512):
    f, t, z = stft(x, fs=sr, nperseg=nperseg, noverlap=nperseg - hop)
    p = np.abs(z) ** 2
    bands = np.stack([p[(f >= lo) & (f < hi)].sum(axis=0)
                      for lo, hi in itertools.pairwise(_BAND_EDGES)])
    return f, t, p, bands


def track_beats(mix_mono: np.ndarray, sr: int) -> tuple[float, np.ndarray, int]:
    """Return (bpm, beat times in s, downbeat phase 0..3). Constant tempo."""
    _, t, p, _ = _band_energy(mix_mono, sr)
    logp = np.log1p(1e3 * p / (p.max() + 1e-20))
    flux = np.maximum(np.diff(logp, axis=1), 0).sum(axis=0)
    flux = flux - np.convolve(flux, np.ones(43) / 43, mode="same")
    flux = np.maximum(flux, 0)
    dt = float(t[1] - t[0])
    lo, hi = int(60 / 200 / dt), int(60 / 60 / dt)
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(lo, min(hi, len(ac) - 1))
    if len(lags) == 0 or flux.sum() == 0:
        return 120.0, np.arange(0, len(mix_mono) / sr, 0.5), 0
    bpm_of = 60.0 / (lags * dt)
    prior = np.exp(-0.5 * (np.log2(bpm_of / 120.0) / 0.9) ** 2)
    lag = lags[int(np.argmax(ac[lags] * prior))]
    # refine tempo and phase jointly on the interpolated onset envelope
    ft = (np.arange(len(flux)) + 1) * dt          # np.diff shifts the time axis by one frame
    best, best_off, period = -1.0, 0.0, lag * dt
    for per in lag * dt * (1 + np.linspace(-0.03, 0.03, 121)):
        for off in np.arange(0, per, dt / 2):
            pos = off + per * np.arange(int((len(mix_mono) / sr - off) / per))
            score = np.interp(pos, ft, flux).sum() * (1 - 1e-3 * off / per)  # earliest wins ties
            if score > best:
                best, best_off, period = score, off, per
    beats = best_off + period * np.arange(int((len(mix_mono) / sr - best_off) / period))
    # downbeat: which of 4 phases has the most low-band energy on its beats
    low = p[(np.linspace(0, sr / 2, p.shape[0]) < 150)].sum(axis=0)
    bidx = np.clip(np.round(beats / dt).astype(int), 0, len(low) - 1)
    phase = int(np.argmax([low[bidx[k::BEATS_PER_BAR]].sum() for k in range(BEATS_PER_BAR)]))
    return float(60.0 / period), beats, phase


def _section_boundaries(mix_mono: np.ndarray, sr: int, bar_starts: np.ndarray) -> list[int]:
    """Novelty on bar-synchronous band features of the mix (checkerboard kernel)."""
    nbars = len(bar_starts) - 1
    if nbars < 8:
        return []
    _, t, _, bands = _band_energy(mix_mono, sr)
    lb = np.log(bands + 1e-12)
    feats = []
    for i in range(nbars):
        sel = (t >= bar_starts[i]) & (t < bar_starts[i + 1])
        feats.append(lb[:, sel].mean(axis=1) if sel.any() else lb[:, -1])
    f = np.array(feats)
    f = (f - f.mean(axis=0)) / (f.std(axis=0) + 1e-9)
    nrm = f / (np.linalg.norm(f, axis=1, keepdims=True) + 1e-9)
    ssm = nrm @ nrm.T
    k = min(4, nbars // 4)
    kern = np.outer(np.r_[-np.ones(k), np.ones(k)], np.r_[-np.ones(k), np.ones(k)])
    nov = np.zeros(nbars)
    for i in range(k, nbars - k):
        nov[i] = (ssm[i - k:i + k, i - k:i + k] * kern).sum()
    if nov.max() <= 0:
        return []
    pk, _ = find_peaks(nov, height=0.5 * nov.max(), distance=max(4, k))
    return [int(i) for i in pk]


def tone_embedding(win: np.ndarray, sr: int) -> np.ndarray:
    """Level-invariant tone shape of a mono window: 24 log-band energies (mean removed), spectral
    centroid, flatness, crest factor. Hand-built stand-in until tone-metrics supplies a learned
    artifact-robust embedding."""
    _, _, _p, bands = _band_energy(win, sr)
    lb = np.log10(bands.mean(axis=1) + 1e-12)
    shape = lb - lb.mean()
    centres = np.sqrt(_BAND_EDGES[:-1] * _BAND_EDGES[1:])
    w = 10 ** lb
    centroid = np.log2((centres * w).sum() / w.sum())
    flat = np.log10(np.exp(np.mean(np.log(w + 1e-20))) / (w.mean() + 1e-20) + 1e-9)
    rms = np.sqrt(np.mean(win**2)) + 1e-12
    crest = 20 * np.log10((np.abs(win).max() + 1e-12) / rms)
    return np.concatenate([shape, [centroid, flat, crest / 6.0]])


def _silhouette(x: np.ndarray, lab: np.ndarray) -> float:
    d = np.linalg.norm(x[:, None] - x[None], axis=-1)
    s = []
    for i in range(len(x)):
        same = (lab == lab[i])
        same[i] = False
        if not same.any():
            s.append(0.0)
            continue
        a = d[i, same].mean()
        b = min(d[i, lab == c].mean() for c in set(lab) if c != lab[i])
        s.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(s))


def cluster_windows(emb: np.ndarray, max_k: int = MAX_SNAPSHOTS, min_silhouette: float = 0.25
                    ) -> np.ndarray:
    """Cluster to 1..max_k groups; a split is accepted only if silhouette clears the bar, which is
    the 'merge until 3 or fewer' rule. Labels are ordered by first appearance."""
    if len(emb) < 4:
        return np.zeros(len(emb), dtype=int)
    x = (emb - emb.mean(axis=0)) / (emb.std(axis=0) + 1e-9)
    best_lab, best_s = np.zeros(len(x), dtype=int), min_silhouette
    for k in range(2, max_k + 1):
        if len(x) < 2 * k:
            break
        _, lab = kmeans2(x, k, minit="++", seed=0, iter=50)
        if len(set(lab)) < 2 or min(np.bincount(lab)[np.bincount(lab) > 0]) < 2:
            continue
        s = _silhouette(x, lab)
        if s > best_s:
            best_lab, best_s = lab, s
    order = {c: i for i, c in enumerate(dict.fromkeys(best_lab.tolist()))}
    return np.array([order[c] for c in best_lab])


def segment(stems: Stems) -> SectionMap:
    sr = stems.sr
    mix_m, gtr_m = stems.mix.mean(axis=0), stems.guitar.mean(axis=0)
    bpm, beats, phase = track_beats(mix_m, sr)
    dur = len(mix_m) / sr
    bar_len = beats[1] - beats[0] if len(beats) > 1 else 2.0
    bar_len *= BEATS_PER_BAR
    first = beats[phase] if len(beats) > phase else 0.0
    first = first % bar_len
    if first > 0.25 * bar_len:  # a short lead-in bar before the first downbeat
        first -= bar_len
    bar_starts = first + bar_len * np.arange(int((dur - first) / bar_len) + 1)
    bar_starts = np.maximum(bar_starts, 0.0)
    if len(bar_starts) < 2:
        bar_starts = np.array([0.0, dur])
    bar_starts = np.append(bar_starts, bar_starts[-1] + (bar_starts[-1] - bar_starts[-2]))
    nbars = len(bar_starts) - 1
    bounds = _section_boundaries(mix_m, sr, bar_starts)

    win, hop = int(WIN_S * sr), int(HOP_S * sr)
    starts = np.arange(0, max(len(gtr_m) - win, 0) + 1, hop)
    rms_db = np.array([20 * np.log10(np.sqrt(np.mean(gtr_m[s:s + win] ** 2)) + 1e-12)
                       for s in starts])
    keep = rms_db > (np.percentile(rms_db, 95) - SILENCE_DB_BELOW_P95)
    keep &= rms_db > -60.0
    used = starts[keep]
    emb = np.array([tone_embedding(gtr_m[s:s + win], sr) for s in used]) if len(used) else \
        np.zeros((0, 27))
    labels = cluster_windows(emb) if len(used) else np.zeros(0, dtype=int)
    centres = (used + win / 2) / sr

    # exposed-guitar windows: guitar holds > 80 % of the mix power
    exposed = []
    for s in used:
        pg = np.sum(gtr_m[s:s + win] ** 2)
        pm = np.sum(mix_m[s:s + win] ** 2) + 1e-20
        if pg / pm > 0.8:
            exposed.append((s / sr, (s + win) / sr))

    # bars -> snapshot: majority of window centres in the bar, then majority within each section
    raw = np.full(nbars, -1)
    for i in range(nbars):
        sel = (centres >= bar_starts[i]) & (centres < bar_starts[i + 1])
        if sel.any():
            raw[i] = np.bincount(labels[sel]).argmax()
    sec_edges = [0, *bounds, nbars]
    smooth = raw.copy()
    for a, b in itertools.pairwise(sec_edges):
        seg = raw[a:b][raw[a:b] >= 0]
        if len(seg):
            smooth[a:b] = np.bincount(seg).argmax()
    last = next((v for v in smooth if v >= 0), 0)
    for i in range(nbars):  # silent bars inherit the previous snapshot
        if smooth[i] < 0:
            smooth[i] = last
        last = smooth[i]
    order = {c: i for i, c in enumerate(dict.fromkeys(smooth.tolist()))}
    snap = [order[c] for c in smooth.tolist()]
    return SectionMap(
        tempo_bpm=round(bpm, 2), beat_times=[round(float(b), 3) for b in beats],
        bar_starts=[round(float(b), 3) for b in bar_starts[:-1]], section_boundaries=bounds,
        bar_snapshot=snap, n_snapshots=len(set(snap)),
        window_times=[round(float(c), 3) for c in centres], window_labels=labels.tolist(),
        exposed_windows=exposed,
        notes="constant tempo, 4/4 assumed; hand-built tone embedding; user override expected")
