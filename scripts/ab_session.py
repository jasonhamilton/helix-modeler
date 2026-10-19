"""Blind A/B listening sessions (Gate 2 and Gate 3 optional, Gate 4 pedal listening and Gate 5 real songs required). Offline; no Native, no pedal.

  build        make a session from pairs of WAV files: trim to one shared excerpt, match loudness (LUFS),
               randomise which clip is A with a seeded key kept OUTSIDE the page folder, optional ABX trials.
               Writes reports/local/ab/<session>/index.html (open it as a file) and
               reports/local/ab/<session>.key.json (the hidden key; do not open it before you finish).
  score        join the exported answers with the key: per-pair preference, binomial test, ABX accuracy;
               ledger rows kind=ab (this reports the human's answers; it never judges the audio).
  gate2-demo   a small session from synthetic-v1 run outputs if any have rendered audio, else instructions.
  rating-build / rating-build-v2 / play / rating-score
               the T170 human rating session (target + two candidates per trial, "which is closer?"),
               played on the HX Stomp outputs 1/2, then every existing score measured against the
               answers. Code in scripts/rating_session.py (v1, withdrawn) and rating_v2.py (hidden Helix settings over the
               human's own dry take); protocols docs/notes/human-ratings-v1.md, human-ratings-v2.md.

In every pair, clip X is the thing under test (our match / the recovered preset) and clip Y is what it is
compared with (the target, or the nearest stock preset). "X chosen" is the headline preference.
No song audio leaves the machine: everything stays under reports/local (gitignored).
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tonematch.separation.audio import integrated_loudness, load_audio

SR = 48000
AB_DIR = ROOT / "reports" / "local" / "ab"
TARGET_LUFS = -23.0
PEAK_CEIL = 0.98
FADE_S = 0.02
DEMO_DIR = ROOT / "cache" / "synthetic-v1" / "runs"


# ---------------------------------------------------------------- audio

def pick_excerpt(clips: list[np.ndarray], n: int) -> int:
    """Start sample of the n-sample window with most energy summed over all clips (deterministic)."""
    total = min(c.shape[-1] for c in clips)
    if total <= n:
        return 0
    e = sum(np.cumsum(np.concatenate([[0.0], (c[:, :total].mean(axis=0)) ** 2])) for c in clips)
    hop = SR // 4
    starts = np.arange(0, total - n + 1, hop)
    return int(starts[int(np.argmax(e[starts + n] - e[starts]))])


def trim_pair(x: np.ndarray, y: np.ndarray, excerpt_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Same excerpt of both clips: shared start and length (at most excerpt_s), short fades."""
    n = min(int(excerpt_s * SR), x.shape[-1], y.shape[-1])
    s = pick_excerpt([x, y], n)
    out = []
    for c in (x, y):
        c = c[:, s:s + n].copy()
        f = min(int(FADE_S * SR), n // 2)
        if f:
            ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)
            c[:, :f] *= ramp
            c[:, -f:] *= ramp[::-1]
        out.append(c)
    return out[0], out[1]


def match_loudness(x: np.ndarray, y: np.ndarray, target: float = TARGET_LUFS):
    """Scale both clips to `target` LUFS. If either would clip, both are lowered by the same extra
    amount, so the pair stays matched. Returns (x, y, info)."""
    lx, ly = integrated_loudness(x), integrated_loudness(y)
    gx = target - lx if lx > -69.9 else 0.0
    gy = target - ly if ly > -69.9 else 0.0
    px = float(np.max(np.abs(x))) * 10 ** (gx / 20)
    py = float(np.max(np.abs(y))) * 10 ** (gy / 20)
    worst = max(px, py)
    extra = 20 * math.log10(PEAK_CEIL / worst) if worst > PEAK_CEIL else 0.0
    gx, gy = gx + extra, gy + extra
    xo = (x * 10 ** (gx / 20)).astype(np.float32)
    yo = (y * 10 ** (gy / 20)).astype(np.float32)
    return xo, yo, {"lufs_in": [lx, ly], "gain_db": [gx, gy],
                    "lufs_out": [integrated_loudness(xo), integrated_loudness(yo)]}


def write_clip(path: Path, x: np.ndarray) -> None:
    sf.write(str(path), np.ascontiguousarray(x.T), SR, subtype="PCM_16")


# ---------------------------------------------------------------- key

def make_assignments(n_pairs: int, abx_trials: int, seed: int) -> dict:
    """Seeded hidden randomisation. x_is_a[i]: clip X (ours) is shown as A in pair i.
    abx: list of {pair, x_is: 'A'|'B'} for the first `abx_trials` pairs."""
    rng = np.random.default_rng(seed)
    x_is_a = [bool(v) for v in rng.integers(0, 2, n_pairs)]
    order = [int(v) for v in rng.permutation(n_pairs)]
    abx = [{"pair": int(p), "x_is": "A" if rng.integers(0, 2) else "B"}
           for p in rng.permutation(n_pairs)[:min(abx_trials, n_pairs)]]
    return {"x_is_a": x_is_a, "order": order, "abx": abx}


# ---------------------------------------------------------------- page

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>body{font:15px system-ui;max-width:760px;margin:2em auto;padding:0 1em}
.t{border:1px solid #ccc;border-radius:6px;padding:.8em;margin:1em 0}.t h3{margin:.2em 0}
button{margin:.2em;padding:.4em .9em}button.on{background:#333;color:#fff}
audio{width:100%}textarea{width:100%}#out{width:100%;height:8em}</style></head><body>
<h1>__TITLE__</h1><p>__INSTR__</p><div id="trials"></div>
<h2>Finish</h2><p><button id="exp">Copy answers as JSON</button> <span id="msg"></span></p>
<textarea id="out" readonly></textarea>
<script>
const SESSION=__SESSION__, TRIALS=__TRIALS__, KEY="ab-"+SESSION;
let ans=JSON.parse(localStorage.getItem(KEY)||"{}");
function save(){localStorage.setItem(KEY,JSON.stringify(ans));}
function slot(id){return ans[id]||(ans[id]={});}
function btns(t,field,opts){return opts.map(o=>{
 const b=document.createElement("button");b.textContent=o[1];
 const upd=()=>b.className=(slot(t.id)[field]===o[0])?"on":"";
 b.onclick=()=>{slot(t.id)[field]=o[0];save();render();};upd();b.dataset.v=o[0];return b;});}
function render(){const root=document.getElementById("trials");root.innerHTML="";
 TRIALS.forEach((t,i)=>{const d=document.createElement("div");d.className="t";
  d.innerHTML="<h3>"+(i+1)+". "+(t.type==="abx"?"ABX: which is X the same as?":"Which is better?")+"</h3>";
  for(const [lab,f] of t.clips){const p=document.createElement("p");
   p.innerHTML="<b>"+lab+"</b><br><audio controls preload=auto src='"+f+"'></audio>";d.appendChild(p);}
  const p=document.createElement("p");
  if(t.type==="abx"){btns(t,"answer",[["A","X is A"],["B","X is B"]]).forEach(b=>p.appendChild(b));}
  else{btns(t,"choice",[["A","A better"],["B","B better"],["same","Same"]]).forEach(b=>p.appendChild(b));
   const s=document.createElement("input");s.type="range";s.min=0;s.max=100;
   s.value=slot(t.id).confidence??50;s.oninput=()=>{slot(t.id).confidence=+s.value;save();};
   p.append(" confidence (optional): ",s);
   const n=document.createElement("textarea");n.rows=2;n.placeholder="notes";n.value=slot(t.id).notes||"";
   n.oninput=()=>{slot(t.id).notes=n.value;save();};d.appendChild(p);d.appendChild(n);root.appendChild(d);return;}
  d.appendChild(p);root.appendChild(d);});}
function exportJson(){const o={session:SESSION,answers:TRIALS.map(t=>({trial:t.id,type:t.type,...(ans[t.id]||{})}))};
 const s=JSON.stringify(o,null,1);document.getElementById("out").value=s;
 (navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(
  ()=>msg.textContent="copied",()=>{out.select();msg.textContent="select and copy the text below";});}
document.getElementById("exp").onclick=exportJson;render();
</script></body></html>"""

INSTR = ("Listen to A and B as often as you like and pick the one you prefer, or Same. Levels are matched. "
         "Answers save in this browser as you go. At the end press Copy, paste the text into a file, "
         "and give that file to the scorer.")


def render_page(name: str, trials: list[dict]) -> str:
    return (PAGE.replace("__TITLE__", html.escape(f"Listening session {name}"))
            .replace("__INSTR__", html.escape(INSTR))
            .replace("__SESSION__", json.dumps(name)).replace("__TRIALS__", json.dumps(trials)))


# ---------------------------------------------------------------- build

def build_session(name: str, pairs: list[dict], seed: int | None = None, excerpt_s: float = 8.0,
                  abx: int = 0, out_dir: Path = AB_DIR) -> tuple[Path, Path]:
    """pairs: [{label, x, y}] with WAV paths. Returns (session folder, key file)."""
    if not pairs:
        raise SystemExit("no pairs given")
    seed = secrets.randbelow(2**31) if seed is None else seed
    folder, keyf = out_dir / name, out_dir / f"{name}.key.json"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    asg = make_assignments(len(pairs), abx, seed)
    tags = [secrets.token_hex(4) for _ in pairs]  # opaque file names
    prepared, trials, kpairs = [], [], []
    for i, p in enumerate(pairs):
        x, y = load_audio(p["x"]), load_audio(p["y"])
        x, y = trim_pair(x, y, excerpt_s)
        x, y, info = match_loudness(x, y)
        a, b = (x, y) if asg["x_is_a"][i] else (y, x)
        write_clip(folder / f"{tags[i]}_a.wav", a)
        write_clip(folder / f"{tags[i]}_b.wav", b)
        prepared.append((x, y))
        kpairs.append({"index": i, "label": p["label"], "tag": tags[i], "x": str(p["x"]),
                       "y": str(p["y"]), "x_is_a": asg["x_is_a"][i], "excerpt_s": x.shape[-1] / SR,
                       **info})
    for pos in asg["order"]:
        t = tags[pos]
        trials.append({"id": f"p{t}", "type": "ab",
                       "clips": [["A", f"{t}_a.wav"], ["B", f"{t}_b.wav"]]})
    kabx = []
    for j, a in enumerate(asg["abx"]):
        i, t = a["pair"], tags[a["pair"]]
        x, y = prepared[i]
        xs_is_a = asg["x_is_a"][i]
        # "A" and "B" of an ABX trial are the pair's shown A and B; the hidden X copies one of them.
        shown = {"A": x if xs_is_a else y, "B": y if xs_is_a else x}
        xt = f"{t}_x{j}"
        write_clip(folder / f"{xt}.wav", shown[a["x_is"]])
        trials.append({"id": f"x{xt}", "type": "abx",
                       "clips": [["A", f"{t}_a.wav"], ["B", f"{t}_b.wav"], ["X", f"{xt}.wav"]]})
        kabx.append({"trial": f"x{xt}", "pair": i, "x_is": a["x_is"]})
    (folder / "index.html").write_text(render_page(name, trials))
    keyf.write_text(json.dumps({"session": name, "seed": seed, "pairs": kpairs, "abx": kabx,
                                "trial_pair": {f"p{tags[i]}": i for i in range(len(pairs))}}, indent=1))
    return folder, keyf


# ---------------------------------------------------------------- score

def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial test p-value (sum of outcomes no more likely than k)."""
    if n == 0:
        return 1.0
    pm = [math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(n + 1)]
    return float(min(1.0, sum(v for v in pm if v <= pm[k] * (1 + 1e-9))))


def score_answers(key: dict, answers: dict) -> dict:
    """Join answers with the key. Pure computation; reports the human's choices only."""
    byid = {a["trial"]: a for a in answers.get("answers", [])}
    per_pair, x_wins, y_wins, ties, open_ = [], 0, 0, 0, 0
    for tid, i in key["trial_pair"].items():
        kp, a = key["pairs"][i], byid.get(tid, {})
        c = a.get("choice")
        if c in ("A", "B"):
            chose = "x" if (c == "A") == kp["x_is_a"] else "y"
        elif c == "same":
            chose = "same"
        else:
            chose = None
        x_wins += chose == "x"
        y_wins += chose == "y"
        ties += chose == "same"
        open_ += chose is None
        per_pair.append({"pair": i, "label": kp["label"], "chose": chose,
                         "confidence": a.get("confidence"), "notes": a.get("notes", "")})
    n = x_wins + y_wins
    abx_n = abx_ok = 0
    for k in key.get("abx", []):
        r = byid.get(k["trial"], {}).get("answer")
        if r in ("A", "B"):
            abx_n += 1
            abx_ok += r == k["x_is"]
    return {
        "session": key["session"], "per_pair": per_pair, "x_chosen": x_wins, "y_chosen": y_wins,
        "same": ties, "unanswered": open_, "decided": n,
        "pref_rate_x": (x_wins / n) if n else None,
        "binom_p": binom_two_sided(x_wins, n) if n else None,
        "abx_n": abx_n, "abx_correct": abx_ok,
        "abx_acc": (abx_ok / abx_n) if abx_n else None,
        "abx_binom_p": binom_two_sided(abx_ok, abx_n) if abx_n else None,
    }


def ledger(dataset: str, metric: str, value: float, notes: str) -> None:
    env = {**os.environ, "TONEMATCH_AGENT": os.environ.get("TONEMATCH_AGENT", "benchmark")}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ledger_append.py"), "--kind", "ab",
                    "--dataset", dataset, "--metric", metric, "--value", f"{value:.6g}",
                    "--notes", notes], check=True, env=env, cwd=ROOT)


def write_ledger(res: dict) -> None:
    ds = f"ab-{res['session']}"
    note = (f"human ratings, not scored by the tool; x_chosen={res['x_chosen']} y_chosen={res['y_chosen']} "
            f"same={res['same']} unanswered={res['unanswered']}")
    if res["decided"]:
        ledger(ds, "ab_pref", res["pref_rate_x"], note)
        ledger(ds, "ab_binom_p", res["binom_p"], f"two-sided exact, n={res['decided']} decided")
    if res["abx_n"]:
        ledger(ds, "abx_acc", res["abx_acc"], f"{res['abx_correct']}/{res['abx_n']} trials; "
               f"p={res['abx_binom_p']:.3g}")


def fmt(res: dict) -> str:
    L = [f"session {res['session']}"]
    for p in res["per_pair"]:
        L.append(f"  pair {p['index'] if 'index' in p else p['pair']:>2} {p['label']:<30} "
                 f"chose {p['chose']}" + (f" (conf {p['confidence']})" if p["confidence"] is not None else ""))
    if res["decided"]:
        L.append(f"X (ours) chosen {res['x_chosen']} of {res['decided']} decided "
                 f"= {res['pref_rate_x']:.0%}; two-sided exact p = {res['binom_p']:.3f}; "
                 f"same {res['same']}, unanswered {res['unanswered']}")
    else:
        L.append("no decided pairs")
    if res["abx_n"]:
        L.append(f"ABX: {res['abx_correct']}/{res['abx_n']} correct = {res['abx_acc']:.0%}, "
                 f"p = {res['abx_binom_p']:.3f}")
    return "\n".join(L)


# ---------------------------------------------------------------- demo

def find_demo_pairs(root: Path = DEMO_DIR, limit: int = 6) -> list[dict]:
    """Pairs <id>_target.wav and <id>_recovered.wav anywhere under the synthetic-v1 runs."""
    out = []
    for tgt in sorted(root.rglob("*_target.wav")) if root.exists() else []:
        rec = tgt.with_name(tgt.name.replace("_target.wav", "_recovered.wav"))
        if rec.exists():
            out.append({"label": f"{tgt.parent.name}/{tgt.name[:-11]}", "x": rec, "y": tgt})
    return out[:limit]


DEMO_HELP = """gate2-demo: no rendered audio found under cache/synthetic-v1/runs (only JSON and logs; the run
stores no audio). Nothing was rendered. After the Gate 2 run, save for about 6 items:
    <run dir>/<item>_target.wav       the target preset rendered on the item's DI
    <run dir>/<item>_recovered.wav    the recovered preset rendered on the same DI
then run:  python scripts/ab_session.py gate2-demo
or by hand:  python scripts/ab_session.py build --name gate2 --pair "i001=rec1.wav,tgt1.wav" ... --abx 3"""


# ---------------------------------------------------------------- cli

def parse_pair(s: str) -> dict:
    label, _, rest = s.partition("=")
    parts = rest.split(",")
    if not rest or len(parts) != 2:
        raise SystemExit(f"--pair wants LABEL=X.wav,Y.wav (X under test, Y comparison): {s!r}")
    return {"label": label, "x": Path(parts[0]), "y": Path(parts[1])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--name", required=True)
    b.add_argument("--pair", action="append", default=[], help="LABEL=X.wav,Y.wav (repeatable)")
    b.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    b.add_argument("--excerpt-s", type=float, default=8.0)
    b.add_argument("--abx", type=int, default=0, help="number of ABX trials (taken from the pairs)")
    s = sub.add_parser("score")
    s.add_argument("--key", required=True, type=Path)
    s.add_argument("--answers", required=True, type=Path, help="exported JSON file, or - for stdin")
    s.add_argument("--no-ledger", action="store_true")
    d = sub.add_parser("gate2-demo")
    d.add_argument("--runs", type=Path, default=DEMO_DIR)
    d.add_argument("--seed", type=int, default=None)
    rb = sub.add_parser("rating-build", help="T170 rating session from clips on disk")
    rb.add_argument("--name", default="human-ratings-v1")
    rb.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    rb.add_argument("--excerpt-s", type=float, default=7.0)
    r2 = sub.add_parser("rating-build-v2", help="T170 v2: hidden Helix drive settings over the human's own dry take")
    r2.add_argument("--name", default="human-ratings-v2")
    r2.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    r2.add_argument("--withdraw-v1", action="store_true", help="replace the v1 page by a notice and delete its audio")
    r3 = sub.add_parser("rating-build-v3", help="T170 v3: hard pairs (scores disagree) on hidden Helix drive settings")
    r3.add_argument("--name", default="human-ratings-v3")
    r3.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    r3.add_argument("--no-ledger", action="store_true")
    r4 = sub.add_parser("rating-build-v4", help="T177 v4: near-twin models versus large knob moves")
    r4.add_argument("--name", default="human-ratings-v4")
    r4.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    r4.add_argument("--no-ledger", action="store_true")
    r5 = sub.add_parser("rating-build-v5", help="T188 v5: blind T185 predictions re-rendered on the target's own DI")
    r5.add_argument("--name", default="human-ratings-v5")
    r5.add_argument("--seed", type=int, default=None, help="default: random, stored in the key")
    r5.add_argument("--no-ledger", action="store_true")
    pl = sub.add_parser("play", help="play T, A, B of one rating trial on the HX Stomp USB outputs 1/2 (audio only)")
    pl.add_argument("session")
    pl.add_argument("--trial", type=int, required=True, help="trial number as shown on the page")
    pl.add_argument("--gain-db", type=float, default=0.0)
    pl.add_argument("--only", default="TAB", help="e.g. TA, B, TAB (order played)")
    pl.add_argument("--gap-s", type=float, default=1.0)
    rs = sub.add_parser("rating-score", help="score the rating session answers against the existing scores")
    rs.add_argument("session")
    rs.add_argument("--answers", type=Path, help="default: reports/local/ratings/<session>.answers.json, else newest in ~/Downloads")
    rs.add_argument("--no-ledger", action="store_true")
    rs.add_argument("--no-embed", action="store_true", help="skip embed_afxrep")
    a = ap.parse_args(argv)
    if a.cmd in ("rating-build", "rating-build-v2", "rating-build-v3", "rating-build-v4", "rating-build-v5", "play", "rating-score"):
        import rating_session as RS
        if a.cmd == "rating-build-v2":
            import rating_v2 as R2
            if a.withdraw_v1:
                print(f"v1 withdrawn: {R2.withdraw()} files deleted")
            folder, keyf = R2.build_session(a.name, seed=a.seed)
            print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder}, "
                  f"{R2.folder_mb(folder, RS.KEY_DIR / (a.name + '.di')):.1f} MB with the DI excerpts)\n"
                  f"hidden key: {keyf}  (do not open before scoring)")
            return 0
        if a.cmd == "rating-build-v3":
            import rating_v3 as R3
            folder, keyf = R3.build_session(a.name, seed=a.seed)
            kj = json.loads(keyf.read_text())
            n = len(kj["trials"])
            mb = R3.folder_mb(folder, RS.KEY_DIR / (a.name + "." + "di"))
            print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder}, {mb:.1f} MB with the DI excerpts)\n"
                  f"hidden key: {keyf}  (do not open before scoring)")
            if not a.no_ledger:
                ledger(f"ab-{a.name}", "trials_built", float(n),
                       f"v3 hard pairs: {n} trials (regular + sanity + retests), seed {kj['seed']}; page {RS.URL_BASE}/{a.name}/index.html; "
                       "built for the human's ratings; nothing scored")
            return 0
        if a.cmd == "rating-build-v4":
            import rating_v4 as R4
            folder, keyf = R4.build_session(a.name, seed=a.seed)
            kj = json.loads(keyf.read_text())
            n = len(kj["trials"])
            print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder}); {n} trials, {kj['n_renders']} renders\n"
                  f"hidden key: {keyf}  (do not open before scoring)")
            if not a.no_ledger:
                ledger(f"ab-{a.name}", "trials_built", float(n),
                       f"v4 near-twin models vs large knob moves: {n} trials (regular + sanity + retests), seed {kj['seed']}, "
                       f"{kj['n_renders']} renders on one Native host; page {RS.URL_BASE}/{a.name}/index.html; "
                       "built for the human's ratings; nothing scored")
            return 0
        if a.cmd == "rating-build-v5":
            import rating_v5 as R5
            folder, keyf = R5.build_session(a.name, seed=a.seed)
            kj = json.loads(keyf.read_text())
            n = len(kj["trials"])
            print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder}); {n} trials, {kj['n_renders']} renders\n"
                  f"hidden key: {keyf}  (do not open before scoring)")
            if not a.no_ledger:
                ledger(f"ab-{a.name}", "trials_built", float(n),
                       f"v5 blind T185 predictions re-rendered on the target's own DI (AFx pick vs provisional pick): {n} trials "
                       f"(regular + sanity + retests), seed {kj['seed']}, {kj['n_renders']} renders on one Native host; "
                       f"page {RS.URL_BASE}/{a.name}/index.html; built for the human's ratings; nothing scored")
            return 0
        if a.cmd == "rating-build":
            folder, keyf = RS.build_session(a.name, seed=a.seed, excerpt_s=a.excerpt_s)
            print(f"page: {RS.URL_BASE}/{a.name}/index.html  (folder {folder})\nhidden key: {keyf}  (do not open before scoring)")
        elif a.cmd == "play":
            RS.play_trial(a.session, a.trial, a.gain_db, a.only.upper(), a.gap_s)
        else:
            ans = a.answers or RS.find_answers(a.session)
            if ans is None:
                raise SystemExit(f"no answers file found: save the page's download as {RS.RATINGS_DIR / (a.session + '.answers.json')}")
            key = json.loads((RS.KEY_DIR / f"{a.session}.key.json").read_text())
            scorer = RS.Scorer(RS.RATINGS_DIR / a.session, key, use_embed=not a.no_embed)
            pooled = None
            if key.get("version") == 3:
                import rating_v3
                pooled = rating_v3.load_v2_records(use_embed=not a.no_embed)
            res = RS.score_all(key, json.loads(Path(ans).read_text()), scorer, pooled_records=pooled)
            txt = RS.fmt(res)
            print(txt)
            (RS.RATINGS_DIR / f"{a.session}.results.json").write_text(json.dumps(res, indent=1, default=float))
            (RS.RATINGS_DIR / f"{a.session}.results.txt").write_text(txt)
            if not a.no_ledger:
                RS.write_ledger(res, ledger)
        return 0
    if a.cmd == "build":
        folder, keyf = build_session(a.name, [parse_pair(p) for p in a.pair], a.seed, a.excerpt_s, a.abx)
    elif a.cmd == "score":
        txt = sys.stdin.read() if str(a.answers) == "-" else a.answers.read_text()
        res = score_answers(json.loads(a.key.read_text()), json.loads(txt))
        print(fmt(res))
        if not a.no_ledger:
            write_ledger(res)
        return 0
    else:
        pairs = find_demo_pairs(a.runs)
        if not pairs:
            print(DEMO_HELP)
            return 0
        folder, keyf = build_session("gate2-demo", pairs, a.seed, abx=min(3, len(pairs)))
    print(f"session page: {folder / 'index.html'}\nhidden key:   {keyf}  (do not open before scoring)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
