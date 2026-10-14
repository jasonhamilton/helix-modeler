"""PROXY signals for testing the loss kit offline. NOT Helix, NOT an engine.

A synthetic guitar DI (additive plucked strings with pick noise), a small numpy/scipy guitar chain
(compressor, gate, boost, tanh drive, tone stack, four filter-based "cabs") and a separation-artifact
model (bleed from a synthetic bass and drum kit, removed by a jittered ratio mask). Used by the unit
tests (content invariance) and by `scripts/loss_trust_check.py --proxy`. Results on this data say
how a term behaves on plausible guitar-like DSP; they are not evidence about Helix models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np
from scipy import signal

SR = 48_000


# --------------------------------------------------------------------------- DI


def guitar_di(seed: int, seconds: float = 4.0, *, chord_prob: float = 0.35,
              low: int = 40, high: int = 76, tempo: float | None = None,
              rhythm_seed: int | None = None) -> np.ndarray:
    """A guitar-like DI: notes or power chords on a random rhythm. `seed` picks the notes;
    `rhythm_seed` (default: same as seed) picks onsets, so a fixed rhythm with new notes is possible."""
    rng_n = np.random.default_rng(seed)
    rng_r = np.random.default_rng(seed if rhythm_seed is None else rhythm_seed + 10_007)
    n = int(seconds * SR)
    out = np.zeros(n)
    bpm = tempo if tempo is not None else float(rng_r.uniform(90, 140))
    beat = 60.0 / bpm
    t = 0.05
    events = []
    while t < seconds - 0.2:
        dur = beat * float(rng_r.choice([0.5, 1.0, 1.0, 2.0]))
        events.append((t, dur, float(rng_r.uniform(0.5, 1.0))))
        t += dur
    for (t0, dur, vel) in events:
        root = int(rng_n.integers(low, high - 7))
        notes = [root]
        if rng_n.random() < chord_prob:
            notes += [root + 7] + ([root + 12] if rng_n.random() < 0.5 else [])
        ring = dur + float(rng_n.uniform(0.0, 0.3))
        for m in notes:
            _pluck(out, int(t0 * SR), ring, 440.0 * 2 ** ((m - 69) / 12), vel / len(notes) ** 0.5, rng_n)
    return (0.5 * out / (np.max(np.abs(out)) + 1e-12)).astype(np.float64)


def _pluck(buf: np.ndarray, start: int, ring_s: float, f0: float, amp: float,
           rng: np.random.Generator) -> None:
    n = min(int(ring_s * SR), buf.size - start)
    if n <= 0:
        return
    t = np.arange(n) / SR
    pp = rng.uniform(0.1, 0.25)
    tau0 = 2.0 * (110.0 / f0) ** 0.5
    note = np.zeros(n)
    k = 1
    while True:
        fk = k * f0 * np.sqrt(1 + 1e-4 * k * k)
        if fk > 12_000:
            break
        ak = abs(np.sin(np.pi * k * pp)) / k
        note += ak * np.exp(-t / (tau0 / (1 + 0.2 * (k - 1)))) * np.sin(2 * np.pi * fk * t + rng.uniform(0, 2 * np.pi))
        k += 1
    rel = int(0.03 * SR)
    if n > rel:
        note[-rel:] *= np.linspace(1, 0, rel)
    m = min(int(0.004 * SR), n)
    noise = rng.standard_normal(m) * np.linspace(1, 0, m) * 0.3
    note[:m] += signal.lfilter([1, -0.95], [1], noise)
    buf[start:start + n] += amp * note


# --------------------------------------------------------------------------- chain


def _peaking(f0, gain_db, q=0.7):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / SR
    al = np.sin(w) / (2 * q)
    b = [1 + al * a, -2 * np.cos(w), 1 - al * a]
    aa = [1 + al / a, -2 * np.cos(w), 1 - al / a]
    return np.array(b) / aa[0], np.array(aa) / aa[0]


def _shelf(f0, gain_db, high: bool):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / SR
    cw, al = np.cos(w), np.sin(w) / np.sqrt(2)
    s = 2 * np.sqrt(a) * al
    if high:
        b = [a * ((a + 1) + (a - 1) * cw + s), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - s)]
        aa = [(a + 1) - (a - 1) * cw + s, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - s]
    else:
        b = [a * ((a + 1) - (a - 1) * cw + s), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - s)]
        aa = [(a + 1) + (a - 1) * cw + s, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - s]
    return np.array(b) / aa[0], np.array(aa) / aa[0]


# hp Hz, lp Hz, resonance Hz, resonance dB, notch Hz, notch dB, low bump dB
CABS = (
    (90.0, 5500.0, 2500.0, 5.0, 900.0, -3.0, 0.0),    # "1x12 open"
    (70.0, 4500.0, 1800.0, 4.0, 600.0, -2.0, 4.0),    # "4x12 closed"
    (100.0, 7000.0, 3200.0, 6.0, 1200.0, -4.0, 0.0),  # "2x12 bright"
    (150.0, 4000.0, 2000.0, 3.0, 700.0, -2.0, 1.0),   # "1x10 small"
)

GROUPS = {
    "comp": ("comp", "comp_thr"),
    "gate": ("gate_thr",),
    "boost": ("boost_f", "boost_g"),
    "drive": ("drive", "bias"),
    "eq": ("bass", "mid", "treble"),
    "cab": ("cab", "mic"),
}


@dataclass(frozen=True)
class ProxyPreset:
    """Every continuous parameter in [0, 1]; `cab` indexes CABS. `out_db` is a nuisance level."""
    comp: float = 0.0
    comp_thr: float = 0.5
    gate_thr: float = 0.0
    boost_f: float = 0.5
    boost_g: float = 0.0
    drive: float = 0.3
    bias: float = 0.0
    bass: float = 0.5
    mid: float = 0.5
    treble: float = 0.5
    cab: int = 0
    mic: float = 0.5
    out_db: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def random_preset(rng: np.random.Generator) -> ProxyPreset:
    u = lambda: float(rng.random())
    return ProxyPreset(comp=u() if rng.random() < 0.5 else 0.0, comp_thr=u(),
                       gate_thr=u() if rng.random() < 0.4 else 0.0, boost_f=u(),
                       boost_g=u() if rng.random() < 0.5 else 0.0, drive=u(), bias=u(),
                       bass=u(), mid=u(), treble=u(), cab=int(rng.integers(len(CABS))), mic=u(),
                       out_db=float(rng.uniform(-12, 12)))


def perturb_group(p: ProxyPreset, group: str, rng: np.random.Generator, min_delta: float = 0.3) -> ProxyPreset:
    """Re-draw one group so it differs audibly-ish: a different cab, or a parameter moved >= min_delta."""
    for _ in range(100):
        ch = {}
        for k in GROUPS[group]:
            ch[k] = int(rng.integers(len(CABS))) if k == "cab" else float(rng.random())
        if group == "comp" and ch["comp"] < 0.3:
            ch["comp"] = 0.3 + 0.7 * ch["comp"]
        q = replace(p, **ch)
        if group == "cab":
            if q.cab != p.cab:
                return q
        elif max(abs(getattr(q, k) - getattr(p, k)) for k in GROUPS[group]) >= min_delta:
            return q
    return q


def _smooth_power_db(x: np.ndarray, tau_s: float) -> np.ndarray:
    a = np.exp(-1.0 / (tau_s * SR))
    pw = signal.lfilter([1 - a], [1, -a], x * x)
    return 10 * np.log10(pw + 1e-12)


def render(p: ProxyPreset, di: np.ndarray) -> np.ndarray:
    x = np.asarray(di, dtype=np.float64)
    if p.comp > 0:
        ratio = 1 + 9 * p.comp
        thr = -10 - 30 * p.comp_thr
        lvl = _smooth_power_db(x, 0.008)
        gdb = np.minimum(0.0, (thr - lvl) * (1 - 1 / ratio))
        x = x * 10 ** (gdb / 20) * 10 ** (-(thr * (1 - 1 / ratio)) / 40)
    if p.gate_thr > 0:
        thr = -70 + 35 * p.gate_thr
        lvl = _smooth_power_db(x, 0.01)
        x = x * np.clip((lvl - thr) / 6.0 + 0.5, 0.0, 1.0)
    if p.boost_g > 0:
        b, a = _peaking(500 + 1000 * p.boost_f, 12 * p.boost_g, q=0.8)
        x = signal.lfilter(b, a, x)
        b, a = signal.butter(1, 80 + 500 * p.boost_g, "highpass", fs=SR)
        x = signal.lfilter(b, a, x)
    g = 10 ** (45 * p.drive / 20)
    bias = 0.6 * p.bias
    x = np.tanh(g * x + bias) - np.tanh(bias)
    x = signal.lfilter(*signal.butter(1, 20.0, "highpass", fs=SR), x)  # DC block
    for b, a in (_shelf(120, 12 * (p.bass - 0.5), False), _peaking(700, 12 * (p.mid - 0.5)),
                 _shelf(3000, 12 * (p.treble - 0.5), True)):
        x = signal.lfilter(b, a, x)
    hp, lp, rf, rg, nf, ng, bump = CABS[p.cab]
    lp *= 0.75 + 0.5 * p.mic
    rg += 4 * (p.mic - 0.5)
    x = signal.sosfilt(signal.butter(2, hp, "highpass", fs=SR, output="sos"), x)
    x = signal.sosfilt(signal.butter(4, min(lp, 0.45 * SR), "lowpass", fs=SR, output="sos"), x)
    for b, a in (_peaking(rf, rg, 1.5), _peaking(nf, ng, 1.0), _peaking(110, bump, 1.0)):
        x = signal.lfilter(b, a, x)
    x = x / (np.sqrt(np.mean(x ** 2)) + 1e-12) * 0.1 * 10 ** (p.out_db / 20)
    return x


# --------------------------------------------------------------------------- separation artifacts


def band_bleed(n: int, rng: np.random.Generator) -> np.ndarray:
    """A synthetic bass line plus kick, snare and hat noise, the 'other stems' a separator leaks."""
    t = np.arange(n) / SR
    out = np.zeros(n)
    beat = int(SR * 60 / float(rng.uniform(90, 140)))
    for s in range(0, n, beat):
        e = min(n, s + beat)
        f = 440 * 2 ** ((int(rng.integers(28, 45)) - 69) / 12)
        tt = t[: e - s]
        out[s:e] += 0.6 * np.sin(2 * np.pi * f * tt) * np.exp(-tt / 0.4)
        kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-tt / 0.02)) * tt) * np.exp(-tt / 0.15)
        out[s:e] += 0.8 * kick
        m = min(e - s, int(0.15 * SR))
        nz = rng.standard_normal(m) * np.exp(-np.arange(m) / (0.05 * SR))
        if (s // beat) % 2:
            out[s:s + m] += 0.4 * signal.sosfilt(signal.butter(2, [200, 5000], "bandpass", fs=SR, output="sos"), nz)
        h = s + beat // 2
        if h + m <= n:
            out[h:h + m] += 0.15 * signal.sosfilt(signal.butter(2, 7000, "highpass", fs=SR, output="sos"), nz)
    return out


def separate_proxy(wet: np.ndarray, rng: np.random.Generator, *, bleed_db: float = -12.0,
                   mask_jitter: float = 0.15) -> tuple[np.ndarray, np.ndarray]:
    """Mix `wet` with band bleed at `bleed_db` (RMS, relative), then 'separate' with a jittered ratio
    mask. Returns (separated guitar, per-sample confidence in [0, 1])."""
    n = wet.size
    bl = band_bleed(n, rng)
    bl *= np.sqrt(np.mean(wet ** 2)) / (np.sqrt(np.mean(bl ** 2)) + 1e-12) * 10 ** (bleed_db / 20)
    nper = 2048
    _, _, W = signal.stft(wet, SR, nperseg=nper)
    _, _, B = signal.stft(bl, SR, nperseg=nper)
    m = np.abs(W) / (np.abs(W) + np.abs(B) + 1e-12)
    mj = np.clip(m + rng.normal(0.0, mask_jitter, m.shape), 0.0, 1.0)
    _, y = signal.istft((W + B) * mj, SR, nperseg=nper)
    y = y[:n] if y.size >= n else np.pad(y, (0, n - y.size))
    w = np.abs(W) ** 2
    conf_f = (m * w).sum(axis=0) / (w.sum(axis=0) + 1e-12)        # energy-weighted mask per frame
    hop = nper // 2
    conf = np.interp(np.arange(n), np.arange(conf_f.size) * hop, conf_f)
    return y, conf
