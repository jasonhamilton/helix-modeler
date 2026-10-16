"""Full-parameter readback check for HX Stomp whole-preset loads (T051, stomp-engine).

Every parameter of every block the .hlx names is compared with what the pedal holds (the live
`chain` read after the write) or with what a compiled device document holds (`stompd decode`,
offline, before the write). A mismatch raises ``ReadbackMismatch``: a preset that did not land
as written must never be rendered, scored or listened to as if it had.

Why this exists: the device recalls the current snapshot's controller values over the block
values on load. For a controller-assigned parameter (04C's delay Mix and Feedback on MIDI CC
110) the value that sounds is the snapshot's, not the block's. The compiler used to keep the
base document's snapshot values, so rank2_H's delay ran Feedback 0.52 where the file said 0.40,
and no check compared delay values.

What the device should hold, per .hlx block key (``effective_values``): the block's values, with
each parameter the current snapshot (``global.@current_snapshot``) names under
``snapshotN.controllers`` replaced by that ``@value``; enabled from ``snapshotN.blocks`` when
present, else ``@enabled``.

Tolerances, by the catalog kind the chain reports: switches and menus exact (after rounding to
bool / int); knobs 1e-5 of the parameter's range (0.2 Hz on a 20 Hz-20 kHz cut, 0.0014 dB on a
-120..+20 dB level, 1e-5 on a 0..1 knob, 8e-5 s on a 0..8 s time); values with no catalog entry
(a cab's ``@mic``, a delay's ``@trails``) exact after rounding. Float32 storage is ~1e-7 relative,
far inside these.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..preset import snapshot_conflicts, sync_snapshot_controllers  # noqa: F401  canonical
from .base import EngineError

REL_SPAN_TOL = 1e-5
NO_RANGE_TOL = 1e-6
UNUSED = 64           # a snapshot controller entry with this place is empty


class ReadbackMismatch(EngineError):
    """The pedal (or the compiled document) does not hold what the .hlx says."""

    def __init__(self, message: str, diff: ReadbackDiff):
        super().__init__(message)
        self.diff = diff


@dataclass
class ReadbackDiff:
    mismatches: list[str] = field(default_factory=list)   # wrong value, model, enabled or routing: fails
    missing: list[str] = field(default_factory=list)      # the file names it, the device has no such parameter: fails
    unspecified: list[str] = field(default_factory=list)  # the device has it, the file does not name it: info
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.missing

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "mismatches": self.mismatches, "missing": self.missing,
                "unspecified": self.unspecified}

    def summary(self, n: int = 8) -> str:
        bad = self.mismatches + self.missing
        more = f" (+{len(bad) - n} more)" if len(bad) > n else ""
        return f"{len(bad)} of {self.checked} checks failed: " + "; ".join(bad[:n]) + more


def _tone(hlx: dict) -> dict:
    return hlx["data"]["tone"]


def current_snapshot(hlx: dict) -> int:
    return int(_tone(hlx).get("global", {}).get("@current_snapshot", 0) or 0)


def _num(v) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, int | float):
        return float(v)
    return None


def block_keys(hlx: dict) -> list[str]:
    dsp = _tone(hlx).get("dsp0", {})
    return [k for k, v in dsp.items() if k.startswith(("block", "cab")) and isinstance(v, dict) and "@model" in v]


def effective_values(hlx: dict, snapshot: int | None = None) -> dict[str, dict]:
    """Per block/cab key: the values the device should run (see the module docstring)."""
    tone = _tone(hlx)
    dsp = tone.get("dsp0", {})
    n = current_snapshot(hlx) if snapshot is None else snapshot
    snap = tone.get(f"snapshot{n}") or {}
    ctl = (snap.get("controllers") or {}).get("dsp0") or {}
    states = (snap.get("blocks") or {}).get("dsp0") or {}
    out: dict[str, dict] = {}
    for key in block_keys(hlx):
        vals = dict(dsp[key])
        for pid, entry in (ctl.get(key) or {}).items():
            if isinstance(entry, dict) and "@value" in entry:
                vals[pid] = entry["@value"]
        if key in states and isinstance(states[key], bool):
            vals["@enabled"] = states[key]
        out[key] = vals
    return out


def slot_map(hlx: dict, report: dict | None = None) -> dict[str, tuple[int, bool]]:
    """.hlx block/cab key -> (device slot, is the paired cab). From the compile report when given,
    else the Stomp layout (Path A row: slot 1 + @position)."""
    dsp = _tone(hlx).get("dsp0", {})
    out: dict[str, tuple[int, bool]] = {}
    if report and report.get("slots"):
        for s in report["slots"]:
            if s.get("key"):
                out[s["key"]] = (int(s["slot"]), False)
    else:
        for k in block_keys(hlx):
            node = dsp[k]
            if "@position" in node and int(node.get("@path", 0)) in (0, 1):
                out[k] = (1 + int(node["@position"]) + 10 * int(node.get("@path", 0)), False)
    for k, (slot, _) in list(out.items()):
        cab = dsp.get(k, {}).get("@cab")
        if cab and cab in dsp:
            out[cab] = (slot, True)
    return out


def _tol(meta: dict) -> float:
    if "min" in meta and "max" in meta:
        return REL_SPAN_TOL * max(abs(float(meta["max"]) - float(meta["min"])), 1e-9)
    return NO_RANGE_TOL


def same_value(meta: dict, got: float, want: float) -> bool:
    kind = meta.get("kind")
    if kind == "Switch":
        return (got >= 0.5) == (want >= 0.5)
    if kind == "Enum" or "min" not in meta:
        return round(got) == round(want) if abs(want - round(want)) < 1e-9 else abs(got - want) <= NO_RANGE_TOL
    return abs(got - want) <= _tol(meta)


def _extra_key(node: dict, extra: int) -> str | None:
    if extra != 0:
        return None
    for k in ("@mic", "@trails"):
        if k in node:
            return k
    return None


def _compare_params(where: str, node: dict, want: dict, dev_params: list[dict], diff: ReadbackDiff) -> None:
    seen = set()
    for p in dev_params or []:
        pid = p.get("sym") or p.get("id")
        if pid is None and "extra" in p:
            pid = _extra_key(node, int(p["extra"]))
            if pid is None:
                diff.unspecified.append(f"{where}: extra value {p['extra']} = {p['value']} (file names none)")
                continue
        if pid is None:
            continue
        seen.add(pid)
        if pid not in want:
            diff.unspecified.append(f"{where}.{pid} = {p['value']} (not in the file; base or default kept)")
            continue
        w = _num(want[pid])
        if w is None:
            continue
        diff.checked += 1
        got = float(p["value"])
        if not same_value(p, got, w):
            diff.mismatches.append(f"{where}.{pid}: device {got:.6g} != file {w:.6g} (tol {_tol(p):.2g}, {p.get('kind')})")
    for pid, v in node.items():
        if pid.startswith("@") and pid not in ("@mic", "@trails"):
            continue
        if pid not in seen and _num(v) is not None:
            diff.missing.append(f"{where}.{pid}: in the file, no such parameter on the device")


def readback_diff(chain: dict, hlx: dict, report: dict | None = None, check_input: bool = True) -> ReadbackDiff:
    """Compare every parameter of every block the .hlx names with a chain (`chain` command, or
    `stompd decode` of a document, which also carries the snapshot controller values)."""
    dsp = _tone(hlx).get("dsp0", {})
    want = effective_values(hlx)
    where = slot_map(hlx, report)
    dev = {s["pos"]: s for s in chain.get("slots", []) if s.get("kind") == "Block"}
    diff = ReadbackDiff()
    for key, (pos, is_cab) in sorted(where.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        node, d = dsp[key], dev.get(pos)
        if d is None:
            diff.mismatches.append(f"{key}: no block at device slot {pos}")
            continue
        model = d.get("paired_id") if is_cab else d.get("model_id")
        diff.checked += 1
        if model != node["@model"]:
            diff.mismatches.append(f"{key}: device slot {pos} {'cab' if is_cab else 'model'} {model} != file {node['@model']}")
            continue
        if not is_cab:
            diff.checked += 1
            if bool(d.get("enabled")) != bool(want[key].get("@enabled", True)):
                diff.mismatches.append(f"{key}: enabled {d.get('enabled')} != file {want[key].get('@enabled', True)}")
        _compare_params(key, node, want[key], d.get("paired_params" if is_cab else "params"), diff)
    named = {pos for pos, _ in where.values()}
    for pos, d in dev.items():
        if pos not in named:
            (diff.mismatches if d.get("enabled") else diff.unspecified).append(
                f"device slot {pos} holds {d.get('model_id')} ({'on' if d.get('enabled') else 'off'}); "
                "the file has no block there")
    if check_input and "@input" in dsp.get("inputA", {}):
        inp = next((s for s in chain.get("slots", []) if s.get("kind") == "Input" and s.get("pos") == 0), None)
        diff.checked += 1
        if inp is None or inp.get("routing") != dsp["inputA"]["@input"]:
            diff.mismatches.append(f"inputA routing {inp and inp.get('routing')} != file {dsp['inputA']['@input']}")
    _compare_junctions(chain, hlx, diff)
    _compare_snapshot_controllers(chain, hlx, where, dev, diff)
    return diff


def _compare_junctions(chain: dict, hlx: dict, diff: ReadbackDiff) -> None:
    """Two-path files only (T063): the split and join model, enabled and values, and the attach
    slot when the chain reports it (`stompd decode` of a document does; the live `chain` of the
    USB helper does not)."""
    from .stomp_layout import device_topology, junction_expectations

    exp = junction_expectations(hlx)
    if not exp:
        return
    want_topo = device_topology(hlx)
    if chain.get("path_topology") is not None and want_topo is not None:
        diff.checked += 1
        if chain["path_topology"] != want_topo:
            diff.mismatches.append(f"path topology code {chain['path_topology']} != {want_topo} for the file's layout "
                                   "(the device re-derives a layout it does not accept)")
    dsp = _tone(hlx).get("dsp0", {})
    have = {s.get("kind", "").lower(): s for s in chain.get("slots", []) if s.get("kind") in ("Split", "Join")}
    for key, e in exp.items():
        d = have.get(key)
        diff.checked += 1
        if d is None:
            diff.mismatches.append(f"{key}: the device has no {key}")
            continue
        if d.get("model_id") != e["model"]:
            diff.mismatches.append(f"{key}: device model {d.get('model_id')} != file {e['model']}")
            continue
        diff.checked += 1
        if bool(d.get("enabled")) != e["enabled"]:
            diff.mismatches.append(f"{key}: enabled {d.get('enabled')} != file {e['enabled']}")
        _compare_params(key, dsp.get(key, {}), e["values"], d.get("params"), diff)
        if "attach" in d and e["attach"] is not None:
            diff.checked += 1
            if d["attach"] != e["attach"]:
                diff.mismatches.append(f"{key}: attaches before slot {d['attach']}, file @position gives {e['attach']}")


def _compare_snapshot_controllers(chain: dict, hlx: dict, where: dict, dev: dict, diff: ReadbackDiff) -> None:
    """Only a decoded document carries these (`snapshot_controllers`); a live chain does not."""
    entries = chain.get("snapshot_controllers")
    if not entries:
        return
    tone = _tone(hlx)
    dsp = tone.get("dsp0", {})
    by_slot = {(pos, cab): key for key, (pos, cab) in where.items()}
    for e in entries:
        if e.get("place") == UNUSED or e.get("slot") is None:
            continue
        key = by_slot.get((e["slot"], bool(e.get("paired"))))
        if key is None:
            continue
        d = dev.get(e["slot"], {})
        params = d.get("paired_params" if e.get("paired") else "params") or []
        meta = next((p for p in params if p.get("i") == e.get("param")), None)
        pid = meta and (meta.get("sym") or meta.get("id"))
        if not pid:
            continue
        n = int(e["snapshot"])
        ctl = (((tone.get(f"snapshot{n}") or {}).get("controllers") or {}).get("dsp0") or {}).get(key) or {}
        w = _num((ctl.get(pid) or {}).get("@value"))
        if w is None:
            w = _num(dsp[key].get(pid))
        if w is None:
            continue
        diff.checked += 1
        if not same_value(meta, float(e["value"]), w):
            diff.mismatches.append(f"snapshot{n}.controllers.{key}.{pid}: document {float(e['value']):.6g} != file {w:.6g} "
                                   "(the device recalls this over the block value)")


def check(chain: dict, hlx: dict, report: dict | None = None, what: str = "readback",
          check_input: bool = True) -> ReadbackDiff:
    """readback_diff, raising ReadbackMismatch when it fails."""
    diff = readback_diff(chain, hlx, report, check_input=check_input)
    if not diff.ok:
        raise ReadbackMismatch(f"{what} does not match the .hlx: {diff.summary()}", diff)
    return diff
