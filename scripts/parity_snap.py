#!/usr/bin/env python3
"""Parity method rule (lead ruling on T066b, DECISIONS 2026-10-16): Native takes every continuous knob through a
128-value MIDI CC, so it plays ``cc_value(lo, hi, round(norm * 127))``, not the value in the file. Before a parity
script renders on the Stomp it writes those snapped values into the Stomp's .hlx, so the CC grid is never scored
as an engine difference.

``lo`` and ``hi`` come from the captured Native template (``ChainTemplate.ranges``, which T072 narrows for delay
times, LFO rates and reverb decay), never from the catalog and never hard-coded: the template that renders the
preset is picked the way ``NativeEngine._match_template`` picks it.

Used by parity_spot.py, parity_timefx.py (to flag off-grid raw renders) and parity_timefx_stimuli.py. Offline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch import preset as P
from tonematch.engines import native as N

REL_TOL = 1e-6


def template_for(preset, templates_dir: Path):
    """The captured template Native renders ``preset`` with: same structure, same baked values, every CC-driven
    value inside its range; the finest CC grid wins. LookupError when none was captured."""
    cands = N.ChainTemplate.load_variants(templates_dir).get(N.chain_key(preset), [])
    ok = [t for t in cands if t.baked_hash and N.baked_fingerprint(preset, t.map, t.verified)[1] == t.baked_hash
          and not N.out_of_range(t, preset)]
    if not ok:
        raise LookupError(f"no Native template in {templates_dir} renders this preset (run the native step first)")
    return min(ok, key=N.range_width_key)


def snap_value(v: float, lo: float, hi: float) -> float:
    """The value Native plays for ``v`` on a CC assigned over [lo, hi]."""
    span = (hi - lo) or 1.0
    c = int(min(max(round((float(v) - lo) / span * N.CC_STEPS), 0), N.CC_STEPS))
    return N.cc_value(lo, hi, c)


def snapped_values(preset, tpl) -> dict[str, tuple[float, float, float, float]]:
    """{"<slot>:<param>": (requested, snapped, lo, hi)} for every CC-driven continuous parameter whose value the
    grid moves (more than REL_TOL relative)."""
    out: dict = {}
    live = set(tpl.map) & set(tpl.verified)
    for i, s in enumerate(preset.slots):
        for name, v in (s.params or {}).items():
            ref = f"{i}:{name}"
            if ref not in live or ref not in tpl.ranges or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            lo, hi = (float(x) for x in tpl.ranges[ref])
            sn = snap_value(v, lo, hi)
            if abs(sn - float(v)) > REL_TOL * max(1.0, abs(float(v))):
                out[ref] = (float(v), sn, lo, hi)
    return out


def snap_preset(preset, templates_dir: Path):
    """(a copy of ``preset`` with Native's snapped values, the moves as in ``snapped_values``)."""
    import copy

    moves = snapped_values(preset, template_for(preset, templates_dir))
    q = copy.deepcopy(preset)
    for ref, (_req, sn, _lo, _hi) in moves.items():
        i, name = ref.split(":", 1)
        q.slots[int(i)].params[name] = sn
    return q, moves


def write_snapped_hlx(src: Path, dst: Path, templates_dir: Path, catalog: dict | None = None) -> dict:
    """Read ``src``, snap, write ``dst``, and return a report ``{"moves": {ref: [req, snapped, lo, hi]}, ...}``."""
    p = P.read_hlx(src, catalog)
    q, moves = snap_preset(p, templates_dir)
    P.write_hlx(q, dst)
    return {"src": str(src), "dst": str(dst), "moves": {k: list(v) for k, v in moves.items()}}


def off_grid(hlx: Path, templates_dir: Path, catalog: dict | None = None) -> dict:
    """The CC-driven parameters of ``hlx`` that sit off Native's grid (empty when the file was snapped)."""
    p = P.read_hlx(hlx, catalog)
    return {k: list(v) for k, v in snapped_values(p, template_for(p, templates_dir)).items()}


def snap_dir(names: list[str], src_dir: Path, dst_dir: Path, templates_dir: Path, catalog: dict | None = None) -> dict:
    """Snap ``names`` (.hlx stems) from ``src_dir`` into ``dst_dir``; writes ``snap_report.json`` there."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    rep = {n: write_snapped_hlx(src_dir / f"{n}.hlx", dst_dir / f"{n}.hlx", templates_dir, catalog) for n in names}
    (dst_dir / "snap_report.json").write_text(json.dumps(rep, indent=1))
    return rep
