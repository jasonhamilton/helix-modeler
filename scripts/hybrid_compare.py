"""Compare run directories of synthetic-v1 on dev16 (items i000-i015 ONLY): model, category, parameter
MAE (own hits and hits shared with the reference), held-out loss, rank-1. Dev numbers, not gate evidence.

    python scripts/hybrid_compare.py v3_base_random v4b_dev16_seed h1_f50_k5
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "cache" / "synthetic-v1" / "runs"
DEV16 = [f"i{n:03d}" for n in range(16)]


def load(tag: str) -> dict[str, dict]:
    out = {}
    for i in DEV16:
        f = RUNS / tag / f"{i}.json"
        if f.exists():
            out[i] = json.loads(f.read_text())
    return out


def mae(r: dict) -> float | None:
    e = r.get("param_abs_err")
    return sum(e.values()) / len(e) if r.get("model_ok") and e else None


def pooled(rs: list[dict]) -> float:
    """Mean over items of the item's parameter MAE (items with the right model only)."""
    v = [m for r in rs if (m := mae(r)) is not None]
    return sum(v) / len(v) if v else float("nan")


def search_renders(r: dict) -> float:
    sr = r.get("stage_renders") or {}
    return sum(v for v in sr.values() if isinstance(v, (int, float))) or float(r.get("draws", 0))


def main(tags: list[str]) -> None:
    runs = {t: load(t) for t in tags}
    print(f"{'run':24s} n  model cat   MAE(own)  heldout med  mean   rank1  search renders (mean, max)")
    for t, rs in runs.items():
        v = list(rs.values())
        n = len(v)
        ho = [r["loss_heldout"] for r in v if "loss_heldout" in r]
        r1 = [r["rank1"] for r in v if "rank1" in r]
        print(f"{t:24s} {n:2d} {sum(r.get('model_ok', 0) for r in v):2d}/{n:<2d} "
              f"{sum(r.get('category_ok', 0) for r in v):2d}/{n:<2d} {pooled(v):.3f}    "
              f"{st.median(ho):.3f}        {st.mean(ho):.3f}  "
              f"{(str(sum(r1)) + '/' + str(len(r1))) if r1 else '-':6s} "
              f"{st.mean(search_renders(r) for r in v):.0f} {max(search_renders(r) for r in v):.0f}")
    ref = tags[0]
    for t in tags[1:]:
        for other in tags:
            if other == t:
                continue
            both = [i for i in DEV16 if i in runs[t] and i in runs[other] and runs[t][i].get("model_ok")
                    and runs[other][i].get("model_ok")]
            a = pooled([runs[t][i] for i in both])
            b = pooled([runs[other][i] for i in both])
            print(f"  {t} vs {other}: both right {len(both)}, MAE {a:.3f} vs {b:.3f}")
    del ref
    print("per item (model ok / pred):")
    for i in DEV16:
        row = []
        for t in tags:
            r = runs[t].get(i)
            row.append("  -  " if r is None else f"{'ok' if r.get('model_ok') else 'NO'} {str(r.get('pred_model', '?'))[4:16]:12s}")
        any_r = next((runs[t][i] for t in tags if i in runs[t]), {})
        print(f"  {i} {any_r.get('category', '?'):6s} {str(any_r.get('model', ''))[4:20]:16s} | " + " | ".join(row))


if __name__ == "__main__":
    main(sys.argv[1:])
