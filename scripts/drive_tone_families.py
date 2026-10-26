#!/usr/bin/env python3
"""T179 (tone-knob ends) and T180 (drive families from the measured fingerprints).

Approved by the human ("go ahead", DECISIONS 2026-10-18, the five post-v4 recommendations).
Measurement only; nothing is adopted. Builds on T174 (scripts/distortion_step3.py, dfp-v1).

    plan       the 8 tone-end settings per two-knob model (tone 0/0.1/0.9/1 at Drive 0.5 and 0.85)
    run        render them on Native with the dfp-v1 probe (at most 4 hosts leased from the host
               scaler, at most 3 analysis workers, resumable; pauses while the busy cores are
               above --max-load busy cores, default 0.95 x cores); fingerprints only, no audio
    tone       the tone-ends analysis -> reports/local/distortion-id/tone-ends/ (+ the search map)
    families   the family clustering -> reports/local/distortion-id/families/ + cache/drive-maps/drive-families-v1.json
    ledger     the benchmark rows (distortion-fp-v1-tone-ends, drive-families-v1)

    nice -n 10 .venv/bin/python scripts/drive_tone_families.py run --hosts 4 --workers 2
    .venv/bin/python scripts/drive_tone_families.py tone
    .venv/bin/python scripts/drive_tone_families.py families
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
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)
os.environ.setdefault("TONEMATCH_NATIVE_AUTOCAPTURE", "0")

from tonematch.metrics import distortion_probe as P  # noqa: E402
from tonematch.metrics import distortion_step3 as S3  # noqa: E402
from tonematch.metrics import drive_fp_mining as DM  # noqa: E402
from tonematch.metrics import drive_tone_families as T  # noqa: E402

FP3 = ROOT / "cache" / "distortion-fp" / "dfp-v1"
FP_DIR = ROOT / "cache" / "distortion-fp" / "dfp-v1-tone-ends"
OUT_T = ROOT / "reports" / "local" / "distortion-id" / "tone-ends"
OUT_F = ROOT / "reports" / "local" / "distortion-id" / "families"
from tonematch import drive_maps as DMAPS  # noqa: E402

DATA_T = DMAPS.maps_dir(ROOT) / DMAPS.SENSITIVITY_NAME   # cache/drive-maps/, where search T178 reads
DATA_F = DMAPS.maps_dir(ROOT) / DMAPS.FAMILIES_NAME
K_RULE = ("consensus of Ward partitions on resamples (settings bootstrap + 20% feature dropout); k in 4..16 "
          "with the two half-consensus partitions agreeing at ARI >= 0.9 and at most 2 single-model families of "
          "unflagged models, then the lowest PAC")
DS_T, DS_F = "distortion-fp-v1-tone-ends", "drive-families-v1"
# controls: step-3 settings rendered again in this run; their fingerprints must equal step 3's
CONTROLS = (("HD2_DistScream808", "Gain@64,Tone@64"), ("HD2_DM4Screamer", "Drive@108,Tone@102"))


def _fp_path(j) -> Path:
    return FP_DIR / j["model"] / f"{j['setting'].replace(',', '_').replace(' ', '')}{j.get('tag', '')}.npz"


def build_plan() -> tuple[list[dict], dict]:
    idx3 = json.loads((FP3 / "index.json").read_text())
    sem = idx3["semantics"]
    jobs = []
    for m in sorted(sem):
        k = sem[m]
        for name, cc, kind in T.tone_end_settings(k["drive"], k["tone"]):
            jobs.append({"model": m, "setting": name, "cc": cc, "kind": kind})
    for m, name in CONTROLS:
        s = next(s for s in idx3["settings"] if s["model"] == m and s["setting"] == name and not s.get("tag"))
        jobs.append({"model": m, "setting": name, "cc": s["cc"], "kind": "control", "tag": "__ctl"})
    return jobs, sem


def cmd_plan(a) -> dict:
    jobs, sem = build_plan()
    two = [m for m in sem if sem[m]["tone"]]
    return {"two_knob_models": len(two), "renders": len(jobs), "controls": len(CONTROLS),
            "tone_cc": T.END_CC, "drive_cc": T.DRIVE_CC}


def _wait_load(guard, log) -> float:
    """Pause while the busy cores are above the guard's line (T193); returns the seconds paused."""
    return guard.wait(lambda d: log({"pause_busy_cores": d}))


def cmd_run(a) -> dict:
    import concurrent.futures as cf
    import multiprocessing as mp

    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib
    from tonematch.engines.host_budget import LoadGuard

    def log(d):
        print(json.dumps(d), flush=True)
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"
    catalog = load_catalog()
    jobs, sem = build_plan()
    FP_DIR.mkdir(parents=True, exist_ok=True)
    idx_path = FP_DIR / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.is_file() else {"version": "dfp-v1", "task": "T179",
                                                                            "settings": []}
    done = {(s["model"], s["setting"], s.get("tag", "")) for s in index["settings"] if "file" in s}
    todo = [j for j in jobs if (j["model"], j["setting"], j.get("tag", "")) not in done]
    if a.limit:
        todo = todo[:a.limit]
    index["semantics"] = {m: sem[m] for m in sem if sem[m]["tone"]}
    sig, _ = P.build_probe("v1")
    sig32 = sig.astype(np.float32)
    t_all = time.perf_counter()
    log({"jobs": len(jobs), "todo": len(todo), "probe_s": round(sig.size / P.SR, 1)})
    render_s = analysis_s = paused_s = 0.0
    loads = []
    guard = LoadGuard(a.max_load)
    paused_s += _wait_load(guard, log)
    pool = nat.NativeRenderPool(workers=a.hosts, templates_dir=lib.library_templates())
    hosts_seen = [len(pool.engines)]
    pending: dict = {}

    def collect(futs) -> float:
        t = time.perf_counter()
        for fu in cf.as_completed(futs):
            j = futs[fu]
            r = fu.result()
            rec = {k: j[k] for k in ("model", "setting", "cc", "kind", "params", "fixed")}
            rec["tag"] = j.get("tag", "")
            rec.update({"file": str(Path(r["file"]).relative_to(ROOT)), "bytes": r["bytes"],
                        "analysis_s": r["analysis_s"]})
            index["settings"].append(rec)
        return time.perf_counter() - t

    ex = cf.ProcessPoolExecutor(max_workers=a.workers, mp_context=mp.get_context("spawn"))
    try:
        for b0 in range(0, len(todo), a.batch):
            paused_s += _wait_load(guard, log)       # never start a batch above the limit
            grp = todo[b0:b0 + a.batch]
            rjobs = []
            for j in grp:
                tpl = lib.template_for(j["model"])
                p = lib.block_preset(j["model"], catalog)
                vals = {}
                for knob, c in j["cc"].items():
                    lo, hi = tpl.ranges.get(f"0:{knob}", (0.0, 1.0))
                    vals[knob] = nat.cc_value(float(lo), float(hi), int(c))
                p.slots[0].params.update(vals)
                j["params"] = vals
                j["fixed"] = {k: v for k, v in p.slots[0].params.items()
                              if k not in vals and not str(k).startswith("@")}
                rjobs.append(nat.RenderJob(dry=sig32, preset=p))
            t0 = time.perf_counter()
            ys = pool.render_batch(rjobs, on_error="none")
            render_s += time.perf_counter() - t0
            hosts_seen.append(len(pool.engines))
            t1 = time.perf_counter()
            futs = {}
            for j, y in zip(grp, ys, strict=True):
                if y is None:
                    index["settings"].append({**{k: j[k] for k in ("model", "setting", "cc", "kind")},
                                              "tag": j.get("tag", ""), "error": "render failed"})
                    continue
                futs[ex.submit(S3.fingerprint_job, np.asarray(y, np.float32), str(_fp_path(j)))] = j
            analysis_s += collect(pending)
            pending = futs
            analysis_s += time.perf_counter() - t1
            ld = guard.demand()
            loads.append(ld)
            idx_path.write_text(json.dumps(P._clean(index), indent=1))
            log({"done": b0 + len(grp), "of": len(todo), "hosts": len(pool.engines),
                 "elapsed_s": round(time.perf_counter() - t_all, 1), "busy": round(ld, 2)})
        analysis_s += collect(pending)
        pending = {}
    finally:
        pool.close()
        ex.shutdown(wait=True, cancel_futures=True)
    runs = index.setdefault("runs", [])
    runs.append({"wall_s": round(time.perf_counter() - t_all, 1), "render_s": round(render_s, 1),
                 "analysis_s": round(analysis_s, 1), "paused_s": round(paused_s, 1), "renders": len(todo),
                 "hosts_seen": sorted(set(hosts_seen)), "busy_max_sampled": round(max(loads), 2) if loads else None,
                 "busy_mean_sampled": round(float(np.mean(loads)), 2) if loads else None, "workers": a.workers})
    index["renders"] = sum(1 for s in index["settings"] if "file" in s)
    index["failed"] = sum(1 for s in index["settings"] if "error" in s)
    idx_path.write_text(json.dumps(P._clean(index), indent=1))
    return {"renders": index["renders"], "failed": index["failed"], **runs[-1]}


# ------------------------------------------------------------------------------------- T179 analysis

def _load(root: Path, idx: dict, tagged: bool = False) -> list[dict]:
    out = []
    for s in idx["settings"]:
        if "file" not in s or (bool(s.get("tag")) != tagged):
            continue
        arr, sc = P.unpack((root / s["file"]).read_bytes())
        out.append({**s, "arr": arr, "sc": sc})
    return out


def controls_check(idx_t: dict, idx3: dict) -> list[dict]:
    rows = []
    for s in idx_t["settings"]:
        if s.get("tag") != "__ctl" or "file" not in s:
            continue
        o = next(x for x in idx3["settings"] if x["model"] == s["model"] and x["setting"] == s["setting"]
                 and not x.get("tag"))
        a, _ = P.unpack((ROOT / s["file"]).read_bytes())
        b, _ = P.unpack((ROOT / o["file"]).read_bytes())
        same = all(np.array_equal(np.nan_to_num(a[k], nan=9e3), np.nan_to_num(b[k], nan=9e3)) for k in b)
        rows.append({"model": s["model"], "setting": s["setting"], "fingerprint_identical_to_step3": bool(same)})
    return rows


def cmd_tone(a) -> dict:
    from tonematch.engines import native_library as lib

    idx3 = json.loads((FP3 / "index.json").read_text())
    idx_t = json.loads((FP_DIR / "index.json").read_text())
    sem = idx3["semantics"]
    recs = _load(ROOT, idx3) + _load(ROOT, idx_t)
    reports, missing = {}, {}
    for m in sorted(sem):
        k = sem[m]
        if not k["tone"]:
            continue
        lines = {}
        for d in T.DRIVE_CC:
            bl = {}
            for r in recs:
                if r["model"] == m and r["cc"].get(k["drive"]) == d and r["cc"].get(k["tone"]) in T.LINE_CC:
                    bl[r["cc"][k["tone"]]] = T.response_block(r["arr"])
            if len(bl) != len(T.LINE_CC):
                missing[f"{m}@{d}"] = sorted(set(T.LINE_CC) - set(bl))
                continue
            lines[d] = bl
        if len(lines) != len(T.DRIVE_CC):
            continue
        rng = lib.template_for(m).ranges.get(f"0:{k['tone']}", (0.0, 1.0))
        reports[m] = T.model_tone_report(m, k, lines, rng)
    res = {"n_models": len(reports), "missing": missing, "controls": controls_check(idx_t, idx3),
           "levels_dbfs": list(T.EFFECT_LEVELS), "band_hz": list(T.BAND), "line_cc": list(T.LINE_CC),
           "rules": {"inert_db": T.INERT_DB, "end_ratio": T.END_RATIO, "taper_rho": T.TAPER_RHO,
                     "taper_ends": T.TAPER_ENDS, "halves_ratio": T.HALVES_RATIO},
           "models": reports, "runs": idx_t.get("runs", [])}
    shapes = {}
    for m, r in reports.items():
        shapes.setdefault(r["shape"], []).append(m)
    res["shape_counts"] = {s: len(v) for s, v in shapes.items()}
    res["shapes"] = shapes
    res["level_free_shape_counts"] = {}
    for r in reports.values():
        res["level_free_shape_counts"][r["shape_level_free"]] = res["level_free_shape_counts"].get(r["shape_level_free"], 0) + 1
    sh = np.array([[r["combined"]["rms_db"][x]["share"] for x in ("bottom", "middle", "top")] for r in reports.values()])
    res["share_median"] = {x: float(v) for x, v in zip(("bottom", "middle", "top"), np.median(sh, axis=0))}
    res["ends_share_median"] = float(np.median(sh[:, 0] + sh[:, 2]))
    res["ends_share_over_half"] = int(np.sum(sh[:, 0] + sh[:, 2] > 0.5))
    res["drives_agree"] = int(sum(r["combined"]["rms_db"]["drives_agree"] for r in reports.values()))
    OUT_T.mkdir(parents=True, exist_ok=True)
    (OUT_T / "summary.json").write_text(json.dumps(P._clean(res), indent=1))
    smap = {"version": "drive-tone-knob-sensitivity-v1", "task": "T179",
            "source": "Native 3.82, dfp-v1 probe, Drive 0.5 and 0.85, Level and other knobs at library defaults; "
                      "measurement only, nothing adopted",
            "effect": "RMS dB difference of the fundamental response, 100 Hz-10 kHz, at -66/-30/0 dBFS input; "
                      "'level_free' removes each level's mean change; per 0.1 of knob travel; mean of the two Drives",
            "format": "per model: drive_knobs / tone_knobs (read by search's knob_tol loader); knobs: tone knob -> "
                      "measured entry; suggested_tol: equal-effect suggestion, NOT read by the loader (tolerances "
                      "are search's choice); one-knob models have drive_knobs only",
            "models": {}}
    for m in sorted(sem):
        k = sem[m]
        if m in reports:
            r = reports[m]
            ent = T.search_entry(r)
            smap["models"][m] = {"drive_knobs": [k["drive"]], "tone_knobs": [k["tone"]], "knobs": {k["tone"]: ent},
                                 "suggested_tol": T.suggested_tol(ent)}
        elif not k["tone"]:
            smap["models"][m] = {"drive_knobs": [k["drive"]], "tone_knobs": [], "knobs": {}}
    (OUT_T / "knob_sensitivity.json").write_text(json.dumps(P._clean(smap), indent=1))
    DATA_T.parent.mkdir(parents=True, exist_ok=True)
    DATA_T.write_text(json.dumps(P._clean(smap), indent=1))
    _tone_figures(res)
    return res


def _tone_figures(res: dict) -> None:
    import distortion_id_steps01 as S01

    ms = sorted(res["models"], key=lambda m: -(res["models"][m]["combined"]["rms_db"]["bottom"]["share"]
                                              + res["models"][m]["combined"]["rms_db"]["top"]["share"]))
    # per-0.1 effect along the knob for every model, small multiples as one line chart per shape
    x = [np.mean([T.pos(a), T.pos(b)]) for a, b in zip(T.LINE_CC, T.LINE_CC[1:])]
    for shape, members in res["shapes"].items():
        ser = []
        for i, m in enumerate(sorted(members)):
            ser.append((x, res["models"][m]["combined"]["rms_db"]["steps_per_0p1_db"], S3.short(m),
                        S01.COLORS[i % len(S01.COLORS)], i >= len(S01.COLORS)))
        S01.svg_lines(OUT_T / f"per0p1_{shape.replace(' ', '_')}.svg", ser,
                      f"Tone knob effect per 0.1 of travel along the knob: shape '{shape}' "
                      "(dB RMS, mean of Drive 0.5 and 0.85)", "tone knob position (step midpoint)", "dB per 0.1",
                      w=1000)
    # shares, sorted
    xs = np.arange(len(ms))
    ser = [(xs, [res["models"][m]["combined"]["rms_db"][r]["share"] for m in ms], r, c, False)
           for r, c in (("bottom", S01.COLORS[0]), ("middle", S01.COLORS[2]), ("top", S01.COLORS[1]))]
    S01.svg_lines(OUT_T / "shares.svg", ser, "Share of the tone knob's total effect in [0, 0.1], [0.1, 0.9], "
                  "[0.9, 1]; models sorted by the two ends' share: " + ", ".join(f"{i}={S3.short(m)}" for i, m in
                                                                                   enumerate(ms)),
                  "model (index)", "share", ylim=(0, 1), w=1400)


# ------------------------------------------------------------------------------------- T180 analysis

def cmd_families(a) -> dict:
    import distortion_id_steps01 as S01

    idx3 = json.loads((FP3 / "index.json").read_text())
    sem = idx3["semantics"]
    recs = _load(ROOT, idx3)
    for r in recs:
        r["card"] = S3.card_values(r["sc"], r["arr"], P.STAIR_LEVELS_V1)
    F = T.model_features(recs)
    models = F["models"]
    Z = T.combine(F["card"], F["curves"], w_card=a.w_card)
    ks = list(range(4, 17))
    ms = sorted(set(models))
    con = T.consensus_report(models, Z, ks, n=a.boot, seed=0)
    k = a.k or T.choose_k_consensus(con, ms)
    cl = np.asarray(con[k]["families"])
    fam = {m: int(c) for m, c in zip(ms, cl)}
    _, M = T.model_means(models, Z)
    stab = {kk: {"pac": v["pac"], "halves_ari": v["halves_ari"], "resample_ari": v["resample_ari"],
                 "sizes": v["sizes"], "singletons": v["singletons"], "silhouette": T.silhouette(M, v["families"]),
                 "confidence_median": float(np.median(list(v["confidence"].values())))} for kk, v in con.items()}
    C = con[k]["coassoc"]
    # variants, for the report: card only, curves only, and without the six quiet-unreliable models
    variants = {}
    for name, Zv in (("card_only", T.combine(F["card"], F["curves"], 1.0)),
                     ("curves_only", T.combine(F["card"], F["curves"], 0.0))):
        _, Mv = T.model_means(models, Zv)
        variants[name] = DM.ari(cl, T.cluster_models(Mv, k))
    keep = [i for i, m in enumerate(ms) if m not in T.QUIET_UNRELIABLE]
    sub = T.consensus_cut(T.coassoc(T.resample_partitions([m for m in models if m not in T.QUIET_UNRELIABLE],
                                                         Z[[i for i, m in enumerate(models) if m not in T.QUIET_UNRELIABLE]],
                                                         k, a.boot, np.random.default_rng(1))), k)
    variants["without_quiet_unreliable_54"] = DM.ari(cl[keep], sub)
    variants["ward_on_means_same_k"] = DM.ari(cl, T.cluster_models(M, k))
    # cards and characters
    cards = {}
    for m in ms:
        ss = [{"cc": r["cc"], "kind": r["kind"], "card": r["card"], "setting": r["setting"]} for r in recs
              if r["model"] == m]
        cards[m] = S3.model_card(m, sem[m], ss)
    centre = {}
    for m in ms:
        s = sem[m]
        want = {s["drive"]: 64} if not s["tone"] else {s["drive"]: 64, s["tone"]: 64}
        r = next(r for r in recs if r["model"] == m and r["cc"] == want)
        centre[m] = r
    member_stab = con[k]["confidence"]
    fams = {}
    for f in sorted(set(cl)):
        mem = [m for m in ms if fam[m] == f]
        chars = [T.member_character(cards[m], centre[m]["sc"]) for m in mem]
        cm = [(centre[m]["card"]["mid_kind"], centre[m]["card"]["mid_db"], centre[m]["card"]["mid_f_hz"]) for m in mem]
        d = T.describe_family(chars, cm)
        fams[str(f)] = {**d, "members": mem, "labels": {m: {"circuit_group": DM.LABELS[m][0],
                                                             "circuit_family": DM.LABELS[m][1],
                                                             "provisional_class": DM.LABELS[m][2]} for m in mem},
                        "quiet_unreliable": [m for m in mem if m in T.QUIET_UNRELIABLE],
                        "member_stability": {m: round(member_stab[m], 3) for m in mem}}
    # duplicate names get a distinguishing suffix
    seen = {}
    for f, d in fams.items():
        seen.setdefault(d["name"], []).append(f)
    for name, fs in seen.items():
        if len(fs) > 1:
            for f in fs:
                fams[f]["name"] = f"{name} [{', '.join(S3.short(m) for m in fams[f]['members'][:2])}]"
    dis = T.label_disagreements(ms, fam)
    null = {}
    sizes = np.repeat(np.arange(k), np.bincount(cl)[1:])
    for j, name in ((0, "groups17"), (1, "family"), (2, "class6")):
        null[name] = DM.ari_null([DM.LABELS[m][j] for m in ms], k, len(ms), n_perm=1000, seed=0, sizes=sizes)[0]
    # nearest other family per model (mean co-assignment), for the report
    second = {}
    for i, m in enumerate(ms):
        other = {f: float(np.mean(C[i, cl == f])) for f in set(cl.tolist()) if f != cl[i]}
        bf = max(other, key=other.get)
        second[m] = {"family": int(bf), "coassoc": round(other[bf], 3)}
    res = {"k": k, "k_rule": K_RULE, "w_card": a.w_card, "n_boot": a.boot, "second_choice": second,
           "stability": {str(kk): v for kk, v in stab.items()},
           "families": fams, "variants_ari": variants, "disagreements": dis, "ari_null95": null}
    OUT_F.mkdir(parents=True, exist_ok=True)
    (OUT_F / "summary.json").write_text(json.dumps(P._clean(res), indent=1))
    def fid(f):
        return f"F{int(f):02d}"
    data = {"version": "drive-families-v1", "task": "T180",
            "source": "measured: dfp-v1 fingerprints (T174), Native 3.82, 12 settings per model; level-free card "
                      "and block-weighted level-free curves, equal weight; consensus of Ward partitions over resamples; circuit names "
                      "are labels only; nothing adopted beyond this map (human 'go ahead' 2026-10-18)",
            "k": k, "k_rule": K_RULE, "halves_ari": round(stab[k]["halves_ari"], 3), "pac": round(stab[k]["pac"], 3),
            "format": "families: family id -> model ids (read by search's shortlist loader); descriptions: family "
                      "id -> measured character; details: per family the circuit-note labels (labels only) and the "
                      "quiet-input-unreliable members",
            "families": {fid(f): d["members"] for f, d in fams.items()},
            "descriptions": {fid(f): d["name"] for f, d in fams.items()},
            "details": {fid(f): {"quiet_unreliable": d["quiet_unreliable"],
                                 "circuit_groups": {m: DM.LABELS[m][0] for m in d["members"]},
                                 "circuit_families": {m: DM.LABELS[m][1] for m in d["members"]}}
                        for f, d in fams.items()},
            "model_to_family": {m: fid(f) for m, f in fam.items()},
            "quiet_unreliable": list(T.QUIET_UNRELIABLE),
            "member_confidence": {m: round(member_stab[m], 3) for m in ms},
            "member_confidence_meaning": "mean share of resamples in which the model sits with its family mates",
            "second_choice": {m: {"family": fid(v["family"]), "coassoc": v["coassoc"]} for m, v in second.items()}}
    DATA_F.parent.mkdir(parents=True, exist_ok=True)
    DATA_F.write_text(json.dumps(P._clean(data), indent=1))
    # figures
    ser = [(ks, [stab[x]["halves_ari"] for x in ks], "two halves agree (ARI)", S01.COLORS[0], False),
           (ks, [stab[x]["resample_ari"][0] for x in ks], "single resample vs consensus (ARI)", S01.COLORS[1], True),
           (ks, [stab[x]["pac"] for x in ks], "ambiguous pairs (PAC, lower is better)", S01.COLORS[2], False),
           (ks, [stab[x]["confidence_median"] for x in ks], "median member confidence", S01.COLORS[3], True)]
    S01.svg_lines(OUT_F / "stability_vs_k.svg", ser, f"Family stability under resampling vs number of families "
                  f"(chosen k={k})", "number of families k", "ARI / silhouette", ylim=(0, 1))
    D = 1.0 - C
    order = sorted(range(len(ms)), key=lambda i: (fam[ms[i]], ms[i]))
    S01.svg_heatmap(OUT_F / "model_distance_by_family.svg", D[np.ix_(order, order)],
                    [S3.short(ms[i]) for i in order], "1 - co-assignment over resamples (0 = always together), "
                    "ordered by measured family", groups=[str(fam[ms[i]]) for i in order])
    # 2-D map (classical MDS)
    n = len(ms)
    J = np.eye(n) - 1 / n
    Dm = np.sqrt(np.maximum(np.sum(M ** 2, 1)[:, None] + np.sum(M ** 2, 1)[None] - 2 * M @ M.T, 0))
    B = -0.5 * J @ (Dm ** 2) @ J
    w, V = np.linalg.eigh(B)
    xy = V[:, -2:][:, ::-1] * np.sqrt(np.maximum(w[-2:][::-1], 0))
    S01.svg_scatter(OUT_F / "family_map.svg", xy, [S3.short(m) + ("*" if m in T.QUIET_UNRELIABLE else "") for m in ms],
                    [f"F{fam[m]}" for m in ms], "Drive models on a 2-D map of the fingerprint distance; colour = "
                    "measured family; * = card not meaningful at quiet input")
    S01.svg_scatter(OUT_F / "family_map_circuit_labels.svg", xy, [S3.short(m) for m in ms],
                    [DM.LABELS[m][1] for m in ms], "Same map, colour = circuit-note family label (B/OD/D/F/O)")
    return res


# ------------------------------------------------------------------------------------- ledger

def cmd_ledger(a) -> int:
    rows = []
    t = json.loads((OUT_T / "summary.json").read_text())
    run = (t.get("runs") or [{}])[-1]
    wall = sum(r.get("wall_s", 0) for r in t.get("runs", []))
    idx_t = json.loads((FP_DIR / "index.json").read_text())
    base = (f"T179 tone-knob ends (human 'go ahead' 2026-10-18); {t['n_models']} two-knob drives, tone 0/0.1/0.9/1 at "
            f"Drive 0.5/0.85 + step-3 tone 0.2/0.5/0.8, dfp-v1 probe, Level fixed; hosts {run.get('hosts_seen')}; "
            "effect = RMS dB of the fundamental response 100 Hz-10 kHz at -66/-30/0 dBFS; nothing adopted; ")
    ctl = t["controls"]
    rows += [(DS_T, "native", "valid_fingerprints", idx_t.get("renders", 0), idx_t.get("renders"), wall,
              base + f"failed {idx_t.get('failed', 0)}; cache/distortion-fp/dfp-v1-tone-ends"),
             (DS_T, "native", "controls_identical_to_step3", sum(c["fingerprint_identical_to_step3"] for c in ctl),
              len(ctl), None, base + "step-3 settings re-rendered in this run, fingerprints byte-compared"),
             (DS_T, "native", "ends_share_median", t["ends_share_median"], None, None,
              base + f"median share of the total effect in [0,0.1]+[0.9,1] (20% of travel); shares median bottom "
              f"{t['share_median']['bottom']:.2f} middle {t['share_median']['middle']:.2f} top {t['share_median']['top']:.2f}"),
             (DS_T, "native", "ends_share_over_half_models", t["ends_share_over_half"], t["n_models"], None,
              base + "models whose two ends carry more than half of the effect"),
             (DS_T, "native", "drives_agree_shape", t["drives_agree"], t["n_models"], None,
              base + "models whose shape label is the same at Drive 0.5 and 0.85")]
    for s, n in sorted(t["shape_counts"].items()):
        rows.append((DS_T, "native", f"shape_{s.replace(' ', '_').replace('-', '_')}", n, t["n_models"], None,
                     base + f"shape count (level included); members {', '.join(S3.short(m) for m in t['shapes'][s])[:400]}"))
    f = json.loads((OUT_F / "summary.json").read_text())
    k = str(f["k"])
    basef = (f"T180 drive families from dfp-v1 fingerprints (human 'go ahead' 2026-10-18); 60 models, level-free card "
             f"+ curves, Ward, k={f['k']} ({f['k_rule']}); circuit names labels only; cache/drive-maps/drive-families-v1.json; ")
    rows += [(DS_F, "-", "k", f["k"], 60, None, basef + "families: " + "; ".join(
                 f"{i}={d['name']} ({len(d['members'])})" for i, d in f["families"].items())[:700]),
             (DS_F, "-", "halves_ari", f["stability"][k]["halves_ari"], f["n_boot"], None,
              basef + "agreement (ARI) of the consensus families built from two independent halves of the resamples"),
             (DS_F, "-", "pac", f["stability"][k]["pac"], None, None,
              basef + "share of model pairs together in 10-90% of resamples (lower = crisper)"),
             (DS_F, "-", "resample_ari_mean", f["stability"][k]["resample_ari"][0], f["n_boot"], None,
              basef + f"single resample vs consensus, 5th pct {f['stability'][k]['resample_ari'][1]:.3f}"),
             (DS_F, "-", "member_confidence_median", f["stability"][k]["confidence_median"], 60, None,
              basef + "median over models of the mean co-assignment with family mates"),
             (DS_F, "-", "silhouette", f["stability"][k]["silhouette"], None, None, basef + "model-mean silhouette"),
             (DS_F, "-", "ari_without_quiet_unreliable", f["variants_ari"]["without_quiet_unreliable_54"], 54, None,
              basef + "re-clustered without the six quiet-input-unreliable models, ARI on the other 54")]
    for name, v in f["disagreements"]["ari"].items():
        rows.append((DS_F, "-", f"ari_vs_{name}", v, None, None, basef + f"agreement with circuit-note labels {name}; "
                     f"random 95th pct {f['ari_null95'][name]:.3f}"))
    for ds, eng, metric, value, n, w, notes in rows:
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", eng,
               "--dataset", ds, "--metric", metric, "--value", str(round(float(value), 6)), "--notes", notes]
        if n is not None and ds == DS_T and metric == "valid_fingerprints":
            cmd += ["--renders", str(int(n))]
        elif n is not None:
            cmd[cmd.index("--notes") + 1] = f"n={int(n)}; " + notes
        if w:
            cmd += ["--wall-clock-s", str(round(w, 1))]
        subprocess.run(cmd, check=True, env={**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")})
    print(f"{len(rows)} ledger rows")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["plan", "run", "tone", "families", "ledger"])
    ap.add_argument("--hosts", type=int, default=4)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-load", type=float, default=None,
                    help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    ap.add_argument("--k", type=int, default=0, help="families: force k (default: the stability rule)")
    ap.add_argument("--w-card", type=float, default=0.5)
    ap.add_argument("--boot", type=int, default=200)
    a = ap.parse_args(argv)
    a.hosts = min(a.hosts, 4)
    a.workers = min(a.workers, 3)
    if a.cmd == "plan":
        print(json.dumps(cmd_plan(a), indent=1))
    elif a.cmd == "run":
        print(json.dumps(cmd_run(a)))
    elif a.cmd == "tone":
        r = cmd_tone(a)
        print(json.dumps({k: r[k] for k in ("n_models", "missing", "controls", "shape_counts", "level_free_shape_counts",
                                            "share_median", "ends_share_median", "ends_share_over_half", "drives_agree")}))
    elif a.cmd == "families":
        r = cmd_families(a)
        print(json.dumps({"k": r["k"], "stability": {k: [round(v["halves_ari"], 3), round(v["pac"], 3), v["sizes"]] for k, v in r["stability"].items()},
                          "families": {f: (d["name"], [S3.short(m) for m in d["members"]]) for f, d in r["families"].items()},
                          "variants": r["variants_ari"], "ari": r["disagreements"]["ari"]}, indent=1))
    else:
        return cmd_ledger(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
