"""Term `embed_afxrep`: AFx-Rep (ST-ITO) audio-effects embedding distance (overall character).

AFx-Rep is a Cnn14 encoder trained by Steinmetz et al. (ST-ITO) to say which effects and settings
were applied, regardless of the source material. It is not trained on Helix, nor on guitar amps
specifically. Licence and training-data notes: docs/notes/afx-rep.md.

Features: the L2-normalised 512-d `mid` embedding (and `side` for stereo input) of the clip,
peak-normalised and at 48 kHz, exactly as `st_ito.utils.get_param_embeds`. Distance: `1 - cos`,
averaged over mid and side when both are stereo (ST-ITO's search objective is minus the cosine
similarity averaged over mid and side). Range [0, 2]; level-invariant through the peak normalisation.

Heavy and optional: needs the `embed` extra (torch, torchlibrosa) and the checkpoint in
`cache/models/afx-rep/` (or `TONEMATCH_AFXREP_DIR`). None of that is imported until the first
embedding. A missing piece raises `AfxRepUnavailable` with what to do. The model loads once per
process (`get_embedder()`). Device: `TONEMATCH_AFXREP_DEVICE=auto|mps|cuda|cpu` (default auto: MPS or
CUDA if it agrees with CPU on a probe clip, else CPU). Tests inject a tiny fake model with
`set_embedder(Embedder(model=...))`.
"""
from __future__ import annotations

import os
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tonematch.metrics._common import SAMPLE_RATE, mask_frames

NAME = "embed_afxrep"
CACHE_ROOT = Path(os.environ.get("TONEMATCH_CACHE", Path(__file__).resolve().parents[3] / "cache"))
HF_REPO = "csteinmetz1/afx-rep"
HF_REVISION = "6582cb5414094146759319819a4242967ecc7e39"
CKPT_SHA256 = "3587c4f3a1a8ecbc53b8023c480a0e6ff80719bcc26ce6ee6d08b8daf41d75d4"
MIN_SAMPLES = 65_536        # ~1.4 s: Cnn14 pools time by 32; shorter clips are repeat-padded
MASK_MIN = 0.5              # target frames with mean separator confidence below this are dropped
MASK_HOP = 1024
MIN_KEPT_S = 1.0            # if fewer confident seconds than this survive, use the whole clip
PROBE_TOL = 1e-3            # max 1 - cos between accelerator and CPU embeddings on the probe clip


class AfxRepUnavailable(RuntimeError):
    """The `embed` extra or the AFx-Rep checkpoint is missing."""


def model_dir() -> Path:
    return Path(os.environ.get("TONEMATCH_AFXREP_DIR", CACHE_ROOT / "models" / "afx-rep"))


def available() -> bool:
    """True when the extra is importable and the checkpoint (or extracted encoder) is present."""
    d = model_dir()
    if not (d / "config.yaml").is_file() or not ((d / "encoder.pt").is_file()
                                                 or (d / "afx-rep.ckpt").is_file()):
        return False
    try:
        import torch  # noqa: F401
        import torchlibrosa  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def _encoder_args(d: Path) -> dict:
    import yaml
    with open(d / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    enc = cfg["model"]["init_args"]["encoder"]
    if not enc["class_path"].endswith("panns.Cnn14"):
        raise AfxRepUnavailable(f"unexpected encoder {enc['class_path']} in {d / 'config.yaml'}")
    return dict(enc["init_args"])


def load_model(d: Path | None = None):
    """Build Cnn14 from config.yaml and load the encoder weights (CPU, eval mode)."""
    d = model_dir() if d is None else Path(d)
    try:
        import torch

        from tonematch.metrics._afxrep_model import Cnn14
    except ImportError as e:
        raise AfxRepUnavailable(f"embed_afxrep needs the `embed` extra: pip install -e .[embed] ({e})") from e
    if not (d / "config.yaml").is_file():
        raise AfxRepUnavailable(
            f"no AFx-Rep config in {d}; fetch with: hf download {HF_REPO} afx-rep.ckpt config.yaml "
            f"--revision {HF_REVISION} --local-dir {d}  (1.16 GB; see docs/notes/afx-rep.md)")
    enc_path = d / "encoder.pt"
    if enc_path.is_file():
        state = torch.load(enc_path, map_location="cpu", weights_only=True)
    else:
        ckpt = d / "afx-rep.ckpt"
        if not ckpt.is_file():
            raise AfxRepUnavailable(f"no afx-rep.ckpt in {d}; see docs/notes/afx-rep.md")
        full = torch.load(ckpt, map_location="cpu", weights_only=True)  # no pickle code runs
        state = {k[len("encoder."):]: v for k, v in full["state_dict"].items()
                 if k.startswith("encoder.")}
        del full
        try:  # the encoder alone is ~330 MB of the 1.16 GB checkpoint (the rest is optimiser state)
            torch.save(state, enc_path)
        except OSError:
            pass
    model = Cnn14(**_encoder_args(d))
    model.load_state_dict(state)
    return model.eval()


def _timed(model, x, reps: int = 2) -> tuple[object, float]:
    import time

    import torch
    with torch.no_grad():
        out = model(x)[0]          # warm-up (kernel compilation on accelerators)
        t0 = time.perf_counter()
        for _ in range(reps):
            out = model(x)[0]
        out = out.cpu()            # synchronises the device
    return torch.nn.functional.normalize(out, dim=-1), (time.perf_counter() - t0) / reps


def _pick_device(model, requested: str):
    """Return (model on device, device name). An accelerator is used only if its embedding of a
    probe clip matches the CPU's (1 - cos <= PROBE_TOL); with `auto` it must also be faster than the
    CPU on that probe (measured 2026-10-14 on this Mac: CPU 0.064 s, MPS 0.246 s per 10 s clip)."""
    import torch
    if requested == "cpu":
        return model.to("cpu"), "cpu"
    cands = [requested] if requested != "auto" else (
        ["cuda"] if torch.cuda.is_available() else
        ["mps"] if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else [])
    if not cands:
        return model.to("cpu"), "cpu"
    rng = np.random.default_rng(0)
    probe = torch.from_numpy((0.3 * rng.standard_normal((1, 1, 10 * SAMPLE_RATE))).astype(np.float32))
    ref, t_cpu = _timed(model.to("cpu"), probe)
    for dev in cands:
        try:
            m = model.to(dev)
            out, t_dev = _timed(m, probe.to(dev))
            if float(1 - (out * ref).sum()) <= PROBE_TOL and (requested != "auto" or t_dev < t_cpu):
                return m, dev
        except Exception as e:  # noqa: BLE001 - any backend failure means "use the CPU"
            warnings.warn(f"{NAME}: device {dev!r} failed ({e}); using the CPU", stacklevel=2)
        model = model.to("cpu")
    return model.to("cpu"), "cpu"


@dataclass(frozen=True)
class AfxRepFeatures:
    mid: np.ndarray            # (512,) unit norm
    side: np.ndarray | None    # (512,) unit norm, only for stereo input


class Embedder:
    """Wraps an encoder `model(x: (b, chs, n) float32 tensor) -> (mid, side)`; any torch module with
    that signature works (tests use a tiny one)."""

    def __init__(self, model=None, device: str | None = None, threads: int | None = None):
        self._model, self._requested = model, device
        self._device: str | None = None
        self._threads = threads
        self._lock = threading.Lock()

    def _ready(self):
        with self._lock:
            if self._device is None:  # place once: load if needed, probe the device, fall back to CPU
                import torch
                if self._threads:
                    torch.set_num_threads(self._threads)
                if self._model is None:
                    self._model = load_model()
                req = self._requested or os.environ.get("TONEMATCH_AFXREP_DEVICE", "auto")
                self._model, self._device = _pick_device(self._model.eval(), req)
        return self._model

    @property
    def device(self) -> str:
        self._ready()
        return self._device

    def embed_batch(self, clips: list[np.ndarray]) -> list[AfxRepFeatures]:
        """clips: 48 kHz arrays, each (n,) mono or (2, n) stereo, all with the same channel count.
        Clips are repeat-padded to the longest so they batch; for mixed lengths call one at a time
        if exactness matters (the pooling is over the padded length)."""
        import torch
        model = self._ready()
        xs = [_prep(c) for c in clips]
        chs = {x.shape[0] for x in xs}
        if len(chs) != 1:
            raise ValueError("embed_batch: mix of mono and stereo clips")
        n = max(x.shape[1] for x in xs)
        batch = np.stack([_repeat_pad(x, n) for x in xs]).astype(np.float32)
        with torch.no_grad():
            mid, side = model(torch.from_numpy(batch).to(self._device))
        mid = torch.nan_to_num(torch.nn.functional.normalize(mid.float(), p=2, dim=-1)).cpu().numpy()
        side = torch.nan_to_num(torch.nn.functional.normalize(side.float(), p=2, dim=-1)).cpu().numpy()
        stereo = chs == {2}
        return [AfxRepFeatures(mid=mid[i].astype(np.float64),
                               side=side[i].astype(np.float64) if stereo else None)
                for i in range(len(xs))]

    def embed(self, x: np.ndarray) -> AfxRepFeatures:
        return self.embed_batch([x])[0]


def _prep(x: np.ndarray) -> np.ndarray:
    """(chs, n) float64, peak-normalised per clip (as get_param_embeds), at least MIN_SAMPLES long."""
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 1:
        a = a[None]
    elif a.ndim == 2 and a.shape[0] > 2 and a.shape[1] <= 2:
        a = a.T
    if a.ndim != 2 or a.shape[0] not in (1, 2) or a.shape[1] == 0:
        raise ValueError(f"{NAME}: expected (n,) or (2, n) audio, got {np.shape(x)}")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{NAME}: audio contains NaN or inf")
    a = a / max(float(np.abs(a).max()), 1e-8)
    return _repeat_pad(a, MIN_SAMPLES) if a.shape[1] < MIN_SAMPLES else a


def _repeat_pad(a: np.ndarray, n: int) -> np.ndarray:
    if a.shape[1] >= n:
        return a
    reps = int(np.ceil(n / a.shape[1]))
    return np.tile(a, (1, reps))[:, :n]


def apply_mask(x: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """Keep only the target's confident regions: frames (hop 1024) whose mean confidence is at least
    MASK_MIN, concatenated with 5 ms crossfades. Falls back to the whole clip if under MIN_KEPT_S."""
    if mask is None:
        return x
    a = np.asarray(x, dtype=np.float64)
    n = a.shape[-1]
    mf = mask_frames(mask, n, MASK_HOP, MASK_HOP)
    keep = np.repeat(mf >= MASK_MIN, MASK_HOP)[:n]
    if keep.sum() < MIN_KEPT_S * SAMPLE_RATE or keep.all():
        return a
    edges = np.flatnonzero(np.diff(np.concatenate([[0], keep.astype(np.int8), [0]])))
    runs = [a[..., s:e] for s, e in zip(edges[::2], edges[1::2], strict=True)]
    fade = int(0.005 * SAMPLE_RATE)
    out = runs[0]
    for r in runs[1:]:
        k = min(fade, out.shape[-1], r.shape[-1])
        if k == 0:
            out = np.concatenate([out, r], axis=-1)
            continue
        w = np.linspace(0.0, 1.0, k)
        mid = out[..., -k:] * (1 - w) + r[..., :k] * w
        out = np.concatenate([out[..., :-k], mid, r[..., k:]], axis=-1)
    return out


_EMBEDDER: Embedder | None = None
_EMBEDDER_LOCK = threading.Lock()


def get_embedder() -> Embedder:
    """The process-wide embedder (the model loads once, on first use)."""
    global _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is None:
            _EMBEDDER = Embedder()
        return _EMBEDDER


def set_embedder(e: Embedder | None) -> None:
    """Replace the process-wide embedder (tests, or a caller that wants a fixed device)."""
    global _EMBEDDER
    with _EMBEDDER_LOCK:
        _EMBEDDER = e


def features(x: np.ndarray, mask: np.ndarray | None = None) -> AfxRepFeatures:
    return get_embedder().embed(apply_mask(x, mask))


def distance(a: AfxRepFeatures, b: AfxRepFeatures) -> float:
    d = 1.0 - float(np.dot(a.mid, b.mid))
    if a.side is not None and b.side is not None:
        d = 0.5 * (d + 1.0 - float(np.dot(a.side, b.side)))
    return max(d, 0.0)


def term(target: np.ndarray, candidate: np.ndarray, mask: np.ndarray | None = None) -> float:
    return distance(features(target, mask), features(candidate))
