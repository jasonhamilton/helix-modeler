#!/usr/bin/env python
"""Offline analysis of the user's impulse-response (IR) library against the Helix cab models (T106).

    python scripts/ir_library_analyze.py all            # every stage, in order
    python scripts/ir_library_analyze.py inventory|analyze|helix|cluster|estimate|summary

Reads `data/ir/` (gitignored, the user's own files, never committed) and local caches only:
block-library fingerprints (`cache/probe/fingerprints/<library>/`) and helix-trust-v2 renders
(`cache/loss-trust/helix-trust-v2/`). No Native, no pedal. Writes only under `reports/local/ir-library/work/`
(gitignored): per-file metadata and smoothed magnitude responses (55 points, no phase), which cannot
reconstruct an IR. No IR audio is copied anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import struct
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.ir_library import scan_library
from tonematch.metrics import cab_response as cr

IR_DIR = ROOT / "data" / "ir"
OUT = ROOT / "reports" / "local" / "ir-library" / "work"
FP_DIR = ROOT / "cache" / "probe" / "fingerprints" / "block-library-v1.2"
TRUST = ROOT / "cache" / "loss-trust" / "helix-trust-v2"
GRID = cr.FINGERPRINT_GRID
BAND = cr.COMPARE_BAND
DUP_DB = 0.15  # responses closer than this (RMS dB, 100 Hz - 8 kHz) are treated as the same IR


def _jdump(obj, name):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1, default=_default))


def _default(o):
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def _clean(v):
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


# ------------------------------------------------------------------------------------- inventory
def riff_chunks(path: Path) -> dict:
    """Chunk ids of a RIFF/WAVE file and any text in LIST/INFO, bext, iXML or similar chunks."""
    out = {"chunks": [], "text": {}}
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            out["error"] = "not RIFF/WAVE"
            return out
        while True:
            h = fh.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4].decode("latin-1"), struct.unpack("<I", h[4:])[0]
            out["chunks"].append(cid)
            if cid == "data" or size > 1 << 20:
                fh.seek(size + (size & 1), 1)
                continue
            body = fh.read(size + (size & 1))
            if cid == "LIST" and body[:4] == b"INFO":
                i = 4
                while i + 8 <= len(body):
                    sid, ssz = body[i:i + 4].decode("latin-1"), struct.unpack("<I", body[i + 4:i + 8])[0]
                    txt = body[i + 8:i + 8 + ssz].split(b"\0")[0].decode("latin-1", "replace").strip()
                    if txt:
                        out["text"][f"INFO.{sid}"] = txt
                    i += 8 + ssz + (ssz & 1)
            elif cid in ("bext", "iXML", "_PMX", "XMP ", "cue ", "note", "labl", "IDit", "ICMT"):
                txt = body.replace(b"\0", b" ").decode("latin-1", "replace").strip()
                txt = " ".join(t for t in txt.split() if t.isprintable())[:300]
                if txt:
                    out["text"][cid] = txt
    return out


def stage_inventory() -> list[dict]:
    import soundfile as sf

    entries = scan_library(IR_DIR)
    for e in entries:
        p = IR_DIR / e["path"]
        b = p.read_bytes()
        e["bytes"] = len(b)
        e["md5"] = hashlib.md5(b).hexdigest()
        try:
            info = sf.info(str(p))
            e.update(sr=info.samplerate, channels=info.channels, frames=info.frames, subtype=info.subtype,
                     ms=1000.0 * info.frames / info.samplerate)
            # same audio in any bit depth or gain: left channel, peak-normalised, rounded to 16-bit steps
            # (an int16 read is NOT safe: float files with peaks above full scale saturate alike)
            data, _ = sf.read(str(p), dtype="float64", always_2d=True)
            x = data[:, 0]
            pk = float(np.max(np.abs(x))) or 1.0
            e["peak_file"] = pk
            e["audio_sha1"] = hashlib.sha1(np.round(x / pk * 32767).astype(np.int32).tobytes()).hexdigest()
        except Exception as exc:  # noqa: BLE001
            e["read_error"] = str(exc)
        e["riff"] = riff_chunks(p)
    others = sorted(str(q.relative_to(IR_DIR)) for q in IR_DIR.rglob("*") if q.is_file() and q.suffix.lower() != ".wav")
    _jdump({"entries": entries, "other_files": others}, "inventory.json")
    print(f"inventory: {len(entries)} wav, {len(others)} other files")
    return entries


# ------------------------------------------------------------------------------------- analysis
def _analyze_one(rel: str) -> dict:
    x, info = cr.load_ir(IR_DIR / rel)
    a = np.abs(x)
    peak = int(np.argmax(a))
    e = x ** 2
    tot = float(e.sum()) + 1e-30
    lo, hi = max(0, peak - 48), peak + 4800
    out = {
        "path": rel,
        "n48": int(x.size),
        "peak_sample": peak,
        "peak_ms": 1000.0 * peak / cr.SAMPLE_RATE,
        "peak_dbfs": float(20 * np.log10(a[peak] + 1e-30)),
        "e_frac_100ms": float(e[lo:hi].sum() / tot),
        "e_frac_2048": float(e[:2048].sum() / tot),
        "e_frac_1024": float(e[:1024].sum() / tot),
        "right_differs_db": info.get("right_differs_db"),
    }
    t = np.cumsum(e[::-1])[::-1] / tot  # Schroeder backward energy
    above = np.flatnonzero(t > 10 ** (-60 / 10))
    out["len_to_-60db_ms"] = 1000.0 * (above[-1] + 1 if above.size else 0) / cr.SAMPLE_RATE
    full = cr.ir_response(x)
    h2048 = cr.ir_response(cr.helix_length(x, 2048))
    h1024 = cr.ir_response(cr.helix_length(x, 1024))
    out["resp"] = full.tolist()
    out["resp2048"] = h2048.tolist()
    out["third_oct"] = cr.band_levels(x).tolist()
    for n, h in (("2048", h2048), ("1024", h1024)):
        out[f"trunc{n}_rms_db"] = cr.response_distance(GRID, full, h, BAND)
        out[f"trunc{n}_low_db"] = float(np.mean((h - full)[(GRID >= 40) & (GRID <= 125)])
                                        - np.mean((h - full)[(GRID >= 500) & (GRID <= 2000)]))
    out.update(cr.response_features(GRID, full))
    return out


def stage_analyze(entries: list[dict] | None = None) -> list[dict]:
    if entries is None:
        entries = json.loads((OUT / "inventory.json").read_text())["entries"]
    rels = [e["path"] for e in entries if "read_error" not in e]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
        rows = list(ex.map(_analyze_one, rels, chunksize=16))
    for r in rows:
        r["is_ir"] = bool(r["e_frac_100ms"] >= 0.5)  # recordings spread energy over seconds
    _jdump(rows, "analysis.json")
    print(f"analyze: {len(rows)} files in {time.time() - t0:.1f} s; {sum(r['is_ir'] for r in rows)} look like IRs")
    return rows


# ------------------------------------------------------------------------------------- shared loading
def load_all():
    inv = json.loads((OUT / "inventory.json").read_text())["entries"]
    ana = {r["path"]: r for r in json.loads((OUT / "analysis.json").read_text())}
    rows = []
    for e in inv:
        if e["path"] in ana:
            r = dict(e)
            r.update(ana[e["path"]])
            r["cab_key"] = f"{r['group']}/{r['brand']}/{r['cab']}"
            rows.append(r)
    return rows


def helix_bank():
    import re

    names, settings, resp, legacy = [], [], [], []
    for f in sorted(FP_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("category") != "cab":
            continue
        for s in d["settings"]:
            g, m = cr.fingerprint_response(s)
            assert np.allclose(g, GRID, rtol=1e-3)
            names.append(d["model"])
            settings.append(s["setting"])
            resp.append(m)
            legacy.append("CabMicIr" not in d["model"])
    sizes = []
    for n in names:
        m = re.search(r"(\d)x(\d+)(?:x(\d+))?", n)
        sizes.append(f"{m.group(1)}x{m.group(2)}" + (f"x{m.group(3)}" if m and m.group(3) else "") if m else None)
    return {"model": names, "setting": settings, "resp": np.array(resp), "legacy": np.array(legacy), "size": sizes}


def _band_for(knobs: dict, legacy: bool) -> tuple[float, float]:
    """Comparison band that avoids the block's own Low Cut / High Cut (CC values, linear maps)."""
    lc = 19.9 + 480.1 * knobs["LowCut"] / 127 if "LowCut" in knobs else (80.0 if legacy else 19.9)
    hc = 500 + 19600 * knobs["HighCut"] / 127 if "HighCut" in knobs else (8000.0 if legacy else 20100.0)
    lo = max(100.0, 1.5 * lc)
    hi = min(8000.0, hc / 1.5)
    if hi / lo < 4:
        hi = min(8000.0, lo * 4)
    return lo, hi


# ------------------------------------------------------------------------------------- helix comparison
def stage_helix():
    rows = [r for r in load_all() if r["is_ir"]]
    hb = helix_bank()
    H = cr.normalise(GRID, hb["resp"])
    gtr = [r for r in rows if r["group"] in ("guitar", "misc", "artist")]
    cabs = [r for r in rows if r["group"] == "guitar"]
    R = cr.normalise(GRID, np.array([r["resp"] for r in gtr]))
    out = {"n_helix_settings": len(hb["model"]), "n_helix_models": len(set(hb["model"])), "n_irs": len(gtr)}

    # Use only settings with an open-ish high cut for coverage (HighCut@24 = 4.2 kHz is a deliberate dark
    # variant) and drop legacy "default" (High Cut 8 kHz sits on the band edge).
    open_sel = np.array([("HighCut@24" not in s) and not (lg and s == "default")
                         for s, lg in zip(hb["setting"], hb["legacy"])])
    Ho = H[open_sel]
    ho_model = [m for m, k in zip(hb["model"], open_sel) if k]
    cov = {}
    for det in (0, 1, 2):
        d_h = np.array([np.min(cr.response_distance(GRID, R[i][None], Ho, BAND, det)) for i in range(len(gtr))])
        cov[det] = d_h
    # IR -> nearest IR from a different cab (leave-cab-out), guitar-cab IRs only
    keys = np.array([r["cab_key"] for r in gtr])
    d_ir = {}
    for det in (0, 1):
        dd = np.empty(len(gtr))
        for i in range(len(gtr)):
            d = cr.response_distance(GRID, R[i][None], R, BAND, det)
            d[keys == keys[i]] = np.inf
            d[d < DUP_DB] = np.inf  # copies of the same IR filed under another folder
            dd[i] = d.min()
        d_ir[det] = dd
    groups = np.array([r["group"] for r in gtr])
    # density-matched control: the Helix bank holds len(Ho) responses (a few settings per model, default
    # mic, centre position). Draw the same number of IRs from other cabs and take the nearest of those.
    rng = np.random.default_rng(0)
    gi_all = np.flatnonzero(groups == "guitar")
    matched = []
    for i in gi_all:
        pool = gi_all[(keys[gi_all] != keys[i])]
        reps = []
        for _ in range(5):
            sub = rng.choice(pool, size=min(len(Ho), pool.size), replace=False)
            d = cr.response_distance(GRID, R[i][None], R[sub], BAND, 0)
            d = d[d >= DUP_DB]
            reps.append(d.min())
        matched.append(float(np.median(reps)))

    def q(a):
        a = np.asarray(a)
        return {"median": float(np.median(a)), "p10": float(np.percentile(a, 10)), "p90": float(np.percentile(a, 90)),
                "frac_le_1db": float(np.mean(a <= 1)), "frac_le_2db": float(np.mean(a <= 2)),
                "frac_gt_3db": float(np.mean(a > 3)), "n": int(a.size)}

    out["ir_to_nearest_helix"] = {f"detrend{k}": {g: q(v[groups == g]) for g in ("guitar", "misc", "artist")}
                                  for k, v in cov.items()}
    out["ir_to_nearest_other_cab_ir"] = {f"detrend{k}": {g: q(v[groups == g]) for g in ("guitar", "misc", "artist")}
                                         for k, v in d_ir.items()}
    out["ir_to_nearest_of_matched_ir_subset"] = q(matched)
    out["matched_subset_size"] = len(Ho)
    out["ir_closer_to_matched_ir_subset_than_helix"] = float(np.mean(np.array(matched) < cov[0][gi_all]))
    out["ir_closer_to_other_ir_than_helix"] = {g: float(np.mean(d_ir[0][groups == g] < cov[0][groups == g]))
                                               for g in ("guitar", "misc", "artist")}
    # worst-covered guitar-cab IRs (local report only)
    gi = np.flatnonzero(groups == "guitar")
    worst = gi[np.argsort(-cov[0][gi])[:15]]
    out["worst_covered"] = [{"path": gtr[i]["path"], "d_helix": float(cov[0][i]), "d_helix_tilt": float(cov[1][i]),
                             "d_other_ir": float(d_ir[0][i])} for i in worst]

    # scale: within-cab moves
    out["scale"] = scale_stats(cabs)

    # Helix model -> nearest IR and nearest other Helix model (per model, best over open settings)
    per_model = []
    models = sorted(set(ho_model))
    for m in models:
        idx = [i for i, mm in enumerate(ho_model) if mm == m]
        best_ir, best_d, best_h, best_hd = None, np.inf, None, np.inf
        for i in idx:
            d = cr.response_distance(GRID, Ho[i][None], R, BAND, 0)
            j = int(np.argmin(d))
            if d[j] < best_d:
                best_d, best_ir = float(d[j]), gtr[j]["path"]
            other = [k for k, mm in enumerate(ho_model) if mm != m]
            dh = cr.response_distance(GRID, Ho[i][None], Ho[other], BAND, 0)
            k = int(np.argmin(dh))
            if dh[k] < best_hd:
                best_hd, best_h = float(dh[k]), ho_model[other[k]]
        top = {}
        for i in idx:  # top-3 IRs at the setting closest to an IR
            d = cr.response_distance(GRID, Ho[i][None], R, BAND, 0)
            for j in np.argsort(d)[:3]:
                top[gtr[j]["path"]] = min(top.get(gtr[j]["path"], np.inf), float(d[j]))
        per_model.append({"model": m, "nearest_ir": best_ir, "d_ir": best_d, "nearest_helix": best_h, "d_helix": best_hd,
                          "top_irs": sorted(top.items(), key=lambda t: t[1])[:3],
                          "ir_closer": best_d < best_hd})
    out["helix_models"] = per_model
    out["helix_models_summary"] = {
        "median_d_ir": float(np.median([p["d_ir"] for p in per_model])),
        "median_d_other_helix": float(np.median([p["d_helix"] for p in per_model])),
        "frac_nearest_is_ir": float(np.mean([p["ir_closer"] for p in per_model])),
    }
    # size agreement: does the nearest IR of a Helix cab share its cab size?
    size_of = {r["path"]: r["cab_size"] for r in gtr}
    hs = dict(zip(hb["model"], hb["size"]))
    agree = [(hs[p["model"]], size_of.get(p["nearest_ir"])) for p in per_model]
    agree = [(a, b) for a, b in agree if a and b]
    out["helix_nearest_ir_same_size"] = {"n": len(agree), "rate": float(np.mean([a == b for a, b in agree])) if agree else None}
    _jdump(out, "helix.json")
    print(json.dumps({k: out[k] for k in ("ir_to_nearest_helix", "ir_to_nearest_other_cab_ir",
                                          "ir_to_nearest_of_matched_ir_subset", "matched_subset_size",
                                          "ir_closer_to_matched_ir_subset_than_helix", "scale",
                                          "ir_closer_to_other_ir_than_helix", "helix_models_summary",
                                          "helix_nearest_ir_same_size")}, indent=1))
    return out


def scale_stats(cabs):
    """Distances for small moves inside one cab: neighbouring grid points, other mic, other cab."""
    R = {r["path"]: cr.normalise(GRID, np.array(r["resp"])) for r in cabs}
    by_folder = defaultdict(list)
    for r in cabs:
        by_folder[(r["cab_key"], tuple(r["mics"]), str(Path(r["path"]).parent))].append(r)
    nb_col, nb_row = [], []
    for rs in by_folder.values():
        pos = {(r["grid_row"], r["grid_col"]): r for r in rs if r["grid_row"]}
        for (row, col), r in pos.items():
            nxt = pos.get((row, chr(ord(col) + 1)))
            if nxt:
                nb_col.append(cr.response_distance(GRID, R[r["path"]], R[nxt["path"]], BAND))
            dn = pos.get((row + 1, col))
            if dn:
                nb_row.append(cr.response_distance(GRID, R[r["path"]], R[dn["path"]], BAND))
    rng = random.Random(0)
    same_cab_other_mic, other_cab = [], []
    by_cab = defaultdict(list)
    for r in cabs:
        if len(r["mics"]) == 1:
            by_cab[r["cab_key"]].append(r)
    keys = list(by_cab)
    for _ in range(3000):
        k = rng.choice(keys)
        rs = by_cab[k]
        a = rng.choice(rs)
        others = [b for b in rs if b["mics"] != a["mics"]]
        if others:
            b = rng.choice(others)
            same_cab_other_mic.append(cr.response_distance(GRID, R[a["path"]], R[b["path"]], BAND))
        k2 = rng.choice([kk for kk in keys if kk != k])
        b = rng.choice(by_cab[k2])
        other_cab.append(cr.response_distance(GRID, R[a["path"]], R[b["path"]], BAND))

    def med(a):
        return {"median": float(np.median(a)), "p10": float(np.percentile(a, 10)), "p90": float(np.percentile(a, 90)),
                "n": len(a)} if a else None

    return {"grid_next_letter": med(nb_col), "grid_next_number": med(nb_row),
            "same_cab_other_mic": med(same_cab_other_mic), "random_other_cab": med(other_cab)}


# ------------------------------------------------------------------------------------- clustering
def stage_cluster():
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.stats import spearmanr
    from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

    rows = [r for r in load_all() if r["is_ir"] and r["group"] == "guitar"]
    sel = (GRID >= 80) & (GRID <= 10000)
    X = cr.normalise(GRID, np.array([r["resp"] for r in rows]))[:, sel]
    X = X - X.mean(axis=1, keepdims=True)
    Z = linkage(X, "ward")
    labels = {
        "brand": [r["brand"] for r in rows],
        "cab": [r["cab_key"] for r in rows],
        "cab_size": [r["cab_size"] or "?" for r in rows],
        "mic": [r["mics"][0] if len(r["mics"]) == 1 else ("multi" if r["mics"] else "?") for r in rows],
        "speaker": [r["speakers"][0] if len(r["speakers"]) == 1 else "?" for r in rows],
    }
    out = {"n": len(rows), "clusters": {}}
    for k in (6, 12, 24):
        c = fcluster(Z, k, "maxclust")
        res = {}
        for name, lab in labels.items():
            known = np.array([v not in ("?", "multi") for v in lab])
            la = np.array(lab)[known]
            res[name] = {"nmi": float(normalized_mutual_info_score(la, c[known])),
                         "ari": float(adjusted_rand_score(la, c[known])), "n": int(known.sum())}
        comp = []
        for ci in sorted(set(c)):
            m = c == ci
            sub = [rows[i] for i in np.flatnonzero(m)]
            comp.append({"cluster": int(ci), "n": int(m.sum()),
                         "top_brand": Counter(r["brand"] for r in sub).most_common(3),
                         "top_mic": Counter(lb for lb, mm in zip(labels["mic"], m) if mm).most_common(3),
                         "top_size": Counter(lb for lb, mm in zip(labels["cab_size"], m) if mm).most_common(2),
                         "median_features": {f: float(np.nanmedian([r[f] for r in sub])) for f in
                                             ("resonance_hz", "high_rolloff_hz", "presence_db", "brightness_db",
                                              "tilt_db_per_oct")}})
        out["clusters"][str(k)] = {"agreement": res, "composition": comp}

    # leave-one-cab-out 1-NN: can the response alone tell size, mic, speaker of an unseen cab?
    D = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).mean(axis=2))
    cab = np.array(labels["cab"])
    D[cab[:, None] == cab[None, :]] = np.inf
    nn = np.argmin(D, axis=1)
    loo = {}
    for name in ("cab_size", "mic", "speaker", "brand"):
        lab = np.array(labels[name])
        known = np.array([v not in ("?", "multi") for v in lab])
        if name == "cab_size":
            known &= np.isin(lab, ["4x12", "2x12", "1x12", "1x10", "2x10"])
        ok = known & np.array([lab[nn[i]] not in ("?", "multi") for i in range(len(lab))])
        hit = float(np.mean(lab[ok] == lab[nn[ok]]))
        major = Counter(lab[ok]).most_common(1)[0]
        loo[name] = {"accuracy": hit, "majority_baseline": major[1] / ok.sum(), "majority": major[0], "n": int(ok.sum()),
                     "classes": dict(Counter(lab[ok]).most_common(8))}
    out["leave_cab_out_1nn"] = loo

    # grid decoding: which axis of "<number><letter>" moves brightness?
    feats = ("centroid_hz", "brightness_db", "presence_db", "resonance_db", "tilt_db_per_oct")
    corr = {f: {"letter": [], "number": []} for f in feats}
    by_folder = defaultdict(list)
    for r in rows:
        if r["grid_row"]:
            by_folder[str(Path(r["path"]).parent) + "|" + ",".join(r["mics"])].append(r)
    for rs in by_folder.values():
        if len(rs) < 8:
            continue
        num = [r["grid_row"] for r in rs]
        let = [ord(r["grid_col"]) for r in rs]
        for f in feats:
            v = [r[f] for r in rs]
            if len(set(let)) > 2:
                corr[f]["letter"].append(spearmanr(let, v).statistic)
            if len(set(num)) > 2:
                corr[f]["number"].append(spearmanr(num, v).statistic)
    out["grid_axes"] = {f: {ax: {"median_rho": float(np.nanmedian(v)) if v else None, "n_folders": len(v),
                                 "frac_negative": float(np.mean(np.array(v) < 0)) if v else None}
                            for ax, v in d.items()} for f, d in corr.items()}

    # mic signatures (single-mic guitar-cab IRs): median normalised 1/3-octave curve per mic
    mic_sig = {}
    th = cr.THIRD_OCTAVE
    for mic, n in Counter(labels["mic"]).most_common():
        if mic in ("?", "multi") or n < 20:
            continue
        sub = [r for r, lb in zip(rows, labels["mic"]) if lb == mic]
        T = cr.normalise(th, np.array([r["third_oct"] for r in sub]))
        mic_sig[mic] = {"n": n, "median_curve_db": np.round(np.median(T, axis=0), 1).tolist(),
                        **{f: float(np.nanmedian([r[f] for r in sub])) for f in
                           ("presence_db", "brightness_db", "high_rolloff_hz", "resonance_db", "centroid_hz")}}
    out["third_oct_centres"] = np.round(th, 1).tolist()
    out["mic_signatures"] = mic_sig
    _jdump(out, "cluster.json")
    print(json.dumps({"agreement": {k: v["agreement"] for k, v in out["clusters"].items()},
                      "loo": out["leave_cab_out_1nn"], "grid_axes": out["grid_axes"]}, indent=1))
    return out


# ------------------------------------------------------------------------------------- estimation (b)
def _smooth3(v: np.ndarray) -> np.ndarray:
    """+-1/3 octave moving average on the 1/6-octave grid (5 points), edges held."""
    p = np.pad(np.asarray(v, float), 2, mode="edge")
    return np.convolve(p, np.ones(5) / 5, mode="valid")


def _load_render(item_dir: str, c: int) -> np.ndarray:
    return np.load(TRUST / "renders" / item_dir / f"c{c:02d}_A.npy").astype(np.float64)


def stage_estimate(n_irs: int = 120, seed: int = 0):
    import soundfile as sf
    from scipy.signal import fftconvolve

    man = json.loads((TRUST / "manifest.json").read_text())
    hb = helix_bank()
    Hn = cr.normalise(GRID, hb["resp"])
    hmodels = np.array(hb["model"])
    out = {}

    # E1: Helix cab blocks alone on real guitar DI (known DI, same notes): linearity and identification.
    e1 = []
    for it in [i for i in man["items"] if i["category"] == "cab"]:
        dry, _ = sf.read(str(ROOT / it["di"]["A"]["file"]), dtype="float64")
        true = it["candidates"][0]
        wet = _load_render(it["dir"], 0)
        mag, coh = cr.transfer_response(dry, wet)
        legacy = "CabMicIr" not in true["model"]
        band = _band_for(true["knobs"], legacy)
        est = cr.normalise(GRID, mag)
        d = cr.response_distance(GRID, est[None], Hn, band, 0)
        best = {}
        for m, dv in zip(hmodels, d):
            best[m] = min(best.get(m, np.inf), float(dv))
        ranked = sorted(best, key=best.get)
        bsel = (GRID >= band[0]) & (GRID <= band[1])
        e1.append({"item": it["item"], "model": true["model"], "band": band, "rank_true": ranked.index(true["model"]) + 1,
                   "top1": ranked[0], "d_true": best[true["model"]], "d_top1": best[ranked[0]],
                   "coherence_median": float(np.median(coh[bsel])), "coherence_min": float(np.min(coh[bsel]))})
    r = np.array([e["rank_true"] for e in e1])
    out["E1_helix_cab_identification"] = {
        "n": len(e1), "top1": float(np.mean(r == 1)), "top3": float(np.mean(r <= 3)), "top5": float(np.mean(r <= 5)),
        "median_rank": float(np.median(r)), "n_models": len(set(hb["model"])),
        "coherence_median": float(np.median([e["coherence_median"] for e in e1])), "items": e1}

    # E2: amp-only Helix render convolved with a library IR = target; recover the IR from it.
    rows = [x for x in load_all() if x["is_ir"] and x["group"] == "guitar"]
    rng = random.Random(seed)
    pick = rng.sample(range(len(rows)), n_irs)
    R = cr.normalise(GRID, np.array([x["resp"] for x in rows]))
    amp_items = [i for i in man["items"] if i["category"] == "amp"]
    cases = defaultdict(list)
    t_conv, t_est = [], []
    for k, j in enumerate(pick):
        it = amp_items[k % len(amp_items)]
        ir, _ = cr.load_ir(IR_DIR / rows[j]["path"])
        ir = cr.helix_length(ir, 2048)
        truth = cr.normalise(GRID, cr.ir_response(ir))
        amp_true = _load_render(it["dir"], 0)
        t = time.perf_counter()
        target = fftconvolve(amp_true, ir)[: amp_true.size]
        t_conv.append(time.perf_counter() - t)
        near = _load_render(it["dir"], 1)  # same model, other knobs
        other_c = next(ci for ci, c in enumerate(it["candidates"]) if c["label"].startswith("nbr:"))
        other = _load_render(it["dir"], other_c)  # a different amp model
        half = amp_true.size // 2
        h1 = cr.transfer_response
        lt = cr.ltas_ratio_response
        variants = [
            ("H1 right amp, same notes", h1, (amp_true, target)),
            ("H1 same amp other knobs, same notes", h1, (near, target)),
            ("H1 other amp model, same notes", h1, (other, target)),
            ("LTAS right amp, other notes", lt, (target[:half], amp_true[half:])),
            ("LTAS same amp other knobs, other notes", lt, (target[:half], near[half:])),
            ("LTAS other amp model, other notes", lt, (target[:half], other[half:])),
        ]
        for name, fn, args in variants:
            t = time.perf_counter()
            res = fn(*args)
            est = cr.normalise(GRID, res[0] if isinstance(res, tuple) else res)
            t_est.append(time.perf_counter() - t)
            err0 = cr.response_distance(GRID, est, truth, BAND, 0)
            err1 = cr.response_distance(GRID, est, truth, BAND, 1)
            errs = cr.response_distance(GRID, _smooth3(est), _smooth3(truth), BAND, 0)
            d = cr.response_distance(GRID, est[None], R, BAND, 0)
            order = np.argsort(d)
            same = set(np.flatnonzero(cr.response_distance(GRID, R[j][None], R, BAND, 0) < DUP_DB).tolist())
            rank = int(np.flatnonzero(np.isin(order, list(same)))[0]) + 1  # duplicates of the true IR count
            top = rows[order[0]]
            cases[name].append({"err_db": err0, "err_tilt_db": err1, "err_smooth_db": errs, "rank_exact": rank,
                                "top1_same_cab": top["cab_key"] == rows[j]["cab_key"],
                                "top1_same_mic": top["mics"] == rows[j]["mics"] and bool(rows[j]["mics"]),
                                "top1_same_size": top["cab_size"] == rows[j]["cab_size"] and bool(rows[j]["cab_size"]),
                                "d_top1": float(d[order[0]]), "d_true": float(d[j])})
    e2 = {}
    for name, cs in cases.items():
        rk = np.array([c["rank_exact"] for c in cs])
        e2[name] = {"n": len(cs), "median_err_db": float(np.median([c["err_db"] for c in cs])),
                    "p90_err_db": float(np.percentile([c["err_db"] for c in cs], 90)),
                    "median_err_after_tilt_db": float(np.median([c["err_tilt_db"] for c in cs])),
                    "median_err_third_oct_smooth_db": float(np.median([c["err_smooth_db"] for c in cs])),
                    "exact_top1": float(np.mean(rk == 1)), "exact_top10": float(np.mean(rk <= 10)),
                    "median_rank": float(np.median(rk)),
                    "top1_same_cab": float(np.mean([c["top1_same_cab"] for c in cs])),
                    "top1_same_mic": float(np.mean([c["top1_same_mic"] for c in cs])),
                    "top1_same_size": float(np.mean([c["top1_same_size"] for c in cs]))}
    out["E2_recover_ir_from_amp_plus_ir"] = {"n_irs": n_irs, "library_size": len(rows), "seed": seed, "cases": e2}
    out["timing"] = {"fft_convolve_10s_2048_ms": 1000 * float(np.median(t_conv)),
                     "estimate_ms": 1000 * float(np.median(t_est))}
    # cost of an exhaustive nearest-IR search over the library from one estimate
    t = time.perf_counter()
    for _ in range(20):
        cr.response_distance(GRID, R[0][None], R, BAND, 0)
    out["timing"]["nearest_over_library_ms"] = 1000 * (time.perf_counter() - t) / 20
    _jdump(out, "estimate.json")
    print(json.dumps({k: v for k, v in out.items() if k != "E1_helix_cab_identification"}, indent=1))
    print(json.dumps({k: v for k, v in out["E1_helix_cab_identification"].items() if k != "items"}, indent=1))
    return out


# ------------------------------------------------------------------------------------- summary
def stage_summary():
    rows = load_all()
    inv = json.loads((OUT / "inventory.json").read_text())
    s = {}
    s["n_wav"] = len(inv["entries"])
    s["other_files"] = inv["other_files"]
    s["bytes_total"] = sum(e["bytes"] for e in inv["entries"])
    s["groups"] = Counter(r["group"] for r in rows)
    s["brands"] = Counter(r["brand"] for r in rows if r["group"] == "guitar")
    s["cab_folders"] = len({r["cab_key"] for r in rows if r["group"] == "guitar"})
    s["cab_size"] = Counter(f"{r['cab_size']} ({r['cab_size_source']})" for r in rows if r["group"] == "guitar")
    s["mics"] = Counter(m for r in rows for m in r["mics"])
    s["multi_mic"] = sum(r["multi_mic"] for r in rows)
    s["speakers"] = Counter(sp for r in rows for sp in r["speakers"])
    s["positions"] = Counter(r["position"] for r in rows if r["position"])
    s["grid_named"] = sum(1 for r in rows if r["grid_row"])
    s["format"] = {
        "sr": Counter(r.get("sr") for r in rows), "subtype": Counter(r.get("subtype") for r in rows),
        "channels": Counter(r.get("channels") for r in rows),
        "frames": Counter(r.get("frames") for r in rows).most_common(12),
        "ms_quantiles": np.percentile([r["ms"] for r in rows], [0, 10, 50, 90, 99, 100]).round(1).tolist(),
    }
    s["stereo_left_vs_right_db"] = sorted(round(r["right_differs_db"], 1) for r in rows
                                          if r.get("right_differs_db") is not None)
    s["not_ir"] = [{"path": r["path"], "ms": round(r["ms"], 1), "e_frac_100ms": round(r["e_frac_100ms"], 3)}
                   for r in rows if not r["is_ir"]]
    # duplicates
    by_md5, by_pcm = defaultdict(list), defaultdict(list)
    for r in rows:
        by_md5[r["md5"]].append(r["path"])
        by_pcm[r.get("audio_sha1")].append(r["path"])
    s["dup_exact_files"] = [v for v in by_md5.values() if len(v) > 1]
    s["dup_same_audio"] = [v for v in by_pcm.values() if len(v) > 1 and v not in s["dup_exact_files"]]
    s["float_files_peak_over_full_scale"] = sum(1 for r in rows if r.get("subtype") in ("FLOAT", "DOUBLE")
                                                and r.get("peak_file", 0) > 1.0)
    irs = [r for r in rows if r["is_ir"]]
    R = np.array([r["resp"] for r in irs])
    Rn = cr.normalise(GRID, R)
    near = []
    seen = set()
    for i in range(len(irs)):
        d = cr.response_distance(GRID, Rn[i][None], Rn, (40.0, 16000.0), 0)
        for j in np.flatnonzero(d < 0.1):
            if j > i and (i, j) not in seen and irs[i]["md5"] != irs[j]["md5"]:
                near.append((irs[i]["path"], irs[j]["path"], round(float(d[j]), 3)))
                seen.add((i, j))
    s["dup_near_response"] = near
    s["riff_chunks"] = Counter(c for r in rows for c in r["riff"]["chunks"])
    texts = Counter()
    for r in rows:
        for k, v in r["riff"]["text"].items():
            texts[f"{k}: {v}"] += 1
    s["riff_text"] = texts.most_common(60)
    # IR-block limits
    s["len48_quantiles"] = np.percentile([r["n48"] for r in irs], [0, 10, 50, 90, 100]).tolist()
    s["frac_longer_than_2048"] = float(np.mean([r["n48"] > 2048 for r in irs]))
    s["e_frac_2048_quantiles"] = np.percentile([r["e_frac_2048"] for r in irs], [1, 10, 50]).tolist()
    s["trunc2048_rms_db_quantiles"] = np.percentile([r["trunc2048_rms_db"] for r in irs], [50, 90, 99]).tolist()
    s["trunc1024_rms_db_quantiles"] = np.percentile([r["trunc1024_rms_db"] for r in irs], [50, 90, 99]).tolist()
    s["trunc1024_low_db_quantiles"] = np.percentile([r["trunc1024_low_db"] for r in irs], [1, 10, 50]).tolist()
    s["peak_ms_quantiles"] = np.percentile([r["peak_ms"] for r in irs], [50, 90, 99, 100]).tolist()
    s["len_to_-60db_ms_quantiles"] = np.percentile([r["len_to_-60db_ms"] for r in irs], [10, 50, 90, 99]).tolist()
    # features by group (guitar cabs only for size split)
    feats = ("resonance_hz", "resonance_db", "low_rolloff_hz", "high_rolloff_hz", "presence_db", "brightness_db",
             "tilt_db_per_oct", "centroid_hz")
    s["features_by_group"] = {g: {f: np.nanpercentile([r[f] for r in irs if r["group"] == g], [10, 50, 90]).round(2).tolist()
                                  for f in feats} for g in ("guitar", "bass", "acoustic", "misc", "artist")}
    s["features_by_size"] = {sz: {f: float(np.nanmedian([r[f] for r in irs if r["group"] == "guitar" and r["cab_size"] == sz]))
                                  for f in feats} | {"n": sum(1 for r in irs if r["group"] == "guitar" and r["cab_size"] == sz)}
                             for sz in ("1x10", "2x10", "1x12", "2x12", "4x10", "4x12")}
    th = cr.THIRD_OCTAVE
    s["third_oct_median_by_group"] = {"centres": np.round(th, 0).tolist(), **{
        g: np.round(np.median(cr.normalise(th, np.array([r["third_oct"] for r in irs if r["group"] == g])), axis=0), 1).tolist()
        for g in ("guitar", "bass", "acoustic", "artist")}}
    s["artist_names_with_helix"] = [r["path"] for r in rows if "helix" in r["path"].lower()]
    _jdump(s, "summary.json")
    print(json.dumps({k: v for k, v in s.items() if k not in ("dup_near_response", "stereo_left_vs_right_db")},
                     indent=1, default=_default)[:12000])
    print("near-duplicate pairs:", len(near))
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["all", "inventory", "analyze", "helix", "cluster", "estimate", "summary"])
    ap.add_argument("--n-irs", type=int, default=120, help="IRs used in the recovery experiment (estimate)")
    a = ap.parse_args(argv)
    stages = ["inventory", "analyze", "summary", "helix", "cluster", "estimate"] if a.stage == "all" else [a.stage]
    for st in stages:
        t = time.time()
        print(f"== {st}", flush=True)
        if st == "estimate":
            stage_estimate(a.n_irs)
        else:
            globals()[f"stage_{st}"]()
        print(f"== {st} done in {time.time() - t:.1f} s", flush=True)


if __name__ == "__main__":
    main()
