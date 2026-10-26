"""rating_rank (T223): plan, pack, hidden key, blind layout and the pre-registered analysis, on fake runs and renders."""
import json
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rating_rank as RR

SR = RR.SR
FAKE_SONGS = [s for s in RR.SONGS_ALL if s != RR.EXCLUDED_UP_FRONT]


def fake_render(model, values, dry):
    g = 1 + (zlib.crc32((model + json.dumps(values, sort_keys=True)).encode()) % 1000) / 100
    return np.tanh(np.asarray(dry) * g).astype(np.float32)


def make_runs(root: Path):
    rng = np.random.default_rng(0)
    for s in FAKE_SONGS:
        run = root / f"{s}-dryrebuild-prs7"
        (run / "listen").mkdir(parents=True)
        sf.write(str(run / "listen" / "di_search.wav"), (0.1 * rng.standard_normal(SR * 2)).astype(np.float32), SR, subtype="FLOAT")
        sf.write(str(run / "listen" / "target_stem.wav"), (0.1 * rng.standard_normal(SR * 2)).astype(np.float32), SR, subtype="FLOAT")
        ranked = [{"model": f"M{i}", "distance": 0.4 + 0.05 * i + 0.001 * len(s)} for i in range(8)]
        rows = [{"model": f"M{i}", "values": {"Drive": 0.1 * i}, "screen": 0, "refined": None} for i in range(8)]
        (run / "result.json").write_text(json.dumps({"window": {"start_s": 0}, "shortlist": {"ranked": ranked, "rows": rows}}))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("rr")
    make_runs(d / "runs")
    f, k = RR.build_session("t", seed=5, render=fake_render, songs=RR.choose_songs(), runs=d / "runs",
                            out_root=d / "pages", key_root=d / "key", log=lambda *a: None)
    return f, json.loads(k.read_text()), d


def test_song_draw_is_fixed_and_excludes_ds_sample():
    s = RR.choose_songs()
    assert len(s) == 5 and "ds-sample" not in s and s == RR.choose_songs()


def test_design_counts(built):
    _, key, _ = built
    tr = key["trials"]
    rank = [t for t in tr if t["type"] == "rank"]
    pair = [t for t in tr if t["type"] == "pair"]
    assert len(rank) == 8 and sum(t["kind"] == "repeat" for t in rank) == 3
    assert sum(t["kind"] == "regular" for t in pair) == 25 and sum(t["kind"] == "repeat" for t in pair) == 6
    assert {t["kind"] for t in pair} == {"regular", "repeat", "sanity_target", "sanity_near"}
    assert sum(t["kind"].startswith("sanity") for t in pair) == 2
    assert sum(t["kind"] in ("repeat",) for t in tr) >= 6
    for s in key["songs"]:
        cls = [(min(t["sides"]["A"]["afx_rank"], t["sides"]["B"]["afx_rank"]), max(t["sides"]["A"]["afx_rank"], t["sides"]["B"]["afx_rank"]))
               for t in pair if t["song"] == s and t["kind"] == "regular"]
        assert sorted(cls) == [(1, 2), (1, 5), (2, 3), (3, 4), (4, 5)]


def test_order_and_repeat_constraints(built):
    _, key, _ = built
    tr = key["trials"]
    pos = {t["id"]: t["n"] for t in tr}
    for t in tr:
        if t["kind"] == "repeat":
            assert pos[t["id"]] - pos[t["repeat_of"]] >= 6
    assert all(tr[i]["song"] != tr[i + 1]["song"] for i in range(len(tr) - 1))


def test_repeat_pairs_swap_sides_and_ranks_reshuffle(built):
    _, key, _ = built
    by = {t["id"]: t for t in key["trials"]}
    for t in key["trials"]:
        if t["kind"] != "repeat":
            continue
        o = by[t["repeat_of"]]
        if t["type"] == "pair":
            assert t["sides"]["A"]["afx_rank"] == o["sides"]["B"]["afx_rank"]
        else:
            assert {k: v["afx_rank"] for k, v in t["letters"].items()} != {k: v["afx_rank"] for k, v in o["letters"].items()}


def test_blind_page_and_files(built):
    f, key, _ = built
    man = json.loads((f / "trials.json").read_text())
    blob = json.dumps(man["trials"]) + (f / "index.html").read_text()
    for banned in ("distance", "afx_rank", "Distortion", "M3", "model", "values"):
        assert banned not in blob, banned
    assert not (f / "x.key.json").exists() and not list(f.glob("*key*"))
    for t in man["trials"]:
        for fn in t["files"].values():
            assert (f / fn).exists()
    assert key["preregistration_sha256"] == man["preregistration_sha256"] == RR.PREREG_SHA256


def test_levels_matched_per_song(built):
    f, key, _ = built
    by_song: dict = {}
    for t in key["trials"]:
        for fn in t["files"].values():
            by_song.setdefault(t["song"], []).append(RR.RS.lufs(RR.RS.read_mono(f / fn)))
    for v in by_song.values():
        non_noise = [x for x in v if x > -69]
        assert max(non_noise) - min(non_noise) < 0.6        # fake tanh clips: all match to -23 within rounding / peak guard


def simulate(key, mode):
    """Fake human: 'afx' agrees with the AFx order, 'reverse' disagrees, 'flat' calls everything the same."""
    out = []
    for t in key["trials"]:
        a = {"trial": t["id"], "n": t["n"], "type": t["type"], "choice": None, "ranks": None, "notes": ""}
        if t["type"] == "rank":
            if mode == "flat":
                a["ranks"] = {l: 3 for l in RR.LETTERS}
            else:
                a["ranks"] = {l: (v["afx_rank"] if mode == "afx" else 6 - v["afx_rank"]) for l, v in t["letters"].items()}
        else:
            if t["kind"] == "sanity_target":
                a["choice"] = t["expected"]
            elif t["kind"] == "sanity_near" or mode == "flat":
                a["choice"] = "tie"
            else:
                lo = "A" if t["sides"]["A"]["distance"] < t["sides"]["B"]["distance"] else "B"
                hi = "B" if lo == "A" else "A"
                a["choice"] = lo if mode == "afx" else hi
        out.append(a)
    return {"session": "t", "answers": out}


def test_analysis_agreeing_listener(built):
    _, key, _ = built
    r = RR.analyse(key, simulate(key, "afx"), draws=500)
    assert r["ranking"]["pooled_mean_rho"] == pytest.approx(1.0)
    assert r["ranking"]["songs_rho_positive"] == 5 and r["ranking"]["afx_top1_is_human_closest"] == 5
    assert r["ranking"]["p_one_sided"] < 0.01
    assert r["pairs"]["all"]["decided"] == 25 and r["pairs"]["all"]["agree"] == 25 and r["pairs"]["all"]["p_two_sided"] < 1e-6
    assert r["retest"]["pairs"]["rate"] == 1.0 and r["retest"]["rank_rho_mean"] == pytest.approx(1.0)
    assert r["session_reliable"] and "pooled mean rho" in RR.fmt(r)


def test_analysis_reversed_and_flat(built):
    _, key, _ = built
    r = RR.analyse(key, simulate(key, "reverse"), draws=200)
    assert r["ranking"]["pooled_mean_rho"] == pytest.approx(-1.0) and r["pairs"]["all"]["agree"] == 0
    r = RR.analyse(key, simulate(key, "flat"), draws=200)
    assert len(r["ranking"]["undecided_songs"]) == 5 and r["pairs"]["all"]["decided"] == 0 and r["pairs"]["all"]["ties"] == 25
    assert r["retest"]["pairs"]["rate"] == 1.0 and r["retest"]["pairs"]["decided_both"] == 0
    RR.fmt(r)


def test_tie_once_is_inconsistent(built):
    _, key, _ = built
    ans = simulate(key, "afx")
    by = {t["id"]: t for t in key["trials"]}
    rep = next(a for a in ans["answers"] if by[a["trial"]]["kind"] == "repeat" and by[a["trial"]]["type"] == "pair")
    rep["choice"] = "tie"
    r = RR.analyse(key, ans, draws=50)
    assert r["retest"]["pairs"]["consistent"] == 5 and r["retest"]["pairs"]["n"] == 6 and r["retest"]["pairs"]["decided_both"] == 5


def test_prereg_mentions_the_rules():
    for w in ("Spearman", "Test-retest", "Tie rule", "223", "ds-sample", "30+"):
        assert w in RR.PREREG
