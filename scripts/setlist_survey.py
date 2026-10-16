#!/usr/bin/env python3
"""Coverage and statistics over a whole HX Stomp setlist (.hls), local only.

Usage:
  python scripts/setlist_survey.py coverage [--hls F] [--base BIN] [--no-compile]
  python scripts/setlist_survey.py stats [--hls F]
Outputs go to cache/setlist/ (gitignored); stdout carries aggregate counts only. Preset bodies and
model lists are never written outside cache/. The .hls stays local (Line 6 content).
"""
import argparse
import collections as C
import itertools
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch import catalog as CAT
from tonematch import preset as P
from tonematch.engines.native_setlist import decode_hls

HLS = ROOT / "fixtures" / "golden" / "HXStompSetlist.hls"
OUT = ROOT / "cache" / "setlist"
BASE = ROOT / "cache" / "stomp" / "live" / "base_twopath.bin"
COMPILER = ROOT / "cache" / "stomp" / "bin" / "stompd-compile"
FW = 0x3800000


def wrap(p: dict) -> dict:
    return {"data": p, "meta": {"original": 0, "pbn": 0, "premium": 0}, "schema": "L6Preset",
            "version": 6}


def blocks(tone: dict):
    for dsp in ("dsp0", "dsp1"):
        for k, v in (tone.get(dsp) or {}).items():
            if isinstance(v, dict) and "@model" in v and not k.startswith(
                    ("input", "output", "split", "join")):
                yield dsp, k, v


def kinds(p: dict, cat: dict) -> list[str]:
    """Feature tags of a preset, used to group failures."""
    t = p["tone"]
    out = []
    if any(v.get("@path") == 1 for _d, _k, v in blocks(t)):
        out.append("two-path")
    if t.get("controller"):
        ids = {e.get("@controller") for bs in t["controller"].values() for pr in bs.values()
               for e in pr.values()}
        out.append("controllers:" + ",".join(str(i) for i in sorted(ids, key=str)))
    if t.get("footswitch"):
        out.append("footswitch")
    if t.get("irUuidTable"):
        out.append("ir-table")
    for _d, _k, v in blocks(t):
        m = cat["models"].get(v["@model"], {})
        if m.get("category") in ("other",):
            out.append("block:" + m.get("source_file", "?").replace(".models", ""))
        if "SendReturn" in v["@model"] or "FXLoop" in v["@model"]:
            out.append("send-return")
        if "IR" in v["@model"] and "Cab" in v["@model"]:
            out.append("ir-cab")
    return sorted(set(out))


def coverage(a) -> int:
    import os

    from tonematch.engines.base import EngineError
    from tonematch.engines.stomp import compile_document
    if a.force_build:
        os.environ["STOMPD_FORCE_BUILD"] = "1"
    cat = CAT.load_catalog("current")
    ps = decode_hls(a.hls)["presets"]
    rows = []
    fails = C.Counter()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for i, p in enumerate(ps):
            raw = wrap(p)
            row = {"i": i, "tags": kinds(p, cat), "roundtrip": "ok", "validate": "ok", "compile": "-"}
            try:
                pre = P.from_dict(raw, cat)
                if pre.to_hlx_dict(sync_snapshots=False) != raw:
                    row["roundtrip"] = "dict-differs"
                txt = P.dumps_hlx(pre.to_hlx_dict(sync_snapshots=False))
                import json as _j
                if _j.loads(txt) != raw:
                    row["roundtrip"] = "text-differs"
                pre.keep_snapshot_varying()
                viol = P.validate(pre, cat, P.build_dsp_costs(cat), FW)
                fl = sorted({f"{v.check}" for v in viol if v.severity == "fail"})
                if fl:
                    row["validate"] = ",".join(fl)
                    row["validate_msgs"] = [str(v)[:160] for v in viol if v.severity == "fail"][:5]
                if not a.no_compile:
                    f, out = td / "p.hlx", td / "p.bin"
                    f.write_text(P.dumps_hlx(pre.to_hlx_dict()))
                    try:
                        rep = compile_document(f, Path(a.base), out, COMPILER)
                        row["compile"] = "ok"
                        row["changed"] = rep.get("changed")
                    except EngineError as e:
                        row["compile"] = "refused"
                        row["compile_msg"] = str(e)[:240]
            except Exception as e:  # noqa: BLE001 - survey: record the failure and go on
                row["roundtrip"] = f"exception {type(e).__name__}: {e}"[:200]
            for k in ("roundtrip", "validate", "compile"):
                if row[k] not in ("ok", "-"):
                    fails[(k, row[k].split(" ")[0][:60])] += 1
            rows.append(row)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "coverage.json").write_text(json.dumps(rows, indent=1))
    print(f"presets {len(rows)}; roundtrip ok {sum(r['roundtrip'] == 'ok' for r in rows)}; "
          f"validate ok {sum(r['validate'] == 'ok' for r in rows)}; "
          f"compile ok {sum(r['compile'] == 'ok' for r in rows)}")
    for (k, v), n in sorted(fails.items()):
        print(f"  {k}: {v} x{n}")
    return 0


def stats(a) -> int:
    cat = CAT.load_catalog("current")
    ps = decode_hls(a.hls)["presets"]
    ncat = C.Counter()
    enabled = C.Counter()
    present_in = C.Counter()
    nblocks = []
    order = C.Counter()
    firsts = C.Counter()
    vals = C.defaultdict(lambda: C.defaultdict(list))
    for p in ps:
        pre = P.from_dict(wrap(p), cat)
        seq = []
        seen = set()
        for sl in pre.slots:
            m = cat["models"].get(sl.model_id)
            c = sl.category
            if c == "other" and m:
                c = m["source_file"].replace(".models", "")  # wah, gate, volumepan, pitch-synth ...
            seq.append(c)
            ncat[c] += 1
            enabled[(c, sl.enabled)] += 1
            seen.add(c)
            for nme, x in sl.params.items():
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    vals[c][f"{sl.model_id}:{nme}" if a.per_model else nme].append(float(x))
        nblocks.append(len([x for x in seq if x != "cab"]))
        for c in seen:
            present_in[c] += 1
        for x, y in itertools.pairwise(seq):
            order[(x, y)] += 1
        if seq:
            firsts[seq[0]] += 1
    n = len(ps)
    out = {
        "method": "126-preset HX Stomp setlist decoded with decode_hls; blocks = Preset.slots "
                  "(flow blocks excluded, cab after its amp, ordered by @position over both paths); "
                  "category from the 3.82 catalog, catalog category other split by model file.",
        "presets": n,
        "blocks_per_preset": {"mean": round(statistics.mean(nblocks), 2),
                              "median": statistics.median(nblocks), "max": max(nblocks),
                              "hist": dict(sorted(C.Counter(nblocks).items()))},
        "share_presets_with_category": {c: round(k / n, 3) for c, k in present_in.most_common()},
        "enabled_rate": {c: round(enabled[(c, True)] / max(enabled[(c, True)] + enabled[(c, False)], 1), 3)
                         for c in present_in},
        "mean_blocks_per_preset_by_category": {c: round(k / n, 2) for c, k in ncat.most_common()},
        "first_block_category": dict(firsts.most_common()),
        "adjacent_pairs_top": [[f"{x}>{y}", k] for (x, y), k in order.most_common(15)],
        "param_distributions": {
            c: {p_: {"n": len(v), "mean": round(statistics.mean(v), 4),
                     "p10": round(sorted(v)[len(v) // 10], 4), "median": round(statistics.median(v), 4),
                     "p90": round(sorted(v)[(len(v) * 9) // 10], 4)}
                for p_, v in sorted(d.items()) if len(v) >= 5}
            for c, d in vals.items()},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "prior_stats.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "param_distributions"}, indent=1))
    print("param distributions:", {c: len(d) for c, d in out["param_distributions"].items()})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("coverage", "stats"):
        s = sub.add_parser(name)
        s.add_argument("--hls", default=str(HLS))
        if name == "coverage":
            s.add_argument("--base", default=str(BASE))
            s.add_argument("--no-compile", action="store_true")
            s.add_argument("--force-build", action="store_true",
                           help="STOMPD_FORCE_BUILD=1: build every slot (skips the base's "
                                "controller-assignment refusal; offline coverage of layouts and "
                                "block types only, the output is not a loadable document)")
        else:
            s.add_argument("--per-model", action="store_true")
    a = ap.parse_args(argv)
    return coverage(a) if a.cmd == "coverage" else stats(a)


if __name__ == "__main__":
    sys.exit(main())
