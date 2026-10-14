"""Test and smoke doubles: a numpy DSP-chain FakeEngine and a spectral-distance loss stub.

The stub honours the call shape of contracts/loss-kit.md (`score(target, candidate, *, stems, mask,
weights) -> Score` with total, terms, measured) but is NOT the real kit. Real scores come from
tonematch.metrics.LossKit (tone-metrics).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lfilter

from tonematch.engines.base import SAMPLE_RATE, Capabilities, Cost, EngineError


@dataclass
class StubScore:
    total: float
    terms: dict = field(default_factory=dict)
    measured: dict = field(default_factory=dict)


class SpectralStubLoss:
    """Log-magnitude distance between long-term average spectra (note-insensitive, like the real LTAS term)."""

    def __init__(self, n_fft: int = 2048):
        self.n_fft = n_fft

    def _ltas(self, x):
        x = np.asarray(x, np.float64)
        n = self.n_fft
        if len(x) < n:
            x = np.pad(x, (0, n - len(x)))
        frames = np.lib.stride_tricks.sliding_window_view(x, n)[:: n // 2] * np.hanning(n)
        mag = np.sqrt(np.mean(np.abs(np.fft.rfft(frames, axis=1)) ** 2, axis=0))
        return 20 * np.log10(mag + 1e-6)

    def score(self, target, candidate, *, stems=None, mask=None, weights=None):
        d = float(np.mean(np.abs(self._ltas(target) - self._ltas(candidate))))
        return StubScore(d, {"ltas_tilt": d})


class FakeEngine:
    """Fake Engine: drive (tanh) -> tone (one-pole lowpass) -> level, plus optional delay mix.

    Slots: 0 'drive' (params Gain, Tone, Level), 1 'delay' (Mix, Time) when `with_delay`.
    A chain is identified by model ids; `allowed_chains` mimics Native templates (others raise
    UnsupportedModelChange). Counts renders and loads for budget tests.
    """

    def __init__(self, seconds_per_render=0.01, parallelism=1, load_penalty_s=float("nan"),
                 engine_name="stomp", allowed_chains=None, param_steps=None, noise=0.0, seed=0):
        self._cost = Cost(seconds_per_render, parallelism, load_penalty_s)
        self.name, self.allowed, self.steps, self.noise = engine_name, allowed_chains, param_steps, noise
        self.params: dict = {}
        self.chain: tuple = ()
        self.renders = 0
        self.loads = 0
        self.rng = np.random.default_rng(seed)

    def load(self, preset) -> None:
        from tonematch.engines.base import UnsupportedModelChange

        key = tuple((s.category, s.model_id) for s in preset.slots)
        if self.allowed is not None and key not in self.allowed:
            raise UnsupportedModelChange(f"no template for {key}")
        self.chain, self.loads = key, self.loads + 1
        self.params = {(i, k): float(v) for i, s in enumerate(preset.slots) for k, v in s.params.items()}

    def set_params(self, changes) -> None:
        for r, v in changes.items():
            if self.steps:
                v = round(v * (self.steps - 1)) / (self.steps - 1)
            self.params[r] = float(v)

    def render(self, dry):
        self.renders += 1
        x = np.asarray(dry, np.float64)
        p = self.params
        gain = 1 + 20 * p.get((0, "Gain"), 0.5)
        y = np.tanh(gain * x)
        a = 0.02 + 0.95 * (1 - p.get((0, "Tone"), 0.5))      # one-pole lowpass coefficient
        y = lfilter([1 - a], [1, -a], y)
        y *= 0.1 + 1.9 * p.get((0, "Level"), 0.5)
        if any(m == "fake_delay" for _, m in self.chain):
            d = int(100 + 4000 * p.get((1, "Time"), 0.3))
            wet = np.concatenate([np.zeros(d), y[:-d]])
            y = y + p.get((1, "Mix"), 0.3) * wet
        if self.noise:
            y = y + self.noise * self.rng.standard_normal(len(y))
        if not np.all(np.isfinite(y)) or np.max(np.abs(y)) < 1e-9:
            raise EngineError("fake silence")
        return y.astype(np.float32)

    def cost(self) -> Cost:
        return self._cost

    def capabilities(self) -> Capabilities:
        return Capabilities(self.name, "fake", (), 3, {"param_steps": self.steps} if self.steps else {})

    def close(self) -> None:
        pass


def fake_dry(seconds=2.0, seed=0):
    """Band-limited noise bursts: stand-in for a DI."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    x = rng.standard_normal(n)
    env = np.repeat(rng.uniform(0.2, 1.0, n // 4800 + 1), 4800)[:n]
    return (0.3 * x * env).astype(np.float32)


def fake_preset(gain=0.5, tone=0.5, level=0.5, delay=None):
    from tonematch.preset import Preset, Slot

    slots = [Slot("drive", "fake_drive", {"Gain": gain, "Tone": tone, "Level": level}, True)]
    if delay is not None:
        slots.append(Slot("delay", "fake_delay", {"Mix": delay[0], "Time": delay[1]}, True))
    return Preset("fake", slots)
