"""The Native single-block library (T048): one captured template per Stomp-compatible model.

Every Stomp-compatible block model of the HX Edit catalog (routing blocks and models Native
refuses left out) is captured once, alone, at catalog defaults with the frozen parameters
(``param_policy``: amp Hum and Ripple 0 on both engines, cab EarlyReflections 0 for the Stomp)
written in, and every parameter plus the bypass assigned to a MIDI CC. Search picks a model per
slot by loading ``block_preset(model_id)`` against ``template_for(model_id)`` and driving the CCs.

Layout (local, gitignored; Line 6 state, never commit)::

    cache/native/library/<version>/
        index.json            model id -> category, template stem, CC counts, frozen handling
        templates/<stem>.json  ChainTemplate meta (map, ranges, verified, baked)
        templates/<stem>.state Native state captured from the drop
        refs/<stem>.npz        capture-time render (test_signal(1 s) after 3 s pre-roll) + CC values

``templates/`` is an ordinary template store: ``NativeEngine(templates_dir=library_templates())``
loads any ``block_preset(model_id)`` from it. The host itself lives in a child process
(``native_host``, pedalboard, GPLv3) and is never imported here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .native import (
    CC_RANGE_RULES_VERSION,
    ChainTemplate,
    baked_fingerprint,
    cache_dir,
    chain_key,
    out_of_range,
    template_stem,
)

# v1.1 (T072, 2026-10-16): v1 with the delay, reverb, modulation and other templates whose time
# or rate params get a narrower CC range (``native.narrow_range``) re-captured. v1 stays on disk.
VERSION = "block-library-v1.1"
# Breadth first (human, 2026-10-15/16): Distortion, Delay, Reverb; then Dynamics, EQ, Modulation;
# then amps, cabs and the rest. Within a tier the categories are interleaved one model at a time.
TIERS = (("drive", "delay", "reverb"), ("comp", "eq", "mod"), ("amp", "cab", "other"))
BLOCK_TYPE = {"amp": 1, "cab": 2, "delay": 7, "reverb": 7}
_ROOT = Path(__file__).resolve().parents[3]
BASE_HLX = _ROOT / "cache" / "stomp" / "inject" / "K.hlx"  # globals only; the slot is replaced


def library_dir(version: str = VERSION) -> Path:
    return cache_dir() / "library" / version


def library_templates(version: str = VERSION) -> Path:
    return library_dir(version) / "templates"


def is_routing(model_id: str) -> bool:
    """Not blocks a file can hold alone: split, join, input and output models (path structure),
    and the IR cabs' pan twins (``...WithPan``, the second cab of a dual cab block; dropped alone
    Native answers "Object not found", code -1000021, 2026-10-16)."""
    return "AppDSPFlow" in model_id or model_id.endswith("WithPan")


def frozen_values() -> dict[tuple[str, str], float]:
    """Frozen values of both engine policies together (they agree where they overlap)."""
    from tonematch import param_policy

    out: dict[tuple[str, str], float] = {}
    for eng in ("native", "stomp"):
        for k, v in param_policy.frozen_for(eng).items():
            if k in out and out[k] != v:
                raise ValueError(f"policies disagree on {k}: {out[k]} vs {v}")
            out[k] = v
    return out


def library_models(catalog: dict, tiers=TIERS, skip: set[str] | None = None) -> list[str]:
    """Stomp models in capture order (breadth first within each tier); routing models and
    ``skip`` (default: ``catalog.native_unavailable()``) left out."""
    if skip is None:
        from tonematch.catalog import native_unavailable

        skip = native_unavailable()
    models = catalog["models"]
    out: list[str] = []
    for tier in tiers:
        by_cat = [sorted(k for k, v in models.items() if v.get("stomp") and v.get("category") == c
                         and not is_routing(k) and k not in skip) for c in tier]
        for i in range(max((len(b) for b in by_cat), default=0)):
            out += [b[i] for b in by_cat if i < len(b)]
    return out


def single_block(model_id: str, catalog: dict, base) -> object:
    """One block at catalog defaults, mono when the model has a mono form, on path 0."""
    from tonematch.preset import Slot

    m = catalog["models"][model_id]
    params, extra = {}, {}
    for name, p in (m.get("params") or {}).items():
        if p.get("default") is not None:
            (extra if name.startswith("@") else params)[name] = p["default"]
    if m.get("stereo") and m.get("mono"):
        extra["@stereo"] = False
    extra = {k: v for k, v in extra.items() if k not in ("@cab", "@type", "@position", "@enabled")}
    extra["@type"] = BLOCK_TYPE.get(m["category"], 0)
    extra.setdefault("@path", 0)
    p = copy.deepcopy(base)
    p.name, p.snapshots = f"LIB {model_id[4:][:14]}", []
    p.slots = [Slot(category=m["category"], model_id=model_id, params=params, enabled=True,
                    key="block0", position=0, extra=extra)]
    return p


def base_preset(path: Path | None = None):
    from tonematch.preset import read_hlx

    for f in (path, BASE_HLX, _ROOT / "fixtures" / "smoke" / "known.hlx"):
        if f is not None and Path(f).is_file():
            return read_hlx(f)
    raise FileNotFoundError("no base .hlx for the block library (cache/stomp/inject/K.hlx or fixtures/smoke/known.hlx)")


def block_preset(model_id: str, catalog: dict | None = None, base=None, frozen=None):
    """The library's preset for ``model_id``: ``single_block`` with the frozen values written in.
    Search starts from this (then moves CC-mapped params); its baked values match the template."""
    if catalog is None:
        from tonematch.catalog import load_catalog

        catalog = load_catalog()
    p = single_block(model_id, catalog, base if base is not None else base_preset())
    fz = frozen_values() if frozen is None else frozen
    for s in p.slots:
        for (cat, name), v in fz.items():
            if s.category == cat and name in (s.params or {}):
                s.params[name] = v
    return p


def load_index(version: str = VERSION) -> dict:
    try:
        return json.loads((library_dir(version) / "index.json").read_text())
    except (OSError, ValueError):
        return {"version": version, "models": {}, "failed": {}}


def save_index(idx: dict, version: str = VERSION) -> Path:
    d = library_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "index.json"
    p.write_text(json.dumps(idx, indent=1, sort_keys=True))
    return p


def _read_template(d: Path, stem: str) -> ChainTemplate | None:
    j, s = d / f"{stem}.json", d / f"{stem}.state"
    if not (j.is_file() and s.is_file()):
        return None
    meta = json.loads(j.read_text())
    return ChainTemplate(tuple(tuple(x) for x in meta["chain"]), s.read_bytes(), meta.get("map", {}),
                         meta.get("ranges", {}), meta.get("name", stem), meta.get("verified", []),
                         meta.get("baked", {}), meta.get("baked_hash", ""))


def template_for(model_id: str, version: str = VERSION) -> ChainTemplate | None:
    """The library template for ``model_id`` (None if not captured)."""
    e = load_index(version)["models"].get(model_id)
    return _read_template(library_templates(version), e["template"]) if e else None


def library(version: str = VERSION) -> dict[str, ChainTemplate]:
    """Every captured model id -> its template."""
    d = library_templates(version)
    out = {}
    for mid, e in load_index(version)["models"].items():
        t = _read_template(d, e["template"])
        if t is not None:
            out[mid] = t
    return out


def entry(model_id: str, category: str, tpl: ChainTemplate, preset) -> dict:
    """Index entry: which frozen params the template drives by CC (verified) and which it bakes."""
    fz = frozen_values()
    frz = [f"0:{n}" for (c, n) in fz if c == category and n in (preset.slots[0].params or {})]
    return {"category": category, "template": template_stem(tpl), "name": tpl.name,
            "ccs": len(tpl.map), "verified": len(tpl.verified),
            "params": len(preset.slots[0].params or {}),
            "unverified": sorted(r for r in tpl.map if r not in tpl.verified),
            "frozen_verified": sorted(r for r in frz if r in tpl.verified),
            "frozen_baked": sorted(r for r in frz if r not in tpl.verified),
            "cc_ranges": narrowed_ranges(tpl, preset), "cc_range_rules": CC_RANGE_RULES_VERSION}


def narrowed_ranges(tpl: ChainTemplate, preset, catalog: dict | None = None) -> dict[str, list[float]]:
    """``tpl``'s CC ranges that differ from the catalog range (T072): ``ref -> [lo, hi]``."""
    from .native import catalog_ranges

    if not tpl.ranges:
        return {}
    full = catalog_ranges(preset, catalog) or {}
    return {r: list(v) for r, v in sorted(tpl.ranges.items())
            if r in full and [float(x) for x in full[r]] != [float(x) for x in v]}


def matches(tpl: ChainTemplate, preset) -> bool:
    """True when ``tpl`` can render ``preset``: same structure, same baked values, and every
    CC-driven value inside the template's CC range (narrowed ranges, T072)."""
    return tpl.chain == chain_key(preset) and bool(tpl.baked_hash) and \
        baked_fingerprint(preset, tpl.map, tpl.verified)[1] == tpl.baked_hash and \
        not out_of_range(tpl, preset)
