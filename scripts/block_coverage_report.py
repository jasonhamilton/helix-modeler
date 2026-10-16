#!/usr/bin/env python3
"""Block coverage report: Stomp and Helix Native side by side, per GUI category, per tested model.

Owner: benchmark. Regenerable (step 8 of the cycle skill, next to the status report). Everything is
derived from what is on disk; nothing is typed in:

  catalog      cache/catalog/current.json (HX Edit 3.82; Stomp flag and minimum firmware vs the pedal)
  presets      every .hlx that was rendered, written to the pedal or captured: cache/stomp/{inject,hw,
               live}, cache/native/{drop,spike,templates*}, cache/blockserial, cache/matches, fixtures/smoke
  parity       cache/parity/*/summary.json and cache/matches/**/summary.json (per preset JSON, stereo
               and residual files); rows voided in ledger/void.csv are skipped
  search       cache/matches/*/result.json (final parameter values per chain)
  ledger       ledger/runs.csv for the evidence links; src/tonematch/param_policy.json for known rules

Output (LOCAL ONLY, it names every Line 6 model, so the whole folder is gitignored):
  reports/local/blocks/index.html   dashboard, per-category tables, per-chain cards with SVG charts
  reports/local/blocks/README.md    the same tables as Markdown (no charts)
The generator refuses to write anywhere else. Spectra come from DI renders only (the parity stereo
files); song audio and stems are never opened. Standard library, numpy, scipy and soundfile only.

    python scripts/block_coverage_report.py [--root DIR] [--out DIR] [--format html,md] [--json FILE]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evidence_links
import task_links

PEDAL_FW = 0x03500000          # HX Stomp firmware on the bench, 3.50
PEDAL_FW_TEXT = "3.50"
NATIVE_TEXT = "3.82"
PARITY_DB = -25.0              # a Stomp-vs-Native null at or below this counts as "in parity"
SR = 48_000
OUT_SUBDIR = Path("reports") / "local"

GUI_CATEGORIES = [
    "Distortion", "Dynamics", "EQ", "Modulation", "Delay", "Reverb", "Pitch/Synth", "Filter",
    "Wah", "Volume/Pan", "Amp", "Preamp", "Cab", "Impulse Response", "Send/Return", "Looper",
]
# category -> (light/dark neutral colour). Fill is drawn at low opacity with a solid outline, and
# every block is also labelled in words, so colour is never the only cue.
CAT_COLOURS = {
    "Distortion": "#D55E00", "Dynamics": "#A0522D", "EQ": "#009E73", "Modulation": "#CC79A7",
    "Delay": "#0072B2", "Reverb": "#56B4E9", "Pitch/Synth": "#E69F00", "Filter": "#7B3294",
    "Wah": "#B2182B", "Volume/Pan": "#6B8E23", "Amp": "#E15759", "Preamp": "#F28E8E",
    "Cab": "#8C6D31", "Impulse Response": "#17A2B8", "Send/Return": "#6C757D", "Looper": "#999933",
}
SOURCE_CATEGORY = {
    "amp.models": "Amp", "preamp.models": "Preamp", "cab.models": "Cab", "cabmicirs.models": "Cab",
    "compressor.models": "Dynamics", "gate.models": "Dynamics", "distortion.models": "Distortion",
    "eq.models": "EQ", "modulation.models": "Modulation", "delay.models": "Delay",
    "reverb.models": "Reverb", "pitch-synth.models": "Pitch/Synth", "filter.models": "Filter",
    "wah.models": "Wah", "volumepan.models": "Volume/Pan", "sendreturn.models": "Send/Return",
}
EXCLUDED_SOURCES = {"io.models", "cabmicirswithpan.models"}  # routing ends; pan twins of the IR cabs

HOW_LABEL = {
    "parity": "parity", "smoke": "smoke", "search": "search", "probe": "probe", "live": "live",
    "inject": "written", "hw": "hardware", "drop": "drop-load", "template": "template",
}
RENDERED = {"parity", "smoke", "search", "probe", "live", "hw", "drop", "template"}
OKABE = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#555555"]
SHAPES = ["circle", "square", "triangle", "diamond", "cross", "circle", "square", "triangle"]
STRUCT_KEY = re.compile(r"^(join|split|input|output)", re.IGNORECASE)


# --------------------------------------------------------------------------- catalog

def gui_category(model: dict) -> str | None:
    """Helix/HX Edit GUI category of a catalog model, or None for routing ends that are no block."""
    src = model.get("source_file", "")
    mid = model.get("id", "")
    if src in EXCLUDED_SOURCES or "AppDSPFlow" in mid:
        return None
    if src == "fixed.models":
        if "ImpulseResponse" in mid:
            return "Impulse Response"
        return "Looper" if "Looper" in mid else "Send/Return"
    return SOURCE_CATEGORY.get(src, "Dynamics" if model.get("category") == "comp" else None)


def parse_fw(v: object) -> int | None:
    """stomp_min_firmware ('0x03690000' or None) -> int."""
    if v in (None, ""):
        return None
    try:
        return int(str(v), 16)
    except ValueError:
        return None


def stomp_usable(model: dict) -> tuple[bool, bool]:
    """(usable on the pedal at firmware 3.50, usable on a Stomp but needs newer firmware)."""
    if not model.get("stomp"):
        return False, False
    need = parse_fw(model.get("stomp_min_firmware"))
    if need is not None and need > PEDAL_FW:
        return False, True
    return True, False


def load_catalog(root: Path) -> dict:
    p = root / "cache" / "catalog" / "current.json"
    if not p.exists():
        return {"models": {}, "_source": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def model_name(cat: dict, mid: str) -> str:
    m = cat["models"].get(mid)
    if m:
        return m.get("name") or mid
    return re.sub(r"^(HD2_|HelixStomp_)", "", mid)


def model_category(cat: dict, mid: str) -> str:
    m = cat["models"].get(mid)
    if m:
        return gui_category(m) or "Other"
    return "Other"


# --------------------------------------------------------------------------- presets

def read_json(p: Path) -> object | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def hlx_blocks(doc: object) -> list[dict]:
    """The audio blocks of an .hlx in signal order (a cab sits right after the amp that names it)."""
    try:
        tone = doc["data"]["tone"]  # type: ignore[index]
    except (KeyError, TypeError):
        return []
    mains: list[dict] = []
    cabs: dict[str, dict] = {}
    for dsp in sorted(k for k in tone if re.fullmatch(r"dsp\d+", k) and isinstance(tone[k], dict)):
        for key, v in tone[dsp].items():
            if not isinstance(v, dict) or not isinstance(v.get("@model"), str):
                continue
            if STRUCT_KEY.match(key) or "AppDSPFlow" in v["@model"]:
                continue
            params = {k: x for k, x in v.items()
                      if not k.startswith("@") and isinstance(x, (int, float, bool))}
            if "@mic" in v and isinstance(v["@mic"], (int, float)):
                params["@mic"] = v["@mic"]
            b = {"dsp": dsp, "key": key, "model": v["@model"], "enabled": bool(v.get("@enabled", True)),
                 "pos": v.get("@position", 0) or 0, "path": v.get("@path", 0) or 0,
                 "cab": v.get("@cab"), "params": params}
            if re.fullmatch(r"cab\d+", key):
                cabs[key] = b
            else:
                mains.append(b)
    mains.sort(key=lambda b: (b["dsp"], b["path"], b["pos"]))
    out: list[dict] = []
    for b in mains:
        out.append(b)
        if b["cab"] in cabs:
            out.append(cabs.pop(b["cab"]))
    out.extend(cabs.values())
    return out


def signature(blocks: list[dict]) -> str:
    return json.dumps([[b["model"], b["enabled"],
                        sorted((k, round(float(v), 5)) for k, v in b["params"].items())]
                       for b in blocks], sort_keys=True)


@dataclass
class Variant:
    sig: str
    label: str
    context: str
    blocks: list[dict]
    engines: dict[str, set[str]] = field(default_factory=dict)
    srcs: list[str] = field(default_factory=list)
    obs: list[dict] = field(default_factory=list)

    @property
    def chain(self) -> tuple[str, ...]:
        return tuple(b["model"] for b in self.blocks)


class Evidence:
    """Everything that was tested, gathered from disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.variants: dict[str, Variant] = {}
        self.labels: dict[str, str] = {}
        self.bulk: dict[tuple[str, str], dict] = {}
        self.blockserial: list[dict] = []
        self.unresolved: list[str] = []
        self.sources: Counter = Counter()
        self.voided: set[float] = set()

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()

    def add_variant(self, label: str, blocks: list[dict], engines: dict[str, str] | list,
                    src: Path, context: str = "") -> Variant | None:
        if not blocks:
            return None
        sig = signature(blocks)
        v = self.variants.get(sig)
        if v is None:
            lab = label
            if lab in self.labels:
                lab = f"{label} [{context or len(self.labels)}]"
            v = Variant(sig, lab, context, blocks)
            self.variants[sig] = v
            self.labels[lab] = sig
        pairs = engines.items() if isinstance(engines, dict) else engines
        for eng, how in pairs:
            v.engines.setdefault(eng, set()).add(how)
        s = self.rel(src)
        if s not in v.srcs:
            v.srcs.append(s)
        return v

    def add_bulk(self, engine: str, model: str, how: str, params: dict | None, src: Path) -> None:
        r = self.bulk.setdefault((engine, model), {"hows": set(), "params": {}, "n": 0, "srcs": set()})
        r["hows"].add(how)
        r["n"] += 1
        r["srcs"].add(self.rel(src.parent) + "/")
        for k, x in (params or {}).items():
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                continue
            lo, hi = r["params"].get(k, (x, x))
            r["params"][k] = (min(lo, x), max(hi, x))


def short_run(name: str) -> str:
    """DS-Sample_run4_ER-free -> 'DS r4'; DS-Sample_userdi_take6 -> 'DS take6'; DS-Sample -> 'DS'."""
    m = re.fullmatch(r"DS-Sample(?:_(run\d+|userdi_take\d+))?(?:_.*)?", name)
    if not m:
        return name
    tag = (m[1] or "").replace("userdi_", "").replace("run", "r")
    return f"DS {tag}".strip()


def stem_label(p: Path) -> str:
    return p.stem


def collect_presets(root: Path, ev: Evidence) -> None:
    cache = root / "cache"

    def add_hlx(p: Path, label: str, engines: list, ctx: str = "") -> Variant | None:
        doc = read_json(p)
        ev.sources["hlx"] += 1
        return ev.add_variant(label, hlx_blocks(doc), engines, p, ctx)

    for p in sorted((cache / "stomp" / "inject").glob("*.hlx")):
        add_hlx(p, stem_label(p), [("stomp", "inject")])
    for p in sorted((cache / "stomp" / "hw" / "presets").glob("*.hlx")):
        add_hlx(p, stem_label(p), [("stomp", "hw")])
    for p in sorted((cache / "stomp" / "live").glob("*.hlx")):
        add_hlx(p, stem_label(p), [("stomp", "live")], "live")
    for p in sorted((cache / "native" / "spike").glob("*.hlx")):
        add_hlx(p, "spike " + stem_label(p), [("native", "drop")], "native spike")
    for p in sorted((cache / "native" / "drop").glob("*.hlx")):
        doc = read_json(p)
        ev.sources["hlx"] += 1
        for b in hlx_blocks(doc):
            ev.add_bulk("native", b["model"], "drop", b["params"], p)
    for p in sorted((cache / "blockserial").glob("*/*.hlx")):
        rest = p.stem.split("_", 1)[-1].removeprefix("BS_")
        add_hlx(p, p.parent.name if rest == p.parent.name else f"{p.parent.name} {rest}",
                [("native", "probe")], "block-serial " + p.parent.name)
    smoke = root / "fixtures" / "smoke" / "known.hlx"
    if smoke.exists():
        add_hlx(smoke, "known", [("stomp", "smoke"), ("native", "smoke")], "smoke fixture")
    matches = cache / "matches"
    if matches.exists():
        for run in sorted(d for d in matches.iterdir() if d.is_dir()):
            for p in sorted(run.glob("*.hlx")):
                add_hlx(p, f"{short_run(run.name)} {p.stem}", [("native", "search")], run.name)
    for tdir in ("templates", "templates_files", "templates_t018fix"):
        for p in sorted((cache / "native" / tdir).rglob("*.json")):
            d = read_json(p)
            if isinstance(d, dict) and isinstance(d.get("chain"), list):
                ev.sources["template"] += 1
                for ent in d["chain"]:
                    if isinstance(ent, list) and len(ent) == 2 and isinstance(ent[1], str):
                        ev.add_bulk("native", ent[1], "template", None, p)


def apply_search_finals(root: Path, ev: Evidence) -> None:
    """Search final values ('0:Drive' = block 0 of the chain) laid over the start chain's file."""
    base_files = {n: root / "cache" / "stomp" / "inject" / f"{n}.hlx" for n in
                  ("K", "A", "B0", "B", "C", "H")}
    base_files["A30"] = root / "cache" / "native" / "spike" / "model_a30.hlx"
    for res in sorted((root / "cache" / "matches").glob("*/result.json")):
        d = read_json(res)
        if not isinstance(d, dict):
            continue
        ev.sources["search result"] += 1
        for fin in d.get("finals", []):
            base = base_files.get(fin.get("chain", ""))
            if not base or not base.exists():
                continue
            blocks = hlx_blocks(read_json(base))
            for k, val in (fin.get("values") or {}).items():
                m = re.fullmatch(r"(\d+):(\w+)", k)
                if m and int(m[1]) < len(blocks):
                    blocks[int(m[1])]["params"][m[2]] = val
            ev.add_variant(f"{short_run(res.parent.name)} final {fin['chain']}", blocks,
                           [("native", "search")], res, res.parent.name)


def newest_hlx_after(p: Path, ref: Path) -> bool:
    try:
        return p.stat().st_mtime > ref.stat().st_mtime + 2
    except OSError:
        return False


def resolve_hlx(root: Path, summary: Path, name: str) -> tuple[Path | None, str]:
    cache = root / "cache"
    cands: list[Path] = []
    d = summary.parent
    for base in [d, *list(d.parents)[:4]]:
        cands += [base / f"{name}.hlx", base / "bisect" / f"{name}.hlx"]
        if base.name == "cache":
            break
    cands += [cache / "stomp" / "inject" / f"{name}.hlx", cache / "stomp" / "hw" / "presets" / f"{name}.hlx"]
    for c in cands:
        if c.exists():
            if newest_hlx_after(c, summary):
                return None, f"{name}: preset file was rewritten after the run"
            return c, ""
    return None, f"{name}: no preset file found"


def load_voided(root: Path) -> set[float]:
    """Null values of parity rows that ledger/void.csv voids."""
    void = root / "ledger" / "void.csv"
    runs = root / "ledger" / "runs.csv"
    if not (void.exists() and runs.exists()):
        return set()
    with void.open(encoding="utf-8", newline="") as fh:
        keys = {(r["timestamp"], r["metric"]) for r in csv.DictReader(fh)}
    out = set()
    with runs.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if (r["timestamp"], r["metric"]) in keys and r["kind"] == "parity":
                try:
                    out.add(round(float(r["value"]), 2))
                except ValueError:
                    pass
    return out


def ledger_parity_rows(idx: evidence_links.Index) -> list[tuple[str, str, float, str]]:
    rows = []
    for ts, items in idx.ledger.items():
        for _line, row in items:
            if len(row) > 10 and row[3] == "parity" and row[6] == "null_residual_db":
                try:
                    rows.append((ts, row[10], float(row[7]), row[2]))
                except ValueError:
                    pass
    return rows


def collect_parity(root: Path, ev: Evidence, idx: evidence_links.Index) -> None:
    cache = root / "cache"
    summaries = sorted(cache.glob("parity/*/summary.json")) + sorted(cache.glob("matches/**/summary.json"))
    ledger_rows = ledger_parity_rows(idx)
    for s in summaries:
        d = read_json(s)
        if not isinstance(d, dict) or not isinstance(d.get("results"), dict):
            continue
        ev.sources["parity run"] += 1
        run_rel = ev.rel(s.parent)
        ctx = s.parts[s.parts.index("matches") + 1] if "matches" in s.parts else s.parent.name
        for name, res in d["results"].items():
            if not isinstance(res, dict) or "null_residual_db" not in res:
                continue
            null = float(res["null_residual_db"])
            if round(null, 2) in ev.voided:
                ev.unresolved.append(f"{run_rel}/{name}: voided in ledger/void.csv, skipped")
                continue
            hlx, why = resolve_hlx(root, s, name)
            obs = {"run": run_rel, "name": name, "di": str(d.get("di", "")), "null": null,
                   "res": res, "dir": s.parent, "started": d.get("started") or "",
                   "mtime": s.stat().st_mtime, "ctx": ctx, "ledger_ts": ""}
            for ts, notes, val, _agent in ledger_rows:
                if f"preset={name};" in notes and abs(val - null) < 0.006:
                    obs["ledger_ts"] = ts.split("+")[0]
            if hlx is None:
                ev.unresolved.append(f"{run_rel}/{name}: {why}")
                continue
            label = f"{short_run(ctx)} {name}" if "matches" in s.parts else name
            v = ev.add_variant(label, hlx_blocks(read_json(hlx)),
                               [("stomp", "parity"), ("native", "parity")], hlx, ctx)
            if v:
                v.obs.append(obs)


def collect_blockserial(root: Path, ev: Evidence) -> None:
    for p in sorted((root / "cache" / "blockserial").glob("*/result.json")):
        d = read_json(p)
        if isinstance(d, dict) and "stages" in d:
            ev.sources["block-serial result"] += 1
            ev.blockserial.append({"config": p.parent.name, "res": d})


def collect(root: Path) -> tuple[Evidence, evidence_links.Index]:
    ev = Evidence(root)
    ev.voided = load_voided(root)
    idx = evidence_links.Index(root, "../../..")
    collect_presets(root, ev)
    apply_search_finals(root, ev)
    collect_parity(root, ev, idx)
    collect_blockserial(root, ev)
    return ev, idx


# --------------------------------------------------------------------------- policy and findings

def load_policy(root: Path) -> list[dict]:
    d = read_json(root / "src" / "tonematch" / "param_policy.json")
    return d.get("rules", []) if isinstance(d, dict) else []


def blame_for(obs: dict, v: Variant, policy: list[dict], cat: dict,
              passing: list[Variant] | None = None) -> tuple[list[int], str]:
    """Block indices to blame for a failed null, and why. Chain level unless something pins it."""
    for rule in policy:
        stomp = rule.get("engines", {}).get("stomp", {})
        if stomp.get("action") != "freeze":
            continue
        for i, b in enumerate(v.blocks):
            if model_category(cat, b["model"]).lower() != str(rule.get("category", "")).lower():
                continue
            val = b["params"].get(rule["param"])
            if val is not None and abs(val - stomp.get("value", 0.0)) > 1e-6:
                return [i], (f"{rule['param']} {fmt_val(val)} (Stomp policy freezes it at "
                             f"{stomp['value']})")
    best: tuple[list[int], str] | None = None
    for other in passing or []:
        if len(other.blocks) != len(v.blocks):
            continue
        changed = [i for i, (x, y) in enumerate(zip(v.blocks, other.blocks, strict=True))
                   if x["model"] != y["model"] or x["enabled"] != y["enabled"]
                   or any(abs(float(x["params"].get(k, 0)) - float(y["params"].get(k, 0))) > 1e-5
                          for k in set(x["params"]) | set(y["params"]))]
        if changed and len(changed) <= 2 and (best is None or len(changed) < len(best[0])):
            names = ", ".join(model_name(cat, v.blocks[i]["model"]) for i in changed)
            best = (changed, f"passes as {other.label}; differs only in {names}")
    if best:
        return best
    res = obs["res"]
    body, tail = res.get("body_null_residual_db"), res.get("tail_null_residual_db")
    if body is not None and tail is not None and body <= PARITY_DB < tail:
        delays = [i for i, b in enumerate(v.blocks)
                  if b["enabled"] and model_category(cat, b["model"]) in ("Delay", "Reverb")]
        if delays:
            return delays, f"echo tail differs (tail null {tail:+.1f} dB, body {body:.1f} dB)"
    return ([i for i, b in enumerate(v.blocks) if b["enabled"]],
            "whole-chain null; no single cause pinned")


# --------------------------------------------------------------------------- formatting

def fmt_val(x: object) -> str:
    if isinstance(x, bool):
        return "on" if x else "off"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        s = f"{x:.3f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(x)


def fmt_db(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.1f} dB" if signed else f"{x:.1f} dB"


def e(x: object) -> str:
    return html.escape(str(x), quote=True)


def md_cell(x: object) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def fmt_param(meta: dict | None, x: object) -> str:
    """A parameter value with its unit, from the catalog's display type (seconds shown as ms)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return fmt_val(x)
    disp = str((meta or {}).get("display", ""))
    low = disp.lower()
    if low in ("percent", "percent_braced", "percent_width"):
        return f"{x * 100:.0f}%"
    if low in ("cab_low_cut", "cab_high_cut", "frequency", "mod_low_cut", "mod_high_cut",
               "eq_low_cut", "eq_high_cut", "tubepre_low_cut", "tubepre_high_cut"):
        return f"{x:.0f} Hz"
    if low in ("volume", "volume_eq"):
        return f"{x:.1f} dB"
    if low == "cab_mic_distance":
        return f"{x:.1f} in"
    if "time_ms" in low or low in ("comp_attack", "comp_release"):
        return f"{x * 1000:.0f} ms"  # the catalog stores these in seconds
    if low.startswith("time_sec"):
        return f"{x:.2f} s"
    if low == "cents":
        return f"{x:.0f} cents"
    return fmt_val(x)


def norm_value(meta: dict | None, v: object) -> float | None:
    """Position of a value inside its parameter range, 0..1; None for switches and steps."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if meta:
        if meta.get("kind") != "continuous":
            return None
        lo, hi = meta.get("min"), meta.get("max")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi != lo:
            return min(1.0, max(0.0, (v - lo) / (hi - lo)))
        return None
    return v if 0.0 <= v <= 1.0 else None


# --------------------------------------------------------------------------- analysis

def third_octave_db(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    from scipy.signal import welch
    f, p = welch(x.astype(np.float64), SR, window="hann", nperseg=8192)
    df = f[1] - f[0]
    centres = 1000.0 * 2.0 ** (np.arange(-13, 13) / 3.0)
    lv = []
    for c in centres:
        m = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        lv.append(10 * np.log10(max(float(p[m].sum() * df), 1e-20)))
    return centres, np.array(lv)


def spectra_for(obs: dict) -> dict | None:
    """1/3-octave spectra of the aligned, level-matched Stomp (L) and Native (R) renders."""
    import soundfile as sf
    files = obs["res"].get("files", {})
    sp = obs["dir"] / files.get("stereo", "")
    if not sp.is_file():
        return None
    try:
        x, sr = sf.read(str(sp), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - unreadable audio just means no chart
        return None
    if sr != SR or x.shape[1] < 2:
        return None
    c, st = third_octave_db(x[:, 0])
    _, na = third_octave_db(x[:, 1])
    out = {"centres": c, "stomp": st, "native": na, "resid": None}
    rp = obs["dir"] / files.get("residual", "")
    if rp.is_file():
        try:
            r, rsr = sf.read(str(rp), dtype="float32", always_2d=True)
            if rsr == SR:
                out["resid"] = third_octave_db(r[:, 0])[1]
        except Exception:  # noqa: BLE001 - no residual chart then
            out["resid"] = None
    return out


def base_rank(label: str) -> int:
    last = label.split()[-1]
    return BASE_PRIORITY.index(last) if last in BASE_PRIORITY else 99


def newest_obs(v: Variant) -> dict[str, dict]:
    """Newest observation of a variant per DI."""
    best: dict[str, dict] = {}
    for o in v.obs:
        if o["di"] not in best or o["mtime"] > best[o["di"]]["mtime"]:
            best[o["di"]] = o
    return best


def diff_blocks(base: list[dict], other: list[dict], cat: dict) -> list[str]:
    out = []
    if len(base) != len(other):
        return ["different chain"]
    for ba, bb in zip(base, other):
        nm = model_name(cat, ba["model"])
        if ba["model"] != bb["model"]:
            out.append(f"{nm} -> {model_name(cat, bb['model'])}")
            continue
        if ba["enabled"] != bb["enabled"]:
            out.append(f"{nm} {'on' if bb['enabled'] else 'off'}")
        for k in sorted(set(ba["params"]) | set(bb["params"])):
            x, y = ba["params"].get(k), bb["params"].get(k)
            if x is None or y is None or abs(float(x) - float(y)) > 1e-5:
                out.append(f"{nm} {k.lstrip('@')} {fmt_val(x)} -> {fmt_val(y)}")
    return out


# --------------------------------------------------------------------------- model

BASE_PRIORITY = ["K", "B00", "C", "H", "best_snapshot1"]


def build_model(root: Path, ev: Evidence, cat: dict, policy: list[dict]) -> dict:
    models = cat["models"]
    # --- availability per category
    cats: dict[str, dict] = {c: {"stomp": 0, "stomp_newfw": 0, "native": 0, "ids": []} for c in GUI_CATEGORIES}
    cats["Other"] = {"stomp": 0, "stomp_newfw": 0, "native": 0, "ids": []}
    for mid, m in models.items():
        c = gui_category(m)
        if c is None:
            continue
        ok, newfw = stomp_usable(m)
        cats[c]["native"] += 1
        cats[c]["stomp"] += ok
        cats[c]["stomp_newfw"] += newfw
        cats[c]["ids"].append(mid)
    # --- per model, per engine evidence
    per: dict[str, dict] = {}

    def rec(mid: str) -> dict:
        return per.setdefault(mid, {"engines": {"stomp": {"hows": set(), "vars": [], "bulk": None},
                                                "native": {"hows": set(), "vars": [], "bulk": None}},
                                    "parity": [], "status_notes": [], "ok": 0, "fail": 0, "off": 0})

    for v in ev.variants.values():
        for b in v.blocks:
            r = rec(b["model"])
            for eng, hows in v.engines.items():
                r["engines"][eng]["hows"] |= hows
                r["engines"][eng]["vars"].append((v, b))
    for (eng, mid), bk in ev.bulk.items():
        r = rec(mid)
        r["engines"][eng]["hows"] |= bk["hows"]
        r["engines"][eng]["bulk"] = bk
    # --- parity verdicts
    for v in ev.variants.values():
        for o in v.obs:
            failed = o["null"] > PARITY_DB
            blamed: list[int] = []
            why = ""
            if failed:
                passing = [w for w in ev.variants.values() if w.chain == v.chain and w is not v
                           and any(x["null"] <= PARITY_DB and Path(x["di"]).name == Path(o["di"]).name
                                   for x in w.obs)]
                blamed, why = blame_for(o, v, policy, cat, passing)
            for i, b in enumerate(v.blocks):
                r = rec(b["model"])
                entry = {"label": v.label, "null": o["null"], "ts": o["ledger_ts"], "run": o["run"],
                         "enabled": b["enabled"], "di": Path(o["di"]).name,
                         "failed": failed, "blamed": failed and i in blamed}
                r["parity"].append(entry)
                if not b["enabled"]:
                    r["off"] += 1
                elif failed and i in blamed:
                    r["fail"] += 1
                    r["status_notes"].append(f"{v.label}: {why}")
                else:
                    r["ok"] += 1
    out_models = {}
    for mid, r in per.items():
        c = model_category(cat, mid)
        s_hows = r["engines"]["stomp"]["hows"]
        n_hows = r["engines"]["native"]["hows"]
        if r["fail"] and r["ok"]:
            status = "partial"
        elif r["fail"]:
            status = "mismatch"
        elif r["ok"]:
            status = "in parity"
        elif r["off"]:
            status = "block off in parity runs"
        else:
            status = "untested for parity"
        out_models[mid] = {
            "id": mid, "name": model_name(cat, mid), "cat": c, "status": status, "notes": r["status_notes"],
            "stomp_rendered": bool(s_hows & RENDERED), "native_rendered": bool(n_hows & RENDERED),
            "stomp_hows": sorted(s_hows), "native_hows": sorted(n_hows),
            "parity_obs": r["parity"], "raw": r,
            "stomp_ok": stomp_usable(models.get(mid, {}))[0] if mid in models else False,
            "load": (models.get(mid) or {}).get("load"),
            "known": mid in models,
        }
    # --- dashboard counts
    dash = []
    for c in [*GUI_CATEGORIES, "Other"]:
        ms = [m for m in out_models.values() if m["cat"] == c]
        if c == "Other" and not ms:
            continue
        dash.append({
            "cat": c, "stomp_avail": cats[c]["stomp"], "stomp_newfw": cats[c]["stomp_newfw"],
            "native_avail": cats[c]["native"],
            "stomp_tested": sum(m["stomp_rendered"] for m in ms),
            "native_tested": sum(m["native_rendered"] for m in ms),
            "stomp_written": sum(1 for m in ms if m["stomp_hows"] and not m["stomp_rendered"]),
            "parity": sum(1 for m in ms if m["status"] in ("in parity", "partial", "mismatch")),
            "in_parity": sum(1 for m in ms if m["status"] == "in parity"),
            "mismatch": sum(1 for m in ms if m["status"] in ("partial", "mismatch")),
            "untested": cats[c]["native"] - sum(1 for m in ms if m["known"] and (m["stomp_rendered"] or m["native_rendered"])),
        })
    return {"cats": cats, "models": out_models, "dash": dash}


def group_cards(ev: Evidence, cat: dict) -> list[dict]:
    chains: dict[tuple, list[Variant]] = defaultdict(list)
    probes: list[Variant] = []
    for v in ev.variants.values():
        if set().union(*v.engines.values()) <= {"probe"}:
            probes.append(v)
        else:
            chains[v.chain].append(v)
    cards = []
    for chain, vs in chains.items():
        if len(chain) < 2:
            continue
        vs.sort(key=lambda v: (0 if v.obs else 1, base_rank(v.label), v.label))
        groups: dict[str, list[tuple[Variant, dict]]] = defaultdict(list)
        for v in vs:
            for di, o in newest_obs(v).items():
                groups[di].append((v, o))
        cards.append({"kind": "chain", "chain": chain, "variants": vs, "groups": groups,
                      "n_obs": sum(len(v.obs) for v in vs)})
    cards.sort(key=lambda c: (-c["n_obs"], -len(c["variants"])))
    if probes:
        probes.sort(key=lambda v: (-len(v.chain), v.label))
        cards.append({"kind": "probe", "chain": probes[0].chain, "variants": probes, "groups": {},
                      "n_obs": 0})
    return cards


# --------------------------------------------------------------------------- svg

CSS = """
:root{--bg:#fff;--fg:#1d2330;--mute:#5b6577;--line:#d4d9e2;--card:#f6f8fb;--stomp:#C14A00;--native:#0072B2;
--ok:#0b7a53;--bad:#b3261e;--part:#8a5a00;--grid:#e3e7ee;--hl:#fff3d6}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e8ebf2;--mute:#9aa4b6;--line:#343b49;--card:#1b2029;
--stomp:#FFA24C;--native:#6CB8F0;--ok:#4cd19b;--bad:#ff8a80;--part:#f0c060;--grid:#2a303c;--hl:#3a3320}}
*{box-sizing:border-box}body{margin:0 auto;max-width:1180px;padding:14px;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.25rem;margin:1.6em 0 .4em;border-bottom:1px solid var(--line);padding-bottom:.2em}
h3{font-size:1.05rem;margin:1.2em 0 .3em;overflow-wrap:anywhere}a{color:var(--native)}small,.mute{color:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:.88rem;margin:.4em 0}th,td{border:1px solid var(--line);padding:4px 6px;
vertical-align:top;text-align:left}th{background:var(--card)}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}.badge{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 6px;margin:1px 2px 1px 0;
font-size:.78rem;background:var(--card);white-space:nowrap}.st-in{color:var(--ok);font-weight:600}.st-mis{color:var(--bad);font-weight:600}
.st-par{color:var(--part);font-weight:600}.sw{display:inline-block;width:.8em;height:.8em;border-radius:2px;margin-right:.4em;vertical-align:-1px}
details{margin:.3em 0}summary{cursor:pointer}.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:14px 0;background:var(--card)}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:10px}.figs.wide{grid-template-columns:repeat(auto-fit,minmax(400px,1fr))}.fig{background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:6px}.fig svg{width:100%;height:auto;display:block}.note{background:var(--hl);border-radius:6px;padding:6px 10px;margin:.5em 0}
.legend span{margin-right:12px;white-space:nowrap}code{font-size:.85em}
svg text{fill:var(--fg);font:11px system-ui,sans-serif}svg .m{fill:var(--mute)}svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--mute);stroke-width:1}svg .stomp{stroke:var(--stomp);fill:none;stroke-width:2}
svg .native{stroke:var(--native);fill:none;stroke-width:2;stroke-dasharray:6 3}svg .resid{stroke:var(--mute);fill:none;stroke-width:1.4;stroke-dasharray:1 3}
svg .thr{stroke:var(--bad);stroke-width:1;stroke-dasharray:4 3}svg .bar{fill:var(--native);fill-opacity:.55;stroke:var(--native)}
svg .barbad{fill:url(#bc-hatch);stroke:var(--bad)}svg .track{stroke:var(--line);stroke-width:5;stroke-linecap:round}
svg .blk{stroke-width:2}svg .blkoff{stroke-dasharray:5 3}svg .ttl{font-weight:600}
@media (max-width:640px){body{padding:8px}.figs,.figs.wide{grid-template-columns:1fr}}
"""

HATCH = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
         '<pattern id="bc-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         '<rect width="6" height="6" fill="none"/><line x1="0" y1="0" x2="0" y2="6" stroke="#b3261e" stroke-width="3"/>'
         '</pattern></defs></svg>')


def marker(shape: str, x: float, y: float, col: str, r: float = 4.5, title: str = "") -> str:
    t = f"<title>{e(title)}</title>" if title else ""
    if shape == "square":
        g = f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" fill="{col}">{t}</rect>'
    elif shape == "triangle":
        g = (f'<polygon points="{x:.1f},{y - r - 1:.1f} {x - r - 1:.1f},{y + r:.1f} {x + r + 1:.1f},{y + r:.1f}" '
             f'fill="{col}">{t}</polygon>')
    elif shape == "diamond":
        g = (f'<polygon points="{x:.1f},{y - r - 1:.1f} {x + r + 1:.1f},{y:.1f} {x:.1f},{y + r + 1:.1f} '
             f'{x - r - 1:.1f},{y:.1f}" fill="{col}">{t}</polygon>')
    elif shape == "cross":
        g = (f'<path d="M{x - r:.1f} {y - r:.1f}L{x + r:.1f} {y + r:.1f}M{x - r:.1f} {y + r:.1f}L{x + r:.1f} {y - r:.1f}" '
             f'stroke="{col}" stroke-width="2.4" fill="none">{t}</path>')
    else:
        g = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{col}">{t}</circle>'
    return g


def svg_wrap(w: int, h: int, body: str, label: str, max_w: int | None = None) -> str:
    style = f' style="max-width:{max_w or int(w * 1.25)}px"'
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(label)}"{style} '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def fig_chain(blocks: list[dict], cat: dict, on_count: list[int], n_var: int) -> str:
    bw, gap, h = 132, 14, 66
    w = len(blocks) * (bw + gap) + 6
    body = []
    for i, b in enumerate(blocks):
        c = model_category(cat, b["model"])
        col = CAT_COLOURS.get(c, "#888888")
        x = 3 + i * (bw + gap)
        cls = "blk" + ("" if on_count[i] else " blkoff")
        body.append(f'<g><title>{e(model_name(cat, b["model"]))} ({e(c)}); on in {on_count[i]} of {n_var} variants</title>'
                    f'<rect class="{cls}" x="{x}" y="4" width="{bw}" height="{h - 8}" rx="8" fill="{col}" '
                    f'fill-opacity=".2" stroke="{col}"/>'
                    f'<text x="{x + bw / 2}" y="24" text-anchor="middle" class="ttl">{e(model_name(cat, b["model"])[:20])}</text>'
                    f'<text x="{x + bw / 2}" y="40" text-anchor="middle" class="m">{e(c)}</text>'
                    f'<text x="{x + bw / 2}" y="55" text-anchor="middle">{"ON" if on_count[i] == n_var else "OFF" if not on_count[i] else f"on in {on_count[i]}/{n_var}"}</text></g>')
        if i:
            body.append(f'<path class="axis" d="M{x - gap + 1} {h / 2}L{x - 1} {h / 2}"/>')
    return svg_wrap(w, h, "".join(body), "block chain", w)


def fig_params(block_idx: int, blk_model: str, variants: list[Variant], cat: dict) -> str:
    meta_all = (cat["models"].get(blk_model) or {}).get("params", {})
    keys: list[str] = []
    for k in meta_all:
        if any(k in v.blocks[block_idx]["params"] for v in variants):
            keys.append(k)
    for v in variants:
        for k in v.blocks[block_idx]["params"]:
            if k not in keys:
                keys.append(k)
    rh, lx, tx, tw = 20, 112, 120, 170
    h = 26 + rh * len(keys)
    w = tx + tw + 150
    body = [f'<text x="2" y="14" class="ttl">{e(model_name(cat, blk_model))}</text>']
    for t in (0, 0.5, 1):
        x = tx + tw * t
        body.append(f'<path class="grid" d="M{x} 20L{x} {h - 2}"/><text x="{x}" y="{h - 1}" text-anchor="middle" class="m">{fmt_val(t)}</text>')
    for r, k in enumerate(keys):
        y = 30 + r * rh
        meta = meta_all.get(k)
        nm = (meta or {}).get("name") or k.lstrip("@")
        vals = [v.blocks[block_idx]["params"].get(k) for v in variants]
        distinct = []
        for x in vals:
            if x is not None and not any(abs(float(x) - float(d)) < 1e-5 for d in distinct):
                distinct.append(x)
        varied = len(distinct) > 1
        body.append(f'<text x="{lx}" y="{y + 4}" text-anchor="end"{"" if varied else " class=m"}>{e(nm[:16])}</text>')
        norms = [norm_value(meta, x) for x in distinct]
        if all(n is not None for n in norms) and distinct:
            body.append(f'<path class="track" d="M{tx} {y}L{tx + tw} {y}"/>')
            for vi, v in enumerate(variants):
                x = v.blocks[block_idx]["params"].get(k)
                n = norm_value(meta, x) if x is not None else None
                if n is None:
                    continue
                px = tx + tw * n
                col = OKABE[vi % 8] if varied else "#888888"
                body.append(marker(SHAPES[vi % 8], px, y + ((vi % 3) - 1) * 2.5 if varied else y, col, 4,
                                   f"{v.label}: {nm} = {fmt_param(meta, x)}"))
        txt = " | ".join(fmt_param(meta, d) for d in distinct[:3]) + (f" +{len(distinct) - 3}" if len(distinct) > 3 else "")
        body.append(f'<text x="{tx + tw + 10}" y="{y + 4}"{"" if varied else " class=m"}>{e(txt)}</text>')
    return svg_wrap(w, h, "".join(body), f"parameters of {model_name(cat, blk_model)}", w)


def fx(f: float, x0: float, w: float) -> float:
    return x0 + w * (math.log10(f) - math.log10(45.0)) / (math.log10(20000.0) - math.log10(45.0))


def axis_freq(x0: float, w: float, y0: float, y1: float) -> str:
    out = []
    for f, lab in ((100, "100"), (1000, "1k"), (10000, "10k")):
        x = fx(f, x0, w)
        out.append(f'<path class="grid" d="M{x:.1f} {y0}L{x:.1f} {y1}"/>'
                   f'<text x="{x:.1f}" y="{y1 + 12}" text-anchor="middle" class="m">{lab} Hz</text>')
    return "".join(out)


def poly(cs: np.ndarray, ys: np.ndarray, x0: float, w: float, y0: float, h: float, lo: float, hi: float) -> str:
    pts = []
    for c, y in zip(cs, ys):
        yy = y0 + h * (1 - (min(max(float(y), lo), hi) - lo) / (hi - lo))
        pts.append(f"{fx(float(c), x0, w):.1f},{yy:.1f}")
    return " ".join(pts)


def fig_spectrum(label: str, sp: dict, res: dict) -> str:
    x0, w, y0, h = 44, 330, 40, 150
    allv = np.concatenate([sp["stomp"], sp["native"]])
    top = math.ceil((float(np.max(allv)) + 2) / 10) * 10
    lo, hi = top - 70, top
    body = [f'<text x="2" y="12" class="ttl">{e(label)}: long-term spectrum</text>']
    for g in range(int(lo), int(hi) + 1, 10):
        yy = y0 + h * (1 - (g - lo) / (hi - lo))
        body.append(f'<path class="grid" d="M{x0} {yy:.1f}L{x0 + w} {yy:.1f}"/><text x="{x0 - 4}" y="{yy + 3:.1f}" text-anchor="end" class="m">{g}</text>')
    body.append(f'<text x="4" y="{y0 + h / 2:.0f}" class="m" transform="rotate(-90 8 {y0 + h / 2:.0f})" text-anchor="middle">dB re full scale, per 1/3 octave</text>')
    body.append(axis_freq(x0, w, y0, y0 + h))
    if sp["resid"] is not None:
        body.append(f'<polyline class="resid" points="{poly(sp["centres"], sp["resid"], x0, w, y0, h, lo, hi)}"><title>Stomp minus Native</title></polyline>')
    body.append(f'<polyline class="stomp" points="{poly(sp["centres"], sp["stomp"], x0, w, y0, h, lo, hi)}"><title>Stomp</title></polyline>')
    body.append(f'<polyline class="native" points="{poly(sp["centres"], sp["native"], x0, w, y0, h, lo, hi)}"><title>Native</title></polyline>')
    # octave residual panel
    by = y0 + h + 44
    bh = 92
    bands = [b for b in res.get("bands", []) if b.get("native_db", -999) > -100]
    blo, bhi = -45.0, 5.0
    body.append(f'<text x="2" y="{by - 12}" class="ttl">Per-octave null, Stomp minus Native (dB re Native)</text>')
    for g in (-40, -30, -20, -10, 0):
        yy = by + bh * (1 - (g - blo) / (bhi - blo))
        body.append(f'<path class="grid" d="M{x0} {yy:.1f}L{x0 + w} {yy:.1f}"/><text x="{x0 - 4}" y="{yy + 3:.1f}" text-anchor="end" class="m">{g}</text>')
    ty = by + bh * (1 - (PARITY_DB - blo) / (bhi - blo))
    body.append(f'<path class="thr" d="M{x0} {ty:.1f}L{x0 + w} {ty:.1f}"/>')
    n = max(len(bands), 1)
    bw = w / n
    for i, b in enumerate(bands):
        v = float(b.get("residual_db_re_native_band", 0))
        yy = by + bh * (1 - (min(max(v, blo), bhi) - blo) / (bhi - blo))
        bad = v > PARITY_DB
        lvl = b.get("stomp_minus_native_db")
        c = b["centre_hz"]
        lab = f"{c / 1000:g}k" if c >= 1000 else f"{c:g}"
        cls = "barbad" if bad else "bar"
        body.append(f'<rect class="{cls}" x="{x0 + i * bw + 3:.1f}" y="{yy:.1f}" width="{bw - 6:.1f}" '
                    f'height="{by + bh - yy:.1f}"><title>{c:g} Hz octave: null {v:.1f} dB; '
                    f'Stomp level {lvl:+.2f} dB vs Native</title></rect>'
                    f'<text x="{x0 + i * bw + bw / 2:.1f}" y="{by + bh + 11}" text-anchor="middle" class="m">{lab}</text>')
        body.append(f'<text x="{x0 + i * bw + bw / 2:.1f}" y="{yy - 2:.1f}" text-anchor="middle">{v:.0f}</text>')
    body.append(f'<text x="{x0 + w}" y="{by + bh + 24}" text-anchor="end" class="m">octave centre, Hz; bar labels in dB</text>')
    # legend
    body.append(f'<path class="stomp" d="M{x0 + 10} 28L{x0 + 40} 28"/><text x="{x0 + 44}" y="31">Stomp</text>'
                f'<path class="native" d="M{x0 + 96} 28L{x0 + 126} 28"/><text x="{x0 + 130}" y="31">Native</text>'
                f'<path class="resid" d="M{x0 + 184} 28L{x0 + 214} 28"/><text x="{x0 + 218}" y="31">Stomp minus Native</text>')
    return svg_wrap(x0 + w + 10, by + bh + 32, "".join(body), f"{label} spectrum and octave residual")


def fig_diff(title: str, base_label: str, curves: list[tuple[str, np.ndarray]], centres: np.ndarray, engine: str) -> str:
    x0, w, y0, h = 44, 330, 22, 130
    mx = max([float(np.max(np.abs(c))) for _, c in curves] + [3.0])
    lim = math.ceil(mx / 3) * 3
    body = [f'<text x="2" y="12" class="ttl">{e(title)}</text>']
    for g in range(-lim, lim + 1, 3 if lim <= 12 else 6):
        yy = y0 + h * (1 - (g + lim) / (2 * lim))
        body.append(f'<path class="{"axis" if g == 0 else "grid"}" d="M{x0} {yy:.1f}L{x0 + w} {yy:.1f}"/>'
                    f'<text x="{x0 - 4}" y="{yy + 3:.1f}" text-anchor="end" class="m">{g:+d}</text>')
    body.append(axis_freq(x0, w, y0, y0 + h))
    body.append(f'<text x="4" y="{y0 + h / 2:.0f}" class="m" transform="rotate(-90 8 {y0 + h / 2:.0f})" text-anchor="middle">dB vs {e(base_label)}</text>')
    for i, (lab, d) in enumerate(curves):
        col = OKABE[i % 8]
        pts = poly(centres, d, x0, w, y0, h, -lim, lim)
        dash = ["", "6 3", "2 3", "8 3 2 3"][(i // 8 + i % 4) % 4]
        body.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" '
                    f'{f"stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}><title>{e(lab)}</title></polyline>')
    return svg_wrap(x0 + w + 10, y0 + h + 24, "".join(body), title)


def fig_stages(res: dict, cat: dict) -> str:
    stages = res["stages"]
    rh, lx = 22, 150
    w, h = 470, 30 + rh * len(stages)
    lo, hi = -60.0, 0.0
    body = ['<text x="2" y="12" class="ttl">Level after each block (dB re full scale, RMS)</text>']
    for g in (-60, -40, -20, 0):
        x = lx + 280 * (g - lo) / (hi - lo)
        body.append(f'<path class="grid" d="M{x:.1f} 20L{x:.1f} {h - 12}"/><text x="{x:.1f}" y="{h - 1}" text-anchor="middle" class="m">{g}</text>')
    for i, s in enumerate(stages):
        y = 26 + i * rh
        v = float(s["out_rms_dbfs"])
        x = lx + 280 * (min(max(v, lo), hi) - lo) / (hi - lo)
        nm = model_name(cat, s["block"])
        body.append(f'<text x="{lx - 6}" y="{y + 12}" text-anchor="end">{e(nm[:22])}</text>'
                    f'<rect class="bar" x="{lx}" y="{y}" width="{x - lx:.1f}" height="14"><title>{e(nm)}: {v:.1f} dB RMS, latency {s.get("latency_samples", "?")} samples</title></rect>'
                    f'<text x="{x + 4:.1f}" y="{y + 12}">{v:.1f}</text>')
    return svg_wrap(w, h, "".join(body), "level after each block", w)


# --------------------------------------------------------------------------- html

def badge(text: str) -> str:
    return f'<span class="badge">{e(text)}</span>'


def how_badges(hows: list[str] | set[str]) -> str:
    return "".join(badge(HOW_LABEL.get(h, h)) for h in sorted(hows)) or '<span class="mute">no</span>'


def status_html(st: str) -> str:
    cls = {"in parity": "st-in", "mismatch": "st-mis", "partial": "st-par"}.get(st, "mute")
    return f'<span class="{cls}">{e(st)}</span>'


def settings_summary(r: dict, eng: str, cat: dict, mid: str) -> tuple[str, str]:
    """(html summary of what varied, html table of every setting) for a model on one engine.

    The summary lists, per varied parameter, the distinct values tried (hover: which preset)."""
    items = r["engines"][eng]["vars"]
    meta = (cat["models"].get(mid) or {}).get("params", {})
    by_params: dict[str, list[str]] = {}
    plist: dict[str, dict] = {}
    for v, b in items:
        key = json.dumps(sorted(b["params"].items())) + str(b["enabled"])
        by_params.setdefault(key, []).append(v.label)
        plist[key] = b
    keys = list(by_params)
    allp = sorted({p for b in plist.values() for p in b["params"]},
                  key=lambda p: (list(meta).index(p) if p in meta else 99, p))
    lines = []
    for p in allp:
        vals: list[tuple[float, list[str]]] = []
        for k in keys:
            x = plist[k]["params"].get(p)
            if x is None:
                continue
            for ent in vals:
                if abs(float(x) - ent[0]) < 1e-5:
                    ent[1].extend(by_params[k])
                    break
            else:
                vals.append((float(x), list(by_params[k])))
        if len(vals) > 1:
            vals.sort(key=lambda t: t[0])
            nm = (meta.get(p) or {}).get("name") or p.lstrip("@")
            shown = "".join(
                f'<span class="badge" title="{e(", ".join(sorted(set(labs))[:12]))}">{e(fmt_param(meta.get(p), x))}</span>'
                for x, labs in vals[:8])
            lines.append(f"<b>{e(nm)}</b> {shown}" + (f" +{len(vals) - 8} more" if len(vals) > 8 else ""))
    parts = []
    if lines:
        parts.append("<br>".join(lines[:6]) + (f"<br>+{len(lines) - 6} more varied parameters" if len(lines) > 6 else ""))
    elif keys:
        parts.append(f"{len(keys)} setting{'s' if len(keys) != 1 else ''}, no parameter varied")
    bulk = r["engines"][eng]["bulk"]
    if bulk and bulk["params"]:
        ranged = [(p, lo, hi) for p, (lo, hi) in bulk["params"].items()
                  if fmt_param(meta.get(p), lo) != fmt_param(meta.get(p), hi)]
        if ranged:
            parts.append(f"{bulk['n']} dropped files, ranges: "
                         + e(", ".join(f"{(meta.get(p) or {}).get('name') or p} {fmt_param(meta.get(p), lo)} to "
                                       f"{fmt_param(meta.get(p), hi)}" for p, lo, hi in ranged[:5])))
    elif bulk:
        parts.append(f"{bulk['n']} template(s)")
    rows = []
    for k in keys:
        b = plist[k]
        vals_txt = ", ".join(f"{(meta.get(p) or {}).get('name') or p.lstrip('@')} {fmt_param(meta.get(p), x)}"
                             for p, x in sorted(b["params"].items()))
        rows.append(f"<tr><td>{e(', '.join(by_params[k][:6]))}{' ...' if len(by_params[k]) > 6 else ''}</td>"
                    f"<td>{'on' if b['enabled'] else 'off'}</td><td>{e(vals_txt)}</td></tr>")
    table = ""
    if rows:
        table = ("<details><summary>all settings</summary><div class='scroll'><table><tr><th>preset</th>"
                 "<th>block</th><th>values</th></tr>" + "".join(rows[:60])
                 + (f"<tr><td colspan=3>... {len(rows) - 60} more</td></tr>" if len(rows) > 60 else "")
                 + "</table></div></details>")
    return "<br>".join(parts), table


def eng_cell(m: dict, eng: str, cat: dict) -> str:
    r = m["raw"]
    hows = m[f"{eng}_hows"]
    if not hows:
        return '<span class="mute">no</span>'
    rendered = m[f"{eng}_rendered"]
    summ, table = settings_summary(r, eng, cat, m["id"])
    labels = sorted({v.label for v, _ in r["engines"][eng]["vars"]})
    head = how_badges(hows) + ("" if rendered else '<br><small>written only, not rendered</small>')
    pre = ""
    if labels:
        pre = "<br><small>in: " + e(", ".join(labels[:6])) + (f" +{len(labels) - 6} more" if len(labels) > 6 else "") + "</small>"
    return head + (f"<br><small>{summ}</small>" if summ else "") + pre + table


def parity_cell(m: dict) -> str:
    obs = [o for o in m["parity_obs"] if o["enabled"]]
    if not obs:
        return '<span class="mute">none</span>'
    seen, parts = set(), []
    for o in sorted(obs, key=lambda o: o["run"]):
        k = (o["label"], round(o["null"], 2))
        if k in seen:
            continue
        seen.add(k)
        cls = "st-mis" if o["blamed"] else ("st-par" if o["failed"] else "")
        ts = f" <small>{e(o['ts'])}</small>" if o["ts"] else ""
        other = " <small>(chain fails, cause elsewhere)</small>" if o["failed"] and not o["blamed"] else ""
        parts.append(f'<div class="{cls}">{e(o["label"])} {o["null"]:.1f} dB{other}{ts}</div>')
    more = (f"<details><summary>+{len(parts) - 6} more</summary>{''.join(parts[6:])}</details>"
            if len(parts) > 6 else "")
    return "".join(parts[:6]) + more


def category_section(c: str, model: dict, cat: dict) -> str:
    d = next(x for x in model["dash"] if x["cat"] == c)
    tested = sorted((m for m in model["models"].values() if m["cat"] == c and (m["stomp_hows"] or m["native_hows"])),
                    key=lambda m: m["name"])
    colour = CAT_COLOURS.get(c, "#888")
    out = [(f'<section id="cat-{slug(c)}"><h3><span class="sw" style="background:{colour}"></span>{e(c)} '
           f'<small>Stomp 3.50: {d["stomp_avail"]} | Native 3.82: {d["native_avail"]} | tested Stomp {d["stomp_tested"]}, Native {d["native_tested"]}</small></h3>')]
    if tested:
        out.append('<div class="scroll"><table><tr><th>Model</th><th>Stomp (fw 3.50)</th><th>Native (3.82)</th>'
                   '<th>Parity null, Stomp vs Native</th><th>Status</th></tr>')
        for m in tested:
            note = f'<br><small>{e("; ".join(sorted(set(m["notes"]))[:3]))}</small>' if m["notes"] else ""
            load = f"<br><small>DSP load {m['load']:.1f}</small>" if isinstance(m["load"], (int, float)) else ""
            out.append(f"<tr><td><b>{e(m['name'])}</b>{load}</td><td>{eng_cell(m, 'stomp', cat)}</td>"
                       f"<td>{eng_cell(m, 'native', cat)}</td><td>{parity_cell(m)}</td><td>{status_html(m['status'])}{note}</td></tr>")
        out.append("</table></div>")
    else:
        out.append('<p class="mute">No model of this category has been rendered on either engine yet.</p>')
    ids = [i for i in model["cats"][c]["ids"] if i not in model["models"]
           or not (model["models"][i]["stomp_hows"] or model["models"][i]["native_hows"])]
    names = []
    for i in sorted(ids, key=lambda i: model_name(cat, i).lower()):
        ok = stomp_usable(cat["models"][i])[0]
        names.append(e(model_name(cat, i)) + ("" if ok else "<sup>*</sup>"))
    if names:
        out.append(f"<details><summary>{len(names)} untested models in {e(c)} (* not usable on Stomp fw 3.50)</summary>"
                   f"<p><small>{', '.join(names)}</small></p></details>")
    out.append("</section>")
    return "".join(out)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def dash_html(model: dict) -> str:
    rows = []
    tot = Counter()
    for d in model["dash"]:
        colour = CAT_COLOURS.get(d["cat"], "#888")
        for k in ("stomp_avail", "stomp_newfw", "native_avail", "stomp_tested", "native_tested", "parity", "in_parity", "mismatch", "untested"):
            tot[k] += d[k]
        rows.append(f'<tr><td><span class="sw" style="background:{colour}"></span><a href="#cat-{slug(d["cat"])}">{e(d["cat"])}</a></td>'
                    f'<td class="n">{d["stomp_avail"]}</td><td class="n">{d["stomp_newfw"]}</td><td class="n">{d["native_avail"]}</td>'
                    f'<td class="n">{d["stomp_tested"]}</td><td class="n">{d["native_tested"]}</td>'
                    f'<td class="n">{d["parity"]}</td><td class="n">{d["in_parity"]}</td><td class="n">{d["mismatch"]}</td>'
                    f'<td class="n">{d["untested"]}</td></tr>')
    rows.append("<tr><th>All</th>" + "".join(f'<th class="n">{tot[k]}</th>' for k in
                ("stomp_avail", "stomp_newfw", "native_avail", "stomp_tested", "native_tested", "parity", "in_parity", "mismatch", "untested")) + "</tr>")
    return ('<div class="scroll"><table><tr><th rowspan=2>Category</th><th colspan=3>Models available</th><th colspan=2>Rendered</th>'
            '<th colspan=3>Parity (both engines)</th><th rowspan=2>Untested in Native</th></tr>'
            '<tr><th>Stomp fw 3.50</th><th>Stomp, needs newer fw</th><th>Native 3.82</th><th>on Stomp</th><th>in Native</th>'
            '<th>checked</th><th>in parity</th><th>known mismatch</th></tr>' + "".join(rows) + "</table></div>")


def probe_card_html(card: dict, ev: Evidence, cat: dict) -> str:
    vs: list[Variant] = card["variants"]
    big = vs[0]
    rows = "".join(f"<tr><td>{e(v.label)}</td><td>{len(v.chain)}</td><td>"
                   f"{e(' > '.join(model_name(cat, m) for m in v.chain))}</td></tr>" for v in vs[:40])
    on = [1 if b["enabled"] else 0 for b in big.blocks]
    out = ['<div class="card"><h3>Native block-serial probe: one instance per block vs one instance for the chain</h3>',
           ("<p>Native only. The same chain is rendered once in a single Native instance and once as one block per "
           "instance, chained; the null says how close the two are. This is the evidence behind the <i>probe</i> "
           "badge in section 2.</p>"),
           f'<div class="figs"><div class="fig">{fig_chain(big.blocks, cat, on, 1)}</div></div>',
           ("<details><summary>All probe presets (chains and single blocks)</summary><div class='scroll'><table>"
           f"<tr><th>preset</th><th>blocks</th><th>chain</th></tr>{rows}</table></div></details>"),
           "<div class='figs wide'>"]
    for bi, b in enumerate(big.blocks):
        out.append(f'<div class="fig">{fig_params(bi, b["model"], [big], cat)}</div>')
    out.append("</div>")
    out.append(blockserial_html(ev, cat, inner=True))
    out.append("</div>")
    return "".join(out)


def card_html(card: dict, ev: Evidence, cat: dict, idx_cap: int = 8) -> str:
    vs: list[Variant] = card["variants"]
    if card.get("kind") == "probe":
        return probe_card_html(card, ev, cat)
    chain = vs[0].blocks
    on = [sum(1 for v in vs if v.blocks[i]["enabled"]) for i in range(len(chain))]
    title = " &rarr; ".join(e(model_name(cat, m)) for m in card["chain"])
    out = [f'<div class="card" id="card-{slug("-".join(card["chain"]))[:60]}"><h3>{title}</h3>',
           f'<div class="figs"><div class="fig">{fig_chain(chain, cat, on, len(vs))}</div></div>']
    # variant table
    rows = []
    for v in vs:
        newest = sorted(newest_obs(v).values(), key=lambda o: -o["mtime"])
        o = newest[0] if newest else None
        eng = " ".join(f"<b>{'Stomp' if k == 'stomp' else 'Native'}</b> {how_badges(h)}"
                       for k, h in sorted(v.engines.items(), reverse=True))
        near = None
        for w in vs:
            if w is not v:
                dd = diff_blocks(w.blocks, v.blocks, cat)
                if near is None or len(dd) < len(near[1]):
                    near = (w, dd)
        diffs = ""
        if near:
            diffs = (f"vs <b>{e(near[0].label)}</b>: " + (e("; ".join(near[1][:5])) if near[1]
                                                          else "same settings, other file")
                     + (f" (+{len(near[1]) - 5} more)" if len(near[1]) > 5 else ""))
        nulls = ""
        if o:
            nulls = f'{o["null"]:.1f} dB' + (f' <small>{e(o["ledger_ts"])}</small>' if o["ledger_ts"] else "")
            if len(v.obs) > 1:
                nulls += f" <small>({len(v.obs)} runs: " + ", ".join(f"{x['null']:.1f}" for x in v.obs[:5]) + ")</small>"
        rows.append(f"<tr><td><b>{e(v.label)}</b><br><small>{e(v.context)}</small></td><td>{eng}</td><td><small>{diffs}</small></td>"
                    f'<td class="{"st-mis" if o and o["null"] > PARITY_DB else ""}">{nulls}</td></tr>')
    out.append("<h4>Variants tested</h4><div class='scroll'><table><tr><th>Preset</th><th>Engines and how</th>"
               "<th>What changed, against the closest other variant</th><th>Stomp vs Native null (newest run)</th></tr>"
               + "".join(rows[:80]) + (f"<tr><td colspan=4>... {len(rows) - 80} more</td></tr>" if len(rows) > 80 else "") + "</table></div>")
    # parameter bars
    shown = vs[:idx_cap]
    leg = "".join(f'<span><svg width="14" height="12" viewBox="0 0 14 12">{marker(SHAPES[i % 8], 7, 6, OKABE[i % 8], 4)}</svg> {e(v.label)}</span>'
                  for i, v in enumerate(shown))
    out.append(f'<h4>Parameters per block, each tested variant overlaid</h4><p class="legend">{leg}'
               f'{f" <small>(first {idx_cap} of {len(vs)} variants)</small>" if len(vs) > idx_cap else ""}</p>'
               "<p class=mute><small>Position of each value inside the parameter's range (0 left, 1 right); grey = identical in every variant shown; coloured = varied; hover for the exact value.</small></p><div class='figs wide'>")
    for bi, b in enumerate(chain):
        out.append(f'<div class="fig">{fig_params(bi, b["model"], shown, cat)}</div>')
    out.append("</div>")
    # spectra per DI group
    for gi, (di, members) in enumerate(card["groups"].items()):
        okm = [(v, o) for v, o in members if spectra_for(o) is not None]
        if not okm:
            continue
        dname = "/".join([*Path(di).parts[-3:-1], Path(di).stem]) or "DI"  # no audio extension shown
        out.append(f"<details{' open' if gi == 0 else ''}><summary><b>Renders through {e(dname)}</b> ({len(okm)} variants with both renders)</summary>")
        specs = []
        out.append("<div class='figs'>")
        for v, o in okm:
            sp = spectra_for(o)
            specs.append((v, o, sp))
            r = o["res"]
            out.append(f'<div class="fig">{fig_spectrum(v.label, sp, r)}'
                       f'<small>null {r["null_residual_db"]:.1f} dB (body {fmt_db(r.get("body_null_residual_db"))}, tail {fmt_db(r.get("tail_null_residual_db"))}); '
                       f'lag {r.get("lag_ms", 0):.2f} ms; xcorr {r.get("xcorr_peak", 0):.3f}; Stomp level offset {r.get("gain_db", 0):+.2f} dB; '
                       f'Native RMS {r.get("native_rms_db", 0):.1f} dB FS; dashed red line = {PARITY_DB:.0f} dB parity line; '
                       f'run <code>{e(o["run"])}</code></small></div>')
        out.append("</div>")
        # variant diffs
        if len(specs) >= 2:
            names = [v.label for v, _, _ in specs]
            bidx = min(range(len(names)), key=lambda k: (base_rank(names[k]), k))
            bv, _, bsp = specs[bidx]
            others = [(v, sp) for k, (v, _, sp) in enumerate(specs) if k != bidx][:8]
            out.append(f"<h4>What each change did, relative to {e(bv.label)} (same DI)</h4><div class='figs'>")
            for engine in ("native", "stomp"):
                curves = [(f"{v.label}: " + ("; ".join(diff_blocks(bv.blocks, v.blocks, cat)[:3]) or "other file"), sp[engine] - bsp[engine]) for v, sp in others]
                out.append(f'<div class="fig">{fig_diff(("Native" if engine == "native" else "Stomp") + f": variant minus {bv.label}", bv.label, curves, bsp["centres"], engine)}'
                           + "<small>" + "<br>".join(f'<span class="sw" style="background:{OKABE[i % 8]}"></span>{e(lab)}' for i, (lab, _) in enumerate(curves)) + "</small></div>")
            out.append("</div><p class='mute'><small>If the two panels agree, both engines hear the change the same way; where they differ, that change is where the engines diverge. Stomp renders are level-matched to Native per preset (offset in each caption), so level changes between presets are partly absorbed.</small></p>")
        out.append("</details>")
    out.append("</div>")
    return "".join(out)


def blockserial_html(ev: Evidence, cat: dict, inner: bool = False) -> str:
    if not ev.blockserial:
        return ""
    rows = "".join(f"<tr><td><code>{e(b['config'])}</code></td><td>{len(b['res']['stages'])}</td>"
                   f"<td class='n'>{b['res'].get('null_aligned_db', float('nan')):.1f} dB</td>"
                   f"<td class='n'>{b['res'].get('gain_db', 0):+.2f} dB</td></tr>" for b in ev.blockserial)
    big = max(ev.blockserial, key=lambda b: len(b["res"]["stages"]))
    body = (f"<div class='scroll'><table><tr><th>config</th><th>blocks</th><th>null, aligned</th>"
            f"<th>level offset</th></tr>{rows}</table></div>"
            f"<div class='figs'><div class='fig'>{fig_stages(big['res'], cat)}<small>{e(big['config'])}</small></div></div>")
    return body if inner else f'<div class="card"><h3>Block-serial results</h3>{body}</div>'


def findings_html(policy: list[dict], model: dict, ev: Evidence) -> str:
    out = ["<ul>"]
    for r in policy:
        eng = r.get("engines", {}).get("stomp", {})
        out.append(f"<li><b>{e(r.get('category', ''))} {e(r.get('param', ''))}</b>: Stomp policy <b>{e(eng.get('action', ''))}</b> "
                   f"at {e(eng.get('value', ''))}. Evidence: {e(r.get('evidence', ''))}. Until: {e(r.get('until', ''))}. "
                   "(<code>src/tonematch/param_policy.json</code>)</li>")
    mism = [m for m in model["models"].values() if m["status"] in ("partial", "mismatch")]
    for m in sorted(mism, key=lambda m: m["name"]):
        out.append(f"<li><b>{e(m['name'])}</b> ({e(m['cat'])}) {status_html(m['status'])}: "
                   + e("; ".join(sorted(set(m["notes"]))[:4])) + "</li>")
    out.append("</ul>")
    return "".join(out)


LIMITS = [
    ("Parity is whole-chain: one Stomp-vs-Native null per preset. A failed null is blamed on a single block only when a "
    "rule pins it (the cab Early Reflections policy, or an echo tail with a clean body); otherwise on every enabled block, "
    "so a model's status can be pessimistic."),
    f"The parity line is {PARITY_DB:.0f} dB. It is a reading aid chosen for this report, not a gate threshold.",
    ("Tested means a preset containing the model was rendered, written or dropped on that engine. It says nothing "
    "about every setting of the model; the settings columns list exactly the values that were tried."),
    ("Search runs try thousands of parameter combinations; only each run's final values and the files kept on disk are listed. "
    "Dropped-file counts show min..max per parameter."),
    "Preset files that were rewritten after a parity run, or that cannot be found, are skipped and listed under sources.",
    "Spectra come from the aligned, level-matched stereo files of DI renders (Stomp left, Native right); no song audio or stem is read.",
    ("The catalog is HX Edit 3.82. Native is assumed to carry every model in it; Stomp availability follows the catalog's Stomp flag "
    "and minimum firmware against 3.50. Routing ends and the pan twins of the IR cabs are not counted."),
]


def render_html(model: dict, cards: list[dict], ev: Evidence, cat: dict, policy: list[dict], gen: str) -> str:
    n_var = len(ev.variants)
    parts = [(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Block coverage: HX Stomp and Helix Native</title><style>{CSS}</style></head><body>{HATCH}"),
             "<h1>Block coverage: HX Stomp and Helix Native, side by side</h1>",
             (f"<p class='mute'>Generated {e(gen)} by <code>scripts/block_coverage_report.py</code>. LOCAL ONLY: this page names Line 6 models "
             "and sits in a gitignored folder. Catalog: HX Edit "
             f"{e(cat.get('_source', {}).get('hxedit_version', NATIVE_TEXT))}; pedal firmware {PEDAL_FW_TEXT}; Native {NATIVE_TEXT}. "
             f"Read {n_var} distinct presets, {sum(len(v.obs) for v in ev.variants.values())} parity measurements, "
             f"{len(ev.blockserial)} block-serial runs.</p>"),
             ("<div class='legend'><span><svg width='34' height='10'><path class='stomp' d='M0 5L34 5' style='stroke:var(--stomp);stroke-width:2.5;fill:none'/></svg> Stomp</span>"
             "<span><svg width='34' height='10'><path d='M0 5L34 5' style='stroke:var(--native);stroke-width:2.5;stroke-dasharray:6 3;fill:none'/></svg> Native</span>"
             f"<span>in parity = null at or below {PARITY_DB:.0f} dB</span><span>written = sent to the pedal, not rendered</span></div>"),
             "<h2>1. Dashboard by GUI category</h2>",
             ("<p class='mute'><small>Rendered counts models seen in a rendered preset; parity counts models in a preset that both engines rendered. "
             "Known mismatch = a model whose chain failed the parity line (see section 4).</small></p>"),
             dash_html(model), "<h2>2. Models by category</h2>"]
    for d in model["dash"]:
        parts.append(category_section(d["cat"], model, cat))
    parts.append("<h2>3. Presets and chains tested</h2>")
    for card in cards:
        parts.append(card_html(card, ev, cat))
    parts.append("<h2>4. Findings and known mismatches</h2>"
                 "<p>Parity findings in <code>docs/DECISIONS.md</code> (DECISIONS 2026-10-15 parity finding cab EarlyReflections) and the per-engine "
                 "parameter policy:</p>" + findings_html(policy, model, ev))
    parts.append("<h2>5. Limits</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in LIMITS) + "</ul>")
    if ev.unresolved:
        parts.append("<details><summary>Parity results skipped or without a preset file "
                     f"({len(ev.unresolved)})</summary><ul>" + "".join(f"<li><small>{e(u)}</small></li>" for u in ev.unresolved) + "</ul></details>")
    parts.append("<h2>Sources read</h2><p class='mute'><small>" + e(", ".join(f"{k}: {v}" for k, v in sorted(ev.sources.items())))
                 + ". Tasks: T018, T049, T053. Scripts: scripts/parity_null.py, scripts/status_report.py.</small></p></body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- markdown

def render_markdown(model: dict, cards: list[dict], ev: Evidence, cat: dict, policy: list[dict], gen: str) -> str:
    md = ["# Block coverage: HX Stomp and Helix Native", "",
          (f"Generated {gen} by `scripts/block_coverage_report.py`. **Local only**: names Line 6 models, gitignored. "
          "Open `index.html` next to this file for the charts (spectra, per-octave residuals, parameter overlays)."), "",
          (f"Catalog HX Edit {cat.get('_source', {}).get('hxedit_version', NATIVE_TEXT)}; pedal firmware {PEDAL_FW_TEXT}; Native {NATIVE_TEXT}. "
          f"In parity = Stomp-vs-Native null at or below {PARITY_DB:.0f} dB."), "", "## Dashboard", "",
          "| Category | Stomp 3.50 | Stomp needs newer fw | Native 3.82 | Rendered on Stomp | Rendered in Native | Parity checked | In parity | Known mismatch | Untested in Native |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    keys = ("stomp_avail", "stomp_newfw", "native_avail", "stomp_tested", "native_tested", "parity", "in_parity", "mismatch", "untested")
    tot = Counter()
    for d in model["dash"]:
        md.append(f"| {d['cat']} | " + " | ".join(str(d[k]) for k in keys) + " |")
        for k in keys:
            tot[k] += d[k]
    md.append("| **All** | " + " | ".join(f"**{tot[k]}**" for k in keys) + " |")
    md += ["", "## Tested models by category", ""]
    for d in model["dash"]:
        tested = sorted((m for m in model["models"].values() if m["cat"] == d["cat"] and (m["stomp_hows"] or m["native_hows"])),
                        key=lambda m: m["name"])
        md.append(f"### {d['cat']}")
        md.append("")
        if tested:
            md += ["| Model | Stomp | Native | Parity null (dB) | Status |", "|---|---|---|---|---|"]
            for m in tested:
                def cell(eng: str, m: dict = m) -> str:
                    if not m[f"{eng}_hows"]:
                        return "no"
                    summ, _ = settings_summary(m["raw"], eng, cat, m["id"])
                    summ = re.sub(r"<br>", "; ", summ)
                    summ = html.unescape(re.sub(r"<[^>]+>", " ", summ))
                    return md_cell(", ".join(HOW_LABEL.get(h, h) for h in m[f"{eng}_hows"]) + (f"; {summ}" if summ else "")
                                   + ("" if m[f"{eng}_rendered"] else " (written only)"))
                par = "; ".join(f"{o['label']} {o['null']:.1f}" for o in m["parity_obs"] if o["enabled"])[:240]
                md.append(f"| {md_cell(m['name'])} | {cell('stomp')} | {cell('native')} | {md_cell(par) or 'none'} | {m['status']} |")
        else:
            md.append("No model of this category has been rendered yet.")
        ids = [i for i in model["cats"][d["cat"]]["ids"] if i not in model["models"]
               or not (model["models"][i]["stomp_hows"] or model["models"][i]["native_hows"])]
        if ids:
            md += ["", f"<details><summary>{len(ids)} untested models</summary>", "",
                   ", ".join(model_name(cat, i) for i in sorted(ids, key=lambda i: model_name(cat, i).lower())), "", "</details>"]
        md.append("")
    md += ["## Chains tested", ""]
    for card in cards:
        if card.get("kind") == "probe":
            continue
        md.append("### " + " -> ".join(model_name(cat, m) for m in card["chain"]))
        md += ["", "| Preset | Engines | Differences vs first | Null (dB) |", "|---|---|---|---|"]
        base = card["variants"][0]
        for v in card["variants"][:40]:
            o = max(v.obs, key=lambda x: x["mtime"]) if v.obs else None
            diffs = "" if v is base else "; ".join(diff_blocks(base.blocks, v.blocks, cat)[:4])
            null = f"{o['null']:.1f}" if o else ""
            md.append(f"| {md_cell(v.label)} | {md_cell(', '.join(sorted(v.engines)))} | {md_cell(diffs)} | {null} |")
        md.append("")
    md += ["## Findings", ""]
    for r in policy:
        eng = r.get("engines", {}).get("stomp", {})
        md.append(f"- {r.get('category')} {r.get('param')}: Stomp {eng.get('action')} at {eng.get('value')}. {r.get('evidence', '')}")
    md += ["", "## Limits", ""] + [f"- {x}" for x in LIMITS]
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------- main

def safe_out(root: Path, out: Path) -> Path:
    allowed = (root / OUT_SUBDIR).resolve()
    o = out.resolve()
    if o != allowed and allowed not in o.parents:
        raise SystemExit(f"refusing to write outside {allowed}: {o}")
    return o


def generate(root: Path, out: Path, formats: tuple[str, ...] = ("html", "md"), json_path: Path | None = None) -> dict:
    root = root.resolve()
    out = safe_out(root, out)
    cat = load_catalog(root)
    policy = load_policy(root)
    ev, _ = collect(root)
    model = build_model(root, ev, cat, policy)
    cards = group_cards(ev, cat)
    gen = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    rel_root = os.path.relpath(root, out).replace(os.sep, "/")
    if "html" in formats:
        page = render_html(model, cards, ev, cat, policy, gen)
        page = evidence_links.linkify_html(page, evidence_links.Index(root, rel_root))
        page = task_links.linkify_html(page, task_links.parse_board(root), task_links.rel_prefix(root, out))
        (out / "index.html").write_text(page, encoding="utf-8")
        written.append(out / "index.html")
    if "md" in formats:
        text = render_markdown(model, cards, ev, cat, policy, gen)
        text = evidence_links.linkify_markdown(text, evidence_links.Index(root, rel_root))
        text = task_links.linkify_markdown(text, task_links.parse_board(root), task_links.rel_prefix(root, out))
        (out / "README.md").write_text(text, encoding="utf-8")
        written.append(out / "README.md")
    if json_path is not None:
        jp = safe_out(root, json_path)
        jp.parent.mkdir(parents=True, exist_ok=True)
        summary = {"dash": model["dash"],
                   "models": {k: {x: v[x] for x in ("name", "cat", "status", "stomp_hows", "native_hows")}
                              for k, v in model["models"].items()},
                   "variants": len(ev.variants), "unresolved": ev.unresolved}
        jp.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        written.append(jp)
    return {"written": written, "model": model, "cards": cards, "evidence": ev}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--out", type=Path, default=None, help="default <root>/reports/local/blocks (must stay under reports/local)")
    ap.add_argument("--format", default="html,md")
    ap.add_argument("--json", type=Path, default=None, help="also dump a small JSON summary (under reports/local)")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    out = a.out or (root / OUT_SUBDIR / "blocks")
    res = generate(root, out, tuple(x.strip() for x in a.format.split(",")), a.json)
    for p in res["written"]:
        print(p)
    for d in res["model"]["dash"]:
        print(f"{d['cat']:<18} stomp {d['stomp_avail']:>3} native {d['native_avail']:>3} | rendered S {d['stomp_tested']} N {d['native_tested']} "
              f"| parity {d['parity']} ok {d['in_parity']} mismatch {d['mismatch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
