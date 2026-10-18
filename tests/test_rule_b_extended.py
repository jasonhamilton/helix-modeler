"""Extended rule B (human 2026-10-18, T146): level-only distractors within the aligned capture's own
gain jitter are ties. Shared helper `tonematch.metrics.trust`, and its three callers
(loss_trust_check --helix, synthetic_v1 rank-1, gain_trust). Offline, no engine."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch.metrics import trust

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _sig(seed=0, n=48_000):
    rng = np.random.default_rng(seed)
    return np.convolve(rng.standard_normal(n), np.ones(8) / 8, mode="same")


def _db(x, d):
    return x * 10 ** (d / 20)


def _rank(truth, ys, scores, range_db):
    nf = [trust.null_fields(truth, y) for y in ys]
    return trust.rank1_rule_b(scores[0], scores[1:], [f["null_db"] for f in nf],
                              gm_null_db=[f["gm_null_db"] for f in nf], gm_gain_db=[f["gm_gain_db"] for f in nf],
                              level_range_db=range_db)


def test_gain_match_reports_the_level_and_a_deep_null():
    x = _sig()
    gm, g = trust.gain_match(x, _db(x, 2.0))
    assert g == pytest.approx(2.0, abs=1e-9) and gm < -200
    gm, g = trust.gain_match(x, _db(x, -5.0))
    assert g == pytest.approx(-5.0, abs=1e-9) and gm < -200
    # silence and a polarity flip are not level matches
    assert trust.gain_match(x, 0 * x) == (0.0, float("-inf"))
    assert trust.gain_match(x, -x)[1] == float("-inf")
    # the direct null of a 2 dB gain is audible (above -30 dB)
    assert trust.null_db(x, _db(x, 2.0)) > -30


def test_range_comes_from_the_capture_setting():
    assert trust.capture_gain_range_db(None) == trust.ALIGNED_CAPTURE["gain_db"] == 3.0
    assert trust.capture_gain_range_db({"gain_db": 1.5}) == 1.5
    import helix_trust_v2 as H2
    import loss_trust_check as LTC

    # one capture: the script's aligned_capture is the helper's, and helix-trust-v2 records the same setting
    assert LTC.aligned_capture is trust.aligned_capture
    assert H2.ALIGNED == trust.ALIGNED_CAPTURE


@pytest.mark.parametrize("gain_db,tie", [(2.0, True), (-2.0, True), (3.0, True), (5.0, False), (-5.0, False)])
def test_pure_gain_distractor_tie_inside_the_jitter_only(gain_db, tie):
    x = _sig()
    y = _db(x, gain_db)
    # the distractor beats the truth on the score (as it can when the capture is louder)
    r = _rank(x, [y], [0.2, 0.1], trust.capture_gain_range_db(None))
    assert r.rank1_audible is tie
    assert r.n_level_tie == int(tie) and r.n_same_sound == int(tie)
    # the old rule (no gain-matched fields, or range 0) never ties it
    assert not trust.rank1_rule_b(0.2, [0.1], [trust.null_db(x, y)]).rank1_audible
    assert not _rank(x, [y], [0.2, 0.1], 0.0).rank1_audible


def test_tone_different_distractor_unchanged():
    x = _sig()
    lp = np.convolve(x, np.ones(32) / 32, mode="same")       # a different tone, any level
    for y in (lp, _db(lp, 1.0)):
        nf = trust.null_fields(x, y)
        assert nf["gm_null_db"] > -30
        assert not _rank(x, [y], [0.2, 0.1], 3.0).rank1_audible     # beats the truth: still a failure
        assert _rank(x, [y], [0.2, 0.5], 3.0).rank1_audible          # worse than the truth: still a pass
        assert _rank(x, [y], [0.2, 0.1], 3.0).n_level_tie == 0


def test_direct_null_case_unchanged():
    x = _sig()
    y = x + 0.01 * _sig(1)                                     # about -40 dB direct null
    nf = trust.null_fields(x, y)
    assert nf["null_db"] <= -30
    for rng in (0.0, 3.0):
        r = _rank(x, [y], [0.2, 0.1], rng)
        assert r.rank1_audible and r.n_same_sound == 1 and r.n_level_tie == 0
    old = trust.rank1_rule_b(0.2, [0.1, 0.5], [-35.0, -10.0])
    new = trust.rank1_rule_b(0.2, [0.1, 0.5], [-35.0, -10.0], gm_null_db=[-35.0, -12.0], gm_gain_db=[0.0, 0.5],
                             level_range_db=3.0)
    assert old == new


def test_same_sound_shape_check():
    with pytest.raises(ValueError):
        trust.same_sound([-10.0, -10.0], -30.0, [-40.0], [1.0], 3.0)


def test_synthetic_v1_rank1_fields_extended():
    import synthetic_v1 as sv

    rows = [{"loss": 0.2, "null_db": 0.0},
            {"loss": 0.1, "null_db": -14.0, "gm_null_db": -140.0, "gm_gain_db": 2.0},
            {"loss": 0.9, "null_db": -5.0, "gm_null_db": -8.0, "gm_gain_db": 0.1}]
    assert not sv.rank1_fields(rows)["rank1_audible"]            # range 0: the direct rule
    f = sv.rank1_fields(rows, 3.0)
    assert f["rank1_audible"] and f["n_level_tie"] == 1
    rows[1]["gm_gain_db"] = 5.0
    assert not sv.rank1_fields(rows, 3.0)["rank1_audible"]


def test_loss_trust_check_extras_extended():
    import loss_trust_check as LTC

    x = _sig()
    ys = [x, _db(x, 2.0), 0.3 * np.convolve(x, np.ones(32) / 32, mode="same")]
    gm = [trust.gain_match(x, y) for y in ys]
    it = {"category": "amp", "labels": ["true", "near:a", "near:b"],
          "null_db_same_notes": [trust.null_db(x, y) for y in ys],
          "gm_null_db_same_notes": [g[0] for g in gm], "gm_gain_db_same_notes": [g[1] for g in gm]}
    sc = [0.2, 0.1, 0.9]
    for s in ("clean/same_notes", "aligned/same_notes"):
        it[s] = {"mrstft_v2": {"scores": sc, "rank": 2}}
    ex = LTC.helix_extras([it], ["mrstft_v2"], ("clean/same_notes", "aligned/same_notes"), {"gain_db": 3.0})
    assert ex["clean/same_notes"]["mrstft_v2"]["rank1_audible_only"] == 0.0     # no jitter, no level tie
    assert ex["aligned/same_notes"]["mrstft_v2"]["rank1_audible_only"] == 1.0
    old = dict(it)
    del old["gm_null_db_same_notes"], old["gm_gain_db_same_notes"]               # pre-T146 items: direct rule
    ex = LTC.helix_extras([old], ["mrstft_v2"], ("aligned/same_notes",), {"gain_db": 3.0})
    assert ex["aligned/same_notes"]["mrstft_v2"]["rank1_audible_only"] == 0.0


def test_gain_trust_evaluate_extended():
    import gain_trust as GT

    it = {"item": 0, "category": "drive", "model": "m", "labels": ["true", "near", "pair"],
          "mrstft_v2": [0.2, 0.1, 0.9], "level_db": [0.0, 0.0, 0.0],
          "null_db": [-14.0, -6.0], "gm_null_db": [-140.0, -8.0], "gm_gain_db": [2.0, 0.0],
          "level_vs_truth_db": [2.0, 0.0], "cap_gain_db": 2.8}
    assert GT.evaluate([it], {"mrstft_v2": 1.0}, 0.0)["rank1"] == 0.0
    r = GT.evaluate([it], {"mrstft_v2": 1.0}, 3.0)
    assert r["rank1"] == 1.0 and r["level_ties"] == 1 and r["unrankable_share"] == 0.0
    it["gm_gain_db"], it["level_vs_truth_db"] = [5.0, 0.0], [5.0, 0.0]
    r = GT.evaluate([it], {"mrstft_v2": 1.0}, 3.0)
    assert r["rank1"] == 0.0 and r["unrankable_share"] == 1.0
