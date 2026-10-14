import numpy as np
import pytest
import soundfile as sf

from tonematch import dry

SR = dry.SR


def _tone(f, dur, decay=None, rng=None):
    t = np.arange(int(dur * SR)) / SR
    x = np.sin(2 * np.pi * f * t) * 0.3
    if decay:
        x *= np.exp(-t * decay)
    return x.astype(np.float32)


def _make_lib(tmp_path):
    sf.write(tmp_path / "low.wav", _tone(110, 4.0), SR, subtype="FLOAT")
    sf.write(tmp_path / "high.wav", _tone(880, 4.0), SR, subtype="FLOAT")
    # repeated plucks, fast decay: muted style
    x = np.zeros(4 * SR, np.float32)
    for k in range(8):
        s = int(k * 0.5 * SR)
        x[s:s + SR // 2] = _tone(165, 0.5, decay=40)
    sf.write(tmp_path / "muted.wav", x, SR, subtype="FLOAT")
    return tmp_path


def test_index_and_manifest(tmp_path):
    lib = dry.index_library(_make_lib(tmp_path), source="synthetic")
    assert len(lib) == 3 and (tmp_path / "manifest.json").exists()
    again = dry.index_library(tmp_path)
    assert [c.path for c in again.clips] == [c.path for c in lib.clips]
    by = {c.path: c for c in lib.clips}
    assert abs(by["low.wav"].pitch_med - 45) < 1.0      # A2
    assert abs(by["high.wav"].pitch_med - 81) < 1.0     # A5
    assert by["muted.wav"].style == "muted"
    assert by["muted.wav"].density > 1.0


def test_pick_by_register_and_load(tmp_path):
    lib = dry.index_library(_make_lib(tmp_path))
    assert dry.pick_di((78, 84), None, library=lib, n=1)[0].path == "high.wav"
    assert dry.pick_di(45, None, library=lib, n=1)[0].path == "low.wav"
    assert dry.pick_di(60, "muted", library=lib, n=1)[0].path == "muted.wav"
    got = dry.pick_di(60, None, library=lib, n=3)
    assert len({c.path for c in got}) == 3
    x = got[0].load()
    assert x.dtype == np.float32 and x.ndim == 1 and len(x) == 4 * SR


def test_resample_to_48k(tmp_path):
    sf.write(tmp_path / "a.wav", _tone(220, 4.0)[: 4 * SR][::1], SR, subtype="FLOAT")
    t = np.arange(4 * 44100) / 44100
    sf.write(tmp_path / "b.wav", (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 44100)
    lib = dry.index_library(tmp_path)
    for c in lib.clips:
        assert abs(len(c.load()) - 4 * SR) <= 2


def test_fallback_warns(tmp_path):
    with pytest.warns(UserWarning, match="SYNTHETIC"):
        out = dry.pick_di(60, "single", library=tmp_path)
    assert len(out) == 1 and out[0].synthetic
    x = out[0].load()
    assert x.dtype == np.float32 and x.ndim == 1


def test_bad_style_and_missing_folder(tmp_path):
    with pytest.raises(ValueError):
        dry.pick_di(60, "shred", library=tmp_path)
    with pytest.raises(FileNotFoundError):
        dry.index_library(tmp_path / "nope")
