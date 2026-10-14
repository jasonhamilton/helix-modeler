#!/usr/bin/env python3
"""Import the model catalog from the user's HX Edit install into cache/catalog/ (gitignored).

Usage: python scripts/catalog_import.py [--hxedit-path PATH] [--cache-dir DIR] [--no-crosscheck]
Exit codes: 0 ok, 2 HX Edit not found, 3 cross-check found unresolved names.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tonematch import catalog as C


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hxedit-path", help="HX Edit install root (app bundle or install folder)")
    ap.add_argument("--cache-dir", help="default: cache/catalog")
    ap.add_argument("--golden-dir", default=str(C.repo_root() / "fixtures" / "golden"))
    ap.add_argument("--no-crosscheck", action="store_true")
    a = ap.parse_args(argv)
    try:
        loc = C.locate(a.hxedit_path)
        cat = C.build_catalog(loc["resource_dir"], loc["version"], loc["root"])
    except C.CatalogError as e:
        print(f"catalog-import: {e}", file=sys.stderr)
        return 2
    paths = C.write_cache(cat, a.cache_dir)
    s = C.summarize(cat)
    print(f"HX Edit {s['version']} ({loc['resource_dir']})")
    print(f"models: {s['models']} ({s['stomp_models']} usable on HX Stomp), params: {s['params']}")
    print("per category: " + ", ".join(f"{k}={v}" for k, v in s["per_category"].items()))
    print("wrote: " + ", ".join(str(p) for p in paths))
    if a.no_crosscheck:
        return 0
    golden = sorted(Path(a.golden_dir).glob("*.hlx"))
    if not golden:
        print("cross-check: no fixtures/golden/*.hlx yet (T010); skipped")
        return 0
    problems = C.crosscheck_hlx(cat, golden)
    for p in problems:
        print("UNRESOLVED " + p)
    print(f"cross-check: {len(golden)} template(s), {len(problems)} unresolved")
    return 3 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
