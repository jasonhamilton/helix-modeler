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


# ---- baselines (T097) -------------------------------------------------------------------------

class FakeSpace:
    def __init__(self, names):
        self.refs = [(0, n) for n in names]

    def from_unit(self, u):
        return np.round(np.asarray(u, float) * 127) / 127 * 10.0     # range 0..10 on a 128-step grid


class FakeBlock:
    def __init__(self, model, category, names=("A", "B")):
        self.model, self.category = model, category
        self.free = [(n, 0.0, 10.0, 5.0) for n in names]
        self.space = FakeSpace(names)

    def norm(self, values):
        return {n: v / 10.0 for n, v in values.items()}


def test_mid_values_are_the_middle_of_each_range():
    v = sv.mid_values(FakeBlock("m", "drive", ("Drive", "Tone", "Mix")))
    assert set(v) == {"Drive", "Tone", "Mix"}
    assert all(abs(x - 5.0) < 10 / 127 for x in v.values())


def test_random_draws_are_seeded_cover_the_pool_and_stay_in_range():
    pool = {f"M{i}": FakeBlock(f"M{i}", "drive") for i in range(5)}
    a = sv.random_draws(np.random.default_rng(3), pool, 300)
    b = sv.random_draws(np.random.default_rng(3), pool, 300)
    assert a == b and len(a) == 300
    assert {m for m, _ in a} == set(pool)
    assert all(0.0 <= x <= 10.0 for _, v in a for x in v.values())


def test_best_index_skips_failed_scores():
    assert sv.best_index([3.0, float("inf"), 1.0, 1.0]) == 2
    assert sv.best_index([float("inf"), float("nan")]) == -1


def base_item():
    return {"id": "i000", "category": "drive", "model": "T", "values": {"A": 2.0, "B": 8.0}}


def test_baseline_fields_score_category_model_and_settings():
    pool = {"T": FakeBlock("T", "drive"), "U": FakeBlock("U", "drive"), "V": FakeBlock("V", "amp")}
    r = sv.baseline_item_fields(base_item(), pool, "T", {"A": 5.0, "B": 5.0})
    assert r["model_ok"] and r["category_ok"]
    assert r["param_abs_err"] == pytest.approx({"A": 0.3, "B": 0.3})
    r = sv.baseline_item_fields(base_item(), pool, "U", {"A": 5.0, "B": 5.0})
    assert not r["model_ok"] and r["category_ok"] and "param_abs_err" not in r
    r = sv.baseline_item_fields(base_item(), pool, "V", None)
    assert not r["model_ok"] and not r["category_ok"]


def test_summarize_baseline_middle_and_random():
    pools = {c: ["a", "b", "c", "d", "e", "f"] for c in sv.CATEGORIES}
    labels = [{"id": f"i{k:03d}", "category": "drive"} for k in range(4)]
    mid = [{"id": f"i{k:03d}", "baseline": "middle", "category_ok": True, "model_ok": k < 3, "renders": 70,
            "wall_s": 10.0, "loss_fit": 4.0, "loss_heldout": 5.0, "param_mid_abs_err": {"A": 0.2, "B": 0.1},
            "all": {"category_ok": k < 2, "model_ok": k < 1}, "rank1": k == 0, "rank1_audible": k < 2,
            "n_same_sound": 1} for k in range(4)]
    s = sv.summarize_baseline(mid, pools, labels)
    assert s["model_acc_category_given"] == 0.75 and "block_f1" not in s
    assert s["model_acc_no_category"] == 0.25 and s["block_f1_no_category"] == 0.5
    assert s["param_mae"] == pytest.approx(0.15) and s["score_rank1_rate"] == 0.25
    assert s["heldout_ratio"] == pytest.approx(1.25) and s["block_f1_chance"] == 0.125
    assert s["model_acc_chance"] == pytest.approx(1 / 6)
    rnd = [{"id": "i000", "baseline": "random", "category_ok": True, "model_ok": True, "renders": 300,
            "wall_s": 9.0, "loss_fit": 6.0, "loss_heldout": 6.0, "param_abs_err": {"A": 0.4}},
           {"id": "i001", "baseline": "random", "category_ok": False, "model_ok": False, "renders": 300,
            "wall_s": 9.0, "loss_fit": 7.0, "loss_heldout": 7.0}]
    s = sv.summarize_baseline(rnd, pools, labels)
    assert s["model_acc"] == 0.5 and s["block_f1"] == 0.5 and s["param_mae"] == pytest.approx(0.4)
    assert s["items_model_right"] == 1


def test_baseline_errors_count_as_misses_and_ledger_names_do_not_collide_with_the_search():
    pools = {c: ["a", "b"] for c in sv.CATEGORIES}
    labels = [{"id": "i000", "category": "drive"}, {"id": "i001", "category": "drive"}]
    res = [{"id": "i000", "baseline": "random", "category_ok": True, "model_ok": True, "renders": 3,
            "wall_s": 1.0, "loss_fit": 1.0, "loss_heldout": 1.0, "param_abs_err": {"A": 0.1}},
           {"id": "i001", "baseline": "random", "category_ok": False, "model_ok": False, "error": "x",
            "renders": 0, "wall_s": 0.0}]
    s = sv.summarize_baseline(res, pools, labels)
    assert s["errors"] == 1 and s["model_acc"] == 0.5
    names = {m for d in sv.BASELINE_LEDGER.values() for m in d.values()}
    assert not names & set(sv.LEDGER_METRICS) and all(n.startswith("baseline_") for n in names)


# ---- Ruling 2 (2026-10-16): known-DI mode ----------------------------------------------------
class FakeKit:
    """score(target, y) = RMS difference, so identical renders score exactly 0."""
    def score(self, target, y, weights=None):
        class R:
            total = float(np.sqrt(np.mean((np.asarray(target) - np.asarray(y)) ** 2)))
        return R()


class GainBlock:
    def __init__(self, name):
        self.name = name

    def with_values(self, v):
        return (self.name, v)

    def from_knobs(self, k):
        return k


class FakeEngine:
    """Renders a preset as a gain on the dry clip; records the DI each render used."""
    def __init__(self):
        self.p, self.dis = None, []

    def load(self, p):
        self.p = p

    def render(self, d):
        self.dis.append(id(d))
        return np.asarray(d, np.float32) * float(self.p[1])


def _known_di_setup():
    rng = np.random.default_rng(0)
    dry = {"A": rng.standard_normal(4800).astype(np.float32), "C": rng.standard_normal(4800).astype(np.float32)}
    it = {"id": "i007", "model": "m_true", "neighbours": [
        {"label": "near", "model": "m_n1", "knobs": 0.1}, {"label": "same", "model": "m_n2", "knobs": 1.0}]}
    pool = {"m_true": GainBlock("m_true"), "m_n1": GainBlock("m_n1"), "m_n2": GainBlock("m_n2")}
    eng = FakeEngine()
    eng.load(("m_true", 1.0))
    ta, tc = eng.render(dry["A"]), eng.render(dry["C"])
    return eng, it, pool, dry, ta, tc


def test_known_di_rows_use_the_same_notes_and_aligned_capture(monkeypatch):
    import loss_trust_check as ltc
    eng, it, pool, dry, ta, tc = _known_di_setup()
    seen = []
    real = ltc.aligned_capture
    monkeypatch.setattr(ltc, "aligned_capture", lambda x, rng, **k: seen.append(1) or real(x, rng, **k))
    rows, extra = sv.rank1_rows(eng, it, pool, FakeKit(), {}, dry, ta, tc, known_di=True)
    assert seen == [1] and extra["rank1_mode"] == "known_di_same_notes_aligned"
    assert rows[0]["loss"] > 0.0                      # the capture is not the render: no free exact zero
    assert rows[0]["null_db"] == 0.0
    assert rows[2]["null_db"] < -100                  # a gain-1 neighbour nulls against the truth on A
    assert "rank1_diffnotes" in extra and "rank1_audible_diffnotes" in extra
    # every neighbour rendered on A (same notes) and on C (diagnostic)
    assert eng.dis.count(id(dry["A"])) >= 3 and eng.dis.count(id(dry["C"])) >= 3


def test_known_di_rule_b_counts_the_tied_neighbours():
    eng, it, pool, dry, ta, tc = _known_di_setup()
    rows, _ = sv.rank1_rows(eng, it, pool, FakeKit(), {}, dry, ta, tc, known_di=True)
    f = sv.rank1_fields(rows)
    assert f["n_same_sound"] == 1                     # the gain-1 neighbour is the same sound
    assert f["rank1_audible"]


def test_default_mode_is_unchanged_and_scores_on_new_notes():
    eng, it, pool, dry, ta, tc = _known_di_setup()
    rows, extra = sv.rank1_rows(eng, it, pool, FakeKit(), {}, dry, ta, tc, known_di=False)
    assert extra == {}
    assert rows[0]["loss"] == pytest.approx(float(np.sqrt(np.mean((ta - tc) ** 2))))


def test_level_knobs_split_only_in_known_di_mode():
    errs = {"Drive": 0.1, "Output": 0.4, "Volume": 0.2, "Level": 0.3}
    assert sv.split_level(errs, False) == (errs, {})
    main, lvl = sv.split_level(errs, True)
    assert main == {"Drive": 0.1} and lvl == {"Output": 0.4, "Volume": 0.2, "Level": 0.3}
    r = item("drive")
    r["param_abs_err"], r["param_abs_err_level"] = main, lvl
    s = sv.summarize([r], [{"id": "i000", "category": "drive"}], {"drive": {"a": 1}})
    assert s["param_mae"] == pytest.approx(0.1) and s["param_mae_level"] == pytest.approx(0.3)
    assert "param_mae_level" not in sv.summarize([item("drive")], [{"id": "i000", "category": "drive"}], {})
