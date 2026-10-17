#!/usr/bin/env python3
"""Corpus analysis and report for `scripts/chain_patterns.py --corpus DIR` (T109).

Offline. Reads the curated .hlx folder tree, computes the statistics in `tonematch.corpus`, and writes
  reports/local/preset-corpus/   README.md + JSON with model, preset, author and artist names (LOCAL, gitignored)
  data/chain_prior/corpus_patterns.json   category-level aggregates only (committed)
Nothing here talks to a device or to Native.
"""
from __future__ import annotations

import contextlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch import chain_prior as CP
from tonematch import corpus as C

LOCAL_REPORT = ROOT / "reports" / "local" / "preset-corpus"
AGG_PATH = ROOT / "data" / "chain_prior" / "corpus_patterns.json"
ORDER_CATS = ["comp", "wah", "volpan", "pitch", "filter", "drive", "amp", "preamp", "cab", "eq", "mod",
              "delay", "reverb", "sendreturn"]


def md(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out) + "\n"


def pct(x, nd=0) -> str:
    return "-" if x is None else f"{100 * x:.{nd}f}%"


def top_str(share: dict, k: int = 4) -> str:
    return ", ".join(f"{c} {pct(v)}" for c, v in list(share.items())[:k])


# ----------------------------------------------------------------------------- quality

def quality(recs: list, kept: list, dup_stats: dict) -> dict:
    ok = [r for r in recs if r.ok]
    fails = [r for r in recs if not r.ok and r.kind != "empty_slot"]
    files = Counter(r.rel.split("#")[0] for r in recs)
    dev = Counter(C.STOMP_IDS.get(r.device, f"device id {r.device}") for r in kept)
    fw = Counter(r.fw for r in kept)
    stomp = [r for r in kept if r.stomp_family]
    nb = Counter(r.stomp_blocks for r in stomp)
    return {
        "files": len(files), "records": len(recs), "setlist_members": sum(r.kind == "setlist_member" for r in recs),
        "setlist_containers": len({r.rel.split("#")[0] for r in recs if r.kind in ("setlist_member", "empty_slot")}),
        "empty_slots": sum(r.kind == "empty_slot" for r in recs),
        "parsed": len(ok), "failures": [(r.rel, r.err) for r in fails],
        "kept_after_dedupe": len(kept), **dup_stats,
        "devices": dict(dev), "firmware": dict(sorted(fw.items(), key=lambda t: str(t[0]))),
        "stomp_family": len(stomp), "fit_8_blocks": sum(r.fits_stomp for r in stomp),
        "stomp_blocks_hist": dict(sorted(nb.items())),
        "over_8": sum(r.stomp_blocks > 8 for r in stomp),
        "uses_dsp1": sum(r.n_dsp1 > 0 for r in kept),
        "no_active_blocks": sum(not r.cats for r in kept),
        "unknown_models": sum(bool(r.unknown) for r in kept),
        "models_not_on_stomp": sum(bool(r.not_stomp) for r in kept),
        "embedded_cab_key": sum(r.n_cab > 0 for r in kept),
        "two_path": sum(bool(r.chain and r.chain.two_path) for r in kept),
        "snapshots_valid": dict(sorted(Counter(r.n_snap for r in kept).items())),
    }


def outliers(kept: list, prior: dict, top: int = 25) -> dict:
    rows = []
    flags: Counter = Counter()
    for r in kept:
        f = []
        cats = r.cats
        if not cats:
            f.append("no_active_blocks")
        if r.stomp_family and r.stomp_blocks > 8:
            f.append("over_8_blocks")
        if r.n_dsp1:
            f.append("uses_dsp1")
        if r.unknown:
            f.append("unknown_model")
        if r.not_stomp:
            f.append("model_not_on_stomp")
        c = Counter(cats)
        if c["amp"] + c["preamp"] >= 3:
            f.append("3plus_amps")
        if max(c.values(), default=0) >= 4:
            f.append("4plus_same_category")
        if c["reverb"] and cats and cats[0] == "reverb":
            f.append("reverb_first")
        if c["cab"] and c["amp"] + c["preamp"] == 0:
            f.append("cab_without_amp")
        if not r.stomp_family:
            f.append("not_stomp_device")
        for x in f:
            flags[x] += 1
        if cats:
            surprisal = -CP.sequence_logprob(cats, prior) / (len(cats) + 1) / np.log(2)
            rows.append((surprisal, r.rel, ">".join(cats), f))
    rows.sort(key=lambda t: -t[0])
    return {"flag_counts": dict(flags), "most_surprising": rows[:top],
            "median_bits_per_transition": round(float(np.median([x[0] for x in rows])), 3)}


# ----------------------------------------------------------------------------- driver

def run(corpus_dir: Path, catalog: dict | None, prior_sources: list, out_prior: Path,
        quick: bool = False) -> dict:
    """Run everything. `prior_sources` = [(chain, ctx)] from non-corpus sources (setlist, golden)."""
    LOCAL_REPORT.mkdir(parents=True, exist_ok=True)
    recs = C.scan(corpus_dir, catalog)
    kept, dup_stats = C.dedupe([r for r in recs if r.kind != "empty_slot"])
    usable = [r for r in kept if r.cats and r.stomp_family]
    base_hashes = {h for _, _, h in prior_sources}
    dup_stats["also_in_other_sources"] = sum(r.thash in base_hashes for r in usable)
    usable_new = [r for r in usable if r.thash not in base_hashes]
    meta_set = [r for r in usable if r.kind == "hlx"]          # folder metadata is meaningful here
    single = [r for r in usable if not r.chain.two_path]
    seqs = [r.cats for r in single]
    perms = 30 if quick else 100
    agg: dict = {"schema": 1, "corpus": {}}

    # ---- data quality
    q = quality(recs, kept, dup_stats)
    q["usable_stomp_family"] = len(usable)
    q["single_path_usable"] = len(single)
    q["metadata_set"] = len(meta_set)
    agg["quality"] = {k: v for k, v in q.items() if k != "failures"}
    agg["quality"]["parse_failures"] = len(q["failures"])

    # ---- global prior from corpus + other sources, with context
    chains = [c for c, _, _ in prior_sources] + [r.chain for r in usable_new]
    ctxs = [x for _, x, _ in prior_sources] + [r.ctx for r in usable_new]
    prior = CP.build_prior(chains, contexts=ctxs)
    # artist sub-priors are local only
    cond_a = CP.build_conditional(chains, ctxs, keys=("artist",), min_n=5)
    prior_local = dict(prior)
    prior_local["conditional"] = {**prior.get("conditional", {}), **cond_a}

    # ---- order statistics
    pos = {"blocks": C.position_stats(seqs)}
    gseqs = [C.collapse(s) for s in seqs]
    pos["groups"] = C.position_stats(gseqs)
    ugseqs = [C.collapse(s, C.AMP_UNIT) for s in seqs]
    pos["amp_unit_groups"] = C.position_stats(ugseqs)
    runs = C.run_lengths(seqs)
    presence = {c: round(sum(c in s for s in seqs) / len(seqs), 4) for c in CP.CATEGORIES
                if any(c in s for s in seqs)}
    freq_cats = [c for c in CP.CATEGORIES if presence.get(c, 0) >= 0.05 and c != "other"]
    pairs = C.pair_stats(seqs, freq_cats, shuffles=10 if quick else 20)
    clusters = C.cluster_categories(seqs, freq_cats)
    hypotheses = {
        "ambience (delay+reverb)": {"delay", "reverb"},
        "time-modulation (mod+delay)": {"mod", "delay"},
        "space+mod (mod+delay+reverb)": {"mod", "delay", "reverb"},
        "gain staging (drive+amp+cab)": {"drive", "amp", "cab"},
        "gain staging, no cab (drive+amp)": {"drive", "amp"},
        "amp unit (amp+cab)": {"amp", "cab"},
        "front-end dynamics (comp+drive)": {"comp", "drive"},
        "pre-amp effects (wah+volpan+pitch+filter+comp)": {"wah", "volpan", "pitch", "filter", "comp"},
        "post-amp shaping (eq+mod+delay+reverb)": {"eq", "mod", "delay", "reverb"},
        "pedalboard (comp+drive+wah+volpan+pitch+filter)": {"comp", "drive", "wah", "volpan", "pitch", "filter"},
    }
    hyp = {k: C.contiguity(seqs, v, shuffles=60 if quick else 150) for k, v in hypotheses.items()}
    proposals = C.propose_groups([[("amp" if c == "preamp" else c) for c in s] for s in seqs],
                                 [c for c in freq_cats if c != "preamp"], shuffles=40 if quick else 80)
    # swap structure inside delay/reverb/mod when both orders occur
    agg["order"] = {"n_sequences": len(seqs), "presence": presence, "position": pos,
                    "run_lengths": runs, "pairs": pairs, "clusters": clusters,
                    "supergroup_hypotheses": hyp, "supergroup_proposals": proposals}

    # ---- usage (LOCAL)
    usage = C.model_usage(usable)
    (LOCAL_REPORT / "model_usage.json").write_text(json.dumps(usage, indent=1))
    if catalog:
        pool = {}
        for cat, v in usage.items():
            used = [m for m, _n, _p in v["ranked"]]
            rest = sorted(m for m in catalog["models"] if CP.categorize(m, catalog) == cat and m not in used)
            pool[cat] = {"used_by_rank": used, "never_used": rest}
        (LOCAL_REPORT / "pool_order.json").write_text(json.dumps(
            {"note": "search pool order proposal: try used_by_rank first, never_used after (T109, local only)",
             "categories": pool}, indent=1))

    # ---- artists (LOCAL)
    listed = []
    al = corpus_dir / "Artist List.txt"
    if al.exists():
        for line in al.read_text(errors="replace").splitlines()[1:]:
            if line.lower().startswith("all impulse"):      # the rest of the file lists IR packs
                break
            if line.strip():
                listed.append(line.strip())
    folders = sorted({r.sub for r in recs if r.group == "artist" and r.sub})
    cross = C.artist_crosswalk(folders, sorted(set(listed)))
    (LOCAL_REPORT / "artists.json").write_text(json.dumps(cross, indent=1))

    # ---- metadata strength
    meta = C.metadata_strength(meta_set, perms=perms)
    cond_cv = C.conditional_cv(meta_set, k=5)
    rev = {t: C.reverse_classifier(meta_set, t) for t in ("group", "style", "effect", "decade")}
    agg["metadata"] = {"counts": {k: {"n": v["n"], "contexts": v["contexts"]} for k, v in meta.items()},
                       "strength": {k: {f: _strip_ctx(e) for f, e in v["features"].items()}
                                    for k, v in meta.items() if k != "author"},
                       "conditional_prior_cv": {k: v for k, v in cond_cv.items() if k != "artist"},
                       "reverse_classifier": rev}
    (LOCAL_REPORT / "metadata_strength.json").write_text(json.dumps(
        {"strength": meta, "conditional_prior_cv": cond_cv, "reverse": rev}, indent=1))

    # ---- style profiles (category-level labels only: decade/style/effect folder names)
    profiles = style_profiles(meta_set)
    agg["profiles"] = profiles

    # ---- outliers (LOCAL)
    out = outliers(kept, prior)
    (LOCAL_REPORT / "outliers.json").write_text(json.dumps(out, indent=1))

    # ---- write committed prior (+ local prior with artists)
    prior["sources"] = {"corpus_records": len(recs), "corpus_kept_usable": len(usable),
                        "other_sources": len(prior_sources)}
    out_prior.parent.mkdir(parents=True, exist_ok=True)
    out_prior.write_text(json.dumps(prior, indent=1, sort_keys=True))
    local = ROOT / "cache" / "chain-patterns"
    local.mkdir(parents=True, exist_ok=True)
    (local / "prior_with_artists.json").write_text(json.dumps(prior_local, indent=1, sort_keys=True))
    AGG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGG_PATH.write_text(json.dumps(agg, indent=1, sort_keys=True))
    (LOCAL_REPORT / "README.md").write_text(render(q, out, agg, usage, cross, meta, cond_cv, rev,
                                                    profiles, kept, usable, prior))
    return {"records": len(recs), "kept": len(kept), "usable": len(usable), "prior": prior,
            "agg": agg, "quality": q}


def _strip_ctx(e: dict) -> dict:
    ca, cr = e.get("cv_by_author"), e.get("cv_random")
    return {"mi_frac_of_H": e["mi"]["frac_of_H"], "z": e["mi"]["z"], "n": e["mi"]["n"],
            "cv_random_gain_bits": cr["gain_bits"] if cr else None,
            "cv_by_author_gain_bits": ca["gain_bits"] if ca else None,
            "acc_by_author": ca["acc_ctx"] if ca else None,
            "acc_majority": ca["acc_majority"] if ca else None}


def style_profiles(recs: list) -> dict:
    """Per decade / style / effect label: n, mean blocks, gain mix, delay/reverb/drive use."""
    out: dict = {}
    for key in ("decade", "style", "effect", "group"):
        by: dict = defaultdict(list)
        for r in recs:
            if r.ctx.get(key):
                by[r.ctx[key]].append(r)
        rows = {}
        for v, rs in by.items():
            if len(rs) < 8:
                continue
            f = [C.features(r) for r in rs]
            n = len(rs)
            rows[v] = {"n": n, "mean_blocks": round(float(np.mean([len(r.cats) for r in rs])), 2),
                       "gain_high": round(sum(x["gain"] == "high" for x in f) / n, 3),
                       "gain_low": round(sum(x["gain"] == "low" for x in f) / n, 3),
                       "has_drive": round(sum(x["has_drive"] == "yes" for x in f) / n, 3),
                       "has_delay": round(sum(x["has_delay"] == "yes" for x in f) / n, 3),
                       "has_reverb": round(sum(x["has_reverb"] == "yes" for x in f) / n, 3),
                       "has_mod": round(sum(x["has_mod"] == "yes" for x in f) / n, 3),
                       "has_comp": round(sum(x["has_comp"] == "yes" for x in f) / n, 3)}
        out[key] = rows
    return out


# ----------------------------------------------------------------------------- markdown

def render(q, out, agg, usage, cross, meta, cond_cv, rev, profiles, kept, usable, prior) -> str:
    o = agg["order"]
    L = ["# Preset corpus analysis (T109)\n",
         ("LOCAL ONLY: contains model ids, preset names, authors and artists. Never commit. Generated by "
         "`python scripts/chain_patterns.py --corpus data/presets`. The committed, category-level subset is "
         "`data/chain_prior/corpus_patterns.json` and `docs/notes/chain-patterns.md`.\n")]

    # data quality
    L.append("## 1. Data quality\n")
    L.append(md(["measure", "value"], [
        ["`.hlx` files", q["files"]], ["records (setlist containers expanded)", q["records"]],
        ["setlist containers saved as `.hlx`", f'{q["setlist_containers"]} ({q["setlist_members"]} presets, {q["empty_slots"]} empty slots)'],
        ["parsed", q["parsed"]], ["parse failures (excluding empty slots)", len(q["failures"])],
        ["exact duplicates removed (identical `tone`)", q["exact_duplicates_removed"]],
        ["kept after dedupe", q["kept_after_dedupe"]],
        ["same name and author among kept (likely variants)", q["same_name_and_author_among_kept"]],
        ["same models + enabled pattern among kept (params differ)", q["same_models_and_enabled_among_kept"]],
        ["usable (Stomp family, at least one active block)", q["usable_stomp_family"]],
        ["usable single path (used for order statistics)", q["single_path_usable"]],
        ["two-path presets", q["two_path"]],
        ["metadata set (plain `.hlx`, folder context valid)", q["metadata_set"]],
        ["no active block at all", q["no_active_blocks"]],
        ["use DSP 1 (not a Stomp layout)", q["uses_dsp1"]],
        ["models missing from the 3.82 catalog", q["unknown_models"]],
        ["include a model flagged not-on-Stomp by the catalog", q["models_not_on_stomp"]],
    ]))
    if q["failures"]:
        L.append("\nParse failures:\n\n" + md(["file", "reason"], [list(x) for x in q["failures"]]))
    L.append("\nDevice id (after dedupe):\n\n" + md(["device", "presets"], [[k, v] for k, v in q["devices"].items()]))
    L.append("\nFirmware (`device_version`, decoded as major.minor):\n\n" + md(
        ["firmware", "presets"], [[k, v] for k, v in q["firmware"].items()]))
    nb = q["stomp_blocks_hist"]
    L.append(f"\nStomp-family presets by block count (blocks that count against the 8-block limit: every "
             f"`blockN`; a `cab0` key is the amp's own cab and is not counted; {q['embedded_cab_key']} presets have one):\n\n"
             + md(["blocks", *[str(k) for k in nb]], [["presets", *nb.values()]])
             + f"\n{q['fit_8_blocks']} of {q['stomp_family']} Stomp-family presets fit 8 blocks on one DSP "
             f"({q['over_8']} exceed 8). The Stomp-family presets were all produced by the pedal's own editor, so "
             "nothing above 8 means the 8-block reading of the format holds.\n")
    L.append("\nSnapshots flagged `@valid` per preset: " + ", ".join(f"{k}: {v}" for k, v in q["snapshots_valid"].items()) + "\n")

    # order patterns
    L.append(f"## 2. Block chain order (single-path presets, n = {o['n_sequences']})\n")
    L.append("Presence share: " + ", ".join(f"{c} {pct(v)}" for c, v in sorted(o["presence"].items(), key=lambda t: -t[1])) + "\n")
    for view, title in (("blocks", "Blocks (every block is one element)"),
                        ("groups", "Groups (consecutive blocks of one category collapse into one element)"),
                        ("amp_unit_groups", "Groups, with amp / preamp / cab folded into one `amp_unit`")):
        p = o["position"][view]
        L.append(f"\n### {title}\n\nMean length {p['mean_length']}. Share of presets having category X at that position "
                 "(denominator: presets at least that long).\n\n")
        rows = [[f"#{int(k) + 1}", v["n"], top_str(v["share"])] for k, v in p["from_start"].items()]
        rows += [[("last" if k == "0" else f"last-{k}"), v["n"], top_str(v["share"])] for k, v in p["from_end"].items()]
        L.append(md(["position", "n", "top categories"], rows))
    L.append("\n### Run lengths (consecutive same-category blocks)\n\n")
    L.append(md(["category", "groups", "size 1", "size 2", "size 3+", "share size 2+"],
                [[c, v["groups"], v["size1"], v["size2"], v["size3plus"],
                  pct((v["size2"] + v["size3plus"]) / v["groups"])]
                 for c, v in sorted(o["run_lengths"].items(), key=lambda t: -t[1]["groups"]) if v["groups"] >= 30]))

    L.append("\n## 3. Cross-category mixing\n\nPairs with both categories present (n >= 100). `x first` = share where the first "
             "occurrence of x precedes y. Swap index: 0 = fixed order, 1 = coin flip. Adjacency lift: how often they touch compared "
             "with the same presets shuffled.\n\n")
    prs = [(k, v) for k, v in o["pairs"].items() if v["n"] >= 100]
    prs.sort(key=lambda t: -t[1]["swap_index"])
    L.append("Most swapped (highest swap index):\n\n" + md(
        ["x / y", "n", "x first", "swap index", "adjacent", "if shuffled", "lift"],
        [[k.replace("|", " / "), v["n"], pct(v["x_first"]), v["swap_index"], pct(v["adjacent"]),
          pct(v["adjacent_if_shuffled"]), v["adjacency_lift"]] for k, v in prs[:14]]))
    prs.sort(key=lambda t: -(t[1]["adjacency_lift"] or 0))
    L.append("\nMost habitually adjacent (adjacency lift):\n\n" + md(
        ["x / y", "n", "adjacent", "if shuffled", "lift", "swap index"],
        [[k.replace("|", " / "), v["n"], pct(v["adjacent"]), pct(v["adjacent_if_shuffled"]), v["adjacency_lift"], v["swap_index"]]
         for k, v in prs[:14]]))
    L.append("\nCluster merges (average linkage, Hellinger distance on position profile + predecessor/successor profile; "
             "low distance = similar role):\n\n")
    L.append(md(["merge", "distance"], [[" + ".join(m["left"]) + "  with  " + " + ".join(m["right"]), m["distance"]]
                                         for m in o["clusters"]["merges"]]))
    L.append("\nSuper-group hypotheses: share of presets (with two or more members present) in which the member blocks "
             "form one contiguous run, versus the same presets shuffled.\n\n")
    L.append(md(["group", "n", "contiguous", "if shuffled", "lift"],
                [[k, v["n"], pct(v["contiguous"]), pct(v["contiguous_if_shuffled"]), v["lift"]]
                 for k, v in o["supergroup_hypotheses"].items()]))
    L.append("\nData-driven candidates (subsets of 2-4 categories ranked by lift weighted by support):\n\n")
    L.append(md(["members", "n", "contiguous", "if shuffled", "lift"],
                [[" + ".join(d["members"]), d["n"], pct(d["contiguous"]), pct(d["contiguous_if_shuffled"]), d["lift"]]
                 for d in o["supergroup_proposals"]]))

    # usage
    L.append("\n## 4. Block usage (LOCAL)\n\nConcentration of model choice by category. `top-k` = share of that category's blocks "
             "covered by its k most-used models; `n for 80%` = how many models cover 80% of blocks.\n\n")
    L.append(md(["category", "blocks", "models seen", "effective models", "top1", "top5", "top10", "top20", "n for 50%", "n for 80%", "n for 95%"],
                [[c, v["concentration"]["blocks"], v["concentration"]["models"], v["concentration"]["effective_models"],
                  pct(v["concentration"]["top1_share"]), pct(v["concentration"]["top5_share"]),
                  pct(v["concentration"]["top10_share"]), pct(v["concentration"]["top20_share"]),
                  v["concentration"]["models_for_50pct"], v["concentration"]["models_for_80pct"],
                  v["concentration"]["models_for_95pct"]]
                 for c, v in sorted(usage.items(), key=lambda t: -t[1]["concentration"]["blocks"])]))
    cat_models = {}
    with contextlib.suppress(Exception):
        from tonematch import catalog as CAT
        cat_models = CAT.load_catalog("current")["models"]
    for c, v in sorted(usage.items(), key=lambda t: -t[1]["concentration"]["blocks"]):
        if v["concentration"]["blocks"] < 100:
            continue
        tot = v["concentration"]["blocks"]
        L.append(f"\n### {c}: top 15 of {v['concentration']['models']} models\n\n")
        L.append(md(["rank", "model", "name", "blocks", "share", "cumulative", "presets"],
                    [[i + 1, m, (cat_models.get(m) or {}).get("name", "?"), n, pct(n / tot, 1),
                      pct(sum(x[1] for x in v["ranked"][:i + 1]) / tot, 1), pr]
                     for i, (m, n, pr) in enumerate(v["ranked"][:15])]))
        # models never seen in this category
        unused = [m for m, i in cat_models.items() if CP.categorize(m, {"models": cat_models}) == c and m not in {x[0] for x in v["ranked"]}]
        L.append(f"\nCatalog models of this category never used in the corpus: {len(unused)}.\n")

    # metadata
    L.append("\n## 5. Metadata\n")
    L.append("\n### Counts\n\n" + md(["context", "presets with it", "distinct values"],
                                      [[k, v["n"], v["contexts"]] for k, v in meta.items()]))
    L.append("\n### Artist folders versus Artist List.txt\n\n")
    L.append(md(["measure", "value"], [["artist folders", cross["folders"]], ["names in Artist List.txt", cross["listed"]],
                                        ["folder matches a listed name exactly (after normalising case, `&`, punctuation)", len(cross["exact"])],
                                        ["folder matches only fuzzily (difflib >= 0.86)", len(cross["fuzzy"])],
                                        ["folder not in list", len(cross["folder_not_in_list"])],
                                        ["listed but no folder", len(cross["list_not_in_folders"])],
                                        ["folders collapsing together after normalising", len(cross["folders_normalising_together"])]]))
    if cross["fuzzy"]:
        L.append("\nFuzzy matches (folder -> list): " + "; ".join(f"{a} -> {b}" for a, b in cross["fuzzy"].items()) + "\n")
    if cross["folder_not_in_list"]:
        L.append("\nFolders not in the list: " + "; ".join(cross["folder_not_in_list"]) + "\n")
    L.append("\nListed names with no folder: " + "; ".join(cross["list_not_in_folders"][:120]) + "\n")
    if cross["folders_normalising_together"]:
        L.append("\nFolders that normalise to the same name: " + "; ".join(" = ".join(x) for x in cross["folders_normalising_together"]) + "\n")

    L.append("\n### How strongly does each kind of metadata predict chain features?\n\n"
             "`MI/H` = bias-corrected mutual information (observed minus the mean over shuffled labels) as a share of the "
             "feature's entropy; `z` = distance from the shuffle null in standard deviations. `gain` = held-out log-loss "
             "reduction in bits per preset against the marginal, 5-fold, labels' folds either random or grouped by `meta.author` "
             "(so the same author never sits on both sides). `acc` = held-out accuracy by author-grouped folds vs majority. "
             "Authors often upload families of near-identical presets under one artist folder, so the random-fold figures overstate "
             "what generalises; read the author-grouped ones.\n\n")
    summ = []
    for ck, v in meta.items():
        fs = {f: e for f, e in v["features"].items() if f != "group_sequence"}
        mi = np.mean([e["mi"]["frac_of_H"] for e in fs.values()])
        ga = [e["cv_by_author"]["gain_bits"] for e in fs.values() if e.get("cv_by_author")]
        gs = v["features"].get("group_sequence", {}).get("mi", {})
        best = sorted(((e["cv_by_author"]["gain_bits"], f) for f, e in fs.items() if e.get("cv_by_author")), reverse=True)[:3]
        summ.append([ck, v["n"], v["contexts"], pct(float(mi), 1), f"{np.mean(ga):.3f}" if ga else "-",
                     pct(gs.get("frac_of_H", 0), 1), ", ".join(f"{f} ({g:.2f})" for g, f in best) or "-"])
    L.append("**Summary** (features other than the full sequence): mean MI/H, mean author-grouped held-out gain, MI/H of the whole "
             "collapsed chain structure, and the three features with the largest author-grouped gain (bits per preset).\n\n")
    L.append(md(["context", "n", "values", "mean MI/H", "mean gain by author", "chain-structure MI/H", "best predicted"], summ))
    for ck, v in meta.items():
        L.append(f"\n**Context: {ck}** (n = {v['n']}, {v['contexts']} values)\n\n")
        rows = []
        for fn, e in sorted(v["features"].items(), key=lambda t: -t[1]["mi"]["frac_of_H"]):
            cr, ca = e.get("cv_random"), e.get("cv_by_author")
            rows.append([fn, e["mi"]["feature_values"], e["mi"]["H_feature"], pct(e["mi"]["frac_of_H"], 1), e["mi"]["z"],
                         cr["gain_bits"] if cr else "-", ca["gain_bits"] if ca else "-",
                         f'{pct(ca["acc_ctx"])} / {pct(ca["acc_majority"])}' if ca else "-"])
        L.append(md(["feature", "values", "H (bits)", "MI/H", "z", "gain random", "gain by author", "acc / majority"], rows))
    L.append("\n### Does the conditional prior beat the global prior? (held-out bits per block, author-grouped folds)\n\n")
    kps = list(next(iter(cond_cv.values()))["by_kappa"]) if cond_cv else []
    L.append("Gain in bits per block against the global prior for each backoff strength kappa (higher kappa leans harder on the "
             "global prior; positive = the context helps).\n\n")
    L.append(md(["context", "n", "global bits/block", *[f"kappa {k}" for k in kps], "share with a usable context prior"],
                [[k, v["n"], v["bits_per_block_global"], *[v["by_kappa"][x] for x in kps],
                  pct(v["share_with_context_prior"])] for k, v in cond_cv.items()]))
    L.append("\n### Reverse direction: predict the folder from the chain (logistic regression, author-grouped folds)\n\n")
    L.append(md(["target", "n", "classes", "accuracy", "majority"],
                [[k, v["n"], v["classes"], pct(v["acc"]), pct(v["acc_majority"])] if v else [k, "-", "-", "n/a", "n/a"]
                 for k, v in rev.items()]))
    for key in ("decade", "style", "effect"):
        rows = profiles.get(key, {})
        if not rows:
            continue
        L.append(f"\n### Profile by {key} (labels with n >= 8)\n\n")
        L.append(md([key, "n", "blocks", "high gain amp", "low gain amp", "drive", "comp", "mod", "delay", "reverb"],
                    [[k, v["n"], v["mean_blocks"], pct(v["gain_high"]), pct(v["gain_low"]), pct(v["has_drive"]),
                      pct(v["has_comp"]), pct(v["has_mod"]), pct(v["has_delay"]), pct(v["has_reverb"])]
                     for k, v in sorted(rows.items(), key=lambda t: -t[1]["n"])]))

    # outliers
    L.append("\n## 6. Outliers\n\nRule flags:\n\n" + md(["flag", "presets"], [[k, v] for k, v in out["flag_counts"].items()]))
    L.append(f"\nMedian surprisal under the global prior: {out['median_bits_per_transition']} bits per transition. "
             "Most surprising chains (candidates for a mislabelled or unusual preset):\n\n")
    L.append(md(["bits/transition", "file", "chain", "flags"], [[f"{s:.2f}", rel, ch, ",".join(fl)]
                                                              for s, rel, ch, fl in out["most_surprising"]]))
    L.append("\n## 7. Prior rebuild\n\n")
    cond = prior.get("conditional", {})
    L.append(f"Prior built from {prior['n_presets']} chains. Conditional sub-priors stored (>= 5 presets; used at query time only "
             f"with >= {prior.get('min_context_n')}): " + ", ".join(f"{k} {len(v)}" for k, v in cond.items()) + ".\n")
    return "\n".join(L)
