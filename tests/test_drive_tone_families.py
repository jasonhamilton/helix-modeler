"""T179 / T180: tone-knob ends and drive families (measurement code; synthetic inputs, no Native)."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.metrics import drive_fp_mining as DM
from tonematch.metrics import drive_tone_families as T

NF = 30


def _line(g, pattern=None, base=0.0):
    """Response blocks along the 7-point tone line; g(pos) scales a fixed EQ pattern."""
    pat = np.linspace(-1, 1, NF) if pattern is None else pattern
    return {c: np.tile(base + g(T.pos(c)) * pat, (3, 1)) for c in T.LINE_CC}


def _shape(blocks):
    s = T.tone_line_summary(blocks)
    return s, T.knob_shape(s["bottom"]["per_0p1_db"], s["middle"]["per_0p1_db"], s["top"]["per_0p1_db"],
                           [x["per_0p1"] for x in s["steps"]], s["total_db"])


def test_tone_end_settings():
    s = T.tone_end_settings("Gain", "Tone")
    assert len(s) == 8
    assert {c["Tone"] for _, c, _ in s} == {0, 13, 114, 127}
    assert {c["Gain"] for _, c, _ in s} == {64, 108}
    assert all(k == "tone_end" for *_, k in s)
    assert T.tone_end_settings("Fuzz", None) == []
    assert sorted(set(T.END_CC) | set(T.STEP3_TONE_CC)) == list(T.LINE_CC)


def test_step_effect_level_and_shape():
    a = np.zeros((3, NF))
    e = T.step_effect(a, a + 4.0)                 # a pure level change
    assert e["rms_db"] == pytest.approx(4.0)
    assert e["shape_db"] == pytest.approx(0.0, abs=1e-12)
    assert e["level_db"] == pytest.approx(4.0)
    tilt = np.tile(np.linspace(-3, 3, NF), (3, 1))  # a pure shape change, zero mean
    e = T.step_effect(a, tilt)
    assert e["level_db"] == pytest.approx(0.0, abs=1e-12)
    assert e["shape_db"] == pytest.approx(e["rms_db"])


def test_linear_knob():
    s, sh = _shape(_line(lambda p: 10 * p))
    assert sh["shape"] == "linear"
    assert s["total_db"] == pytest.approx(s["end_to_end"]["rms_db"])     # monotone: path = direct
    assert s["bottom"]["share"] == pytest.approx(s["bottom"]["effect_db"] / s["total_db"])
    for r in ("bottom", "middle", "top"):                                   # constant per 0.1
        assert s[r]["per_0p1_db"] == pytest.approx(s["middle"]["per_0p1_db"], rel=1e-6)


def test_end_loaded_knob():
    g = lambda p: 0.2 * p + (6.0 if p >= 0.95 else 0.0) - (6.0 if p <= 0.01 else 0.0)  # noqa: E731
    s, sh = _shape(_line(g))
    assert sh["shape"] == "end-loaded" and sh["ends"] == "both"
    assert s["bottom"]["share"] + s["top"]["share"] > 0.8
    g = lambda p: 0.2 * p + (6.0 if p >= 0.95 else 0.0)  # noqa: E731
    _, sh = _shape(_line(g))
    assert sh["shape"] == "end-loaded" and sh["ends"] == "top"


def test_taper_and_inert():
    _, sh = _shape(_line(lambda p: 10 * (np.exp(4 * p) - 1) / (np.e ** 4 - 1)))
    assert sh["shape"] == "taper" and sh["ends"] == "top"
    _, sh = _shape(_line(lambda p: 0.3 * p))
    assert sh["shape"] == "little effect"


def test_model_report_and_search_entry():
    lines = {64: _line(lambda p: 10 * p), 108: _line(lambda p: 12 * p, base=3.0)}
    rep = T.model_tone_report("M", {"drive": "Gain", "tone": "Tone"}, lines, (-12.0, 12.0))
    assert rep["shape"] == "linear" and rep["combined"]["rms_db"]["drives_agree"]
    e = T.search_entry(rep)
    assert e["range"] == [-12.0, 12.0]
    assert set(e["effect_per_0p1_db"]) == {"bottom", "middle", "top"}
    assert e["regions"]["top"][1] == 1.0
    assert e["knob_step_for_1db"]["middle"] == pytest.approx(0.1 / e["effect_per_0p1_db"]["middle"], rel=0.02)
    # equal effect at every input level: the knob acts after the clipper
    v = list(rep["end_to_end_by_level_db"].values())
    assert max(v) - min(v) < 1e-9


def test_response_block_floor_and_band():
    from tonematch.metrics import distortion_probe as P

    M1 = np.full((len(P.FGRID), len(P.SWEEP_LEVELS)), -300.0)
    b = T.response_block({"M1": M1})
    f = np.asarray(P.FGRID)
    assert b.shape == (3, int(np.sum((f >= 100) & (f <= 10000))))
    assert np.all(b == T.FLOOR_DB)


def _blobs(k=4, per=5, settings=6, dims=8, seed=0):
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 10, (k, dims))
    models, rows = [], []
    for c in range(k):
        for j in range(per):
            mc = centres[c] + rng.normal(0, 0.5, dims)
            for s in range(settings):
                models.append(f"m{c}_{j}")
                rows.append(mc + rng.normal(0, 0.3, dims))
    return models, np.array(rows)


def test_consensus_recovers_clear_families():
    models, Z = _blobs()
    rep = T.consensus_report(models, Z, ks=[3, 4, 5], n=40, seed=0)
    ms = sorted(set(models))
    truth = [m.split("_")[0] for m in ms]
    assert DM.ari(truth, rep[4]["families"]) == pytest.approx(1.0)
    assert rep[4]["halves_ari"] == pytest.approx(1.0)
    assert rep[4]["pac"] <= rep[5]["pac"] + 1e-9 or rep[4]["pac"] < 0.05
    assert min(rep[4]["confidence"].values()) > 0.9
    assert T.choose_k_consensus(rep, ms, flagged=()) == 4


def test_choose_k_singleton_rule():
    fam_ok = np.array([1, 1, 2, 2, 3])          # one single-model family
    fam_bad = np.array([1, 2, 3, 4, 4])         # three
    rep = {3: {"families": fam_ok, "halves_ari": 1.0, "pac": 0.3},
           4: {"families": fam_bad, "halves_ari": 1.0, "pac": 0.1}}
    ms = ["a", "b", "c", "d", "e"]
    assert T.choose_k_consensus(rep, ms, flagged=(), max_singletons=2) == 3
    # flagged models may stand alone
    assert T.choose_k_consensus(rep, ms, flagged=("a", "b", "c"), max_singletons=0) == 4


def test_rapp_smoothness_filters_bad_fits():
    assert T.rapp_smoothness({"rapp_pos": {"p": 3.0, "rms_db": 0.1}, "rapp_neg": {"p": 3.0, "rms_db": 0.1}}) == \
        pytest.approx(3.0)
    assert T.rapp_smoothness({"rapp_pos": {"p": 200.0, "rms_db": 0.1}, "rapp_neg": {"p": 0.2, "rms_db": 0.1}}) is None
    assert T.rapp_smoothness({"rapp_pos": {"p": 4.0, "rms_db": 5.0}, "rapp_neg": {"p": 2.0, "rms_db": 0.5}}) == \
        pytest.approx(2.0)


def test_describe_family_names_measured_character():
    ch = {"even_db": -140.0, "knee_dbfs": -46.0, "onset_dbfs": -46.0, "onset_below_start": False,
          "knee_none": False, "rapp_p": 2.2, "pre_hp_hz": 600.0, "post_lp_hz": 700.0, "mid_db": 11.0,
          "mid_kind": "hump", "mid_f_hz": 800.0, "drive_knee_move_db": -15.0, "drive_gain_move_db": 15.0}
    d = T.describe_family([ch, dict(ch, rapp_p=2.4)], [("hump", 11.0, 800.0), ("hump", 12.0, 800.0)])
    assert d["name"].startswith("soft symmetric, medium gain, mid-hump 0.8 kHz, pre-HPF")
    assert "drive after the clipper" not in d["name"]
    fz = dict(ch, even_db=-10.0, rapp_p=None, pre_hp_hz=None, post_lp_hz=None, knee_dbfs=-70.0,
              drive_knee_move_db=0.0, drive_gain_move_db=25.0)
    d = T.describe_family([fz], [("scoop", 6.0, 900.0)])
    assert d["name"] == "asymmetric, high gain, mid-scoop 0.9 kHz, drive after the clipper"


def test_label_disagreements():
    ms = ["HD2_DistScream808", "HD2_DM4Screamer", "HD2_DistTriangleFuzz", "HD2_DM4FuzzPi", "HD2_DistKWB"]
    fam = {"HD2_DistScream808": 1, "HD2_DM4Screamer": 1, "HD2_DistTriangleFuzz": 2, "HD2_DM4FuzzPi": 1,
           "HD2_DistKWB": 2}
    d = T.label_disagreements(ms, fam)
    assert "A" in d["groups_kept_together"]
    assert "G" in d["groups_split"]
    assert any(x["model"] == "HD2_DM4FuzzPi" for x in d["minority_members"])


def test_suggested_tol_equal_effect():
    e = {"effect_per_0p1_db": {"bottom": 4.0, "middle": 1.0, "top": 1.0}}
    s = T.suggested_tol(e)
    assert s["tone_tol"] == 0.1
    assert s["tone_end_tol_bottom"] == pytest.approx(0.025)
    assert s["tone_end_tol_top"] == pytest.approx(0.1)          # capped: no looser than the middle
    assert s["tone_end_tol"] == pytest.approx(0.025)
    assert T.suggested_tol({"effect_per_0p1_db": {"bottom": 100.0, "middle": 1.0, "top": 0.0}})["tone_end_tol"] == 0.02


def test_outputs_load_with_search_loaders(tmp_path):
    """The two hand-off formats load with search's T178 loaders."""
    import json

    from tonematch.search import knob_tol as KT
    from tonematch.search import shortlist as SL

    smap = {"version": "x", "models": {"HD2_DistScream808": {"drive_knobs": ["Gain"], "tone_knobs": ["Tone"],
                                                             "knobs": {"Tone": {"shape": "end-loaded"}},
                                                             "suggested_tol": {"tone_end_tol": 0.03}}}}
    p = tmp_path / "ks.json"
    p.write_text(json.dumps(smap))
    pm, src = KT.load_sensitivity(p)
    kt = KT.KnobTol(per_model=pm).for_model("HD2_DistScream808")
    assert kt.is_tone("Tone") and not kt.is_tone("Gain")
    assert kt.tolerance("Tone", 0.5) == pytest.approx(KT.TONE_TOL)    # no tolerance keys: search defaults
    fam = {"families": {"F01": ["HD2_DistScream808", "HD2_DM4Screamer"], "F02": ["HD2_DistKWB"]},
           "descriptions": {"F01": "medium symmetric", "F02": "x"}}
    q = tmp_path / "fam.json"
    q.write_text(json.dumps(fam))
    got = SL.load_families(q)
    assert got["HD2_DM4Screamer"] == ["HD2_DistScream808", "HD2_DM4Screamer"]
    assert got["HD2_DistKWB"] == ["HD2_DistKWB"]


def test_middle_loaded_and_falling_taper():
    # an S-shaped knob: nearly all of the change in the middle
    _, sh = _shape(_line(lambda p: 10 / (1 + np.exp(-12 * (p - 0.5)))))
    assert sh["shape"] == "middle-loaded"
    # effect concentrated at the bottom and fading steadily (a log-like taper read backwards)
    _, sh = _shape(_line(lambda p: 10 * np.log1p(30 * p) / np.log1p(30)))
    assert sh["shape"] == "taper" and sh["ends"] == "bottom"


def test_level_dependent_flag():
    lines = {64: _line(lambda p: 10 * p), 108: _line(lambda p: 10 * p)}
    for d in lines:                     # make the quiet level move twice as much
        for c in lines[d]:
            lines[d][c][0] *= 2
    rep = T.model_tone_report("M", {"drive": "Gain", "tone": "Tone"}, lines)
    assert T.search_entry(rep)["level_dependent"] is True
