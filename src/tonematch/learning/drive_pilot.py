"""predictor-pilot-v0 (T186): drive-only blind predictor trained on single drive-block renders already on disk.

Measurement only; nothing here is adopted. Design: docs/notes/predictor-design.md section 6, re-scoped to drives.

Data (cached Native 3.82 renders, block-library-v1.2, Guitar-TECHS DI, no new renders):
  * drive-specialist calibration: 60 models x 25 settings x 8 DI excerpts (the same 25 settings on every DI);
  * helix-trust-v2 TRAIN split only, drive-category candidates;
  * gain-trust-v1 (fit and test) and gain-trust-v1b, drive-category candidates.
Never read: synthetic-v1/-v2/-v3 (sealed), helix-trust-v2 test split, any P3 file (Gate 3 reserve proposal).

Split by whole DI file (`split_of`): every render goes where its DI file goes.
Labels: knob position = CC / 127 (Native plays CC/127). Drive / Tone knob names from
cache/drive-maps/knob_sensitivity.json (T179). Families from cache/drive-maps/drive-families-v1.json (T180).
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
FAMILIES = ROOT / "cache/drive-maps/drive-families-v1.json"
KNOBS = ROOT / "cache/drive-maps/knob_sensitivity.json"
DS = ROOT / "cache/drive-specialist"
LT = ROOT / "cache/loss-trust"
SR = 48_000
N_FFT, HOP, N_MELS, FMIN, FMAX = 2048, 960, 128, 30.0, 20_000.0      # 20 ms hop: 500 frames per 10 s
REF_DBFS = -26.7

# Split (registered in docs/notes/predictor-pilot-v0.md before training). Test: two drive-specialist DI files
# (one per player) plus every other file holding gain-trust test renders, except PalmMute (a drive-specialist DI
# kept for training so five specialist DIs train). Calibration: drive-specialist P1 Bb plus every 10th other
# train file in sorted order. Everything else trains.
TEST_DS = ("Guitar-TECHS/P2_chords/audio/directinput/directinput_Drop3_m7b5.wav",
           "Guitar-TECHS/P1_singlenotes/audio/directinput/directinput_allsinglenotes.wav")
CAL_DS = ("Guitar-TECHS/P1_scales/audio/directinput/directinput_Bb.wav",)
KEEP_TRAIN = ("Guitar-TECHS/P2_techniques/audio/directinput/directinput_PalmMute.wav",)


@dataclass
class Rec:
    path: str
    source: str            # ds | htv2 | gt1 | gt1b
    model: str
    family: str
    drive: float           # knob position 0..1 of the model's Drive knob
    tone: float            # nan when the model has no tone knob
    di_file: str
    player: str
    split: str = ""


def _player(p: str) -> str:
    return p.split("/")[1][:2]


def _knob_names() -> dict[str, tuple[str, str | None]]:
    ks = json.loads(KNOBS.read_text())["models"]
    return {m: (e["drive_knobs"][0], (e.get("tone_knobs") or [None])[0]) for m, e in ks.items()}


def families() -> tuple[list[str], list[str], dict[str, str]]:
    m2f = json.loads(FAMILIES.read_text())["model_to_family"]
    fams = sorted(set(m2f.values()))
    models = sorted(m2f)
    return fams, models, m2f


def build_index() -> list[Rec]:
    """Every labelled single-drive render on disk that this pilot may use, with its DI file and split."""
    _, _, m2f = families()
    kn = _knob_names()
    recs: list[Rec] = []
    plan = json.loads((DS / "cal_plan.json").read_text())
    for m, e in plan["models"].items():
        dk, tk = kn[m]
        for si, st in enumerate(e["settings"]):
            for di, d in enumerate(plan["dis"]):
                f = DS / "cal_renders" / m / f"s{si:02d}_d{di}.npy"
                if f.is_file():
                    recs.append(Rec(str(f), "ds", m, m2f[m], st[dk] / 127.0,
                                    st[tk] / 127.0 if tk else math.nan, d["path"], _player(d["path"])))
    for s, tag in (("helix-trust-v2", "htv2"), ("gain-trust-v1", "gt1"), ("gain-trust-v1b", "gt1b")):
        man = json.loads((LT / s / "manifest.json").read_text())
        fp = man["free_params"].get("drive", {})
        for it in man["items"]:
            if s == "helix-trust-v2" and it["split"] != "train":
                continue                       # helix-trust-v2 test split is not used (T186 brief)
            dpath = it["di"]["A"]["path"]
            if _player(dpath) == "P3":
                continue
            for ci, c in enumerate(it["candidates"]):
                m = c["model"]
                if c["category"] != "drive" or m not in m2f:
                    continue
                f = LT / s / "renders" / it["dir"] / f"c{ci:02d}_A.npy"
                if not f.is_file():
                    continue
                dk, tk = kn[m]
                dflt = {n: d for n, _lo, _hi, d in fp.get(m, [])}
                dv = c["knobs"][dk] / 127.0 if dk in c["knobs"] else dflt.get(dk, math.nan)
                tv = (c["knobs"][tk] / 127.0 if tk in c["knobs"] else dflt.get(tk, math.nan)) if tk else math.nan
                recs.append(Rec(str(f), tag, m, m2f[m], float(dv), float(tv), dpath, _player(dpath)))
    assign_splits(recs)
    return recs


def assign_splits(recs: list[Rec]) -> None:
    test_files = set(TEST_DS)
    # gain-trust test items sit on helix-trust-v2 test files; their files go to test unless a training DI
    gt_test = {r.di_file for r in recs if r.source in ("gt1", "gt1b")
               and _gt_split(r) == "test" and r.di_file not in KEEP_TRAIN}
    ds_train = {r.di_file for r in recs if r.source == "ds"} - set(TEST_DS) - set(CAL_DS)
    test_files |= gt_test - ds_train
    others = sorted({r.di_file for r in recs} - test_files - set(CAL_DS) - ds_train)
    cal_files = set(CAL_DS) | set(others[::10])
    for r in recs:
        r.split = "test" if r.di_file in test_files else "cal" if r.di_file in cal_files else "train"


_GT_SPLIT: dict[str, str] = {}


def _gt_split(r: Rec) -> str:
    if not _GT_SPLIT:
        for s in ("gain-trust-v1", "gain-trust-v1b"):
            man = json.loads((LT / s / "manifest.json").read_text())
            for it in man["items"]:
                _GT_SPLIT[str(LT / s / "renders" / it["dir"])] = it["split"]
    return _GT_SPLIT.get(str(Path(r.path).parent), "")


# ---------------------------------------------------------------- features

def mel_frontend(device):
    import librosa
    import torch
    fb = librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=N_MELS, fmin=FMIN, fmax=FMAX)
    fb = torch.tensor(fb, dtype=torch.float32, device=device)
    win = torch.hann_window(N_FFT, device=device)

    def f(x: torch.Tensor) -> torch.Tensor:            # (B, T) float32 -> (B, n_mels, frames) log-mel
        rms = x.pow(2).mean(dim=1, keepdim=True).clamp_min(1e-12).sqrt()
        x = x * (10 ** (REF_DBFS / 20) / rms)
        S = torch.stft(x, N_FFT, HOP, window=win, center=True, return_complex=True).abs().pow(2)
        return torch.log(fb @ S + 1e-5)
    return f


def load_audio(path: str, n: int = 10 * SR) -> np.ndarray:
    a = np.load(path).astype(np.float32).reshape(-1)
    if a.size >= n:
        return a[:n]
    return np.pad(a, (0, n - a.size))


def audio_rms_db(a: np.ndarray) -> float:
    return 10 * math.log10(float(np.mean(a.astype(np.float64) ** 2)) + 1e-20)


# ---------------------------------------------------------------- model

def make_model(n_fam: int, n_models: int, emb: int = 256):
    import torch
    from torch import nn

    class Block(nn.Module):
        def __init__(self, ci, co, tpool):
            super().__init__()
            self.c = nn.Sequential(nn.Conv2d(ci, co, 3, padding=1, bias=False), nn.BatchNorm2d(co), nn.GELU(),
                                   nn.Conv2d(co, co, 3, padding=1, bias=False), nn.BatchNorm2d(co), nn.GELU()) \
                if co <= 64 else nn.Sequential(nn.Conv2d(ci, co, 3, padding=1, bias=False), nn.BatchNorm2d(co),
                                               nn.GELU())
            self.p = nn.MaxPool2d((2, 2 if tpool else 1))

        def forward(self, x):
            return self.p(self.c(x))

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            ch = (32, 64, 128, 128, 256, 256)
            layers, ci = [], 1
            for i, co in enumerate(ch):
                layers.append(Block(ci, co, i < 4))
                ci = co
            self.trunk = nn.Sequential(*layers)
            self.inorm = nn.InstanceNorm2d(1, affine=False)
            self.proj = nn.Sequential(nn.Linear(2 * 256 * 2, emb), nn.GELU(), nn.Dropout(0.2))
            self.fam = nn.Linear(emb, n_fam)
            self.mod = nn.Linear(emb, n_models)
            self.memb = nn.Embedding(n_models, 32)
            self.knob = nn.Sequential(nn.Linear(emb + 32, 128), nn.GELU(), nn.Linear(128, 4))

        def embed(self, x):                     # x (B, mels, frames)
            x = x.unsqueeze(1)
            x = x - x.mean(dim=(2, 3), keepdim=True)
            h = self.trunk(x)                   # (B, 256, 2, t)
            h = h.flatten(1, 2)                 # (B, 512, t)
            z = torch.cat([h.mean(-1), h.std(-1)], 1)
            return self.proj(z)

        def knobs(self, z, m):
            o = self.knob(torch.cat([z, self.memb(m)], 1))
            mu = torch.sigmoid(o[:, 0::2])      # Drive, Tone
            ls = o[:, 1::2].clamp(-6, 2)
            return mu, ls

        def forward(self, x):
            z = self.embed(x)
            return z, self.fam(z), self.mod(z)

    return Net()


def joint_logp(fam_logits, mod_logits, mod_fam, n_fam: int, T: float = 1.0):
    """log p(model) = log p(family) + log p(model | family), the latter a softmax over the family's members."""
    import torch
    lf = torch.log_softmax(fam_logits / T, 1)
    ml = mod_logits / T
    lse = torch.stack([torch.logsumexp(ml[:, mod_fam == f], 1) for f in range(n_fam)], 1)
    return lf[:, mod_fam] + ml - lse[:, mod_fam], lf


def to_jsonable(recs: list[Rec]) -> list[dict]:
    return [asdict(r) for r in recs]
