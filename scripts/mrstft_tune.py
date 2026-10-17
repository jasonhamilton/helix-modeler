#!/usr/bin/env python3
"""Tune `mrstft_v2` on the TRAIN split of `helix-trust-v2` (owner: tone-metrics, T096).

There is deliberately no way to point this at the test split: tuning reads train items only, and the
test split is scored once, by `scripts/loss_trust_check.py --split test`, with the chosen config.

    python scripts/mrstft_tune.py --stage stage1.json [--jobs 8] [--out cache/.../tune-stage1.json] [--ledger]
    python scripts/mrstft_tune.py --ledger-report cache/.../tune-stage1.json   # rows for a finished stage

A stage file is {"name": ..., "configs": [{...Config fields...}, ...]} (missing fields take
`mrstft_v2.V1`'s values). For every config it reports, on the `aligned` and `sep` targets (same
notes), rank-1 plain and under rule B, the audible margin, per category and per distractor kind,
and the selection objective registered in docs/DECISIONS.md (T096): the mean of rule B rank-1 over
`aligned` and `sep`; ties within 0.005 go to the higher mean p10 audible margin, then the cheaper
configuration (fewer STFT frames).

Per-resolution parts are cached across configs that share them (a part depends on the FFT size,
hop, level step, weighting, floor and silence gate, not on the set of sizes or the weights).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tonematch.metrics import mrstft_v2 as MV2
from tonematch.metrics import trust as TR

SET = ROOT / "cache" / "loss-trust" / "helix-trust-v2"
SPLIT = "train"          # fixed: never test
CONDS = ("aligned", "sep")
POOLS = ("near", "nbr", "random")


def make_config(d: dict) -> MV2.Config:
    d = dict(d)
    if "fft_sizes" in d:
        d["fft_sizes"] = tuple(d["fft_sizes"])
    return MV2.Config(**d)


def local_key(cfg: MV2.Config, n_fft: int) -> tuple:
    return (n_fft, MV2.hop_for(n_fft, cfg), cfg.level, cfg.bands, cfg.n_bands if cfg.bands != "linear" else 0,
            cfg.a_weight, cfg.floor_db, cfg.silence_db, cfg.f_lo, cfg.f_hi)


def item_job(args) -> dict:
    import loss_trust_check as LTC

    it, cfg_dicts, seed, bleed, aligned, root = args
    cfgs = [make_config(d) for d in cfg_dicts]
    rdir = Path(root) / "renders" / it["dir"]
    n = len(it["candidates"])
    ren = [np.load(rdir / f"c{k:02d}_A.npy").astype(np.float64) for k in range(n)]
    clean = ren[0].astype(np.float32)
    targets = LTC.helix_targets(clean, seed, it["item"], set(CONDS), bleed, False, aligned)
    sigs = {c: np.asarray(targets[c][0], np.float64) for c in CONDS}
    nulls = [TR.null_db(ren[0], y) for y in ren[1:]]
    L = min(min(s.size for s in sigs.values()), min(y.size for y in ren))
    sigs = {c: s[:L] for c, s in sigs.items()}
    ren = [y[:L] for y in ren]
    # level scalars per level mode
    scal: dict = {}
    for lv in {c.level for c in cfgs}:
        scal[lv] = {c: MV2.level_scale(s, lv) for c, s in sigs.items()}
        scal[lv]["cand"] = [MV2.level_scale(y, lv) for y in ren]
    # which local parts are needed, grouped by (n_fft, hop)
    need: dict[tuple[int, int], dict[tuple, MV2.Config]] = {}
    for cfg in cfgs:
        for nf in cfg.fft_sizes:
            k = local_key(cfg, nf)
            need.setdefault((nf, k[1]), {})[k] = cfg
    parts: dict[tuple, dict[str, np.ndarray]] = {}   # local key -> {cond: (n_cand, 3) array}
    t_spec = 0.0
    for (nf, hop), locs in sorted(need.items()):
        t0 = time.perf_counter()
        raw_t = {c: MV2.spectrum(s, nf, hop) for c, s in sigs.items()}
        raw_c = [MV2.spectrum(y, nf, hop) for y in ren]
        t_spec += time.perf_counter() - t0
        for k, cfg in locs.items():
            full = cfg.with_(w_sc=1.0, w_log=1.0, w_lin=1.0)
            lv = cfg.level
            out = {}
            for c in CONDS:
                pt = raw_t[c] / np.float32(scal[lv][c] ** 2)
                rows = []
                for j, pc in enumerate(raw_c):
                    p = MV2.compare(pt, pc / np.float32(scal[lv]["cand"][j] ** 2), nf, full)
                    rows.append((p["sc"], p["log"], p["lin"]))
                out[c] = np.asarray(rows)
            parts[k] = out
        del raw_t, raw_c
    res = []
    for cfg in cfgs:
        w = np.array([cfg.w_sc, cfg.w_log, cfg.w_lin])
        r = {}
        for c in CONDS:
            tot = np.mean([parts[local_key(cfg, nf)][c] @ w for nf in cfg.fft_sizes], axis=0)
            rb = TR.rank1_rule_b(tot[0], tot[1:], nulls)
            aud = ~TR.same_sound(nulls)
            kinds = {}
            for pool in POOLS:
                sel = np.array([lab.startswith(pool) for lab in [cd["label"] for cd in it["candidates"][1:]]]) & aud
                kinds[pool] = bool(np.all(tot[1:][sel] > tot[0]))
            lost = [it["candidates"][1 + k]["label"].split(":")[0] for k in np.flatnonzero(aud & (tot[1:] <= tot[0]))]
            r[c] = {**rb.as_dict(), "kinds": kinds, "lost_to": lost}
        res.append(r)
    return {"item": it["item"], "category": it["category"], "n_same_sound": int(TR.same_sound(nulls).sum()),
            "results": res, "spec_s": t_spec}


def summarise(per_item: list[dict], cfgs: list[MV2.Config]) -> list[dict]:
    out = []
    for ci, cfg in enumerate(cfgs):
        row = {"config": cfg.as_dict(), "frames_per_s": float(sum(48_000 / MV2.hop_for(n, cfg) for n in cfg.fft_sizes))}
        for c in CONDS:
            rs = [TR.Rank1(**{k: v for k, v in it["results"][ci][c].items() if k not in ("kinds", "lost_to")})
                  for it in per_item]
            s = TR.rates(rs)
            cats: dict[str, list[bool]] = {}
            for it in per_item:
                cats.setdefault(it["category"], []).append(it["results"][ci][c]["rank1_audible"])
            lost: dict[str, int] = {}
            for it in per_item:
                for k in it["results"][ci][c]["lost_to"]:
                    lost[k] = lost.get(k, 0) + 1
            s["by_category_b"] = {k: float(np.mean(v)) for k, v in sorted(cats.items())}
            s["vs_kind_b"] = {p: float(np.mean([it["results"][ci][c]["kinds"][p] for it in per_item])) for p in POOLS}
            s["audible_losses_by_kind"] = lost
            row[c] = s
        row["objective"] = float(np.mean([row[c]["rank1_audible_only"] for c in CONDS]))
        row["margin_p10_mean"] = float(np.mean([row[c]["margin_audible_p10"] for c in CONDS]))
        out.append(row)
    return out


def best(rows: list[dict]) -> int:
    top = max(r["objective"] for r in rows)
    near = [i for i, r in enumerate(rows) if r["objective"] >= top - 0.005]
    return max(near, key=lambda i: (round(rows[i]["margin_p10_mean"], 3), -rows[i]["frames_per_s"]))


def ledger_rows(rep: dict, note: str = "") -> None:
    """loss-trust rows (native, the set's dataset) for a stage's selected config: rule B rank-1 and
    plain rank-1 on `aligned` and `sep`, TRAIN split. Several configs were tried; the notes say so."""
    import subprocess

    r = rep["rows"][rep["best"]]
    cfg = {k: v for k, v in r["config"].items() if v != MV2.V1.as_dict()[k]}
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "tone-metrics")}
    for c in CONDS:
        h = r[c]
        cats = "/".join(f"{k}={v:.2f}" for k, v in h["by_category_b"].items())
        notes = (f"term=mrstft_v2 TRAIN split (tuning; not admission evidence) stage={rep['stage']} "
                 f"selected config {rep['best']} of {len(rep['rows'])} tried subset={c}/same_notes "
                 f"rank1_plain={h['rank1_rate']:.3f} rank1_audible_rule_b={h['rank1_audible_only']:.3f} "
                 f"margin_audible_p10={h['margin_audible_p10']:.3f} objective={r['objective']:.3f} "
                 f"by_category_b={cats} items={rep['items']} config_vs_v1={json.dumps(cfg, separators=(';', '='))}"
                 + (f" {note}" if note else "")).replace(",", ";")
        for metric, v in (("rank1_rate_audible", h["rank1_audible_only"]), ("rank1_rate", h["rank1_rate"])):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "loss-trust",
                            "--engine", "native", "--dataset", rep["dataset"], "--metric", metric,
                            "--value", f"{v:.4g}", "--wall-clock-s", f"{rep['wall_s']:.1f}", "--notes", notes],
                           check=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage", type=Path)
    ap.add_argument("--ledger", action="store_true", help="append loss-trust rows for the selected config")
    ap.add_argument("--ledger-report", type=Path, default=None,
                    help="append the rows for an already written tune report, then exit")
    ap.add_argument("--note", default="", help="extra text for the ledger notes")
    ap.add_argument("--set-dir", type=Path, default=SET)
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--items", type=int, default=None, help="first N train items (smoke)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.ledger_report:
        ledger_rows(json.loads(a.ledger_report.read_text()), a.note)
        return 0
    if a.stage is None:
        ap.error("--stage is required (or --ledger-report)")
    st = json.loads(a.stage.read_text())
    cfg_dicts = [{**MV2.V1.as_dict(), **d} for d in st["configs"]]
    cfgs = [make_config(d) for d in cfg_dicts]
    mf = json.loads((a.set_dir / "manifest.json").read_text())
    items = [it for it in mf["items"] if it.get("split") == SPLIT]
    assert items and all(it["split"] == "train" for it in items)
    if a.items:
        items = items[: a.items]
    t0 = time.perf_counter()
    jobs = [(it, cfg_dicts, mf["seed"], mf.get("sep_bleed_db", -12.0), mf.get("aligned"), str(a.set_dir))
            for it in items]
    with ProcessPoolExecutor(a.jobs) as ex:
        per_item = list(ex.map(item_job, jobs))
    wall = time.perf_counter() - t0
    rows = summarise(per_item, cfgs)
    b = best(rows)
    rep = {"dataset": mf["dataset"], "split": SPLIT, "stage": st.get("name"), "items": len(items),
           "wall_s": round(wall, 1), "best": b, "rows": rows}
    out = a.out or a.set_dir / f"tune-{st.get('name', a.stage.stem)}.json"
    out.write_text(json.dumps(rep, indent=1))
    print(f"{st.get('name')}: {len(cfgs)} configs on {len(items)} TRAIN items in {wall:.0f} s -> {out}")
    print(f"{'#':>3s} {'obj':>6s} {'al_r1':>6s} {'al_B':>6s} {'sep_r1':>6s} {'sep_B':>6s} {'mB10al':>7s} "
          f"{'mB10sp':>7s} {'fr/s':>6s}  config")
    for i, r in enumerate(rows):
        c = r["config"]
        short = {k: v for k, v in c.items() if v != MV2.V1.as_dict()[k]}
        print(f"{i:3d} {r['objective']:6.3f} {r['aligned']['rank1_rate']:6.3f} {r['aligned']['rank1_audible_only']:6.3f} "
              f"{r['sep']['rank1_rate']:6.3f} {r['sep']['rank1_audible_only']:6.3f} "
              f"{r['aligned']['margin_audible_p10']:7.3f} {r['sep']['margin_audible_p10']:7.3f} "
              f"{r['frames_per_s']:6.0f}  {short}{'  <== best' if i == b else ''}")
    r = rows[b]
    for c in CONDS:
        print(f"best {c}: by category {r[c]['by_category_b']} vs kind {r[c]['vs_kind_b']} "
              f"audible losses {r[c]['audible_losses_by_kind']}")
    if a.ledger:
        ledger_rows(rep, a.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
