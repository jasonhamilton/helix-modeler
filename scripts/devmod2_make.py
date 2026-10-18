"""Build synthetic-devmod2 (T146, seed 20261022): 6 items in each of the 8 categories plus 12 more mod items (18 mod, 60 items).

The generator makes equal counts per category, so: (1) the full 8-category set, 6 per category; (2) a mod-only set of 12
with the SAME mod pool (taken from the full build_pools) and DI excerpts that avoid set (1) and every avoided set;
(3) the extras are appended as i048..i059. Avoided DI excerpts (positions only, never labels): synthetic-v1, v2, v3,
devfam, devgm, lindev (gain-trust: see TRUST).
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import synthetic_v1 as SV

SEED = 20261022
AVOID = ["synthetic-v1", "synthetic-v2", "synthetic-v3", "synthetic-devfam", "synthetic-devgm", "synthetic-lindev"]
TRUST = []  # gain-trust-v1 cannot be avoided: with its test split avoided the seed has no second passage for item 0; with all of it, 62 free excerpts are left. Overlap is reported instead.
_orig = SV.used_excerpts


def used(names):
    out = _orig([n for n in names if (ROOT / "cache" / n / "labels.json").exists()])
    for t in TRUST:
        mp = ROOT / "cache" / "loss-trust" / t / "manifest.json"
        if mp.exists():
            for it in json.loads(mp.read_text()).get("items", []):
                if it.get("split") != "test":
                    continue
                for d in it.get("di", {}).values():
                    out.append({"path": d["path"], "start_s": d["start_s"]})
    return out


SV.used_excerpts = used
NAME, EX = "synthetic-devmod2", "synthetic-devmod2-x"


def main():
    SV.set_paths(NAME)
    man = SV.generate(SEED, items_per_category=6, avoid_sets=AVOID, name=NAME)
    full_pools = SV.build_pools(SEED)
    SV.build_pools = lambda seed=SEED, library=None, catalog=None, tv=None, categories=None: {
        c: full_pools[c] for c in (categories or SV.CATEGORIES)}
    SV.set_paths(EX)
    SV.N_NEIGHBOURS = SV.N_NEAR + len(full_pools['mod']) - 1  # mod-only: no other-category neighbours to draw
    xman = SV.generate(SEED, items_per_category=12, categories=("mod",), avoid_sets=AVOID, name=EX)
    assert xman["pool"]["mod"] == man["pool"]["mod"]
    base, xd = ROOT / "cache" / NAME, ROOT / "cache" / EX
    labels = json.loads((base / "labels.json").read_text())
    xl = json.loads((xd / "labels.json").read_text())
    n0 = len(labels)
    for k, it in enumerate(xl):
        i = n0 + k
        it["id"] = f"i{i:03d}"
        for tag, d in it["di"].items():
            new = base / "di" / f"item{i:03d}_{tag}.wav"
            shutil.copy(ROOT / d["file"], new)
            d["file"] = str(new.relative_to(ROOT))
        labels.append(it)
    man["items"] = len(labels)
    man["item_categories"] = sorted({it["category"] for it in labels})
    man["items_per_category"] = 6
    man["mod_extra_items"] = len(xl)
    man["note"] = "T146: 6 per category plus 12 extra mod items (18 mod); extras from set synthetic-devmod2-x"
    (base / "labels.json").write_text(json.dumps(labels, indent=1))
    (base / "manifest.json").write_text(json.dumps(man, indent=1))
    shutil.rmtree(xd)
    from collections import Counter
    print(len(labels), Counter(it["category"] for it in labels), Counter(it["model"] for it in labels if it["category"] == "mod"))


main()
