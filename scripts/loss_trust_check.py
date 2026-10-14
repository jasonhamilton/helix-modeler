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
  (default) fixtures/synthetic/manifest.json from `synthetic-benchmark` (benchmark, T024). Not built
            yet: the script exits 2 until the manifest and its renders exist.

    python scripts/loss_trust_check.py --proxy --all [--items 40] [--distractors 15] [--ledger]

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
    IMPLEMENTED,
    NO_DI_TERMS,
    PROVISIONAL_WEIGHTS,
    LossKit,
    proxy,
)

PROXY_DATASET = "proxy-dsp-v0"
COMBO = "provisional_sum"
SUBSETS = ("heldout_notes", "same_notes")


def _rank(scores: np.ndarray) -> int:
    """1-based rank of candidate 0 (the true preset); ties count against it."""
    return int(1 + np.sum(scores[1:] <= scores[0]))


def proxy_item(args: tuple) -> dict:
    i, seed, n_distract, terms, use_mask, seconds, bleed_db = args
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
        combo = sum(PROVISIONAL_WEIGHTS[t] * np.asarray(per[t]) for t in NO_DI_TERMS if t in per)
        if not np.isscalar(combo):
            res[COMBO] = {"rank": _rank(combo), "winner": labels[int(np.argmin(combo))],
                          "scores": np.asarray(combo).tolist()}
        out[subset] = res
    return out


def summarise(items: list[dict], names: list[str]) -> dict:
    summ = {}
    for subset in SUBSETS:
        summ[subset] = {}
        for t in names:
            ranks = np.array([it[subset][t]["rank"] for it in items])
            beaten_by: dict[str, int] = {}
            for it in items:
                w = it[subset][t]["winner"]
                if w != "true":
                    beaten_by[w] = beaten_by.get(w, 0) + 1
            pools = {}
            for pool in ("random", "near"):
                r1 = []
                for it in items:
                    sc = np.asarray(it[subset][t]["scores"])
                    keep = [0] + [k for k, lab in enumerate(it["labels"]) if lab.startswith(pool)]
                    r1.append(_rank(sc[keep]) == 1)
                pools[pool] = float(np.mean(r1))
            summ[subset][t] = {"rank1_rate": float(np.mean(ranks == 1)),
                               "mean_rank": float(ranks.mean()),
                               "rank1_vs_random_only": pools["random"],
                               "rank1_vs_near_only": pools["near"],
                               "beaten_by": dict(sorted(beaten_by.items()))}
    return summ


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
    g.add_argument("--term", choices=IMPLEMENTED)
    g.add_argument("--all", action="store_true", help="every implemented term")
    ap.add_argument("--proxy", action="store_true", help="offline numpy DSP proxy (not Helix)")
    ap.add_argument("--items", type=int, default=40)
    ap.add_argument("--distractors", type=int, default=15)
    ap.add_argument("--seed", type=int, default=2026,
                    help="item seed; the weights were tuned on seeds 0 and 1, keep this different")
    ap.add_argument("--mask", action="store_true", help="pass the proxy separator's confidence mask")
    ap.add_argument("--seconds", type=float, default=12.0, help="clip length (sections are 10-30 s)")
    ap.add_argument("--bleed-db", type=float, default=-12.0, help="proxy separation bleed level")
    ap.add_argument("--no-sep", action="store_true", help="diagnostic: no separation artifacts")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--out", type=Path, default=None, help="JSON report path (default under cache/)")
    ap.add_argument("--ledger", action="store_true", help="append loss-trust rows (proxy dataset)")
    a = ap.parse_args(argv)

    terms = list(IMPLEMENTED) if a.all else [a.term]
    if not a.proxy:
        manifest = ROOT / "fixtures" / "synthetic" / "manifest.json"
        print(f"no synthetic set: {manifest} is missing (benchmark builds it in T024 with "
              "synthetic-benchmark --generate). Run with --proxy for the offline proxy check.",
              file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    bleed = None if a.no_sep else a.bleed_db
    jobs = [(i, a.seed, a.distractors, terms, a.mask, a.seconds, bleed) for i in range(a.items)]
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            items = list(ex.map(proxy_item, jobs))
    else:
        items = [proxy_item(j) for j in jobs]
    wall = time.perf_counter() - t0
    names = terms + ([COMBO] if COMBO in items[0]["heldout_notes"] else [])
    summ = summarise(items, names)
    report = {"dataset": PROXY_DATASET, "proxy": True,
              "warning": "PROXY: numpy DSP chains and synthetic DI; not Helix; not admission evidence",
              "items": a.items, "distractors": a.distractors, "near_distractors": a.distractors // 2,
              "seed": a.seed, "mask": a.mask, "seconds": a.seconds, "bleed_db": bleed, "wall_clock_s": round(wall, 1), "summary": summ,
              "chance_rank1_rate": 1.0 / (a.distractors + 1), "per_item": items}
    tag = ("-mask" if a.mask else "") + ("-nosep" if a.no_sep else "") + f"-{a.seconds:g}s"
    out = a.out or ROOT / "cache" / "loss-trust" / f"{PROXY_DATASET}{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))

    print(f"PROXY loss-trust-check ({PROXY_DATASET}; NOT Helix): {a.items} items x "
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
    print(f"report: {out}  ({wall:.1f} s)")

    if a.ledger:
        extra = (f"items={a.items} distractors={a.distractors} near={a.distractors // 2} seed={a.seed} "
                 f"seconds={a.seconds:g} bleed_db={bleed} mask={int(a.mask)}")
        for t in terms:
            for subset in SUBSETS:
                ledger_row(t, subset, summ[subset][t]["rank1_rate"], wall, extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
