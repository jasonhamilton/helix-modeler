"""Guitar isolation and section segmentation (plan Steps 1 and 2)."""
from tonematch.separation.confidence import Mask, confidence_mask
from tonematch.separation.core import recombine_and_reseparate, separate
from tonematch.separation.models import (
    DEFAULT_MODEL,
    ROFORMER_MODELS,
    SeparatorUnavailable,
    get_separator,
)
from tonematch.separation.segment import SectionMap, segment
from tonematch.separation.stems import Stems

__all__ = ["DEFAULT_MODEL", "ROFORMER_MODELS", "Mask", "SectionMap", "SeparatorUnavailable",
           "Stems", "confidence_mask", "get_separator", "recombine_and_reseparate", "segment",
           "separate"]
