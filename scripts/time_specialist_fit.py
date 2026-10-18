#!/usr/bin/env python3
"""Library preparation for the time-based specialist (search, 2026-10-17; src/tonematch/search/time_specialist.py).

  render   per delay / reverb model, N Latin-hypercube draws over ALL its free knobs (not one at a time:
           the items of the benchmark vary every knob), each rendered through one of three calibration DIs
           (none of them in any benchmark set); the audio goes to cache/time-specialist/renders/<model>/
           as 24-bit FLAC with an index.jsonl of knobs. One Native host (or more with
           TONEMATCH_NATIVE_MANY_HOSTS); resumable.
  fit      from those renders: the per-model knob regressors (the seeds) and, with --classifier, the
           delay / reverb / mod / other classifier; writes cache/time-specialist/seeds.json and
           data/time_specialist/classifier.json.

Local output only (cache/, gitignored): the renders are of Line 6 models.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

OUT = ROOT / "cache" / "time-specialist"
N_DI = 3
SEED = 20261019
U_LO, U_HI = 0.10, 0.90      # the range of the benchmark items' knobs (synthetic_v1.U_RANGE)
AVOID_SETS = ("synthetic-v1", "synthetic-v2", "synthetic-devfam")


def wait_for_load(limit: float = 7.0, poll_s: float = 60.0) -> None:
    """Heavy steps wait while the 1-minute load average is at or above `limit` (DECISIONS 2026-10-17, machine load limit)."""
    import os

    while os.getloadavg()[0] >= limit:
        print(f"load {os.getloadavg()[0]:.1f} >= {limit:g}: waiting", flush=True)
        time.sleep(poll_s)


def calibration_dis(clip_s: float, ref_db: float) -> list[tuple[np.ndarray, dict]]:
    """Three DI excerpts that overlap no excerpt of any benchmark set (drawn with a fixed seed)."""
    import helix_trust_set as HT
    import synthetic_v1 as S

    avoid = S.used_excerpts([s for s in AVOID_SETS if (ROOT / "cache" / s / "labels.json").exists()])
    rng = np.random.default_rng([SEED, 5])
    ex = HT.pick_excerpts(rng, N_DI, clip_s, avoid)
    out = []
    for e in ex:
        x, g = HT.load_excerpt(e, clip_s, ref_db)
        out.append((x, {**e, "gain_db": round(g, 2)}))
    return out


def lhs_knobs(rng: np.random.Generator, free, n: int) -> list[dict[str, int]]:
    """n draws on the CC grid, Latin hypercube over the free knobs, u in [U_LO, U_HI]."""
    from tonematch.search.hybrid import latin_hypercube

    u = latin_hypercube(n, len(free), rng)
    return [{nm: int(np.clip(round((U_LO + (U_HI - U_LO) * float(u[i, j])) * 127), 0, 127))
             for j, (nm, *_r) in enumerate(free)} for i in range(n)]


def model_list(a) -> list[tuple[str, str]]:
    import synthetic_v1 as S

    from tonematch.catalog import native_unavailable
    from tonematch.engines import native_library as NL

    out: list[tuple[str, str]] = []
    if a.set:
        S.set_paths(a.set)
        man = json.loads((S.SET_DIR / "manifest.json").read_text())
        for c in ("delay", "reverb"):
            out += [(m, c) for m in man["pool"].get(c, {})]
    if a.models:
        idx = NL.load_index()["models"]
        out += [(m, idx[m]["category"]) for m in a.models]
    if a.mods:       # the mod models the benchmark can draw (class "mod" of the classifier)
        idx = NL.load_index()["models"]
        bad = set(native_unavailable())
        out += [(m, "mod") for m, e in sorted(idx.items()) if e["category"] == "mod" and m not in bad and not e.get("time_variant")
                and S.eligible(m, "mod", set()) and not NL.is_routing(m)]
    if a.all:
        idx = NL.load_index()["models"]
        bad = set(native_unavailable())
        for m, e in sorted(idx.items()):
            if e["category"] in a.all and m not in bad and not e.get("time_variant") and not NL.is_routing(m) \
                    and (a.all_reverbs or e["category"] != "reverb" or m in S.REVERBS):
                out.append((m, e["category"]))
    return list(dict.fromkeys(out))


def cmd_render(a) -> int:
    import soundfile as sf
    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.engines.base import EngineError

    wait_for_load()
    models = model_list(a)
    cat = load_catalog()
    dis = calibration_dis(S.CLIP_S, S.REF_LOUD_DBFS)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "di").mkdir(exist_ok=True)
    for i, (x, meta) in enumerate(dis):
        sf.write(str(OUT / "di" / f"cal{i}.flac"), x, S.SR, subtype="PCM_24")
    (OUT / "dis.json").write_text(json.dumps([m for _, m in dis], indent=1))
    print(f"{len(models)} models, {a.draws} draws each, {len(dis)} DIs; machine hosts {S.machine_hosts()}", flush=True)
    if S.machine_hosts() + 1 > a.max_machine_hosts:
        print("refusing: too many native_host processes", file=sys.stderr)
        return 3
    engine = S.make_engine()
    t0, n = time.time(), 0
    try:
        for model, category in models:
            d = OUT / "renders" / model
            d.mkdir(parents=True, exist_ok=True)
            idx_f = d / "index.jsonl"
            done = {json.loads(l)["k"] for l in idx_f.read_text().splitlines()} if idx_f.exists() else set()
            b = S.Block(model, category, S.free_params(model, NL.template_for(model), cat, category), cat)
            if not b.free:
                print(model, "no free knobs, skipped", flush=True)
                continue
            rng = np.random.default_rng([SEED, zlib.crc32(model.encode())])
            draws = lhs_knobs(rng, b.free, a.draws)
            todo = [k for k in range(a.draws) if k not in done]
            if not todo:
                continue
            for k in todo:
                ks = draws[k]
                vals = b.from_knobs(ks)
                dry = dis[k % N_DI][0]
                rec = {"k": k, "di": k % N_DI, "knobs": ks, "values": vals}
                try:
                    engine.load(b.with_values(vals))
                    y = np.asarray(engine.render(dry), np.float32)
                    sf.write(str(d / f"{k:03d}.flac"), y, S.SR, subtype="PCM_24")
                except EngineError as e:      # e.g. a swell delay that stays silent on this input
                    rec["error"] = str(e)[:120]
                with idx_f.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                n += 1
            print(f"{model} ({category}): {len(todo)} renders, total {n}, {time.time() - t0:.0f} s", flush=True)
    finally:
        engine.close()
    print(f"done: {n} renders in {time.time() - t0:.0f} s")
    return 0


# ------------------------------------------------------------------------------------------ fit

def _feat_job(job):
    """(model, k, probe-features or None) for one rendered draw."""
    import soundfile as sf

    from tonematch.search.time_specialist import analyse

    model, k, flac, di = job
    try:
        y, _ = sf.read(flac, dtype="float32")
        x, _ = sf.read(str(OUT / "di" / f"cal{di}.flac"), dtype="float32")
        ev = analyse(y, x, with_presence=True)
        return model, k, ev.probe
    except Exception as e:  # noqa: BLE001
        return model, k, {"error": str(e)}


def cmd_fit_seeds(a) -> int:
    from multiprocessing import Pool

    import synthetic_v1 as S

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.search.time_specialist import SEEDS_PATH, fit_seed_model

    wait_for_load()
    cat = load_catalog()
    idx = NL.load_index()["models"]
    rows, jobs = {}, []
    only = set(a.models or [])
    if a.set:
        S.set_paths(a.set)
        man = json.loads((S.SET_DIR / "manifest.json").read_text())
        only |= {m for c in ("delay", "reverb") for m in man["pool"].get(c, {})}
    for d in sorted((OUT / "renders").iterdir()):
        if (only and d.name not in only) or idx[d.name]["category"] not in ("delay", "reverb"):
            continue
        recs = [json.loads(l) for l in (d / "index.jsonl").read_text().splitlines()] if (d / "index.jsonl").exists() else []
        rows[d.name] = [r for r in recs if "error" not in r]
        jobs += [(d.name, r["k"], str(d / f"{r['k']:03d}.flac"), r["di"]) for r in rows[d.name]]
    feat_cache = OUT / "feats.json"
    have = json.loads(feat_cache.read_text()) if feat_cache.exists() and not a.refresh else {}
    todo = [j for j in jobs if f"{j[0]}/{j[1]}" not in have]
    print(f"{len(jobs)} renders, {len(todo)} to analyse", flush=True)
    with Pool(a.procs) as pool:
        for m, k, f in pool.imap_unordered(_feat_job, todo, chunksize=4):
            have[f"{m}/{k}"] = f
    feat_cache.write_text(json.dumps(have))
    bank = json.loads(SEEDS_PATH.read_text()) if SEEDS_PATH.exists() and only else {}
    report = []
    for model, recs in rows.items():
        category = idx[model]["category"]
        b = S.Block(model, category, S.free_params(model, NL.template_for(model), cat, category), cat)
        F, Y = [], []
        for r in recs:
            f = have.get(f"{model}/{r['k']}")
            if not f or "error" in f:
                continue
            F.append(f)
            Y.append([(r["values"][n] - lo) / (hi - lo) for n, lo, hi, _ in b.free])
        if len(F) < 20:
            print(model, "too few renders", len(F))
            continue
        sm = fit_seed_model(model, category, [n for n, *_ in b.free], F, np.array(Y))
        bank[model] = sm.to_json()
        for j, n in enumerate(sm.knobs):
            report.append((model, n, float(sm.loo[j]), float(sm.spread[j]), sm.keys))
        print(model, " ".join(f"{n}:{sm.loo[j]:.2f}/{sm.spread[j]:.2f}" for j, n in enumerate(sm.knobs)), flush=True)
    SEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEDS_PATH.write_text(json.dumps(bank))
    print(f"wrote {SEEDS_PATH} ({len(bank)} models)")
    return 0


def _clf_job(job):
    """(key, class, feature dict) for one labelled render, full analysis (shape, echoes, presence)."""
    import soundfile as sf

    from tonematch.search.time_specialist import PRESENCE_KEYS, analyse

    key, cls, y_path, x_path, pres = job
    try:
        if y_path.startswith("npz:"):                 # a render stored in a trust-check file: npz:<file>:<row>
            _, f, j = y_path.split(":")
            z = np.load(f)
            y, x = z["renders"][int(j)], z["dry"]
        elif y_path.endswith(".npy"):
            y = np.load(y_path)
            x, _ = sf.read(x_path, dtype="float32")
        else:
            y, _ = sf.read(y_path, dtype="float32")
            x, _ = sf.read(x_path, dtype="float32")
        ev = analyse(y, x, with_presence=pres is None)
        if pres is not None:                      # the presence-v1 features of the corpus are on disk already
            ev.presence = {k: float(pres[k]) for k in PRESENCE_KEYS if k in pres}
        return key, cls, ev.vector()
    except Exception as e:  # noqa: BLE001
        return key, cls, {"error": str(e)}


def cmd_fit_classifier(a) -> int:
    from multiprocessing import Pool

    from tonematch.engines import native_library as NL
    from tonematch.search.time_specialist import CLASSES, CLASSIFIER_PATH, FEATURE_KEYS, Classifier

    wait_for_load()
    man = json.loads((ROOT / "cache/loss-trust/helix-trust-v2/manifest.json").read_text())
    base = ROOT / "cache/loss-trust/helix-trust-v2"
    cls_of = lambda c: c if c in ("delay", "reverb", "mod") else "other"
    jobs = []
    pres = {}
    for line in (ROOT / "cache/presence-v1/features-train.jsonl").read_text().splitlines():
        r = json.loads(line)
        pres[(int(r["item"]), int(r["k"]))] = r["known_di"]
    for it in man["items"]:
        if it["split"] != "train":                    # the test split of presence-v1 stays unread
            continue
        x = str(ROOT / it["di"]["A"]["file"])
        for k, c in enumerate(it["candidates"]):
            jobs.append((f"corpus/{it['item']}/{k}", cls_of(c["category"]), str(base / "renders" / it["dir"] / f"c{k:02d}_A.npy"), x,
                         pres.get((int(it["item"]), k))))
        jobs.append((f"corpus/{it['item']}/di", "other", x, x, None))      # the DI itself: no effect
    for d in sorted((OUT / "renders").iterdir()):
        recs = [json.loads(l) for l in (d / "index.jsonl").read_text().splitlines()] if (d / "index.jsonl").exists() else []
        cat = NL.load_index()["models"][d.name]["category"]
        cat = cat if cat in ("delay", "reverb", "mod") else "other"
        recs = [r for r in recs if "error" not in r]
        recs = recs[:: max(len(recs) // max(a.cal_per_model, 1), 1)][: a.cal_per_model]
        jobs += [(f"cal/{d.name}/{r['k']}", cat, str(d / f"{r['k']:03d}.flac"), str(OUT / "di" / f"cal{r['di']}.flac"), None)
                 for r in recs]
    if a.tune_set:      # DI-diverse examples: the TUNING half of a dev set (truth and neighbours; never its held-out half)
        import synthetic_v1 as S

        S.set_paths(a.tune_set)
        labels = json.loads((S.SET_DIR / "labels.json").read_text())
        idx = NL.load_index()["models"]
        for c in sorted({x["category"] for x in labels}):
            for pos, it in enumerate([x for x in labels if x["category"] == c]):
                f = OUT / "trust" / a.tune_set / f"{it['id']}.npz"
                if pos % 2 or not f.exists():
                    continue
                names = [str(n) for n in np.load(f)["names"]]
                for j, nm in enumerate(names):
                    model = nm.split("|", 1)[1]
                    jobs.append((f"tune/{it['id']}/{j}", cls_of(idx[model]["category"]), f"npz:{f}:{j}", str(f), None))
    cache = OUT / "clf_feats.json"
    have = json.loads(cache.read_text()) if cache.exists() and not a.refresh else {}
    todo = [j for j in jobs if j[0] not in have]
    print(f"{len(jobs)} labelled renders, {len(todo)} to analyse", flush=True)
    with Pool(a.procs) as pool:
        for key, cls, f in pool.imap_unordered(_clf_job, todo, chunksize=4):
            have[key] = {"cls": cls, "f": f}
    cache.write_text(json.dumps(have))
    rows = [(k, v["cls"], v["f"]) for k, v in have.items() if "error" not in v["f"]]
    # transparent renders are no evidence of a delay or a reverb
    rows = [(k, c, f) for k, c, f in rows if not (c in ("delay", "reverb") and f.get("null_db", -99) <= -28.0)]
    rows = [(k, c, f) for k, c, f in rows if not (c == "mod" and f.get("null_db", -99) <= -28.0 and k.startswith("cal"))]
    X = np.array([[f.get(k, np.nan) for k in FEATURE_KEYS] for _, _, f in rows], float)
    y = np.array([CLASSES.index(c) for _, c, _ in rows])
    grp = np.array([k.split("/")[1] if k.startswith("corpus") else k.split("/")[1] + k.split("/")[2][:0] for k, _, _ in rows])
    print({c: int((y == i).sum()) for i, c in enumerate(CLASSES)})
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold

    med = np.nanmedian(X, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    Xf = np.where(np.isfinite(X), X, med)
    mu, sd = Xf.mean(0), Xf.std(0) + 1e-9
    Z = np.concatenate([(Xf - mu) / sd, (~np.isfinite(X)).astype(float)], axis=1)
    best = None
    for C in (0.03, 0.1, 0.3, 1.0):
        pred = np.zeros(len(y), int)
        for tr, te in GroupKFold(5).split(Z, y, grp):
            m = LogisticRegression(C=C, max_iter=3000, class_weight="balanced").fit(Z[tr], y[tr])
            pred[te] = m.predict(Z[te])
        acc = float((pred == y).mean())
        cm = np.zeros((4, 4), int)
        for t, p_ in zip(y, pred, strict=True):
            cm[t, p_] += 1
        print(f"C={C}: grouped-CV accuracy {acc:.3f}\n{cm}", flush=True)
        if best is None or acc > best[0]:
            best = (acc, C)
    m = LogisticRegression(C=best[1], max_iter=3000, class_weight="balanced").fit(Z, y)
    # the family head: time (delay or reverb) against everything else
    yt = np.isin(y, [CLASSES.index("delay"), CLASSES.index("reverb")]).astype(int)
    fam_best = None
    for C in (0.1, 0.3, 1.0, 3.0):
        pred = np.zeros(len(yt), int)
        for tr, te in GroupKFold(5).split(Z, yt, grp):
            pred[te] = LogisticRegression(C=C, max_iter=3000, class_weight="balanced").fit(Z[tr], yt[tr]).predict(Z[te])
        rec, rej = float(pred[yt == 1].mean()), float(1 - pred[yt == 0].mean())
        print(f"family head C={C}: grouped-CV time recall {rec:.3f}, not-time rejection {rej:.3f}", flush=True)
        if fam_best is None or rec + rej > fam_best[0]:
            fam_best = (rec + rej, C)
    mf = LogisticRegression(C=fam_best[1], max_iter=3000, class_weight="balanced").fit(Z, yt)
    clf = Classifier(tuple(FEATURE_KEYS), med, mu, sd, m.coef_, m.intercept_, mf.coef_[0], float(mf.intercept_[0]))
    CLASSIFIER_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFIER_PATH.write_text(json.dumps(clf.to_json()))
    print(f"wrote {CLASSIFIER_PATH} (C={best[1]}, CV accuracy {best[0]:.3f})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--set", default=None, help="the delay and reverb pool of cache/<SET>/manifest.json")
    r.add_argument("--models", nargs="*", default=None)
    r.add_argument("--all", nargs="*", default=None, choices=["delay", "reverb"], help="every eligible model of these")
    r.add_argument("--mods", action="store_true", help="the mod models the benchmark draws (tremolo, 70s chorus, bubble vibrato, MM4 chorus)")
    r.add_argument("--all-reverbs", action="store_true", help="with --all reverb: all 24 reverbs, not the benchmark's five")
    r.add_argument("--draws", type=int, default=60)
    r.add_argument("--max-machine-hosts", type=int, default=6)
    f = sub.add_parser("fit-seeds")
    f.add_argument("--models", nargs="*", default=None)
    f.add_argument("--set", default=None, help="only the delay and reverb pool of cache/<SET>/manifest.json")
    f.add_argument("--procs", type=int, default=2)
    f.add_argument("--refresh", action="store_true")
    c = sub.add_parser("fit-classifier")
    c.add_argument("--procs", type=int, default=2)
    c.add_argument("--tune-set", default=None, help="also train on the tuning half (even positions) of this dev set's renders")
    c.add_argument("--cal-per-model", type=int, default=6, help="calibration renders per model added to the corpus (presence is costly)")
    c.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    return {"render": cmd_render, "fit-seeds": cmd_fit_seeds, "fit-classifier": cmd_fit_classifier}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
