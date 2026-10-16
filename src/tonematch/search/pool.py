"""Candidate-pool hygiene: time-variant blocks.

The Native block library flags a few models as time-variant (index field ``time_variant``; today
HD2_ReverbOcto, VIC_DelayGlitch, Victoria_ShufflingDelay): they render differently on every pass,
so a null-based loss between two renders of the same settings is not zero and the score is noise.
Search keeps them out of candidate pools unless asked (``allow_time_variant``), and says so when a
chain that contains one is scored anyway (or is the target's own chain).
"""
from __future__ import annotations

import logging

log = logging.getLogger("tonematch.search")

NULL_LOSS_WARNING = ("chain contains time-variant block(s) {models}: they render differently on every "
                     "pass, so the null-based loss is unreliable for this chain")


def time_variant_models(index: dict | None = None) -> set[str]:
    """Model ids flagged time-variant. With no ``index``, the library's own answer
    (``native_library.time_variant_models``: index flags plus the measured set, so a model with no
    template in the current library version, such as VIC_DelayGlitch in v1.2, still counts)."""
    if index is None:
        try:
            from tonematch.engines import native_library as NL
            return set(NL.time_variant_models())
        except Exception:  # noqa: BLE001 - no library on this machine: nothing is flagged
            return set()
    models = index.get("models", index)
    return {m for m, e in models.items() if isinstance(e, dict) and e.get("time_variant")}


def chain_time_variant(preset, tv: set[str]) -> list[str]:
    """Sorted time-variant model ids used by ``preset``'s slots."""
    return sorted({s.model_id for s in preset.slots if s.model_id in tv})


def filter_pool(models, allow_time_variant: bool = False, tv: set[str] | None = None) -> list[str]:
    """``models`` without the time-variant ones (order kept), unless ``allow_time_variant``."""
    tv = time_variant_models() if tv is None else tv
    return list(models) if allow_time_variant else [m for m in models if m not in tv]


def warn_if_time_variant(preset, tv: set[str] | None = None, what: str = "chain", emit=None) -> list[str]:
    """Log (and pass to ``emit``) that the null-based loss is unreliable when ``preset`` holds a
    time-variant block. Returns the offending model ids."""
    tv = time_variant_models() if tv is None else tv
    bad = chain_time_variant(preset, tv)
    if bad:
        msg = f"{what}: " + NULL_LOSS_WARNING.format(models=", ".join(bad))
        log.warning(msg)
        if emit is not None:
            emit(msg)
    return bad
