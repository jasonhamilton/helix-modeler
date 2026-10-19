"""T170 human-ratings-v3 (hard pairs): pair selection (disagreement criterion), the hidden key, the ratings UI wiring,
and scoring. Fake audio, fake renderer, fake pool scorer."""
import itertools
import json
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import rating_session as RS
import rating_v3 as R3

SR = 48000
NAMES = [n for n in RS.SCORE_ORDER if n not in R3.SPLIT_EXCLUDE]


def fake_render(model, params, dry):
    drive, tones = R3.KNOBS3[model]
    g = 1 + 20 * params[drive]
    h = (zlib.crc32(model.encode()) % 1000 + 1) / 1000
    y = np.tanh(g * dry * (0.5 + h)) * (0.2 + 0.1 * sum(params.get(t, 0.5) for t in tones))
    # a tone-dependent one-pole smoothing so tone knobs change the spectrum too
    a = 0.1 + 0.8 * (params.get(tones[0], 0.5) if tones else 0.5)
    from scipy.signal import lfilter
    out = lfilter([1 - a], [1, -a], y)
    return out.astype(np.float32)


def band_feats(x):
    X = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    edges = [0, 150, 400, 1000, 2500, 6000, 24001]
    return np.array([np.log10(X[(f >= lo) & (f < hi)].sum() + 1e-9) for lo, hi in itertools.pairwise(edges)])


def fake_pool_scorer(entries):
    """16 different 'scores' = |difference| of different band-energy mixtures, so they disagree with each other."""
    rng = np.random.default_rng(0)
    W = rng.standard_normal((len(RS.SCORE_ORDER), 6))
    out = []
    for e in entries:
        d = band_feats(e["C"]) - band_feats(e["T"])
        out.append({n: float(abs(W[i] @ d)) for i, n in enumerate(RS.SCORE_ORDER)})
        out[-1]["level_db"] = abs(e["c_db"] - e["t_db"])
    return out


@pytest.fixture()
def takes(tmp_path):
    d = tmp_path / "takes"
    d.mkdir()
    rng = np.random.default_rng(0)
    for n in (1, 2, 3):
        t = np.arange(40 * SR) / SR
        env = (np.sin(2 * np.pi * 0.7 * t + n) > -0.3) * (0.2 + 0.2 * np.abs(np.sin(2 * np.pi * 3.1 * t)))
        x = env * np.sin(2 * np.pi * (110 + 20 * n) * t) + 0.01 * rng.standard_normal(len(t))
        sf.write(str(d / f"20261018-113158_take{n}_dry.wav"), x.astype(np.float32), SR, subtype="FLOAT")
    return d


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("v3")
    d = tmp / "takes"
    d.mkdir()
    rng = np.random.default_rng(0)
    for n in (1, 2, 3):
        t = np.arange(40 * SR) / SR
        env = (np.sin(2 * np.pi * 0.7 * t + n) > -0.3) * (0.2 + 0.2 * np.abs(np.sin(2 * np.pi * 3.1 * t)))
        x = env * np.sin(2 * np.pi * (110 + 20 * n) * t) + 0.01 * rng.standard_normal(len(t))
        sf.write(str(d / f"20261018-113158_take{n}_dry.wav"), x.astype(np.float32), SR, subtype="FLOAT")
    folder, keyf = R3.build_session("s", seed=11, render=fake_render, pool_scorer=fake_pool_scorer, takes_dir=d,
                                    out_root=tmp / "pages", key_root=tmp / "keys", excerpt_s=1.5, log=lambda *a: None)
    return tmp, folder, keyf, json.loads(keyf.read_text())


# ------------------------------------------------------------ pools and the model list

def test_models_exclude_novelty_and_time_variant():
    assert not (R3.all_models() & R3.EXCLUDED)
    assert all(m in R3.KNOBS3 for m in R3.all_models())
    try:
        from tonematch.engines import native_library as NL
        tv = NL.time_variant_models()
    except Exception:  # noqa: BLE001
        tv = frozenset()
    assert not (R3.all_models() & tv)
    assert len(R3.TARGETS) == 10


@pytest.mark.parametrize("seed", range(8))
def test_pool_composition(seed):
    rng = np.random.default_rng(seed)
    for spec in R3.TARGETS:
        pool = R3.make_pool(spec, rng)
        c = pool["cands"]
        assert 11 <= len(c) <= 15, (spec[0], len(c))
        drive, tones = R3.KNOBS3[pool["model"]]
        for x in c:
            assert all(0.0 <= v <= 1.0 for v in x["params"].values())
            if x["kind"] == "knob":                     # exactly one knob differs from the target, by a small step
                diff = {k: abs(x["params"][k] - pool["target"][k]) for k in x["params"] if abs(x["params"][k] - pool["target"][k]) > 1e-9}
                assert list(diff) == [x["knob"]] and abs(diff[x["knob"]] - abs(x["delta"])) < 0.011
                assert x["knob"] in [drive, *tones]
        kinds = {x["kind"] for x in c}
        assert {"knob", "twin", "diff"} <= kinds


# ------------------------------------------------------------ pair selection

def test_pair_stats_votes_and_split():
    names = ["a", "b", "c", "d"]
    st = R3.pair_stats({"a": 1, "b": 1, "c": 5, "d": 5}, {"a": 2, "b": 2, "c": 4, "d": 4}, names, {n: 1.0 for n in names})
    assert st["n_i"] == 2 and st["n_j"] == 2 and st["split"] == 1.0 and st["min_share"] == 0.5
    st = R3.pair_stats({"a": 1, "b": 1, "c": 1, "d": float("nan")}, {"a": 2, "b": 2, "c": 2, "d": 0}, names, {n: 1.0 for n in names})
    assert st["n_i"] == 3 and st["n_j"] == 0 and st["split"] == 0.0 and st["votes"]["d"] is None


def synthetic_pools(seed=0, n_t=10, k=12):
    rng = np.random.default_rng(seed)
    pools = []
    kinds = ["knob"] * 6 + ["twin"] * 3 + ["same"] * 1 + ["diff"] * 2
    for t in range(n_t):
        cands = [{"cid": f"c{i}", "kind": kinds[i], "model": "M" if kinds[i] == "knob" else f"X{i}",
                  "knob": "Gain" if kinds[i] == "knob" and i % 2 == 0 else "Tone", "delta": (0.05 * (i + 1)) * (1 if i % 3 else -1)}
                 for i in range(k)]
        # each score = noisy version of a latent distance; a few scores have their own latent, so pairs split
        latent = rng.uniform(0, 1, k)
        other = rng.uniform(0, 1, k)
        scores = []
        for i in range(k):
            s = {}
            for q, n in enumerate(RS.SCORE_ORDER):
                s[n] = float((latent[i] if q % 3 else other[i]) + 0.05 * rng.standard_normal())
            scores.append(s)
        pools.append({"cands": cands, "scores": scores})
    return pools


def test_selection_meets_the_disagreement_criterion():
    pools = synthetic_pools(1)
    ch = R3.select_pairs(pools, np.random.default_rng(3))
    assert len(ch) == R3.N_DISPUTED + R3.N_AGREE
    dis = [r for r in ch if r["kind"] == "disputed"]
    ag = [r for r in ch if r["kind"] == "agree"]
    assert len(dis) == R3.N_DISPUTED and len(ag) == R3.N_AGREE
    assert all(r["min_share"] >= R3.MIN_SHARE for r in dis)           # at least 30% of the scores on each side
    assert all(r["min_share"] <= R3.AGREE_MAX_SHARE for r in ag)
    # near-threshold pairs are the small differences among the agreeing pairs of their target
    allr = {(r["t"], r["i"], r["j"]): r for r in R3.all_pairs(pools)}
    for r in ag:
        un = sorted(x["z_median"] for x in allr.values() if x["t"] == r["t"] and x["min_share"] <= R3.AGREE_MAX_SHARE and x["n_voting"] >= 5)
        assert r["z_median"] <= un[len(un) // 2]
    # spread: every target used, none more than the maximum, three pair types all present
    per_t = np.bincount([r["t"] for r in ch], minlength=10)
    assert per_t.min() >= 3 and per_t.max() <= R3.PER_TARGET_MAX
    assert {r["type"] for r in ch} == {"knob", "twin", "class"}
    # disputed pairs are chosen among the most split ones: well above what random pairs give
    rand = np.mean([r["split"] for r in allr.values()])
    assert np.mean([r["split"] for r in dis]) > rand


def test_selection_avoids_duplicates_and_overuse():
    pools = synthetic_pools(2)
    ch = R3.select_pairs(pools, np.random.default_rng(4))
    for t in range(10):
        mine = [r for r in ch if r["t"] == t]
        c = pools[t]["cands"]
        uses = {}
        for r in mine:
            for x in (r["i"], r["j"]):
                uses[x] = uses.get(x, 0) + 1
        assert max(uses.values()) <= R3.USE_CAP
        for a in range(len(mine)):
            for b in range(a + 1, len(mine)):
                assert not R3.near_dup((c[mine[a]["i"]], c[mine[a]["j"]]), (c[mine[b]["i"]], c[mine[b]["j"]]))


def test_near_dup_rule():
    a = {"cid": "a", "kind": "knob", "model": "M", "knob": "Gain", "delta": 0.1}
    b = {"cid": "b", "kind": "knob", "model": "M", "knob": "Gain", "delta": 0.2}
    c = {"cid": "c", "kind": "knob", "model": "M", "knob": "Gain", "delta": -0.1}
    t = {"cid": "t", "kind": "twin", "model": "X", "knob": None, "delta": None}
    assert R3.near_dup((a, t), (b, t))                 # same direction of the same knob, same partner
    assert not R3.near_dup((a, t), (c, t))             # opposite direction
    assert R3.near_dup((a, t), (t, a))                 # the same pair
    assert not R3.near_dup((a, b), (c, t))


def test_pair_types():
    assert R3.pair_type("knob", "knob") == "knob"
    assert R3.pair_type("knob", "twin") == "twin" and R3.pair_type("twin", "twin") == "twin"
    assert R3.pair_type("twin", "same") == "class" and R3.pair_type("knob", "diff") == "class"


# ------------------------------------------------------------ build, key, levels

def test_trial_mix_and_key(built):
    _, folder, keyf, key = built
    kinds = [t["kind"] for t in key["trials"]]
    assert kinds.count("regular") == 40 and kinds.count("repeat") == 3
    assert kinds.count("sanity_target") == 2 and kinds.count("sanity_near") == 1 and len(kinds) == 46
    assert key["version"] == 3 and "split" in key["selection_rule"].lower() and key["min_share"] == 0.30
    assert keyf.parent != folder and not any(keyf.name in str(p) for p in folder.iterdir())   # key not served
    # served files carry opaque names and no key content
    served = "".join(p.read_text() for p in folder.glob("*.json")) + (folder / "index.html").read_text()
    assert "Scream808" not in served and "mrstft" not in served and "disputed" not in served
    for t in key["trials"]:
        if t["kind"] in ("regular", "repeat"):
            s = t["selection"]
            assert s["kind"] in ("disputed", "agree", "relaxed") and s["pair_type"] in ("knob", "twin", "class")
            assert s["n_A"] + s["n_B"] == sum(v is not None for v in s["votes"].values())
            if s["kind"] == "disputed":
                assert min(s["n_A"], s["n_B"]) / (s["n_A"] + s["n_B"]) >= 0.30
    assert len(key["targets"]) == 10 and all(len(v["pool"]) >= 10 for v in key["targets"].values())


def test_votes_in_key_match_pool_scores_and_sides(built):
    _, _, _, key = built
    for t in key["trials"]:
        if t["kind"] not in ("regular", "repeat"):
            continue
        pool = {c["cid"]: c["scores"] for c in key["targets"][str(t["target"])]["pool"]}
        sa, sb = pool[t["A"]["cid"]], pool[t["B"]["cid"]]
        for n, v in t["selection"]["votes"].items():
            if v is None:
                continue
            assert v == ("A" if sa[n] < sb[n] else "B"), (t["n"], n)


def test_levels_and_originals_recorded(built):
    _, folder, _, key = built
    for t in key["trials"]:
        for r in "TAB":
            x = RS.read_mono(folder / t["files"][r])
            assert abs(RS.lufs(x) + 23.0) < 0.3, (t["n"], r, RS.lufs(x))
            assert float(np.max(np.abs(x))) <= 0.99
        assert np.isfinite(t["A"]["orig_db"]) and np.isfinite(t["B"]["orig_db"]) and np.isfinite(t["target_orig_db"])
    assert np.isclose(key["peak_guard_db"], min(0.0, key["peak_guard_db"]))


def test_sanity_and_retests(built):
    _, folder, _, key = built
    byid = {t["id"]: t for t in key["trials"]}
    pos = {t["id"]: t["n"] for t in key["trials"]}
    for t in key["trials"]:
        if t["kind"] == "sanity_target":
            side = t["expected"]
            other = "B" if side == "A" else "A"
            assert t[side]["role"] == "target_copy" and t[other]["role"] == "candidate"
            a = RS.read_mono(folder / t["files"]["T"])
            b = RS.read_mono(folder / t["files"][side])
            assert np.max(np.abs(a - b)) < 1e-4                                  # an exact copy (16-bit rounding only)
        if t["kind"] == "sanity_near":
            assert {t["A"]["role"], t["B"]["role"]} == {"target_copy", "near_identical"}
        if t["kind"] == "repeat":
            o = byid[t["repeat_of"]]
            assert (t["A"]["cid"], t["B"]["cid"]) == (o["B"]["cid"], o["A"]["cid"])   # sides swapped
            assert pos[t["id"]] > pos[o["id"]] + 3                                     # later in the order
            assert t["target"] == o["target"]
    reps = [t for t in key["trials"] if t["kind"] == "repeat"]
    assert len({t["target"] for t in reps}) == 3


def test_side_randomised(built):
    _, _, _, key = built
    first_is_i = []
    for t in key["trials"]:
        if t["kind"] == "regular":
            a, b = t["A"]["cid"], t["B"]["cid"]
            first_is_i.append(int(a[1:]) < int(b[1:]))
    assert 5 < sum(first_is_i) < 35


# ------------------------------------------------------------ the ratings UI

def test_page_has_prominent_rating_buttons(built):
    _, folder, _, key = built
    page = (folder / "index.html").read_text()
    assert "rating_a" in page and "rating_b" in page and "button.r" in page
    assert "[1,2,3,4,5]" in page                         # a clickable 1-5 button each, not a drop-down
    assert "<select" not in page
    assert page.index("ratingRow(s,\"rating_a\"") < page.index("Which is closer to the Target")   # ratings come first
    assert "too close" in page.lower() or "Too close to call" in page
    assert json.dumps(key["trials"][0]["id"]) in page


JS_HARNESS = r"""
const fs=require('fs');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
class El{constructor(t){this.tag=t;this.children=[];this.dataset={};this.style={};this.className='';this.value='';this.textContent='';}
 appendChild(c){this.children.push(c);return c;} append(){} set innerHTML(v){this._h=v;} get innerHTML(){return this._h||'';}
 querySelectorAll(){return [];} addEventListener(){}
 walk(f){f(this);this.children.forEach(c=>c.walk(f));}}
const els={};
global.document={getElementById:id=>els[id]||(els[id]=new El('div')),createElement:t=>new El(t),querySelectorAll:()=>[]};
const store={};
global.localStorage={getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v;}};
global.URL={createObjectURL:()=>'x'};global.Blob=function(){};global.navigator={};
const fn=new Function(script+';return {payload,TRIALS,render};');
const api=fn();
const root=els['trials'];
function click(match){let hit=null;root.walk(e=>{if(!hit&&match(e))hit=e;});hit.onclick();}
const t0=api.TRIALS[0], t1=api.TRIALS[1];
click(e=>e.dataset.rating==='rating_a:4'&&true);
click(e=>e.dataset.rating==='rating_b:2');
click(e=>e.dataset.choice==='B');
const p=JSON.parse(api.payload());
console.log(JSON.stringify({first:p.answers[0],n:p.answers.length,keys:Object.keys(p.answers[0])}));
"""


def test_answers_json_includes_the_ratings(built, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not available")
    _, folder, _, _ = built
    h = tmp_path / "h.js"
    h.write_text(JS_HARNESS)
    r = subprocess.run(["node", str(h), str(folder / "index.html")], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["n"] == 46
    assert {"trial", "n", "choice", "rating_a", "rating_b", "notes"} <= set(got["keys"])
    a = got["first"]
    assert a["rating_a"] == 4 and a["rating_b"] == 2 and a["choice"] == "B"


# ------------------------------------------------------------ scoring

def fake_scorer_factory(key):
    """Scores = the key's own pool scores, so the analysis can be checked against the stored votes."""
    class S:
        def one(self, trial, role):
            pool = {c["cid"]: c["scores"] for c in key["targets"][str(trial["target"])]["pool"]}
            return dict(pool[trial[role]["cid"]])
    return S()


def answers_following(key, who):
    """who(trial) -> 'A'|'B'|'tie'. Ratings: the chosen side 5, the other 2."""
    out = []
    for t in key["trials"]:
        ch = who(t)
        out.append({"trial": t["id"], "n": t["n"], "choice": ch,
                    "rating_a": 5 if ch == "A" else 2, "rating_b": 5 if ch == "B" else 2, "notes": ""})
    return {"answers": out}


def test_scoring_disputed_pair_type_and_pooled(built):
    _, _, _, key = built
    # the "human" always agrees with the mrstft_v2 vote on regular trials, and ties otherwise
    def who(t):
        if t["kind"] in ("regular", "repeat"):
            return t["selection"]["votes"]["mrstft_v2"] or "tie"
        if t["kind"] == "sanity_target":
            return t["expected"]
        return "tie"
    ans = answers_following(key, who)
    pooled = [("A", {n: 1.0 for n in RS.SCORE_ORDER}, {n: 2.0 for n in RS.SCORE_ORDER}, "fam", 5, 1),
              ("B", {n: 1.0 for n in RS.SCORE_ORDER}, {n: 2.0 for n in RS.SCORE_ORDER}, "fam", 1, 5),
              ("A", {n: 1.0 for n in RS.SCORE_ORDER}, {n: 2.0 for n in RS.SCORE_ORDER}, "fam2", 4, 2),
              ("B", {n: 1.0 for n in RS.SCORE_ORDER}, {n: 2.0 for n in RS.SCORE_ORDER}, "fam2", 2, 4),
              ("A", {n: 3.0 for n in RS.SCORE_ORDER}, {n: 1.0 for n in RS.SCORE_ORDER}, "fam2", 1, 5)]
    res = RS.score_all(key, ans, fake_scorer_factory(key), log=lambda *a: None, pooled_records=pooled)
    v3 = res["v3"]
    assert v3["n_trials"]["disputed"] + v3["n_trials"]["near_threshold"] <= 43
    r = v3["disputed"]["mrstft_v2"]
    assert r["n"] > 0 and r["k"] == r["n"]                    # perfect agreement with the score the human follows
    for n in v3["disputed"]:                                  # disputed trials: no score can win them all
        if n != "mrstft_v2" and v3["disputed"][n]["n"]:
            assert v3["disputed"][n]["rate"] < 1.0
    assert v3["disputed"]["mrstft_v2"]["spearman"]["rho"] is not None and v3["disputed"]["mrstft_v2"]["spearman"]["rho"] > 0.5
    # the main table (v2 report) is still there, with Spearman against the ratings
    assert res["scores"]["mrstft_v2"]["spearman"]["rho"] > 0.3
    # pair types
    pt = v3["by_pair_type"]
    assert set(pt) <= {"knob", "twin", "class"} and sum(d["n"] for d in pt.values()) == 43
    assert all(d["mean_rating"] is not None for d in pt.values())
    # pooled v2 + v3: counts add up
    p = v3["pooled"]["mrstft_v2"]
    assert p["pooled"]["n"] == p["v2"]["n"] + p["v3"]["n"]
    # who won each disputed trial
    assert v3["disputed_trials"] and all("mrstft_v2" in d["agreed"] for d in v3["disputed_trials"] if "agreed" in d)
    txt = RS.fmt(res)
    assert "DISPUTED" in txt and "by pair type" in txt and "Pooled v2 + v3" in txt
    assert res["sanity"] and all(s["correct"] for s in res["sanity"] if s["kind"] == "sanity_target")
    assert all(x["consistent"] for x in res["retest"] if x["consistent"] is not None)
    json.dumps(res, default=float)                            # results.json can be written


def test_scoring_unanswered_is_safe(built):
    _, _, _, key = built
    res = RS.score_all(key, {"answers": []}, fake_scorer_factory(key), log=lambda *a: None)
    assert res["answered"] == 0 and res["v3"]["n_decided"]["all"] == 0
    RS.fmt(res)


def test_excerpts_avoid_v2_windows(takes, tmp_path):
    n = int(1.5 * SR)
    avoid = {1: [int(10 * SR)], 2: [], 3: []}
    ex = R3.choose_excerpts(takes, "20261018-113158_take{n}_dry.wav", R3.TAKE_PLAN, 1.5, np.random.default_rng(0), avoid)
    assert len(ex) == 10
    for take in (1, 2, 3):
        st = sorted(e["start"] for e in ex if e["take"] == take)
        assert all(b - a >= n for a, b in itertools.pairwise(st))
    assert all(abs(e["start"] - 10 * SR) >= n for e in ex if e["take"] == 1)
