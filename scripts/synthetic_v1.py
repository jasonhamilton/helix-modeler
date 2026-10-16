#!/usr/bin/env python3
"""synthetic-v1: the Gate 2 synthetic recovery set and its harness (owner: benchmark, T073).

Known presets are single blocks from the Native block library (one captured template per Stomp
model, `tonematch.engines.native_library`). Each item renders one block at known parameters on the
CC grid through a clean guitar DI and asks the search to recover the category, the model and the
settings from the sound alone.

  generate   offline, no Native: draw the pool, the items, the DI excerpts and the neighbours;
             writes cache/synthetic-v1/{manifest,labels}.json and di/*.wav (local, gitignored: model
             ids and DI excerpts) and the committable fixtures/synthetic/manifest.json (no ids)
  run        Native, ONE host: render each target, run the search at a fixed budget, score
             block-type F1, model accuracy, parameter MAE and rank-1 of the true preset against its
             neighbours on held-out notes; writes per-item JSON (resumable), a summary and, with
             --ledger, kind=benchmark rows
  summarize  recompute the summary of an existing run directory (no Native)

Set design (rulings in docs/DECISIONS.md, 2026-10-16):
  * categories: drive, delay, reverb, comp (dynamics), eq, mod, amp, cab; "other" is left out (no
    Native stand-in ruling covers it);
  * left out: time-variant blocks, `native_unavailable()` models, Bitcrusher, reverbs other than
    Room, Plate, '63 Spring, Chamber, Hall, modulation other than tremolo, 70s Chorus, Bubble
    Vibrato, MM4 Analog Chorus (the lead's Native stand-in list), routing and pan-twin models;
  * parameters: continuous, CC-verified, on the CC grid k/127 of the template range (read from the
    template, so native-engine's narrowed ranges apply); Level, ChVol, compressor Gain, Hum, Ripple,
    cab EarlyReflections, Spread, Stereo are not varied (frozen or output level);
  * the search pool is a seeded sample of POOL_PER_CATEGORY models per category; every item's true
    model is in the pool, so chance model accuracy is 1/pool size of its category;
  * DI: Guitar-TECHS excerpts (data/di, CC BY 4.0). A is the fit clip, C another passage of the same
    recording (held-out notes). No song audio.

Loss weights are a parameter (default PROVISIONAL_WEIGHTS, not admitted). Every run with
unadmitted weights is labelled "provisional, not gate evidence". A run of fewer than all items
writes dataset `synthetic-v1-n<k>`, never `synthetic-v1`.
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

SR = 48_000
DATASET = "synthetic-v1"
SET_DIR = ROOT / "cache" / "synthetic-v1"
FIXTURE = ROOT / "fixtures" / "synthetic" / "manifest.json"
SEED = 20261016
CLIP_S = 10.0
REF_LOUD_DBFS = -26.7          # loud-frame level of the parity DI (same convention as helix-trust-v1)
CATEGORIES = ("drive", "delay", "reverb", "comp", "eq", "mod", "amp", "cab")
ITEMS_PER_CATEGORY = 12
POOL_PER_CATEGORY = 6
N_NEIGHBOURS = 24
N_NEAR = 9
NEAR_DELTA = (0.12, 0.30)
U_RANGE = (0.10, 0.90)
NOT_VARIED = {"Level", "ChVol", "Hum", "Ripple", "EarlyReflections", "Spread", "Stereo"}
NOT_VARIED_IN = {"comp": {"Gain"}}
REVERBS = ("HD2_ReverbRoom", "HD2_ReverbPlate", "HD2_Reverb63Spring", "HD2_ReverbChamber", "HD2_ReverbHall")
MOD_KEEP = ("HD2_Chorus70sChorus", "HD2_VibratoBubbleVibrato", "HD2_MM4AnalogChorus")
SAME_SOUND_DB = -30.0          # a neighbour that nulls this far against the truth is the same sound
MAX_MACHINE_HOSTS = 6
TOLERANCE = {"block_f1": 0.02, "model_acc": 0.02, "param_mae": 0.01, "rank1_rate": 0.02,
             "rank1_rate_audible": 0.02, "loss_heldout": 0.5, "heldout_ratio": 0.05}
LOWER_BETTER = ("param_mae", "loss_heldout", "heldout_ratio")
TARGETS = {"block_f1": 0.85, "model_acc": 0.80, "param_mae": 0.15, "rank1_rate": 0.95}  # working, not frozen


def now_s() -> float:
    return time.perf_counter()


# --------------------------------------------------------------------------- the set (offline)

def eligible(model_id: str, category: str, tv: set[str]) -> bool:
    if model_id in tv:
        return False
    if category == "drive":
        return "Bitcrusher" not in model_id
    if category == "reverb":
        return model_id in REVERBS
    if category == "mod":
        return "Tremolo" in model_id or model_id.endswith("PatternTrem") or model_id in MOD_KEEP
    return True


def free_params(model_id: str, tpl, catalog: dict, category: str) -> list[tuple[str, float, float, float]]:
    """(name, lo, hi, default) of the continuous, CC-verified parameters of a library block."""
    params = catalog["models"][model_id].get("params") or {}
    out = []
    for ref in sorted(tpl.map):
        slot, name = ref.split(":", 1)
        if slot != "0" or name.startswith("@") or ref not in tpl.verified:
            continue
        if name in NOT_VARIED or name in NOT_VARIED_IN.get(category, ()):
            continue
        pd = params.get(name)
        if not pd or pd.get("kind") != "continuous":
            continue
        lo, hi = (float(x) for x in tpl.ranges.get(ref, [0.0, 1.0]))
        if hi <= lo:
            continue
        d = pd.get("default")
        out.append((name, lo, hi, float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else lo))
    return out


def grid_value(lo: float, hi: float, k: int) -> float:
    """The value at CC step k of 0..127 over [lo, hi] (the ParamSpace grid the search snaps to)."""
    return float(lo + (k / 127.0) * (hi - lo))


def draw_knobs(rng: np.random.Generator, free) -> dict[str, int]:
    return {n: int(np.clip(round(rng.uniform(*U_RANGE) * 127), 0, 127)) for n, *_ in free}


def near_knobs(rng: np.random.Generator, ks: dict[str, int]) -> dict[str, int]:
    names = sorted(ks)
    m = 1 if len(names) < 3 else int(rng.integers(1, 3))
    out = dict(ks)
    for n in rng.choice(names, size=min(m, len(names)), replace=False):
        d = rng.uniform(*NEAR_DELTA) * (1 if rng.random() < 0.5 else -1)
        u = ks[n] / 127 + d
        u = -u if u < 0 else (2 - u if u > 1 else u)
        out[n] = int(np.clip(round(u * 127), 0, 127))
        if out[n] == ks[n]:
            out[n] = min(127, ks[n] + 16) if ks[n] < 64 else ks[n] - 16
    return out


def build_pools(seed: int = SEED, library=None, catalog=None, tv=None) -> dict:
    """category -> {model: free params}: the seeded pool the search chooses from."""
    from tonematch.catalog import load_catalog, native_unavailable
    from tonematch.engines import native_library as NL

    catalog = catalog or load_catalog()
    idx = NL.load_index()["models"]
    bad = set(native_unavailable())
    tv = {m for m, e in idx.items() if e.get("time_variant")} | bad
    rng = np.random.default_rng([seed, 1])
    pools: dict = {}
    for c in CATEGORIES:
        names = sorted(m for m, e in idx.items() if e["category"] == c and eligible(m, c, tv)
                       and not NL.is_routing(m))
        usable = {}
        for m in names:
            tpl = NL.template_for(m)
            if tpl is None:
                continue
            fp = free_params(m, tpl, catalog, c)
            if fp:
                usable[m] = fp
        keys = sorted(usable)
        pick = sorted(rng.choice(len(keys), size=min(POOL_PER_CATEGORY, len(keys)), replace=False))
        pools[c] = {keys[i]: usable[keys[i]] for i in pick}
    return pools


def second_passage(rng, runs, ea, seconds):
    cands = [r for r in runs if r["path"] == ea["path"] and abs(r["start_s"] - ea["start_s"]) >= seconds]
    same = bool(cands)
    if not cands:
        folder = ea["path"].rsplit("/", 1)[0]
        cands = [r for r in runs if r["path"] != ea["path"] and r["path"].rsplit("/", 1)[0] == folder]
    if not cands:
        return None
    return dict(cands[int(rng.integers(len(cands)))], same_file=same)


def generate(seed: int = SEED, out: Path = SET_DIR) -> dict:
    import helix_trust_set as HT
    import soundfile as sf

    pools = build_pools(seed)
    rng = np.random.default_rng([seed, 2])
    n_items = ITEMS_PER_CATEGORY * len(CATEGORIES)
    runs = HT.usable_runs(CLIP_S)
    exc = HT.pick_excerpts(rng, n_items, CLIP_S)
    (out / "di").mkdir(parents=True, exist_ok=True)
    items = []
    order = {c: list(rng.permutation(len(pools[c]))) for c in CATEGORIES}
    for j in range(ITEMS_PER_CATEGORY):
        for c in CATEGORIES:
            i = len(items)
            models = list(pools[c])
            model = models[order[c][j % len(models)]]
            free = pools[c][model]
            ks = draw_knobs(rng, free)
            ea = exc[i]
            ec = None
            for _ in range(len(exc)):
                ec = second_passage(rng, runs, ea, CLIP_S)
                if ec:
                    break
            if ec is None:
                raise SystemExit(f"no second passage for {ea['path']}")
            dis = {}
            for tag, e in (("A", ea), ("C", ec)):
                x, g = HT.load_excerpt(e, CLIP_S, REF_LOUD_DBFS)
                f = out / "di" / f"item{i:03d}_{tag}.wav"
                sf.write(str(f), x, SR, subtype="FLOAT")
                dis[tag] = {**e, "gain_db": round(g, 2), "file": str(f.relative_to(ROOT))}
            nbrs = [{"label": "near", "model": model, "knobs": near_knobs(rng, ks)} for _ in range(N_NEAR)]
            same_cat = [m for m in models if m != model]
            for m in same_cat:
                nbrs.append({"label": "same_category", "model": m, "knobs": draw_knobs(rng, pools[c][m])})
            others = [(oc, om) for oc in CATEGORIES if oc != c for om in pools[oc]]
            while len(nbrs) < N_NEIGHBOURS:
                oc, om = others[int(rng.integers(len(others)))]
                nbrs.append({"label": f"random:{oc}", "model": om, "knobs": draw_knobs(rng, pools[oc][om])})
            items.append({
                "id": f"i{i:03d}", "category": c, "model": model, "knobs": ks,
                "values": {n: grid_value(lo, hi, ks[n]) for n, lo, hi, _ in free},
                "ranges": {n: [lo, hi] for n, lo, hi, _ in free}, "di": dis, "neighbours": nbrs[:N_NEIGHBOURS]})
    from tonematch.engines import native_library as NL

    man = {
        "name": DATASET, "version": 1, "seed": seed, "sample_rate": SR, "clip_seconds": CLIP_S,
        "engine": "native", "library_version": NL.VERSION, "categories": list(CATEGORIES),
        "items": len(items), "items_per_category": ITEMS_PER_CATEGORY,
        "pool": {c: {m: [f[0] for f in fp] for m, fp in p.items()} for c, p in pools.items()},
        "excluded": {"other": "no Native stand-in ruling covers it (DECISIONS 2026-10-16)",
                     "time_variant": "flagged in the library index", "bitcrusher": "Stomp checks it",
                     "reverb": "all but Room, Plate, 63 Spring, Chamber, Hall",
                     "mod": "all but tremolo, 70s Chorus, Bubble Vibrato, MM4 Analog Chorus"},
        "not_varied": sorted(NOT_VARIED), "ref_loud_dbfs": REF_LOUD_DBFS, "neighbours": N_NEIGHBOURS,
        "tolerance": TOLERANCE, "targets_working": TARGETS,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    (out / "labels.json").write_text(json.dumps(items, indent=1))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture_manifest(man), indent=2) + "\n")
    return man


def fixture_manifest(man: dict) -> dict:
    """Committable description: how the set is regenerated, with no Line 6 model ids."""
    return {
        "name": man["name"], "version": 1, "seed": man["seed"], "sample_rate": SR,
        "clip_seconds": man["clip_seconds"], "engine": "native",
        "regenerate": "python scripts/synthetic_v1.py generate (needs the user's Native block library "
                      "and data/di; model ids are read at generation time and kept in cache/)",
        "categories": man["categories"], "items": man["items"],
        "items_per_category": man["items_per_category"], "pool_per_category": POOL_PER_CATEGORY,
        "neighbours_per_item": man["neighbours"], "near_neighbours": N_NEAR,
        "parameters": "continuous CC-verified block parameters on the CC grid k/127 of the template range, "
                      "drawn at 0.10-0.90 of the range; Level, ChVol, compressor Gain, Hum, Ripple, cab "
                      "EarlyReflections, Spread, Stereo not varied",
        "di_clips": "Guitar-TECHS DI excerpts (data/di, CC BY 4.0): A = fit clip, C = another passage of "
                    "the same recording (held-out notes); loud-frame level set to "
                    f"{man['ref_loud_dbfs']} dBFS; no song audio",
        "mix_stems": "none in v1 (oracle isolated target, density d0); separation stress is sep-stress-v1",
        "excluded": man["excluded"], "tolerance": man["tolerance"],
        "targets_working": man["targets_working"],
    }


# --------------------------------------------------------------------------- the harness (Native)

def machine_hosts() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True,
                       check=False)
    return len([x for x in r.stdout.split() if x.strip() and int(x) != os.getpid()])


def load_weights(spec: str) -> tuple[dict, bool, str]:
    """(weights, admitted, label). 'provisional' = metrics.PROVISIONAL_WEIGHTS (not admitted)."""
    if spec == "provisional":
        from tonematch.metrics import PROVISIONAL_WEIGHTS

        return dict(PROVISIONAL_WEIGHTS), False, "provisional, not gate evidence"
    doc = json.loads(Path(spec).read_text())
    w = doc.get("weights", doc)
    admitted = bool(doc.get("admitted", False))
    return {k: float(v) for k, v in w.items()}, admitted, \
        ("admitted weights (" + str(doc.get("ledger_row", "no ledger row named")) + ")") if admitted \
        else "provisional, not gate evidence"


class Block:
    """One pool model: its block preset, free refs, bounds and search space."""

    def __init__(self, model: str, category: str, free, catalog):
        from tonematch.engines import native_library as NL
        from tonematch.search import ParamSpace

        self.model, self.category, self.free = model, category, free
        self.preset = NL.block_preset(model, catalog)
        self.refs = [(0, n) for n, *_ in free]
        bounds = {(0, n): (lo, hi) for n, lo, hi, _ in free}
        for n, lo, hi, d in free:
            self.preset.slots[0].params[n] = d
        self.space = ParamSpace.from_preset(self.preset, self.refs, bounds, 128)
        self.space.start = self.space.from_unit(self.space.to_unit(self.space.start))
        self.rng = {n: (lo, hi) for n, lo, hi, _ in free}

    def with_values(self, values: dict[str, float]):
        import copy

        p = copy.deepcopy(self.preset)
        for n, v in values.items():
            p.slots[0].params[n] = float(v)
        return p

    def from_knobs(self, knobs: dict[str, int]) -> dict[str, float]:
        return {n: grid_value(*self.rng[n], k) for n, k in knobs.items()}

    def norm(self, values: dict[str, float]) -> dict[str, float]:
        return {n: (v - self.rng[n][0]) / (self.rng[n][1] - self.rng[n][0]) for n, v in values.items()}


def make_engine():
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeEngine

    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    import match_song as MS

    return MS.TimedEngine(NativeEngine(templates_dir=NL.library_templates()), log=lambda *_: None)


def search_item(engine, blocks: dict[str, Block], section, kit, weights: dict, budget: int, beam: int,
                seed: int, clip_s: float) -> dict:
    """Screen every pool model at its catalog defaults (one render each), then refine the best `beam`
    with the remaining renders (CMA-ES per chain). Returns the ranked candidates."""
    from tonematch.search import Budget, Evaluator, Plan
    from tonematch.search.loop import refine_chain

    bud = Budget(max_renders=budget, clip_s=clip_s, seed=seed, weights=weights)
    plan = Plan("native", budget, 10, False, 1.0, 1, 1.0, 0.0, False, est_wall_s=0.0)
    screen: dict[str, float] = {}
    for m, b in blocks.items():
        engine.load(b.with_values(dict(zip([n for n, *_ in b.free], b.space.start, strict=True))))
        ev = Evaluator(engine, kit, section, b.space, plan, bud)
        screen[m] = ev.score(b.space.start)[0]
    left = max(budget - len(blocks), beam)
    top = sorted(screen, key=screen.get)[:beam]
    refined: dict[str, tuple[float, np.ndarray]] = {}
    for m in top:
        b = blocks[m]
        engine.load(b.with_values(dict(zip([n for n, *_ in b.free], b.space.start, strict=True))))
        ev = Evaluator(engine, kit, section, b.space, plan, bud)
        v, f, _, _ = refine_chain(ev, b.space.start, left / beam, 0.3, seed)
        refined[m] = (f, v)
    best = min(refined, key=lambda m: refined[m][0])
    f, v = refined[best]
    return {"model": best, "values": {n: float(x) for (_, n), x in zip(blocks[best].space.refs, v, strict=True)},
            "loss_fit": float(f), "screen": {m: float(s) for m, s in screen.items()},
            "refined": {m: float(r[0]) for m, r in refined.items()}}


def run_item(engine, it: dict, blocks_by_cat: dict, catalog, kit, weights, budget, beam, do_rank1,
             clip_s: float) -> dict:
    import soundfile as sf

    from tonematch.engines.native import null_db
    from tonematch.search import Section

    t0 = now_s()
    r0 = engine.renders
    pool = {m: b for bc in blocks_by_cat.values() for m, b in bc.items()}
    tb = pool[it["model"]]
    dry = {t: sf.read(str(ROOT / it["di"][t]["file"]), dtype="float32")[0][: int(clip_s * SR)]
           for t in ("A", "C")}
    true_p = tb.with_values(it["values"])
    engine.load(true_p)
    target_a = np.asarray(engine.render(dry["A"]), np.float32)
    sec = Section("synthetic-v1", it["id"], target_a, dry["A"])
    allowed = {m: b for m, b in pool.items()}
    s = search_item(engine, allowed, sec, kit, weights, budget, beam, int(it["id"][1:]), clip_s)
    t_search = now_s() - t0
    pb = pool[s["model"]]
    res = {
        "id": it["id"], "category": it["category"], "model": it["model"],
        "pred_category": pb.category, "pred_model": s["model"], "pred_values": s["values"],
        "model_ok": s["model"] == it["model"], "category_ok": pb.category == it["category"],
        "loss_fit": s["loss_fit"], "screen": s["screen"], "refined": s["refined"],
        "true_model_screen_rank": 1 + sorted(s["screen"].values()).index(s["screen"][it["model"]]),
        "search_s": round(t_search, 1),
    }
    if res["model_ok"]:
        tn, pn = tb.norm(it["values"]), tb.norm(s["values"])
        res["param_abs_err"] = {n: abs(tn[n] - pn[n]) for n in tn}
        res["param_mae_item"] = float(np.mean(list(res["param_abs_err"].values())))
        res["param_mid_err"] = float(np.mean([abs(v - 0.5) for v in tn.values()]))
    # held-out notes (DI C): the found preset scored against the true preset on the same new notes
    engine.load(true_p)
    target_c = np.asarray(engine.render(dry["C"]), np.float32)
    engine.load(pb.with_values(s["values"]))
    found_c = np.asarray(engine.render(dry["C"]), np.float32)
    res["loss_heldout"] = float(kit.score(target_c, found_c, weights=weights).total)
    if do_rank1:
        # The gate's rank-1: the target is the true preset on the fit notes (DI A); the true preset and its
        # neighbours are rendered on the held-out notes (DI C, another passage of the same recording).
        # Scoring them on A as well would hand the truth a loss of exactly 0 (same render), so it
        # tests nothing.
        rows = [{"label": "true", "model": it["model"], "loss": float(kit.score(target_a, target_c, weights=weights).total),
                 "null_db": 0.0}]
        for nb in it["neighbours"]:
            b = pool[nb["model"]]
            engine.load(b.with_values(b.from_knobs(nb["knobs"])))
            y = np.asarray(engine.render(dry["C"]), np.float32)
            rows.append({"label": nb["label"], "model": nb["model"],
                         "loss": float(kit.score(target_a, y, weights=weights).total),
                         "null_db": float(null_db(target_c, y))})
        true_loss = rows[0]["loss"]
        nb = rows[1:]
        audible = [r for r in nb if r["null_db"] > SAME_SOUND_DB]
        res["rank1"] = bool(all(true_loss < r["loss"] for r in nb))
        res["rank1_audible"] = bool(all(true_loss < r["loss"] for r in audible))
        res["n_same_sound"] = len(nb) - len(audible)
        res["true_loss_heldout"] = true_loss
        res["neighbour_rank"] = 1 + sum(r["loss"] <= true_loss for r in nb)
    res["renders"] = engine.renders - r0
    res["wall_s"] = round(now_s() - t0, 1)
    return res


def summarize(results: list[dict], labels: list[dict], pools: dict) -> dict:
    n = len(results)
    ok = [r for r in results if "error" not in r]
    tp = sum(r["category_ok"] for r in ok)
    wrong = len(results) - tp
    out = {"items": n, "errors": n - len(ok),
           "block_f1": 2 * tp / (2 * tp + 2 * wrong) if n else float("nan"),
           "model_acc": sum(r["model_ok"] for r in ok) / n if n else float("nan")}
    mae = [e for r in ok for e in r.get("param_abs_err", {}).values()]
    out["param_mae"] = float(np.mean(mae)) if mae else float("nan")
    mid = [r["param_mid_err"] for r in ok if "param_mid_err" in r]
    out["param_mae_always_middle"] = float(np.mean(mid)) if mid else float("nan")
    r1 = [r for r in ok if "rank1" in r]
    if r1:
        out["rank1_rate"] = float(np.mean([r["rank1"] for r in r1]))
        out["rank1_rate_audible"] = float(np.mean([r["rank1_audible"] for r in r1]))
        out["same_sound_neighbours_per_item"] = float(np.mean([r["n_same_sound"] for r in r1]))
    if ok:
        out["loss_heldout"] = float(np.mean([r["loss_heldout"] for r in ok]))
        out["loss_fit"] = float(np.mean([r["loss_fit"] for r in ok]))
        out["heldout_ratio"] = out["loss_heldout"] / max(out["loss_fit"], 1e-9)
    by_id = {x["id"]: x for x in labels}
    ch = [1 / len(pools[by_id[r["id"]]["category"]]) for r in ok] if pools else []
    out["model_acc_chance"] = float(np.mean(ch)) if ch else float("nan")
    out["block_f1_chance"] = 1 / len(CATEGORIES)
    out["by_category"] = {c: {"items": sum(r["category"] == c for r in ok),
                              "category_acc": float(np.mean([r["category_ok"] for r in ok if r["category"] == c]))
                              if any(r["category"] == c for r in ok) else None,
                              "model_acc": float(np.mean([r["model_ok"] for r in ok if r["category"] == c]))
                              if any(r["category"] == c for r in ok) else None} for c in CATEGORIES}
    out["renders"] = sum(r.get("renders", 0) for r in ok)
    out["item_wall_s_mean"] = float(np.mean([r["wall_s"] for r in ok])) if ok else float("nan")
    return out


def select_items(labels: list[dict], count: int | None, ids: list[str] | None, shard: tuple[int, int] | None):
    """Stratified order: round-robin across categories, so any prefix covers every category."""
    if ids:
        return [x for x in labels if x["id"] in ids]
    by = {c: [x for x in labels if x["category"] == c] for c in CATEGORIES}
    order = [by[c][j] for j in range(ITEMS_PER_CATEGORY) for c in CATEGORIES if j < len(by[c])]
    if count:
        order = order[:count]
    if shard:
        order = [x for i, x in enumerate(order) if i % shard[1] == shard[0]]
    return order


def previous_ledger(dataset: str, engine: str = "native") -> dict[str, float]:
    import csv

    path = ROOT / "ledger" / "runs.csv"
    prev: dict[str, float] = {}
    vp = ROOT / "ledger" / "void.csv"
    void = set()
    if vp.exists():
        with vp.open(newline="") as f:
            void = {(r["timestamp"], r["metric"]) for r in csv.DictReader(f)}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                if (r["timestamp"], r["metric"]) in void:
                    continue
                if r["kind"] == "benchmark" and r["dataset"] == dataset and r["engine"] == engine:
                    try:
                        prev[r["metric"]] = float(r["value"])
                    except ValueError:
                        pass
    return prev


def regressions(current: dict, prev: dict, dataset: str) -> list[str]:
    out = []
    for m, v in current.items():
        if m in prev and np.isfinite(v):
            drop = (v - prev[m]) if m in LOWER_BETTER else (prev[m] - v)
            if drop > TOLERANCE.get(m, 0.0):
                out.append(f"REGRESSION {dataset} {m}: {prev[m]:.4f} -> {v:.4f}")
    return out


LEDGER_METRICS = ("block_f1", "model_acc", "param_mae", "rank1_rate", "rank1_rate_audible", "loss_heldout",
                  "heldout_ratio")


def write_ledger(summary: dict, dataset: str, notes: str, wall: float) -> list[str]:
    rows = []
    env = {**os.environ, "TONEMATCH_AGENT": "benchmark"}
    for m in LEDGER_METRICS:
        v = summary.get(m)
        if v is None or not np.isfinite(v):
            continue
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark",
                        "--engine", "native", "--dataset", dataset, "--metric", m, "--value", f"{v:.6g}",
                        "--renders", str(summary["renders"]), "--wall-clock-s", f"{wall:.1f}",
                        "--notes", notes], check=True, env=env, cwd=ROOT)
        rows.append(m)
    return rows


def cmd_run(a) -> int:
    man = json.loads((SET_DIR / "manifest.json").read_text())
    labels = json.loads((SET_DIR / "labels.json").read_text())
    shard = tuple(int(x) for x in a.shard.split("/")) if a.shard else None
    items = select_items(labels, a.items, a.ids, shard)
    weights, admitted, wlabel = load_weights(a.weights)
    running = machine_hosts()
    print(f"native_host processes before start: {running}; this run opens 1", flush=True)
    if running + 1 > MAX_MACHINE_HOSTS:
        print(f"refusing: {running} native_host processes already running (limit {MAX_MACHINE_HOSTS})",
              file=sys.stderr)
        return 3
    from tonematch.catalog import load_catalog
    from tonematch.metrics import LossKit

    catalog = load_catalog()
    blocks_by_cat = {}
    from tonematch.engines import native_library as NL
    for c in CATEGORIES:
        blocks_by_cat[c] = {}
        for m in man["pool"][c]:
            tpl = NL.template_for(m)
            blocks_by_cat[c][m] = Block(m, c, free_params(m, tpl, catalog, c), catalog)
    run_dir = SET_DIR / "runs" / a.tag
    run_dir.mkdir(parents=True, exist_ok=True)
    kit = LossKit()
    t0 = now_s()
    engine = make_engine()
    t_open = now_s() - t0
    print(f"engine open in {t_open:.1f} s; machine now {machine_hosts()} native_host; {len(items)} items, "
          f"search budget {a.budget} renders, weights: {wlabel}", flush=True)
    results = []
    try:
        for it in items:
            f = run_dir / f"{it['id']}.json"
            if f.exists() and not a.redo:
                results.append(json.loads(f.read_text()))
                continue
            try:
                r = run_item(engine, it, blocks_by_cat, catalog, kit, weights, a.budget, a.beam,
                             not a.no_rank1, man["clip_seconds"])
            except Exception as e:  # noqa: BLE001 - recorded, counted as a miss
                r = {"id": it["id"], "category": it["category"], "model": it["model"], "category_ok": False,
                     "model_ok": False, "error": f"{type(e).__name__}: {e}", "renders": 0, "wall_s": 0.0}
            f.write_text(json.dumps(r, indent=1))
            results.append(r)
            print(f"{it['id']} {it['category']:6s} {it['model'][4:]:28s} -> "
                  f"{(r.get('pred_model') or '?')[4:]:28s} cat {'ok' if r['category_ok'] else 'NO'} "
                  f"model {'ok' if r['model_ok'] else 'NO'} mae "
                  f"{r.get('param_mae_item', float('nan')):.3f} rank1 {r.get('rank1', '-')} "
                  f"{r.get('renders', 0)} renders {r.get('wall_s', 0)} s", flush=True)
    finally:
        engine.close()
    wall = now_s() - t0
    full = len(items) == len(labels) and not shard
    dataset = DATASET if full else f"{DATASET}-n{len(items)}" + (f"-shard{a.shard.replace('/', 'of')}" if shard else "")
    pools = {c: man["pool"][c] for c in CATEGORIES}
    summ = summarize(results, labels, pools)
    summ.update({"dataset": dataset, "weights": wlabel, "admitted": admitted, "budget": a.budget,
                 "wall_s": round(wall, 1), "engine_open_s": round(t_open, 1),
                 "full_run_estimate_s": round(summ["item_wall_s_mean"] * len(labels), 0),
                 "gate_evidence": bool(full and admitted and not a.no_rank1)})
    (run_dir / "summary.json").write_text(json.dumps(summ, indent=1))
    show = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summ.items() if k != "by_category"}
    print(json.dumps(show, indent=1))
    regs = regressions({m: summ[m] for m in LEDGER_METRICS if m in summ}, previous_ledger(dataset), dataset)
    for r in regs:
        print(r, file=sys.stderr)
    if a.ledger:
        notes = (f"{wlabel}; {len(results)} of {len(labels)} items; search budget {a.budget} renders per item; "
                 f"one-block presets from block-library; block_f1 = category identified; "
                 f"oracle isolated target, DI A fit and C held-out" + (f"; {a.note}" if a.note else ""))
        wrote = write_ledger(summ, dataset, notes, wall)
        print(f"ledger: {len(wrote)} rows, dataset {dataset}")
    return 1 if regs else 0


def cmd_summarize(a) -> int:
    man = json.loads((SET_DIR / "manifest.json").read_text())
    labels = json.loads((SET_DIR / "labels.json").read_text())
    run_dir = SET_DIR / "runs" / a.tag
    results = [json.loads(p.read_text()) for p in sorted(run_dir.glob("i*.json"))]
    print(json.dumps(summarize(results, labels, man["pool"]), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=SEED)
    r = sub.add_parser("run")
    r.add_argument("--items", type=int, default=None, help="first N items in category round-robin order")
    r.add_argument("--ids", nargs="+", default=None)
    r.add_argument("--shard", default=None, help="k/n: items with index %% n == k (parallel processes)")
    r.add_argument("--budget", type=int, default=150, help="search renders per item (screen + refine)")
    r.add_argument("--beam", type=int, default=3)
    r.add_argument("--weights", default="provisional", help="'provisional' or a JSON file of loss weights")
    r.add_argument("--no-rank1", action="store_true")
    r.add_argument("--ledger", action="store_true")
    r.add_argument("--redo", action="store_true")
    r.add_argument("--tag", default="default")
    r.add_argument("--note", default="", help="free text appended to the ledger notes")
    s = sub.add_parser("summarize")
    s.add_argument("--tag", default="default")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        man = generate(a.seed)
        print(f"{man['items']} items, pool {sum(len(p) for p in man['pool'].values())} models, "
              f"wrote {SET_DIR} and {FIXTURE}")
        return 0
    return {"run": cmd_run, "summarize": cmd_summarize}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
