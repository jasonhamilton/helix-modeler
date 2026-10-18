"""Compare run directories on synthetic-devgm (T143): counts by category family and half.

    python scripts/gm_compare.py TAG [TAG ...] [--half tune|held|all]

Model / category correct on mod, drive, amp; settings MAE on right-model items (the item files' own `param_mae`
is not stored, so it is recomputed from `pred_values` against the label values and ranges, level knobs
Output/Volume/Level/Master left out as in the run summary); mean renders per item.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEVEL = {"Output", "Volume", "Level", "Master", "Channel Volume"}


def load(tag: str, set_name: str = "synthetic-devgm") -> dict[str, dict]:
    d = ROOT / "cache" / set_name / "runs" / tag
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("i[0-9][0-9][0-9].json"))}


def mae(item: dict, lab: dict) -> float | None:
    pv, vals, rng = item.get("pred_values") or {}, lab["values"], lab["ranges"]
    errs = []
    for n, (lo, hi) in rng.items():
        if n in LEVEL or n not in pv or hi <= lo:
            continue
        errs.append(abs(float(pv[n]) - float(vals[n])) / (hi - lo))
    return sum(errs) / len(errs) if errs else None


def table(tags: list[str], half: str, set_name: str = "synthetic-devgm") -> dict:
    labels = {x["id"]: x for x in json.loads((ROOT / "cache" / set_name / "labels.json").read_text())}
    out = {}
    for tag in tags:
        runs = load(tag, set_name)
        ids = [i for i in runs if (half == "all" or (half == "tune") == (int(i[1:]) < 36))]
        row = {"items": len(ids)}
        for fam in ("mod", "drive", "amp", "mod+drive", "all"):
            sel = [i for i in ids if fam == "all" or labels[i]["category"] in fam.split("+")]
            m = sum(bool(runs[i].get("model_ok")) for i in sel)
            c = sum(bool(runs[i].get("category_ok")) for i in sel)
            row[fam] = {"n": len(sel), "model": m, "category": c}
        maes = [mae(runs[i], labels[i]) for i in ids if runs[i].get("model_ok")]
        maes = [x for x in maes if x is not None]
        row["mae_right_model"] = round(sum(maes) / len(maes), 4) if maes else None
        row["renders_mean"] = round(sum(runs[i].get("renders", 0) for i in ids) / max(len(ids), 1), 1)
        row["errors"] = sum(1 for i in ids if runs[i].get("error"))
        out[tag] = row
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tags", nargs="+")
    ap.add_argument("--half", choices=("tune", "held", "all"), default="all")
    ap.add_argument("--set", default="synthetic-devgm")
    a = ap.parse_args(argv)
    print(json.dumps(table(a.tags, a.half, a.set), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
