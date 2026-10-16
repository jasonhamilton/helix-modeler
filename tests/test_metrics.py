"""Loss kit v0 (contracts/loss-kit.md): content invariance per term, level invariance, kit API.

Signals are the offline proxy in tonematch.metrics.proxy (synthetic plucked DI through numpy DSP);
nothing here touches an engine.
"""
from __future__ import annotations

import itertools
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
    assert gated.as_dict()["floor_db"] < open_.as_dict()["floor_db"] - 6.0  # lower floor


# ------------------------------------------------------------------ envelope v1 (T025)
# The attack, onset and rhythm tests fail on envelope v0 (5 ms-hop attack, level-rise onsets); the
# noise-floor test passes on v0 too and guards the new floor estimate. See docs/DECISIONS.md.


def _ramp_notes(attack_ms: float, n_notes: int = 10, ioi: float = 0.3, f: float = 3000.0) -> np.ndarray:
    """Repeated 3 kHz notes with a linear amplitude attack of `attack_ms`, then a 150 ms decay."""
    sr = 48_000
    t = np.arange(int(ioi * sr)) / sr
    amp = np.minimum(t / (attack_ms / 1000), 1.0) * np.exp(-np.maximum(t - attack_ms / 1000, 0) / 0.15)
    x = np.zeros(int((n_notes * ioi + 0.3) * sr))
    for k in range(n_notes):
        s = int((0.1 + k * ioi) * sr)
        x[s:s + t.size] += 0.5 * amp * np.sin(2 * np.pi * f * t)
    return x


def test_envelope_attack_resolves_below_the_hop():
    """v0 quantised attack to the 5 ms hop (0.5 and 2 ms ramps both read 10 ms)."""
    att = [envelope.features(_ramp_notes(a)).as_dict()["attack_ms"] for a in (0.5, 2.0, 4.0, 8.0)]
    assert all(b - a > 0.4 for a, b in itertools.pairwise(att)), att
    assert att[0] < 2.0 and att[-1] > 3.0, att


def _plucks_at(times: np.ndarray, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    buf = np.zeros(int((times[-1] + 1.0) * 48_000))
    for t0 in times:
        midi = int(rng.integers(40, 70))
        proxy._pluck(buf, int(t0 * 48_000), 0.9, 440.0 * 2 ** ((midi - 69) / 12), 0.8, rng)
    return 0.5 * buf / np.abs(buf).max()


@pytest.mark.parametrize("drive", [0.0, 0.5])
def test_envelope_finds_the_notes_of_a_driven_tone(drive):
    """v0's level-rise onsets found 1 of 24 overlapping notes once driven (25% when clean)."""
    times = np.cumsum(np.random.default_rng(1).choice([0.25, 0.35, 0.5], 24))
    on = envelope.features(proxy.render(ProxyPreset(drive=drive), _plucks_at(times))).onsets_s
    # the flux frame sits up to ~25 ms after the pluck
    hit = np.mean([np.min(np.abs(on - t - 0.01)) < 0.04 for t in times])
    false = np.mean([np.min(np.abs(times + 0.01 - o)) >= 0.04 for o in on])
    assert hit >= 0.85 and false <= 0.35, (hit, false, on.size)


@pytest.mark.parametrize("a,b", [
    (ProxyPreset(drive=0.3), ProxyPreset(drive=0.3, comp=0.8, comp_thr=0.8)),
    (ProxyPreset(drive=0.5), ProxyPreset(drive=0.9)),
])
def test_envelope_insensitive_to_rhythm_and_notes(a, b):
    """Other notes AND another rhythm: same tone must stay closer than the changed tone.
    v0 failed both cases (per-note medians over two or three detected notes)."""
    dis = [proxy.guitar_di(s, 12.0) for s in (21, 22, 23)]
    fa = [envelope.features(proxy.render(a, d)) for d in dis]
    fb = [envelope.features(proxy.render(b, d)) for d in dis]
    same = [envelope.distance(fa[i], fa[j]) for i in range(3) for j in range(3) if i != j]
    diff = [envelope.distance(fa[i], fb[j]) for i in range(3) for j in range(3) if i != j]
    assert max(same) < 0.5 * min(diff), (same, diff)


def test_envelope_noise_floor_tracks_hiss():
    rng = np.random.default_rng(4)
    n = int(SECONDS * 48_000)
    phrases = (np.arange(n) // 24_000) % 2 == 0
    di = proxy.guitar_di(6, SECONDS) * phrases
    floors = []
    for hiss_db in (-70.0, -50.0):
        y = proxy.render(ProxyPreset(drive=0.2), di)
        y = y + np.sqrt(np.mean(y ** 2)) * 10 ** (hiss_db / 20) * rng.standard_normal(n)
        floors.append(envelope.features(y).as_dict()["floor_db"])
    assert floors[1] - floors[0] == pytest.approx(20.0, abs=4.0), floors


# ------------------------------------------------------------------ kit API


@pytest.fixture(scope="module")
def pair(dis):
    return proxy.render(ProxyPreset(), dis[0]), proxy.render(ProxyPreset(drive=0.8, cab=3), dis[1])


def test_default_weights_are_zero_until_admitted(pair):
    # embed_afxrep is listed at weight 0 but skipped (heavy, optional) until its weight is non-zero
    assert set(DEFAULT_WEIGHTS) == set(NO_DI_TERMS) | {"embed_afxrep"}
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
        kit.score(*pair, weights={"embed_fxenc": 1.0})
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


def _fake_helix_set(root, n_cand: int = 5, seconds: float = 2.0):
    """A tiny helix-trust-v1 layout: manifest plus candidate renders on DI A and B (proxy DSP)."""
    from tonematch.metrics import proxy
    rng = np.random.default_rng(7)
    p = proxy.random_preset(rng)
    di = {"A": proxy.guitar_di(1, seconds), "B": proxy.guitar_di(2, seconds)}
    labels = ["true", "near:x", "nbr:m", "random:EQ", "random:Cab"][:n_cand]
    presets = [p, proxy.perturb_group(p, "cab", rng)] + [proxy.random_preset(rng) for _ in labels[2:]]
    d = root / "renders" / "item00"
    d.mkdir(parents=True)
    for k, q in enumerate(presets):
        for tag in ("A", "B"):
            np.save(d / f"c{k:02d}_{tag}.npy", proxy.render(q, di[tag]).astype(np.float32))
    mf = {"dataset": "helix-trust-v1", "seed": 1, "seconds": seconds,
          "items": [{"item": 0, "category": "Cab", "true_model": "m",
                     "candidates": [{"label": lab} for lab in labels]}]}
    (root / "manifest.json").write_text(json.dumps(mf))


def test_trust_check_helix_mode_on_a_fake_set(tmp_path):
    _fake_helix_set(tmp_path)
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--helix", "--all",
                        "--helix-dir", str(tmp_path), "--jobs", "1", "--out", str(out)],
                       check=False, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["proxy"] is False and rep["engine"] == "native" and rep["distractors"] == 4
    assert rep["subsets"] == ["clean/other_di", "sep/other_di", "sep/same_notes"]   # no DI C here
    for s in rep["subsets"]:
        sm = rep["summary"][s]
        assert set(sm) >= set(NO_DI_TERMS) | {"mrstft", "provisional_sum"}
        for t in sm.values():
            assert {"rank1_vs_near_only", "rank1_vs_nbr_only", "rank1_vs_random_only", "margin_median",
                    "rank1_audible_only", "rank1_by_category"} <= set(t)
    it = rep["per_item"][0]
    assert it["null_db_same_notes"][0] < -100     # the true preset nulls against itself


def test_trust_check_helix_mode_without_set_exits_2(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--helix", "--all",
                        "--helix-dir", str(tmp_path)], check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "helix_trust_set.py" in r.stderr


def test_trust_check_without_synthetic_set_exits_2():
    if (ROOT / "fixtures" / "synthetic" / "manifest.json").exists():
        pytest.skip("synthetic set present")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "loss_trust_check.py"), "--all"],
                       check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "--proxy" in r.stderr
