"""Blind A/B session tool: loudness matching, hidden-key randomisation, scoring. Offline."""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import ab_session as AB

SR = 48000


def tone(f, secs, amp):
    t = np.arange(int(secs * SR)) / SR
    x = (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)
    return np.stack([x, x])


def test_loudness_match_and_trim():
    x, y = AB.trim_pair(tone(220, 5, 0.05), tone(330, 4, 0.4), 3.0)
    assert x.shape == y.shape and x.shape[1] == 3 * SR
    x, y, info = AB.match_loudness(x, y)
    assert abs(info["lufs_out"][0] - info["lufs_out"][1]) < 0.1
    assert abs(info["lufs_out"][0] - AB.TARGET_LUFS) < 0.5
    assert np.abs(x).max() <= 0.99 and np.abs(y).max() <= 0.99


def test_loudness_match_peak_guard_keeps_pair_matched():
    x, y = np.clip(tone(220, 2, 3.0), -1, 1) * 0.0 + tone(220, 2, 0.001), tone(220, 2, 0.9)
    y[:, 100] = 1.0  # very peaky vs quiet
    xo, yo, info = AB.match_loudness(x, y, target=-3.0)
    assert abs(info["lufs_out"][0] - info["lufs_out"][1]) < 0.1
    assert np.abs(xo).max() <= 0.99 and np.abs(yo).max() <= 0.99


def test_assignments_seeded_and_balanced():
    a1, a2 = AB.make_assignments(40, 3, 7), AB.make_assignments(40, 3, 7)
    assert a1 == a2 and a1 != AB.make_assignments(40, 3, 8)
    assert 8 < sum(a1["x_is_a"]) < 32 and sorted(a1["order"]) == list(range(40))
    assert len(a1["abx"]) == 3


def _session(tmp_path, abx=2):
    pairs = []
    for i in range(4):
        fx, fy = tmp_path / f"x{i}.wav", tmp_path / f"y{i}.wav"
        sf.write(str(fx), tone(200 + i, 3, 0.2).T, SR)
        sf.write(str(fy), tone(300 + i, 3, 0.1).T, SR)
        pairs.append({"label": f"p{i}", "x": fx, "y": fy})
    return AB.build_session("t", pairs, seed=3, excerpt_s=2.0, abx=abx, out_dir=tmp_path / "ab")


def test_key_hidden_from_page(tmp_path):
    folder, keyf = _session(tmp_path)
    key = json.loads(keyf.read_text())
    assert keyf.parent == folder.parent and not (folder / keyf.name).exists()
    page = (folder / "index.html").read_text()
    for banned in ("x_is_a", "x_is", "seed", "recovered", "target", "\"x0.wav", "\"y0.wav"):
        assert banned not in page
    names = {p.name for p in folder.iterdir()}
    assert not any(n.startswith(("x0", "y0")) for n in names)
    # the A clip of a pair really is X's audio when the key says so
    p = key["pairs"][0]
    a, _ = sf.read(str(folder / f"{p['tag']}_a.wav"))
    f = np.argmax(np.abs(np.fft.rfft(a[:, 0])))  # bins at 0.5 Hz for 2 s
    assert abs(f / 2 - (200 if p["x_is_a"] else 300)) < 5 + 3


def test_score_known_answers(tmp_path):
    _, keyf = _session(tmp_path)
    key = json.loads(keyf.read_text())
    answers = []
    for tid, i in key["trial_pair"].items():
        xa = key["pairs"][i]["x_is_a"]
        if i < 3:
            answers.append({"trial": tid, "choice": "A" if xa else "B"})  # picks X
        else:
            answers.append({"trial": tid, "choice": "same"})
    for k in key["abx"]:
        answers.append({"trial": k["trial"], "answer": k["x_is"]})
    res = AB.score_answers(key, {"answers": answers})
    assert (res["x_chosen"], res["y_chosen"], res["same"]) == (3, 0, 1)
    assert res["pref_rate_x"] == 1.0 and abs(res["binom_p"] - 0.25) < 1e-9
    assert res["abx_acc"] == 1.0 and res["abx_n"] == 2
    assert all(p["chose"] == "x" for p in res["per_pair"] if p["pair"] < 3)


def test_binomial_and_unanswered():
    assert abs(AB.binom_two_sided(5, 10) - 1.0) < 1e-9
    assert abs(AB.binom_two_sided(9, 10) - 22 / 1024) < 1e-9
    assert AB.binom_two_sided(0, 0) == 1.0


def test_demo_without_audio(tmp_path, capsys):
    assert AB.find_demo_pairs(tmp_path) == []
    assert AB.main(["gate2-demo", "--runs", str(tmp_path)]) == 0
    assert "Nothing was rendered" in capsys.readouterr().out
