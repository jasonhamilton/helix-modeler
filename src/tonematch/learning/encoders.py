"""Frozen permissive audio encoders for the encoder-swap measurement (T195, learning). Measurement only.

Two encoders that could stand in for AFx-Rep under the drive-predictor heads (`afx_heads`):

* `PannsEmbedder`: PANNs Cnn14 trained on AudioSet, checkpoint `Cnn14_mAP=0.431.pth` (32 kHz, 64 mels) from
  Zenodo record 3987831 (CC BY 4.0, Kong et al. 2019; md5 541141fa2ee191a88f24a3219fff024e). The architecture is
  the Cnn14 already vendored in `tonematch.metrics._afxrep_model` (MIT, Qiuqiang Kong), with batchnorm in the conv
  blocks and PANNs' own `fc1` (2048 -> 2048) loaded beside it. Feature: the published PANNs `embedding`,
  relu(fc1(pooled)), 2048-d. Input: mono, peak-normalised (as AFx-Rep), resampled 48 -> 32 kHz.
* `DacEmbedder`: the encoder of the Descript Audio Codec 24 kHz 8 kbps model, release 0.0.4 `weights_24khz.pth`
  (sha256 44bad592...7e20ad; the 44 kHz model, release 0.0.1, also loads but costs 0.61 s per 10 s clip on MPS
  against 0.35 s, too slow for T195; code and weights MIT, Copyright (c) 2023-present Descript; licenses/MIT-descript-audio-codec.txt). The encoder
  below is a re-implementation of `dac.model.dac.Encoder` (ResidualUnit / EncoderBlock / Snake1d), with the weight
  norm folded into plain convolutions at load time; the parameter layout follows the original so the checkpoint's
  `encoder.*` tensors load unchanged. Feature: the continuous latent z before the
  quantiser (1024 channels; hop 320 at 24 kHz), pooled as [mean over time, std over time], 2048-d. Input: mono at
  -24 dBFS RMS (DAC normalises its training excerpts to -24 LUFS), resampled from 48 kHz to the model's rate,
  right-padded to a multiple of the hop.

Weights are never in the repo: they live under cache/models/{panns,dac}/ (gitignored) and are fetched by hand
(commands in docs/notes/encoder-swap-t195.md). Both checkpoints load with `torch.load(weights_only=True)`.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
CACHE = Path(os.environ.get("TONEMATCH_CACHE", ROOT / "cache"))
PANNS_PATH = CACHE / "models" / "panns" / "Cnn14_mAP=0.431.pth"
PANNS_MD5 = "541141fa2ee191a88f24a3219fff024e"
PANNS_URL = "https://zenodo.org/records/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
DAC_PATH = CACHE / "models" / "dac" / "weights_24khz_8kbps_0.0.4.pth"
DAC_SHA256 = "44bad592fc393e03eb0be7a5120b7d487fe9612fa41269dc03fca3d4b87e20ad"
DAC_URL = "https://github.com/descriptinc/descript-audio-codec/releases/download/0.0.4/weights_24khz.pth"
DAC44_SHA256 = "a88eed82a7024ccc1facdb1e605c4c2f99281c8118c22c9895ffa846d8fb61aa"   # release 0.0.1 weights.pth
SR_IN = 48_000


class EncoderUnavailable(RuntimeError):
    pass


def _digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _resample(x: np.ndarray, sr_out: int) -> np.ndarray:
    from math import gcd

    from scipy.signal import resample_poly
    g = gcd(SR_IN, sr_out)
    return resample_poly(np.asarray(x, np.float64), sr_out // g, SR_IN // g, axis=-1)


# ------------------------------------------------------------------------------------------ PANNs

def load_panns(path: Path = PANNS_PATH, check: bool = True):
    import torch
    from torch import nn

    from tonematch.metrics._afxrep_model import Cnn14
    if not Path(path).is_file():
        raise EncoderUnavailable(f"no PANNs checkpoint at {path}; fetch: curl -L -o '{path}' '{PANNS_URL}'")
    if check and _digest(Path(path), "md5") != PANNS_MD5:
        raise EncoderUnavailable(f"{path}: md5 does not match Zenodo 3987831 ({PANNS_MD5})")
    sd = torch.load(path, map_location="cpu", weights_only=True)["model"]

    class Panns(nn.Module):
        def __init__(self):
            super().__init__()
            self.trunk = Cnn14(embed_dim=8, sample_rate=32000, window_size=1024, hop_size=320, mel_bins=64,
                               fmin=50, fmax=14000, use_batchnorm=True, input_norm="batchnorm")
            self.fc1 = nn.Linear(2048, 2048)

        def forward(self, x):          # x (b, n) at 32 kHz -> (b, 2048) relu(fc1(pooled))
            t = self.trunk
            x = t.logmel_extractor(t.spectrogram_extractor(x))
            x = t.bn0(x.transpose(1, 3)).transpose(1, 3)
            for i in range(5):
                x = getattr(t, f"conv_block{i + 1}")(x, pool_size=(2, 2))
            x = t.conv_block6(x, pool_size=(1, 1))
            x = torch.mean(x, dim=3)
            x = torch.max(x, dim=2)[0] + torch.mean(x, dim=2)
            return torch.relu(self.fc1(x))

    m = Panns()
    trunk = {k: v for k, v in sd.items() if not k.startswith(("fc1.", "fc_audioset."))}
    missing, unexpected = m.trunk.load_state_dict(trunk, strict=False)
    missing = [k for k in missing if not k.startswith(("fc_mid.", "fc_side."))]
    if missing or unexpected:
        raise EncoderUnavailable(f"PANNs state dict mismatch: missing {missing[:5]}, unexpected {unexpected[:5]}")
    m.fc1.load_state_dict({"weight": sd["fc1.weight"], "bias": sd["fc1.bias"]})
    return m.eval()


class PannsEmbedder:
    name = "panns"
    dim = 2048
    sr = 32_000

    def __init__(self, threads: int | None = None, device: str = "cpu", model=None):
        import torch
        if threads:
            torch.set_num_threads(threads)
        self.device = device
        self.model = (model or load_panns()).to(device)

    def prep(self, x: np.ndarray) -> np.ndarray:
        a = np.asarray(x, np.float64)
        a = a / max(float(np.abs(a).max()), 1e-8)
        return _resample(a, self.sr).astype(np.float32)

    def embed_batch(self, clips: list[np.ndarray]) -> np.ndarray:
        import torch
        xs = [self.prep(c) for c in clips]
        n = max(len(x) for x in xs)
        b = np.stack([np.pad(x, (0, n - len(x))) for x in xs])
        with torch.no_grad():
            return self.model(torch.from_numpy(b).to(self.device)).float().cpu().numpy()


# ------------------------------------------------------------------------------------------ DAC encoder

def _fold_weight_norm(sd: dict, prefix: str) -> dict:
    """{name: tensor} with every `<p>.weight_g` / `<p>.weight_v` pair replaced by `<p>.weight` = g * v / ||v||
    (norm over every dim but 0, as torch.nn.utils.weight_norm with dim=0)."""
    import torch
    out = {}
    for k, v in sd.items():
        if not k.startswith(prefix):
            continue
        k = k[len(prefix):]
        head, _, leaf = k.rpartition(".")
        dot = head + "." if head else ""
        if leaf == "weight_v":
            g = sd[prefix + dot + "weight_g"]
            norm = torch.linalg.vector_norm(v, dim=tuple(range(1, v.dim())), keepdim=True)
            out[dot + "weight"] = g * v / norm
        elif leaf != "weight_g":
            out[k] = v
    return out


def build_dac_encoder(d_model: int = 64, strides=(2, 4, 8, 8), d_latent: int | None = None):
    import torch
    from torch import nn

    class Snake1d(nn.Module):
        def __init__(self, ch):
            super().__init__()
            self.alpha = nn.Parameter(torch.ones(1, ch, 1))

        def forward(self, x):
            return x + (self.alpha + 1e-9).reciprocal() * torch.sin(self.alpha * x).pow(2)

    class ResidualUnit(nn.Module):
        def __init__(self, dim, dilation):
            super().__init__()
            pad = ((7 - 1) * dilation) // 2
            self.block = nn.Sequential(Snake1d(dim), nn.Conv1d(dim, dim, 7, dilation=dilation, padding=pad),
                                       Snake1d(dim), nn.Conv1d(dim, dim, 1))

        def forward(self, x):
            y = self.block(x)
            pad = (x.shape[-1] - y.shape[-1]) // 2
            if pad > 0:
                x = x[..., pad:-pad]
            return x + y

    class EncoderBlock(nn.Module):
        def __init__(self, dim, stride):
            super().__init__()
            self.block = nn.Sequential(ResidualUnit(dim // 2, 1), ResidualUnit(dim // 2, 3),
                                       ResidualUnit(dim // 2, 9), Snake1d(dim // 2),
                                       nn.Conv1d(dim // 2, dim, 2 * stride, stride=stride,
                                                 padding=math.ceil(stride / 2)))

        def forward(self, x):
            return self.block(x)

    class Encoder(nn.Module):
        def __init__(self):
            super().__init__()
            d = d_model
            blocks = [nn.Conv1d(1, d, 7, padding=3)]
            for s in strides:
                d *= 2
                blocks.append(EncoderBlock(d, s))
            blocks += [Snake1d(d), nn.Conv1d(d, d_latent or d, 3, padding=1)]
            self.block = nn.Sequential(*blocks)

        def forward(self, x):
            return self.block(x)
    return Encoder()


def load_dac(path: Path = DAC_PATH, check: bool = True):
    import torch
    if not Path(path).is_file():
        raise EncoderUnavailable(f"no DAC checkpoint at {path}; fetch: curl -L -o '{path}' '{DAC_URL}'")
    if check and _digest(Path(path), "sha256") not in (DAC_SHA256, DAC44_SHA256):
        raise EncoderUnavailable(f"{path}: sha256 matches neither DAC release 0.0.4 (24 kHz) nor 0.0.1 (44 kHz)")
    ck = torch.load(path, map_location="cpu", weights_only=True)
    kw = ck["metadata"]["kwargs"]
    enc = build_dac_encoder(kw["encoder_dim"], tuple(kw["encoder_rates"]), kw.get("latent_dim"))
    enc.load_state_dict(_fold_weight_norm(ck["state_dict"], "encoder."), strict=True)
    enc.hop = int(np.prod(kw["encoder_rates"]))
    enc.sample_rate = int(kw["sample_rate"])
    return enc.eval()


class DacEmbedder:
    name = "dac"
    dim = 2048
    RMS_DB = -24.0

    def __init__(self, threads: int | None = None, device: str = "cpu", model=None):
        import torch
        if threads:
            torch.set_num_threads(threads)
        self.device = device
        self.model = (model or load_dac()).to(device)
        self.hop = int(getattr(self.model, "hop", 320))
        self.sr = int(getattr(self.model, "sample_rate", 24_000))

    def prep(self, x: np.ndarray) -> np.ndarray:
        y = _resample(np.asarray(x, np.float64), self.sr)
        rms = math.sqrt(float(np.mean(y ** 2)) + 1e-20)
        y = np.clip(y * (10 ** (self.RMS_DB / 20) / rms), -1.0, 1.0)
        n = int(math.ceil(len(y) / self.hop) * self.hop)
        return np.pad(y, (0, n - len(y))).astype(np.float32)

    def embed_batch(self, clips: list[np.ndarray]) -> np.ndarray:
        import torch
        xs = [self.prep(c) for c in clips]
        n = max(len(x) for x in xs)
        b = np.stack([np.pad(x, (0, n - len(x))) for x in xs])[:, None]
        with torch.no_grad():
            z = self.model(torch.from_numpy(b).to(self.device)).float()
            f = torch.cat([z.mean(-1), z.std(-1)], 1)
        return f.cpu().numpy()
