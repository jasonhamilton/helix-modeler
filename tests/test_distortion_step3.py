"""Tests for the T174 step-3 code (dfp-v1 probe, settings plan, cards, identification).
Research code; nothing is adopted. The two full-probe analyses take about 6 s each."""
import numpy as np
import pytest

from tonematch.metrics import distortion_probe as P
from tonematch.metrics import distortion_step3 as S3
from tonematch.metrics import drive_fp_mining as DM

SR = P.SR


@pytest.fixture(scope="module")
def probe_v1():
    return P.build_probe("v1")


def _hard(x, thr_dbfs=-40.0, gain_db=12.0):
    """Exactly linear below the threshold, hard clip above (the Native-like switch-on)."""
    t = 10 ** (thr_dbfs / 20)
    return 10 ** (gain_db / 20) * np.clip(x, -t, t)


def test_v1_probe_staircase_starts_near_minus_90(probe_v1):
    sig, lay = probe_v1
    assert lay["version"] == "dfp-v1" and lay["kind"] == "v1"
    L = lay["stair_levels"]
    assert L[0] == -91.0 and L[-1] == -1.0 and len(L) == 19 and -6.0 in L
    assert np.all(np.diff(L) == 5.0)
    st = [s for s in lay["segments"] if s["name"] == "stair"]
    assert all(s["n"] == s["step_n"] * 19 for s in st)
    assert lay["sweep_T"] == pytest.approx(P.build_probe("main")[1]["sweep_T"])     # 6 s sweeps kept
    _, lay_p = P.build_probe("main")
    assert lay_p["stair_levels"] == P.STAIR_LEVELS and len(P.STAIR_LEVELS) == 15    # pilot unchanged
    assert sig.size / SR < 120.0


def test_harmonic_onset_states():
    L = np.arange(-91.0, 0.0, 5.0)
    st = np.full((9, L.size), -300.0)
    st[0] = L
    st[2, 10:] = L[10:] - 30.0                                  # H3 at -30 dB from step 10 on
    on = P.harmonic_onset(L, st)
    assert on["state"] == "within" and on["onset_dbfs"] == L[10] and on["below_db"] < -200
    st[2, :] = L - 30.0
    assert P.harmonic_onset(L, st)["state"] == "below"
    st[2, :] = -300.0
    assert P.harmonic_onset(L, st) == {"onset_dbfs": None, "state": "never", "below_db": pytest.approx(-300.0 - L.min())}


def test_mid_feature_hump_scoop_flat():
    f = P.FGRID
    lf = np.log2(f / 1000.0)
    hump = -6.0 * lf ** 2                                       # peak at 1 kHz
    scoop = 4.0 * np.tanh(lf ** 2) - 4.0                        # 4 dB dip at 1 kHz
    flat = np.zeros_like(f)
    assert S3.P.mid_feature(f, hump, flat)["kind"] == "hump"
    m = P.mid_feature(f, flat, scoop)
    assert m["kind"] == "scoop" and m["db"] < -3.0 and 700 < m["f_hz"] < 1400
    assert P.mid_feature(f, flat, flat)["kind"] == "flat"


def test_settings_plan_twelve_per_model():
    two = S3.settings_for("Gain", "Tone")
    one = S3.settings_for("Fuzz", None)
    assert len(two) == 12 and len(one) == 12
    assert len({n for n, *_ in two}) == 12 and len({n for n, *_ in one}) == 12
    grid = [cc for _, cc, k in two if k == "grid"]
    assert sorted((c["Gain"], c["Tone"]) for c in grid) == sorted((d, t) for d in (25, 64, 108) for t in (25, 102))
    assert {k for *_, k in two} == {"grid", "centre", "end", "off"}
    assert [cc["Fuzz"] for _, cc, _ in one][0] == 0 and [cc["Fuzz"] for _, cc, _ in one][-1] == 127
    assert all(0 <= c <= 127 for _, cc, _ in two + one for c in cc.values())


def test_knob_semantics_needs_mapped_knobs():
    params = {"Gain": {}, "Tone": {}, "Level": {}, "Mode": {}}
    tmap = {"0:Gain": "cc_5", "0:Tone": "cc_6", "0:Level": "cc_7"}
    k = S3.knob_semantics("HD2_DistPillars", params, tmap)
    assert k["drive"] == "Gain" and k["tone"] == "Tone"
    assert k["level_fixed"] == ["Level"] and k["unmapped_at_default"] == ["Mode"]
    with pytest.raises(KeyError):
        S3.knob_semantics("HD2_DistPillars", params, {"0:Gain": "cc_5"})
    assert set(S3.KNOBS) <= set(DM.LABELS)                      # every model has reference labels


def test_loso_ranks_and_rates():
    rng = np.random.default_rng(0)
    models = [m for m in ("a", "b", "c") for _ in range(4)]
    centres = {"a": 0.0, "b": 10.0, "c": 20.0}
    Z = np.array([[centres[m] + 0.1 * rng.standard_normal()] for m in models])
    ranks, nw, ms = S3.loso(models, S3.pairwise(Z))
    assert ms == ["a", "b", "c"] and np.all(ranks == 1)
    assert [ms[i] for i in nw[:4]] == ["b"] * 4                  # nearest wrong model of "a" is "b"
    Z[0, 0] = 8.0                                               # one "a" setting sits near "b"
    ranks, _, _ = S3.loso(models, S3.pairwise(Z))
    assert ranks[0] == 2 and np.all(ranks[1:] == 1)
    r = S3.rates(ranks, models, ["grid"] * 12)
    assert r["top1"][0] == pytest.approx(11 / 12) and r["top5"][0] == 1.0


@pytest.fixture(scope="module")
def fp_hard(probe_v1, tmp_path_factory):
    sig, _ = probe_v1
    path = tmp_path_factory.mktemp("fp") / "m" / "s.npz"
    r = S3.fingerprint_job(_hard(sig).astype(np.float32), str(path))
    return r, P.unpack(path.read_bytes())


def test_fingerprint_job_stores_card_quantities_no_audio(fp_hard):
    r, (arr, sc) = fp_hard
    assert r["bytes"] < 9000 and max(v.size for v in arr.values()) < 1000
    assert "odd_harmonic_residual_db" not in sc                 # the non-W-H flag is dropped
    c = S3.card_values(sc, arr, P.STAIR_LEVELS_V1)
    assert c["onset_state"] == "within" and -46.0 <= c["onset_dbfs"] <= -36.0     # threshold -40 dBFS
    assert c["knee_state"] == "within" and -42.0 < c["knee_dbfs"] < -30.0
    assert c["even_share_db"] < -60.0                            # symmetric
    assert abs(c["g0_db"] - 12.0) < 0.2
    f = S3.card_features(c)
    assert set(f) >= {"knee", "onset", "even", "log_pre", "log_post", "mid_db", "g0"}
    assert "g0" not in S3.card_features(c, level=False)


def test_clean_gain_card_has_no_knee_and_no_onset(probe_v1):
    sig, lay = probe_v1
    arr, sc = P.analyse(0.5 * sig, lay)
    c = S3.card_values(sc, arr, P.STAIR_LEVELS_V1)
    assert c["knee_state"] == "none" and c["onset_state"] == "never"


def test_model_card_knob_effects():
    sem = {"drive": "Gain", "tone": "Tone"}
    ss = []
    for name, cc, kind in S3.settings_for("Gain", "Tone"):
        d, t = cc["Gain"] / 127, cc["Tone"] / 127
        card = {"knee_dbfs": -20 - 30 * d, "onset_dbfs": -25 - 30 * d, "even_share_db": -80.0,
                "pre_hp_hz": 700.0, "post_lp_hz": 1000.0 * 2 ** (3 * t), "mid_db": 5.0, "g0_db": 10 + 20 * d,
                "knee_state": "within", "onset_state": "within", "mid_kind": "hump", "mid_f_hz": 900.0,
                "memory": False}
        ss.append({"cc": cc, "kind": kind, "card": card})
    c = S3.model_card("HD2_DistScream808", sem, ss)
    v = c["values"]
    assert v["knee_dbfs"]["drive_effect"] == pytest.approx(-30 * (108 - 25) / 127)
    assert v["knee_dbfs"]["tone_effect"] == pytest.approx(0.0)
    assert v["post_lp_hz"]["tone_effect"] == pytest.approx(3 * (102 - 25) / 127)   # octaves
    assert v["post_lp_hz"]["drive_effect"] == pytest.approx(0.0)
    assert c["labels"][2] == "soft_od"
