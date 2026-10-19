"""T170 human-ratings-v2: ladder generation, hidden key, level recording, schedule, withdrawal. Fake audio and a fake renderer."""
import itertools
import json
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
import rating_v2 as R2

SR = 48000


def fake_render(model, params, dry):
    """Deterministic stand-in: gain from the drive knob, a tilt from the tone knob, a model-specific offset."""
    drive, tones = R2.KNOBS[model]
    g = 1 + 20 * params[drive]
    h = (zlib.crc32(model.encode()) % 1000 + 1) / 1000
    y = np.tanh(g * dry * (0.5 + h)) * (0.2 + 0.1 * sum(params.get(t, 0.5) for t in tones))
    return y.astype(np.float32)


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


def build(tmp_path, takes, name="s", seed=5):
    return R2.build_session(name, seed=seed, render=fake_render, takes_dir=takes, out_root=tmp_path / "pages",
                            key_root=tmp_path / "keys", excerpt_s=3.0, log=lambda *a: None)


# ------------------------------------------------------------ ladder

def test_pools_exclude_novelty_and_time_variant_models():
    assert not (R2.all_models() & R2.EXCLUDED)
    for c in R2.CLASSES:
        assert not (set(R2.models_of(c)) & R2.EXCLUDED)
    try:
        from tonematch.engines import native_library as NL
        tv = NL.time_variant_models()
    except Exception:  # noqa: BLE001 - no cache on this machine
        tv = frozenset()
    assert not (R2.all_models() & tv)
    assert set(R2.CLASSES) == {"ts", "klon", "rat", "ds1", "muff", "gefuzz", "metal", "boost"}


@pytest.mark.parametrize("seed", range(12))
def test_ladder_distances(seed):
    rng = np.random.default_rng(seed)
    for cls in R2.CLASSES:
        lad = R2.make_ladder(cls, rng)
        tm, tg = lad["model"], lad["target"]
        drive, tones = R2.KNOBS[tm]
        assert R2.DRIVE_RANGE[0] <= tg[drive] <= R2.DRIVE_RANGE[1]
        assert all(R2.TONE_RANGE[0] <= tg[t] <= R2.TONE_RANGE[1] for t in tones)
        r = lad["rungs"]
        assert r[1]["model"] == r[2]["model"] == tm
        d1, d2 = R2.knob_distance(r[1]["params"], tg), R2.knob_distance(r[2]["params"], tg)
        assert 0.07 <= d1 <= 0.13 and 0.28 <= d2 <= 0.42, (cls, d1, d2)
        for p in (r[1]["params"], r[2]["params"]):
            assert all(0.0 <= v <= 1.0 for v in p.values())
        assert r[3]["model"] != tm and R2.class_of(r[3]["model"]) == cls
        for key in (4, "4b"):
            assert R2.class_of(r[key]["model"]) != cls
            assert R2.class_of(r[key]["model"]) != "boost" or cls == "boost"
            sd = R2.KNOBS[r[key]["model"]][0]
            assert abs(r[key]["params"][sd] - min(0.95, max(0.1, tg[drive]))) < 0.011     # similar gain
        assert r[3]["params"][R2.KNOBS[r[3]["model"]][0]] == pytest.approx(min(0.95, max(0.1, tg[drive])), abs=0.011)
        assert {k: v["rung"] for k, v in r.items() if k != "3b"} == {1: 1, 2: 2, 3: 3, 4: 4, "4b": 4}


def test_ladder_seeded():
    a = R2.make_ladder("ts", np.random.default_rng(3))
    b = R2.make_ladder("ts", np.random.default_rng(3))
    c = R2.make_ladder("ts", np.random.default_rng(4))
    assert a == b and a != c


def test_pair_schedule_covers_slots_and_respects_clean():
    for seed in range(5):
        has = [True, True, True, False, True, False, True, True]
        b = R2.schedule_pairs(has, np.random.default_rng(seed))
        flat = [p for x in b for p in x]
        assert sorted(flat) == sorted(R2.PAIR_SLOTS) and all(len(x) == 3 and len(set(x)) == 3 for x in b)
        for i, x in enumerate(b):
            if not has[i]:
                assert all(5 not in p for p in x)
    adj = sum(1 for a, c in R2.PAIR_SLOTS if c - a == 1)
    assert adj == 16 and len(R2.PAIR_SLOTS) == 24


def test_excerpts_are_real_playing_and_differ(takes):
    ex = R2.choose_excerpts(takes, rng=np.random.default_rng(1), n_s=3.0)
    assert len(ex) == 8 and {e["take"] for e in ex} == {1, 2, 3}
    assert sum(e["take"] in (1, 2) for e in ex) == 7
    same = [(e["take"], e["start"]) for e in ex]
    assert len(set(same)) == 8
    for take in (1, 2, 3):                                   # no overlap within a take
        s = sorted(e["start"] for e in ex if e["take"] == take)
        assert all(b - a >= 3 * SR for a, b in itertools.pairwise(s))


# ------------------------------------------------------------ session

def test_counts_and_key_hidden(tmp_path, takes):
    folder, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    kinds = [t["kind"] for t in key["trials"]]
    assert kinds.count("regular") == 24 and kinds.count("repeat") == 2
    assert kinds.count("sanity_target") + kinds.count("sanity_near") == 3
    assert len(key["trials"]) == 29 and len(key["targets"]) == 8
    assert {t["class"] for t in key["targets"].values()} == set(R2.CLASSES)
    assert keyf.parent != folder and not (folder / keyf.name).exists()
    page = (folder / "index.html").read_text() + (folder / "trials.json").read_text()
    for banned in ("HD2_", "rung", "Scream", "Minotaur", "orig_db", "seed", "target_copy", "expected", "ts-type",
                   "TS-type", "Klon", "RAT", "Big Muff", "peak_guard"):
        assert banned not in page, banned
    names = {p.name for p in folder.iterdir()}
    assert names - {"index.html", "trials.json"} <= {n for n in names if n.endswith(".wav")}
    assert len(names) == 2 + 29 * 3
    # the key holds the true settings, rung by rung
    t1 = key["targets"]["1"]
    assert t1["model"].startswith("HD2_Dist") and t1["params"] and set(t1["rungs"]) >= {"1", "2", "3", "4"}
    for t in key["trials"]:
        if t["kind"] in ("regular", "repeat"):
            assert t["A"]["rung"] != t["B"]["rung"]
            assert {t["A"]["rung"], t["B"]["rung"]} <= {1, 2, 3, 4, 5}


def test_retest_is_same_pair_sides_swapped_and_after(tmp_path, takes):
    _, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    byid = {t["id"]: t for t in key["trials"]}
    reps = [t for t in key["trials"] if t["kind"] == "repeat"]
    assert len(reps) == 2 and reps[0]["target"] != reps[1]["target"]
    for r in reps:
        o = byid[r["repeat_of"]]
        assert o["n"] < r["n"] and r["A"]["name"] == o["B"]["name"] and r["B"]["name"] == o["A"]["name"]


def test_sanity_trials(tmp_path, takes):
    folder, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    for t in key["trials"]:
        clips = {k: RS.read_mono(folder / t["files"][k]) for k in "TAB"}
        if t["kind"] == "sanity_target":
            side = "A" if t["A"]["role"] == "target_copy" else "B"
            other = "B" if side == "A" else "A"
            assert t["expected"] == side and np.array_equal(clips[side], clips["T"])
            assert t[other]["role"] == "clean" and t[other]["rung"] == 5
        if t["kind"] == "sanity_near":
            assert np.array_equal(clips["A"], clips["T"]) and np.array_equal(clips["B"], clips["T"])


def test_levels_matched_recorded_and_shared_guard(tmp_path, takes):
    folder, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    guard = key["peak_guard_db"]
    assert guard <= 0
    tgt_raw = {}
    for t in key["trials"]:
        clips = {k: RS.read_mono(folder / t["files"][k]) for k in "TAB"}
        ls = [RS.lufs(c) for c in clips.values()]
        assert max(ls) - min(ls) < 0.15, t["n"]
        assert abs(ls[0] - (RS.TARGET_LUFS + guard)) < 0.15
        assert max(np.abs(c).max() for c in clips.values()) <= 0.99
        # every clip of every trial has its original level recorded, and the gain applied is on the record
        assert np.isfinite(t["target_orig_db"]) and np.isfinite(t["A"]["orig_db"]) and np.isfinite(t["B"]["orig_db"])
        assert len(t["loudness"]["gain_db"]) == 3 and len(t["loudness"]["lufs_in"]) == 3
        # level difference between candidate and target survives: orig level difference == -(gain difference) + final level difference
        for side, gi in (("A", 1), ("B", 2)):
            lufs_in_diff = t["loudness"]["lufs_in"][gi] - t["loudness"]["lufs_in"][0]
            assert abs((t[side]["orig_db"] - t["target_orig_db"]) - lufs_in_diff) < 6.0     # RMS dB vs LUFS agree loosely
        tgt_raw.setdefault(t["target"], t["target_orig_db"])
    # orig_db of the target copy equals the target's
    for t in key["trials"]:
        for s in "AB":
            if t[s]["role"] == "target_copy":
                assert t[s]["orig_db"] == pytest.approx(t["target_orig_db"])


def test_orig_db_matches_rendered_rms(tmp_path, takes):
    """The recorded original level is the RMS of the unlevelled render of that setting on the excerpt."""
    _, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    t = next(x for x in key["trials"] if x["kind"] == "regular")
    tg = key["targets"][str(t["target"])]
    x = RS.read_mono(Path(tg["di"]))
    for s in "AB":
        if t[s]["role"] == "candidate":
            y = fake_render(t[s]["model"], t[s]["params"], x)
            assert t[s]["orig_db"] == pytest.approx(RS.rms_db(y), abs=0.05)
    y = fake_render(tg["model"], tg["params"], x)
    assert t["target_orig_db"] == pytest.approx(RS.rms_db(y), abs=0.05)


def test_same_seed_same_key_other_seed_differs(tmp_path, takes):
    sig = lambda k: [(t["kind"], t["A"]["name"], t["B"]["name"]) for t in json.loads(k.read_text())["trials"]]
    _, a = build(tmp_path, takes, "a", 7)
    _, b = build(tmp_path, takes, "b", 7)
    _, c = build(tmp_path, takes, "c", 8)
    assert sig(a) == sig(b) != sig(c)


def test_failed_rung_uses_spare(tmp_path, takes):
    calls = {"n": 0}

    def flaky(model, params, dry):
        calls["n"] += 1
        return np.zeros_like(dry) if calls["n"] == 4 else fake_render(model, params, dry)

    _, keyf = R2.build_session("f", seed=2, render=flaky, takes_dir=takes, out_root=tmp_path / "p",
                                    key_root=tmp_path / "k", excerpt_s=3.0, log=lambda *a: None)
    assert json.loads(keyf.read_text())["trials"]


def test_scoring_ladder_report_on_fake_answers(tmp_path, takes):
    _, keyf = build(tmp_path, takes)
    key = json.loads(keyf.read_text())
    byid = {}
    for t in key["trials"]:
        if t["kind"] in ("regular", "repeat"):
            lo = "A" if t["A"]["rung"] < t["B"]["rung"] else "B"
            byid[t["id"]] = {"choice": lo, "rating_a": 3, "rating_b": 2}
    regs = [t for t in key["trials"] if t["kind"] in ("regular", "repeat")]
    rep = RS.ladder_report(regs, byid)
    assert rep["overall"]["rate"] == 1.0 and rep["overall"]["n"] == 26
    assert set(rep["by_pair"]) <= {"1 vs 2", "2 vs 3", "3 vs 4", "4 vs 5", "1 vs 3", "2 vs 4", "3 vs 5"}
    assert len(rep["by_class"]) == 8


def test_withdraw_v1_deletes_audio_and_leaves_notice(tmp_path):
    f = tmp_path / "human-ratings-v1"
    f.mkdir()
    for n in ("a_t.wav", "a_a.wav", "trials.json", "index.html"):
        (f / n).write_text("x")
    assert R2.withdraw("human-ratings-v1", tmp_path) == 3
    assert sorted(p.name for p in f.iterdir()) == ["index.html"]
    assert "WITHDRAWN" in (f / "index.html").read_text()
