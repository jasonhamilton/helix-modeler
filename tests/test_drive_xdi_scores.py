"""T175 different-guitar test on drive: plan building and statistics of scripts/drive_xdi_scores.py.
Synthetic plans and distances only; no cached renders, no engine."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import drive_xdi_scores as X


def _cal(models, n_set=25, seed=0):
    rng = np.random.default_rng(seed)
    dis = [{"path": f"Guitar-TECHS/P{1 + (d % 2)}_x/audio/directinput/f{d}.wav"} for d in range(8)]
    return {"dis": dis, "models": {m: {"settings": [{"Gain": int(rng.integers(128)), "Tone": int(rng.integers(128)),
                                                      "Level": int(rng.integers(128))} for _ in range(n_set)]}
                                   for m in models}}


def test_knob_move_ignores_level_knobs():
    assert X.knob_move({"Gain": 64, "Level": 0}, {"Gain": 64, "Level": 127}) == 0.0
    assert X.knob_type(0.0) is None                      # same sound once loudness is matched
    assert X.knob_type(0.1) == "knob_small"
    assert X.knob_type(0.3) == "knob_medium"
    assert X.knob_type(0.5) == "knob_large"
    assert X.knob_move({"Gain": 127, "Tone": 0}, {"Gain": 0, "Tone": 0}) == pytest.approx(1.0)


def test_player_from_path():
    assert X.player("Guitar-TECHS/P2_chords/audio/directinput/a.wav") == "P2"
    assert X.di_condition(["P1", "P1", "P2"], 0, 0) == "same_di"
    assert X.di_condition(["P1", "P1", "P2"], 0, 1) == "same_player"
    assert X.di_condition(["P1", "P1", "P2"], 0, 2) == "other_player"


def test_plan_candidate_types():
    models = ["HD2_DistScream808", "HD2_DistStuporOD", "HD2_DistMinotaur", "HD2_DistKWB", "HD2_DistTriangleFuzz",
              "HD2_DistRamsHead", "HD2_DistKinkyBoost", "HD2_DistVerminDist"]
    plan = X.build_plan(_cal(models), models, seed=1)
    assert len(plan["targets"]) == len(models) * X.N_TARGET_SETTINGS
    for t in plan["targets"]:
        c0 = t["cands"][0]
        assert c0 == [t["model"], t["setting"], "true"]
        assert 0 <= t["di"] < 8
        for m, j, typ in t["cands"][1:]:
            g, f = X.CIRCUIT[m]
            if typ.startswith("knob"):
                assert m == t["model"] and j != t["setting"]
            elif typ == "twin":
                assert m != t["model"] and g == t["group"]
            elif typ == "same_class":
                assert f == t["family"] and g != t["group"]
            else:
                assert typ == "diff_class" and f != t["family"]
    sc = [t for t in plan["targets"] if t["model"] == "HD2_DistScream808"][0]
    assert sum(c[2] == "twin" for c in sc["cands"]) == 25          # every setting of Stupor OD
    vm = [t for t in plan["targets"] if t["model"] == "HD2_DistVerminDist"][0]
    assert not any(c[2] == "twin" for c in vm["cands"])            # no train twin


def test_plan_rejects_unknown_model():
    with pytest.raises(ValueError):
        X.build_plan(_cal(["NotADrive"]), ["NotADrive"])


def test_excluded_models_have_no_circuit_entry_needed():
    assert not (X.EXCLUDED & set(X.CIRCUIT))


def _target(types, di=0):
    return {"tid": "t", "model": "M", "family": "OD", "di": di,
            "cands": [["M", i, t] for i, t in enumerate(["true"] + types)]}


def test_records_rank1_wins_and_ratio():
    pl = ["P1", "P1", "P2"]
    t = _target(["knob_small", "twin", "twin"])
    D = {0: np.array([0.0, 1.0, 2.0, 4.0]),      # own DI: truth is the target itself
         1: np.array([0.5, 1.5, 2.5, 4.5]),      # same player: truth first
         2: np.array([3.0, 1.0, 2.0, 5.0])}      # other player: a small knob step and a twin beat it
    recs = {r["cond"]: r for r in X.records(t, D, pl)}
    assert set(recs) == {"same_player", "other_player"}
    s, o = recs["same_player"], recs["other_player"]
    assert s["rank1"] and s["first_type"] == "true"
    assert s["r"]["twin"] == pytest.approx(0.5 / 3.0)
    assert s["wins"]["twin"] == [2, 2]
    assert not o["rank1"] and o["first_type"] == "knob_small" and o["rank1_or_small"]
    assert o["wins"]["twin"] == [1, 2] and o["rank1_type"]["twin"] is False
    assert o["r"]["knob_small"] == pytest.approx(3.0)


def test_records_ties_count_against_truth():
    t = _target(["twin"])
    recs = X.records(t, {0: np.array([0.0, 1.0]), 1: np.array([1.0, 1.0])}, ["P1", "P2"])
    assert not recs[0]["rank1"] and recs[0]["wins"]["twin"] == [0, 1]


def test_spearmans_kinds():
    t = _target(["twin", "twin", "diff_class"])
    D = {x: np.array([0.0, 1.0, 2.0, 3.0]) + 0.01 * x for x in range(4)}
    sp = X.spearmans(t, D, ["P1", "P1", "P2", "P2"])
    kinds = {q["kind"] for q in sp}
    assert kinds == {"vs_own_di|same_player", "vs_own_di|other_player", "between|mixed", "between|other_player"}
    assert all(q["rho"] == pytest.approx(1.0) for q in sp)


def test_summarise_bootstrap_shapes():
    pl = ["P1", "P2"]
    recs, sps = [], []
    for m in ("A", "B", "C"):
        t = {**_target(["twin", "knob_large"]), "model": m, "tid": m}
        D = {0: np.array([0.0, 1.0, 2.0]), 1: np.array([0.5, 1.0, 2.0])}
        recs += X.records(t, D, pl)
        sps += X.spearmans(t, D, pl)
    s = X.summarise(recs, sps, ["A", "B", "C"], boot=50)
    o = s["other_player"]
    assert o["rank1"] == 1.0 and o["win_all"] == 1.0 and o["chance"] == pytest.approx(1 / 3)
    assert o["ci"]["rank1"] == [1.0, 1.0]
    assert s["same_player"] == {"ci": {}}
    assert s["spearman"]["rho_vs_own_di|other_player"]["mean"] == pytest.approx(1.0)
