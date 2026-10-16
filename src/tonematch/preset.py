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
SNAPSHOT_CONTROLLER = 9  # tone.controller @controller id of "snapshot" (setlist survey, 126 presets)
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
    current_snapshot: int = 0
    # Opt-in snapshot-varying parameters: (block key, param) -> (min, max, controller id).
    # Empty by default: block values win and every snapshot value is synced to them.
    snapshot_controlled: dict = field(default_factory=dict)

    def to_hlx_dict(self, sync_snapshots: bool = True) -> dict:
        """The preset as an .hlx dict. Snapshot controller values are set to the block values
        (lead ruling 2026-10-15: block values win) unless ``sync_snapshots`` is False, which exists
        only so an engine can report what a foreign file's snapshots disagreed on. Parameters
        opted in through :meth:`vary_in_snapshots` keep their per-snapshot values; their block
        value mirrors the current snapshot's, as HX Edit writes it."""
        d = _apply(self)
        if sync_snapshots:
            sync_snapshot_controllers(d, keep=set(self.snapshot_controlled))
        return d

    def vary_in_snapshots(self, key: str, param: str, values, lo: float | None = None,
                          hi: float | None = None, catalog: dict | None = None) -> None:
        """Make ``key.param`` take ``values[i]`` in snapshot ``i`` (one value per snapshot).

        Written as a ``tone.controller`` entry with ``@controller`` 9 (snapshot) whose
        ``@min``/``@max`` span the parameter's catalog range, plus one ``@value`` per snapshot.
        The block value becomes ``values[current_snapshot]``."""
        slot = next((s for s in self.slots if s.key == key), None)
        if slot is None or param not in slot.params:
            raise PresetError(f"{key}.{param}: no such block parameter")
        if len(values) != len(self.snapshots):
            raise PresetError(f"{len(values)} values for {len(self.snapshots)} snapshots")
        if lo is None or hi is None:
            pd = ((catalog or {}).get("models", {}).get(slot.model_id, {}).get("params", {})
                  .get(param))
            if not pd or pd.get("min") is None:
                raise PresetError(f"{key}.{param}: give lo and hi (no catalog range)")
            lo, hi = pd["min"], pd["max"]
        for sn, v in zip(self.snapshots, values):
            sn.params.setdefault(key, {})[param] = v
        slot.params[param] = values[self.current_snapshot]
        self.snapshot_controlled[(key, param)] = (lo, hi, SNAPSHOT_CONTROLLER)

    def keep_snapshot_varying(self) -> int:
        """Adopt the file's controller-assigned parameters whose snapshot values differ from each
        other or from the block (snapshot-controlled ``@controller`` 9, but also expression-pedal
        and footswitch positions the pedal stores per snapshot) as opted in, so a read-write keeps
        them. Returns how many."""
        ctl = self.raw.get("data", {}).get("tone", {}).get("controller", {}).get("dsp0", {})
        n = 0
        for key, ps in ctl.items():
            for param, e in ps.items():
                vals = [sn.params.get(key, {}).get(param) for sn in self.snapshots]
                blk = next((x.params.get(param) for x in self.slots if x.key == key), None)
                if len(set(vals)) > 1 or any(v is not None and v != blk for v in vals):
                    self.snapshot_controlled[(key, param)] = (
                        e.get("@min"), e.get("@max"), e.get("@controller"))
                    n += 1
        return n

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
                  raw=copy.deepcopy(raw), path=path,
                  current_snapshot=int(tone.get("global", {}).get("@current_snapshot", 0)))


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
    for (key, param), (lo, hi, cid) in pre.snapshot_controlled.items():
        if key in dsp0 and param in dsp0[key]:
            dsp0[key][param] = pre.snapshots[pre.current_snapshot].params[key][param]
            c = tone.setdefault("controller", {}).setdefault("dsp0", {}).setdefault(key, {})
            ent = c.setdefault(param, {"@min": lo, "@max": hi, "@controller": cid,
                                       "@snapshot_disable": False})
            ent["@controller"] = cid
    if "global" in tone:
        tone["global"]["@current_snapshot"] = pre.current_snapshot
    _prune(tone, {k for k, v in dsp0.items() if isinstance(v, dict) and "@model" in v})
    if pre.export_mode == "effects_only":
        data["meta"].setdefault("tonematch", {})["export_mode"] = "effects_only"
    return out


# ----------------------------------------------------------------------------- snapshot sync
# Canonical implementation (T051). The pedal applies the current snapshot's controller values over
# the block values on load, so a stale snapshot silently changes the sound. Block values win.

def _num(v):
    return None if isinstance(v, bool) or not isinstance(v, (int, float)) else float(v)


def _snapshot_entries(hlx: dict):
    """Yield (snapshot index, dsp name, block key, param, controller entry, block dict)."""
    tone = hlx.get("data", {}).get("tone", {})
    for n in range(8):
        sn = tone.get(f"snapshot{n}")
        if not isinstance(sn, dict):
            continue
        for dsp, blocks in (sn.get("controllers") or {}).items():
            for bkey, ps in (blocks or {}).items():
                blk = (tone.get(dsp) or {}).get(bkey) or {}
                for name, ent in (ps or {}).items():
                    if isinstance(ent, dict) and "@value" in ent:
                        yield n, dsp, bkey, name, ent, blk


def snapshot_conflicts(hlx: dict, keep=frozenset()) -> list[str]:
    """Snapshot controller values that differ from the block's own value (the file says two
    things). ``keep`` is a set of ``(block key, param)`` that are allowed to vary."""
    out = []
    for n, dsp, bkey, name, ent, blk in _snapshot_entries(hlx):
        if (bkey, name) in keep:
            continue
        b, v = _num(blk.get(name)), _num(ent["@value"])
        if b is not None and v is not None and abs(b - v) > 1e-6 * max(1.0, abs(b)):
            out.append(f"snapshot{n}.controllers.{bkey}.{name} = {v} but the block says {b}")
        elif (b is None) != (v is None) and name in blk and blk[name] != ent["@value"]:
            out.append(f"snapshot{n}.controllers.{bkey}.{name} = {ent['@value']!r} "
                       f"but the block says {blk[name]!r}")
    return out


def sync_snapshot_controllers(hlx: dict, keep=frozenset()) -> list[str]:
    """Set every snapshot controller ``@value`` in an .hlx dict to its block's value, in place.
    Works on the document, so stale entries left in ``Preset.raw`` are fixed too. Returns what
    changed (``snapshot_conflicts`` before the sync)."""
    changed = snapshot_conflicts(hlx, keep)
    for _n, _dsp, bkey, name, ent, blk in _snapshot_entries(hlx):
        if (bkey, name) not in keep and name in blk:
            ent["@value"] = copy.deepcopy(blk[name])
    return changed


def sync_hlx_snapshots(hlx: dict) -> dict:
    """``sync_snapshot_controllers`` returning the same dict (call-chain form)."""
    sync_snapshot_controllers(hlx)
    return hlx


def snapshot_mismatches(preset: Preset, tol: float = 1e-6) -> list[str]:
    """``"snapshot<i>:<block>:<param> snap=<v> block=<v>"`` for every ``Preset.snapshots`` value
    that differs from its block's value."""
    by_key = {s.key: s for s in preset.slots if s.key}
    out = []
    for i, sn in enumerate(preset.snapshots or []):
        for bkey, ps in (sn.params or {}).items():
            blk = by_key.get(bkey)
            for name, v in (ps or {}).items():
                b = (blk.params or {}).get(name) if blk else None
                if b is None:
                    continue
                fv, fb = _num(v), _num(b)
                differs = abs(fv - fb) > tol if fv is not None and fb is not None else v != b
                if differs:
                    out.append(f"snapshot{i}:{bkey}:{name} snap={v} block={b}")
    return out


def sync_snapshots_to_blocks(preset: Preset) -> Preset:
    """A copy of ``preset`` whose ``snapshots`` params equal the block values."""
    p = copy.deepcopy(preset)
    by_key = {s.key: s for s in p.slots if s.key}
    for sn in p.snapshots or []:
        for bkey, ps in (sn.params or {}).items():
            blk = by_key.get(bkey)
            for name in list(ps or {}):
                if blk is not None and name in (blk.params or {}):
                    ps[name] = blk.params[name]
    return p


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
             firmware: int | None = None, engine: str | None = None) -> list[Violation]:
    """Checks 1-5 and 7 of the hlx-validate skill (6, the golden diff, lives in ``golden_diff``).
    ``engine="native"`` also fails models the user's Helix Native refuses (``native_unavailable``)."""
    v: list[Violation] = []
    if engine == "native":
        from .catalog import native_unavailable

        bad = native_unavailable()
        for s in preset.blocks():
            if s.model_id in bad:
                v.append(Violation("model", "fail", f"{s.key or s.model_id}: {s.model_id} is not "
                                   "available on Helix Native (refused as an invalid model)"))

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
        extra = set(sn.enabled) - keys - {"split", "join"}  # split/join can be bypassed too
        if extra:
            F("snapshots", f"snapshot{i} references blocks not in dsp0: {sorted(extra)}")
        if sn.valid and keys - set(sn.enabled):
            W("snapshots", f"snapshot{i} has no bypass state for {sorted(keys - set(sn.enabled))}")
    if len({tuple(sorted(sn.enabled)) for sn in valid}) > 1:
        F("snapshots", "valid snapshots do not cover the same block set")
    # block values win: a document whose snapshots disagree with its blocks is refused, except
    # parameters explicitly opted in as snapshot-controlled (checked separately)
    doc = preset.to_hlx_dict(sync_snapshots=False)
    for msg in snapshot_conflicts(doc, set(preset.snapshot_controlled)):
        F("snapshot_values", msg)
    ctl = doc["data"]["tone"].get("controller", {}).get("dsp0", {})
    for (key, param), (_lo, _hi, cid) in preset.snapshot_controlled.items():
        e = ctl.get(key, {}).get(param, {})
        if not e:
            F("snapshot_controlled", f"{key}.{param}: no tone.controller entry")
            continue
        if cid != SNAPSHOT_CONTROLLER:
            continue  # pedal/footswitch position stored per snapshot: nothing to mirror
        cur = preset.snapshots[preset.current_snapshot].params.get(key, {}).get(param) \
            if preset.snapshots else None
        slot = next((x for x in blocks if x.key == key), None)
        if slot is not None and cur is not None and slot.params.get(param) != cur:
            F("snapshot_controlled", f"{key}.{param}: block value is not the current snapshot's")
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
