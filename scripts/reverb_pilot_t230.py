#!/usr/bin/env python3
"""T230: reverb-model pilot (human 2026-10-25 "yes"). LOCAL ONLY: song audio stays under cache/ and reports/local/.
Measurement only; nothing is adopted; this tool never judges audio and never scores before the human has answered.

Per song (the seven T229 songs), after the T229 chain c (drive match -> fitted Graphic 10 Band, cache/colour-t229/<song>/):
27 reverb candidates = Room / Plate / Hall x Mix {0.25, 0.35, 0.50} x Decay {short, medium, long}, the three Decay values
per model chosen by impulse calibration so the measured RT60 is the same across models (Predelay 11 ms, LowCut 150 Hz,
HighCut 3700 Hz as T229), plus the two T229 settings r35 (Room) and p35 (Plate) as reused audio. Four candidate signals per
item, computed before any listening (src/tonematch/metrics/reverb_signals.py and AFx-Rep). One blind page per song: six
candidates (four from the grid by a seeded design + r35 + p35) and one of them repeated under a new letter; the human ranks
the seven clips 1 to 7 with ties. The analysis is pre-registered (PREREG below) and written to the session files before any
pack is built.

    nice -n 10 .venv/bin/python scripts/reverb_pilot_t230.py calibrate      # impulse renders, Decay values per model
    .venv/bin/python scripts/reverb_pilot_t230.py prereg                     # writes the pre-registration (before build)
    nice -n 10 .venv/bin/python scripts/reverb_pilot_t230.py render [--songs ...]
    nice -n 10 .venv/bin/python scripts/reverb_pilot_t230.py signals         # sealed per-candidate values
    .venv/bin/python scripts/reverb_pilot_t230.py build                      # pages, answer sheets, sealed key
    nice -n 10 .venv/bin/python scripts/reverb_pilot_t230.py hlx             # best.hlx per candidate + load list
    .venv/bin/python scripts/reverb_pilot_t230.py ledger
    .venv/bin/python scripts/reverb_pilot_t230.py score                      # ONLY after the human has answered
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SR = 48_000
SESSION = "reverb-pilot-v1"
BASE = ROOT / "cache" / "reverb-pilot-t230"
T229 = ROOT / "cache" / "colour-t229"
RATINGS_DIR = ROOT / "reports" / "local" / "ratings"
KEY_DIR = ROOT / "cache" / "ratings"
URL_BASE = "http://127.0.0.1:8000/local/ratings"
SONGS = ("mother", "seek-destroy", "teen-spirit", "territorial", "satisfaction", "cherub-rock", "ds-sample")
NOT_A_REAL_STEM = "ds-sample"           # T223: its target is not a separated real-song stem in the usual sense; pooled with and without it
NAME = {"mother": "Mother", "seek-destroy": "Seek & Destroy", "teen-spirit": "Smells Like Teen Spirit",
        "territorial": "Territorial Pissings", "satisfaction": "Satisfaction", "cherub-rock": "Cherub Rock", "ds-sample": "DS-Sample"}
TYPES = {"Room": "HD2_ReverbRoom", "Plate": "HD2_ReverbPlate", "Hall": "HD2_ReverbHall"}
MIXES = (0.25, 0.35, 0.50)
DECAY_LEVELS = ("short", "medium", "long")
FIXED = {"Predelay": 0.011, "LowCut": 150.0, "HighCut": 3700.0}
T229_ITEMS = {"t229-r35": ("Room", 0.40, 0.35, "r35"), "t229-p35": ("Plate", 0.40, 0.35, "p35")}
CAL_DECAYS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 1.00)
CAL_MIX = 0.50
CAL_LEN_S, CAL_T0_S = 12.0, 0.5
DESIGN_SEED = 230
TYPE_COUNTS = {"Hall": 2, "Room": 1, "Plate": 1}   # with r35 (Room) and p35 (Plate): two clips of each type per page
N_GRID_PICKS = 4
PERM_SEED = 230
PERM_DRAWS = 20000
LETTERS = "ABCDEFG"
SIGNALS = ("afxrep", "tail", "regressor", "dipfill")
SIGNAL_NAMES = {"afxrep": "(i) AFx-Rep distance to the stem",
                "tail": "(ii) note-tail statistics in the real note gaps (T228 gap rule)",
                "regressor": "(iii) time-specialist Room/Plate/Hall regressors, distance in (type, decay, mix) space",
                "dipfill": "(iv) envelope dip fill (10th/25th/50th percentile frame level re the 95th, 300 Hz-4 kHz, while the dry plays)"}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def cell_id(t: str, mix: float, dlev: str) -> str:
    return f"{t.lower()}-m{round(mix * 100):02d}-{dlev}"


def grid_cells() -> list[tuple[str, int, int]]:
    return [(t, i, j) for t in TYPES for i in range(len(MIXES)) for j in range(len(DECAY_LEVELS))]


def cell_of(c: tuple[str, int, int]) -> str:
    return cell_id(c[0], MIXES[c[1]], DECAY_LEVELS[c[2]])


def load_cal() -> dict:
    return json.loads((BASE / "calibration.json").read_text())


def item_settings(cal: dict) -> dict[str, dict]:
    """Every item of a song: id -> {type, model, values, mix, decay_level, decay}. 27 grid cells + the 2 T229 settings."""
    out = {}
    for t, i, j in grid_cells():
        dec = cal["decays"][TYPES[t]][j]
        out[cell_of((t, i, j))] = {"type": t, "model": TYPES[t], "mix": MIXES[i], "decay_level": DECAY_LEVELS[j], "decay": dec,
                                   "values": {"Decay": dec, **FIXED, "Mix": MIXES[i]}, "source": "grid"}
    for k, (t, dec, mix, v) in T229_ITEMS.items():
        out[k] = {"type": t, "model": TYPES[t], "mix": mix, "decay_level": "t229", "decay": dec,
                  "values": {"Decay": dec, **FIXED, "Mix": mix}, "source": f"T229 {v} (audio reused)"}
    return out


# ------------------------------------------------------------------------------------------ engine

class Engine:
    """One Native host, block-serial renders, LoadGuard before every render, render count."""

    def __init__(self):
        import match_block as MB

        from tonematch.catalog import load_catalog
        from tonematch.engines.host_budget import LoadGuard
        from tonematch.engines.native_chain import NativeBlockLibrary

        subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)
        self.guard = LoadGuard()
        self.guard.wait(lambda x: log(f"pause: busy cores {x}"))
        self.catalog = load_catalog()
        self.lib = NativeBlockLibrary(catalog=self.catalog)
        self.eng = MB.make_engine(144_000, log)            # as T228/T229
        self.renders = 0

    def render(self, model: str, values: dict, x: np.ndarray) -> np.ndarray:
        p = self.lib.preset(model, values)
        self.guard.wait(lambda d: log(f"pause: busy cores {d}"))
        self.eng.load(p)
        y = np.asarray(self.eng.render(np.asarray(x, np.float32)), np.float32)
        self.renders += 1
        if y.ndim > 1:                                     # Room, Plate, Hall are stereo-only: folded to mono as T228/T229
            y = y.mean(axis=1) if y.shape[1] <= 2 else y.mean(axis=0)
        return y[: len(x)]

    def close(self):
        if self.eng is not None:
            self.eng.close()
            self.eng = None
        subprocess.run([sys.executable, str(ROOT / "scripts" / "proc_check.py")], cwd=ROOT, check=False)


def bump_counts(key: str, n: int) -> None:
    p = BASE / "render_counts.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    d[key] = d.get(key, 0) + n
    p.write_text(json.dumps(d, indent=1))


# ------------------------------------------------------------------------------------------ calibrate

def cmd_calibrate(_a) -> None:
    from tonematch.metrics.reverb_signals import ir_rt60, pick_decays

    BASE.mkdir(parents=True, exist_ok=True)
    x = np.zeros(int(CAL_LEN_S * SR), np.float32)
    x[int(CAL_T0_S * SR)] = 0.5
    eng = Engine()
    rows = {}
    try:
        for t, m in TYPES.items():
            rows[m] = []
            for d in CAL_DECAYS:
                y = eng.render(m, {"Decay": d, **FIXED, "Mix": CAL_MIX}, x)
                r = ir_rt60(y, CAL_T0_S)
                rows[m].append({"decay": d, **r})
                log(f"{t} Decay {d:.2f}: RT60 {r['rt60_s']:.2f} s ({r['method']}), EDT {r['edt_s']:.2f} s")
    finally:
        n = eng.renders
        eng.close()
    bump_counts("calibration", n)
    pk = pick_decays({m: [(r["decay"], r["rt60_s"]) for r in v] for m, v in rows.items()})
    out = {"what": "impulse renders, Mix 0.50, Predelay 0.011, LowCut 150, HighCut 3700; RT60 250 Hz-4 kHz (T30 else T20)",
           "rows": rows, **pk, "renders": n}
    (BASE / "calibration.json").write_text(json.dumps(out, indent=1, default=float))
    log(f"targets {[round(v, 2) for v in pk['targets_s']]} s; decays {pk['decays']}; {n} renders")


# ------------------------------------------------------------------------------------------ pre-registration

def prereg_lines(cal: dict) -> list[str]:
    dv = "; ".join(f"{t} {', '.join(f'{v:.3f}' for v in cal['decays'][m])}" for t, m in TYPES.items())
    tg = ", ".join(f"{v:.2f}" for v in cal["targets_s"])
    return [
        ("Question: does any of four signals computed from the separated stem and the candidate renders, with no knowledge of "
         "the human's answers, order reverb candidates the way the human ranks them against the song's guitar? Measurement "
         "only; no bar; nothing is admitted or adopted. The human rules on what the result means."),
        ("Songs: the seven T229 songs (mother, seek-destroy, teen-spirit, territorial, satisfaction, cherub-rock, ds-sample). "
         "Chain before the reverb: T229 variant c (fine-tuned drive match -> fitted Graphic 10 Band), audio "
         "cache/colour-t229/<song>/audio/c.wav. Target: the T229 stem.wav (separated guitar, 10 s; ds-sample 15 s)."),
        (f"Items per song: 27 grid candidates, Room / Plate / Hall x Mix 0.25, 0.35, 0.50 x Decay short, medium, long, "
         f"Predelay 0.011, LowCut 150, HighCut 3700, rendered after c on one Native host (stereo folded to mono as T229). "
         f"Decay values per model chosen by impulse calibration at common RT60 targets {tg} s (250 Hz to 4 kHz): {dv}. "
         f"Plus the two T229 settings, r35 (Room Decay 0.40 Mix 0.35) and p35 (Plate Decay 0.40 Mix 0.35), as the T229 audio."),
        (f"Pack per song: 6 candidates = 4 grid cells drawn by numpy default_rng({DESIGN_SEED}) in song order, uniformly among "
         f"the 4-subsets with exactly 2 Hall, 1 Room, 1 Plate that cover all three Mix values and all three Decay levels, "
         f"+ r35 + p35; one of the 6, drawn by the same generator, is repeated under a new letter (7 clips, letters A-G "
         f"random). Each clip is stereo, left = the song's guitar, right = the candidate; the stem and all candidates of a "
         f"song LUFS-matched together to -23 with one common peak guard."),
        ("Human answer: each of the 7 clips gets a rank 1 (closest to the song's guitar) to 7; equal ranks are allowed (ties). "
         "Ranks are converted to average ranks over the 7 clips; the repeated candidate's value is the mean of its two; the six "
         "values are the human's order for the song. A song with any clip unranked is unanswered and left out (reported)."),
        ("Reported first, the listener's repeat consistency: per song the absolute difference between the two raw ranks of the "
         "repeated candidate; the mean over songs against the chance value 8/3 = 2.67 (two random distinct ranks out of 7); how "
         "many songs give the same rank and how many are within 1. A song with a difference of 3 or more is flagged as not "
         "reproducible; it stays in the analysis."),
        ("Signals (distances; lower = closer to the stem), all computed for every item before any pack is built and sealed with "
         "the key: (i) AFx-Rep, 1 - cosine of the embed_afxrep embeddings of stem and candidate (no mask). (ii) note tails, "
         "colour.gap_tail on stem and candidate (gaps: rebuilt dry silent >= 250 ms, c silent, stem below -12 dB re loud 60-100 "
         "ms in): |median slope difference| / 30 dB/s + |median early level difference| / 6 dB; undefined on a song whose stem "
         "has no real gap (before registration: mother, territorial, ds-sample have real gaps; the other four do not). (iii) "
         "time-specialist regressors: time_specialist.analyse(x, c) and the fitted Room, Plate and Hall seed regressors read "
         "on stem and candidate, the six numbers (Decay, Mix per regressor, normalised); RMS over the six of the difference "
         "divided by each regressor's leave-one-out error. (iv) envelope dip fill, chosen before listening: 10 ms frame levels "
         "of the 300 Hz-4 kHz band re their 95th percentile, floored at -60 dB, over the frames where the rebuilt dry is within 40 dB "
         "of its loud level (widened by 100 ms; long rests left out); 10th, 25th, 50th percentiles; mean absolute difference in dB."),
        ("Primary analysis, per signal: per song Spearman rho between the human's six values (1 = closest) and the signal's six "
         "distances, average ranks for ties; positive = agreement. Pooled = mean of the per-song rhos over the songs where "
         "the signal is defined, the human's values are not all equal and the distances are not all equal (others "
         "reported). p: permutation test, 20000 draws, numpy default_rng(230), the human's six values permuted within each "
         "song (one permutation per song per draw, shared by the four signals); one-sided (pooled rho > 0) and two-sided; "
         "Holm adjustment of the one-sided p over the four signals. Pooled over all seven songs (primary) and over the six "
         "without ds-sample (secondary)."),
        ("Decided pairs, per signal: within each song the 15 pairs of the six candidates; a pair the human valued equal is a "
         "tie and is excluded from the decided count (reported); a pair with equal distances is excluded too (reported). "
         "Agreement = share of decided pairs where the candidate with the smaller distance has the better human value. Pairs "
         "are not independent; the count is descriptive. Also: how often the signal's closest candidate is the human's unique "
         "closest (chance 1 in 6)."),
        ("Descriptive, not candidate signals: the human's values against the Mix knob and the Decay level (Spearman, pooled "
         "mean), the mean human value by reverb type, and where r35 and p35 land. These describe the human's preference; they "
         "are not tested as predictors."),
        ("Proposed reading for the human, not a bar: a signal 'agrees' if its pooled rho is positive with Holm-adjusted "
         "one-sided p < 0.05 and it is positive on at least 5 of its defined songs; a model is then proposed only for a signal "
         "that agrees. Otherwise the by-ear starting point 'Plate, Mix 0.35' stands as the lead proposed. The human decides."),
        ("Size: 7 songs x 6 candidates; one song's rho on 6 items needs about 0.83 to be significant alone, so per-song values are "
         "directional; the pooled mean over 7 songs is the test. With one repeat per song the listener's consistency is only "
         "roughly known; a weak agreement can be the listener's noise."),
        ("Blind: the pages and trials.json carry no type, setting, signal value or item id; letters are random per song; the "
         "key (cache/ratings/reverb-pilot-v1.key.json) holds the design, the letters and every signal value, and is not "
         "opened before the human's answers exist. The scorer reads the key only after the answers are on disk."),
    ]


def prereg_text(cal: dict) -> str:
    return "\n".join(f"{i + 1}. {t}" for i, t in enumerate(prereg_lines(cal)))


def cmd_prereg(_a) -> None:
    cal = load_cal()
    txt = prereg_text(cal)
    h = hashlib.sha256(txt.encode()).hexdigest()
    p = KEY_DIR / f"{SESSION}.prereg.json"
    if p.exists():
        old = json.loads(p.read_text())
        if old["sha256"] != h:
            raise SystemExit(f"a different pre-registration already exists ({old['sha256'][:12]}); refusing to overwrite")
        log(f"pre-registration unchanged, sha256 {h}")
        return
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"session": SESSION, "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": txt, "sha256": h}
    p.write_text(json.dumps(rec, indent=1))
    (BASE / "prereg.json").write_text(json.dumps(rec, indent=1))
    print(txt)
    log(f"pre-registration written: {p}  sha256 {h}")


AMENDMENTS = [
    ("Amendment 1, written after the signals were computed and before any pack was built or any answer existed: on a song "
     "where signal (ii) is defined, a candidate whose own gaps all fail the real-gap rule (its reverb keeps it within 12 dB "
     "of its loud level in every gap, so no tail statistic exists) is further from the stem than every measured candidate: "
     "for ranking it gets the song's largest measured tail distance + 1 (ties among such candidates). Reason: 6 of mother's "
     "29 items are like this; the registration did not say what happens to them."),
]


def full_prereg(rec: dict) -> tuple[str, str]:
    """(registered text + amendments, its sha256)."""
    txt = rec["text"] + "".join(f"\n{a['text']}" for a in rec.get("amendments", []))
    return txt, hashlib.sha256(txt.encode()).hexdigest()


def cmd_amend(_a) -> None:
    p = KEY_DIR / f"{SESSION}.prereg.json"
    rec = json.loads(p.read_text())
    if (KEY_DIR / f"{SESSION}.key.json").exists():
        raise SystemExit("the pages are built: no more amendments")
    have = [a["text"] for a in rec.get("amendments", [])]
    for t in AMENDMENTS:
        if t not in have:
            rec.setdefault("amendments", []).append({"written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": t})
    _txt, h = full_prereg(rec)
    rec["sha256_with_amendments"] = h
    p.write_text(json.dumps(rec, indent=1))
    (BASE / "prereg.json").write_text(json.dumps(rec, indent=1))
    log(f"amendments {len(rec['amendments'])}; sha256 with amendments {h}")


def load_prereg() -> dict:
    """The registration as used from build on: {"text": registered text + amendments, "sha256": its hash, "sha256_v1"}."""
    p = KEY_DIR / f"{SESSION}.prereg.json"
    if not p.exists():
        raise SystemExit("no pre-registration: run `prereg` first")
    rec = json.loads(p.read_text())
    if hashlib.sha256(rec["text"].encode()).hexdigest() != rec["sha256"]:
        raise SystemExit("pre-registration text does not match its hash")
    txt, h = full_prereg(rec)
    if rec.get("amendments") and rec.get("sha256_with_amendments") != h:
        raise SystemExit("amended pre-registration does not match its hash")
    return {"text": txt, "sha256": h, "sha256_v1": rec["sha256"]}


# ------------------------------------------------------------------------------------------ render

def t229_audio(song: str, name: str) -> np.ndarray:
    return sf.read(str(T229 / song / "audio" / f"{name}.wav"), dtype="float32")[0]


def cmd_render(a) -> None:
    cal = load_cal()
    items = item_settings(cal)
    eng = None
    try:
        for s in a.songs:
            d = BASE / s / "audio"
            d.mkdir(parents=True, exist_ok=True)
            st_p = BASE / s / "state.json"
            st = json.loads(st_p.read_text()) if st_p.exists() else {"renders": 0}
            c = t229_audio(s, "c")
            todo = [k for k, v in items.items() if v["source"] == "grid" and not (d / f"{k}.wav").exists()]
            if not todo:
                log(f"{s}: all 27 rendered")
                continue
            if eng is None:
                eng = Engine()
            n0 = eng.renders
            for k in todo:
                v = items[k]
                y = eng.render(v["model"], v["values"], c)
                if not np.all(np.isfinite(y)) or float(np.sqrt(np.mean(y ** 2))) < 1e-5:
                    raise RuntimeError(f"{s} {k}: render failed or silent")
                sf.write(str(d / f"{k}.wav"), y, SR, subtype="FLOAT")
            st["renders"] += eng.renders - n0
            st["c_sha256"] = hashlib.sha256((T229 / s / "audio" / "c.wav").read_bytes()).hexdigest()
            st_p.write_text(json.dumps(st, indent=1))
            bump_counts(f"grid:{s}", eng.renders - n0)
            log(f"{s}: {eng.renders - n0} renders")
        if a.check_t229 and not (BASE / "t229_check.json").exists():
            if eng is None:
                eng = Engine()
            s = a.songs[0]
            k, (t, dec, mix, v) = next(iter(T229_ITEMS.items()))
            y = eng.render(TYPES[t], {"Decay": dec, **FIXED, "Mix": mix}, t229_audio(s, "c"))
            z = t229_audio(s, v)
            n = min(len(y), len(z))
            res = {"song": s, "item": k, "max_abs_diff": float(np.max(np.abs(y[:n] - z[:n]))), "corr": float(np.corrcoef(y[:n], z[:n])[0, 1])}
            (BASE / "t229_check.json").write_text(json.dumps(res, indent=1))
            bump_counts("check_t229", 1)
            log(f"T229 reproduction check: {res}")
    finally:
        if eng is not None:
            eng.close()


# ------------------------------------------------------------------------------------------ signals

def item_audio(song: str, k: str) -> np.ndarray:
    if k in T229_ITEMS:
        return t229_audio(song, T229_ITEMS[k][3])
    return sf.read(str(BASE / song / "audio" / f"{k}.wav"), dtype="float32")[0]


def cmd_signals(a) -> None:
    from tonematch.metrics import afxrep
    from tonematch.metrics import reverb_signals as RV
    from tonematch.search.time_specialist import load_seed_bank

    load_prereg()                                          # the signal definitions are the registered ones
    cal = load_cal()
    items = item_settings(cal)
    bank = load_seed_bank()
    scales = RV.regressor_scales(bank)
    out = {"session": SESSION, "computed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "regressor_scales": scales, "songs": {}}
    for s in a.songs:
        stem, dry, c = t229_audio(s, "stem"), t229_audio(s, "dry"), t229_audio(s, "c")
        f_stem = afxrep.features(stem)
        ts = RV.tail_stats(stem, c, dry)
        vs = RV.regressor_vector(stem, c, bank)
        mask = RV.active_mask(dry)
        ds = RV.dip_stats(stem, mask)
        rec = {"stem": {"tail": ts.__dict__, "regressor": vs, "dipfill": ds.tolist()}, "items": {}}
        for k in items:
            y = item_audio(s, k)
            n = min(len(y), len(stem))
            y = y[:n]
            tc = RV.tail_stats(y, c[:n], dry[:n])
            vc = RV.regressor_vector(y, c[:n], bank)
            dc = RV.dip_stats(y, mask)
            rec["items"][k] = {
                "afxrep": afxrep.distance(f_stem, afxrep.features(y)),
                "tail": RV.tail_distance(ts, tc),
                "regressor": RV.regressor_distance(vs, vc, scales),
                "dipfill": RV.dip_distance(ds, dc),
                "raw": {"tail": tc.__dict__, "regressor": vc, "dipfill": dc.tolist()}}
        out["songs"][s] = rec
        log(f"{s}: signals for {len(items)} items; stem tail: {ts.reason}")
    p = BASE / "sealed" / "signals.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=_json))
    log(f"sealed signals {p}  sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}")


def _json(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


# ------------------------------------------------------------------------------------------ design and build

def build_design(songs=SONGS, seed: int = DESIGN_SEED) -> dict:
    """Per song: the 4 grid cells, the 6 candidates, the repeated one, and the letter of each of the 7 clips."""
    from tonematch.metrics.reverb_signals import draw_design

    rng = np.random.default_rng(seed)
    cells = grid_cells()
    out = {}
    for s in songs:
        picks = [cell_of(c) for c in draw_design(rng, cells, N_GRID_PICKS, TYPE_COUNTS)]
        six = picks + list(T229_ITEMS)
        rep = six[int(rng.integers(0, 6))]
        clips = six + [rep]
        perm = rng.permutation(7)
        letters = {LETTERS[int(perm[i])]: clips[i] for i in range(7)}
        rep_letters = sorted(L for L, k in letters.items() if k == rep)
        out[s] = {"grid_picks": picks, "candidates": six, "repeat": rep, "repeat_letters": rep_letters,
                  "letters": dict(sorted(letters.items()))}
    return out


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:15px system-ui;max-width:820px;margin:2em auto;padding:0 1em;color:#222}
.row{margin:.5em 0;padding:.4em .6em;border:1px solid #ccc;border-radius:6px}.row.done{border-color:#4a8}
.row b{display:inline-block;width:2em;font-size:18px}audio{width:calc(100% - 12em);vertical-align:middle}
select,button{font:inherit;padding:.3em .7em;margin:.2em}textarea{width:100%}.hint{color:#555;font-size:13px}
#bar{position:sticky;top:0;background:#fff;padding:.4em 0;border-bottom:1px solid #ccc}</style></head><body>
<div id="bar"><b>__TITLE__</b> &nbsp; ranked <span id="cnt">0</span> of 7 &nbsp;
<button id="dl">Download answers</button> <span id="msg"></span></div>
__NOTE__
<div class="row"><b>&#9835;</b> The song's guitar alone <audio controls preload="auto" src="song.wav"></audio></div>
<div id="clips"></div>
<p>Notes (optional)</p><textarea id="notes" rows="3"></textarea>
<p class="hint"><a href="../index.html">Back to all songs</a></p>
<script>
const SESSION=__SESSION_JS__, SONG=__SONG_JS__, LET=__LETTERS__, KEY=SESSION+":"+SONG;
let ans=JSON.parse(localStorage.getItem(KEY)||'{"ranks":{},"notes":""}');
const save=()=>localStorage.setItem(KEY,JSON.stringify(ans));
function count(){document.getElementById("cnt").textContent=LET.filter(l=>ans.ranks[l]).length;}
function pauseOthers(e){document.querySelectorAll("audio").forEach(a=>{if(a!==e.target)a.pause();});}
function render(){const root=document.getElementById("clips");root.innerHTML="";
 LET.forEach(l=>{const d=document.createElement("div");d.className="row"+(ans.ranks[l]?" done":"");
  d.innerHTML="<b>"+l+"</b><audio controls preload='auto' src='clip_"+l+".wav'></audio> rank ";
  const s=document.createElement("select");
  s.innerHTML="<option value=''>-</option>"+[1,2,3,4,5,6,7].map(i=>"<option>"+i+"</option>").join("");
  s.value=ans.ranks[l]??"";s.onchange=()=>{ans.ranks[l]=s.value?+s.value:null;save();render();};
  d.appendChild(s);root.appendChild(d);});
 document.querySelectorAll("audio").forEach(a=>a.addEventListener("play",pauseOthers));count();}
document.getElementById("notes").value=ans.notes||"";
document.getElementById("notes").oninput=e=>{ans.notes=e.target.value;save();};
document.getElementById("dl").onclick=()=>{const p=JSON.stringify({session:SESSION,song:SONG,saved_at:new Date().toISOString(),
 ranks:ans.ranks,notes:ans.notes||""},null,1);const u=URL.createObjectURL(new Blob([p],{type:"application/json"}));
 const a=document.createElement("a");a.href=u;a.download=SESSION+"."+SONG+".answers.json";a.click();
 document.getElementById("msg").textContent="downloaded (check your Downloads folder)";};
render();
</script></body></html>"""

NOTE = """<p>Headphones. Every clip: <b>left ear = the song's guitar</b> (separated from the record), <b>right ear = one
candidate</b>: our drive match and EQ with a different reverb after it. All clips are loudness-matched. Rank the seven
clips <b>1 = closest to the song's guitar</b> to <b>7 = furthest</b>. Judge how close the space and ambience sound to the
song (and the tone overall), not the notes and not which you like best. Use the same number twice if you cannot tell two
clips apart. One clip is repeated on purpose under another letter; rank it as if new. Answers save in this browser as you
go. When done press <b>Download answers</b>.</p>"""

INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>Reverb pilot T230</title>
<style>body{font:15px system-ui;max-width:760px;margin:2em auto;padding:0 1em;color:#222}li{margin:.4em 0}
button{font:inherit;padding:.35em .8em}</style></head><body>
<h1>Reverb pilot (T230): seven songs</h1>
<p>One page per song, seven clips each. Rank every clip on every page, then press <b>Download all answers</b> below (or the
Download button on each page). Save the file(s) in <code>reports/local/ratings/</code> (or leave them in Downloads) and run
<code>python scripts/reverb_pilot_t230.py score</code>. Do not open anything under <code>cache/ratings/</code> or
<code>cache/reverb-pilot-t230/sealed/</code> before that.</p>
<ol>__LIST__</ol>
<p><button id="all">Download all answers</button> <span id="msg"></span></p>
<script>
const SESSION=__SESSION_JS__, SONGS=__SONGS__;
function stat(){SONGS.forEach(s=>{const a=JSON.parse(localStorage.getItem(SESSION+":"+s)||'{"ranks":{}}');
 const n=Object.values(a.ranks||{}).filter(v=>v).length;document.getElementById("n-"+s).textContent=n+" of 7 ranked";});}
document.getElementById("all").onclick=()=>{const out={session:SESSION,saved_at:new Date().toISOString(),songs:{}};
 SONGS.forEach(s=>{out.songs[s]=JSON.parse(localStorage.getItem(SESSION+":"+s)||'{"ranks":{},"notes":""}');});
 const u=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:"application/json"}));
 const a=document.createElement("a");a.href=u;a.download=SESSION+".answers.json";a.click();
 document.getElementById("msg").textContent="downloaded (check your Downloads folder)";};
stat();
</script></body></html>"""


def cmd_build(a) -> None:
    import dry_ablation_t224 as DA

    pre = load_prereg()
    if (KEY_DIR / f"{SESSION}.key.json").exists() and not a.rebuild:
        raise SystemExit("the pages are already built (the key exists); pass --rebuild only if no one has listened yet")
    sig_p = BASE / "sealed" / "signals.json"
    if not sig_p.exists():
        raise SystemExit("no sealed signals: run `signals` first (they must exist before any pack)")
    sig_sha = hashlib.sha256(sig_p.read_bytes()).hexdigest()
    signals = json.loads(sig_p.read_text())
    cal = load_cal()
    items = item_settings(cal)
    design = build_design()
    root = RATINGS_DIR / SESSION
    root.mkdir(parents=True, exist_ok=True)
    pub = {"session": SESSION, "kind": "reverb-pilot", "songs": list(SONGS), "preregistration": pre["text"],
           "preregistration_sha256": pre["sha256"], "preregistration_v1_sha256": pre["sha256_v1"],
           "sealed_signals_sha256": sig_sha, "pages": {}}
    keyd = {"session": SESSION, "design_seed": DESIGN_SEED, "preregistration_sha256": pre["sha256"],
            "sealed_signals_sha256": sig_sha, "calibration": {k: cal[k] for k in ("targets_s", "decays", "range_s")},
            "items": items, "songs": {}}
    for s in SONGS:
        d = root / s
        d.mkdir(parents=True, exist_ok=True)
        stem = t229_audio(s, "stem")
        des = design[s]
        aud = {k: item_audio(s, k) for k in des["candidates"]}
        n = min([len(stem)] + [len(v) for v in aud.values()])
        st2, m2, gains = DA.level_match_pack(stem[:n], {k: v[:n] for k, v in aud.items()})
        sf.write(str(d / "song.wav"), st2, SR, subtype="FLOAT")
        for L, k in des["letters"].items():
            sf.write(str(d / f"clip_{L}.wav"), np.stack([st2, m2[k]], 1), SR, subtype="FLOAT")
        letters = list(LETTERS)
        page = (PAGE.replace("__TITLE__", html.escape(f"Reverb pilot: {NAME[s]}")).replace("__NOTE__", NOTE)
                .replace("__SESSION_JS__", json.dumps(SESSION)).replace("__SONG_JS__", json.dumps(s))
                .replace("__LETTERS__", json.dumps(letters)))
        (d / "index.html").write_text(page)
        sheet = [f"# Reverb pilot, {NAME[s]}: answer sheet", "",
                 ("Left ear = the song's guitar, right ear = a candidate. Rank 1 (closest) to 7 (furthest); ties allowed. "
                 "Do not open anything under cache/ratings/ before you have answered."), "", "| clip | rank |", "|---|---|"]
        sheet += [f"| {L} | |" for L in letters] + ["", "Notes:", ""]
        (d / "answer_sheet.md").write_text("\n".join(sheet))
        pub["pages"][s] = {"url": f"{URL_BASE}/{SESSION}/{s}/index.html", "letters": letters, "excerpt_s": n / SR}
        keyd["songs"][s] = {**des, "lufs_gains_db": gains, "excerpt_s": n / SR,
                            "signals": {k: {g: signals["songs"][s]["items"][k][g] for g in SIGNALS} for k in items},
                            "stem_measurements": signals["songs"][s]["stem"]}
    lst = "".join(f'<li><a href="{s}/index.html">{html.escape(NAME[s])}</a> <span id="n-{s}"></span></li>' for s in SONGS)
    (root / "index.html").write_text(INDEX.replace("__LIST__", lst).replace("__SESSION_JS__", json.dumps(SESSION))
                                     .replace("__SONGS__", json.dumps(list(SONGS))))
    (root / "trials.json").write_text(json.dumps(pub, indent=1))
    keyf = KEY_DIR / f"{SESSION}.key.json"
    keyf.write_text(json.dumps(keyd, indent=1, default=_json))
    log(f"pages {root}/<song>/index.html ({URL_BASE}/{SESSION}/index.html); key {keyf} (sealed); "
        f"prereg {pre['sha256'][:12]}; signals {sig_sha[:12]}")


# ------------------------------------------------------------------------------------------ hlx and load list

def cmd_hlx(_a) -> None:
    import colour_pilot_t228 as CP

    from tonematch.catalog import load_catalog
    from tonematch.engines.native_chain import NativeBlockLibrary
    from tonematch.preset import validate as pv
    from tonematch.preset import write_hlx as wh
    from tonematch.search.chain_builder import BlockSpec, assemble_preset

    cat = load_catalog()
    lib = NativeBlockLibrary(catalog=cat)
    items = item_settings(load_cal())
    L = ["# T230 reverb pilot: load list (local only)", "",
         ("Nothing was loaded on the Stomp by this task. One validated preset per candidate (27 grid + the two T229 settings) "
         "per song. This list names settings, not letters: it does not tell which clip on a page is which. To put one in the "
         "edit buffer (never a flash write):"), "",
         "    .venv/bin/python scripts/stomp_live_preview.py --dir <folder with the .hlx as current_best.hlx> --once --no-restore-on-exit",
         "", "or import the .hlx in HX Edit. On the Stomp the reverb is stereo; the renders fold it to mono.", "",
         "| song | candidate | reverb | Decay | Mix | hlx | validate |", "|---|---|---|---|---|---|---|"]
    res = {}
    for s in SONGS:
        st = json.loads((T229 / s / "state.json").read_text())
        base_blocks = [(m, v) for m, v in st["c"]["blocks_all"]]
        for k, v in items.items():
            if k in T229_ITEMS:
                path = T229 / s / "hlx" / T229_ITEMS[k][3] / "best.hlx"
                code = st["hlx"][T229_ITEMS[k][3]]["validate_exit"]
            else:
                bl = [*base_blocks, (v["model"], v["values"])]
                specs = [BlockSpec(cat["models"][m]["category"], m).with_values(vals) for m, vals in bl]
                p = assemble_preset(specs, lib, name=f"{s[:5]}-{k[:10]}")
                viol = [x for x in pv(p, cat) if getattr(x, "severity", "error") == "error"]
                if viol:
                    raise RuntimeError(f"{s} {k}: {viol}")
                path = BASE / s / "hlx" / k / "best.hlx"
                path.parent.mkdir(parents=True, exist_ok=True)
                wh(p, path)
                code, _t = CP.validate_hlx(path)
            res[f"{s}/{k}"] = code
            L.append(f"| {s} | {k} | {v['type']} | {v['decay']:.3f} | {v['mix']:.2f} | `{path}` | exit {code} |")
    (BASE / "load_list.md").write_text("\n".join(L) + "\n")
    bad = {k: c for k, c in res.items() if c != 0}
    log(f"load list {BASE / 'load_list.md'}; {len(res)} presets; validate failures {bad or 'none'}")


# ------------------------------------------------------------------------------------------ ledger

def cmd_ledger(_a) -> None:
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
    pub = json.loads((RATINGS_DIR / SESSION / "trials.json").read_text())
    counts = json.loads((BASE / "render_counts.json").read_text())
    grid = sum(v for k, v in counts.items() if k.startswith("grid:"))
    dev = sum(v for k, v in counts.items() if not k.startswith("grid:"))
    tot = grid + dev
    rows = [("trials_built", len(SONGS), None,
             (f"T230 reverb pilot: {len(SONGS)} blind ranking pages of 7 clips (6 candidates + 1 repeat) songs {' '.join(SONGS)}; "
             f"design seed {DESIGN_SEED}; preregistration sha256 {pub['preregistration_sha256']}; sealed signals sha256 "
             f"{pub['sealed_signals_sha256']}; built for the human; nothing scored; nothing adopted")),
            ("renders", tot, tot,
             f"T230 Native renders: {grid} grid (27 per song x 7) + {dev} development/calibration ({counts}); one host; nice 10; LoadGuard")]
    for metric, val, ren, note in rows:
        cmd = [sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "native",
               "--dataset", SESSION, "--metric", metric, "--value", str(val), "--notes", note]
        if ren is not None:
            cmd += ["--renders", str(ren)]
        subprocess.run(cmd, cwd=ROOT, env=env, check=True)


# ------------------------------------------------------------------------------------------ analysis (pre-registered)

def _avg_ranks(v) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(np.asarray(v, float), method="average")


def _rho(x, y) -> float:
    a, b = _avg_ranks(x), _avg_ranks(y)
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def human_values(des: dict, ranks: dict) -> tuple[np.ndarray, dict] | None:
    """The human's six values in `des['candidates']` order (average ranks over the 7 clips; the repeat averaged) and the
    repeat record. None if any clip is unranked."""
    if not all(ranks.get(L) for L in LETTERS):
        return None
    v = dict(zip(LETTERS, _avg_ranks([float(ranks[L]) for L in LETTERS]), strict=True))
    by_item: dict[str, list[float]] = {}
    for L, k in des["letters"].items():
        by_item.setdefault(k, []).append(v[L])
    h = np.array([float(np.mean(by_item[k])) for k in des["candidates"]])
    r1, r2 = (float(ranks[L]) for L in des["repeat_letters"])
    return h, {"item": des["repeat"], "raw": [r1, r2], "abs_diff": abs(r1 - r2)}


def signal_vector(key: dict, song: str, g: str) -> np.ndarray:
    """The six distances of a song for signal g, in candidate order. Amendment 1: for (ii), on a song where it is defined
    (some item measurable), an unmeasurable candidate gets the song's largest measured distance + 1."""
    sig = key["songs"][song]["signals"]
    d = np.array([float(sig[k][g]) for k in key["songs"][song]["candidates"]])
    if g == "tail":
        allv = np.array([float(v[g]) for v in sig.values()])
        if np.isfinite(allv).any():
            d = np.where(np.isfinite(d), d, float(np.nanmax(allv)) + 1.0)
    return d


def holm(ps: dict[str, float]) -> dict[str, float]:
    ks = sorted((k for k in ps if ps[k] is not None and not math.isnan(ps[k])), key=lambda k: ps[k])
    m, out, run = len(ks), {}, 0.0
    for i, k in enumerate(ks):
        run = max(run, min(1.0, (m - i) * ps[k]))
        out[k] = run
    return out


def analyse(key: dict, answers: dict[str, dict], draws: int = PERM_DRAWS, seed: int = PERM_SEED) -> dict:
    """Exactly the pre-registered analysis. `answers` = {song: {"ranks": {letter: 1..7}}}."""
    songs = [s for s in key["songs"]]
    H, rep, unanswered = {}, {}, []
    for s in songs:
        a = answers.get(s)
        r = human_values(key["songs"][s], (a or {}).get("ranks") or {})
        if r is None:
            unanswered.append(s)
            continue
        H[s], rep[s] = r
    out: dict = {"preregistration_sha256": key["preregistration_sha256"], "unanswered": unanswered}
    diffs = [rep[s]["abs_diff"] for s in rep]
    out["repeat"] = {"by_song": rep, "mean_abs_diff": float(np.mean(diffs)) if diffs else None, "chance_mean_abs_diff": 8 / 3,
                     "same_rank": int(sum(d == 0 for d in diffs)), "within_1": int(sum(d <= 1 for d in diffs)),
                     "not_reproducible": [s for s in rep if rep[s]["abs_diff"] >= 3]}
    D = {g: {s: signal_vector(key, s, g) for s in H} for g in SIGNALS}

    def live(g, ss):
        return [s for s in ss if np.all(np.isfinite(D[g][s])) and np.std(H[s]) > 0 and np.std(D[g][s]) > 0]

    rng = np.random.default_rng(seed)
    order = [s for s in songs if s in H]
    perms = [{s: rng.permutation(6) for s in order} for _ in range(draws)]
    res = {}
    for g in SIGNALS:
        per = {s: _rho(H[s], D[g][s]) if s in live(g, [s]) else float("nan") for s in order}
        sets = {"all7": live(g, order), "without_ds_sample": live(g, [s for s in order if s != NOT_A_REAL_STEM])}
        pooled = {}
        for name, ls in sets.items():
            if not ls:
                pooled[name] = {"n_songs": 0, "mean_rho": None, "p_one_sided": None, "p_two_sided": None, "songs_positive": 0}
                continue
            obs = float(np.mean([per[s] for s in ls]))
            null = np.array([np.mean([_rho(H[s][p[s]], D[g][s]) for s in ls]) for p in perms])
            pooled[name] = {"n_songs": len(ls), "songs": ls, "mean_rho": obs,
                            "p_one_sided": float((np.sum(null >= obs - 1e-12) + 1) / (len(null) + 1)),
                            "p_two_sided": float((np.sum(np.abs(null) >= abs(obs) - 1e-12) + 1) / (len(null) + 1)),
                            "songs_positive": int(sum(per[s] > 0 for s in ls))}
        k = n = ties_h = ties_d = 0
        top1 = top1_n = 0
        for s in [s for s in order if np.all(np.isfinite(D[g][s]))]:
            h, d = H[s], D[g][s]
            for i in range(6):
                for j in range(i + 1, 6):
                    if h[i] == h[j]:
                        ties_h += 1
                        continue
                    if abs(d[i] - d[j]) < 1e-12:
                        ties_d += 1
                        continue
                    n += 1
                    k += (h[i] < h[j]) == (d[i] < d[j])
            if np.sum(h == h.min()) == 1:
                top1_n += 1
                top1 += int(np.argmin(d) == np.argmin(h))
        res[g] = {"per_song_rho": per, "undefined_or_undecided": [s for s in order if s not in sets["all7"]], "pooled": pooled,
                  "pairs": {"decided": n, "agree": k, "rate": k / n if n else None, "human_ties": ties_h, "signal_ties": ties_d},
                  "top1": {"hit": top1, "songs": top1_n, "chance": 1 / 6}}
    hp = holm({g: res[g]["pooled"]["all7"]["p_one_sided"] for g in SIGNALS})
    for g in SIGNALS:
        res[g]["pooled"]["all7"]["p_holm"] = hp.get(g)
        p = res[g]["pooled"]["all7"]
        res[g]["proposed_reading_agrees"] = bool(p["mean_rho"] is not None and p["mean_rho"] > 0 and (p.get("p_holm") or 1) < 0.05
                                                 and p["songs_positive"] >= 5)
    out["signals"] = res
    # descriptive: the human's preference against the design factors
    items = key["items"]
    desc = {"mix_rho": {}, "decay_rho": {}, "mean_value_by_type": {}, "t229_values": {}}
    lev = {"short": 0, "medium": 1, "long": 2}
    by_type: dict[str, list[float]] = {}
    for s in H:
        cs = key["songs"][s]["candidates"]
        desc["mix_rho"][s] = _rho(H[s], [items[k]["mix"] for k in cs])
        g = [(H[s][i], lev[items[k]["decay_level"]]) for i, k in enumerate(cs) if items[k]["decay_level"] in lev]
        desc["decay_rho"][s] = _rho([a for a, _ in g], [b for _, b in g]) if len(g) >= 3 else float("nan")
        for i, k in enumerate(cs):
            by_type.setdefault(items[k]["type"], []).append(float(H[s][i]))
        desc["t229_values"][s] = {k: float(H[s][cs.index(k)]) for k in T229_ITEMS}
    desc["mean_value_by_type"] = {t: float(np.mean(v)) for t, v in by_type.items()}
    for k in ("mix_rho", "decay_rho"):
        vals = [v for v in desc[k].values() if not math.isnan(v)]
        desc[k + "_mean"] = float(np.mean(vals)) if vals else None
    out["descriptive"] = desc
    return out


def fmt(res: dict) -> str:
    f = lambda v, p="{:.2f}": "-" if v is None or (isinstance(v, float) and math.isnan(v)) else p.format(v)
    r = res["repeat"]
    L = [f"Reverb pilot T230; pre-registration sha256 {res['preregistration_sha256'][:12]}; unanswered songs {res['unanswered']}",
         "", "The listener first (repeat consistency):"]
    for s, v in r["by_song"].items():
        L.append(f"  {s:<14} repeated candidate ranked {v['raw'][0]:g} and {v['raw'][1]:g} (difference {v['abs_diff']:g})")
    L.append(f"  mean difference {f(r['mean_abs_diff'])} (chance {r['chance_mean_abs_diff']:.2f}); same rank {r['same_rank']}, "
             f"within 1: {r['within_1']}; not reproducible (3 or more): {r['not_reproducible']}")
    for g, v in res["signals"].items():
        p, q = v["pooled"]["all7"], v["pooled"]["without_ds_sample"]
        L += ["", f"{SIGNAL_NAMES[g]}:",
              "  per song rho: " + ", ".join(f"{s} {f(x)}" for s, x in v["per_song_rho"].items()),
              (f"  pooled (all seven) mean rho {f(p['mean_rho'])} over {p['n_songs']} songs, {p['songs_positive']} positive; "
               f"p one-sided {f(p['p_one_sided'], '{:.4f}')}, two-sided {f(p['p_two_sided'], '{:.4f}')}, Holm {f(p.get('p_holm'), '{:.4f}')}"),
              (f"  without ds-sample: mean rho {f(q['mean_rho'])} over {q['n_songs']} songs, p one-sided {f(q['p_one_sided'], '{:.4f}')}"),
              (f"  decided pairs {v['pairs']['agree']}/{v['pairs']['decided']} = {f(v['pairs']['rate'], '{:.0%}')} "
               f"(human ties {v['pairs']['human_ties']}, signal ties {v['pairs']['signal_ties']}); closest = human's closest "
               f"{v['top1']['hit']}/{v['top1']['songs']} (chance 1 in 6)"),
              f"  undefined or undecided songs {v['undefined_or_undecided']}; proposed reading 'agrees': {v['proposed_reading_agrees']}"]
    d = res["descriptive"]
    L += ["", "Descriptive (the human's preference, not tested):",
          f"  human value vs Mix: mean rho {f(d['mix_rho_mean'])} (positive = more Mix ranked further)",
          f"  human value vs Decay level: mean rho {f(d['decay_rho_mean'])}",
          "  mean human value by type: " + ", ".join(f"{t} {v:.2f}" for t, v in d["mean_value_by_type"].items()),
          "  T229 r35 / p35 values: " + ", ".join(f"{s} {v['t229-r35']:.1f}/{v['t229-p35']:.1f}" for s, v in d["t229_values"].items())]
    return "\n".join(L)


def find_answers(downloads: Path | None = None) -> dict[str, dict]:
    """{song: {"ranks":..}} from the combined file (index page) or per-song files, in reports/local/ratings/ or Downloads
    (newest wins per song)."""
    dl = Path.home() / "Downloads" if downloads is None else downloads
    found: dict[str, tuple[float, dict]] = {}
    paths = []
    for d in (RATINGS_DIR, dl):
        if d.exists():
            paths += list(d.glob(f"{SESSION}.answers*.json")) + list(d.glob(f"{SESSION}.*.answers*.json"))
    for p in sorted(set(paths), key=lambda q: q.stat().st_mtime):
        j = json.loads(p.read_text())
        if j.get("session") != SESSION:
            continue
        t = p.stat().st_mtime
        if "songs" in j:
            for s, v in j["songs"].items():
                if v and any((v.get("ranks") or {}).values()):
                    found[s] = (t, v)
        elif "song" in j:
            found[j["song"]] = (t, j)
    return {s: v for s, (t, v) in found.items()}


def cmd_score(a) -> None:
    ans = find_answers()
    if not ans:
        raise SystemExit(f"no answers found: save the page downloads as {RATINGS_DIR}/{SESSION}.answers.json (or per song)")
    key = json.loads((KEY_DIR / f"{SESSION}.key.json").read_text())
    pre = load_prereg()
    if pre["sha256"] != key["preregistration_sha256"]:
        raise SystemExit("the key's pre-registration hash does not match the registered one")
    sig_p = BASE / "sealed" / "signals.json"
    if hashlib.sha256(sig_p.read_bytes()).hexdigest() != key["sealed_signals_sha256"]:
        raise SystemExit("the sealed signals changed after the pages were built")
    (RATINGS_DIR / f"{SESSION}.answers.collected.json").write_text(json.dumps(ans, indent=1))
    res = analyse(key, ans)
    txt = fmt(res)
    print(txt)
    (RATINGS_DIR / f"{SESSION}.results.json").write_text(json.dumps(res, indent=1, default=float))
    (RATINGS_DIR / f"{SESSION}.results.txt").write_text(txt)
    if not a.no_ledger:
        env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
        rows = [("repeat_mean_abs_rank_diff", res["repeat"]["mean_abs_diff"], f"chance 2.67; songs {len(res['repeat']['by_song'])}")]
        for g, v in res["signals"].items():
            p = v["pooled"]["all7"]
            if p["mean_rho"] is not None:
                rows.append((f"rho_{g}", p["mean_rho"], f"{p['n_songs']} songs; p1 {p['p_one_sided']}; Holm {p.get('p_holm')}; human answers, not judged by the tool"))
        for metric, val, note in rows:
            if val is None:
                continue
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "ab", "--engine", "native",
                            "--dataset", SESSION, "--metric", metric, "--value", f"{val:.6g}", "--notes", note],
                           cwd=ROOT, env=env, check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["calibrate", "prereg", "render", "signals", "build", "hlx", "ledger", "score", "amend"])
    ap.add_argument("--songs", nargs="+", default=list(SONGS))
    ap.add_argument("--check-t229", action="store_true", help="render: one extra render reproducing T229 r35 on the first song")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="build: overwrite existing pages (only before anyone has listened)")
    a = ap.parse_args(argv)
    {"calibrate": cmd_calibrate, "prereg": cmd_prereg, "render": cmd_render, "signals": cmd_signals, "build": cmd_build,
     "hlx": cmd_hlx, "ledger": cmd_ledger, "score": cmd_score, "amend": cmd_amend}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
