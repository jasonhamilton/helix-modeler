"""`LossKit.score`, the one call of contracts/loss-kit.md (v0).

Implemented in v0: `ltas_tilt`, `crest_flatness_hnr`, `envelope` (no DI needed, content-invariant)
and `mrstft` (needs an aligned DI). `embed_afxrep` (AFx-Rep learned embedding, T032) is implemented
but heavy and optional (the `embed` extra plus a checkpoint in cache/models/afx-rep/): it is computed
only when its weight is non-zero, and raises `afxrep.AfxRepUnavailable` if the model is missing. The
other contract names are reserved: asking for one with a non-zero weight raises
`NotImplementedError`; with weight 0 it is skipped.

Admission rule: every term is weighted 0 in `DEFAULT_WEIGHTS` until `loss-trust-check` passes it on
the synthetic set at `benchmark`'s threshold. Scores with the default weights therefore have
`total == 0` and only the breakdown in `terms` is informative. A caller that needs a usable total
before any term is admitted (search on proxy data, development) must pass weights explicitly, e.g.
`PROVISIONAL_WEIGHTS`, which are NOT admitted and say so.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from tonematch.metrics import afxrep, crest, envelope, ltas, mrstft, mrstft_v2
from tonematch.metrics._common import as_mono

Audio = np.ndarray
Mask = np.ndarray          # per-sample separator confidence in [0, 1], same length as the target
Stems = dict[str, np.ndarray]  # the song's other stems for `sepcons`, name -> 48 kHz mono

TERMS: tuple[str, ...] = (
    "ltas_tilt", "crest_flatness_hnr", "envelope", "embed_afxrep", "embed_fxenc", "embed_openamp",
    "delay_echo", "reverb_tail", "mod_periodicity", "mrstft", "sepcons", "mrstft_v2",
)
# terms with features(x, mask) / distance(a, b): target features are cached across calls
_FEATURE_TERMS = {ltas.NAME: ltas, crest.NAME: crest, envelope.NAME: envelope}
_ALIGNED_TERMS = {mrstft.NAME: mrstft, mrstft_v2.NAME: mrstft_v2}
# learned embeddings: feature terms too (target embedding cached), but only run at non-zero weight
_EMBED_TERMS = {afxrep.NAME: afxrep}
NO_DI_TERMS: tuple[str, ...] = tuple(_FEATURE_TERMS)      # the cheap numpy DSP no-DI terms
EMBED_TERMS: tuple[str, ...] = tuple(_EMBED_TERMS)
DSP_TERMS: tuple[str, ...] = (*_FEATURE_TERMS, *_ALIGNED_TERMS)
IMPLEMENTED: tuple[str, ...] = (*DSP_TERMS, *EMBED_TERMS)

# Admission rule (contracts/loss-kit.md): 0 until loss-trust-check passes on synthetic-v<N>.
# mrstft is absent because it is only valid with an aligned DI; ask for it explicitly.
# embed_afxrep is listed at 0 but, being heavy and optional, is skipped while its weight is 0.
DEFAULT_WEIGHTS: dict[str, float] = {name: 0.0 for name in (*NO_DI_TERMS, *EMBED_TERMS)}

# NOT ADMITTED. Equal weights in each term's own scale units; for development and proxy runs only.
PROVISIONAL_WEIGHTS: dict[str, float] = {name: 1.0 for name in NO_DI_TERMS}

# ADMITTED for the known-DI mode only (a sample-aligned DI of the same performance, e.g. Gate 2's
# synthetic-v1 recovery): mrstft_v2 2.0 passed loss-trust-check on helix-trust-v2 test
# aligned/same_notes at 0.990 under rule B (bar 0.90; lead 2026-10-16). Not valid without an aligned DI.
KNOWN_DI_WEIGHTS: dict[str, float] = {"mrstft_v2": 1.0}


@dataclass
class Score:
    total: float                                         # lower is better
    terms: dict[str, float] = field(default_factory=dict)
    measured: dict[str, float] = field(default_factory=dict)  # delay/reverb/mod starting values


def _digest(x: np.ndarray, mask: np.ndarray | None) -> str:
    h = hashlib.blake2b(np.ascontiguousarray(x, dtype=np.float64).tobytes(), digest_size=16)
    h.update(str(x.shape).encode())
    if mask is not None:
        h.update(np.ascontiguousarray(mask, dtype=np.float64).tobytes())
    return h.hexdigest()


class LossKit:
    """Compare a target guitar stem with a candidate render. Stateless apart from a small cache of
    target features, so scoring many candidates against one target costs one target analysis."""

    def __init__(self, default_weights: dict[str, float] | None = None, cache_size: int = 8):
        self.default_weights = dict(DEFAULT_WEIGHTS if default_weights is None else default_weights)
        self._check_names(self.default_weights)
        self._cache: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._cache_size = cache_size

    @staticmethod
    def _check_names(weights: dict[str, float]) -> None:
        unknown = set(weights) - set(TERMS)
        if unknown:
            raise KeyError(f"unknown loss terms {sorted(unknown)}; names are fixed in contracts/loss-kit.md")

    def _target_features(self, name: str, x: np.ndarray, mask: np.ndarray | None):
        key = (name, _digest(x, mask))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        f = {**_FEATURE_TERMS, **_EMBED_TERMS}[name].features(x, mask)
        self._cache[key] = f
        while len(self._cache) > self._cache_size * (len(_FEATURE_TERMS) + len(_EMBED_TERMS)):
            self._cache.popitem(last=False)
        return f

    def score(self, target: Audio, candidate: Audio, *, stems: Stems | None = None,
              mask: Mask | None = None, weights: dict[str, float] | None = None) -> Score:
        w = self.default_weights if weights is None else dict(weights)
        self._check_names(w)
        t = as_mono(target, "target")
        c = as_mono(candidate, "candidate")
        if mask is not None and np.asarray(mask).reshape(-1).size != t.size:
            raise ValueError("mask must have one value per target sample")
        terms: dict[str, float] = {}
        total = 0.0
        for name, wt in w.items():
            if name in _FEATURE_TERMS:
                mod = _FEATURE_TERMS[name]
                v = mod.distance(self._target_features(name, t, mask), mod.features(c))
            elif name in _ALIGNED_TERMS:
                v = _ALIGNED_TERMS[name].term(t, c, mask)
            elif name in _EMBED_TERMS and wt != 0:
                mod = _EMBED_TERMS[name]
                v = mod.distance(self._target_features(name, t, mask), mod.features(c))
            elif wt == 0:
                continue
            elif name == "sepcons" and stems is None:
                raise ValueError("sepcons needs the song's other stems (stems=...)")
            else:
                raise NotImplementedError(f"loss term {name!r} is not implemented in loss kit v0")
            terms[name] = float(v)
            total += float(wt) * float(v)
        return Score(total=float(total), terms=terms, measured={})
