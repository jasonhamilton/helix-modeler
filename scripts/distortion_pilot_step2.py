#!/usr/bin/env python3
"""T173: step 2 of docs/notes/distortion-identification.md, the dfp-v1 measurement pilot on Native.

Approved by the human ("yes measure", DECISIONS 2026-10-18) with the five method choices of
docs/notes/distortion-id-steps01.md. Nothing is adopted: the pass/fail checks are placeholders,
reported as measured; step 3, the class list, scores and bars all need the human.

Renders (Native, single-block library templates, other knobs at the library default, Level fixed):
  6 drive models of different classes; 9 settings (Drive x Tone at CC 25/64/108 x 25/64/102, i.e.
  0.20/0.50/0.85 x 0.20/0.50/0.80) for two-knob models, 5 Drive settings (0, 0.2, 0.5, 0.85, 1) for
  the two models with one continuous knob; 2 repeat renders; 12 short-sweep renders (1.5 s sweeps,
  low and high Drive per model) for the ringing comparison.
Stores fingerprints only (no audio): cache/distortion-fp/dfp-v1-pilot/<model>/<setting>.npz.
Summary and figures (local, gitignored): reports/local/distortion-id/pilot/.

    nice -n 10 .venv/bin/python scripts/distortion_pilot_step2.py run --hosts 3
    .venv/bin/python scripts/distortion_pilot_step2.py summarise     # from the stored fingerprints
"""
from __future__ import annotations

import argparse
import json
import os
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

from tonematch.metrics import distortion_probe as P

FP_DIR = ROOT / "cache" / "distortion-fp" / "dfp-v1-pilot"
OUT = ROOT / "reports" / "local" / "distortion-id" / "pilot"

# model -> (drive knob, tone knob or None, short name, class expectation tag)
MODELS = {
    "HD2_DistScream808": ("Gain", "Tone", "Scream 808", "ts"),
    "HD2_DistVerminDist": ("Gain", "Filter", "Vermin Dist", "rat"),
    "HD2_DistTriangleFuzz": ("Sustain", "Tone", "Triangle Fuzz", "muff"),
    "HD2_DistArbitratorFuzz": ("Fuzz", None, "Arbitrator Fuzz", "fuzz"),
    "HD2_DistKinkyBoost": ("Drive", None, "Kinky Boost", "boost"),
    "HD2_DistDerangedMaster": ("Drive", "Treble", "Deranged Master", "treble"),
}
DRIVE_CC = (25, 64, 108)
TONE_CC = (25, 64, 102)
ONE_KNOB_CC = (0, 25, 64, 108, 127)
REPEATS = (("HD2_DistScream808", (64, 64)), ("HD2_DistTriangleFuzz", (108, 64)))
SHORT_CC = (25, 108)          # drive CCs (tone mid) rendered with the 1.5 s sweeps
L_SHORT = P.build_probe("short")[1]["sweep_L"]
RECHECK = OUT / "recheck.json"
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
          "#bcbd22", "#17becf"]


def setting_name(drive, tone, dcc, tcc=None):
    return f"{drive}@{dcc}" + ("" if tone is None else f",{tone}@{tcc}")


def settings_for(model):
    drive, tone, *_ = MODELS[model]
    if tone is None:
        return [(setting_name(drive, None, d), {drive: d}) for d in ONE_KNOB_CC]
    return [(setting_name(drive, tone, d, t), {drive: d, tone: t}) for d in DRIVE_CC for t in TONE_CC]


def short_settings_for(model):
    drive, tone, *_ = MODELS[model]
    return [(setting_name(drive, tone, d, None if tone is None else 64),
             {drive: d} if tone is None else {drive: d, tone: 64}) for d in SHORT_CC]


def plan():
    jobs = []
    for m in MODELS:
        for name, cc in settings_for(m):
            jobs.append({"model": m, "setting": name, "cc": cc, "kind": "main", "tag": ""})
        for name, cc in short_settings_for(m):
            jobs.append({"model": m, "setting": name, "cc": cc, "kind": "short", "tag": "__short"})
    for m, ccs in REPEATS:
        drive, tone, *_ = MODELS[m]
        name = setting_name(drive, tone, *ccs)
        cc = {drive: ccs[0]} if tone is None else {drive: ccs[0], tone: ccs[1]}
        jobs.append({"model": m, "setting": name, "cc": cc, "kind": "main", "tag": "__rep"})
    return jobs


# ------------------------------------------------------------------------------------- render

def cmd_run(a) -> dict:
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    catalog = load_catalog()
    probes = {k: P.build_probe(k) for k in ("main", "short")}
    jobs = plan()
    FP_DIR.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()
    pool = nat.NativeRenderPool(workers=a.hosts, templates_dir=lib.library_templates())
    print(json.dumps({"jobs": len(jobs), "hosts": len(pool.engines)}), flush=True)
    keep_raw = {}            # first render of each repeat setting, compared then dropped
    index = {"version": P.PROBE_VERSION, "native_hosts": len(pool.engines), "settings": []}
    render_s = 0.0
    try:
        # main + short per model, then the repeats in their own batch (other host order)
        groups = [[j for j in jobs if j["model"] == m and j["tag"] != "__rep"] for m in MODELS]
        groups.append([j for j in jobs if j["tag"] == "__rep"])
        for grp in groups:
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
                rjobs.append(nat.RenderJob(dry=probes[j["kind"]][0].astype(np.float32), preset=p))
            t0 = time.perf_counter()
            ys = pool.render_batch(rjobs, on_error="none")
            dt = time.perf_counter() - t0
            render_s += dt
            for j, y in zip(grp, ys, strict=True):
                rec = {k: j[k] for k in ("model", "setting", "cc", "kind", "tag", "params", "fixed")}
                if y is None:
                    rec["error"] = "render failed"
                    index["settings"].append(rec)
                    continue
                y = np.asarray(y, float)
                lay = probes[j["kind"]][1]
                ta = time.perf_counter()
                arr, sc = P.analyse(y, lay, full=j["kind"] == "main")
                if j["kind"] == "main":
                    for lv in (-12.0, -42.0):
                        sc[f"leak_m{int(-lv)}"] = P.leakage(y, lay, lv, L_SHORT)
                key = (j["model"], j["setting"])
                if j["kind"] == "main" and any(key == (m, setting_name(MODELS[m][0], MODELS[m][1], *c))
                                               for m, c in REPEATS):
                    if j["tag"] == "__rep" and key in keep_raw:
                        y0 = keep_raw.pop(key)
                        d = y - y0
                        sc["repeat_null"] = {
                            "max_abs_diff": float(np.max(np.abs(d))),
                            "null_db": float(P.D.db(np.sqrt(np.mean(d ** 2)) / (np.sqrt(np.mean(y0 ** 2)) + 1e-30))),
                            "bit_identical": bool(np.array_equal(y, y0))}
                    elif j["tag"] == "":
                        keep_raw[key] = y.copy()
                sc["analysis_s"] = round(time.perf_counter() - ta, 2)
                b = P.pack(arr, sc)
                f = FP_DIR / j["model"] / f"{j['setting'].replace(',', '_')}{j['tag']}.npz"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b)
                rec.update({"file": str(f.relative_to(ROOT)), "bytes": len(b), "analysis_s": sc["analysis_s"]})
                index["settings"].append(rec)
            print(json.dumps({"model": grp[0]["model"], "renders": len(grp), "render_s": round(dt, 1),
                              "elapsed_s": round(time.perf_counter() - t_all, 1),
                              "load1": round(os.getloadavg()[0], 2)}), flush=True)
    finally:
        pool.close()
    index["wall_s"] = round(time.perf_counter() - t_all, 1)
    index["render_s"] = round(render_s, 1)
    index["renders"] = sum(1 for s in index["settings"] if "error" not in s)
    index["failed"] = sum(1 for s in index["settings"] if "error" in s)
    (FP_DIR / "index.json").write_text(json.dumps(P._clean(index), indent=1))
    return index


# ------------------------------------------------------------------------------------- load

def load_fps():
    idx = json.loads((FP_DIR / "index.json").read_text())
    fps = {}
    for s in idx["settings"]:
        if "file" not in s:
            continue
        arr, sc = P.unpack((ROOT / s["file"]).read_bytes())
        fps[(s["model"], s["setting"], s["tag"])] = {"arr": arr, "sc": sc, "meta": s}
    return idx, fps


def _cc_of(s, knob):
    return s["meta"]["cc"].get(knob)


# ------------------------------------------------------------------------------------- checks

def _f(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else v


def derived(v) -> dict:
    """Quantities computed from the stored arrays (so older fingerprints get them too)."""
    arr, sc = v["arr"], v["sc"]
    f = P.FGRID
    out = {}
    for nm, kind in (("P", "hp"), ("Q", "lp")):
        fc, rms, n = P.fit_first_order(f, arr[f"{nm}_db"], kind)
        out[f"{nm}_{kind}1_fc_hz"], out[f"{nm}_{kind}1_rms_db"], out[f"{nm}_fit_n"] = fc, rms, n
    ok = np.isfinite(arr["P_db"])
    if ok.any():
        i = int(np.nonzero(ok)[0][0])
        out["f_low_hz"], out["P_low_db"], out["Q_low_db"] = float(f[i]), float(arr["P_db"][i]), float(arr["Q_db"][i])
    else:
        out["f_low_hz"] = out["P_low_db"] = out["Q_low_db"] = None
    L = np.asarray(P.STAIR_LEVELS)
    kr = P.knee_ref(L, arr["stair1k_db"][0], sc["g0_ss_1k_db"])
    out["knee_ref_sweep_dbfs"] = kr if kr is None or np.isfinite(kr) else "below -71"
    hrel = np.maximum(arr["stair1k_db"][1], arr["stair1k_db"][2]) - arr["stair1k_db"][0]
    on = np.nonzero(hrel > -60.0)[0]
    out["harmonic_onset_dbfs"] = float(L[on[0]]) if on.size else None
    out["harmonics_below_onset_db"] = (float(np.max(hrel[:on[0]])) if on.size and on[0] > 0 else None)
    i1k = int(np.argmin(np.abs(np.log(f / 1000.0))))
    out["knee_sweep_1k_dbfs"] = P.D.p1db_from_levels(P.SWEEP_LEVELS, arr["M1"][i1k])
    out["stair_first_step_compression_vs_sweep_db"] = float(sc["g0_ss_1k_db"] - (arr["stair1k_db"][0][0] - L[0]))
    return out


def checks(idx, fps) -> dict:
    out = {}
    main = {k: v for k, v in fps.items() if k[2] == ""}

    # 1. repeat agreement, against the smallest between-setting difference of the same model
    rep = []
    for m, ccs in REPEATS:
        drive, tone, *_ = MODELS[m]
        name = setting_name(drive, tone, *ccs)
        a, b = fps.get((m, name, "")), fps.get((m, name, "__rep"))
        if a is None or b is None:
            rep.append({"model": m, "setting": name, "missing": True})
            continue
        dM = P.table_diff(a["arr"]["M1"], b["arr"]["M1"])
        dS = float(np.nanmax(np.abs(a["arr"]["stair1k_db"] - b["arr"]["stair1k_db"])))
        sc_keys = ("g0_stair_1k_db", "knee_1k_dbfs", "even_share_db", "P_corner_hz", "Q_corner_hz")
        dsc = {k: (None if a["sc"].get(k) is None or b["sc"].get(k) is None
                   else abs(a["sc"][k] - b["sc"][k])) for k in sc_keys}
        # nearest other setting of the same model: smallest M1 difference (median, dB)
        near = min((P.table_diff(a["arr"]["M1"], v["arr"]["M1"])["median"], k[1])
                   for k, v in main.items() if k[0] == m and k[1] != name)
        rep.append({"model": m, "setting": name, "waveform": b["sc"].get("repeat_null"),
                    "M1_diff_db": dM, "stair1k_max_diff_db": dS, "scalar_abs_diff": dsc,
                    "nearest_setting_M1_median_diff_db": near[0], "nearest_setting": near[1]})
    ok = all(r.get("waveform", {}) and r["M1_diff_db"]["max"] <= 0.01 for r in rep if not r.get("missing"))
    out["repeat"] = {"rows": rep, "verdict": "met" if ok else "not met",
                     "rule": "repeat fingerprint (M1 table) within 0.01 dB everywhere and far below the "
                             "nearest other setting's difference"}

    # 2. boost stays clean up to -6 dBFS
    rows = []
    for (m, s, _), v in main.items():
        if MODELS[m][3] != "boost":
            continue
        sc = v["sc"]
        rows.append({"setting": s, "compression_at_minus6_db": sc["compression_at_minus6_db"],
                     "thd_1k_at_minus6_db": sc["thd_1k_at_minus6_db"], "knee_1k_dbfs": sc["knee_1k_dbfs"],
                     "g0_db": sc["g0_stair_1k_db"],
                     "clean": bool(sc["compression_at_minus6_db"] < 1.0 and sc["thd_1k_at_minus6_db"] < -40.0)})
    rows.sort(key=lambda r: r["setting"])
    out["boost_clean"] = {"rows": rows, "rule": "at -6 dBFS (1 kHz): gain within 1 dB of small-signal and "
                          "THD below -40 dB (1%)",
                          "verdict": f"{sum(r['clean'] for r in rows)} of {len(rows)} settings clean"}

    # 3. fuzz even share (asymmetry), compared with the symmetric classes
    def evens(tag):
        return [v["sc"]["even_share_db"] for (m, s, _), v in main.items() if MODELS[m][3] == tag]
    sym_ref = evens("ts") + evens("rat")
    med_sym = float(np.median(sym_ref)) if sym_ref else np.nan
    rows = []
    for (m, s, _), v in sorted(main.items()):
        if MODELS[m][3] != "fuzz":
            continue
        sc = v["sc"]
        rows.append({"setting": s, "even_share_db": sc["even_share_db"], "even_share_raw_top_db": sc["even_share_raw_top_db"],
                     "ceiling_ratio": sc["ceiling_ratio"], "curve_level_dbfs": sc["curve_level_dbfs"],
                     "large": bool(sc["even_share_db"] >= -20.0)})
    out["fuzz_even"] = {"rows": rows, "symmetric_classes_median_even_share_db": med_sym,
                        "rule": "even-harmonic share of the static curve (about 10 dB above the knee, post-filter "
                                "removed) at or above -20 dB (placeholder; the step-1 asymmetric diode read -22.8)",
                        "verdict": f"{sum(r['large'] for r in rows)} of {len(rows)} settings large"}

    # 4. g0 rises with Drive
    rows = []
    for m, (drive, tone, short, tag) in MODELS.items():
        tones = [None] if tone is None else list(TONE_CC)
        for t in tones:
            pts = sorted(((_cc_of(v, drive), v["sc"]["g0_stair_1k_db"], v["sc"]["g0_ss_1k_db"])
                          for (mm, s, _), v in main.items()
                          if mm == m and (t is None or _cc_of(v, tone) == t)))
            g = [p[1] for p in pts]
            rows.append({"model": short, "tone_cc": t, "drive_cc": [p[0] for p in pts], "g0_stair_db": g,
                         "g0_sweep_db": [p[2] for p in pts],
                         "rises": bool(all(b > a + 0.5 for a, b in zip(g, g[1:])))})
    out["g0_rises"] = {"rows": rows, "rule": "small-signal gain at 1 kHz (-71 dBFS tone) rises by more than "
                       "0.5 dB at every Drive step, per tone setting",
                       "verdict": f"{sum(r['rises'] for r in rows)} of {len(rows)} Drive series rise"}

    # 5. class expectations
    cls = {}
    for m, (drive, tone, short, tag) in MODELS.items():
        rows = []
        for (mm, s, _), v in sorted(main.items(), key=lambda kv: (kv[0][1])):
            if mm != m:
                continue
            sc = v["sc"]
            r = {"setting": s, "drive_cc": _cc_of(v, drive), "tone_cc": None if tone is None else _cc_of(v, tone),
                 "knee_1k_dbfs": sc["knee_1k_dbfs"], "knee_at_floor": sc["knee_at_floor"],
                 "split_identifiable_fraction": sc["split_identifiable_fraction"],
                 "odd_harmonic_residual_db": sc["odd_harmonic_residual_db"],
                 "fund_residual_db": sc["fund_residual_db"],
                 "P_corner_hz": _f(sc["P_corner_hz"]), "P_100_db": sc["P_100_db"], "Q_100_db": sc["Q_100_db"],
                 "Q_corner_hz": _f(sc["Q_corner_hz"]), "sat_corner_above_hz": _f(sc["sat_corner_above_hz"]),
                 "ss_corner_above_hz": _f(sc["ss_corner_above_hz"]),
                 "ss_hump": sc["ss_hump"], "ss_scoop": sc["ss_scoop"], "sat_scoop": sc["sat_scoop"],
                 "Q_scoop": sc["Q_scoop"], "even_share_db": sc["even_share_db"], "ceiling_ratio": sc["ceiling_ratio"],
                 "rapp_p": [sc["rapp_pos"]["p"], sc["rapp_neg"]["p"]], "ss_tilt_3k_200_db": sc["ss_tilt_3k_200_db"],
                 "memory_flag": sc["memory_flag"], "imd_440_554_at_minus12_db": sc["imd_440_554_at_minus12_db"],
                 "compression_at_minus6_db": sc["compression_at_minus6_db"], "thd_1k_at_minus6_db": sc["thd_1k_at_minus6_db"],
                 "noise_floor_dbfs_rms": sc["noise_floor_dbfs_rms"], "g0_db": sc["g0_stair_1k_db"],
                 "g0_sweep_db": sc["g0_ss_1k_db"], **derived(v)}
            if tag == "ts":
                fc, fl, pl, ql = r["P_hp1_fc_hz"], r["f_low_hz"], r["P_low_db"], r["Q_low_db"]
                r["pre_bass_cut_near_720"] = bool(np.isfinite(fc) and 480 <= fc <= 1080 and fl is not None
                                                  and fl <= 300 and pl <= -6 and ql >= -3)
                h = sc["ss_hump"]
                r["mid_hump"] = bool(400 <= h["f_hz"] <= 2500 and h["above_100_db"] >= 6 and h["above_10k_db"] >= 3)
            if tag == "muff":
                sat, ss = sc["sat_scoop"], sc["ss_scoop"]
                r["mid_scoop"] = bool((sat["depth_db"] or 0) >= 3 and 300 <= sat["f_hz"] <= 3000) or \
                    bool((ss["depth_db"] or 0) >= 3 and 300 <= ss["f_hz"] <= 3000)
            if tag == "fuzz":
                cr = sc["ceiling_ratio"]
                r["asymmetric"] = bool(sc["even_share_db"] >= -20 or (cr is not None and not 0.8 <= cr <= 1.25))
            if tag == "boost":
                r["clean_to_minus6"] = bool(sc["compression_at_minus6_db"] < 1.0 and sc["thd_1k_at_minus6_db"] < -40.0)
            if tag == "treble":
                r["treble_tilt"] = bool(sc["ss_tilt_3k_200_db"] >= 6.0)
            rows.append(r)
        res = {"model": short, "tag": tag, "rows": rows}
        if tag == "rat":
            moves = []
            for d in DRIVE_CC:
                ser = sorted((x["tone_cc"], x["Q_lp1_fc_hz"], x["P_hp1_fc_hz"], x["sat_corner_above_hz"])
                              for x in rows if x["drive_cc"] == d)
                q = [s[1] for s in ser]
                sat = [s[3] for s in ser]
                pc = [s[2] for s in ser]
                fin = [v for v in q if v is not None]
                mono = len(fin) == 3 and (all(b < a for a, b in zip(fin, fin[1:])) or all(b > a for a, b in zip(fin, fin[1:])))
                ratio = (max(fin) / min(fin)) if len(fin) >= 2 else None
                finp = [v for v in pc if v is not None]
                pratio = (max(finp) / min(finp)) if len(finp) >= 2 else None
                fins = [v for v in sat if v is not None]
                sratio = (max(fins) / min(fins)) if len(fins) >= 2 else None
                moves.append({"drive_cc": d, "filter_cc": [s[0] for s in ser], "Q_lp1_fc_hz": q, "P_hp1_fc_hz": pc,
                              "sat_corner_hz": sat, "Q_corner_ratio": ratio, "P_corner_ratio": pratio,
                              "sat_corner_ratio": sratio,
                              "post_lpf_moves": bool(mono and ratio is not None and ratio >= 2
                                                     and (pratio is None or pratio < ratio))})
            res["filter_series"] = moves
        if tone is not None:
            eff = []
            for d in DRIVE_CC:
                ss = [v["arr"]["ss_db"] for (mm, s_, _), v in main.items() if mm == m and _cc_of(v, drive) == d]
                sat = [v["arr"]["sat_db"] for (mm, s_, _), v in main.items() if mm == m and _cc_of(v, drive) == d]
                if len(ss) >= 2:
                    ss, sat = np.array(ss), np.array(sat)
                    eff.append({"drive_cc": d, "small_signal_max_spread_db": float(np.nanmax(np.nanmax(ss, 0) - np.nanmin(ss, 0))),
                                "saturated_max_spread_db": float(np.nanmax(np.nanmax(sat, 0) - np.nanmin(sat, 0)))})
            res["tone_knob_effect"] = eff
        cls[m] = res
    out["class"] = cls

    # 6. sweep length: the 1.5 s vs 6 s comparison and the measured leakage (recheck renders)
    rc = json.loads(RECHECK.read_text()) if RECHECK.is_file() else {}
    sw = []
    for (m, s, tag), v in sorted(fps.items()):
        if tag != "__short":
            continue
        main_fp = fps.get((m, s, ""))
        if main_fp is None:
            continue
        diffs = short_vs_main(main_fp["arr"], v["arr"])
        lk = (rc.get("rows", {}).get(f"{m}|{s}") or {}).get("leak_m12")
        leak_ok = None
        if lk is not None:
            vals = [x for k, x in lk["contamination_p95_db"].items() if int(k) <= 4 and x is not None]
            leak_ok = bool(vals and max(vals) <= -30.0)
        p95 = max((d["p95"] for d in diffs.values() if d["n"] > 0), default=np.nan)
        sw.append({"model": MODELS[m][2], "setting": s, "diff_db": diffs, "worst_p95_db": p95,
                   "worst_order": max((k for k in diffs if diffs[k]["n"] > 0), key=lambda k: diffs[k]["p95"]),
                   "leak_m12": lk, "leak_ok": leak_ok,
                   "short_ok": bool(p95 <= 0.5 and leak_ok is not False)})
    out["sweep_length"] = {"rows": sw,
                           "gaps_short_ms": {k: P.gap_ms(L_SHORT, k) for k in range(1, 9)},
                           "gaps_main_ms": {k: P.gap_ms(P.D.ess_sync(P.F1, P.F2, 6.0, P.SR)[1], k) for k in range(1, 9)},
                           "identity_floor": rc.get("identity_floor"),
                           "rule": "1.5 s is enough if, on every subset setting, (a) the 1.5 s and 6 s sweep tables agree: "
                                   "95th percentile of |difference| <= 0.5 dB on orders 1-5, counting only bins where the "
                                   "order is within 40 dB of the fundamental's peak at that level and of its own peak; and "
                                   "(b) the leakage a 1.5 s sweep would add, measured on the 6 s record of the -12 dBFS sweep, "
                                   "is at or below -30 dB (95th percentile over bins, orders 1-4; -30 dB changes a level by "
                                   "at most 0.27 dB). Otherwise 6 s.",
                           "decision": ("1.5 s" if sw and all(r["short_ok"] for r in sw) else "6 s"),
                           "n_ok": sum(r["short_ok"] for r in sw), "n": len(sw)}
    if rc:
        out["recheck"] = {k: rc[k] for k in ("renders", "bit_identical_to_stored", "noise") if k in rc}

    # 7. bookkeeping
    sizes = [s["bytes"] for s in idx["settings"] if "bytes" in s and s["kind"] == "main"]
    out["storage"] = {"fingerprints": len(sizes), "bytes_mean": float(np.mean(sizes)) if sizes else None,
                      "bytes_total_all": int(sum(s.get("bytes", 0) for s in idx["settings"]))}
    out["noise_floor_dbfs_rms"] = {MODELS[k[0]][2]: None for k in main}
    for (m, s, _), v in main.items():
        cur = out["noise_floor_dbfs_rms"][MODELS[m][2]]
        nf = v["sc"]["noise_floor_dbfs_rms"]
        out["noise_floor_dbfs_rms"][MODELS[m][2]] = nf if cur is None else max(cur, nf)
    out["non_wh_flag"] = {MODELS[m][2]: sorted(((s, v["sc"]["odd_harmonic_residual_db"]) for (mm, s, _), v in main.items()
                                                if mm == m), key=lambda t: t[0]) for m in MODELS}
    return out


def short_vs_main(a_main: dict, a_short: dict, rel_db: float = 40.0) -> dict:
    """|1.5 s - 6 s| per order (dB): M1 bins within `rel_db` of the fundamental's peak; Mh (orders
    2-5 at the HARM_LEVELS) bins within `rel_db` of their own peak AND of the fundamental's peak
    at the same level (an order with no energy is the deconvolution floor, not a measurement)."""
    out = {1: P.table_diff(a_main["M1"], a_short["M1"], rel_db)}
    lev = list(P.SWEEP_LEVELS)
    for i, k in enumerate((2, 3, 4, 5)):
        A, B = a_main["Mh"][i], a_short["Mh"][i]
        top = np.array([np.nanmax(a_main["M1"][:, lev.index(v)]) for v in P.HARM_LEVELS])
        m = np.isfinite(A) & np.isfinite(B) & (A >= np.nanmax(A) - rel_db) & (A >= top[None, :] - rel_db)
        d = np.abs(A - B)[m]
        out[k] = ({"n": 0, "median": np.nan, "p95": np.nan, "max": np.nan} if d.size == 0 else
                  {"n": int(d.size), "median": float(np.median(d)), "p95": float(np.percentile(d, 95)),
                   "max": float(d.max())})
    return out


def cmd_recheck(a) -> dict:
    """Re-render the 12 ringing-subset settings (main probe, Drive low/high, tone mid): the leakage
    measure, the output noise, and a cross-session repeat of the stored fingerprints."""
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib

    catalog = load_catalog()
    sig, lay = P.build_probe("main")
    jobs = [j for j in plan() if j["kind"] == "short"]
    rjobs = []
    for j in jobs:
        tpl = lib.template_for(j["model"])
        p = lib.block_preset(j["model"], catalog)
        p.slots[0].params.update({k: nat.cc_value(*map(float, tpl.ranges.get(f"0:{k}", (0.0, 1.0))), int(c))
                                  for k, c in j["cc"].items()})
        rjobs.append(nat.RenderJob(dry=sig.astype(np.float32), preset=p))
    t0 = time.perf_counter()
    pool = nat.NativeRenderPool(workers=a.hosts, templates_dir=lib.library_templates())
    try:
        ys = pool.render_batch(rjobs, on_error="none")
    finally:
        pool.close()
    rows, same, noise = {}, [], {}
    floor = P.leakage(sig, lay, -12.0, L_SHORT)
    for j, y in zip(jobs, ys, strict=True):
        key = f"{j['model']}|{j['setting']}"
        if y is None:
            rows[key] = {"error": "render failed"}
            continue
        y = np.asarray(y, float)
        arr, sc = P.analyse(y, lay)
        stored = FP_DIR / j["model"] / f"{j['setting'].replace(',', '_')}.npz"
        a0, _ = P.unpack(stored.read_bytes())
        ident = all(np.array_equal(np.nan_to_num(P.np.asarray(arr[k], float).astype(np.float16), nan=9e3),
                                   np.nan_to_num(a0[k].astype(np.float16), nan=9e3)) for k in a0)
        same.append(ident)
        lead = next(s_ for s_ in lay["segments"] if s_["name"] == "lead_in")
        seg = y[lead["start"] + int(0.1 * P.SR):lead["start"] + lead["n"]]
        sw0 = [s_ for s_ in lay["segments"] if s_["name"] == "sweep"][0]
        gap = y[sw0["start"] + sw0["sweep_n"] + int(0.5 * P.SR):sw0["start"] + sw0["n"]]
        tail = next(s_ for s_ in lay["segments"] if s_["name"] == "tail")
        tl = y[tail["start"] + int(0.2 * P.SR):tail["start"] + tail["n"]]
        spec = np.abs(np.fft.rfft(seg - seg.mean())) ** 2
        fr = np.fft.rfftfreq(seg.size, 1 / P.SR)
        noise[key] = {"lead_dc": float(seg.mean()),
                      "lead_ac_rms_dbfs": float(P.D.db(np.std(seg))),
                      "after_first_sweep_rms_dbfs": float(P.D.db(np.sqrt(np.mean(gap ** 2)))),
                      "tail_rms_dbfs": float(P.D.db(np.sqrt(np.mean(tl ** 2)))),
                      "lead_power_above_1k_share": float(spec[fr > 1000].sum() / (spec.sum() + 1e-30)),
                      "lead_power_below_100_share": float(spec[fr < 100].sum() / (spec.sum() + 1e-30))}
        rows[key] = {"leak_m12": P.leakage(y, lay, -12.0, L_SHORT), "leak_m42": P.leakage(y, lay, -42.0, L_SHORT)}
    res = {"renders": sum(y is not None for y in ys), "wall_s": round(time.perf_counter() - t0, 1),
           "bit_identical_to_stored": {"n": len(same), "identical": int(sum(same))},
           "identity_floor": floor, "rows": rows, "noise": noise}
    OUT.mkdir(parents=True, exist_ok=True)
    RECHECK.write_text(json.dumps(P._clean(res), indent=1))
    return res


# ------------------------------------------------------------------------------------- figures

def figures(fps, out: Path):
    import distortion_id_steps01 as S01
    f = P.FGRID
    L = np.asarray(P.STAIR_LEVELS)
    for m, (drive, tone, short, tag) in MODELS.items():
        items = sorted(((k[1], v) for k, v in fps.items() if k[0] == m and k[2] == ""),
                       key=lambda kv: (_cc_of(kv[1], drive), _cc_of(kv[1], tone) if tone else 0))
        if not items:
            continue
        # static curves
        ser = []
        for i, (s, v) in enumerate(items):
            y = v["arr"]["curve_y"]
            x = np.linspace(-1, 1, y.size)
            ser.append((x, y / (np.nanmax(np.abs(y)) + 1e-30), f"{s} ({v['sc']['curve_level_dbfs']:.0f} dBFS)",
                        COLORS[i % len(COLORS)], tone is not None and _cc_of(v, tone) != 64))
        S01.svg_lines(out / f"{m}_curve.svg", ser, f"{short}: static curve, 1 kHz tone ~10 dB above the knee "
                      "(post-filter removed)", "input / peak", "output / max", w=760)
        # harmonics vs level (tone mid)
        ser = []
        mids = [(s, v) for s, v in items if tone is None or _cc_of(v, tone) == 64]
        for i, (s, v) in enumerate(mids):
            st = v["arr"]["stair1k_db"]
            g = st[0] - L
            ser.append((L, g - g[0], f"{s} H1 gain re small-signal", COLORS[i % len(COLORS)], False))
            ser.append((L, st[1] - st[0], f"{s} H2 re H1", COLORS[i % len(COLORS)], True))
            ser.append((L, st[2] - st[0], f"{s} H3 re H1", COLORS[i % len(COLORS)], True))
        S01.svg_lines(out / f"{m}_harmonics.svg", ser, f"{short}: 1 kHz staircase, gain change and harmonics vs level",
                      "input level (dBFS peak)", "dB", ylim=(-80, 5), w=900)
        # pre/post EQ
        ser = []
        sel = [(s, v) for s, v in items if (tone is None) or _cc_of(v, drive) == 64]
        for i, (s, v) in enumerate(sel):
            c = COLORS[i % len(COLORS)]
            ss = v["arr"]["ss_db"]
            ser.append((f, ss - np.interp(0.0, np.log(f / 1000.0), ss), f"{s} small-signal", c, False))
            ser.append((f, v["arr"]["P_db"], f"{s} pre (P)", c, True))
            ser.append((f, v["arr"]["Q_db"], f"{s} post (Q)", c, True))
        S01.svg_lines(out / f"{m}_eq.svg", ser, f"{short}: pre/post EQ split and small-signal response "
                      "(0 dB at 1 kHz)", "frequency (Hz)", "dB", logx=True, ylim=(-45, 25), w=900)


def cmd_summarise(a) -> dict:
    idx, fps = load_fps()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = checks(idx, fps)
    res["index"] = {k: idx.get(k) for k in ("version", "native_hosts", "wall_s", "render_s", "renders", "failed")}
    figures(fps, out)
    (out / "summary.json").write_text(json.dumps(P._clean(res), indent=1))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["plan", "run", "recheck", "summarise"])
    ap.add_argument("--hosts", type=int, default=3)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        jobs = plan()
        print(json.dumps({"renders": len(jobs), "main": sum(j["kind"] == "main" and not j["tag"] for j in jobs),
                          "short": sum(j["kind"] == "short" for j in jobs),
                          "repeats": sum(j["tag"] == "__rep" for j in jobs),
                          "audio_s": round(sum(P.build_probe(j["kind"])[0].size for j in jobs) / P.SR, 1)}))
        return 0
    if a.cmd == "recheck":
        r = cmd_recheck(a)
        print(json.dumps({k: r[k] for k in ("renders", "wall_s", "bit_identical_to_stored")}))
        a.cmd = "summarise"
    if a.cmd == "run":
        r = cmd_run(a)
        print(json.dumps({k: r[k] for k in ("wall_s", "render_s", "renders", "failed")}))
        a.cmd = "summarise"
    r = cmd_summarise(a)
    print(json.dumps({"repeat": r["repeat"]["verdict"], "boost": r["boost_clean"]["verdict"],
                      "fuzz": r["fuzz_even"]["verdict"], "g0": r["g0_rises"]["verdict"],
                      "sweep": r["sweep_length"]["decision"], "storage": r["storage"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
