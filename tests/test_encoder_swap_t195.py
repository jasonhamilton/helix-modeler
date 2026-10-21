"""T195 encoder-swap: weight-norm folding, DAC encoder shapes, PANNs wrapper, paired file bootstrap. No weights needed."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchlibrosa")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch.learning import encoders as E


def _script():
    spec = importlib.util.spec_from_file_location("encoder_swap_t195", ROOT / "scripts/encoder_swap_t195.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_fold_weight_norm_matches_legacy_weight_norm():
    from torch.nn.utils import weight_norm
    torch.manual_seed(0)
    conv = weight_norm(torch.nn.Conv1d(4, 6, 3))
    with torch.no_grad():
        conv.weight_g.mul_(1.7)
    sd = {f"enc.{k}": v for k, v in conv.state_dict().items()}
    folded = E._fold_weight_norm(sd, "enc.")
    plain = torch.nn.Conv1d(4, 6, 3)
    plain.load_state_dict(folded)
    x = torch.randn(2, 4, 50)
    assert torch.allclose(plain(x), conv(x), atol=1e-6)


def test_dac_encoder_shape_and_embedder_feature():
    enc = E.build_dac_encoder(d_model=4, strides=(2, 4, 5, 8), d_latent=16).eval()
    enc.hop, enc.sample_rate = 320, 24_000
    emb = E.DacEmbedder(model=enc)
    f = emb.embed_batch([np.random.default_rng(0).standard_normal(48_000).astype(np.float32)] * 2)
    assert f.shape == (2, 32) and np.isfinite(f).all()


def test_dac_prep_level_and_padding():
    enc = E.build_dac_encoder(d_model=4, d_latent=8).eval()
    enc.hop, enc.sample_rate = 320, 24_000
    emb = E.DacEmbedder(model=enc)
    y = emb.prep(0.01 * np.random.default_rng(1).standard_normal(48_000))
    assert len(y) % 320 == 0 and len(y) >= 24_000
    assert abs(20 * np.log10(np.sqrt(np.mean(y[:24_000] ** 2))) + 24) < 0.5


def test_panns_wrapper_on_random_weights_gives_2048():
    from tonematch.metrics._afxrep_model import Cnn14

    class Fake(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.trunk = Cnn14(8, 32000, 1024, 320, 64, 50, 14000, use_batchnorm=True)

        def forward(self, x):
            return torch.zeros(x.shape[0], 2048)
    f = E.PannsEmbedder(model=Fake().eval()).embed_batch([np.ones(48_000, np.float32)])
    assert f.shape == (1, 2048)


def test_paired_boot_interval_contains_diff_and_counts_files():
    m = _script()
    rng = np.random.default_rng(0)
    files = np.repeat(np.arange(10), 30).astype(str)
    a = (rng.random(300) < 0.8).astype(float)
    b = (rng.random(300) < 0.6).astype(float)
    b[:5] = np.nan
    r = m.paired_boot(a, b, files, n=500)
    assert r["files"] == 10 and r["clips"] == 295
    assert r["lo"] <= r["diff"] <= r["hi"]


def test_zprep_uses_train_rows_only():
    m = _script()
    X = np.random.default_rng(0).standard_normal((20, 3, 8)).astype(np.float32)
    X[10:] += 100.0
    Z = m.zprep(X, np.arange(10))
    flat = Z[:10].reshape(-1, 8) * np.sqrt(8)
    assert np.allclose(flat.mean(0), 0, atol=1e-5) and np.allclose(flat.std(0), 1, atol=1e-3)
