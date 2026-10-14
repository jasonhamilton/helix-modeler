"""Helix Native saved-state analysis and codec (pure Python, no plugin host imported here).

A VST3 plugin hosted by pedalboard (JUCE) exposes ``raw_state`` as JUCE's wrapper:

    b"VC2!" + uint32_le(len(xml)) + xml + b"\\0"
    xml = <VST3PluginState><IComponent>N.b64</IComponent><IEditController>N.b64</IEditController></VST3PluginState>

where ``N.b64`` is JUCE ``MemoryBlock::toBase64Encoding`` (byte count, a dot, then JUCE's own
6-bit alphabet, little-endian bit order; it is not RFC 4648 base64). The plugin's own state is
the IComponent bytes. Verified on this Mac with a stand-in VST3 (docs/notes/native-spike.md).

What sits inside Helix Native's IComponent bytes is the open question of task T002. ``scan``
walks the bytes recursively looking for JSON, XML, zlib, gzip and base64 layers, and flags the
ones that look like a Helix preset (``"tone"``, ``"dsp0"``, ``"@model"``, ``HD2_``). If one is
found, ``Locator`` records the path of layers to it and ``replace`` writes a new document back
along that path, fixing up length fields it can identify. The round trip is only trusted once
``scripts/native_spike.py`` proves it against the real plugin (state read back equals state
written, and the render matches a GUI-loaded preset).

No Line 6 data is stored in this module or its tests; state blobs are read at runtime and any
dumps go to ``cache/native/`` (gitignored).
"""
from __future__ import annotations

import base64
import binascii
import gzip
import json
import math
import re
import struct
import zlib
from dataclasses import dataclass, field

JUCE_MAGIC = b"VC2!"
_JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"
_JUCE_B64_INDEX = {c: i for i, c in enumerate(_JUCE_B64)}
HELIX_MARKERS = (b'"tone"', b'"dsp0"', b'"@model"', b"HD2_", b'"device"', b"snapshot")


# ----------------------------------------------------------------------------- JUCE layers


def juce_b64_decode(text: str) -> bytes:
    """Decode JUCE MemoryBlock::toBase64Encoding ("<size>.<chars>")."""
    size_s, _, body = text.strip().partition(".")
    if not size_s.isdigit():
        raise ValueError("not a JUCE base64 string (missing '<size>.')")
    size = int(size_s)
    out = bytearray(size)
    pos = 0
    for ch in body:
        v = _JUCE_B64_INDEX.get(ch)
        if v is None:
            raise ValueError(f"bad JUCE base64 char {ch!r}")
        for b in range(6):  # setBitRange(pos, 6, v), LSB first
            bit = pos + b
            if bit // 8 >= size:
                break
            if (v >> b) & 1:
                out[bit // 8] |= 1 << (bit % 8)
        pos += 6
    return bytes(out)


def juce_b64_encode(data: bytes) -> str:
    nbits = len(data) * 8
    chars = []
    for pos in range(0, nbits, 6):
        v = 0
        for b in range(6):
            bit = pos + b
            if bit < nbits and (data[bit // 8] >> (bit % 8)) & 1:
                v |= 1 << b
        chars.append(_JUCE_B64[v])
    return f"{len(data)}." + "".join(chars)


def unwrap_vc2(raw: bytes) -> str | None:
    """Return the XML string inside a JUCE VC2! blob, or None if it is not one."""
    if not raw.startswith(JUCE_MAGIC) or len(raw) < 8:
        return None
    n = struct.unpack("<I", raw[4:8])[0]
    return raw[8:8 + n].decode("utf-8", errors="replace")


def wrap_vc2(xml: str) -> bytes:
    b = xml.encode("utf-8")
    return JUCE_MAGIC + struct.pack("<I", len(b)) + b + b"\0"


def split_vst3_state(raw: bytes) -> dict[str, bytes]:
    """{'IComponent': bytes, 'IEditController': bytes} from a pedalboard raw_state blob."""
    xml = unwrap_vc2(raw)
    if xml is None:
        return {}
    out = {}
    for tag in ("IComponent", "IEditController"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
        if m:
            out[tag] = juce_b64_decode(m.group(1))
    return out


def join_vst3_state(raw_template: bytes, parts: dict[str, bytes]) -> bytes:
    """Rebuild a raw_state blob from a template blob, replacing the named parts."""
    xml = unwrap_vc2(raw_template)
    if xml is None:
        raise ValueError("template is not a JUCE VC2! blob")
    for tag, data in parts.items():
        xml, n = re.subn(rf"<{tag}>.*?</{tag}>", f"<{tag}>{juce_b64_encode(data)}</{tag}>", xml, flags=re.DOTALL)
        if n != 1:
            raise ValueError(f"tag {tag} not found exactly once in template")
    return wrap_vc2(xml)


# ----------------------------------------------------------------------------- scanning


@dataclass
class Layer:
    kind: str           # "zlib" | "gzip" | "b64" | "json" | "xml" | "text"
    offset: int         # byte offset inside the parent
    length: int         # bytes consumed in the parent
    info: dict = field(default_factory=dict)


@dataclass
class Finding:
    path: list[Layer]   # outermost first, relative to the scanned root bytes
    data: bytes         # the decoded bytes at the end of the path
    helix_markers: list[str]

    @property
    def kind(self) -> str:
        return self.path[-1].kind if self.path else "raw"

    def describe(self) -> str:
        p = " > ".join(f"{l.kind}@{l.offset}+{l.length}" for l in self.path) or "raw"
        return f"{p}: {len(self.data)} bytes, markers={self.helix_markers}"


def entropy(b: bytes) -> float:
    if not b:
        return 0.0
    counts = [0] * 256
    for x in b:
        counts[x] += 1
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def printable_ratio(b: bytes) -> float:
    if not b:
        return 0.0
    return sum(1 for x in b if 32 <= x < 127 or x in (9, 10, 13)) / len(b)


def markers(b: bytes) -> list[str]:
    return [m.decode() for m in HELIX_MARKERS if m in b]


def _try_zlib(buf: bytes, i: int) -> tuple[bytes, int] | None:
    d = zlib.decompressobj()
    try:
        out = d.decompress(buf[i:], 64 << 20)
    except zlib.error:
        return None
    if not d.eof or len(out) < 8:
        return None
    return out, len(buf) - i - len(d.unused_data)


def _try_gzip(buf: bytes, i: int) -> tuple[bytes, int] | None:
    d = zlib.decompressobj(wbits=31)
    try:
        out = d.decompress(buf[i:], 64 << 20)
    except zlib.error:
        return None
    if not d.eof:
        return None
    return out, len(buf) - i - len(d.unused_data)


def _json_span(buf: bytes, i: int) -> tuple[bytes, int] | None:
    """Longest valid JSON object starting at buf[i] == '{'."""
    try:
        text = buf[i:].decode("utf-8")
    except UnicodeDecodeError as e:  # cut at the first undecodable byte
        text = buf[i:i + e.start].decode("utf-8")
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    n = len(text[:end].encode("utf-8"))
    return buf[i:i + n], n


_B64_RUN = re.compile(rb"[A-Za-z0-9+/]{64,}={0,2}")


def scan(buf: bytes, depth: int = 4, _path: list[Layer] | None = None) -> list[Finding]:
    """Recursively find embedded documents. Returns every layer found, deepest last."""
    path = _path or []
    found: list[Finding] = []
    if depth <= 0 or not buf:
        return found
    # zlib / gzip streams anywhere
    for i in range(len(buf) - 2):
        b0, b1 = buf[i], buf[i + 1]
        r = None
        if b0 == 0x78 and b1 in (0x01, 0x5E, 0x9C, 0xDA):
            r = _try_zlib(buf, i)
            kind = "zlib"
        elif b0 == 0x1F and b1 == 0x8B:
            r = _try_gzip(buf, i)
            kind = "gzip"
        if r:
            data, used = r
            lay = Layer(kind, i, used, {"decompressed": len(data)})
            found.append(Finding(path + [lay], data, markers(data)))
            found.extend(scan(data, depth - 1, path + [lay]))
    # JSON objects (only at plausible starts, skip nested ones already covered)
    i, n = 0, len(buf)
    while i < n:
        j = buf.find(b"{", i)
        if j < 0:
            break
        nxt = buf[j + 1:j + 64].lstrip()
        if nxt[:1] in (b'"', b"}"):
            r = _json_span(buf, j)
            if r:
                data, used = r
                found.append(Finding(path + [Layer("json", j, used)], data, markers(data)))
                i = j + used
                continue
        i = j + 1
    # XML documents
    for m in re.finditer(rb"<\?xml|<[A-Za-z_][\w:.-]*[ >]", buf):
        j = m.start()
        tag = re.match(rb"<([A-Za-z_][\w:.-]*)", buf[j:]) if not buf.startswith(b"<?xml", j) else None
        end = buf.rfind(b">") + 1
        if tag:
            close = buf.rfind(b"</" + tag.group(1) + b">")
            if close < 0:
                continue
            end = close + len(tag.group(1)) + 3
        data = buf[j:end]
        if len(data) >= 16:
            found.append(Finding(path + [Layer("xml", j, end - j)], data, markers(data)))
        break  # one XML document per buffer is enough to locate it
    # long base64 runs (standard alphabet)
    for m in _B64_RUN.finditer(buf):
        try:
            data = base64.b64decode(m.group(0), validate=True)
        except (binascii.Error, ValueError):
            continue
        lay = Layer("b64", m.start(), m.end() - m.start())
        found.append(Finding(path + [lay], data, markers(data)))
        found.extend(scan(data, depth - 1, path + [lay]))
    return found


def best_preset_candidate(findings: list[Finding]) -> Finding | None:
    """The finding most likely to be a whole Helix preset: JSON with the most markers."""
    cands = [f for f in findings if f.kind == "json" and f.helix_markers]
    if not cands:
        cands = [f for f in findings if f.helix_markers]
    if not cands:
        return None
    return max(cands, key=lambda f: (len(f.helix_markers), len(f.data)))


def summarize(buf: bytes) -> dict:
    """A JSON-able description of one byte buffer, for the spike report."""
    fs = scan(buf)
    best = best_preset_candidate(fs)
    return {
        "bytes": len(buf),
        "head_hex": buf[:32].hex(),
        "entropy_bits_per_byte": round(entropy(buf), 3),
        "printable_ratio": round(printable_ratio(buf), 3),
        "markers_raw": markers(buf),
        "layers": [f.describe() for f in fs][:50],
        "preset_candidate": best.describe() if best else None,
    }


# ----------------------------------------------------------------------------- rewriting


def _fix_lengths(prefix: bytes, old_len: int, new_len: int, window: int = 16) -> bytes:
    """Rewrite the last uint32 (LE or BE) equal to old_len inside the trailing window of prefix."""
    if old_len == new_len:
        return prefix
    p = bytearray(prefix)
    lo = max(0, len(p) - window)
    for fmt in ("<I", ">I"):
        pat = struct.pack(fmt, old_len)
        k = bytes(p).rfind(pat, lo)
        if k >= 0:
            p[k:k + 4] = struct.pack(fmt, new_len)
            return bytes(p)
    return bytes(p)


def _encode_layer(lay: Layer, inner: bytes, old_inner: bytes) -> bytes:
    if lay.kind == "zlib":
        return zlib.compress(inner, 9)
    if lay.kind == "gzip":
        return gzip.compress(inner, 9, mtime=0)
    if lay.kind == "b64":
        return base64.b64encode(inner)
    return inner  # json / xml / text: the bytes themselves


def replace(root: bytes, finding_path: list[Layer], new_doc: bytes) -> bytes:
    """Write new_doc at the end of finding_path inside root, re-encoding every layer outward.

    Length fields directly before a replaced segment (uint32 LE or BE equal to the old segment
    length or old decoded length) are updated. Anything cleverer (checksums, offsets tables) is
    the spike's job to discover; the round-trip check catches it.
    """
    # collect the buffers along the path
    bufs = [root]
    for lay in finding_path:
        seg = bufs[-1][lay.offset:lay.offset + lay.length]
        if lay.kind == "zlib":
            seg = zlib.decompress(seg)
        elif lay.kind == "gzip":
            seg = gzip.decompress(seg)
        elif lay.kind == "b64":
            seg = base64.b64decode(seg)
        bufs.append(seg)
    inner = new_doc
    for depth in range(len(finding_path) - 1, -1, -1):
        lay = finding_path[depth]
        parent = bufs[depth]
        old_decoded = bufs[depth + 1]
        enc = _encode_layer(lay, inner, old_decoded)
        prefix = parent[:lay.offset]
        prefix = _fix_lengths(prefix, lay.length, len(enc))
        if lay.kind in ("zlib", "gzip"):
            prefix = _fix_lengths(prefix, len(old_decoded), len(inner))
        inner = prefix + enc + parent[lay.offset + lay.length:]
    return inner
