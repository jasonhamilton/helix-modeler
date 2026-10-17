#!/usr/bin/env python3
"""Helix trust set `helix-trust-v2`: the known-DI case (owner: tone-metrics, T096).

Gate 2 option A (human, 2026-10-16): tune `mrstft` for the case where the DI is known and aligned,
which is what Gate 2's synthetic recovery uses. Every candidate is rendered on the target's own DI
clip (the same notes). Design registered in docs/DECISIONS.md BEFORE any scoring (T096).

  gen     offline: draw the split, items, DI excerpts and candidates; writes
          cache/loss-trust/helix-trust-v2/manifest.json and di/*.wav (local, gitignored: model ids)
  render  Native: NativeRenderPool on the block library templates, at most 4 hosts, and never more
          than 6 native_host processes on the machine counting other agents'; resumable; closes
          every host; per-chunk timings in render_summary.json

Set: single blocks from the Native block library (`block-library-v1.2`) in the 8 categories of
synthetic-v1 (drive, delay, reverb, comp, eq, mod, amp, cab) with synthetic-v1's eligibility rule
(no time-variant blocks, the Native stand-in list for reverb and modulation). Knobs on the CC grid at
0.10-0.90 of each template range (synthetic-v1's `draw_knobs`, `near_knobs`). Per item: the true
preset, 9 near misses (same model, 1-2 knobs moved 0.12-0.30 of their range), 4 neighbouring models
of the same category (random knobs) and random blocks of other categories to make 19 distractors.

Split: models of a category are split in half between train and test (disjoint) when the category
has at least 8 eligible models; reverb (5 models) is shared. DI recordings (Guitar-TECHS files) are
split in half (disjoint). Each split draws its targets, neighbours, random distractors and DI clips
from its own half only. 12 items per category per split.

Target conditions (built at score time from the true preset's render, scripts/loss_trust_check.py):
  clean    the render itself: exactly Gate 2's synthetic recovery (the true candidate equals the
           target, so it ranks first by construction; reported as a check, not as evidence)
  aligned  the render as an aligned capture would give it: residual fractional delay U(-0.5, 0.5)
           samples, gain U(-3, 3) dB, pink noise 60 dB below the render's RMS
  sep      the proxy separation stand-in of T067 (jittered ratio mask, band bleed at -12 dB)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

SR = 48_000
DATASET = "helix-trust-v2"
OUT = ROOT / "cache" / "loss-trust" / DATASET
SEED = 4102
LIBRARY = "block-library-v1.2"
SECONDS = 10.0
ITEMS_PER_CATEGORY_PER_SPLIT = 12
N_DISTRACTORS = 19
N_NEAR = 9
N_NBR = 4
SPLIT_MIN_MODELS = 8
SPLITS = ("train", "test")
CONDITIONS = ("clean", "aligned", "sep")
ALIGNED = {"frac_delay_samples": 0.5, "gain_db": 3.0, "noise_db_re_rms": -60.0}
MAX_HOSTS = 4
MAX_MACHINE_HOSTS = 6


def _rel(f: Path) -> str:
    """Path relative to the repo when inside it (the manifest is portable), else absolute."""
    try:
        return str(f.relative_to(ROOT))
    except ValueError:
        return str(f)


def item_dir(i: int) -> str:
    return f"item{i:03d}"


def render_path(root: Path, i: int, k: int) -> Path:
    return root / "renders" / item_dir(i) / f"c{k:02d}_A.npy"


# --------------------------------------------------------------------------- generate

def eligible_models(catalog) -> dict[str, dict[str, list]]:
    """category -> {model: free params} for every eligible library model (synthetic-v1's rule)."""
    import synthetic_v1 as SV

    from tonematch.catalog import native_unavailable
    from tonematch.engines import native_library as NL

    if NL.VERSION != LIBRARY:
        raise SystemExit(f"block library is {NL.VERSION}, the set is registered on {LIBRARY}")
    idx = NL.load_index()["models"]
    tv = {m for m, e in idx.items() if e.get("time_variant")} | set(native_unavailable())
    out: dict[str, dict[str, list]] = {}
    for c in SV.CATEGORIES:
        out[c] = {}
        for m in sorted(m for m, e in idx.items() if e["category"] == c):
            if not SV.eligible(m, c, tv) or NL.is_routing(m):
                continue
            tpl = NL.template_for(m)
            fp = SV.free_params(m, tpl, catalog, c) if tpl is not None else []
            if fp:
                out[c][m] = [list(f) for f in fp]
    return out


def split_models(rng: np.random.Generator, models: dict[str, dict[str, list]]) -> dict[str, dict[str, list[str]]]:
    split = {s: {} for s in SPLITS}
    for c, ms in models.items():
        names = sorted(ms)
        if len(names) >= SPLIT_MIN_MODELS:
            perm = [names[i] for i in rng.permutation(len(names))]
            h = len(perm) // 2
            split["train"][c], split["test"][c] = sorted(perm[:h]), sorted(perm[h:])
        else:
            split["train"][c] = split["test"][c] = names
    return split


def split_files(rng: np.random.Generator, runs: list[dict]) -> dict[str, list[str]]:
    files = sorted({r["path"] for r in runs})
    perm = [files[i] for i in rng.permutation(len(files))]
    h = len(perm) // 2
    return {"train": sorted(perm[:h]), "test": sorted(perm[h:])}


def pick_clip(rng, runs_by_file: dict[str, list[dict]], files: list[str], used: dict[str, list[float]]) -> dict:
    """One 10 s excerpt from a random file of the split, not overlapping an earlier pick."""
    for _ in range(10_000):
        f = files[int(rng.integers(len(files)))]
        cands = [r for r in runs_by_file[f] if all(abs(r["start_s"] - s) >= SECONDS for s in used.get(f, []))]
        if cands:
            r = dict(cands[int(rng.integers(len(cands)))])
            used.setdefault(f, []).append(r["start_s"])
            return r
    raise SystemExit("ran out of DI excerpts")


def cmd_gen(a) -> int:
    import helix_trust_set as HT
    import soundfile as sf
    import synthetic_v1 as SV

    from tonematch.catalog import load_catalog

    root = Path(a.out)
    mfp = root / "manifest.json"
    if mfp.exists() and not a.force:
        print(f"{mfp} exists; the set is registered, refusing to redraw it (--force to overwrite)", file=sys.stderr)
        return 3
    catalog = load_catalog()
    rng = np.random.default_rng(a.seed)
    models = eligible_models(catalog)
    msplit = split_models(rng, models)
    runs = HT.usable_runs(SECONDS)
    fsplit = split_files(rng, runs)
    by_file: dict[str, list[dict]] = {}
    for r in runs:
        by_file.setdefault(r["path"], []).append(r)
    used: dict[str, list[float]] = {}
    (root / "di").mkdir(parents=True, exist_ok=True)
    items = []
    for split in SPLITS:
        ms = msplit[split]
        order = {c: [ms[c][i] for i in rng.permutation(len(ms[c]))] for c in SV.CATEGORIES}
        for j in range(ITEMS_PER_CATEGORY_PER_SPLIT):
            for c in SV.CATEGORIES:
                i = len(items)
                model = order[c][j % len(order[c])]
                free = models[c][model]
                ks = SV.draw_knobs(rng, free)
                e = pick_clip(rng, by_file, fsplit[split], used)
                x, g = HT.load_excerpt(e, SECONDS, SV.REF_LOUD_DBFS)
                f = root / "di" / f"{item_dir(i)}_A.wav"
                sf.write(str(f), x, SR, subtype="FLOAT")
                cands = [{"label": "true", "model": model, "category": c, "knobs": ks}]
                for _ in range(N_NEAR):
                    cands.append({"label": "near", "model": model, "category": c,
                                  "knobs": SV.near_knobs(rng, ks)})
                others_c = [m for m in ms[c] if m != model]
                for k in rng.permutation(len(others_c))[:N_NBR]:
                    m = others_c[int(k)]
                    cands.append({"label": f"nbr:{m}", "model": m, "category": c,
                                  "knobs": SV.draw_knobs(rng, models[c][m])})
                pool = [(oc, om) for oc in SV.CATEGORIES if oc != c for om in ms[oc]]
                while len(cands) < N_DISTRACTORS + 1:
                    oc, om = pool[int(rng.integers(len(pool)))]
                    cands.append({"label": f"random:{oc}", "model": om, "category": oc,
                                  "knobs": SV.draw_knobs(rng, models[oc][om])})
                items.append({"item": i, "dir": item_dir(i), "split": split, "category": c, "true_model": model,
                              "di": {"A": {**e, "gain_db": round(g, 2), "file": _rel(f)}},
                              "candidates": cands})
    mf = {"dataset": DATASET, "seed": a.seed, "seconds": SECONDS, "distractors": N_DISTRACTORS,
          "library_version": LIBRARY, "ref_loud_dbfs": SV.REF_LOUD_DBFS, "conditions": list(CONDITIONS),
          "subsets": [f"{c}/same_notes" for c in CONDITIONS], "aligned": ALIGNED, "sep_bleed_db": -12.0,
          "splits": {s: {"items": sum(it["split"] == s for it in items), "models": msplit[s],
                         "di_files": fsplit[s]} for s in SPLITS},
          "free_params": models, "items": items,
          "about": "known-DI case: every candidate rendered on the target's DI A (same notes); target "
                   "conditions clean / aligned / sep built at score time; train split for tuning only"}
    mfp.write_text(json.dumps(mf, indent=1))
    shared = [c for c in SV.CATEGORIES if msplit["train"][c] == msplit["test"][c]]
    print(f"{len(items)} items ({ITEMS_PER_CATEGORY_PER_SPLIT} per category per split) x {N_DISTRACTORS} "
          f"distractors; models per split " + " ".join(f"{c}={len(msplit['train'][c])}/{len(msplit['test'][c])}"
                                                    for c in SV.CATEGORIES)
          + f"; shared {shared}; DI files {len(fsplit['train'])}/{len(fsplit['test'])}; {mfp}")
    return 0


# --------------------------------------------------------------------------- render

def machine_hosts() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True,
                       check=False)
    return len([x for x in r.stdout.split() if x.strip()])


def build_preset(cand: dict, free: dict, catalog, cache: dict):
    import copy

    import synthetic_v1 as SV

    from tonematch.engines import native_library as NL

    m = cand["model"]
    if m not in cache:
        cache[m] = NL.block_preset(m, catalog)
    p = copy.deepcopy(cache[m])
    rng = {n: (lo, hi) for n, lo, hi, _ in free[cand["category"]][m]}
    for n, k in cand["knobs"].items():
        p.slots[0].params[n] = SV.grid_value(*rng[n], k)
    return p


def cmd_render(a) -> int:
    import soundfile as sf

    from tonematch.catalog import load_catalog

    root = Path(a.out)
    mf = json.loads((root / "manifest.json").read_text())
    hosts = min(a.hosts, MAX_HOSTS)
    running = machine_hosts()
    if running + hosts > MAX_MACHINE_HOSTS:
        hosts = MAX_MACHINE_HOSTS - running
        if hosts < 1:
            print(f"{running} native_host processes already running; limit {MAX_MACHINE_HOSTS}", file=sys.stderr)
            return 3
    print(f"native_host processes before start: {running}; opening {hosts}", flush=True)
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeRenderPool, RenderJob

    catalog = load_catalog()
    cache: dict = {}
    todo = [it for it in mf["items"] if (a.only is None or it["item"] in a.only)
            and any(not render_path(root, it["item"], k).exists() for k in range(len(it["candidates"])))]
    if not todo:
        print("nothing to render")
        return 0
    summ_path = root / "render_summary.json"
    summ = json.loads(summ_path.read_text()) if summ_path.exists() else {"chunks": []}
    t0 = time.perf_counter()
    pool = NativeRenderPool(workers=hosts, templates_dir=NL.library_templates())
    t_open = time.perf_counter() - t0
    print(f"pool open: {pool.workers} hosts in {t_open:.1f} s; machine now {machine_hosts()} native_host", flush=True)
    errors: dict[str, str] = {}
    silent: dict[str, str] = {}
    try:
        for s in range(0, len(todo), a.chunk):
            chunk = todo[s:s + a.chunk]
            jobs, keys = [], []
            for it in chunk:
                dry = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0]
                for k, cd in enumerate(it["candidates"]):
                    if render_path(root, it["item"], k).exists():
                        continue
                    jobs.append(RenderJob(dry=dry, preset=build_preset(cd, mf["free_params"], catalog, cache)))
                    keys.append((it["item"], k))
            if not jobs:
                continue
            tc = time.perf_counter()
            outs = pool.render_batch(jobs, on_error="none")
            wall = time.perf_counter() - tc
            for j, ((i, k), y) in enumerate(zip(keys, outs, strict=True)):
                if y is None:
                    err = repr(pool.last_errors.get(j))
                    if "returned silence" not in err:
                        errors[f"{i}:{k}"] = err
                        continue
                    # the setting mutes the block (output below -100 dBFS RMS: Master or Output at 0, a
                    # swell threshold never reached). A silent candidate is a valid, audible distractor.
                    y = np.zeros(len(jobs[j].dry), np.float32)
                    silent[f"{i}:{k}"] = err
                y = np.asarray(y, np.float32)
                if y.ndim == 2:
                    y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
                f = render_path(root, i, k)
                f.parent.mkdir(parents=True, exist_ok=True)
                np.save(f, y)
            item_s = list(pool.last_item_s)
            rec = {"items": [it["item"] for it in chunk], "renders": len(jobs), "wall_s": round(wall, 2),
                   "hosts": pool.workers, "item_s_mean": round(float(np.mean(item_s)), 4) if item_s else None,
                   "stats": pool.stats()}
            summ["chunks"].append(rec)
            summ_path.write_text(json.dumps(summ, indent=1))
            print(f"items {rec['items'][0]}-{rec['items'][-1]}: {len(jobs)} renders in {wall:.1f} s "
                  f"({wall / len(jobs):.3f} s/render wall); mem {rec['stats']['mem_mb']} MB", flush=True)
    finally:
        pool.close()
    old = {k: v for k, v in summ.get("errors", {}).items() if k not in silent}
    summ["errors"] = {**{k: v for k, v in old.items() if not render_path(root, *map(int, k.split(":"))).exists()},
                      **errors}
    summ["silent_as_zeros"] = {**summ.get("silent_as_zeros", {}), **silent}
    summ["open_s"] = round(t_open, 1)
    summ_path.write_text(json.dumps(summ, indent=1))
    print(f"done; errors {len(errors)}; silent renders kept as zeros {len(silent)}; machine native_host after close: {machine_hosts()}", flush=True)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--seed", type=int, default=SEED)
    g.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=MAX_HOSTS)
    r.add_argument("--chunk", type=int, default=8, help="items per render batch")
    r.add_argument("--only", type=int, nargs="+", default=None)
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
