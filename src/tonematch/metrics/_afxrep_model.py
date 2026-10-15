# AFx-Rep encoder (Cnn14), vendored for the `embed_afxrep` loss term.
#
# Source: https://github.com/csteinmetz1/st-ito/blob/ec50e0cc647cc3637d0cbbfc01d059c682c9fb27/st_ito/models/panns.py
# Author Christian J. Steinmetz (setup.py; the LICENSE names no copyright holder). Licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). That file is itself "Adapted from
# https://github.com/qiuqiangkong/audioset_tagging_cnn/blob/master/pytorch/models.py / Under MIT License"
# (Copyright (c) 2018-2020 Qiuqiang Kong). Licence texts: licenses/Apache-2.0-st-ito.txt and
# licenses/MIT-panns-audioset_tagging_cnn.txt.
#
# Changes made here (Apache-2.0 section 4(b)): kept only ConvBlock and Cnn14; removed ConvBlock5x5,
# SpecAugmentation and the training-only dropout and augmentation branches (this copy is inference
# only); removed weight initialisation (weights always come from the checkpoint); the "batchnorm" and
# "none" input-norm options are kept. Parameter names and shapes are unchanged, so the checkpoint's
# `encoder.*` state dict loads as is. See docs/notes/afx-rep.md.
"""Cnn14 encoder of AFx-Rep (ST-ITO). Needs torch and torchlibrosa (the `embed` extra)."""
from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn
from torchlibrosa.stft import LogmelFilterBank, Spectrogram


class ConvBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, use_batchnorm: bool = True):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=(3, 3), stride=(1, 1),
                               padding=(1, 1), bias=False)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=(3, 3), stride=(1, 1),
                               padding=(1, 1), bias=False)
        self.bn1 = nn.BatchNorm2d(out_channels) if use_batchnorm else nn.Identity()
        self.bn2 = nn.BatchNorm2d(out_channels) if use_batchnorm else nn.Identity()

    def forward(self, x: torch.Tensor, pool_size=(2, 2)) -> torch.Tensor:
        x = F.relu_(self.bn1(self.conv1(x)))
        x = F.relu_(self.bn2(self.conv2(x)))
        return F.avg_pool2d(x, kernel_size=pool_size)


class Cnn14(nn.Module):
    def __init__(self, embed_dim: int, sample_rate: float, window_size: int, hop_size: int,
                 mel_bins: int, fmin: float, fmax: float, use_batchnorm: bool = False,
                 input_norm: str = "batchnorm"):
        super().__init__()
        self.embed_dim = embed_dim
        self.input_norm = input_norm
        self.spectrogram_extractor = Spectrogram(
            n_fft=window_size, hop_length=hop_size, win_length=window_size, window="hann",
            center=True, pad_mode="reflect", freeze_parameters=True)
        self.logmel_extractor = LogmelFilterBank(
            sr=sample_rate, n_fft=window_size, n_mels=mel_bins, fmin=fmin, fmax=fmax, ref=1.0,
            amin=1e-10, top_db=None, freeze_parameters=True)
        self.bn0 = nn.BatchNorm2d(mel_bins)
        chans = (1, 64, 128, 256, 512, 1024, 2048)
        for i in range(6):
            setattr(self, f"conv_block{i + 1}", ConvBlock(chans[i], chans[i + 1], use_batchnorm))
        self.fc_mid = nn.Linear(2048, embed_dim, bias=True)
        self.fc_side = nn.Linear(2048, embed_dim, bias=True)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """x: (batch, chs, seq_len) waveform, chs 1 or 2 -> (mid_embed, side_embed), each
        (batch, embed_dim), not normalised. Mono input runs the mid head and returns it twice."""
        batch_size, chs, seq_len = x.size()
        if chs == 2:
            x = torch.stack([(x[:, 0] + x[:, 1]) / 2, (x[:, 0] - x[:, 1]) / 2], dim=1)
        elif chs != 1:
            raise ValueError(f"Invalid number of channels: {chs}")
        x = x.reshape(batch_size * chs, seq_len)
        x = self.logmel_extractor(self.spectrogram_extractor(x))  # (b*chs, 1, frames, mels)
        if self.input_norm == "batchnorm":
            x = self.bn0(x.transpose(1, 3)).transpose(1, 3)
        elif self.input_norm == "minmax":
            x = (x.clamp(-80, 40.0) + 80) / 120 * 2 - 1
        elif self.input_norm != "none":
            raise ValueError(f"Invalid input_norm: {self.input_norm}")
        for i in range(5):
            x = getattr(self, f"conv_block{i + 1}")(x, pool_size=(2, 2))
        x = self.conv_block6(x, pool_size=(1, 1))
        x = torch.mean(x, dim=3)
        x = torch.max(x, dim=2)[0] + torch.mean(x, dim=2)
        x = x.view(batch_size, chs, -1)
        mid = self.fc_mid(x[:, 0])
        side = mid if chs == 1 else self.fc_side(x[:, 1])
        return mid, side
