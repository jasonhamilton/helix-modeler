"""gate2_report: a plain-language, local HTML report on the Gate 2 methods (owner: benchmark).

  build            (default) read existing outputs only and write reports/local/gate2/index.html plus
                   a loudness-matched copy of any audio pairs. Runs no Native and no pedal. Local and
                   gitignored: do not commit its output.
  render-examples  re-render target and guess for chosen items from the saved item JSON (pred_model,
                   pred_values). Needs Native; refuses while other native_host processes run. Run by
                   the lead, not by CI. Writes reports/local/gate2/rendered/<run>/<method>/.

The "3 results" are the methods compared on synthetic-v1: middle of every knob, random walk (300
tries), and our search (dev16 stages, hybrid tuning, later the full 96). Runs are discovered from
cache/synthetic-v1/runs/*/ item files; every number carries a source tooltip.
"""
# ruff: noqa: ISC004
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from tonematch import cli_result

SET_REL = Path("cache") / "synthetic-v1"
SET_REL_V2 = Path("cache") / "synthetic-v2"
SET_REL_V3 = Path("cache") / "synthetic-v3"
OUT_REL = Path("reports") / "local" / "gate2"
SR = 48000
N_DEV = 16
CATEGORIES = ("drive", "delay", "reverb", "comp", "eq", "mod", "amp", "cab")
LEVEL_KNOBS = {"Output", "Volume", "Level"}
# Pass-rule numbers, frozen in docs/DECISIONS.md ("Gate 2 pass rule FROZEN").
BARS = {"category": 0.85, "model": 0.60, "mae": 0.15, "rank1": 0.95, "model_vs_random": -0.05}
# Okabe-Ito palette: safe for the common kinds of colour blindness.
COL = {"middle": "#999999", "random": "#E69F00", "s0": "#56B4E9", "s1": "#0072B2", "s2": "#009E73",
       "hyb": "#CC79A7", "full": "#D55E00", "truth": "#000000"}
STAGE_TAGS = [("v3_dev16_before", "Ours: first version (pool pass)", "s0"),
              ("v3_dev16_after", "Ours: + model choice", "s1"),
              ("v4b_dev16_seed", "Ours: + measured seeding", "s2")]
# The run choice lives here, in one place. Changing it changes the headline, the examples, the charts and the preview.
# "full": the Gate 2 run over all 96 items. "dev": the dev16 method chosen by the rule written in DECISIONS.md
# (T114, iteration 3) BEFORE that iteration ran. Nothing is picked by looking at the results.
DESIGNATED = {"full": "gate2_full_hybrid", "dev": "h3_f40_k4_lf"}
# The two Gate 2 attempts (DECISIONS 2026-10-17). Attempt 2 is the headline; attempt 1 stays as the earlier attempt.
# Attempt 2 ran on the sealed set synthetic-v2 with no dev16 tuning: all 96 items are untuned.
ATTEMPTS = {
    "v1": {"set_rel": SET_REL, "set": "synthetic-v1", "full": DESIGNATED["full"], "dev": DESIGNATED["dev"],
           "middle": "v3_base_middle", "random": "v3_base_random", "stages": True, "n": 1},
    "v2": {"set_rel": SET_REL_V2, "set": "synthetic-v2", "full": "gate2_v2_attempt2", "dev": None,
           "middle": "v2_base_middle", "random": "v2_base_random", "stages": False, "n": 2},
    # Attempt 3 (DECISIONS 2026-10-18): sealed synthetic-v3, rank-1 under the extended rule B, no tuning items. Spent on its one run.
    "v3": {"set_rel": SET_REL_V3, "set": "synthetic-v3", "full": "gate2_v3_attempt3", "dev": None,
           "middle": "v3_base_middle", "random": "v3_base_random", "stages": False, "n": 3},
}
LATER = ("v2", "v3")        # attempts that ran with the specialists on and no dev16 tuning
# Tuning history: (folder, name, eligibility). Shown in a table, never used as the headline.
TUNING_HISTORY = [
    ("h1_f50_k5", "Iteration 1 (hybrid, f 0.5, K 5)", "ineligible: over budget (313 to 339 renders against 300)"),
    ("h2_f40_k4", "Iteration 2 (f 0.4, K 4, scale 0.8)", "eligible; met 2 of 3 dev targets"),
    ("h3_f40_k4_lf", "Iteration 3 (f 0.4, K 4, scale 0.9, level-free broad)", "eligible; met 3 of 3 dev targets: CHOSEN"),
]
# Effect families (DECISIONS 2026-10-17, "a specialist per effect family"). Categories in the synthetic set map to them.
FAMILIES = [("gain", "Gain", ("drive", "amp")), ("linear", "Linear", ("cab", "eq")), ("dynamics", "Dynamics", ("comp",)),
            ("time", "Time-based", ("delay", "reverb")), ("mod", "Modulation", ("mod",))]
FAMILY_OF = {c: key for key, _, cats in FAMILIES for c in cats}
FAMILY_NAME = {key: name for key, name, _ in FAMILIES}
HEAT_ORDER = tuple(c for _, _, cats in FAMILIES for c in cats)
# planned / building / adopted. "adopted" only after the adoption rule is met on a family (see the specialists section).
# "in use" = already part of the general method (time-based measured seeding, T113, inside the hybrid);
# "planned" = specialist not started. Set by the lead (2026-10-17).
# Updated by the lead's brief for Gate 2 attempt 2 (2026-10-17): `--specialists all` switched on gain (amp specialist with
# its refiner), linear (cab + EQ screen, seeds, refiner), time (family score 0.25) and mod (screen and seeds only).
SPECIALIST_STATUS = {"gain": "in use", "linear": "in use", "dynamics": "planned", "time": "in use", "mod": "in use"}
SPECIALIST_NOTE = {"gain": "amp specialist with its own refiner", "linear": "cab and EQ screen, seeds and refiner",
                   "dynamics": "stub only; no compressor specialist yet", "time": "echo and decay seeds plus a family score",
                   "mod": "seeds and screen only; no family score"}
ANCHOR = "Gate 2 pass rule FROZEN"
AUDIO_SECONDS = 5.0
AUDIO_LIMIT = 16


# --------------------------------------------------------------------------- reading

def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load_items(run_dir: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(run_dir.glob("i[0-9]*.json")):
        d = read_json(p)
        if isinstance(d, dict) and d.get("id"):
            out[d["id"]] = d
    return out


def load_ledger(root: Path) -> list[dict]:
    p = root / "ledger" / "runs.csv"
    try:
        with p.open(newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def frozen_rule_text(root: Path) -> str:
    try:
        lines = (root / "docs" / "DECISIONS.md").read_text().splitlines()
    except OSError:
        return ""
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## ") and ANCHOR in ln), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def item_mae(it: dict) -> float | None:
    """Mean knob error of one item, level knobs excluded (Ruling 3); None if no right-model figure."""
    errs = it.get("param_abs_err")
    if not isinstance(errs, dict):
        return None
    v = [e for n, e in errs.items() if n not in LEVEL_KNOBS and finite(e)]
    return float(np.mean(v)) if v else None


def middle_mae(it: dict) -> float | None:
    errs = it.get("param_mid_abs_err")
    if not isinstance(errs, dict):
        return None
    v = [e for n, e in errs.items() if n not in LEVEL_KNOBS and finite(e)]
    return float(np.mean(v)) if v else None


def item_view(method_kind: str, it: dict) -> dict:
    """The fields every method is compared on. The middle baseline uses its 'nothing given' pick."""
    if method_kind == "middle":
        al = it.get("all") or {}
        return {"category_ok": bool(al.get("category_ok", it.get("category_ok", False))),
                "model_ok": bool(al.get("model_ok", False)), "mae": middle_mae(it),
                "pred_model": al.get("pred_model", it.get("pred_model")),
                "pred_category": al.get("pred_category", it.get("pred_category"))}
    return {"category_ok": bool(it.get("category_ok")), "model_ok": bool(it.get("model_ok")),
            "mae": item_mae(it) if it.get("model_ok") else None,
            "pred_model": it.get("pred_model"), "pred_category": it.get("pred_category")}


# --------------------------------------------------------------------------- statistics

def metrics(kind: str, items: dict[str, dict], ids=None) -> dict:
    ids = [i for i in (sorted(items) if ids is None else sorted(ids)) if i in items]
    rows = [items[i] for i in ids]
    views = [item_view(kind, r) for r in rows]
    n = len(rows)
    out: dict = {"n": n, "ids": ids}
    if not n:
        return out
    out["category"] = float(np.mean([v["category_ok"] for v in views]))
    out["model"] = float(np.mean([v["model_ok"] for v in views]))
    maes = [v["mae"] for v in views if v["mae"] is not None and finite(v["mae"])]
    out["mae"] = float(np.mean(maes)) if maes else None
    out["mae_n"] = len(maes)
    r1 = [r["rank1_audible"] for r in rows if "rank1_audible" in r]
    r1p = [r["rank1"] for r in rows if "rank1" in r]
    out["rank1"] = float(np.mean(r1)) if r1 else None
    out["rank1_plain"] = float(np.mean(r1p)) if r1p else None
    out["rank1_n"] = len(r1)
    ho = [r["loss_heldout"] for r in rows if finite(r.get("loss_heldout"))]
    out["heldout_median"] = float(np.median(ho)) if ho else None
    out["heldout_mean"] = float(np.mean(ho)) if ho else None
    sr = [r["true_model_screen_rank"] for r in rows if finite(r.get("true_model_screen_rank"))]
    out["screen_rank_median"] = float(np.median(sr)) if sr else None
    out["model_hits"] = int(sum(v["model_ok"] for v in views))
    out["category_hits"] = int(sum(v["category_ok"] for v in views))
    by = {}
    for c in CATEGORIES:
        cv = [v for r, v in zip(rows, views, strict=True) if r.get("category") == c]
        by[c] = {"n": len(cv), "category": sum(v["category_ok"] for v in cv),
                 "model": sum(v["model_ok"] for v in cv)}
    out["by_category"] = by
    return out


def bootstrap_ci(values, n_boot: int = 5000, seed: int = 20261017) -> tuple[float, float, float]:
    """Mean and 95 % percentile interval of `values` (paired differences or per-item scores)."""
    v = np.asarray([x for x in values if finite(x)], dtype=float)
    if v.size == 0:
        return (float("nan"),) * 3
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n_boot, v.size), replace=True).mean(axis=1)
    return float(v.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def paired_vs_random(ours: dict, rnd: dict, ids) -> dict:
    """Paired comparison of ours against random walk over `ids` (rule 2 and rule 3)."""
    ids = [i for i in ids if i in ours and i in rnd]
    ov = {i: item_view("ours", ours[i]) for i in ids}
    rv = {i: item_view("random", rnd[i]) for i in ids}
    d_model = [float(ov[i]["model_ok"]) - float(rv[i]["model_ok"]) for i in ids]
    both = [i for i in ids if ov[i]["model_ok"] and rv[i]["model_ok"]
            and ov[i]["mae"] is not None and rv[i]["mae"] is not None]
    d_mae = [ov[i]["mae"] - rv[i]["mae"] for i in both]
    m = bootstrap_ci(d_model)
    p = bootstrap_ci(d_mae)
    return {"n": len(ids), "model_diff": m, "mae_diff": p, "both_right": len(both)}


def boot_metric_ci(kind: str, items: dict, ids, key: str) -> tuple[float, float, float]:
    """Item-resampled 95 % interval of one headline metric (used for the 80-item agreement check)."""
    vals = []
    for i in ids:
        if i not in items:
            continue
        v = item_view(kind, items[i])
        if key == "mae":
            if v["mae"] is not None:
                vals.append(v["mae"])
        elif key == "rank1":
            if "rank1_audible" in items[i]:
                vals.append(float(items[i]["rank1_audible"]))
        else:
            vals.append(float(v[f"{key}_ok"]))
    return bootstrap_ci(vals)


def evaluate_pass(ours: dict, rnd: dict | None, total: int, all_ids: list[str]) -> dict:
    """Pass-rule checks. `ours` are the items of the full-run candidate. Every check is None (waiting)
    unless all `total` items are present."""
    checks = []
    n = len(ours)
    complete = n >= total > 0
    dev = {f"i{k:03d}" for k in range(N_DEV)}
    test80 = [i for i in all_ids if i not in dev]
    m_all = metrics("ours", ours)
    m_80 = metrics("ours", ours, test80)

    def add(key, text, bar, value, ok, note=""):
        checks.append({"key": key, "text": text, "bar": bar, "value": value,
                       "ok": (ok if complete else None), "note": note})

    add("category", "Right kind of effect (category)", ">= 0.85", m_all.get("category"),
        m_all.get("category", 0) >= BARS["category"] if n else None)
    add("model", "Right model", ">= 0.60", m_all.get("model"),
        m_all.get("model", 0) >= BARS["model"] if n else None)
    add("mae", "Settings error on right-model items", "<= 0.15", m_all.get("mae"),
        (m_all["mae"] <= BARS["mae"]) if m_all.get("mae") is not None else None)
    add("rank1", "Score ranks the true preset first (rule B)", ">= 0.95", m_all.get("rank1"),
        (m_all["rank1"] >= BARS["rank1"]) if m_all.get("rank1") is not None else None)
    pv = paired_vs_random(ours, rnd, all_ids) if rnd else None
    if pv and pv["n"]:
        d = pv["model_diff"]
        add("vs_random_model", "Model accuracy not worse than random walk", "CI low >= -0.05", d[0],
            d[1] >= BARS["model_vs_random"], f"difference {d[0]:+.3f}, 95% CI {d[1]:+.3f} to {d[2]:+.3f}")
        d = pv["mae_diff"]
        ok = (d[2] < 0) if finite(d[2]) else None
        add("vs_random_mae", "Settings error better than random walk (items both got right)",
            "CI high < 0", d[0], ok,
            f"{pv['both_right']} items both right; difference {d[0]:+.3f}, 95% CI {d[1]:+.3f} to {d[2]:+.3f}"
            if finite(d[0]) else "no item both got right")
    else:
        add("vs_random_model", "Model accuracy not worse than random walk", "CI low >= -0.05", None, None)
        add("vs_random_mae", "Settings error better than random walk", "CI high < 0", None, None)
    agree = []
    for key in ("category", "model", "mae", "rank1"):
        _, lo, hi = boot_metric_ci("ours", ours, all_ids, key)
        v80 = m_80.get(key)
        agree.append(None if v80 is None or not finite(lo) else lo - 1e-9 <= v80 <= hi + 1e-9)
    ag = None if (not complete or None in agree) else all(agree)
    add("split80", "The 80 items outside dev16 agree with the 96", "within each CI", None, ag,
        f"80-item figures: category {fmt(m_80.get('category'))}, model {fmt(m_80.get('model'))}, "
        f"settings {fmt(m_80.get('mae'))}, rank-1 {fmt(m_80.get('rank1'))}" if m_80.get("n") else "")
    allok = None if (not complete or any(c["ok"] is None for c in checks)) \
        else all(c["ok"] for c in checks)
    return {"checks": checks, "complete": complete, "n": n, "all_ok": allok, "m_all": m_all, "m_80": m_80}


# --------------------------------------------------------------------------- discovery

def run_meta(run_dir: Path, items: dict) -> dict:
    sums = [s for s in (read_json(p) for p in sorted(run_dir.glob("summary*.json"))) if isinstance(s, dict)]
    mt = 0.0
    for p in run_dir.glob("i[0-9]*.json"):
        mt = max(mt, p.stat().st_mtime)
    return {"summaries": sums, "mtime": mt,
            "admitted": bool(sums) and all(bool(s.get("admitted")) for s in sums),
            "gate_evidence": any(s.get("gate_evidence") for s in sums),
            "budget": next((s.get("budget") for s in sums if s.get("budget")), None)}


def discover(root: Path, attempt: str = "v1") -> dict:
    """Find the methods. Returns {'labels', 'dev_ids', 'all_ids', 'methods', 'other'}."""
    cfg = ATTEMPTS[attempt]
    set_dir = root / cfg["set_rel"]
    labels = read_json(set_dir / "labels.json") or []
    all_ids = [x["id"] for x in labels]
    dev_ids = [f"i{k:03d}" for k in range(N_DEV)]
    runs_dir = set_dir / "runs"
    found = {}
    if runs_dir.is_dir():
        for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            items = load_items(d)
            if items:
                found[d.name] = {"tag": d.name, "dir": d, "items": items, **run_meta(d, items)}

    def kind_of(r):
        b = next(iter(r["items"].values())).get("baseline")
        return b if b in ("middle", "random") else "ours"

    methods = []
    used = set()
    for base, label in (("middle", "Middle of every knob"), ("random", "Random walk, 300 tries")):
        pref = cfg[base]
        cands = [r for r in found.values() if kind_of(r) == base]
        pick = found.get(pref) if pref in found and kind_of(found[pref]) == base else None
        if pick is None and cands:
            pick = max(cands, key=lambda r: (r["admitted"], len(r["items"]), r["mtime"]))
        if pick:
            used.add(pick["tag"])
            methods.append(_method(pick, base, base, label, COL[base], dev_ids, all_ids))
    ours = {t: r for t, r in found.items() if kind_of(r) == "ours"}
    for tag, label, col in (STAGE_TAGS if cfg["stages"] else []):
        if tag in ours:
            used.add(tag)
            methods.append(_method(ours[tag], "ours", "stage", label, COL[col], dev_ids, all_ids))
    dev_tag, full_tag = cfg["dev"], cfg["full"]
    if dev_tag and dev_tag in ours:
        used.add(dev_tag)
        methods.append(_method(ours[dev_tag], "ours", "hybrid", f"Ours: chosen dev method ({dev_tag})", COL["hyb"],
                               dev_ids, all_ids))
    if full_tag in found and found[full_tag]["items"]:
        used.add(full_tag)
        methods.append(_method(found[full_tag], "ours", "full", f"Ours: full run ({full_tag})" if attempt == "v1"
                               else f"Ours: attempt {cfg['n']} ({full_tag})", COL["full"], dev_ids, all_ids))
    tuning = [_method(found[t], "ours", "tuning", name, COL["hyb"], dev_ids, all_ids)
              for t, name, _ in (TUNING_HISTORY if cfg["stages"] else []) if t in found]
    spec: dict[str, list[dict]] = {}
    for t, r in found.items():
        for key, _, _ in FAMILIES:
            if f"spec_{key}" in t and r["items"]:
                spec.setdefault(key, []).append(_method(r, "ours", "spec", f"Specialist ({t})", COL["s2"], dev_ids, all_ids))
    other = [r for t, r in found.items() if t not in used]
    # Rank-1 is a property of the score and the item, not of the method: fill it in where a run skipped it.
    r1 = {}
    for r in found.values():
        for i, it in r["items"].items():
            if "rank1_audible" in it:
                r1.setdefault(i, {k: it[k] for k in ("rank1", "rank1_audible") if k in it})
    # Only our runs keep a rank-1 figure. It is a property of the SCORE (true preset vs its 24 neighbours),
    # not of the method, so a baseline column showing it would read as "the baseline finds the true preset"
    # (human question, 2026-10-17). Baselines show no rank-1; the score's rank-1 is reported once.
    for m in methods:
        # keep the raw figure for the report's own explanation (the middle baseline gets the same rank-1 as ours)
        m["rank1_raw"] = {i: it["rank1_audible"] for i, it in m["items"].items() if "rank1_audible" in it}
        if m["kind"] in ("middle", "random"):
            m["items"] = {i: {k: v for k, v in it.items() if k not in ("rank1", "rank1_audible")}
                          for i, it in m["items"].items()}
        else:
            m["items"] = {i: ({**it, **{k: v for k, v in r1.get(i, {}).items() if k not in it}}
                              if "rank1_audible" not in it and "error" not in it else it)
                          for i, it in m["items"].items()}
    return {"labels": labels, "dev_ids": dev_ids, "all_ids": all_ids, "methods": methods, "other": other,
            "found": found, "tuning": tuning, "spec": spec}


def _method(run: dict, kind: str, role: str, label: str, color: str, dev_ids, all_ids) -> dict:
    ids = set(run["items"])
    if role == "full":
        scope = "full" if len(ids) >= len(all_ids) > 0 else f"partial ({len(ids)} of {len(all_ids)})"
    elif ids <= set(dev_ids):
        scope = "dev16"
    else:
        scope = "full" if len(ids) >= len(all_ids) > 0 else f"partial ({len(ids)} of {len(all_ids)})"
    return {"tag": run["tag"], "kind": kind, "role": role, "label": label, "color": color, "items": run["items"],
            "scope": scope, "admitted": run["admitted"], "mtime": run["mtime"], "dir": run["dir"],
            "gate_evidence": run["gate_evidence"], "budget": run["budget"]}


# --------------------------------------------------------------------------- formatting helpers

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt(v, nd: int = 2, pct: bool = False) -> str:
    if v is None or not finite(v):
        return "n/a"
    return f"{v * 100:.0f}%" if pct else f"{v:.{nd}f}"


def pretty_model(model: str | None) -> str:
    if not model:
        return "?"
    m = re.sub(r"^(HD2_|L6SPB_|L6_|VIC_|L6)", "", model)
    m = re.sub(r"^(Dist|Preamp|Amp|Cab|CabMicIr_|Compressor|EQ|Reverb|Delay|Tremolo|Chorus|Vibrato|DM4|DL4|MM4)(?=[A-Z0-9_])",
               lambda g: g.group(1) + " ", m)
    m = m.replace("_", " ")
    m = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", m)
    return re.sub(r"\s+", " ", m).strip()


def src_title(*parts) -> str:
    return "Source: " + "; ".join(str(p) for p in parts if p)


def num(text, src) -> str:
    return f'<span class="num" tabindex="0" title="{esc(src)}">{esc(text)}</span>'


def chip(kind: str, text: str, tip: str = "") -> str:
    return f'<span class="chip {kind}" title="{esc(tip)}">{esc(text)}</span>'


def ok_chip(ok, waiting="waiting for the full run") -> str:
    if ok is None:
        return chip("wait", waiting)
    return chip("pass", "pass") if ok else chip("fail", "not met")


def scope_chip(scope: str) -> str:
    if scope == "dev16":
        return chip("dev", "dev16 only: tuning items, not gate evidence")
    if scope == "full":
        return chip("full", "full 96")
    return chip("wait", scope)


# --------------------------------------------------------------------------- SVG charts

def svg_bars(rows: list[dict], vmax: float, target: float | None = None, target_label: str = "",
             axis_label: str = "", title: str = "", lower_better: bool = False, nd: int = 2) -> str:
    """Horizontal bar chart. rows: label, value (or None = waiting), color, tip, sub."""
    w, left, right, rh, top = 640, 8, 60, 44, 22
    plot = w - left - right
    h = top + rh * len(rows) + 46
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}" class="chart">',
           f"<title>{esc(title)}</title>"]
    for t in range(6):
        x = left + plot * t / 5
        out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{h - 36}" class="grid"/>')
        out.append(f'<text x="{x:.1f}" y="{h - 22}" class="tick" text-anchor="middle">{vmax * t / 5:.2g}</text>')
    out.append(f'<text x="{left + plot / 2:.1f}" y="{h - 6}" class="axis" text-anchor="middle">{esc(axis_label)}</text>')
    for k, r in enumerate(rows):
        y = top + k * rh
        out.append(f"<g><title>{esc(r.get('tip', ''))}</title>")
        out.append(f'<text x="{left}" y="{y + 11}" class="lbl">{esc(r["label"])}'
                   f'<tspan class="sub"> {esc(r.get("sub", ""))}</tspan></text>')
        v = r.get("value")
        if v is None or not finite(v):
            out.append(f'<rect x="{left}" y="{y + 16}" width="{plot}" height="16" class="wait" rx="3"/>')
            out.append(f'<text x="{left + 8}" y="{y + 28}" class="val">waiting</text>')
        else:
            bw = max(1.5, plot * min(max(v, 0), vmax) / vmax)
            out.append(f'<rect x="{left}" y="{y + 16}" width="{bw:.1f}" height="16" fill="{r["color"]}" rx="3"/>')
            out.append(f'<text x="{left + bw + 5:.1f}" y="{y + 28}" class="val">{v:.{nd}f}</text>')
        out.append("</g>")
    if target is not None:
        x = left + plot * min(target, vmax) / vmax
        out.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{h - 36}" class="target"/>')
        out.append(f'<text x="{x + 4:.1f}" y="{top - 9}" class="tgt">{esc(target_label)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_track(rows: list[dict], width: int = 520) -> str:
    """Parameter tracks 0..1. rows: name, marks [(x, color, shape, label)]."""
    rh, left, right = 34, 110, 14
    plot = width - left - right
    h = 14 + rh * len(rows)
    out = [f'<svg viewBox="0 0 {width} {h}" class="chart track" role="img" aria-label="knob positions">']
    for k, r in enumerate(rows):
        y = 18 + k * rh
        out.append(f'<text x="2" y="{y + 4}" class="lbl">{esc(r["name"])}</text>')
        out.append(f'<line x1="{left}" y1="{y}" x2="{left + plot}" y2="{y}" class="rail"/>')
        for x, color, shape, tip in r["marks"]:
            cx = left + plot * min(max(x, 0.0), 1.0)
            if shape == "diamond":
                out.append(f'<path d="M{cx:.1f} {y - 9} l7 9 l-7 9 l-7 -9z" fill="{color}" class="mark">'
                           f"<title>{esc(tip)}</title></path>")
            elif shape == "ring":
                out.append(f'<circle cx="{cx:.1f}" cy="{y}" r="6" fill="none" stroke="{color}" stroke-width="2.5">'
                           f"<title>{esc(tip)}</title></circle>")
            else:
                out.append(f'<circle cx="{cx:.1f}" cy="{y}" r="6" fill="{color}" class="mark">'
                           f"<title>{esc(tip)}</title></circle>")
    out.append("</svg>")
    return "".join(out)


def heat_color(frac: float | None) -> str:
    if frac is None:
        return "transparent"
    a = 0.12 + 0.78 * frac
    return f"rgba(0,114,178,{a:.2f})"


def heatmap(rows: list[dict], key: str, title: str) -> str:
    """rows: label, metrics (with by_category). key: 'model' or 'category'. Columns grouped by effect family."""
    fam_head = "".join(f"<th scope='colgroup' colspan='{len(cats)}'>{esc(name)}</th>" for _, name, cats in FAMILIES)
    head = "".join(f"<th scope='col'>{c}</th>" for c in HEAT_ORDER)
    body = []
    for r in rows:
        bc = r["metrics"].get("by_category") or {}
        cells = []
        for c in HEAT_ORDER:
            e = bc.get(c) or {"n": 0}
            if not e["n"]:
                cells.append("<td>-</td>")
                continue
            frac = e[key] / e["n"]
            dark = frac > 0.55
            cells.append(f'<td style="background:{heat_color(frac)}" class="{"hd" if dark else ""}" '
                         f'title="{esc(r["label"])}: {e[key]} of {e["n"]} {esc(c)} items ({esc(FAMILY_NAME[FAMILY_OF[c]])} family)">'
                         f'{e[key]}/{e["n"]}</td>')
        body.append(f"<tr><th scope='row'>{esc(r['label'])}</th>{''.join(cells)}</tr>")
    return (f'<div class="scroll"><table class="heat" aria-label="{esc(title)}"><caption>{esc(title)}</caption>'
            f"<thead><tr><th></th>{fam_head}</tr><tr><th></th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")


# --------------------------------------------------------------------------- the panels

def method_rows(methods: list[dict], ids, key: str, src_note: str):
    rows = []
    for m in methods:
        mm = metrics(m["kind"], m["items"], ids)
        v = mm.get(key) if mm["n"] else None
        sub = f"({mm['n']} items)" if mm["n"] else ""
        if key == "heldout_median":
            tip = f"{m['label']}: median {fmt(v, 3)}, mean {fmt(mm.get('heldout_mean'), 3)} over {mm['n']} items. {src_note}"
        elif key == "mae":
            basis = "true model at middle" if m["kind"] == "middle" else "items with the right model"
            tip = f"{m['label']}: {fmt(v, 3)} over {mm.get('mae_n', 0)} items ({basis}). {src_note}"
        else:
            tip = f"{m['label']}: {fmt(v, 3)} over {mm['n']} items. {src_note}"
        rows.append({"label": m["label"], "sub": sub, "value": v, "color": m["color"], "tip": tip, "m": mm})
    return rows


def results_panel(methods: list[dict], ids, scope_name: str, wait_label: str | None = None,
                  set_name: str = "synthetic-v1") -> str:
    src = f"run files under cache/{set_name}/runs/ ({scope_name})"
    charts = [
        ("Picked the right kind of effect", "category", 1.0, BARS["category"], "target 0.85",
         "share of items (higher is better)", False),
        ("Picked the exact model", "model", 1.0, BARS["model"], "target 0.60",
         "share of items (higher is better)", False),
        ("Settings error when the model is right", "mae", 0.5, BARS["mae"], "target 0.15",
         "average gap, share of the knob's range (lower is better)", True),
        ("Our score picks the true preset first (rank-1)", "rank1", 1.0, BARS["rank1"], "target 0.95",
         "share of items (higher is better)", False),
        ("Held-out difference on new notes", "heldout_median", 1.0, None, "",
         "median difference score (lower is better; 0 = identical)", True),
    ]
    cards = []
    for title, key, vmax, tgt, tl, axis, lower in charts:
        rows = method_rows(methods, ids, key, src)
        if key == "rank1":   # a property of the score: baselines carry no figure, so do not draw a "waiting" bar for them
            rows = [r for r in rows if r["value"] is not None] or rows
        if wait_label:
            rows.append({"label": wait_label, "sub": "", "value": None, "color": COL["full"],
                         "tip": "waiting for the full run"})
        cards.append(f'<figure class="card"><figcaption>{esc(title)}</figcaption>'
                     f"{svg_bars(rows, vmax, tgt, tl, axis, title, lower)}</figure>")
    return f'<div class="grid2">{"".join(cards)}</div>'


# --------------------------------------------------------------------------- examples

def pick_examples(ours: dict, labels: dict, limit: int = 10) -> list[dict]:
    """6-10 items: wins, near-misses, misses. Deterministic."""
    wins, near, miss = [], [], []
    for i in sorted(ours):
        it = ours[i]
        if "error" in it:
            continue
        mae = item_mae(it) if it.get("model_ok") else None
        if it.get("model_ok") and mae is not None and mae < 0.10:
            wins.append(i)
        elif it.get("model_ok") or it.get("category_ok"):
            near.append(i)
        else:
            miss.append(i)

    def spread(ids, k):
        seen, first, rest = set(), [], []
        for i in ids:
            c = ours[i].get("category")
            (rest if c in seen else first).append(i)
            seen.add(c)
        return (first + rest)[:k]

    pick = spread(wins, 4) + spread(near, 3) + spread(miss, 3)
    if len(pick) < min(limit, len(ours)):
        extra = [i for i in sorted(ours) if i not in pick and "error" not in ours[i]]
        pick += extra[: limit - len(pick)]
    return [{"id": i, "group": "win" if i in wins else "near" if i in near else "miss"} for i in pick[:limit]]


def verdict(it: dict, mae, rnd_it: dict | None, mid_it: dict | None, labels_by_id: dict) -> str:
    cat = it.get("category", "?")
    held = it.get("loss_heldout")
    close = ("sounds very close" if held is not None and held < 0.1 else
             "sounds somewhat different" if held is not None and held < 0.3 else "sounds clearly different")
    if it.get("model_ok"):
        if mae is not None and mae < 0.10:
            return f"Win. Found the exact {cat} block; its knobs are off by {mae * 100:.0f}% of their range on average."
        off = "an unknown amount" if mae is None else f"{mae * 100:.0f}%"
        return f"Right block, knobs a little off: on average {off} of the knob range. On new notes it {close}."
    if it.get("category_ok"):
        tail = " The score still ranked the true block first, so the slip was in the search, not the judge." \
            if it.get("rank1_audible") else ""
        return (f"Near miss. Right kind of effect ({cat}) but chose {pretty_model(it.get('pred_model'))} instead of "
                f"{pretty_model(it.get('model'))}; on new notes it {close}.{tail}")
    pc = it.get("pred_category", "?")
    art = lambda w: "an" if w[:1] in "aeiou" else "a"
    return f"Miss. Called it {art(pc)} {pc} when it was {art(cat)} {cat}; on new notes it {close}."


def example_card(ex: dict, truth: dict, ours_m: dict, mid_m: dict | None, rnd_m: dict | None) -> str:
    i = ex["id"]
    it = ours_m["items"][i]
    mid = (mid_m or {}).get("items", {}).get(i)
    rnd = (rnd_m or {}).get("items", {}).get(i)
    ranges = truth.get("ranges", {})
    tvals = truth.get("values", {})

    def norm(name, v):
        lo, hi = ranges.get(name, [0.0, 1.0])
        return (v - lo) / (hi - lo) if hi > lo else 0.0

    rows = []
    names = [n for n in tvals if n not in LEVEL_KNOBS] or list(tvals)
    for n in names:
        tx = norm(n, tvals[n])
        marks = [(tx, COL["truth"], "diamond", f"True {n}: {tx:.2f}")]
        if mid is not None:
            marks.append((0.5, COL["middle"], "ring", "Middle-of-every-knob guess: 0.50 (always the middle)"))
        pv = it.get("pred_values") or {}
        if it.get("model_ok") and n in pv:
            ox = norm(n, pv[n])
            marks.append((ox, ours_m["color"], "dot", f"Ours: {ox:.2f} (true {tx:.2f})"))
        rows.append({"name": n, "marks": marks})
    held = {"Ours": it.get("loss_heldout"),
            "Random": (rnd or {}).get("loss_heldout"), "Middle": (mid or {}).get("loss_heldout")}
    guess_model = lambda d: pretty_model(d.get("pred_model")) if d else "n/a"
    mid_all = (mid or {}).get("all") or {}
    table = (
        "<table class='mini'><thead><tr><th></th><th>Picked</th><th>Exact model?</th>"
        "<th title='Difference score on new notes; 0 is identical, random picks about 0.7'>Difference on new notes</th></tr></thead><tbody>"
        f"<tr><th scope='row'>Truth</th><td>{esc(pretty_model(it.get('model')))}</td><td>-</td><td>-</td></tr>"
        f"<tr><th scope='row'><span class='dot' style='background:{ours_m['color']}'></span>Ours</th>"
        f"<td>{esc(guess_model(it))}</td><td>{'yes' if it.get('model_ok') else 'no'}</td><td>{fmt(held['Ours'], 3)}</td></tr>"
        f"<tr><th scope='row'><span class='dot' style='background:{COL['random']}'></span>Random</th>"
        f"<td>{esc(guess_model(rnd))}</td><td>{'yes' if (rnd or {}).get('model_ok') else 'no'}</td>"
        f"<td>{fmt(held['Random'], 3)}</td></tr>"
        f"<tr><th scope='row'><span class='dot' style='background:{COL['middle']}'></span>Middle</th>"
        f"<td>{esc(pretty_model(mid_all.get('pred_model')) if mid else 'n/a')}</td>"
        f"<td>{'yes' if mid_all.get('model_ok') else 'no' if mid else 'n/a'}</td><td>{fmt(held['Middle'], 3)}</td></tr>"
        "</tbody></table>")
    mae = item_mae(it) if it.get("model_ok") else None
    word = {"win": ("pass", "win"), "near": ("wait", "near miss"), "miss": ("fail", "miss")}[ex["group"]]
    legend = (f"<p class='legend'><span class='sw diamond'></span>true setting "
              f"<span class='dot' style='background:{ours_m['color']}'></span>our guess "
              f"<span class='ring'></span>middle of the knob</p>")
    tracks = svg_track(rows) if rows and it.get("model_ok") else ""
    note = "" if it.get("model_ok") else \
        "<p class='muted'>Our guess is a different model, so its knobs cannot be compared with the true ones.</p>"
    src = src_title(f"{ours_m['tag']}/{i}.json", "labels.json")
    return (f'<article class="card ex" title="{esc(src)}"><h4>{esc(pretty_model(it.get("model")))} '
            f'<small>({esc(it.get("category"))}, item {esc(i)})</small> {chip(word[0], word[1])}</h4>'
            f'<p class="verdict">{esc(verdict(it, mae, rnd, mid, {}))}</p>{table}{tracks}{legend}{note}</article>')


# --------------------------------------------------------------------------- ledger facts

def ledger_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trust_rows(ledger: list[dict]) -> dict:
    """Pull the trust-check numbers: v1 per term, v2 mrstft versus mrstft_v2 (test split, rule B)."""
    v1 = {}
    for r in ledger:
        if r.get("dataset") == "helix-trust-v1" and r.get("metric") == "rank1_rate":
            t = re.search(r"term=(\S+)", r["notes"])
            s = re.search(r"subset=(\S+)", r["notes"])
            if t and s:
                v1[(t.group(1), s.group(1))] = (ledger_float(r["value"]), r["timestamp"])
    v2 = {}
    for r in ledger:
        if r.get("dataset") == "helix-trust-v2" and r.get("metric") in ("rank1_rate", "rank1_rate_audible") \
                and "TRAIN" not in r["notes"]:
            t = re.search(r"term=(\S+)", r["notes"])
            s = re.search(r"subset=(\S+)", r["notes"])
            if t and s:
                v2[(t.group(1), s.group(1), r["metric"])] = (ledger_float(r["value"]), r["timestamp"])
    return {"v1": v1, "v2": v2}


def verify_rows(ledger: list[dict], dataset: str) -> dict:
    """Latest row per metric of a verify dataset (value, timestamp, note head, 'x/y' if present)."""
    out = {}
    for r in ledger:
        if r.get("dataset") != dataset:
            continue
        m = re.search(r"(\d+)/(\d+)", r.get("notes", ""))
        out[r["metric"]] = {"value": ledger_float(r["value"]), "ts": r["timestamp"],
                            "frac": f"{m.group(1)}/{m.group(2)}" if m else "", "notes": r.get("notes", "")}
    return out


# --------------------------------------------------------------------------- audio

def find_audio_pairs(root: Path, found: dict, methods: list[dict], attempt: str = "v1") -> list[dict]:
    pairs = []
    prefix = f"Attempt {ATTEMPTS[attempt]['n']}: "
    labels_by_tag = {m["tag"]: prefix + m["label"] for m in methods}
    for tag, run in sorted(found.items()):
        for tgt in sorted(run["dir"].glob("*_target.wav")):
            rec = tgt.with_name(tgt.name.replace("_target.wav", "_recovered.wav"))
            if rec.exists():
                pairs.append({"id": tgt.name[:-11], "run": tag, "method": labels_by_tag.get(tag, prefix + tag),
                              "target": tgt, "guess": rec, "source": "search run (--save-audio)", "attempt": attempt})
    rdir = root / OUT_REL / "rendered"
    if rdir.is_dir() and attempt == "v1":
        for tgt in sorted(rdir.glob("*/*/*_target.wav")):
            rec = tgt.with_name(tgt.name.replace("_target.wav", "_recovered.wav"))
            if rec.exists():
                pairs.append({"id": tgt.name[:-11], "run": tgt.parent.parent.name,
                              "method": f"{tgt.parent.parent.name} / {tgt.parent.name}",
                              "target": tgt, "guess": rec, "source": "render-examples", "attempt": attempt})
    return pairs


def choose_players_v3(pairs: list[dict], M3: dict, limit: int = 20) -> dict:
    """Players for attempt 3: its rank-1 misses (ours, then the middle baseline), every delay miss, then clear wins. -> {(run, id): why}"""
    ours_tag = M3["full"]["tag"]
    have = {(p["run"], p["id"]) for p in pairs}
    chosen: dict[tuple[str, str], str] = {}

    def add(tag, i, why):
        if len(chosen) < limit and (tag, i) in have and (tag, i) not in chosen:
            chosen[(tag, i)] = why

    miss = [e["id"] for e in rank1_misses(M3)]
    for i in miss:
        add(ours_tag, i, "rank-1 miss")
    if M3.get("middle"):
        for i in miss:
            add(M3["middle"]["tag"], i, "rank-1 miss, middle-of-every-knob baseline")
    for r in delay_rows(M3):
        add(ours_tag, r["id"], f"delay miss: {pretty_model(r['model'])} named as {pretty_model(r['pred'])}")
    wins = [e["id"] for e in pick_examples(M3["full"]["items"], {}) if e["group"] == "win"]
    delay_wins = [i for i, it in sorted(M3["full"]["items"].items()) if it.get("category") == "delay" and it.get("model_ok")]
    for i in delay_wins[:2]:
        add(ours_tag, i, "delay win")
    for i in wins:
        add(ours_tag, i, "clear win")
    return chosen


def choose_players(pairs: list[dict], M2: dict | None, limit: int = AUDIO_LIMIT, v1_players: int = 6,
                   M3: dict | None = None) -> list[dict]:
    """Mark which pairs get a player on the page. Attempt 2: our recovered sound on the rank-1 miss items, the middle
    baseline on the same items, then some of our clear wins. Attempt 1: the first few. Every pair still gets its stereo file."""
    if M3 and M3.get("full"):
        why = choose_players_v3(pairs, M3)
        return [({**p, "player": (p["run"], p["id"]) in why, "why": why.get((p["run"], p["id"]), "")}
                 if p.get("attempt") == "v3" else {**p, "player": False}) for p in pairs]
    want: list[tuple[str, str]] = []
    if M2 and M2.get("full"):
        miss = [e["id"] for e in rank1_misses(M2)]
        ours_tag = M2["full"]["tag"]
        mid_tag = M2["middle"]["tag"] if M2.get("middle") else None
        want += [(ours_tag, i) for i in miss] + ([(mid_tag, i) for i in miss] if mid_tag else [])
        wins = [e["id"] for e in pick_examples(M2["full"]["items"], {}) if e["group"] == "win"]
        want += [(ours_tag, i) for i in wins]
    chosen = []
    for tag, i in want:
        if len(chosen) >= limit:
            break
        if (tag, i) not in chosen and any(p["run"] == tag and p["id"] == i for p in pairs):
            chosen.append((tag, i))
    n1 = 0
    out = []
    for p in pairs:
        if p.get("attempt") == "v2":
            p = {**p, "player": (p["run"], p["id"]) in chosen}
        else:
            p = {**p, "player": n1 < v1_players}
            n1 += 1
        out.append(p)
    return out


def copy_audio(pairs: list[dict], out: Path, limit: int = AUDIO_LIMIT) -> list[dict]:
    """Loudness-match each pair to -23 LUFS (shared excerpt, one gain rule) and write mono 16-bit WAVs,
    plus a stereo comparison file for EVERY pair: target on the LEFT channel, recovered on the RIGHT
    (human, 2026-10-17), under audio/<method>/stereo_L-target_R-recovered/. Reuses scripts/ab_session
    helpers. Returns the pairs that got players in the page (the first `limit`)."""
    try:
        import ab_session as AB
        import soundfile as sf

        from tonematch.separation.audio import load_audio
    except Exception:  # noqa: BLE001 - audio is optional; the report still builds
        return []
    adir = out / "audio"
    if adir.exists():
        shutil.rmtree(adir)
    done = []
    for k, p in enumerate(pairs):
        try:
            x, y = load_audio(p["target"]), load_audio(p["guess"])
            x, y = AB.trim_pair(x, y, AUDIO_SECONDS)
            x, y, info = AB.match_loudness(x, y)
            xm, ym = np.ascontiguousarray(x.mean(axis=0)), np.ascontiguousarray(y.mean(axis=0))
            sub = adir / re.sub(r"[^A-Za-z0-9_.-]+", "_", p["method"])
            lr_dir = sub / "stereo_L-target_R-recovered"
            lr_dir.mkdir(parents=True, exist_ok=True)
            lp = lr_dir / f"{p['id']}_L-target_R-recovered.wav"
            sf.write(str(lp), np.stack([xm, ym], axis=1), SR, subtype="PCM_16")
            if not p.get("player", k < limit):
                continue
            tp, gp = sub / f"{p['id']}_target.wav", sub / f"{p['id']}_guess.wav"
            sf.write(str(tp), xm, SR, subtype="PCM_16")
            sf.write(str(gp), ym, SR, subtype="PCM_16")
            done.append({**p, "target_rel": tp.relative_to(out).as_posix(), "guess_rel": gp.relative_to(out).as_posix(),
                         "lr_rel": lp.relative_to(out).as_posix(), "lufs": info["lufs_out"]})
        except Exception as exc:  # noqa: BLE001 - skip an unreadable pair, keep the rest
            print(f"gate2_report: audio pair {p['id']} skipped: {exc}", file=sys.stderr)
    return done


# --------------------------------------------------------------------------- page

CSS = """
:root{--bg:#fbfbfa;--fg:#1d2328;--muted:#5a6570;--card:#fff;--bd:#d9dde1;--accent:#0072B2;--grid:#e3e6e9;
--wait:#eceef0;--tick:#5a6570}
@media (prefers-color-scheme:dark){:root{--bg:#14181c;--fg:#e8ebee;--muted:#a3adb7;--card:#1c2228;--bd:#323b44;
--accent:#56B4E9;--grid:#2b333b;--wait:#262d34;--tick:#a3adb7}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
header,main,footer{max-width:1080px;margin:0 auto;padding:0 16px}
header{padding-top:28px}h1{font-size:1.9rem;margin:.2em 0}h2{font-size:1.45rem;margin:2em 0 .4em;border-top:2px solid var(--bd);padding-top:.7em}
h3{font-size:1.1rem;margin:1.2em 0 .3em}h4{margin:.1em 0 .4em;font-size:1rem}
a{color:var(--accent)}p,li{max-width:70ch}.muted,.legend,small{color:var(--muted)}
nav{display:flex;flex-wrap:wrap;gap:6px 14px;margin:.6em 0}nav a{font-size:.9rem}
.lede{font-size:1.1rem;background:var(--card);border:1px solid var(--bd);border-left:5px solid var(--accent);padding:12px 16px;border-radius:8px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:14px}
.chart{width:100%;max-width:680px;height:auto;display:block}.chart .grid{stroke:var(--grid)}.chart text{fill:var(--fg);font:12px system-ui,sans-serif}
.chart .tick,.chart .axis{fill:var(--tick);font-size:11px}.chart .sub{fill:var(--muted)}.chart .val{font-weight:600}
.chart .wait{fill:var(--wait);stroke:var(--muted);stroke-dasharray:4 3}.chart .target{stroke:var(--fg);stroke-width:2;stroke-dasharray:6 4}
.chart .lbl.weak{font-weight:700}.chart .flag{fill:var(--fg);font-weight:700;text-decoration:underline}.chart .tgt{font-size:11px;font-weight:600}.chart .rail{stroke:var(--muted);stroke-width:2}.chart .mark{stroke:var(--bg);stroke-width:1.5}
figcaption{font-weight:600;margin-bottom:4px}
details{background:var(--card);border:1px solid var(--bd);border-radius:8px;margin:.7em 0;padding:.3em 14px}
summary{cursor:pointer;font-weight:600;padding:.4em 0}summary:focus-visible,a:focus-visible,.num:focus-visible,audio:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.chip{display:inline-block;border-radius:999px;padding:1px 10px;font-size:.78rem;font-weight:700;border:1.5px solid;white-space:nowrap}
.chip.pass{color:#006b4e;border-color:#009E73;background:rgba(0,158,115,.14)}.chip.fail{color:#a33a00;border-color:#D55E00;background:rgba(213,94,0,.14)}
.chip.wait{color:var(--muted);border-color:var(--muted);border-style:dashed}.chip.dev{color:#7a5a00;border-color:#E69F00;background:rgba(230,159,0,.16)}
.chip.full{color:#005c8a;border-color:#0072B2;background:rgba(0,114,178,.14)}
@media (prefers-color-scheme:dark){.chip.pass{color:#5fe0b5}.chip.fail{color:#ff9a60}.chip.dev{color:#f3c04e}.chip.full{color:#7cc8f5}}
.num{border-bottom:1px dotted var(--muted);cursor:help}.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.92rem}th,td{padding:5px 8px;border-bottom:1px solid var(--bd);text-align:left}
table.heat{margin-top:.8em}table.heat td{text-align:center;min-width:3.2em}table.heat td.hd{color:#fff}caption{text-align:left;font-weight:600;padding:4px 0}
table.mini{font-size:.85rem;margin:.4em 0}
.dot{display:inline-block;width:.8em;height:.8em;border-radius:50%;margin-right:.35em;vertical-align:baseline}
.ring{display:inline-block;width:.8em;height:.8em;border-radius:50%;border:2.5px solid #999;margin:0 .3em 0 .8em;vertical-align:baseline}
.sw.diamond{display:inline-block;width:.7em;height:.7em;background:#000;transform:rotate(45deg);margin:0 .5em 0 0}
@media (prefers-color-scheme:dark){.sw.diamond{background:#fff}}
.ex h4 small{font-weight:400}.verdict{margin:.2em 0 .5em}
blockquote{margin:.6em 0;padding:.2em 14px;border-left:4px solid var(--bd);color:var(--muted);white-space:pre-wrap;font-size:.9rem}
.pic{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch;margin:.8em 0}.pic .step{flex:1 1 150px;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px;text-align:center}
.pic .arrow{align-self:center;font-size:1.6rem;color:var(--muted)}.pic svg{width:56px;height:40px}
dl dt{font-weight:700;margin-top:.7em}dl dd{margin:0 0 0 0}
audio{width:100%;max-width:360px}.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:8px;margin:.4em 0}
.cols3{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px}
.pc{font-size:.9rem}.pc ul{padding-left:1.1em;margin:.2em 0}
footer{padding:30px 16px 60px;color:var(--muted);font-size:.9rem}
@media (max-width:560px){body{font-size:15px}h1{font-size:1.5rem}header,main,footer{padding-left:10px;padding-right:10px}}
@media print{details{display:block}}
"""

GLOSSARY = [
    ("Block", "One effect or amp in the signal chain, such as a distortion pedal, a delay or a speaker cabinet."),
    ("Model", "The specific make of a block, for example one particular overdrive pedal. Gate 2 asks us to name it."),
    ("Category", "The kind of block: drive, delay, reverb, comp (compressor), eq, mod (chorus, tremolo), amp or cab."),
    ("Knob / setting / parameter", "A dial on a block. We express its position as 0 (fully left) to 1 (fully right)."),
    ("DI (direct input)", "A clean recording of the guitar with no amp or effects. Every test plays the same DI through the known preset and through our guess."),
    ("Native / Stomp", "Helix Native is the software that renders sound for the search. The HX Stomp is the pedal; it verifies the result."),
    ("Render", "Playing the DI through one candidate preset and recording what comes out (about 0.3 seconds each in Native)."),
    ("Budget", "How many renders a method may spend per item. Every method gets 300."),
    ("Score (loss), mrstft_v2", "A number for how different two recordings sound. Lower means closer. 0 is identical."),
    ("Rank-1", "Whether the score rates the true preset as closer to the target than every wrong neighbour. If not, searching harder cannot help."),
    ("Rule B", "A neighbour that is inaudibly different from the truth (the two cancel to -30 dB or better) counts as a tie, not as the score failing."),
    ("Extended rule B", "Added by the human before attempt 3: a neighbour that differs from the truth only in volume, by no more than the 3 dB random volume change the test recordings carry, is also a tie. Attempts 1 and 2 keep their figures under the old rule."),
    ("Held-out / new notes", "A different passage of the same guitar recording that the search never saw. It shows whether the guess sounds right beyond the notes it was fitted on."),
    ("Settings error (MAE)", "Mean absolute error: the average gap between true and guessed knob positions, as a share of the knob's range. 0 is perfect. Guessing the middle gives about 0.2."),
    ("Baseline", "A simple method we must beat to show that the search adds value."),
    ("Random walk", "A baseline with no search intelligence: 300 tries, each a model picked at random from the 47-model test pool (the true model is always in it) with every knob set at random; each try is rendered and scored with the admitted score, and the best-scoring try is kept. The score does the choosing, so this is not a single random guess: a single guess is right 1 time in 8 for the category (12.5 %) and 1 time in 47 for the exact model (2.1 %). With 300 tries over 47 models each model is tried about 6 times, so the true model is almost always among the tries and the score usually picks it. That is why the random walk scores high here: the test pool is small. It shows what a trustworthy score achieves by luck alone."),
    ("Attempts 1, 2 and 3", "Gate 2 has been tried up to three times. Attempt 1 ran on the first test set (synthetic-v1) and missed the rule. Attempt 2 ran once on a second, sealed set (synthetic-v2) with a specialist method for each family of effects and missed one check of seven. Attempt 3 ran once on a third sealed set (synthetic-v3), with the rank-1 check amended by the human (extended rule B). The sets differ, so the attempts are a before-and-after, not a paired test."),
    ("Specialist", "A search method built for one family of effects (gain, linear, time-based, modulation, dynamics) that measures what matters for that family and seeds or refines the search accordingly."),
    ("Known-input case", "Gate 2 gives the search the same clean guitar recording that made the target, so only the preset differs. Real songs give no such recording."),
    ("Dev vs test", "On the first test set, dev items (the first 16, i000 to i015) were used to tune the method; only the full 96, including the other 80, could count as evidence. The second test set (attempt 2) had no tuning items: all 96 are untuned."),
    ("Fingerprint", "A short list of settings each model is tried at, chosen so different models sound different from each other."),
    ("Seeding", "Measuring the target first (echo time, reverb length, tremolo speed) and starting the search near those values."),
    ("CMA-ES", "A search method that tries a cloud of guesses, keeps the best and moves the cloud toward them. It needs no gradients."),
    ("Successive halving", "Give every candidate a few renders, drop the worse half, give the rest more, and repeat."),
    ("Confidence interval (CI)", "The range the true value likely sits in, given only 96 items. A wide interval means be cautious."),
    ("LUFS", "A loudness unit. Audio on this page is matched to -23 LUFS so that louder does not sound better."),
    ("Ledger", "ledger/runs.csv: the project's record of every measured claim. A claim with no row did not happen."),
]


def glossary_html() -> str:
    return "<dl>" + "".join(f"<dt>{esc(a)}</dt><dd>{esc(b)}</dd>" for a, b in GLOSSARY) + "</dl>"


def pic_step(svg: str, title: str, text: str) -> str:
    return f'<div class="step">{svg}<br><strong>{esc(title)}</strong><br><span class="muted">{esc(text)}</span></div>'


def flow_picture() -> str:
    ic = lambda p: (f'<svg viewBox="0 0 56 40" aria-hidden="true" fill="none" stroke="currentColor" '
                    f'stroke-width="2.5">{p}</svg>')
    guitar = ic('<path d="M4 30 L26 14 M26 14 l8 -8 l6 6 l-8 8 M14 24 a7 7 0 1 0 10 10 z"/>')
    knobs = ic('<circle cx="14" cy="20" r="9"/><path d="M14 20 L18 12"/><circle cx="40" cy="20" r="9"/><path d="M40 20 L34 14"/>')
    ear = ic('<path d="M8 20 h8 l10 -10 v20 l-10 -10 M34 12 q8 8 0 16 M40 7 q13 13 0 26"/>')
    ques = ic('<circle cx="28" cy="20" r="14"/><path d="M22 16 q6 -8 12 0 q0 4 -6 6 v3 M28 30 v1"/>')
    cmp_ = ic('<path d="M8 28 L20 12 L32 24 L48 8"/><path d="M8 34 H48" stroke-dasharray="4 4"/>')
    arrow = '<div class="arrow" aria-hidden="true">&rarr;</div>'
    return ('<div class="pic">' + pic_step(guitar, "1. Clean guitar", "One recording with no effects (the DI).") + arrow
            + pic_step(knobs, "2. Known preset", "A single block with settings we chose. This is the answer key.") + arrow
            + pic_step(ear, "3. Target sound", "The DI played through the known preset.") + arrow
            + pic_step(ques, "4. Our search listens", "It sees only the sound, tries 300 candidates, guesses block and settings.") + arrow
            + pic_step(cmp_, "5. Compare", "Right kind? Right model? Knobs close? Same on new notes?") + "</div>")


def build_model(root: Path, attempt: str = "v1") -> dict:
    d = discover(root, attempt)
    ledger = load_ledger(root)
    methods = d["methods"]
    total = len(d["all_ids"])
    kinds = {m["kind"]: m for m in methods if m["kind"] in ("middle", "random")}
    full = next((m for m in methods if m["role"] == "full"), None)
    dev = next((m for m in methods if m["role"] == "hybrid"), None)
    return {"d": d, "ledger": ledger, "methods": methods, "total": total, "middle": kinds.get("middle"),
            "random": kinds.get("random"), "full": full, "dev": dev, "attempt": attempt,
            "set": ATTEMPTS[attempt]["set"]}


def section_headline(M: dict) -> str:
    if M["attempt"] == "v3":
        return section_headline_v3(M)
    if M["attempt"] == "v2":
        return section_headline_v2(M)
    d = M["d"]
    rnd = M["random"]
    full, dev = M["full"], M["dev"]
    complete = bool(full and full["scope"] == "full")
    ours = full if complete else dev
    if not ours or not rnd:
        return ("<p class='lede'>The designated runs are not all in yet "
                f"(<code>{esc(DESIGNATED['dev'])}</code>, <code>{esc(DESIGNATED['full'])}</code>), so there is nothing to compare.</p>")
    ids = d["all_ids"] if complete else d["dev_ids"]
    mb = metrics("ours", ours["items"], ids)
    mr = metrics("random", rnd["items"], ids)
    mm = metrics("middle", M["middle"]["items"], ids) if M["middle"] else {}
    mid_txt = f" and the middle-of-every-knob baseline for {mm['model_hits']} of {mm['n']}" if mm.get("n") else ""
    if complete:
        return (f"<p class='lede'><strong>In one sentence.</strong> On all {mb['n']} items, the designated full run "
                f"(<code>{esc(ours['tag'])}</code>) names the exact model for {mb['model_hits']} of {mb['n']}, random walk "
                f"for {mr['model_hits']} of {mr['n']}{mid_txt}. Whether that passes Gate 2 is decided by the frozen rule below "
                "and needs the human's sign-off.</p>")
    state = (f"The full 96-item gate run (<code>{esc(DESIGNATED['full'])}</code>) is "
             + (f"in progress: {esc(full['scope'])}" if full else "not started") + ", so Gate 2 is <strong>not decided</strong>.")
    return (f"<p class='lede'><strong>In one sentence.</strong> {chip('dev', 'dev16: not evidence')} On the 16 tuning items, "
            f"our chosen dev method (<code>{esc(ours['tag'])}</code>, picked by the rule written down before it ran) names the exact "
            f"model for {mb['model_hits']} of {mb['n']} items, random walk for {mr['model_hits']} of {mr['n']}{mid_txt}. "
            f"{state} Those 16 items were used to tune the method, so this is a progress check and not the Gate 2 result.</p>")


def section_headline_v2(M: dict) -> str:
    full = M["full"]
    if not (full and full["scope"] == "full" and M["random"]):
        return "<p class='lede'>Attempt 2 (<code>gate2_v2_attempt2</code>) has not finished all 96 items, so nothing is decided.</p>"
    ev = evaluate_pass(full["items"], M["random"]["items"], M["total"], M["d"]["all_ids"])
    n_ok, n_all = count_checks(ev)
    mo = metrics("ours", full["items"], M["d"]["all_ids"])
    word = "met all the checks (needs human sign-off)" if ev["all_ok"] else f"did <strong>not pass</strong> ({n_ok} of {n_all} checks met)"
    return (f"<p class='lede'><strong>In one sentence.</strong> {chip('full', 'attempt 2, full 96, untuned')} On a fresh sealed set, the "
            f"search with a specialist for each kind of effect named the exact model for {mo['model_hits']} of {mo['n']} items and "
            f"{word}: the frozen rule's rank-1 check on the judging score fell short. "
            "Attempt 1 is kept lower down as the earlier attempt. <a href='#attempt2'>Jump to the result</a>.</p>")


# --------------------------------------------------------------------------- tuning history and families

def tuning_history(M: dict) -> str:
    """Every T114 dev16 iteration, with its eligibility. None of them is the headline except the chosen one."""
    d = M["d"]
    rows = []
    for (tag, name, elig) in TUNING_HISTORY:
        m = next((x for x in d["tuning"] if x["tag"] == tag), None)
        if m is None:
            rows.append(f"<tr><td>{esc(name)}<br><small><code>{esc(tag)}</code></small></td><td colspan='4'>run not found</td>"
                        f"<td>{esc(elig)}</td></tr>")
            continue
        mm = metrics("ours", m["items"], d["dev_ids"])
        src = src_title(f"cache/synthetic-v1/runs/{tag}", "dev16, i000 to i015")
        chosen = tag == DESIGNATED["dev"]
        chip_html = chip("pass" if chosen else ("fail" if elig.startswith("ineligible") else "dev"), elig)
        t_model = f"{mm['model_hits']}/{mm['n']}"
        t_cat = f"{mm['category_hits']}/{mm['n']}"
        rows.append(f"<tr><td>{esc(name)}<br><small><code>{esc(tag)}</code></small></td>"
                    f"<td>{num(t_model, src)}</td><td>{num(t_cat, src)}</td>"
                    f"<td>{num(fmt(mm.get('heldout_median'), 3), src)}</td><td>{num(fmt(mm.get('mae'), 3), src)}</td>"
                    f"<td>{chip_html}</td></tr>")
    return ("<h3>Tuning history (dev16, not evidence)</h3>"
            "<p>These are the three design iterations of the hybrid search. The rule for the final choice was written in "
            "<code>docs/DECISIONS.md</code> before iteration 3 ran, so the chosen method was not picked by looking at the best "
            "number. A run that went over the 300-render budget cannot be chosen, however good its score. Only the chosen one "
            "is used for the headline, the examples and the charts.</p>"
            "<div class='scroll'><table><thead><tr><th>Iteration</th><th>Right model</th><th>Right category</th>"
            "<th>Held-out difference (median)</th><th>Settings error</th><th>Eligibility</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def family_metrics(kind: str, items: dict, ids) -> dict:
    out = {}
    for key, _, cats in FAMILIES:
        sub = [i for i in ids if i in items and items[i].get("category") in cats]
        out[key] = metrics(kind, items, sub)
    return out


def svg_family_bars(groups: list[dict], vmax: float, title: str, axis_label: str, nd: int = 2) -> str:
    """Grouped horizontal bars: one group per family, one bar per method."""
    w, left, right, bh, top = 640, 8, 60, 13, 12
    plot = w - left - right
    gh = lambda g: 18 + bh * len(g["bars"]) + 10
    h = top + sum(gh(g) for g in groups) + 46
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}" class="chart"><title>{esc(title)}</title>']
    for t in range(6):
        x = left + plot * t / 5
        out.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{h - 36}" class="grid"/>')
        out.append(f'<text x="{x:.1f}" y="{h - 22}" class="tick" text-anchor="middle">{vmax * t / 5:.2g}</text>')
    out.append(f'<text x="{left + plot / 2:.1f}" y="{h - 6}" class="axis" text-anchor="middle">{esc(axis_label)}</text>')
    y = top
    for g in groups:
        flag = " (next specialist to build)" if g.get("weak") else ""
        out.append(f'<text x="{left}" y="{y + 12}" class="lbl{" weak" if g.get("weak") else ""}">{esc(g["label"])}'
                   f'<tspan class="sub"> {esc(g.get("sub", ""))}</tspan>'
                   f'<tspan class="flag">{esc(flag)}</tspan></text>')
        for k, b in enumerate(g["bars"]):
            by = y + 18 + k * bh
            out.append(f"<g><title>{esc(b.get('tip', ''))}</title>")
            v = b.get("value")
            if v is None or not finite(v):
                out.append(f'<rect x="{left}" y="{by}" width="{plot}" height="{bh - 3}" class="wait" rx="2"/>')
                out.append(f'<text x="{left + 6}" y="{by + bh - 4}" class="val">no data</text>')
            else:
                bw = max(1.5, plot * min(max(v, 0), vmax) / vmax)
                out.append(f'<rect x="{left}" y="{by}" width="{bw:.1f}" height="{bh - 3}" fill="{b["color"]}" rx="2"/>')
                out.append(f'<text x="{left + bw + 5:.1f}" y="{by + bh - 4}" class="val">{v:.{nd}f}</text>')
            out.append("</g>")
        y += gh(g)
    out.append("</svg>")
    return "".join(out)


def weakest_families(fam_ours: dict, k: int = 2) -> list[str]:
    """The k families where our designated method is weakest: lowest right-model share, then right-category share."""
    scored = [(fm["model"], fm["category"], key) for key, fm in fam_ours.items() if fm.get("n")]
    scored.sort()
    return [key for _, _, key in scored[:k]]


def family_view(M: dict) -> dict:
    """What the family charts show: the full run when it is complete, otherwise the chosen dev method on dev16."""
    d = M["d"]
    full, dev = M["full"], M["dev"]
    complete = bool(full and full["scope"] == "full")
    ours = full if complete else dev
    ids = d["all_ids"] if complete else d["dev_ids"]
    series = []
    for m in (M["middle"], M["random"], ours):
        if m is not None:
            series.append((m, family_metrics(m["kind"], m["items"], ids)))
    fam_ours = next((fm for m, fm in series if m is ours), {}) if ours else {}
    return {"complete": complete, "ours": ours, "ids": ids, "series": series,
            "weak": weakest_families(fam_ours) if fam_ours else []}


FAMILY_CARDS = {
    "gain": {
        "does": "Pushes the signal into a non-linear curve, so loud peaks are squashed and new overtones appear.",
        "uses": "Overtone (harmonic) levels, how much the sound saturates, and a gain index; the general spectrogram score as a cross-check.",
        "search": "A broad screen across the drive, fuzz, amp and preamp models, then refine the best few; later a learned embedding to shortlist models.",
        "evidence": [("Right-model share in this family, from the chart above and the run files", "cache/synthetic-v1/runs/"),
                     ("Plan: DECISIONS 2026-10-17, a specialist per effect family", "docs/DECISIONS.md")],
    },
    "linear": {
        "does": "Reshapes the tone by frequency only (a speaker cabinet or equaliser), with no distortion, so the effect is one fixed response.",
        "uses": "The response itself, computed by deconvolution of the output against the known DI (coherence near 1 means it is trustworthy).",
        "search": "Compute the response directly and look up the nearest cab or EQ; little or no search.",
        "evidence": [("T106: Helix cab found from a DI recording at rank 1 in 22 of 24; coherence 0.9997", "docs/notes/ir-library.md"),
                     ("T106 task row", "board/TASKS.md")],
    },
    "dynamics": {
        "does": "Turns loud parts down (or quiet parts off) over time, so the volume curve changes while the tone colour mostly stays.",
        "uses": "The loudness envelope: threshold, ratio, attack and release measured from the target against the DI.",
        "search": "Seed threshold, ratio and timing from the measured envelope, then a narrow search around them.",
        "evidence": [("Compressor items in the chart above (only the comp category is in the synthetic set)", "cache/synthetic-v1/runs/"),
                     ("Plan: DECISIONS 2026-10-17", "docs/DECISIONS.md")],
    },
    "time": {
        "does": "Adds delayed copies of the sound (echoes) or a dense wash of them (reverb), so the sound lingers after the note.",
        "uses": "Echo time, feedback and mix measured against the DI; reverb decay and pre-delay.",
        "search": "Measured seeding sets those knobs before the search, which then works in a narrow band around them (T113).",
        "evidence": [("T113: dev16 model accuracy 6/16 to 10/16 with measured seeding; reverb 2/2; delay 1/2",
                      "board/TASKS.md (T113), docs/notes/measured-seed.md")],
    },
    "mod": {
        "does": "Wobbles the sound with a slow oscillator (an LFO): pitch, delay time or volume sweeps back and forth.",
        "uses": "LFO rate and depth, with a score that ignores where in its cycle the oscillator started (phase-invariant), because a free-running LFO makes ordinary null-style scores fail.",
        "search": "Measure rate and depth from the target, then search the model and mix with the phase-invariant score.",
        "evidence": [("T066b: sine-wave probes through chorus, flanger, phaser and vibrato on Native and Stomp", "board/TASKS.md (T066b)"),
                     ("Modulation items in the measured-seeding dev16 run: 2/2 right", "docs/notes/measured-seed.md")],
    },
}


def spec_compare(M: dict, fam: str, view: dict) -> str:
    """Specialist runs for a family against the general method, on the same items and the same budget."""
    runs = M["d"]["spec"].get(fam, [])
    if not runs and M["attempt"] in LATER:
        return ("<p class='muted'>No separate per-family run. The specialists ran together inside attempt 2, so the "
                "result-by-family table above is the evidence.</p>")
    if not runs:
        return "<p class='muted'>No specialist runs yet. When a run tagged <code>spec_" + fam + "</code> appears it is compared here automatically.</p>"
    cats = next(c for k, _, c in FAMILIES if k == fam)
    gen = view["ours"]
    rnd = M["random"]
    rows = []
    for sm in runs:
        ids = [i for i in sm["items"] if i in (gen["items"] if gen else {}) and sm["items"][i].get("category") in cats]
        if not ids:
            rows.append(f"<tr><td><code>{esc(sm['tag'])}</code></td><td colspan='5'>no items in common with the general method yet</td></tr>")
            continue
        ms = metrics("ours", sm["items"], ids)
        mg = metrics("ours", gen["items"], ids)
        mr = metrics("random", rnd["items"], ids) if rnd else {}
        same_budget = sm.get("budget") == gen.get("budget")
        beats = ms["model_hits"] > mg["model_hits"] or (ms["model_hits"] == mg["model_hits"] and (ms.get("mae") or 9) < (mg.get("mae") or 9))
        verdict_html = (chip("pass", "beats the general method on these items") if beats and same_budget
                        else chip("wait", "budget differs: not comparable") if not same_budget
                        else chip("fail", "does not beat the general method"))
        src = src_title(f"cache/synthetic-v1/runs/{sm['tag']}", f"{len(ids)} {'/'.join(cats)} items in common")
        t_s, t_g = f"{ms['model_hits']}/{ms['n']}", f"{mg['model_hits']}/{mg['n']}"
        t_r = num(f"{mr['model_hits']}/{mr['n']}", src) if mr.get("n") else "-"
        rows.append(f"<tr><td><code>{esc(sm['tag'])}</code></td><td>{len(ids)}</td>"
                    f"<td>{num(t_s, src)}</td><td>{num(t_g, src)}</td><td>{t_r}</td><td>{verdict_html}</td></tr>")
    return ("<div class='scroll'><table class='mini'><thead><tr><th>Specialist run</th><th>Items</th><th>Specialist right model</th>"
            "<th>General method</th><th>Random walk</th><th>Verdict</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            "<p class='muted'>Adoption also needs the paired comparison against the general method and random walk "
            "(see the rule above); this table is the first look.</p>")


def family_attempt_rows(M: dict) -> dict | None:
    """Per-family result of an attempt's full run: {family: {"n", "model_hits", "category_hits", "rnd_model", "mid_model"}}."""
    full = M.get("full") if M else None
    if not full or full["scope"] != "full":
        return None
    ids = M["d"]["all_ids"]
    out = {}
    fo = family_metrics("ours", full["items"], ids)
    fr = family_metrics("random", M["random"]["items"], ids) if M["random"] else {}
    fm = family_metrics("middle", M["middle"]["items"], ids) if M["middle"] else {}
    for key, _, _ in FAMILIES:
        e = fo[key]
        out[key] = {"n": e.get("n", 0), "model_hits": e.get("model_hits", 0), "category_hits": e.get("category_hits", 0),
                    "rnd_model": fr.get(key, {}).get("model_hits"), "mid_model": fm.get(key, {}).get("model_hits")}
    return out


def family_result_table(M2: dict, M1: dict | None) -> str:
    """Attempt 2 per family (headline), with attempt 1 beside it for the same families."""
    a2 = family_attempt_rows(M2)
    a1 = family_attempt_rows(M1) if M1 else None
    if not a2:
        return ""
    rows = []
    for key, name, cats in FAMILIES:
        e2 = a2[key]
        e1 = a1[key] if a1 else None
        cell1 = f"{e1['model_hits']}/{e1['n']}, {e1['category_hits']}/{e1['n']}" if e1 and e1["n"] else "-"
        rows.append(f"<tr><th scope='row'>{esc(name)} <small>({esc(', '.join(cats))})</small></th>"
                    f"<td>{esc(SPECIALIST_STATUS.get(key, 'planned'))}: {esc(SPECIALIST_NOTE.get(key, ''))}</td>"
                    f"<td><strong>{e2['model_hits']}/{e2['n']}</strong></td><td><strong>{e2['category_hits']}/{e2['n']}</strong></td>"
                    f"<td>{e2['rnd_model']}/{e2['n']}</td><td>{e2['mid_model']}/{e2['n']}</td><td>{cell1}</td></tr>")
    fell = [f"{name} ({a1[key]['model_hits']} to {a2[key]['model_hits']} right models)" for key, name, _ in FAMILIES
            if a1 and a1[key]["n"] and a2[key]["model_hits"] < a1[key]["model_hits"]]
    rose = [f"{name} ({a1[key]['model_hits']} to {a2[key]['model_hits']})" for key, name, _ in FAMILIES
            if a1 and a1[key]["n"] and a2[key]["model_hits"] > a1[key]["model_hits"]]
    trend = ("<p><strong>Plainly.</strong> "
             + (f"Right-model counts rose for {esc('; '.join(rose))}. " if rose else "")
             + (f"They <strong>fell</strong> for {esc('; '.join(fell))}. " if fell else "")
             + "The per-category table above shows which categories moved. "
               "The comparison is across different test sets, so it is a direction, not proof.</p>") if a1 else ""
    return ("<h3>Result by family: attempt 2, the run with specialists switched on</h3>"
            "<p>" + chip("full", "attempt 2, all 96 untuned items") + " Each cell counts items. Attempt 1 is beside it (its "
            "specialists were off, apart from the time-based seeding that was already part of the general method). "
            "These are different test sets, so read the change as a direction, not a paired result.</p>"
            "<div class='scroll'><table><thead><tr><th>Family</th><th>Specialist in attempt 2</th><th>Right model</th>"
            "<th>Right kind</th><th>Random walk, right model</th><th>Middle, right model</th>"
            "<th>Attempt 1: right model, right kind</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>" + trend)


def section_specialists(M: dict, M1: dict | None = None) -> str:
    view = family_view(M)
    scope_name = "all 96 items" if view["complete"] else "dev16 (16 tuning items)"
    parts = ["<h2 id='specialists'>3a. A specialist for each kind of effect</h2>",
             "<p class='lede'><strong>In plain English.</strong> Different effects work in different ways, so one method for "
             "everything is not the best plan. A distortion bends the shape of the sound, a speaker cabinet only reshapes its "
             "tone, an echo repeats it later, a chorus wobbles it. So each family of effects gets its own method (a "
             "<em>specialist</em>) that measures the thing that matters for that family and searches in the way that suits it. "
             "The human asked for this on 2026-10-17 (<code>docs/DECISIONS.md</code>, &ldquo;a specialist per effect family&rdquo;).</p>",
             "<h3>The adoption rule</h3><p>A specialist is tuned on the dev items only. It is adopted for a family only if it "
             "beats the general method on that family at the same budget of 300 renders. Until then the general method stays in "
             "charge of that family. "
             + ("For Gate 2 attempt 2 the lead switched on the specialists for gain, linear, time-based and modulation "
                "(modulation: seeds and screen only) in one combined run. That run is judged by the Gate 2 rule as a whole; "
                "it is not the per-family adoption test, so the family table below is a first look, not an adoption.</p>"
                if M["attempt"] in LATER else "Nothing is adopted yet.</p>")]
    # chart
    series = view["series"]
    if not view["ours"]:
        parts.append("<p>" + chip("wait", "waiting for the designated run") + " No results by family yet.</p>")
    else:
        parts.append(family_result_table(M, M1))
        parts.append(f"<h3>How each method does, by family</h3><p>{scope_chip('full' if view['complete'] else 'dev16')} "
                     f"Scope: {esc(scope_name)}. Families marked <strong>next specialist to build</strong> are the two where our "
                     "designated method is weakest (lowest share of right models, then right categories). "
                     + ("" if view["complete"] else
                        "On dev16 a family has only two to four items, so one item moves a bar a lot; treat the order as a hint.")
                     + "</p>")
        legend = "".join(f"<span><span class='dot' style='background:{m['color']}'></span>{esc(m['label'])}</span> &nbsp; " for m, _ in series)
        parts.append(f"<p class='legend'>{legend}</p>")
        charts = [("Picked the exact model (higher is better)", "model", 1.0, "share of items"),
                  ("Picked the right kind of effect (higher is better)", "category", 1.0, "share of items"),
                  ("Settings error when the model is right (lower is better)", "mae", 0.5, "average gap, share of the knob's range")]
        weak = set(view["weak"])
        for title, key, vmax, axis in charts:
            groups = []
            for fkey, fname, _ in FAMILIES:
                n_f = next((fm[fkey]["n"] for m, fm in series if m is view["ours"]), 0)
                bars = []
                for m, fm in series:
                    e = fm[fkey]
                    v = e.get(key) if e.get("n") else None
                    bars.append({"color": m["color"], "value": v,
                                 "tip": f"{m['label']}, {fname} family: {fmt(v, 3)} over {e.get('n', 0)} items. "
                                        f"Source: run files under cache/{M['set']}/runs/ ({scope_name})"})
                groups.append({"label": fname, "sub": f"({n_f} items)", "bars": bars, "weak": fkey in weak})
            parts.append(f"<figure><figcaption>{esc(title)}</figcaption>"
                         f"{svg_family_bars(groups, vmax, title + ', by family', axis)}</figure>")
        # table equivalent for screen readers and precision
        trs = []
        for fkey, fname, cats in FAMILIES:
            cells = []
            for m, fm in series:
                e = fm[fkey]
                cells.append(f"<td>{e['model_hits']}/{e['n']}</td>" if e.get("n") else "<td>-</td>")
            mark = chip("dev", "next specialist to build") if fkey in weak else ""
            trs.append(f"<tr><th scope='row'>{esc(fname)} <small>({esc(', '.join(cats))})</small> {mark}</th>{''.join(cells)}</tr>")
        parts.append("<details><summary>The same numbers as a table (right model)</summary><div class='scroll'><table><thead><tr><th>Family</th>"
                     + "".join(f"<th>{esc(m['label'])}</th>" for m, _ in series) + f"</tr></thead><tbody>{''.join(trs)}</tbody></table></div></details>")
    # cards
    parts.append("<h3>One card per family</h3><div class='cols3'>")
    weak = set(view["weak"])
    for fkey, fname, cats in FAMILIES:
        c = FAMILY_CARDS[fkey]
        status = SPECIALIST_STATUS.get(fkey, "planned")
        kind = {"adopted": "pass", "in use": "pass", "building": "dev"}.get(status, "wait")
        ev = "".join(f"<li>{esc(t)} <small class='muted'>(source: <code>{esc(s_.replace('synthetic-v1', M['set']))}</code>)</small></li>"
                     for t, s_ in c["evidence"])
        if view["ours"]:
            rows = " ; ".join(esc(m["label"].split(" (")[0]) + f": {fm[fkey]['model_hits']}/{fm[fkey]['n']}"
                              for m, fm in view["series"] if fm[fkey].get("n"))
            ev += (f"<li>Right model on {esc(scope_name)}: {rows or 'no items'} "
                   f"<small class='muted'>(source: run files)</small></li>")
        nxt = "<p>" + chip("dev", "next specialist to build") + "</p>" if fkey in weak else ""
        note = SPECIALIST_NOTE.get(fkey, "") if M["attempt"] in LATER else ""
        status_txt = f"{status} ({note})" if note else status
        extra = {"gain": "; also fuzz and preamp in the real block list", "dynamics": "; also gate in the real block list"}.get(fkey, "")
        cat_txt = ", ".join(cats)
        parts.append(f"<article class='card pc'><h4>{esc(fname)} <small>(synthetic set: {esc(cat_txt)}{esc(extra)})</small></h4>"
                     f"<p>Status: {chip(kind, status_txt)}</p>{nxt}"
                     f"<p><strong>What it does.</strong> {esc(c['does'])}</p>"
                     f"<p><strong>What we measure.</strong> {esc(c['uses'])}</p>"
                     f"<p><strong>Search strategy.</strong> {esc(c['search'])}</p>"
                     f"<strong>Evidence so far</strong><ul>{ev}</ul>"
                     f"<strong>Specialist runs against the general method</strong>{spec_compare(M, fkey, view)}</article>")
    parts.append("</div>")
    return "".join(parts)



# --------------------------------------------------------------------------- attempt 2 (headline) and the comparison

KNOWN_DI_NOTE = ("Gate 2 tests the known-input case: the search is given the very same clean guitar recording (the DI) that made "
                 "the target, so the only thing that differs is the preset. Later gates do not give it that. Gate 3 (blind single block) gives the matcher only the rendered output, never the item's own dry sample; "
                 "Gate 4 (real pedals) supplies only a separate dry sample with different notes, and Gate 5 (real songs) has no clean guitar at all, only a guitar "
                 "separated from the mix. So even a Gate 2 pass would not show that a song can be matched.")


KNOWN_DI_LEDE = f"<p class='lede'><strong>Known-input case.</strong> {esc(KNOWN_DI_NOTE)}</p>"


def rank1_misses(M: dict) -> list[dict]:
    """Items of the full run whose true preset the score did not rank first (rule B), with what the middle baseline did."""
    full, mid = M["full"], M["middle"]
    out = []
    for i in sorted(full["items"]):
        it = full["items"][i]
        if it.get("rank1_audible") is False:
            out.append({"id": i, "category": it.get("category"), "model": it.get("model"), "model_ok": bool(it.get("model_ok")),
                        "neighbour_rank": it.get("neighbour_rank_audible"),
                        "middle_miss": (mid or {}).get("rank1_raw", {}).get(i) is False})
    return out


def texts_failed(ev: dict) -> list[str]:
    return [c["text"] if c["key"] != "rank1" else "rank1" for c in ev["checks"] if c["ok"] is False]


def rank1_rate_raw(m: dict | None) -> float | None:
    vals = list((m or {}).get("rank1_raw", {}).values())
    return float(np.mean(vals)) if vals else None


def section_attempt2(M2: dict, M1: dict | None, rule_text: str, ev2: dict, latest: bool = True) -> str:
    d = M2["d"]
    all_ids = d["all_ids"]
    full, rnd, mid = M2["full"], M2["random"], M2["middle"]
    base = [m for m in (mid, rnd) if m]
    n_ok, n_all = count_checks(ev2)
    verdict_txt = ("not decided" if not ev2["complete"] else "criteria all met, needs human sign-off" if ev2["all_ok"]
                   else f"NOT PASSED, {n_ok} of {n_all} checks met")
    parts = [f"<h2 id='attempt2'>Gate 2 attempt 2 ({'latest' if latest else 'earlier attempt'}): {esc(verdict_txt)}</h2>"]
    mo, mr = metrics("ours", full["items"], all_ids), metrics("random", rnd["items"], all_ids)
    mm = metrics("middle", mid["items"], all_ids) if mid else {}
    by = {c["key"]: c for c in ev2["checks"]}
    miss = rank1_misses(M2)
    r1_ours, r1_mid = mo.get("rank1"), rank1_rate_raw(mid)
    parts.append(
        "<p class='lede'><strong>In plain English.</strong> "
        + chip("fail" if ev2["complete"] and not ev2["all_ok"] else "wait", verdict_txt)
        + f" Attempt 2 ran our search on a fresh, sealed set of 96 known presets (<code>{esc(M2['set'])}</code>), run once with "
        f"the pass rule already frozen. It named the exact model for {mo['model_hits']} of {mo['n']} (random walk {mr['model_hits']}, "
        f"middle-of-every-knob {mm.get('model_hits', 'n/a')})"
        + (", and its model accuracy is not worse than the random walk's, which attempt 1 could not show. "
           if by["vs_random_model"]["ok"] else ". ")
        + f"{n_ok} of the {n_all} checks are met. "
        + (f"The one that is not is the <strong>rank-1 check</strong>: the judging score put the true "
           f"preset first on {fmt(r1_ours, 4)} of the items ({len(all_ids) - len(miss)} of {len(all_ids)}) against a bar of 0.95. "
           if texts_failed(ev2) == ["rank1"] else
           f"Not met: {esc('; '.join(texts_failed(ev2)))}. ")
        + ("The rule is unchanged and nothing was re-scored. Whether to try again is the human's decision.</p>" if latest else
           "The rule was unchanged and nothing was re-scored. This figure stays under the old rank-1 rule (see the attempt 3 section). "
           "Attempt 3 followed on a fresh set.</p>"))
    parts.append("<h3>The seven checks</h3>")
    parts.append(pass_table(ev2, None, M2, v2=True))
    parts.append("<h3>The frozen rule, word for word</h3><p>Quoted from <code>docs/DECISIONS.md</code>; it was written before "
                 "any full run and has not been changed. Its text names synthetic-v1 because it was written for attempt 1; attempt 2 "
                 "applied it unchanged to synthetic-v2 (DECISIONS, &ldquo;Gate 2 attempt 2 configuration&rdquo;). Its fifth point, about the "
                 "80 items outside dev16, has no meaning on v2:</p>"
                 f"<blockquote>{esc(rule_text) if rule_text else 'The frozen-rule section was not found in docs/DECISIONS.md.'}</blockquote>")
    # rank-1 miss
    parts.append("<h3>The one missed check: rank-1 is a check on the score, not on the search</h3>")
    parts.append(
        "<p><strong>What rank-1 asks.</strong> We hand the score the true preset and a set of close neighbours (nearby settings and "
        "models) and ask it which one sounds closest to the target. If it does not pick the true preset, the score is "
        "a poor judge in that spot, and no amount of searching can fix a poor judge.</p>"
        "<p><strong>Why it is the score, not the search.</strong> The rank-1 test never uses the search's answer. The "
        f"middle-of-every-knob baseline, which does no searching at all, gets exactly the same figure: "
        f"{num(fmt(r1_mid, 4), 'v2_base_middle item files, rank1_audible before the report strips it from baselines')} "
        f"against ours {num(fmt(r1_ours, 4), 'gate2_v2_attempt2 item files, rank1_audible')}. "
        "That is why the baseline columns elsewhere on this page show no rank-1 figure.</p>")
    if miss:
        rows = []
        for e in miss:
            rows.append(f"<tr><td><code>{esc(e['id'])}</code></td><td>{esc(e['category'])}</td><td>{esc(pretty_model(e['model']))}</td>"
                        f"<td>{'yes' if e['model_ok'] else 'no'}</td><td>{esc(e['neighbour_rank'])}</td>"
                        f"<td>{'yes' if e['middle_miss'] else 'no'}</td></tr>")
        found = sum(1 for e in miss if e["model_ok"])
        same = sum(1 for e in miss if e["middle_miss"])
        gain_n = sum(1 for e in miss if FAMILY_OF.get(e["category"]) == "gain")
        parts.append(
            f"<p><strong>The {len(miss)} items.</strong> The search still found the right model on {found} of the {len(miss)}, and the "
            f"middle baseline's score also failed on {same} of the {len(miss)}. The pattern is gain-stage neighbours: {gain_n} of the {len(miss)} "
            "are drive or amp, where a nearby gain or drive setting scores as well as the truth, so the score cannot tell them apart. "
            f"The rest: {esc(', '.join(e['category'] for e in miss if FAMILY_OF.get(e['category']) != 'gain'))}.</p>"
            "<div class='scroll'><table class='mini'><thead><tr><th>Item</th><th>Kind</th><th>True model</th>"
            "<th>Search found the right model?</th><th>Where the true preset ranked (1 = first)</th>"
            "<th>Middle baseline's score also missed?</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")
    parts.append("<details><summary>Technical detail</summary><p>Rank-1 under rule B, known-DI mode (<code>rank1_mode</code> "
                 "<code>known_di_same_notes_aligned</code>): the true preset against its neighbours on the same notes, "
                 "<code>mrstft_v2</code> alone at weight 1.0; a neighbour that nulls at -30 dB or better against the truth is a tie. "
                 "Source: <code>rank1_audible</code> in each item file; ledger rows 2026-10-18T02:33:52 (benchmark); "
                 "docs/DECISIONS.md, &ldquo;Gate 2 attempt 2 result&rdquo;.</p></details>")
    # all 96
    parts.append("<h3>All 96 items, next to both baselines</h3><p>" + chip("full", "full 96, never tuned on")
                 + " Same 96 items for every bar. The dashed line is the Gate 2 target. Every item was untuned: attempt 2 "
                   "had no tuning items.</p>")
    parts.append(results_panel(base + [full], all_ids, "all 96", set_name=M2["set"]))
    rows96 = [{"label": m["label"], "metrics": metrics(m["kind"], m["items"], all_ids)} for m in base + [full]]
    parts.append("<h3>By category, grouped by family of effect</h3><p>Each cell is how many of that category's 12 items were right. Darker is better. "
                 "Drive and modulation are the weak spots.</p>")
    parts.append(heatmap(rows96, "model", "Attempt 2: exact model (twelve items per category)"))
    parts.append(heatmap(rows96, "category", "Attempt 2: right kind of effect (twelve items per category)"))
    parts.append("<h3>What this does and does not show</h3><ul>"
                 f"<li>{esc(KNOWN_DI_NOTE)}</li>"
                 "<li><strong>Split-80 means nothing here.</strong> There was no dev16 tuning on v2, so the 80-versus-96 check has "
                 "nothing to catch. It is not evidence in either direction.</li>"
                 + ("<li><strong>v2 is now seen.</strong> Attempt 3 needs a fresh sealed set (synthetic-v3, avoiding v1, v2 and devfam). "
                    "Nothing on v2 can be re-scored to pass.</li>" if latest else
                    "<li><strong>v2 is spent.</strong> Attempt 3 ran on a fresh sealed set, synthetic-v3. Nothing on v2 can be re-scored to pass.</li>")
                 + 
                 "<li><strong>Ear not yet consulted.</strong> No listening session has been scored; the audio below is for you to "
                 "listen to.</li></ul>")
    return "".join(parts)


# --------------------------------------------------------------------------- attempt 3 (headline)

RULE_B_AMENDMENT = (
    "The rank-1 check asks whether our judging score prefers the true preset over close neighbours. Before attempt 3 the human "
    "amended it (DECISIONS, 2026-10-18, &ldquo;rule B extended&rdquo;). Two settings that differ only in volume used to count against "
    "the score, because the test recordings are given a random volume change of up to 3 dB and so a louder or quieter copy of the "
    "truth could look closer than the truth itself; nobody can tell such copies apart by ear. Now a neighbour counts as a tie when "
    "it cancels the truth to -30 dB or better, either as it is or after a volume match within that 3 dB range. Every other bar is "
    "unchanged. The figures for attempts 1 and 2 stay as they were recorded, under the old rule.")


def delay_rows(M: dict) -> list[dict]:
    """Delay items our full run got wrong: what the true model was and what we named instead."""
    out = []
    for i in sorted(M["full"]["items"]):
        it = M["full"]["items"][i]
        if it.get("category") == "delay" and not it.get("model_ok") and "error" not in it:
            out.append({"id": i, "model": it.get("model"), "pred": it.get("pred_model"), "pred_category": it.get("pred_category"),
                        "category_ok": bool(it.get("category_ok"))})
    return out


def section_headline_v3(M: dict) -> str:
    full = M["full"]
    if not (full and full["scope"] == "full" and M["random"]):
        return "<p class='lede'>Attempt 3 (<code>gate2_v3_attempt3</code>) has not finished all 96 items, so nothing is decided.</p>"
    ev = evaluate_pass(full["items"], M["random"]["items"], M["total"], M["d"]["all_ids"])
    n_ok, n_all = count_checks(ev)
    mo = metrics("ours", full["items"], M["d"]["all_ids"])
    if ev["all_ok"]:
        word = f"<strong>met all {n_all} checks</strong> of the frozen rule. Gate 2 is passed only when the human signs it"
    else:
        word = f"did <strong>not pass</strong> ({n_ok} of {n_all} checks met)"
    return (f"<p class='lede'><strong>In one sentence.</strong> {chip('full', 'attempt 3, full 96, untuned')} On a third, fresh sealed set, "
            f"the search named the exact model for {mo['model_hits']} of {mo['n']} items and {word}. "
            "Attempts 1 and 2 are kept lower down as earlier attempts. <a href='#attempt3'>Jump to the result</a>.</p>")


def section_attempt3(M3: dict, M2: dict | None, M1: dict | None, rule_text: str, ev3: dict,
                     ev2: dict | None, ev1: dict | None) -> str:
    d = M3["d"]
    all_ids = d["all_ids"]
    full, rnd, mid = M3["full"], M3["random"], M3["middle"]
    base = [m for m in (mid, rnd) if m]
    n_ok, n_all = count_checks(ev3)
    ok_all = bool(ev3["complete"] and ev3["all_ok"])
    if not ev3["complete"]:
        verdict_txt = "not decided"
    elif ok_all:
        verdict_txt = f"All {n_all} checks met, awaiting the human's sign-off"
    else:
        verdict_txt = f"NOT PASSED, {n_ok} of {n_all} checks met"
    parts = [f"<h2 id='attempt3'>Gate 2 attempt 3 (latest): {esc(verdict_txt)}</h2>"]
    mo, mr = metrics("ours", full["items"], all_ids), metrics("random", rnd["items"], all_ids)
    mm = metrics("middle", mid["items"], all_ids) if mid else {}
    miss = rank1_misses(M3)
    fam = family_metrics("ours", full["items"], all_ids)
    weakest = weakest_families(fam, 1)
    dly = delay_rows(M3)
    delay_hits = mo["by_category"]["delay"]["model"]
    parts.append(
        "<p class='lede'><strong>In plain English.</strong> "
        + chip("pass" if ok_all else "fail" if ev3["complete"] else "wait", verdict_txt)
        + f" Attempt 3 ran our search once on a third fresh, sealed set of 96 known presets (<code>{esc(M3['set'])}</code>), with the "
        f"pass rule frozen and its rank-1 check amended as described below. It named the exact model for {mo['model_hits']} of {mo['n']} "
        f"(random walk {mr['model_hits']}, middle-of-every-knob {mm.get('model_hits', 'n/a')}). {n_ok} of the {n_all} judged checks are met"
        + (". The gate is <strong>not passed yet</strong>: it counts only when the human signs it off. " if ok_all else ". ")
        + f"The weakest kind of effect is now <strong>delay</strong>: the exact model was right on {delay_hits} of 12 delay items, "
        "and most of those misses name one delay model in place of another.</p>")
    parts.append("<h3>The seven checks</h3>")
    parts.append(pass_table(ev3, None, M3, v2=True))
    parts.append("<h3>What changed in the rule, in plain words</h3><p>" + RULE_B_AMENDMENT + "</p>"
                 "<p><strong>Not re-scored.</strong> Attempts 1 and 2 are shown on this page with the figures recorded at the time. "
                 "Where a re-scored figure is ever shown, it is labelled as such.</p>")
    parts.append("<h3>The frozen rule, word for word</h3><p>Quoted from <code>docs/DECISIONS.md</code>; written before any full run. Its text "
                 "names synthetic-v1 and the old rank-1 reading because it was written for attempt 1; attempt 3 applied it unchanged "
                 "except for the rank-1 amendment above. Its fifth point (the 80 items outside dev16) has no meaning on v3, which had "
                 "no tuning items, and is not counted as evidence:</p>"
                 f"<blockquote>{esc(rule_text) if rule_text else 'The frozen-rule section was not found in docs/DECISIONS.md.'}</blockquote>")
    # three attempts side by side
    names = {"category": "Right kind of effect", "model": "Right model", "mae": "Settings error (lower is better)",
             "rank1": "Score ranks the true preset first", "vs_random_model": "Model accuracy against random walk (difference)",
             "vs_random_mae": "Settings error against random walk (difference; below 0 is better)"}
    bars = {"category": ">= 0.85", "model": ">= 0.60", "mae": "<= 0.15", "rank1": ">= 0.95",
            "vs_random_model": "CI low >= -0.05", "vs_random_mae": "CI high < 0"}
    evs = [("Attempt 1 (old rank-1 rule)", ev1), ("Attempt 2 (old rank-1 rule)", ev2), ("Attempt 3 (extended rule B)", ev3)]

    def cell(ev, key):
        c = next((x for x in (ev or {}).get("checks", []) if x["key"] == key), None)
        if not c or c["value"] is None:
            return "<td>-</td><td>-</td>"
        v = f"{c['value']:+.3f}" if key.startswith("vs_") else f"{c['value']:.3f}"
        return f"<td>{esc(v)}</td><td>{ok_chip(c['ok'])}</td>"

    rows = [f"<tr><th scope='row'>{esc(nm)}</th><td>{esc(bars[k])}</td>" + "".join(cell(e, k) for _, e in evs) + "</tr>"
            for k, nm in names.items()]
    cnt = " ".join(f"{lbl.split(' (')[0]}: {a} of {b}." for lbl, e in evs if e for a, b in [count_checks(e)])
    parts.append("<h3>The three attempts side by side</h3><div class='scroll'><table><thead><tr><th>Check</th><th>Bar</th>"
                 + "".join(f"<th colspan='2'>{esc(lbl)}</th>" for lbl, _ in evs) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
                 f"<p>Checks met (of those judged): {esc(cnt)} The three sets of 96 items are different, so this is a before-and-after, "
                 "not a paired test. The rank-1 row changed meaning between attempt 2 and attempt 3 (the amendment above), so do not read its rise as "
                 "a better score alone.</p>")
    if miss:
        mrows = "".join(f"<tr><td><code>{esc(e['id'])}</code></td><td>{esc(e['category'])}</td><td>{esc(pretty_model(e['model']))}</td>"
                        f"<td>{'yes' if e['model_ok'] else 'no'}</td><td>{esc(e['neighbour_rank'])}</td></tr>" for e in miss)
        parts.append(f"<h3>The {len(miss)} rank-1 misses</h3><p>The score ranked the true preset first on {len(all_ids) - len(miss)} of "
                     f"{len(all_ids)} items. The misses are listed so nothing is hidden; the bar of 0.95 allows at most 4.</p>"
                     "<div class='scroll'><table class='mini'><thead><tr><th>Item</th><th>Kind</th><th>True model</th>"
                     "<th>Search found the right model?</th><th>Where the true preset ranked (1 = first)</th></tr></thead>"
                     f"<tbody>{mrows}</tbody></table></div>")
    parts.append("<h3>All 96 items, next to both baselines</h3><p>" + chip("full", "full 96, never tuned on")
                 + " Same 96 items for every bar. The dashed line is the Gate 2 target. Every item was untuned.</p>")
    parts.append(results_panel(base + [full], all_ids, "all 96", set_name=M3["set"]))
    # per-family chart
    series = [(m, family_metrics(m["kind"], m["items"], all_ids)) for m in base + [full]]
    groups = []
    for fkey, fname, cats in FAMILIES:
        n_f = next((fm[fkey]["n"] for m, fm in series if m is full), 0)
        bars_ = []
        for m, fm in series:
            e = fm[fkey]
            v = e.get("model") if e.get("n") else None
            bars_.append({"color": m["color"], "value": v,
                          "tip": f"{m['label']}, {fname} family: right model {fmt(v, 3)} over {e.get('n', 0)} items. "
                                 f"Source: run files under cache/{M3['set']}/runs/"})
        groups.append({"label": fname, "sub": f"({', '.join(cats)}; {n_f} items)", "bars": bars_, "weak": fkey in weakest})
    legend = "".join(f"<span><span class='dot' style='background:{m['color']}'></span>{esc(m['label'])}</span> &nbsp; " for m, _ in series)
    parts.append("<h3>By family of effect: exact model right</h3><p class='legend'>" + legend + "</p>"
                 "<figure><figcaption>Share of items where the exact model was named (higher is better)</figcaption>"
                 + svg_family_bars(groups, 1.0, "Attempt 3: exact model, by family", "share of items") + "</figure>")
    # per category, with attempt 2 beside it
    crow, cat_groups = [], []
    bycat = {m["kind"]: metrics(m["kind"], m["items"], all_ids)["by_category"] for m in base + [full]}
    by2 = metrics("ours", M2["full"]["items"], M2["d"]["all_ids"])["by_category"] if M2 and M2.get("full") else None
    for c in HEAT_ORDER:
        b3 = bycat["ours"][c]
        b2 = by2[c] if by2 else None
        flag = " <strong>(weakest)</strong>" if c == "delay" else ""
        rnd_c, mid_c = bycat.get("random", {}).get(c), bycat.get("middle", {}).get(c)
        crow.append(f"<tr><th scope='row'>{esc(c)}{flag} <small>({esc(FAMILY_NAME[FAMILY_OF[c]])})</small></th>"
                    f"<td><strong>{b3['model']}/{b3['n']}</strong></td><td>{b3['category']}/{b3['n']}</td>"
                    f"<td>{rnd_c['model'] if rnd_c else '-'}/{b3['n']}</td><td>{mid_c['model'] if mid_c else '-'}/{b3['n']}</td>"
                    f"<td>{(str(b2['model']) + '/' + str(b2['n'])) if b2 else '-'}</td></tr>")
        cat_groups.append({"label": c, "sub": f"({b3['model']}/{b3['n']})", "weak": c == "delay",
                           "bars": [{"color": m["color"],
                                     "value": (bycat[m["kind"]][c]["model"] / bycat[m["kind"]][c]["n"]) if bycat[m["kind"]][c]["n"] else None,
                                     "tip": f"{m['label']}, {c}: right model. Source: run files under cache/{M3['set']}/runs/"}
                                    for m in base + [full]]})
    parts.append("<figure><figcaption>By category: exact model right, 12 items each</figcaption>"
                 + svg_family_bars(cat_groups, 1.0, "Attempt 3: exact model, by category", "share of items") + "</figure>")
    parts.append("<div class='scroll'><table><thead><tr><th>Category</th><th>Right model, attempt 3</th><th>Right kind, attempt 3</th>"
                 "<th>Random walk, right model</th><th>Middle, right model</th><th>Attempt 2 right model (different set)</th></tr></thead>"
                 f"<tbody>{''.join(crow)}</tbody></table></div>")
    if dly:
        swaps = [r for r in dly if r["pred_category"] == "delay"]
        drows = "".join(f"<tr><td><code>{esc(r['id'])}</code></td><td>{esc(pretty_model(r['model']))}</td>"
                        f"<td>{esc(pretty_model(r['pred']))}</td><td>{esc(r['pred_category'])}</td></tr>" for r in dly)
        parts.append(f"<h3>The biggest problem now: delay</h3><p>Delay is the weakest kind: {12 - len(dly)} of 12 right models. Of the "
                     f"{len(dly)} misses, {len(swaps)} named a different delay model (a swap within the family) and {len(dly) - len(swaps)} named a "
                     "different kind of effect. A swap between two delays may still sound close, which is a question for the ear: "
                     "the audio section below has the delay misses with the target on the left channel and our guess on the right.</p>"
                     "<div class='scroll'><table class='mini'><thead><tr><th>Item</th><th>True model</th><th>Our guess</th>"
                     f"<th>Kind of our guess</th></tr></thead><tbody>{drows}</tbody></table></div>")
    parts.append("<h3>What this does and does not show</h3><ul>"
                 f"<li>{esc(KNOWN_DI_NOTE)}</li>"
                 "<li><strong>The rank-1 rule was amended before this attempt.</strong> That is the human's ruling, recorded before the run. "
                 "Attempts 1 and 2 would have different rank-1 figures under it; they are not re-scored here.</li>"
                 "<li><strong>Split-80 carries no information on v3.</strong> There were no tuning items. It is shown and not counted as evidence.</li>"
                 "<li><strong>Three sealed sets are spent</strong> (v1, then v2, then v3). v3 is seen now and is not a test set any more.</li>"
                 "<li><strong>Ear not yet consulted.</strong> No listening session has been scored; the audio below is for you to listen to.</li>"
                 "<li><strong>Not passed until you sign.</strong> The gate row in the ledger is written only with the human's sign-off.</li></ul>")
    return "".join(parts)


def section_compare(M2: dict, M1: dict, ev2: dict, ev1: dict | None) -> str:
    """Attempt 1 (earlier) against attempt 2, per check and per category. Different sets: not a paired result."""
    parts = ["<h2 id='compare'>Attempt 1 versus attempt 2</h2>",
             "<p>Attempt 1 (<code>synthetic-v1</code>, run <code>gate2_full_hybrid</code>) was the earlier try and missed the "
             "rule. Attempt 2 (<code>synthetic-v2</code>, run <code>gate2_v2_attempt2</code>) added the family specialists. "
             "They used <strong>different sets of 96 items</strong>, so this is a before-and-after, not a paired test.</p>"]
    if not ev1 or not ev1["complete"]:
        parts.append("<p>" + chip("wait", "attempt 1 full run not found") + "</p>")
        return "".join(parts)
    names = {"category": "Right kind of effect", "model": "Right model", "mae": "Settings error (lower is better)",
             "rank1": "Score ranks the true preset first (rule B)", "vs_random_model": "Model accuracy against random walk (difference)",
             "vs_random_mae": "Settings error against random walk (difference; below 0 is better)",
             "split80": "The 80 items outside dev16 agree with the 96"}
    bars = {"category": ">= 0.85", "model": ">= 0.60", "mae": "<= 0.15", "rank1": ">= 0.95",
            "vs_random_model": "CI low >= -0.05", "vs_random_mae": "CI high < 0", "split80": "within each CI"}
    b1 = {c["key"]: c for c in ev1["checks"]}
    b2 = {c["key"]: c for c in ev2["checks"]}
    def val(c, key):
        if not c or c["value"] is None:
            return "-"
        return f"{c['value']:+.3f}" if key.startswith("vs_") else f"{c['value']:.3f}"

    rows = []
    for k, name in names.items():
        c1, c2 = b1.get(k), b2.get(k)

        v2chip = chip("wait", "no information on v2") if k == "split80" else ok_chip(c2["ok"] if c2 else None)
        rows.append(f"<tr><th scope='row'>{esc(name)}</th><td>{esc(bars[k])}</td><td>{esc(val(c1, k))}</td>"
                    f"<td>{ok_chip(c1['ok'] if c1 else None)}</td><td>{esc(val(c2, k))}</td><td>{v2chip}</td></tr>")
    n1, t1 = count_checks(ev1)
    n2, t2 = count_checks(ev2)
    parts.append("<h3>Per check</h3><div class='scroll'><table><thead><tr><th>Check</th><th>Bar</th><th>Attempt 1</th><th>Verdict</th>"
                 "<th>Attempt 2</th><th>Verdict</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
                 f"<p>Checks met: attempt 1 {n1} of {t1}, attempt 2 {n2} of {t2}.</p>")
    m1, m2 = metrics("ours", M1["full"]["items"], M1["d"]["all_ids"]), metrics("ours", M2["full"]["items"], M2["d"]["all_ids"])
    r1, r2 = metrics("random", M1["random"]["items"], M1["d"]["all_ids"]), metrics("random", M2["random"]["items"], M2["d"]["all_ids"])
    parts.append("<p>" + f"Category {fmt(m1['category'], 3)} to {fmt(m2['category'], 3)}, model {fmt(m1['model'], 3)} to "
                 f"{fmt(m2['model'], 3)}, rank-1 {fmt(m1['rank1'], 4)} to {fmt(m2['rank1'], 4)} (bar 0.95). Different sets, so "
                 "part of any change may be which items were drawn.</p>")
    crow = []
    for c in HEAT_ORDER:
        a, b = m1["by_category"][c], m2["by_category"][c]
        ra, rb = r1["by_category"][c], r2["by_category"][c]
        crow.append(f"<tr><th scope='row'>{esc(c)} <small>({esc(FAMILY_NAME[FAMILY_OF[c]])})</small></th>"
                    f"<td>{a['model']}/{a['n']}</td><td><strong>{b['model']}/{b['n']}</strong></td>"
                    f"<td>{a['category']}/{a['n']}</td><td><strong>{b['category']}/{b['n']}</strong></td>"
                    f"<td>{ra['model']}/{ra['n']}</td><td>{rb['model']}/{rb['n']}</td></tr>")
    parts.append("<h3>Per category (twelve items each)</h3><div class='scroll'><table><thead><tr><th>Category</th>"
                 "<th>Right model, attempt 1</th><th>Right model, attempt 2</th><th>Right kind, attempt 1</th>"
                 "<th>Right kind, attempt 2</th><th>Random walk model, attempt 1</th><th>Random walk model, attempt 2</th></tr></thead>"
                 f"<tbody>{''.join(crow)}</tbody></table></div>")
    return "".join(parts)


def section_what(M) -> str:
    return ("<h2 id='what'>1. What Gate 2 tests</h2>"
            "<p>Gate 2 asks one question: <strong>if we hear a sound made by a preset we know, can our search work out what the preset was?</strong> "
            "We make the answer key ourselves, so we can mark the answer.</p>" + flow_picture()
            + "<details><summary>Technical detail</summary><p>Each item is a one-block preset from the Helix Native block library "
              "(synthetic-v1, 96 items). The target is the preset rendered on a known DI clip (clip A). The search renders candidates "
              "on the same clip, scored by <code>mrstft_v2</code>, and returns a model and knob values. Held-out notes are a second passage "
              "(clip C) of the same recording. See <code>docs/notes/synthetic-benchmark-design.md</code>.</p></details>")


def section_method(M, rule_text: str, rule_shown_above: bool = False) -> str:
    parts = ["<h2 id='method'>2. Methodology</h2>"]
    items = [
        ("The synthetic set: 96 items, 8 categories",
         "We built 96 test presets, 12 for each of eight kinds of block: drive, delay, reverb, compressor, EQ, modulation (chorus, tremolo), amp and speaker cabinet. "
         "Each has randomly chosen but plausible knob settings. Because we chose them, we know the right answer.",
         "Pool: 6 models per category (48 in total), each item's true model is in the pool, so blind guessing the model within a known category is right 1 time in 6. "
         "Time-variant and unavailable blocks are excluded. Parameters sit on the 127-step grid of the template range; output-level knobs are not varied. "
         "Set generated by <code>scripts/synthetic_v1.py generate</code> (seed 20261016). Attempt 2 used a second sealed set, "
         "<code>synthetic-v2</code> (seed 20261017, avoiding v1's excerpts), with the same categories, the same pool sizes and the same "
         "rules."),
        ("The known DI",
         "A DI is a guitar recording with no amp or effects. Every test uses the same clean guitar playing, so the only thing that differs between the target and our guess is the preset. "
         "That removes the problem of guessing what the guitarist played.",
         "Guitar-TECHS DI excerpts (CC BY 4.0). Clip A (10 s) is used to fit; clip C is a different passage of the same recording and is used for the held-out check. "
         "This is the 'known-DI mode' ruled on 2026-10-16/17."),
        ("The admitted score: mrstft_v2",
         "To tell how alike two recordings sound, we compare their spectrograms (how much energy sits at each pitch range over time) at three zoom levels, after matching overall loudness. "
         "We use ear-like frequency bands so that the comparison cares about what people hear. 0 means identical; a wrong block of the right kind scores about 0.08; a random block about 0.7.",
         "Multi-resolution STFT, FFT 512, 2048, 8192, hop 1/4, 64 ERB bands from 20 Hz to 20 kHz, linear-magnitude L1 only, whole-clip unit-RMS level step, no silence gate. "
         "Frozen as <code>mrstft_v2</code> 2.0 on the train split, scored once on the test split (96 items, 19 distractors each): rank-1 0.990 under rule B on aligned notes (plain 0.875). "
         "Admitted for known-DI mode at weight 1.0, alone (DECISIONS 2026-10-16)."),
        ("Rule B (fair judging of near-identical neighbours)",
         "To trust the score, we check that it prefers the true preset over many wrong ones. Some wrong ones are so close that nobody could hear a difference. Rule B calls those a tie instead of a failure.",
         "A neighbour that nulls at -30 dB or lower against the truth on the same notes is a tie (<code>SAME_SOUND_DB</code>). Rank-1 'audible' is the rule-B figure. See <code>tonematch.metrics.trust.rank1_rule_b</code>."),
        ("The 300-render budget",
         "Every method may play the clip through at most 300 candidate presets per item. That makes the comparison fair: the search wins only if it is smarter, not because it tried more.",
         "Native renders a 10 s clip in about 0.3 s after the T043 fix. The rank-1 check and the new-notes check use extra renders that are not part of the search budget."),
        ("The baselines, and why they exist",
         "A result like '60% right' means little alone. <strong>Middle of every knob</strong> sets every dial to halfway and just picks the best-sounding model: it shows what you get with no search at all. "
         "<strong>Random walk</strong> tries 300 random models and settings from the 47-model test pool and keeps the one the score likes best: it shows what luck plus the same score achieves. Its numbers are high because the pool is small (each model is tried about 6 times), not because a random guess is good: one random guess is right 12.5 % of the time for the category and 2.1 % for the exact model. Our search must beat both baselines, or it adds nothing.",
         "Middle: one render per pool model at mid-values; the table here uses its 'nothing given' pick (best over all 48), and the settings error is the true model at middle. "
         "Random: 300 draws, model uniform over the pool, every knob uniform over its range, best by score. Code: <code>run_baseline_item</code> in <code>scripts/synthetic_v1.py</code>."),
        ("Dev versus test",
         "We tune the method on 16 items (two of each category, called dev16). Because we looked at those while building, they cannot be the exam. The exam is all 96, and we also report the other 80 to check that dev16 did not flatter us.",
         "dev16 = i000 to i015, the first stratified prefix. The pass rule uses all 96 and requires the 80-item figures not to contradict it."),
    ]
    for t, plain, tech in items:
        parts.append(f"<h3>{esc(t)}</h3><p>{plain}</p><details><summary>Technical detail</summary><p>{tech}</p></details>")
    if rule_shown_above:
        parts.append("<h3>The frozen pass rule</h3><p>Written down <em>before</em> the full run, so nobody can move the goalposts. "
                     "It is quoted word for word in the attempt 2 section above (<a href='#attempt2'>jump</a>) and has not changed.</p>")
        return "".join(parts)
    parts.append("<h3>The frozen pass rule</h3><p>We wrote the pass rule down <em>before</em> the full run, so nobody can move the goalposts after seeing results. "
                 "It is quoted word for word from <code>docs/DECISIONS.md</code>:</p>")
    parts.append(f"<blockquote>{esc(rule_text) if rule_text else 'The frozen-rule section was not found in docs/DECISIONS.md.'}</blockquote>")
    return "".join(parts)


def section_results(M, earlier: bool = False) -> tuple[str, dict]:
    """Gate evidence first (the full 96-item run against both baselines, then the 80 untuned items),
    tuning history last. A dev16 preview appears only while no full run exists (human, 2026-10-17:
    a 'latest' dev16 figure beside the verdict read as the gate result)."""
    d = M["d"]
    dev_ids = d["dev_ids"]
    all_ids = d["all_ids"]
    ms = M["methods"]
    full = M["full"]
    rnd = M["random"]
    base = [m for m in ms if m["kind"] in ("middle", "random")]
    stage = [m for m in ms if m["kind"] != "ours" or m["role"] != "full"]
    have_full = bool(full and full["scope"] == "full")
    parts = ["<h2 id='results'>3. Results of attempt 1 (the earlier attempt)</h2>" if earlier
             else "<h2 id='results'>3. Results at a glance</h2>"]
    if earlier:
        parts.append("<p class='lede'>" + chip("dev", "earlier attempt") + " Everything in this section is <strong>attempt 1</strong> on "
                     "<code>synthetic-v1</code>. The latest result is the attempt 2 section near the top of this page.</p>")
    # 1. the verdict
    ev = evaluate_pass(full["items"], rnd["items"], M["total"], all_ids) if (full and rnd) else None
    prev = None
    parts.append("<h3>The Gate 2 pass rule</h3>")
    if not (ev and ev["complete"]):
        if M["dev"] and rnd:
            prev = (M["dev"], evaluate_pass(M["dev"]["items"], rnd["items"], 10**9, all_ids))
        parts.append("<p>" + chip("wait", "waiting for the full run") + " The rule can only be judged on all 96 items. "
                     "The last column shows the chosen method on the 16 tuning items for orientation; it is a preview, not evidence.</p>")
    else:
        parts.append("<p>" + chip("full", "full 96") + " Judged on all 96 items of the gate run. Every number in this table comes "
                     "from that run; tuning-item numbers are shown only in the tuning history further down.</p>")
    parts.append(pass_table(ev, prev, M))
    # 2. all 96
    parts.append("<h3>All 96 items</h3>")
    if have_full:
        parts.append("<p>" + chip("full", "full 96") + " The complete gate run, next to both baselines on the same 96 items. "
                     "The dashed line is the Gate 2 target.</p>")
        parts.append(results_panel(base + [full], all_ids, "all 96"))
        test80 = [i for i in all_ids if i not in set(dev_ids)]
        parts.append("<h3>The 80 items never used for tuning</h3><p>Items i016 to i095 were never used to tune, so these are the honest numbers.</p>")
        parts.append(results_panel(base + [full], test80, "80 items i016 to i095"))
        rows96 = [{"label": m["label"], "metrics": metrics(m["kind"], m["items"], all_ids)} for m in base + [full]]
        parts.append("<h3>By category, grouped by family of effect (all 96)</h3><p>Each cell is how many of that category's 12 items were right. Darker is better.</p>")
        parts.append(heatmap(rows96, "model", "Exact model, all 96 (twelve items per category)"))
        parts.append(heatmap(rows96, "category", "Right kind of effect, all 96 (twelve items per category)"))
    else:
        note = f"the full run is partial: {full['scope']}" if full else "waiting for the full run"
        parts.append(f"<p>{chip('wait', 'waiting for the full run')} The baselines are complete; our bar appears here when "
                     f"the 96-item run finishes ({esc(note)}).</p>")
        parts.append(results_panel(base, all_ids, "all 96", wait_label="Ours: full 96 run"))
    parts.append("<p class='muted'>The judging score's rank-1 is a property of the score, not of a method, so it is shown once "
                 "for our runs and not for the baselines. The middle baseline's settings error is measured on the true model at "
                 "the middle (no model guess is involved).</p>")
    # 3. tuning history, clearly secondary
    parts.append("<h3>Tuning history: the same 16 tuning items, every method</h3>"
                 "<p>" + chip("dev", "dev16 only: tuning items, not gate evidence")
                 + " How the method was developed. These 16 items (i000 to i015) were used to choose the method, so they "
                   "flatter it; the gate result above is what counts.</p>")
    parts.append(results_panel(stage, dev_ids, "dev16"))
    parts.append(tuning_history(M))
    rows = [{"label": m["label"], "metrics": metrics(m["kind"], m["items"], dev_ids)} for m in stage]
    parts.append(heatmap(rows, "model", "Exact model, dev16 (two items per category)"))
    parts.append(heatmap(rows, "category", "Right kind of effect, dev16 (two items per category)"))
    return "".join(parts), {"eval": ev, "preview": prev}


def count_checks(ev) -> tuple[int, int]:
    """(checks met, checks judged) for a complete evaluation; the pass-rule logic itself is unchanged."""
    cs = [c for c in (ev or {}).get("checks", []) if c["ok"] is not None]
    return sum(1 for c in cs if c["ok"]), len(cs)


def pass_table(ev, prev, M, v2: bool = False) -> str:
    keys = ["category", "model", "mae", "rank1", "vs_random_model", "vs_random_mae", "split80"]
    texts = {"category": ("Right kind of effect (category)", ">= 0.85"), "model": ("Right model", ">= 0.60"),
             "mae": ("Settings error on right-model items", "<= 0.15"),
             "rank1": ("Our score ranks the true preset first (rule B)", ">= 0.95"),
             "vs_random_model": ("Model accuracy not worse than random walk", "CI low >= -0.05"),
             "vs_random_mae": ("Settings error better than random walk (items both got right)", "CI high < 0"),
             "split80": ("The 80 items outside dev16 agree with the 96", "within each CI")}
    by_full = {c["key"]: c for c in ev["checks"]} if ev else {}
    by_prev = {c["key"]: c for c in prev[1]["checks"]} if prev else {}
    head = ("<thead><tr><th>Rule</th><th>Bar</th><th>Full 96</th><th>Verdict</th>"
            + (f"<th>Chosen method on tuning items ({esc(prev[0]['tag'])}) <small>preview</small></th>" if prev else "")
            + "</tr></thead>")
    body = []
    for k in keys:
        t, bar = texts[k]
        c = by_full.get(k)
        p = by_prev.get(k)
        val = "-" if not c or c["value"] is None else num(f"{c['value']:+.3f}" if k.startswith("vs_")
                                                           else f"{c['value']:.4f}" if k == "rank1" else f"{c['value']:.3f}",
                                                           src_title("full run item files", "paired bootstrap 5000, seed 20261017"))
        verdict_html = ok_chip(c["ok"] if c else None)
        if v2 and k == "split80":
            # v2 had no dev16 tuning: all 96 items are untuned, so comparing 80 of them with the 96 tells us nothing.
            verdict_html = chip("wait", f"counted as pass, but no information on {M['set'][-2:]}" if M else "counted as pass, but no information",
                                "v2 had no tuning items; all 96 are untuned, so this check cannot fail in a meaningful way")
        pv = "-"
        if p and p["value"] is not None:
            pv = num(f"{p['value']:+.3f}" if k.startswith("vs_") else f"{p['value']:.3f}",
                     src_title(f"{prev[0]['tag']} item files vs random on the same items", "indicative only"))
        elif p and p["note"]:
            pv = esc(p["note"])
        note = f"<br><small class='muted'>{esc(c['note'])}</small>" if c and c.get("note") else ""
        if v2 and k == "split80":
            note = (f"<br><small class='muted'>Attempt {ATTEMPTS[M['attempt']]['n'] if M else 2} used no tuning items (no dev16 tuning). All 96 are untuned, so "
                    "the 80 and the 96 are the same kind of item and this line is not evidence. It is shown only because the "
                    "recorded count of seven checks includes it.</small>")
        body.append(f"<tr><th scope='row'>{esc(t)}{note}</th><td>{esc(bar)}</td><td>{val}</td><td>{verdict_html}</td>"
                    + (f"<td>{pv}</td>" if prev else "") + "</tr>")
    n_ok, n_all = count_checks(ev)
    if ev is None or not ev["complete"]:
        overall = ok_chip(None, "Gate 2 not decided")
    elif ev["all_ok"]:
        overall = chip("pass", "Gate 2 criteria all met (needs human sign-off)")
    elif v2:
        overall = chip("fail", f"Gate 2 NOT PASSED: {n_ok} of {n_all} checks met")
    else:
        overall = chip("fail", f"Gate 2 criteria not all met ({n_ok} of {n_all} checks)")
    return (f'<div class="scroll"><table class="pass">{head}<tbody>{"".join(body)}</tbody></table></div>'
            f"<p>Overall: {overall} <small class='muted'>The gate row itself is written only with the human's sign-off.</small></p>")


def section_examples(M, ex_ctx) -> str:
    d = M["d"]
    labels = {x["id"]: x for x in d["labels"]}
    ms = [m for m in M["methods"] if m["kind"] == "ours"]
    parts = ["<h2 id='examples'>4. Accuracy with examples</h2>"]
    if not ms:
        return parts[0] + "<p>No search run found yet.</p>"
    src = M["full"] or M["dev"]
    if src is None:
        return parts[0] + "<p>The designated runs have not produced items yet.</p>"
    parts.append(f"<p>Real items from <code>{esc(src['tag'])}</code> ({scope_chip(src['scope'])}). "
                 "Each card shows the true block and where each knob really was (black diamond), where we put it (coloured dot) and the "
                 "middle-of-every-knob guess (grey ring). Random walk's knob values were not saved, so it appears in the table only. "
                 "Wins, near misses and misses are all shown on purpose.</p>")
    cards = []
    for ex in pick_examples(src["items"], labels):
        truth = labels.get(ex["id"], {})
        cards.append(example_card(ex, truth, src, M["middle"], M["random"]))
    parts.append(f'<div class="grid2">{"".join(cards)}</div>')
    return "".join(parts)


def section_audio(M, pairs, ab_link: str) -> str:
    parts = ["<h2 id='audio'>5. Audio comparisons</h2>",
             "<p>Press play to hear the target (the real preset) against our recovered guess, both on <strong>new notes</strong> the search never heard. "
             "Each pair is matched to the same loudness (-23 LUFS) so the louder one does not seem better.</p>",
             "<p><strong>Side-by-side files:</strong> every item also has a stereo file with the target on the <strong>left</strong> "
             "channel and our recovered sound on the <strong>right</strong>, for listening on headphones. They are in "
             "<code>reports/local/gate2/audio/&lt;run&gt;/stereo_L-target_R-recovered/</code> (all items, not only the ones with players below).</p>"]
    if pairs:
        last = None
        newest = "v3" if any(q.get("attempt") == "v3" for q in pairs) else "v2"
        for p in pairs:
            att = p.get("attempt", "v1")
            grp = f"Attempt {ATTEMPTS[att]['n']} ({'latest' if att == newest else 'earlier attempt'})"
            if grp != last and any(q.get("attempt") in LATER for q in pairs):
                parts.append(f"<h3>{grp}</h3>")
                if att == "v3":
                    parts.append("<p class='muted'>Players: our recovered sound on the rank-1 miss items (with the middle-of-every-knob baseline "
                                 "on the same items), every delay miss, two delay wins, then some of our clear wins. Each is labelled with why it "
                                 "is here. All 96 items for ours and for both baselines are in the stereo files "
                                 "(target left, recovered right).</p>")
                elif att == "v2" and newest == "v3":
                    parts.append("<p class='muted'>No players for the earlier attempts on this page once attempt 3 exists; their stereo files "
                                 "(target left, recovered right) are still written for every item.</p>")
                elif att == "v2":
                    parts.append("<p class='muted'>Players: our recovered sound on the six rank-1 miss items, the middle-of-every-knob "
                                 "baseline on the same six (its model pick with every knob at the middle), then some of our clear wins. "
                                 "All 96 items for ours and for both baselines are in the stereo files.</p>")
                last = grp
            if not p.get("target_rel"):
                continue
            parts.append(f'<div class="card"><strong>{esc(p["id"])}</strong> '
                         + (f'{chip("dev", p["why"])} ' if p.get("why") else "")
                         + f'<small>{esc(p["method"])} (source: {esc(p["source"])})</small><div class="pair">'
                         f'<div><div class="muted">Target (the real preset)</div><audio controls preload="none" src="{esc(p["target_rel"])}"></audio></div>'
                         f'<div><div class="muted">Recovered (our guess)</div><audio controls preload="none" src="{esc(p["guess_rel"])}"></audio></div>'
                         + (f'<div><div class="muted">Side by side: target LEFT, recovered RIGHT (headphones)</div><audio controls preload="none" src="{esc(p["lr_rel"])}"></audio></div>' if p.get("lr_rel") else "")
                         + "</div></div>")
    else:
        parts.append("<div class='card'>" + chip("wait", "no audio rendered yet") +
                     "<p>No <code>&lt;item&gt;_target.wav</code> and <code>&lt;item&gt;_recovered.wav</code> exist yet in any run folder "
                     "(only runs started with <code>--save-audio</code> save them). When Native is free, the lead can render the examples with:</p>"
                     "<pre>python scripts/gate2_report.py render-examples --run v4b_dev16_seed --items i000,i003,i008,i012\n"
                     "python scripts/gate2_report.py build</pre></div>")
    parts.append(f"<h3>Listen blind</h3><p>Knowing which is which changes how you hear. For an honest test, use the blind A/B tool (task T107): "
                 f"it hides which clip is the target and keeps the answer key outside the page folder. {ab_link}</p>"
                 "<details><summary>Technical detail</summary><p>Build a session with <code>python scripts/ab_session.py gate2-demo</code> (uses the same pairs) "
                 "or by hand with <code>python scripts/ab_session.py build --name gate2 --pair \"i001=rec.wav,tgt.wav\"</code>. "
                 "Score it with <code>ab_session.py score</code>. The report never scores a listening session: the ear is the final judge.</p></details>")
    return "".join(parts)


def trust_chart(M) -> str:
    tr = trust_rows(M["ledger"])
    v1 = tr["v1"]
    terms = [("ltas_tilt", "Overall tone tilt"), ("crest_flatness_hnr", "Crest, flatness, noisiness"),
             ("envelope", "Loudness shape over time"), ("embed_afxrep", "Learned effect embedding (AFx-Rep)"),
             ("provisional_sum", "All the terms added up"), ("mrstft", "Spectrogram comparison (mrstft v1)")]
    rows_a, rows_b = [], []
    for k, name in terms:
        a = v1.get((k, "sep/same_notes"))
        b = v1.get((k, "sep/heldout_notes"))
        col = COL["s1"] if k == "mrstft" else COL["middle"]
        rows_a.append({"label": name, "value": a[0] if a else None, "color": col,
                       "tip": src_title(f"ledger/runs.csv {a[1]}" if a else "", "dataset helix-trust-v1, rank1_rate, sep, same notes")})
        rows_b.append({"label": name, "value": b[0] if b else None, "color": col,
                       "tip": src_title(f"ledger/runs.csv {b[1]}" if b else "", "dataset helix-trust-v1, rank1_rate, sep, different notes")})
    c1 = svg_bars(rows_a, 1.0, None, "", "share of items where the true preset ranked first", "Trust check v1, same notes")
    c2 = svg_bars(rows_b, 1.0, None, "", "share of items where the true preset ranked first", "Trust check v1, different notes")
    r2 = tr["v2"]
    rows_c = []
    for term, name, col in (("mrstft", "Spectrogram comparison v1 (mrstft)", COL["s0"]),
                            ("mrstft_v2", "Tuned version (mrstft_v2)", COL["s2"])):
        for subset, sname in (("aligned/same_notes", "aligned DI"),):
            for metric, mname in (("rank1_rate", "plain"), ("rank1_rate_audible", "rule B")):
                e = r2.get((term, subset, metric))
                rows_c.append({"label": f"{name}, {mname}", "value": e[0] if e else None, "color": col,
                               "tip": src_title(f"ledger/runs.csv {e[1]}" if e else "", "dataset helix-trust-v2, test split, 96 items, " + sname)})
    c3 = svg_bars(rows_c, 1.0, 0.95, "bar 0.95", "share of 96 test items where the true preset ranked first", "Trust check v2")
    return c1, c2, c3


def verify_card(title: str, rows: dict, spec: list[tuple[str, str]], intro: str) -> str:
    if not rows:
        return f"<div class='card'><h4>{esc(title)}</h4><p class='muted'>No ledger rows found.</p></div>"
    li = []
    for metric, text in spec:
        r = rows.get(metric)
        if not r:
            continue
        shown = r["frac"] or (f"{r['value']:g}" if r["value"] is not None else "?")
        li.append(f"<li>{esc(text)}: {num(shown, src_title('ledger/runs.csv ' + r['ts'], metric))}</li>")
    return f"<div class='card'><h4>{esc(title)}</h4><p class='muted'>{esc(intro)}</p><ul>{''.join(li)}</ul></div>"


def section_why(M) -> str:
    c1, c2, c3 = trust_chart(M)
    ms = M["methods"]
    stages = [m for m in ms if m["kind"] == "ours" and m["scope"] == "dev16"]
    rows = []
    for m in stages:
        mm = metrics("ours", m["items"], M["d"]["dev_ids"])
        rows.append({"label": m["label"], "value": mm.get("screen_rank_median"), "color": m["color"],
                     "tip": src_title(f"{m['tag']} item files", "true_model_screen_rank, median over dev16"), "sub": ""})
    rank_chart = svg_bars(rows, 50, None, "", "where the true model sat in the first look, of 48 (1 is best)",
                          "Median position of the true model after the first look", True, nd=1) if rows else ""
    ledger = M["ledger"]
    nat = verify_rows(ledger, "native-verify-v1")
    stp = verify_rows(ledger, "stomp-verify-v1")
    parts = ["<h2 id='why'>6. Why we use these models</h2>",
             "<h3>The judge: why mrstft_v2</h3>"
             "<p>Before any search, we checked each candidate score by asking: <em>does it prefer the true preset over wrong ones?</em> "
             "A score that cannot do that would send the search the wrong way. On the first trust check (v1, 40 items, no matching DI) most ideas were poor; "
             "the plain spectrogram comparison was the only one that did well, and only when it could compare the same notes.</p>",
             f'<div class="grid2"><figure class="card"><figcaption>v1: same notes</figcaption>{c1}</figure>'
             f'<figure class="card"><figcaption>v1: different notes</figcaption>{c2}</figure></div>',
             "<p>That is why Gate 2 uses a known DI: with the same notes on both sides the comparison works. We then tuned the spectrogram comparison on training items only and "
             "scored it once on held-out test items:</p>",
             f'<figure class="card"><figcaption>v2: the tuned score on the test split</figcaption>{c3}</figure>',
             "<details><summary>Technical detail</summary><p>helix-trust-v1: 40 items x 19 distractors, Native 3.82 renders, no rule B (plain rank-1). "
             "helix-trust-v2: 96 items, 19 distractors, 118 same-sound distractors, train/test split, frozen <code>mrstft_v2</code> 2.0 scored once; "
             "bar 0.90 per term, kit bar 0.95 met by the single-term known-DI kit. The learned embedding AFx-Rep scored 0.075 (worse than the plain comparison) and stays at weight 0. "
             "Rows come from <code>ledger/runs.csv</code> (kind <code>loss-trust</code>).</p></details>",
             "<h3>Choosing the model: fingerprints, measured seeding and the hybrid</h3>"
             "<p>The first search just tried each model once and refined the best few. It often missed delays and reverbs, because a delay with the wrong echo time sounds nothing like the target "
             "whatever the model. We improved it in steps:</p><ol>"
             "<li><strong>Model choice with fingerprints.</strong> Try every candidate model at a few different settings, picked so that different models can be told apart, then spend the rest of the renders on the best.</li>"
             "<li><strong>Measured seeding.</strong> Listen to the target first: measure the echo time, reverb length or tremolo speed directly, and start the search near those values. This costs zero extra renders.</li>"
             "<li><strong>The hybrid</strong> (being tuned now): cast a wide net of space-filling draws over all models, narrow to the top few, then refine them with halving. It spends the same 300 renders.</li></ol>",
             rank_chart and f'<figure class="card"><figcaption>How early the true model shows up</figcaption>{rank_chart}</figure>',
             "<details><summary>Technical detail</summary><p>Stages A (one render per model, defaults or seeds), B (equal extra fingerprint settings), C (successive halving over the best beam with banded CMA-ES). "
             "Seeds are mapped to knobs through per-model calibration curves from <code>scripts/seed_calibrate.py</code>. Limits seen: multi-voice delays (Harmony Delay), models without a rate knob, "
             "reverb decay seeds about 0.14 of range off. Full write-up: <code>docs/notes/measured-seed.md</code>; hybrid: task T114 and <code>src/tonematch/search/hybrid.py</code>.</p></details>",
             "<h3>Two engines, two jobs</h3>"
             "<p><strong>Helix Native</strong> (software) is the workhorse: it renders a clip in about 0.3 seconds, so it can afford hundreds of tries per tone. "
             "<strong>The HX Stomp</strong> (the real pedal) is the referee: it is slow (about 3 to 4 seconds per render) and is used to verify a finished preset on the real hardware. "
             "Before trusting either, we ran verification suites with pass/fail rules written in advance.</p>",
             '<div class="grid2">',
             verify_card("Native checks (native-verify-v1)", nat,
                         [("truth_audit_pass", "Settings match the reference drop files"), ("baked_render_pass", "Baked-in values render as expected"),
                          ("repeat_pass", "Repeat renders identical"), ("canary_unexplained", "Unexplained numeric drifts"),
                          ("baked_violations", "Search frees an unverified setting")],
                         "Does Native render exactly what we asked for, every time?"),
             verify_card("Stomp checks (stomp-verify-v1)", stp,
                         [("check1_setting_truth_pass", "Setting truth"), ("check2_repeat_pass", "Repeat renders"),
                          ("check3_settle_pass", "Settings settle fast enough"), ("check4_sentinel_pass", "Slip sentinel works"),
                          ("restore_ok", "Pedal restored to its original preset")],
                         "Does the pedal behave well enough to be the referee? The first sessions found problems that are still being worked on (tasks T110 to T112)."),
             "</div>"]
    return "".join(parts)


STRATEGIES = [
    ("A learned guitar embedding trained on note-change pairs",
     "Train a network so two recordings of the same preset on different notes land close together. It would judge tone while ignoring which notes were played, which is what real songs need.",
     ["Targets the real-song problem (no matching DI)", "We now have the IR library and a 1,932-preset corpus to generate training data, and training is allowed"],
     ["Needs thousands of Native renders and careful held-out sets", "The off-the-shelf learned embedding (AFx-Rep) did worse than the plain comparison here"],
     "Weeks. Largest single piece of work.",
     "Try it if the real-song trust check (different notes, separated stems) stays far below 0.9 while the known-DI score passes. Gate 2 does not need it.",
     "docs/PLAN.md (learning track); docs/notes/afx-rep.md; docs/TONE-COMPARISON.md"),
    ("A neural predictor that seeds the search",
     "Train a network to look at a sound and propose the likely model and settings, then let the search polish the proposal.",
     ["Cheap at run time; could lift model accuracy where measured seeding cannot (amps, cabs, comps, EQs)", "Improves as more verified matches are collected"],
     ["Needs a training set; accuracy on real songs is unknown until tested", "Risk of confidently wrong starts"],
     "Weeks (data generation is the cost).",
     "Worth trying if the full-96 run shows amp, cab, comp and EQ categories as the main misses.",
     "docs/PLAN.md (blind predictor); docs/AGENT-TEAM.md (learning)"),
    ("Differentiable proxies of blocks (gradient search)",
     "Train a small network to imitate each block, so settings can be tuned by following the slope rather than guessing.",
     ["Very fast inner loop; helps hardware-only users", "Settings could get closer than a 300-render search"],
     ["Each block needs its own proxy; imitation errors become search errors", "Must be re-verified against the real renderer"],
     "Months for a whole library; days for a pilot on one block.",
     "Pilot if settings error stays above 0.15 on right-model items after the full run.",
     "docs/PLAN.md (neural proxies)"),
    ("Bayesian optimisation",
     "Fit a cheap model of 'settings to score' as renders come in and choose each next try where it expects the biggest gain.",
     ["Sample-efficient: suits the Stomp's 100 to 300 render budget", "Little new infrastructure"],
     ["Slower per step; loses its edge past a handful of knobs; our blocks have only 1 to 6 knobs, where CMA-ES already does well"],
     "Days.",
     "Try for Stomp mode, where renders are 10 times costlier. Not a priority for Native.",
     "docs/PLAN.md (budget per engine); docs/notes/search-design.md"),
    ("A larger budget, or two-stage budgets",
     "Spend 300 renders for easy items and more where the top two candidates are close.",
     ["Simplest; no new code beyond a stopping rule", "Directly addresses near misses"],
     ["Costs time per tone; makes comparisons with the 300-render baselines unfair unless re-run"],
     "Hours.",
     "Cheap to test once the full run shows how many misses were close calls.",
     "docs/BENCHMARKING.md; this report's baselines"),
    ("Cab by deconvolution",
     "Estimate a speaker cabinet's frequency response directly from the target and the DI, then pick the nearest cab, instead of searching.",
     ["About 30 ms instead of renders", "With the right amp, the exact IR was recovered 94% of the time in tests"],
     ["Only works once the amp is right; with the wrong amp model it was 2% useful", "Known-DI only"],
     "Days (T106 has the code; wiring is the work).",
     "Use as a re-ranker after the amp stage once cab misses are confirmed in the full run.",
     "docs/notes/ir-library.md; DECISIONS 2026-10-17 (T106)"),
    ("Corpus and usage priors",
     "Try the models real players use most first, and lay out chains the way real presets do.",
     ["Already accepted for ordering the pool and for the chain builder", "Free at run time"],
     ["Only breaks ties; cannot rescue a model the score cannot tell apart", "Metadata added little: about +0.01 bits per block for style"],
     "Done for ordering; hours to tune.",
     "Keep. Measure its tie-break value on the full run.",
     "docs/notes/chain-patterns.md; DECISIONS 2026-10-17 (T109)"),
    ("Song-context priors (artist, era, genre)",
     "Use public song facts to guess the likely rig.",
     ["Could help a lot for songs with well-documented rigs"],
     ["Genre adds little beyond what the corpus already gives; needs an opt-in and a network lookup; gain may be zero", "Not testable on synthetic-v1"],
     "Days for a probe; weeks to ship.",
     "Only test on the real-song set (Gate 5), and only after the audio-based methods.",
     "docs/notes/song-context.md (T104); DECISIONS 2026-10-17 (T109)"),
    ("More DI variety",
     "Test and train with many players, pickups and playing styles, not a few excerpts.",
     ["Shows whether results hold beyond three players' playing", "Needed before any learned model can be trusted"],
     ["Only lengthens the test; does not itself improve accuracy"],
     "Hours to generate; Native time to run.",
     "Do before any learned approach and before quoting Gate 2 as general.",
     "docs/notes/dry-signal.md; docs/BENCHMARKING.md"),
]


def section_other() -> str:
    parts = ["<h2 id='other'>7. Other strategies that may work better</h2>",
             "<p>Honest assessments, not promises. Each lists what it offers, what it costs, and what evidence would make it worth trying. "
             "None of these is needed to run Gate 2.</p><div class='cols3'>"]
    for name, plain, pros, cons, effort, when, cite in STRATEGIES:
        parts.append(f"<article class='card pc'><h4>{esc(name)}</h4><p>{esc(plain)}</p>"
                     f"<strong>For</strong><ul>{''.join(f'<li>{esc(x)}</li>' for x in pros)}</ul>"
                     f"<strong>Against</strong><ul>{''.join(f'<li>{esc(x)}</li>' for x in cons)}</ul>"
                     f"<p><strong>Effort:</strong> {esc(effort)}<br><strong>Try it when:</strong> {esc(when)}</p>"
                     f"<p class='muted'>Sources: {esc(cite)}</p></article>")
    parts.append("</div>")
    return "".join(parts)


def section_next(M) -> str:
    if M["attempt"] == "v3":
        return ("<h2 id='next'>8. What happens next</h2><ol>"
                "<li><strong>The human signs Gate 2, or does not.</strong> All seven checks are met on synthetic-v3, but the gate row is written "
                "only with the human's sign-off.</li>"
                "<li><strong>Listen first if you want to.</strong> The audio below has the target on the left channel and our guess on the right, "
                "for wins and for the misses, especially delay. The blind A/B tool is the honest test.</li>"
                "<li><strong>The next target is delay.</strong> It is the weakest kind of effect (5 of 12 right models), mostly swaps between delay models.</li>"
                "<li><strong>Gate 3</strong> is the blind single-block test: the matcher gets only the output audio, not the dry track. Real pedals (Gate 4) and then real songs (Gate 5) follow.</li></ol>"
                "<p>Sources: <code>docs/DECISIONS.md</code> (&ldquo;Gate 2 attempt 3 result&rdquo;), <code>board/GATES.md</code>.</p>")
    if M["attempt"] == "v2":
        return ("<h2 id='next'>8. What happens next</h2><ol>"
                "<li><strong>Gate 2 is not passed.</strong> The rule is unchanged and nothing on v2 is re-scored; v2 is now seen.</li>"
                "<li><strong>Attempt 3 needs a fresh sealed set</strong> (<code>synthetic-v3</code>, avoiding v1, v2 and devfam).</li>"
                "<li><strong>Before attempt 3:</strong> a gain-stage term for the score that passes the score trust check on gain neighbours "
                "(tone-metrics), plus specialist work for modulation and drive (drive is in the gain family).</li>"
                "<li><strong>The human decides the order.</strong></li>"
                "<li><strong>Listen.</strong> The audio below has the target on the left channel and our guess on the right; "
                "the blind A/B tool is the honest test.</li></ol>"
                "<p>Sources: <code>docs/DECISIONS.md</code> (&ldquo;Gate 2 attempt 2 result&rdquo;), <code>board/GATES.md</code>.</p>")
    full = M["full"]
    state = ("The full run is " + (f"{esc(full['scope'])}." if full else "not started."))
    return ("<h2 id='next'>8. What happens next</h2><ol>"
            "<li><strong>Finish tuning the hybrid</strong> on dev16 (task T114, at most three design iterations).</li>"
            f"<li><strong>Run all 96 items</strong> with the frozen settings and 300 renders each. {state}</li>"
            "<li><strong>Apply the frozen pass rule</strong>, report the 96 and the 80 outside dev16, and record ledger rows.</li>"
            "<li><strong>Render and listen.</strong> The lead renders the example audio; you can then listen openly here and blind in the A/B tool.</li>"
            "<li><strong>Human sign-off</strong> on Gate 2 (the gate row is written only with it).</li></ol>"
            "<p><strong>Then Gate 3</strong> tests blind prediction of a single block without the dry track (research T158 first); real pedals (Gate 4) follow, and <strong>Gate 5</strong> moves to real songs: 20 songs you choose, from clean to high gain. "
            "The matched preset is compared in blind listening against the nearest community preset and a text-only AI preset; the pedal-versus-software null residual and the Native-versus-Stomp comparison are also recorded. "
            "Sources: <code>board/GATES.md</code>, <code>docs/PLAN.md</code>, <code>docs/TONE-COMPARISON.md</code>.</p>")


def section_runs(M, *older) -> str:
    rows = []
    for mm_ in [M] + [o for o in older if o is not None and o is not M]:
        att = f"attempt {ATTEMPTS[mm_['attempt']]['n']}"
        for m in mm_["methods"]:
            mt = metrics(m["kind"], m["items"])
            rows.append(f"<tr><td>{att}: {esc(m['label'])}</td><td><code>{esc(m['tag'])}</code></td><td>{scope_chip(m['scope'])}</td>"
                        f"<td>{mt['n']}</td><td>{'yes' if m['admitted'] else 'no (provisional)'}</td><td>{esc(m['budget'] or '?')}</td></tr>")
        for r in mm_["d"]["other"]:
            rows.append(f"<tr><td>{att}: earlier / diagnostic run</td><td><code>{esc(r['tag'])}</code></td><td>{chip('dev', 'not used above')}</td>"
                        f"<td>{len(r['items'])}</td><td>{'yes' if r['admitted'] else 'no (provisional)'}</td><td>{esc(r['budget'] or '?')}</td></tr>")
    return ("<details><summary>Every run found, and what it was used for</summary><div class='scroll'><table>"
            "<thead><tr><th>Role</th><th>Folder</th><th>Scope</th><th>Items</th><th>Admitted score</th><th>Budget</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></details>")


def render_page(root: Path, pairs: list[dict]) -> str:
    M1 = build_model(root, "v1")
    M2 = build_model(root, "v2")
    M3 = build_model(root, "v3")
    has2 = bool(M2["full"] and M2["random"])
    has3 = bool(M3["full"] and M3["random"] and M3["full"]["scope"] == "full")
    M = M3 if has3 else M2 if has2 else M1       # the headline attempt
    rule = frozen_rule_text(root)
    now = dt.datetime.now(dt.UTC)
    newest = max([m["mtime"] for m in M["methods"]] or [0])
    newest_s = dt.datetime.fromtimestamp(newest, dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if newest else "n/a"
    ab = root / "reports" / "local" / "ab"
    ab_page = ab / "gate2-demo" / "index.html"
    ab_link = ('<a href="../ab/gate2-demo/index.html">Open the blind A/B page</a>.' if ab_page.exists()
               else "No session has been built yet: run <code>python scripts/ab_session.py gate2-demo</code> after the examples are rendered.")
    res_html, ctx1 = section_results(M1, earlier=has2)
    if has2:
        ev2 = evaluate_pass(M2["full"]["items"], M2["random"]["items"], M2["total"], M2["d"]["all_ids"])
        ev1 = ctx1["eval"]
        if has3:
            ev3 = evaluate_pass(M3["full"]["items"], M3["random"]["items"], M3["total"], M3["d"]["all_ids"])
            top = [section_headline(M3), KNOWN_DI_LEDE, section_attempt3(M3, M2, M1, rule, ev3, ev2, ev1),
                   section_attempt2(M2, M1, rule, ev2, latest=False), section_compare(M2, M1, ev2, ev1)]
        else:
            top = [section_headline(M2), KNOWN_DI_LEDE, section_attempt2(M2, M1, rule, ev2), section_compare(M2, M1, ev2, ev1)]
        tail = [section_what(M), section_method(M, rule, rule_shown_above=True), res_html, section_specialists(M2, M1),
                section_runs(*([M3] if has3 else []), M2, M1), section_examples(M, None), section_audio(M, pairs, ab_link), section_why(M1),
                section_other(), section_next(M)]
        nav_items = ((("attempt3", "Attempt 3"),) if has3 else ()) + (("attempt2", "Attempt 2"), ("compare", "Attempt 1 vs 2"), ("what", "What it tests"), ("method", "Methodology"),
                     ("results", "Attempt 1 results"), ("specialists", "Specialists"), ("examples", "Examples"), ("audio", "Audio"),
                     ("why", "Why these models"), ("other", "Other strategies"), ("next", "Next"), ("glossary", "Glossary"))
    else:
        top = [section_headline(M1)]
        tail = [section_what(M1), section_method(M1, rule), res_html, section_specialists(M1), section_runs(M1),
                section_examples(M1, None), section_audio(M1, pairs, ab_link), section_why(M1), section_other(), section_next(M1)]
        nav_items = (("what", "What it tests"), ("method", "Methodology"), ("results", "Results"), ("specialists", "Specialists"),
                     ("examples", "Examples"), ("audio", "Audio"), ("why", "Why these models"),
                     ("other", "Other strategies"), ("next", "Next"), ("glossary", "Glossary"))
    body = top + tail + ["<h2 id='glossary'>Glossary</h2>", glossary_html()]
    nav = "".join(f'<a href="#{a}">{t}</a>' for a, t in nav_items)
    sets = ("cache/synthetic-v3/runs/, " if has3 else "") + "cache/synthetic-v2/runs/ or cache/synthetic-v1/runs/" if has2 else "cache/synthetic-v1/runs/"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='light dark'><title>Gate 2: can the search recover a known preset?</title>"
            f"<style>{CSS}</style></head><body><header><h1>Gate 2: can the search recover a known preset?</h1>"
            f"<p class='muted'>Local report for the human. Last updated {now.strftime('%Y-%m-%d %H:%M UTC')}; newest run file {esc(newest_s)}. "
            "Regenerated by <code>scripts/gate2_report.py</code> (and by <code>scripts/status_report.py</code>). Not committed.</p>"
            f"<nav aria-label='sections'>{nav}</nav></header><main>{''.join(body)}</main>"
            f"<footer>Every number comes from a run file under <code>{sets}</code> or a row of <code>ledger/runs.csv</code>; "
            "hover or focus a number or bar for its source. Works offline: no scripts, no web fonts.</footer></body></html>")


# --------------------------------------------------------------------------- commands

def build(root: Path = ROOT, out: Path | None = None, audio: bool = True) -> Path:
    out = out or (root / OUT_REL)
    out.mkdir(parents=True, exist_ok=True)
    M1, M2, M3 = build_model(root, "v1"), build_model(root, "v2"), build_model(root, "v3")
    has2 = bool(M2["full"] and M2["random"])
    has3 = bool(M3["full"] and M3["random"] and M3["full"]["scope"] == "full")
    pairs = (find_audio_pairs(root, M3["d"]["found"], M3["methods"], "v3") if has3 else []) \
        + (find_audio_pairs(root, M2["d"]["found"], M2["methods"], "v2") if has2 else []) \
        + find_audio_pairs(root, M1["d"]["found"], M1["methods"], "v1")
    pairs = choose_players(pairs, M2 if has2 else None, M3=M3 if has3 else None)
    written = copy_audio(pairs, out) if (audio and pairs) else []
    page = render_page(root, written)
    path = out / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def machine_hosts() -> int:
    import os

    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True, check=False)
    return len([x for x in r.stdout.split() if x.strip() and int(x) != os.getpid()])


def cmd_render_examples(a) -> int:
    n = machine_hosts()
    if n > 0:
        print(f"refusing: {n} native_host process(es) from other jobs are running. Run this when Native is free.",
              file=sys.stderr)
        return 3
    root = ROOT
    set_rel = ATTEMPTS[a.attempt]["set_rel"]
    run_dir = root / set_rel / "runs" / a.run
    items = load_items(run_dir)
    if not items:
        print(f"no item files in {run_dir}", file=sys.stderr)
        return 2
    ids = [x.strip() for x in a.items.split(",") if x.strip()] if a.items else sorted(items)[:6]
    missing = [i for i in ids if i not in items]
    if missing:
        print(f"items not in {a.run}: {missing}", file=sys.stderr)
        return 2
    methods = [x.strip() for x in a.methods.split(",") if x.strip()]
    bad = [m for m in methods if m not in ("ours", "middle")]
    if bad:
        print(f"methods {bad}: random walk did not save its settings; use ours,middle", file=sys.stderr)
        return 2
    import soundfile as sf
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL

    man = json.loads((root / set_rel / "manifest.json").read_text())
    labels = {x["id"]: x for x in json.loads((root / set_rel / "labels.json").read_text())}
    catalog = load_catalog()
    blocks = {}
    for c in S.CATEGORIES:
        for m in man["pool"][c]:
            blocks[m] = S.Block(m, c, S.free_params(m, NL.template_for(m), catalog, c), catalog)
    engine = S.make_engine()
    out = root / OUT_REL / "rendered" / a.run
    try:
        for i in ids:
            lab, it = labels[i], items[i]
            dry = sf.read(str(root / lab["di"]["C"]["file"]), dtype="float32")[0][: int(man["clip_seconds"] * SR)]
            engine.load(blocks[lab["model"]].with_values(lab["values"]))
            target = np.asarray(engine.render(dry), np.float32)
            for meth in methods:
                if meth == "ours":
                    model, vals = it.get("pred_model"), it.get("pred_values")
                else:
                    model = (it.get("all") or {}).get("pred_model")
                    vals = S.mid_values(blocks[model]) if model in blocks else None
                if model not in blocks or vals is None:
                    print(f"{i}/{meth}: no saved guess, skipped")
                    continue
                engine.load(blocks[model].with_values(vals))
                guess = np.asarray(engine.render(dry), np.float32)
                d = out / meth
                d.mkdir(parents=True, exist_ok=True)
                sf.write(str(d / f"{i}_target.wav"), target, SR, subtype="FLOAT")
                sf.write(str(d / f"{i}_recovered.wav"), guess, SR, subtype="FLOAT")
                print(f"{i}/{meth}: {pretty_model(lab['model'])} -> {pretty_model(model)} written")
    finally:
        engine.close()
    print(f"done: run `python scripts/gate2_report.py build` to add them to the report ({out})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="write reports/local/gate2/index.html (default)")
    b.add_argument("--root", type=Path, default=ROOT)
    b.add_argument("--out", type=Path, default=None)
    b.add_argument("--no-audio", action="store_true")
    cli_result.add_result_arg(b)
    r = sub.add_parser("render-examples", help="re-render target and guess with Native (refuses while Native is busy)")
    r.add_argument("--attempt", choices=sorted(ATTEMPTS), default="v1", help="v1 = synthetic-v1 (default), v2, v3")
    r.add_argument("--run", required=True, help="run folder tag under cache/<set>/runs")
    r.add_argument("--items", default="", help="comma list of item ids (default: first 6 of the run)")
    r.add_argument("--methods", default="ours,middle", help="comma list from ours,middle")
    a = ap.parse_args(argv)
    if a.cmd == "render-examples":
        return cmd_render_examples(a)
    root = getattr(a, "root", ROOT)
    with cli_result.scope(a, tool="gate2_report build", inputs={"ledger": root / "ledger" / "runs.csv"}) as res:
        path = build(root, getattr(a, "out", None), audio=not getattr(a, "no_audio", False))
        print(path)
        res.outputs.update({"index_html": path, "out_dir": path.parent})
        audio = sorted(p for p in path.parent.glob("*") if p.suffix in (".wav", ".flac", ".mp3"))
        res.metrics.update({"audio_files": len(audio), "audio": getattr(a, "no_audio", False) is False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
