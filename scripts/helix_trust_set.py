#!/usr/bin/env python3
"""Helix trust set `helix-trust-v1` for loss-trust-check (owner: tone-metrics, T067).

Real Helix Native renders of known chains, so a loss term can be admitted on Helix evidence rather than
on the numpy proxy. Chains come from the T060 block library templates (`cache/parity/spot380/hlx`,
captured in `cache/native/templates_files/spot380`): the K chain (EssexA15 amp, 1x12 Blue Bell cab, one
block) with one block changed, over 8 categories: Distortion, Delay, Reverb, Dynamics, EQ, Modulation,
Amp, Cab. No new captures are needed: every candidate is one of those chains with its knobs moved by CC.

  gen     offline: pick DI excerpts (Guitar-TECHS, data/di), draw targets and candidates, write
          cache/loss-trust/helix-trust-v1/manifest.json and the DI excerpts
  add-di  offline: add DI C, another passage of DI A's recording (held-out notes on the same
          guitar and player; B is a different recording, so it tests DI invariance)
  render  Native: NativeRenderPool (at most 3 hosts; refuses if the machine would exceed 6
          native_host processes), renders every candidate on the target's DI (A, the same notes) and
          on a different DI (B, held-out notes); resumable; per-render timings in render_summary.json

Per item: the target is the true preset rendered on DI A (an oracle stem, d0). Candidates: the true
preset, 9 "near" (the same model with 1-2 of its knobs moved by 0.12-0.30 of their range), every other
model of the same category ("nbr", random knobs), and random chains from other categories to make 19
distractors. Knobs are drawn on Native's CC grid (k/127 of the template range). Output-level knobs
(Level, ChVol), Hum, Ripple and cab Early Reflections are not varied.

No song audio is used. Everything written stays under cache/ (gitignored).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SR = 48_000
DATASET = "helix-trust-v1"
OUT = ROOT / "cache" / "loss-trust" / DATASET
SPOT = ROOT / "cache" / "parity" / "spot380"
TEMPLATES = ROOT / "cache" / "native" / "templates_files" / "spot380"
DI_LIB = ROOT / "data" / "di"
FOCUS_SLOT = {"Amp": 0, "Cab": 1}          # every other category: slot 2 (the block)
CATEGORIES = ("Distortion", "Delay", "Reverb", "Dynamics", "EQ", "Modulation", "Amp", "Cab")
NOT_VARIED = {"Level", "ChVol", "Hum", "Ripple", "EarlyReflections", "Spread", "Stereo"}
NOT_VARIED_IN = {"Dynamics": {"Gain"}}   # compressor make-up gain is an output level
U_RANGE = {"Time": (0.10, 0.45), "Mix": (0.15, 0.70), "Feedback": (0.05, 0.70)}
U_DEFAULT = (0.10, 0.90)
N_NEAR = 9
NEAR_DELTA = (0.12, 0.30)
MAX_HOSTS = 3
MAX_MACHINE_HOSTS = 6
REF_LOUD_DBFS = None     # set from the parity DI at gen time (loud-frame level of a known-good DI)


# --------------------------------------------------------------------------- DI excerpts

def loud_level_db(x: np.ndarray, win: int = 2400) -> float:
    """Level of the loud part: RMS of 50 ms frames within 30 dB of the loudest, in dBFS."""
    n = len(x) // win
    fr = np.sqrt(np.mean(x[: n * win].reshape(n, win).astype(np.float64) ** 2, axis=1)) + 1e-12
    db = 20 * np.log10(fr)
    keep = db > db.max() - 30
    return float(20 * np.log10(np.sqrt(np.mean(fr[keep] ** 2))))


def usable_runs(seconds: float) -> list[dict]:
    """Every start of `seconds` of consecutive, non-silent 4 s index segments, per file."""
    man = json.loads((DI_LIB / "manifest.json").read_text())
    by_file: dict[str, list[dict]] = {}
    for c in man["clips"]:
        by_file.setdefault(c["path"], []).append(c)
    need = int(np.ceil(seconds / 4.0))
    runs = []
    for path, cl in by_file.items():
        cl = sorted(cl, key=lambda c: c["start_s"])
        for j in range(len(cl) - need + 1):
            seg = cl[j:j + need]
            if any(abs(seg[k + 1]["start_s"] - seg[k]["start_s"] - 4.0) > 1e-6 for k in range(need - 1)):
                continue
            if min(s["rms_dbfs"] for s in seg) < -50 or max(s["density"] for s in seg) <= 0:
                continue
            runs.append({"path": path, "start_s": seg[0]["start_s"], "style": seg[0]["style"]})
    return runs


def pick_excerpts(rng: np.random.Generator, n: int, seconds: float) -> list[dict]:
    """n excerpts of `seconds`, each from consecutive 4 s index segments of one file, files in a
    random order, never overlapping an earlier pick from the same file."""
    runs = usable_runs(seconds)
    files = sorted({r["path"] for r in runs})
    order = rng.permutation(len(files))
    out: list[dict] = []
    k = 0
    while len(out) < n:
        f = files[order[k % len(files)]]
        cands = [r for r in runs if r["path"] == f and all(abs(r["start_s"] - o["start_s"]) >= seconds
                                                          for o in out if o["path"] == f)]
        if cands:
            out.append(dict(cands[int(rng.integers(len(cands)))]))
        k += 1
    return out


def load_excerpt(e: dict, seconds: float, ref_db: float) -> tuple[np.ndarray, float]:
    from tonematch.dry import _read  # resamples the 44.1 kHz Guitar-TECHS files to 48 kHz
    x = _read(DI_LIB / e["path"], e["start_s"], seconds)[: int(seconds * SR)]
    g = ref_db - loud_level_db(x)
    y = x * np.float32(10 ** (g / 20))
    pk = float(np.abs(y).max())
    if pk > 0.89:                     # keep 1 dB of headroom
        g -= 20 * np.log10(pk / 0.89)
        y = x * np.float32(10 ** (g / 20))
    return y.astype(np.float32), float(g)


# --------------------------------------------------------------------------- chains and knobs

def bases(cat: dict) -> dict[str, list[dict]]:
    """Category -> [{name, hlx, slot, free: [(param, lo, hi, ulo, uhi)]}] from the T060 set."""
    from tonematch import preset as P
    from tonematch.engines.native import ChainTemplate, baked_fingerprint, chain_key

    variants = ChainTemplate.load_variants(TEMPLATES)
    mf = json.loads((SPOT / "manifest.json").read_text())
    out: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for it in mf["items"]:
        if it["category"] not in out:
            continue
        p = P.read_hlx(ROOT / it["hlx"], cat)
        tpl = None
        for t in variants.get(chain_key(p), []):
            if t.baked_hash and baked_fingerprint(p, t.map, t.verified)[1] == t.baked_hash:
                tpl = t
        if tpl is None:
            raise SystemExit(f"no captured template matches {it['hlx']}; run parity_spot.py native first")
        slot = FOCUS_SLOT.get(it["category"], 2)
        model = p.slots[slot].model_id
        kinds = cat["models"][model]["params"]
        free = []
        for k, v in sorted(p.slots[slot].params.items()):
            key = f"{slot}:{k}"
            if (k in NOT_VARIED or k in NOT_VARIED_IN.get(it["category"], ()) or k.startswith("@") or kinds.get(k, {}).get("kind") != "continuous"
                    or isinstance(v, bool) or key not in tpl.map or key not in tpl.verified):
                continue
            lo, hi = tpl.ranges.get(key, [0.0, 1.0])
            ulo, uhi = U_RANGE.get(k, U_DEFAULT)
            free.append([k, float(lo), float(hi), ulo, uhi])
        out[it["category"]].append({"name": it["name"], "hlx": it["hlx"], "slot": slot, "model": model,
                                    "template": tpl.name, "free": free})
    return out


def snap(u: float) -> int:
    return int(np.clip(np.round(u * 127), 0, 127))


def draw(rng: np.random.Generator, base: dict) -> dict[str, int]:
    return {k: snap(rng.uniform(ulo, uhi)) for k, _, _, ulo, uhi in base["free"]}


def near(rng: np.random.Generator, base: dict, ks: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    names = [f[0] for f in base["free"]]
    m = 1 if len(names) < 3 else int(rng.integers(1, 3))
    pick = list(rng.choice(names, size=min(m, len(names)), replace=False))
    out = dict(ks)
    for k in pick:
        d = rng.uniform(*NEAR_DELTA) * (1 if rng.random() < 0.5 else -1)
        u = ks[k] / 127 + d
        u = -u if u < 0 else (2 - u if u > 1 else u)        # reflect into [0, 1]
        out[k] = snap(u)
        if out[k] == ks[k]:
            out[k] = min(127, ks[k] + 16) if ks[k] < 64 else ks[k] - 16
    return out, pick


def values(base: dict, ks: dict[str, int]) -> dict[str, float]:
    return {k: lo + ks[k] / 127 * (hi - lo) for k, lo, hi, _, _ in base["free"]}


def cmd_gen(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    cat = C.load_catalog("current")
    rng = np.random.default_rng(a.seed)
    bs = bases(cat)
    import parity_null as PN  # the parity guitar DI: a known-good input level for these chains
    ref_db = loud_level_db(PN.load_di(PN.DI_DEFAULT))
    per = a.items // len(CATEGORIES)
    targets = []
    for c in CATEGORIES:
        order = rng.permutation(len(bs[c]))
        targets += [(c, int(order[j % len(bs[c])])) for j in range(per)]
    exc = pick_excerpts(rng, 2 * len(targets), a.seconds)
    (OUT / "di").mkdir(parents=True, exist_ok=True)
    items = []
    for i, (c, bi) in enumerate(targets):
        base = bs[c][bi]
        ea, eb = exc[2 * i], exc[2 * i + 1]
        if ea["path"] == eb["path"]:
            ea, eb = exc[2 * i], exc[(2 * i + 3) % len(exc)]
        dis = {}
        for tag, e in (("A", ea), ("B", eb)):
            x, g = load_excerpt(e, a.seconds, ref_db)
            f = OUT / "di" / f"item{i:02d}_{tag}.wav"
            sf.write(str(f), x, SR, subtype="FLOAT")
            dis[tag] = {**e, "gain_db": round(g, 2), "file": str(f.relative_to(ROOT))}
        ks = draw(rng, base)
        cands = [{"label": "true", "base": [c, bi], "knobs": ks}]
        for _ in range(N_NEAR):
            k2, moved = near(rng, base, ks)
            cands.append({"label": "near:" + "+".join(moved), "base": [c, bi], "knobs": k2})
        for bj, nb in enumerate(bs[c]):
            if bj != bi:
                cands.append({"label": f"nbr:{nb['model']}", "base": [c, bj], "knobs": draw(rng, nb)})
        others = [(oc, oj) for oc in CATEGORIES if oc != c for oj in range(len(bs[oc]))]
        while len(cands) < a.distractors + 1:
            oc, oj = others[int(rng.integers(len(others)))]
            cands.append({"label": f"random:{oc}", "base": [oc, oj], "knobs": draw(rng, bs[oc][oj])})
        items.append({"item": i, "category": c, "true_model": base["model"], "di": dis, "candidates": cands})
    mf = {"dataset": DATASET, "seed": a.seed, "seconds": a.seconds, "distractors": a.distractors,
          "ref_loud_dbfs": round(ref_db, 2), "bases": bs, "items": items,
          "about": "targets: the true preset on DI A (oracle stem); candidates on DI A (same notes) and B "
                   "(held-out notes); knobs are CC steps k/127 of each template range"}
    (OUT / "manifest.json").write_text(json.dumps(mf, indent=1))
    n_lab: dict[str, int] = {}
    for it in items:
        for cd in it["candidates"][1:]:
            n_lab[cd["label"].split(":")[0]] = n_lab.get(cd["label"].split(":")[0], 0) + 1
    print(f"{len(items)} items x {a.distractors} distractors {n_lab}; DIs at {ref_db:.1f} dBFS loud level; "
          f"{OUT / 'manifest.json'}")
    return 0


def cmd_add_di(a) -> int:
    """Add DI C to every item: a non-overlapping passage of the SAME recording as DI A (same player,
    guitar and style, different notes). B (a different recording) then tests DI invariance and C
    tests note invariance alone. Added 2026-10-16 after the first B-only scoring (DECISIONS)."""
    import parity_null as PN
    import soundfile as sf
    mfp = OUT / "manifest.json"
    mf = json.loads(mfp.read_text())
    rng = np.random.default_rng([mf["seed"], 3])
    runs = usable_runs(mf["seconds"])
    ref_db = loud_level_db(PN.load_di(PN.DI_DEFAULT))
    for it in mf["items"]:
        ea = it["di"]["A"]
        cands = [r for r in runs if r["path"] == ea["path"] and abs(r["start_s"] - ea["start_s"]) >= mf["seconds"]]
        same_file = bool(cands)
        if not cands:   # short recording: another file of the same folder (same player and session)
            folder = ea["path"].rsplit("/", 1)[0]
            cands = [r for r in runs if r["path"] != ea["path"] and r["path"].rsplit("/", 1)[0] == folder]
        if not cands:
            raise SystemExit(f"item {it['item']}: no second passage for {ea['path']}")
        e = dict(cands[int(rng.integers(len(cands)))], same_file=same_file)
        x, g = load_excerpt(e, mf["seconds"], ref_db)
        f = OUT / "di" / f"item{it['item']:02d}_C.wav"
        sf.write(str(f), x, SR, subtype="FLOAT")
        it["di"]["C"] = {**e, "gain_db": round(g, 2), "file": str(f.relative_to(ROOT))}
    mf["about"] += "; DI C: another passage of DI A's recording (held-out notes, same guitar and player)"
    mfp.write_text(json.dumps(mf, indent=1))
    n_same = sum(it["di"]["C"]["same_file"] for it in mf["items"])
    print(f"DI C added to {len(mf['items'])} items ({n_same} from the same file, the rest from the same folder)")
    return 0


# --------------------------------------------------------------------------- render

def machine_hosts() -> int:
    r = subprocess.run(["pgrep", "-f", "tonematch.engines.native_host"], capture_output=True, text=True,
                       check=False)
    return len([x for x in r.stdout.split() if x.strip()])


def build_preset(cat: dict, bs: dict, cand: dict, cache: dict):
    from tonematch import preset as P
    c, bi = cand["base"]
    base = bs[c][bi]
    if base["hlx"] not in cache:
        cache[base["hlx"]] = P.read_hlx(ROOT / base["hlx"], cat)
    p = copy.deepcopy(cache[base["hlx"]])
    for k, v in values(base, cand["knobs"]).items():
        p.slots[base["slot"]].params[k] = float(v)
    return p


def render_path(i: int, k: int, tag: str) -> Path:
    return OUT / "renders" / f"item{i:02d}" / f"c{k:02d}_{tag}.npy"


def cmd_render(a) -> int:
    import soundfile as sf

    from tonematch import catalog as C
    mf = json.loads((OUT / "manifest.json").read_text())
    hosts = min(a.hosts, MAX_HOSTS)
    running = machine_hosts()
    if running + hosts > MAX_MACHINE_HOSTS:
        hosts = MAX_MACHINE_HOSTS - running
        if hosts < 1:
            print(f"{running} native_host processes already running; limit {MAX_MACHINE_HOSTS}", file=sys.stderr)
            return 3
    print(f"native_host processes before start: {running}; opening {hosts}")
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    from tonematch.engines.native import NativeRenderPool, RenderJob

    cat = C.load_catalog("current")
    bs = mf["bases"]
    cache: dict = {}
    todo = [it for it in mf["items"] if a.only is None or it["item"] in a.only]
    summ_path = OUT / "render_summary.json"
    summ = json.loads(summ_path.read_text()) if summ_path.exists() else {"chunks": []}
    t0 = time.perf_counter()
    pool = NativeRenderPool(workers=hosts, templates_dir=TEMPLATES)
    t_open = time.perf_counter() - t0
    print(f"pool open: {pool.workers} hosts in {t_open:.1f} s; machine now {machine_hosts()} native_host")
    errors: dict[str, str] = {}
    try:
        for s in range(0, len(todo), a.chunk):
            chunk = todo[s:s + a.chunk]
            jobs, keys = [], []
            for it in chunk:
                dry = {t: sf.read(str(ROOT / it["di"][t]["file"]), dtype="float32")[0] for t in it["di"]}
                for k, cd in enumerate(it["candidates"]):
                    for tag in it["di"]:
                        if render_path(it["item"], k, tag).exists():
                            continue
                        jobs.append(RenderJob(dry=dry[tag], preset=build_preset(cat, bs, cd, cache)))
                        keys.append((it["item"], k, tag))
            if not jobs:
                continue
            tc = time.perf_counter()
            outs = pool.render_batch(jobs, on_error="none")
            wall = time.perf_counter() - tc
            for j, ((i, k, tag), y) in enumerate(zip(keys, outs, strict=True)):
                if y is None:
                    errors[f"{i}:{k}:{tag}"] = repr(pool.last_errors.get(j))
                    continue
                y = np.asarray(y, np.float32)
                if y.ndim == 2:
                    y = y.mean(axis=1 if y.shape[1] <= 2 else 0)
                f = render_path(i, k, tag)
                f.parent.mkdir(parents=True, exist_ok=True)
                np.save(f, y)
            item_s = list(pool.last_item_s)
            rec = {"items": [it["item"] for it in chunk], "renders": len(jobs), "wall_s": round(wall, 2),
                   "hosts": pool.workers, "item_s_mean": round(float(np.mean(item_s)), 4) if item_s else None,
                   "item_s_median": round(float(np.median(item_s)), 4) if item_s else None,
                   "stats": pool.stats()}
            summ["chunks"].append(rec)
            summ_path.write_text(json.dumps(summ, indent=1))
            print(f"items {rec['items']}: {len(jobs)} renders in {wall:.1f} s "
                  f"({wall / len(jobs):.3f} s/render wall, {rec['item_s_mean']} s/render per host); "
                  f"mem {rec['stats']['mem_mb']} MB", flush=True)
    finally:
        pool.close()
    summ["errors"] = {**summ.get("errors", {}), **errors}
    summ["open_s"] = round(t_open, 1)
    summ_path.write_text(json.dumps(summ, indent=1))
    print(f"done; errors {len(errors)}; machine native_host after close: {machine_hosts()}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--seed", type=int, default=4101)
    g.add_argument("--items", type=int, default=40)
    g.add_argument("--distractors", type=int, default=19)
    g.add_argument("--seconds", type=float, default=10.0)
    sub.add_parser("add-di", help="add DI C: another passage of DI A's recording")
    r = sub.add_parser("render")
    r.add_argument("--hosts", type=int, default=MAX_HOSTS)
    r.add_argument("--chunk", type=int, default=3, help="items per render batch (memory)")
    r.add_argument("--only", type=int, nargs="+", default=None)
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    return {"gen": cmd_gen, "add-di": cmd_add_di, "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
