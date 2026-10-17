#!/usr/bin/env python3
"""Learn category-order patterns from real presets (offline: no Native, no pedal).

Usage:
  python scripts/chain_patterns.py [--hls F ...] [--hlx-dir PATH ...] [--no-default]
                                   [--out data/chain_prior/category_prior.json]
Sources: by default the local setlist and every .hlx under fixtures/golden/; --hlx-dir adds any
folder (recursive, .hlx and .hls). Line 6 content stays local. Writes:
  data/chain_prior/category_prior.json   committed; category level only
  cache/chain-patterns/model_frequencies.json   LOCAL ONLY; model ids per category

Corpus mode (T104): `--corpus data/presets` scans the curated folder tree (artist / style / effect
folders; setlists saved as .hlx are expanded, encrypted files are not read), drops exact duplicates,
folds the chains into the prior with their folder metadata (conditional priors by decade, style,
effect and group) and runs the full analysis. Writes, besides the above:
  data/chain_prior/corpus_patterns.json   committed; category level only (no models, artists, names)
  reports/local/preset-corpus/            LOCAL ONLY; README.md, model_usage.json, artists.json, ...
  cache/chain-patterns/prior_with_artists.json   LOCAL ONLY; the prior plus per-artist sub-priors
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from tonematch import chain_prior as CP

GOLDEN = ROOT / "fixtures" / "golden"
LOCAL = ROOT / "cache" / "chain-patterns"


def load_file(path: Path) -> list[dict]:
    """Preset dicts from an .hlx (one) or .hls (many). Unreadable files are skipped by the caller."""
    if path.suffix.lower() == ".hls":
        from tonematch.engines.native_setlist import decode_hls
        return decode_hls(path)["presets"]
    return [json.loads(path.read_text())]


def collect(hls: list[Path], dirs: list[Path]) -> tuple[list[dict], dict]:
    presets, src = [], Counter()
    files = [(p, "setlist") for p in hls]
    seen = {p.resolve() for p in hls}
    for d in dirs:
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in (".hlx", ".hls") and p.resolve() not in seen:
                seen.add(p.resolve())
                files.append((p, "dir"))
    for p, kind in files:
        try:
            got = load_file(p)
        except Exception as e:  # noqa: BLE001
            print(f"skip {p.name}: {type(e).__name__}", file=sys.stderr)
            src["unreadable_files"] += 1
            continue
        presets += got
        src[f"{kind}_presets"] += len(got)
    return presets, dict(src)


def unique_presets(presets: list[dict]) -> list[dict]:
    """Drop presets whose `tone` is identical to an earlier one (setlists repeat the golden files)."""
    import hashlib
    seen, out = set(), []
    for p in presets:
        t = CP.preset_tone(p)
        h = hashlib.sha1(json.dumps(t, sort_keys=True).encode()).hexdigest() if t else None
        if h is None or h not in seen:
            seen.add(h)
            out.append(p)
    return out


def category_costs(catalog: dict | None, chains_models: list[tuple[str, str]]) -> dict:
    """Median DSP cost per category over the models actually used (local costs table)."""
    f = ROOT / "cache" / "catalog" / "dsp-costs.json"
    if not f.exists():
        return {}
    costs = json.loads(f.read_text()).get("models", {})
    by = defaultdict(list)
    for cat, m in chains_models:
        c = (costs.get(m) or {}).get("mono")
        if c is not None:
            by[cat].append(c)
    import statistics
    return {c: round(statistics.median(v), 2) for c, v in by.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--hls", action="append", type=Path, default=[])
    ap.add_argument("--hlx-dir", action="append", type=Path, default=[])
    ap.add_argument("--no-default", action="store_true", help="skip the default golden sources")
    ap.add_argument("--out", type=Path, default=CP.PRIOR_PATH)
    ap.add_argument("--corpus", type=Path, help="curated folder tree of .hlx (T104): full analysis and report")
    ap.add_argument("--quick", action="store_true", help="fewer permutations/shuffles (corpus mode)")
    ap.add_argument("--no-catalog", action="store_true", help="name heuristic instead of the catalog")
    a = ap.parse_args(argv)
    hls, dirs = list(a.hls), list(a.hlx_dir)
    if not a.no_default:
        hls += [p for p in [GOLDEN / "HXStompSetlist.hls"] if p.exists() and p not in hls]
        if GOLDEN.exists() and GOLDEN not in dirs:
            dirs.append(GOLDEN)
    catalog = None
    if not a.no_catalog:
        try:
            from tonematch import catalog as CAT
            catalog = CAT.load_catalog("current")
        except Exception as e:  # noqa: BLE001
            print(f"no catalog ({type(e).__name__}); using the name heuristic", file=sys.stderr)
    presets, src = collect(hls, dirs)
    presets = unique_presets(presets)
    if a.corpus:
        import hashlib

        import corpus_report as CR
        base = [(CP.extract_chain(p, catalog), {}, hashlib.sha1(json.dumps(
            CP.preset_tone(p), sort_keys=True).encode()).hexdigest()[:16]) for p in presets]
        res = CR.run(a.corpus, catalog, [b for b in base if b[0].linear], a.out, quick=a.quick)
        print(f"corpus: {res['records']} records, {res['kept']} after dedupe, {res['usable']} usable; "
              f"prior over {res['prior']['n_presets']} chains -> {a.out}")
        print(f"report: {CR.LOCAL_REPORT / 'README.md'}")
        return 0
    chains = [CP.extract_chain(p, catalog) for p in presets]
    chains = [c for c in chains if c.linear]
    models = [m for c in chains for m in c.models]
    prior = CP.build_prior(chains, category_costs(catalog, models))
    prior["sources"] = src
    prior["category_mapping"] = "catalog" if catalog else "name heuristic"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(prior, indent=1, sort_keys=True))
    LOCAL.mkdir(parents=True, exist_ok=True)
    mf: dict = defaultdict(Counter)
    for cat, m in models:
        mf[cat][m] += 1
    (LOCAL / "model_frequencies.json").write_text(json.dumps(
        {c: dict(v.most_common()) for c, v in sorted(mf.items())}, indent=1))
    print(f"parsed {len(presets)} presets, {len(chains)} with active blocks; sources {src}")
    print(f"two-path: {prior['n_two_path']}; distinct sequences: {prior['distinct_sequences']}")
    for s, k in prior["top_sequences"]:
        print(f"{k:3d}  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
