"""Human rating session (T170): which of two candidates sounds closer to a target guitar tone?

Used through scripts/ab_session.py (rating-build, play, rating-score). Nothing here judges audio: the
human's answers are the data, and every existing score is only MEASURED against them.

  build   trials from clips already on disk (cache/matches/*/listen and listen_t170): target T plus
          candidates A and B, one shared excerpt, all three loudness-matched, A/B side randomised.
          Page folder (served, opaque names only):  reports/local/ratings/<session>/
          Hidden key and DI excerpts (NOT served):  cache/ratings/<session>.key.json
  play    T, A, B of one trial through the HX Stomp USB outputs 1/2 (audio only, no control channel).
  score   join answers with the key: sanity and retest consistency, then for every existing score the
          share of decided trials where it prefers the human's choice (exact binomial CI) and Spearman
          against the optional 1-5 ratings. A measurement, not an admission of anything.

Song audio never leaves the machine: reports/local and cache are gitignored.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import secrets
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.separation.audio import integrated_loudness

SR = 48000
EXCERPT_S = 7.0
TARGET_LUFS = -23.0
PEAK_CEIL = 0.98
FADE_S = 0.02
RATINGS_DIR = ROOT / "reports" / "local" / "ratings"
KEY_DIR = ROOT / "cache" / "ratings"
MATCHES = ROOT / "cache" / "matches"
URL_BASE = "http://127.0.0.1:8000/local/ratings"

# regular trials per target family, plus 2 repeats (sides swapped) and 3 sanity trials
DEFAULT_PLAN = {"mother-drive": 11, "mother-amp": 4, "ds-sample": 6}
N_REPEATS = 2
LEVEL_TIE_WEIGHT = 1e-7          # match_block's mrstft_v2 + level_db weighting

SCORE_ORDER = [
    "mrstft_v2", "mrstft_v2+level_db", "level_db", "provisional_kit", "ltas_tilt", "crest_flatness_hnr",
    "envelope", "embed_afxrep", "nl_harmonic_max", "nl_harmonic_mean", "nl_band_rms", "nl_inharmonic",
    "ltas_eqfit", "texmod", "mmd_cep", "presence_nodi",
]


# ---------------------------------------------------------------- audio helpers

def rms_db(x: np.ndarray) -> float:
    p = float(np.mean(np.asarray(x, np.float64) ** 2))
    return 10 * math.log10(p) if p > 0 else -200.0


def read_mono(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SR:
        raise ValueError(f"{path}: expected {SR} Hz, got {sr}")
    return x.mean(axis=1).astype(np.float32)


def write_clip(path: Path, x: np.ndarray) -> None:
    sf.write(str(path), np.asarray(x, np.float32).reshape(-1), SR, subtype="PCM_16")


def cut(x: np.ndarray, start: int, n: int, fade: bool = True) -> np.ndarray:
    c = np.array(x[start:start + n], np.float32)
    f = min(int(FADE_S * SR), len(c) // 2)
    if fade and f:
        ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)
        c[:f] *= ramp
        c[-f:] *= ramp[::-1]
    return c


def lufs(x: np.ndarray) -> float:
    return integrated_loudness(np.asarray(x, np.float32).reshape(1, -1))


def match_set(clips: list[np.ndarray], target_lufs: float = TARGET_LUFS) -> tuple[list[np.ndarray], dict]:
    """Scale every clip to `target_lufs`. If any would exceed the peak ceiling, ALL are lowered by the
    same extra amount, so the set stays loudness-matched."""
    l_in = [lufs(c) for c in clips]
    g = [target_lufs - l if l > -69.9 else 0.0 for l in l_in]
    worst = max(float(np.max(np.abs(c))) * 10 ** (gi / 20) for c, gi in zip(clips, g))
    extra = 20 * math.log10(PEAK_CEIL / worst) if worst > PEAK_CEIL else 0.0
    g = [gi + extra for gi in g]
    out = [(c * 10 ** (gi / 20)).astype(np.float32) for c, gi in zip(clips, g)]
    return out, {"lufs_in": l_in, "gain_db": g, "lufs_out": [lufs(o) for o in out]}


# ---------------------------------------------------------------- candidates on disk

def discover(matches: Path = MATCHES) -> dict[str, dict]:
    """Families of {target, di, cands:[{name, path, orig_db, kind}]} from listen/ dirs under `matches`.
    `orig_db` is the clip's level before the listening pack levelled it (None when unrecoverable)."""
    fams: dict[str, dict] = {}

    def add(fam, target, di, name, path, orig_db, kind):
        f = fams.setdefault(fam, {"target": target, "di": di, "target_db": None, "cands": []})
        if f["target"] == target:
            f["cands"].append({"name": name, "path": path, "orig_db": orig_db, "kind": kind})

    def pack(d: Path, fam: str, names: list[str], prefix: str = "", kind: str = "amp-cab"):
        tgt, di = d / "target_stem.wav", d / "di_dry.wav"
        if not tgt.exists():
            return
        gains = {}
        if (d / "gains.json").exists():
            gains = json.loads((d / "gains.json").read_text()).get("gains_db", {})
        for nm in names:
            p = d / f"{nm}.wav"
            if not p.exists():
                continue
            x = read_mono(p)
            og = rms_db(x) - gains[nm] if nm in gains else None
            add(fam, tgt, di if di.exists() else None, f"{prefix}{nm}", p, og, kind)

    amp = ["match", "match_rank2", "match_rank3", "start_unmatched"]
    pack(matches / "Mother-0-68s" / "listen", "mother-amp", amp)
    drive = matches / "Mother-0-68s-drive" / "listen"
    pack(drive, "mother-drive", ["seq14_match", "seq12h_match"], kind="drive")
    mother_di = matches / "Mother-0-68s" / "listen" / "di_dry.wav"
    if "mother-drive" in fams and mother_di.exists():
        fams["mother-drive"]["di"] = mother_di
    raw = matches / "Mother-0-68s-drive" / "listen_t170"
    if raw.exists() and "mother-drive" in fams:
        for p in sorted(raw.glob("*.wav")):
            fams["mother-drive"]["cands"].append(
                {"name": p.stem, "path": p, "orig_db": rms_db(read_mono(p)), "kind": "drive"})
    base = matches / "DS-Sample" / "listen"
    pack(base, "ds-sample", amp)
    for d in sorted(matches.glob("DS-Sample_run*/listen")):
        pack(d, "ds-sample", amp, prefix=d.parent.name.replace("DS-Sample_", "") + "/")
    for f in fams.values():           # drop exact duplicates (same samples), keep the first
        seen, keep = set(), []
        for c in f["cands"]:
            h = hashlib.blake2b(read_mono(c["path"]).tobytes(), digest_size=12).hexdigest()
            if h not in seen:
                seen.add(h)
                keep.append(c)
        f["cands"] = keep
        f["target_db"] = rms_db(read_mono(f["target"]))
    return fams


def prelim_scores(target: np.ndarray, cand: np.ndarray) -> dict[str, float]:
    """Cheap scores used only to spread the pairs over the score range (full clips, no mask)."""
    from tonematch.metrics import LossKit
    from tonematch.metrics.kit import PROVISIONAL_WEIGHTS

    kit = LossKit()
    m = kit.score(target, cand, weights={"mrstft_v2": 1.0}).total
    p = kit.score(target, cand, weights=dict(PROVISIONAL_WEIGHTS)).total
    return {"mrstft_v2": float(m), "kit": float(p)}


def select_pairs(cands: list[dict], scores: list[dict], n: int, rng: np.random.Generator,
                 disagree_frac: float = 0.4, cap: int = 4, tries: int = 20000) -> list[tuple[int, int]]:
    """n distinct pairs of candidate indices covering the score range: at least `disagree_frac` of them are
    pairs where mrstft_v2 and the provisional kit rank the two candidates oppositely (so the existing scores
    make different predictions), no candidate used more than `cap` times, nearly all candidates used."""
    k = len(cands)
    allp = [(i, j) for i in range(k) for j in range(i + 1, k)]
    n = min(n, len(allp))

    def disagrees(i, j):
        return (scores[i]["mrstft_v2"] - scores[j]["mrstft_v2"]) * (scores[i]["kit"] - scores[j]["kit"]) < 0

    dis = {p: disagrees(*p) for p in allp}
    need = min(math.ceil(disagree_frac * n), sum(dis.values()))
    best, best_pen = None, 1e9
    for _ in range(tries):
        pick = [allp[i] for i in rng.choice(len(allp), size=n, replace=False)]
        use = np.zeros(k, int)
        for i, j in pick:
            use[i] += 1
            use[j] += 1
        pen = max(0, need - sum(dis[p] for p in pick)) * 3 + max(0, int(use.max()) - cap) * 3 \
            + max(0, min(k, n * 2) - 1 - int((use > 0).sum()))
        if pen < best_pen:
            best, best_pen = pick, pen
        if pen == 0:
            break
    return sorted(best)


# ---------------------------------------------------------------- build

def _audio_of(role, T: np.ndarray, f: dict, rng) -> tuple[np.ndarray, dict]:
    """Audio and key entry for one side of a trial: the target itself, the target plus -50 dB noise, or a candidate."""
    if role == "target":
        return T.copy(), {"name": "target", "role": "target_copy", "source": str(f["target"]), "orig_db": f["target_db"]}
    if role == "target+noise":
        noise = rng.standard_normal(len(T)).astype(np.float32)
        x = T + noise * (float(np.sqrt(np.mean(T ** 2))) * 10 ** (-50 / 20) / float(np.std(noise)))
        return x, {"name": "target+noise(-50 dB)", "role": "near_identical", "source": str(f["target"]),
                   "orig_db": f["target_db"]}
    return read_mono(role["path"]), {"name": role["name"], "role": "candidate", "source": str(role["path"]),
                                     "orig_db": role["orig_db"]}


def build_session(name: str, seed: int | None = None, plan: dict | None = None, matches: Path = MATCHES,
                  out_root: Path = RATINGS_DIR, key_root: Path = KEY_DIR, excerpt_s: float = EXCERPT_S,
                  repeats: int = N_REPEATS, fams: dict | None = None, prelim=prelim_scores) -> tuple[Path, Path]:
    seed = secrets.randbelow(2**31) if seed is None else seed
    rng = np.random.default_rng(seed)
    plan = DEFAULT_PLAN if plan is None else plan
    fams = discover(matches) if fams is None else fams
    folder, keyf = out_root / name, key_root / f"{name}.key.json"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    key_root.mkdir(parents=True, exist_ok=True)
    didir = key_root / f"{name}.di"
    if didir.exists():
        shutil.rmtree(didir)
    didir.mkdir()

    tgt_audio = {f: read_mono(v["target"]) for f, v in fams.items()}
    specs: list[dict] = []          # one per trial, before audio is written
    worst_kit: dict[str, tuple[float, dict]] = {}
    for fam, n in plan.items():
        if fam not in fams:
            continue
        f = fams[fam]
        sc = []
        for c in f["cands"]:
            sc.append(prelim(tgt_audio[fam], read_mono(c["path"])))
        w = int(np.argmax([s["kit"] for s in sc]))
        worst_kit[fam] = (sc[w]["kit"], f["cands"][w])
        for i, j in select_pairs(f["cands"], sc, n, rng):
            specs.append({"kind": "regular", "family": fam, "x": f["cands"][i], "y": f["cands"][j]})
    # sanity trials: A or B is the target itself (a clear loser, then a near-identical render)
    first = next(iter(plan))
    fam0 = first if first in fams else next(iter(fams))
    di0 = fams[fam0].get("di")
    if di0 is not None:
        specs.append({"kind": "sanity_target", "family": fam0, "x": "target",
                      "y": {"name": "clean DI", "path": di0, "orig_db": None, "kind": "clean"}, "expected": "target"})
    loser_fam = "mother-drive" if "mother-drive" in worst_kit else next(iter(worst_kit))
    specs.append({"kind": "sanity_target", "family": loser_fam, "x": "target", "y": worst_kit[loser_fam][1],
                  "expected": "target"})
    ds = "ds-sample" if "ds-sample" in fams else fam0
    specs.append({"kind": "sanity_near", "family": ds, "x": "target", "y": "target+noise", "expected": None})
    # repeats: the same two candidates again, sides swapped, different excerpt (retest)
    regs = [i for i, s in enumerate(specs) if s["kind"] == "regular"]
    for k in rng.permutation(len(regs))[:repeats]:
        o = regs[int(k)]
        r = specs[o]
        specs.append({"kind": "repeat", "family": r["family"], "x": r["y"], "y": r["x"], "orig": o})
    # presentation order: random, repeats after their originals
    order = [int(v) for v in rng.permutation(len(specs))]
    pos = {s: i for i, s in enumerate(order)}
    for s_i, s in enumerate(specs):
        if s["kind"] == "repeat":
            o = s["orig"]
            if pos[s_i] < pos[o]:
                pos[s_i], pos[o] = pos[o], pos[s_i]
    order = sorted(range(len(specs)), key=lambda i: pos[i])

    trials, ktrials = [], []
    n_len = int(excerpt_s * SR)
    used_start: dict[int, int] = {}
    id_of: dict[int, str] = {}
    for rank, si in enumerate(order, start=1):
        s = specs[si]
        fam, f = s["family"], fams[s["family"]]
        T = tgt_audio[fam]
        total = len(T)
        n = min(n_len, total)
        if s["kind"] == "repeat":
            avoid = used_start.get(s["orig"], -10**9)
            start = avoid
            for _ in range(20):
                start = int(rng.integers(0, max(1, total - n + 1)))
                if abs(start - avoid) > SR:
                    break
        else:
            start = int(rng.integers(0, max(1, total - n + 1)))
        used_start[si] = start

        xa, xi = _audio_of(s["x"], T, f, rng)
        ya, yi = _audio_of(s["y"], T, f, rng)
        x_is_a = bool(rng.integers(0, 2))
        a, b = (xa, ya) if x_is_a else (ya, xa)
        ai, bi = (xi, yi) if x_is_a else (yi, xi)
        t_c = cut(T, start, n)
        a_c, b_c = cut(a, start, n), cut(b, start, n)
        (t_m, a_m, b_m), info = match_set([t_c, a_c, b_c])
        tag = secrets.token_hex(4)
        files = {"T": f"{tag}_t.wav", "A": f"{tag}_a.wav", "B": f"{tag}_b.wav"}
        for k, v in (("T", t_m), ("A", a_m), ("B", b_m)):
            write_clip(folder / files[k], v)
        di_file = None
        if f.get("di") is not None:
            di_file = didir / f"{tag}_di.wav"
            sf.write(str(di_file), cut(read_mono(f["di"]), start, n, fade=False), SR, subtype="FLOAT")
        expected = None
        if s["kind"] == "sanity_target":
            expected = "A" if ai["role"] == "target_copy" else "B"
        trials.append({"n": rank, "id": f"t{tag}", "files": files})
        ktrials.append({"n": rank, "id": f"t{tag}", "kind": s["kind"], "family": fam, "excerpt_start_s": start / SR,
                        "excerpt_s": n / SR, "A": ai, "B": bi, "files": files, "target_orig_db": f["target_db"],
                        "di": str(di_file) if di_file else None, "expected": expected,
                        "repeat_of": id_of[s["orig"]] if s["kind"] == "repeat" else None, "loudness": info})
        id_of[si] = f"t{tag}"
    (folder / "trials.json").write_text(json.dumps({"session": name, "excerpt_s": excerpt_s, "trials": trials}, indent=1))
    (folder / "index.html").write_text(render_page(name, trials))
    keyf.write_text(json.dumps({"session": name, "seed": seed, "plan": plan, "trials": ktrials}, indent=1))
    return folder, keyf


# ---------------------------------------------------------------- page

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:15px system-ui;max-width:780px;margin:2em auto;padding:0 1em;color:#222}
.t{border:1px solid #bbb;border-radius:6px;padding:.8em 1em;margin:1em 0}.t.done{border-color:#4a8}
.t h3{margin:.1em 0 .4em}.row{margin:.35em 0}.row b{display:inline-block;width:5.5em}
audio{width:calc(100% - 6em);vertical-align:middle}
button,select{margin:.15em;padding:.35em .8em;font:inherit}button.on{background:#222;color:#fff}
textarea{width:100%}#bar{position:sticky;top:0;background:#fff;padding:.4em 0;border-bottom:1px solid #ccc}
#out{width:100%;height:6em}.hint{color:#666;font-size:13px}</style></head><body>
<div id="bar"><b>__TITLE__</b> &nbsp; answered <span id="cnt">0</span> of <span id="tot"></span>
 &nbsp; <button id="dl">Download answers (JSON)</button> <span id="msg"></span></div>
<p>__NOTE__</p>
<p>For each trial, listen to the <b>Target</b>, then to <b>A</b> and <b>B</b> (as often as you like; the
loudness of all three is matched). Decide which of A and B is <b>closer to the Target</b> in the tone of the guitar
(the distortion and the overall sound), not which you like more. Ratings are optional:
1 = not close at all, 5 = indistinguishable from the Target. Answers save in this browser as you go.
When done, press <b>Download answers</b> and save the file as
<code>reports/local/ratings/__SESSION__.answers.json</code> in the project.</p>
<div id="trials"></div>
<h2>Finish</h2><p><button id="cp">Copy answers as text</button> <span class="hint">(if the download is blocked)</span></p>
<textarea id="out" readonly></textarea>
<script>
const SESSION=__SESSION_JS__, TRIALS=__TRIALS__, KEY="rating-"+SESSION;
let ans=JSON.parse(localStorage.getItem(KEY)||"{}");
const save=()=>localStorage.setItem(KEY,JSON.stringify(ans));
const slot=id=>ans[id]||(ans[id]={});
document.getElementById("tot").textContent=TRIALS.length;
function count(){document.getElementById("cnt").textContent=TRIALS.filter(t=>(ans[t.id]||{}).choice).length;}
function pauseOthers(e){document.querySelectorAll("audio").forEach(a=>{if(a!==e.target)a.pause();});}
function render(){const root=document.getElementById("trials");root.innerHTML="";
 TRIALS.forEach(t=>{const d=document.createElement("div");const s=slot(t.id);
  d.className="t"+(s.choice?" done":"");
  let h="<h3>Trial "+t.n+" of "+TRIALS.length+"</h3>";
  for(const [lab,k] of [["Target","T"],["A","A"],["B","B"]])
   h+="<div class='row'><b>"+lab+"</b><audio controls preload='auto' src='"+t.files[k]+"'></audio></div>";
  d.innerHTML=h;d.querySelectorAll("audio").forEach(a=>a.addEventListener("play",pauseOthers));
  const p=document.createElement("div");p.className="row";
  p.append("Which is closer to the Target? ");
  [["A","A is closer"],["B","B is closer"],["tie","Too close to call"]].forEach(([v,l])=>{
   const b=document.createElement("button");b.textContent=l;b.className=s.choice===v?"on":"";
   b.onclick=()=>{s.choice=s.choice===v?null:v;save();render();};p.appendChild(b);});
  d.appendChild(p);
  const r=document.createElement("div");r.className="row";r.append("Closeness (optional, 1-5):  A ");
  for(const f of ["rating_a","rating_b"]){
   const sel=document.createElement("select");
   sel.innerHTML="<option value=''>-</option>"+[1,2,3,4,5].map(i=>"<option>"+i+"</option>").join("");
   sel.value=s[f]??"";sel.onchange=()=>{s[f]=sel.value?+sel.value:null;save();};r.appendChild(sel);
   if(f==="rating_a")r.append("   B ");}
  d.appendChild(r);
  const n=document.createElement("textarea");n.rows=1;n.placeholder="notes (optional)";n.value=s.notes||"";
  n.oninput=()=>{s.notes=n.value;save();};d.appendChild(n);root.appendChild(d);});count();}
function payload(){return JSON.stringify({session:SESSION,saved_at:new Date().toISOString(),
 answers:TRIALS.map(t=>({trial:t.id,n:t.n,choice:(ans[t.id]||{}).choice??null,rating_a:(ans[t.id]||{}).rating_a??null,
 rating_b:(ans[t.id]||{}).rating_b??null,notes:(ans[t.id]||{}).notes||""}))},null,1);}
document.getElementById("dl").onclick=()=>{const u=URL.createObjectURL(new Blob([payload()],{type:"application/json"}));
 const a=document.createElement("a");a.href=u;a.download=SESSION+".answers.json";a.click();
 document.getElementById("msg").textContent="downloaded (check your Downloads folder)";};
document.getElementById("cp").onclick=()=>{const s=payload();const o=document.getElementById("out");o.value=s;o.select();
 (navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(()=>document.getElementById("msg").textContent="copied",()=>{});};
render();
</script></body></html>"""


def render_page(name: str, trials: list[dict], note: str = "") -> str:
    return (PAGE.replace("__NOTE__", html.escape(note)).replace("__TITLE__", html.escape(f"Rating session {name}"))
            .replace("__SESSION_JS__", json.dumps(name)).replace("__SESSION__", html.escape(name))
            .replace("__TRIALS__", json.dumps(trials)))


# ---------------------------------------------------------------- play (audio only)

def _wait(sd, secs: float) -> None:
    deadline = time.monotonic() + secs + 5.0
    get = getattr(sd, "get_stream", None)
    while get is not None:
        try:
            if not get().active:
                break
        except RuntimeError:
            break
        if time.monotonic() > deadline:
            sd.stop()
            raise RuntimeError("the audio stream did not finish; check the HX Stomp USB connection")
        time.sleep(0.02)
    if hasattr(sd, "wait"):
        sd.wait()


def play_trial(session: str, trial_n: int, gain_db: float = 0.0, which: str = "TAB", gap_s: float = 1.0,
               out_root: Path = RATINGS_DIR, sd=None, device: int | None = None, say=print) -> list[str]:
    """Play T, A, B of one trial (as numbered on the page) on host outputs 1/2 of the HX Stomp. Audio
    playback only: no Stomp control channel is touched. Returns the order played."""
    folder = out_root / session
    man = json.loads((folder / "trials.json").read_text())
    tr = next((t for t in man["trials"] if t["n"] == trial_n), None)
    if tr is None:
        raise SystemExit(f"session {session} has no trial {trial_n} (1..{len(man['trials'])})")
    if sd is None:
        import sounddevice as sd
    if device is None:
        from tonematch.reamp import find_device
        device = find_device()
    g = 10 ** (gain_db / 20)
    played = []
    for k in which:
        x = read_mono(folder / tr["files"][k]) * g
        if float(np.max(np.abs(x))) > 1.0:
            raise SystemExit(f"gain {gain_db:+.1f} dB would clip clip {k}; lower --gain-db")
        say({"T": "Target", "A": "A", "B": "B"}[k] + " ...")
        buf = np.stack([x, x], axis=1).astype(np.float32)
        sd.play(buf, SR, device=device, mapping=[1, 2])
        _wait(sd, len(x) / SR)
        played.append(k)
        if k != which[-1]:
            time.sleep(gap_s)
    return played


# ---------------------------------------------------------------- statistics

def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    from scipy.stats import beta

    if n == 0:
        return (float("nan"), float("nan"))
    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    if n == 0:
        return 1.0
    pm = [math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(n + 1)]
    return float(min(1.0, sum(v for v in pm if v <= pm[k] * (1 + 1e-9))))


def agreement(rows: list[tuple[str, float, float]]) -> dict:
    """rows: (human choice 'A'|'B', score of A, score of B), lower score = the score says closer.
    A row counts only if the human decided and the score is finite and not tied."""
    k = n = skipped = 0
    for ch, sa, sb in rows:
        if ch not in ("A", "B") or not (np.isfinite(sa) and np.isfinite(sb)) or sa == sb:
            skipped += 1
            continue
        n += 1
        k += (ch == "A") == (sa < sb)
    lo, hi = clopper_pearson(k, n)
    return {"n": n, "k": k, "rate": (k / n) if n else None, "ci": [lo, hi],
            "p": binom_two_sided(k, n) if n else None, "skipped": skipped}


def spearman(obs: list[tuple[str, float, float]], seed: int = 0, boots: int = 2000) -> dict:
    """obs: (family, score, rating). The score is z-scored within its family (different targets have
    different scales) and sign-flipped (lower score = closer), so a POSITIVE rho means the score agrees
    with the human's ratings. Bootstrap 95% interval over observations."""
    from scipy.stats import spearmanr

    ok = [(f, s, r) for f, s, r in obs if r is not None and np.isfinite(s)]
    if len(ok) < 5:
        return {"n": len(ok), "rho": None, "ci": [None, None]}
    z = []
    for f, s, r in ok:
        vals = np.array([o[1] for o in ok if o[0] == f])
        sd = vals.std()
        z.append(-(s - vals.mean()) / sd if sd > 0 else 0.0)
    z = np.array(z)
    rt = np.array([o[2] for o in ok], float)

    def rho(a, b):
        if np.ptp(a) == 0 or np.ptp(b) == 0:
            return float("nan")
        return float(spearmanr(a, b)[0])

    r0 = rho(z, rt)
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(boots):
        i = rng.integers(0, len(z), len(z))
        v = rho(z[i], rt[i])
        if np.isfinite(v):
            bs.append(v)
    ci = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))] if bs else [None, None]
    return {"n": len(ok), "rho": r0, "ci": ci}


# ---------------------------------------------------------------- the existing scores

class Scorer:
    """All existing scores as distances target -> candidate (lower = closer), on the clips the human
    heard (loudness-matched excerpt), except level_db which needs the original levels."""

    def __init__(self, folder: Path, key: dict, use_embed: bool = True, log=print):
        from tonematch.metrics import LossKit, afxrep
        from tonematch.metrics import nodi_candidates as N

        self.N, self.kit, self.folder, self.log = N, LossKit(), folder, log
        self.use_embed = use_embed and afxrep.available()
        self.afx = afxrep
        self.clips = {}
        for t in key["trials"]:
            for r in "TAB":
                self.clips[(t["id"], r)] = read_mono(folder / t["files"][r])
        feats = {k: (N.texmod_features(v), N.frame_features(v), N.presence_vector(v)) for k, v in self.clips.items()}
        self.feats = feats
        self.scales = N.fit_scales([f[0] for f in feats.values()], [f[1] for f in feats.values()],
                                   [f[2] for f in feats.values()])

    def one(self, trial: dict, role: str) -> dict[str, float]:
        from tonematch.metrics import ltas
        from tonematch.metrics.kit import PROVISIONAL_WEIGHTS
        from tonematch.metrics.nonlinear import diagnose

        N, tid = self.N, trial["id"]
        t, c = self.clips[(tid, "T")], self.clips[(tid, role)]
        out: dict[str, float] = {}
        s = self.kit.score(t, c, weights={"mrstft_v2": 1.0, **PROVISIONAL_WEIGHTS})
        out["mrstft_v2"] = s.terms["mrstft_v2"]
        for k in ("ltas_tilt", "crest_flatness_hnr", "envelope"):
            out[k] = s.terms[k]
        out["provisional_kit"] = float(sum(s.terms[k] for k in PROVISIONAL_WEIGHTS))
        info = trial[role]
        t_db, c_db = trial.get("target_orig_db"), info.get("orig_db")
        if t_db is not None and c_db is not None:
            lv = abs(c_db - t_db)
            out["level_db"] = lv
            out["mrstft_v2+level_db"] = out["mrstft_v2"] + LEVEL_TIE_WEIGHT * lv
        else:
            out["level_db"] = float("nan")
            out["mrstft_v2+level_db"] = float("nan")
        if self.use_embed:
            out["embed_afxrep"] = float(self.kit.score(t, c, weights={"embed_afxrep": 1.0}).terms["embed_afxrep"])
        di = read_mono(Path(trial["di"])) if trial.get("di") else None
        try:
            d = diagnose(c, t, di=di)
        except Exception as e:  # noqa: BLE001 - a failed diagnostic is a missing score, not a crash
            self.log(f"  diagnose failed on {tid}/{role}: {type(e).__name__}: {e}")
            d = {}
        hd = d.get("harmonic_diff_db")
        out["nl_harmonic_max"] = float(d.get("harmonic_diff_max_abs_db", float("nan")))
        out["nl_harmonic_mean"] = float(np.nanmean(np.abs(np.asarray(hd)[1:10]))) if hd else float("nan")
        out["nl_band_rms"] = float(d.get("core_band_diff_rms_db", float("nan")))
        inh = d.get("inharmonic_share_db")
        out["nl_inharmonic"] = abs(inh["a"] - inh["b"]) if inh else float("nan")
        ft, fc = self.feats[(tid, "T")], self.feats[(tid, role)]
        out["ltas_eqfit"] = N.eqfit(ltas.features(c), ltas.features(t))[0]
        out["texmod"] = N.texmod_distance(ft[0], fc[0], self.scales.tex)
        out["mmd_cep"] = N.mmd(ft[1], fc[1], self.scales.frame, self.scales.sigma)
        out["presence_nodi"] = N.vec_distance(ft[2], fc[2], self.scales.pres)
        return {k: float(v) for k, v in out.items()}


def score_all(key: dict, answers: dict, scorer, *, log=print, pooled_records=None) -> dict:
    """Join answers with the key and compute every report number. `scorer.one(trial, 'A'|'B')` -> dict."""
    byid = {a["trial"]: a for a in answers.get("answers", [])}
    trials = key["trials"]
    out: dict = {"session": key["session"], "n_trials": len(trials)}
    answered = [t for t in trials if byid.get(t["id"], {}).get("choice") in ("A", "B", "tie")]
    out["answered"] = len(answered)
    # sanity
    san = []
    for t in trials:
        if not t["kind"].startswith("sanity"):
            continue
        c = byid.get(t["id"], {}).get("choice")
        if t["kind"] == "sanity_target":
            san.append({"trial": t["n"], "kind": t["kind"], "choice": c,
                        "correct": None if c is None else c == t["expected"]})
        else:
            san.append({"trial": t["n"], "kind": t["kind"], "choice": c,
                        "correct": None, "called_tie": None if c is None else c == "tie"})
    out["sanity"] = san
    # retest: same candidate chosen both times
    byt = {t["id"]: t for t in trials}
    ret = []
    for t in trials:
        if t["kind"] != "repeat":
            continue
        o = byt[t["repeat_of"]]
        pick = []
        for tr in (o, t):
            c = byid.get(tr["id"], {}).get("choice")
            pick.append(None if c is None else ("tie" if c == "tie" else tr[c]["name"]))
        ret.append({"trial": t["n"], "original": o["n"], "first": pick[0], "second": pick[1],
                    "consistent": None if None in pick else pick[0] == pick[1]})
    out["retest"] = ret
    # scores
    regs = [t for t in trials if t["kind"] in ("regular", "repeat")]
    sc: dict[str, dict[str, dict]] = {}
    for i, t in enumerate(regs):
        log(f"  scoring trial {t['n']} ({i + 1}/{len(regs)})")
        sc[t["id"]] = {"A": scorer.one(t, "A"), "B": scorer.one(t, "B")}
    names = [n for n in SCORE_ORDER if any(n in v["A"] for v in sc.values())]
    table = {}
    for n in names:
        rows, obs = [], []
        for t in regs:
            a = byid.get(t["id"], {})
            sa, sb = sc[t["id"]]["A"].get(n, float("nan")), sc[t["id"]]["B"].get(n, float("nan"))
            rows.append((a.get("choice"), sa, sb))
            obs += [(t["family"], sa, a.get("rating_a")), (t["family"], sb, a.get("rating_b"))]
        row = agreement(rows)
        row["spearman"] = spearman(obs)
        table[n] = row
    out["scores"] = table
    if any("rung" in t["A"] for t in regs):         # v2: ladder and per-class reports
        out["per_class"] = {}
        for fam in sorted({t["family"] for t in regs}):
            ts = [t for t in regs if t["family"] == fam]
            out["per_class"][fam] = {
                n: agreement([(byid.get(t["id"], {}).get("choice"), sc[t["id"]]["A"].get(n, float("nan")),
                               sc[t["id"]]["B"].get(n, float("nan"))) for t in ts]) for n in names}
        out["ladder"] = ladder_report(regs, byid)
    out["trial_scores"] = sc
    if key.get("version") == 3:                     # v3: disputed-only, pooled and pair-type extras
        import rating_v3
        out["v3"] = rating_v3.analyse(key, byid, sc, pooled_records)
    if key.get("version") == 4:                     # v4: twin swaps versus large knob moves
        import rating_v4
        out["v4"] = rating_v4.analyse(key, byid, sc)
    out["decided"] = sum(1 for t in regs if byid.get(t["id"], {}).get("choice") in ("A", "B"))
    out["too_close"] = sum(1 for t in regs if byid.get(t["id"], {}).get("choice") == "tie")
    out["per_trial"] = [{"n": t["n"], "family": t["family"], "kind": t["kind"],
                         "A": t["A"]["name"], "B": t["B"]["name"], "choice": byid.get(t["id"], {}).get("choice"),
                         "rating_a": byid.get(t["id"], {}).get("rating_a"), "rating_b": byid.get(t["id"], {}).get("rating_b")}
                        for t in trials]
    return out


def ladder_report(regs: list[dict], byid: dict) -> dict:
    """Does the HUMAN's choice follow the ladder (lower rung = closer)? Decided regular/repeat trials only.
    A sanity check of the ladder itself, not of any score. Also the mean 1-5 rating by rung."""
    def rows_of(ts):
        out = []
        for t in ts:
            ch = byid.get(t["id"], {}).get("choice")
            if ch not in ("A", "B") or t["A"].get("rung") == t["B"].get("rung"):
                continue
            lo = "A" if t["A"]["rung"] < t["B"]["rung"] else "B"
            out.append((t, ch == lo))
        return out

    def rate(rs):
        k, n = sum(x for _, x in rs), len(rs)
        lo, hi = clopper_pearson(k, n)
        return {"n": n, "k": k, "rate": (k / n) if n else None, "ci": [lo, hi],
                "p": binom_two_sided(k, n) if n else None}
    allr = rows_of(regs)
    pair, cls = {}, {}
    for t, ok in allr:
        a, b = sorted((t["A"]["rung"], t["B"]["rung"]))
        pair.setdefault(f"{a} vs {b}", []).append((t, ok))
        cls.setdefault(t["family"], []).append((t, ok))
    ratings: dict[int, list[float]] = {}
    for t in regs:
        a = byid.get(t["id"], {})
        for side, f in (("A", "rating_a"), ("B", "rating_b")):
            if a.get(f) is not None:
                ratings.setdefault(t[side]["rung"], []).append(float(a[f]))
    return {"overall": rate(allr), "by_pair": {k: rate(v) for k, v in sorted(pair.items())},
            "by_class": {k: rate(v) for k, v in sorted(cls.items())},
            "mean_rating_by_rung": {str(k): {"n": len(v), "mean": float(np.mean(v))} for k, v in sorted(ratings.items())}}


def fmt(res: dict) -> str:
    L = [f"rating session {res['session']}: {res['answered']} of {res['n_trials']} trials answered "
         + f"({res['decided']} decided A/B on regular+repeat trials, {res['too_close']} 'too close to call')"]
    L.append("\nSanity (the human's own consistency):")
    for s in res["sanity"]:
        if s["kind"] == "sanity_target":
            L.append(f"  trial {s['trial']:>2} target-copy vs other: chose {s['choice']} -> "
                     + ("not answered" if s["correct"] is None else "picked the target copy" if s["correct"] else "DID NOT pick the target copy"))
        else:
            L.append(f"  trial {s['trial']:>2} near-identical pair: chose {s['choice']}"
                     + (" (called it too close)" if s.get("called_tie") else ""))
    for r in res["retest"]:
        L.append(f"  retest trial {r['trial']} (again {r['original']}): {r['first']} then {r['second']} -> "
                 + ("not answered" if r["consistent"] is None else "consistent" if r["consistent"] else "changed"))
    L.append("\nAgreement with the human's choice (decided trials only; CI = exact 95% binomial; "
             "Spearman: + means the score ranks like the 1-5 ratings):")
    L.append(f"  {'score':<20}{'agree':>12}{'rate':>7}  {'95% CI':<14}{'p(=.5)':>7}   {'Spearman rho (n, 95% CI)'}")
    for n, r in res["scores"].items():
        sp = r["spearman"]
        spt = "n/a" if sp["rho"] is None else f"{sp['rho']:+.2f} (n={sp['n']}, {sp['ci'][0]:+.2f}..{sp['ci'][1]:+.2f})"
        if r["n"]:
            L.append(f"  {n:<20}{r['k']:>6}/{r['n']:<5}{r['rate']:>7.0%}  [{r['ci'][0]:.2f},{r['ci'][1]:.2f}]   "
                     f"{r['p']:>6.3f}   {spt}")
        else:
            L.append(f"  {n:<20}{'no data':>12}{'':>7}  {'':<14}{'':>7}   {spt}")
    if "ladder" in res:
        lad = res["ladder"]

        def line(lab, r):
            if not r["n"]:
                return f"  {lab:<14}no decided trials"
            return f"  {lab:<14}{r['k']:>3}/{r['n']:<3} {r['rate']:>5.0%}  [{r['ci'][0]:.2f},{r['ci'][1]:.2f}]  p={r['p']:.3f}"
        L.append("\nDoes the human's choice follow the ladder? (picked the lower rung as closer; "
                 "rung 1 = same model nudged, 2 = same model moved a lot, 3 = same group, 4 = other class, 5 = clean)")
        L.append(line("all decided", lad["overall"]))
        for k, r in lad["by_pair"].items():
            L.append(line("rung " + k, r))
        L.append("  by circuit class:")
        for k, r in lad["by_class"].items():
            L.append(line("  " + k, r))
        if lad["mean_rating_by_rung"]:
            L.append("  mean 1-5 rating by rung: " + ", ".join(
                f"{k}: {v['mean']:.2f} (n={v['n']})" for k, v in lad["mean_rating_by_rung"].items()))
        pc = res["per_class"]
        L.append("\nAgreement of each score with the human's choice, per circuit class (agree/decided):")
        L.append(f"  {'score':<20}" + "".join(f"{c[:9]:>11}" for c in pc))
        for n in res["scores"]:
            L.append(f"  {n:<20}" + "".join(
                f"{(str(pc[c][n]['k']) + '/' + str(pc[c][n]['n'])) if pc[c][n]['n'] else '-':>11}" for c in pc))
    if "v3" in res:
        import rating_v3
        L.append(rating_v3.fmt_extras(res["v3"]))
    if "v4" in res:
        import rating_v4
        L.append(rating_v4.fmt_extras(res["v4"]))
    L.append("\nThis is a measurement. It admits and rejects nothing.")
    return "\n".join(L)


def write_ledger(res: dict, ledger) -> None:
    ds = f"ab-{res['session']}"
    for n, r in res["scores"].items():
        if r["n"]:
            sp = r["spearman"]
            note = (f"agreement with the human's A/B choice (measurement only, admits nothing); {r['k']}/{r['n']} decided; "
                    f"95% CI {r['ci'][0]:.2f}-{r['ci'][1]:.2f}; p={r['p']:.3f}; spearman="
                    + ("n/a" if sp["rho"] is None else f"{sp['rho']:+.2f} n={sp['n']}"))
            ledger(ds, f"agree_{n}", r["rate"], note)
    for g, tag in (("disputed", "disputed"), ("near_threshold", "nearthr")):
        for n, r in res.get("v3", {}).get(g, {}).items():
            if r["n"]:
                ledger(ds, f"agree_{tag}_{n}", r["rate"], f"agreement with the human on {tag} v3 trials only (measurement, "
                       f"admits nothing); {r['k']}/{r['n']} decided; 95% CI {r['ci'][0]:.2f}-{r['ci'][1]:.2f}; p={r['p']:.3f}")
    s = [x for x in res["sanity"] if x["kind"] == "sanity_target" and x["correct"] is not None]
    if s:
        ledger(ds, "sanity_correct", sum(x["correct"] for x in s) / len(s), f"{sum(x['correct'] for x in s)}/{len(s)} target-copy trials")
    lad = res.get("ladder", {}).get("overall")
    if lad and lad["n"]:
        ledger(ds, "ladder_follow", lad["rate"], f"human picked the lower (nearer) rung {lad['k']}/{lad['n']}; "
               f"95% CI {lad['ci'][0]:.2f}-{lad['ci'][1]:.2f}; sanity check of the ladder, not a score")
    r = [x for x in res["retest"] if x["consistent"] is not None]
    if r:
        ledger(ds, "retest_consistent", sum(x["consistent"] for x in r) / len(r), f"{sum(x['consistent'] for x in r)}/{len(r)} repeated trials")


def find_answers(session: str, out_root: Path = RATINGS_DIR, downloads: Path | None = None) -> Path | None:
    p = out_root / f"{session}.answers.json"
    if p.exists():
        return p
    dl = Path.home() / "Downloads" if downloads is None else downloads
    cands = sorted(dl.glob(f"{session}.answers*.json"), key=lambda q: q.stat().st_mtime) if dl.exists() else []
    return cands[-1] if cands else None
