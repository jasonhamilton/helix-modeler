#!/usr/bin/env python3
# historical, Mother-specific
"""T183: end-to-end check of the no-DI drive matcher (T178 flow) on held-out single-drive targets from real DIs.

TEST SETTINGS, NOT ADOPTED (human 2026-10-18 "proceed with any testing"): measured per-model tone tolerances
(`suggested_tol` of cache/drive-maps/knob_sensitivity.json), k = 15 families (cache/drive-maps/drive-families-v1.json), and the second-choice
family for models whose member_confidence is under 0.5. Measurement only; no song audio; the targets are
rendered from guitar DI excerpts (Guitar-TECHS P1/P2, the human's own dry takes) and the matcher renders through
a DIFFERENT excerpt (another player or another take): the blind condition.

    .venv/bin/python scripts/nodi_drive_e2e.py plan
    .venv/bin/python scripts/nodi_drive_e2e.py render            # one Native host: the 24 target audio files
    .venv/bin/python scripts/nodi_drive_e2e.py match --path coarse_grid --procs 2
    .venv/bin/python scripts/nodi_drive_e2e.py match --path inversion --procs 2
    .venv/bin/python scripts/nodi_drive_e2e.py match --path full --procs 2 --subset 6 --wall-min 6
    .venv/bin/python scripts/nodi_drive_e2e.py analyse
    .venv/bin/python scripts/nodi_drive_e2e.py clean
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

SR = 48000
SEED = 183
EXC_S = 5.0
LEAD_S = 0.5
N_TARGETS = 24
OUT = ROOT / "cache" / "matches" / "t183"
AUDIO = OUT / "audio"
REPORT = ROOT / "reports" / "local" / "t183"
T181_PLAN = ROOT / "cache" / "distortion-step4" / "plan.json"
from tonematch import drive_maps as _DM  # noqa: E402

FAMILIES = _DM.read_path(ROOT, _DM.FAMILIES_NAME)
SENS = _DM.read_path(ROOT, _DM.SENSITIVITY_NAME)
TAKES = ROOT / "data" / "songs" / "mother-playalong" / "takes"
GT = ROOT / "data" / "di" / "Guitar-TECHS"
# slot order matters: the matcher DI of slot i is slot (i + 3) % 8, always another file
SLOTS = [("P1a", "gt", "P1_scales"), ("P2a", "gt", "P2_chords"), ("H1", "human", "112611"), ("H2", "human", "112935"),
         ("P1b", "gt", "P1_chords"), ("P2b", "gt", "P2_scales"), ("H3", "human", "113158"), ("H4", "human", "113158")]


def read_mono(p: Path) -> np.ndarray:
    import soundfile as sf
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    assert sr == SR, (p, sr)
    return x.mean(axis=1).astype(np.float32)


def write_wav(p: Path, x) -> None:
    import soundfile as sf
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), np.asarray(x, np.float32), SR, subtype="FLOAT")


def tol_map() -> Path:
    """The test-setting tolerance map: knob_sensitivity.json with `suggested_tol` written into the keys search reads."""
    raw = json.loads(SENS.read_text())
    models = raw.get("models", raw)
    out = {}
    for m, v in models.items():
        if not isinstance(v, dict):
            continue
        e = {k: v[k] for k in ("drive_knobs", "tone_knobs") if k in v}
        st = v.get("suggested_tol") or {}
        for k in ("tone_tol", "tone_end_tol", "end_zone"):
            if k in st:
                e[k] = float(st[k])
        out[m] = e
    p = OUT / "tol_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"models": out, "note": "T183 TEST SETTING: suggested_tol applied explicitly, not adopted"}, indent=1))
    return p


# ------------------------------------------------------------------------------------------- plan

def cmd_plan(a) -> None:
    import distortion_step4 as D4          # T181's helpers (read only): pool, knob semantics

    rng = np.random.default_rng(SEED)
    models, sem = D4.pool_models()
    t181 = json.loads(T181_PLAN.read_text()) if T181_PLAN.exists() else {"excerpts": [], "targets": []}
    taken = [(e["path"], e["start"], e["start"] + e["n"]) for e in t181["excerpts"]]
    n = int(EXC_S * SR)
    exc = []
    for sid, kind, sel in SLOTS:
        if kind == "gt":
            files = sorted((GT / sel / "audio" / "directinput").glob("*.wav"))
        else:
            files = sorted(TAKES.glob(f"20261018-{sel}_take*_dry.wav"))
        for _ in range(500):
            f = files[int(rng.integers(len(files)))]
            x = read_mono(f)
            if len(x) < n + 2 * SR:
                continue
            s = int(rng.integers(int(LEAD_S * SR), len(x) - n))
            seg = x[s:s + n]
            peak = 20 * np.log10(np.max(np.abs(seg)) + 1e-12)
            rms = 10 * np.log10(np.mean(seg.astype(float) ** 2) + 1e-20)
            rel = str(f.relative_to(ROOT))
            clash = any(rel == p and s < b and s + n > a_ for p, a_, b in taken)
            if peak < -26 or rms < -50 or clash:
                continue
            taken.append((rel, s, s + n))
            exc.append({"id": sid, "path": rel, "start": s, "n": n, "peak_dbfs": float(peak), "rms_dbfs": float(rms),
                        "player": "human" if kind == "human" else sel[:2]})
            break
        else:
            raise SystemExit(f"no excerpt for {sid}")
    # matcher DI of slot i = slot (i+3)%8: another file by construction (checked)
    for i, e in enumerate(exc):
        e["matcher_slot"] = exc[(i + 3) % 8]["id"]
        assert exc[(i + 3) % 8]["path"] != e["path"], (e["id"], "matcher DI is the same file")
    old = {(t["model"], tuple(sorted(t["cc"].items()))) for t in t181["targets"]}
    from tonematch.metrics import distortion_step3 as S3
    pick = [str(m) for m in rng.choice(models, N_TARGETS, replace=False)]
    dcc = [c for c in range(128) if c not in D4.DRIVE_MEASURED]
    tcc = [c for c in range(128) if c not in D4.TONE_MEASURED]
    one = [c for c in range(128) if c not in set(S3.ONE_KNOB)]
    targets = []
    for i, m in enumerate(pick):
        s = sem[m]
        while True:
            cc = {s["drive"]: int(rng.choice(one))} if s["tone"] is None else \
                {s["drive"]: int(rng.choice(dcc)), s["tone"]: int(rng.choice(tcc))}
            if (m, tuple(sorted(cc.items()))) not in old:
                break
        targets.append({"id": f"u{i:02d}", "model": m, "excerpt": exc[i % 8]["id"], "cc": cc, "drive": s["drive"],
                        "tone": s["tone"]})
    sub = sorted(int(i) for i in rng.choice(N_TARGETS, 6, replace=False))
    plan = {"version": "nodi-drive-e2e-v1", "seed": SEED, "excerpt_s": EXC_S, "lead_s": LEAD_S, "excerpts": exc,
            "targets": targets, "old_search_subset": [targets[i]["id"] for i in sub]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))
    tol_map()
    print(json.dumps({"excerpts": [(e["id"], e["path"].split("/")[-1], e["start"]) for e in exc],
                      "targets": [(t["id"], t["model"].replace("HD2_", ""), t["excerpt"], t["cc"]) for t in targets],
                      "subset": plan["old_search_subset"]}, indent=0))


def load_plan() -> dict:
    return json.loads((OUT / "plan.json").read_text())


# ------------------------------------------------------------------------------------------- render

def cmd_render(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    import match_block as MB
    from tonematch.catalog import load_catalog
    from tonematch.engines import native as nat
    from tonematch.engines import native_library as lib
    import distortion_step4 as D4

    plan = load_plan()
    catalog = load_catalog()
    exc = {e["id"]: e for e in plan["excerpts"]}
    lead = int(LEAD_S * SR)
    for e in plan["excerpts"]:                 # the matcher's DI (no lead-in) and the target DI (with lead-in)
        x = read_mono(ROOT / e["path"])
        write_wav(AUDIO / f"dry_{e['id']}.wav", x[e["start"]:e["start"] + e["n"]])
    engine = MB.make_engine(144_000)
    try:
        for t in plan["targets"]:
            if (AUDIO / f"{t['id']}_target.wav").exists():
                continue
            guard.wait(lambda d: print(f"busy cores {d:.1f} > {guard.gate.pause_above:.1f}: pausing", flush=True), poll_s=10)
            e = exc[t["excerpt"]]
            x = read_mono(ROOT / e["path"])[e["start"] - lead:e["start"] + e["n"]]
            p, vals = D4._preset(lib, nat, catalog, t["model"], t["cc"])
            t["params"] = vals
            engine.load(p)
            y = np.asarray(engine.render(x), np.float32)[lead:]
            write_wav(AUDIO / f"{t['id']}_target.wav", y)
            print(f"{t['id']} {t['model']} {vals} rms {20*np.log10(np.sqrt(np.mean(y.astype(float)**2))+1e-12):.1f} dB", flush=True)
    finally:
        engine.close()
    (OUT / "plan.json").write_text(json.dumps(plan, indent=1))


# ------------------------------------------------------------------------------------------- match

def match_cmd(t: dict, plan: dict, path: str, wall_min: float) -> tuple[list[str], Path]:
    exc = {e["id"]: e for e in plan["excerpts"]}
    mdi = exc[t["excerpt"]] if path == "inversion_samedi" else exc[exc[t["excerpt"]]["matcher_slot"]]
    out = OUT / {"coarse_grid": "cg", "inversion": "inv", "inversion_samedi": "invsame", "full": "full"}[path] / t["id"]
    cmd = [sys.executable, str(ROOT / "scripts" / "match_block.py"), "--target-wav", str(AUDIO / f"{t['id']}_target.wav"),
           "--dry-wav", str(AUDIO / f"dry_{mdi['id']}.wav"), "--song", f"t183-{t['id']}", "--category", "drive",
           "--out", str(out), "--lean", "--knob-tol-map", str(OUT / "tol_map.json"), "--seed", str(SEED)]
    if path == "full":
        cmd += ["--workers", "2", "--wall-min", str(wall_min)]
    else:
        cmd += ["--workers", "1", "--shortlist", "afxrep", "--shortlist-n", "8", "--families", str(FAMILIES),
                "--second-choice", "0.5", "--knob-path", "inversion" if path == "inversion_samedi" else path]
    return cmd, out


def cmd_match(a) -> None:
    from tonematch.engines.host_budget import LoadGuard
    guard = LoadGuard(a.max_load)
    plan = load_plan()
    ts = plan["targets"]
    if a.subset:
        ts = [t for t in ts if t["id"] in set(plan["old_search_subset"])]
    if a.only:
        ts = [t for t in ts if t["id"] in a.only.split(",")]
    procs: list[tuple[subprocess.Popen, dict, Path, float]] = []
    todo = [t for t in ts if not (match_cmd(t, plan, a.path, a.wall_min)[1] / "result.json").exists()]
    print(f"{len(todo)} matches to run, path {a.path}, up to {a.procs} at a time", flush=True)
    while todo or procs:
        procs = [(p, t, o, t0) for p, t, o, t0 in procs if p.poll() is None or print(f"done {t['id']} rc={p.returncode} {time.time()-t0:.0f}s", flush=True)]
        while todo and len(procs) < a.procs:
            if guard.paused():
                break
            t = todo.pop(0)
            cmd, out = match_cmd(t, plan, a.path, a.wall_min)
            log = open(OUT / f"{a.path}_{t['id']}.log", "w")
            procs.append((subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT), t, out, time.time()))
            time.sleep(3)
        time.sleep(5)


# ------------------------------------------------------------------------------------------- analyse

def cmd_analyse(a) -> None:
    from tonematch.search.knob_tol import make_knob_tol

    plan = load_plan()
    from tonematch.search import shortlist as SL
    fam = json.loads(FAMILIES.read_text())["model_to_family"]
    FAMS = SL.load_families(FAMILIES)
    kt = make_knob_tol(0.15, map_path=OUT / "tol_map.json")
    import match_block as MB
    from tonematch.catalog import load_catalog
    pool = MB.category_pool("drive", load_catalog())
    rng = {m: {n: (lo, hi) for n, lo, hi, _ in MB.make_blocks({m: pool[m]}, "drive", load_catalog())[m].free} for m in
           {t["model"] for t in plan["targets"]}}
    rows = []
    for path in ("coarse_grid", "inversion", "inversion_samedi", "full"):
        d = OUT / {"coarse_grid": "cg", "inversion": "inv", "inversion_samedi": "invsame", "full": "full"}[path]
        for t in plan["targets"]:
            f = d / t["id"] / "result.json"
            if not f.exists():
                continue
            r = json.loads(f.read_text())
            fin = r["finals"]
            sl = r.get("shortlist") or {}
            best = fin[0]
            truth = t["params"]
            lo_hi = rng[t["model"]]
            unit = lambda n, v: (float(v) - lo_hi[n][0]) / (lo_hi[n][1] - lo_hi[n][0])   # noqa: E731 - knob position 0..1
            pos = lambda n: unit(n, truth[n])    # noqa: E731
            ktm = kt.for_model(t["model"])
            ok_model = best["model"] == t["model"]
            drive_err = abs(unit(t["drive"], best["values"].get(t["drive"], np.nan)) - pos(t["drive"])) if ok_model else None
            tone_err = tone_tol = None
            if ok_model and t["tone"]:
                tone_err = abs(unit(t["tone"], best["values"].get(t["tone"], np.nan)) - pos(t["tone"]))
                tone_tol = ktm.tolerance(t["tone"], pos(t["tone"]))
            ranked = [x["model"] for x in fin]
            tone_tol_t = ktm.tolerance(t["tone"], pos(t["tone"])) if t["tone"] else 0.0
            # diagnostic, not the flow's output: the AFx order itself (the flow re-ranks by the provisional loss)
            afx_top = [x["model"] for x in sl.get("ranked", [])]
            afx_pick = afx_top[0] if afx_top else None
            fv = {x["model"]: x["values"] for x in fin}.get(afx_pick) if afx_pick == t["model"] else None
            a_drive = None if fv is None else abs(unit(t["drive"], fv.get(t["drive"], np.nan)) - pos(t["drive"]))
            a_tone = None if fv is None or not t["tone"] else abs(unit(t["tone"], fv.get(t["tone"], np.nan)) - pos(t["tone"]))
            rows.append({
                "path": path, "id": t["id"], "model": t["model"], "family": fam.get(t["model"]),
                "top8": t["model"] in sl.get("top", []) if sl else None,
                "in_expanded": t["model"] in sl.get("models", []) if sl else None,
                "family_in_expanded": any(fam.get(m) == fam.get(t["model"]) for m in sl.get("models", [])) if sl else None,
                "second_choice_rescue": (t["model"] in sl.get("models", []) and t["model"] not in
                                         SL.expand_families(sl["top"], FAMS, set(sl["models"]))) if sl else None,
                "afx_rank": ([x["model"] for x in sl["ranked"]].index(t["model"]) + 1) if sl and sl.get("ranked") else None,
                "n_shortlist": len(sl.get("models", [])) if sl else None,
                "afx_top1_model": afx_pick == t["model"], "afx_top3_model": t["model"] in afx_top[:3],
                "afx_top1_family": bool(afx_pick) and fam.get(afx_pick) == fam.get(t["model"]),
                "afx_drive_ok": None if a_drive is None else bool(a_drive <= 0.15),
                "afx_tone_ok": None if a_tone is None else bool(a_tone <= tone_tol_t + 1e-9),
                "top1_model": ok_model, "top3_model": t["model"] in ranked[:3], "top1_family": fam.get(best["model"]) == fam.get(t["model"]),
                "best": best["model"], "n_models": r["n_models"], "n_candidates": len(fin),
                "drive_err": drive_err, "tone_err": tone_err, "tone_tol": tone_tol,
                "drive_ok": None if drive_err is None else bool(drive_err <= 0.15),
                "tone_ok": None if tone_err is None else bool(tone_err <= tone_tol + 1e-9),
                "renders": (r["renders"] - sl.get("renders", 0)) if sl else r["renders"],     # these runs predate the double-count fix
                "second_choice_added": len(sl.get("second_choice_added", [])) if sl else None, "wall_s": r["wall_s"], "values": best["values"], "truth": truth})
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "rows.json").write_text(json.dumps(rows, indent=1, default=float))
    (REPORT / "summary.json").write_text(json.dumps(summarise(rows), indent=1))
    print(json.dumps(summarise(rows), indent=1))


def summarise(rows: list[dict]) -> dict:
    out = {}
    for path in ("coarse_grid", "inversion", "inversion_samedi", "full"):
        rs = [r for r in rows if r["path"] == path]
        if not rs:
            continue
        n = len(rs)
        s = {"n": n}
        for k in ("top8", "in_expanded", "family_in_expanded", "top1_model", "top3_model", "top1_family",
                  "afx_top1_model", "afx_top3_model", "afx_top1_family", "afx_drive_ok", "afx_tone_ok"):
            v = [r[k] for r in rs if r[k] is not None]
            s[k] = f"{sum(v)}/{len(v)}" if v else None
        d = [r for r in rs if r["drive_ok"] is not None]
        ar = [r["afx_rank"] for r in rs if r["afx_rank"] is not None]
        if ar:
            s["afx_top1"] = f"{sum(x == 1 for x in ar)}/{len(ar)}"
            s["afx_rank_median"] = float(np.median(ar))
            s["second_choice_targets_widened"] = sum(bool(r["second_choice_added"]) for r in rs)
            s["second_choice_added_mean"] = float(np.mean([r["second_choice_added"] for r in rs]))
            s["second_choice_rescues"] = sum(bool(r["second_choice_rescue"]) for r in rs)
            s["shortlist_size_median"] = float(np.median([r["n_shortlist"] for r in rs]))
        s["drive_ok_when_model_right"] = f"{sum(r['drive_ok'] for r in d)}/{len(d)}"
        tn = [r for r in rs if r["tone_ok"] is not None]
        s["tone_ok_when_model_right"] = f"{sum(r['tone_ok'] for r in tn)}/{len(tn)}"
        s["renders_median"] = float(np.median([r["renders"] for r in rs]))
        s["wall_s_median"] = float(np.median([r["wall_s"] for r in rs]))
        out[path] = s
    return out


def cmd_ledger(a) -> None:
    summ = json.loads((REPORT / "summary.json").read_text())
    rows = json.loads((REPORT / "rows.json").read_text())
    base = ("T183 TEST SETTINGS, not adopted: measured per-model tone tolerances, k=15 families, second-choice family under "
            "member_confidence 0.5. 24 held-out random single-drive targets from real DI, matcher on a different DI. ")

    def put(metric, value, notes, renders=None, wall=None):
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
               "--dataset", "nodi-drive-e2e-v1", "--metric", metric, "--value", f"{value:.4f}", "--notes", base + notes]
        if renders is not None:
            cmd += ["--renders", str(int(renders))]
        if wall is not None:
            cmd += ["--wall-clock-s", f"{wall:.1f}"]
        subprocess.run(cmd, check=True, cwd=ROOT, env={**os.environ, "TONEMATCH_AGENT": "search"})

    frac = lambda t: (lambda a_, b_: a_ / b_)(*map(int, t.split("/")))   # noqa: E731
    for path, s in summ.items():
        rs = [r for r in rows if r["path"] == path]
        ren, wall = s["renders_median"], s["wall_s_median"]
        for k in ("top8", "in_expanded", "family_in_expanded", "top1_model", "top3_model", "top1_family", "afx_top1_model",
                  "drive_ok_when_model_right", "tone_ok_when_model_right"):
            if s.get(k) and not s[k].endswith("/0") and not (path == "full" and k.startswith("afx")):
                put(f"{path}_{k}", frac(s[k]), f"{s[k]} targets; path {path}", ren, wall)
        put(f"{path}_renders_per_match_median", ren, f"path {path}, n={s['n']}", ren, wall)
    print("ledger rows appended")


def cmd_clean(a) -> None:
    n = 0
    for f in list(AUDIO.glob("*.wav")) + list(OUT.glob("*/*/listen/*")):
        f.unlink()
        n += 1
    print(f"deleted {n} audio files")


def main() -> None:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("plan")
    r = sp.add_parser("render")
    r.add_argument("--max-load", type=float, default=None,
                   help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    m = sp.add_parser("match")
    m.add_argument("--path", choices=["coarse_grid", "inversion", "inversion_samedi", "full"], required=True)
    m.add_argument("--procs", type=int, default=2)
    m.add_argument("--subset", action="store_true", help="the 6-target subset (the old full search)")
    m.add_argument("--only")
    m.add_argument("--wall-min", type=float, default=6.0)
    m.add_argument("--max-load", type=float, default=None,
                   help="pause above this many busy cores (default 0.95 x cores; resume at 0.85/0.95 of it; T193)")
    sp.add_parser("analyse")
    sp.add_parser("ledger")
    sp.add_parser("clean")
    a = ap.parse_args()
    {"plan": cmd_plan, "render": cmd_render, "match": cmd_match, "analyse": cmd_analyse, "ledger": cmd_ledger, "clean": cmd_clean}[a.cmd](a)


if __name__ == "__main__":
    main()
