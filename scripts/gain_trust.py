#!/usr/bin/env python3
"""Gain-stage loss trust: sets `gain-trust-v1` (T144) and `gain-trust-v1b` (T146) and the score check
(owner: tone-metrics).

Registered in docs/DECISIONS.md 2026-10-17 (T144) BEFORE any candidate was scored. `gain-trust-v1b`
(T146, 2026-10-18) is a fresh held-out split drawn with the same recipe: helix-trust-v2's TEST models
and DI files, new excerpts that avoid every excerpt of the synthetic sets, helix-trust-v2 and both
gain-trust-v1 splits. synthetic-v3's excerpts are found from its DI audio (matched against the DI
library's 4 s grid), never from its labels. Rank-1 uses the extended rule B (human 2026-10-18): a
level-only neighbour within the aligned capture's own gain jitter is a tie (`--rule direct` = old).

  gen       offline: draw gain-trust-v1 (amp and drive, the true preset against 16 gain-stage
            neighbours), on helix-trust-v2's own model and DI-file splits, avoiding every DI excerpt of
            the synthetic sets and of helix-trust-v2. Writes cache/loss-trust/gain-trust-v1/
  render    Native, through helix_trust_v2's renderer (NativeRenderPool, lease from the host scaler),
            at most 2 hosts
  features  per item and candidate: mrstft_v2, level_db, ltas_tilt, crest_flatness_hnr, envelope against
            the aligned capture of the truth (and mrstft_v2 against the clean truth), the plain null and
            the gain-matched null to the truth. Cached as features-<dataset>.json; at most 2 processes
  eval      rule-B rank-1 per family for named scores (weighted sums of the cached features) on a split
  diag-v2   DIAGNOSIS ONLY (synthetic-v2 is spent): the six attempt-2 misses under a score, from the
            renders in cache/gain-trust/diag-v2/

Scores are written `term=w,term=w`, e.g. `mrstft_v2=1,level_db=1e-6`.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

SR = 48_000
DATASET = "gain-trust-v1"
DATASET_B = "gain-trust-v1b"
GAIN_SETS = (DATASET, DATASET_B)
OUT = ROOT / "cache" / "loss-trust" / DATASET
SEED_B = 1461
# gain-trust-v1b: the DI library's 10 s excerpts on helix-trust-v2's test files are used up (3 left
# after avoiding 1,488 excerpts), so v1b uses 8 s excerpts: 91 free slots there (DECISIONS T146).
SECONDS_B = 8.0
USED_SECONDS = 10.0   # every avoided set's clips are 10 s
HTV2 = ROOT / "cache" / "loss-trust" / "helix-trust-v2"
SEED = 1442
SECONDS = 10.0
CATEGORIES = ("amp", "drive")
ITEMS_PER_CATEGORY_PER_SPLIT = 48
N_NEAR = 9            # synthetic_v1.near_knobs, the gates' own near-miss generator
N_PAIR = 4            # gain-compensated pairs
N_SMALL = 3           # small single-knob moves
PAIR_STEP = (0.12, 0.30)
PAIR_COMP = (0.5, 1.0)
SMALL_STEP = (0.06, 0.12)
MAX_HOSTS = 2
AVOID_SETS = ("synthetic-v1", "synthetic-v2", "synthetic-v3", "synthetic-devfam", "synthetic-devgm",
              "synthetic-lindev")
# gain-trust-v1b: these sets' DI fields only (labels.json "di" or manifest "di"); synthetic-v3 by audio
AVOID_SETS_B = ("synthetic-v1", "synthetic-v2", "synthetic-devfam", "synthetic-devgm", "synthetic-lindev")
AUDIO_ONLY_SETS_B = ("synthetic-v3",)
TERMS = ("mrstft_v2", "level_db", "ltas_tilt", "crest_flatness_hnr", "envelope")
SPLIT_NAMES = {"fit": "train", "test": "test"}   # gain-trust-v1 "fit" uses helix-trust-v2 "train" halves
V2_MISSES = ("i022", "i046", "i040", "i056", "i072", "i055")

LEVEL_WORDS = ("master", "output", "level", "volume", "vol", "postgain")
GAIN_WORDS = ("drive", "gain", "fuzz", "sustain", "distortion", "dist", "boost")


def is_level(name: str) -> bool:
    n = name.lower().replace(" ", "")
    return any(w in n for w in LEVEL_WORDS)


def is_gain(name: str) -> bool:
    n = name.lower().replace(" ", "")
    return not is_level(name) and any(w in n for w in GAIN_WORDS)


def _move(rng, k: int, step) -> int:
    d = rng.uniform(*step) * (1 if rng.random() < 0.5 else -1)
    u = k / 127 + d
    u = -u if u < 0 else (2 - u if u > 1 else u)
    return int(np.clip(round(u * 127), 0, 127))


def pair_knobs(rng, ks: dict[str, int]) -> dict[str, int]:
    """A gain-like knob moved PAIR_STEP and a level-like knob moved the other way by PAIR_COMP of the
    step (louder gain, quieter level: the loudness-compensated neighbour). Two random knobs moved when
    the model has no such pair."""
    import synthetic_v1 as SV

    g = sorted(n for n in ks if is_gain(n))
    lv = sorted(n for n in ks if is_level(n))
    if not g or not lv:
        out = dict(ks)
        names = sorted(ks)
        for n in rng.choice(names, size=min(2, len(names)), replace=False):
            out[n] = _move(rng, ks[n], SV.NEAR_DELTA)
        return out if out != ks else SV.near_knobs(rng, ks)
    gn, ln = g[int(rng.integers(len(g)))], lv[int(rng.integers(len(lv)))]
    out = dict(ks)
    d = rng.uniform(*PAIR_STEP) * (1 if rng.random() < 0.5 else -1)
    for n, dd in ((gn, d), (ln, -d * rng.uniform(*PAIR_COMP))):
        u = ks[n] / 127 + dd
        u = -u if u < 0 else (2 - u if u > 1 else u)
        out[n] = int(np.clip(round(u * 127), 0, 127))
    if out == ks:
        out[gn] = min(127, ks[gn] + 16) if ks[gn] < 64 else ks[gn] - 16
    return out


def small_knobs(rng, ks: dict[str, int]) -> dict[str, int]:
    names = sorted(ks)
    n = names[int(rng.integers(len(names)))]
    out = dict(ks)
    out[n] = _move(rng, ks[n], SMALL_STEP)
    if out[n] == ks[n]:
        out[n] = min(127, ks[n] + 8) if ks[n] < 64 else ks[n] - 8
    return out


def free_slots(runs: list[dict], files, used: dict[str, list[float]], seconds: float) -> list[dict]:
    """Every excerpt of `seconds` on the given files that overlaps no avoided excerpt (each
    USED_SECONDS long) by any time, packed earliest-first per file, so no two slots overlap."""
    files = set(files)
    taken = {k: [(s0, s0 + USED_SECONDS) for s0 in v] for k, v in used.items()}
    out = []
    for r in sorted((r for r in runs if r["path"] in files), key=lambda r: (r["path"], r["start_s"])):
        a0, a1 = r["start_s"], r["start_s"] + seconds
        if all(a1 <= u0 or a0 >= u1 for u0, u1 in taken.get(r["path"], [])):
            taken.setdefault(r["path"], []).append((a0, a1))
            out.append(dict(r))
    return out


def avoided_excerpts(sets=AVOID_SETS, audio_sets=(), gain_sets=()) -> dict[str, list[float]]:
    """file -> start times already used by the synthetic sets, helix-trust-v2 and the named gain-trust
    sets (DI fields only). `audio_sets` are found from their DI audio (`excerpts_from_di_audio`)."""
    import synthetic_v1 as SV

    used: dict[str, list[float]] = {}
    names = [n for n in sets if (ROOT / "cache" / n / "labels.json").exists()]
    missing = [n for n in sets if n not in names]
    if missing:
        raise SystemExit(f"cannot avoid {missing}: no labels.json")
    for e in SV.used_excerpts(names):
        used.setdefault(e["path"], []).append(float(e["start_s"]))
    for mfp in [HTV2 / "manifest.json"] + [ROOT / "cache" / "loss-trust" / g / "manifest.json" for g in gain_sets]:
        for it in json.loads(mfp.read_text())["items"]:
            for d in it["di"].values():
                used.setdefault(d["path"], []).append(float(d["start_s"]))
    for n in audio_sets:
        for e in excerpts_from_di_audio(n):
            used.setdefault(e["path"], []).append(float(e["start_s"]))
    return used


PROBE = (24_000, 28_800)      # 0.5-0.6 s into a clip: clear of the resampler's edge


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / (float(np.linalg.norm(v)) or 1.0)


def excerpts_from_di_audio(set_name: str) -> list[dict]:
    """(path, start_s) of every DI clip of a sealed set, found from the clip audio alone (its labels
    are never opened): each clip's 0.5-0.6 s probe is matched, gain-invariantly, against the same
    probe of every 10 s run on the DI library's 4 s grid (`helix_trust_set.usable_runs`), which is
    where every set's excerpts start. Every clip must match one run with correlation > 0.9999.
    Cached in cache/gain-trust/di-positions-<set>.json (positions only)."""
    import helix_trust_set as HT
    import soundfile as sf

    from tonematch.dry import _read

    cp = ROOT / "cache" / "gain-trust" / f"di-positions-{set_name}.json"
    if cp.exists():
        return json.loads(cp.read_text())["excerpts"]
    clips = sorted((ROOT / "cache" / set_name / "di").glob("*.wav"))
    q = np.stack([_unit(sf.read(str(f), dtype="float32")[0][PROBE[0]:PROBE[1]]) for f in clips])
    runs = HT.usable_runs(SECONDS)
    lib = np.stack([_unit(_read(HT.DI_LIB / r["path"], r["start_s"], 1.0)[PROBE[0]:PROBE[1]]) for r in runs])
    c = q @ lib.T
    best = c.argmax(axis=1)
    out, bad = [], []
    for k, f in enumerate(clips):
        j = int(best[k])
        if c[k, j] <= 0.9999:
            bad.append((f.name, float(c[k, j])))
        out.append({"clip": f.name, "path": runs[j]["path"], "start_s": float(runs[j]["start_s"]),
                    "corr": round(float(c[k, j]), 7)})
    if bad:
        raise SystemExit(f"{set_name}: {len(bad)} DI clips match no library run: {bad[:5]}")
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"set": set_name, "how": "DI audio matched to the library grid; labels not read",
                              "excerpts": out}, indent=1))
    return out


# --------------------------------------------------------------------------- gen

def cmd_gen(a) -> int:
    import helix_trust_set as HT
    import helix_trust_v2 as H2
    import soundfile as sf
    import synthetic_v1 as SV

    dataset = a.dataset
    root = Path(a.out) if a.out else ROOT / "cache" / "loss-trust" / dataset
    mfp = root / "manifest.json"
    if mfp.exists() and not a.force:
        print(f"{mfp} exists; the set is registered, refusing to redraw it (--force to overwrite)", file=sys.stderr)
        return 3
    h2 = json.loads((HTV2 / "manifest.json").read_text())
    free = {c: h2["free_params"][c] for c in CATEGORIES}
    seed = a.seed if a.seed is not None else (SEED if dataset == DATASET else SEED_B)
    rng = np.random.default_rng(seed)
    seconds = SECONDS if dataset == DATASET else SECONDS_B
    runs = HT.usable_runs(seconds)
    by_file: dict[str, list[dict]] = {}
    for r in runs:
        by_file.setdefault(r["path"], []).append(r)
    if dataset == DATASET:
        avoid_sets, audio_sets, gain_sets, splits = AVOID_SETS, (), (), SPLIT_NAMES
    else:
        avoid_sets, audio_sets, gain_sets = AVOID_SETS_B, AUDIO_ONLY_SETS_B, (DATASET,)
        splits = {"test": "test"}      # held-out only: helix-trust-v2's test models and DI files
    used = avoided_excerpts(avoid_sets, audio_sets, gain_sets)
    used0 = {k: list(v) for k, v in used.items()}
    n_avoid = sum(len(v) for v in used.values())
    (root / "di").mkdir(parents=True, exist_ok=True)
    items = []
    for split, hsplit in splits.items():
        ms = {c: h2["splits"][hsplit]["models"][c] for c in CATEGORIES}
        files = h2["splits"][hsplit]["di_files"]
        order = {c: [ms[c][i] for i in rng.permutation(len(ms[c]))] for c in CATEGORIES}
        slots = None
        n_per = ITEMS_PER_CATEGORY_PER_SPLIT
        if dataset != DATASET:
            free_s = free_slots(runs, files, used, seconds)
            slots = [free_s[k] for k in rng.permutation(len(free_s))]
            n_per = min(n_per, len(slots) // len(CATEGORIES))
        for j in range(n_per):
            for c in CATEGORIES:
                i = len(items)
                model = order[c][j % len(order[c])]
                ks = SV.draw_knobs(rng, free[c][model])
                e = H2.pick_clip(rng, by_file, files, used) if slots is None else slots.pop()
                x, g = HT.load_excerpt(e, seconds, SV.REF_LOUD_DBFS)
                f = root / "di" / f"{H2.item_dir(i)}_A.wav"
                sf.write(str(f), x, SR, subtype="FLOAT")
                cands = [{"label": "true", "model": model, "category": c, "knobs": ks}]
                gens = ([("near", SV.near_knobs)] * N_NEAR + [("pair", pair_knobs)] * N_PAIR
                        + [("small", small_knobs)] * N_SMALL)
                for lab, fn in gens:
                    cands.append({"label": lab, "model": model, "category": c, "knobs": fn(rng, ks)})
                items.append({"item": i, "dir": H2.item_dir(i), "split": split, "category": c, "true_model": model,
                              "di": {"A": {**e, "gain_db": round(g, 2), "file": H2._rel(f)}},
                              "candidates": cands})
    clash = [it["item"] for it in items for d in it["di"].values()
             if any(d["start_s"] < s0 + USED_SECONDS and s0 < d["start_s"] + seconds for s0 in used0.get(d["path"], []))]
    spans = sorted((d["path"], d["start_s"]) for it in items for d in it["di"].values())
    clash += [b for (pa, a0), (pb, b) in itertools.pairwise(spans) if pa == pb and b - a0 < seconds]
    if len({it["true_model"] for it in items} - {m for c in CATEGORIES for sp in splits.values()
                                                  for m in h2["splits"][sp]["models"][c]}):
        raise SystemExit("internal: a model outside the split")
    if clash:
        raise SystemExit(f"internal: items {clash} overlap an avoided excerpt")
    mf = {"dataset": dataset, "seed": seed, "seconds": seconds, "distractors": N_NEAR + N_PAIR + N_SMALL,
          "library_version": H2.LIBRARY, "ref_loud_dbfs": SV.REF_LOUD_DBFS, "conditions": ["clean", "aligned"],
          "subsets": ["aligned/same_notes"], "aligned": H2.ALIGNED,
          "splits": {s: {"items": sum(it["split"] == s for it in items), "helix_trust_v2_split": hs}
                     for s, hs in splits.items()},
          "free_params": free, "items": items, "avoided_excerpts": n_avoid,
          "avoided_sets": list(avoid_sets) + ["helix-trust-v2"] + list(gain_sets),
          "avoided_by_di_audio": list(audio_sets),
          "about": "gain-stage neighbours (T144): truth vs 9 near (synthetic_v1.near_knobs), 4 gain-compensated "
                   "pairs, 3 small moves, same notes; model and DI-file splits are helix-trust-v2's; "
                   "fit split for choices only, test scored once"}
    mfp.write_text(json.dumps(mf, indent=1))
    import hashlib
    print(f"{len(items)} items x {mf['distractors']} neighbours; avoided {n_avoid} excerpts; {mfp} "
          f"sha256 {hashlib.sha256(mfp.read_bytes()).hexdigest()}")
    return 0


def cmd_render(a) -> int:
    import helix_trust_v2 as H2

    args = ["--out", a.out or str(ROOT / "cache" / "loss-trust" / a.dataset), "render", "--hosts", str(min(a.hosts, MAX_HOSTS)), "--chunk", str(a.chunk)]
    return H2.main(args)


# --------------------------------------------------------------------------- features

def _item_features(args) -> dict:
    root, it, seed, aligned = args
    from loss_trust_check import aligned_capture

    from tonematch.metrics import crest, envelope, level, ltas
    from tonematch.metrics import mrstft_v2 as MV2

    rd = Path(root) / "renders" / it["dir"]
    ys = [np.load(rd / f"c{k:02d}_A.npy").astype(np.float64) for k in range(len(it["candidates"]))]
    t = ys[0]
    cap = aligned_capture(t, np.random.default_rng([seed, it["item"], 2]), **aligned)
    out = {"item": it["item"], "split": it["split"], "category": it["category"], "model": it["true_model"],
           "labels": [c["label"] for c in it["candidates"]]}
    out["mrstft_v2"] = [MV2.term(cap, y, None) for y in ys]
    out["mrstft_v2_clean"] = [MV2.term(t, y, None) for y in ys]
    out["level_db"] = [level.term(cap, y) for y in ys]
    for mod in (ltas, crest, envelope):
        tf = mod.features(cap, None)
        out[mod.NAME] = [float(mod.distance(tf, mod.features(y))) for y in ys]
    out.update(null_features(t, ys[1:]))
    out["cap_gain_db"] = level.rms_db(cap) - level.rms_db(t)
    return out


def null_features(t: np.ndarray, ys: list[np.ndarray]) -> dict:
    """Per neighbour: direct null, gain-matched null and gain (shared helper), RMS level vs truth."""
    from tonematch.metrics import level
    from tonematch.metrics import trust as TR

    nf = [TR.null_fields(t, y) for y in ys]
    return {"null_db": [f["null_db"] for f in nf], "gm_null_db": [f["gm_null_db"] for f in nf],
            "gm_gain_db": [f["gm_gain_db"] for f in nf],
            "level_vs_truth_db": [level.rms_db(y) - level.rms_db(t) for y in ys]}


def _item_nulls(args) -> dict:
    root, it = args
    rd = Path(root) / "renders" / it.get("dir", f"item{it['item']:02d}")
    ys = [np.load(rd / f"c{k:02d}_A.npy").astype(np.float64) for k in range(len(it["candidates"]))]
    return {"item": it["item"], **null_features(ys[0], ys[1:])}


def cmd_nulls(a) -> int:
    """Refresh the null fields of a cached features file from its renders (adds `gm_gain_db` for the
    extended rule B; no score is recomputed)."""
    root = Path(a.root)
    mf = json.loads((root / "manifest.json").read_text())
    fp = features_path(mf["dataset"])
    feats = json.loads(fp.read_text())
    with ProcessPoolExecutor(min(a.procs, 2)) as ex:
        res = {r["item"]: r for r in ex.map(_item_nulls, [(str(root), it) for it in mf["items"]], chunksize=8)}
    changed = 0
    for it in feats["items"]:
        r = res[it["item"]]
        changed += int(not np.allclose(it["null_db"], r["null_db"]))
        it.update({k: v for k, v in r.items() if k != "item"})
    feats["aligned"] = mf["aligned"]
    fp.write_text(json.dumps(feats))
    print(f"{len(feats['items'])} items refreshed ({changed} with a changed direct null) -> {fp}")
    return 0


def features_path(dataset: str) -> Path:
    return ROOT / "cache" / "gain-trust" / f"features-{dataset}.json"


def cmd_features(a) -> int:
    root = Path(a.root)
    mf = json.loads((root / "manifest.json").read_text())
    jobs = [(str(root), it, mf["seed"], mf["aligned"]) for it in mf["items"]]
    procs = min(a.procs, 2)
    with ProcessPoolExecutor(procs) as ex:
        res = list(ex.map(_item_features, jobs, chunksize=4))
    fp = features_path(mf["dataset"])
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({"dataset": mf["dataset"], "aligned": mf["aligned"], "items": res}))
    print(f"{len(res)} items -> {fp}")
    return 0


# --------------------------------------------------------------------------- eval

def parse_score(spec: str) -> dict[str, float]:
    w = {}
    for part in spec.split(","):
        k, v = part.split("=")
        w[k.strip()] = float(v)
    return w


def combine(it: dict, w: dict[str, float]) -> np.ndarray:
    return sum(float(v) * np.asarray(it[k], dtype=np.float64) for k, v in w.items())


def tie_mask(it: dict, level_range_db: float) -> np.ndarray:
    """Rule B ties per neighbour (shared helper); `level_range_db` 0 = the direct-null rule."""
    from tonematch.metrics import trust as TR

    if level_range_db > 0 and "gm_gain_db" not in it:
        raise SystemExit(f"item {it['item']}: no gm_gain_db; run `gain_trust.py nulls` for this set")
    return TR.same_sound(it["null_db"], TR.SAME_SOUND_NULL_DB, it.get("gm_null_db"), it.get("gm_gain_db"),
                         level_range_db)


def unrankable(it: dict, gm_db: float = -60.0, level_range_db: float = 0.0) -> bool:
    """A pure-gain neighbour (gain-matched null <= gm_db) that is NOT a rule B tie and whose level is
    closer to the capture's than the truth's is. The registered diagnostic uses -60 dB; at -100 dB
    (added after the fit split was seen, reported separately) the neighbour is the same waveform up to
    float error and no score can rank the truth first. Under the extended rule B the only such
    neighbours left are those beyond the capture's gain range (|gain| > range, < twice the capture's)."""
    g = it["cap_gain_db"]
    tie = tie_mask(it, level_range_db)
    return any((not tj) and gm <= gm_db and abs(g - lv) < abs(g)
               for tj, gm, lv in zip(tie, it["gm_null_db"], it["level_vs_truth_db"], strict=True))


def evaluate(items: list[dict], w: dict[str, float], level_range_db: float = 0.0) -> dict:
    """Rule-B rank-1 per family. `level_range_db` > 0: the extended rule B with that range (the
    aligned capture's own `gain_db`); 0: the direct-null rule of T144."""
    from tonematch.metrics import trust as TR

    fam: dict[str, list[bool]] = {}
    fails, unr, n_level = [], [], 0
    for it in items:
        sc = combine(it, w)
        kw = ({"gm_null_db": it["gm_null_db"], "gm_gain_db": it["gm_gain_db"], "level_range_db": level_range_db}
              if level_range_db > 0 else {})
        r = TR.rank1_rule_b(sc[0], sc[1:], it["null_db"], **kw)
        n_level += r.n_level_tie
        fam.setdefault(it["category"], []).append(r.rank1_audible)
        u = unrankable(it, -60.0, level_range_db)
        unr.append((u, r.rank1_audible, unrankable(it, -100.0, level_range_db)))
        if not r.rank1_audible:
            nd = np.asarray(it["null_db"])
            tie = tie_mask(it, level_range_db)
            k = [int(j) + 1 for j in np.flatnonzero((sc[1:] <= sc[0]) & ~tie)]
            fails.append({"item": it["item"], "category": it["category"], "model": it["model"], "unrankable": u,
                          "cap_gain_db": round(float(it["cap_gain_db"]), 2),
                          "beaten_by": [(j, it["labels"][j], round(float(nd[j - 1]), 1),
                                         round(float(it["gm_null_db"][j - 1]), 1),
                                         round(float(it["gm_gain_db"][j - 1]), 2) if "gm_gain_db" in it else None)
                                        for j in k]})
    allv = [x for v in fam.values() for x in v]
    keep = [ok for u, ok, _ in unr if not u]
    keep_x = [ok for _, ok, x in unr if not x]
    return {"n": len(allv), "rank1": float(np.mean(allv)) if allv else float("nan"),
            "rule_b": "extended" if level_range_db > 0 else "direct", "level_range_db": level_range_db,
            "level_ties": n_level,
            "by_family": {c: (float(np.mean(v)), int(sum(v)), len(v)) for c, v in sorted(fam.items())},
            "unrankable_share": float(np.mean([u for u, _, _ in unr])) if unr else float("nan"),
            "rank1_rankable_only": float(np.mean(keep)) if keep else float("nan"),
            "exact_gain_unrankable_share": float(np.mean([x for _, _, x in unr])) if unr else float("nan"),
            "rank1_without_exact_gain_unrankable": float(np.mean(keep_x)) if keep_x else float("nan"),
            "fails": fails}


def load_features(dataset: str, split: str | None) -> list[dict]:
    items = json.loads(features_path(dataset).read_text())["items"]
    return [it for it in items if split is None or it["split"] == split]


def level_range_for(ds: str, rule: str) -> float:
    """Extended rule B: the aligned capture's own gain jitter as recorded in the set's manifest."""
    from tonematch.metrics import trust as TR

    if rule == "direct":
        return 0.0
    root = HTV2 if ds == "helix-trust-v2" else ROOT / "cache" / "loss-trust" / ds
    return TR.capture_gain_range_db(json.loads((root / "manifest.json").read_text())["aligned"])


def cmd_eval(a) -> int:
    out = {}
    for ds in a.datasets:
        split = a.split if ds in GAIN_SETS else {"fit": "train", "test": "test"}.get(a.split, a.split)
        items = load_features(ds, split)
        if a.categories:
            items = [it for it in items if it["category"] in a.categories]
        lr = level_range_for(ds, a.rule)
        out[ds] = {}
        for spec in a.score:
            r = evaluate(items, parse_score(spec), lr)
            out[ds][spec] = r
            fam = " ".join(f"{c} {v[0]:.3f} ({v[1]}/{v[2]})" for c, v in r["by_family"].items())
            print(f"{ds} [{split}] rule {r['rule_b']} (+-{lr:g} dB, {r['level_ties']} level ties) {spec}: "
                  f"rank1 {r['rank1']:.4f} (n {r['n']}); unrankable {r['unrankable_share']:.3f}, "
                  f"rankable-only {r['rank1_rankable_only']:.4f}; exact-gain unrankable "
                  f"{r['exact_gain_unrankable_share']:.3f}, without them {r['rank1_without_exact_gain_unrankable']:.4f}; {fam}")
            if a.show_fails:
                for f in r["fails"]:
                    print("   ", f)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))
    return 0


# --------------------------------------------------------------------------- diagnosis on v2

def cmd_diag_v2(a) -> int:
    """DIAGNOSIS ONLY: the six attempt-2 misses re-scored (synthetic-v2 is spent)."""
    import synthetic_v1 as SV
    from loss_trust_check import aligned_capture

    from tonematch.metrics import level
    from tonematch.metrics import mrstft_v2 as MV2
    from tonematch.metrics import trust as TR

    SV.set_paths("synthetic-v2")
    labels = {it["id"]: it for it in json.loads((SV.SET_DIR / "labels.json").read_text())}
    d = ROOT / "cache" / "gain-trust" / "diag-v2"
    out, detail = {}, {}
    for spec in a.score:
        detail[spec] = {}
        w = parse_score(spec)
        res = {}
        for iid in V2_MISSES:
            it = labels[iid]
            t = np.load(d / iid / "true_A.npy").astype(np.float64)
            cap = aligned_capture(t, np.random.default_rng([SV.SEED, 98, int(iid[1:])]))
            ys = [t] + [np.load(d / iid / f"n{k:02d}_A.npy").astype(np.float64) for k in range(len(it["neighbours"]))]
            feats = {"mrstft_v2": [MV2.term(cap, y, None) for y in ys], "level_db": [level.term(cap, y) for y in ys]}
            for name in w:
                if name not in feats:
                    from tonematch.metrics import crest, envelope, ltas
                    mod = {ltas.NAME: ltas, crest.NAME: crest, envelope.NAME: envelope}[name]
                    tf = mod.features(cap, None)
                    feats[name] = [float(mod.distance(tf, mod.features(y))) for y in ys]
            sc = combine(feats, w)
            nf = [TR.null_fields(t, y) for y in ys[1:]]
            kw = ({"gm_null_db": [f["gm_null_db"] for f in nf], "gm_gain_db": [f["gm_gain_db"] for f in nf],
                   "level_range_db": TR.capture_gain_range_db(None)} if a.rule == "extended" else {})
            r = TR.rank1_rule_b(sc[0], sc[1:], [f["null_db"] for f in nf], **kw)
            res[iid] = bool(r.rank1_audible)
            detail[spec][iid] = {"rank1_audible": bool(r.rank1_audible), "n_level_tie": r.n_level_tie,
                                 "cap_gain_db": round(level.rms_db(cap) - level.rms_db(t), 2),
                                 "beaten_by": [(j + 1, round(f["null_db"], 1), round(f["gm_null_db"], 1),
                                                round(f["gm_gain_db"], 2)) for j, f in enumerate(nf)
                                               if sc[1 + j] <= sc[0] and not (
                                                   f["null_db"] <= TR.SAME_SOUND_NULL_DB or (
                                                       kw and f["gm_null_db"] <= TR.SAME_SOUND_NULL_DB
                                                       and abs(f["gm_gain_db"]) <= kw["level_range_db"]))]}
        out[spec] = res
        print(f"DIAGNOSIS ONLY (synthetic-v2 spent) rule {a.rule} {spec}: "
              + " ".join(f"{k} {'ok' if v else 'MISS'}" for k, v in res.items()))
        for k, v in detail[spec].items():
            if not v["rank1_audible"]:
                print(f"    {k}: capture {v['cap_gain_db']:+.2f} dB; beaten by (n, null, gm null, gain) {v['beaten_by']}")
    if a.json:
        Path(a.json).write_text(json.dumps({"diagnosis_only": True, "rule_b": a.rule, "result": out,
                                            "detail": detail}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--dataset", choices=GAIN_SETS, default=DATASET)
    g.add_argument("--out", default=None, help="default cache/loss-trust/<dataset>")
    g.add_argument("--seed", type=int, default=None, help=f"default {SEED} (v1), {SEED_B} (v1b)")
    g.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--dataset", choices=GAIN_SETS, default=DATASET)
    r.add_argument("--out", default=None, help="default cache/loss-trust/<dataset>")
    r.add_argument("--hosts", type=int, default=MAX_HOSTS)
    r.add_argument("--chunk", type=int, default=8)
    f = sub.add_parser("features")
    f.add_argument("--root", default=str(OUT))
    f.add_argument("--procs", type=int, default=2)
    nl = sub.add_parser("nulls")
    nl.add_argument("--root", default=str(OUT))
    nl.add_argument("--procs", type=int, default=2)
    e = sub.add_parser("eval")
    e.add_argument("--rule", choices=("extended", "direct"), default="extended",
                   help="rule B: extended (human 2026-10-18, default) or direct (the T144 reading)")
    e.add_argument("--datasets", nargs="+", default=[DATASET, "helix-trust-v2"])
    e.add_argument("--split", default="fit", help="fit or test (helix-trust-v2: train or test)")
    e.add_argument("--score", nargs="+", default=["mrstft_v2=1"])
    e.add_argument("--categories", nargs="*", default=None)
    e.add_argument("--show-fails", action="store_true")
    e.add_argument("--json", default=None)
    d = sub.add_parser("diag-v2")
    d.add_argument("--score", nargs="+", default=["mrstft_v2=1"])
    d.add_argument("--json", default=None)
    d.add_argument("--rule", choices=("extended", "direct"), default="extended")
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "render": cmd_render, "features": cmd_features, "eval": cmd_eval,
            "nulls": cmd_nulls, "diag-v2": cmd_diag_v2}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
