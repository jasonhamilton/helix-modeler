"""Pluggable separators. `htdemucs_6s` is the default; RoFormer checkpoints sit behind a flag.

torch is NOT a hard dependency: everything torch-related is imported inside methods, and a missing
extra raises SeparatorUnavailable with the install command. Model weights download into
cache/models/ (never the repo tree, never committed).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar, Protocol

import numpy as np

from tonematch.gpu import SAMPLE_RATE, detect_backend
from tonematch.separation.audio import resample

CACHE_ROOT = Path(os.environ.get("TONEMATCH_CACHE", Path(__file__).resolve().parents[3] / "cache"))
MODEL_DIR = CACHE_ROOT / "models"
DEFAULT_MODEL = "htdemucs_6s"
# demucs 4.1 pulls weights from the HuggingFace hub; keep them in cache/models/, not ~/.cache.
os.environ.setdefault("HF_HUB_CACHE", str(MODEL_DIR / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
ROFORMER_MODELS = ("bs-roformer", "mel-roformer", "mdx23c")


class SeparatorUnavailable(RuntimeError):
    """The model's optional dependencies (or weights) are not installed."""


class Separator(Protocol):
    name: str
    guitar_stem: str

    def separate(self, mix: np.ndarray) -> tuple[dict[str, np.ndarray], str]:
        """mix [2, n] at 48 kHz -> ({stem name: [2, n]}, backend actually used)."""


def torch_device(backend: str) -> tuple[str, str]:
    """Map a gpu.py backend to (torch device, backend actually used). DirectML has no demucs path."""
    if backend in ("cuda", "rocm"):  # ROCm builds of torch expose themselves as "cuda"
        return "cuda", backend
    if backend == "mps":
        return "mps", backend
    return "cpu", "cpu"


class Htdemucs6s:
    """Demucs v4 hybrid transformer, 6 stems. Code MIT; weights see docs/notes/separation-models.md."""

    name = "htdemucs_6s"
    guitar_stem = "guitar"
    model_sr = 44100

    def __init__(self, backend: str | None = None, shifts: int = 1, overlap: float = 0.25):
        self.backend = backend or detect_backend()
        self.shifts, self.overlap = shifts, overlap
        self._model = None

    def _load(self):
        try:
            import torch
            from demucs.pretrained import get_model
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise SeparatorUnavailable(
                "htdemucs_6s needs the optional extra: pip install -e '.[separation]'") from e
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        torch.hub.set_dir(str(MODEL_DIR / "torch"))
        if self._model is None:
            self._model = get_model(self.name)
            self._model.eval()
        return self._model

    def separate(self, mix: np.ndarray) -> tuple[dict[str, np.ndarray], str]:
        import torch
        from demucs.apply import apply_model

        model = self._load()
        device, used = torch_device(self.backend)
        x = resample(mix, SAMPLE_RATE, self.model_sr)
        ref = x.mean(axis=0)
        mean, std = ref.mean(), ref.std() + 1e-8
        wav = torch.from_numpy(((x - mean) / std).astype(np.float32))[None]
        try:
            with torch.no_grad():
                out = apply_model(model, wav, device=device, shifts=self.shifts,
                                  split=True, overlap=self.overlap, progress=False)[0]
        except (RuntimeError, NotImplementedError):
            if device == "cpu":
                raise
            with torch.no_grad():  # an op missing on this accelerator: CPU always works
                out = apply_model(model, wav, device="cpu", shifts=self.shifts,
                                  split=True, overlap=self.overlap, progress=False)[0]
            used = "cpu"
        out = out.cpu().numpy() * std + mean
        n = mix.shape[1]
        stems = {}
        for name, s in zip(model.sources, out, strict=True):
            y = resample(s.astype(np.float32), self.model_sr, SAMPLE_RATE)
            y = y[:, :n]
            if y.shape[1] < n:
                y = np.pad(y, ((0, 0), (0, n - y.shape[1])))
            stems[name] = y.astype(np.float32)
        return stems, used


class AudioSeparatorRoformer:
    """BS-/Mel-RoFormer and MDX23C checkpoints through the `audio-separator` package.

    Only ONE checkpoint in audio-separator 0.47's catalogue produces a guitar stem:
    `BS-Roformer-SW.ckpt` (6 stems). Its licence is NOT STATED (see docs/notes/separation-models.md),
    so it is not loaded unless the user opts in with TONEMATCH_ROFORMER_LICENCE_ACK=1 or names a
    checkpoint explicitly in TONEMATCH_ROFORMER_CKPT. The other catalogue RoFormer/MDX23C files
    (vocals, instrumental, karaoke, de-reverb, drumsep) have no guitar output. Weights download
    into cache/models/audio-separator/.
    """

    guitar_stem = "guitar"
    # None = no catalogue checkpoint of this family produces a guitar stem.
    default_ckpt: ClassVar[dict[str, str | None]] = {
        "bs-roformer": "BS-Roformer-SW.ckpt",
        "mel-roformer": None,
        "mdx23c": None,
    }

    def __init__(self, name: str, backend: str | None = None):
        self.name = name
        self.backend = backend or detect_backend()

    def separate(self, mix: np.ndarray) -> tuple[dict[str, np.ndarray], str]:
        try:
            from audio_separator.separator import Separator as _Sep  # type: ignore
        except ImportError as e:
            raise SeparatorUnavailable(
                f"{self.name} needs the optional extra: pip install -e '.[separation-roformer]'"
            ) from e
        import tempfile

        import soundfile as sf

        explicit = os.environ.get("TONEMATCH_ROFORMER_CKPT")
        ckpt = explicit or self.default_ckpt[self.name]
        if ckpt is None:
            raise SeparatorUnavailable(
                f"{self.name}: no audio-separator catalogue checkpoint of this family has a guitar "
                "stem. Use 'bs-roformer' (BS-Roformer-SW, licence unstated) or htdemucs_6s.")
        if (not explicit and ckpt == self.default_ckpt["bs-roformer"]
                and os.environ.get("TONEMATCH_ROFORMER_LICENCE_ACK") != "1"):
            raise SeparatorUnavailable(
                "BS-Roformer-SW has no stated licence; set TONEMATCH_ROFORMER_LICENCE_ACK=1 to "
                "accept that for private local use (docs/notes/separation-models.md).")
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "mix.wav"
            sf.write(str(src), mix.T, SAMPLE_RATE, subtype="FLOAT")
            sep = _Sep(output_dir=td, model_file_dir=str(MODEL_DIR / "audio-separator"),
                       output_format="WAV")
            sep.load_model(ckpt)
            files = sep.separate(str(src))
            stems = {}
            for f in files:
                p = Path(td) / f if not Path(f).is_absolute() else Path(f)
                data, sr = sf.read(str(p), dtype="float32", always_2d=True)
                label = p.stem.split("(")[-1].rstrip(")").lower()
                stems[label] = resample(data.T, sr, SAMPLE_RATE)[:, :mix.shape[1]]
        if "guitar" not in stems:
            raise SeparatorUnavailable(
                f"checkpoint {ckpt} produced stems {sorted(stems)}; it has no guitar output. "
                "Set TONEMATCH_ROFORMER_CKPT to a guitar-capable checkpoint.")
        return stems, torch_device(self.backend)[1]


def get_separator(name: str = DEFAULT_MODEL, backend: str | None = None) -> Separator:
    if name == DEFAULT_MODEL:
        return Htdemucs6s(backend)
    if name in ROFORMER_MODELS:
        return AudioSeparatorRoformer(name, backend)
    raise ValueError(f"unknown model {name!r}; expected {DEFAULT_MODEL} or one of {ROFORMER_MODELS}")
