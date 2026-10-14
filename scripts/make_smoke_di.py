#!/usr/bin/env python3
"""Generate fixtures/smoke/di_2s.wav: a SYNTHETIC Karplus-Strong plucked-string clip (not a real guitar).

Deterministic (seed 8), 2 s, 48 kHz, float32 mono, open E-A-D-G chord arpeggio, peak about -12 dBFS.
Placeholder until a licence-clean real DI clip is supplied. See fixtures/smoke/PROVENANCE.md.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

SR = 48_000


def pluck(freq: float, dur: float, rng: np.random.Generator) -> np.ndarray:
    n = int(dur * SR)
    d = round(SR / freq)
    buf = rng.uniform(-1, 1, d)
    out = np.empty(n)
    for i in range(n):
        out[i] = buf[i % d]
        buf[i % d] = 0.4985 * (buf[i % d] + buf[(i + 1) % d])
    return out


def write_wav_f32(path: Path, x: np.ndarray, sr: int = SR) -> None:
    data = x.astype("<f4").tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 3, 1, sr, sr * 4, 4, 32)
    path.write_bytes(hdr + fmt + b"data" + struct.pack("<I", len(data)) + data)


def main() -> None:
    rng = np.random.default_rng(8)
    y = np.zeros(2 * SR)
    for k, f in enumerate([82.41, 110.0, 146.83, 196.0, 246.94, 329.63]):
        s = int(k * 0.12 * SR)
        p = pluck(f, 2.0, rng)[: len(y) - s]
        y[s:s + len(p)] += p
    y *= 10 ** (-12 / 20) / np.max(np.abs(y))
    y[-480:] *= np.linspace(1, 0, 480)
    out = Path(__file__).resolve().parents[1] / "fixtures" / "smoke" / "di_2s.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav_f32(out, y.astype(np.float32))
    print(out)


if __name__ == "__main__":
    main()
