#!/usr/bin/env python3
"""synthetic-benchmark: manifest, label generator, metrics and cost estimate for synthetic-v1.

Spec: .claude/skills/synthetic-benchmark, docs/notes/synthetic-benchmark-design.md.
v1 design constraints (Gate 1 findings): Native renders only chains with a GUI-captured template
(16 knobs + 16 switches vary), the Stomp varies models in its loaded layout one render at a time.

This file does NOT render; `--render` and `--ci` point to scripts/synthetic_v1.py (exit 2) (the rendered
synthetic-v1 set, T073). `--generate` writes labels only (to cache/synthetic/, gitignored) (no audio, no engine). `--plan` prints the
render/wall-clock estimate. Rendering and the search loop are wired in later (T022/T023); until then
`--render` and `--ci` exit 2 and write no ledger row. Nothing here writes a benchmark ledger row.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Real manifest and labels carry Line 6 model ids, so they go to gitignored cache/. Only the
# EXAMPLE_ form (see example_manifest) may live under fixtures/.
SET_DIR = ROOT / "cache" / "synthetic"
EXAMPLE_DIR = ROOT / "fixtures" / "synthetic"
LEDGER = ROOT / "ledger" / "runs.csv"
VOID = ROOT / "ledger" / "void.csv"  # timestamp,metric,reason; rows declared void in docs/DECISIONS.md

# Switchable categories (amp and cab stay on: every chain here is full-rig).
SWITCHABLE = ("comp", "drive", "eq", "mod", "delay", "reverb")
# Prior probability a switchable block is on in a plausible chain (to be refit on community presets).
P_ON = {"comp": 0.30, "drive": 0.50, "eq": 0.40, "mod": 0.35, "delay": 0.60, "reverb": 0.70}
# Knob budget per category (sum = 16, the Native automation limit).
KNOBS = {"comp": 1, "drive": 2, "amp": 5, "eq": 2, "mod": 2, "delay": 2, "reverb": 2}

# Native chain structures to capture in the GUI (ids from the 3.82 catalog; verify on capture).
STRUCTURES = {
    "A_clean": {"comp": "HD2_CompressorDeluxeComp", "drive": "HD2_DistKinkyBoost",
                "amp": "HD2_AmpUSDoubleVib", "eq": "HD2_EQParametric", "mod": "HD2_Chorus",
                "delay": "HD2_DelayTransistorTape", "reverb": "HD2_ReverbPlate"},
    "B_crunch": {"comp": "HD2_CompressorRedSqueeze", "drive": "HD2_DistScream808",
                 "amp": "HD2_AmpBrit2204", "eq": "HD2_EQSimple3Band",
                 "mod": "HD2_PhaserScriptModPhase", "delay": "HD2_DelaySimpleDelay",
                 "reverb": "HD2_ReverbRoom"},
    "C_highgain": {"comp": "HD2_CompressorLAStudioComp", "drive": "HD2_DistHorizonDrive",
                   "amp": "HD2_AmpRevvGenRed", "eq": "HD2_EQParametric",
                   "mod": "HD2_TremoloOpticalTrem", "delay": "HD2_DelayDuckedDelay",
                   "reverb": "HD2_ReverbHall"},
}
# Stomp model pools (6 per category keeps chance accuracy near 1/6; amp+cab move as a pair).
STOMP_POOL = {
    "comp": ["HD2_CompressorDeluxeComp", "HD2_CompressorRedSqueeze", "HD2_CompressorKinkyComp",
             "HD2_CompressorLAStudioComp", "HD2_Compressor3BandComp", "HD2_CompressorAutoSwell"],
    "drive": ["HD2_DistKinkyBoost", "HD2_DistScream808", "HD2_DistMinotaur", "HD2_DistTeemah",
              "HD2_DistHorizonDrive", "HD2_DistDerangedMaster"],
    "amp": ["HD2_AmpUSDoubleVib", "HD2_AmpBrit2204", "HD2_AmpTweedBluesBrt", "HD2_AmpRevvGenRed",
            "HD2_AmpPVPanama", "HD2_AmpSoupPro"],
    "eq": ["HD2_EQParametric", "HD2_EQSimple3Band", "HD2_EQGraphic10Band", "HD2_EQLowCutHighCut",
           "HD2_EQLowShelfHighShelf", "HD2_EQSimpleTilt"],
    "mod": ["HD2_Chorus", "HD2_Chorus70sChorus", "HD2_PhaserScriptModPhase",
            "HD2_PhaserUbiquitousVibe", "HD2_FlangerGrayFlanger", "HD2_TremoloOpticalTrem"],
    "delay": ["HD2_DelayTransistorTape", "HD2_DelaySimpleDelay", "HD2_DelayDuckedDelay",
              "HD2_DelayAdriaticDelay", "HD2_DelayBucketBrigade", "HD2_DelaySweepEcho"],
    "reverb": ["HD2_ReverbRoom", "HD2_ReverbPlate", "HD2_ReverbHall"],
}
# Measured seconds (ledger 2026-10-14): Native reset render, Stomp render, Stomp model change.
COST = {"native_render_s": 1.883, "stomp_render_s": 2.682, "stomp_model_change_s": 0.658}
# Density = guitar level against the rest of the mix in dB; None means the solo guitar.
DENSITY_DB = {"d0": None, "d1": 6.0, "d2": 0.0, "d3": -6.0}

DEFAULT_MANIFEST = {
    "name": "synthetic-v1", "version": 1, "seed": 20261014,
    "sample_rate": 48000, "clip_seconds": 2.0,
    "native": {"structures": STRUCTURES, "items_per_structure": 40, "knobs": KNOBS,
               "p_on": P_ON},
    "stomp": {"items": 60, "pool": STOMP_POOL, "p_on": P_ON, "p_model_change": 0.5,
              "layout": "same 8-block layout as structure A (comp drive amp cab eq mod delay reverb)"},
    "di_clips": {"source": "dry-signal library (EGDB subset), 2 clips per item: fit and held-out notes",
                 "ids": []},
    "mix": {"stems_source": "TBD: multitrack set chosen by the human; local only",
            "densities": DENSITY_DB, "v1_default_density": "d0"},
    "perturbation": {"neighbours_per_item": 24, "param_step": 0.15},
    "tolerance": {"block_f1": 0.02, "model_acc": 0.02, "param_mae": 0.01, "rank1_rate": 0.02},
    "thresholds_proposed": {
        "block_f1": 0.85, "model_acc_native": 0.80, "model_acc_stomp": 0.60,
        "param_mae": 0.15, "rank1_rate_kit": 0.95, "rank1_rate_term": 0.90},
}


def build_manifest() -> dict:
    return json.loads(json.dumps(DEFAULT_MANIFEST))


def example_manifest() -> dict:
    """Committable shape-only copy: every Line 6 model id replaced by a stable EXAMPLE_ id."""
    ids: dict[str, str] = {}

    def walk(x):
        if isinstance(x, str) and x.startswith("HD2_"):
            return ids.setdefault(x, f"EXAMPLE_{len(ids):02d}")
        if isinstance(x, dict):
            return {walk(k): walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(build_manifest())


def _sample_params(rng: np.random.Generator, n: int) -> list[float]:
    # Beta(2,2) pulls toward mid-range like real presets while still covering the knob.
    return [round(float(v), 3) for v in rng.beta(2.0, 2.0, size=n)]


def _sample_switches(rng: np.random.Generator, p_on: dict) -> dict:
    return {c: bool(rng.random() < p_on[c]) for c in SWITCHABLE}


def generate_items(manifest: dict) -> list[dict]:
    """Deterministic label set from the manifest seed. No audio, no engine."""
    rng = np.random.default_rng(manifest["seed"])
    items: list[dict] = []
    nat = manifest["native"]
    for sid, models in nat["structures"].items():
        for _ in range(nat["items_per_structure"]):
            items.append({
                "id": f"n{len(items):04d}", "engine": "native", "structure": sid,
                "models": dict(models), "enabled": _sample_switches(rng, nat["p_on"]),
                "knobs": _sample_params(rng, sum(nat["knobs"].values())),
                "clip_seed": int(rng.integers(1 << 31)),
            })
    stm = manifest["stomp"]
    for _ in range(stm["items"]):
        models = {c: stm["pool"][c][int(rng.integers(len(stm["pool"][c])))] for c in stm["pool"]}
        items.append({
            "id": f"s{len(items):04d}", "engine": "stomp", "structure": "A_layout",
            "models": models, "enabled": _sample_switches(rng, stm["p_on"]),
            "knobs": _sample_params(rng, sum(nat["knobs"].values())),
            "clip_seed": int(rng.integers(1 << 31)),
        })
    return items


# ---- metrics (pure functions; truth and prediction are items in the shape above) ----------

def block_f1(truth: list[dict], pred: list[dict]) -> float:
    """Micro-F1 over the switchable categories being on. Defined as 1.0 when nothing is on in both."""
    tp = fp = fn = 0
    for t, p in zip(truth, pred, strict=True):
        for c in SWITCHABLE:
            tp += t["enabled"][c] and p["enabled"][c]
            fp += (not t["enabled"][c]) and p["enabled"][c]
            fn += t["enabled"][c] and (not p["enabled"][c])
    return 1.0 if tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn)


def model_acc(truth: list[dict], pred: list[dict]) -> float:
    """Fraction of truly enabled slots (amp always) whose predicted model id is right."""
    hit = tot = 0
    for t, p in zip(truth, pred, strict=True):
        for c in ("amp", *SWITCHABLE):
            if c == "amp" or t["enabled"][c]:
                tot += 1
                hit += t["models"][c] == p["models"][c]
    return hit / tot if tot else 1.0


def _knob_owner(knobs: dict) -> list[str]:
    return [c for c, n in knobs.items() for _ in range(n)]


def param_mae(truth: list[dict], pred: list[dict], knobs: dict | None = None) -> float:
    """Mean absolute error of normalized knobs (0..1) on truly enabled blocks whose model is right.
    A wrong model makes the knob meaning differ, so those knobs are scored in model_acc, not here."""
    owner = _knob_owner(knobs or KNOBS)
    err: list[float] = []
    for t, p in zip(truth, pred, strict=True):
        for i, c in enumerate(owner):
            if (c == "amp" or t["enabled"][c]) and t["models"][c] == p["models"][c]:
                err.append(abs(t["knobs"][i] - p["knobs"][i]))
    return float(np.mean(err)) if err else float("nan")


def rank1_rate(true_scores: list[float], neighbour_scores: list[list[float]]) -> float:
    """Fraction of items where the true preset's loss is strictly below every neighbour's."""
    ok = [all(ts < ns for ns in nb) for ts, nb in zip(true_scores, neighbour_scores, strict=True)]
    return float(np.mean(ok)) if ok else float("nan")


def baseline_all_on_f1(p_on: dict = P_ON) -> float:
    """Expected block F1 of the trivial predictor 'every switchable block on'."""
    tp = sum(p_on.values())
    n = len(p_on)
    return 2 * tp / (tp + n)


def baseline_mid_mae() -> float:
    """Expected param MAE of always guessing 0.5 when truth ~ Beta(2,2): E|x-0.5| = 3/16."""
    return 3 / 16


# ---- cost -------------------------------------------------------------------------------

def plan(manifest: dict, budget: int, ci_items: int = 6, ci_budget: int = 40) -> dict:
    nat_items = manifest["native"]["items_per_structure"] * len(manifest["native"]["structures"])
    stm_items = manifest["stomp"]["items"]
    n_cat = len(manifest["stomp"]["pool"])
    expected_changes = (1 + (n_cat - 1) * manifest["stomp"]["p_model_change"]) * 5 / 6
    nr, sr, mc = COST["native_render_s"], COST["stomp_render_s"], COST["stomp_model_change_s"]
    out = {
        "native_items": nat_items, "stomp_items": stm_items,
        # truth: fit clip + held-out clip per item; neighbours for loss-trust on the fit clip
        "native_truth_renders": nat_items * 2,
        "native_loss_trust_renders": nat_items * manifest["perturbation"]["neighbours_per_item"],
        "stomp_truth_renders": stm_items * 2,
        "native_recovery_renders_per_pass": nat_items * budget,
        "native_ci_renders": ci_items * ci_budget,
        "stomp_recovery_renders_10_items": 10 * budget,
    }
    out["native_truth_s"] = out["native_truth_renders"] * nr
    out["native_loss_trust_s"] = out["native_loss_trust_renders"] * nr
    out["stomp_truth_s"] = out["stomp_truth_renders"] * sr + stm_items * 2 * expected_changes * mc
    out["native_recovery_s"] = out["native_recovery_renders_per_pass"] * nr
    out["native_ci_s"] = out["native_ci_renders"] * nr
    out["stomp_recovery_10_items_s"] = out["stomp_recovery_renders_10_items"] * (sr + 2 * mc)
    return out


def _fmt_s(s: float) -> str:
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.1f} min"


# ---- regression check against the ledger ----------------------------------------------

def load_void(path: Path | None = None) -> set[tuple[str, str]]:
    path = path or VOID
    if not path.exists():
        return set()
    with path.open() as f:
        return {(r["timestamp"], r["metric"]) for r in csv.DictReader(f)}


def regressions(current: dict[str, float], dataset: str, engine: str, tol: dict) -> list[str]:
    """Compare against the previous ledger row with the same kind, dataset, engine and metric."""
    if not LEDGER.exists():
        return []
    prev: dict[str, float] = {}
    void = load_void()
    with LEDGER.open() as f:
        for r in csv.DictReader(f):
            if (r["timestamp"], r["metric"]) in void:
                continue
            if r["kind"] == "benchmark" and r["dataset"] == dataset and r["engine"] == engine:
                try:
                    prev[r["metric"]] = float(r["value"])
                except ValueError:
                    pass
    out = []
    for m, v in current.items():
        if m in prev:
            lower_better = m in ("param_mae", "loss_heldout")
            drop = (v - prev[m]) if lower_better else (prev[m] - v)
            if drop > tol.get(m, 0.0):
                out.append(f"REGRESSION {dataset}/{engine} {m}: {prev[m]:.4f} -> {v:.4f}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generate", action="store_true", help="write manifest.json and labels.json (no audio)")
    ap.add_argument("--example", action="store_true",
                    help="write the committable EXAMPLE_-id manifest to fixtures/synthetic/")
    ap.add_argument("--plan", action="store_true", help="print render and wall-clock estimates")
    ap.add_argument("--render", action="store_true", help="moved to scripts/synthetic_v1.py (exits 2)")
    ap.add_argument("--ci", action="store_true", help="moved to scripts/synthetic_v1.py (exits 2)")
    ap.add_argument("--budget", type=int, default=150)
    ap.add_argument("--out", type=Path, default=SET_DIR)
    a = ap.parse_args(argv)
    if a.render or a.ci:
        print("render/CI path moved: the rendered set is synthetic-v1 (T073). Run "
              "`python scripts/synthetic_v1.py run ...` (needs Native; see docs/BENCHMARKING.md).", file=sys.stderr)
        return 2
    m = build_manifest()
    if a.example:
        EXAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        (EXAMPLE_DIR / "manifest.example.json").write_text(json.dumps(example_manifest(), indent=2) + "\n")
        print(f"wrote {EXAMPLE_DIR / 'manifest.example.json'}")
    if a.generate:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
        items = generate_items(m)
        (a.out / "labels.json").write_text(json.dumps(items) + "\n")
        print(f"wrote {len(items)} labelled items to {a.out} (no audio rendered)")
    if a.plan or not (a.generate or a.example):
        for k, v in plan(m, a.budget).items():
            print(f"{k}: {_fmt_s(v) if k.endswith('_s') else v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
