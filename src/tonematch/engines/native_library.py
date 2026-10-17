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
        on_demand/templates/   full-range variants captured on demand (T098), listed in
                               index.json under "on_demand"

Values outside the narrowed CC ranges (T098, 2026-10-16). v1.1 and v1.2 narrow delay times,
LFO rates, reverb Decay (v1.1) and Block Level (v1.2) to finer CC grids; a template refuses a
value outside its grid rather than clamp it. A preset holding such a value loads through the
finest template that does hold it, searched in this order: the version's own templates, the
older versions on disk (``fallback_versions``: v1.1 keeps Level at -60..+6 dB, v1 every catalog
range), then ``on_demand/``. When none holds it, ``NativeEngine`` captures a variant whose CC
covers the catalog range for the out-of-range params (other params keep the narrowed grid),
saves it under ``on_demand/`` and records it in the index (``capture_on_demand``).
``param_ranges(model_id)`` / ``ChainTemplate.param_ranges`` give search both ranges per param.

``templates/`` is an ordinary template store: ``NativeEngine(templates_dir=library_templates())``
loads any ``block_preset(model_id)`` from it. The host itself lives in a child process
(``native_host``, pedalboard, GPLv3) and is never imported here.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import time
from pathlib import Path

from .native import (
    CC_RANGE_RULES_VERSION,
    ChainTemplate,
    baked_fingerprint,
    cache_dir,
    chain_key,
    dir_variants,
    out_of_range,
    range_width_key,
    template_stem,
)

# v1.1 (T072, 2026-10-16): v1 with the delay, reverb, modulation and other templates whose time
# or rate params get a narrower CC range (``native.narrow_range``) re-captured. v1 stays on disk.
# v1.2 (engine push N6d, 2026-10-16): v1.1 with the 278 templates holding a -60..+6 dB Block Level
# re-captured under cc-range-v2 (Level on [-25.75, +6] dB). VIC_DelayGlitch has no v1.2 template
# (time-variant across hosts). v1 and v1.1 stay on disk. TONEMATCH_NATIVE_LIBRARY picks another.
VERSION = os.environ.get("TONEMATCH_NATIVE_LIBRARY") or "block-library-v1.2"
# Newest first. A version's fallbacks are the ones after it (wider CC ranges, same captures).
LADDER = ("block-library-v1.2", "block-library-v1.1", "block-library-v1")
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


# Measured on Native 3.82 (T048, ``native_block_library.py time-variant``): the same host cannot
# repeat a render of these (same-host repeat null -68.3, -6.5 and -11.3 dB on v1.1), so they
# cannot be scored by a null or exact-render loss. Kept as the fallback when no index is cached;
# the index flag ``time_variant`` is the measurement and is always unioned in.
TIME_VARIANT_NATIVE_382 = frozenset({"HD2_ReverbOcto", "VIC_DelayGlitch", "Victoria_ShufflingDelay",
                                     "HD2_DelayADT"})  # ADT: repeat -44.5 dB (T103, lead 2026-10-17)


def time_variant_models(version: str = VERSION) -> frozenset[str]:
    """Model ids whose Native render does not repeat (random or free-running blocks): the
    library index's ``time_variant`` flags plus ``TIME_VARIANT_NATIVE_382``. The one source for
    search (opt-in model choice), benchmark (excluded from null-based parity) and tone-metrics."""
    idx = load_index(version)
    return TIME_VARIANT_NATIVE_382 | {m for m, e in idx.get("models", {}).items() if e.get("time_variant")}


def is_time_variant(model_id: str, version: str = VERSION) -> bool:
    """True when ``model_id``'s Native render is time-variant (see ``time_variant_models``)."""
    return model_id in time_variant_models(version)


def fingerprint_dir(version: str = VERSION) -> Path:
    """Block-probe fingerprints of this library (T047, ``scripts/native_block_fingerprints.py``)."""
    return cache_dir().parent / "probe" / "fingerprints" / version


def fingerprint_index(version: str = VERSION) -> dict:
    """``index.json`` of the fingerprints: model -> category, template, and per grid setting its
    params, CC values and summary scalars (empty ``models`` if not computed yet)."""
    try:
        return json.loads((fingerprint_dir(version) / "index.json").read_text())
    except (OSError, ValueError):
        return {"models": {}}


def fingerprint_set(model_id: str, version: str = VERSION) -> dict | None:
    """The full fingerprint set of ``model_id`` (schema ``tonematch.block-fingerprint-set`` v1;
    each setting holds a ``tonematch.block-fingerprint`` v1), or None."""
    try:
        return json.loads((fingerprint_dir(version) / f"{model_id}.json").read_text())
    except (OSError, ValueError):
        return None


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


# T103 setting-truth audit (2026-10-17, scripts/native_verify.py landing): CCs that Native 3.82
# does not land on the commanded value, so they are never driven. The template treats them as
# baked at the library value. Search cannot free them and ``set_params`` refuses to move them.
# (Native's 1-float32-ulp CC rounding on other params is not listed: it is a precision effect,
# reported with the audit.)
CC_UNRELIABLE: dict[tuple[str, str], str] = {
    ("HD2_ReverbNonLinear", "Mod"): "its CC matches no drop of any Mod value (-13.3 dB against every candidate)",
    ("SynthLead", "Mix"): "Native quantizes the CC value to 0.01 (step 80 lands on 0.63, not 0.629921; -70 dB)",
}
# FX Loop, Send and Return blocks have no loop on Native. Their bypass CC plus a param CC miss the
# drop truth by -9 to -45 dB. Not searchable on Native (proposal to search, T103).
IO_BLOCK_MARKERS = ("FXLoop", "_Send", "_Return")


def is_io_block(model_id: str) -> bool:
    return any(k in model_id for k in IO_BLOCK_MARKERS)


def unverifiable_models(version: str = VERSION) -> dict[str, str]:
    """Models whose Native renders cannot be verified against drop truth, with the reason (T103):
    time-variant blocks and the FX Loop / Send / Return blocks. A proposal for search and
    benchmark pools; nothing in the engine refuses them."""
    idx = load_index(version)
    out = {m: "time-variant (renders do not repeat)" for m in time_variant_models(version)}
    out.update({m: "I/O block: no loop on Native, bypass CC + param CC miss drop truth (-9 to -45 dB)"
                for m in idx.get("models", {}) if is_io_block(m)})
    return out


def apply_cc_policy(tpl: ChainTemplate | None) -> ChainTemplate | None:
    """``tpl`` with any ``CC_UNRELIABLE`` param of a single-block library template moved from
    ``verified`` to baked at the library preset's value (baked values and hash recomputed)."""
    if tpl is None or len([1 for _, m in tpl.chain if m]) != 1:
        return tpl
    mid = next(m for _, m in tpl.chain if m)
    bad = [f"0:{n}" for (m, n) in CC_UNRELIABLE if m == mid and f"0:{n}" in tpl.verified]
    if not bad:
        return tpl
    from .native import baked_fingerprint

    tpl.verified = [r for r in tpl.verified if r not in bad]
    tpl.baked, tpl.baked_hash = baked_fingerprint(block_preset(mid), tpl.map, tpl.verified)
    return tpl


def _read_template(d: Path, stem: str) -> ChainTemplate | None:
    j, s = d / f"{stem}.json", d / f"{stem}.state"
    if not (j.is_file() and s.is_file()):
        return None
    meta = json.loads(j.read_text())
    return apply_cc_policy(ChainTemplate(tuple(tuple(x) for x in meta["chain"]), s.read_bytes(), meta.get("map", {}),
                                         meta.get("ranges", {}), meta.get("name", stem), meta.get("verified", []),
                                         meta.get("baked", {}), meta.get("baked_hash", "")))


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


# --------------------------------------------------------------------------- full-range values (T098)


def fallback_versions(version: str = VERSION) -> list[str]:
    """Older library versions on disk whose templates ``version`` may fall back to for values
    outside its narrowed CC ranges (``LADDER`` order, newest first)."""
    if version not in LADDER:
        return []
    return [v for v in LADDER[LADDER.index(version) + 1:] if library_templates(v).is_dir()]


def on_demand_templates(version: str = VERSION) -> Path:
    """Where on-demand full-range variants of ``version`` are saved."""
    return library_dir(version) / "on_demand" / "templates"


def version_of(templates_dir) -> str | None:
    """The library version whose ``templates/`` (or ``on_demand/templates/``) is ``templates_dir``."""
    if templates_dir is None:
        return None
    d = Path(templates_dir).resolve()
    root = (cache_dir() / "library").resolve()
    for cand in (d.parent, d.parent.parent):
        if cand.parent == root and d.name == "templates":
            return cand.name
    return None


def fallback_template_dirs(templates_dir) -> list[Path]:
    """Template dirs ``NativeEngine(templates_dir=...)`` consults for out-of-range values: the
    older versions' ``templates/`` then this version's ``on_demand/templates/`` ([] off the library)."""
    v = version_of(templates_dir)
    if v is None or Path(templates_dir).resolve() == on_demand_templates(v).resolve():
        return []
    return [library_templates(f) for f in fallback_versions(v)] + [on_demand_templates(v)]


def candidates(preset, version: str = VERSION) -> tuple[list[ChainTemplate], list[ChainTemplate]]:
    """(primary, fallback) templates for ``preset``'s chain: ``version``'s own, then its older
    versions' and on-demand ones. Fallbacks are empty when the primary store lacks the chain."""
    key = chain_key(preset)
    prim = dir_variants(library_templates(version), key)
    if not prim:
        return [], []
    extra = [t for d in fallback_template_dirs(library_templates(version)) for t in dir_variants(d, key)]
    return prim, extra


def match(preset, version: str = VERSION) -> ChainTemplate | None:
    """The template ``NativeEngine`` renders ``preset`` with from this library: the finest CC
    grid among the version's templates that ``matches``; else among its fallbacks; else None."""
    prim, extra = candidates(preset, version)
    for group in (prim, extra):
        ok = [t for t in group if matches(t, preset)]
        if ok:
            return min(ok, key=range_width_key)
    return None


def out_of_range_refs(preset, version: str = VERSION) -> list[str]:
    """The params keeping ``preset`` off every library template whose chain and baked values
    match ([] if one matches, or if none matches for another reason)."""
    if match(preset, version) is not None:
        return []
    prim, extra = candidates(preset, version)
    for t in prim + extra:
        if t.baked_hash and baked_fingerprint(preset, t.map, t.verified)[1] == t.baked_hash:
            bad = out_of_range(t, preset)
            if bad:
                return bad
    return []


@contextlib.contextmanager
def _locked(path: Path):
    """An exclusive advisory lock on ``path`` across processes (no-op where fcntl is missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # Windows: one process at a time is the caller's job
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_UN)
            except ImportError:
                pass


def record_on_demand(tpl: ChainTemplate, preset, version: str = VERSION, wide: list[str] | None = None,
                     capture_s: float | None = None) -> dict:
    """Add ``tpl`` to ``index.json`` under ``on_demand`` (keyed by template stem) and return the
    entry: models, the params it widened, its CC ranges where they differ from the catalog's."""
    idx = load_index(version)
    e = {"template": template_stem(tpl), "chain": [list(x) for x in tpl.chain],
         "models": [m for _, m in tpl.chain if m], "wide": sorted(wide or []),
         "ranges": {r: list(tpl.ranges[r]) for r in sorted(wide or []) if r in tpl.ranges},
         "cc_ranges": narrowed_ranges(tpl, preset), "cc_range_rules": CC_RANGE_RULES_VERSION,
         "ccs": len(tpl.map), "verified": len(tpl.verified),
         "captured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         "capture_s": None if capture_s is None else round(float(capture_s), 3)}
    idx.setdefault("on_demand", {})[e["template"]] = e
    save_index(idx, version)
    return e


def capture_on_demand(preset, capture, *, version: str = VERSION, wide: list[str] | None = None,
                      recheck=None) -> ChainTemplate:
    """Capture a full-range variant of ``preset``'s chain into ``on_demand_templates(version)``
    and record it in the index; one capture at a time across processes (file lock), and
    ``recheck()`` (or ``match``) runs under the lock first so a variant another process just
    captured is reused. ``capture(preset, templates_dir) -> ChainTemplate`` does the drop
    (``NativeEngine`` passes ``native.capture_chain`` bound to its plugin; tests pass a fake).

    The drop file assigns each param the range ``native.cc_ranges`` gives the preset's own
    values: a value outside the narrowed range keeps the catalog range, every other param keeps
    the narrowed grid. Raises ``UnsupportedModelChange`` from the capture."""
    d = on_demand_templates(version)
    with _locked(d.parent / ".lock"):
        t = recheck() if recheck is not None else match(preset, version)
        if t is not None:
            return t
        t0 = time.perf_counter()
        tpl = capture(preset, d)
        dt = time.perf_counter() - t0
        if not (d / f"{template_stem(tpl)}.json").is_file():
            tpl.save(d)
        record_on_demand(tpl, preset, version, wide=wide or out_of_range_refs(preset, version), capture_s=dt)
    return tpl


def template_for_preset(preset, version: str = VERSION, capture=None) -> ChainTemplate | None:
    """``match``, then (with ``capture``, see ``capture_on_demand``) an on-demand full-range
    capture when the only miss is a value outside the CC ranges. None otherwise."""
    t = match(preset, version)
    if t is not None or capture is None:
        return t
    bad = out_of_range_refs(preset, version)
    if not bad:
        return None
    return capture_on_demand(preset, capture, version=version, wide=bad)


def param_ranges(model_id: str, version: str = VERSION, catalog: dict | None = None) -> dict[str, dict]:
    """Per CC-mapped continuous param of ``model_id``'s library template (keys ``"0:<param>"``):
    ``grid`` (this version's CC range), ``full`` (catalog range), ``narrowed``, ``step``, ``live``
    (``ChainTemplate.param_ranges``). Search samples in ``grid`` for the fine CC steps or in
    ``full`` to reach the whole catalog range; the engine picks the template either way."""
    tpl = template_for(model_id, version)
    return tpl.param_ranges(catalog) if tpl is not None else {}


def full_range(model_id: str, param: str, catalog: dict | None = None) -> tuple[float, float] | None:
    """The HX catalog [min, max] of ``model_id``'s continuous ``param`` (None if not continuous)."""
    from .native import _catalog_models

    p = ((_catalog_models(catalog).get(model_id) or {}).get("params") or {}).get(param)
    if p and p.get("kind") == "continuous" and p.get("min") is not None and p.get("max") is not None:
        return (float(p["min"]), float(p["max"]))
    return None
