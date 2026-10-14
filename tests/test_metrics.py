"""Loss kit v0 (contracts/loss-kit.md): content invariance per term, level invariance, kit API.

Signals are the offline proxy in tonematch.metrics.proxy (synthetic plucked DI through numpy DSP);
nothing here touches an engine.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tonematch.metrics import (
    DEFAULT_WEIGHTS,
    NO_DI_TERMS,
    TERMS,
    LossKit,
    crest,
    envelope,
    ltas,
    mrstft,
    proxy,
)
from tonematch.metrics.proxy import ProxyPreset

ROOT = Path(__file__).resolve().parents[1]
SECONDS = 6.0
NOTE_SEEDS = (11, 12, 13)


def _di(seed: int, rhythm: int = 7) -> np.ndarray:
    # same rhythm, different notes: what "insensitive to which notes were played" means
    return proxy.guitar_di(seed, SECONDS, rhythm_seed=rhythm)


# (term module, tone A, tone B): B differs from A in what the term is meant to see
CASES = {
    "ltas_tilt": (ltas, ProxyPreset(cab=2, treble=0.8, mic=0.8, drive=0.4),
                  ProxyPreset(cab=3, treble=0.2, mic=0.1, drive=0.4)),
    "crest_flatness_hnr": (crest, ProxyPreset(drive=0.05), ProxyPreset(drive=0.9)),
    "envelope": (envelope, ProxyPreset(drive=0.1), ProxyPreset(drive=0.1, comp=1.0, comp_thr=1.0)),
}


@pytest.fixture(scope="module")
def dis():
    return [_di(s) for s in NOTE_SEEDS]


@pytest.mark.parametrize("name", list(CASES))
def test_term_is_insensitive_to_notes(name, dis):
    mod, a, b = CASES[name]
    ya = [proxy.render(a, d) for d in dis]
    yb = [proxy.render(b, d) for d in dis]
    same = [mod.term(ya[i], ya[j]) for i in range(3) for j in range(3) if i != j]
    diff = [mod.term(ya[i], yb[j]) for i in range(3) for j in range(3) if i != j]
    # same tone with other notes must score well below another tone with other notes
    assert max(same) < 0.5 * min(diff), (name, same, diff)


@pytest.mark.parametrize("mod", [ltas, crest, envelope, mrstft])
def test_identity_and_level_invariance(mod, dis):
    y = proxy.render(ProxyPreset(drive=0.5, cab=1), dis[0])
    assert mod.term(y, y) == pytest.approx(0.0, abs=1e-9)
    assert mod.term(y, 0.13 * y) < 0.02
    assert mod.term(y, 4.0 * y) < 0.02


def test_mrstft_is_aligned_only(dis):
    """mrstft is the one term that is NOT content-invariant: other notes cost more than another tone."""
    a, b = ProxyPreset(drive=0.4, cab=0), ProxyPreset(drive=0.6, cab=1)
    same_tone_other_notes = mrstft.term(proxy.render(a, dis[0]), proxy.render(a, dis[1]))
    other_tone_same_notes = mrstft.term(proxy.render(a, dis[0]), proxy.render(b, dis[0]))
    assert other_tone_same_notes < same_tone_other_notes


def test_ltas_tilt_sign():
    rng = np.random.default_rng(0)
    white = rng.standard_normal(48_000 * 2)
    from scipy import signal
    dark = signal.lfilter(*signal.butter(1, 500, fs=48_000), white)
    assert ltas.features(dark).tilt_db_oct < ltas.features(white).tilt_db_oct - 2.0


def test_crest_tracks_clipping():
    t = np.arange(48_000 * 2) / 48_000
    sine = 0.5 * np.sin(2 * np.pi * 220 * t)
    clipped = np.clip(4 * sine, -0.5, 0.5)
    c_s, c_c = crest.features(sine).vec[0], crest.features(clipped).vec[0]
    assert c_s == pytest.approx(3.01, abs=0.3)  # sine crest factor is 3 dB (partial periods per frame)
    assert c_c < c_s - 1.5


def test_envelope_sees_compression(dis):
    dry = envelope.features(proxy.render(ProxyPreset(drive=0.1), dis[0]))
    comp = envelope.features(proxy.render(ProxyPreset(drive=0.1, comp=1.0, comp_thr=1.0), dis[0]))
    assert dry.n_notes > 3
    assert comp.vec[0] < dry.vec[0]  # less dynamic range


def test_envelope_sees_gate():
    rng = np.random.default_rng(3)
    n = int(SECONDS * 48_000)
    bursts = (np.arange(n) // 24_000) % 2 == 0  # half-second gaps between phrases
    di = proxy.guitar_di(5, SECONDS) * bursts + 3e-4 * rng.standard_normal(n)
    open_ = envelope.features(proxy.render(ProxyPreset(drive=0.6), di))
    gated = envelope.features(proxy.render(ProxyPreset(drive=0.6, gate_thr=0.6), di))
    assert gated.vec[1] < open_.vec[1] - 6.0  # lower floor


# ------------------------------------------------------------------ kit API


@pytest.fixture(scope="module")
def pair(dis):
    return proxy.render(ProxyPreset(), dis[0]), proxy.render(ProxyPreset(drive=0.8, cab=3), dis[1])


def test_default_weights_are_zero_until_admitted(pair):
    assert set(DEFAULT_WEIGHTS) == set(NO_DI_TERMS)
    assert all(w == 0.0 for w in DEFAULT_WEIGHTS.values())
    s = LossKit().score(*pair)
    assert s.total == 0.0
    assert set(s.terms) == set(NO_DI_TERMS) and all(v > 0 for v in s.terms.values())
    assert s.measured == {}


def test_weights_and_names(pair):
    kit = LossKit()
    s = kit.score(*pair, weights={"ltas_tilt": 2.0, "mrstft": 1.0})
    assert set(s.terms) == {"ltas_tilt", "mrstft"}
    assert s.total == pytest.approx(2 * s.terms["ltas_tilt"] + s.terms["mrstft"])
    with pytest.raises(KeyError):
        kit.score(*pair, weights={"not_a_term": 1.0})
    with pytest.raises(NotImplementedError):
        kit.score(*pair, weights={"embed_afxrep": 1.0})
    assert kit.score(*pair, weights={"embed_afxrep": 0.0, "envelope": 1.0}).terms.keys() == {"envelope"}
    with pytest.raises(ValueError):
        kit.score(*pair, weights={"sepcons": 1.0})
    assert len(TERMS) == 11


def test_cache_and_mask(pair):
    t, c = pair
    kit = LossKit()
    a = kit.score(t, c, weights={n: 1.0 for n in NO_DI_TERMS})
    b = kit.score(t, c, weights={n: 1.0 for n in NO_DI_TERMS})  # cached target features
    assert a.terms == b.terms
    ones = kit.score(t, c, mask=np.ones_like(t), weights={n: 1.0 for n in NO_DI_TERMS})
    assert ones.terms == pytest.approx(a.terms)
    half = np.ones_like(t)
    half[: t.size // 2] = 0.0
    masked = kit.score(t, c, mask=half, weights={"ltas_tilt": 1.0})
    assert masked.terms["ltas_tilt"] != pytest.approx(a.terms["ltas_tilt"])
    with pytest.raises(ValueError):
        kit.score(t, c, mask=np.ones(10))


def test_bad_audio_rejected(pair):
    t, c = pair
    bad = c.copy()
    bad[5] = np.nan
    with pytest.raises(ValueError):
        LossKit().score(t, bad)
    with pytest.raises(ValueError):
        LossKit().score(t, np.zeros((3, 3, 3)))


def test_losses_alias():
    import tonematch.losses as L
    assert L.LossKit is LossKit


# ------------------------------------------------------------------ trust check script


def test_trust_check_proxy_smoke(tmp_path):
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--proxy", "--all",
                        "--items", "2", "--distractors", "3", "--seconds", "2", "--jobs", "1",
                        "--out", str(out)], check=False, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["proxy"] is True and "not Helix" in rep["warning"]
    assert set(rep["summary"]["heldout_notes"]) >= set(NO_DI_TERMS) | {"mrstft"}


def test_trust_check_without_synthetic_set_exits_2():
    if (ROOT / "fixtures" / "synthetic" / "manifest.json").exists():
        pytest.skip("synthetic set present")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--all"],
                       check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "--proxy" in r.stderr
