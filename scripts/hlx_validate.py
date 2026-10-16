#!/usr/bin/env python3
"""Validate a generated .hlx preset (skill: hlx-validate).

Usage: python scripts/hlx_validate.py <preset.hlx> [--golden fixtures/golden/] [--firmware 0x3800000]
Writes <name>.validate.md next to the preset. Exit 0 pass/warn, 1 any fail, 2 unreadable input.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tonematch import catalog as C
from tonematch import preset as P


def _load_costs(cat_dir: Path, cat: dict | None) -> dict | None:
    f = cat_dir / "dsp-costs.json"
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    return P.build_dsp_costs(cat) if cat else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("preset")
    ap.add_argument("--golden", default=str(C.repo_root() / "fixtures" / "golden"))
    ap.add_argument("--firmware", help="expected device_version, e.g. 0x3800000 or 58720256")
    ap.add_argument("--cache-dir")
    ap.add_argument("--keep-snapshot-values", action="store_true",
                    help="accept snapshot-controlled (@controller 9) parameters that vary by snapshot")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args(argv)
    cache = Path(a.cache_dir) if a.cache_dir else C.default_cache_dir()
    try:
        cat = C.load_catalog("current", cache)
    except C.CatalogError:
        cat = None
    try:
        pre = P.read_hlx(a.preset, cat)
    except P.PresetError as e:
        print(f"hlx-validate: {e}", file=sys.stderr)
        return 2
    if a.keep_snapshot_values:
        pre.keep_snapshot_varying()
    fw = int(a.firmware, 0) if a.firmware else None
    viol = P.validate(pre, cat, _load_costs(cache, cat), fw)
    ver = cat["_source"]["hxedit_version"] if cat else "none"
    head = (f"catalog: {ver}; device={pre.device}; device_version={pre.device_version:#x}; "
            f"blocks={len(pre.blocks())}; mode={pre.export_mode}")
    lines = [f"# hlx-validate: {Path(a.preset).name}", "", head, ""]
    checked = {v.check for v in viol}
    for v in viol:
        lines.append(f"- {v}")
    passed = sorted({"device", "model", "param", "snapshots", "amp_cab", "block_count"} - checked)
    lines.append(f"- PASS (no findings): {', '.join(passed)}")
    goldens = sorted(Path(a.golden).glob("*.hlx")) if Path(a.golden).is_dir() else []
    if not goldens:
        lines.append("- WARN golden_diff: no fixtures/golden/*.hlx; structural diff skipped")
    nfail = sum(v.severity == "fail" for v in viol)
    for g in goldens:
        d = P.golden_diff(pre.to_hlx_dict(), json.loads(g.read_text(encoding="utf-8")), cat)
        for lvl, r in d.items():
            if r["missing"]:
                # tone-level and effect-block keys differ legitimately between the two goldens
                sev = "FAIL" if lvl in ("top", "data", "meta") else "WARN"
                nfail += sev == "FAIL"
                lines.append(f"- {sev} golden_diff[{g.name}] {lvl}: missing {r['missing']}")
            if r["added"]:
                lines.append(f"- INFO golden_diff[{g.name}] {lvl}: adds {r['added']}")
    lines.append("")
    lines.append(f"result: {'FAIL' if nfail else 'ok'} ({nfail} fail, "
                 f"{sum(v.severity == 'warn' for v in viol)} warn)")
    text = "\n".join(lines)
    print(text)
    if not a.no_report:
        Path(a.preset).with_suffix(".validate.md").write_text(text + "\n", encoding="utf-8")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
