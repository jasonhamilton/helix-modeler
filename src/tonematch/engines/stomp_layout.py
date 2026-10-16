"""Two-path (Path A + Path B) layout on the HX Stomp, as HX Edit writes it (T063, stomp-engine).

HX Edit stores every two-path preset of the human's setlist (42 of 126) as the Y-split topology
``global.@topology0 = "SABJ"``: one input, a Split, Path A and Path B in parallel, a Join, one
output. Path B has no input or output of its own (``inputB.@input`` = ``outputB.@output`` = 0).
The Split Y feeds both paths at Balance A = Balance B = 0.5 (centre) and the Join mixes them at
A Pan = B Pan = 0.5 (centre), 0 dB each. These are also the catalog defaults.

A preset our tools generate from a single-path template keeps that template's junctions: in the
HX Stomp golden those are Balance A/B 0/1 and A/B Pan 0/1 (hard left / hard right), topology "A".
:func:`ensure_sabj` gives such a file HX Edit's layout as soon as it has a block on Path B, so
both paths get the same signal and are mixed equally. A file that already says SABJ (an HX Edit
export) is left exactly as it is; ``stompd-compile`` then compiles its split and join, including
the attach points, from the file (see docs/notes/stompd.md).

Interop only: values and keys are read from the human's own HX Edit exports, nothing is copied
from Line 6 files.
"""
from __future__ import annotations

SABJ = "SABJ"
SAB = "SAB"   # Path B with its own output (dual renders); kept as written
SPLIT_Y = "HD2_AppDSPFlowSplitY"
JOIN = "HD2_AppDSPFlowJoin"
# HX Edit's values on a two-path preset (36 of 36 Split Y, 38 of 42 Joins in the setlist survey).
HX_EDIT_SPLIT_Y = {"BalanceA": 0.5, "BalanceB": 0.5, "bypass": False}
HX_EDIT_JOIN = {"A Level": 0, "A Pan": 0.5, "B Level": 0, "B Pan": 0.5, "B Polarity": False, "Level": 0}
# A first block on an empty Path B parallels the whole row (HX Edit and the device both do this):
# split before cell 0, join before the output (cell 8 on the Stomp's 8-cell row).
FULL_ROW = (0, 8)


def _dsp0(hlx: dict) -> dict:
    return hlx.setdefault("data", {}).setdefault("tone", {}).setdefault("dsp0", {})


def path_b_blocks(hlx: dict) -> list[str]:
    """Keys of the blocks on Path B (``@path`` 1)."""
    return sorted(k for k, v in _dsp0(hlx).items()
                  if k.startswith("block") and isinstance(v, dict) and int(v.get("@path", 0) or 0) == 1)


def topology(hlx: dict) -> str:
    return str(hlx.get("data", {}).get("tone", {}).get("global", {}).get("@topology0", "A"))


def ensure_sabj(hlx: dict, split_position: int | None = None, join_position: int | None = None) -> list[str]:
    """Give a two-path .hlx dict HX Edit's SABJ layout, in place. Returns what changed.

    No-op (returns []) when the file has no Path B block, or already says SABJ (an HX Edit export:
    its own split and join values are kept) or SAB (Path B with its own output, written on purpose). Otherwise: ``@topology0`` = SABJ; a Split Y and a Join
    with HX Edit's values (Balance A/B and A/B Pan 0.5, levels 0 dB); split and join positions from
    the arguments, else the file's own, else the full row (0, 8); Path B input and output 0 (fed by
    the split, merged by the join); ``split`` on in every snapshot that lists blocks."""
    if not path_b_blocks(hlx) or topology(hlx) in (SABJ, SAB):
        return []
    changes: list[str] = []
    tone = hlx["data"]["tone"]
    dsp = _dsp0(hlx)
    g = tone.setdefault("global", {})
    changes.append(f"global.@topology0: {g.get('@topology0')!r} -> {SABJ!r}")
    g["@topology0"] = SABJ
    for key, model, values, pos in (("split", SPLIT_Y, HX_EDIT_SPLIT_Y, split_position),
                                    ("join", JOIN, HX_EDIT_JOIN, join_position)):
        node = dsp.setdefault(key, {})
        if node.get("@model") != model:
            changes.append(f"{key}.@model: {node.get('@model')!r} -> {model!r}")
            # Values of another junction type do not apply to this one.
            for k in [k for k in node if not k.startswith("@")]:
                del node[k]
            node["@model"] = model
        for k, v in values.items():
            if node.get(k) != v:
                changes.append(f"{key}.{k}: {node.get(k)!r} -> {v!r}")
                node[k] = v
        default = FULL_ROW[0] if key == "split" else FULL_ROW[1]
        want = pos if pos is not None else node.get("@position", default)
        if node.get("@position") != want:
            changes.append(f"{key}.@position: {node.get('@position')!r} -> {want!r}")
        node["@position"] = int(want)
        if node.get("@enabled") is not True:
            changes.append(f"{key}.@enabled: {node.get('@enabled')!r} -> True")
            node["@enabled"] = True
        node.setdefault("@no_snapshot_bypass", False)
    if int(dsp["split"]["@position"]) >= int(dsp["join"]["@position"]):
        raise ValueError(f"split @position {dsp['split']['@position']} must be before join @position "
                         f"{dsp['join']['@position']}")
    for key, field in (("inputB", "@input"), ("outputB", "@output")):
        node = dsp.get(key)
        if node is not None and node.get(field, 0) != 0:
            changes.append(f"{key}.{field}: {node.get(field)!r} -> 0")
            node[field] = 0
    for n in range(8):
        blocks = (tone.get(f"snapshot{n}") or {}).get("blocks", {}).get("dsp0")
        if isinstance(blocks, dict) and blocks.get("split") is not True:
            blocks["split"] = True
            changes.append(f"snapshot{n}.blocks.split -> True")
    return changes


# The path's topology code on the device (tone[0][21], `stompd decode` "path_topology"), fw 3.80:
# 0 = A (single path), 1 = SABJ (HX Edit's two-path presets), 2 = SAB (Path B with its own output).
TOPOLOGY_CODES = {"A": 0, SABJ: 1, SAB: 2}


def device_topology(hlx: dict) -> int | None:
    """The topology code a two-path file compiles to (a Path B block on an "A" file: SABJ, or SAB
    when Path B has its own output), None for a single-path file."""
    if not path_b_blocks(hlx):
        return None
    t = topology(hlx)
    if t == "A":
        out_b = int((_dsp0(hlx).get("outputB") or {}).get("@output", 0) or 0)
        return TOPOLOGY_CODES[SAB] if out_b else TOPOLOGY_CODES[SABJ]
    return TOPOLOGY_CODES.get(t)


def junction_expectations(hlx: dict) -> dict:
    """What a two-path file asks of the device's split and join (for readback): per junction its
    model, enabled, the values the file names, and the attach slot (``@position`` + 1 on the Stomp,
    whose Path A input is slot 0). Empty for a single-path file."""
    if not path_b_blocks(hlx):
        return {}
    dsp = _dsp0(hlx)
    out = {}
    for key in ("split", "join"):
        node = dsp.get(key) or {}
        out[key] = {"model": node.get("@model"), "enabled": bool(node.get("@enabled", True)),
                    "attach": 1 + int(node["@position"]) if "@position" in node else None,
                    "values": {k: v for k, v in node.items() if not k.startswith("@")}}
    return out
