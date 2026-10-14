"""Separation tests: short synthetic audio only; no torch, no model weights, no network."""
import subprocess
import sys

import numpy as np
import pytest

from tonematch.separation import (
    SectionMap,
    SeparatorUnavailable,
    confidence_mask,
    get_separator,
    recombine_and_reseparate,
    segment,
    separate,
)
from tonematch.separation import core as core_mod
from tonematch.separation.audio import align_to, integrated_loudness, normalize, write_wav
from tonematch.separation.models import torch_device
from tonematch.separation.synth import make_mix


@pytest.fixture(scope="module")
def synth():
    return make_mix(seed=1)


@pytest.fixture
def files(tmp_path, synth):
    mix, parts = synth
    write_wav(tmp_path / "mix.wav", mix)
    write_wav(tmp_path / "guitar.wav", parts["guitar"])
    return tmp_path


def test_import_does_not_pull_torch():
    code = "import sys, tonematch.separation; sys.exit('torch' in sys.modules)"
    assert subprocess.call([sys.executable, "-c", code]) == 0


def test_loudness_normalize_hits_target():
    t = np.arange(48_000 * 3) / 48_000
    x = (0.05 * np.sin(2 * np.pi * 997 * t))[None].repeat(2, 0).astype(np.float32)
    y, gain = normalize(x)
    assert abs(integrated_loudness(y) - (-23.0)) < 0.1
    assert abs(gain) > 1


def test_align_recovers_shift():
    rng = np.random.default_rng(0)
    ref = rng.standard_normal((2, 48_000)).astype(np.float32)
    late = np.pad(ref, ((0, 0), (700, 0)))[:, :48_000]
    out, shift = align_to(ref, late)
    assert shift == -700
    assert np.allclose(out[:, :40_000], ref[:, :40_000], atol=1e-6)


def test_user_stem_skips_separation_and_caches(files):
    root = files / "cache"
    s = separate(files / "mix.wav", files / "guitar.wav", cache_root=root)
    assert s.user_supplied and s.others_approximate and s.model == "user" and s.backend == "-"
    assert np.allclose(s.guitar + s.others, s.mix, atol=1e-5)
    for name in ("mix", "guitar", "others"):
        assert (s.cache_dir / f"{name}.wav").exists()
    assert (s.cache_dir / "manifest.json").exists()
    again = separate(files / "mix.wav", files / "guitar.wav", cache_root=root)
    assert again.cached and again.song_hash == s.song_hash


def test_mix_is_required(files):
    with pytest.raises((FileNotFoundError, RuntimeError, OSError)):
        separate(files / "nope.wav", files / "guitar.wav", cache_root=files / "c")


def test_confidence_high_for_exposed_user_stem(files):
    s = separate(files / "mix.wav", files / "guitar.wav", cache_root=files / "cache")
    m = confidence_mask(s)
    assert m.values.min() >= 0 and m.values.max() <= 1
    assert m.values.shape[0] == m.n_fft // 2 + 1
    assert 0.0 <= m.energy_weighted_mean <= 1.0 and m.exposed_fraction > 0.3


def test_segment_two_tones_at_most_three_snapshots(files):
    s = separate(files / "mix.wav", files / "guitar.wav", cache_root=files / "cache")
    sm = segment(s)
    assert abs(sm.tempo_bpm - 120) < 3
    assert 2 <= sm.n_snapshots <= 3
    assert len(sm.bar_snapshot) == len(sm.bar_starts)
    assert sm.bar_snapshot[0] != sm.bar_snapshot[-1]
    assert sm.exposed_windows and sm.exposed_windows[0][0] < 3.0  # bars 1-2 are guitar alone


def test_override_and_roundtrip(files, tmp_path):
    s = separate(files / "mix.wav", files / "guitar.wav", cache_root=files / "cache")
    sm = segment(s).override("1-4:2")
    assert sm.overridden and sm.bar_snapshot[:4] == [2, 2, 2, 2]
    with pytest.raises(ValueError):
        sm.override("1-999:0")
    with pytest.raises(ValueError):
        sm.override("1-2:7")
    sm.save(tmp_path / "s.json")
    assert SectionMap.load(tmp_path / "s.json") == sm
    assert "snapshot 2" in sm.table()


def test_roformer_flag_without_extra_is_a_clear_error():
    pytest.importorskip("numpy")
    try:
        import audio_separator  # noqa: F401
        pytest.skip("audio-separator installed")
    except ImportError:
        pass
    with pytest.raises(SeparatorUnavailable, match="separation-roformer"):
        get_separator("bs-roformer").separate(np.zeros((2, 4800), np.float32))
    with pytest.raises(ValueError):
        get_separator("nonsense")


def test_torch_device_mapping():
    assert torch_device("rocm") == ("cuda", "rocm")
    assert torch_device("mps") == ("mps", "mps")
    assert torch_device("directml") == ("cpu", "cpu")  # no demucs path: CPU always works


def test_recombine_and_reseparate_uses_same_model(files, monkeypatch):
    s = separate(files / "mix.wav", files / "guitar.wav", cache_root=files / "cache")
    seen = {}

    class Fake:
        name, guitar_stem = "fake", "guitar"

        def separate(self, mix):
            seen["mix"] = mix
            return {"guitar": mix * 0.5}, "cpu"

    monkeypatch.setattr(core_mod, "get_separator", lambda model, backend=None: Fake())
    cand = s.guitar * 2
    out = recombine_and_reseparate(cand, s)
    assert np.allclose(seen["mix"], cand + s.others) and np.allclose(out, seen["mix"] * 0.5)


def test_hf_telemetry_disabled_and_cache_redirected():
    code = ("import os, tonematch.separation.models as m; "
            "assert os.environ['HF_HUB_DISABLE_TELEMETRY'] == '1'; "
            "assert os.environ['HF_HUB_CACHE'].endswith('models/hf')")
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_CACHE")}
    assert subprocess.call([sys.executable, "-c", code], env=env) == 0


def test_roformer_gating(monkeypatch):
    import numpy as np
    import pytest

    from tonematch.separation.models import AudioSeparatorRoformer, SeparatorUnavailable

    monkeypatch.delenv("TONEMATCH_ROFORMER_CKPT", raising=False)
    monkeypatch.delenv("TONEMATCH_ROFORMER_LICENCE_ACK", raising=False)
    mix = np.zeros((2, 4800), dtype=np.float32)
    assert AudioSeparatorRoformer.default_ckpt["bs-roformer"] == "BS-Roformer-SW.ckpt"
    for name in ("mel-roformer", "mdx23c", "bs-roformer"):
        with pytest.raises(SeparatorUnavailable):
            AudioSeparatorRoformer(name, backend="cpu").separate(mix)
