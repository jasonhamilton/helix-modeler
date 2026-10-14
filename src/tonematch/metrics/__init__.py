"""Loss kit (plan Step 6, losses). Contract: contracts/loss-kit.md. Owner: tone-metrics.

    from tonematch.metrics import LossKit
    s = LossKit().score(target, candidate, weights={"ltas_tilt": 1.0})
    s.total, s.terms, s.measured

Each term is its own module (`ltas`, `crest`, `envelope`, `mrstft`) with `term(target, candidate,
mask=None) -> float`; the feature terms also expose `features(x, mask)` and `distance(a, b)`.
"""
from tonematch.metrics.kit import (
    DEFAULT_WEIGHTS,
    IMPLEMENTED,
    NO_DI_TERMS,
    PROVISIONAL_WEIGHTS,
    TERMS,
    Audio,
    LossKit,
    Mask,
    Score,
    Stems,
)

__all__ = ["DEFAULT_WEIGHTS", "IMPLEMENTED", "NO_DI_TERMS", "PROVISIONAL_WEIGHTS", "TERMS", "Audio",
           "LossKit", "Mask", "Score", "Stems"]
