import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sb", ROOT / "scripts" / "synthetic_benchmark.py")
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)


def test_knob_budget_is_16():
    assert sum(sb.KNOBS.values()) == 16


def test_generation_is_deterministic_and_sized():
    m = sb.build_manifest()
    a, b = sb.generate_items(m), sb.generate_items(m)
    assert a == b and len(a) == 3 * 40 + 60
    assert all(len(i["knobs"]) == 16 and all(0 <= k <= 1 for k in i["knobs"]) for i in a)
    m2 = sb.build_manifest()
    m2["seed"] += 1
    assert sb.generate_items(m2) != a


def test_models_come_from_structure_or_pool():
    m = sb.build_manifest()
    for i in sb.generate_items(m):
        if i["engine"] == "native":
            assert i["models"] == m["native"]["structures"][i["structure"]]
        else:
            assert all(i["models"][c] in m["stomp"]["pool"][c] for c in i["models"])


def test_oracle_and_corrupted_metrics():
    items = sb.generate_items(sb.build_manifest())
    assert sb.block_f1(items, items) == 1.0
    assert sb.model_acc(items, items) == 1.0
    assert sb.param_mae(items, items) == 0.0
    bad = copy.deepcopy(items)
    for i in bad:
        i["enabled"] = {c: not v for c, v in i["enabled"].items()}
    assert sb.block_f1(items, bad) < 0.5
    off = copy.deepcopy(items)
    for i in off:
        i["knobs"] = [min(1.0, k + 0.1) if k < 0.9 else k - 0.1 for k in i["knobs"]]
    assert sb.param_mae(items, off) == pytest.approx(0.1, abs=1e-6)


def test_param_mae_skips_wrong_model_slots():
    items = sb.generate_items(sb.build_manifest())[:1]
    wrong = copy.deepcopy(items)
    wrong[0]["models"]["amp"] = "HD2_Other"
    wrong[0]["knobs"] = [1 - k for k in wrong[0]["knobs"]]
    owner = sb._knob_owner(sb.KNOBS)
    t = items[0]
    scored = [i for i, c in enumerate(owner) if c != "amp" and t["enabled"][c]]
    exp = sum(abs(t["knobs"][i] - wrong[0]["knobs"][i]) for i in scored) / len(scored)
    assert sb.param_mae(items, wrong) == pytest.approx(exp)
    assert sb.model_acc(items, wrong) < 1.0


def test_baselines_sit_below_proposed_thresholds():
    th = sb.DEFAULT_MANIFEST["thresholds_proposed"]
    assert sb.baseline_all_on_f1() < th["block_f1"] - 0.1
    assert sb.baseline_mid_mae() > th["param_mae"]
    # empirical check of the Beta(2,2) mid-guess MAE claim
    items = sb.generate_items(sb.build_manifest())
    guess = copy.deepcopy(items)
    for i in guess:
        i["knobs"] = [0.5] * 16
    assert sb.param_mae(items, guess) == pytest.approx(sb.baseline_mid_mae(), abs=0.02)


def test_rank1_rate():
    assert sb.rank1_rate([0.1, 0.5], [[0.2, 0.3], [0.4, 0.9]]) == 0.5
    assert sb.rank1_rate([0.1], [[0.1]]) == 0.0  # ties do not count as rank 1


def test_plan_costs_follow_the_measured_rates():
    p = sb.plan(sb.build_manifest(), budget=150)
    assert p["native_truth_renders"] == 240
    assert p["native_truth_s"] == pytest.approx(240 * sb.COST["native_render_s"])
    assert p["stomp_truth_s"] > p["stomp_truth_renders"] * sb.COST["stomp_render_s"]


def test_generate_writes_labels_only(tmp_path):
    assert sb.main(["--generate", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "manifest.json").read_text())["name"] == "synthetic-v1"
    assert not list(tmp_path.glob("*.wav"))


def test_render_path_is_gated():
    assert sb.main(["--render"]) == 2 and sb.main(["--ci"]) == 2


def test_regression_detection(tmp_path, monkeypatch):
    led = tmp_path / "runs.csv"
    led.write_text("timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
                   "t,c,a,benchmark,native,synthetic-v1,block_f1,0.90,,,\n"
                   "t,c,a,benchmark,native,synthetic-v1,param_mae,0.10,,,\n")
    monkeypatch.setattr(sb, "LEDGER", led)
    tol = {"block_f1": 0.02, "param_mae": 0.01}
    assert sb.regressions({"block_f1": 0.89, "param_mae": 0.105}, "synthetic-v1", "native", tol) == []
    out = sb.regressions({"block_f1": 0.85, "param_mae": 0.13}, "synthetic-v1", "native", tol)
    assert len(out) == 2
    assert sb.regressions({"block_f1": 0.1}, "synthetic-v1", "stomp", tol) == []


def test_example_manifest_has_no_line6_ids_and_same_shape():
    ex = sb.example_manifest()
    assert "HD2_" not in json.dumps(ex) and "EXAMPLE_" in json.dumps(ex)
    assert ex["seed"] == sb.build_manifest()["seed"]
    assert set(ex["native"]["structures"]) == set(sb.STRUCTURES)


def test_default_output_is_gitignored_cache():
    assert sb.SET_DIR == ROOT / "cache" / "synthetic"


def test_regressions_skip_void_rows(tmp_path, monkeypatch):
    led = tmp_path / "runs.csv"
    led.write_text("timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
                   "T1,c,a,benchmark,native,d,block_f1,0.9,,,\n"
                   "T2,c,a,benchmark,native,d,block_f1,0.99,,,\n")
    void = tmp_path / "void.csv"
    void.write_text("timestamp,metric,reason\nT2,block_f1,test\n")
    monkeypatch.setattr(sb, "LEDGER", led)
    monkeypatch.setattr(sb, "VOID", void)
    assert sb.regressions({"block_f1": 0.9}, "d", "native", {}) == []
    monkeypatch.setattr(sb, "VOID", tmp_path / "none.csv")
    assert len(sb.regressions({"block_f1": 0.9}, "d", "native", {})) == 1
