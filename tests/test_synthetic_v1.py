"""Offline tests for scripts/synthetic_v1.py (no Native, no audio)."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sv1", ROOT / "scripts" / "synthetic_v1.py")
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)


def test_eligibility_follows_the_stand_in_list():
    tv = {"HD2_ReverbOcto"}
    assert sv.eligible("HD2_ReverbRoom", "reverb", tv)
    assert not sv.eligible("HD2_ReverbGanymede", "reverb", tv)
    assert not sv.eligible("HD2_ReverbOcto", "reverb", {"HD2_ReverbOcto"})
    assert not sv.eligible("HD2_DistBitcrusher", "drive", tv)
    assert sv.eligible("HD2_DistScream808", "drive", tv)
    assert sv.eligible("HD2_TremoloOpticalTrem", "mod", tv)
    assert sv.eligible("HD2_Chorus70sChorus", "mod", tv)
    assert not sv.eligible("HD2_FlangerGrayFlanger", "mod", tv)
    assert not sv.eligible("HD2_PhaserDeluxePhaser", "mod", tv)


def test_grid_value_is_on_the_cc_grid():
    assert sv.grid_value(0.0, 8.0, 0) == 0.0
    assert sv.grid_value(0.0, 8.0, 127) == pytest.approx(8.0)
    assert sv.grid_value(-12.0, 12.0, 64) == pytest.approx(-12.0 + 64 / 127 * 24.0)


def test_knob_draws_stay_in_range_and_near_moves_something():
    rng = np.random.default_rng(1)
    free = [("Gain", 0.0, 1.0, 0.5), ("Tone", 0.0, 1.0, 0.5), ("Mix", 0.0, 1.0, 0.5)]
    for _ in range(50):
        ks = sv.draw_knobs(rng, free)
        assert all(round(0.10 * 127) <= k <= round(0.90 * 127) for k in ks.values())
        nb = sv.near_knobs(rng, ks)
        assert nb != ks and all(0 <= k <= 127 for k in nb.values())


def labels(n_per=3):
    return [{"id": f"i{j * len(sv.CATEGORIES) + i:03d}", "category": c}
            for j in range(n_per) for i, c in enumerate(sv.CATEGORIES)]


def test_subset_covers_every_category_first(monkeypatch):
    monkeypatch.setattr(sv, "ITEMS_PER_CATEGORY", 3)
    sel = sv.select_items(labels(), 8, None, None)
    assert sorted(x["category"] for x in sel) == sorted(sv.CATEGORIES)
    halves = [sv.select_items(labels(), None, None, (k, 2)) for k in range(2)]
    assert {x["id"] for x in halves[0]}.isdisjoint({x["id"] for x in halves[1]})


def item(cat, model_ok=True, cat_ok=True, mae=0.1, rank1=True):
    r = {"id": "i000", "category": cat, "category_ok": cat_ok, "model_ok": model_ok, "loss_fit": 1.0,
         "loss_heldout": 2.0, "renders": 10, "wall_s": 5.0, "rank1": rank1, "rank1_audible": rank1,
         "n_same_sound": 1}
    if model_ok:
        r.update(param_abs_err={"a": mae, "b": mae}, param_mae_item=mae, param_mid_err=0.25)
    return r


def test_summary_metrics():
    lab = [{"id": "i000", "category": "drive"}]
    res = [item("drive"), item("drive", model_ok=False, cat_ok=True), item("drive", False, False, rank1=False)]
    s = sv.summarize(res, lab, {"drive": {"a": 1, "b": 2}})
    assert s["block_f1"] == pytest.approx(2 / 3)
    assert s["model_acc"] == pytest.approx(1 / 3)
    assert s["param_mae"] == pytest.approx(0.1)
    assert s["rank1_rate"] == pytest.approx(2 / 3)
    assert s["heldout_ratio"] == pytest.approx(2.0)
    assert s["model_acc_chance"] == pytest.approx(0.5)


def test_errors_count_as_misses():
    lab = [{"id": "i000", "category": "drive"}]
    res = [item("drive"), {"id": "i000", "category": "drive", "category_ok": False, "model_ok": False,
                           "error": "boom", "renders": 0, "wall_s": 0.0}]
    s = sv.summarize(res, lab, {})
    assert s["errors"] == 1 and s["model_acc"] == 0.5


def test_regressions_respect_direction_and_tolerance():
    prev = {"model_acc": 0.5, "param_mae": 0.10, "heldout_ratio": 1.0}
    assert sv.regressions({"model_acc": 0.49, "param_mae": 0.105}, prev, "d") == []
    got = sv.regressions({"model_acc": 0.40, "param_mae": 0.20}, prev, "d")
    assert len(got) == 2 and "model_acc" in got[0] and "param_mae" in got[1]


def test_default_weights_are_provisional_and_labelled():
    w, admitted, label = sv.load_weights("provisional")
    assert not admitted and "not gate evidence" in label and w


def test_weights_file_without_admission_stays_provisional(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"weights": {"ltas_tilt": 2.0}}))
    w, admitted, label = sv.load_weights(str(f))
    assert w == {"ltas_tilt": 2.0} and not admitted and "not gate evidence" in label
    f.write_text(json.dumps({"weights": {"ltas_tilt": 2.0}, "admitted": True, "ledger_row": "2026-x"}))
    assert sv.load_weights(str(f))[1] is True


def test_fixture_manifest_has_no_line6_ids():
    man = {"name": "synthetic-v1", "seed": 1, "clip_seconds": 10.0, "categories": ["drive"], "items": 1,
           "items_per_category": 1, "neighbours": 24, "ref_loud_dbfs": -26.7,
           "excluded": {}, "tolerance": {}, "targets_working": {},
           "pool": {"drive": {"HD2_DistScream808": ["Gain"]}}}
    assert "HD2_" not in json.dumps(sv.fixture_manifest(man))
