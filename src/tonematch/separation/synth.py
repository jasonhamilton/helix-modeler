"""Short synthetic mixes with known stems, for tests and the separate smoke run. No real audio.

16 bars at 120 bpm (32 s, 48 kHz stereo). Bars 1-2: guitar alone (an exposed section). Bars 1-8
guitar is a clean plucked string; bars 9-16 it is distorted, brighter and louder (a second tone).
Drums and bass enter at bar 3. The guitar is Karplus-Strong, not a real instrument, so a trained
separator may not label it "guitar"; quality here says nothing about real songs.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

SR = 48_000
BPM = 120.0


def _pluck(freq: float, dur: float, rng: np.random.Generator) -> np.ndarray:
    n = int(dur * SR)
    period = round(SR / freq)
    a = np.zeros(period + 2)
    a[0], a[period], a[period + 1] = 1.0, -0.498, -0.498
    x = np.zeros(n)
    x[:period] = rng.uniform(-1, 1, period)
    return lfilter([1.0], a, x)


def make_mix(seed: int = 0) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    rng = np.random.default_rng(seed)
    beat, bar = 60.0 / BPM, 4 * 60.0 / BPM
    n = int(16 * bar * SR)
    guitar, drums, bass = (np.zeros(n) for _ in range(3))
    riff = [82.41, 98.0, 110.0, 98.0, 123.47, 110.0, 98.0, 82.41]  # E2.. eighth notes
    for b in range(16):
        dist = b >= 8
        for i, f in enumerate(riff):
            s = int((b * bar + i * beat / 2) * SR)
            note = _pluck(f * (2 if dist else 1), beat / 2 + 0.3, rng)
            if dist:
                note = np.tanh(6.0 * note / (np.abs(note).max() + 1e-9)) * 0.9
            else:
                note = note / (np.abs(note).max() + 1e-9) * 0.3
            e = min(n, s + len(note))
            guitar[s:e] += note[:e - s]
        if b >= 2:
            for k in range(4):
                s = int((b * bar + k * beat) * SR)
                t = np.arange(int(0.25 * SR)) / SR
                kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-t * 40)) * t) * np.exp(-t * 12)
                drums[s:s + len(kick)] += 0.6 * kick
                for h in (0, 1):
                    hs = s + int(h * beat / 2 * SR)
                    hat = rng.uniform(-1, 1, int(0.04 * SR)) * np.exp(-np.arange(int(0.04 * SR)) / 400)
                    drums[hs:hs + len(hat)] += 0.15 * hat
            t = np.arange(int(bar * SR)) / SR
            s = int(b * bar * SR)
            bass[s:s + len(t)] += 0.35 * np.sin(2 * np.pi * 55.0 * t) * (0.6 + 0.4 * np.sin(np.pi * t / bar))
    pan = np.array([[0.8], [0.6]])
    g = (guitar[None] * pan).astype(np.float32)
    others = (np.stack([drums + bass, drums + bass]) * 0.8).astype(np.float32)
    mix = g + others
    return mix.astype(np.float32), {"guitar": g, "others": others, "drums": drums[None].repeat(2, 0).astype(np.float32), "bass": bass[None].repeat(2, 0).astype(np.float32)}
