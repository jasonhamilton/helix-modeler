"""Replay the final order of a synthetic-devgm run under other policy settings (T143; no renders).

The narrowing and the refine are fixed by the run; only the order of the refined models (prior, rank swap) is
recomputed, so this answers "what would the pick have been" for final-order changes, not for narrowing changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.search.family_policy import Policy, final_order
from tonematch.search.model_choice import ModelScore
from tonematch.search.specialists import Dispatch


def replay(run: str, pol: Policy, set_name: str = "synthetic-devgm", half: str = "tune") -> dict:
    man = json.loads((ROOT / "cache" / set_name / "manifest.json").read_text())
    cat = {m: c for c, p in man["pool"].items() for m in p}
    labels = {x["id"]: x for x in json.loads((ROOT / "cache" / set_name / "labels.json").read_text())}
    d = ROOT / "cache" / set_name / "runs" / run
    res = {"mod": [0, 0], "drive": [0, 0], "amp": [0, 0]}
    picks = {}
    for f in sorted(d.glob("i[0-9][0-9][0-9].json")):
        j = json.loads(f.read_text())
        n = int(j["id"][1:])
        if (n < 36) != (half == "tune"):
            continue
        disp = Dispatch(hints=dict(j["dispatch"]["hints"]), shortlist=list(j["dispatch"].get("shortlist", [])))
        scores = [ModelScore(m, cat[m], screen=s, refined=j["refined"].get(m)) for m, s in j["screen"].items()]
        ref, others = final_order(scores, disp, pol)
        pick = (ref or others)[0].model
        lab = labels[j["id"]]
        picks[j["id"]] = pick
        res[lab["category"]][1] += 1
        res[lab["category"]][0] += pick == lab["model"]
    return {"model_correct": res, "picks": picks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--tau", type=float, default=0.0)
    ap.add_argument("--gate", type=float, default=0.0)
    ap.add_argument("--rank-slack", type=float, default=0.0)
    ap.add_argument("--half", default="tune")
    a = ap.parse_args(argv)
    r = replay(a.run, Policy(tau=a.tau, gate=a.gate, rank_slack=a.rank_slack), half=a.half)
    print(json.dumps(r["model_correct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
