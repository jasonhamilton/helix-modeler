"""Paired summary of the T147 mod-prior check on synthetic-devmod2: base (attempt 2) vs prior, bars 1-5."""
import json
import sys
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "cache" / "synthetic-devmod2" / "runs"


def load(tag):
    return {p.stem: json.loads(p.read_text()) for p in sorted((D / tag).glob("i[0-9][0-9][0-9].json"))}


def main(a="dm2_base", b="dm2_prior"):
    A, B = load(a), load(b)
    ids = sorted(set(A) & set(B))
    cats = sorted({A[i]["category"] for i in ids})

    def n(R, key, c=None):
        return sum(bool(R[i][key]) for i in ids if c is None or R[i]["category"] == c)

    out = {"items": len(ids), "cats": {}}
    print(f"items {len(ids)} (base {len(A)}, prior {len(B)})")
    print(f"{'category':8} {'items':>5} {'model base':>10} {'prior':>6} {'cat base':>9} {'prior':>6}")
    for c in cats + [None]:
        k = sum(1 for i in ids if c is None or A[i]["category"] == c)
        row = (n(A, "model_ok", c), n(B, "model_ok", c), n(A, "category_ok", c), n(B, "category_ok", c))
        print(f"{c or 'ALL':8} {k:5d} {row[0]:10d} {row[1]:6d} {row[2]:9d} {row[3]:6d}")
        out["cats"][c or "all"] = {"items": k, "model_base": row[0], "model_prior": row[1], "cat_base": row[2], "cat_prior": row[3]}
    ra = sum(A[i]["renders"] for i in ids) / len(ids)
    rb = sum(B[i]["renders"] for i in ids) / len(ids)
    def mae(R):
        v = [R[i]["param_mae_item"] for i in ids if R[i]["model_ok"]]
        return sum(v) / len(v)
    print(f"renders/item base {ra:.1f} prior {rb:.1f} ratio {rb / ra:.3f}; MAE right-model base {mae(A):.3f} prior {mae(B):.3f}")
    m = out["cats"]["mod"]
    nonmod = {c: v["model_base"] - v["model_prior"] for c, v in out["cats"].items() if c not in ("mod", "all")}
    bars = {
        "1 mod model above base": m["model_prior"] > m["model_base"],
        "2 mod category above base": m["cat_prior"] > m["cat_base"],
        "3 all-model not lower": out["cats"]["all"]["model_prior"] >= out["cats"]["all"]["model_base"],
        "4 no non-mod loses >1": max(nonmod.values()) <= 1,
        "5 renders <= 1.05x": rb <= 1.05 * ra,
    }
    print("non-mod losses (base - prior):", nonmod)
    for k, v in bars.items():
        print(("MET   " if v else "NOT   ") + k)
    print("ADOPT" if all(bars.values()) else "NOT ADOPTED")
    ch = [(i, A[i]["category"], A[i]["model"], A[i]["pred_model"], B[i]["pred_model"]) for i in ids if A[i]["model_ok"] != B[i]["model_ok"]]
    for c in ch:
        print("changed", *c)
    out.update(renders_base=ra, renders_prior=rb, mae_base=mae(A), mae_prior=mae(B), bars=bars, nonmod_loss=nonmod)
    return out


if __name__ == "__main__":
    main(*sys.argv[1:3])
