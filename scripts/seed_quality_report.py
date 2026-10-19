"""T162: aggregate the seed-quality grid (cache/synthetic-devfam/runs/t162{a,b}_*) into cells, paired differences
against the unseeded hybrid at the same budget, and a markdown table. Diagnostic only. Writes
reports/t162/cells_<cond>.json and prints the table; `--ledger` appends kind=search rows."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "cache" / "synthetic-devfam" / "runs"
LEVEL = {"Output", "Volume", "Level"}
N_BOOT = 4000


def load_cell(d: Path, n: int | None = None) -> dict[str, dict]:
    """Item results in a run dir; `n` keeps only the first n (the pilot's items; extra finished items are ignored)."""
    files = sorted(d.glob("i*.json"))
    return {p.stem: json.loads(p.read_text()) for p in (files[:n] if n else files)}


def item_mae(r: dict) -> float | None:
    if not r.get("model_ok"):
        return None
    e = {n: v for n, v in r.get("param_abs_err", {}).items() if n not in LEVEL}
    return float(np.mean(list(e.values()))) if e else None


def cell_stats(res: dict[str, dict]) -> dict:
    ok = [r for r in res.values() if "error" not in r]
    maes = [m for m in (item_mae(r) for r in ok) if m is not None]
    fam = {}
    for c in ("mod", "reverb", "delay", "amp"):
        rs = [r for r in ok if r["category"] == c]
        fam[c] = float(np.mean([r["model_ok"] for r in rs])) if rs else None
    out = {"items": len(res), "errors": len(res) - len(ok),
           "model_acc": float(np.mean([r["model_ok"] for r in ok])),
           "category_acc": float(np.mean([r["category_ok"] for r in ok])),
           "mae": float(np.mean(maes)) if maes else None, "n_mae": len(maes),
           "renders": float(np.mean([r.get("renders", 0) for r in ok])),
           "wall_s": float(np.mean([r.get("wall_s", 0) for r in ok])), "by_family_model_acc": fam}
    out["true_refined_but_lost"] = int(sum(1 for r in ok if not r["model_ok"] and r["model"] in r.get("refined", {})))
    sd = [r["seed"] for r in ok if "seed" in r]
    if sd:
        out["seed_model_acc"] = float(np.mean([s["model_ok"] for s in sd]))
        out["seed_refined"] = float(np.mean([s["refined"] for s in sd]))
        out["seed_knob_mae"] = float(np.nanmean([s["mae"] for s in sd]))
    return out


def paired(a: dict, b: dict, key: str, rng) -> tuple[float, float, float] | None:
    """mean(a - b) with a 95 % bootstrap interval over items present in both; key 'model_ok'|'category_ok'|'mae'."""
    ids = sorted(set(a) & set(b))
    if key == "mae":
        ids = [i for i in ids if item_mae(a[i]) is not None and item_mae(b[i]) is not None]
        d = np.array([item_mae(a[i]) - item_mae(b[i]) for i in ids])
    else:
        d = np.array([float(a[i][key]) - float(b[i][key]) for i in ids if "error" not in a[i] and "error" not in b[i]])
    if len(d) < 3:
        return None
    bs = rng.choice(d, (N_BOOT, len(d))).mean(axis=1)
    return float(d.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def parse(tag: str):
    m = re.match(r"t162([ab])_(unseeded|rw|p([\d.]+)(?:st)?_s([\d.]+))_b(\d+)$", tag)
    if not m:
        return None
    cond, kind, p, s, b = m.groups()
    strat = "st_" in tag
    return cond, ("unseeded" if kind == "unseeded" else "random" if kind == "rw" else "seedst" if strat else "seed"), \
        (float(p) if p else None), (float(s) if s else None), int(b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cond", choices=("a", "b"))
    ap.add_argument("--ledger", action="store_true")
    ap.add_argument("--n", type=int, default=None, help="use only the first N items of every cell")
    a = ap.parse_args()
    rng = np.random.default_rng(162)
    cells = []
    data = {}
    for d in sorted(glob.glob(str(RUNS / f"t162{a.cond}_*"))):
        pr = parse(Path(d).name)
        if pr is None:
            continue
        res = load_cell(Path(d), a.n)
        if not res:
            continue
        cond, kind, p, s, b = pr
        data[(kind, p, s, b)] = res
        cells.append({"tag": Path(d).name, "kind": kind, "p": p, "sigma": s, "budget": b, **cell_stats(res)})
    for c in cells:
        base = data.get(("unseeded", None, None, c["budget"]))
        if base is not None and c["kind"] not in ("unseeded",):
            res = data[(c["kind"], c["p"], c["sigma"], c["budget"])]
            for k, key in (("d_model", "model_ok"), ("d_cat", "category_ok"), ("d_mae", "mae")):
                c[k] = paired(res, base, key, rng)
    (ROOT / "reports" / "t162").mkdir(parents=True, exist_ok=True)
    (ROOT / "reports" / "t162" / f"cells_{a.cond}.json").write_text(json.dumps(cells, indent=1))
    order = {"random": 0, "unseeded": 1, "seed": 2, "seedst": 3}
    cells.sort(key=lambda c: (c["budget"], order[c["kind"]], -(c["p"] or 0), c["sigma"] or 0))

    def f(x, n=2):
        return "-" if x is None else f"{x:.{n}f}"

    def d(x):
        return "" if not x else f" ({x[0]:+.2f} [{x[1]:+.2f}, {x[2]:+.2f}])"

    print("| budget | arm | p | sigma | model acc (vs unseeded) | category acc | MAE (n) | seed model acc | seed in refine | renders | s/item |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for c in cells:
        print(f"| {c['budget']} | {c['kind']} | {f(c['p'])} | {f(c['sigma'])} | {f(c['model_acc'])}{d(c.get('d_model'))} | "
              f"{f(c['category_acc'])}{d(c.get('d_cat'))} | {f(c['mae'], 3)} ({c['n_mae']}){d(c.get('d_mae'))} | "
              f"{f(c.get('seed_model_acc'))} | {f(c.get('seed_refined'))} | {c['renders']:.0f} | {c['wall_s']:.0f} |")
    if a.ledger:
        env = {**os.environ, "TONEMATCH_AGENT": "search"}
        ds = "synthetic-devfam-seedcorrupt" + ("-diffdi" if a.cond == "b" else "")
        for c in cells:
            lab = c["kind"] if not c["kind"].startswith("seed") else f"p{c['p']}{'st' if c['kind'] == 'seedst' else ''}_s{c['sigma']}"
            note = (f"T162 PILOT n={c['items']} diagnostic, not gate evidence; current Gate 2 score used blind (control); arm {lab}; budget {c['budget']}; "
                    f"category_acc {c['category_acc']:.3f}; MAE {f(c['mae'], 3)} on {c['n_mae']} right-model items; "
                    f"{'DIFFERENT-DI CONTROL' if a.cond == 'b' else 'known DI'}; admitted gate2-known-di-v3 weights")
            for metric, val in (("model_acc", c["model_acc"]), ("param_mae", c["mae"])):
                if val is None:
                    continue
                subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search",
                                "--engine", "native", "--dataset", ds, "--metric", f"{metric}_{lab}_b{c['budget']}",
                                "--value", f"{val:.4f}", "--renders", str(round(c["renders"] * c["items"])),
                                "--wall-clock-s", str(round(c["wall_s"] * c["items"])), "--notes", note],
                               check=True, env=env, cwd=ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
