"""Model catalog: locate the user's HX Edit install, derive a local cache, load it.

Nothing here ships Line 6 data. The importer reads the user's own install in place and writes
derived data to ``cache/catalog/`` (gitignored). Tests use synthetic installs only.

Source layout (verified on macOS HX Edit 3.51; Windows/Linux layouts are documented guesses,
the locator searches recursively so a differing sub-folder still resolves):
  *.models files, each a JSON list of models with ``symbolicID`` (the ``@model`` string),
  ``category``, ``load`` / ``load_stereo`` (DSP load), ``devices`` (device id + min firmware
  filter, absent = all) and ``params`` (``symbolicID``, ``valueType``, ``min``, ``max``,
  ``default``). ``valueType``: 0 stepped int, 1 continuous float, 2 bool, 3 string.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

STOMP_DEVICE_ID = 2162694
SCHEMA = 1

# .models "category" number -> template category. Files override where the number is shared.
_CATEGORY_BY_NUMBER = {
    1: "amp", 13: "amp", 2: "cab", 19: "cab", 3: "drive", 4: "comp",
    14: "eq", 8: "mod", 9: "delay", 10: "reverb",
}
_CATEGORY_BY_FILE = {"gate": "other"}
KINDS = {0: "stepped", 1: "continuous", 2: "bool", 3: "string"}
CATEGORIES = ("comp", "drive", "amp", "cab", "eq", "mod", "delay", "reverb", "other")

# Candidate install roots per OS. Order is irrelevant; the newest version wins.
_MAC = ["/Applications/Line6/HX Edit.app", "/Applications/HX Edit.app",
        "~/Applications/Line6/HX Edit.app", "~/Applications/HX Edit.app"]
_WIN = ["C:/Program Files/Line 6/HX Edit", "C:/Program Files (x86)/Line 6/HX Edit",
        "C:/Program Files/Line 6/HX Edit*", "C:/Program Files (x86)/Line 6/HX Edit*"]
_LINUX_WINE = ["~/.wine/drive_c/Program Files/Line 6/HX Edit",
               "~/.wine/drive_c/Program Files (x86)/Line 6/HX Edit"]


class CatalogError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_cache_dir() -> Path:
    return Path(os.environ.get("TONEMATCH_CACHE", repo_root() / "cache")) / "catalog"


# ----------------------------------------------------------------------------- locating


def _expand(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        p = Path(pat).expanduser()
        if any(c in pat for c in "*?["):
            anchor = Path(p.anchor) if p.anchor else Path(".")
            rel = str(p.relative_to(anchor)) if p.anchor else str(p)
            out.extend(sorted(anchor.glob(rel)))
        elif p.exists():
            out.append(p)
    return out


def candidate_roots(platform: str | None = None) -> list[Path]:
    platform = platform or sys.platform
    pats = list(_MAC) if platform == "darwin" else _WIN if platform.startswith("win") else _LINUX_WINE
    env = os.environ.get("HXEDIT_PATH")
    roots = _expand(pats)
    if env:
        roots.insert(0, Path(env).expanduser())
    return roots


def find_resource_dir(root: Path, max_depth: int = 4) -> Path | None:
    """Folder under ``root`` holding the model JSON (``*.models`` files)."""
    if not root.is_dir():
        return None
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base >= max_depth:
            dirnames[:] = []
        if any(f.endswith(".models") for f in filenames):
            return Path(dirpath)
    return None


def read_version(root: Path, resource_dir: Path) -> str:
    """HX Edit version: macOS Info.plist, else a version-like folder name, else 'unknown'."""
    for plist in (root / "Contents" / "Info.plist", resource_dir.parent / "Info.plist"):
        if plist.is_file():
            try:
                info = plistlib.loads(plist.read_bytes())
                v = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
                if v:
                    return str(v)
            except (plistlib.InvalidFileException, ValueError, OSError, AttributeError):
                continue
    for part in (root.name, *[p.name for p in root.parents][:2]):
        m = re.search(r"\d+(?:\.\d+)+", part)
        if m:
            return m.group(0)
    return "unknown"


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (-1,)


def locate(explicit: str | Path | None = None, platform: str | None = None) -> dict:
    """Return {'root', 'resource_dir', 'version'} for the newest install, or raise.

    Never guesses beyond the documented candidates and never downloads anything.
    """
    roots = [Path(explicit).expanduser()] if explicit else candidate_roots(platform)
    found = []
    for r in roots:
        rd = find_resource_dir(r)
        if rd:
            found.append({"root": r, "resource_dir": rd, "version": read_version(r, rd)})
    if not found:
        tried = ", ".join(str(r) for r in roots) or "(no candidate exists on this machine)"
        raise CatalogError(
            "HX Edit resources not found. Tried: " + tried +
            ". Pass the install path with --hxedit-path or set HXEDIT_PATH."
        )
    found.sort(key=lambda f: (_vkey(f["version"]), f["resource_dir"].stat().st_mtime))
    return found[-1]


# ----------------------------------------------------------------------------- importing


def _supports_stomp(devices) -> tuple[bool, str | None]:
    if not devices:
        return True, None
    for d in devices:
        if d.get("id") == STOMP_DEVICE_ID:
            return True, d.get("version")
    return False, None


def _param(p: dict) -> dict:
    out = {
        "name": p.get("name", p["symbolicID"]),
        "kind": KINDS.get(p.get("valueType"), "unknown"),
        "min": p.get("min"),
        "max": p.get("max"),
        "default": p.get("default"),
    }
    if "displayType" in p:
        out["display"] = p["displayType"]
    for k_src, k_dst in (("max_stereo", "max_stereo"), ("default_stereo", "default_stereo"),
                         ("stereo-only", "stereo_only")):
        if k_src in p:
            out[k_dst] = p[k_src]
    return out


def parse_models_file(path: Path) -> tuple[dict, dict]:
    """Return (models, globals) from one ``.models`` file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    stem = path.name[: -len(".models")]
    models: dict[str, dict] = {}
    glob: dict[str, dict] = {}
    for m in data:
        mid = m["symbolicID"]
        params = {p["symbolicID"]: _param(p) for p in m.get("params", [])}
        if mid.startswith("@"):  # e.g. @global_params
            glob.update(params)
            continue
        cat = _CATEGORY_BY_FILE.get(stem) or _CATEGORY_BY_NUMBER.get(m.get("category"), "other")
        stomp, min_fw = _supports_stomp(m.get("devices"))
        entry = {
            "id": mid,
            "name": m.get("name", mid),
            "category": cat,
            "native_category": m.get("category"),
            "source_file": path.name,
            "mono": m.get("mono", True),
            "stereo": m.get("stereo", False),
            "load": m.get("load"),
            "load_stereo": m.get("load_stereo"),
            "stomp": stomp,
            "stomp_min_firmware": min_fw,
            "params": params,
        }
        if stem == "preamp":
            entry["preamp"] = True
        for k in ("cablink", "ircablink"):
            if k in m:
                entry[k] = m[k]
        models[mid] = entry
    return models, glob


def build_catalog(resource_dir: Path, version: str, source_root: Path | None = None) -> dict:
    files = sorted(resource_dir.glob("*.models"))
    if not files:
        raise CatalogError(f"no *.models files in {resource_dir}")
    models: dict[str, dict] = {}
    glob: dict[str, dict] = {}
    for f in files:
        try:
            m, g = parse_models_file(f)
        except (ValueError, KeyError, TypeError) as e:
            raise CatalogError(f"cannot parse {f.name}: {e}") from e
        models.update(m)
        glob.update(g)
    return {
        "_source": {
            "path": str(resource_dir),
            "install_root": str(source_root) if source_root else None,
            "hxedit_version": version,
            "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "files": [f.name for f in files],
            "note": "Derived from the user's own HX Edit install; local cache, never commit.",
        },
        "schema": SCHEMA,
        "globals": glob,
        "models": models,
    }


def summarize(cat: dict) -> dict:
    per: dict[str, int] = {c: 0 for c in CATEGORIES}
    nparams = 0
    for m in cat["models"].values():
        per[m["category"]] = per.get(m["category"], 0) + 1
        nparams += len(m["params"])
    return {
        "version": cat["_source"]["hxedit_version"],
        "models": len(cat["models"]),
        "stomp_models": sum(1 for m in cat["models"].values() if m["stomp"]),
        "per_category": per,
        "params": nparams,
    }


def write_cache(cat: dict, cache_dir: Path | None = None) -> list[Path]:
    cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", cat["_source"]["hxedit_version"])
    text = json.dumps(cat, indent=1, sort_keys=False)
    paths = [cache_dir / f"{safe}.json", cache_dir / "current.json"]
    for p in paths:
        p.write_text(text, encoding="utf-8")
    s = summarize(cat)
    with (cache_dir / "IMPORTS.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{cat['_source']['imported_at']} version={s['version']} models={s['models']} "
                 f"params={s['params']} source={cat['_source']['path']}\n")
    return paths


def load_catalog(version: str = "current", cache_dir: Path | None = None) -> dict:
    p = Path(cache_dir) if cache_dir else default_cache_dir()
    p = p / f"{version}.json"
    if not p.is_file():
        raise CatalogError(f"{p} missing; run the catalog-import skill (scripts/catalog_import.py)")
    return json.loads(p.read_text(encoding="utf-8"))


def native_unavailable() -> set[str]:
    """Models in HX Edit's catalog that the user's Helix Native refuses ("Invalid model"), from
    native-engine's capture log (``invalid_models()``; lead-approved 2026-10-16: treat these as
    not available on Native). Empty when Native was never scanned or is not installed."""
    try:
        from .engines.native import invalid_models
    except Exception:  # noqa: BLE001 - the Native engine's optional deps are missing
        return set()
    return invalid_models()


def available_on(model_id: str, engine: str, catalog: dict | None = None,
                 unavailable: set[str] | None = None) -> bool:
    """Whether ``engine`` (``"stomp"`` or ``"native"``) can load ``model_id``: Stomp needs the
    catalog's ``stomp`` flag, Native also excludes ``native_unavailable()`` (pass ``unavailable``
    to avoid re-reading it)."""
    m = (catalog or {}).get("models", {}).get(model_id) if catalog else None
    if engine == "stomp":
        return m is None or bool(m.get("stomp", True))
    if engine == "native":
        return model_id not in (native_unavailable() if unavailable is None else unavailable)
    raise ValueError(f"unknown engine {engine!r}")


# ----------------------------------------------------------------------------- cross-check


def crosscheck_hlx(cat: dict, hlx_paths: list[Path]) -> list[str]:
    """Every ``@model`` and non-``@`` parameter key in the templates must resolve."""
    problems: list[str] = []
    models = cat["models"]
    for path in hlx_paths:
        try:
            tone = json.loads(Path(path).read_text(encoding="utf-8"))["data"]["tone"]
        except (OSError, ValueError, KeyError) as e:
            problems.append(f"{path}: unreadable ({e})")
            continue
        for dsp, dsp_v in tone.items():
            if not (dsp.startswith("dsp") and isinstance(dsp_v, dict)):
                continue
            for bname, block in dsp_v.items():
                if not (isinstance(block, dict) and "@model" in block):
                    continue
                mid = block["@model"]
                if mid.startswith("HD2_App"):  # DSP-flow plumbing (inputs, outputs, split, join)
                    continue
                m = models.get(mid)
                if m is None:
                    problems.append(f"{Path(path).name}:{dsp}.{bname}: unknown model {mid}")
                    continue
                for k in block:
                    if not k.startswith("@") and k not in m["params"]:
                        problems.append(f"{Path(path).name}:{dsp}.{bname}: {mid} has no param {k!r}")
    return problems
