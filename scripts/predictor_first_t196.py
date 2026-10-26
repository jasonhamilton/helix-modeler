#!/usr/bin/env python3
# historical, Mother-specific
"""T196: predictor-first drive matcher vs the AFx-Rep shortlist (AFx final ranking), on the T185 harness. MEASUREMENT ONLY.

Nothing is adopted; `match_block.py --shortlist predictor` is a non-default option. Targets (seed 196; no T181/T183/T185 setting
or excerpt reused), all 10 s of real DI (what the head was trained on) rendered through one random drive model:
  gt_P1, gt_P2   24 each, from Guitar-TECHS files the head never trained on (drive-dataset-v1 'test' + 'v1test' splits);
  human          24 from the human's own dry takes (data/songs/mother-playalong/takes; a player the head never saw).
Two matcher-DI conditions per target: blind (another player; for a human target a Guitar-TECHS DI) and same_player (another,
non-overlapping excerpt of the same player). Per (target, condition) ONE job runs both arms on the same host:
  afx   the T185 AFx-Rep shortlist (336-render grid, top 8, + families + second choice), AFx final order;
  pred  tonematch.search.predictor_first (embed once, head, <=6 models x ~5 renders, AFx-Rep order), + the final re-score.

    .venv/bin/python scripts/predictor_first_t196.py plan
    .venv/bin/python scripts/predictor_first_t196.py render
    .venv/bin/python scripts/predictor_first_t196.py run --shard 0/3        (one process per host)
    .venv/bin/python scripts/predictor_first_t196.py analyse
    .venv/bin/python scripts/predictor_first_t196.py clean                  (deletes the target and DI audio)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("TONEMATCH_AFXREP_DEVICE", "cpu")
import nodi_drive_e2e_v2 as V2  # noqa: E402

SR, SEED = 48000, 196
EXC_S, LEAD_S = 10.0, 0.5
N_GT, N_HUMAN = 24, 24
OUT = ROOT / "cache" / "matches" / "t196"
AUDIO = OUT / "audio"
REPORT = ROOT / "reports" / "local" / "t196"
DI_ROOT = ROOT / "data" / "di"
TAKES = ROOT / "data" / "songs" / "mother-playalong" / "takes"
CONDS = ("blind", "same_player")
GROUPS = ("gt_P1", "gt_P2", "human")
V2.OUT, V2.AUDIO = OUT, AUDIO                       # run_job reads the audio from V2.AUDIO
DRIVE_TOL = 0.15
ARMS = ("afx_top8", "afx_fam2", "pred_confirm", "pred_own", "pred_top1_confirm")


def _starts(x, min_active):
    import drive_dataset_render as DDR
    return DDR.active_starts(x, SR, EXC_S, 0.5, min_active=min_active, min_onsets=3)


def _overlaps(taken, rel, s, n, tol=0):
    return any(rel == p and s < b - tol and s + n > a + tol for p, a, b in taken)


_CACHE: dict = {}


def _load(f):
    if f not in _CACHE:
        import soundfile as sf
        if sf.info(str(f)).samplerate != SR:
            _CACHE[f] = (None, [])
        else:
            x = V2.read_mono(f)
            _CACHE[f] = (x, next((st for st in (_starts(x, ma) for ma in (0.6, 0.4)) if st), []))
        if len(_CACHE) > 12:
            _CACHE.pop(next(iter(_CACHE)))
    return _CACHE[f]


def pick_excerpt(files, rng, taken, avoid=(), tries=3000, own=(), own_tol=0):
    """A random active 10 s window of a random file, not overlapping `taken` (and not `avoid`); None if none found."""
    n = int(EXC_S * SR)
    for _ in range(tries):
        f = files[int(rng.integers(len(files)))]
        x, st = _load(f)
        if not st:
            continue
        s = int(st[int(rng.integers(len(st)))])
        if s < int(LEAD_S * SR):
            continue
        rel = str(f.relative_to(ROOT))
        if _overlaps(taken, rel, s, n, own_tol) or _overlaps(avoid, rel, s, n) or _overlaps(own, rel, s, n, own_tol):
            continue
        seg = x[s:s + n]
        rms = 10 * np.log10(np.mean(seg.astype(float) ** 2) + 1e-20)
        if rms < -50:
            continue
        return {"path": rel, "start": s, "n": n, "rms_dbfs": float(rms), "peak_dbfs": float(20 * np.log10(np.max(np.abs(seg)) + 1e-12))}
    return None


def cmd_plan(a) -> None:
    import distortion_step4 as D4
    from tonematch.metrics import distortion_step3 as S3

    rng = np.random.default_rng(SEED)
    dd = json.loads((ROOT / "cache" / "drive-dataset-v1" / "plan.json").read_text())["files"]
    held = {p: sorted(DI_ROOT / f for f in dd["test"] + dd["v1test"] if f"/{p}_" in f or f.split("/")[1].startswith(p)) for p in ("P1", "P2")}
    allgt = {p: sorted(f for d in sorted((DI_ROOT / "Guitar-TECHS").glob(f"{p}_*")) if d.name.split("_", 1)[1] in
                       ("scales", "chords", "singlenotes", "techniques") for f in (d / "audio" / "directinput").glob("*.wav"))
              for p in ("P1", "P2")}
    human_files = [f for f in sorted(TAKES.glob("*_dry.wav")) if V2.read_mono(f).shape[0] > 30 * SR]
    plans = [ROOT / "cache" / "distortion-step4" / "plan.json", ROOT / "cache" / "matches" / "t183" / "plan.json",
             ROOT / "cache" / "matches" / "t185" / "plan.json"]
    taken, old = [], set()
    for pl in plans:
        if pl.exists():
            j = json.loads(pl.read_text())
            taken += [(e["path"], e["start"] - int(LEAD_S * SR), e["start"] + e["n"]) for e in j["excerpts"]]
            old |= {(t["model"], tuple(sorted(t["cc"].items()))) for t in j["targets"]}
    # target excerpts, interleaved P1, P2, human
    queue = {"gt_P1": [], "gt_P2": [], "human": []}
    own = []                                          # target windows of this plan
    for g, k in (("gt_P1", N_GT), ("gt_P2", N_GT), ("human", N_HUMAN)):
        files = held["P1"] if g == "gt_P1" else held["P2"] if g == "gt_P2" else human_files
        assert files, g
        for _ in range(k):
            e = pick_excerpt(files, rng, taken, own=own, own_tol=int(5 * SR) if g == 'human' else 0)
            assert e, f"no excerpt for {g} after {len(queue[g])}"
            e["player"] = g
            own.append((e["path"], e["start"] - int(LEAD_S * SR), e["start"] + e["n"]))
            queue[g].append(e)
    models, sem = D4.pool_models()
    pick = [str(m) for m in rng.permutation(models)] + [str(m) for m in rng.permutation(models)[:N_GT * 2 + N_HUMAN - len(models)]]
    pick = [pick[i] for i in rng.permutation(len(pick))]
    dcc = [c for c in range(128) if c not in D4.DRIVE_MEASURED]
    tcc = [c for c in range(128) if c not in D4.TONE_MEASURED]
    one = [c for c in range(128) if c not in set(S3.ONE_KNOB)]
    targets = []
    i = 0
    for k in range(N_GT):
        for g in GROUPS[:2] + ("human",):
            if g == "human" and k >= N_HUMAN:
                continue
            e = queue[g][k]
            m = pick[i]
            s = sem[m]
            while True:
                cc = {s["drive"]: int(rng.choice(one))} if s["tone"] is None else \
                    {s["drive"]: int(rng.choice(dcc)), s["tone"]: int(rng.choice(tcc))}
                if (m, tuple(sorted(cc.items()))) not in old:
                    break
            tid = f"x{i:02d}"
            rec = {"id": tid, "group": g, "model": m, "excerpt": e, "cc": cc, "drive": s["drive"], "tone": s["tone"], "matcher": {}}
            # matcher DIs: 'blind' = another player (human: Guitar-TECHS), 'same_player' = same player, no overlap with the target
            for c in CONDS:
                if c == "same_player":
                    files = held["P1"] + allgt["P1"] if g == "gt_P1" else held["P2"] + allgt["P2"] if g == "gt_P2" else human_files
                elif g == "gt_P1":
                    files = allgt["P2"]
                elif g == "gt_P2":
                    files = allgt["P1"]
                else:
                    files = allgt["P1"] + allgt["P2"]
                me = pick_excerpt(files, rng, taken, avoid=[(e["path"], e["start"] - int(LEAD_S * SR), e["start"] + e["n"])])
                assert me, (tid, c)
                rec["matcher"][c] = {**me, "id": f"{tid}_{c}"}
            targets.append(rec)
            i += 1
    plan = {"version": "predictor-first-t196", "seed": SEED, "excerpt_s": EXC_S, "lead_s": LEAD_S, "targets": targets,
            "head": "heads_main_v2data_mlp_multi.pkl",
            "note": "GT targets come only from drive-dataset-v1 test/v1test files; matcher DIs are free-choice excerpts"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))
    (OUT / "tol_map.json").write_text((ROOT / "cache" / "matches" / "t185" / "tol_map.json").read_text())
    from collections import Counter
    print(len(targets), "targets", Counter(t["group"] for t in targets), "models", len({t["model"] for t in targets}))


def load_plan():
    return json.loads((OUT / "plan.json").read_text())


def cmd_render(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    import distortion_step4 as D4
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    plan, catalog, lead = load_plan(), load_catalog(), int(LEAD_S * SR)
    for t in plan["targets"]:
        for c in CONDS:
            m = t["matcher"][c]
            if not (AUDIO / f"dry_{m['id']}.wav").exists():
                V2.write_wav(AUDIO / f"dry_{m['id']}.wav", V2.read_mono(ROOT / m["path"])[m["start"]:m["start"] + m["n"]])
    engine = MB.make_engine(144_000)
    rng = np.random.default_rng(SEED + 1)
    try:
        for t in plan["targets"]:
            if (AUDIO / f"{t['id']}_target.wav").exists() and "params" in t:
                continue
            guard.wait(lambda d: print(f"busy cores {d:.1f}: pausing", flush=True), poll_s=10)
            e = t["excerpt"]
            x = V2.read_mono(ROOT / e["path"])[e["start"] - lead:e["start"] + e["n"]]
            for attempt in range(8):
                p, vals = D4._preset(lib, nat, catalog, t["model"], t["cc"])
                engine.load(p)
                try:
                    y = np.asarray(engine.render(x), np.float32)[lead:]
                    break
                except Exception as ex:  # noqa: BLE001
                    if "silence" not in str(ex):
                        raise
                    print(t["id"], "silent setting, redrawn", t["cc"], flush=True)
                    t["cc"] = {k: int(rng.integers(8, 120)) for k in t["cc"]}
            else:
                raise RuntimeError(f"{t['id']}: no audible setting")
            t["params"] = vals
            V2.write_wav(AUDIO / f"{t['id']}_target.wav", y)
            print(t["id"], t["group"], t["model"], vals, flush=True)
            (OUT / "plan.json").write_text(json.dumps(plan, indent=1))
    finally:
        engine.close()
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))


def job_path(t, cond) -> Path:
    return OUT / "jobs" / cond / f"{t['id']}.json"


def run_pred(engine0, blocks, t, cond, head, knob_map, excluded) -> dict:
    import match_block as MB
    from tonematch.search import predictor_first as PF

    t0 = time.time()
    engine = V2.SilentSafe(engine0)
    r0 = getattr(engine, "renders", 0)
    args = SimpleNamespace(target_wav=str(AUDIO / f"{t['id']}_target.wav"), dry_wav=str(AUDIO / f"dry_{t['matcher'][cond]['id']}.wav"),
                           song=f"t196-{t['id']}")
    section, _ = MB.build_target_section(args)
    r = PF.run_predictor_first(engine, blocks, section, head, knob_map, allowed=[m for m in blocks if m not in excluded],
                               log=lambda *_: None)
    kit = MB.make_kit(section, 12.0, 0.5)
    fin = MB.final_check(engine, {m: blocks[m] for m in r["selected"]}, r["rows"], section, kit, len(r["rows"]), log=lambda *_: None)
    return {"id": t["id"], "cond": cond, "predicted": r["predicted"], "predictor_own": r["predictor_own"], "selected": r["selected"],
            "p_selected": r["p_selected"], "sigma": r["sigma"], "ranked": r["ranked"],
            "candidates": [{k: c[k] for k in ("model", "label", "drive", "tone", "values", "distance")} for c in r["candidates"]],
            "final": [{"model": d["model"], "values": d["values"], "loss": d["loss"]} for d in fin],
            "renders_candidates": r["renders"], "renders_total": getattr(engine, "renders", 0) - r0, "silent_renders": engine.silent,
            "embed_s": r["embed_s"], "wall_s": time.time() - t0}


def cmd_run(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.metrics.distortion_step4 import EXCLUDED
    from tonematch.search import knob_tol as KT
    from tonematch.search import predictor_first as PF
    from tonematch.search import shortlist as SL

    i, n = map(int, a.shard.split("/"))
    plan, catalog = load_plan(), load_catalog()
    for t in plan["targets"]:                                  # run_job reads t['matcher'][cond] as an excerpt id
        pass
    blocks = MB.make_blocks(MB.category_pool("drive", catalog), "drive", catalog)
    fams, second, members = SL.load_families(V2.FAMILIES), SL.load_second_choice(V2.FAMILIES, 0.5), SL.family_members(V2.FAMILIES)
    head, knob_map = PF.Head.load(), KT.load_sensitivity()[0]
    jobs = [(t, c) for t in plan["targets"] for c in CONDS]
    jobs = [j for k, j in enumerate(jobs) if k % n == i and not job_path(*j).exists()]
    print(f"shard {a.shard}: {len(jobs)} jobs", flush=True)
    engine = MB.make_engine(144_000)
    try:
        for k, (t, c) in enumerate(jobs):
            guard.wait(lambda d: print(f"busy cores {d:.1f} > {guard.gate.pause_above:.1f}: pausing", flush=True), poll_s=15)
            if a.stop_file and Path(a.stop_file).exists():
                print("stop file present", flush=True)
                break
            if job_path(t, c).exists():
                continue
            tt = {**t, "matcher": {cc: t["matcher"][cc]["id"] for cc in CONDS}}        # the v2 job wants excerpt ids
            afx = V2.run_job(engine, blocks, tt, c, plan, fams, second, members, None, (12.0, 0.5))
            pred = run_pred(engine, blocks, t, c, head, knob_map, EXCLUDED)
            p = job_path(t, c)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"id": t["id"], "cond": c, "afx": afx, "pred": pred}, default=float))
            print(f"[{k + 1}/{len(jobs)}] {t['id']} {c} afx {afx['wall_s']:.0f}s/{afx['renders_total']} pred {pred['wall_s']:.0f}s/"
                  f"{pred['renders_total']} busy {guard.last:.1f}", flush=True)
    finally:
        engine.close()


# ------------------------------------------------------------------------------------------- analyse

def cmd_analyse(a) -> None:
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.search.knob_tol import make_knob_tol

    plan, catalog = load_plan(), load_catalog()
    fam = json.loads(V2.FAMILIES.read_text())["model_to_family"]
    kt = make_knob_tol(DRIVE_TOL, map_path=OUT / "tol_map.json")
    allb = MB.make_blocks(MB.category_pool("drive", catalog), "drive", catalog)
    rng = {m: {n: (lo, hi) for n, lo, hi, _ in b.free} for m, b in allb.items()}
    rows, preds = [], {}
    import helix_trust_set as HT
    lv, mlv = {}, {}
    for t in plan["targets"]:
        e = t["excerpt"]
        x = V2.read_mono(ROOT / e["path"])[e["start"]:e["start"] + e["n"]]
        for c in CONDS:
            mf = AUDIO / f"dry_{t['matcher'][c]['id']}.wav"
            if mf.exists():
                mlv[f"{t['id']}_{c}"] = HT.loud_level_db(V2.read_mono(mf)) + 26.7
        lv[t["id"]] = HT.loud_level_db(x) - HT.REF_DB if hasattr(HT, "REF_DB") else HT.loud_level_db(x) + 26.7
    (REPORT).mkdir(parents=True, exist_ok=True)
    (REPORT / "input_levels.json").write_text(json.dumps({"note": "T194: loud-frame level of each target DI relative to -26.7 dBFS (the level of the old renders); "
                                                          "no level normalisation was applied in T196", "rel_db": lv, "matcher_rel_db": mlv}, indent=1))
    for t in plan["targets"]:
        if "params" not in t:
            continue
        ktm = kt.for_model(t["model"])
        unit = lambda n, v: (float(v) - rng[t["model"]][n][0]) / (rng[t["model"]][n][1] - rng[t["model"]][n][0])   # noqa: E731
        for c in CONDS:
            f = job_path(t, c)
            if not f.exists():
                continue
            j = json.loads(f.read_text())
            A, P = j["afx"], j["pred"]
            fin = {d["model"]: d for d in P["final"]}
            pcand = {}
            for cd in P["candidates"]:
                if cd["model"] not in pcand or cd["distance"] < pcand[cd["model"]]["distance"]:
                    pcand[cd["model"]] = cd
            by_p = sorted(P["p_selected"], key=lambda m: -P["p_selected"][m])
            arm_out = {}
            for arm in ("afx_top8", "afx_fam2"):
                job = {"expanded_fam2": A["expanded_fam2"], "top8": A["top8"], "ranked": A["ranked"], "final": A["final"]}
                o = V2.arm_order(job, "afx_top8" if arm == "afx_top8" else "afx_fam2")
                arm_out[arm] = ([x["model"] for x in o], o[0]["values"], V2.arm_renders(A, arm.replace("afx_", "afx_")), A["wall_s"])
            order = [r["model"] for r in P["ranked"]]
            arm_out["pred_confirm"] = (order, pcand[order[0]]["values"], P["renders_total"], P["wall_s"])
            own = P["predictor_own"]
            top3_p = [x["model"] for x in P["predicted"][:3]]
            arm_out["pred_own"] = ([own["model"]] + top3_p[1:], own["values"], 0, P["embed_s"])
            m1 = by_p[0]
            arm_out["pred_top1_confirm"] = ([m1] + [m for m in order if m != m1], pcand[m1]["values"], P["renders_total"], P["wall_s"])
            for arm, (names, vals, nren, wall) in arm_out.items():
                ok = names[0] == t["model"]
                d_ok = t_ok = None
                if ok:
                    d_ok = bool(abs(unit(t["drive"], vals[t["drive"]]) - unit(t["drive"], t["params"][t["drive"]])) <= DRIVE_TOL)
                    if t["tone"]:
                        pos = unit(t["tone"], t["params"][t["tone"]])
                        t_ok = bool(abs(unit(t["tone"], vals[t["tone"]]) - pos) <= ktm.tolerance(t["tone"], pos) + 1e-9)
                rows.append({"id": t["id"], "group": t["group"], "cond": c, "arm": arm, "model": t["model"], "family": fam.get(t["model"]),
                             "has_tone": bool(t["tone"]), "in_list": t["model"] in names, "top1": ok, "top3": t["model"] in names[:3],
                             "fam1": fam.get(names[0]) == fam.get(t["model"]), "drive_ok": d_ok, "tone_ok": t_ok,
                             "renders": nren, "wall_s": wall,
                             "level_rel_db": lv[t["id"]], "matcher_level_rel_db": mlv.get(f"{t['id']}_{c}"),
                             "drive_serr": (unit(t["drive"], vals[t["drive"]]) - unit(t["drive"], t["params"][t["drive"]]) if ok else None),
                             "drive_err": (abs(unit(t["drive"], vals[t["drive"]]) - unit(t["drive"], t["params"][t["drive"]])) if ok else None),
                             "p_true": next((x["p"] for x in P["predicted"] if x["model"] == t["model"]), None),
                             "covered": t["model"] in P["selected"]})
                preds.setdefault(c, {}).setdefault(arm, {})[t["id"]] = {"predicted": names[:3], "values": vals}
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "rows.json").write_text(json.dumps(rows, indent=1))
    tinfo = {t["id"]: {"group": t["group"], "model": t["model"], "settings": t.get("params"), "target_di": t["excerpt"]["path"],
                       "target_di_start": t["excerpt"]["start"], "target_di_n": t["excerpt"]["n"], "lead_in_samples": int(LEAD_S * SR),
                       "matcher": {c: t["matcher"][c]["path"] for c in CONDS}} for t in plan["targets"]}
    (REPORT / "predictions.json").write_text(json.dumps({"note": "T196 predictions per condition and arm. LOCAL ONLY.",
                                                         "targets": tinfo, "predictions": preds}, indent=1))
    summ = summarise(rows)
    (REPORT / "summary.json").write_text(json.dumps(summ, indent=1))
    print_tables(summ)


CELLS = [("gt_blind", lambda r: r["group"] != "human" and r["cond"] == "blind"),
         ("gt_same", lambda r: r["group"] != "human" and r["cond"] == "same_player"),
         ("human_blind", lambda r: r["group"] == "human" and r["cond"] == "blind"),
         ("human_same", lambda r: r["group"] == "human" and r["cond"] == "same_player")]


def summarise(rows):
    out = {"cells": {}, "paired": {}, "coverage": {}}
    for cell, sel in CELLS:
        for arm in ARMS:
            rs = [r for r in rows if r["arm"] == arm and sel(r)]
            if not rs:
                continue
            s = {k: V2.frac(rs, k) for k in ("in_list", "top1", "top3", "fam1", "drive_ok", "tone_ok")}
            both = [bool(r["top1"] and r["drive_ok"] and (r["tone_ok"] if r["has_tone"] else True)) for r in rs]
            s["knobs_all_ok"] = {"k": sum(both), "n": len(rs), "ci": list(V2.wilson(sum(both), len(rs)))}
            s["renders_mean"] = float(np.mean([r["renders"] for r in rs]))
            s["wall_mean_s"] = float(np.mean([r["wall_s"] for r in rs]))
            out["cells"][f"{cell}/{arm}"] = s
        by = {arm: {(r["id"], r["cond"]): r for r in rows if r["arm"] == arm and sel(r)} for arm in ARMS}
        ids = sorted(by["afx_top8"])
        if "pred_confirm" in by and ids:
            pc = [by["pred_confirm"][i] for i in ids if i in by["pred_confirm"]]
            out["coverage"][cell] = {"selected_contains_true": sum(r["covered"] for r in pc), "n": len(pc)}
        for name, (x, y) in {"pred_confirm vs afx_top8": ("pred_confirm", "afx_top8"), "pred_own vs afx_top8": ("pred_own", "afx_top8"),
                             "pred_top1_confirm vs afx_top8": ("pred_top1_confirm", "afx_top8"),
                             "pred_confirm vs pred_own": ("pred_confirm", "pred_own")}.items():
            k2 = sorted(set(by[x]) & set(by[y]))
            for key in ("top1", "top3", "fam1"):
                out["paired"][f"{cell} | {name} | {key}"] = V2.mcnemar([by[x][i][key] for i in k2], [by[y][i][key] for i in k2])
            allk = lambda r: bool(r["top1"] and r["drive_ok"] and (r["tone_ok"] if r["has_tone"] else True))   # noqa: E731
            out["paired"][f"{cell} | {name} | all_knobs"] = V2.mcnemar([allk(by[x][i]) for i in k2], [allk(by[y][i]) for i in k2])
    out["level_split"] = level_split(rows)
    return out


def level_split(rows):
    """Drive error by the target DI's loud-frame level relative to -26.7 dBFS (T194: the head's Drive is level-confounded,
    about -0.009 per dB). Only rows where the arm named the right model. Bins are fixed: below -12 dB, -12 to -6, above -6."""
    bins = [("< -12 dB", -1e9, -12), ("-12 .. -6 dB", -12, -6), ("> -6 dB", -6, 1e9)]
    out = {}
    for gname, gsel in (("gt", lambda r: r["group"] != "human"), ("human", lambda r: r["group"] == "human")):
        for arm in ("pred_own", "pred_confirm", "afx_top8"):
            rs = [r for r in rows if r["arm"] == arm and gsel(r) and r["drive_serr"] is not None]
            if not rs:
                continue
            lvl = np.array([r["level_rel_db"] for r in rs])
            se = np.array([r["drive_serr"] for r in rs])
            cell = {"n": len(rs), "level_rel_median": float(np.median(lvl)), "level_rel_min": float(lvl.min()), "level_rel_max": float(lvl.max()),
                    "slope_per_db": float(np.polyfit(lvl, se, 1)[0]) if len(rs) > 3 and np.ptp(lvl) > 0 else None, "bins": {}}
            for nm, lo, hi in bins:
                sel = (lvl >= lo) & (lvl < hi)
                if sel.any():
                    cell["bins"][nm] = {"n": int(sel.sum()), "mean_signed_err": float(se[sel].mean()), "mean_abs_err": float(np.abs(se[sel]).mean()),
                                        "within_015": int((np.abs(se[sel]) <= DRIVE_TOL).sum())}
            out[f"{gname}/{arm}"] = cell
    return out


def print_tables(summ):
    for key, s in summ["cells"].items():
        print(key, "| top1", V2.fmt(s["top1"]), "| top3", V2.fmt(s["top3"]), "| fam1", V2.fmt(s["fam1"]), "| drive", V2.fmt(s["drive_ok"]),
              "| tone", V2.fmt(s["tone_ok"]), "| all", V2.fmt(s["knobs_all_ok"]), "| renders", round(s["renders_mean"]), "| wall", round(s["wall_mean_s"], 1))
    for k, v in summ["paired"].items():
        print(k, v)
    print("coverage", summ["coverage"])
    for k, v in summ["level_split"].items():
        print("level", k, json.dumps(v))


def cmd_clean(a) -> None:
    n = 0
    for f in AUDIO.glob("*.wav"):
        f.unlink()
        n += 1
    print(f"deleted {n} audio files")


def cmd_ledger(a) -> None:
    summ = json.loads((REPORT / "summary.json").read_text())
    base = "T196 measurement only, nothing adopted: predictor-first (v2data MLP head, 95% / max 6 models, ~5 renders each) vs AFx-Rep shortlist. "
    for key, s in summ["cells"].items():
        cell, arm = key.split("/")
        for m in ("top1", "top3", "fam1", "drive_ok", "tone_ok", "knobs_all_ok"):
            if s[m]["n"]:
                cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native", "--dataset",
                       "predictor-first-v1", "--metric", f"{cell}_{arm}_{m}", "--value", f"{s[m]['k'] / s[m]['n']:.4f}",
                       "--notes", base + f"{s[m]['k']}/{s[m]['n']}", "--renders", str(int(s["renders_mean"])), "--wall-clock-s", f"{s['wall_mean_s']:.1f}"]
                subprocess.run(cmd, check=True, cwd=ROOT, env={**os.environ, "TONEMATCH_AGENT": "search"})


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("plan")
    for nm in ("render", "run"):
        m = sp.add_parser(nm)
        m.add_argument("--max-load", type=float, default=None)
        if nm == "run":
            m.add_argument("--shard", required=True)
            m.add_argument("--stop-file")
    for nm in ("analyse", "clean", "ledger"):
        sp.add_parser(nm)
    a = ap.parse_args()
    {"plan": cmd_plan, "render": cmd_render, "run": cmd_run, "analyse": cmd_analyse, "clean": cmd_clean, "ledger": cmd_ledger}[a.cmd](a)


if __name__ == "__main__":
    main()
