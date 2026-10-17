"""Scan a user's impulse-response (IR) library and parse what its folder and file names say.

The library is the user's own (for example `data/ir/`, gitignored). Nothing here copies IR audio; the
parser only reads paths. Names follow no single standard, so every field is a best effort and
`source` says where a cab size came from ("name" when spelled out, "inferred" from a well-known
amp or cab name, None when unknown).

    from tonematch.ir_library import scan_library, parse_ir_name
    entries = scan_library("data/ir")
"""
from __future__ import annotations

import re
from pathlib import Path

# Top folders that are not a cab brand.
SPECIAL_GROUPS = {
    "##Acoustic##": "acoustic",
    "##BASS##": "bass",
    "##MISC##": "misc",
    "##Metal Artist & Song IRs##": "artist",
}

# Leaf folders that only name a microphone (the cab is the parent folder).
_MIC_FOLDER = re.compile(r"^(?:SM ?57|SM ?58|SM7B|MD ?421|421|MD ?409|R ?121|M ?160|U ?87|I5|KM ?84|e?906|"
                         r"\d{5} \d{3}ms)$", re.IGNORECASE)

# (regex, canonical name). Order matters only for readability; all matches are collected.
MICS: list[tuple[str, str]] = [
    (r"SM ?57|(?<![0-9A-Za-z])57(?![0-9])|-57\b|57(?=ON|OFF)", "SM57"),
    (r"SM ?58|Beta ?58A?", "SM58"),
    (r"SM ?7B", "SM7B"),
    (r"SM ?53", "SM53"),
    (r"MD ?421|D421|(?<![0-9A-Za-z])421(?![0-9])", "MD421"),
    (r"MD ?409|(?<![0-9A-Za-z])409(?![0-9])", "MD409"),
    (r"MD ?441|D441", "MD441"),
    (r"R ?121|(?<![0-9A-Za-z])121(?![0-9])", "R121"),
    (r"R ?101", "R101"),
    (r"M ?160|R160|(?<![0-9A-Za-z])160(?![0-9])", "M160"),
    (r"U ?87G?|(?<![0-9A-Za-z])87(?![0-9])", "U87"),
    (r"U ?47|E47", "U47-type"),
    (r"KM ?84", "KM84"),
    (r"C ?414|(?<![0-9A-Za-z])414(?![0-9])", "C414"),
    (r"AT ?4050|(?<![0-9A-Za-z])4050(?![0-9])", "AT4050"),
    (r"AT ?4033|(?<![0-9A-Za-z])4033(?![0-9])", "AT4033"),
    (r"e ?609|E609", "e609"),
    (r"e ?906|FAT-?906", "e906"),
    (r"(?<![0-9A-Za-z])I5(?![0-9])", "i5"),
    (r"NT1", "NT1"),
    (r"TLM ?103", "TLM103"),
    (r"(?<![0-9A-Za-z])D20(?![0-9])", "D20"),
    (r"(?<![0-9A-Za-z])D57(?![0-9])", "D57"),
    (r"C87", "C87"),
    (r"545", "545"),
    (r"P420", "P420"),
    (r"VM1", "VM1"),
    (r"DPA ?4011|(?<![0-9A-Za-z])4011", "DPA4011"),
    (r"M ?147", "M147"),
    (r"CS ?1000", "CS1000"),
    (r"CMC ?64", "CMC64"),
    (r"CU ?31", "CU31"),
    (r"Fathead", "Fathead"),
]

# Two-mic names written as one number: "57121", "421121", "5757", "57421".
_FUSED = re.compile(r"(?<![0-9])((?:57|121|421|160)(?:57|121|421|160))(?![0-9])")
_FUSED_PART = {"57": "SM57", "121": "R121", "421": "MD421", "160": "M160"}

SPEAKERS: list[tuple[str, str]] = [
    (r"V ?30|Vintage ?30", "V30"),
    (r"V ?60", "V60"),
    (r"G12M ?-?25|G12M|Greenback|G12M Green", "G12M"),
    (r"G12T-?75|T-?75", "G12T-75"),
    (r"G12H|H30", "G12H"),
    (r"G12-?65", "G12-65"),
    (r"Cream", "Cream"),
    (r"K ?100", "G12K-100"),
    (r"C90", "C90"),
    (r"EVH", "EVH"),
    (r"Alnico Silver|AC30 Silver", "Alnico Silver"),
    (r"Alnico Blue|AC30 Blue", "Alnico Blue"),
    (r"P10N", "P10N"),
    (r"P12N", "P12N"),
    (r"Utah", "Utah"),
    (r"S12X", "S12X"),
]

POSITIONS: list[tuple[str, str]] = [
    (r"CapEdge ?45", "cap-edge 45deg"),
    (r"CapEdge", "cap-edge"),
    (r"Cap ?45", "cap 45deg"),
    (r"(?<![A-Za-z])Cap(?![A-Za-z])", "cap"),
    (r"ConeEdge", "cone-edge"),
    (r"(?<![A-Za-z])Cone(?![A-Za-z])", "cone"),
    (r"Off ?axis|57OFF|angle ?45", "off-axis"),
    (r"57ON|on ?axis", "on-axis"),
]

_SIZE = re.compile(r"(?<![0-9])([1-8]) ?x ?(1[0258]|6x9|8|10|12|15)(?![0-9])", re.IGNORECASE)
_SIZE3 = re.compile(r"(?<![0-9])(412|212|112|410|810|210|115|215)(?![0-9])")
_GRID = re.compile(r"(?:^|[ _-])(\d{1,2})([A-S])$")
_VARIANT = re.compile(r"(?:^|[ _-])([A-F])$")
_DIST = re.compile(r"(\d+)(?: (5))? ?in$", re.IGNORECASE)

# Cab sizes of well-known amps or cabs whose names do not spell the size out.
INFERRED_SIZE = [
    (r"Princeton|Super ?Champ|G15R", "1x10"),
    (r"Vibrolux", "2x10"),
    (r"AC30|Vetta|Jazz Chorus|Bassman 2x12", "2x12"),
    (r"Bassman", "4x10"),
    (r"1960|JCM ?2000|Plexi|JCM Slash", "4x12"),
    (r"SVT", "8x10"),
    (r"SWR 15", "1x15"),
]


def _find_all(table: list[tuple[str, str]], text: str) -> list[str]:
    out: list[str] = []
    for pat, name in table:
        if re.search(pat, text) and name not in out:
            out.append(name)
    return out


def parse_ir_name(relpath: str | Path) -> dict:
    """Fields parsed from a library-relative path such as 'Brand/Cab 4x12/SM57/Cab 4x12 SM57 1A.wav'."""
    p = Path(relpath)
    parts = list(p.parts)
    stem = p.stem
    top = parts[0] if len(parts) > 1 else ""
    group = SPECIAL_GROUPS.get(top, "guitar")
    folders = parts[:-1]
    cab_folder = folders[-1] if folders else ""
    if len(folders) >= 2 and _MIC_FOLDER.match(cab_folder):
        cab_folder = folders[-2]
    text = " ".join([*folders, stem]) if group == "guitar" else stem
    mics = _find_all(MICS, stem) or (_find_all(MICS, folders[-1]) if folders else [])
    for m in _FUSED.finditer(stem):
        s = m.group(1)
        for part in (s[:2], s[2:]) if s[:2] == "57" else (s[:3], s[3:]):
            name = _FUSED_PART.get(part)
            if name and name not in mics:
                mics.append(name)
    size, source = None, None
    m = _SIZE.search(text) or _SIZE.search(stem)
    if m:
        size, source = f"{m.group(1)}x{m.group(2)}", "name"
    else:
        m3 = _SIZE3.search(text)
        if m3:
            s = m3.group(1)
            size, source = f"{s[0]}x{s[1:]}", "name"
        else:
            for pat, sz in INFERRED_SIZE:
                if re.search(pat, text, re.IGNORECASE):
                    size, source = sz, "inferred"
                    break
    grid = _GRID.search(stem)
    variant = _VARIANT.search(stem) if not grid else None
    dist = _DIST.search(stem)
    distance_in = None
    if dist:
        distance_in = float(dist.group(1)) + (0.5 if dist.group(2) else 0.0)
    return {
        "path": str(p),
        "group": group,
        "brand": top if group == "guitar" else None,
        "cab": cab_folder,
        "cab_size": size,
        "cab_size_source": source,
        "speakers": _find_all(SPEAKERS, text),
        "mics": mics,
        "multi_mic": len(mics) > 1,
        "position": (_find_all(POSITIONS, stem) or [None])[0],
        "distance_in": distance_in,
        "grid_row": int(grid.group(1)) if grid else None,
        "grid_col": grid.group(2) if grid else None,
        "variant": variant.group(1) if variant else None,
    }


def scan_library(root: str | Path) -> list[dict]:
    """Parse every .wav under `root` (sorted, case-insensitive suffix). Paths are relative to root."""
    root = Path(root)
    files = sorted(q for q in root.rglob("*") if q.is_file() and q.suffix.lower() == ".wav")
    return [parse_ir_name(q.relative_to(root)) for q in files]
