"""Blind validation of AFx-Rep distance against separated stems (T223, audit recommendation 2).

Question: when five candidate drive models are rendered on the SAME rebuilt dry track and compared with the song's
separated guitar stem, does the AFx-Rep distance order them the way the human's ear does?

Per song (5 songs of the T211 `<song>-dryrebuild-prs7` runs): the top-5 candidates of `result.json
shortlist.ranked`, each at its T211 coarse-grid settings (`shortlist.rows`), rendered on the T211 search dry
(`listen/di_search.wav`, identical to the rebuilt `playlevel-prs7` dry at the run window), against the target =
`listen/target_stem.wav` (the separated guitar, 10 s window). Trials:

  rank   Target + the 5 candidates in random letters A-E; the human gives each candidate a rank 1 (closest) to 5.
         Equal ranks are allowed (ties). 5 songs + 3 repeats (new random letters).
  pair   Target + A + B; "A closer / B closer / too close to call". Per song: the 4 adjacent AFx ranks (1v2, 2v3, 3v4,
         4v5) and the extreme pair (1v5) = 25 trials; plus 6 repeats (sides swapped) and 2 sanity trials.

The analysis is pre-registered below (PREREG): its text and hash are written to trials.json and the key before any
trial is shown. This module builds the pack, plays trials on the pedal outputs and implements the analysis; the
analysis is run by the human's instruction only, after the answers exist. Nothing here judges audio.

  python scripts/rating_rank.py build [--name afx-stem-validation-v1]
  python scripts/rating_rank.py play  afx-stem-validation-v1 --trial N [--only TABCDE]
  python scripts/rating_rank.py score afx-stem-validation-v1            (after the human has answered)
"""
from __future__ import annotations

import argparse
import hashlib
import html
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
NAME = "afx-stem-validation-v1"
RUNS = ROOT / "cache" / "matches"
SONGS_ALL = ["mother", "cherub-rock", "ds-sample", "satisfaction", "seek-destroy", "teen-spirit", "territorial"]
EXCLUDED_UP_FRONT = "ds-sample"          # its target is a Helix render, not a separated real-song stem
DROP_SEED = 223223                       # draws the one more song dropped from the remaining six
N_CAND = 5
N_REPEAT_RANK = 3
N_REPEAT_PAIR = 6
NOISE_DB = -60.0
SEED_ANALYSIS = 223
PERM_DRAWS = 20000
LETTERS = "ABCDE"

PREREG_LINES = [
    ("Question: does the AFx-Rep distance (embed_afxrep, as ranked in each T211 result.json shortlist.ranked) order the "
     "top-5 candidates of a song the way the human hears them against that song's separated guitar stem? "
     "All five candidates are rendered on the same rebuilt dry track. Measurement only; no bar, nothing is admitted."),
    ("Songs: the T211 dryrebuild-prs7 runs minus ds-sample (its target is a Helix render, not a separated stem), minus one "
     "more song drawn by numpy default_rng(223223) from the remaining six, in the order mother, cherub-rock, satisfaction, "
     "seek-destroy, teen-spirit, territorial. The draw was made before any rendering or listening."),
    ("Candidates: the first 5 entries of shortlist.ranked (distance ascending), each at the values of the matching "
     "shortlist.rows entry (the T211 coarse-grid setting)."),
    ("Primary 1 (ranking): per song, human rank (1 = closest; equal ranks get the average rank; a repeated ranking of the "
     "same song is averaged) against the AFx distance; Spearman rho with average ranks for ties. Pooled statistic = the mean "
     "of the per-song rhos (equal weight). A song whose human ranks are all equal is undecided and left out (reported). "
     "p-value: one-sided permutation test (20000 draws, seed 223) permuting the human ranks within each song; "
     "two-sided also shown. Also reported: how many songs have rho > 0, and how often the human's closest candidate is "
     "the AFx top-1 (chance 1 in 5)."),
    ("Primary 2 (pairs): over the 25 regular pair trials (4 adjacent AFx ranks + the 1v5 pair per song), the share of "
     "decided trials in which the human chose the candidate with the lower AFx distance; exact two-sided binomial "
     "test against 0.5 and Clopper-Pearson 95% interval. Adjacent pairs and the 1v5 pairs are also reported separately. "
     "Repeat trials are not added to this count."),
    ("Test-retest (the listener, reported first): (a) pairs: the 6 repeats (sides swapped); consistent = the same candidate "
     "chosen both times, or too-close-to-call both times; also the rate among trials decided both times. (b) rankings: "
     "Spearman between the first and second ranking of each of the 3 repeated songs, and their mean."),
    ("Tie rule: 'too close to call' is excluded from every decided count and always reported with its count. A pair "
     "trial where the human called a tie once and chose a side once counts as inconsistent in (a). AFx distances are never "
     "treated as ties. Differences the listener cannot reproduce are noise: an agreement rate is read against the "
     "listener's own consistency rate (the ceiling), and a song whose repeated ranking has rho below 0.5 is flagged as "
     "not reproducible."),
    ("Size: 25 regular pair trials give a decided count of about 20; that detects an agreement rate of about 0.8 or more at "
     "80% power, so anything weaker is directional only. The audit asks for 30+ decided trials for a firm conclusion; this "
     "session is below that and says so. Per-song rankings (5 songs) are directional by construction."),
    ("Sanity trials: (1) the exact target copy against one candidate; the copy is expected to be chosen. (2) the copy "
     "against the copy plus -60 dB noise; expected 'too close to call'. A failed sanity trial is reported and marks the "
     "session unreliable; it does not change the analysis above."),
    ("Blind: the human is not told which AFx rank any candidate has, the letters and sides are random per trial, all clips "
     "of a song are LUFS-matched to -23 together, and the key is kept outside the page folder."),
]
PREREG = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(PREREG_LINES))
PREREG_SHA256 = hashlib.sha256(PREREG.encode()).hexdigest()


# ---------------------------------------------------------------- songs and candidates

def choose_songs(seed: int = DROP_SEED) -> list[str]:
    pool = [s for s in SONGS_ALL if s != EXCLUDED_UP_FRONT]
    drop = pool[int(np.random.default_rng(seed).integers(0, len(pool)))]
    return [s for s in pool if s != drop]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_song(song: str, runs: Path = RUNS, n: int = N_CAND) -> dict:
    run = runs / f"{song}-dryrebuild-prs7"
    r = json.loads((run / "result.json").read_text())
    sl = r["shortlist"]
    rows = {x["model"]: x for x in sl["rows"]}
    cands = []
    for rk, x in enumerate(sl["ranked"][:n]):
        if x["model"] not in rows:
            raise RuntimeError(f"{song}: ranked model {x['model']} has no shortlist row")
        cands.append({"afx_rank": rk + 1, "model": x["model"], "distance": float(x["distance"]),
                      "values": {k: float(v) for k, v in rows[x["model"]]["values"].items()}})
    listen = run / "listen"
    dry, sr = sf.read(str(listen / "di_search.wav"), dtype="float32", always_2d=True)
    stem, sr2 = sf.read(str(listen / "target_stem.wav"), dtype="float32", always_2d=True)
    if sr != SR or sr2 != SR:
        raise ValueError(f"{song}: expected {SR} Hz")
    return {"song": song, "run": str(run.relative_to(ROOT)) if run.is_relative_to(ROOT) else str(run),
            "window": r["window"], "cands": cands, "dry": dry.mean(axis=1).astype(np.float32),
            "stem": stem.mean(axis=1).astype(np.float32), "listen": listen,
            "hashes": {"di_search": sha256_file(listen / "di_search.wav"),
                       "target_stem": sha256_file(listen / "target_stem.wav"),
                       "result_json": sha256_file(run / "result.json")}}


def native_renderer(log=print):
    """(render(model, values, dry) -> audio, close). One Native host. No novelty exclusion: the T211 search rendered
    these models, and the pack must show exactly what it ranked."""
    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    os.environ["TONEMATCH_NATIVE_AUTOCAPTURE"] = "0"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    from tonematch.catalog import load_catalog
    from tonematch.engines import native_library as NL
    from tonematch.engines.native import NativeEngine

    cat = load_catalog()
    eng = NativeEngine(templates_dir=NL.library_templates())

    def render(model, values, dry):
        p = NL.block_preset(model, cat)
        p.slots[0].params.update(values)
        eng.load(p)
        return np.asarray(eng.render(np.asarray(dry, np.float32)), np.float32)

    return render, eng.close


def check_against_listen(song: dict, rendered: dict[int, np.ndarray]) -> dict:
    """The T211 run kept level-matched match.wav / match_rank2/3.wav for the top 3 FINAL candidates. When the ranked and
    final candidate coincide, the new render must be the same audio (any gain): returns the correlation per file."""
    out = {}
    for fn, rk in (("match.wav", 1), ("match_rank2.wav", 2), ("match_rank3.wav", 3)):
        p = song["listen"] / fn
        if not p.exists() or rk not in rendered:
            continue
        m, _ = sf.read(str(p), dtype="float32", always_2d=True)
        m = m.mean(axis=1)
        y = rendered[rk]
        k = min(len(m), len(y))
        c = float(np.corrcoef(m[:k], y[:k])[0, 1]) if np.std(y[:k]) > 0 and np.std(m[:k]) > 0 else float("nan")
        out[fn] = c
    return out


# ---------------------------------------------------------------- plan

def _cname(c: dict) -> str:
    kv = ",".join(f"{k}={v:g}" for k, v in sorted(c["values"].items()))
    return f"{c['model'].replace('HD2_Dist', '').replace('HD2_', '')}({kv})"


def pair_specs(n: int = N_CAND) -> list[tuple[int, int, str]]:
    """AFx-rank pairs per song: adjacent ones, then the extreme."""
    return [(i, i + 1, "adjacent") for i in range(1, n)] + [(1, n, "extreme")]


def build_plan(songs: list[str], rng, n_repeat_rank: int = N_REPEAT_RANK, n_repeat_pair: int = N_REPEAT_PAIR) -> list[dict]:
    plan: list[dict] = []
    for s in songs:
        plan.append({"type": "rank", "kind": "regular", "song": s, "perm": [int(v) for v in rng.permutation(N_CAND)]})
    for s in songs:
        for lo, hi, cls in pair_specs():
            a, b = (lo, hi) if rng.random() < 0.5 else (hi, lo)
            plan.append({"type": "pair", "kind": "regular", "song": s, "A": a, "B": b, "pclass": cls, "pair": [lo, hi]})
    reg_pairs = [i for i, p in enumerate(plan) if p["type"] == "pair"]
    reg_rank = [i for i, p in enumerate(plan) if p["type"] == "rank"]
    for i in sorted(int(v) for v in rng.permutation(len(reg_rank))[:n_repeat_rank]):
        o = plan[reg_rank[i]]
        while True:
            perm = [int(v) for v in rng.permutation(N_CAND)]
            if perm != o["perm"]:
                break
        plan.append({"type": "rank", "kind": "repeat", "song": o["song"], "perm": perm, "orig": reg_rank[i]})
    # repeats of pairs: spread over songs (at most 2 per song)
    chosen: list[int] = []
    per_song: dict[str, int] = {}
    for i in (int(v) for v in rng.permutation(len(reg_pairs))):
        j = reg_pairs[i]
        if per_song.get(plan[j]["song"], 0) >= 2:
            continue
        chosen.append(j)
        per_song[plan[j]["song"]] = per_song.get(plan[j]["song"], 0) + 1
        if len(chosen) == n_repeat_pair:
            break
    for j in chosen:
        o = plan[j]
        plan.append({"type": "pair", "kind": "repeat", "song": o["song"], "A": o["B"], "B": o["A"],
                     "pclass": o["pclass"], "pair": o["pair"], "orig": j})
    s_pair = [songs[int(v)] for v in rng.permutation(len(songs))[:2]]
    far = int(rng.integers(N_CAND - 1, N_CAND + 1))          # a low-ranked candidate (4 or 5) as the foil
    first_a = rng.random() < 0.5
    plan.append({"type": "pair", "kind": "sanity_target", "song": s_pair[0], "A": "copy" if first_a else far,
                 "B": far if first_a else "copy", "pclass": "sanity", "pair": None})
    near_a = rng.random() < 0.5
    plan.append({"type": "pair", "kind": "sanity_near", "song": s_pair[1], "A": "copy" if near_a else "noise",
                 "B": "noise" if near_a else "copy", "pclass": "sanity", "pair": None})
    return plan


def order_plan(plan: list[dict], rng, min_gap: int = 6, tries: int = 2000) -> list[int]:
    """Presentation order: repeats at least `min_gap` places after their originals, no two neighbours on the same song.
    Random greedy construction with restarts."""
    n = len(plan)
    for _ in range(tries):
        left = set(range(n))
        order: list[int] = []
        pos: dict[int, int] = {}
        while left:
            ok = [i for i in left
                  if (not order or plan[order[-1]]["song"] != plan[i]["song"])
                  and (plan[i]["kind"] != "repeat" or (plan[i]["orig"] in pos and len(order) - pos[plan[i]["orig"]] >= min_gap))]
            if not ok:
                break
            i = sorted(ok)[int(rng.integers(0, len(ok)))]
            pos[i] = len(order)
            order.append(i)
            left.discard(i)
        if not left:
            return order
    raise RuntimeError("no admissible presentation order found")


# ---------------------------------------------------------------- page

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:15px system-ui;max-width:800px;margin:2em auto;padding:0 1em;color:#222}
.t{border:1px solid #bbb;border-radius:6px;padding:.8em 1em;margin:1em 0}.t.done{border-color:#4a8}
.t h3{margin:.1em 0 .4em}.row{margin:.35em 0}.row b{display:inline-block;width:5.5em}
audio{width:calc(100% - 13em);vertical-align:middle}
button,select{margin:.15em;padding:.35em .8em;font:inherit}button.on{background:#222;color:#fff}
textarea{width:100%}#bar{position:sticky;top:0;background:#fff;padding:.4em 0;border-bottom:1px solid #ccc}
#out{width:100%;height:6em}.hint{color:#666;font-size:13px}</style></head><body>
<div id="bar"><b>__TITLE__</b> &nbsp; answered <span id="cnt">0</span> of <span id="tot"></span>
 &nbsp; <button id="dl">Download answers (JSON)</button> <span id="msg"></span></div>
<p>__NOTE__</p>
<div id="trials"></div>
<h2>Finish</h2><p><button id="cp">Copy answers as text</button> <span class="hint">(if the download is blocked)</span></p>
<textarea id="out" readonly></textarea>
<script>
const SESSION=__SESSION_JS__, TRIALS=__TRIALS__, KEY="rank-"+SESSION;
let ans=JSON.parse(localStorage.getItem(KEY)||"{}");
const save=()=>localStorage.setItem(KEY,JSON.stringify(ans));
const slot=id=>ans[id]||(ans[id]={});
const doneT=t=>{const s=ans[t.id]||{};return t.type==="rank"?t.letters.every(l=>(s.ranks||{})[l]):!!s.choice;};
document.getElementById("tot").textContent=TRIALS.length;
function count(){document.getElementById("cnt").textContent=TRIALS.filter(doneT).length;}
function pauseOthers(e){document.querySelectorAll("audio").forEach(a=>{if(a!==e.target)a.pause();});}
function render(){const root=document.getElementById("trials");root.innerHTML="";
 TRIALS.forEach(t=>{const d=document.createElement("div");const s=slot(t.id);
  d.className="t"+(doneT(t)?" done":"");
  let h="<h3>Trial "+t.n+" of "+TRIALS.length+(t.type==="rank"?" &nbsp;(rank all five)":" &nbsp;(A or B)")+"</h3>";
  h+="<div class='row'><b>Target</b><audio controls preload='auto' src='"+t.files.T+"'></audio></div>";
  d.innerHTML=h;
  if(t.type==="rank"){
   t.letters.forEach(l=>{const r=document.createElement("div");r.className="row";
    r.innerHTML="<b>"+l+"</b><audio controls preload='auto' src='"+t.files[l]+"'></audio> rank ";
    const sel=document.createElement("select");
    sel.innerHTML="<option value=''>-</option>"+[1,2,3,4,5].map(i=>"<option>"+i+"</option>").join("");
    sel.value=(s.ranks||{})[l]??"";
    sel.onchange=()=>{s.ranks=s.ranks||{};s.ranks[l]=sel.value?+sel.value:null;save();render();};
    r.appendChild(sel);d.appendChild(r);});
   const hint=document.createElement("div");hint.className="hint";
   hint.textContent="1 = closest to the Target, 5 = furthest. Give two clips the same number if you cannot tell them apart.";
   d.appendChild(hint);
  }else{
   for(const l of ["A","B"]){const r=document.createElement("div");r.className="row";
    r.innerHTML="<b>"+l+"</b><audio controls preload='auto' src='"+t.files[l]+"'></audio>";d.appendChild(r);}
   const p=document.createElement("div");p.className="row";p.append("Which is closer to the Target? ");
   [["A","A is closer"],["B","B is closer"],["tie","Too close to call"]].forEach(([v,lab])=>{
    const b=document.createElement("button");b.textContent=lab;b.className=s.choice===v?"on":"";
    b.onclick=()=>{s.choice=s.choice===v?null:v;save();render();};p.appendChild(b);});
   d.appendChild(p);
  }
  d.querySelectorAll("audio").forEach(a=>a.addEventListener("play",pauseOthers));
  const n=document.createElement("textarea");n.rows=1;n.placeholder="notes (optional)";n.value=s.notes||"";
  n.oninput=()=>{s.notes=n.value;save();};d.appendChild(n);root.appendChild(d);});count();}
function payload(){return JSON.stringify({session:SESSION,saved_at:new Date().toISOString(),
 answers:TRIALS.map(t=>({trial:t.id,n:t.n,type:t.type,choice:(ans[t.id]||{}).choice??null,
 ranks:(ans[t.id]||{}).ranks??null,notes:(ans[t.id]||{}).notes||""}))},null,1);}
document.getElementById("dl").onclick=()=>{const u=URL.createObjectURL(new Blob([payload()],{type:"application/json"}));
 const a=document.createElement("a");a.href=u;a.download=SESSION+".answers.json";a.click();
 document.getElementById("msg").textContent="downloaded (check your Downloads folder)";};
document.getElementById("cp").onclick=()=>{const s=payload();const o=document.getElementById("out");o.value=s;o.select();
 (navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(()=>document.getElementById("msg").textContent="copied",()=>{});};
render();
</script></body></html>"""

NOTE = ("Every trial: the Target is the song's separated guitar (about 10 seconds); the other clips are candidate "
        "Helix drive blocks playing a rebuilt version of the same part. Judge only how close each candidate's tone is to the "
        "Target (the distortion and the overall sound), not the notes and not which you like. All clips of a song are "
        "loudness-matched. Some trials repeat on purpose; answer each as if new. Answers save in this browser as you go. "
        "When done, press Download answers and save the file as reports/local/ratings/__SESSION__.answers.json.")


def render_page(name: str, trials: list[dict]) -> str:
    return (PAGE.replace("__NOTE__", html.escape(NOTE.replace("__SESSION__", name)))
            .replace("__TITLE__", html.escape(f"Rating session {name}"))
            .replace("__SESSION_JS__", json.dumps(name)).replace("__TRIALS__", json.dumps(trials)))


# ---------------------------------------------------------------- build

def build_session(name: str = NAME, seed: int | None = None, render=None, songs: list[str] | None = None,
                  runs: Path = RUNS, out_root: Path = RS.RATINGS_DIR, key_root: Path = RS.KEY_DIR, log=print) -> tuple[Path, Path]:
    seed = secrets.randbelow(2**31) if seed is None else seed
    rng = np.random.default_rng(seed)
    songs = songs or choose_songs()
    data = {s: load_song(s, runs) for s in songs}
    close = None
    if render is None:
        from tonematch.engines.host_budget import LoadGuard

        guard = LoadGuard()
        waited = guard.wait(log)
        if waited:
            log(f"waited {waited:.0f} s for the load guard")
        render, close = native_renderer(log)
    n_renders = 0
    try:
        for s in songs:
            d = data[s]
            rend: dict[int, np.ndarray] = {}
            for c in d["cands"]:
                y = np.asarray(render(c["model"], c["values"], d["dry"]), np.float32).reshape(-1)
                n_renders += 1
                if not np.all(np.isfinite(y)) or RS.rms_db(y) < -80:
                    raise RuntimeError(f"render failed or silent: {s} {c['model']}")
                rend[c["afx_rank"]] = y
            d["rend"] = rend
            d["check"] = check_against_listen(d, rend)
            log(f"{s}: {len(rend)} renders; match-file correlation {d['check']}")
    finally:
        if close is not None:
            close()

    # one loudness-matched clip set per song (target + 5 candidates), same gain in every trial of the song
    clips: dict[str, dict] = {}
    for s in songs:
        d = data[s]
        n = min(len(d["stem"]), *(len(v) for v in d["rend"].values()))
        raw = [RS.cut(d["stem"], 0, n)] + [RS.cut(d["rend"][r], 0, n) for r in range(1, N_CAND + 1)]
        lev, info = RS.match_set(raw)
        nz = rng.standard_normal(n).astype(np.float32)
        t0 = raw[0]
        noisy = t0 + nz * (float(np.sqrt(np.mean(t0 ** 2))) * 10 ** (NOISE_DB / 20) / float(np.std(nz)))
        noisy = (noisy * 10 ** (info["gain_db"][0] / 20)).astype(np.float32)
        clips[s] = {"T": lev[0], **{r: lev[r] for r in range(1, N_CAND + 1)}, "noise": noisy, "match": info, "n": n}

    plan = build_plan(songs, rng)
    order = order_plan(plan, rng)
    folder, keyf = out_root / name, key_root / f"{name}.key.json"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    key_root.mkdir(parents=True, exist_ok=True)

    ids = {}
    for pos, si in enumerate(order):
        ids[si] = f"t{secrets.token_hex(4)}"
    trials, ktrials = [], []
    for pos, si in enumerate(order):
        p = plan[si]
        n_ = pos + 1
        s = p["song"]
        cl = clips[s]
        tag = ids[si].lstrip("t")
        files = {"T": f"{tag}_t.wav"}
        RS.write_clip(folder / files["T"], cl["T"])
        kt = {"n": n_, "id": ids[si], "type": p["type"], "kind": p["kind"], "song": s,
              "repeat_of": ids[p["orig"]] if p["kind"] == "repeat" else None, "files": files}
        if p["type"] == "rank":
            letters = list(LETTERS)
            kt["letters"] = {}
            for letter, r in zip(letters, p["perm"]):
                r1 = r + 1
                files[letter] = f"{tag}_{letter.lower()}.wav"
                RS.write_clip(folder / files[letter], cl[r1])
                c = data[s]["cands"][r1 - 1]
                kt["letters"][letter] = {"afx_rank": r1, "distance": c["distance"], "model": c["model"],
                                         "values": c["values"], "name": _cname(c)}
            trials.append({"n": n_, "id": ids[si], "type": "rank", "letters": letters, "files": files})
        else:
            kt["pclass"] = p["pclass"]
            sides = {}
            for side in "AB":
                ref = p[side]
                if ref == "copy":
                    x, info = cl["T"], {"role": "target_copy"}
                elif ref == "noise":
                    x, info = cl["noise"], {"role": "near_identical", "noise_db": NOISE_DB}
                else:
                    c = data[s]["cands"][ref - 1]
                    x = cl[ref]
                    info = {"role": "candidate", "afx_rank": ref, "distance": c["distance"], "model": c["model"],
                            "values": c["values"], "name": _cname(c)}
                files[side] = f"{tag}_{side.lower()}.wav"
                RS.write_clip(folder / files[side], x)
                sides[side] = info
            kt["sides"] = sides
            kt["expected"] = ("A" if sides["A"]["role"] == "target_copy" else "B") if p["kind"] == "sanity_target" else (
                "tie" if p["kind"] == "sanity_near" else None)
            trials.append({"n": n_, "id": ids[si], "type": "pair", "files": files})
        ktrials.append(kt)
    (folder / "trials.json").write_text(json.dumps(
        {"session": name, "kind": "afx-stem-validation", "excerpt_s": clips[songs[0]]["n"] / SR, "trials": trials,
         "preregistration": PREREG, "preregistration_sha256": PREREG_SHA256}, indent=1))
    (folder / "index.html").write_text(render_page(name, trials))
    song_info = {s: {"run": data[s]["run"], "window": data[s]["window"], "hashes": data[s]["hashes"],
                     "dry_wav": f"data/songs/{s}-rebuilt/playlevel-prs7/rebuilt_dry_playlevel.wav",
                     "candidates": data[s]["cands"], "render_vs_listen_corr": data[s]["check"],
                     "match": clips[s]["match"]} for s in songs}
    keyf.write_text(json.dumps({"session": name, "version": "rank-1", "seed": seed, "songs": songs,
                                "song_draw_seed": DROP_SEED, "n_renders": n_renders, "target_lufs": RS.TARGET_LUFS,
                                "preregistration": PREREG, "preregistration_sha256": PREREG_SHA256,
                                "song_info": song_info, "trials": ktrials}, indent=1, default=float))
    return folder, keyf


# ---------------------------------------------------------------- play

def play_trial(session: str, trial_n: int, gain_db: float = 0.0, which: str = "TAB", gap_s: float = 1.0,
               out_root: Path = RS.RATINGS_DIR, sd=None, device: int | None = None, say=print) -> list[str]:
    """Play clips of one trial (T, then A-E or A-B) on host outputs 1/2 of the HX Stomp. Audio playback only."""
    folder = out_root / session
    man = json.loads((folder / "trials.json").read_text())
    tr = next((t for t in man["trials"] if t["n"] == trial_n), None)
    if tr is None:
        raise SystemExit(f"session {session} has no trial {trial_n} (1..{len(man['trials'])})")
    if which == "TAB" and tr["type"] == "rank":
        which = "T" + LETTERS
    bad = [k for k in which if k not in tr["files"]]
    if bad:
        raise SystemExit(f"trial {trial_n} has no clip(s) {''.join(bad)}; it has {''.join(tr['files'])}")
    if sd is None:
        import sounddevice as sd
    if device is None:
        from tonematch.reamp import find_device
        device = find_device()
    g = 10 ** (gain_db / 20)
    played = []
    for k in which:
        x = RS.read_mono(folder / tr["files"][k]) * g
        if float(np.max(np.abs(x))) > 1.0:
            raise SystemExit(f"gain {gain_db:+.1f} dB would clip clip {k}; lower --gain-db")
        say("Target ..." if k == "T" else f"{k} ...")
        sd.play(np.stack([x, x], axis=1).astype(np.float32), SR, device=device, mapping=[1, 2])
        RS._wait(sd, len(x) / SR)
        played.append(k)
        if k != which[-1]:
            import time
            time.sleep(gap_s)
    return played


# ---------------------------------------------------------------- analysis (pre-registered; run after the answers exist)

def _avg_ranks(v) -> np.ndarray:
    from scipy.stats import rankdata
    return rankdata(np.asarray(v, float), method="average")


def _rho(x, y) -> float:
    a, b = _avg_ranks(x), _avg_ranks(y)
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def analyse(key: dict, answers: dict, draws: int = PERM_DRAWS, seed: int = SEED_ANALYSIS) -> dict:
    """The analysis exactly as pre-registered in PREREG."""
    byid = {a["trial"]: a for a in answers["answers"]}
    songs = key["songs"]
    out: dict = {"prereg_sha256": key["preregistration_sha256"], "songs": {}}

    def rank_vec(t):
        a = byid.get(t["id"], {})
        rk = a.get("ranks") or {}
        if not all(rk.get(l) for l in LETTERS):
            return None
        order = sorted(t["letters"], key=lambda l: t["letters"][l]["afx_rank"])           # by AFx rank
        return [float(rk[l]) for l in order]

    # --- test-retest first
    rr = {}
    for s in songs:
        ts = [t for t in key["trials"] if t["type"] == "rank" and t["song"] == s]
        vs = [rank_vec(t) for t in ts]
        if len(ts) >= 2 and all(v is not None for v in vs[:2]):
            rr[s] = _rho(vs[0], vs[1])
    pair_rep = [t for t in key["trials"] if t["type"] == "pair" and t["kind"] == "repeat"]

    def cand_choice(t):
        a = byid.get(t["id"], {})
        c = a.get("choice")
        if c in ("A", "B"):
            return t["sides"][c]["afx_rank"]
        return "tie" if c == "tie" else None

    cons = n_ans = dec_both = dec_cons = 0
    for t in pair_rep:
        o = next(x for x in key["trials"] if x["id"] == t["repeat_of"])
        c1, c2 = cand_choice(o), cand_choice(t)
        if c1 is None or c2 is None:
            continue
        n_ans += 1
        cons += c1 == c2
        if c1 != "tie" and c2 != "tie":
            dec_both += 1
            dec_cons += c1 == c2
    out["retest"] = {"pairs": {"n": n_ans, "consistent": cons, "rate": cons / n_ans if n_ans else None,
                               "decided_both": dec_both, "decided_consistent": dec_cons,
                               "decided_rate": dec_cons / dec_both if dec_both else None},
                     "rank_rho_by_song": rr,
                     "rank_rho_mean": float(np.nanmean(list(rr.values()))) if rr and not all(math.isnan(v) for v in rr.values()) else None,
                     "not_reproducible": [s for s, v in rr.items() if not (v >= 0.5)]}

    # --- primary 1: ranking
    rng = np.random.default_rng(seed)
    per_song, human_vecs, dists = {}, {}, {}
    for s in songs:
        ts = [t for t in key["trials"] if t["type"] == "rank" and t["song"] == s]
        vs = [v for v in (rank_vec(t) for t in ts) if v is not None]
        if not vs:
            continue
        d = [next(l["distance"] for l in ts[0]["letters"].values() if l["afx_rank"] == r) for r in range(1, N_CAND + 1)]
        h = np.mean([_avg_ranks(v) for v in vs], axis=0)
        human_vecs[s], dists[s] = h, d
        per_song[s] = {"rho": _rho(h, d), "n_rankings": len(vs), "human_mean_rank_by_afx_rank": [float(x) for x in h],
                       "human_closest_is_afx_top1": bool(np.argmin(h) == 0 and np.sum(h == h.min()) == 1)}
    live = [s for s in per_song if not math.isnan(per_song[s]["rho"])]
    obs = float(np.mean([per_song[s]["rho"] for s in live])) if live else float("nan")
    perm = []
    for _ in range(draws if live else 0):
        perm.append(np.mean([_rho(rng.permutation(human_vecs[s]), dists[s]) for s in live]))
    perm = np.asarray(perm)
    out["ranking"] = {"per_song": per_song, "undecided_songs": [s for s in per_song if s not in live],
                      "pooled_mean_rho": obs, "songs_rho_positive": int(sum(per_song[s]["rho"] > 0 for s in live)),
                      "n_songs": len(live),
                      "p_one_sided": float((np.sum(perm >= obs - 1e-12) + 1) / (len(perm) + 1)) if len(perm) else None,
                      "p_two_sided": float((np.sum(np.abs(perm) >= abs(obs) - 1e-12) + 1) / (len(perm) + 1)) if len(perm) else None,
                      "afx_top1_is_human_closest": int(sum(per_song[s]["human_closest_is_afx_top1"] for s in live)),
                      "chance_top1": 0.2}

    # --- primary 2: pairs
    def tally(ts):
        k = n = tie = none = 0
        for t in ts:
            a = byid.get(t["id"], {})
            c = a.get("choice")
            if c in ("A", "B"):
                n += 1
                other = "B" if c == "A" else "A"
                k += t["sides"][c]["distance"] < t["sides"][other]["distance"]
            elif c == "tie":
                tie += 1
            else:
                none += 1
        lo, hi = RS.clopper_pearson(k, n)
        return {"decided": n, "agree": k, "rate": k / n if n else None, "ci": [lo, hi],
                "p_two_sided": RS.binom_two_sided(k, n) if n else None, "ties": tie, "unanswered": none}

    regs = [t for t in key["trials"] if t["type"] == "pair" and t["kind"] == "regular"]
    out["pairs"] = {"all": tally(regs), "adjacent": tally([t for t in regs if t["pclass"] == "adjacent"]),
                    "extreme": tally([t for t in regs if t["pclass"] == "extreme"]),
                    "by_song": {s: tally([t for t in regs if t["song"] == s]) for s in songs}}
    san = {}
    for t in key["trials"]:
        if t["kind"] in ("sanity_target", "sanity_near"):
            c = byid.get(t["id"], {}).get("choice")
            san[t["kind"]] = {"answer": c, "expected": t["expected"], "ok": c == t["expected"]}
    out["sanity"] = san
    out["session_reliable"] = bool(san) and all(v["ok"] for v in san.values())
    return out


def fmt(res: dict) -> str:
    f = lambda v, p="{:.2f}": "-" if v is None or (isinstance(v, float) and math.isnan(v)) else p.format(v)
    r, rk, pr = res["retest"], res["ranking"], res["pairs"]
    L = [f"AFx-Rep against separated stems (T223); pre-registration sha256 {res['prereg_sha256'][:12]}",
         "", "The listener first (test-retest):",
         (f"  pair repeats: {r['pairs']['consistent']}/{r['pairs']['n']} consistent (ties count); "
          f"{r['pairs']['decided_consistent']}/{r['pairs']['decided_both']} among those decided both times"),
         (f"  repeated rankings, Spearman between first and second: {r['rank_rho_by_song']}; mean {f(r['rank_rho_mean'])}; "
          f"not reproducible (rho < 0.5): {r['not_reproducible']}"),
         "", "Primary 1, human rank vs AFx distance per song:"]
    for s, v in rk["per_song"].items():
        L.append(f"  {s:<14} rho {f(v['rho'])}  (rankings {v['n_rankings']}; human mean rank by AFx rank 1..5: "
                 f"{[round(x, 1) for x in v['human_mean_rank_by_afx_rank']]})")
    L.append(f"  pooled mean rho {f(rk['pooled_mean_rho'])} over {rk['n_songs']} songs, {rk['songs_rho_positive']} positive; "
             f"permutation p one-sided {f(rk['p_one_sided'], '{:.4f}')}, two-sided {f(rk['p_two_sided'], '{:.4f}')}; "
             f"AFx top-1 was the human's closest in {rk['afx_top1_is_human_closest']}/{rk['n_songs']} (chance 0.2); "
             f"undecided songs {rk['undecided_songs']}")
    L.append("\nPrimary 2, pair choices that match the lower AFx distance:")
    for g in ("all", "adjacent", "extreme"):
        v = pr[g]
        L.append(f"  {g:<9} {v['agree']}/{v['decided']} = {f(v['rate'], '{:.0%}')} (95% {f(v['ci'][0])}-{f(v['ci'][1])}), "
                 f"p {f(v['p_two_sided'], '{:.3f}')}; too close to call {v['ties']}, unanswered {v['unanswered']}")
    L.append(f"\nSanity: {res['sanity']}; session reliable: {res['session_reliable']}")
    return "\n".join(L)


# ---------------------------------------------------------------- cli

def ledger(dataset: str, metric: str, value: float, notes: str, kind: str = "benchmark") -> None:
    import subprocess
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "benchmark")}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", kind, "--dataset", dataset,
                    "--metric", metric, "--value", f"{value:.6g}", "--notes", notes], check=True, env=env, cwd=ROOT)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--name", default=NAME)
    b.add_argument("--seed", type=int, default=None)
    b.add_argument("--no-ledger", action="store_true")
    pl = sub.add_parser("play")
    pl.add_argument("session")
    pl.add_argument("--trial", type=int, required=True)
    pl.add_argument("--gain-db", type=float, default=0.0)
    pl.add_argument("--only", default="TAB", help="clips in order, e.g. TAB, or TABCDE for a ranking trial (default: all)")
    pl.add_argument("--gap-s", type=float, default=1.0)
    sc = sub.add_parser("score")
    sc.add_argument("session")
    sc.add_argument("--answers", type=Path)
    sc.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        os.nice(10)
        folder, keyf = build_session(a.name, seed=a.seed)
        kj = json.loads(keyf.read_text())
        n = len(kj["trials"])
        print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder}); {n} trials, {kj['n_renders']} renders\n"
              f"hidden key: {keyf}  (do not open before scoring)")
        if not a.no_ledger:
            ledger(f"ab-{a.name}", "trials_built", float(n),
                   f"T223 AFx-Rep vs separated stems: {n} trials ({sum(t['type'] == 'rank' for t in kj['trials'])} ranking, "
                   f"{sum(t['type'] == 'pair' for t in kj['trials'])} pair incl. repeats and sanity), songs {kj['songs']}, "
                   f"seed {kj['seed']}, {kj['n_renders']} renders on one Native host; preregistration sha256 "
                   f"{kj['preregistration_sha256']}; built for the human's ratings; nothing scored")
        return 0
    if a.cmd == "play":
        play_trial(a.session, a.trial, a.gain_db, a.only.upper(), a.gap_s)
        return 0
    ans = a.answers or RS.find_answers(a.session)
    if ans is None:
        raise SystemExit(f"no answers file found: save the page's download as {RS.RATINGS_DIR / (a.session + '.answers.json')}")
    key = json.loads((RS.KEY_DIR / f"{a.session}.key.json").read_text())
    res = analyse(key, json.loads(Path(ans).read_text()))
    txt = fmt(res)
    print(txt)
    (RS.RATINGS_DIR / f"{a.session}.results.json").write_text(json.dumps(res, indent=1, default=float))
    (RS.RATINGS_DIR / f"{a.session}.results.txt").write_text(txt)
    if not a.no_ledger:
        ds = f"ab-{a.session}"
        rk, pr = res["ranking"], res["pairs"]["all"]
        ledger(ds, "rank_pooled_rho", rk["pooled_mean_rho"], f"human answers, not scored by the tool; {rk['n_songs']} songs, p1={rk['p_one_sided']}", "ab")
        if pr["decided"]:
            ledger(ds, "pair_afx_agreement", pr["rate"], f"{pr['agree']}/{pr['decided']} decided, ties {pr['ties']}, p={pr['p_two_sided']}", "ab")
        if res["retest"]["pairs"]["n"]:
            ledger(ds, "pair_retest_rate", res["retest"]["pairs"]["rate"], f"{res['retest']['pairs']['consistent']}/{res['retest']['pairs']['n']}", "ab")
    return 0


if __name__ == "__main__":
    sys.exit(main())
