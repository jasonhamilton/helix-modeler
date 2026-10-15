"""Helix setlist (.hls) codec and MIDI-CC assignment for Native-ready presets (T018n).

Pure stdlib; reads and writes only our own files. Nothing here touches a Line 6 file.

Setlist format (observed 2026-10-14 on HX Edit 3.82 exports and Native 3.82's own setlists,
read in place): a JSON wrapper ``{version: 2, meta, encoding: "Base64", encoded_data,
compression: {type: "zlib", crc32, decompressed_size}, schema: "L6Setlist"}``. ``encoded_data``
is base64 of zlib-compressed JSON ``{meta: {name}, presets: [...]}``; ``crc32`` is CRC-32 of the
*decompressed* JSON. Each preset entry is exactly the ``data`` object of a ``.hlx`` file
(``{meta, device, tone, device_version}``). HX Stomp setlists hold 126 presets; Native imports
only through its GUI ("Setlist Import"), so this codec is for bulk hand-off, not for headless
loading (see docs/notes/native-spike.md, T018n).

MIDI-CC assignment: an ``.hlx`` ``tone.controller`` entry ``{"@controller": 8, "@cc": n,
"@min": lo, "@max": hi}`` makes Native drive that parameter from CC n. Native's VST3 exposes CC n
as host parameter ``cc_<n>`` (0..1 maps to @min..@max), so a dropped/loaded file with CC
assignments gives the host far more than the 16 automation knobs. Reserved CCs (Pilot's Guide
3.8x p. 59): in HC modes 0-3, 32, 49-81 (69 = snapshot); in Off mode only 0, 32, 69.
"""
from __future__ import annotations

import base64
import copy
import json
import time
import zlib
from pathlib import Path

MIDI_CC_CONTROLLER = 8
STOMP_SETLIST_SIZE = 126
SETLIST_SIZE = 128

_RESERVED_HC = set(range(4)) | {32} | set(range(49, 82))
_RESERVED_OFF = {0, 32, 69}


def free_ccs(hc_mode: str = "stomp") -> list[int]:
    """CC numbers Native lets a preset assign (Pilot's Guide p. 59). 90 in HC modes, 125 in Off."""
    reserved = _RESERVED_OFF if hc_mode == "off" else _RESERVED_HC
    return [c for c in range(128) if c not in reserved]


def encode_hls(presets: list[dict], name: str, device: int | None = None,
               application: str = "tonematch") -> dict:
    """Setlist wrapper dict from ``.hlx`` dicts (``Preset.to_hlx_dict()``) or their ``data``."""
    entries = [copy.deepcopy(p.get("data", p)) for p in presets]
    if not entries:
        raise ValueError("a setlist needs at least one preset")
    dev = device if device is not None else entries[0].get("device")
    limit = STOMP_SETLIST_SIZE if dev == 2162694 else SETLIST_SIZE
    if len(entries) > limit:
        raise ValueError(f"{len(entries)} presets > {limit} slots for device {dev}")
    payload = json.dumps({"meta": {"name": name}, "presets": entries}, separators=(",", ":")).encode()
    meta = {"name": name, "application": application, "modifieddate": int(time.time())}
    if dev is not None:
        meta["device"] = dev
    dv = entries[0].get("device_version")
    if dv is not None:
        meta["device_version"] = dv
    return {"version": 2, "meta": meta, "encoding": "Base64",
            "encoded_data": base64.b64encode(zlib.compress(payload, 9)).decode(),
            "compression": {"type": "zlib", "crc32": zlib.crc32(payload),
                            "decompressed_size": len(payload)},
            "schema": "L6Setlist"}


def decode_hls(obj: dict | str | Path) -> dict:
    """``{meta, presets}`` from a setlist dict or path. Checks the CRC and size."""
    if not isinstance(obj, dict):
        obj = json.loads(Path(obj).read_text())
    if obj.get("schema") != "L6Setlist" or obj.get("encoding") != "Base64":
        raise ValueError("not an L6Setlist with Base64 encoding")
    comp = obj.get("compression", {})
    raw = base64.b64decode(obj["encoded_data"])
    data = zlib.decompress(raw) if comp.get("type") == "zlib" else raw
    if "crc32" in comp and zlib.crc32(data) != comp["crc32"]:
        raise ValueError("setlist CRC-32 mismatch")
    if "decompressed_size" in comp and len(data) != comp["decompressed_size"]:
        raise ValueError("setlist size mismatch")
    return json.loads(data)


def write_hls(presets: list[dict], name: str, path: str | Path, **kw) -> Path:
    p = Path(path)
    p.write_text(json.dumps(encode_hls(presets, name, **kw), indent=1))
    return p


def assign_ccs(hlx: dict, targets: list[tuple[int, str]], hc_mode: str = "stomp",
               ranges: dict[str, tuple[float, float]] | None = None,
               ) -> tuple[dict, dict[str, str], dict[str, list[float]], list[str]]:
    """Write MIDI-CC assignments into an ``.hlx`` dict for ``targets`` = [(slot_index, param)].

    Slot indices follow ``Preset.slots`` order with the blocks' file keys taken from the dict's
    ``dsp0`` (slots carry ``key``). Returns ``(hlx, map, ranges, skipped)`` where ``map`` and
    ``ranges`` are in ``ChainTemplate`` form (``"<slot>:<param>" -> "cc_<n>"``, ``[lo, hi]``).
    Params without a known range are given [0, 1] only if their current value lies in [0, 1];
    others are skipped (pass ``ranges`` from the catalog to include them). A parameter can have
    only one controller, so any existing assignment on a target is replaced.
    """
    out = copy.deepcopy(hlx)
    tone = out["data"]["tone"]
    keys = _slot_keys(tone)
    ccs = iter(free_ccs(hc_mode))
    ctl = tone.setdefault("controller", {})
    used = {v.get("@cc") for dsp in ctl.values() for blk in dsp.values() for v in blk.values()
            if v.get("@controller") == MIDI_CC_CONTROLLER}
    amap: dict[str, str] = {}
    rng: dict[str, list[float]] = {}
    skipped: list[str] = []
    for slot, param in targets:
        ref = f"{slot}:{param}"
        if slot >= len(keys):
            skipped.append(ref)
            continue
        dsp, key = keys[slot]
        block = tone[dsp].get(key, {})
        if param not in block or isinstance(block[param], bool):
            skipped.append(ref)
            continue
        lo_hi = (ranges or {}).get(ref)
        if lo_hi is None:
            v = block[param]
            if not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
                skipped.append(ref)
                continue
            lo_hi = (0.0, 1.0)
        cc = next(c for c in ccs if c not in used)
        ctl.setdefault(dsp, {}).setdefault(key, {})[param] = {
            "@controller": MIDI_CC_CONTROLLER, "@cc": cc, "@min": float(lo_hi[0]),
            "@max": float(lo_hi[1]), "@snapshot_disable": False}
        amap[ref] = f"cc_{cc}"
        rng[ref] = [float(lo_hi[0]), float(lo_hi[1])]
    return out, amap, rng, skipped


def _slot_keys(tone: dict) -> list[tuple[str, str]]:
    """(dsp, block key) per model block in ``Preset.slots`` order: blocks by @position, a block's
    cab right after it (as ``preset.py`` orders slots), on dsp0 then dsp1."""
    out = []
    for dsp in ("dsp0", "dsp1"):
        d = tone.get(dsp, {})
        blocks = sorted(((v.get("@position", 0), k) for k, v in d.items()
                         if k.startswith("block") and isinstance(v, dict) and "@model" in v))
        for _, k in blocks:
            out.append((dsp, k))
            cab = d[k].get("@cab")
            if cab and cab in d:
                out.append((dsp, cab))
    return out
