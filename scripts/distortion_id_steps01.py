#!/usr/bin/env python3
"""T171: steps 0 and 1 of docs/notes/distortion-identification.md section 8. Zero renders.

Step 0: mine the stored coarse-v1 drive fingerprints (cache/probe/fingerprints/block-library-v1.2)
        for model separability: leave-one-setting-out identification, clustering versus the
        circuit groups of docs/notes/distortion-circuits.md, and where the T164 top matches sit.
Step 1: validate the distortion_id estimators on synthetic systems with known truth.

Outputs (local, gitignored): reports/local/distortion-id/results_step{0,1}.json and SVG figures.
No song audio is read. Nothing is adopted: the placeholder checks are reported as measured.

    nice -n 10 .venv/bin/python scripts/distortion_id_steps01.py --step all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.metrics import drive_fp_mining as F

FP_DIR = ROOT / "cache/probe/fingerprints/block-library-v1.2"
OUT = ROOT / "reports/local/distortion-id"
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
          "#bcbd22", "#17becf"]


# ------------------------------------------------------------------------------- tiny SVG

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_lines(path, series, title, xlabel, ylabel, logx=False, ylim=None, w=640, h=380):
    """series: list of (x, y, label, color, dashed)."""
    ml, mr, mt, mb = 60, 160, 30, 45
    xs = np.concatenate([np.asarray(s[0], float) for s in series])
    ys = np.concatenate([np.asarray(s[1], float) for s in series])
    ok = np.isfinite(xs) & np.isfinite(ys)
    tx = np.log10 if logx else (lambda v: np.asarray(v, float))
    x0, x1 = tx(xs[ok].min()), tx(xs[ok].max())
    y0, y1 = ylim if ylim else (ys[ok].min(), ys[ok].max())
    if y1 - y0 < 1e-9:
        y0, y1 = y0 - 1, y1 + 1
    pad = 0.05 * (y1 - y0)
    y0, y1 = y0 - pad, y1 + pad

    def px(v):
        return ml + (tx(v) - x0) / (x1 - x0 + 1e-12) * (w - ml - mr)

    def py(v):
        return mt + (y1 - np.clip(v, y0, y1)) / (y1 - y0) * (h - mt - mb)
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'font-family="sans-serif" font-size="11">'),
           f'<rect width="{w}" height="{h}" fill="white"/>',
           f'<text x="{w / 2}" y="18" text-anchor="middle" font-size="13">{_esc(title)}</text>',
           (f'<rect x="{ml}" y="{mt}" width="{w - ml - mr}" height="{h - mt - mb}" fill="none" '
            f'stroke="#444"/>')]
    for q in np.linspace(y0, y1, 6):
        out.append(f'<text x="{ml - 5}" y="{py(q) + 4:.1f}" text-anchor="end">{q:.3g}</text>')
        out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{py(q):.1f}" y2="{py(q):.1f}" '
                   f'stroke="#ddd"/>')
    ticks = [10 ** e for e in range(int(np.floor(x0)), int(np.ceil(x1)) + 1)] if logx \
        else np.linspace(xs[ok].min(), xs[ok].max(), 6)
    for q in ticks:
        if tx(q) < x0 - 1e-9 or tx(q) > x1 + 1e-9:
            continue
        out.append(f'<text x="{px(q):.1f}" y="{h - mb + 15}" text-anchor="middle">{q:.4g}</text>')
    out.append(f'<text x="{(ml + w - mr) / 2}" y="{h - 8}" text-anchor="middle">'
               f'{_esc(xlabel)}</text>')
    out.append(f'<text x="14" y="{(mt + h - mb) / 2}" text-anchor="middle" '
               f'transform="rotate(-90 14 {(mt + h - mb) / 2})">{_esc(ylabel)}</text>')
    for i, (x, y, label, color, dashed) in enumerate(series):
        x, y = np.asarray(x, float), np.asarray(y, float)
        m = np.isfinite(x) & np.isfinite(y)
        pts = " ".join(f"{px(a):.1f},{py(b):.1f}" for a, b in zip(x[m], y[m]))
        da = ' stroke-dasharray="5,3"' if dashed else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"{da}/>')
        ly = mt + 14 + 16 * i
        out.append(f'<line x1="{w - mr + 8}" x2="{w - mr + 28}" y1="{ly - 4}" y2="{ly - 4}" '
                   f'stroke="{color}" stroke-width="2"{da}/>')
        out.append(f'<text x="{w - mr + 32}" y="{ly}">{_esc(label)}</text>')
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def svg_heatmap(path, Mx, labels, title, groups=None, cell=9):
    n = len(labels)
    ml, mt = 150, 40
    w, h = ml + n * cell + 20, mt + n * cell + 20
    lo, hi = np.nanpercentile(Mx, 2), np.nanpercentile(Mx, 98)
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'font-family="sans-serif" font-size="8">'), f'<rect width="{w}" height="{h}" fill="white"/>',
           f'<text x="{w / 2}" y="16" text-anchor="middle" font-size="12">{_esc(title)}</text>']
    for i in range(n):
        for j in range(n):
            v = (np.clip(Mx[i, j], lo, hi) - lo) / (hi - lo + 1e-12)
            g = int(255 * v)
            out.append(f'<rect x="{ml + j * cell}" y="{mt + i * cell}" width="{cell}" '
                       f'height="{cell}" fill="rgb({g},{g},255)"/>')
        out.append(f'<text x="{ml - 3}" y="{mt + i * cell + cell - 1}" text-anchor="end">'
                   f'{_esc(labels[i])}</text>')
    if groups is not None:
        prev = None
        for i, g in enumerate(groups):
            if g != prev and i:
                out.append(f'<line x1="{ml}" x2="{ml + n * cell}" y1="{mt + i * cell}" '
                           f'y2="{mt + i * cell}" stroke="black" stroke-width="0.6"/>')
                out.append(f'<line y1="{mt}" y2="{mt + n * cell}" x1="{ml + i * cell}" '
                           f'x2="{ml + i * cell}" stroke="black" stroke-width="0.6"/>')
            prev = g
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def svg_scatter(path, xy, labels, cats, title, w=760, h=560):
    ml, mt, mr, mb = 40, 30, 20, 30
    x, y = xy[:, 0], xy[:, 1]
    cs = sorted(set(cats))

    def px(v):
        return ml + (v - x.min()) / (np.ptp(x) + 1e-12) * (w - ml - mr)

    def py(v):
        return mt + (y.max() - v) / (np.ptp(y) + 1e-12) * (h - mt - mb)
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'font-family="sans-serif" font-size="9">'), f'<rect width="{w}" height="{h}" fill="white"/>',
           f'<text x="{w / 2}" y="16" text-anchor="middle" font-size="12">{_esc(title)}</text>']
    for i in range(len(labels)):
        c = COLORS[cs.index(cats[i]) % len(COLORS)]
        out.append(f'<circle cx="{px(x[i]):.1f}" cy="{py(y[i]):.1f}" r="3" fill="{c}"/>')
        out.append(f'<text x="{px(x[i]) + 4:.1f}" y="{py(y[i]) + 3:.1f}" fill="{c}">'
                   f'{_esc(labels[i])}</text>')
    for i, c in enumerate(cs):
        out.append(f'<text x="{w - 110}" y="{40 + 12 * i}" fill="{COLORS[i % len(COLORS)]}">'
                   f'{_esc(c)}</text>')
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def short(m):
    return m.replace("HD2_Dist", "").replace("HD2_DM4", "DM4 ")


# ------------------------------------------------------------------------------- step 0

def step0(out: Path) -> dict:
    recs = F.load(FP_DIR)
    models_all = sorted({r["model"] for r in recs})
    n_models = len(models_all)
    res = {"n_settings": len(recs), "n_models": n_models,
           "chance_top1": 1 / n_models, "chance_top5": 5 / n_models,
           "excluded": sorted(F.EXCLUDED)}

    # how much of the knee range the stored probe sees
    p1 = []
    for r in recs:
        for h in r["fp"]["harmonics"]["per_freq"]:
            if h["freq_hz"] == 440.0:
                p1.append(h["static"]["p1db_in_dbfs"])
    p1v = np.array([np.nan if v is None else v for v in p1])
    res["probe_range"] = {
        "p1db_440_none": int(np.sum(~np.isfinite(p1v))),
        "p1db_440_within_1p5db_of_lowest_level": int(np.sum(p1v <= -43.5)),
        "knee_dbfs_none": int(sum(r["summary"].get("knee_dbfs") is None for r in recs)),
        "n": len(recs)}
    dup = 0
    Zs, _ = F.matrix(recs, "scalars")
    Ds = F.pairwise(Zs)
    for i, r in enumerate(recs):
        if r["setting"] == "default":
            js = [j for j, q in enumerate(recs) if q["model"] == r["model"] and j != i]
            dup += int(min(Ds[i, j] for j in js) < 1e-9)
    res["default_identical_to_another_setting"] = dup

    # identification
    ident = {}
    for kind in ("note6", "scalars", "curves"):
        for nl in (False, True):
            Z, names = F.matrix(recs, kind, no_level=nl)
            Dm = F.pairwise(Z)
            for sd in (True, False):
                ranks, q, models = F.loso_identification(recs, Dm, skip_default=sd)
                t1 = F.bootstrap_rate(ranks, q, 1)
                t5 = F.bootstrap_rate(ranks, q, 5)
                key = f"{kind}{'_nolevel' if nl else ''}{'' if sd else '_withdefault'}"
                ident[key] = {"n_features": len(names), "n_queries": len(ranks),
                              "top1": t1, "top5": t5, "median_rank": float(np.median(ranks)),
                              "mean_rank": float(np.mean(ranks))}
    res["identification"] = ident

    # nearest-model analysis and group-level hit on the main feature set (scalars, no default)
    Z, names = F.matrix(recs, "scalars")
    Dm = F.pairwise(Z)
    models = models_all
    idx = {m: [i for i, r in enumerate(recs) if r["model"] == m and r["setting"] != "default"]
           for m in models}
    grp = {m: F.LABELS[m][0] for m in models}
    from collections import Counter
    gsize = Counter(grp.values())
    csize = Counter(F.LABELS[m][2] for m in models)
    group_hits, fam_hits, cls_hits, conf = [], [], [], {}
    wrong_same, wrong_chance, wrong_same_cls, wrong_chance_cls = [], [], [], []
    for m in models:
        for i in idx[m]:
            best = None
            for m2 in models:
                cand = [j for j in idx[m2] if j != i]
                if not cand:
                    continue
                d = min(Dm[i, j] for j in cand)
                if best is None or d < best[0]:
                    best = (d, m2)
            m2 = best[1]
            if m2 != m:
                wrong_same.append(grp[m2] == grp[m])
                wrong_same_cls.append(F.LABELS[m2][2] == F.LABELS[m][2])
                wrong_chance.append((gsize[grp[m]] - 1) / (len(models) - 1))
                wrong_chance_cls.append((csize[F.LABELS[m][2]] - 1) / (len(models) - 1))
            group_hits.append(grp[m2] == grp[m])
            fam_hits.append(F.LABELS[m2][1] == F.LABELS[m][1])
            cls_hits.append(F.LABELS[m2][2] == F.LABELS[m][2])
            if m2 != m:
                conf[(m, m2)] = conf.get((m, m2), 0) + 1
    res["nearest_in_same"] = {"group": float(np.mean(group_hits)),
                              "family": float(np.mean(fam_hits)),
                              "class6": float(np.mean(cls_hits)),
                              "wrong_model_n": len(wrong_same),
                              "wrong_model_same_group": float(np.mean(wrong_same)),
                              "wrong_model_same_group_chance": float(np.mean(wrong_chance)),
                              "wrong_model_same_class6": float(np.mean(wrong_same_cls)),
                              "wrong_model_same_class6_chance": float(np.mean(wrong_chance_cls))}
    # chance of nearest-wrong-model landing in the same group if the wrong model were random
    res["top_confusions"] = sorted(([short(a), short(b), n] for (a, b), n in conf.items()),
                                   key=lambda t: -t[2])[:15]

    # model-level distances (mean of setting z-vectors), clustering vs labels
    mods, MM = F.model_means([r for r in recs if r["setting"] != "default"],
                             Z[[i for i, r in enumerate(recs) if r["setting"] != "default"]])
    labels = {"group17": [F.LABELS[m][0] for m in mods],
              "family": [F.LABELS[m][1] for m in mods],
              "class6": [F.LABELS[m][2] for m in mods]}
    clus = {}
    for method in ("ward", "average"):
        for k in (5, 6, 10, 17, 23):
            cl, _ = F.cluster(MM, k, method)
            row = {}
            for ln, lab in labels.items():
                a = F.ari(lab, cl)
                p95, mean = F.ari_null(lab, k, len(mods), sizes=cl)
                row[ln] = {"ari": a, "null_p95": p95, "null_mean": mean}
            clus[f"{method}_k{k}"] = row
    # same on the centre setting only (every model has one '@64' setting): a model mean mixes
    # its Drive corners, the centre is one sound
    ci = [next(i for i, r in enumerate(recs) if r["model"] == m and "@64" in r["setting"])
          for m in mods]
    MC = Z[ci]
    for method in ("ward", "average"):
        for k in (6, 17):
            cl, _ = F.cluster(MC, k, method)
            row = {}
            for ln, lab in labels.items():
                p95, mean = F.ari_null(lab, k, len(mods), sizes=cl)
                row[ln] = {"ari": F.ari(lab, cl), "null_p95": p95, "null_mean": mean}
            clus[f"centre_{method}_k{k}"] = row
    res["clustering"] = clus
    cl17, _ = F.cluster(MM, 17, "ward")
    cl6, _ = F.cluster(MM, 6, "ward")
    table = []
    DMm = F.pairwise(MM)
    for i, m in enumerate(mods):
        d = DMm[i].copy()
        d[i] = np.inf
        j = int(np.argmin(d))
        table.append({"model": short(m), "group": F.LABELS[m][0], "family": F.LABELS[m][1],
                      "class6": F.LABELS[m][2], "cluster17": int(cl17[i]), "cluster6": int(cl6[i]),
                      "nearest": short(mods[j]), "nearest_group": F.LABELS[mods[j]][0],
                      "nearest_dist": float(d[j])})
    res["model_table"] = table

    # T164 top matches
    iu = np.triu_indices(len(mods), 1)
    alld = DMm[iu]
    t164 = []
    for m in F.T164_TOP:
        i = mods.index(m)
        t164.append({"model": short(m), "group": F.LABELS[m][0], "family": F.LABELS[m][1],
                     "class6": F.LABELS[m][2], "cluster17": int(cl17[i]),
                     "cluster6": int(cl6[i])})
    pair = []
    for a in range(len(F.T164_TOP)):
        for b in range(a + 1, len(F.T164_TOP)):
            ia, ib = mods.index(F.T164_TOP[a]), mods.index(F.T164_TOP[b])
            d = DMm[ia, ib]
            pair.append([short(F.T164_TOP[a]), short(F.T164_TOP[b]), float(d),
                         float(np.mean(alld < d))])
    res["t164"] = {"models": t164, "pairs_dist_and_percentile": pair,
                   "distinct_clusters17": len({t["cluster17"] for t in t164}),
                   "distinct_clusters6": len({t["cluster6"] for t in t164})}

    # figures
    order = sorted(range(len(mods)), key=lambda i: (F.LABELS[mods[i]][0], mods[i]))
    svg_heatmap(out / "step0_model_distance.svg", DMm[np.ix_(order, order)],
                [f"{F.LABELS[mods[i]][0][:1]} {short(mods[i])}" for i in order],
                "Step 0: distance between drive models (stored scalars, darker = closer), "
                "ordered by confusable group", groups=[F.LABELS[mods[i]][0][:1] for i in order])
    U, S, _ = np.linalg.svd(MM - MM.mean(axis=0), full_matrices=False)
    xy = U[:, :2] * S[:2]
    res["pca_var_explained_2"] = float((S[:2] ** 2).sum() / (S ** 2).sum())
    svg_scatter(out / "step0_pca_class6.svg", xy, [short(m) for m in mods], labels["class6"],
                "Step 0: drive models on the first two principal components (stored scalars), "
                "colour = provisional circuit class")
    ranks, q, _ = F.loso_identification(recs, Dm, skip_default=True)
    hist = np.bincount(np.minimum(ranks, 30), minlength=31)[1:]
    svg_lines(out / "step0_loso_rank_hist.svg",
              [(np.arange(1, 31), hist, "stored scalars", COLORS[0], False)],
              "Step 0: rank of the true model, leave-one-setting-out (30 = 30 or worse)",
              "rank of the true model among 60", "number of held-out settings")
    return res


# ------------------------------------------------------------------------------- step 1

def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def step1(out: Path) -> dict:
    from tonematch.metrics import distortion_id as D
    from tonematch.metrics import distortion_id_eval as E
    from tonematch.metrics import distortion_synth as S
    t0 = time.time()
    systems = S.build_systems()
    allres = [E.evaluate(s) for s in systems]
    summary = {"wall_s": None, "systems": []}
    for s, r in zip(systems, allres):
        row = {"name": r["name"], "description": r["description"], "is_wh": r["is_wh"],
               "notes": r["notes"], "knee": r["knee"], "hysteresis_db": r["hysteresis_db"],
               "settle": r["settle"]}
        # timing rule (only orders that carry energy)
        tim = {}
        for T, v in r["timing"].items():
            ok = [k for k in v["peak_offset_ms"]
                  if "ess_extraction" in r and r["ess_extraction"][T][k]["n"] > 0]
            if "ess_extraction" not in r:
                ok = [k for k in v["peak_offset_ms"] if k in (1, 3, 5, 7, 9)]
            tim[str(T)] = {"gap_9_10_ms": v["predicted_gap_9_10_ms"],
                           "max_abs_offset_ms": float(max(abs(v["peak_offset_ms"][k]) for k in ok)),
                           "orders": ok}
        row["timing"] = tim
        row["L_s"] = r["ess_L"]
        if "ess_extraction" in r:
            row["ess_extraction"] = {str(T): {str(k): v for k, v in pk.items()}
                                     for T, pk in r["ess_extraction"].items()}
        if "small_signal_err" in r:
            row["small_signal_err"] = r["small_signal_err"]
        cur = {}
        for tag, cv in r["curve_levels"].items():
            c = {"level_db": cv["level_db"]}
            for nm in ("uncorrected", "oracle_post", "split_post", "truth"):
                if nm in cv:
                    c[nm] = {"sym": cv[nm]["sym"]}
                    for e in ("err", "err_vs_truncated"):
                        if e in cv[nm]:
                            c[nm][e] = cv[nm][e]
            cur[tag] = c
        row["curve"] = cur
        if "hardness" in r:
            row["hardness"] = {nm: {"pos_v_p": v["pos"][1:3], "neg_v_p": v["neg"][1:3],
                                    "pos_rms_db": v["pos"][3], "neg_rms_db": v["neg"][3]}
                               for nm, v in r["hardness"].items()}
        row["output_dc_re_fund"] = r["sym_output_dc_re_fund"]
        for route in ("split_ess", "split_steady"):
            sp = r[route]
            row[route] = {"fund_residual_rms_db": sp["residual_rms_db"],
                          "harmonic_residual_db": sp["harmonic_residual_db"],
                          "identifiable_fraction": float(np.mean(sp["identifiable"]))}
            if route + "_err" in r:
                row[route]["err"] = r[route + "_err"]
        summary["systems"].append(row)

        # figures per system
        sp, spt = r["split_ess"], r["split_steady"]
        ser = []
        if s.P is not None:
            ser += [(sp["f"], D.db(s.P(sp["f"])), "pre P truth", COLORS[0], False),
                    (sp["f"], D.db(s.Q(sp["f"])), "post Q truth", COLORS[1], False)]
        idm = sp["identifiable"]
        ser += [(sp["f"], np.where(idm, sp["P_db"], np.nan), "P est (sweeps)", COLORS[0], True),
                (sp["f"], np.where(idm, sp["Q_db"], np.nan), "Q est (sweeps)", COLORS[1], True),
                (spt["f"], np.where(spt["identifiable"], spt["P_db"], np.nan), "P est (sines)",
                 COLORS[2], True),
                (spt["f"], np.where(spt["identifiable"], spt["Q_db"], np.nan), "Q est (sines)",
                 COLORS[3], True)]
        svg_lines(out / f"step1_split_{r['name']}.svg", ser,
                  f"Step 1 {r['name']}: pre/post split (0 dB at 1 kHz by convention)",
                  "frequency (Hz)", "dB", logx=True)
        cv = r["curve_levels"]["knee_plus_10"]
        ser = []
        if "truth" in cv:
            ser.append((cv["x"], cv["truth"]["y"], "truth", "black", False))
        for nm, col in (("uncorrected", COLORS[3]), ("split_post", COLORS[0])):
            if nm in cv:
                ser.append((cv["x"], cv[nm]["y"], nm.replace("_", " "), col, True))
        svg_lines(out / f"step1_curve_{r['name']}.svg", ser,
                  f"Step 1 {r['name']}: static curve from a 1 kHz sine at {cv['level_db']:.0f} dBFS",
                  "input (full scale = 1, referred to 1 kHz)", "output")
    names = [x["name"] for x in summary["systems"]]
    svg_lines(out / "step1_residuals.svg",
              [(np.arange(len(names)), [x["split_ess"]["fund_residual_rms_db"]
                                        for x in summary["systems"]],
                "fundamental residual (sweeps)", COLORS[0], False),
               (np.arange(len(names)), [np.nanmax([v for k, v in
                                                   x["split_ess"]["harmonic_residual_db"].items()
                                                   if int(k) % 2 == 1 and v is not None] or [np.nan])
                                        for x in summary["systems"]],
                "odd-harmonic residual (sweeps)", COLORS[1], False)],
              "Step 1: W-H alignment residual per system (x = " + ", ".join(
                  f"{i} {n}" for i, n in enumerate(names)) + ")",
              "system index", "dB", w=900)
    summary["wall_s"] = time.time() - t0
    return _clean(summary)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--step", choices=["0", "1", "all"], default="all")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.step in ("0", "all"):
        t = time.time()
        r0 = step0(out)
        r0["wall_s"] = time.time() - t
        (out / "results_step0.json").write_text(json.dumps(_clean(r0), indent=1))
        i = r0["identification"]
        print(f"step 0: {r0['n_models']} models, {r0['n_settings']} settings; top-1 (scalars, "
              f"no default) {i['scalars']['top1'][0]:.3f}, note6 {i['note6']['top1'][0]:.3f}, "
              f"curves {i['curves']['top1'][0]:.3f}; chance {r0['chance_top1']:.3f}")
    if a.step in ("1", "all"):
        r1 = step1(out)
        (out / "results_step1.json").write_text(json.dumps(r1, indent=1))
        print(f"step 1: {len(r1['systems'])} systems in {r1['wall_s']:.0f} s")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
