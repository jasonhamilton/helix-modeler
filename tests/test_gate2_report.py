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
    files = sorted(f for f in (out / "audio").rglob("*.wav") if "stereo_" not in f.parent.name)
    assert len(files) == 2
    lr = sorted((out / "audio").rglob("*_L-target_R-recovered.wav"))
    assert len(lr) == 1
    import soundfile as sf
    x, _ = sf.read(str(lr[0]))
    assert x.shape[1] == 2   # target on the left channel, recovered on the right (human, 2026-10-17)
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


# --------------------------------------------------------------------------- two-attempt layout (Gate 2 attempt 2)

SIX_MISSES = tuple(f"i{k:03d}" for k in (20, 30, 40, 50, 60, 70))     # rank-1 90 of 96 = 0.9375


def make_v2(tmp: Path, miss_ids=("i005", "i006"), n_fail_cat=0) -> Path:
    """Add a sealed synthetic-v2 set and its three runs to a fixture project: ours (all untuned), middle, random."""
    root = make_project(tmp, n_full=96)
    sd = root / "cache" / "synthetic-v2"
    runs = sd / "runs"
    labels = []
    for k in range(96):
        labels.append({"id": f"i{k:03d}", "category": CATS[k % 8], "model": f"HD2_Dist{k}Fake",
                       "values": {"Gain": 0.8}, "ranges": {"Gain": [0, 1]}})
    sd.mkdir(parents=True)
    (sd / "labels.json").write_text(json.dumps(labels))

    def write(tag, kind):
        r = runs / tag
        r.mkdir(parents=True)
        for k in range(96):
            i = f"i{k:03d}"
            ok = (k % 4 != 0) if kind == "ours" else (k % 2 == 0) if kind == "random" else False
            d = {"id": i, "category": CATS[k % 8], "model": labels[k]["model"], "model_ok": ok, "category_ok": True,
                 "pred_model": labels[k]["model"] if ok else "HD2_DistOtherFake", "pred_category": CATS[k % 8],
                 "loss_heldout": 0.1}
            if kind == "ours":
                d.update({"pred_values": {"Gain": 0.78}, "rank1_audible": i not in miss_ids, "rank1": i not in miss_ids})
                if ok:
                    d["param_abs_err"] = {"Gain": 0.05}
            elif kind == "random":
                d["baseline"] = "random"
                if ok:
                    d["param_abs_err"] = {"Gain": 0.2}
            else:
                d.update({"baseline": "middle", "all": {"pred_model": "HD2_DistOtherFake", "pred_category": CATS[k % 8],
                                                        "category_ok": True, "model_ok": False},
                          "param_mid_abs_err": {"Gain": 0.3}, "rank1_audible": i not in miss_ids, "rank1": i not in miss_ids})
            (r / f"{i}.json").write_text(json.dumps(d))
        (r / "summary.json").write_text(json.dumps({"admitted": True, "budget": 300}))

    write("gate2_v2_attempt2", "ours")
    write("v2_base_middle", "middle")
    write("v2_base_random", "random")
    return root


def test_attempts_config_and_split():
    assert G.ATTEMPTS["v1"]["full"] == "gate2_full_hybrid" and G.ATTEMPTS["v1"]["set"] == "synthetic-v1"
    assert G.ATTEMPTS["v2"]["full"] == "gate2_v2_attempt2" and G.ATTEMPTS["v2"]["dev"] is None
    assert G.ATTEMPTS["v2"]["middle"] == "v2_base_middle" and G.ATTEMPTS["v2"]["random"] == "v2_base_random"
    assert G.SPECIALIST_STATUS == {"gain": "in use", "linear": "in use", "dynamics": "planned", "time": "in use", "mod": "in use"}
    assert "seeds and screen only" in G.SPECIALIST_NOTE["mod"]


def test_v2_discovery_uses_v2_set_and_keeps_v1(tmp_path):
    root = make_v2(tmp_path)
    m2 = G.build_model(root, "v2")
    assert m2["set"] == "synthetic-v2" and m2["full"]["tag"] == "gate2_v2_attempt2" and m2["full"]["scope"] == "full"
    assert m2["middle"]["tag"] == "v2_base_middle" and m2["random"]["tag"] == "v2_base_random"
    assert m2["dev"] is None and not [m for m in m2["methods"] if m["role"] in ("stage", "hybrid")]
    m1 = G.build_model(root, "v1")
    assert m1["full"]["tag"] == "gate2_full_hybrid" and m1["middle"]["tag"] == "v3_base_middle"


def test_v2_verdict_not_passed_six_of_seven(tmp_path):
    root = make_v2(tmp_path, miss_ids=SIX_MISSES)     # rank-1 90 of 96 = 0.9375
    m2 = G.build_model(root, "v2")
    ev = G.evaluate_pass(m2["full"]["items"], m2["random"]["items"], 96, m2["d"]["all_ids"])
    by = {c["key"]: c for c in ev["checks"]}
    assert abs(by["rank1"]["value"] - 0.9375) < 1e-9 and by["rank1"]["ok"] is False
    assert all(c["ok"] for k, c in by.items() if k != "rank1")
    assert G.count_checks(ev) == (6, 7) and ev["all_ok"] is False


def test_rank1_is_a_property_of_the_score_middle_gets_it_too(tmp_path):
    m2 = G.build_model(make_v2(tmp_path, miss_ids=("i001", "i002")), "v2")
    assert G.rank1_rate_raw(m2["middle"]) == G.rank1_rate_raw(m2["full"]) == G.metrics("ours", m2["full"]["items"])["rank1"]
    assert G.metrics("middle", m2["middle"]["items"])["rank1"] is None          # still hidden in baseline columns
    assert [e["id"] for e in G.rank1_misses(m2)] == ["i001", "i002"]
    assert all(e["middle_miss"] for e in G.rank1_misses(m2))


def test_page_has_attempt2_headline_and_attempt1_earlier(tmp_path):
    root = make_v2(tmp_path, miss_ids=SIX_MISSES)
    h = G.build(root).read_text()
    assert h.index("id='attempt2'") < h.index("id='compare'") < h.index("id='results'")
    assert "Gate 2 attempt 2 (latest): NOT PASSED, 6 of 7 checks met" in h
    assert "Results of attempt 1 (the earlier attempt)" in h and "earlier attempt" in h
    assert "rank-1 is a check on the score, not on the search" in h
    assert "rule one" in h                                       # the frozen rule is quoted (once, in the attempt 2 section)
    assert "Gate 2 tests the known-input case" in h
    assert "Split-80 means nothing here" in h and "no information on v2" in h
    assert "Attempt 1 versus attempt 2" in h and "Per category (twelve items each)" in h
    assert "Result by family: attempt 2" in h
    assert "amp specialist with its own refiner" in h and "seeds and screen only" in h and "planned" in h
    assert "<script" not in h
    for sec in ("attempt2", "compare", "what", "method", "results", "specialists", "examples", "audio", "why", "other", "next", "glossary"):
        assert f"id='{sec}'" in h


def test_v1_only_project_still_builds_as_before(tmp_path):
    h = G.build(make_project(tmp_path)).read_text()
    assert "id='attempt2'" not in h and "3. Results at a glance" in h


def test_v2_audio_players_and_stereo_for_every_pair(tmp_path):
    root = make_v2(tmp_path, miss_ids=("i005", "i006"))
    x = (0.1 * np.sin(2 * np.pi * 220 * np.arange(48000 * 6) / 48000)).astype(np.float32)
    for tag in ("gate2_v2_attempt2", "v2_base_middle"):
        for i in ("i005", "i006", "i007"):
            d = root / "cache" / "synthetic-v2" / "runs" / tag
            sf.write(str(d / f"{i}_target.wav"), x, 48000)
            sf.write(str(d / f"{i}_recovered.wav"), x * 0.5, 48000)
    out = root / "reports" / "local" / "gate2"
    h = G.build(root, out).read_text()
    lr = sorted((out / "audio").rglob("*_L-target_R-recovered.wav"))
    assert len(lr) == 6                                              # every pair, ours and the middle baseline
    assert all(sf.read(str(f))[0].shape[1] == 2 for f in lr)
    assert "Attempt_2_" in str(lr[0]) and "Attempt 2 (latest)" in h
    assert h.count("<audio") >= 4                                    # miss items for ours and the middle baseline


# --------------------------------------------------------------------------- three-attempt layout (Gate 2 attempt 3)

def make_v3(tmp: Path, miss_ids=("i005", "i006")) -> Path:
    """Add a sealed synthetic-v3 set to the v1+v2 fixture: ours, middle and random, baselines with summary_shardall.json."""
    root = make_v2(tmp, miss_ids=SIX_MISSES)
    sd = root / "cache" / "synthetic-v3"
    runs = sd / "runs"
    labels = [{"id": f"i{k:03d}", "category": CATS[k % 8], "model": f"HD2_Dist{k}Fake", "values": {"Gain": 0.8},
               "ranges": {"Gain": [0, 1]}} for k in range(96)]
    sd.mkdir(parents=True)
    (sd / "labels.json").write_text(json.dumps(labels))

    def write(tag, kind):
        r = runs / tag
        r.mkdir(parents=True)
        for k in range(96):
            i = f"i{k:03d}"
            ok = (k % 4 != 0 and k % 16 != 1) if kind == "ours" else (k % 2 == 0) if kind == "random" else False
            d = {"id": i, "category": CATS[k % 8], "model": labels[k]["model"], "model_ok": ok, "category_ok": True,
                 "pred_model": labels[k]["model"] if ok else "HD2_DistOtherFake",
                 "pred_category": CATS[k % 8], "loss_heldout": 0.1}
            if kind == "ours":
                d.update({"pred_values": {"Gain": 0.78}, "rank1_audible": i not in miss_ids, "rank1": True,
                          "rule_b": "extended (T146)"})
                if ok:
                    d["param_abs_err"] = {"Gain": 0.05}
            elif kind == "random":
                d["baseline"] = "random"
                if ok:
                    d["param_abs_err"] = {"Gain": 0.2}
            else:
                d.update({"baseline": "middle", "all": {"pred_model": "HD2_DistOtherFake", "pred_category": CATS[k % 8],
                                                        "category_ok": True, "model_ok": False},
                          "param_mid_abs_err": {"Gain": 0.3}, "rank1_audible": i not in miss_ids, "rank1": True})
            (r / f"{i}.json").write_text(json.dumps(d))
        name = "summary.json" if kind == "ours" else "summary_shardall.json"
        (r / name).write_text(json.dumps({"admitted": True, "budget": 300, "gate_evidence": kind == "ours" or None}))

    write("gate2_v3_attempt3", "ours")
    write("v3_base_middle", "middle")
    write("v3_base_random", "random")
    return root


def test_v3_config():
    cfg = G.ATTEMPTS["v3"]
    assert cfg["set"] == "synthetic-v3" and cfg["full"] == "gate2_v3_attempt3" and cfg["dev"] is None and cfg["n"] == 3
    assert cfg["middle"] == "v3_base_middle" and cfg["random"] == "v3_base_random" and not cfg["stages"]
    assert "v3" in G.LATER


def test_v3_discovery_reads_shardall_baselines(tmp_path):
    root = make_v3(tmp_path)
    m3 = G.build_model(root, "v3")
    assert m3["set"] == "synthetic-v3" and m3["full"]["tag"] == "gate2_v3_attempt3" and m3["full"]["scope"] == "full"
    assert m3["middle"]["tag"] == "v3_base_middle" and m3["random"]["tag"] == "v3_base_random"
    assert m3["middle"]["admitted"] and m3["random"]["admitted"]
    assert G.build_model(root, "v2")["full"]["tag"] == "gate2_v2_attempt2"      # earlier attempts untouched


def test_v3_verdict_all_checks_met(tmp_path):
    m3 = G.build_model(make_v3(tmp_path, miss_ids=("i005", "i006")), "v3")
    ev = G.evaluate_pass(m3["full"]["items"], m3["random"]["items"], 96, m3["d"]["all_ids"])
    assert G.count_checks(ev) == (7, 7) and ev["all_ok"] is True
    assert "met all 7 checks" in G.section_headline(m3) and "signs it" in G.section_headline(m3)


def test_v3_page_headline_amendment_and_earlier_attempts(tmp_path):
    h = G.build(make_v3(tmp_path), tmp_path / "out").read_text()
    assert h.index("id='attempt3'") < h.index("id='attempt2'") < h.index("id='compare'") < h.index("id='results'")
    assert "Gate 2 attempt 3 (latest): All 7 checks met, awaiting the human&#x27;s sign-off" in h or \
        "All 7 checks met, awaiting the human" in h
    assert "Gate 2 attempt 2 (earlier attempt): NOT PASSED, 6 of 7 checks met" in h
    assert "What changed in the rule, in plain words" in h and "stay as they were recorded, under the old rule" in h
    assert "The three attempts side by side" in h and "By family of effect: exact model right" in h
    assert "The biggest problem now: delay" in h and "Extended rule B" in h
    assert "Results of attempt 1 (the earlier attempt)" in h and "<script" not in h
    for sec in ("attempt3", "attempt2", "compare", "what", "method", "results", "specialists", "examples", "audio", "next", "glossary"):
        assert f"id='{sec}'" in h


def test_v3_audio_stereo_for_every_pair_and_delay_players(tmp_path):
    root = make_v3(tmp_path, miss_ids=("i005", "i006"))
    x = (0.1 * np.sin(2 * np.pi * 220 * np.arange(48000 * 6) / 48000)).astype(np.float32)
    d = root / "cache" / "synthetic-v3" / "runs"
    for tag in ("gate2_v3_attempt3", "v3_base_middle"):
        for i in ("i001", "i005", "i006", "i009", "i017"):          # i001, i009, i017 are delay items (k % 8 == 1)
            sf.write(str(d / tag / f"{i}_target.wav"), x, 48000)
            sf.write(str(d / tag / f"{i}_recovered.wav"), x * 0.5, 48000)
    out = tmp_path / "out"
    h = G.build(root, out).read_text()
    lr = sorted((out / "audio").rglob("*_L-target_R-recovered.wav"))
    assert len(lr) == 10 and all(sf.read(str(f))[0].shape[1] == 2 for f in lr)
    assert any("Attempt_3_" in str(f) for f in lr)
    assert "delay miss" in h and "rank-1 miss" in h and "Attempt 3 (latest)" in h


def test_v1_v2_only_project_keeps_attempt2_as_latest(tmp_path):
    h = G.build(make_v2(tmp_path, miss_ids=SIX_MISSES), tmp_path / "out").read_text()
    assert "id='attempt3'" not in h and "Gate 2 attempt 2 (latest): NOT PASSED, 6 of 7 checks met" in h
