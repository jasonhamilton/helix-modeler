#!/usr/bin/env python3
"""T232: pre-registered confirmation of the T231 blind reverb estimator on Mother and Territorial. LOCAL ONLY: song audio
stays under cache/ and reports/local/. Measurement only; nothing is adopted; this tool never judges audio and never
scores before the human has answered.

The estimator (src/tonematch/metrics/reverb_blind.py, `adc`) reads a decay time and a tail level from the FULL separated
stem of each song; the fixed T231 distance rule (|ln(RT_c / RT_s)| / 0.30 + |early_c - early_s| / 5 dB) is applied to the
29 existing items (the 27 T230 grid candidates and the T229 r35 / p35 audio, 10 s each), and the closest and the
furthest are NAMED in a registration written before any pack exists. A fresh blind pack per song (named closest, named
furthest, 4 others by a seeded design, 1 repeat) is ranked by the human; the scorer runs the registered analysis.
0 renders: every clip is audio already on disk.

    .venv/bin/python scripts/reverb_confirm_t232.py register     # estimates, sealed distances, registration (first)
    .venv/bin/python scripts/reverb_confirm_t232.py build        # blind pages, answer sheets, sealed key
    TONEMATCH_AGENT=tone-metrics .venv/bin/python scripts/reverb_confirm_t232.py ledger
    .venv/bin/python scripts/reverb_confirm_t232.py score        # ONLY after the human has answered
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import itertools
import json
import math
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
warnings.filterwarnings("ignore", category=RuntimeWarning)

from tonematch.metrics import reverb_blind as RB

SR = 48_000
SESSION = "reverb-confirm-v1"
BASE = ROOT / "cache" / "reverb-confirm-t232"
T230 = ROOT / "cache" / "reverb-pilot-t230"
T229 = ROOT / "cache" / "colour-t229"
T231 = ROOT / "cache" / "reverb-estimator-t231"
KEY_DIR = ROOT / "cache" / "ratings"
RATINGS_DIR = ROOT / "reports" / "local" / "ratings"
URL_BASE = "http://127.0.0.1:8000/local/ratings"
SONGS = ("mother", "territorial")
NAME = {"mother": "Mother", "territorial": "Territorial Pissings"}
TYPES = ("room", "plate", "hall")
MIXES = (0.25, 0.35, 0.50)
LEVELS = ("short", "medium", "long")
T229_ITEMS = {"t229-r35": "r35", "t229-p35": "p35"}
SCALE_LOG_RT = 0.30                 # T231, fixed before its post hoc comparison
SCALE_EARLY_DB = 5.0
DESIGN_SEED = 232
PERM_SEED = 232
PERM_DRAWS = 20000
LETTERS = "ABCDEFG"
TOP_K = 2                           # proposed reading: named closest in the top 2 of 7
# Filled in after `register` wrote the registration; `build` and `score` refuse to run if the registration differs.
REGISTERED_SHA256 = "de5c7f7a0a62db813a1f7ab0548344b3508c0456af6006377930258090fb6b6e"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _t231():
    spec = importlib.util.spec_from_file_location("t231", ROOT / "scripts" / "reverb_estimator_t231.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def items_of() -> dict[str, dict]:
    """The 29 items of a song: id -> {type, mix, level, path(song)}."""
    out = {}
    for t in TYPES:
        for m in MIXES:
            for lv in LEVELS:
                out[f"{t}-m{round(m * 100):02d}-{lv}"] = {"type": t, "mix": m, "level": lv, "source": "T230 grid"}
    out["t229-r35"] = {"type": "room", "mix": 0.35, "level": "t229", "source": "T229 r35 (Room Decay 0.40)"}
    out["t229-p35"] = {"type": "plate", "mix": 0.35, "level": "t229", "source": "T229 p35 (Plate Decay 0.40)"}
    return out


def item_path(song: str, k: str) -> Path:
    if k in T229_ITEMS:
        return T229 / song / "audio" / f"{T229_ITEMS[k]}.wav"
    return T230 / song / "audio" / f"{k}.wav"


def wav(p: Path) -> np.ndarray:
    x = sf.read(str(p), dtype="float32")[0]
    return x.mean(axis=1) if x.ndim > 1 else x


def _f(v) -> float:
    return float(v) if v is not None and math.isfinite(float(v)) else float("nan")


def distance(rc: float, ec: float, rs: float, es: float) -> float:
    """The T231 rule. NaN when either side has no reading."""
    if not all(math.isfinite(v) for v in (rc, ec, rs, es)) or rc <= 0 or rs <= 0:
        return float("nan")
    return abs(math.log(rc / rs)) / SCALE_LOG_RT + abs(ec - es) / SCALE_EARLY_DB


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------ register

def rho_null_quantile(n: int = 6, songs: int = 2, q: float = 0.95) -> float:
    """95th percentile of the mean Spearman rho over `songs` independent songs of `n` untied items (exact)."""
    base = np.arange(n, dtype=float)
    rh = np.array([np.corrcoef(base, np.array(p, float))[0, 1] for p in itertools.permutations(range(n))])
    vals, cnt = np.unique(np.round(rh, 10), return_counts=True)
    pr = cnt / cnt.sum()
    dist = {0.0: 1.0}
    for _ in range(songs):
        nd: dict[float, float] = {}
        for s0, p0 in dist.items():
            for v, p in zip(vals, pr, strict=True):
                k = round(s0 + v, 10)
                nd[k] = nd.get(k, 0.0) + p0 * p
        dist = nd
    ks = sorted(dist)
    acc = 0.0
    for k in ks[::-1]:
        acc += dist[k]
        if acc >= 1 - q:
            return k / songs
    return float("nan")


def t230_values(t230_key: dict, s: str) -> dict:
    """The human's T230 values (average ranks, repeat averaged) for the six T230 items of song s (already scored)."""
    spec = importlib.util.spec_from_file_location("t230", ROOT / "scripts" / "reverb_pilot_t230.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    ans = json.loads((RATINGS_DIR / "reverb-pilot-v1.answers.json").read_text())["songs"]
    des = t230_key["songs"][s]
    hv = m.human_values(des, ans.get(s, {}).get("ranks", {}))
    return {} if hv is None else dict(zip(des["candidates"], [float(v) for v in hv[0]], strict=True))


def cmd_register(_a) -> None:
    p_reg = KEY_DIR / f"{SESSION}.prereg.json"
    t231 = _t231()
    items = items_of()
    val = json.loads((T231 / "validate.json").read_text())
    t231_songs = json.loads((T231 / "songs.json").read_text())
    t230_key = json.loads((KEY_DIR / "reverb-pilot-v1.key.json").read_text())
    est: dict = {"session": SESSION, "computed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "scale_log_rt": SCALE_LOG_RT, "scale_early_db": SCALE_EARLY_DB, "songs": {}}
    for s in SONGS:
        h = t231.song_hash(s)
        stem_p = ROOT / "cache" / "stems" / h / "htdemucs_6s" / "guitar.wav"
        x = wav(stem_p)
        e = RB.estimate(x).to_dict()
        ref = {"path": str(stem_p.relative_to(ROOT)), "sha256": sha_file(stem_p), "duration_s": len(x) / SR,
               "adc_rt60_s": _f(e["adc_rt60_s"]), "adc_early_db": _f(e["adc_early_db"]), "adc_events": e["adc_events"],
               "ci16_84_t231": t231_songs[s]["stem_full"]["ci"]}
        old = t231_songs[s]["stem_full"]
        ref["t231_reading"] = {"adc_rt60_s": old["adc_rt60_s"], "adc_early_db": old["adc_early_db"]}
        rows = {}
        for k in items:
            p = item_path(s, k)
            if not p.exists():
                raise SystemExit(f"{s} {k}: missing {p} (this task renders nothing; stop and report)")
            y = wav(p)
            c = RB.estimate(y).to_dict()
            rc, ec = _f(c["adc_rt60_s"]), _f(c["adc_early_db"])
            ov = val[s][k]
            rows[k] = {**items[k], "path": str(p.relative_to(ROOT)), "sha256": sha_file(p), "adc_rt60_s": rc, "adc_early_db": ec,
                       "adc_events": c["adc_events"], "t231_reading": {"adc_rt60_s": ov["adc_rt60_s"], "adc_early_db": ov["adc_early_db"]},
                       "distance": distance(rc, ec, ref["adc_rt60_s"], ref["adc_early_db"])}
        dd = {k: v["distance"] for k, v in rows.items()}
        finite = {k: d for k, d in dd.items() if math.isfinite(d)}
        if not finite:
            raise SystemExit(f"{s}: no item has a reading")
        top = max(finite.values()) + 1.0
        rank_d = {k: (d if math.isfinite(d) else top) for k, d in dd.items()}
        order = sorted(rank_d, key=lambda k: (rank_d[k], k))
        dev = max(max(abs(rows[k]["adc_rt60_s"] - _f(rows[k]["t231_reading"]["adc_rt60_s"])),
                      abs(rows[k]["adc_early_db"] - _f(rows[k]["t231_reading"]["adc_early_db"]))) for k in rows if math.isfinite(rows[k]["distance"]))
        est["songs"][s] = {"stem": ref, "items": rows, "order_by_distance": order, "closest": order[0], "furthest": order[-1],
                           "unreadable": sorted(k for k, d in dd.items() if not math.isfinite(d)),
                           "max_abs_deviation_from_t231": dev,
                           "t230_pack": t230_key["songs"][s]["candidates"], "t230_values": t230_values(t230_key, s),
                           "t231_posthoc_rho_full": json.loads((T231 / "compare.json").read_text())["songs"][s]["distances"]["full"]["rho"]}
        log(f"{s}: stem {ref['adc_rt60_s']:.3f} s / {ref['adc_early_db']:.2f} dB; closest {order[0]} ({rank_d[order[0]]:.2f}), "
            f"furthest {order[-1]} ({rank_d[order[-1]]:.2f}); max deviation from the T231 readings {dev:.2e}")
    BASE.mkdir(parents=True, exist_ok=True)
    sealed = BASE / "sealed" / "estimates.json"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(est, indent=1, default=float)
    if sealed.exists() and sealed.read_text() != blob and p_reg.exists():
        raise SystemExit("the registration exists and the estimates would change; refusing to overwrite")
    sealed.write_text(blob)
    est_sha = sha_file(sealed)
    txt = prereg_text(est, est_sha)
    h = hashlib.sha256(txt.encode()).hexdigest()
    if p_reg.exists():
        old = json.loads(p_reg.read_text())
        if old["sha256"] != h:
            raise SystemExit(f"a different registration already exists ({old['sha256'][:12]}); refusing to overwrite")
        log(f"registration unchanged, sha256 {h}")
        return
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"session": SESSION, "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": txt, "sha256": h,
           "sealed_estimates_sha256": est_sha}
    p_reg.write_text(json.dumps(rec, indent=1))
    (BASE / "prereg.json").write_text(json.dumps(rec, indent=1))
    (BASE / "prereg.txt").write_text(txt + "\n")
    print(txt)
    log(f"registration written: {p_reg}  sha256 {h}")


def _desc(k: str, it: dict) -> str:
    if it["level"] == "t229":
        return f"{k} ({it['source']}, Mix {it['mix']:.2f})"
    return f"{k} ({it['type'].capitalize()}, Mix {it['mix']:.2f}, {it['level']} decay)"


def prereg_text(est: dict, est_sha: str) -> str:
    q95 = rho_null_quantile()
    L = [("Question: does the T231 blind reverb estimator (adc: abandonment-synchronous decay averaging, "
          "src/tonematch/metrics/reverb_blind.py, unchanged since T231), read on a song's full separated guitar stem, name "
          "in advance the reverb candidate the human hears as closest to the song's guitar, and order the candidates the way "
          "the human ranks them? Songs: Mother and Territorial Pissings, the two of seven whose stems show a readable tail "
          "in T231. Measurement only; no bar; nothing is admitted or adopted; the human rules on what the result means.")]
    for s in SONGS:
        e = est["songs"][s]
        st = e["stem"]
        items = e["items"]
        c, f = e["closest"], e["furthest"]
        heard = [k for k in (c, f) if k in e["t230_pack"]]
        L.append(
            f"{NAME[s]}: the estimator on the full stem ({st['path']}, {st['duration_s']:.0f} s, sha256 {st['sha256'][:12]}): "
            f"decay {st['adc_rt60_s']:.3f} s, early tail level {st['adc_early_db']:.2f} dB re the held level "
            f"({st['adc_events']} events; T231 bootstrap 16-84%: {st['ci16_84_t231']['adc_rt60_s'][0]:.2f} to "
            f"{st['ci16_84_t231']['adc_rt60_s'][1]:.2f} s, {st['ci16_84_t231']['adc_early_db'][0]:.1f} to "
            f"{st['ci16_84_t231']['adc_early_db'][1]:.1f} dB). Over the 29 items: NAMED CLOSEST = {_desc(c, items[c])}, "
            f"its reading {items[c]['adc_rt60_s']:.2f} s / {items[c]['adc_early_db']:.1f} dB, distance {items[c]['distance']:.2f}; "
            f"NAMED FURTHEST = {_desc(f, items[f])}, {items[f]['adc_rt60_s']:.2f} s / {items[f]['adc_early_db']:.1f} dB, "
            f"distance {items[f]['distance']:.2f}. Items without a reading: {', '.join(e['unreadable']) or 'none'}. "
            + (("Disclosed: the human heard " + "; ".join(
                f"{k} in the T230 pack for this song (its T230 value {e['t230_values'][k]:.1f}; the best T230 value on that page "
                f"was {min(e['t230_values'].values()):.1f}; values are average ranks over 7 clips, 1 = closest)" for k in heard)
                + ".") if heard else "Neither named item was in the human's T230 pack for this song.")
            + f" T231's post hoc check of this same rule and reference on the six T230 items of this song gave rho "
              f"{e['t231_posthoc_rho_full']:.2f} (after the fact, disclosed).")
    L += [
        ("Items: the 27 T230 grid candidates (Room / Plate / Hall x Mix 0.25 / 0.35 / 0.50 x decay short 1.05 / medium 1.48 / "
         "long 2.09 s, after the T229 chain c) and the T229 r35 and p35 audio, 10 s each, read by the same estimator. "
         "Distance rule (T231, fixed before its post hoc step): d = |ln(RT_candidate / RT_stem)| / 0.30 + "
         "|early_candidate - early_stem| / 5 dB; an item without a reading counts as further than every measured item "
         "(largest distance + 1); ties broken by item name. The 29 readings and distances per song are sealed in "
         f"cache/reverb-confirm-t232/sealed/estimates.json, sha256 {est_sha}."),
        (f"Pack per song: 7 clips = the named closest, the named furthest, 4 others, and one repeat. The 4 others are drawn "
         f"by numpy default_rng({DESIGN_SEED}), songs in the order mother, territorial, uniformly among the 4-subsets of the "
         f"song's remaining grid candidates (27 minus the named two when they are grid items; r35 and p35 are not drawn) "
         f"that contain all three types, all three Mix values and all three decay lengths. Then the same generator draws "
         f"which of the 6 is repeated and a random letter order A-G. This is a fresh draw: new seed, new letters, not the "
         f"T230 pack. Each clip is stereo, left = the song's guitar (the T229 10 s stem excerpt), right = the candidate; the "
         f"stem and the six candidates LUFS-matched together to -23 with one common peak guard (dry_ablation_t224.level_match_pack)."),
        ("Human answer: each of the 7 clips gets a rank 1 (closest to the song's guitar) to 7; equal ranks are allowed. Raw "
         "ranks are turned into average ranks over the 7 clips; an item's value is the mean over its clips (the repeated "
         "item has two). A song with any clip unranked is unanswered and reported; the reading below needs both songs."),
        ("Reported first, the listener's repeat consistency: per song the absolute difference of the repeated item's two raw "
         "ranks, against the chance value 8/3 = 2.67; a difference of 3 or more flags the song as not reproducible (it stays "
         "in the analysis)."),
        ("Primary 1, the named closest: per song its value, the median of the six item values, and the difference (value "
         "minus median; negative = ranked better than the middle). One-sided test that the named closest is ranked better "
         "than chance: the statistic is the sum over the two songs of (value minus median); p = share of 20000 draws, numpy "
         f"default_rng({PERM_SEED}), with the six values permuted within each song (one permutation per song per draw, shared "
         "with primary 2), whose statistic is <= the observed. Per song the exact one-sided p (share of the six positions "
         "with a value <= the named closest's). Secondary, the named furthest, the mirror: value minus median, one-sided "
         "for ranked worse than the middle, same draws."),
        ("Primary 2, the order: per song Spearman rho between the human's six values (1 = closest) and the six distances, "
         "average ranks for ties; positive = agreement. Pooled = mean of the per-song rhos over the songs where the human's "
         "values are not all equal. p one-sided (pooled rho > 0) and two-sided from the same within-song permutation draws. "
         f"For scale: with two songs of six untied items the one-sided 5% point of the pooled rho is {q95:.2f}."),
        ("Decided pairs: within each song the 15 pairs of the six items; a pair the human valued equal is a tie and is "
         "excluded from the decided count (reported); agreement = share of decided pairs where the item with the smaller "
         "distance has the better (smaller) human value. Descriptive (pairs are not independent)."),
        ("Proposed reading for the human, a proposal and not a bar: the estimator is 'confirmed' on these two songs if the "
         f"named closest's value is <= {TOP_K} (top 2 of 7) on BOTH songs AND the pooled rho is > 0 with one-sided p < 0.05. "
         "Chance of the first part alone if the human ranked at random: about (2/7)^2 = 0.08 for unrepeated items. "
         "Otherwise 'not confirmed'. The human decides what follows; no model, default or search starting value is adopted "
         "from this test."),
        ("Size and limits: two songs, six items each; one song alone cannot reach significance on rho; the named-closest "
         "test is the more direct one. The candidate readings come from 10 s renders while the stem reading is the full song, as in "
         "T231. The estimator cannot tell Room from Plate from Hall (T231); it reads decay length and tail level only."),
        ("Blind: the pages and trials.json carry no type, setting, distance or item id; letters are random per song; the "
         "key (cache/ratings/reverb-confirm-v1.key.json) holds the design, letters, gains and distances and is not opened "
         "before the human's answers exist. The named picks are public by design (they are this registration); which "
         "letter they are is not. The scorer refuses to run if this text, the sealed estimates or the key changed."),
    ]
    return "\n".join(f"{i + 1}. {t}" for i, t in enumerate(L))


def load_reg() -> dict:
    p = KEY_DIR / f"{SESSION}.prereg.json"
    if not p.exists():
        raise SystemExit("no registration: run `register` first")
    rec = json.loads(p.read_text())
    h = hashlib.sha256(rec["text"].encode()).hexdigest()
    if h != rec["sha256"]:
        raise SystemExit("the registration text does not match its recorded hash")
    if REGISTERED_SHA256 and h != REGISTERED_SHA256:
        raise SystemExit(f"the registration ({h[:12]}) is not the one registered in this script ({REGISTERED_SHA256[:12]})")
    if (BASE / "prereg.txt").exists() and (BASE / "prereg.txt").read_text() != rec["text"] + "\n":
        raise SystemExit("cache/reverb-confirm-t232/prereg.txt differs from the registration")
    sealed = BASE / "sealed" / "estimates.json"
    if sha_file(sealed) != rec["sealed_estimates_sha256"]:
        raise SystemExit("the sealed estimates changed after registration")
    return rec


# ------------------------------------------------------------------------------------------ design and build

def build_design(est: dict, seed: int = DESIGN_SEED) -> dict:
    rng = np.random.default_rng(seed)
    items = items_of()
    out = {}
    for s in SONGS:
        e = est["songs"][s]
        named = [e["closest"], e["furthest"]]
        pool = sorted(k for k, v in items.items() if v["level"] != "t229" and k not in named)
        ok = [q for q in itertools.combinations(pool, 4)
              if {items[k]["type"] for k in q} == set(TYPES) and {items[k]["mix"] for k in q} == set(MIXES)
              and {items[k]["level"] for k in q} == set(LEVELS)]
        others = list(ok[int(rng.integers(0, len(ok)))])
        six = named + others
        rep = six[int(rng.integers(0, 6))]
        clips = six + [rep]
        perm = rng.permutation(7)
        letters = {LETTERS[int(perm[i])]: clips[i] for i in range(7)}
        out[s] = {"named_closest": named[0], "named_furthest": named[1], "others": others, "n_admissible_designs": len(ok),
                  "candidates": six, "repeat": rep, "repeat_letters": sorted(L for L, k in letters.items() if k == rep),
                  "letters": dict(sorted(letters.items())),
                  "distances": {k: e["items"][k]["distance"] for k in six}}
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
<p class="hint">Registration sha256 <code>__SHA__</code>. <a href="../index.html">Back to both songs</a></p>
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
candidate</b>: our drive match and EQ with a reverb after it. All clips are loudness-matched. Rank the seven clips
<b>1 = closest to the song's guitar</b> to <b>7 = furthest</b>, judging the space and ambience (and the tone overall), not
the notes and not which you like best. Use the same number twice if you cannot tell two clips apart. One clip is repeated
on purpose under another letter; rank it as if new. Answers save in this browser as you go.</p>"""

INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>Reverb confirmation T232</title>
<style>body{font:15px system-ui;max-width:760px;margin:2em auto;padding:0 1em;color:#222}li{margin:.4em 0}
button{font:inherit;padding:.35em .8em}code{font-size:13px}</style></head><body>
<h1>Reverb confirmation (T232): two songs</h1>
<p>A short blind test, like the reverb pilot: two pages, seven clips each, left ear the song's guitar, right ear a
candidate. Rank every clip on both pages (1 = closest), then press <b>Download all answers</b> below. Leave the file in
Downloads (or move it to <code>reports/local/ratings/</code>) and run
<code>.venv/bin/python scripts/reverb_confirm_t232.py score</code>. Do not open anything under <code>cache/ratings/</code>
or <code>cache/reverb-confirm-t232/</code> before that.</p>
<ol>__LIST__</ol>
<p><button id="all">Download all answers</button> <span id="msg"></span></p>
<p style="color:#555;font-size:13px">The test was registered before these pages were built: sha256 <code>__SHA__</code>.
The registration names the candidate the measurement expects you to rank closest and the one it expects furthest; it
does not say which letter they are, and you do not need to read it.</p>
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

    rec = load_reg()
    if not REGISTERED_SHA256:
        raise SystemExit("put the registration hash in REGISTERED_SHA256 before building")
    keyf = KEY_DIR / f"{SESSION}.key.json"
    if keyf.exists() and not a.rebuild:
        raise SystemExit("the pages are already built (the key exists); pass --rebuild only if no one has listened yet")
    est = json.loads((BASE / "sealed" / "estimates.json").read_text())
    design = build_design(est)
    root = RATINGS_DIR / SESSION
    root.mkdir(parents=True, exist_ok=True)
    sha = rec["sha256"]
    pub = {"session": SESSION, "kind": "reverb-confirm", "songs": list(SONGS), "preregistration": rec["text"],
           "preregistration_sha256": sha, "sealed_estimates_sha256": rec["sealed_estimates_sha256"], "pages": {}}
    keyd = {"session": SESSION, "design_seed": DESIGN_SEED, "preregistration_sha256": sha,
            "sealed_estimates_sha256": rec["sealed_estimates_sha256"], "items": items_of(), "songs": {}}
    for s in SONGS:
        d = root / s
        d.mkdir(parents=True, exist_ok=True)
        stem = wav(T229 / s / "audio" / "stem.wav")
        des = design[s]
        aud = {}
        for k in des["candidates"]:
            p = item_path(s, k)
            if sha_file(p) != est["songs"][s]["items"][k]["sha256"]:
                raise SystemExit(f"{s} {k}: audio changed since registration")
            aud[k] = wav(p)
        n = min([len(stem)] + [len(v) for v in aud.values()])
        st2, m2, gains = DA.level_match_pack(stem[:n], {k: v[:n] for k, v in aud.items()})
        sf.write(str(d / "song.wav"), st2, SR, subtype="FLOAT")
        for L, k in des["letters"].items():
            sf.write(str(d / f"clip_{L}.wav"), np.stack([st2, m2[k]], 1), SR, subtype="FLOAT")
        letters = list(LETTERS)
        page = (PAGE.replace("__TITLE__", html.escape(f"Reverb confirmation: {NAME[s]}")).replace("__NOTE__", NOTE)
                .replace("__SHA__", sha).replace("__SESSION_JS__", json.dumps(SESSION)).replace("__SONG_JS__", json.dumps(s))
                .replace("__LETTERS__", json.dumps(letters)))
        (d / "index.html").write_text(page)
        sheet = [f"# Reverb confirmation, {NAME[s]}: answer sheet", "", f"Registration sha256 `{sha}`.", "",
                 ("Left ear = the song's guitar, right ear = a candidate. Rank 1 (closest) to 7 (furthest); ties allowed. "
                  "Do not open anything under cache/ratings/ before you have answered."), "", "| clip | rank |", "|---|---|"]
        sheet += [f"| {L} | |" for L in letters] + ["", "Notes:", ""]
        (d / "answer_sheet.md").write_text("\n".join(sheet))
        pub["pages"][s] = {"url": f"{URL_BASE}/{SESSION}/{s}/index.html", "letters": letters, "excerpt_s": n / SR}
        keyd["songs"][s] = {**des, "lufs_gains_db": gains, "excerpt_s": n / SR}
    lst = "".join(f'<li><a href="{s}/index.html">{html.escape(NAME[s])}</a> <span id="n-{s}"></span></li>' for s in SONGS)
    (root / "index.html").write_text(INDEX.replace("__LIST__", lst).replace("__SHA__", sha)
                                     .replace("__SESSION_JS__", json.dumps(SESSION)).replace("__SONGS__", json.dumps(list(SONGS))))
    (root / "trials.json").write_text(json.dumps(pub, indent=1))
    keyf.write_text(json.dumps(keyd, indent=1, default=float))
    (BASE / "key.sha256").write_text(sha_file(keyf) + "\n")
    log(f"pages {root} ({URL_BASE}/{SESSION}/index.html); key {keyf} (sealed, sha256 {sha_file(keyf)[:12]}); registration {sha[:12]}")


# ------------------------------------------------------------------------------------------ ledger

def cmd_ledger(_a) -> None:
    rec = load_reg()
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
    est = json.loads((BASE / "sealed" / "estimates.json").read_text())
    picks = "; ".join(f"{s}: closest {est['songs'][s]['closest']} furthest {est['songs'][s]['furthest']}" for s in SONGS)
    note = (f"T232 reverb confirmation: {len(SONGS)} blind ranking pages of 7 clips (named closest + named furthest + 4 seeded "
            f"+ 1 repeat) songs {' '.join(SONGS)}; registration sha256 {rec['sha256']}; sealed estimates sha256 "
            f"{rec['sealed_estimates_sha256']}; named {picks}; 0 renders; built for the human; nothing scored; nothing adopted")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "benchmark", "--engine", "-",
                    "--dataset", SESSION, "--metric", "trials_built", "--value", str(len(SONGS)), "--renders", "0",
                    "--notes", note.replace(",", ";")], cwd=ROOT, env=env, check=True)
    log(note)


# ------------------------------------------------------------------------------------------ analysis (registered)

def _avg_ranks(v) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(np.asarray(v, float), method="average")


def _rho(x, y) -> float:
    a, b = _avg_ranks(x), _avg_ranks(y)
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def human_values(des: dict, ranks: dict) -> tuple[np.ndarray, dict] | None:
    if not all(ranks.get(L) for L in LETTERS):
        return None
    v = dict(zip(LETTERS, _avg_ranks([float(ranks[L]) for L in LETTERS]), strict=True))
    by: dict[str, list[float]] = {}
    for L, k in des["letters"].items():
        by.setdefault(k, []).append(v[L])
    h = np.array([float(np.mean(by[k])) for k in des["candidates"]])
    r1, r2 = (float(ranks[L]) for L in des["repeat_letters"])
    return h, {"item": des["repeat"], "raw": [r1, r2], "abs_diff": abs(r1 - r2)}


def analyse(key: dict, answers: dict[str, dict], draws: int = PERM_DRAWS, seed: int = PERM_SEED) -> dict:
    """Exactly the registered analysis. `answers` = {song: {"ranks": {letter: 1..7}}}. Candidate order in the key:
    index 0 = named closest, index 1 = named furthest."""
    H, rep, unanswered = {}, {}, []
    for s in SONGS:
        r = human_values(key["songs"][s], ((answers.get(s) or {}).get("ranks")) or {})
        if r is None:
            unanswered.append(s)
            continue
        H[s], rep[s] = r
    out: dict = {"preregistration_sha256": key["preregistration_sha256"], "unanswered": unanswered}
    diffs = [rep[s]["abs_diff"] for s in rep]
    out["repeat"] = {"by_song": rep, "mean_abs_diff": float(np.mean(diffs)) if diffs else None, "chance_mean_abs_diff": 8 / 3,
                     "not_reproducible": [s for s in rep if rep[s]["abs_diff"] >= 3]}
    order = [s for s in SONGS if s in H]
    D = {s: np.array([float(key["songs"][s]["distances"][k]) for k in key["songs"][s]["candidates"]]) for s in order}
    rng = np.random.default_rng(seed)
    perms = [{s: rng.permutation(6) for s in order} for _ in range(draws)]
    # primary 1: named closest (index 0) and secondary: named furthest (index 1)
    named = {}
    for idx, nm, better in ((0, "closest", True), (1, "furthest", False)):
        per = {}
        for s in order:
            h = H[s]
            med = float(np.median(h))
            val = float(h[idx])
            p_exact = float(np.mean(h <= val + 1e-12)) if better else float(np.mean(h >= val - 1e-12))
            per[s] = {"item": key["songs"][s]["candidates"][idx], "value": val, "median": med, "diff": val - med,
                      "p_one_sided_exact": p_exact, "in_top2": bool(val <= TOP_K + 1e-12)}
        if order:
            obs = sum(per[s]["diff"] for s in order)
            null = np.array([sum(float(H[s][p[s]][idx]) - float(np.median(H[s])) for s in order) for p in perms])
            p = float((np.sum(null <= obs + 1e-12) + 1) / (draws + 1)) if better else float((np.sum(null >= obs - 1e-12) + 1) / (draws + 1))
        else:
            obs, p = None, None
        named[nm] = {"by_song": per, "sum_diff": obs, "p_one_sided": p}
    out["named"] = named
    # primary 2: Spearman
    per_rho = {s: _rho(H[s], D[s]) if np.std(H[s]) > 0 else float("nan") for s in order}
    live = [s for s in order if math.isfinite(per_rho[s])]
    if live:
        obs = float(np.mean([per_rho[s] for s in live]))
        null = np.array([np.mean([_rho(H[s][p[s]], D[s]) for s in live]) for p in perms])
        pooled = {"songs": live, "mean_rho": obs, "p_one_sided": float((np.sum(null >= obs - 1e-12) + 1) / (draws + 1)),
                  "p_two_sided": float((np.sum(np.abs(null) >= abs(obs) - 1e-12) + 1) / (draws + 1))}
    else:
        pooled = {"songs": [], "mean_rho": None, "p_one_sided": None, "p_two_sided": None}
    out["spearman"] = {"per_song": per_rho, "pooled": pooled}
    # decided pairs
    k = n = ties = 0
    for s in order:
        h, d = H[s], D[s]
        for i in range(6):
            for j in range(i + 1, 6):
                if h[i] == h[j]:
                    ties += 1
                    continue
                n += 1
                k += (h[i] < h[j]) == (d[i] < d[j])
    out["pairs"] = {"decided": n, "agree": k, "rate": k / n if n else None, "human_ties": ties}
    out["values"] = {s: dict(zip(key["songs"][s]["candidates"], H[s].tolist(), strict=True)) for s in order}
    both = len(order) == len(SONGS)
    out["proposed_reading_confirmed"] = bool(both and all(named["closest"]["by_song"][s]["in_top2"] for s in order)
                                             and pooled["mean_rho"] is not None and pooled["mean_rho"] > 0
                                             and pooled["p_one_sided"] < 0.05)
    return out


def fmt(res: dict) -> str:
    f = lambda v, p="{:.2f}": "-" if v is None or (isinstance(v, float) and math.isnan(v)) else p.format(v)
    r = res["repeat"]
    L = [f"Reverb confirmation T232; registration sha256 {res['preregistration_sha256'][:12]}; unanswered {res['unanswered']}",
         "", "The listener first (repeat consistency):"]
    for s, v in r["by_song"].items():
        L.append(f"  {s:<12} repeated item ranked {v['raw'][0]:g} and {v['raw'][1]:g} (difference {v['abs_diff']:g})")
    L.append(f"  mean difference {f(r['mean_abs_diff'])} (chance {r['chance_mean_abs_diff']:.2f}); not reproducible: {r['not_reproducible']}")
    for nm in ("closest", "furthest"):
        v = res["named"][nm]
        L += ["", f"Named {nm} ({'primary 1' if nm == 'closest' else 'secondary'}):"]
        for s, x in v["by_song"].items():
            L.append(f"  {s:<12} {x['item']:<18} value {x['value']:.2f} (median {x['median']:.2f}, diff {x['diff']:+.2f}); "
                     f"exact p {x['p_one_sided_exact']:.2f}; top 2 of 7: {x['in_top2']}")
        L.append(f"  pooled sum of diffs {f(v['sum_diff'])}, permutation p one-sided {f(v['p_one_sided'], '{:.4f}')}")
    sp = res["spearman"]
    L += ["", "Order (primary 2): per song rho " + ", ".join(f"{s} {f(x)}" for s, x in sp["per_song"].items()),
          (f"  pooled mean rho {f(sp['pooled']['mean_rho'])}, p one-sided {f(sp['pooled']['p_one_sided'], '{:.4f}')}, "
           f"two-sided {f(sp['pooled']['p_two_sided'], '{:.4f}')}"),
          f"  decided pairs {res['pairs']['agree']}/{res['pairs']['decided']} agree (human ties {res['pairs']['human_ties']})",
          "", f"Proposed reading 'confirmed' (a proposal, not a bar): {res['proposed_reading_confirmed']}"]
    for s, vals in res["values"].items():
        L.append(f"  {s}: " + ", ".join(f"{k} {v:.1f}" for k, v in sorted(vals.items(), key=lambda kv: kv[1])))
    return "\n".join(L)


def find_answers(downloads: Path | None = None) -> dict[str, dict]:
    dl = Path.home() / "Downloads" if downloads is None else downloads
    found: dict[str, tuple[float, dict]] = {}
    paths = []
    for d in (RATINGS_DIR, dl):
        if d.exists():
            paths += list(d.glob(f"{SESSION}.answers*.json")) + list(d.glob(f"{SESSION}.*.answers*.json"))
    for p in sorted(set(paths), key=lambda q: q.stat().st_mtime):
        if p.name.endswith(".collected.json"):
            continue
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
    rec = load_reg()
    if not REGISTERED_SHA256:
        raise SystemExit("no registration hash in the script")
    keyf = KEY_DIR / f"{SESSION}.key.json"
    if (BASE / "key.sha256").read_text().strip() != sha_file(keyf):
        raise SystemExit("the sealed key changed after the pages were built")
    ans = find_answers()
    if not ans:
        raise SystemExit(f"no answers found: save the download as {RATINGS_DIR}/{SESSION}.answers.json (or leave it in Downloads)")
    key = json.loads(keyf.read_text())
    if key["preregistration_sha256"] != rec["sha256"]:
        raise SystemExit("the key's registration hash does not match the registration")
    (RATINGS_DIR / f"{SESSION}.answers.collected.json").write_text(json.dumps(ans, indent=1))
    res = analyse(key, ans)
    txt = fmt(res)
    print(txt)
    (RATINGS_DIR / f"{SESSION}.results.json").write_text(json.dumps(res, indent=1, default=float))
    (RATINGS_DIR / f"{SESSION}.results.txt").write_text(txt + "\n")
    if a.no_ledger:
        return
    env = dict(os.environ, TONEMATCH_AGENT=os.environ.get("TONEMATCH_AGENT", "tone-metrics"))
    rows = [("repeat_mean_abs_rank_diff", res["repeat"]["mean_abs_diff"], "chance 2.67")]
    for s, x in res["named"]["closest"]["by_song"].items():
        rows.append((f"named_closest_value_{s}", x["value"], f"{x['item']}; median {x['median']:.2f}; exact p {x['p_one_sided_exact']:.2f}"))
    rows.append(("named_closest_p", res["named"]["closest"]["p_one_sided"], "pooled one-sided permutation p"))
    rows.append(("pooled_rho", res["spearman"]["pooled"]["mean_rho"], f"p1 {res['spearman']['pooled']['p_one_sided']}"))
    rows.append(("proposed_reading_confirmed", float(res["proposed_reading_confirmed"]), "a proposal for the human; nothing adopted"))
    for metric, val, note in rows:
        if val is None:
            continue
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "ab", "--engine", "-",
                        "--dataset", SESSION, "--metric", metric, "--value", f"{val:.6g}",
                        "--notes", f"{note}; registration {rec['sha256'][:12]}; human answers; not judged by the tool".replace(",", ";")],
                       cwd=ROOT, env=env, check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["register", "build", "ledger", "score"])
    ap.add_argument("--rebuild", action="store_true", help="build: overwrite existing pages (only before anyone has listened)")
    ap.add_argument("--no-ledger", action="store_true", help="score: do not append ledger rows")
    a = ap.parse_args(argv)
    {"register": cmd_register, "build": cmd_build, "ledger": cmd_ledger, "score": cmd_score}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
