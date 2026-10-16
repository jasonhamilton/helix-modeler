#!/usr/bin/env python3
"""loss-trust-check (owner: tone-metrics). Skill: .claude/skills/loss-trust-check/SKILL.md.

For each item, score the true preset and N distractors against the target with one loss term alone,
and record whether the true preset ranks first. Reports `rank1_rate` per term, on the held-out-notes
subset (candidates rendered from a different DI clip than the target: the real no-DI case) and on the
same-notes subset (candidates rendered from the target's own DI: the aligned case, where `mrstft`
is valid).

Two data sources:

  --proxy   numpy/scipy DSP chains and a synthetic DI (tonematch.metrics.proxy). Dataset name
            `proxy-dsp-v0`. A PROXY: it says how a term behaves on guitar-like DSP, NOT whether it
            ranks Helix presets correctly. Never admission evidence for contracts/loss-kit.md.
  --helix   real Helix Native renders of known chains (`helix-trust-v1`, scripts/helix_trust_set.py,
            T067): held-out notes against a clean target (d0) and against a proxy-separated target,
            plus same notes against the separated target (where `mrstft` is valid). Reports rank-1,
            rank-1 per distractor kind (near / nbr / random), rank-1 against audible distractors
            only, the margin and per-category rank-1. Admission evidence once benchmark's threshold
            is frozen; ledger rows `--engine native --dataset helix-trust-v1`.
  (default) fixtures/synthetic/manifest.json from `synthetic-benchmark` (benchmark, T024). Not built
            yet: the script exits 2 until the manifest and its renders exist.

    python scripts/loss_trust_check.py --proxy --all [--items 40] [--distractors 15] [--ledger]
    python scripts/loss_trust_check.py --proxy --term embed_afxrep ltas_tilt envelope --jobs 2
    python scripts/loss_trust_check.py --helix --term ltas_tilt crest_flatness_hnr envelope mrstft \
        embed_afxrep --jobs 2 --ledger

`--all` is the numpy DSP terms. Learned-embedding terms (`embed_afxrep`) run only when named with
`--term`; they need the `embed` extra and the checkpoint, and use at most 2 worker processes.

Exit 0 on a completed run, 2 when the data is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.metrics import (
    DSP_TERMS,
    EMBED_TERMS,
    IMPLEMENTED,
    NO_DI_TERMS,
    PROVISIONAL_WEIGHTS,
    LossKit,
    proxy,
)

MAX_EMBED_JOBS = 2  # learned embeddings: keep compute modest (T032 brief)

PROXY_DATASET = "proxy-dsp-v0"
COMBO = "provisional_sum"
SUBSETS = ("heldout_notes", "same_notes")


def _rank(scores: np.ndarray) -> int:
    """1-based rank of candidate 0 (the true preset); ties count against it."""
    return int(1 + np.sum(scores[1:] <= scores[0]))


def proxy_item(args: tuple) -> dict:
    i, seed, n_distract, terms, use_mask, seconds, bleed_db, threads = args
    if set(terms) & set(EMBED_TERMS):
        from tonematch.metrics import afxrep
        if afxrep._EMBEDDER is None:
            afxrep.set_embedder(afxrep.Embedder(threads=threads))
    rng = np.random.default_rng([seed, i])
    p = proxy.random_preset(rng)
    di_a = proxy.guitar_di(int(rng.integers(1 << 30)), seconds)
    di_b = proxy.guitar_di(int(rng.integers(1 << 30)), seconds)
    wet = proxy.render(p, di_a)
    if bleed_db is None:  # --no-sep: the clean wet render is the target
        target, conf = wet, np.ones_like(wet)
    else:
        target, conf = proxy.separate_proxy(wet, rng, bleed_db=bleed_db)
    groups = list(proxy.GROUPS)
    cands, labels = [p], ["true"]
    for k in range(n_distract):
        if k < n_distract // 2:
            g = groups[(i + k) % len(groups)]
            cands.append(proxy.perturb_group(p, g, rng))
            labels.append(f"near:{g}")
        else:
            cands.append(proxy.random_preset(rng))
            labels.append("random")
    # candidate output level is a nuisance, including for the true preset
    cands = [c.__class__(**{**c.as_dict(), "out_db": float(rng.uniform(-12, 12))}) for c in cands]
    kit = LossKit()
    weights = {t: 1.0 for t in terms}
    out = {"item": i, "true": p.as_dict(), "labels": labels}
    for subset, di in (("heldout_notes", di_b), ("same_notes", di_a)):
        per = {t: [] for t in terms}
        for c in cands:
            s = kit.score(target, proxy.render(c, di), mask=conf if use_mask else None, weights=weights)
            for t in terms:
                per[t].append(s.terms[t])
        res = {}
        for t in terms:
            sc = np.asarray(per[t])
            res[t] = {"rank": _rank(sc), "winner": labels[int(np.argmin(sc))], "scores": sc.tolist()}
        if all(t in per for t in NO_DI_TERMS):  # the provisional sum needs every DSP no-DI term
            combo = sum(PROVISIONAL_WEIGHTS[t] * np.asarray(per[t]) for t in NO_DI_TERMS)
            res[COMBO] = {"rank": _rank(combo), "winner": labels[int(np.argmin(combo))],
                          "scores": np.asarray(combo).tolist()}
        out[subset] = res
    return out


def summarise(items: list[dict], names: list[str], subsets: tuple[str, ...] = SUBSETS,
              pools: tuple[str, ...] = ("random", "near")) -> dict:
    summ = {}
    for subset in subsets:
        summ[subset] = {}
        for t in names:
            ranks = np.array([it[subset][t]["rank"] for it in items])
            beaten_by: dict[str, int] = {}
            for it in items:
                w = it[subset][t]["winner"]
                if w != "true":
                    beaten_by[w] = beaten_by.get(w, 0) + 1
            pair: dict[str, list[bool]] = {}
            for it in items:
                sc = np.asarray(it[subset][t]["scores"])
                for k, lab in enumerate(it["labels"][1:], 1):
                    pair.setdefault(lab, []).append(bool(sc[k] > sc[0]))
            pool_r1 = {}
            for pool in pools:
                r1 = []
                for it in items:
                    sc = np.asarray(it[subset][t]["scores"])
                    keep = [0] + [k for k, lab in enumerate(it["labels"]) if lab.startswith(pool)]
                    r1.append(_rank(sc[keep]) == 1)
                pool_r1[pool] = float(np.mean(r1))
            summ[subset][t] = {"rank1_rate": float(np.mean(ranks == 1)),
                               "mean_rank": float(ranks.mean()),
                               **{f"rank1_vs_{pool}_only": pool_r1[pool] for pool in pools},
                               "beaten_by": dict(sorted(beaten_by.items())),
                               # P(true scores strictly better than one distractor of this kind)
                               "pairwise_win": {k: float(np.mean(v)) for k, v in sorted(pair.items())}}
    return summ


# --------------------------------------------------------------------------- Helix set (T067)

HELIX_DATASET = "helix-trust-v1"
HELIX_DIR = ROOT / "cache" / "loss-trust" / HELIX_DATASET
# "<target condition>/<candidate subset>". Conditions: clean = the true render itself (oracle stem,
# d0); sep = the same render through the proxy separator (jittered ratio mask, band bleed). Subsets
# by candidate DI: heldout_notes = C (another passage of the target's recording: same guitar and
# player, different notes), other_di = B (a different recording), same_notes = A (the target's own
# DI). Clean + same notes is omitted: the true candidate IS the target there, so it ranks first
# trivially.
SUBSET_DI = {"heldout_notes": "C", "other_di": "B", "same_notes": "A"}


def helix_subsets(tags) -> tuple[str, ...]:
    out = [f"{c}/{s}" for c in ("clean", "sep") for s in SUBSET_DI
           if SUBSET_DI[s] in tags and not (c == "clean" and s == "same_notes")]
    return tuple(out)
HELIX_POOLS = ("near", "nbr", "random")
INAUDIBLE_NULL_DB = -30.0   # gain-matched null to the true preset, same notes: "the same sound"


def _null_db(ref: np.ndarray, y: np.ndarray) -> float:
    n = min(ref.size, y.size)
    ref, y = ref[:n].astype(np.float64), y[:n].astype(np.float64)
    g = float(np.dot(y, ref) / (np.dot(y, y) + 1e-20))
    r = ref - g * y
    return float(10 * np.log10((np.mean(r ** 2) + 1e-30) / (np.mean(ref ** 2) + 1e-30)))


def helix_item(args: tuple) -> dict:
    """Score one Helix item: every candidate rendered on DI A and B against the clean and the
    separated target, with each term alone. Candidate features are computed once per render."""
    item, terms, seed, bleed_db, use_mask, threads, root = args
    tags = sorted(item["di"]) if "di" in item else ["A", "B"]
    subsets = helix_subsets(tags)
    from tonematch.metrics import afxrep, crest, envelope, ltas, mrstft
    mods = {ltas.NAME: ltas, crest.NAME: crest, envelope.NAME: envelope, afxrep.NAME: afxrep}
    if afxrep.NAME in terms and afxrep._EMBEDDER is None:
        afxrep.set_embedder(afxrep.Embedder(threads=threads))
    i = item["item"]
    rdir = Path(root) / "renders" / f"item{i:02d}"
    n = len(item["candidates"])
    ren = {tag: [np.load(rdir / f"c{k:02d}_{tag}.npy") for k in range(n)] for tag in tags}
    clean = ren["A"][0]
    rng = np.random.default_rng([seed, i, 1])
    sep, conf = proxy.separate_proxy(clean.astype(np.float64), rng, bleed_db=bleed_db)
    sep = sep.astype(np.float32)
    targets = {"clean": (clean, None), "sep": (sep, conf if use_mask else None)}
    timing: dict[str, list[float]] = {}
    scores: dict[str, dict[str, list[float]]] = {s: {} for s in subsets}
    for t in terms:
        if t in mods:
            mod = mods[t]
            tf = {c: mod.features(x, m) for c, (x, m) in targets.items()}
            cf = {}
            for tag in tags:
                feats = []
                for y in ren[tag]:
                    t0 = time.perf_counter()
                    feats.append(mod.features(y))
                    timing.setdefault(f"{t}:features", []).append(time.perf_counter() - t0)
                cf[tag] = feats
            for s in subsets:
                cond, sub = s.split("/")
                tag = SUBSET_DI[sub]
                row = []
                for f in cf[tag]:
                    t0 = time.perf_counter()
                    row.append(float(mod.distance(tf[cond], f)))
                    timing.setdefault(f"{t}:distance", []).append(time.perf_counter() - t0)
                scores[s][t] = row
        elif t == mrstft.NAME:
            for s in subsets:
                cond, sub = s.split("/")
                x, m = targets[cond]
                row = []
                for y in ren[SUBSET_DI[sub]]:
                    t0 = time.perf_counter()
                    row.append(float(mrstft.term(x, y, m)))
                    timing.setdefault(f"{t}:term", []).append(time.perf_counter() - t0)
                scores[s][t] = row
        else:
            raise NotImplementedError(t)
    labels = [c["label"] for c in item["candidates"]]
    out = {"item": i, "category": item["category"], "true_model": item["true_model"], "labels": labels,
           "null_db_same_notes": [_null_db(clean, y) for y in ren["A"]],
           "timing_s": {k: float(np.mean(v)) for k, v in timing.items()}}
    out["subsets"] = list(subsets)
    for s in subsets:
        res = {}
        per = scores[s]
        for t in terms:
            sc = np.asarray(per[t])
            res[t] = {"rank": _rank(sc), "winner": labels[int(np.argmin(sc))], "scores": sc.tolist()}
        if all(t in per for t in NO_DI_TERMS):
            combo = sum(PROVISIONAL_WEIGHTS[t] * np.asarray(per[t]) for t in NO_DI_TERMS)
            res[COMBO] = {"rank": _rank(combo), "winner": labels[int(np.argmin(combo))],
                          "scores": np.asarray(combo).tolist()}
        out[s] = res
    return out


def helix_extras(items: list[dict], names: list[str], subsets: tuple[str, ...]) -> dict:
    """Margin, rank-1 against audible distractors only, and rank-1 per category."""
    ex: dict = {}
    for s in subsets:
        ex[s] = {}
        for t in names:
            margins, aud, cats = [], [], {}
            for it in items:
                sc = np.asarray(it[s][t]["scores"])
                d = sc[1:]
                med = float(np.median(d)) or 1e-12
                margins.append(float((d.min() - sc[0]) / med))
                keep = [0] + [k for k in range(1, sc.size) if it["null_db_same_notes"][k] > INAUDIBLE_NULL_DB]
                aud.append(_rank(sc[keep]) == 1)
                cats.setdefault(it["category"], []).append(it[s][t]["rank"] == 1)
            ex[s][t] = {"margin_median": float(np.median(margins)), "margin_mean": float(np.mean(margins)),
                        "margin_p10": float(np.percentile(margins, 10)),
                        "rank1_audible_only": float(np.mean(aud)),
                        "rank1_by_category": {c: float(np.mean(v)) for c, v in sorted(cats.items())}}
    return ex


def run_helix(a, terms: list[str], threads: int) -> int:
    root = Path(a.helix_dir) if a.helix_dir else HELIX_DIR
    mfp = root / "manifest.json"
    if not mfp.exists():
        print(f"no Helix trust set: {mfp} is missing (scripts/helix_trust_set.py gen, then render)",
              file=sys.stderr)
        return 2
    mf = json.loads(mfp.read_text())
    items_in = mf["items"][: a.items] if a.items else mf["items"]
    tags = sorted(items_in[0].get("di", {"A": 0, "B": 0}))
    subsets = helix_subsets(tags)
    missing = [it["item"] for it in items_in for k in range(len(it["candidates"])) for tag in tags
               if not (root / "renders" / f"item{it['item']:02d}" / f"c{k:02d}_{tag}.npy").exists()]
    if missing:
        print(f"renders missing for items {sorted(set(missing))}; run helix_trust_set.py render", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    jobs = [(it, terms, mf["seed"], a.bleed_db, a.mask, threads, str(root)) for it in items_in]
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            items = list(ex.map(helix_item, jobs))
    else:
        items = [helix_item(j) for j in jobs]
    wall = time.perf_counter() - t0
    # the kit's one call must agree with the per-term path used here (spot check on item 0)
    it0 = items_in[0]
    tgt = np.load(root / "renders" / "item00" / "c00_A.npy")
    s0 = subsets[0]
    cand = np.load(root / "renders" / "item00" / f"c01_{SUBSET_DI[s0.split('/')[1]]}.npy")
    ks = LossKit().score(tgt, cand, weights={t: 1.0 for t in terms if t in DSP_TERMS})
    for t, v in ks.terms.items():
        assert abs(v - items[0][s0][t]["scores"][1]) < 1e-6 * max(1.0, abs(v)), (t, v)
    del it0
    names = terms + ([COMBO] if COMBO in items[0][s0] else [])
    summ = summarise(items, names, subsets, HELIX_POOLS)
    extra = helix_extras(items, names, subsets)
    for s in subsets:
        for t in names:
            summ[s][t].update(extra[s][t])
    timing = {k: float(np.mean([it["timing_s"][k] for it in items if k in it["timing_s"]]))
              for k in sorted({k for it in items for k in it["timing_s"]})}
    inaud = sum(1 for it in items for v in it["null_db_same_notes"][1:] if v <= INAUDIBLE_NULL_DB)
    rsum = json.loads((root / "render_summary.json").read_text()) if (root / "render_summary.json").exists() else {}
    ch = rsum.get("chunks", [])
    render = {"renders": int(sum(c["renders"] for c in ch)), "wall_s": float(sum(c["wall_s"] for c in ch)),
              "hosts": sorted({c["hosts"] for c in ch}),
              "per_host_s": float(np.average([c["item_s_mean"] for c in ch], weights=[c["renders"] for c in ch]))
              if ch else None}
    if render["renders"]:
        render["wall_s_per_render"] = render["wall_s"] / render["renders"]
        render["wall_s_per_render_by_hosts"] = {
            str(h): sum(c["wall_s"] for c in ch if c["hosts"] == h) / sum(c["renders"] for c in ch if c["hosts"] == h)
            for h in render["hosts"]}
    dev = None
    if "embed_afxrep" in terms:
        from tonematch.metrics import afxrep
        dev = afxrep.get_embedder().device
    n_dis = len(items[0]["labels"]) - 1
    report = {"dataset": HELIX_DATASET, "proxy": False, "engine": "native", "items": len(items),
              "distractors": n_dis, "seed": mf["seed"], "seconds": mf["seconds"], "bleed_db": a.bleed_db,
              "mask": a.mask, "chance_rank1_rate": 1.0 / (n_dis + 1), "inaudible_null_db": INAUDIBLE_NULL_DB,
              "inaudible_distractors": inaud, "timing_s": timing, "render": render, "afxrep_device": dev,
              "wall_clock_s": round(wall, 1), "subsets": list(subsets), "summary": summ,
              "per_item": items}
    out = a.out or root / f"report{'-mask' if a.mask else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))

    print(f"HELIX loss-trust-check ({HELIX_DATASET}, Native): {len(items)} items x {n_dis} distractors; "
          f"chance {1 / (n_dis + 1):.3f}; {inaud} of {len(items) * n_dis} distractors null <= "
          f"{INAUDIBLE_NULL_DB:g} dB against the true preset (same notes)")
    for s in subsets:
        print(f"-- {s}")
        print(f"{'term':19s} {'r1':>5s} {'r1aud':>5s} {'rank':>5s} {'near':>5s} {'nbr':>5s} {'rand':>5s} "
              f"{'marg':>6s} {'m_p10':>6s}  by category")
        for t in names:
            h = summ[s][t]
            cats = " ".join(f"{c[:4]}={v:.1f}" for c, v in h["rank1_by_category"].items())
            print(f"{t:19s} {h['rank1_rate']:5.3f} {h['rank1_audible_only']:5.3f} {h['mean_rank']:5.2f} "
                  f"{h['rank1_vs_near_only']:5.2f} {h['rank1_vs_nbr_only']:5.2f} {h['rank1_vs_random_only']:5.2f} "
                  f"{h['margin_median']:6.3f} {h['margin_p10']:6.3f}  {cats}")
    print("timing (s, mean per clip or pair):", {k: round(v, 4) for k, v in timing.items()})
    print("render:", render, "afxrep device:", dev)
    print(f"report: {out}  ({wall:.1f} s)")

    if a.ledger:
        env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}

        def row(metric: str, value: float, notes: str, renders: int | None = None, wall_s: float | None = None):
            cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                   "--engine", "native", "--dataset", HELIX_DATASET, "--metric", metric,
                   "--value", f"{value:.4g}", "--notes", notes.replace(",", ";")]
            if renders is not None:
                cmd += ["--renders", str(renders)]
            if wall_s is not None:
                cmd += ["--wall-clock-s", f"{wall_s:.1f}"]
            subprocess.run(cmd, check=True, env=env)

        base = (f"Helix Native 3.82 renders; items={len(items)} distractors={n_dis} (near/nbr/random) "
                f"seed={mf['seed']} seconds={mf['seconds']:g} chance={1 / (n_dis + 1):.3f}; "
                f"threshold proposed not frozen so nothing admitted")
        for t in names:
            for s in subsets:
                h = summ[s][t]
                note = (f"term={t} subset={s} rank1_audible_only={h['rank1_audible_only']:.3f} "
                        f"vs_near={h['rank1_vs_near_only']:.3f} vs_nbr={h['rank1_vs_nbr_only']:.3f} "
                        f"vs_random={h['rank1_vs_random_only']:.3f} margin_median={h['margin_median']:.3f} "
                        f"margin_p10={h['margin_p10']:.3f} mean_rank={h['mean_rank']:.2f}; " + base
                        + (f" {a.note}" if a.note else ""))
                row("rank1_rate", h["rank1_rate"], note, render.get("renders"), wall)
        for t in terms:
            if t == "mrstft":
                v = timing.get("mrstft:term", 0.0)
                how = "one aligned pair"
            else:
                v = timing.get(f"{t}:features", 0.0) + timing.get(f"{t}:distance", 0.0)
                how = "candidate features + distance (target features cached)"
            row("seconds_per_test", v, f"term={t} scoring cost per candidate: {how}; render excluded; "
                f"single process; {mf['seconds']:g} s clip" + (f"; device={dev}" if t == "embed_afxrep" else ""))
        if "embed_afxrep" in terms:
            row("seconds_per_embedding", timing.get("embed_afxrep:features", 0.0),
                f"term=embed_afxrep AFx-Rep cost: one {mf['seconds']:g} s clip embedded; device={dev}; "
                f"threads={threads}; jobs={a.jobs}")
        if render.get("renders"):
            row("seconds_per_test", render["wall_s_per_render"] + timing.get("ltas_tilt:features", 0.0)
                + timing.get("crest_flatness_hnr:features", 0.0) + timing.get("envelope:features", 0.0),
                f"term=render+dsp_terms: wall seconds per candidate = Native render at {render['hosts']} host(s) "
                f"({render['wall_s_per_render']:.3f} s; by hosts "
                + "/".join(f"{h}:{v:.3f}" for h, v in render["wall_s_per_render_by_hosts"].items())
                + f"; {render['per_host_s']:.3f} s per render per host) + "
                f"the three no-DI DSP terms; {mf['seconds']:g} s clip", render["renders"], render["wall_s"])
    return 0


def ledger_row(term: str, subset: str, value: float, wall: float, note_extra: str) -> None:
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}
    notes = (f"PROXY not Helix: numpy DSP chains + synthetic DI; not admission evidence; "
             f"term={term} subset={subset} {note_extra}").replace(",", ";")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                    "--engine", "-", "--dataset", PROXY_DATASET, "--metric", "rank1_rate",
                    "--value", f"{value:.4f}", "--wall-clock-s", f"{wall:.1f}", "--notes", notes],
                   check=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--term", choices=IMPLEMENTED, nargs="+", help="one or more terms, same items")
    g.add_argument("--all", action="store_true", help="every numpy DSP term (not the embeddings)")
    ap.add_argument("--proxy", action="store_true", help="offline numpy DSP proxy (not Helix)")
    ap.add_argument("--helix", action="store_true",
                    help="the Helix Native trust set helix-trust-v1 (scripts/helix_trust_set.py)")
    ap.add_argument("--helix-dir", default=None, help="Helix set folder (default cache/loss-trust/helix-trust-v1)")
    ap.add_argument("--items", type=int, default=None, help="items (proxy default 40; Helix: all)")
    ap.add_argument("--distractors", type=int, default=15)
    ap.add_argument("--seed", type=int, nargs="+", default=[2026],
                    help="item seed(s), pooled; feature scales were tuned on seeds 0 and 1, never "
                         "evaluate on those")
    ap.add_argument("--mask", action="store_true", help="pass the proxy separator's confidence mask")
    ap.add_argument("--seconds", type=float, default=12.0, help="clip length (sections are 10-30 s)")
    ap.add_argument("--bleed-db", type=float, default=-12.0, help="proxy separation bleed level")
    ap.add_argument("--no-sep", action="store_true", help="diagnostic: no separation artifacts")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--out", type=Path, default=None, help="JSON report path (default under cache/)")
    ap.add_argument("--ledger", action="store_true", help="append loss-trust rows (proxy dataset)")
    ap.add_argument("--note", default="", help="extra text for the ledger notes")
    a = ap.parse_args(argv)

    terms = list(DSP_TERMS) if a.all else list(dict.fromkeys(a.term))
    embed = [t for t in terms if t in EMBED_TERMS]
    threads = 1
    if embed:
        from tonematch.metrics import afxrep
        if not afxrep.available():
            print(f"{embed}: model unavailable (pip install -e .[embed] and the checkpoint in "
                  f"{afxrep.model_dir()}; docs/notes/afx-rep.md)", file=sys.stderr)
            return 2
        if a.jobs > MAX_EMBED_JOBS:
            print(f"embedding terms: --jobs capped at {MAX_EMBED_JOBS}", file=sys.stderr)
            a.jobs = MAX_EMBED_JOBS
        threads = max(1, 2 // max(a.jobs, 1))
    if a.helix:
        return run_helix(a, terms, threads)
    if a.items is None:
        a.items = 40
    if not a.proxy:
        manifest = ROOT / "fixtures" / "synthetic" / "manifest.json"
        print(f"no synthetic set: {manifest} is missing (benchmark builds it in T024 with "
              "synthetic-benchmark --generate). Run with --proxy for the offline proxy check.",
              file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    bleed = None if a.no_sep else a.bleed_db
    if {0, 1} & set(a.seed):
        print("warning: seeds 0 and 1 were used to tune the term scales; this is not held out",
              file=sys.stderr)
    jobs = [(i, sd, a.distractors, terms, a.mask, a.seconds, bleed, threads) for sd in a.seed
            for i in range(a.items)]
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            items = list(ex.map(proxy_item, jobs))
    else:
        items = [proxy_item(j) for j in jobs]
    wall = time.perf_counter() - t0
    names = terms + ([COMBO] if COMBO in items[0]["heldout_notes"] else [])
    summ = summarise(items, names)
    per_seed = {str(sd): {subset: {t: float(np.mean([it[subset][t]["rank"] == 1 for it in
                                                      items[k * a.items:(k + 1) * a.items]]))
                                   for t in names} for subset in SUBSETS}
                for k, sd in enumerate(a.seed)}
    report = {"dataset": PROXY_DATASET, "proxy": True,
              "warning": "PROXY: numpy DSP chains and synthetic DI; not Helix; not admission evidence",
              "items": a.items, "distractors": a.distractors, "near_distractors": a.distractors // 2,
              "seed": a.seed, "per_seed_rank1": per_seed, "mask": a.mask, "seconds": a.seconds, "bleed_db": bleed, "wall_clock_s": round(wall, 1), "summary": summ,
              "chance_rank1_rate": 1.0 / (a.distractors + 1), "per_item": items}
    tag = ("-mask" if a.mask else "") + ("-nosep" if a.no_sep else "") + f"-{a.seconds:g}s"
    out = a.out or ROOT / "cache" / "loss-trust" / f"{PROXY_DATASET}{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))

    print(f"PROXY loss-trust-check ({PROXY_DATASET}; NOT Helix): {a.items * len(a.seed)} items "
          f"(seeds {a.seed}) x "
          f"{a.distractors} distractors ({a.distractors // 2} near); chance = {1 / (a.distractors + 1):.3f}")
    print(f"{'term':19s} | heldout: {'r1':>5s} {'rank':>5s} {'r1rnd':>5s} {'r1near':>6s} | same: "
          f"{'r1':>5s} {'rank':>5s} {'r1rnd':>5s} {'r1near':>6s} | top beaten-by (heldout)")
    for t in names:
        h, s = summ["heldout_notes"][t], summ["same_notes"][t]
        top = sorted(h["beaten_by"].items(), key=lambda kv: -kv[1])[:3]
        print(f"{t:19s} |          {h['rank1_rate']:5.3f} {h['mean_rank']:5.2f} "
              f"{h['rank1_vs_random_only']:5.3f} {h['rank1_vs_near_only']:6.3f} |       "
              f"{s['rank1_rate']:5.3f} {s['mean_rank']:5.2f} {s['rank1_vs_random_only']:5.3f} "
              f"{s['rank1_vs_near_only']:6.3f} | {top}")
    for t in names:
        pw = summ["heldout_notes"][t]["pairwise_win"]
        print(f"  {t:17s} heldout pairwise win: " + " ".join(f"{k}={v:.2f}" for k, v in pw.items()))
    print(f"report: {out}  ({wall:.1f} s)")

    if a.ledger:
        seeds = "+".join(str(sd) for sd in a.seed)
        extra = (f"items={a.items * len(a.seed)} distractors={a.distractors} near={a.distractors // 2} "
                 f"seed={seeds} seconds={a.seconds:g} bleed_db={bleed} mask={int(a.mask)}")
        for t in terms:
            for subset in SUBSETS:
                sm = summ[subset][t]
                diag = (f" rank1_vs_random={sm['rank1_vs_random_only']:.3f} pairwise_win_mean="
                        f"{np.mean([v for v in sm['pairwise_win'].values()]):.3f}")
                if len(a.seed) > 1:
                    diag += " per_seed=" + "/".join(f"{per_seed[str(sd)][subset][t]:.3f}" for sd in a.seed)
                ledger_row(t, subset, sm["rank1_rate"], wall,
                           extra + diag + (f" {a.note}" if a.note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
