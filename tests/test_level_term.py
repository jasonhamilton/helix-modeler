"""Tests for the proposed known-DI term `level_db` (T144) and the gain-trust helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch.metrics import level
from tonematch.metrics import mrstft_v2 as MV2
from tonematch.metrics import trust as TR

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SR = 48_000


def _notes(seed: int, n: int = SR * 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for k in range(8):
        f = 110 * 2 ** (rng.integers(0, 24) / 12)
        seg = slice(k * n // 8, (k + 1) * n // 8)
        x[seg] = np.tanh(3 * np.sin(2 * np.pi * f * t[seg])) * np.exp(-3 * (t[seg] - t[seg][0]))
    return x


def test_zero_on_identical_and_on_equal_level_other_notes():
    a, b = _notes(1), _notes(2)
    b = b * np.sqrt(np.mean(a ** 2) / np.mean(b ** 2))
    assert level.term(a, a) == 0.0
    # content does not matter once the level is equal
    assert level.term(a, b) == pytest.approx(0.0, abs=1e-9)


def test_pure_gain_is_its_db_and_symmetric():
    a = _notes(3)
    for db in (-9.2, -0.3, 3.5, 12.6):
        y = a * 10 ** (db / 20)
        assert level.term(a, y) == pytest.approx(abs(db), abs=1e-9)
        assert level.term(y, a) == pytest.approx(abs(db), abs=1e-9)


def test_polarity_and_shift_free():
    a = _notes(4)
    assert level.term(a, -a) == pytest.approx(0.0, abs=1e-12)
    assert level.term(np.pad(a, (480, 0))[: a.size], np.pad(a, (0, 0))) < 0.05


def test_silence_is_finite():
    a = _notes(5)
    v = level.term(a, np.zeros_like(a))
    assert np.isfinite(v) and v <= level.MAX_DB


def test_mask_weights_the_level():
    a = _notes(6)
    y = a.copy()
    y[: a.size // 2] *= 10.0              # louder only where the mask is 0
    m = np.zeros(a.size)
    m[a.size // 2:] = 1.0
    assert level.term(a, y, m) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError):
        level.term(a, y, m[:-1])


def test_tiebreak_ranks_truth_among_level_only_neighbours():
    """The v2 failure mode: mrstft_v2 ties a level-only neighbour; the tie-break orders it by level."""
    t = np.tanh(4 * _notes(7))
    rng = np.random.default_rng(9)
    cap = 10 ** (-2.0 / 20) * t + 1e-3 * np.sqrt(np.mean(t ** 2)) * rng.standard_normal(t.size)  # 2 dB down, noisy
    nbs = [t * 10 ** (d / 20) for d in (-9.2, +3.8, +12.6)]
    m0 = MV2.term(cap, t)
    for y in nbs:
        assert MV2.term(cap, y) == pytest.approx(m0, rel=1e-6, abs=1e-9)   # mrstft_v2 alone cannot order them
    sc = [m0 + 1e-6 * level.term(cap, t)] + [MV2.term(cap, y) + 1e-6 * level.term(cap, y) for y in nbs]
    nd = [TR.null_db(t, y) for y in nbs]
    assert TR.rank1_rule_b(sc[0], sc[1:], nd).rank1_audible


def test_tiebreak_cannot_beat_a_neighbour_closer_to_the_capture_level():
    """The protocol limit: a level-only neighbour nearer the capture's gain than the truth wins."""
    t = np.tanh(4 * _notes(8))
    cap = 10 ** (2.77 / 20) * t
    y = t * 10 ** (3.51 / 20)
    assert level.term(cap, y) < level.term(cap, t)


def test_gain_and_level_knob_names():
    import gain_trust as G

    for n in ("Master", "Output", "Level", "Ch Vol", "Ch1 Master", "PostGain", "ODLevel", "Volume"):
        assert G.is_level(n) and not G.is_gain(n), n
    for n in ("Drive", "Gain", "PreGain", "DriveNorm", "Ch2 Drive", "Sustain", "Fuzz", "Boost"):
        assert G.is_gain(n), n
    for n in ("Bass", "Treble", "Sag", "Presence", "Bias"):
        assert not G.is_gain(n) and not G.is_level(n), n


def test_pair_and_small_neighbours_move_knobs():
    import gain_trust as G

    rng = np.random.default_rng(0)
    ks = {"Drive": 64, "Master": 64, "Bass": 64}
    for _ in range(20):
        p = G.pair_knobs(rng, ks)
        assert p != ks and p["Bass"] == 64
        if p["Drive"] != 64 and p["Master"] != 64:
            assert (p["Drive"] - 64) * (p["Master"] - 64) < 0   # opposite directions
        s = G.small_knobs(rng, ks)
        assert sum(s[n] != ks[n] for n in ks) == 1


def test_unrankable_flags_the_protocol_limit():
    import gain_trust as G

    it = {"cap_gain_db": 2.77, "null_db": [-6.0, -3.0], "gm_null_db": [-120.0, -10.0],
          "level_vs_truth_db": [3.51, 1.0]}
    assert G.unrankable(it)
    it["level_vs_truth_db"] = [-4.0, 1.0]
    assert not G.unrankable(it)


def test_loss_kit_accepts_level_db_known_di_only():
    """contracts/loss-kit.md amendment 2026-10-18 (T146): `level_db` is a kit term, known-DI only."""
    from tonematch.metrics import DEFAULT_WEIGHTS, KNOWN_DI_WEIGHTS, NO_DI_TERMS, TERMS, LossKit
    from tonematch.metrics import level as LV

    rng = np.random.default_rng(3)
    t = rng.standard_normal(48_000).astype(np.float32)
    c = (t * 10 ** (2.0 / 20)).astype(np.float32)
    s = LossKit().score(t, c, weights={"mrstft_v2": 1.0, "level_db": 1e-7})
    assert s.terms["level_db"] == pytest.approx(LV.term(t, c)) == pytest.approx(2.0, abs=1e-4)
    assert s.total == pytest.approx(s.terms["mrstft_v2"] + 1e-7 * s.terms["level_db"])
    assert "level_db" in TERMS and "level_db" not in NO_DI_TERMS
    assert "level_db" not in DEFAULT_WEIGHTS and KNOWN_DI_WEIGHTS == {"mrstft_v2": 1.0}   # not admitted
    # same level, other notes: 0 (the note content does not matter once the level is equal)
    o = rng.standard_normal(48_000).astype(np.float32)
    o *= float(np.sqrt(np.mean(t.astype(np.float64) ** 2) / np.mean(o.astype(np.float64) ** 2)))
    assert LossKit().score(t, o, weights={"level_db": 1.0}).total == pytest.approx(0.0, abs=1e-4)
