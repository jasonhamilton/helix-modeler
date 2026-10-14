"""Stems container and the on-disk cache layout (cache/stems/<song-hash>/<model>/)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from tonematch.gpu import SAMPLE_RATE
from tonematch.separation.audio import write_wav
from tonematch.separation.models import CACHE_ROOT

STEMS_ROOT = CACHE_ROOT / "stems"


@dataclass
class Stems:
    song_hash: str
    model: str                      # separator name, or "user" for a supplied guitar stem
    mix: np.ndarray                 # [2, n] loudness-normalized, 48 kHz float32
    guitar: np.ndarray              # [2, n]
    others: np.ndarray              # [2, n] sum of every non-guitar stem (mix - guitar if user)
    parts: dict[str, np.ndarray] = field(default_factory=dict)  # per-stem, e.g. drums, bass
    backend: str = "-"
    wall_clock_s: float = 0.0
    sr: int = SAMPLE_RATE
    source: str = ""
    user_supplied: bool = False
    others_approximate: bool = False
    gain_db: float = 0.0
    cache_dir: Path | None = None
    cached: bool = False            # True when loaded from cache rather than computed now

    @property
    def n_samples(self) -> int:
        return self.mix.shape[1]


def cache_dir_for(song_hash: str, model: str, root: Path | None = None) -> Path:
    return (root or STEMS_ROOT) / song_hash / model


def save(stems: Stems, root: Path | None = None) -> Path:
    d = cache_dir_for(stems.song_hash, stems.model, root)
    d.mkdir(parents=True, exist_ok=True)
    write_wav(d / "mix.wav", stems.mix)
    write_wav(d / "guitar.wav", stems.guitar)
    write_wav(d / "others.wav", stems.others)
    for name, x in stems.parts.items():
        if name != "guitar":
            write_wav(d / f"{name}.wav", x)
    manifest = {
        "source": stems.source, "song_hash": stems.song_hash, "model": stems.model,
        "backend": stems.backend, "sample_rate": stems.sr, "wall_clock_s": stems.wall_clock_s,
        "stems": sorted({"mix", "guitar", "others", *[k for k in stems.parts if k != "guitar"]}),
        "user_supplied": stems.user_supplied, "others_approximate": stems.others_approximate,
        "loudness_gain_db": stems.gain_db, "samples": stems.n_samples,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    stems.cache_dir = d
    return d


def load(song_hash: str, model: str, root: Path | None = None) -> Stems | None:
    d = cache_dir_for(song_hash, model, root)
    mf = d / "manifest.json"
    if not mf.exists():
        return None
    m = json.loads(mf.read_text())

    def rd(name: str) -> np.ndarray:
        x, _ = sf.read(str(d / f"{name}.wav"), dtype="float32", always_2d=True)
        return np.ascontiguousarray(x.T)

    parts = {n: rd(n) for n in m["stems"] if n not in ("mix", "guitar", "others")}
    return Stems(
        song_hash=song_hash, model=model, mix=rd("mix"), guitar=rd("guitar"), others=rd("others"),
        parts=parts, backend=m["backend"], wall_clock_s=m["wall_clock_s"], sr=m["sample_rate"],
        source=m["source"], user_supplied=m["user_supplied"],
        others_approximate=m["others_approximate"], gain_db=m["loudness_gain_db"],
        cache_dir=d, cached=True)
