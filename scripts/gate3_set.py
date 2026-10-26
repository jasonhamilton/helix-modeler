#!/usr/bin/env python3
"""Gate 3 sealed blind set `gate3-v1` (T223, owner: benchmark). Single drive-block targets on the reserved player P3.

  generate   draw 120 items (the 60 product drive models x 2 random settings) from Guitar-TECHS P3 DI excerpts,
             render them on one Native host (LoadGuard, nice 10), write the matcher's inputs and seal the labels
  verify     recompute the hashes of the set against the manifest (no Native, reads no label content)
  score      the registered scoring (labels unsealed). Refuses to run without --launched-by (the lead's launch
             reference); prints that the bars are PROPOSED unless --bars-approved names the DECISIONS entry

Layout (cache/gate3-v1/, gitignored):
  manifest.json          public: item ids and the sha256 of every target audio file, counts, rules, seed
  targets/g3-NNN.wav     what a matcher may read: the rendered 10 s drive block, mono float32, 48 kHz
  sealed/labels.json     model, settings, ranges, P3 file and start per item. Nobody reads it before scoring
  sealed/dry/g3-NNN.wav  the P3 excerpt of each item, kept for the later ear step only (a matcher never sees it)
  sha256 of manifest.json and labels.json are written to the ledger and to docs/DECISIONS.md.

Conditions (chosen to match drive-dataset-v1 so a predictor trained on it is tested on a new player):
random uniform CC-grid settings (level-type knobs at their default), 10 s active excerpts (drive_dataset_render
.active_starts), DI at its recorded level, Native 3.82 single-block library, preroll 3 s as the dataset.
P3 is the reserved third player: it is read here to build the set and later to score; it is in no training path.
Files directinput_02, _03 and _08 are held back as the reserve for a second set (they were used earlier, only as
numpy-stand-in DI for the separation stress test sep-stress-v1; no matcher or training saw them).
The model pool is the product's (match_block.category_pool('drive'), 60 models), so the six models the T191
dataset leaves out are in the set and flagged `in_dataset_pool` false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

NAME = "gate3-v1"
SEED = 223001
SR = 48_000
CLIP_S = 10.0
PER_MODEL = 2
P3_DIR = "Guitar-TECHS/P3_music/audio/directinput"
RESERVE = ("02", "03", "08")                       # held back for a second set
SEP_LEVELS_S = (5.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.0)          # minimum start distance between two items on one file
TOL = 0.15
OUT = ROOT / "cache" / NAME
RULES = [
    "single drive block per item, 60 product drive models x 2 random settings, Guitar-TECHS P3 DI only",
    "the matcher is given only targets/<id>.wav; sealed/ is for scoring and the later ear step",
    "one scoring attempt per frozen method; a changed method needs a new set (reserve files 02, 03, 08)",
    "bars PROPOSED by benchmark 2026-10-25 (docs/DECISIONS.md), not approved until the human says so",
]


def p3_path(n: str) -> str:
    return f"{P3_DIR}/directinput_{n}.wav"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ plan (pure)

def make_plan(starts: dict[str, list[int]], pool: dict, knobs: dict, level_names, seed: int = SEED,
              per_model: int = PER_MODEL, clip_n: int = int(CLIP_S * SR)) -> list[dict]:
    """Draw the items. `starts[file]` are the allowed active start samples. Only P3 files are accepted. Two items of one
    model use different files when more than one file exists; two items on one file keep their starts at least
    SEP_LEVELS_S apart (relaxed stepwise only when no admissible start is left)."""
    bad = [f for f in starts if "/P3_music/" not in f]
    if bad:
        raise ValueError(f"not a P3 file: {bad[:2]}")
    rng = np.random.default_rng(seed)
    files = sorted(f for f in starts if starts[f])
    jobs = [(m, r) for m in sorted(pool) for r in range(per_model)]
    rng.shuffle(jobs)
    used: dict[str, list[int]] = {f: [] for f in files}
    by_model: dict[str, list[str]] = {}
    items = []
    for model, rep in jobs:
        placed = None
        for sep_s in SEP_LEVELS_S:
            sep = int(sep_s * SR)
            prefer = [f for f in files if f not in by_model.get(model, [])] or files
            for f in (prefer[int(i)] for i in rng.permutation(len(prefer))):
                ok = [s for s in starts[f] if all(abs(s - u) >= sep for u in used[f])]
                if ok:
                    placed = (f, int(ok[int(rng.integers(0, len(ok)))]), sep_s)
                    break
            if placed:
                break
        if placed is None:
            raise RuntimeError("no admissible P3 excerpt left; the set needs more P3 material")
        f, s, sep_s = placed
        used[f].append(s)
        by_model.setdefault(model, []).append(f)
        free = pool[model]
        dk, tk = knobs.get(model, (None, None))
        keep = tuple(n for n in (dk, tk) if n)
        cc = {}
        for n, _lo, _hi, _d in free:
            if n in level_names and n not in keep:
                continue
            cc[n] = int(rng.integers(0, 128))
        items.append({"model": model, "rep": rep, "cc": cc, "di_file": f, "start": s, "n": clip_n, "player": "P3",
                      "drive_knob": dk, "tone_knob": tk, "sep_s": sep_s})
    order = rng.permutation(len(items))
    items = [items[int(i)] for i in order]
    for k, it in enumerate(items):
        it["id"] = f"g3-{k + 1:03d}"
    return items


def redraw_silent(item: dict, starts: dict[str, list[int]], pool, level_names, rng) -> dict:
    """A silent render before sealing: new setting on the same excerpt (counted in the manifest)."""
    new = dict(item)
    keep = tuple(n for n in (item["drive_knob"], item["tone_knob"]) if n)
    new["cc"] = {n: int(rng.integers(0, 128)) for n, _lo, _hi, _d in pool[item["model"]]
                 if not (n in level_names and n not in keep)}
    new["redrawn"] = item.get("redrawn", 0) + 1
    return new


# ------------------------------------------------------------------------------------------ generate

def cmd_generate(a) -> int:
    import drive_dataset_render as DD
    import match_block as MB
    import soundfile as sf

    from tonematch.catalog import load_catalog
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.learning import drive_pilot as dp

    out = Path(a.out)
    if (out / "manifest.json").exists() and not a.force:
        print(f"{out / 'manifest.json'} exists: the set is sealed (use --force only before anyone has run it)")
        return 1
    os.nice(10)
    catalog = load_catalog()
    pool = MB.category_pool("drive", catalog)
    knobs = dp._knob_names()
    dataset_pool = set(DD.drive_pool()[0])
    starts, dry_cache = {}, {}
    for n in ("01", "04", "05", "06", "07", "09", "10", "11", "12"):
        f = p3_path(n)
        x = DD.read_mono_file(f)
        starts[f] = DD.active_starts(x)
        dry_cache[f] = x
    items = make_plan(starts, pool, knobs, DD.LEVEL_NAMES, seed=a.seed)
    LoadGuard().wait(print)
    blocks = MB.make_blocks(pool, "drive", catalog)
    rng = np.random.default_rng(a.seed + 1)
    eng = MB.make_engine(144_000, log=lambda *_: None)
    (out / "targets").mkdir(parents=True, exist_ok=True)
    (out / "sealed" / "dry").mkdir(parents=True, exist_ok=True)
    labels, hashes, redrawn = [], {}, 0
    try:
        for it in items:
            dry = dry_cache[it["di_file"]][it["start"]:it["start"] + it["n"]].astype(np.float32)
            for _try in range(10):
                try:
                    y = DD.render_one(eng, blocks[it["model"]], it, dry)
                    break
                except RuntimeError:
                    it = redraw_silent(it, starts, pool, DD.LEVEL_NAMES, rng)
                    redrawn += 1
            else:
                raise RuntimeError(f"{it['id']} stays silent")
            b = blocks[it["model"]]
            vals = b.from_knobs(it["cc"])
            sf.write(str(out / "targets" / f"{it['id']}.wav"), y.astype(np.float32), SR, subtype="FLOAT")
            sf.write(str(out / "sealed" / "dry" / f"{it['id']}.wav"), dry, SR, subtype="FLOAT")
            hashes[it["id"]] = sha256_file(out / "targets" / f"{it['id']}.wav")
            labels.append({**{k: it[k] for k in ("id", "model", "rep", "cc", "di_file", "start", "n", "player", "drive_knob",
                                                 "tone_knob", "sep_s")},
                           "values": {n: float(v) for n, v in vals.items()},
                           "norm": b.norm(vals), "ranges": {n: list(map(float, b.rng[n])) for n in b.rng},
                           "in_dataset_pool": it["model"] in dataset_pool, "redrawn": it.get("redrawn", 0),
                           "dry_rms_dbfs": 10 * math.log10(float(np.mean(dry.astype(np.float64) ** 2)) + 1e-30),
                           "target_rms_dbfs": 10 * math.log10(float(np.mean(y.astype(np.float64) ** 2)) + 1e-30)})
    finally:
        eng.close()
    labels.sort(key=lambda r: r["id"])
    lp = out / "sealed" / "labels.json"
    lp.write_text(json.dumps(labels, indent=1))
    manifest = {"name": NAME, "version": 1, "seed": a.seed, "sample_rate": SR, "clip_seconds": CLIP_S,
                "engine": "native", "player": "P3 (Guitar-TECHS, the reserved third player)",
                "n_items": len(labels), "items_per_model": PER_MODEL, "n_models": len(pool),
                "p3_files_used": ["01", "04", "05", "06", "07", "09", "10", "11", "12"], "p3_reserve_files": list(RESERVE),
                "redrawn_silent": redrawn,
                "excerpt_spacing_s_counts": {str(k): sum(1 for r in labels if r["sep_s"] == k) for k in SEP_LEVELS_S},
                "excerpt_note": "P3 holds about 6.5 min of usable playing, so items share notes: two items on one file start at least the stated spacing apart (down to 1 s) and use different models or settings", "rules": RULES, "targets_sha256": hashes,
                "conditions": "uniform CC-grid settings, level knobs at default, active 10 s excerpts at recorded level, "
                              "Native 3.82 block library, preroll 3 s (as drive-dataset-v1)",
                "regenerate": "python scripts/gate3_set.py generate --force (reads P3 DI and the Native library; seed above)"}
    mp = out / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=1))
    for p in (lp, mp):
        os.chmod(p, 0o444)
    hs = {"manifest": sha256_file(mp), "labels": sha256_file(lp)}
    print(json.dumps({"n_items": len(labels), "redrawn_silent": redrawn, **hs}, indent=1))
    if not a.no_ledger:
        _ledger("sealed_items", float(len(labels)), f"T223 Gate 3 sealed blind set: {len(labels)} single drive-block items, "
                f"60 models x {PER_MODEL}, Guitar-TECHS P3 DI, seed {a.seed}, silent redraws {redrawn}; sha256 manifest "
                f"{hs['manifest']} labels {hs['labels']}; labels unread by the matcher and by benchmark after generation")
    return 0


def _ledger(metric: str, value: float, notes: str) -> None:
    import subprocess
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "benchmark")}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--dataset", NAME,
                    "--metric", metric, "--value", f"{value:.6g}", "--notes", notes], check=True, env=env, cwd=ROOT)


# ------------------------------------------------------------------------------------------ verify

def cmd_verify(a) -> int:
    out = Path(a.out)
    man = json.loads((out / "manifest.json").read_text())
    bad = [i for i, h in man["targets_sha256"].items() if sha256_file(out / "targets" / f"{i}.wav") != h]
    hs = {"manifest": sha256_file(out / "manifest.json"), "labels": sha256_file(out / "sealed" / "labels.json")}
    print(json.dumps({"items": man["n_items"], "target_files_changed": bad, **hs}, indent=1))
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------ score (pure parts)

def mcnemar(b: int, c: int) -> float:
    """Exact two-sided p: b = only the method right, c = only the baseline right."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return float(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n))


def item_hits(labels: list[dict], preds: dict) -> dict[str, dict]:
    """Per item: top1, top3 and (for right-model items) drive_ok and the share of knobs within TOL."""
    out = {}
    for lab in labels:
        p = preds.get(lab["id"])
        if p is None:
            out[lab["id"]] = {"top1": False, "top3": False, "drive_ok": None, "knob_share": None, "missing": True}
            continue
        ranked = list(p["ranked"])
        top1 = bool(ranked) and ranked[0] == lab["model"]
        rec = {"top1": top1, "top3": lab["model"] in ranked[:3], "drive_ok": None, "knob_share": None, "missing": False}
        if top1:
            vals = p.get("values") or {}
            ok = []
            for n in lab["cc"]:
                lo, hi = lab["ranges"][n]
                tn = lab["norm"][n]
                pn = (float(vals[n]) - lo) / (hi - lo) if n in vals else None
                ok.append(pn is not None and abs(pn - tn) <= TOL)
            rec["knob_share"] = float(np.mean(ok)) if ok else None
            if lab["drive_knob"] in lab["cc"]:
                rec["drive_ok"] = bool(ok[list(lab["cc"]).index(lab["drive_knob"])])
        out[lab["id"]] = rec
    return out


def baseline_preds(labels: list[dict], models: list[str], seed: int = 0) -> tuple[dict, dict]:
    """(i) uniform-random model, middle-of-every-knob settings; (ii) the true model at middle-of-every-knob."""
    rng = np.random.default_rng(seed)
    rand, mid_true = {}, {}
    for lab in labels:
        mid = {n: lab["ranges"][n][0] + 0.5 * (lab["ranges"][n][1] - lab["ranges"][n][0]) for n in lab["cc"]}
        perm = [models[int(i)] for i in rng.permutation(len(models))]
        rand[lab["id"]] = {"ranked": perm, "values": mid}
        mid_true[lab["id"]] = {"ranked": [lab["model"]], "values": mid}
    return rand, mid_true


def rate(vals: list) -> float | None:
    v = [x for x in vals if x is not None]
    return float(np.mean(v)) if v else None


def summarise(hits: dict[str, dict]) -> dict:
    h = list(hits.values())
    right = [x for x in h if x["top1"]]
    return {"n": len(h), "top1": rate([x["top1"] for x in h]), "top3": rate([x["top3"] for x in h]),
            "n_right": len(right), "drive_ok": rate([x["drive_ok"] for x in right]),
            "knob_share": rate([x["knob_share"] for x in right]), "missing": sum(x["missing"] for x in h)}


def paired(hm: dict, hb: dict, key: str) -> dict:
    b = sum(1 for i in hm if hm[i][key] and not hb[i][key])
    c = sum(1 for i in hm if hb[i][key] and not hm[i][key])
    n = len(hm)
    return {"only_method": b, "only_baseline": c, "diff": (b - c) / n if n else None, "p": mcnemar(b, c)}


def paired_knob(hm: dict, hb: dict, key: str) -> dict:
    """On items where the method AND the baseline both name the true model (so both have a knob verdict)."""
    ids = [i for i in hm if hm[i]["top1"] and hb[i]["top1"]]
    if key == "drive_ok":
        ids = [i for i in ids if hm[i]["drive_ok"] is not None and hb[i]["drive_ok"] is not None]
        b = sum(1 for i in ids if hm[i]["drive_ok"] and not hb[i]["drive_ok"])
        c = sum(1 for i in ids if hb[i]["drive_ok"] and not hm[i]["drive_ok"])
        n = len(ids)
        return {"n": n, "only_method": b, "only_baseline": c, "diff": (b - c) / n if n else None, "p": mcnemar(b, c)}
    d = [hm[i]["knob_share"] - hb[i]["knob_share"] for i in ids if hm[i]["knob_share"] is not None]
    return {"n": len(d), "mean_diff": float(np.mean(d)) if d else None}


BARS = {"top1": 0.50, "top3": 0.70, "drive_ok": 0.60, "knob_share": 0.50, "min_diff": 0.15, "alpha": 0.05}


def score(labels: list[dict], preds: dict, ref_preds: dict | None = None, seed: int = 0) -> dict:
    models = sorted({lab["model"] for lab in labels})
    hm = item_hits(labels, preds)
    rand, mid_true = baseline_preds(labels, models, seed)
    hr, hmid = item_hits(labels, rand), item_hits(labels, mid_true)
    res = {"method": summarise(hm), "baseline_random_model": summarise(hr), "baseline_true_model_mid_knobs": summarise(hmid),
           "paired_vs_random": {k: paired(hm, hr, k) for k in ("top1", "top3")},
           "paired_vs_mid_knobs": {"drive_ok": paired_knob(hm, hmid, "drive_ok"), "knob_share": paired_knob(hm, hmid, "knob_share")}}
    if ref_preds is not None:
        hx = item_hits(labels, ref_preds)
        res["reference"] = summarise(hx)
        res["paired_vs_reference"] = {k: paired(hm, hx, k) for k in ("top1", "top3")}
        res["paired_knob_vs_reference"] = {"drive_ok": paired_knob(hm, hx, "drive_ok"), "knob_share": paired_knob(hm, hx, "knob_share")}
    s = res["method"]
    chk = {"G3-1 top1": (s["top1"] or 0) >= BARS["top1"], "G3-2 top3": (s["top3"] or 0) >= BARS["top3"],
           "G3-3a drive": (s["drive_ok"] or 0) >= BARS["drive_ok"], "G3-3b knobs": (s["knob_share"] or 0) >= BARS["knob_share"]}
    beats = {}
    for k, pr in res["paired_vs_random"].items():
        beats[f"{k} vs random"] = bool(pr["diff"] is not None and pr["diff"] >= BARS["min_diff"] and pr["p"] < BARS["alpha"])
    d = res["paired_vs_mid_knobs"]["drive_ok"]
    beats["drive vs mid"] = bool(d["diff"] is not None and d["diff"] >= BARS["min_diff"] and d["p"] < BARS["alpha"])
    if ref_preds is not None:
        for k, pr in res["paired_vs_reference"].items():
            beats[f"{k} vs reference"] = bool(pr["diff"] is not None and pr["diff"] >= BARS["min_diff"] and pr["p"] < BARS["alpha"])
    res["against_proposed_bars"] = {"bars": BARS, "bars_status": "PROPOSED by benchmark, NOT APPROVED", "levels": chk, "beats_baselines": beats}
    return res


def cmd_score(a) -> int:
    if not a.launched_by:
        print("refusing: --launched-by <the lead's launch reference> is required; the labels stay sealed until then")
        return 2
    labels = json.loads((Path(a.out) / "sealed" / "labels.json").read_text())
    preds = json.loads(Path(a.predictions).read_text())
    ref = json.loads(Path(a.reference).read_text()) if a.reference else None
    res = score(labels, preds.get("items", preds), (ref["items"] if ref and "items" in ref else ref))
    res["method_name"] = preds.get("method", "unnamed") if isinstance(preds, dict) else "unnamed"
    res["launched_by"] = a.launched_by
    res["bars_approved_ref"] = a.bars_approved
    if not a.bars_approved:
        res["against_proposed_bars"]["bars_status"] = "PROPOSED by benchmark, NOT APPROVED; read as numbers, not a pass or fail"
    txt = json.dumps(res, indent=1)
    print(txt)
    Path(a.results).write_text(txt)
    if not a.no_ledger:
        m = res["method"]
        for k in ("top1", "top3", "drive_ok", "knob_share"):
            if m[k] is not None:
                _ledger(f"score_{k}", m[k], f"Gate 3 sealed-set score by method {res['method_name']}; launched by {a.launched_by}; "
                        f"bars {'approved ' + a.bars_approved if a.bars_approved else 'PROPOSED not approved'}; n={m['n']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", default=str(OUT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=SEED)
    g.add_argument("--force", action="store_true")
    g.add_argument("--no-ledger", action="store_true")
    sub.add_parser("verify")
    s = sub.add_parser("score")
    s.add_argument("--predictions", required=True)
    s.add_argument("--reference", default=None)
    s.add_argument("--launched-by", default=None)
    s.add_argument("--bars-approved", default=None, help="DECISIONS reference of the human's approval of the bars")
    s.add_argument("--results", default=str(OUT / "score.json"))
    s.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args(argv)
    return {"generate": cmd_generate, "verify": cmd_verify, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
