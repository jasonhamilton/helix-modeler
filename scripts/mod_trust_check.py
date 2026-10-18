#!/usr/bin/env python3
"""Trust check `mod-trust-v1` for the modulation specialist (T116, owner: tone-metrics).

Question: does `mod_specialist.mod_distance` rank the true modulation preset first, against near misses
of the same model, other modulation models and non-modulation confusers, when the target's LFO is at a
different point of its cycle from the candidates' (a free-running LFO)? And how does `mrstft_v2` (the
admitted known-DI term) do on the same items? Design registered in docs/DECISIONS.md (T116) BEFORE any
render was scored.

  gen      offline: draw items, DI excerpts and candidates -> cache/mod-trust-v1/manifest.json, di/*.wav
           (local, gitignored: model ids). Refuses to redraw a registered set.
  render   Native, at most 2 hosts (never more than 6 native_host processes on the machine): every
           candidate at the engine's default pre-roll, plus the truth again with a longer pre-roll
           (the LFO-shifted target). Resumable.
  score    offline: the terms on both target conditions, rule B, per subset and distractor kind; the
           specialist's measurements (detection, sub-type, model ranking, knob seeds) on the shifted
           target. Writes report.json; --ledger appends rows (kind loss-trust and benchmark,
           dataset mod-trust-v1).

Set (seed 1161, block-library-v1.2, 10 s clips at 48 kHz):
  core      the 11 Native stand-in modulation models (synthetic-v1's eligibility rule), 3 items each;
  extended  the other calibration models of `scripts/mod_calibrate.py` (choruses, flangers, phasers,
            vibes, rotaries), 1 item each; reported separately, outside the bar.
  Per item: the truth (knobs on the CC grid at 0.10-0.90), 9 near misses (same model, 1-2 knobs moved
  0.12-0.30 of their range), 6 other core modulation models at random knobs, and 4 confusers (one random
  eq, comp, delay and reverb block of synthetic-v1's eligible lists): 19 distractors.
  DI: Guitar-TECHS excerpts not used by synthetic-v1, synthetic-devfam (when present), helix-trust-v2's
  modulation items or the calibration file.
Target conditions:
  aligned  the truth render through `loss_trust_check.aligned_capture` (fractional delay, gain, pink noise);
  phase    the truth rendered with the pre-roll lengthened by U(0.15, 1.9) s (the LFO starts elsewhere in
           its cycle), then the same aligned capture.
Rule B: a distractor that nulls at -30 dB or lower against the truth (default pre-roll) is a tie.
Terms: `mod_distance`; `mrstft_v2` (DEFAULT); `mod_family` = mrstft_v2 + 0.2 x mod_distance (the family score the
dispatch would use; its own bar, amendment in DECISIONS before scoring).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

SR = 48_000
DATASET = "mod-trust-v1"
OUT = ROOT / "cache" / DATASET
SEED = 1161
LIBRARY = "block-library-v1.2"
SECONDS = 10.0
CORE_PER_MODEL = 3
N_NEAR = 9
N_OTHER_MOD = 6
CONFUSER_CATS = ("eq", "comp", "delay", "reverb")
PHASE_PRE_S = (0.15, 1.9)
DEFAULT_PREROLL = 4800            # NativeEngine.PREROLL for chains without delay, reverb or "other" blocks
MAX_HOSTS = 2
BAR = {"subset": "core/phase", "metric": "rank1_rate_audible", "min": 0.90}
TERMS = ("mod_distance", "mrstft_v2", "mod_family")
FAMILY_W = 0.2                   # mod_family = mrstft_v2 + FAMILY_W x mod_distance (registered, T116)
FAMILY_BAR = {"subset": "core/phase", "min": 0.90, "aligned_max_drop": 0.02}


def render_path(i: int, k: int | str) -> Path:
    return OUT / "renders" / f"item{i:03d}" / (f"c{k:02d}.npy" if isinstance(k, int) else f"{k}.npy")


def excluded_excerpts() -> tuple[set[str], list[dict]]:
    """(files never used, excerpts not to overlap)."""
    import synthetic_v1 as SV

    files = {"Guitar-TECHS/P3_music/audio/directinput/directinput_04.wav"}
    ex = []
    for n in ("synthetic-v1", "synthetic-devfam"):
        if (ROOT / "cache" / n / "labels.json").exists():
            ex += SV.used_excerpts([n])
    h2 = ROOT / "cache" / "loss-trust" / "helix-trust-v2" / "manifest.json"
    if h2.exists():
        for it in json.loads(h2.read_text())["items"]:
            if it["category"] == "mod":
                ex.append({"path": it["di"]["A"]["path"], "start_s": it["di"]["A"]["start_s"]})
    return files, ex


def cmd_gen(a) -> int:
    import helix_trust_set as HT
    import helix_trust_v2 as H2
    import mod_calibrate as MC
    import soundfile as sf
    import synthetic_v1 as SV

    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL

    mfp = OUT / "manifest.json"
    if mfp.exists() and not a.force:
        print(f"{mfp} exists; the set is registered, refusing to redraw it", file=sys.stderr)
        return 3
    if NL.VERSION != LIBRARY:
        raise SystemExit(f"block library is {NL.VERSION}, the set is registered on {LIBRARY}")
    catalog = load_catalog()
    elig = H2.eligible_models(catalog)
    core = sorted(elig["mod"])
    ext = {}
    for m in MC.CAL_MODELS:
        if m in elig["mod"]:
            continue
        tpl = NL.template_for(m)
        fp = SV.free_params(m, tpl, catalog, "mod") if tpl is not None else []
        if fp:
            ext[m] = [list(f) for f in fp]
    free = {"mod": {**elig["mod"], **ext}, **{c: elig[c] for c in CONFUSER_CATS}}
    rng = np.random.default_rng(SEED)
    bad_files, used_ex = excluded_excerpts()
    runs = [r for r in HT.usable_runs(SECONDS) if r["path"] not in bad_files]
    by_file: dict[str, list[dict]] = {}
    for r in runs:
        if all(not (u["path"] == r["path"] and abs(u["start_s"] - r["start_s"]) < SECONDS) for u in used_ex):
            by_file.setdefault(r["path"], []).append(r)
    files = sorted(by_file)
    used: dict[str, list[float]] = {}
    (OUT / "di").mkdir(parents=True, exist_ok=True)
    plan = [("core", m) for m in core for _ in range(CORE_PER_MODEL)] + [("extended", m) for m in sorted(ext)]
    plan = [plan[i] for i in rng.permutation(len(plan))]
    items = []
    for i, (subset, model) in enumerate(plan):
        ks = SV.draw_knobs(rng, free["mod"][model])
        e = H2.pick_clip(rng, by_file, files, used)
        x, g = HT.load_excerpt(e, SECONDS, SV.REF_LOUD_DBFS)
        f = OUT / "di" / f"item{i:03d}_A.wav"
        sf.write(str(f), x, SR, subtype="FLOAT")
        cands = [{"label": "true", "model": model, "category": "mod", "knobs": ks}]
        for _ in range(N_NEAR):
            cands.append({"label": "near", "model": model, "category": "mod", "knobs": SV.near_knobs(rng, ks)})
        others = [m for m in core if m != model]
        for k in rng.permutation(len(others))[:N_OTHER_MOD]:
            m = others[int(k)]
            cands.append({"label": f"mod:{m}", "model": m, "category": "mod", "knobs": SV.draw_knobs(rng, free["mod"][m])})
        for c in CONFUSER_CATS:
            ms = sorted(free[c])
            m = ms[int(rng.integers(len(ms)))]
            cands.append({"label": f"confuser:{c}", "model": m, "category": c, "knobs": SV.draw_knobs(rng, free[c][m])})
        items.append({"item": i, "subset": subset, "true_model": model,
                      "phase_extra_preroll": round(rng.uniform(*PHASE_PRE_S) * SR),
                      "di": {"A": {**e, "gain_db": round(g, 2), "file": str(f.relative_to(ROOT))}},
                      "candidates": cands})
    mf = {"dataset": DATASET, "seed": SEED, "seconds": SECONDS, "library_version": LIBRARY,
          "ref_loud_dbfs": SV.REF_LOUD_DBFS, "conditions": ["aligned", "phase"], "bar": BAR,
          "core_models": core, "extended_models": sorted(ext), "free_params": free, "items": items,
          "default_preroll": DEFAULT_PREROLL,
          "about": "known-DI modulation trust set (T116): candidates at the default pre-roll on the target's DI; "
                   "targets aligned (same LFO phase) and phase (LFO shifted by a longer pre-roll)"}
    mfp.write_text(json.dumps(mf, indent=1))
    print(f"{len(items)} items: core {sum(it['subset'] == 'core' for it in items)} ({len(core)} models), "
          f"extended {sum(it['subset'] == 'extended' for it in items)}; {len(files)} DI files; {mfp}")
    return 0


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
    import mod_calibrate as MC
    import soundfile as sf

    from tonematch.catalog import load_catalog

    mf = json.loads((OUT / "manifest.json").read_text())
    running = MC.machine_hosts()
    hosts = min(a.hosts, MAX_HOSTS, MC.MAX_MACHINE_HOSTS - running)
    if hosts < 1:
        print(f"{running} native_host processes already running; limit {MC.MAX_MACHINE_HOSTS}", file=sys.stderr)
        return 3
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeEngine, NativeRenderPool, RenderJob

    catalog = load_catalog()
    cache: dict = {}
    print(f"native_host before start: {running}; opening {hosts}", flush=True)
    t0 = time.perf_counter()
    errors: dict[str, str] = {}
    todo = [(it, k) for it in mf["items"] for k in range(len(it["candidates"])) if not render_path(it["item"], k).exists()]
    if todo:
        pool = NativeRenderPool(workers=hosts, templates_dir=NL.library_templates())
        try:
            for s in range(0, len(todo), a.chunk):
                chunk = todo[s:s + a.chunk]
                jobs = [RenderJob(dry=sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0],
                                  preset=build_preset(it["candidates"][k], mf["free_params"], catalog, cache))
                        for it, k in chunk]
                outs = pool.render_batch(jobs, on_error="none")
                for j, ((it, k), y) in enumerate(zip(chunk, outs, strict=True)):
                    if y is None:
                        err = repr(pool.last_errors.get(j))
                        if "returned silence" not in err:
                            errors[f"{it['item']}:{k}"] = err
                            continue
                        y = np.zeros(len(jobs[j].dry), np.float32)
                    y = np.asarray(y, np.float32)
                    if y.ndim == 2:
                        y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
                    render_path(it["item"], k).parent.mkdir(parents=True, exist_ok=True)
                    np.save(render_path(it["item"], k), y)
                print(f"candidates {s + len(chunk)}/{len(todo)} in {time.perf_counter() - t0:.0f} s", flush=True)
        finally:
            pool.close()
    ph = [it for it in mf["items"] if not render_path(it["item"], "phase").exists()]
    if ph:
        eng = NativeEngine(templates_dir=NL.library_templates())
        try:
            for it in ph:
                dry = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0]
                eng.load(build_preset(it["candidates"][0], mf["free_params"], catalog, cache))
                eng.preroll = mf["default_preroll"] + it["phase_extra_preroll"]
                y = np.asarray(eng.render(dry), np.float32)
                eng.preroll = None
                if y.ndim == 2:
                    y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
                np.save(render_path(it["item"], "phase"), y)
            print(f"phase targets {len(ph)} in {time.perf_counter() - t0:.0f} s", flush=True)
        finally:
            eng.close()
    (OUT / "render_errors.json").write_text(json.dumps(errors, indent=1))
    print(f"done; errors {len(errors)}; native_host after close: {MC.machine_hosts()}", flush=True)
    return 1 if errors else 0


# --------------------------------------------------------------------------- score

def score_item(args) -> dict:
    it, free, core, ext = args
    import soundfile as sf
    from loss_trust_check import aligned_capture

    from tonematch.metrics import mod_specialist as MS
    from tonematch.metrics import mrstft_v2 as MV2
    from tonematch.metrics import trust as TR

    i = it["item"]
    n = len(it["candidates"])
    dry = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float32")[0]
    ren = [np.load(render_path(i, k)) for k in range(n)]
    clean = ren[0]
    targets = {"aligned": aligned_capture(clean, np.random.default_rng([SEED, i, 2])),
               "phase": aligned_capture(np.load(render_path(i, "phase")), np.random.default_rng([SEED, i, 3]))}
    nulls = [TR.null_db(clean, y) for y in ren[1:]]
    labels = [c["label"] for c in it["candidates"]]
    t0 = time.perf_counter()
    cf = [MS.features(y, dry) for y in ren]
    t_feat = (time.perf_counter() - t0) / n
    out = {"item": i, "subset": it["subset"], "true_model": it["true_model"], "labels": labels, "nulls": nulls,
           "phase_null_db": TR.null_db(clean, np.load(render_path(i, "phase"))), "feat_s": t_feat}
    for cond, x in targets.items():
        tf = MS.features(x, dry)
        sc = {"mod_distance": [MS.distance(tf, f) for f in cf], "mrstft_v2": [MV2.term(x, y) for y in ren]}
        sc["mod_family"] = [a + FAMILY_W * b for a, b in zip(sc["mrstft_v2"], sc["mod_distance"], strict=True)]
        res = {}
        for t, s in sc.items():
            r = TR.rank1_rule_b(s[0], s[1:], nulls)
            kinds = {}
            for kind in ("near", "mod:", "confuser:"):
                idx = [j for j, lb in enumerate(labels[1:]) if lb.startswith(kind)]
                kinds[kind.rstrip(":")] = bool(all(s[1 + j] > s[0] or nulls[j] <= TR.SAME_SOUND_NULL_DB for j in idx))
            j = int(np.argmin(s[1:]))
            res[t] = {**r.as_dict(), "by_kind": kinds, "best_distractor": labels[1 + j], "scores": [float(v) for v in s]}
        out[cond] = res
    # the specialist's measurements on the shifted target (what a real recording would give)
    pool = core if it["subset"] == "core" else core + ext
    sd = MS.seed(targets["phase"], dry, models=pool)
    an = sd["analysis"]
    truth_sub = MS.subtype_of_model(it["true_model"])
    rngs = {nm: (lo, hi) for nm, lo, hi, _ in free["mod"][it["true_model"]]}
    tv = {nm: lo + k / 127 * (hi - lo) for nm, k in it["candidates"][0]["knobs"].items() for lo, hi in [rngs[nm]]}
    knob_err = {}
    est = sd["models"].get(it["true_model"], {}).get("values", {})
    for nm, v in tv.items():
        kind = MS.knob_kind(nm)
        if kind != "other" and nm in est:
            lo, hi = rngs[nm]
            knob_err.setdefault(kind, []).append(abs(est[nm] - v) / (hi - lo))
    hz_rate = None
    if it["true_model"] in ("HD2_TremoloTremolo", "HD2_TremoloHarmonic"):
        hz_rate = {"true": tv["Speed"], "est": an["rate_hz"]}
    conf_an = []
    for k, c in enumerate(it["candidates"]):
        if c["category"] != "mod":
            ca = MS.analyse(ren[k], dry, cf[k], presence_mod=MS.presence_mod_probability(ren[k], dry))
            conf_an.append({"category": c["category"], "mod_score": ca.mod_score, "presence_mod": ca.presence_mod,
                            "is_mod": ca.is_mod})
    out["measures"] = {"is_mod": an["is_mod"], "mod_score": an["mod_score"], "subtype_rule": an["subtype"],
                       "subtype_vote": sd["subtype_vote"], "subtype_true": truth_sub,
                       "model_rank": (sd["ranking"].index(it["true_model"]) + 1) if it["true_model"] in sd["ranking"] else None,
                       "model_top": sd["ranking"][:3], "knob_err": knob_err, "hz_rate": hz_rate,
                       "rate_hz": an["rate_hz"], "am_depth_db": an["am_depth_db"], "pm_depth_cents": an["pm_depth_cents"],
                       "wet_mix": an["wet_mix"], "confusers": conf_an}
    return out


def summarise(items: list[dict]) -> dict:
    from tonematch.metrics import trust as TR

    out: dict = {}
    for subset in ("core", "extended", "all"):
        its = [r for r in items if subset == "all" or r["subset"] == subset]
        if not its:
            continue
        for cond in ("aligned", "phase"):
            for t in TERMS:
                rs = [TR.Rank1(**{k: r[cond][t][k] for k in ("rank1", "rank1_audible", "rank", "rank_audible",
                                                              "n_same_sound", "margin", "margin_audible")}) for r in its]
                rt = TR.rates(rs)
                rt["by_kind"] = {k: float(np.mean([r[cond][t]["by_kind"][k] for r in its])) for k in ("near", "mod", "confuser")}
                rt["fails"] = [(r["item"], r["true_model"], r[cond][t]["best_distractor"]) for r in its if not r[cond][t]["rank1_audible"]]
                out[f"{subset}/{cond}/{t}"] = rt
        ms = [r["measures"] for r in its]
        sub_ok = [m["subtype_vote"] == m["subtype_true"] for m in ms]
        rule_ok = [m["subtype_rule"] == m["subtype_true"] for m in ms]
        ke = {}
        for m in ms:
            for k, v in m["knob_err"].items():
                ke.setdefault(k, []).extend(v)
        hz = [abs(np.log(m["hz_rate"]["est"] / m["hz_rate"]["true"])) for m in ms if m["hz_rate"] and m["hz_rate"]["est"] > 0]
        conf = [c["is_mod"] for m in ms for c in m["confusers"]]
        out[f"{subset}/measures"] = {
            "items": len(ms), "is_mod_recall": float(np.mean([m["is_mod"] for m in ms])),
            "confuser_false_pos": float(np.mean(conf)) if conf else float("nan"),
            "subtype_acc_vote": float(np.mean(sub_ok)), "subtype_acc_rule": float(np.mean(rule_ok)),
            "model_top1": float(np.mean([m["model_rank"] == 1 for m in ms])),
            "model_top3": float(np.mean([m["model_rank"] is not None and m["model_rank"] <= 3 for m in ms])),
            "knob_mae": {k: float(np.mean(v)) for k, v in ke.items()},
            "knob_within_0.1": {k: float(np.mean(np.asarray(v) <= 0.1)) for k, v in ke.items()},
            "hz_rate_items": len(hz), "hz_rate_within_5pct": float(np.mean(np.asarray(hz) <= np.log(1.05))) if hz else float("nan"),
        }
    return out


def file_md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def cmd_score(a) -> int:
    from concurrent.futures import ProcessPoolExecutor

    from tonematch.metrics import mod_specialist as MS

    mf = json.loads((OUT / "manifest.json").read_text())
    missing = [it["item"] for it in mf["items"] if not render_path(it["item"], "phase").exists()
               or any(not render_path(it["item"], k).exists() for k in range(len(it["candidates"])))]
    if missing:
        print(f"renders missing for items {missing[:10]}...", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    args = [(it, mf["free_params"], mf["core_models"], mf["extended_models"]) for it in mf["items"]]
    with ProcessPoolExecutor(a.jobs) as ex:
        items = list(ex.map(score_item, args))
    wall = time.perf_counter() - t0
    summ = summarise(items)
    frozen = {"mod_specialist.py": file_md5(ROOT / "src" / "tonematch" / "metrics" / "mod_specialist.py"),
              "mrstft_v2.py": file_md5(ROOT / "src" / "tonematch" / "metrics" / "mrstft_v2.py"),
              "calibration.json": file_md5(MS.CALIBRATION) if MS.CALIBRATION.exists() else None,
              "mod_specialist_version": MS.VERSION}
    bar_val = summ[f"{BAR['subset']}/mod_distance"]["rank1_audible_only"]
    fam_ph = summ[f"{FAMILY_BAR['subset']}/mod_family"]["rank1_audible_only"]
    fam_al = summ["core/aligned/mod_family"]["rank1_audible_only"]
    gen_al = summ["core/aligned/mrstft_v2"]["rank1_audible_only"]
    verdict = {"bar": BAR, "value": bar_val, "pass": bool(bar_val >= BAR["min"]),
               "family_bar": FAMILY_BAR, "family_phase": fam_ph, "family_aligned": fam_al, "general_aligned": gen_al,
               "family_pass": bool(fam_ph >= FAMILY_BAR["min"] and fam_al >= gen_al - FAMILY_BAR["aligned_max_drop"])}
    rep = {"dataset": DATASET, "frozen": frozen, "verdict": verdict, "summary": summ, "items": items,
           "wall_s": round(wall, 1)}
    (OUT / a.report).write_text(json.dumps(rep, indent=1, default=float))
    for k, v in summ.items():
        if k.endswith("/measures"):
            print(k, json.dumps(v))
        else:
            print(f"{k:34s} rank1(rule B) {v['rank1_audible_only']:.3f} plain {v['rank1_rate']:.3f} "
                  f"p10 margin {v['margin_audible_p10']:+.3f} by kind {v['by_kind']} fails {len(v['fails'])}")
    print("VERDICT", verdict)
    if a.ledger:
        import subprocess

        def row(kind, metric, value, notes):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind,
                            "--engine", "native", "--dataset", DATASET, "--metric", metric, "--value", f"{value:.4f}",
                            "--renders", str(sum(len(it["candidates"]) + 1 for it in mf["items"])),
                            "--wall-clock-s", f"{wall:.1f}", "--notes", notes], check=True)
        md5 = frozen["mod_specialist.py"][:8]
        for subset in ("core", "extended"):
            for cond in ("aligned", "phase"):
                for t in TERMS:
                    v = summ[f"{subset}/{cond}/{t}"]
                    row("loss-trust", f"rank1_rate_{t}", v["rank1_audible_only"],
                        f"{subset}/{cond} rule B; {v['items']} items; plain {v['rank1_rate']:.3f}; p10 margin "
                        f"{v['margin_audible_p10']:+.3f}; mod_specialist {MS.VERSION} md5 {md5}; T116 registered bar "
                        f"core/phase mod_distance >= {BAR['min']}")
            m = summ[f"{subset}/measures"]
            for key in ("is_mod_recall", "confuser_false_pos", "subtype_acc_vote", "model_top1", "model_top3"):
                row("benchmark", f"mod_{key}", m[key], f"{subset}; LFO-shifted targets; mod_specialist {MS.VERSION} md5 {md5}; T116")
            for k, v in m["knob_mae"].items():
                row("benchmark", f"mod_seed_{k}_knob_mae", v, f"{subset}; true model's seeded {k} knob, normalised 0..1; T116")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--force", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=2)
    r.add_argument("--chunk", type=int, default=40)
    s = sub.add_parser("score")
    s.add_argument("--jobs", type=int, default=2)
    s.add_argument("--report", default="report.json")
    s.add_argument("--ledger", action="store_true")
    a = ap.parse_args(argv)
    return {"gen": cmd_gen, "render": cmd_render, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
