"""Human rating session v2 (T170): "Hidden Helix setting" (the human's choice, DECISIONS 2026-10-18).

Why v2: v1 compared the original song guitarist (target) with renders of the human's own playing, so a trial
judged the performance, not the tone. In v2 EVERY clip is the human's own dry take, excerpt by excerpt:

  target     one excerpt of the human's dry take through a single Helix drive block with a HIDDEN random setting
  candidates the SAME excerpt through other settings, on a ladder of distance from the hidden setting:
               rung 1  same model, knobs moved slightly (about +-0.1)
               rung 2  same model, knobs moved a lot (about +-0.35)
               rung 3  another model of the same confusable group / circuit class, similar gain
               rung 4  a model of a different circuit class, similar gain
               rung 5  clean (no drive: the dry excerpt), for some targets
  trials     24 regular A-vs-B trials of ladder rungs (adjacent or near rungs), 3 sanity trials, 2 retests
             (the same pair, sides swapped)

Loudness: every clip -23 LUFS, one shared peak guard for the whole session; each clip's ORIGINAL level is
recorded in the key so `level_db` scores work. The answer key (true settings, rungs) is outside the served folder.
Nothing here judges audio or admits a score: the human's answers are the data. Song audio is not used at all.

Used through scripts/ab_session.py (rating-build-v2, play, rating-score).
"""
from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import rating_session as RS

SR = RS.SR
EXCERPT_S = 7.0
LEAD_S = 0.5                       # context rendered before the excerpt and thrown away
TAKES_DIR = ROOT / "data" / "songs" / "mother-playalong" / "takes"
TAKES_GLOB = "20261018-113158_take{n}_dry.wav"     # the human's own playing; no song audio
TAKE_PLAN = [1, 1, 1, 1, 2, 2, 2, 3]               # takes 1 and 2 mainly, one excerpt per target
RUNGS = {1: "same model, knobs nudged (~0.1)", 2: "same model, knobs moved a lot (~0.35)",
         3: "same group or class, similar gain", 4: "different circuit class, similar gain", 5: "clean (no drive)"}

# Models named by docs/notes/distortion-circuits.md as time-variant, novelty, octave, bass or lo-fi: never used.
EXCLUDED = frozenset({
    "HD2_DistBitcrusher", "HD2_DistMegaphone", "HD2_DistJetFuzz", "HD2_DistXenomorphFuzz", "HD2_DistIndustrialFuzz",
    "HD2_DistThrifterFuzz", "HD2_DistTycoctaviaFuzz", "HD2_DistSubOctFuzz", "HD2_DistClawthornDrive",
    "HD2_DistAmpegScramblerOD", "HD2_DistZeroAmpBassDI", "HD2_DistRegalBassDI", "HD2_DistObsidian7000",
    "HD2_DistVitalDist", "HD2_DistToneSovereign", "HD2_DistL6Distortion"})

# model -> (drive knob, [tone knobs]); Level is never moved (loudness is matched afterwards)
KNOBS = {
    "HD2_DistScream808": ("Gain", ["Tone"]), "HD2_DistPillars": ("Gain", ["Tone"]), "HD2_DistDhyanaDrive": ("Gain", ["Tone"]),
    "HD2_DistMinotaur": ("Gain", ["Tone"]), "HD2_DistTeemah": ("Gain", ["Bass", "Treble"]),
    "HD2_DistHeirApparent": ("Gain", ["Tone"]), "HD2_DistPrizeDrive": ("Drive", ["Spectrum"]),
    "HD2_DistVerminDist": ("Gain", ["Filter"]), "HD2_DistRatatouilleDist": ("Gain", ["Filter"]),
    "HD2_DistDeezOneVintage": ("Drive", ["Tone"]), "HD2_DistHedgehogD9": ("Gain", ["Tone"]),
    "HD2_DistDeezOneMod": ("Drive", ["Tone"]),
    "HD2_DistTriangleFuzz": ("Sustain", ["Tone"]), "HD2_DistDarkDoveFuzz": ("Sustain", ["Tone"]),
    "HD2_DistRamsHead": ("Sustain", ["Tone"]),
    "HD2_DistArbitratorFuzz": ("Fuzz", []), "HD2_DistWringerFuzz": ("Gain", ["Treble", "Bass"]),
    "HD2_DistSwedishChainsaw": ("Drive", ["Bass", "Treble"]), "HD2_DistLegendaryDrive": ("Drive", ["Bass", "Treble"]),
    "HD2_DistKinkyBoost": ("Drive", []), "HD2_DistVitalBoost": ("Boost", []),
    "HD2_DistDerangedMaster": ("Drive", ["Bass", "Treble"]),
}
# circuit class -> (label, hidden-target model, other models of the same confusable group / class)
CLASSES = {
    "ts": ("TS-type", "HD2_DistScream808", ["HD2_DistPillars", "HD2_DistDhyanaDrive"]),
    "klon": ("Klon-type", "HD2_DistMinotaur", ["HD2_DistTeemah", "HD2_DistHeirApparent", "HD2_DistPrizeDrive"]),
    "rat": ("RAT-type", "HD2_DistVerminDist", ["HD2_DistRatatouilleDist"]),
    "ds1": ("DS-1 / hard clip", "HD2_DistDeezOneVintage", ["HD2_DistHedgehogD9", "HD2_DistDeezOneMod"]),
    "muff": ("Big Muff", "HD2_DistTriangleFuzz", ["HD2_DistDarkDoveFuzz", "HD2_DistRamsHead"]),
    "gefuzz": ("germanium fuzz", "HD2_DistArbitratorFuzz", ["HD2_DistWringerFuzz"]),
    "metal": ("HM-2 / metal", "HD2_DistSwedishChainsaw", ["HD2_DistLegendaryDrive"]),
    "boost": ("clean boost", "HD2_DistKinkyBoost", ["HD2_DistVitalBoost", "HD2_DistDerangedMaster"]),
}
CLASS_ORDER = list(CLASSES)
DRIVE_RANGE, TONE_RANGE = (0.2, 0.9), (0.2, 0.8)
SMALL, LARGE = 0.1, 0.35
# regular-trial pair slots (24): mostly adjacent rungs, some two apart; rung 5 only for targets that have it
PAIR_SLOTS = [(1, 2)] * 5 + [(2, 3)] * 5 + [(3, 4)] * 4 + [(4, 5)] * 2 + [(1, 3)] * 3 + [(2, 4)] * 3 + [(3, 5)] * 2
PER_TARGET = 3
N_CLEAN_TARGETS = 6
N_RETESTS = 2


def all_models() -> set[str]:
    return set(KNOBS)


def models_of(cls: str) -> list[str]:
    return [CLASSES[cls][1], *CLASSES[cls][2]]


def class_of(model: str) -> str:
    return next(c for c in CLASSES if model in models_of(c))


# ---------------------------------------------------------------- ladder

def _r2(v: float) -> float:
    return round(float(v), 2)


def random_setting(model: str, rng: np.random.Generator) -> dict[str, float]:
    drive, tones = KNOBS[model]
    s = {drive: _r2(rng.uniform(*DRIVE_RANGE))}
    for t in tones:
        s[t] = _r2(rng.uniform(*TONE_RANGE))
    return s


def shifted(setting: dict[str, float], mag: float, rng: np.random.Generator, jitter: float = 0.2) -> dict[str, float]:
    """Every knob moved by about `mag` (mag * U(1-jitter, 1+jitter)) in a random direction; the direction is
    flipped when the move would leave [0.02, 0.98], so the realised move stays near `mag`."""
    out = {}
    for k, v in setting.items():
        d = mag * float(rng.uniform(1 - jitter, 1 + jitter))
        sgn = 1.0 if rng.random() < 0.5 else -1.0
        if not 0.02 <= v + sgn * d <= 0.98:
            sgn = -sgn
        out[k] = _r2(min(0.98, max(0.02, v + sgn * d)))
    return out


def transfer_setting(src_model: str, src: dict[str, float], dst_model: str, defaults: dict[str, float]) -> dict[str, float]:
    """The same drive amount on another model ("similar gain"); tone knobs carried over by position, else default."""
    sd, st = KNOBS[src_model]
    dd, dt_ = KNOBS[dst_model]
    out = {dd: _r2(min(0.95, max(0.1, src[sd])))}
    for i, k in enumerate(dt_):
        out[k] = _r2(src[st[i]]) if i < len(st) else _r2(defaults.get(k, 0.5))
    return out


def make_ladder(cls: str, rng: np.random.Generator, defaults: dict[str, dict[str, float]] | None = None) -> dict:
    """Hidden target setting plus the rungs for one circuit class. `defaults`: model -> catalog default knob values
    (used for tone knobs the target does not have). Returns {"class", "target", "rungs": {1: spec, 2.., "3b", "4b"}}
    where each spec is {"model", "params", "rung"}. 3b and 4b are spares."""
    defaults = defaults or {}
    _, tmodel, alts = CLASSES[cls]
    target = random_setting(tmodel, rng)
    rungs = {
        1: {"model": tmodel, "params": shifted(target, SMALL, rng, 0.2), "rung": 1},
        2: {"model": tmodel, "params": shifted(target, LARGE, rng, 0.15), "rung": 2},
    }
    order = [alts[i] for i in rng.permutation(len(alts))]
    for key, m in (("3", order[0]), ("3b", order[1] if len(order) > 1 else None)):
        if m is not None:
            rungs[int(key) if key == "3" else key] = {"model": m, "rung": 3,
                                                       "params": transfer_setting(tmodel, target, m, defaults.get(m, {}))}
    # other classes (the boost class only when the target is itself a boost: it has no clipping to compare)
    others = [c for c in CLASS_ORDER if c != cls and (c != "boost" or cls == "boost")]
    pick = [others[i] for i in rng.permutation(len(others))[:2]]
    for key, oc in zip((4, "4b"), pick, strict=True):
        pool = models_of(oc)
        m = pool[int(rng.integers(0, len(pool)))]
        rungs[key] = {"model": m, "rung": 4, "params": transfer_setting(tmodel, target, m, defaults.get(m, {}))}
    return {"class": cls, "model": tmodel, "target": target, "rungs": rungs}


def knob_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Largest absolute knob difference over the knobs both settings have (same-model rungs)."""
    return max(abs(a[k] - b[k]) for k in a if k in b)


# ---------------------------------------------------------------- excerpts

def frame_db(x: np.ndarray, hop: int = 960) -> np.ndarray:
    n = len(x) // hop
    f = x[: n * hop].reshape(n, hop).astype(np.float64)
    return 10 * np.log10(np.mean(f ** 2, axis=1) + 1e-12)


def window_score(x: np.ndarray, start: int, n: int) -> float:
    """Real playing: a window that is mostly active (frames within 35 dB of the take's loudest) AND dense with
    onsets (frame-to-frame rises of 5 dB or more). Higher is better."""
    db = frame_db(x[start:start + n])
    peak = 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2) + 1e-12) + 6
    active = float(np.mean(db > peak - 35.0))
    onsets = int(np.sum((np.diff(db) > 5.0) & (db[1:] > peak - 30.0)))
    return active * (1 + onsets)


def pick_windows(x: np.ndarray, k: int, n: int, hop_s: float = 0.5) -> list[tuple[int, float]]:
    """k non-overlapping windows (start sample, score), best first."""
    hop = int(hop_s * SR)
    cand = sorted(((window_score(x, s, n), s) for s in range(0, len(x) - n + 1, hop)), reverse=True)
    out: list[tuple[int, float]] = []
    for sc, s in cand:
        if all(abs(s - o) >= n for o, _ in out):
            out.append((s, sc))
        if len(out) == k:
            break
    return out


def choose_excerpts(takes_dir: Path = TAKES_DIR, pattern: str = TAKES_GLOB, plan=TAKE_PLAN,
                    n_s: float = EXCERPT_S, rng: np.random.Generator | None = None) -> list[dict]:
    """One excerpt per entry of `plan` (a take number), best-scoring first within each take, shuffled so the
    class-to-excerpt assignment is random. Returns [{take, start, path, score}]."""
    n = int(n_s * SR)
    out = []
    for take in sorted(set(plan)):
        p = takes_dir / pattern.format(n=take)
        x = RS.read_mono(p)
        for s, sc in pick_windows(x, plan.count(take), n):
            out.append({"take": take, "start": s, "path": str(p), "score": sc})
    if len(out) < len(plan):
        raise SystemExit(f"only {len(out)} usable excerpts for {len(plan)} targets")
    if rng is not None:
        out = [out[i] for i in rng.permutation(len(out))]
    return out


# ---------------------------------------------------------------- trial schedule

def schedule_pairs(has_clean: list[bool], rng: np.random.Generator, per_target: int = PER_TARGET,
                   slots=PAIR_SLOTS, tries: int = 5000) -> list[list[tuple[int, int]]]:
    """Per target, `per_target` distinct rung pairs; all of `slots` used once. Pairs with rung 5 only go to
    targets that have a clean rung."""
    k = len(has_clean)
    assert len(slots) == k * per_target
    for _ in range(tries):
        order = [slots[i] for i in rng.permutation(len(slots))]
        buckets: list[list[tuple[int, int]]] = [[] for _ in range(k)]
        ok = True
        for pr in order:
            fits = [i for i in range(k) if len(buckets[i]) < per_target and pr not in buckets[i]
                    and (5 not in pr or has_clean[i])]
            if not fits:
                ok = False
                break
            buckets[fits[int(rng.integers(0, len(fits)))]].append(pr)
        if ok:
            return buckets
    raise RuntimeError("no pair schedule found")


# ---------------------------------------------------------------- rendering (Native, one host)

def native_renderer(log=print):
    """(render(model, params, dry) -> audio, close). One Native host; one load per render (single-block library)."""
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)          # not resolved: the venv needs the symlink
    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeEngine

    cat = load_catalog()
    eng = NativeEngine(templates_dir=NL.library_templates())
    tv = NL.time_variant_models()

    def render(model: str, params: dict[str, float], dry: np.ndarray) -> np.ndarray:
        if model in tv or model in EXCLUDED:
            raise ValueError(f"{model} is excluded (time-variant or novelty)")
        p = NL.block_preset(model, cat)
        p.slots[0].params.update(params)
        eng.load(p)
        return np.asarray(eng.render(np.asarray(dry, np.float32)), np.float32)

    def defaults(model: str) -> dict[str, float]:
        m = cat["models"][model]["params"]
        return {k: float(v["default"]) for k, v in m.items() if not k.startswith("@")
                and isinstance(v.get("default"), (int, float)) and not isinstance(v.get("default"), bool)}

    render.defaults = defaults          # type: ignore[attr-defined]
    return render, eng.close


# ---------------------------------------------------------------- build

def _pair_name(spec: dict) -> str:
    kv = ",".join(f"{k}={v:g}" for k, v in spec["params"].items())
    return f"rung{spec['rung']}:{spec['model'].replace('HD2_Dist', '')}({kv})"


def build_session(name: str, seed: int | None = None, render=None, takes_dir: Path = TAKES_DIR,
                  takes_pattern: str = TAKES_GLOB, take_plan=TAKE_PLAN, out_root: Path = RS.RATINGS_DIR,
                  key_root: Path = RS.KEY_DIR, excerpt_s: float = EXCERPT_S, defaults_fn=None,
                  per_target: int = PER_TARGET, slots=PAIR_SLOTS, n_clean: int = N_CLEAN_TARGETS,
                  n_retests: int = N_RETESTS, log=print) -> tuple[Path, Path]:
    seed = secrets.randbelow(2**31) if seed is None else seed
    rng = np.random.default_rng(seed)
    close = None
    if render is None:
        render, close = native_renderer(log)
        defaults_fn = render.defaults
    defaults_fn = defaults_fn or (lambda m: {})
    n_s = int(excerpt_s * SR)
    n_t = len(CLASS_ORDER)
    folder, keyf = out_root / name, key_root / f"{name}.key.json"
    for d in (folder,):
        if d.exists():
            shutil.rmtree(d)
    folder.mkdir(parents=True)
    key_root.mkdir(parents=True, exist_ok=True)
    didir = key_root / f"{name}.di"
    if didir.exists():
        shutil.rmtree(didir)
    didir.mkdir()

    exc = choose_excerpts(takes_dir, takes_pattern, take_plan, excerpt_s, rng)
    clean_set = {int(i) for i in rng.permutation(n_t)[:n_clean]}
    defaults = {m: defaults_fn(m) for m in KNOBS}
    takes_x: dict[str, np.ndarray] = {}
    targets: list[dict] = []
    try:
        for ti, cls in enumerate(CLASS_ORDER):
            e = exc[ti]
            x = takes_x.setdefault(e["path"], RS.read_mono(Path(e["path"])))
            s0 = e["start"]
            lead = min(int(LEAD_S * SR), s0)
            seg = x[s0 - lead:s0 + n_s]                     # lead-in context, then the excerpt
            lad = make_ladder(cls, rng, defaults)
            specs = {"target": {"model": lad["model"], "params": lad["target"], "rung": 0}, **lad["rungs"]}
            raw: dict = {}
            for key, sp in specs.items():
                y = np.asarray(render(sp["model"], sp["params"], seg), np.float32)[lead:lead + n_s]
                if not np.all(np.isfinite(y)) or RS.rms_db(y) < -80:
                    log(f"  render failed or silent: {cls} {key} {sp['model']}")
                    raw[key] = None
                else:
                    raw[key] = y
            if raw["target"] is None:
                raise RuntimeError(f"target render for {cls} failed")
            for r, spare in ((3, "3b"), (4, "4b")):          # a failed rung falls back to its spare
                if raw.get(r) is None and raw.get(spare) is not None:
                    specs[r], raw[r] = specs[spare], raw[spare]
                if raw.get(r) is None:
                    raise RuntimeError(f"rung {r} for {cls} failed and has no spare")
            for r in (1, 2):
                if raw[r] is None:
                    raise RuntimeError(f"rung {r} for {cls} failed")
            dry_ex = np.array(x[s0:s0 + n_s], np.float32)
            for r in (1, 2, 3, 4):
                if np.array_equal(raw[r], raw["target"]):
                    raise RuntimeError(f"rung {r} for {cls} renders identically to the target")
            raw[5], specs[5] = dry_ex.copy(), {"model": "clean", "params": {}, "rung": 5}   # sanity trials may use it
            targets.append({"ti": ti, "class": cls, "label": CLASSES[cls][0], "excerpt": e, "specs": specs, "raw": raw,
                            "dry": dry_ex, "has_clean": ti in clean_set})
    finally:
        if close is not None:
            close()

    # which clips each trial uses
    pairs = schedule_pairs([t["has_clean"] for t in targets], rng, per_target, slots)
    plan: list[dict] = []
    for t, prs in zip(targets, pairs, strict=True):
        for a, b in prs:
            x, y = (a, b) if rng.random() < 0.5 else (b, a)          # which rung lands on side A
            plan.append({"kind": "regular", "t": t["ti"], "A": x, "B": y})
    san_t = [int(i) for i in rng.permutation(n_t)[:3]]
    plan.append({"kind": "sanity_target", "t": san_t[0], "A": "copy", "B": 5, "expected": "copy"})
    plan.append({"kind": "sanity_target", "t": san_t[1], "A": "copy", "B": 5, "expected": "copy"})
    plan.append({"kind": "sanity_near", "t": san_t[2], "A": "copy", "B": "copy2"})
    for p in plan:
        if p["kind"] != "regular" and rng.random() < 0.5:
            p["A"], p["B"] = p["B"], p["A"]
            if p["kind"] == "sanity_target":
                p["expected"] = "copy"
    regs = [i for i, p in enumerate(plan) if p["kind"] == "regular"]
    rep_src: list[int] = []
    for i in rng.permutation(len(regs)):          # retests of trials on different targets
        if plan[regs[int(i)]]["t"] not in {plan[regs[j]]["t"] for j in rep_src}:
            rep_src.append(int(i))
        if len(rep_src) == n_retests:
            break
    for j in rep_src:
        o = plan[regs[j]]
        plan.append({"kind": "repeat", "t": o["t"], "A": o["B"], "B": o["A"], "orig": regs[j]})

    def clip_of(t: dict, ref):
        if ref in ("copy", "copy2"):
            return t["raw"]["target"], {"name": "target copy" if ref == "copy" else "exact copy of the target",
                                        "role": "target_copy", "rung": 0, "model": t["specs"]["target"]["model"],
                                        "params": t["specs"]["target"]["params"]}
        sp = t["specs"][ref]
        role = "clean" if ref == 5 else "candidate"
        return t["raw"][ref], {"name": _pair_name(sp), "role": role, "rung": ref, "model": sp["model"],
                               "params": sp["params"]}

    # presentation order: random, repeats after their originals, no two neighbours on the same target where avoidable
    order = [int(v) for v in rng.permutation(len(plan))]
    for _ in range(300):
        pos = {s: i for i, s in enumerate(order)}
        bad = [s for s, p in enumerate(plan) if p["kind"] == "repeat" and pos[s] < pos[p["orig"]]]
        adj = sum(plan[order[i]]["t"] == plan[order[i + 1]]["t"] for i in range(len(order) - 1))
        if not bad and adj == 0:
            break
        if bad:
            s = bad[0]
            o = plan[s]["orig"]
            i, j = order.index(s), order.index(o)
            order[i], order[j] = order[j], order[i]
        else:
            i = next(i for i in range(len(order) - 1) if plan[order[i]]["t"] == plan[order[i + 1]]["t"])
            j = int(rng.integers(0, len(order)))
            order[i + 1], order[j] = order[j], order[i + 1]
    pos = {s: i for i, s in enumerate(order)}
    if any(p["kind"] == "repeat" and pos[s] < pos[p["orig"]] for s, p in enumerate(plan)):
        raise RuntimeError("could not order the retests after their originals")

    # loudness: every clip used, -23 LUFS, ONE shared peak guard for the session
    used: dict[tuple[int, object], np.ndarray] = {}
    for p in plan:
        t = targets[p["t"]]
        used[(p["t"], "T")] = t["raw"]["target"]
        for side in "AB":
            ref = p[side]
            used[(p["t"], "target" if ref in ("copy", "copy2") else ref)] = clip_of(t, ref)[0]
    faded = {k: RS.cut(v, 0, len(v)) for k, v in used.items()}
    lin = {k: RS.lufs(v) for k, v in faded.items()}
    g0 = {k: (RS.TARGET_LUFS - lin[k] if lin[k] > -69.9 else 0.0) for k in faded}
    worst = max(float(np.max(np.abs(faded[k]))) * 10 ** (g0[k] / 20) for k in faded)
    guard = 20 * math.log10(RS.PEAK_CEIL / worst) if worst > RS.PEAK_CEIL else 0.0
    gain = {k: g0[k] + guard for k in faded}

    def leveled(k):
        return (faded[k] * 10 ** (gain[k] / 20)).astype(np.float32)

    trials, ktrials, id_of = [], [], {}
    tinfo = {}
    for t in targets:
        di_file = didir / f"target{t['ti'] + 1}_di.wav"
        sf.write(str(di_file), t["dry"], SR, subtype="FLOAT")
        tinfo[t["ti"]] = {"class": t["class"], "label": t["label"], "model": t["specs"]["target"]["model"],
                          "params": t["specs"]["target"]["params"], "take": t["excerpt"]["take"],
                          "excerpt_start_s": t["excerpt"]["start"] / SR, "excerpt_s": excerpt_s,
                          "orig_db": RS.rms_db(used[(t["ti"], "T")]), "has_clean": t["has_clean"],
                          "di": str(di_file), "rungs": {str(k): {"model": v["model"], "params": v["params"], "rung": v["rung"]}
                                                       for k, v in t["specs"].items()}}
    for rank, si in enumerate(sorted(range(len(plan)), key=lambda i: pos[i]), start=1):
        p = plan[si]
        t = targets[p["t"]]
        _, ai = clip_of(t, p["A"])
        _, bi = clip_of(t, p["B"])
        keys = {"T": (p["t"], "T"), "A": (p["t"], "target" if p["A"] in ("copy", "copy2") else p["A"]),
                "B": (p["t"], "target" if p["B"] in ("copy", "copy2") else p["B"])}
        if p["kind"] == "sanity_near":
            keys["A"] = keys["B"] = (p["t"], "T")
        tag = secrets.token_hex(4)
        files = {"T": f"{tag}_t.wav", "A": f"{tag}_a.wav", "B": f"{tag}_b.wav"}
        for r in "TAB":
            RS.write_clip(folder / files[r], leveled(keys[r]))
        for side, info in (("A", ai), ("B", bi)):
            k = keys[side]
            info["orig_db"] = RS.rms_db(used[k])
            info["source"] = f"target {p['t'] + 1} rung {info['rung']}"
        expected = None
        if p["kind"] == "sanity_target":
            expected = "A" if ai["role"] == "target_copy" else "B"
        li = [lin[keys[r]] for r in "TAB"]
        gi = [gain[keys[r]] for r in "TAB"]
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": p["kind"], "family": t["label"], "target": p["t"] + 1,
                        "excerpt_start_s": t["excerpt"]["start"] / SR, "excerpt_s": excerpt_s, "A": ai, "B": bi,
                        "files": files, "target_orig_db": tinfo[p["t"]]["orig_db"], "di": tinfo[p["t"]]["di"],
                        "expected": expected,
                        "repeat_of": id_of[p["orig"]] if p["kind"] == "repeat" else None,
                        "loudness": {"lufs_in": li, "gain_db": gi, "lufs_out": [RS.lufs(leveled(keys[r])) for r in "TAB"]}})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": name, "excerpt_s": excerpt_s, "trials": trials}, indent=1))
    note = ("All clips are the same guitar notes; only the Helix drive setting differs. Judge which of A and B has "
            "the tone closer to the Target.")
    (folder / "index.html").write_text(RS.render_page(name, trials, note))
    keyf.write_text(json.dumps({"session": name, "version": 2, "seed": seed, "peak_guard_db": guard,
                                "target_lufs": RS.TARGET_LUFS, "rung_meaning": {str(k): v for k, v in RUNGS.items()},
                                "targets": {str(k + 1): v for k, v in tinfo.items()}, "trials": ktrials}, indent=1))
    return folder, keyf


# ---------------------------------------------------------------- withdrawing v1

WITHDRAWN_NOTICE = ("<!doctype html><meta charset=utf-8><title>withdrawn</title>"
                    "<p>human-ratings-v1 is WITHDRAWN (it compared the original guitarist with renders of the human's "
                    "own playing, so it judged performance, not tone). Use human-ratings-v2.</p>\n")


def withdraw(session: str = "human-ratings-v1", out_root: Path = RS.RATINGS_DIR) -> int:
    """Replace the page by a one-line notice and delete the served audio (it was song audio). Returns files deleted."""
    folder = out_root / session
    n = 0
    if folder.is_dir():
        for p in folder.iterdir():
            if p.suffix in (".wav", ".json"):
                p.unlink()
                n += 1
        (folder / "index.html").write_text(WITHDRAWN_NOTICE)
    return n


def folder_mb(*paths: Path) -> float:
    return sum(f.stat().st_size for p in paths for f in Path(p).rglob("*") if f.is_file()) / 1e6


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="human-ratings-v2")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    f, k = build_session(a.name, seed=a.seed)
    print(f"page folder {f}, key {k}, {folder_mb(f, RS.KEY_DIR / (a.name + '.di')):.1f} MB")
