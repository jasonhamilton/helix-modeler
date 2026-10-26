"""T228 colour pilot: the direct measurements and the bounded fits (tonematch.search.colour). Synthetic signals only; no Native."""
from __future__ import annotations

import numpy as np

from tonematch.search import colour as C

SR = 48_000


def bursts(rng, seconds=10.0, starts=None, dur=0.25):
    """Noise bursts (a stand-in for notes) at the given start times; silent between. Returns (signal, dry-like gate signal)."""
    n = int(seconds * SR)
    x = np.zeros(n, np.float32)
    starts = starts if starts is not None else [0.2, 1.4, 2.9, 4.3, 5.5, 7.1, 8.4]
    for s in starts:
        a = int(s * SR)
        b = a + int(dur * SR)
        env = np.exp(-np.arange(b - a) / (0.12 * SR))
        x[a:b] += (rng.standard_normal(b - a) * env).astype(np.float32) * 0.3
    return x


def add_echo(x, t, g, n=2):
    y = x.copy()
    for k in range(1, n + 1):
        s = int(t * SR * k)
        y[s:] += (g ** k) * x[:-s]
    return y


def test_onset_deconv_recovers_a_known_echo():
    rng = np.random.default_rng(1)
    ref = bursts(rng)
    stem = add_echo(ref, 0.4, 0.6)
    t, h = C.echo_deconv(C.onset_envelope(stem), C.onset_envelope(ref))
    main, lag, rel, z = C.late_peak(t, h)
    assert C.explained_variance(C.onset_envelope(stem), C.onset_envelope(ref), t, h) > 0.5
    assert abs(lag - 0.4) < 0.02
    assert rel > 0.1 and z > C.Z_MIN


def test_delay_evidence_needs_alignment_peak_and_a_real_gap():
    rng = np.random.default_rng(2)
    ref = bursts(rng)
    dry = (np.abs(ref) > 0).astype(np.float32) * 0.1 * rng.standard_normal(len(ref)).astype(np.float32)   # silent exactly where ref is
    dry = np.where(np.abs(ref) > 1e-6, dry, 0.0).astype(np.float32)
    ev = C.delay_evidence(add_echo(ref, 0.4, 0.6), ref, dry)
    assert ev.aligned and abs(ev.lag_s - 0.4) < 0.02
    # the echo fills the silence after each burst, so the gap check confirms it
    assert ev.gap_checked > 0 and ev.present, ev.reason
    # no echo: no late tap stands out
    ev0 = C.delay_evidence(ref + 0.001 * rng.standard_normal(len(ref)).astype(np.float32), ref, dry)
    assert not ev0.present
    # an unrelated performance is not measurable at all
    other = bursts(np.random.default_rng(9), starts=[0.7, 2.1, 3.3, 4.9, 6.2, 7.7, 9.0])
    assert not C.delay_evidence(other, ref, dry).aligned


def test_a_candidate_echo_without_any_confirming_gap_is_not_accepted():
    rng = np.random.default_rng(3)
    ref = bursts(rng)
    dry = np.ones_like(ref) * 0.1                       # never silent: no gap to confirm in
    ev = C.delay_evidence(add_echo(ref, 0.4, 0.6), ref, dry)
    assert not ev.present and "cannot be confirmed" in ev.reason


def test_gap_tail_tells_a_decaying_tail_from_a_flat_floor_and_from_no_gap():
    rng = np.random.default_rng(4)
    n = 6 * SR
    dry = np.zeros(n, np.float32)
    ref = np.zeros(n, np.float32)
    for s in (0.3, 2.3, 4.3):                           # 0.5 s notes, 1.5 s gaps
        a, b = int(s * SR), int((s + 0.5) * SR)
        dry[a:b] = rng.standard_normal(b - a) * 0.1
        ref[a:b] = rng.standard_normal(b - a) * 0.1
    t = np.arange(n) / SR
    # tail: -3 dB per 100 ms after each note offset on top of the notes (RT60 about 2 s), level high enough to count
    tail = np.zeros(n, np.float32)
    for s in (0.3, 2.3, 4.3):
        off = s + 0.5
        m = t >= off
        tail[m] += (rng.standard_normal(m.sum()) * 0.03 * 10 ** (-(t[m] - off) * 30 / 20)).astype(np.float32)
    floor = (rng.standard_normal(n) * 0.0005).astype(np.float32)
    assert C.gap_tail(ref + tail, ref, dry).present
    flat = C.gap_tail(ref + floor, ref, dry)
    assert flat.measurable and not flat.present
    solid = C.gap_tail(ref + 0.0 * ref, np.ones(n, np.float32) * 0.01 * 0 + ref, np.ones(n, np.float32) * 0.1)
    assert not solid.measurable and "not measurable" in solid.reason


def test_a_transcription_gap_where_the_stem_keeps_playing_is_not_a_gap():
    rng = np.random.default_rng(5)
    n = 4 * SR
    dry = np.zeros(n, np.float32)
    dry[: SR // 2] = 0.1
    dry[-SR:] = 0.1
    ref = dry.copy()
    stem = (rng.standard_normal(n) * 0.1).astype(np.float32)          # the real guitar never stops
    ev = C.gap_tail(stem, ref, dry)
    assert not ev.measurable and ev.skipped_levels_db


def test_eq_gains_limits_ignore_low_bands_and_remove_the_core_offset():
    c = C.GRAPHIC10_CENTRES
    ratio = np.array([-20, -15, 2, 4, 6, 8, 20, 4, -30, -40], float)     # lows, a +6 shape in the core, big highs mismatch
    g = C.eq_gains_from_ltas(ratio)
    assert np.all(np.abs(g) <= C.EQ_LIMIT_DB + 1e-9)
    assert g[0] == 0 and g[1] == 0                                       # 31 and 62 Hz ignored
    assert g[8] == -C.EQ_LIMIT_DB and g[9] == -C.EQ_LIMIT_DB             # the huge treble mismatch is limited, and does not drag the core
    core = (np.asarray(c) >= 250) & (np.asarray(c) <= 4000)
    assert abs(np.median(g[core])) < 4.5
    flat = C.eq_gains_from_ltas(np.full(10, 7.0))
    assert np.allclose(flat, 0.0)                                         # a pure level difference is not an EQ matter


def test_eq_smoothing_damps_a_single_band_spike():
    r = np.zeros(10)
    r[5] = 10.0
    g = C.eq_gains_from_ltas(r)
    assert 0 < g[5] <= 5.1 and g[4] > 0 and g[6] > 0


def test_eq_residual_falls_after_applying_the_gains():
    r = np.array([0, 0, 3, -2, 4, 1, -3, 2, -5, -6], float)
    before = C.eq_residual_db(r)
    after = C.eq_residual_db(r - C.eq_gains_from_ltas(r))
    assert after < before


def test_ltas_ratio_sees_a_spectral_tilt():
    rng = np.random.default_rng(6)
    x = rng.standard_normal(4 * SR).astype(np.float32) * 0.1
    from scipy.signal import butter, sosfilt
    y = sosfilt(butter(2, 2000, "low", fs=SR, output="sos"), x).astype(np.float32)
    r = C.ltas_ratio_db(x, y)                                              # x has more highs than y
    assert r[8] > r[3] + 5.0


def test_light_caps_snap_and_linear_inversion():
    assert C.light_mix(0.9, "delay") == (C.LIGHT_MIX_CAP["delay"], True)
    assert C.light_mix(0.2, "reverb") == (0.2, False)
    assert C.invert_linear(0.2, 0.4, 0.25) == 0.125
    assert C.invert_linear(5.0, 0.4, 0.25) == 1.0                          # clipped to the knob range
    assert C.invert_linear(0.2, 0.0, 0.25) == 0.25                         # a dead probe keeps the probe knob
    t = C.snap_time(0.383, 0.0, 1.27)
    assert abs(t - 0.38) < 1e-9
    assert C.snap_time(5.0, 0.0, 1.27) == 1.27


def test_graphic10_values_names_follow_the_catalog_order():
    v = C.graphic10_values(np.arange(10, dtype=float))
    assert list(v) == list(C.GRAPHIC10_KNOBS) and v["31p25Hz"] == 0 and v["16kHz"] == 9


def test_amp_pool_by_usage_skips_clean_and_missing_and_non_amps():
    catalog = {"models": {
        "HD2_AmpA": {"category": "amp", "name": "Brit A"}, "HD2_AmpCleanB": {"category": "amp", "name": "Placater Clean"},
        "HD2_AmpC": {"category": "amp", "name": "US Deluxe Nrm"}, "HD2_DistD": {"category": "drive", "name": "D"},
        "HD2_AmpE": {"category": "amp", "name": "Revv E"}, "HD2_AmpF": {"category": "amp", "name": "Line 6 F"}}}
    ranked = [["HD2_AmpA", 9, 9], ["HD2_AmpCleanB", 8, 8], ["HD2_AmpC", 7, 7], ["HD2_DistD", 6, 6], ["HD2_Gone", 5, 5],
              ["HD2_AmpE", 4, 4], ["HD2_AmpF", 3, 3]]
    assert C.amp_pool_by_usage(ranked, catalog, n=2) == ["HD2_AmpA", "HD2_AmpE"]
    assert C.amp_pool_by_usage(ranked, catalog, n=10) == ["HD2_AmpA", "HD2_AmpE", "HD2_AmpF"]


def test_amp_screen_points_move_only_the_gain_knob():
    free = [("Drive", 0.0, 1.0, 0.4), ("Bass", 0.0, 1.0, 0.5), ("ChVol", 0.0, 1.0, 0.5)]
    assert C.amp_screen_points(free) == [{"Drive": 0.5}, {"Drive": 0.85}]
    assert C.amp_gain_knob(["Bass", "LeadGain", "LeadDrive"]) == "LeadGain"
    assert C.amp_gain_knob(["ChVol", "Bass"]) is None
    assert C.amp_screen_points([("Bass", 0.0, 1.0, 0.5)]) == [{}]


def test_locked_level_hides_and_drops_the_level_knobs():
    class B:
        free = [("Drive", 0, 1, 0.5), ("Master", 0, 1, 0.5), ("ChVol", 0, 1, 0.5), ("Treble", 0, 1, 0.5)]

        def with_values(self, v):
            return dict(v)

    lb = C.LockedLevel(B())
    assert [f[0] for f in lb.free] == ["Drive", "Treble"] and lb.locked == ["Master", "ChVol"]
    assert lb.with_values({"Drive": 0.1, "Master": 0.9}) == {"Drive": 0.1}
