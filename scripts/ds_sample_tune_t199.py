#!/usr/bin/env python3
"""T199: refine DS-Sample version c with the cab High Cut freed; confirm on the Stomp; build human-ratings-v7.

  scan     c's settings with only the cab High Cut moved (Native), AFx-Rep distance and band balance
  search   --seed N : CMA-ES from c, 18 knobs (the 17 T198 knobs + 4x12 Greenback 25 High Cut, 500-20100 Hz),
           AFx-Rep distance, T198's +-12 dB EQ limits and LUFS level guard
  report   native render of c and c2, scores, write ds_c2.hlx (validated)
  stomp    read back + save the edit buffer FIRST; render c, c2 (and the human's edited state if it differs
           from c) through take2; leave c (or the human's state) loaded, Input on Main L/R; never flash
  analyze  Stomp scores and band balance against the stem
  session  build human-ratings-v7 (blind pairs + 1-5 ratings, LUFS-matched, 1 retest)

Reuses scripts/ds_sample_tune_t198.py. Song audio stays local (cache/ and reports/local/ are gitignored).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

import ds_sample_tune_t198 as T  # noqa: E402  (sets env: native python, at most 4 hosts)

REF, ROOT, SR, TUNED, WORK = T.REF, T.ROOT, T.SR, T.TUNED, T.WORK
HC = (2, "HighCut")
HC_LO, HC_HI = 500.0, 20100.0       # catalog / template range, Hz
T.KNOBS.append((2, "HighCut", HC_LO, HC_HI))          # 18th knob (T198 functions read the module list)
NAMES = ["c", "c2"]
SESSION = "human-ratings-v7"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def c_values() -> dict:
    v = dict(T.arm_values("c"))
    v[HC] = 8000.0
    return v


def c2_values(seed=None) -> dict:
    f = WORK / "t199_best.json"
    d = json.loads(f.read_text())
    return {(int(k.split(":")[0]), k.split(":")[1]): float(x) for k, x in d["values"].items()}


def tut_c_template_values():
    """What the Native tutorial template holds for every tuned knob (cab High Cut included)."""
    p = T.tutorial_preset()
    return {(s, n): float(p.slots[s].params.get(n, 0.0)) for s, n, _, _ in T.KNOBS}


def native_vals(vals, tpl):
    return {k: T.cc_snap(k, v, tpl) for k, v in vals.items()}


def cmd_scan(a) -> int:
    from tonematch.engines.native import ChainTemplate
    di, target, _ = T.data()
    tpl = next(iter(ChainTemplate.load_all(T.TEMPLATES).values()))
    sc = T.scorer("c", target)
    c = native_vals(c_values(), tpl)
    grid = [4000, 6000, 8000, 10000, 12000, 14000, 16000, 20000]
    sets = []
    for hc in grid:
        v = dict(c)
        v[HC] = T.cc_snap(HC, float(hc), tpl)
        sets.append(v)
    eng = T.make_engine()
    try:
        eng.load(T.tutorial_preset())
        ys = T.render_batch(eng, di, sets)
    finally:
        eng.close()
    raw = sc(ys)
    ref = T.lufs(np.load(WORK / "native_tutorial.npy"))
    rows = []
    for hc, s, y in zip(grid, sets, ys, strict=True):
        bd = T.band_db(y * 10 ** ((T.lufs(target) - T.lufs(y)) / 20)) - T.band_db(target)
        rows.append({"high_cut_hz": hc, "snapped": s[HC], "afxrep": round(float(raw[len(rows)]), 4),
                     "dlufs_vs_tutorial": round(T.lufs(y) - ref, 2),
                     "bands_8_16k_db_vs_stem": round(float(bd[8]), 2), "bands_16_20k_db_vs_stem": round(float(bd[9]), 2)})
        log(rows[-1])
    (WORK / "t199_scan.json").write_text(json.dumps(rows, indent=1))
    return 0


def cmd_search(a) -> int:
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.engines.native import ChainTemplate
    from tonematch.search.cmaes import CMAES

    guard = LoadGuard(a.max_load)
    di, target, _ = T.data()
    tpl = next(iter(ChainTemplate.load_all(T.TEMPLATES).values()))
    ref_l = T.lufs(np.load(WORK / "native_tutorial.npy"))      # T198's guard reference: the tutorial render
    score = T.scorer("c", target)
    x0 = T.values_to_x(native_vals(c_values(), tpl))
    es = CMAES(x0, sigma0=a.sigma, popsize=a.pop, seed=a.seed)
    eng = T.make_engine()
    t0 = time.time()
    hist, best, renders = [], {"loss": 1e9}, 0

    def evaluate(xs):
        nonlocal renders, best
        vals = [T.x_to_values(np.clip(x, 0, 1), None, tpl, None) for x in xs]
        guard.wait(lambda d: log(f"busy cores {d:.1f}: pausing"), poll_s=10)
        ys = T.render_batch(eng, di, vals)
        renders += len(ys)
        raw = score(ys)
        out = []
        for v, y, r in zip(vals, ys, raw, strict=True):
            d = T.lufs(y) - ref_l
            pen = T.LOUD_PEN * max(0.0, abs(d) - T.LOUD_FREE_DB)
            out.append(r + pen)
            if r + pen < best["loss"]:
                best = {"loss": float(r + pen), "raw": float(r), "dlufs": float(d),
                        "values": {f"{s}:{n}": v[(s, n)] for s, n, _, _ in T.KNOBS}, "render": renders}
        return out

    try:
        eng.load(T.tutorial_preset())
        l0 = evaluate([x0])[0]
        start = dict(best)
        log(f"start (c, CC-snapped, High Cut 8 kHz) loss {l0:.4f}")
        gen = 0
        while renders < a.budget:
            X = es.ask()
            f = evaluate(es.clip(X))
            es.tell(X, f)
            gen += 1
            hist.append({"gen": gen, "renders": renders, "best": best["loss"], "gen_best": float(min(f)),
                         "sigma": es.sigma, "best_high_cut_hz": best["values"]["2:HighCut"]})
            log(f"gen {gen} renders {renders} gen_best {min(f):.4f} best {best['loss']:.4f} "
                f"HC {best['values']['2:HighCut']:.0f} sigma {es.sigma:.3f}")
    finally:
        eng.close()
    res = {"seed": a.seed, "popsize": es.lam, "sigma0": a.sigma, "renders": renders, "wall_s": round(time.time() - t0, 1),
           "start_loss": start["loss"], "start": start, "best": best, "history": hist,
           "loudness_guard": {"free_db": T.LOUD_FREE_DB, "penalty_per_db": T.LOUD_PEN, "reference_lufs": round(ref_l, 2)}}
    (WORK / f"t199_search_seed{a.seed}.json").write_text(json.dumps(res, indent=1, default=float))
    log(f"seed {a.seed} done: {renders} renders, {res['wall_s']} s; best {best['loss']:.4f} (start {start['loss']:.4f})")
    return 0


def cmd_pick(a) -> int:
    """Choose the best of the seeds by its own search loss; write t199_best.json."""
    rs = [json.loads(p.read_text()) for p in sorted(WORK.glob("t199_search_seed*.json"))]
    b = min(rs, key=lambda r: r["best"]["loss"])
    out = dict(b["best"], seed=b["seed"], all={str(r["seed"]): r["best"]["loss"] for r in rs})
    (WORK / "t199_best.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["all"]), "-> seed", b["seed"])
    return 0


def stomp_preset(vals):
    p = T.stomp_preset({k: v for k, v in vals.items() if k != HC})
    p.slots[2].params["HighCut"] = float(vals[HC])
    return p


def cmd_report(a) -> int:
    from tonematch import preset as P
    di, target, _ = T.data()
    sets = {"c": c_values(), "c2": c2_values()}
    cat = T.catalog()
    hl = {}
    import subprocess
    for n, nm in (("c2", "DS Tune C2"),):
        p = stomp_preset(sets[n])
        p.name = nm
        viol = [str(v) for v in P.validate(p, cat, None, engine="stomp") if v.severity == "fail"]
        f = TUNED / f"ds_{n}.hlx"
        P.write_hlx(p, f)
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(f)], cwd=ROOT,
                            capture_output=True).returncode
        hl[n] = {"file": str(f.relative_to(ROOT)), "validate_fails": viol, "hlx_validate_exit": rc}
    from tonematch.engines.native import ChainTemplate
    tpl = next(iter(ChainTemplate.load_all(T.TEMPLATES).values()))
    eng = T.make_engine()
    try:
        eng.load(T.tutorial_preset())
        ys = dict(zip(sets, T.render_batch(eng, di, [native_vals(v, tpl) for v in sets.values()]), strict=True))
    finally:
        eng.close()
    ref = T.lufs(np.load(WORK / "native_tutorial.npy"))
    sc = T.scorer("c", target)
    raw = sc(list(ys.values()))
    res = {"hlx": hl, "settings": {n: {f"{s}:{k}": round(v, 4) for (s, k), v in sets[n].items()} for n in sets},
           "native_afxrep": dict(zip(ys, [round(float(x), 4) for x in raw], strict=True)),
           "native_dlufs_vs_tutorial": {n: round(T.lufs(y) - ref, 2) for n, y in ys.items()}}
    (WORK / "t199_report_native.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


def cmd_stomp(a) -> int:
    import dataclasses

    import soundfile as sf

    from tonematch import pedal_lock
    from tonematch.engines.stomp import reamp_hlx_dict
    from tonematch.engines.stomp_readback import readback_diff

    REF._env()
    if REF.hx_edit_running():
        print("HX Edit is running: quit it first. Nothing done.")
        return 2
    di, target, where = T.data()
    sets = {"c": c_values(), "c2": c2_values()}
    out_dir = TUNED / "stomp"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-t199-confirm"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"run_dir": str(run_dir), "steps": [], "renders": {}}

    def out(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
        res["steps"].append(msg)

    pc = stomp_preset(sets["c"])
    final_p = pc
    try:
        with pedal_lock.session("T199 DS-Sample version c2 (cab High Cut freed): confirm on the Stomp"):
            pos = REF.pedal_position()
            res["preflight"] = pos
            out(f"pedal on {pos.get('label')} '{pos.get('name')}' fw {pos.get('firmware')}")
            if (pos["setlist"], pos["index"]) != REF.SCRATCH[:2]:
                out("not slot 04C: nothing done")
                res["error"] = "not on 04C"
                return 3
            eng = REF._open(run_dir, 6, "T199 reamp take2 through c and c2")
            try:
                cur = eng.original_chain             # READ BEFORE ANY WRITE
                (TUNED / "t199_edit_buffer_before.json").write_text(json.dumps(cur, indent=1))
                out(f"edit buffer read and saved: {eng.backup_path.name}")
                diff = readback_diff(cur, reamp_hlx_dict(pc, 1), None, check_input=True)
                res["current_vs_c"] = diff.as_dict()
                out(f"current buffer vs c: ok={diff.ok}, {len(diff.mismatches)} mismatches: {diff.mismatches[:20]}")
                cands = {"c": pc, "c2": stomp_preset(sets["c2"])}
                if not diff.ok:
                    hp = stomp_preset(sets["c"])
                    by_model = {s.get("model_id"): s for s in cur["slots"] if s["kind"] == "Block"}
                    by_paired = {s.get("paired_id"): s for s in cur["slots"] if s["kind"] == "Block" and s.get("paired_id")}
                    changed = {}
                    for sl in hp.slots:
                        if sl.category == "cab":
                            blk = by_paired.get(sl.model_id)
                            pp = (blk or {}).get("paired_params") or []
                        else:
                            blk = by_model.get(sl.model_id)
                            pp = (blk or {}).get("params") or []
                        for q in pp:
                            if q.get("id") in sl.params and abs(float(sl.params[q["id"]]) - float(q["value"])) > 1e-6:
                                changed[f"{sl.model_id}:{q['id']}"] = [sl.params[q["id"]], float(q["value"])]
                                sl.params[q["id"]] = float(q["value"])
                    cands["human_current"] = hp
                    final_p = hp
                    res["human_edits_vs_c"] = changed
                    out(f"HUMAN EDITED STATE differs from c: {json.dumps(changed)[:800]}")
                cands["c_repeat"] = pc
                x = np.concatenate([di, np.zeros(int(REF.TAIL_S * SR), np.float32)])
                for name, p in cands.items():
                    t0 = time.perf_counter()
                    eng.load(p)
                    rb = eng.last_readback or {}
                    routing = REF._input_routing(eng)
                    out(f"{name}: loaded in {time.perf_counter() - t0:.1f} s; readback ok {rb.get('ok')}; input {routing}")
                    if rb.get("ok") is not True or routing != 6:
                        raise RuntimeError(f"readback not ok or input not USB 5/6 for {name}: {json.dumps(rb)[:600]}")
                    t0 = time.perf_counter()
                    y = np.asarray(eng.render(x), np.float32)
                    sf.write(out_dir / f"t199_{name}_raw.wav", y, SR, subtype="FLOAT")
                    res["renders"][name] = {"render_s": round(time.perf_counter() - t0, 1), "offset": eng.last_offset,
                                            "rms_dbfs": round(float(20 * np.log10(np.sqrt(np.mean(y[:len(di)] ** 2)) + 1e-12)), 2),
                                            "peak_dbfs": round(float(20 * np.log10(np.max(np.abs(y)) + 1e-12)), 2)}
                    out(f"{name}: rendered {res['renders'][name]}")
            finally:
                if not eng._closed:
                    try:
                        eng.cal = dataclasses.replace(eng.cal, input_usb56=REF.INPUT_MAIN_LR)
                        eng.load(final_p)
                        fin = readback_diff(eng.chain, reamp_hlx_dict(final_p, REF.INPUT_MAIN_LR), None, check_input=True)
                        res.update(final_input_routing=REF._input_routing(eng), final_readback_ok=bool(fin.ok),
                                   final_mismatches=fin.mismatches,
                                   final_state="human_current" if final_p is not pc else "c")
                        out(f"{res['final_state']} left loaded; input {res['final_input_routing']} (Main L/R = 1); readback ok {fin.ok}")
                    except Exception as e:  # noqa: BLE001
                        res["final_error"] = f"{type(e).__name__}: {e}"
                        out(f"FINAL PUT-BACK FAILED: {res['final_error']}")
                    REF._leave_loaded(eng, res, out)
    except Exception as e:  # noqa: BLE001 - reported, never retried
        res["error"] = f"{type(e).__name__}: {e}"
        out(f"ERROR {res['error']} -- stopped, no retry")
    finally:
        res["lock_released"] = not pedal_lock.held_here()
        (TUNED / "t199_stomp_confirm_summary.json").write_text(json.dumps(res, indent=1, default=str))
    return 1 if "error" in res else 0


def stomp_names():
    n = ["c", "c2"]
    if (TUNED / "stomp" / "t199_human_current_raw.wav").exists():
        n.append("human_current")
    return n


def cmd_analyze(a) -> int:
    import soundfile as sf

    from tonematch.metrics import mrstft_v2 as M
    di, target, _ = T.data()
    n = len(di)
    names = stomp_names() + ["c_repeat"]
    st = {k: sf.read(TUNED / "stomp" / f"t199_{k}_raw.wav", dtype="float32")[0][:n] for k in names}
    tab = T.score_table({k: st[k] for k in names}, target, ref_name="c")
    res = {"stomp_repeat_floor": {"mrstft_v2_c_vs_repeat": round(M.term(st["c"], st["c_repeat"]), 4),
                                  "null_db_c_vs_repeat": round(T.null_db(st["c"], st["c_repeat"]), 1)},
           "scores_stomp": tab, "band_labels_hz": T.band_labels()}
    gi = {k: T.lufs(target) - T.lufs(st[k]) for k in names}
    res["band_db_at_equal_lufs_minus_target"] = {
        k: [round(float(x), 2) for x in T.band_db(st[k] * 10 ** (gi[k] / 20)) - T.band_db(target)] for k in names}
    res["dlufs_c2_vs_c"] = round(T.lufs(st["c2"]) - T.lufs(st["c"]), 2)
    # native vs stomp parity at c2 (cab High Cut is the new knob)
    nat = {}
    try:
        from tonematch.engines.native import ChainTemplate
        tpl = next(iter(ChainTemplate.load_all(T.TEMPLATES).values()))
        eng = T.make_engine()
        try:
            eng.load(T.tutorial_preset())
            sets = {"c": c_values(), "c2": c2_values()}
            ys = dict(zip(sets, T.render_batch(eng, di, [native_vals(v, tpl) for v in sets.values()]), strict=True))
        finally:
            eng.close()
        for k, y in ys.items():
            lag = T.best_lag(st[k], y)
            nat[k] = {"mrstft_v2_native_vs_stomp": round(M.term(st[k], np.roll(y, lag)), 4),
                      "band_db_native_minus_stomp": T.band_diff(y, st[k]),
                      "lufs_native_minus_stomp": round(T.lufs(y) - T.lufs(st[k]), 2)}
    except Exception as e:  # noqa: BLE001
        nat = {"error": str(e)}
    res["native_vs_stomp"] = nat
    (WORK / "t199_analysis.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float))
    return 0


PROTOCOL = (
    "Per trial: Target = the htdemucs_6s guitar stem of DS-Sample, song 0:30.1-0:50.0 (19.9 s, mono mid); A and B = two "
    "of the candidate settings of the Scream 808 > Brit 2204 + 4x12 Greenback 25 > Simple Delay > 10 Band Graphic > Hall "
    "chain, each rendered on the real HX Stomp (edit buffer, reamp) from the human's own take2 DI. Candidates: c (T198 "
    "arm c, the AFx-Rep tuned version the human preferred), c2 (c refined with the cab High Cut freed, AFx-Rep distance, "
    "CMA-ES, Native), plus the human's edited pedal state if it differed from c. Every pair of candidates is one trial; one "
    "trial is repeated with the sides swapped. Every clip (target included) is set to -23 LUFS (BS.1770 K-weighted "
    "integrated loudness) with one shared peak guard; not matched by RMS. The key (which candidate is A and B) is in "
    "cache/ratings and stays hidden until the answers are in. The human rates A and B 1 to 5 and says which is closer. "
    "No score is used to choose pairs or judge answers; nothing is admitted by this session. Written before any answers.")


def cmd_session(a) -> int:
    import secrets
    import shutil

    import rating_session as RS
    import rating_v3 as R3
    import soundfile as sf

    di, target, where = T.data()
    n = len(di)
    names = stomp_names()
    raw = {k: sf.read(TUNED / "stomp" / f"t199_{k}_raw.wav", dtype="float32")[0][:n] for k in names}
    seed = secrets.randbelow(2**31) if a.seed is None else a.seed
    rng = np.random.default_rng(seed)
    folder, keyf = RS.RATINGS_DIR / SESSION, RS.KEY_DIR / f"{SESSION}.key.json"
    if keyf.exists() and not a.rebuild:
        raise SystemExit(f"{keyf} exists (a session with answers may depend on it); pass --rebuild to replace")
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    RS.KEY_DIR.mkdir(parents=True, exist_ok=True)
    clips = {"T": RS.cut(target, 0, n), **{k: RS.cut(v, 0, n) for k, v in raw.items()}}
    ks = list(clips)
    lev, info = RS.match_set([clips[k] for k in ks])
    lev = dict(zip(ks, lev, strict=True))
    gains = dict(zip(ks, info["gain_db"], strict=True))
    pairs = [(x, y) for i, x in enumerate(names) for y in names[i + 1:]]
    plan = [{"kind": "regular", "t": i, "A": x, "B": y} for i, (x, y) in enumerate(pairs)]
    for p in plan:
        if rng.random() < 0.5:
            p["A"], p["B"] = p["B"], p["A"]
    j = int(rng.integers(len(plan)))
    o = plan[j]
    plan.append({"kind": "repeat", "t": o["t"], "A": o["B"], "B": o["A"], "orig": j})
    order = [int(v) for v in rng.permutation(len(plan) - 1)]
    at = order.index(j)
    order.insert(int(rng.integers(min(at + 1, len(order)), len(order) + 1)), len(plan) - 1)
    pos = {s: i for i, s in enumerate(order)}
    trials, ktrials, id_of = [], [], {}
    for rank, si in enumerate(sorted(range(len(plan)), key=lambda i: pos[i]), start=1):
        p = plan[si]
        tag = secrets.token_hex(4)
        files = {"T": f"{tag}_t.wav", "A": f"{tag}_a.wav", "B": f"{tag}_b.wav"}
        for r, k in (("T", "T"), ("A", p["A"]), ("B", p["B"])):
            RS.write_clip(folder / files[r], lev[k])
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": p["kind"], "family": "ds-sample-chain", "target": 1,
                        "A": {"name": p["A"], "cand_kind": p["A"], "role": "candidate", "gain_db": gains[p["A"]]},
                        "B": {"name": p["B"], "cand_kind": p["B"], "role": "candidate", "gain_db": gains[p["B"]]},
                        "files": files, "repeat_of": id_of[p["orig"]] if p["kind"] == "repeat" else None,
                        "loudness": {"gain_db": [gains["T"], gains[p["A"]], gains[p["B"]]]}})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": SESSION, "excerpt_s": n / SR, "trials": trials}, indent=1))
    note = ("The Target is the guitar of the song, separated from the recording. A and B are two settings of the same "
            "effects chain, played by you (your own take) through the real HX Stomp. Judge which of A and B is closer to the "
            "Target in tone (the amp's drive, the brightness and the body, the sustain). Rate both A and B 1 to 5 with the "
            "big buttons. Either may be close, or neither; use the whole 1 to 5 range. Every clip is set to the same "
            "loudness (LUFS), so loudness should not tell them apart. Trials are about 20 s per clip.")
    (folder / "index.html").write_text(R3.render_page(SESSION, trials, note))
    keyf.write_text(json.dumps({"session": SESSION, "version": 7, "seed": seed, "target_lufs": RS.TARGET_LUFS,
                                "peak_guard_db": info["gain_db"][0] - (RS.TARGET_LUFS - info["lufs_in"][0]),
                                "loudness_match": "LUFS (BS.1770 K-weighted), one shared peak guard; not RMS",
                                "lufs_in": dict(zip(ks, info["lufs_in"], strict=True)), "gain_db": gains,
                                "candidates": names, "protocol": PROTOCOL, "where": where, "trials": ktrials},
                               indent=1, default=float))
    print(f"page: {RS.URL_BASE}/{SESSION}/index.html ({len(trials)} trials, candidates {names})\nkey: {keyf}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("scan", "pick", "report", "stomp", "analyze"):
        sub.add_parser(c)
    ss = sub.add_parser("session")
    ss.add_argument("--seed", type=int, default=None)
    ss.add_argument("--rebuild", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("--budget", type=int, default=400)
    s.add_argument("--pop", type=int, default=20)
    s.add_argument("--sigma", type=float, default=0.08)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--max-load", type=float, default=None)
    a = ap.parse_args(argv)
    return {"scan": cmd_scan, "search": cmd_search, "pick": cmd_pick, "report": cmd_report, "stomp": cmd_stomp,
            "analyze": cmd_analyze, "session": cmd_session}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
