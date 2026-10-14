"""separate() and recombine_and_reseparate()."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from tonematch.separation import stems as stems_mod
from tonematch.separation.audio import align_to, file_hash, load_audio, normalize, to_stereo
from tonematch.separation.models import DEFAULT_MODEL, get_separator
from tonematch.separation.stems import Stems


def separate(song: str | Path, stem: str | Path | None = None, model: str = DEFAULT_MODEL,
             backend: str | None = None, cache_root: Path | None = None,
             use_cache: bool = True) -> Stems:
    """Decode `song`, normalize, and return Stems (cached by content hash and model).

    With `stem` (a guitar stem), separation is skipped; the mix is still required. The stem is
    aligned to the mix by cross-correlation, and `others` becomes mix minus guitar (approximate).
    """
    song = Path(song)
    song_hash = file_hash(song)
    name = "user" if stem is not None else model
    if stem is not None:  # a different stem is a different result
        song_hash = f"{song_hash}-{file_hash(stem)[:8]}"
    if use_cache and (hit := stems_mod.load(song_hash, name, cache_root)) is not None:
        return hit

    mix, gain_db = normalize(load_audio(song))
    t0 = time.perf_counter()
    if stem is not None:
        g = load_audio(stem) * np.float32(10 ** (gain_db / 20))  # same gain keeps mix-minus-stem sane
        g, _ = align_to(mix, g)
        out = Stems(song_hash, "user", mix, g, mix - g, {}, backend="-", user_supplied=True,
                    others_approximate=True)
    else:
        sep = get_separator(model, backend)
        parts, used = sep.separate(mix)
        g = parts[sep.guitar_stem]
        others = sum((v for k, v in parts.items() if k != sep.guitar_stem), np.zeros_like(g))
        out = Stems(song_hash, model, mix, g, others.astype(np.float32), parts, backend=used)
    out.wall_clock_s = time.perf_counter() - t0
    out.source, out.gain_db = str(song), gain_db
    stems_mod.save(out, cache_root)
    return out


def recombine_and_reseparate(candidate: np.ndarray, stems: Stems, backend: str | None = None
                             ) -> np.ndarray:
    """Separation-consistent comparison: mix the candidate guitar render [2, n] (48 kHz, same
    loudness scale as the stems) with the song's other stems, separate again with the same model,
    and return the re-separated guitar. One extra separation; use on final candidates only.
    A user-supplied stem has no model, so it re-separates with the default model.
    """
    cand = to_stereo(candidate if candidate.ndim == 2 else candidate[None]).astype(np.float32)
    n = stems.n_samples
    cand = cand[:, :n] if cand.shape[1] >= n else np.pad(cand, ((0, 0), (0, n - cand.shape[1])))
    model = DEFAULT_MODEL if stems.model == "user" else stems.model
    sep = get_separator(model, backend or (stems.backend if stems.backend != "-" else None))
    parts, _ = sep.separate(cand + stems.others)
    return parts[sep.guitar_stem]
