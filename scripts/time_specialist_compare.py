#!/usr/bin/env python3
"""Compare two synthetic-devfam runs (general hybrid vs hybrid with the time specialist) on the delay and reverb items
(T122; bars in docs/DECISIONS.md 2026-10-17 "time-based specialist"). No Native.

  python scripts/time_specialist_compare.py --general time_general --special time_special [--ledger]

Reports, for all 48 items, the tuning half (even positions per category) and the held-out half (odd): model and category
accuracy, delay-vs-reverb confusions, parameter MAE on items both got right, median held-out loss, mean search renders, and the
bars. Items whose true preset renders silent (Native refuses) are unscoreable for both arms and are excluded from both and
counted.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(set_name: str, tag: str) -> dict[str, dict]:
    d = ROOT / "cache" / set_name / "runs" / tag
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("i*.json"))}


def halves(labels: list[dict]) -> dict[str, str]:
    out = {}
    for c in ("delay", "reverb"):
        ids = [x["id"] for x in labels if x["category"] == c]
        out.update({i: ("tune" if k % 2 == 0 else "heldout") for k, i in enumerate(ids)})
    return out


def audible_ids(set_name: str, ids: list[str]) -> set[str]:
    """Items whose true render differs audibly from the DI (null above -30 dB, rule B). A target that nulls against its own DI
    is a pass-through: every model that passes the signal through is as right as the true one."""
    from tonematch.metrics.presence import null_db

    out = set()
    for i in ids:
        f = ROOT / "cache" / "time-specialist" / "trust" / set_name / f"{i}.npz"
        if not f.exists():
            out.add(i)
            continue
        z = np.load(f)
        if np.any(z["renders"][0]) and null_db(z["renders"][0], z["dry"]) > -30.0:
            out.add(i)
    return out


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    ok = [r for r in rows if "error" not in r]
    conf = sum(1 for r in ok if r["category"] in ("delay", "reverb") and r.get("pred_category") in ("delay", "reverb")
               and r["pred_category"] != r["category"])
    mae = [r["param_mae_item"] for r in ok if r.get("model_ok") and np.isfinite(r.get("param_mae_item", np.nan))]
    ho = [r["loss_heldout"] for r in ok if "loss_heldout" in r]
    return {"n": n, "model_acc": float(np.mean([r["model_ok"] for r in rows])) if n else float("nan"),
            "model_n": int(sum(r["model_ok"] for r in rows)),
            "category_acc": float(np.mean([r["category_ok"] for r in rows])) if n else float("nan"),
            "category_n": int(sum(r["category_ok"] for r in rows)),
            "delay_reverb_confusions": conf, "param_mae": float(np.mean(mae)) if mae else float("nan"),
            "heldout_median": float(np.median(ho)) if ho else float("nan"),
            "renders": float(np.mean([r.get("renders", 0) for r in ok])) if ok else float("nan")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", default="synthetic-devfam")
    ap.add_argument("--general", default="time_general")
    ap.add_argument("--special", default="time_special")
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    labels = json.loads((ROOT / "cache" / a.set / "labels.json").read_text())
    half = halves(labels)
    g, s = load(a.set, a.general), load(a.set, a.special)
    # silent true render: both arms error with the same message -> unscoreable
    silent = {i for i in half if "silence" in str(g.get(i, {}).get("error", "")) or "silence" in str(s.get(i, {}).get("error", ""))}
    ids = [i for i in half if i in g and i in s and i not in silent]
    print(f"{len(ids)} items scored in both arms; {len(silent)} excluded (true render silent): {sorted(silent)}")
    out = {}
    aud = audible_ids(a.set, ids)
    print(f"{len(aud)} of {len(ids)} scored items are audible (true render not within -30 dB of the DI)")
    for name, sel in (("all", ids), ("tune", [i for i in ids if half[i] == "tune"]), ("heldout", [i for i in ids if half[i] == "heldout"]),
                      ("delay", [i for i in ids if g[i]["category"] == "delay"]), ("reverb", [i for i in ids if g[i]["category"] == "reverb"]),
                      ("audible", [i for i in ids if i in aud]), ("aud_tune", [i for i in ids if i in aud and half[i] == "tune"]),
                      ("aud_held", [i for i in ids if i in aud and half[i] == "heldout"]),
                      ("aud_delay", [i for i in ids if i in aud and g[i]["category"] == "delay"]),
                      ("aud_rev", [i for i in ids if i in aud and g[i]["category"] == "reverb"])):
        out[name] = {"general": stats([g[i] for i in sel]), "special": stats([s[i] for i in sel])}
    keys = ("model_n", "category_n", "delay_reverb_confusions", "param_mae", "heldout_median", "renders")
    print(f"{'subset':8s} {'n':>3s} " + " ".join(f"{k:>24s}" for k in keys))
    for name, v in out.items():
        for arm in ("general", "special"):
            r = v[arm]
            print(f"{name:8s} {r['n']:3d} " + " ".join(f"{r[k]:24.3f}" for k in keys) + f"   {arm}")
    # paired
    both = [(g[i]["model_ok"], s[i]["model_ok"]) for i in ids]
    print("paired model: special right & general wrong", sum(1 for x, y in both if y and not x), "; general right & special wrong",
          sum(1 for x, y in both if x and not y))

    def bars(sub: str) -> dict:
        G, S = out[sub]["general"], out[sub]["special"]
        return {"1 model +2/24": S["model_n"] - G["model_n"] >= 2 * (G["n"] / 24),
                "2 category not below, confusions not above": S["category_n"] >= G["category_n"] and S["delay_reverb_confusions"] <= G["delay_reverb_confusions"],
                "3 MAE within 0.02 and held-out within 10%": (S["param_mae"] - G["param_mae"] <= 0.02 or not np.isfinite(S["param_mae"] - G["param_mae"]))
                and S["heldout_median"] <= 1.10 * G["heldout_median"],
                "4 renders <= 1.05 x the general arm's": S["renders"] <= 1.05 * G["renders"]}

    for sub in ("heldout", "all", "aud_held", "audible"):
        print(f"bars on {sub}:", bars(sub))
    Path(ROOT / "cache" / a.set / "runs" / f"compare_{a.general}_vs_{a.special}.json").write_text(json.dumps(out, indent=1))
    if a.ledger:
        for sub, tag in (("all", "all46"), ("heldout", "heldout24"), ("audible", "audible38"), ("aud_held", "audible_heldout21")):
            for arm, label in (("general", "general hybrid"), ("special", "hybrid + time specialist")):
                r = out[sub][arm]
                for metric, val in (("model_acc", r["model_acc"]), ("block_f1", r["category_acc"]), ("param_mae", r["param_mae"]),
                                    ("loss_heldout_median", r["heldout_median"])):
                    if not np.isfinite(val):
                        continue
                    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
                                    "--dataset", "synthetic-devfam",
                                    "--metric", f"{metric}_time_{tag}_{'specialist' if arm == 'special' else 'general'}",
                                    "--value", f"{val:.4f}", "--renders", f"{r['renders']:.0f}",
                                    "--notes", f"{label}; {r['n']} delay+reverb items of synthetic-devfam ({sub}; {len(silent)} items with a silent true "
                                               f"render excluded from both arms); budget 300, admitted known-DI weights, f0.4 K4 scale0.9 level-free; "
                                               f"{r['delay_reverb_confusions']} delay-reverb confusions; dev, not gate evidence; T122; "
                                               f"held-out half run once with frozen code" + (f"; {a.note}" if a.note else "")],
                                   check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
