"""gate3_set (T223): plan rules (P3 only, 2 per model, determinism), scoring maths and the refusal to score unlaunched."""
import itertools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gate3_set as G

SR = G.SR
FREE = [("Drive", 0.0, 1.0, 0.5), ("Tone", 0.0, 1.0, 0.5), ("Level", 0.0, 1.0, 0.5)]


def fake_inputs(n_models=6):
    pool = {f"M{i}": FREE for i in range(n_models)}
    knobs = {f"M{i}": ("Drive", "Tone") for i in range(n_models)}
    starts = {G.p3_path(n): list(range(0, 40 * SR, SR // 2)) for n in ("01", "04", "05")}
    return pool, knobs, starts


def test_plan_rules():
    pool, knobs, starts = fake_inputs()
    items = G.make_plan(starts, pool, knobs, {"Level"}, seed=1)
    assert len(items) == 12 and [i["id"] for i in items] == [f"g3-{k:03d}" for k in range(1, 13)]
    for m in pool:
        mine = [i for i in items if i["model"] == m]
        assert len(mine) == 2 and mine[0]["di_file"] != mine[1]["di_file"]
    assert all("/P3_music/" in i["di_file"] and i["player"] == "P3" for i in items)
    assert all("Level" not in i["cc"] and set(i["cc"]) == {"Drive", "Tone"} for i in items)
    assert all(0 <= v <= 127 for i in items for v in i["cc"].values())
    for f in starts:
        s = sorted(i["start"] for i in items if i["di_file"] == f)
        assert all(b - a >= 5 * SR for a, b in itertools.pairwise(s))
    assert items == G.make_plan(starts, pool, knobs, {"Level"}, seed=1)
    assert items != G.make_plan(starts, pool, knobs, {"Level"}, seed=2)


def test_plan_refuses_other_players():
    pool, knobs, _ = fake_inputs()
    with pytest.raises(ValueError):
        G.make_plan({"Guitar-TECHS/P1_chords/audio/directinput/x.wav": [0]}, pool, knobs, {"Level"})


def test_reserve_files_are_not_used_by_generate_source():
    src = (ROOT / "scripts" / "gate3_set.py").read_text()
    assert '("01", "04", "05", "06", "07", "09", "10", "11", "12")' in src
    assert set(G.RESERVE) == {"02", "03", "08"}


def labels(n=20):
    out = []
    for k in range(n):
        out.append({"id": f"g3-{k + 1:03d}", "model": f"M{k % 5}", "cc": {"Drive": 64, "Tone": 10}, "drive_knob": "Drive",
                    "norm": {"Drive": 0.5, "Tone": 0.2}, "ranges": {"Drive": [0, 1], "Tone": [0, 1]}})
    return out


def good_pred(lab, drive=0.5):
    return {"ranked": [lab["model"], "X", "Y"], "values": {"Drive": drive, "Tone": 0.2}}


def test_score_perfect_and_baselines():
    labs = labels()
    preds = {lab["id"]: good_pred(lab) for lab in labs}
    r = G.score(labs, preds)
    assert r["method"]["top1"] == 1.0 and r["method"]["top3"] == 1.0 and r["method"]["drive_ok"] == 1.0 and r["method"]["knob_share"] == 1.0
    assert r["baseline_true_model_mid_knobs"]["top1"] == 1.0 and r["baseline_true_model_mid_knobs"]["drive_ok"] == 1.0
    assert r["baseline_random_model"]["top1"] < 0.5
    assert r["paired_vs_random"]["top1"]["diff"] > 0.5 and r["paired_vs_random"]["top1"]["p"] < 0.05
    assert all(r["against_proposed_bars"]["levels"].values())
    assert "NOT APPROVED" in r["against_proposed_bars"]["bars_status"]


def test_score_knob_tolerance_and_missing():
    labs = labels(10)
    preds = {lab["id"]: good_pred(lab, drive=0.5 + (0.2 if k % 2 else 0.1)) for k, lab in enumerate(labs)}
    del preds[labs[0]["id"]]
    r = G.score(labs, preds)
    assert r["method"]["missing"] == 1 and r["method"]["top1"] == 0.9
    assert r["method"]["n_right"] == 9
    assert r["method"]["drive_ok"] == pytest.approx(4 / 9)       # +0.1 within 0.15 (4 items); +0.2 outside (5 items)


def test_mcnemar_exact():
    assert G.mcnemar(0, 0) == 1.0
    assert G.mcnemar(5, 5) == 1.0
    assert G.mcnemar(10, 0) == pytest.approx(2 / 1024)
    assert G.mcnemar(21, 9) < 0.05


def test_score_refuses_without_launch(tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"items": {}}))
    rc = G.main(["--out", str(tmp_path), "score", "--predictions", str(p)])
    assert rc == 2 and "refusing" in capsys.readouterr().out


def test_p3_not_in_any_training_plan():
    """The drive-dataset-v1 plan and the slot-dataset plans never touch P3 (the Gate 3 reserve)."""
    import glob
    plans = glob.glob(str(ROOT / "cache" / "*" / "plan.json")) + glob.glob(str(ROOT / "cache" / "*" / "*" / "plan.json"))
    if not plans:
        pytest.skip("no plans on disk")
    for p in plans:
        txt = Path(p).read_text()
        assert "P3_music" not in txt, p
