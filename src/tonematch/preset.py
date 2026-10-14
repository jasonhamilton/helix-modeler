"""Preset model and `.hlx` reader/writer/validator (contract: contracts/preset-schema.md).

The in-memory ``Preset`` keeps the full parsed JSON in ``raw`` so that a read followed by a write
reproduces the file byte for byte when nothing was edited. Edits to ``slots`` and ``snapshots`` are
applied onto a deep copy of ``raw`` by :meth:`Preset.to_hlx_dict`.

Nothing here embeds Line 6 catalog content; the catalog is passed in (see ``tonematch.catalog``).
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

STOMP_DEVICE_ID = 2162694
TEMPLATE_ORDER = ("comp", "drive", "amp", "cab", "eq", "mod", "delay", "reverb")
MAX_BLOCKS = 8
# Keys of a block dict that the Slot fields own; every other "@" key rides along in Slot.extra.
_OWNED = ("@model", "@enabled", "@position")
_FLOW_PREFIXES = ("HD2_App", "HelixStomp_App")
_FLOW_KEYS = ("input", "output", "split", "join")
_CAT_PREFIX = (  # fallback when no catalog is given
    ("HD2_Amp", "amp"), ("HD2_Preamp", "amp"), ("HD2_Cab", "cab"), ("HD2_Compressor", "comp"),
    ("HD2_Dist", "drive"), ("HD2_DM4", "drive"), ("HD2_EQ", "eq"), ("HD2_Delay", "delay"),
    ("HD2_DL4", "delay"), ("HD2_Reverb", "reverb"), ("HD2_Chorus", "mod"), ("HD2_Flanger", "mod"),
    ("HD2_Phaser", "mod"), ("HD2_Tremolo", "mod"), ("HD2_Rotary", "mod"),
)


class PresetError(ValueError):
    pass


@dataclass
class Slot:
    category: str
    model_id: str | None
    params: dict[str, float] = field(default_factory=dict)
    enabled: bool = False
    key: str | None = None          # "block3" / "cab0" in the file; None for a new slot
    position: int | None = None     # "@position"; cabs have none
    extra: dict = field(default_factory=dict)  # remaining "@..." keys (@path, @stereo, @cab ...)


@dataclass
class Snapshot:
    name: str = "SNAPSHOT"
    valid: bool = True
    enabled: dict[str, bool] = field(default_factory=dict)            # block key -> on
    params: dict[str, dict[str, float]] = field(default_factory=dict)  # block key -> param -> value


@dataclass
class Preset:
    name: str
    slots: list[Slot] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    export_mode: str = "full_rig"  # full_rig | effects_only
    device: int = STOMP_DEVICE_ID
    device_version: int = 0
    raw: dict = field(default_factory=dict)
    path: Path | None = None

    def to_hlx_dict(self) -> dict:
        return _apply(self)

    def blocks(self) -> list[Slot]:
        return [s for s in self.slots if s.model_id is not None]


@dataclass
class Violation:
    check: str
    severity: str  # "fail" | "warn"
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} {self.check}: {self.message}"


# ----------------------------------------------------------------------------- text format

def dumps_hlx(obj, _level: int = 0) -> str:
    """Serialise the way HX Edit does: 2-space indent, ``"key" : value``, blank line in empty
    containers, no trailing newline."""
    pad = "  " * (_level + 1)
    end = "  " * _level
    if isinstance(obj, dict):
        if not obj:
            return "{\n\n" + end + "}"
        items = [f"{pad}{json.dumps(k, ensure_ascii=False)} : {dumps_hlx(v, _level + 1)}"
                 for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    if isinstance(obj, list):
        if not obj:
            return "[\n\n" + end + "]"
        return "[\n" + ",\n".join(pad + dumps_hlx(v, _level + 1) for v in obj) + "\n" + end + "]"
    if isinstance(obj, float) and math.isfinite(obj):
        return format(obj, ".17g")  # HX Edit writes 17 significant digits
    return json.dumps(obj, ensure_ascii=False)


# ----------------------------------------------------------------------------- reading

def _is_flow(key: str, block: dict) -> bool:
    mid = block.get("@model", "")
    return mid.startswith(_FLOW_PREFIXES) or key.startswith(_FLOW_KEYS)


def _category(model_id: str, catalog: dict | None) -> str:
    if catalog and model_id in catalog.get("models", {}):
        return catalog["models"][model_id]["category"]
    for pre, cat in _CAT_PREFIX:
        if model_id.startswith(pre):
            return cat
    return "other"


def _slot_from(key: str, block: dict, catalog: dict | None) -> Slot:
    params = {k: v for k, v in block.items() if not k.startswith("@")}
    extra = {k: v for k, v in block.items() if k.startswith("@") and k not in _OWNED}
    return Slot(category=_category(block["@model"], catalog), model_id=block["@model"],
                params=params, enabled=bool(block.get("@enabled", True)), key=key,
                position=block.get("@position"), extra=extra)


def from_dict(raw: dict, catalog: dict | None = None, path: Path | None = None) -> Preset:
    try:
        data = raw["data"]
        tone = data["tone"]
    except (KeyError, TypeError) as e:
        raise PresetError(f"not an .hlx preset (missing {e})") from e
    dsp0 = tone.get("dsp0", {})
    blocks = [(k, v) for k, v in dsp0.items()
              if isinstance(v, dict) and "@model" in v and not _is_flow(k, v)]
    main = sorted((kv for kv in blocks if not kv[0].startswith("cab")),
                  key=lambda kv: kv[1].get("@position", 0))
    cabs = {k: v for k, v in blocks if k.startswith("cab")}
    slots: list[Slot] = []
    for k, v in main:
        slots.append(_slot_from(k, v, catalog))
        link = v.get("@cab")
        if link in cabs:
            slots.append(_slot_from(link, cabs.pop(link), catalog))
    slots.extend(_slot_from(k, v, catalog) for k, v in cabs.items())  # orphan cabs, kept visible
    snaps = []
    for i in range(3):
        s = tone.get(f"snapshot{i}")
        if not isinstance(s, dict):
            continue
        ctrl = s.get("controllers", {}).get("dsp0", {})
        snaps.append(Snapshot(
            name=s.get("@name", f"SNAPSHOT {i + 1}"), valid=bool(s.get("@valid", False)),
            enabled=dict(s.get("blocks", {}).get("dsp0", {})),
            params={b: {p: e["@value"] for p, e in ps.items() if "@value" in e}
                    for b, ps in ctrl.items()}))
    meta = data.get("meta", {})
    mode = "effects_only" if (meta.get("tonematch") or {}).get("export_mode") == "effects_only" \
        else "full_rig"
    return Preset(name=meta.get("name", ""), slots=slots, snapshots=snaps, export_mode=mode,
                  device=data.get("device", 0), device_version=data.get("device_version", 0),
                  raw=copy.deepcopy(raw), path=path)


def read_hlx(path: str | Path, catalog: dict | None = None) -> Preset:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PresetError(f"{p}: cannot parse ({e})") from e
    return from_dict(raw, catalog, p)


# ----------------------------------------------------------------------------- writing

def _apply(pre: Preset) -> dict:
    out = copy.deepcopy(pre.raw) if pre.raw else {
        "data": {"meta": {"name": pre.name}, "device": pre.device, "tone": {"dsp0": {}}},
        "meta": {"original": 0, "pbn": 0, "premium": 0}, "schema": "L6Preset", "version": 6}
    data = out["data"]
    data["device"] = pre.device
    if pre.device_version:
        data["device_version"] = pre.device_version
    data.setdefault("meta", {})["name"] = pre.name
    tone = data.setdefault("tone", {})
    dsp0 = tone.setdefault("dsp0", {})
    keep = {s.key for s in pre.slots if s.key and s.model_id is not None}
    for k in [k for k, v in dsp0.items()
              if isinstance(v, dict) and "@model" in v and not _is_flow(k, v) and k not in keep]:
        del dsp0[k]
    used = set(dsp0)
    for s in pre.slots:
        if s.model_id is None:
            continue
        if s.key is None:
            prefix = "cab" if s.category == "cab" else "block"
            n = 0
            while f"{prefix}{n}" in used:
                n += 1
            s.key = f"{prefix}{n}"
            used.add(s.key)
        old = dsp0.get(s.key, {})
        blk = {"@model": s.model_id, "@enabled": s.enabled}
        if s.position is not None:
            blk["@position"] = s.position
        blk.update(s.extra)
        blk.update(s.params)
        # keep the original key order when the block already existed and only values changed
        if old and old.get("@model") == s.model_id:
            merged = dict(old)
            merged.update(blk)
            blk = {k: merged[k] for k in old} | {k: v for k, v in merged.items() if k not in old}
        dsp0[s.key] = blk
    for i, sn in enumerate(pre.snapshots):
        t = tone.setdefault(f"snapshot{i}", {})
        t["@name"], t["@valid"] = sn.name, sn.valid
        if sn.enabled:
            t.setdefault("blocks", {})["dsp0"] = dict(sn.enabled)
        if sn.params:
            c = t.setdefault("controllers", {}).setdefault("dsp0", {})
            for b, ps in sn.params.items():
                for p, v in ps.items():
                    c.setdefault(b, {}).setdefault(p, {"@fs_enabled": False})["@value"] = v
    _prune(tone, {k for k, v in dsp0.items() if isinstance(v, dict) and "@model" in v})
    if pre.export_mode == "effects_only":
        data["meta"].setdefault("tonematch", {})["export_mode"] = "effects_only"
    return out


def _prune(tone: dict, live: set[str]) -> None:
    """Drop controller, footswitch and snapshot entries that point at removed blocks."""
    for sec in ("controller", "footswitch"):
        d = tone.get(sec)
        if isinstance(d, dict) and isinstance(d.get("dsp0"), dict):
            d["dsp0"] = {k: v for k, v in d["dsp0"].items() if k in live}
    for i in range(3):
        sn = tone.get(f"snapshot{i}")
        if not isinstance(sn, dict):
            continue
        if isinstance(sn.get("blocks", {}).get("dsp0"), dict):
            sn["blocks"]["dsp0"] = {k: v for k, v in sn["blocks"]["dsp0"].items() if k in live}
        c = sn.get("controllers", {})
        if isinstance(c.get("dsp0"), dict):
            c["dsp0"] = {k: v for k, v in c["dsp0"].items() if k in live}


def write_hlx(preset: Preset, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps_hlx(preset.to_hlx_dict()), encoding="utf-8")
    return p


def export(preset: Preset, mode: str, standin: dict | None = None) -> Preset:
    """Return a copy for ``mode`` (``full_rig`` | ``effects_only``).

    effects_only drops amp and cab slots (and their snapshot entries) and records the stand-in
    model ids in ``meta.tonematch`` so the result can be reproduced.
    """
    if mode not in ("full_rig", "effects_only"):
        raise PresetError(f"unknown export mode {mode!r}")
    out = copy.deepcopy(preset)
    out.export_mode = mode
    if mode == "full_rig":
        return out
    gone = [s for s in out.slots if s.category in ("amp", "cab")]
    rec = standin or {"amp": next((s.model_id for s in gone if s.category == "amp"), None),
                      "cab": next((s.model_id for s in gone if s.category == "cab"), None)}
    out.slots = [s for s in out.slots if s.category not in ("amp", "cab")]
    dead = {s.key for s in gone}
    for sn in out.snapshots:
        sn.enabled = {k: v for k, v in sn.enabled.items() if k not in dead}
        sn.params = {k: v for k, v in sn.params.items() if k not in dead}
    out.raw = copy.deepcopy(preset.raw)
    if out.raw:
        out.raw.setdefault("data", {}).setdefault("meta", {})["tonematch"] = {
            "export_mode": "effects_only", "standin": rec}
        ctrl = out.raw["data"]["tone"].get("controller", {}).get("dsp0", {})
        for k in dead:
            ctrl.pop(k, None)
        for i in range(3):
            c = out.raw["data"]["tone"].get(f"snapshot{i}", {}).get("controllers", {}).get("dsp0", {})
            for k in dead:
                c.pop(k, None)
    return out


# ----------------------------------------------------------------------------- DSP costs

def build_dsp_costs(catalog: dict, budget: float | None = None) -> dict:
    """Derive a cost table from the catalog's per-model ``load`` figures (catalog-estimated, not
    measured on the device). ``budget`` stays None until a spike measures the real limit."""
    models = {mid: {"mono": m.get("load"), "stereo": m.get("load_stereo")}
              for mid, m in catalog["models"].items()}
    return {"source": "catalog load field; not device-measured", "budget": budget,
            "heavy": [], "models": models}


def _cost(model_id: str, stereo: bool, costs: dict) -> float | None:
    c = costs.get("models", {}).get(model_id)
    if not c:
        return None
    v = (c.get("stereo") if stereo else c.get("mono"))
    return v if v is not None else (c.get("mono") if c.get("mono") is not None else c.get("stereo"))


def dsp_total(preset: Preset, costs: dict) -> tuple[float, list[str]]:
    total, unknown = 0.0, []
    for s in preset.blocks():
        v = _cost(s.model_id, bool(s.extra.get("@stereo", False)), costs)
        if v is None:
            unknown.append(s.model_id)
        else:
            total += v
    return total, unknown


# ----------------------------------------------------------------------------- validation

def _in_range(v, p) -> bool:
    lo, hi = p.get("min"), p.get("max")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return isinstance(v, bool)
    hi_all = max([x for x in (hi, p.get("max_stereo")) if isinstance(x, (int, float))],
                 default=None)
    if isinstance(lo, (int, float)) and v < lo - 1e-6:
        return False
    return not (hi_all is not None and v > hi_all + 1e-6)


def validate(preset: Preset, catalog: dict | None, costs: dict | None = None,
             firmware: int | None = None) -> list[Violation]:
    """Checks 1-5 and 7 of the hlx-validate skill (6, the golden diff, lives in ``golden_diff``)."""
    v: list[Violation] = []

    def F(c: str, m: str) -> None:
        v.append(Violation(c, "fail", m))

    def W(c: str, m: str) -> None:
        v.append(Violation(c, "warn", m))

    if preset.device != STOMP_DEVICE_ID:
        F("device", f"data.device is {preset.device}, expected {STOMP_DEVICE_ID} (HX Edit error -8309)")
    if firmware is not None and preset.device_version != firmware:
        W("device_version", f"{preset.device_version:#x} != expected {firmware:#x}")
    elif not preset.device_version:
        F("device_version", "missing")
    models = (catalog or {}).get("models", {})
    if catalog is None:
        W("catalog", "no catalog: model ids and parameter ranges not checked")
    blocks = preset.blocks()
    mains = [s for s in blocks if s.position is not None]
    if len(mains) > MAX_BLOCKS:
        F("block_count", f"{len(mains)} blocks, max {MAX_BLOCKS}")
    for s in blocks:
        where = s.key or s.model_id
        m = models.get(s.model_id) if catalog else None
        if catalog and m is None:
            F("model", f"{where}: unknown model {s.model_id}")
            continue
        if s.category != "cab" and s.position is None:
            F("block_keys", f"{where}: missing @position")
        if s.category != "cab" and "@path" not in s.extra:
            F("block_keys", f"{where}: missing @path")
        if m:
            for pn, pv in s.params.items():
                pd = m["params"].get(pn)
                if pd is None:
                    F("param", f"{where}: {s.model_id} has no parameter {pn!r}")
                elif not _in_range(pv, pd):
                    F("param_range", f"{where}.{pn}={pv} outside [{pd.get('min')}, {pd.get('max')}]")
            if not m.get("stomp", True):
                F("model", f"{where}: {s.model_id} is not available on HX Stomp")
    if costs:
        total, unknown = dsp_total(preset, costs)
        bud = costs.get("budget")
        if bud is not None and total > bud:
            F("dsp_budget", f"estimated cost {total:.1f} > budget {bud}")
        elif bud is None:
            W("dsp_budget", f"estimated cost {total:.1f}; budget not yet measured, not enforced")
        for mid in unknown:
            W("dsp_budget", f"no cost known for {mid}")
        heavy = [s.model_id for s in blocks if s.model_id in costs.get("heavy", [])]
        if heavy:
            F("dsp_budget", f"known-heavy models: {heavy}")
    else:
        W("dsp_budget", "no dsp-costs table: budget not checked")
    # snapshots
    keys = {s.key for s in blocks if s.key and not s.key.startswith("cab")}
    valid = [sn for sn in preset.snapshots if sn.valid]
    if not valid:
        W("snapshots", "no valid snapshot")
    for i, sn in enumerate(preset.snapshots):
        extra = set(sn.enabled) - keys
        if extra:
            F("snapshots", f"snapshot{i} references blocks not in dsp0: {sorted(extra)}")
        if sn.valid and keys - set(sn.enabled):
            W("snapshots", f"snapshot{i} has no bypass state for {sorted(keys - set(sn.enabled))}")
    if len({tuple(sorted(sn.enabled)) for sn in valid}) > 1:
        F("snapshots", "valid snapshots do not cover the same block set")
    # amp/cab pairing
    amps = [s for s in blocks if s.category == "amp"]
    cabs = [s for s in blocks if s.category == "cab"]
    if preset.export_mode == "effects_only":
        if amps or cabs:
            F("export_mode", "effects_only preset still has amp/cab blocks")
        if not (preset.raw.get("data", {}).get("meta", {}).get("tonematch", {}).get("standin")):
            F("export_mode", "effects_only but meta.tonematch.standin is not recorded")
    else:
        if cabs and not amps:
            F("amp_cab", "cab block without an amp in full-rig mode")
        for a in amps:
            link = a.extra.get("@cab")
            if link and not any(c.key == link for c in cabs):
                F("amp_cab", f"{a.key}: @cab {link!r} points at no cab block")
            if not link and not cabs:
                W("amp_cab", f"{a.key}: amp has no cab (fine only for preamp/IR setups)")
    return v


# ----------------------------------------------------------------------------- golden diff

def _keys(raw: dict, catalog: dict | None = None) -> dict:
    data = raw.get("data", {})
    tone = data.get("tone", {})
    out = {"top": set(raw), "data": set(data), "meta": set(data.get("meta", {})),
           "tone": set(tone)}
    for k, b in tone.get("dsp0", {}).items():
        if isinstance(b, dict) and "@model" in b:
            cat = "flow" if _is_flow(k, b) else _category(b["@model"], catalog)
            if cat in ("amp", "cab", "flow"):
                lvl = f"block@{cat}"
            else:
                lvl = "block@effect"
            out.setdefault(lvl, set()).update(x for x in b if x.startswith("@"))
    return out


def golden_diff(raw: dict, golden_raw: dict, catalog: dict | None = None) -> dict:
    """Per level: keys the golden has that ``raw`` lacks, and keys ``raw`` adds. Block-level keys
    are compared per class (amp, cab, flow, effect) and only where both files have such a block."""
    a, g = _keys(raw, catalog), _keys(golden_raw, catalog)
    out = {}
    for lvl in sorted(set(a) | set(g)):
        if lvl.startswith("block@") and (lvl not in a or lvl not in g):
            continue
        sa, sg = a.get(lvl, set()), g.get(lvl, set())
        out[lvl] = {"missing": sorted(sg - sa), "added": sorted(sa - sg)}
    return out
