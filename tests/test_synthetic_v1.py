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


def test_set_paths_follow_the_set_name():
    old = (sv.DATASET, sv.SET_DIR)
    try:
        assert sv.set_paths("synthetic-v2") == ("synthetic-v2", ROOT / "cache" / "synthetic-v2")
        assert sv.DATASET == "synthetic-v2"
    finally:
        sv.set_paths(old[0])
    assert sv.SET_DIR == old[1] == ROOT / "cache" / "synthetic-v1"


def test_generate_cli_defaults_keep_synthetic_v1(monkeypatch):
    seen = {}
    monkeypatch.setattr(sv, "generate", lambda *a, **k: seen.update(a=a, k=k) or
                        {"items": 0, "pool": {}})
    try:
        sv.main(["generate"])
        assert seen["a"] == (sv.SEED,)
        assert seen["k"] == {"items_per_category": None, "categories": None, "avoid_sets": [], "name": "synthetic-v1",
                           "check_targets": None}
        assert sv.DATASET == "synthetic-v1"
        sv.main(["generate", "--set", "x", "--seed", "5", "--items-per-category", "3", "--categories", "mod", "amp",
                 "--avoid-sets", "synthetic-v1"])
        assert seen["a"] == (5,) and seen["k"]["categories"] == ["mod", "amp"] and seen["k"]["name"] == "x"
        assert sv.SET_DIR == ROOT / "cache" / "x"
    finally:
        sv.set_paths("synthetic-v1")


def test_pick_excerpts_avoid_keeps_clear_of_given_excerpts(monkeypatch):
    import helix_trust_set as HT
    runs = [{"path": "a.wav", "start_s": float(s), "style": "x"} for s in range(0, 200, 4)] + \
           [{"path": "b.wav", "start_s": float(s), "style": "x"} for s in range(0, 200, 4)]
    monkeypatch.setattr(HT, "usable_runs", lambda seconds: runs)
    avoid = [{"path": "a.wav", "start_s": 20.0}]
    got = HT.pick_excerpts(np.random.default_rng(3), 12, 10.0, avoid)
    assert not any(HT.overlaps_any(g, avoid, 10.0) for g in got)
    assert HT.pick_excerpts(np.random.default_rng(3), 5, 10.0) == HT.pick_excerpts(np.random.default_rng(3), 5, 10.0, None)


def test_baseline_fields_record_the_guessed_values():
    pool = {"T": FakeBlock("T", "drive")}
    r = sv.baseline_item_fields(base_item(), pool, "T", {"A": 5.0, "B": 6.0})
    assert r["pred_values"] == {"A": 5.0, "B": 6.0}
    assert "pred_values" not in sv.baseline_item_fields(base_item(), pool, "T", None)


def _fake_generate_env(monkeypatch, tmp_path):
    import sys
    import types

    ht = types.ModuleType("helix_trust_set")
    ex = [{"path": f"f{i}.wav", "start_s": 10.0 * i} for i in range(40)]
    ht.usable_runs = lambda s: ex
    ht.overlaps_any = lambda r, a, s: False
    ht.pick_excerpts = lambda rng, n, s, avoid: ex[:n]
    ht.load_excerpt = lambda e, s, ref: (np.ones(480, np.float32) * 0.1, 0.0)
    monkeypatch.setitem(sys.modules, "helix_trust_set", ht)
    pools = {c: {f"HD2_{c}{k}": [("A", 0.0, 1.0, 0.5), ("B", 0.0, 1.0, 0.5)] for k in range(3)} for c in sv.CATEGORIES}
    monkeypatch.setattr(sv, "build_pools", lambda seed, categories=None: pools)
    monkeypatch.setattr(sv, "second_passage", lambda rng, runs, ea, s: dict(runs[-1], same_file=False))
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    nl = types.ModuleType("tonematch.engines.native_library")
    nl.VERSION = "fake"
    monkeypatch.setitem(sys.modules, "tonematch.engines.native_library", nl)
    monkeypatch.setattr(sv, "FIXTURE", tmp_path / "fx.json")


def test_generate_redraws_a_digital_zero_target(monkeypatch, tmp_path):
    _fake_generate_env(monkeypatch, tmp_path)
    calls = []

    def checker(model, cat, free, values, dry):
        calls.append(model)
        return "digital zero" if model == "HD2_delay0" else None   # this model never makes sound

    man = sv.generate(7, tmp_path / "s", items_per_category=3, name="synthetic-vx", checker=checker)
    labels = json.loads((tmp_path / "s" / "labels.json").read_text())
    assert len(labels) == 24 and all(it["model"] != "HD2_delay0" for it in labels)
    delay = sorted(it["model"] for it in labels if it["category"] == "delay")
    assert len(delay) == 3 and all(m.startswith("HD2_delay") for m in delay)
    rd = man["target_check"]["redraws"]
    assert [r["category"] for r in rd] == ["delay"] and rd[0]["rejected"][0]["model"] == "HD2_delay0"
    assert len(rd[0]["rejected"]) == sv.MAX_KNOB_REDRAWS   # knobs first, then the model changes


def test_generate_knob_redraw_keeps_the_model(monkeypatch, tmp_path):
    _fake_generate_env(monkeypatch, tmp_path)
    seen = {"n": 0}

    def checker(model, cat, free, values, dry):
        seen["n"] += 1
        return "silence guard" if seen["n"] == 1 else None      # only the first draw fails

    sv.generate(7, tmp_path / "s", items_per_category=1, name="synthetic-vx", checker=checker)
    clean = json.loads((tmp_path / "s" / "labels.json").read_text())
    seen["n"] = 100
    sv.generate(7, tmp_path / "t", items_per_category=1, name="synthetic-vx", checker=checker)
    base = json.loads((tmp_path / "t" / "labels.json").read_text())
    assert clean[0]["model"] == base[0]["model"] and clean[0]["knobs"] != base[0]["knobs"]
    assert [i["knobs"] for i in clean[1:]] == [i["knobs"] for i in base[1:]]   # other items unchanged


def test_legacy_sets_skip_the_rule(monkeypatch, tmp_path):
    _fake_generate_env(monkeypatch, tmp_path)
    man = sv.generate(7, tmp_path / "s", items_per_category=1, name="synthetic-v2",
                      checker=lambda *a: pytest.fail("legacy set must not render"))
    assert "target_check" not in man


def test_true_screen_rank_is_none_when_the_search_never_screened_the_true_model():
    # T148: the hybrid drops time-variant models (HD2_DelayADT), so the true model can be absent.
    assert sv.true_screen_rank({"A": 0.3, "B": 0.1, "C": 0.2}, "C") == 2
    assert sv.true_screen_rank({"A": 0.3, "B": 0.1}, "HD2_DelayADT") is None


def test_build_pools_drops_measured_time_variant_models_after_the_draw(monkeypatch):
    # T148: a model only in native_library.TIME_VARIANT_NATIVE_382 (not flagged in the index) must
    # not reach a pool, and dropping it after the draw leaves every other pick as it was.
    import tonematch.catalog as cat
    from tonematch.engines import native_library as NL

    models = {f"HD2_Delay{x}": {"category": "delay"} for x in "ABCDEFGHIJ"}
    models["HD2_DelayADT"] = {"category": "delay"}
    monkeypatch.setattr(NL, "load_index", lambda *a, **k: {"models": models})
    monkeypatch.setattr(NL, "template_for", lambda m: object())
    monkeypatch.setattr(NL, "is_routing", lambda m: False)
    monkeypatch.setattr(cat, "native_unavailable", list)
    monkeypatch.setattr(sv, "free_params", lambda m, tpl, catalog, c: [("Mix", 0.0, 1.0, 0.5)])
    monkeypatch.setattr(NL, "time_variant_models", lambda *a, **k: frozenset())
    before = sv.build_pools(7, catalog={}, categories=("delay",))["delay"]
    monkeypatch.setattr(NL, "time_variant_models", lambda *a, **k: frozenset({"HD2_DelayADT"}))
    after = sv.build_pools(7, catalog={}, categories=("delay",))["delay"]
    assert "HD2_DelayADT" in before          # seed 7 draws it, so the test is not vacuous
    assert "HD2_DelayADT" not in after
    assert list(after) == [m for m in before if m != "HD2_DelayADT"]


class SilenceGuardEngine(FakeEngine):
    """FakeEngine whose gain-0 presets trip Native's silence guard, as the real engine does."""
    def render(self, d):
        from tonematch.engines.base import EngineError

        if float(self.p[1]) == 0.0:
            raise EngineError("Native returned silence for a non-silent input")
        return super().render(d)


@pytest.mark.parametrize("known_di", [True, False])
def test_a_neighbour_that_renders_silent_is_a_worst_case_distractor_not_a_crash(known_di):
    # T148: devmod2 i019 i024 i040 died on a Compressor Auto Swell neighbour with a high Threshold.
    eng, it, pool, dry, ta, tc = _known_di_setup()
    eng = SilenceGuardEngine()
    it["neighbours"].append({"label": "same_category", "model": "m_n3", "knobs": 0.0})
    pool["m_n3"] = GainBlock("m_n3")
    rows, extra = sv.rank1_rows(eng, it, pool, FakeKit(), {}, dry, ta, tc, known_di=known_di)
    silent = rows[-1]
    assert silent["model"] == "m_n3" and silent["loss"] == sv.NEIGHBOUR_BAD_LOSS
    assert silent["null_db"] == pytest.approx(0.0)     # digital zero: residual = the truth, never a tie
    if known_di:
        assert silent["gm_gain_db"] == float("-inf")   # gain_match on silence: not a level tie
    assert len(extra["neighbour_render_errors"]) == (2 if known_di else 1)
    f = sv.rank1_fields(rows, extra.get("rule_b_level_range_db", 0.0))
    assert f["n_same_sound"] == 1                       # only the gain-1 neighbour; the silent row adds no tie
    # the other rows are exactly what they are without the silent neighbour
    eng2, it2, pool2, *_ = _known_di_setup()
    rows2, _ = sv.rank1_rows(eng2, it2, pool2, FakeKit(), {}, dry, ta, tc, known_di=known_di)
    assert rows[:-1] == rows2
