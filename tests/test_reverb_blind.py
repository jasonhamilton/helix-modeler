"""Blind reverb estimator (T231): synthetic reverb with a known RT60 must be recovered; invariances."""
import math

import numpy as np
import pytest

from tonematch.metrics import reverb_blind as RB


@pytest.mark.parametrize("rt60", [0.5, 1.0, 2.0])
def test_adc_recovers_known_rt60_within_15pct(rt60):
    x = RB.synth_reverb_burst(rt60, drr_db=15.0, seed=3)
    e = RB.estimate(x)
    assert e.adc_events >= RB.ADC_MIN_EVENTS
    assert math.isfinite(e.adc_rt60_s)
    assert abs(e.adc_rt60_s / rt60 - 1) <= 0.15, e


def test_sdd_and_ml_rank_three_decays():
    rts = [0.5, 1.0, 2.0]
    ests = [RB.estimate(RB.synth_reverb_burst(rt, drr_db=15.0, seed=4)) for rt in rts]
    for k in ("sdd_peak_s", "ml_peak_s"):
        v = [getattr(e, k) for e in ests]
        assert all(math.isfinite(a) for a in v)
        assert v[0] < v[1] < v[2], (k, v)


def test_early_level_tracks_drr():
    """More reverb (a lower DRR) gives a higher early tail level, monotonically."""
    early = [RB.estimate(RB.synth_reverb_burst(1.0, drr_db=d, seed=5)).adc_early_db for d in (10.0, 20.0, 30.0)]
    assert early[0] > early[1] > early[2], early


def test_level_invariant_and_note_invariant():
    """Scaling the signal by 20 dB and changing the note content (a different seed = different burst levels and
    noise) changes the RT60 reading by less than 15%."""
    a = RB.estimate(RB.synth_reverb_burst(1.0, 15.0, seed=6))
    b = RB.estimate(0.1 * RB.synth_reverb_burst(1.0, 15.0, seed=6))
    c = RB.estimate(RB.synth_reverb_burst(1.0, 15.0, seed=7))
    assert abs(a.adc_rt60_s - b.adc_rt60_s) < 1e-6
    assert abs(a.adc_rt60_s / c.adc_rt60_s - 1) < 0.15


def test_no_reverb_gives_no_tail():
    """Dry bursts (cut to silence) give no measurable decay after 100 ms and a very low early level."""
    x = RB.synth_reverb_burst(0.3, drr_db=80.0, seed=8)        # tail 80 dB down: effectively none
    e = RB.estimate(x)
    assert not math.isfinite(e.adc_rt60_s) or e.adc_rt60_s < 0.4
    assert e.adc_early_db < -40 or not math.isfinite(e.adc_early_db)


def test_ml_decay_solves_exact_exponential():
    fps = 200.0
    N = 50
    rt = 1.2
    slope_db_s = -60.0 / rt
    W = (np.arange(N) / fps * slope_db_s)[None, :] + np.zeros((3, 1))
    rt_hat = RB.ml_decay(W, fps)
    assert np.allclose(rt_hat, rt, rtol=0.02)


def test_bootstrap_returns_intervals():
    x = RB.synth_reverb_burst(1.0, 15.0, seed=9)
    ci = RB.bootstrap(x, n=20, seed=1)
    lo, hi = ci["adc_rt60_s"]
    assert lo <= hi and 0.5 < lo and hi < 2.0


def test_chunked_analysis_matches_single():
    """A 70 s signal is analysed in 60 s stretches; the pooled readings stay close to the one-piece analysis."""
    x = RB.synth_reverb_burst(1.0, 15.0, seed=10, n_bursts=56)
    assert len(x) / RB.SR > 60
    a = RB.estimate(x)
    b = RB._fill(RB.ReverbEstimate(), RB._analyse(x, chunk_s=1e9))
    assert abs(a.adc_rt60_s / b.adc_rt60_s - 1) < 0.1
