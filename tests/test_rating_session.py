"""T170 human rating session: hidden key, randomisation, LUFS matching, scoring, agreement, playback. Fake audio only."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import rating_session as RS

SR = 48000
PLAN = {"mother-drive": 4, "mother-amp": 2, "ds-sample": 2}


def tone(seed, amp, secs=9.0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(secs * SR)) / SR
    x = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) for f in (110 * (1 + seed % 3), 220, 330 + seed))
    return (amp * np.tanh(x * (0.5 + seed % 4)) / 3).astype(np.float32)


def w(p, x):
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), x, SR, subtype="FLOAT")


@pytest.fixture()
def matches(tmp_path):
    m = tmp_path / "matches"
    for d, fam_seed in (("Mother-0-68s", 1), ("DS-Sample", 5)):
        L = m / d / "listen"
        w(L / "target_stem.wav", tone(fam_seed, 0.3))
        w(L / "di_dry.wav", tone(fam_seed + 50, 0.2))
        gains = {}
        for i, nm in enumerate(["match", "match_rank2", "match_rank3", "start_unmatched"]):
            w(L / f"{nm}.wav", tone(fam_seed + 10 + i, 0.2 + 0.05 * i))
            gains[nm] = -3.0 * i
        gains["target_stem"] = 0.0
        (L / "gains.json").write_text(json.dumps({"gains_db": gains}))
    D = m / "Mother-0-68s-drive" / "listen"
    w(D / "target_stem.wav", tone(1, 0.3))
    w(D / "seq14_match.wav", tone(31, 0.3))
    w(D / "seq12h_match.wav", tone(32, 0.25))
    for i in range(4):
        w(m / "Mother-0-68s-drive" / "listen_t170" / f"raw{i}.wav", tone(40 + i, 0.1 + 0.1 * i))
    return m


def stub_prelim(t, c):
    return {"mrstft_v2": float(np.std(c)), "kit": float(np.abs(c).max())}


def build(tmp_path, matches, name="s", seed=3, **kw):
    return RS.build_session(name, seed=seed, plan=PLAN, matches=matches, out_root=tmp_path / "pages",
                            key_root=tmp_path / "keys", excerpt_s=3.0, repeats=1, prelim=stub_prelim, **kw)


def test_discover_levels_and_dedupe(matches):
    fams = RS.discover(matches)
    assert set(fams) == {"mother-amp", "mother-drive", "ds-sample"}
    amp = {c["name"]: c for c in fams["mother-amp"]["cands"]}
    # original level = listening-pack level minus the recorded gain; unknown when no gains.json
    x = RS.read_mono(amp["match_rank2"]["path"])
    assert amp["match_rank2"]["orig_db"] == pytest.approx(RS.rms_db(x) + 3.0)
    drive = {c["name"]: c for c in fams["mother-drive"]["cands"]}
    assert drive["seq14_match"]["orig_db"] is None and drive["raw2"]["orig_db"] is not None
    # an exact duplicate clip is dropped
    w(matches / "DS-Sample_run9" / "listen" / "target_stem.wav", RS.read_mono(matches / "DS-Sample/listen/target_stem.wav"))
    w(matches / "DS-Sample_run9" / "listen" / "match.wav", RS.read_mono(matches / "DS-Sample/listen/match.wav"))
    n = [c["name"] for c in RS.discover(matches)["ds-sample"]["cands"]]
    assert not any(x.startswith("run9/") for x in n)


def test_key_hidden_from_page_folder(tmp_path, matches):
    folder, keyf = build(tmp_path, matches)
    assert keyf.parent != folder and not (folder / keyf.name).exists()
    assert str(folder) not in str(keyf.parent)
    page = (folder / "index.html").read_text()
    man = (folder / "trials.json").read_text()
    for banned in ("target_copy", "expected", "seq14", "match_rank", "start_unmatched", "clean DI", "orig_db", "sanity",
                   "repeat", "\"seed\"", "mother", "ds-sample"):
        assert banned not in page and banned not in man
    names = sorted(p.name for p in folder.iterdir())
    assert set(names) - {"index.html", "trials.json"} <= {n for n in names if n.endswith(".wav")}
    assert all(len(n.split("_")[0]) == 8 for n in names if n.endswith(".wav"))
    key = json.loads(keyf.read_text())
    assert key["seed"] == 3 and len(key["trials"]) == sum(PLAN.values()) + 1 + 3


def test_randomisation_seeded_and_balanced(tmp_path, matches):
    _, k1 = build(tmp_path, matches, "a", 3)
    _, k2 = build(tmp_path, matches, "b", 3)
    _, k3 = build(tmp_path, matches, "c", 4)
    sig = lambda k: [(t["kind"], t["A"]["name"], t["B"]["name"]) for t in json.loads(k.read_text())["trials"]]
    assert sig(k1) == sig(k2) != sig(k3)
    # A/B side is random: over many seeds the candidate-listed-first lands on A about half the time
    firsts = 0
    for s in range(10, 18):
        _, k = build(tmp_path, matches, f"r{s}", s)
        firsts += sum(t["A"]["role"] == "target_copy" for t in json.loads(k.read_text())["trials"] if t["kind"] == "sanity_target")
    assert 3 <= firsts <= 13           # 16 sanity trials, side 50/50


def test_repeat_follows_original_with_sides_swapped(tmp_path, matches):
    _, keyf = build(tmp_path, matches)
    tr = json.loads(keyf.read_text())["trials"]
    byid = {t["id"]: t for t in tr}
    reps = [t for t in tr if t["kind"] == "repeat"]
    assert len(reps) == 1
    r = reps[0]
    o = byid[r["repeat_of"]]
    assert o["n"] < r["n"]
    assert {r["A"]["name"], r["B"]["name"]} == {o["A"]["name"], o["B"]["name"]}
    assert r["excerpt_start_s"] != o["excerpt_start_s"]


def test_loudness_matched_and_target_copy_identical(tmp_path, matches):
    folder, keyf = build(tmp_path, matches)
    key = json.loads(keyf.read_text())
    for t in key["trials"]:
        clips = {k: RS.read_mono(folder / t["files"][k]) for k in "TAB"}
        ls = [RS.lufs(c) for c in clips.values()]
        assert max(ls) - min(ls) < 0.15, t["n"]                 # PCM16 rounding only
        assert max(np.abs(c).max() for c in clips.values()) <= 0.99
        if t["kind"] == "sanity_target":
            side = "A" if t["A"]["role"] == "target_copy" else "B"
            assert t["expected"] == side
            assert np.array_equal(clips[side], clips["T"])
        if t["kind"] == "sanity_near":
            other = clips["B"] if t["A"]["role"] == "target_copy" else clips["A"]
            assert 0 < np.abs(other - clips["T"]).max() < 0.02


def test_match_set_peak_guard_keeps_set_matched():
    quiet = (0.001 * np.sin(np.arange(2 * SR) / 20)).astype(np.float32)
    peaky = (0.2 * np.sin(np.arange(2 * SR) / 20)).astype(np.float32)
    peaky[100] = 1.0
    out, info = RS.match_set([quiet, peaky], target_lufs=-3.0)
    assert abs(info["lufs_out"][0] - info["lufs_out"][1]) < 0.1
    assert max(np.abs(o).max() for o in out) <= 0.99


def test_agreement_and_ci():
    rows = [("A", 1.0, 2.0), ("B", 3.0, 1.0), ("A", 2.0, 1.0), ("tie", 1.0, 2.0), ("A", 1.0, 1.0), ("B", np.nan, 1.0)]
    r = RS.agreement(rows)
    assert (r["n"], r["k"], r["skipped"]) == (3, 2, 3)       # tie, tied score and nan are skipped
    assert r["rate"] == pytest.approx(2 / 3) and r["ci"][0] < 2 / 3 < r["ci"][1]
    allr = RS.agreement([("A", 0.0, 1.0)] * 10)
    assert allr["rate"] == 1.0 and allr["ci"][1] == 1.0 and 0.65 < allr["ci"][0] < 0.70 and allr["p"] < 0.01
    assert RS.agreement([])["rate"] is None
    lo, hi = RS.clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-3) and hi == pytest.approx(0.8129, abs=1e-3)


def test_spearman_sign_means_agreement():
    obs = [("f", s, 5 - i) for i, s in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])]    # low score <-> high rating
    assert RS.spearman(obs)["rho"] == pytest.approx(1.0)
    flipped = [(f, -s, r) for f, s, r in obs]
    assert RS.spearman(flipped)["rho"] == pytest.approx(-1.0)
    assert RS.spearman([("f", 1.0, None)] * 6)["rho"] is None


class Oracle:
    """A scorer that knows the key: distance 0 for the clip the 'human' picks, 1 for the other."""

    def __init__(self, pref):
        self.pref = pref

    def one(self, trial, role):
        good = self.pref(trial)
        return {"oracle": 0.0 if role == good else 1.0, "anti": 1.0 if role == good else 0.0}


def test_score_all_with_known_answers(tmp_path, matches, monkeypatch):
    _, keyf = build(tmp_path, matches)
    key = json.loads(keyf.read_text())
    # the 'human' always prefers the target copy / the candidate with the larger gain-matched level name order
    def pref(t):
        if t["kind"] == "sanity_target":
            return t["expected"]
        return "A" if t["A"]["name"] < t["B"]["name"] else "B"
    answers = {"answers": []}
    for t in key["trials"]:
        ch = "tie" if t["kind"] == "sanity_near" else pref(t)
        answers["answers"].append({"trial": t["id"], "choice": ch, "rating_a": 5 if ch == "A" else 2,
                                   "rating_b": 5 if ch == "B" else 2})
    monkeypatch.setattr(RS, "SCORE_ORDER", ["oracle", "anti"])
    res = RS.score_all(key, answers, Oracle(pref), log=lambda *_: None)
    assert res["answered"] == len(key["trials"])
    assert res["scores"]["oracle"]["rate"] == 1.0 and res["scores"]["anti"]["rate"] == 0.0
    assert res["scores"]["oracle"]["spearman"]["rho"] > 0.8 and res["scores"]["anti"]["spearman"]["rho"] < -0.8
    assert all(s["correct"] for s in res["sanity"] if s["kind"] == "sanity_target")
    assert [s["called_tie"] for s in res["sanity"] if s["kind"] == "sanity_near"] == [True]
    assert all(r["consistent"] for r in res["retest"]) and len(res["retest"]) == 1
    txt = RS.fmt(res)
    assert "admits and rejects nothing" in txt and "oracle" in txt
    rows = []
    RS.write_ledger(res, lambda ds, metric, v, note: rows.append((ds, metric, v)))
    assert ("ab-s", "agree_oracle", 1.0) in rows and ("ab-s", "sanity_correct", 1.0) in rows


def test_unanswered_trials_are_not_counted(tmp_path, matches):
    _, keyf = build(tmp_path, matches)
    key = json.loads(keyf.read_text())
    res = RS.score_all(key, {"answers": []}, Oracle(lambda t: "A"), log=lambda *_: None)
    assert res["answered"] == 0 and res["decided"] == 0
    assert res["scores"] == {} or all(r["n"] == 0 for r in res["scores"].values())


class FakeSD:
    def __init__(self):
        self.calls = []

    def play(self, buf, sr, device=None, mapping=None):
        self.calls.append((buf.copy(), sr, device, mapping))

    def wait(self):
        pass

    def stop(self):
        pass


def test_play_trial_order_channels_and_gain(tmp_path, matches):
    folder, _ = build(tmp_path, matches)
    sd = FakeSD()
    out = RS.play_trial("s", 2, gain_db=-6.0, which="TAB", gap_s=0, out_root=tmp_path / "pages", sd=sd, device=7, say=lambda *_: None)
    assert out == ["T", "A", "B"] and len(sd.calls) == 3
    buf, sr, dev, mapping = sd.calls[0]
    man = json.loads((folder / "trials.json").read_text())
    ref = RS.read_mono(folder / man["trials"][1]["files"]["T"])
    assert (sr, dev, mapping) == (SR, 7, [1, 2]) and buf.shape == (len(ref), 2)
    assert np.allclose(buf[:, 0], ref * 10 ** (-6 / 20)) and np.array_equal(buf[:, 0], buf[:, 1])
    assert RS.play_trial("s", 2, which="B", gap_s=0, out_root=tmp_path / "pages", sd=FakeSD(), device=1, say=lambda *_: None) == ["B"]
    with pytest.raises(SystemExit):
        RS.play_trial("s", 2, gain_db=40.0, gap_s=0, out_root=tmp_path / "pages", sd=FakeSD(), device=1, say=lambda *_: None)
    with pytest.raises(SystemExit):
        RS.play_trial("s", 99, out_root=tmp_path / "pages", sd=FakeSD(), device=1)


def test_find_answers(tmp_path):
    assert RS.find_answers("s", tmp_path / "r", tmp_path / "dl") is None
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "s.answers (1).json").write_text("{}")
    assert RS.find_answers("s", tmp_path / "r", tmp_path / "dl").name == "s.answers (1).json"
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "s.answers.json").write_text("{}")
    assert RS.find_answers("s", tmp_path / "r", tmp_path / "dl") == tmp_path / "r" / "s.answers.json"
