#!/usr/bin/env python3
"""T198: tune the DS-Sample tutorial reference chain three ways on Native, confirm on the Stomp,
and build the blind listening session human-ratings-v6.

  parity   Native at the tutorial values against the Stomp reference (T197 raw reamp of take2)
  cccheck  every tuned knob: CC route moves the sound and equals a fresh load of the same value
  search   --arm a|b|c : CMA-ES from the tutorial values on Native (a: mrstft_v2 above 250 Hz,
           b: mrstft_v2 full range, c: AFx-Rep distance); level guard on LUFS
  report   cross-score every arm's best and the tutorial, write the tuned .hlx files
  stomp    render tutorial, each arm's best (and the human's current settings if they differ) on the
           Stomp through take2: edit buffer only, never flash, tutorial chain put back at the end
  session  build human-ratings-v6 (blind pairs + 1-5 ratings, LUFS-matched)

Knobs (human-approved 2026-10-20): Brit 2204 Drive, Bass, Mid, Treble, Presence; the ten 10 Band Graphic
bands (|dB| <= 12); Scream 808 Gain and Tone. Delay, reverb, cab, 808 Level, amp ChVol and Master fixed.
Song audio stays local (cache/ is gitignored).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
HV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HV)
os.environ.setdefault("TONEMATCH_NATIVE_MANY_HOSTS", "4")      # at most 4 hosts (human's rule)

import ds_sample_reference_chain as REF

SR = 48_000
LIVE = ROOT / "cache" / "matches" / "DS-Sample-live"
TUNED = LIVE / "tuned"
WORK = TUNED / "work"
TEMPLATES = ROOT / "cache" / "native" / "templates_files" / "reference_chain_native"
HLX = REF.HLX                                  # what the Stomp gets (T197 file)
# Native needs the amp as type 3 (Amp+Cab) or it ignores the linked cab (found in T198: the T197 file has
# the amp as type 1, which the Stomp accepts but Native renders without the cab; every cab knob was dead).
NATIVE_HLX = WORK / "reference_chain_native.hlx"
STOMP_REF_RAW = ROOT / "cache" / "stomp" / "hw" / "20261020T200228Z-t197-capture" / "reference_raw.wav"

EQ_NAMES = ["31p25Hz", "62p5Hz", "125Hz", "250Hz", "500Hz", "1kHz", "2kHz", "4kHz", "8kHz", "16kHz"]
EQ_HZ = [31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_LIM = 12.0
# slots of the loaded preset: 0 Scream 808, 1 Brit 2204, 2 cab, 3 Simple Delay, 4 10 Band Graphic, 5 Hall
KNOBS: list[tuple[int, str, float, float]] = (
    [(0, "Gain", 0.0, 1.0), (0, "Tone", 0.0, 1.0)]
    + [(1, n, 0.0, 1.0) for n in ("Drive", "Bass", "Mid", "Treble", "Presence")]
    + [(4, n, -EQ_LIM, EQ_LIM) for n in EQ_NAMES])
TUTORIAL: dict[tuple[int, str], float] = {}
LOW_EQ = {(4, "31p25Hz"), (4, "62p5Hz"), (4, "125Hz")}          # frozen in arm a (below the 250 Hz cut)
LOUD_FREE_DB = 3.0      # level guard: free drift in LUFS vs the tutorial render
LOUD_PEN = 0.05         # score units per dB beyond that
ARMS = {"a": "mrstft_v2 aligned, bins below 250 Hz excluded", "b": "mrstft_v2 aligned, full range",
        "c": "AFx-Rep distance"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------------- data


def data():
    di, target, where = REF.excerpts()
    return di, target, where


def catalog():
    from tonematch.catalog import load_catalog
    return load_catalog()


def tutorial_preset():
    from tonematch import preset as P
    return P.read_hlx(NATIVE_HLX, catalog())


def tutorial_values() -> dict[tuple[int, str], float]:
    p = tutorial_preset()
    return {(s, n): float(p.slots[s].params.get(n, 0.0)) for s, n, _, _ in KNOBS}


def cc_snap(slot_name: tuple[int, str], v: float, tpl) -> float:
    from tonematch.engines.native import CC_STEPS, cc_norm, cc_value
    lo, hi = tpl.ranges.get(f"{slot_name[0]}:{slot_name[1]}", [0.0, 1.0])
    return float(cc_value(float(lo), float(hi), round(cc_norm(float(v), float(lo), float(hi)) * CC_STEPS)))


def x_to_values(x, arm: str | None = None, tpl=None, tut=None) -> dict[tuple[int, str], float]:
    out = {}
    for xi, (s, n, lo, hi) in zip(x, KNOBS, strict=True):
        if arm == "a" and (s, n) in LOW_EQ:
            v = tut[(s, n)]
        else:
            v = lo + float(np.clip(xi, 0, 1)) * (hi - lo)
        out[(s, n)] = cc_snap((s, n), v, tpl) if tpl is not None else v
    return out


def values_to_x(vals):
    return np.array([(vals[(s, n)] - lo) / (hi - lo) for s, n, lo, hi in KNOBS])


def preset_with(values: dict[tuple[int, str], float]):
    p = tutorial_preset()
    for (s, n), v in values.items():
        p.slots[s].params[n] = float(v)
    return p


# ---------------------------------------------------------------------------------- engine


def make_engine(templates=TEMPLATES):
    from tonematch.engines.native import NativeEngine
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    return NativeEngine(templates_dir=templates, autocapture=False)


def render_batch(eng, di, vals_list: list[dict]) -> list[np.ndarray]:
    ys = eng.render_many(di, [dict(v) for v in vals_list])
    return [np.asarray(y, np.float32)[:len(di)] for y in ys]


# ---------------------------------------------------------------------------------- scores


def lufs(x) -> float:
    from tonematch.separation.audio import integrated_loudness
    return integrated_loudness(np.asarray(x, np.float64)[None])


def highpass(x, fc=250.0):
    from scipy.signal import butter, sosfiltfilt
    return sosfiltfilt(butter(4, fc, "highpass", fs=SR, output="sos"), np.asarray(x, np.float64)).astype(np.float32)


class Mrstft:
    """mrstft_v2 (frozen 2.0) with cached target spectra; level-invariant (unit RMS)."""

    def __init__(self, target, hp: bool):
        from tonematch.metrics import mrstft_v2 as M
        self.M, self.hp = M, hp
        self.cfg = M.DEFAULT.with_(f_lo=250.0) if hp else M.DEFAULT
        t = highpass(target) if hp else np.asarray(target, np.float32)
        self.n = len(t)
        t = t / M.level_scale(t, self.cfg.level)
        self.pt = {n: M.spectrum(t, n, M.hop_for(n, self.cfg)) for n in self.cfg.fft_sizes}

    def __call__(self, y) -> float:
        M = self.M
        c = highpass(y[:self.n]) if self.hp else np.asarray(y[:self.n], np.float32)
        c = c / M.level_scale(c, self.cfg.level)
        parts = [M.compare(self.pt[n], M.spectrum(c, n, M.hop_for(n, self.cfg)), n, self.cfg, None)
                 for n in self.cfg.fft_sizes]
        return M.combine(parts, self.cfg)


class Afx:
    def __init__(self, target):
        from tonematch.metrics import afxrep
        self.A = afxrep
        self.ft = afxrep.features(np.asarray(target, np.float32))

    def many(self, ys) -> list[float]:
        out = []
        for i in range(0, len(ys), 4):
            fs = self.A.get_embedder().embed_batch([np.asarray(y, np.float32) for y in ys[i:i + 4]])
            out += [self.A.distance(self.ft, f) for f in fs]
        return out


def scorer(arm: str, target):
    if arm == "c":
        a = Afx(target)
        return lambda ys: a.many(ys)
    m = Mrstft(target, hp=(arm == "a"))
    return lambda ys: [m(y) for y in ys]


# ---------------------------------------------------------------------------------- bands

BAND_EDGES = [20, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000, 20000]


def band_db(x) -> np.ndarray:
    from scipy.signal import welch
    f, p = welch(np.asarray(x, np.float64), SR, nperseg=8192)
    out = []
    for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:], strict=True):
        k = (f >= lo) & (f < hi)
        out.append(10 * np.log10(p[k].sum() + 1e-20))
    return np.array(out)


def band_labels():
    return [f"{lo:g}-{hi:g}" for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:], strict=True)]


def band_diff(a, b) -> list[float]:
    """a minus b per band, after equal total power (so a pure level offset does not show)."""
    da, db = band_db(a), band_db(b)
    da -= 10 * np.log10(np.sum(10 ** (da / 10)))
    db -= 10 * np.log10(np.sum(10 ** (db / 10)))
    return [round(float(v), 2) for v in da - db]


def null_db(ref, y) -> float:
    n = min(len(ref), len(y))
    r, y = np.asarray(ref[:n], np.float64), np.asarray(y[:n], np.float64)
    return float(10 * np.log10(np.sum((r - y) ** 2) / (np.sum(r ** 2) + 1e-20) + 1e-20))


def best_lag(ref, y, max_ms=20) -> int:
    n = min(len(ref), len(y))
    a, b = np.asarray(ref[:n], np.float64), np.asarray(y[:n], np.float64)
    m = int(max_ms * SR / 1000)
    sz = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(a, sz) * np.conj(np.fft.rfft(b, sz)), sz)
    idx = np.concatenate([np.arange(m + 1), np.arange(-m, 0)])
    cand = np.concatenate([cc[:m + 1], cc[-m:]])
    return int(idx[int(np.argmax(cand))])


# ---------------------------------------------------------------------------------- commands


def cmd_parity(a) -> int:
    import soundfile as sf
    WORK.mkdir(parents=True, exist_ok=True)
    di, target, where = data()
    stomp = sf.read(STOMP_REF_RAW, dtype="float32")[0][:len(di)]
    eng = make_engine()
    try:
        eng.load(tutorial_preset())
        y = np.asarray(eng.render(di), np.float32)[:len(di)]
        y2 = np.asarray(eng.render(di), np.float32)[:len(di)]
    finally:
        eng.close()
    np.save(WORK / "native_tutorial.npy", y)
    lag = best_lag(stomp, y)
    ys = np.roll(y, lag)
    from tonematch.metrics import mrstft_v2 as M
    res = {"native_repeat_null_db": round(null_db(y, y2), 1), "lag_samples_stomp_vs_native": lag,
           "null_db_unaligned": round(null_db(stomp, y), 1), "null_db_aligned": round(null_db(stomp, ys), 1),
           "lufs_stomp": round(lufs(stomp), 2), "lufs_native": round(lufs(y), 2),
           "rms_db_diff_native_minus_stomp": round(20 * np.log10(np.std(y) / np.std(stomp)), 2),
           "mrstft_v2_native_vs_stomp": round(M.term(stomp, ys), 4),
           "mrstft_v2_native_vs_target": round(M.term(target, ys), 4),
           "mrstft_v2_stomp_vs_target": round(M.term(target, stomp), 4),
           "band_labels_hz": band_labels(), "band_db_native_minus_stomp": band_diff(y, stomp),
           "band_db_stomp_minus_target": band_diff(stomp, target)}
    # a coarse per-second gain difference (envelope parity)
    k = SR
    g = [20 * np.log10((np.std(y[i:i + k]) + 1e-9) / (np.std(stomp[i:i + k]) + 1e-9)) for i in range(0, len(y) - k, k)]
    res["per_second_level_diff_db_min_max"] = [round(float(min(g)), 2), round(float(max(g)), 2)]
    (WORK / "parity.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float))
    return 0


def cmd_cccheck(a) -> int:
    from tonematch.engines.native import ChainTemplate
    di, _, _ = data()
    clip = di[: 4 * SR]
    tpl = next(iter(ChainTemplate.load_all(TEMPLATES).values()))
    tut = tutorial_values()
    eng = make_engine()
    rows = []
    try:
        p0 = tutorial_preset()
        eng.load(p0)
        base = np.asarray(eng.render(clip), np.float32)
        for (s, n, lo, hi) in KNOBS:
            ref = f"{s}:{n}"
            if ref not in tpl.map:
                rows.append({"knob": ref, "error": "not in the template CC map"})
                continue
            v = cc_snap((s, n), tut[(s, n)] + (0.25 if hi == 1.0 else 6.0) * (1 if tut[(s, n)] < (hi + lo) / 2 else -1), tpl)
            eng.load(p0)
            eng.set_params({(s, n): v})
            y_cc = np.asarray(eng.render(clip), np.float32)
            p1 = copy.deepcopy(p0)
            p1.slots[s].params[n] = v
            eng.load(p1)
            y_ld = np.asarray(eng.render(clip), np.float32)
            rows.append({"knob": ref, "cc": tpl.map[ref], "range": tpl.ranges.get(ref), "verified": ref in tpl.verified,
                         "from": tut[(s, n)], "to": v, "moves_sound_null_db": round(null_db(base, y_cc), 1),
                         "cc_vs_fresh_load_null_db": round(null_db(y_ld, y_cc), 1)})
            log(rows[-1])
    finally:
        eng.close()
    ok = all("error" not in r and r["moves_sound_null_db"] > -60 and r["cc_vs_fresh_load_null_db"] < -60 for r in rows)
    (WORK / "cccheck.json").write_text(json.dumps({"all_ok": ok, "rows": rows}, indent=1))
    print("ALL OK" if ok else "SOME NOT OK")
    return 0 if ok else 1


def cmd_search(a) -> int:
    from tonematch.engines.host_budget import LoadGuard
    from tonematch.engines.native import ChainTemplate
    from tonematch.search.cmaes import CMAES

    arm = a.arm
    guard = LoadGuard(a.max_load)
    di, target, _ = data()
    tpl = next(iter(ChainTemplate.load_all(TEMPLATES).values()))
    tut = tutorial_values()
    ref_y = np.load(WORK / "native_tutorial.npy")
    ref_l = lufs(ref_y)
    score = scorer(arm, target)
    x0 = values_to_x(tut)
    free = [i for i, (s, n, _, _) in enumerate(KNOBS) if not (arm == "a" and (s, n) in LOW_EQ)]
    es = CMAES(x0, sigma0=a.sigma, popsize=a.pop, seed=a.seed)
    eng = make_engine()
    t0 = time.time()
    hist, best = [], {"loss": 1e9}
    renders = 0

    def evaluate(xs):
        nonlocal renders, best
        vals = [x_to_values(np.clip(x, 0, 1), arm, tpl, tut) for x in xs]
        w = guard.wait(lambda d: log(f"busy cores {d:.1f}: pausing"), poll_s=10)
        ys = render_batch(eng, di, vals)
        renders += len(ys)
        raw = score(ys)
        out = []
        for x, v, y, r in zip(xs, vals, ys, raw, strict=True):
            d = lufs(y) - ref_l
            pen = LOUD_PEN * max(0.0, abs(d) - LOUD_FREE_DB)
            out.append(r + pen)
            if r + pen < best["loss"]:
                best = {"loss": float(r + pen), "raw": float(r), "dlufs": float(d),
                        "values": {f"{s}:{n}": v[(s, n)] for s, n, _, _ in KNOBS}, "render": renders}
        return out

    try:
        eng.load(tutorial_preset())
        l0 = evaluate([x0])[0]
        start = dict(best)
        log(f"arm {arm}: start (CC-snapped tutorial) loss {l0:.4f}")
        gen = 0
        while renders < a.budget:
            X = es.ask()
            if arm == "a":
                X[:, [i for i in range(len(KNOBS)) if i not in free]] = x0[[i for i in range(len(KNOBS)) if i not in free]]
            Xc = es.clip(X)
            f = evaluate(Xc)
            es.tell(X, f)
            gen += 1
            hist.append({"gen": gen, "renders": renders, "best": best["loss"], "gen_best": float(min(f)), "sigma": es.sigma})
            log(f"gen {gen} renders {renders} gen_best {min(f):.4f} best {best['loss']:.4f} sigma {es.sigma:.3f}")
    finally:
        eng.close()
    res = {"arm": arm, "what": ARMS[arm], "seed": a.seed, "popsize": es.lam, "sigma0": a.sigma, "renders": renders,
           "wall_s": round(time.time() - t0, 1), "start_loss": start["loss"], "best": best, "history": hist,
           "loudness_guard": {"free_db": LOUD_FREE_DB, "penalty_per_db": LOUD_PEN, "reference_lufs": round(ref_l, 2)},
           "frozen_in_arm": sorted(f"{s}:{n}" for s, n in LOW_EQ) if arm == "a" else []}
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / f"search_{arm}_seed{a.seed}.json").write_text(json.dumps(res, indent=1, default=float))
    log(f"arm {arm} done: {renders} renders, {res['wall_s']} s; best {best['loss']:.4f} (start {start['loss']:.4f})")
    return 0


# ---------------------------------------------------------------------------------- report


def load_arm(arm: str, seed: int = 0) -> dict:
    return json.loads((WORK / f"search_{arm}_seed{seed}.json").read_text())


def arm_values(arm: str) -> dict[tuple[int, str], float]:
    b = load_arm(arm)["best"]["values"]
    return {(int(k.split(":")[0]), k.split(":")[1]): float(v) for k, v in b.items()}


def stomp_preset(values=None):
    """The preset the Stomp gets (T197 file: amp type 1, the way the pedal accepted it) with tuned values."""
    from tonematch import preset as P
    p = P.read_hlx(HLX, catalog())
    for (s, n), v in (values or {}).items():
        p.slots[s].params[n] = float(v)
    return p


def candidate_sets() -> dict[str, dict[tuple[int, str], float]]:
    out = {"tutorial": tutorial_values()}
    for a in ARMS:
        out[a] = arm_values(a)
    return out


def write_hlx_files(sets) -> dict:
    import subprocess

    from tonematch import preset as P
    TUNED.mkdir(parents=True, exist_ok=True)
    cat = catalog()
    rep = {}
    for name, vals in sets.items():
        p = stomp_preset(vals)
        p.name = {"tutorial": "DS Tutorial", "a": "DS Tune A", "b": "DS Tune B", "c": "DS Tune C"}.get(name, f"DS {name}"[:16])
        viol = [str(v) for v in P.validate(p, cat, None, engine="stomp") if v.severity == "fail"]
        f = TUNED / f"ds_{name}.hlx"
        P.write_hlx(p, f)
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(f)], cwd=ROOT,
                            capture_output=True).returncode
        rep[name] = {"file": str(f.relative_to(ROOT)), "validate_fails": viol, "hlx_validate_exit": rc}
    return rep


def score_table(ys: dict[str, np.ndarray], target, ref_name="tutorial") -> dict:
    """Every candidate under the three scores (raw, level-invariant) and its band balance and LUFS."""
    sa, sb, sc = scorer("a", target), scorer("b", target), scorer("c", target)
    names = list(ys)
    arr = [ys[n] for n in names]
    out = {}
    ra, rb, rc = sa(arr), sb(arr), sc(arr)
    ref_l = lufs(ys[ref_name])
    for i, n in enumerate(names):
        out[n] = {"mrstft_v2_above250": round(float(ra[i]), 4), "mrstft_v2_full": round(float(rb[i]), 4),
                  "afxrep": round(float(rc[i]), 4), "lufs": round(lufs(ys[n]), 2),
                  "dlufs_vs_tutorial": round(lufs(ys[n]) - ref_l, 2), "band_db_minus_target": band_diff(ys[n], target)}
    return out


def cmd_report(a) -> int:
    di, target, _ = data()
    sets = candidate_sets()
    hl = write_hlx_files(sets)
    eng = make_engine()
    try:
        eng.load(tutorial_preset())
        ys = {"tutorial": np.asarray(eng.render(di), np.float32)[:len(di)]}
        base = tutorial_preset()
        for n in ARMS:
            ys[n] = render_batch(eng, di, [sets[n]])[0]
    finally:
        eng.close()
    for n, y in ys.items():
        np.save(WORK / f"native_{n}.npy", y)
    tab = score_table(ys, target)
    tut = sets["tutorial"]
    settings = {n: {f"{s}:{k}": round(v, 4) for (s, k), v in sets[n].items()} for n in sets}
    res = {"scores_native": tab, "settings": settings, "hlx": hl, "band_labels_hz": band_labels(),
           "knob_names": {f"{s}:{n}": ("Scream808 " if s == 0 else "Brit2204 " if s == 1 else "EQ ") + n for s, n, _, _ in KNOBS},
           "arms": {n: {k: load_arm(n)[k] for k in ("renders", "wall_s", "start_loss", "popsize", "sigma0")} for n in ARMS},
           "arm_best_loss": {n: load_arm(n)["best"]["loss"] for n in ARMS}}
    (WORK / "report_native.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps({"scores": tab, "hlx": hl}, indent=1, default=float)[:6000])
    return 0


# ---------------------------------------------------------------------------------- stomp


def chain_values(chain: dict) -> dict[tuple[int, str], float]:
    """The tuned knobs as the pedal holds them (by block model), for the human-current comparison."""
    by_model = {s.get("model_id"): s for s in chain["slots"] if s["kind"] == "Block"}
    out = {}
    for (s, n, _, _) in KNOBS:
        model = {0: "HD2_DistScream808", 1: "HD2_AmpBrit2204", 4: "HD2_EQGraphic10Band"}[s]
        blk = by_model.get(model)
        if blk is None:
            raise RuntimeError(f"the pedal chain has no {model}")
        for p in blk["params"]:
            if p.get("id") == n:
                out[(s, n)] = float(p["value"])
    return out


def cmd_stomp(a) -> int:
    """Render tutorial / arms / (human's current settings) on the Stomp through take2, edit buffer only."""
    import dataclasses

    import soundfile as sf

    from tonematch import pedal_lock
    from tonematch.engines.stomp import reamp_hlx_dict
    from tonematch.engines.stomp_readback import readback_diff

    REF._env()
    if REF.hx_edit_running():
        print("HX Edit is running: quit it first. Nothing done.")
        return 2
    di, target, where = data()
    sets = candidate_sets()
    out_dir = TUNED / "stomp"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = ROOT / "cache" / "stomp" / "hw" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-t198-confirm"
    run_dir.mkdir(parents=True, exist_ok=True)
    res: dict = {"run_dir": str(run_dir), "steps": [], "renders": {}}

    def out(msg):
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
        res["steps"].append(msg)

    tut_p = stomp_preset()
    try:
        with pedal_lock.session("T198 DS-Sample tuned chains: confirm on the Stomp"):
            pos = REF.pedal_position()
            res["preflight"] = pos
            out(f"pedal on {pos.get('label')} '{pos.get('name')}' fw {pos.get('firmware')}")
            if (pos["setlist"], pos["index"]) != REF.SCRATCH[:2]:
                out("not slot 04C: nothing done")
                res["error"] = "not on 04C"
                return 3
            eng = REF._open(run_dir, 6, "T198 reamp take2 through tutorial and tuned chains")
            try:
                # 1. READ the current edit buffer BEFORE any write (open() only backs up and reads).
                cur = eng.original_chain
                (TUNED / "stomp_edit_buffer_before.json").write_text(json.dumps(cur, indent=1))
                out(f"edit buffer read and saved: {eng.backup_path.name} (+ chain json in {run_dir.name})")
                diff = readback_diff(cur, reamp_hlx_dict(tut_p, 1), None, check_input=True)
                res["current_vs_tutorial"] = diff.as_dict()
                cv = chain_values(cur)
                tv = tutorial_values()
                knob_diff = {f"{s}:{n}": [tv[(s, n)], cv[(s, n)]] for (s, n, _, _) in KNOBS if abs(tv[(s, n)] - cv[(s, n)]) > 1e-4}
                res["tuned_knob_differences_tutorial_vs_current"] = knob_diff
                out(f"current buffer vs tutorial: ok={diff.ok}, {len(diff.mismatches)} mismatches: {diff.mismatches[:12]}")
                order = ["tutorial", "a", "b", "c"]
                human_set = None
                if not diff.ok:
                    # the human's current settings as an extra candidate: every parameter of the tuned blocks as held
                    human_set = dict(cv)
                    res["human_current_values"] = {f"{s}:{n}": v for (s, n), v in cv.items()}
                    out("DIFFERENT from the tutorial: human's current settings added as a candidate")
                    order = ["human_current"] + order
                # a full copy of the human's current preset, for the extra candidate
                cand_presets = {"tutorial": tut_p}
                for n in ("a", "b", "c"):
                    cand_presets[n] = stomp_preset(sets[n])
                if human_set is not None:
                    hp = stomp_preset(human_set)
                    # carry non-tuned differences too (e.g. the 808 Level, delay, reverb): by block model
                    by_model = {s.get("model_id"): s for s in cur["slots"] if s["kind"] == "Block"}
                    by_paired = {s.get("paired_id"): s for s in cur["slots"] if s["kind"] == "Block" and s.get("paired_id")}
                    for sl in hp.slots:
                        if sl.category == "cab":
                            blk = by_paired.get(sl.model_id)
                            pp = (blk or {}).get("paired_params") or []
                        else:
                            blk = by_model.get(sl.model_id)
                            pp = (blk or {}).get("params") or []
                        for q in pp:
                            if q.get("id") in sl.params and abs(float(sl.params[q["id"]]) - float(q["value"])) > 1e-6:
                                sl.params[q["id"]] = float(q["value"])
                    cand_presets["human_current"] = hp
                    (TUNED / "human_current_chain_values.json").write_text(json.dumps(
                        {s.model_id: s.params for s in hp.slots}, indent=1, default=float))
                order = order + ["tutorial_repeat"]
                cand_presets["tutorial_repeat"] = tut_p
                x = np.concatenate([di, np.zeros(int(REF.TAIL_S * SR), np.float32)])
                for name in order:
                    p = cand_presets[name]
                    t0 = time.perf_counter()
                    eng.load(p)
                    rb = eng.last_readback or {}
                    routing = REF._input_routing(eng)
                    out(f"{name}: loaded in {time.perf_counter() - t0:.1f} s; readback ok {rb.get('ok')}; input {routing}")
                    if rb.get("ok") is not True or routing != 6:
                        raise RuntimeError(f"readback not ok or input not USB 5/6 for {name}: {json.dumps(rb)[:600]}")
                    t0 = time.perf_counter()
                    y = np.asarray(eng.render(x), np.float32)
                    sf.write(out_dir / f"{name}_raw.wav", y, SR, subtype="FLOAT")
                    res["renders"][name] = {"render_s": round(time.perf_counter() - t0, 1), "offset": eng.last_offset,
                                            "rms_dbfs": round(float(20 * np.log10(np.sqrt(np.mean(y[:len(di)] ** 2)) + 1e-12)), 2),
                                            "peak_dbfs": round(float(20 * np.log10(np.max(np.abs(y)) + 1e-12)), 2)}
                    out(f"{name}: rendered {res['renders'][name]}")
            finally:
                if not eng._closed:
                    # tutorial chain back in the edit buffer, Input on Main L/R; 04C is NOT restored (human's rule)
                    try:
                        eng.cal = dataclasses.replace(eng.cal, input_usb56=REF.INPUT_MAIN_LR)
                        eng.load(tut_p)
                        rb = eng.last_readback or {}
                        fin = readback_diff(eng.chain, reamp_hlx_dict(tut_p, REF.INPUT_MAIN_LR), None, check_input=True)
                        res.update(final_input_routing=REF._input_routing(eng), final_readback_ok=bool(fin.ok),
                                   final_mismatches=fin.mismatches)
                        out(f"tutorial chain put back; input {res['final_input_routing']} (Main L/R = 1); readback ok {fin.ok}")
                    except Exception as e:  # noqa: BLE001
                        res["final_error"] = f"{type(e).__name__}: {e}"
                        out(f"FINAL PUT-BACK FAILED: {res['final_error']}")
                    REF._leave_loaded(eng, res, out)
    except Exception as e:  # noqa: BLE001 - reported, never retried
        res["error"] = f"{type(e).__name__}: {e}"
        out(f"ERROR {res['error']} -- stopped, no retry")
    finally:
        res["lock_released"] = not pedal_lock.held_here()
        (TUNED / "stomp_confirm_summary.json").write_text(json.dumps(res, indent=1, default=str))
    return 1 if "error" in res else 0


# ---------------------------------------------------------------------------------- session

SESSION = "human-ratings-v6"
PROTOCOL = (
    "Per trial: Target = the htdemucs_6s guitar stem of DS-Sample, song 0:30.1-0:50.0 (19.9 s, mono mid); A and B = two "
    "of the candidate settings of the Scream 808 > Brit 2204 + 4x12 Greenback 25 > Simple Delay > 10 Band Graphic > "
    "Hall chain, each rendered on the real HX Stomp (edit buffer, reamp) from the human's own take2 DI. Candidates: "
    "tutorial (the tutorial settings), arm a (mrstft_v2 aligned, bins below 250 Hz excluded), arm b (mrstft_v2 aligned, "
    "full range), arm c (AFx-Rep distance), plus the human's current pedal settings when they differed from the tutorial. "
    "Every pair of candidates is one trial; two trials are repeated with the sides swapped. Every clip (target included) "
    "is set to -23 LUFS (BS.1770 K-weighted integrated loudness) with one shared peak guard; clips are not matched by "
    "RMS. The key (which candidate is A and B) is in cache/ratings and stays hidden until the answers are in. The human "
    "rates A and B 1 to 5 and says which is closer. No score is used to choose pairs or judge answers; nothing is "
    "admitted by this session.")


def cmd_session(a) -> int:
    import secrets

    import soundfile as sf

    import rating_session as RS
    import rating_v3 as R3

    di, target, where = data()
    n = len(di)
    names = ["tutorial", "a", "b", "c"]
    if (TUNED / "stomp" / "human_current_raw.wav").exists():
        names.append("human_current")
    raw = {k: sf.read(TUNED / "stomp" / f"{k}_raw.wav", dtype="float32")[0][:n] for k in names}
    seed = secrets.randbelow(2**31) if a.seed is None else a.seed
    rng = np.random.default_rng(seed)
    folder, keyf = RS.RATINGS_DIR / SESSION, RS.KEY_DIR / f"{SESSION}.key.json"
    if keyf.exists() and not a.rebuild:
        raise SystemExit(f"{keyf} exists (a session with answers may depend on it); pass --rebuild to replace")
    import shutil
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
    rep = [int(v) for v in rng.permutation(len(plan))[:2]]
    for j in rep:
        o = plan[j]
        plan.append({"kind": "repeat", "t": o["t"], "A": o["B"], "B": o["A"], "orig": j})
    # order: shuffled regular trials, each retest dropped in at a random place at least 3 trials after its original
    order = [int(v) for v in rng.permutation(len([q for q in plan if q["kind"] == "regular"]))]
    for si, q in enumerate(plan):
        if q["kind"] == "repeat":
            at = order.index(q["orig"])
            order.insert(int(rng.integers(min(at + 3, len(order)), len(order) + 1)), si)
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
    keyf.write_text(json.dumps({"session": SESSION, "version": 6, "seed": seed, "target_lufs": RS.TARGET_LUFS,
                                "peak_guard_db": info["gain_db"][0] - (RS.TARGET_LUFS - info["lufs_in"][0]),
                                "loudness_match": "LUFS (BS.1770 K-weighted), one shared peak guard; not RMS",
                                "lufs_in": dict(zip(ks, info["lufs_in"], strict=True)), "gain_db": gains,
                                "candidates": names, "protocol": PROTOCOL, "where": where, "trials": ktrials},
                               indent=1, default=float))
    print(f"page: {RS.URL_BASE}/{SESSION}/index.html ({len(trials)} trials, candidates {names})\nkey: {keyf}")
    return 0


def cmd_analyze(a) -> int:
    import soundfile as sf

    from tonematch.metrics import mrstft_v2 as M
    di, target, _ = data()
    n = len(di)
    names = ["tutorial", "a", "b", "c"]
    st = {k: sf.read(TUNED / "stomp" / f"{k}_raw.wav", dtype="float32")[0][:n] for k in names + ["tutorial_repeat"]}
    nat = {k: np.load(WORK / f"native_{k}.npy")[:n] for k in names}
    t197 = sf.read(STOMP_REF_RAW, dtype="float32")[0][:n]
    res = {"stomp_repeat_floor": {
        "mrstft_v2_tutorial_vs_repeat": round(M.term(st["tutorial"], st["tutorial_repeat"]), 4),
        "null_db_tutorial_vs_repeat": round(null_db(st["tutorial"], st["tutorial_repeat"]), 1),
        "mrstft_v2_tutorial_vs_t197_reference": round(M.term(t197, st["tutorial"]), 4),
        "null_db_tutorial_vs_t197_reference": round(null_db(t197, st["tutorial"]), 1)}}
    tab = score_table(st, target)
    res["scores_stomp"] = {k: tab[k] for k in names}
    par = {}
    for k in names:
        lag = best_lag(st[k], nat[k])
        par[k] = {"mrstft_v2_native_vs_stomp": round(M.term(st[k], np.roll(nat[k], lag)), 4),
                  "band_db_native_minus_stomp": band_diff(nat[k], st[k]),
                  "lufs_native_minus_stomp": round(lufs(nat[k]) - lufs(st[k]), 2),
                  "level_diff_vs_tutorial_native_db": round(lufs(nat[k]) - lufs(nat["tutorial"]), 2),
                  "level_diff_vs_tutorial_stomp_db": round(lufs(st[k]) - lufs(st["tutorial"]), 2)}
    res["native_vs_stomp"] = par
    res["band_labels_hz"] = band_labels()
    # low end vs 1-4 kHz, as the lead measured it (equal total power, Stomp renders)
    res["lowend_vs_mid"] = {k: {"below_250_db": round(float(np.mean(np.array(tab[k]["band_db_minus_target"])[:3])), 2),
                                "1_to_4k_db": round(float(np.mean(np.array(tab[k]["band_db_minus_target"])[5:7])), 2)} for k in names}
    # absolute per-band level at equal LUFS (a pure gain match, not equal power)
    gi = {k: lufs(target) - lufs(st[k]) for k in names}
    res["band_db_at_equal_lufs_minus_target"] = {
        k: [round(float(x), 2) for x in band_db(st[k] * 10 ** (gi[k] / 20)) - band_db(target)] for k in names}
    (WORK / "analysis.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("parity")
    sub.add_parser("cccheck")
    sub.add_parser("report")
    sub.add_parser("stomp")
    sub.add_parser("analyze")
    ss = sub.add_parser("session")
    ss.add_argument("--seed", type=int, default=None)
    ss.add_argument("--rebuild", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("--arm", choices=list(ARMS), required=True)
    s.add_argument("--budget", type=int, default=400)
    s.add_argument("--pop", type=int, default=20)
    s.add_argument("--sigma", type=float, default=0.12)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--max-load", type=float, default=None)
    a = ap.parse_args(argv)
    return {"parity": cmd_parity, "cccheck": cmd_cccheck, "search": cmd_search,
            "report": cmd_report, "stomp": cmd_stomp, "session": cmd_session, "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
