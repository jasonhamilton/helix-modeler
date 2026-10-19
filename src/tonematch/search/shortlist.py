"""AFx-Rep shortlist for no-DI drive matching (T178; decision 5 of 2026-10-18, evidence T175).

Ranks every model by the embed_afxrep distance between the target and the model's coarse renders (the smallest over
the coarse Drive x Tone grid), keeps the top N, and optionally widens them to their measured family
(`cache/drive-maps/drive-families-v1.json`, T180). It chooses MODELS only. Knobs come from the fingerprint inversion (T181, if
present), or from the user's aligned take (mrstft_v2); with neither, the coarse grid's best point is reported as
low-confidence. A sound score is never used to claim knob accuracy.
"""
from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
FAMILIES_PATH = ROOT / "cache" / "drive-maps" / "drive-families-v1.json"
COARSE_DRIVE = (0.2, 0.5, 0.85)
COARSE_TONE = (0.3, 0.7)
INVERSION_MODULE = "tonematch.metrics.distortion_step4"     # T181 interface: estimate_knobs(target, di, models) -> {model: {knob: value}}
TONE_KEYS = ("tone", "treble", "filter", "contour", "spectrum")


def coarse_points(free, drive=COARSE_DRIVE, tone=COARSE_TONE, knobs: dict | None = None) -> list[dict[str, float]]:
    """Drive x Tone settings (fractions of the range -> parameter values) for one block's `free` knobs
    [(name, lo, hi, default)]; other knobs stay at their default. [{}] for a model with neither knob.
    `knobs` ({"drive_knobs": [...], "tone_knobs": [...]}, the measured map of T179) names the knobs; the name
    rules below are the fallback. T183 found the name rules move the wrong knob or none on 10 of 60 models
    (Classic Distortion, KillerZ, Clawthorn Drive, Tone Sovereign, Vital Boost, ...)."""
    names = {n.lower(): (n, lo, hi) for n, lo, hi, _ in free}
    exact = {n: (n, lo, hi) for n, lo, hi, _ in free}
    dk = next((exact[k] for k in (knobs or {}).get("drive_knobs", []) if k in exact), None)
    tk = next((exact[k] for k in (knobs or {}).get("tone_knobs", []) if k in exact), None)
    dk = dk or next((names[k] for k in ("drive", "gain", "fuzz", "sustain", "dist", "distortion") if k in names), None)
    tk = tk or next((names[k] for k in TONE_KEYS if k in names), None)

    def val(k, f):
        return k[1] + f * (k[2] - k[1])
    pts: list[dict[str, float]] = [{}]
    if dk:
        pts = [{dk[0]: val(dk, f)} for f in drive]
    if tk:
        pts = [{**p, tk[0]: val(tk, f)} for p in pts for f in tone]
    return pts


def load_families(path: str | Path | None = None) -> dict[str, list[str]]:
    """model -> its family members (itself included), from {"families": {name: [models]}} or {name: [models]}.
    {} when the file is missing or unreadable (the T180 agent has not delivered it)."""
    p = Path(path) if path else FAMILIES_PATH
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    fams = raw.get("families", raw) if isinstance(raw, dict) else {}
    if isinstance(fams, list):
        fams = {str(i): f for i, f in enumerate(fams)}
    out: dict[str, list[str]] = {}
    for members in fams.values():
        if isinstance(members, dict):
            members = members.get("models", [])
        for m in members:
            out[m] = list(members)
    return out


def load_second_choice(path: str | Path | None = None, below: float = 0.5) -> dict[str, str]:
    """model -> its second-choice family id, for the models whose `member_confidence` is under `below`
    (T180's file). {} when the file or those keys are missing."""
    p = Path(path) if path else FAMILIES_PATH
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    conf, second = raw.get("member_confidence", {}), raw.get("second_choice", {})
    out = {}
    for m, c in conf.items():
        sc = second.get(m)
        fam = sc.get("family") if isinstance(sc, dict) else sc
        if fam and float(c) < below:
            out[m] = str(fam)
    return out


def family_members(path: str | Path | None = None) -> dict[str, list[str]]:
    """family id -> members."""
    p = Path(path) if path else FAMILIES_PATH
    try:
        fams = json.loads(p.read_text()).get("families", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(k): list(v) for k, v in fams.items() if isinstance(v, list)}


def expand_second_choice(top: list[str], base: list[str], second: dict[str, str], members: dict[str, list[str]],
                         available: set[str]) -> tuple[list[str], list[str]]:
    """`base` plus the members of each `top` model's second-choice family. Returns (models, the ones added)."""
    out, added = list(base), []
    for m in top:
        for f in members.get(second.get(m, ""), []):
            if f in available and f not in out:
                out.append(f)
                added.append(f)
    return out, added


def expand_families(top: list[str], families: dict[str, list[str]], available: set[str]) -> list[str]:
    out = list(top)
    for m in top:
        for f in families.get(m, []):
            if f in available and f not in out:
                out.append(f)
    return out


def rank_models(engine, blocks: dict, section, *, embed: Callable | None = None, dist: Callable | None = None,
                points: Callable = coarse_points, knob_map: dict | None = None, log=print) -> list[dict]:
    """[{model, distance, best_values, renders}] by ascending distance. `embed(audio)` / `dist(a, b)` default to
    AFx-Rep (`tonematch.metrics.afxrep.features` / `distance`). Renders go through `section.dry`."""
    if embed is None or dist is None:
        from tonematch.metrics import afxrep
        embed, dist = embed or afxrep.features, dist or afxrep.distance
    ft = embed(section.target)
    if knob_map is None:
        from tonematch.search.knob_tol import load_sensitivity
        knob_map = load_sensitivity()[0]
    rows = []
    for m, b in blocks.items():
        best, bv, n = float("inf"), {}, 0
        for vals in points(b.free, knobs=knob_map.get(m)):
            engine.load(b.with_values(vals) if vals else b.preset)
            y = engine.render(section.dry)
            n += 1
            d = float(dist(ft, embed(y)))
            if np.isfinite(d) and d < best:
                best, bv = d, dict(vals)
        rows.append({"model": m, "distance": best, "best_values": bv, "renders": n})
    rows.sort(key=lambda r: r["distance"])
    log(f"AFx-Rep shortlist: {len(rows)} models, {sum(r['renders'] for r in rows)} renders; top "
        + ", ".join(f"{r['model']} {r['distance']:.3f}" for r in rows[:5]))
    return rows


def shortlist(engine, blocks: dict, section, n: int = 8, *, families_path=None, expand: bool = True,
              second_choice_below: float | None = None, **kw) -> dict:
    """{"ranked": all rows, "top": top-N models, "models": top-N plus their family, "families": used?}"""
    rows = rank_models(engine, blocks, section, **kw)
    top = [r["model"] for r in rows[:max(n, 1)]]
    fams = load_families(families_path) if expand else {}
    models = expand_families(top, fams, set(blocks)) if fams else top
    added: list[str] = []
    if fams and second_choice_below is not None:
        models, added = expand_second_choice(top, models, load_second_choice(families_path, second_choice_below),
                                             family_members(families_path), set(blocks))
    return {"ranked": rows, "top": top, "models": models, "families_used": bool(fams), "second_choice_added": added,
            "renders": sum(r["renders"] for r in rows)}


def fingerprint_inversion():
    """T181's `estimate_knobs(target, di, models)` if that code exists, else None."""
    try:
        mod = importlib.import_module(INVERSION_MODULE)
    except ImportError:
        return None
    return getattr(mod, "estimate_knobs", None)


def knob_path(*, inversion, aligned_take: bool) -> str:
    """Which path sets the knobs: 'inversion' (a), 'aligned_take' (b) or 'coarse_grid' (c, low confidence)."""
    if inversion is not None:
        return "inversion"
    return "aligned_take" if aligned_take else "coarse_grid"
