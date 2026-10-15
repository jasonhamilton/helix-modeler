"""`embed_afxrep` (AFx-Rep, T032): wrapper tests with a tiny fake encoder, plus real-checkpoint tests
that skip when the `embed` extra or cache/models/afx-rep/ is absent."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.metrics import DEFAULT_WEIGHTS, EMBED_TERMS, LossKit, afxrep, proxy
from tonematch.metrics.proxy import ProxyPreset

torch = pytest.importorskip("torch")

SR = 48_000


class FakeEncoder(torch.nn.Module):
    """(b, chs, n) -> (mid, side): log band energies of a 2048-point spectrum, projected to 16-d."""

    def __init__(self):
        super().__init__()
        g = torch.Generator().manual_seed(0)
        self.proj_mid = torch.nn.Parameter(torch.randn(32, 16, generator=g))
        self.proj_side = torch.nn.Parameter(torch.randn(32, 16, generator=g))
        self.calls = 0

    def _feat(self, x):  # x (b, n)
        fr = x.unfold(-1, 2048, 1024)
        p = torch.fft.rfft(fr * torch.hann_window(2048, device=x.device), dim=-1).abs() ** 2
        bands = p[..., 1:1025].reshape(*p.shape[:-1], 32, 32).mean(-1).mean(-2)
        return torch.log(bands + 1e-8)

    def forward(self, x):
        self.calls += 1
        if x.shape[1] == 1:
            m = self._feat(x[:, 0]) @ self.proj_mid
            return m, m
        mid, side = (x[:, 0] + x[:, 1]) / 2, (x[:, 0] - x[:, 1]) / 2
        return self._feat(mid) @ self.proj_mid, self._feat(side) @ self.proj_side


@pytest.fixture
def fake():
    enc = FakeEncoder()
    afxrep.set_embedder(afxrep.Embedder(model=enc, device="cpu"))
    yield enc
    afxrep.set_embedder(None)


@pytest.fixture(scope="module")
def tones():
    di = [proxy.guitar_di(s, 4.0, rhythm_seed=7) for s in (21, 22)]
    a = [proxy.render(ProxyPreset(drive=0.1, cab=1), d) for d in di]
    b = [proxy.render(ProxyPreset(drive=0.9, cab=3, treble=0.9), d) for d in di]
    return a, b


def test_identity_level_and_range(fake, tones):
    a, b = tones
    assert afxrep.term(a[0], a[0]) == pytest.approx(0.0, abs=1e-6)
    assert afxrep.term(a[0], 0.05 * a[0]) == pytest.approx(0.0, abs=1e-6)  # peak-normalised
    d = afxrep.term(a[0], b[0])
    assert 0.0 < d <= 2.0
    f = afxrep.features(a[0])
    assert f.mid.shape == (16,) and f.side is None
    assert np.linalg.norm(f.mid) == pytest.approx(1.0)


def test_stereo_uses_side(fake, tones):
    a, _ = tones
    st = np.stack([a[0], a[0]])
    wide = np.stack([a[0], np.roll(a[0], 480)])
    fs, fw = afxrep.features(st), afxrep.features(wide)
    assert fs.side is not None and fw.side is not None
    assert afxrep.distance(fs, fw) > 0
    assert afxrep.features(st.T).side is not None  # (n, 2) layout accepted


def test_short_clip_is_padded_and_bad_input_rejected(fake):
    x = np.sin(2 * np.pi * 220 * np.arange(4000) / SR)
    assert afxrep.features(x).mid.shape == (16,)
    with pytest.raises(ValueError):
        afxrep.features(np.full(5000, np.nan))
    with pytest.raises(ValueError):
        afxrep.features(np.zeros((3, 3, 3)))


def test_apply_mask():
    x = np.arange(4 * SR, dtype=float)
    assert afxrep.apply_mask(x, None) is x
    m = np.ones_like(x)
    m[SR:2 * SR] = 0.0
    y = afxrep.apply_mask(x, m)
    assert 2.9 * SR < y.size < 3.0 * SR + 1
    tiny = np.zeros_like(x)
    tiny[:1000] = 1.0          # under MIN_KEPT_S confident: whole clip
    assert afxrep.apply_mask(x, tiny).size == x.size


def test_kit_skips_at_zero_weight_and_caches_target(fake, tones):
    a, b = tones
    assert set(EMBED_TERMS) == {"embed_afxrep"} and DEFAULT_WEIGHTS["embed_afxrep"] == 0.0
    kit = LossKit()
    s = kit.score(a[0], b[0])
    assert "embed_afxrep" not in s.terms and fake.calls == 0 and s.total == 0.0
    s1 = kit.score(a[0], b[0], weights={"embed_afxrep": 1.0})
    assert fake.calls == 2 and s1.total == pytest.approx(s1.terms["embed_afxrep"])
    kit.score(a[0], b[1], weights={"embed_afxrep": 1.0})
    assert fake.calls == 3  # target embedding came from the cache


def test_unavailable_without_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_AFXREP_DIR", str(tmp_path))
    assert not afxrep.available()
    with pytest.raises(afxrep.AfxRepUnavailable, match="hf download"):
        afxrep.load_model()
    afxrep.set_embedder(None)
    with pytest.raises(afxrep.AfxRepUnavailable):
        LossKit().score(np.ones(SR), np.ones(SR), weights={"embed_afxrep": 1.0})
    afxrep.set_embedder(None)


def test_device_choice_falls_back_to_cpu():
    enc = FakeEncoder()
    m, dev = afxrep._pick_device(enc, "cpu")
    assert dev == "cpu"
    with pytest.warns(UserWarning, match="using the CPU"):
        m, dev = afxrep._pick_device(enc, "no-such-device")
    assert dev == "cpu" and next(m.parameters()).device.type == "cpu"


# ------------------------------------------------------------- real checkpoint (skips if absent)

real = pytest.mark.skipif(not afxrep.available(), reason="AFx-Rep checkpoint or embed extra absent")


@pytest.fixture(scope="module")
def real_embedder():
    e = afxrep.Embedder(device="cpu", threads=2)
    afxrep.set_embedder(e)
    yield e
    afxrep.set_embedder(None)


@real
def test_real_model_embeds_512_unit(real_embedder, tones):
    a, _ = tones
    f = afxrep.features(a[0])
    assert f.mid.shape == (512,) and np.linalg.norm(f.mid) == pytest.approx(1.0, abs=1e-5)
    g = afxrep.features(np.stack([a[0], np.roll(a[0], 480)]))
    assert g.side.shape == (512,)


@real
def test_real_model_is_insensitive_to_notes(real_embedder):
    # contract invariance: same tone on different notes is closer than a different tone
    dis = [proxy.guitar_di(s, 6.0, rhythm_seed=7) for s in (11, 12, 13)]
    ta, tb = ProxyPreset(drive=0.1), ProxyPreset(drive=0.9, cab=3)
    ya = [proxy.render(ta, d) for d in dis]
    yb = [proxy.render(tb, d) for d in dis]
    same = [afxrep.term(ya[i], ya[j]) for i in range(3) for j in range(3) if i != j]
    diff = [afxrep.term(ya[i], yb[j]) for i in range(3) for j in range(3) if i != j]
    assert max(same) < min(diff)


@real
def test_vendored_encoder_matches_upstream(real_embedder):
    src = afxrep.CACHE_ROOT / "models" / "st-ito-src" / "st_ito" / "models" / "panns.py"
    if not src.is_file():
        pytest.skip("st-ito source not cloned into cache/models/st-ito-src")
    pytest.importorskip("torchlibrosa")
    import importlib.util
    spec = importlib.util.spec_from_file_location("st_ito_panns", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ours = afxrep.load_model()
    theirs = mod.Cnn14(**afxrep._encoder_args(afxrep.model_dir()))
    theirs.load_state_dict(ours.state_dict())
    theirs.eval()
    x = torch.from_numpy(np.random.default_rng(1).standard_normal((1, 2, 3 * SR)).astype(np.float32))
    with torch.no_grad():
        for p, q in zip(ours(x), theirs(x), strict=True):
            assert torch.allclose(p, q, atol=1e-6)
