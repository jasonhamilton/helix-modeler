#!/usr/bin/env python3
"""T228: colour pilot (human 2026-10-25). LOCAL ONLY; song audio stays under cache/. Nothing is adopted.

Product chain under test: drive -> (delay and/or reverb) -> EQ, no amp or cab (no cab block anywhere in the pilot: human
2026-10-25), plus an amp block in place of the drive. Three songs (mother, seek-destroy, teen-spirit), the T211 windows and calibrated dry. Variants per song:

  a  the fine-tuned drive match as is (cache/matches/<song>-finetune-prs7)
  b  a + delay and/or reverb set by DIRECT MEASUREMENT of the separated stem (tonematch.search.colour); a block is
     included only if the measurement finds it present (Mix capped: "light")
  c  a + one bounded EQ (Graphic 10 Band) fitted by the LTAS ratio of stem to the a-render (+-6 dB, < 100 Hz ignored)
  d  b + EQ (the EQ fitted on the b-render)
  e  an amp in place of the drive: AFx-Rep screen of a usage-ranked amp pool (coarse: gain knob at 2 settings), then
     a CMA-ES knob fine-tune with Output/Master/ChVol/Level locked
  g  CONTROL, not asked for: a + a fixed light Room reverb (not measured), to hear whether ambience helps at all
  h  EXTRA, not asked for: a + Parametric EQ (high-cut + 3 bands) fitted on the same LTAS ratio: a chain without a cab keeps the
     drive's treble, which a +-6 dB band cannot remove

    nice -n 10 .venv/bin/python scripts/colour_pilot_t228.py run --songs mother seek-destroy teen-spirit
    .venv/bin/python scripts/colour_pilot_t228.py pack | ledger | table | loadlist

`run` is resumable per song (cache/colour-pilot-t228/<song>/state.json); one Native host at a time, LoadGuard before each
stage. Outputs: cache/colour-pilot-t228/<song>/{audio,hlx,state.json}, packs/<song>/, stomp-load/<song>-<v>/, load_list.md.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SR = 48_000
BASE = ROOT / "cache" / "colour-pilot-t228"
SONGS = ("mother", "seek-destroy", "teen-spirit")
VARIANTS = ("a", "b", "c", "d", "e")                  # the asked-for variants (f, amp + cab, was withdrawn by the human: no cab blocks)
EXTRAS = ("g", "h")                                    # two additions of my own, in a separate optional pack
ALL_V = VARIANTS + EXTRAS
DESC = {
    "a": "fine-tuned drive match as is",
    "b": "a + measured delay/reverb (blocks only where the measurement finds them)",
    "c": "a + bounded EQ (LTAS ratio, +-6 dB)",
    "d": "b + bounded EQ",
    "e": "amp in place of the drive (AFx-Rep screen, level knobs locked)",
    "g": "CONTROL (not measured): a + fixed light Room reverb",
    "h": "EXTRA: a + Parametric EQ with a high-cut (cab-less chains have the drive's treble), fitted on the LTAS ratio",
}
DELAY_MODEL = "HD2_DelayTransistorTape"
REVERB_MODEL = "HD2_ReverbRoom"
EQ_MODEL = "HD2_EQGraphic10Band"
PEQ_MODEL = "HD2_EQParametric"
PEQ_FIXED = {"LowFreq": 125.0, "LowQ": 0.7, "MidFreq": 1000.0, "MidQ": 0.7, "HighFreq": 4000.0, "HighQ": 0.7, "LowCut": 19.9}
PEQ_HIGHCUTS = (20100.0, 9000.0, 6500.0, 4500.0)       # 20100 = off (the block default)
CONTROL_REVERB = {"Decay": 0.40, "Mix": 0.35}                                          # variant g: tail about -26 dB under the direct sound
# known effects rendered onto the real stem to find the detection limit: at the light cap and a weaker one
CONTROL_LEVELS = {"cap": ({"Time": 0.38, "Feedback": 0.30, "Mix": 0.45, "WowFlutter": 0.0}, {"Decay": 0.50, "Mix": 0.50}),
                  "weak": ({"Time": 0.38, "Feedback": 0.30, "Mix": 0.30, "WowFlutter": 0.0}, {"Decay": 0.50, "Mix": 0.35})}
AMP_POOL_N = 15
AMP_FINETUNE_BUDGET = 21
EQ_PASSES = 2


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------------------------ context

class Ctx:
    """One song: target section, Native engine (opened lazily), block library, render cache and count."""

    def __init__(self, song: str):
        self.song = song
        self.dir = BASE / song
        (self.dir / "audio").mkdir(parents=True, exist_ok=True)
        (self.dir / "hlx").mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.eng = None
        self.cache: dict[tuple, np.ndarray] = {}
        self.renders = 0
        self.t0 = time.time()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1, default=_json))

    def open(self) -> None:
        import match_block as MB

        from tonematch import songs as SG
        from tonematch.catalog import load_catalog
        from tonematch.engines.host_budget import LoadGuard
        from tonematch.engines.native_chain import NativeBlockLibrary

        self.guard = LoadGuard()
        self.guard.wait(lambda x: log(f"pause: busy cores {x}"))
        subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)
        self.MB = MB
        self.catalog = load_catalog()
        self.lib = NativeBlockLibrary(catalog=self.catalog)
        self.ft = json.loads((ROOT / f"cache/matches/{self.song}-finetune-prs7/result.json").read_text())
        w = self.ft["window"]
        d = ROOT / f"data/songs/{self.song}-rebuilt/playlevel-prs7"
        args = SimpleNamespace(target_wav=None, target_stems=str(SG.resolve(self.song).stems_dir),
                               target_window=f"{w['start_s']}:{w['end_s']}", di=str(d / "rebuilt_dry_playlevel.wav"),
                               di_json=str(d / "rebuilt_dry_playlevel_align.json"), song=self.song)
        self.section, _ = MB.build_target_section(args)
        self.stem = np.asarray(self.section.target, np.float32)
        self.dry = np.asarray(self.section.dry, np.float32)
        self.eng = MB.make_engine(144_000, log)

    def close(self) -> None:
        if self.eng is not None:
            self.eng.close()
            self.eng = None
            subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)

    # -- rendering
    def render_block(self, model: str, values: dict, x: np.ndarray) -> np.ndarray:
        p = self.lib.preset(model, values)
        self.guard.wait(lambda d: log(f"pause: busy cores {d}"))
        self.eng.load(p)
        y = np.asarray(self.eng.render(x), np.float32)
        self.renders += 1
        if y.ndim > 1:
            y = y.mean(axis=1) if y.shape[1] <= 2 else y.mean(axis=0)
        return y[: len(x)]

    def chain(self, blocks: list[tuple[str, dict]], x: np.ndarray | None = None, tag: str = "") -> np.ndarray:
        """Block-serial render of [(model, values)] from `x` (default the dry track), cached by the block list."""
        x0 = self.dry if x is None else x
        key0 = (tag, id(x0) if x is not None else "dry")
        y = x0
        for i in range(len(blocks)):
            key = (*key0, json.dumps(blocks[: i + 1], sort_keys=True, default=_json))
            if key not in self.cache:
                self.cache[key] = self.render_block(blocks[i][0], blocks[i][1], y)
            y = self.cache[key]
        return y

    def wav(self, name: str, y: np.ndarray) -> Path:
        p = self.dir / "audio" / f"{name}.wav"
        sf.write(str(p), np.asarray(y, np.float32), SR, subtype="FLOAT")
        return p


def _json(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def snap_t(ctx: Ctx, model: str, t: float) -> float:
    lo, hi, _ = ctx.lib.free_params(model)["Time"]
    from tonematch.search.colour import snap_time

    return round(snap_time(t, lo, hi), 4)


# ------------------------------------------------------------------------------------------ measurement fits

def fit_delay(ctx: Ctx, stem_like: np.ndarray, a: np.ndarray, ev, tag: str) -> dict:
    """Delay knobs from a DelayEvidence that says present: Time from the tap lag (CC grid), Mix and Feedback from one probe
    render's taps against the target's (same deconvolution), then the Mix/Feedback caps ("light")."""
    from tonematch.search import colour as C

    T = snap_t(ctx, DELAY_MODEL, ev.lag_s)
    probe_vals = {"Time": T, "Feedback": 0.35, "Mix": 0.25, "WowFlutter": 0.0}
    yp = ctx.render_block(DELAY_MODEL, probe_vals, a)
    t, h = C.echo_deconv(C.onset_envelope(yp), C.onset_envelope(a))
    main = float(h[(t > -0.04) & (t < 0.03)].sum())
    g1p, g2p = C.tap_at(t, h, T) / max(main, 1e-9), C.tap_at(t, h, 2 * T) / max(main, 1e-9)
    mix = C.invert_linear(ev.taps["T"], g1p, probe_vals["Mix"])
    ratio_t = ev.taps["2T"] / max(ev.taps["T"], 1e-9)
    ratio_p = g2p / max(g1p, 1e-9)
    fb = float(np.clip(probe_vals["Feedback"] * ratio_t / ratio_p, 0.0, 0.95)) if ratio_p > 1e-6 else 0.2
    mix_c, mix_capped = C.light_mix(mix, "delay")
    fb_c = min(fb, C.LIGHT_FEEDBACK_CAP)
    return {"values": {"Time": T, "Feedback": round(fb_c, 3), "Mix": round(mix_c, 3), "WowFlutter": 0.0},
            "measured": {"lag_s": ev.lag_s, "tap_T": ev.taps["T"], "tap_2T": ev.taps["2T"], "mix_uncapped": mix, "feedback_uncapped": fb,
                         "probe_tap_T": g1p, "probe_tap_2T": g2p},
            "mix_capped": mix_capped, "probe_renders": 1, "tag": tag}


def fit_reverb(ctx: Ctx, stem_like: np.ndarray, a: np.ndarray, tail) -> dict:
    """Reverb knobs from a TailEvidence that says present: Decay from the gap slope (two probes, interpolated), Mix from the
    early gap level (amplitude ratio to a probe), then the Mix cap."""
    from tonematch.search import colour as C

    dry = ctx.dry
    probes = []
    for dec in (0.25, 0.65):
        vals = {"Decay": dec, "Mix": 0.20}
        yp = ctx.render_block(REVERB_MODEL, vals, a)
        te = C.gap_tail(yp, a, dry)
        probes.append((dec, te.slope_db_s if te.slope_db_s is not None else 0.0, te.early_db if te.early_db is not None else -60.0))
    (d0, s0, e0), (d1, s1, e1) = probes
    if abs(s1 - s0) > 1e-6:
        decay = float(np.clip(d0 + (tail.slope_db_s - s0) * (d1 - d0) / (s1 - s0), 0.05, 0.95))
    else:
        decay = 0.4
    e_ref = e0 + (e1 - e0) * (decay - d0) / (d1 - d0)
    mix = float(np.clip(0.20 * 10 ** ((tail.early_db - e_ref) / 20), 0.02, 1.0))
    mix_c, capped = C.light_mix(mix, "reverb")
    return {"values": {"Decay": round(decay, 3), "Mix": round(mix_c, 3)},
            "measured": {"slope_db_s": tail.slope_db_s, "early_db": tail.early_db, "rt60_s": tail.rt60_s, "mix_uncapped": mix,
                         "probes": probes}, "mix_capped": capped, "probe_renders": 2}


# ------------------------------------------------------------------------------------------ stages

def stage_a(ctx: Ctx) -> dict:
    ft = ctx.ft
    blocks = [(ft["model"], ft["final_values"])]
    y = ctx.chain(blocks, tag="main")
    ctx.wav("a", y)
    ctx.wav("stem", ctx.stem)
    ctx.wav("dry", ctx.dry)
    prev = BASE / "audio" / f"{ctx.song}_a.wav"                       # the first look at this song (same block, Block path)
    if prev.exists():
        z = sf.read(str(prev), dtype="float32")[0]
        ctx.state["a_check_vs_block_path_max_abs_diff"] = float(np.max(np.abs(z[: len(y)] - y[: len(z)])))
    ctx.state["a"] = {"blocks": blocks, "renders": 1}
    return {"y": y, "blocks": blocks}


def stage_controls(ctx: Ctx, a: np.ndarray) -> dict:
    """Detection limits on the real stem: the stem with a KNOWN delay / reverb rendered onto it (at the light cap and at a
    weaker level), measured exactly like the real one. If it detects, the fit is run on it too (does it recover the knobs?)."""
    from tonematch.search import colour as C

    out: dict = {"levels": {k: {"delay": d, "reverb": r} for k, (d, r) in CONTROL_LEVELS.items()}}
    n0 = ctx.renders
    stem = ctx.stem
    out["real_stem"] = {"delay": _ev(C.delay_evidence(stem, a, ctx.dry)), "tail": _tl(C.gap_tail(stem, a, ctx.dry))}
    for lvl, (dv, rv) in CONTROL_LEVELS.items():
        dv = {**dv, "Time": snap_t(ctx, DELAY_MODEL, dv["Time"])}
        ctl_d = ctx.render_block(DELAY_MODEL, dv, stem)
        ctl_r = ctx.render_block(REVERB_MODEL, rv, stem)
        ctx.wav(f"ctl_delay_{lvl}", ctl_d)
        ctx.wav(f"ctl_reverb_{lvl}", ctl_r)
        evd, evr = C.delay_evidence(ctl_d, a, ctx.dry), C.delay_evidence(ctl_r, a, ctx.dry)
        out[f"stem_plus_delay_{lvl}"] = {"delay": _ev(evd), "tail": _tl(C.gap_tail(ctl_d, a, ctx.dry)), "set": dv}
        out[f"stem_plus_reverb_{lvl}"] = {"delay": _ev(evr), "tail": _tl(C.gap_tail(ctl_r, a, ctx.dry)), "set": rv}
        if evd.present:
            out[f"fit_on_stem_plus_delay_{lvl}"] = fit_delay(ctx, ctl_d, a, evd, "control")
        tr = C.gap_tail(ctl_r, a, ctx.dry)
        if tr.present:
            out[f"fit_on_stem_plus_reverb_{lvl}"] = fit_reverb(ctx, ctl_r, a, tr)
    out["renders"] = ctx.renders - n0
    ctx.state["controls"] = out
    return out


def _ev(e) -> dict:
    return {k: getattr(e, k) for k in ("aligned", "main_tap", "r2", "lag_s", "rel_tap", "z", "gap_checked", "gap_confirmed", "present", "reason", "taps")}


def _tl(t) -> dict:
    return {k: getattr(t, k) for k in ("gaps", "tails", "present", "measurable", "slope_db_s", "early_db", "rt60_s", "reason", "skipped_levels_db")}


def stage_time(ctx: Ctx, a: np.ndarray, blocks_a: list) -> dict:
    """Variant b."""
    from tonematch.search import colour as C

    n0 = ctx.renders
    ev = C.delay_evidence(ctx.stem, a, ctx.dry)
    tl = C.gap_tail(ctx.stem, a, ctx.dry)
    res: dict = {"delay_evidence": _ev(ev), "tail_evidence": _tl(tl), "blocks": []}
    blocks = list(blocks_a)
    if ev.present:
        fit = fit_delay(ctx, ctx.stem, a, ev, "song")
        blocks.append((DELAY_MODEL, fit["values"]))
        res["delay_fit"] = fit
    if tl.present:
        fit = fit_reverb(ctx, ctx.stem, a, tl)
        blocks.append((REVERB_MODEL, fit["values"]))
        res["reverb_fit"] = fit
    y = ctx.chain(blocks, tag="main") if len(blocks) > len(blocks_a) else a
    res.update(blocks_all=blocks, added=len(blocks) - len(blocks_a), renders=ctx.renders - n0,
               identical_to_a=len(blocks) == len(blocks_a))
    ctx.wav("b", y)
    ctx.state["b"] = res
    return {"y": y, "blocks": blocks, "res": res}


def stage_eq(ctx: Ctx, y0: np.ndarray, blocks0: list, name: str) -> dict:
    """Variants c and d: one Graphic 10 Band after `blocks0`, fitted on the LTAS ratio of the stem to `y0`; up to EQ_PASSES
    renders, each pass corrects the remaining ratio; the pass with the smallest residual is kept."""
    from tonematch.search import colour as C

    n0 = ctx.renders
    ratio0 = C.ltas_ratio_db(ctx.stem, y0)
    res0 = C.eq_residual_db(ratio0)
    gains = C.eq_gains_from_ltas(ratio0)
    best = {"gains": np.zeros(10), "residual": res0, "y": y0, "pass": 0}
    passes = [{"pass": 0, "residual_db": res0, "gains": np.zeros(10).tolist()}]
    cur = gains
    for k in range(1, EQ_PASSES + 1):
        vals = C.graphic10_values(cur)
        y = ctx.chain([*blocks0, (EQ_MODEL, vals)], tag="main")
        rk = C.ltas_ratio_db(ctx.stem, y)
        res = C.eq_residual_db(rk)
        passes.append({"pass": k, "residual_db": res, "gains": cur.tolist()})
        if res < best["residual"] - 0.05:
            best = {"gains": cur.copy(), "residual": res, "y": y, "pass": k}
        else:
            break
        cur = np.clip(cur + C.eq_gains_from_ltas(rk), -C.EQ_LIMIT_DB, C.EQ_LIMIT_DB)
    out = {"ratio_before_db": ratio0.tolist(), "residual_before_db": res0, "residual_after_db": best["residual"],
           "gains_db": best["gains"].tolist(), "values": C.graphic10_values(best["gains"]), "chosen_pass": best["pass"], "passes": passes,
           "centres_hz": list(C.GRAPHIC10_CENTRES), "ignored_below_hz": C.EQ_IGNORE_BELOW_HZ, "limit_db": C.EQ_LIMIT_DB,
           "renders": ctx.renders - n0, "helped": best["pass"] > 0}
    if best["pass"] > 0:
        blocks = [*blocks0, (EQ_MODEL, out["values"])]
        y = best["y"]
    else:                                   # the EQ did not reduce the residual: no EQ block
        blocks, y = list(blocks0), y0
    out["blocks_all"] = blocks
    ctx.wav(name, y)
    ctx.state[name] = out
    return {"y": y, "blocks": blocks, "res": out}


def stage_parametric(ctx: Ctx, a: np.ndarray, blocks0: list) -> dict:
    """Variant h: one Parametric EQ after `blocks0`. Step 1 scans the high-cut over PEQ_HIGHCUTS by the rendered LTAS residual (the
    quantity being fitted, not a sound score); step 2 sets the Low/Mid/High band gains (125 Hz, 1 kHz, 4 kHz, Q 0.7) from the
    smoothed ratio left, +-6 dB, one correction pass."""
    from tonematch.search import colour as C

    n0 = ctx.renders
    scan = []
    for fc in PEQ_HIGHCUTS:
        vals = {**PEQ_FIXED, "LowGain": 0.0, "MidGain": 0.0, "HighGain": 0.0, "HighCut": fc}
        y = ctx.chain([*blocks0, (PEQ_MODEL, vals)], tag="main")
        scan.append({"highcut_hz": fc, "residual_db": C.eq_residual_db(C.ltas_ratio_db(ctx.stem, y)), "values": vals})
    ratio_a = C.ltas_ratio_db(ctx.stem, a)
    res_a = C.eq_residual_db(ratio_a)
    best = min(scan, key=lambda r: r["residual_db"])
    cur_vals, cur_res = best["values"], best["residual_db"]
    y_cur = ctx.chain([*blocks0, (PEQ_MODEL, cur_vals)], tag="main")
    passes = [{"step": "highcut", "residual_db": cur_res, "values": cur_vals}]
    idx = {"LowGain": 2, "MidGain": 5, "HighGain": 7}                 # 125 Hz, 1 kHz, 4 kHz bands of the graphic centres
    for k in range(2):
        r = C.ltas_ratio_db(ctx.stem, y_cur)
        g10 = C.eq_gains_from_ltas(r)
        add = {n: float(g10[i]) for n, i in idx.items()}
        vals = {**cur_vals, **{n: float(np.clip(cur_vals[n] + add[n], -C.EQ_LIMIT_DB, C.EQ_LIMIT_DB)) for n in idx}}
        y = ctx.chain([*blocks0, (PEQ_MODEL, vals)], tag="main")
        res = C.eq_residual_db(C.ltas_ratio_db(ctx.stem, y))
        passes.append({"step": f"bands pass {k + 1}", "residual_db": res, "values": vals})
        if res < cur_res - 0.05:
            cur_vals, cur_res, y_cur = vals, res, y
        else:
            break
    out = {"residual_before_db": res_a, "residual_after_db": cur_res, "values": {k: round(float(v), 3) for k, v in cur_vals.items()},
           "highcut_scan": scan, "passes": passes, "blocks_all": [*blocks0, (PEQ_MODEL, {k: round(float(v), 3) for k, v in cur_vals.items()})],
           "renders": ctx.renders - n0}
    ctx.wav("h", y_cur)
    ctx.state["h"] = out
    return out


def stage_amp(ctx: Ctx) -> dict:
    """Variant e (an amp block in place of the drive; no cab)."""
    from tonematch.metrics import afxrep
    from tonematch.search import colour as C
    from tonematch.search import shortlist as SL
    from tonematch.search.afx_refine import refine_block

    n0 = ctx.renders
    usage = json.loads((ROOT / "reports/local/preset-corpus/model_usage.json").read_text())["amp"]["ranked"]
    pool_names = C.amp_pool_by_usage(usage, ctx.catalog, AMP_POOL_N)
    full_pool = ctx.MB.category_pool("amp", ctx.catalog)
    pool_names = [m for m in pool_names if m in full_pool and m in ctx.lib.templates]
    blocks = ctx.MB.make_blocks({m: full_pool[m] for m in pool_names}, "amp", ctx.catalog)

    class CountEng:                                   # counts the screen's renders through the context
        def load(_s, p):
            ctx.guard.wait(lambda d: log(f"pause: busy cores {d}"))
            ctx.eng.load(p)

        def render(_s, x):
            ctx.renders += 1
            return ctx.eng.render(x)

    rows = SL.rank_models(CountEng(), blocks, ctx.section, points=lambda free, knobs=None: C.amp_screen_points(free),
                          knob_map={}, log=log)
    top = rows[0]
    m = top["model"]
    b = C.LockedLevel(blocks[m])
    start = {n: d for n, _lo, _hi, d in blocks[m].free}
    start.update(top["best_values"])
    tf = afxrep.features(ctx.section.target)

    r = refine_block(CountEng(), b, ctx.dry, tf, {k: v for k, v in start.items() if k not in b.locked}, embed=afxrep.features,
                     dist=afxrep.distance, budget=AMP_FINETUNE_BUDGET, seed=0, patience=3,
                     wait=lambda: ctx.guard.wait(lambda d: log(f"pause: busy cores {d}")), log=log)
    vals = {**{n: d for n, _lo, _hi, d in blocks[m].free if n in b.locked}, **r["final_values"]}
    block_e = [(m, vals)]
    y_e = ctx.chain(block_e, tag="amp")
    ctx.wav("e", y_e)
    res = {"pool": pool_names, "screen_rows": [{"model": x["model"], "distance": x["distance"], "values": x["best_values"]} for x in rows],
           "screen_renders": sum(x["renders"] for x in rows), "model": m, "values": vals, "locked": b.locked,
           "finetune_start": r["start_values"], "dist_screen": top["distance"], "dist_before": r["dist_before"], "dist_after": r["dist_after"],
           "finetune_renders": r["renders"], "stopped": r["stopped"]}
    ctx.state["e"] = {**res, "renders": ctx.renders - n0, "blocks_all": block_e}
    return res


def stage_control_g(ctx: Ctx, a: np.ndarray, blocks_a: list) -> None:
    n0 = ctx.renders
    blocks = [*blocks_a, (REVERB_MODEL, CONTROL_REVERB)]
    y = ctx.chain(blocks, tag="main")
    ctx.wav("g", y)
    ctx.state["g"] = {"blocks_all": blocks, "renders": ctx.renders - n0}


def write_hlx_all(ctx: Ctx) -> None:
    """A validated .hlx per variant (blocks_all of the state) and the Stomp-load folders."""
    from tonematch.preset import write_hlx
    from tonematch.search.chain_builder import BlockSpec, assemble_preset

    cat_of = lambda m: ctx.catalog["models"][m]["category"]
    out = {}
    for v in ALL_V:
        st = ctx.state.get(v)
        if v == "b":
            st = ctx.state["b"]
            bl = st["blocks_all"]
        elif v == "a":
            bl = ctx.state["a"]["blocks"]
        else:
            bl = (st or {}).get("blocks_all")
        if not bl:
            continue
        specs = [BlockSpec(cat_of(m), m).with_values(vals) for m, vals in bl]
        p = assemble_preset(specs, ctx.lib, name=f"{ctx.song[:6]}-{v}")
        path = ctx.dir / "hlx" / f"{ctx.song}_{v}.hlx"
        write_hlx(p, path)
        code, text = validate_hlx(path)
        sdir = BASE / "stomp-load" / f"{ctx.song}-{v}"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "current_best.hlx").write_bytes(path.read_bytes())
        (sdir / "current_best.json").write_text(json.dumps({
            "seq": 1, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "loss": 0.0, "model": bl[0][0], "category": cat_of(bl[0][0]),
            "values": {}, "renders": 0, "stage": "final", "label": f"{ctx.song}-{v}"}))
        out[v] = {"hlx": str(path), "validate_exit": code, "validate_last": text.splitlines()[-1] if text else "",
                  "blocks": [m for m, _ in bl], "stomp_load_dir": str(sdir)}
    ctx.state["hlx"] = out


def validate_hlx(path: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(path)], check=False, capture_output=True,
                       text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


def redo_h(song: str) -> None:
    """Re-run only variant h (a loads from audio/a.wav and the state: no render for it)."""
    ctx = Ctx(song)
    ctx.open()
    t0 = time.time()
    try:
        a = sf.read(str(ctx.dir / "audio" / "a.wav"), dtype="float32")[0]
        ba = [(m, v) for m, v in ctx.state["a"]["blocks"]]
        ctx.cache[("main", "dry", json.dumps(ba, sort_keys=True, default=_json))] = a      # prefix of every chain from the dry
        n0, old = ctx.renders, ctx.state["h"]["renders"]
        stage_parametric(ctx, a, ba)
        ctx.state["renders_total"] = ctx.state["renders_total"] - old + ctx.state["h"]["renders"]
        ctx.save()
        log(f"{song}: h redone, {ctx.renders - n0} renders, {time.time() - t0:.0f} s")
    finally:
        ctx.close()
    write_hlx_all(ctx)
    ctx.save()


def redo_controls(song: str) -> None:
    """Re-run only the controls (the evidence record gained a field). a loads from audio/a.wav."""
    ctx = Ctx(song)
    ctx.open()
    try:
        a = sf.read(str(ctx.dir / "audio" / "a.wav"), dtype="float32")[0]
        old = ctx.state["controls"]["renders"]
        stage_controls(ctx, a)
        ctx.state["renders_total"] = ctx.state["renders_total"] - old + ctx.state["controls"]["renders"]
        ctx.state["controls"]["redone_for_record"] = True
        ctx.save()
    finally:
        ctx.close()


def remeasure(song: str) -> None:
    """Offline (0 renders): the measurements again from the saved audio (stem, a, dry, the control renders), for when only the
    evidence code changed. Refuses if a block would now be added (that needs the fit's renders: run the song)."""
    from tonematch.search import colour as C

    d = BASE / song
    st = json.loads((d / "state.json").read_text())
    rd = lambda n: sf.read(str(d / "audio" / f"{n}.wav"), dtype="float32")[0]
    stem, a, dry = rd("stem"), rd("a"), rd("dry")
    ctl = st["controls"]
    ctl["real_stem"] = {"delay": _ev(C.delay_evidence(stem, a, dry)), "tail": _tl(C.gap_tail(stem, a, dry))}
    for lvl in CONTROL_LEVELS:
        for kind, key in (("delay", "stem_plus_delay"), ("reverb", "stem_plus_reverb")):
            y = rd(f"ctl_{kind}_{lvl}")
            ctl[f"{key}_{lvl}"] = {"delay": _ev(C.delay_evidence(y, a, dry)), "tail": _tl(C.gap_tail(y, a, dry)),
                                   "set": ctl[f"{key}_{lvl}"]["set"]}
    ev, tl = C.delay_evidence(stem, a, dry), C.gap_tail(stem, a, dry)
    if ev.present or tl.present:
        raise SystemExit(f"{song}: a block would now be added; run the song (the fit needs renders)")
    st["b"]["delay_evidence"], st["b"]["tail_evidence"] = _ev(ev), _tl(tl)
    (d / "state.json").write_text(json.dumps(st, indent=1, default=_json))
    log(f"{song}: re-measured offline; delay: {ev.reason} | reverb: {tl.reason}")


def run_song(song: str, force: bool) -> None:
    ctx = Ctx(song)
    if force:
        ctx.state = {}
    ctx.open()
    t0 = time.time()
    try:
        log(f"{song}: a")
        A = stage_a(ctx)
        a, ba = A["y"], A["blocks"]
        if "controls" not in ctx.state:
            log(f"{song}: controls (known effect on the real stem)")
            stage_controls(ctx, a)
            ctx.save()
        log(f"{song}: b (direct measurement)")
        B = stage_time(ctx, a, ba)
        ctx.save()
        log(f"{song}: c (EQ on a)")
        Cc = stage_eq(ctx, a, ba, "c")
        ctx.save()
        if B["res"]["identical_to_a"]:
            ctx.state["d"] = {**Cc["res"], "same_as_c": True, "renders": 0}
            ctx.wav("d", Cc["y"])
        else:
            log(f"{song}: d (EQ on b)")
            stage_eq(ctx, B["y"], B["blocks"], "d")
        ctx.save()
        if "e" not in ctx.state or force:
            log(f"{song}: e (amp substitute, no cab)")
            stage_amp(ctx)
            ctx.save()
        log(f"{song}: g (control)")
        stage_control_g(ctx, a, ba)
        log(f"{song}: h (parametric EQ with high-cut)")
        stage_parametric(ctx, a, ba)
        ctx.state["renders_total"] = ctx.renders
        ctx.state["wall_s"] = round(time.time() - t0, 1)
        ctx.save()
    finally:
        ctx.close()
    write_hlx_all(ctx) if ctx.state.get("e") else None
    ctx.save()
    log(f"{song}: done, {ctx.renders} renders, {time.time() - t0:.0f} s")


# ------------------------------------------------------------------------------------------ pack, ledger, tables

def _load(song: str, v: str) -> np.ndarray | None:
    p = BASE / song / "audio" / f"{v}.wav"
    return sf.read(str(p), dtype="float32")[0] if p.exists() else None


def cmd_pack(songs: list[str], seed: int) -> None:
    build_packs(songs, seed, VARIANTS, BASE / "packs", "colour pilot")
    build_packs(songs, seed + 1, ("a", *EXTRAS), BASE / "packs-extra", "colour pilot, OPTIONAL extras (not asked for)")


def build_packs(songs: list[str], seed: int, variants: tuple, root: Path, title: str) -> None:
    import dry_ablation_t224 as DA

    rng = random.Random(seed)
    for song in songs:
        stem = np.asarray(sf.read(str(BASE / song / "audio" / "stem.wav"), dtype="float32")[0]) if (BASE / song / "audio" / "stem.wav").exists() else None
        if stem is None:
            raise SystemExit(f"{song}: audio/stem.wav missing (run `run` first)")
        mats_all = {v: _load(song, v) for v in variants}
        mats_all = {v: y for v, y in mats_all.items() if y is not None}
        n = min([len(stem)] + [len(y) for y in mats_all.values()])
        groups: dict[str, list[str]] = {}                      # identical audio is one clip (the key says which variants it stands for)
        for v, y in mats_all.items():
            twin = next((w for w in groups if np.allclose(mats_all[w][:n], y[:n], atol=1e-7)), None)
            groups.setdefault(twin or v, []).append(v)
        mats = {w: mats_all[w] for w in groups}
        t2, m2, gains = DA.level_match_pack(stem[:n], {v: y[:n] for v, y in mats.items()})
        letters = list("ABCDEFGH")[: len(mats)]
        rng.shuffle(letters)
        key = dict(zip(letters, mats))
        d = root / song
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*"):
            old.unlink()
        for L in sorted(key):
            sf.write(str(d / f"clip_{L}.wav"), np.stack([t2, m2[key[L]]], 1), SR, subtype="FLOAT")
        rows = "\n".join(f'<section><b>Clip {L}</b><audio controls preload="none" src="clip_{L}.wav"></audio></section>' for L in sorted(key))
        (d / "index.html").write_text(
            f"""<!doctype html><meta charset="utf-8"><title>{song}: {title}, blind pack</title>
<style>body{{font:16px/1.5 system-ui;max-width:46rem;margin:2rem auto;padding:0 1rem}}section{{border:1px solid #ccc;border-radius:6px;padding:.6rem 1rem;margin:.6rem 0}}audio{{width:100%}}</style>
<h1>{song}: {title}, blind pack</h1><p>Headphones. Each clip is stereo: <b>left = the separated song guitar, right = a candidate</b>
(use balance or mono-L/mono-R to switch). Every clip is loudness-matched (K-weighted LUFS), with the same guitar on the left.
Rate every clip in <code>answer_sheet.md</code>. <b>Do not open key_SEALED.json until you have rated.</b></p>{rows}""")
        (d / "answer_sheet.md").write_text(
            f"# {song}: {title}, answer sheet (blind)\n\nListen to the left channel (the song's guitar) then the right (the candidate). "
            f"Score how close the RIGHT is to the LEFT in tone: 1 = far, 5 = very close. Then rank all clips, best first.\n"
            f"Do not open `key_SEALED.json` before filling this in. If two clips sound identical, say so under notes.\n\n"
            f"| clip | closeness 1-5 | notes |\n|---|---|---|\n" + "\n".join(f"| {L} |  |  |" for L in sorted(key)) +
            "\n\nRanking, best first (letters): \n")
        (d / "key_SEALED.json").write_text(json.dumps({
            "song": song, "key": {L: groups[v] for L, v in key.items()}, "variants": {v: DESC[v] for v in mats_all},
            "note": "a clip standing for several variants means their audio is identical (no block was added)", "gains_db": gains,
            "seed": seed, "warning": "do not open before rating"}, indent=1))
        log(f"{song}: pack {d} clips {','.join(sorted(key))}")
    (root / "README.md").write_text(
        f"# T228 {title} blind packs (local only; song audio inside)\n\nOne folder per song: `index.html` (players), `clip_<letter>.wav` "
        "(left = separated song guitar, right = a variant), `answer_sheet.md`, `key_SEALED.json` (do not open before rating). Letters are random per song.\n")


def cmd_loadlist() -> None:
    lines = ["# T228 colour pilot: Stomp load list (local only)", "",
             "Nothing was loaded on the Stomp by this task. Each row is one validated preset; to put it in the edit buffer (never a flash write):",
             "", "    .venv/bin/python scripts/stomp_live_preview.py --dir <stomp-load dir> --once --no-restore-on-exit", "",
             "(or import the .hlx in HX Edit). Variants with identical blocks are listed once with their twin.", "",
             "| song | variant | what | blocks | hlx | validate | stomp-load dir |", "|---|---|---|---|---|---|---|"]
    for song in SONGS:
        sp = BASE / song / "state.json"
        if not sp.exists():
            continue
        st = json.loads(sp.read_text())
        seen: dict[str, str] = {}
        for v, h in (st.get("hlx") or {}).items():
            sig = ",".join(h["blocks"])
            twin = f" (same blocks as {seen[sig]})" if sig in seen else ""
            seen.setdefault(sig, v)
            lines.append(f"| {song} | {v} | {DESC[v]}{twin} | {' > '.join(m.replace('HD2_', '') for m in h['blocks'])} | `{h['hlx']}` | "
                         f"exit {h['validate_exit']} | `{h['stomp_load_dir']}` |")
    (BASE / "load_list.md").write_text("\n".join(lines) + "\n")
    log(f"load list: {BASE / 'load_list.md'}")


def cmd_ledger(songs: list[str]) -> None:
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "search"))
    for song in songs:
        st = json.loads((BASE / song / "state.json").read_text())
        for v in ALL_V:
            s = st.get(v) or {}
            if v == "a":
                n, note = 1, f"fine-tuned drive match as is ({st['a']['blocks'][0][0]}); the render every other variant starts from"
            elif v == "b":
                b = st["b"]
                n = b["renders"]
                note = ("direct measurement: " + b["delay_evidence"]["reason"] + " | " + b["tail_evidence"]["reason"] +
                        ("; identical to a" if b["identical_to_a"] else "; blocks " + ",".join(m for m, _ in b["blocks_all"][1:])))
            elif v in ("c", "d"):
                n = s.get("renders", 0)
                note = (f"EQ Graphic10 +-6 dB, <100 Hz ignored; LTAS residual {s['residual_before_db']:.2f} -> {s['residual_after_db']:.2f} dB"
                        + ("; same as c (no b blocks)" if s.get("same_as_c") else ""))
            elif v == "e":
                n = s["renders"]
                note = (f"amp {s['model']} screen {s['screen_renders']} renders over {len(s['pool'])} amps + fine-tune {s['finetune_renders']} "
                        f"(Output/Master/ChVol/Level locked); AFx-Rep distance {s['dist_before']:.3f} -> {s['dist_after']:.3f} (not comparable to the drive's)")
            elif v == "g":
                n, note = s["renders"], "CONTROL a + fixed light Room reverb Decay .40 Mix .35 (not measured)"
            else:
                n = s["renders"]
                note = (f"EXTRA Parametric EQ high-cut {s['values']['HighCut']:.0f} Hz + Low/Mid/High gains; LTAS residual "
                        f"{s['residual_before_db']:.2f} -> {s['residual_after_db']:.2f} dB")
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
                            "--dataset", f"colour-pilot-{song}-{v}", "--metric", "renders", "--value", str(n), "--renders", str(n),
                            "--wall-clock-s", str(round(st.get("wall_s", 0)) if v == "a" else ""),
                            "--notes", "T228 colour pilot (measurement only; nothing adopted): " + note], cwd=ROOT, env=env, check=True)
        c = st.get("controls", {})
        det = {k: bool(v["delay"]["present"]) if k.startswith("stem_plus_delay") else bool(v["tail"]["present"])
               for k, v in c.items() if k.startswith("stem_plus")}
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native",
                        "--dataset", f"colour-pilot-{song}-controls", "--metric", "renders", "--value", str(c.get("renders", 0)),
                        "--renders", str(c.get("renders", 0)),
                        "--notes", "T228 detection limits: known delay (Transistor Tape T .38 FB .3 Mix .45/.30) and Room reverb (Decay .5 Mix .50/.35) "
                        f"rendered onto the real stem; detected: {det}"],
                       cwd=ROOT, env=env, check=True)


def cmd_ledger_fix(songs: list[str]) -> None:
    """Corrections after the first ledger pass (the ledger is append-only): variant f withdrawn by the human (no cab blocks); b and
    the controls re-stated with the final alignment gate (explained variance + zero-lag tap); the totals after f was dropped."""
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "search"))

    def row(dataset, value, renders, notes):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "search", "--engine", "native", "--dataset", dataset,
                        "--metric", "renders", "--value", str(value), "--renders", str(renders), "--notes", notes], cwd=ROOT, env=env, check=True)

    for song in songs:
        st = json.loads((BASE / song / "state.json").read_text())
        row(f"colour-pilot-{song}-f", 0, 0, "T228 CORRECTION: variant f (amp + cab) withdrawn by the human 2026-10-25 (no cab blocks anywhere); the earlier row of "
                                              "this dataset is void and its audio, hlx and Stomp-load folder were deleted")
        b = st["b"]
        row(f"colour-pilot-{song}-b", b["renders"], b["renders"],
            "T228 CORRECTION (supersedes the earlier row; alignment gate is now explained variance >= 0.30 and zero-lag tap >= 0.15): direct measurement: "
            + b["delay_evidence"]["reason"] + " | " + b["tail_evidence"]["reason"] + "; identical to a")
        c = st["controls"]
        det = {k: bool(v["delay"]["present"]) if k.startswith("stem_plus_delay") else bool(v["tail"]["present"])
               for k, v in c.items() if k.startswith("stem_plus")}
        row(f"colour-pilot-{song}-controls", c["renders"], c["renders"],
            "T228 CORRECTION (supersedes the earlier row; control audio now saved, final gate): known delay (Transistor Tape T .38 FB .3 Mix .45/.30) and Room reverb "
            f"(Decay .5 Mix .50/.35) rendered onto the real stem; detected: {det}; real-stem explained variance {c['real_stem']['delay']['r2']:.2f}")
        row(f"colour-pilot-{song}-total", st["renders_total"], st["renders_total"],
            "T228 final renders of the pilot per song (a, controls, c, e, g, h; no f), without the earlier development re-runs")


def cmd_eqstudy(songs: list[str]) -> None:
    """Offline (0 renders): how far each EQ choice can bring the a-render's spectrum to the stem's. The 10-band is checked against its
    rendered residual; the high-cut rows apply an ideal 2nd-order Butterworth low-pass to the a-render in numpy (the pedal's own
    high-cut slope is not measured here), then fit the 10-band on what is left."""
    from scipy.signal import butter, sosfilt

    from tonematch.search import colour as C

    out = {}
    for song in songs:
        st = json.loads((BASE / song / "state.json").read_text())
        stem = sf.read(str(BASE / song / "audio" / "stem.wav"), dtype="float32")[0]
        a = sf.read(str(BASE / song / "audio" / "a.wav"), dtype="float32")[0]
        r0 = C.ltas_ratio_db(stem, a)
        g0 = C.eq_gains_from_ltas(r0)
        rows = {"no EQ": C.eq_residual_db(r0), "10-band (model: ratio - gains)": C.eq_residual_db(r0 - g0),
                "10-band as rendered": st["c"]["residual_after_db"]}
        for fc in (3000, 4000, 5000, 6000, 8000):
            y = sosfilt(butter(2, fc, "low", fs=SR, output="sos"), a).astype(np.float32)
            r = C.ltas_ratio_db(stem, y)
            rows[f"high-cut {fc} Hz alone"] = C.eq_residual_db(r)
            rows[f"high-cut {fc} Hz + 10-band"] = C.eq_residual_db(r - C.eq_gains_from_ltas(r))
        out[song] = {k: round(float(v), 2) for k, v in rows.items()}
        out[song]["ratio_db"] = [round(float(x), 1) for x in r0]
    (BASE / "eq_study.json").write_text(json.dumps(out, indent=1))
    for song, rows in out.items():
        print(song)
        for k, v in rows.items():
            if k != "ratio_db":
                print(f"   {k:34s} {v}")


def cmd_table(songs: list[str]) -> None:
    for song in songs:
        st = json.loads((BASE / song / "state.json").read_text())
        print(f"## {song}")
        print("renders:", {v: (st.get(v) or {}).get("renders") for v in ALL_V}, "total", st.get("renders_total"))
        b = st["b"]
        print("delay:", b["delay_evidence"]["reason"])
        print("reverb:", b["tail_evidence"]["reason"])
        for v in ("c", "d"):
            s = st[v]
            print(v, "EQ dB per band", dict(zip([str(x) for x in s["centres_hz"]], [round(g, 1) for g in s["gains_db"]])),
                  f"residual {s['residual_before_db']:.2f} -> {s['residual_after_db']:.2f}")
        e = st["e"]
        print("e:", e["model"], {k: round(x, 2) for k, x in e["values"].items()}, f"dist {e['dist_screen']:.3f} -> {e['dist_after']:.3f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["run", "pack", "ledger", "ledgerfix", "table", "loadlist", "eqstudy"])
    ap.add_argument("--songs", nargs="*", default=list(SONGS))
    ap.add_argument("--seed", type=int, default=228)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--redo-controls", action="store_true", help="re-run only the controls per song (and save their audio)")
    ap.add_argument("--remeasure", action="store_true", help="offline: recompute the evidence from the saved audio (0 renders)")
    ap.add_argument("--redo-h", action="store_true", help="re-run only variant h (the parametric EQ) per song")
    a = ap.parse_args(argv)
    BASE.mkdir(parents=True, exist_ok=True)
    if a.step == "run":
        for s in a.songs:
            redo_h(s) if a.redo_h else redo_controls(s) if a.redo_controls else remeasure(s) if a.remeasure else run_song(s, a.force)
    elif a.step == "pack":
        cmd_pack(a.songs, a.seed)
    elif a.step == "ledger":
        cmd_ledger(a.songs)
    elif a.step == "ledgerfix":
        cmd_ledger_fix(a.songs)
    elif a.step == "table":
        cmd_table(a.songs)
    elif a.step == "eqstudy":
        cmd_eqstudy(a.songs)
    else:
        cmd_loadlist()
    return 0


if __name__ == "__main__":
    sys.exit(main())
