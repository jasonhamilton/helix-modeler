"""Rule B (T096): the shared rank-1 helper, mrstft_v2, and the helix-trust-v2 trust check on a fake set.

Offline: proxy DSP renders only, no engine."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch.metrics import mrstft, mrstft_v2, proxy, trust
from tonematch.metrics.proxy import ProxyPreset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


# ------------------------------------------------------------------ rule B helper


def test_null_db_plain_not_gain_matched():
    x = np.random.default_rng(0).standard_normal(4800)
    assert trust.null_db(x, x) < -250
    assert trust.null_db(x, 0.5 * x) == pytest.approx(20 * np.log10(0.5), abs=1e-9)
    assert trust.null_db(x, 0.0 * x) == pytest.approx(0.0, abs=1e-9)


def test_same_sound_distractor_is_a_tie_not_a_failure():
    # distractor 0 beats the truth but nulls at -35 dB (same sound); distractor 1 is audible and worse
    r = trust.rank1_rule_b(0.2, [0.1, 0.5], [-35.0, -10.0])
    assert not r.rank1 and r.rank1_audible
    assert r.rank == 2 and r.rank_audible == 1 and r.n_same_sound == 1
    # exactly at the threshold counts as the same sound
    assert trust.rank1_rule_b(0.2, [0.1, 0.5], [-30.0, -10.0]).rank1_audible
    # an audible distractor that beats the truth fails both
    r = trust.rank1_rule_b(0.2, [0.1, 0.5], [-29.9, -10.0])
    assert not r.rank1 and not r.rank1_audible
    # a score tie with an audible distractor counts against the truth
    assert not trust.rank1_rule_b(0.2, [0.2, 0.5], [-10.0, -10.0]).rank1_audible


def test_margin_and_rates():
    r = trust.rank1_rule_b(1.0, [2.0, 3.0, 4.0], [-5.0, -5.0, -40.0])
    assert r.margin == pytest.approx((2.0 - 1.0) / 3.0)
    assert r.margin_audible == pytest.approx((2.0 - 1.0) / 2.5)
    allsame = trust.rank1_rule_b(1.0, [0.5], [-60.0])
    assert allsame.rank1_audible and np.isnan(allsame.margin_audible)
    s = trust.rates([r, allsame, trust.rank1_rule_b(1.0, [0.5], [-3.0])])
    assert s["rank1_rate"] == pytest.approx(1 / 3) and s["rank1_audible_only"] == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        trust.rank1_rule_b(0.0, [1.0, 2.0], [-1.0])


def test_synthetic_v1_rank1_uses_rule_b():
    import synthetic_v1 as sv

    rows = [{"loss": 0.2, "null_db": 0.0}, {"loss": 0.1, "null_db": -40.0}, {"loss": 0.9, "null_db": -5.0}]
    f = sv.rank1_fields(rows)
    assert f["rank1"] is False and f["rank1_audible"] is True and f["n_same_sound"] == 1
    assert f["neighbour_rank"] == 2 and f["neighbour_rank_audible"] == 1
    rows[1]["null_db"] = -20.0
    assert sv.rank1_fields(rows)["rank1_audible"] is False


# ------------------------------------------------------------------ mrstft_v2


@pytest.fixture(scope="module")
def pair():
    d = proxy.guitar_di(3, 4.0)
    return proxy.render(ProxyPreset(drive=0.4, cab=0), d), proxy.render(ProxyPreset(drive=0.6, cab=1), d), d


def test_v1_config_reproduces_mrstft(pair):
    a, b, _ = pair
    assert mrstft_v2.term(a, b, config=mrstft_v2.V1) == pytest.approx(mrstft.term(a, b), rel=1e-6)


CONFIGS = [mrstft_v2.DEFAULT, mrstft_v2.V1,
           mrstft_v2.Config(level="active_rms", bands="mel", n_bands=64, silence_db=-50, floor_db=-80, w_lin=1.0),
           mrstft_v2.Config(level="median_log", bands="erb", a_weight=True, fft_sizes=(256, 1024), hop_div=8,
                            w_sc=0.5, floor_db=-70)]


@pytest.mark.parametrize("cfg", CONFIGS)
def test_v2_identity_and_level_invariance(pair, cfg):
    a, b, _ = pair
    assert mrstft_v2.term(a, a, config=cfg) == pytest.approx(0.0, abs=1e-6)
    base = mrstft_v2.term(a, b, config=cfg)
    assert base > 0.02
    # never absolute level: the level guard owns loudness
    assert mrstft_v2.term(0.1 * a, b, config=cfg) == pytest.approx(base, rel=1e-3)
    assert mrstft_v2.term(a, 5.0 * b, config=cfg) == pytest.approx(base, rel=1e-3)


@pytest.mark.parametrize("cfg", CONFIGS)
def test_v2_is_aligned_only(cfg):
    """Like mrstft, NOT content-invariant: other notes cost more than another tone on the same notes."""
    da, db = proxy.guitar_di(11, 4.0, rhythm_seed=7), proxy.guitar_di(12, 4.0, rhythm_seed=7)
    a, b = ProxyPreset(drive=0.4, cab=0), ProxyPreset(drive=0.6, cab=1)
    other_notes = mrstft_v2.term(proxy.render(a, da), proxy.render(a, db), config=cfg)
    other_tone = mrstft_v2.term(proxy.render(a, da), proxy.render(b, da), config=cfg)
    assert other_tone < other_notes


def test_silence_gate_ignores_differences_in_near_silent_frames(pair):
    a, _, _ = pair
    x = np.concatenate([a, np.zeros(48_000)])
    rng = np.random.default_rng(1)
    y = x.copy()
    y[a.size:] += 1e-5 * np.std(a) * rng.standard_normal(48_000)      # -100 dB hiss in the silence only
    gated = mrstft_v2.Config(silence_db=-60.0, floor_db=-120.0)
    open_ = gated.with_(silence_db=None)
    assert mrstft_v2.term(x, y, config=gated) < 1e-3 < mrstft_v2.term(x, y, config=open_)


def test_weighting_shapes():
    w = mrstft_v2.weighting(2048, "mel", 64, False, 20.0, 20_000.0)
    assert w.shape == (1025, 64) and w.min() >= 0
    small = mrstft_v2.weighting(128, "erb", 128, True, 20.0, 20_000.0)
    assert 0 < small.shape[1] < 128            # bands narrower than a bin are dropped
    assert mrstft_v2.weighting(512, "linear", 0, False, 20.0, 20_000.0) is None
    aw = mrstft_v2.weighting(512, "linear", 0, True, 20.0, 20_000.0)
    assert aw.shape == (257,)
    p = np.ones((3, 1025))
    assert mrstft_v2.apply_weighting(p, w).shape == (3, 64)
    assert abs(mrstft_v2.a_weight_db(np.array([1000.0]))[0]) < 0.01


def test_config_validation():
    with pytest.raises(ValueError):
        mrstft_v2.Config(level="absolute")
    with pytest.raises(ValueError):
        mrstft_v2.Config(fft_sizes=(500,))
    with pytest.raises(ValueError):
        mrstft_v2.Config(bands="bark")


# ------------------------------------------------------------------ helix-trust-v2 layout


def _fake_v2_set(root: Path, seconds: float = 2.0):
    rng = np.random.default_rng(5)
    p = proxy.random_preset(rng)
    di = proxy.guitar_di(4, seconds)
    labels = ["true", "near", "nbr:m2", "random:eq", "random:cab"]
    presets = [p, proxy.perturb_group(p, "cab", rng)] + [proxy.random_preset(rng) for _ in labels[2:]]
    items = []
    for i, split in enumerate(("train", "test")):
        d = root / "renders" / f"item{i:03d}"
        d.mkdir(parents=True)
        for k, q in enumerate(presets):
            np.save(d / f"c{k:02d}_A.npy", proxy.render(q, di).astype(np.float32))
        items.append({"item": i, "dir": f"item{i:03d}", "split": split, "category": "cab", "true_model": "m",
                      "di": {"A": {}}, "candidates": [{"label": lab} for lab in labels]})
    mf = {"dataset": "helix-trust-v2-fake", "seed": 9, "seconds": seconds, "items": items,
          "subsets": ["clean/same_notes", "aligned/same_notes", "sep/same_notes"],
          "aligned": {"frac_delay_samples": 0.5, "gain_db": 3.0, "noise_db_re_rms": -60.0}, "sep_bleed_db": -12.0}
    (root / "manifest.json").write_text(json.dumps(mf))


def test_aligned_capture_is_seeded_and_at_the_stated_noise():
    import loss_trust_check as LTC

    x = proxy.render(ProxyPreset(drive=0.3), proxy.guitar_di(2, 2.0)).astype(np.float32)
    a1 = LTC.aligned_capture(x, np.random.default_rng(1))
    a2 = LTC.aligned_capture(x, np.random.default_rng(1))
    assert np.array_equal(a1, a2) and a1.shape == x.shape
    nul = trust.null_db(x, a1)
    assert -40 < nul < -3                         # the gain offset dominates the null
    z = LTC.aligned_capture(x, np.random.default_rng(1), frac_delay_samples=0.0, gain_db=0.0)
    assert trust.null_db(x, z) == pytest.approx(-60.0, abs=0.5)


def test_trust_check_v2_split_and_rule_b(tmp_path):
    _fake_v2_set(tmp_path)
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--helix",
                        "--helix-dir", str(tmp_path), "--split", "test", "--term", "mrstft", "mrstft_v2", "ltas_tilt",
                        "--jobs", "1", "--out", str(out)], check=False, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["dataset"] == "helix-trust-v2-fake" and rep["split"] == "test" and rep["items"] == 1
    assert rep["per_item"][0]["split"] == "test"
    assert rep["subsets"] == ["clean/same_notes", "aligned/same_notes", "sep/same_notes"]
    for s in rep["subsets"]:
        for t in ("mrstft", "mrstft_v2", "ltas_tilt"):
            h = rep["summary"][s][t]
            assert {"rank1_rate", "rank1_audible_only", "rank1_audible_vs_near_only",
                    "rank1_audible_by_category", "margin_audible_median"} <= set(h)
    # clean: the true candidate is the target, so every distance term ranks it first
    assert rep["summary"]["clean/same_notes"]["mrstft_v2"]["rank1_rate"] == 1.0
    assert rep["mrstft_v2_config"] == mrstft_v2.DEFAULT.as_dict()


def test_tuner_reads_train_only(tmp_path):
    import mrstft_tune as MT

    assert MT.SPLIT == "train"
    _fake_v2_set(tmp_path)
    stage = tmp_path / "s.json"
    stage.write_text(json.dumps({"name": "t", "configs": [{}, {"fft_sizes": [1024], "hop_div": 2}]}))
    out = tmp_path / "t.json"
    assert MT.main(["--stage", str(stage), "--set-dir", str(tmp_path), "--jobs", "1", "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["split"] == "train" and rep["items"] == 1 and len(rep["rows"]) == 2
    assert {"aligned", "sep", "objective"} <= set(rep["rows"][0])
