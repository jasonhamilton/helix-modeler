"""gate2_report: run discovery, metrics, pass rule, page build on a tiny fixture project. No Native."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import gate2_report as G

CATS = G.CATEGORIES


def make_project(tmp: Path, n_full: int = 0, audio: bool = False, specs=()) -> Path:
    n = max(n_full, 16)
    labels = []
    for k in range(n):
        labels.append({"id": f"i{k:03d}", "category": CATS[k % 8], "model": f"HD2_Dist{k}Fake",
                       "values": {"Gain": 0.8, "Level": 0.5}, "ranges": {"Gain": [0, 1], "Level": [0, 1]}})
    sd = tmp / "cache" / "synthetic-v1"
    runs = sd / "runs"
    (sd).mkdir(parents=True)
    (sd / "labels.json").write_text(json.dumps(labels))

    def item(k, kind, ok_model, ok_cat=True):
        i = f"i{k:03d}"
        d = {"id": i, "category": CATS[k % 8], "model": labels[k]["model"], "model_ok": ok_model,
             "category_ok": ok_cat, "pred_model": labels[k]["model"] if ok_model else "HD2_DistOtherFake",
             "pred_category": CATS[k % 8], "loss_heldout": 0.1}
        if kind == "middle":
            d.update({"baseline": "middle", "all": {"pred_model": "HD2_DistOtherFake", "pred_category": CATS[k % 8],
                                                    "category_ok": ok_cat, "model_ok": False},
                      "param_mid_abs_err": {"Gain": 0.3, "Level": 0.9}, "rank1_audible": True, "rank1": True})
        elif kind == "random":
            d.update({"baseline": "random"})
            if ok_model:
                d["param_abs_err"] = {"Gain": 0.2}
        else:
            d.update({"pred_values": {"Gain": 0.78}, "rank1_audible": True, "rank1": True})
            if ok_model:
                d["param_abs_err"] = {"Gain": 0.05, "Level": 0.9}
        return d

    def write(tag, kind, ids, model_ok, admitted=True):
        r = runs / tag
        r.mkdir(parents=True, exist_ok=True)
        for k in ids:
            (r / f"i{k:03d}.json").write_text(json.dumps(item(k, kind, model_ok(k))))
        (r / "summary.json").write_text(json.dumps({"admitted": admitted, "budget": 300}))

    write("v3_base_middle", "middle", range(n), lambda k: False)
    write("v3_base_random", "random", range(n), lambda k: k % 2 == 0)
    write("v4b_dev16_seed", "ours", range(16), lambda k: k % 4 != 0)
    # decoy: a better-looking but ineligible dev run must never become the headline
    write("h1_f50_k5", "ours", range(16), lambda k: True)
    write("h3_f40_k4_lf", "ours", range(16), lambda k: k % 4 != 0)
    if n_full:
        write("gate2_full_hybrid", "ours", range(n_full), lambda k: True)
    for t in specs:
        write(t, "ours", [k for k in range(16) if CATS[k % 8] in ("drive", "amp")], lambda k: True)
    if audio:
        d = runs / "v4b_dev16_seed"
        x = (0.1 * np.sin(2 * np.pi * 220 * np.arange(48000 * 6) / 48000)).astype(np.float32)
        sf.write(str(d / "i001_target.wav"), x, 48000)
        sf.write(str(d / "i001_recovered.wav"), x * 0.5, 48000)
    (tmp / "docs").mkdir()
    (tmp / "docs" / "DECISIONS.md").write_text("## 2026-10-17 (lead): Gate 2 pass rule FROZEN before x\n\n- rule one\n\n## next\n")
    return tmp


def test_discover_roles_and_scope(tmp_path):
    d = G.discover(make_project(tmp_path))
    roles = {(m["kind"], m["role"]): m for m in d["methods"]}
    assert ("middle", "middle") in roles and ("random", "random") in roles
    assert roles[("ours", "stage")]["scope"] == "dev16"
    assert roles[("ours", "hybrid")]["tag"] == G.DESIGNATED["dev"]
    assert roles[("middle", "middle")]["scope"] == "full" or roles[("middle", "middle")]["scope"] == "dev16"
    assert not any(m["role"] == "full" for m in d["methods"])


def test_metrics_and_middle_uses_nothing_given_pick():
    items = {"i000": {"category": "drive", "category_ok": True, "model_ok": True,
                      "all": {"category_ok": False, "model_ok": False}, "param_mid_abs_err": {"Gain": 0.4, "Level": 1.0}}}
    mid = G.metrics("middle", items)
    assert mid["category"] == 0.0 and mid["model"] == 0.0
    assert abs(mid["mae"] - 0.4) < 1e-9          # level knob excluded
    ours = G.metrics("ours", {"i000": {"category": "drive", "category_ok": True, "model_ok": True,
                                        "param_abs_err": {"Gain": 0.1, "Volume": 0.9}}})
    assert ours["model"] == 1.0 and abs(ours["mae"] - 0.1) < 1e-9


def test_baselines_show_no_rank1(tmp_path):
    """Rank-1 is a property of the score, not of a method: baselines never carry it (human, 2026-10-17)."""
    d = G.discover(make_project(tmp_path))
    for kind in ("random", "middle"):
        m = next((x for x in d["methods"] if x["kind"] == kind), None)
        if m:
            assert G.metrics(kind, m["items"])["rank1"] is None


def test_bootstrap_is_deterministic_and_ordered():
    a = G.bootstrap_ci([0.1, -0.2, 0.0, 0.3, -0.1])
    assert a == G.bootstrap_ci([0.1, -0.2, 0.0, 0.3, -0.1])
    assert a[1] <= a[0] <= a[2]
    assert all(np.isnan(G.bootstrap_ci([])))


def test_pass_rule_waits_until_full_then_judges(tmp_path):
    root = make_project(tmp_path / "a")
    M = G.build_model(root)
    ours = next(m for m in M["methods"] if m["kind"] == "ours")
    ev = G.evaluate_pass(ours["items"], M["random"]["items"], 96, M["d"]["all_ids"])
    assert not ev["complete"] and all(c["ok"] is None for c in ev["checks"])
    root = make_project(tmp_path / "b", n_full=96)
    M = G.build_model(root)
    assert M["full"] and M["full"]["scope"] == "full"
    ev = G.evaluate_pass(M["full"]["items"], M["random"]["items"], 96, M["d"]["all_ids"])
    assert ev["complete"]
    by = {c["key"]: c for c in ev["checks"]}
    assert by["model"]["ok"] is True and by["mae"]["ok"] is True and by["category"]["ok"] is True
    assert by["vs_random_model"]["ok"] is True and by["vs_random_mae"]["ok"] is True
    assert ev["all_ok"] is not None


def test_build_page_dev_label_waiting_and_source_tooltips(tmp_path):
    root = make_project(tmp_path)
    path = G.build(root)
    h = path.read_text()
    assert path == root / "reports" / "local" / "gate2" / "index.html"
    assert "dev16 only" in h and "waiting for the full run" in h
    assert "rule one" in h                      # frozen rule quoted
    assert 'title="Source:' in h or "title=\"Source" in h or "run files under" in h
    assert "<script" not in h                    # works offline, no scripts
    assert "prefers-color-scheme:dark" in h and "@media (max-width:560px)" in h
    for sec in ("what", "method", "results", "specialists", "examples", "audio", "why", "other", "next", "glossary"):
        assert f"id='{sec}'" in h


def test_build_with_full_run_shows_80_split(tmp_path):
    root = make_project(tmp_path, n_full=96)
    h = G.build(root).read_text()
    assert "The 80 items outside dev16" in h and "full 96" in h


def test_audio_copy_is_loudness_matched(tmp_path):
    root = make_project(tmp_path, audio=True)
    out = root / "reports" / "local" / "gate2"
    G.build(root, out)
    files = sorted((out / "audio").rglob("*.wav"))
    assert len(files) == 2
    import ab_session as AB

    from tonematch.separation.audio import integrated_loudness, load_audio
    ls = [integrated_loudness(load_audio(f)) for f in files]
    assert all(abs(x - AB.TARGET_LUFS) < 0.6 for x in ls)
    assert "<audio" in (out / "index.html").read_text()


def test_render_examples_refuses_when_native_busy(monkeypatch, capsys):
    monkeypatch.setattr(G, "machine_hosts", lambda: 2)
    rc = G.main(["render-examples", "--run", "x"])
    assert rc == 3 and "refusing" in capsys.readouterr().err


def test_pretty_model():
    assert G.pretty_model("HD2_DistRatatouilleDist") == "Dist Ratatouille Dist"
    assert G.pretty_model(None) == "?"


def test_designated_runs_not_best_of(tmp_path):
    M = G.build_model(make_project(tmp_path / "a"))
    assert M["dev"]["tag"] == "h3_f40_k4_lf" and M["full"] is None
    h = G.build(tmp_path / "a").read_text()
    assert "h3_f40_k4_lf" in h and "ineligible: over budget" in h
    assert "Ours: hybrid (h1_f50_k5)" not in h
    M = G.build_model(make_project(tmp_path / "b", n_full=96))
    assert M["full"]["tag"] == "gate2_full_hybrid" and M["full"]["scope"] == "full"
    # the choice is configurable in one place
    assert set(G.DESIGNATED) == {"full", "dev"}


def test_partial_full_run_is_designated_but_not_full(tmp_path):
    root = make_project(tmp_path, n_full=96)
    for f in (root / "cache" / "synthetic-v1" / "runs" / "gate2_full_hybrid").glob("i0[2-9]*.json"):
        f.unlink()
    M = G.build_model(root)
    assert M["full"]["scope"].startswith("partial")


def test_family_grouping_covers_every_category():
    assert set(G.FAMILY_OF) == set(G.CATEGORIES)
    assert G.FAMILY_OF["drive"] == G.FAMILY_OF["amp"] == "gain"
    assert G.FAMILY_OF["cab"] == G.FAMILY_OF["eq"] == "linear"
    assert G.FAMILY_OF["delay"] == G.FAMILY_OF["reverb"] == "time"
    fm = G.family_metrics("ours", {"i000": {"category": "drive", "category_ok": True, "model_ok": True},
                                   "i001": {"category": "amp", "category_ok": True, "model_ok": False}}, ["i000", "i001"])
    assert fm["gain"]["n"] == 2 and fm["gain"]["model_hits"] == 1 and fm["linear"]["n"] == 0
    assert G.weakest_families({"gain": {"n": 2, "model": 0.5, "category": 1}, "mod": {"n": 2, "model": 0.0, "category": 1},
                               "time": {"n": 0}}, 1) == ["mod"]


def test_specialist_section_and_heatmap_families(tmp_path):
    h = G.build(make_project(tmp_path / "a")).read_text()
    assert "A specialist for each kind of effect" in h and "next specialist to build" in h
    assert "beats the general method on that family at the same budget" in h
    for name in ("Gain", "Linear", "Dynamics", "Time-based", "Modulation"):
        assert name in h
    assert "colspan='2'" in h                               # heatmap grouped by family
    assert "No specialist runs yet" in h
    h2 = G.build(make_project(tmp_path / "b", specs=("spec_gain_v1",))).read_text()
    assert "spec_gain_v1" in h2 and "Specialist right model" in h2
