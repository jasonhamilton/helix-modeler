"""Reference DI library: index a LOCAL folder of clean guitar DI audio, pick clips by register and style.

Plan option B (v0 default). Nothing is downloaded or bundled here. The manifest format is described
in ``docs/notes/dry-signal.md``. Audio is 48 kHz float32 mono throughout.

``pick_di`` never fails for lack of a library: it falls back to ``fixtures/smoke/di_2s.wav``, which is
SYNTHETIC, and emits a ``UserWarning`` and marks the clip ``synthetic=True``.
"""
from __future__ import annotations

import itertools
import json
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48_000
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY = ROOT / "data" / "di"
FALLBACK_CLIP = ROOT / "fixtures" / "smoke" / "di_2s.wav"
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1
AUDIO_EXT = {".wav", ".flac", ".aiff", ".aif"}
STYLES = ("single", "chords", "muted", "sustained")
F_MIN, F_MAX = 70.0, 1300.0


@dataclass
class DIClip:
    path: str                 # path relative to the library root (absolute for the fallback)
    start_s: float
    dur_s: float
    pitch_lo: float           # MIDI note, 10th percentile of voiced frames
    pitch_hi: float           # MIDI note, 90th percentile
    pitch_med: float
    density: float            # onsets per second
    style: str                # one of STYLES
    clarity: float            # median autocorrelation peak, 0..1 (low = polyphonic)
    rms_dbfs: float
    source: str = ""          # dataset name, free text
    licence: str = ""
    synthetic: bool = False
    root: str = field(default="", repr=False)

    def load(self) -> np.ndarray:
        """Return the excerpt as 48 kHz float32 mono."""
        p = Path(self.path) if Path(self.path).is_absolute() else Path(self.root) / self.path
        return _read(p, self.start_s, self.dur_s)


@dataclass
class DILibrary:
    root: str
    clips: list[DIClip]
    version: int = MANIFEST_VERSION

    def __len__(self) -> int:
        return len(self.clips)


def _read(path: Path, start_s: float = 0.0, dur_s: float | None = None) -> np.ndarray:
    with sf.SoundFile(str(path)) as f:
        sr = f.samplerate
        f.seek(int(start_s * sr))
        x = f.read(int(dur_s * sr) if dur_s else -1, dtype="float32", always_2d=True)
    x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    if sr != SR:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(SR, sr)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    return np.ascontiguousarray(x, dtype=np.float32)


def analyse(x: np.ndarray) -> dict:
    """Cheap numpy-only descriptors of a mono 48 kHz excerpt (not a transcription)."""
    n, hop = 2048, 1024
    dur = len(x) / SR
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    nfr = 1 + (len(x) - n) // hop
    win = np.hanning(n).astype(np.float32)
    lag_lo, lag_hi = int(SR / F_MAX), int(SR / F_MIN)
    rms = np.empty(nfr)
    f0, clar = [], []
    for i in range(nfr):
        fr = x[i * hop:i * hop + n]
        rms[i] = np.sqrt(np.mean(fr**2) + 1e-12)
        if rms[i] < 10 ** (-50 / 20):
            continue
        w = fr * win
        spec = np.fft.rfft(w, 2 * n)
        ac = np.fft.irfft(np.abs(spec) ** 2)[:n]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lag_lo - 1:lag_hi + 1]
        peaks = np.flatnonzero((seg[1:-1] >= seg[:-2]) & (seg[1:-1] > seg[2:]))
        if len(peaks) == 0:
            continue
        # First local maximum within 85% of the best: avoids octave-down errors and the lag-0 slope.
        best = seg[1:-1][peaks].max()
        k = int(peaks[np.argmax(seg[1:-1][peaks] >= 0.85 * best)])
        seg = seg[1:-1]
        f0.append(SR / (lag_lo + k))
        clar.append(float(seg[k]))
    if f0:
        midi = 69 + 12 * np.log2(np.array(f0) / 440.0)
        lo, med, hi = (float(v) for v in np.percentile(midi, [10, 50, 90]))
        clarity = float(np.median(clar))
    else:
        lo = med = hi = 0.0
        clarity = 0.0
    # Onsets: positive flux of the dB envelope.
    db = 20 * np.log10(rms)
    flux = np.diff(db, prepend=db[0])
    on = np.flatnonzero((flux > 6.0) & (db > -45))
    onsets = [] if len(on) == 0 else [on[0]] + [b for a, b in itertools.pairwise(on) if b - a > 4]
    density = len(onsets) / max(dur, 1e-6)
    # Decay after onsets (~0.2 s later, 9 frames at hop 1024) relative to the onset peak, in dB.
    drops = []
    for o in onsets:
        pk = db[o:o + 2].max()
        j = o + 9
        if j < nfr:
            drops.append(db[j] - pk)
    decay = float(np.median(drops)) if drops else 0.0
    if drops and decay < -18:
        style = "muted"
    elif clarity < 0.6:
        style = "chords"
    elif density < 1.0 and (not drops or decay > -9):
        style = "sustained"
    else:
        style = "single"
    return {"pitch_lo": lo, "pitch_hi": hi, "pitch_med": med, "density": float(density), "style": style,
            "clarity": clarity, "rms_dbfs": float(20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-12))}


def index_library(path: str | Path, *, segment_s: float = 4.0, source: str = "", licence: str = "",
                  rebuild: bool = False) -> DILibrary:
    """Scan ``path`` for audio, analyse ``segment_s`` excerpts, write/read ``manifest.json``.

    An existing manifest is reused unless ``rebuild``. Raises FileNotFoundError if the folder is missing.
    """
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"DI library folder not found: {root}")
    mpath = root / MANIFEST
    if mpath.exists() and not rebuild:
        return load_manifest(root)
    clips: list[DIClip] = []
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in AUDIO_EXT or not f.is_file():
            continue
        try:
            x = _read(f)
        except (RuntimeError, sf.LibsndfileError) as e:
            warnings.warn(f"skipping unreadable {f}: {e}", stacklevel=2)
            continue
        seg = int(segment_s * SR)
        for s in range(0, max(len(x) - seg // 2, 1), seg):
            part = x[s:s + seg]
            if len(part) < seg // 2 or np.sqrt(np.mean(part**2)) < 10 ** (-55 / 20):
                continue
            d = analyse(part)
            clips.append(DIClip(path=f.relative_to(root).as_posix(), start_s=s / SR,
                                dur_s=len(part) / SR, source=source, licence=licence, root=str(root), **d))
    lib = DILibrary(root=str(root), clips=clips)
    mpath.write_text(json.dumps({"version": MANIFEST_VERSION,
                                 "clips": [{k: v for k, v in asdict(c).items() if k != "root"} for c in clips]},
                                indent=1))
    return lib


def load_manifest(root: str | Path) -> DILibrary:
    root = Path(root)
    d = json.loads((root / MANIFEST).read_text())
    if d.get("version") != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version {d.get('version')}; rebuild with rebuild=True")
    return DILibrary(root=str(root), clips=[DIClip(root=str(root), **c) for c in d["clips"]])


def _fallback() -> DIClip:
    warnings.warn(
        "No DI library found (data/di/manifest.json). Using fixtures/smoke/di_2s.wav, a SYNTHETIC "
        "plucked-string clip: fine for plumbing, NOT for tone judgements. See data/di/README.md.",
        UserWarning, stacklevel=3)
    x = _read(FALLBACK_CLIP)
    d = analyse(x)
    return DIClip(path=str(FALLBACK_CLIP), start_s=0.0, dur_s=len(x) / SR, source="synthetic",
                  licence="project original", synthetic=True, **d)


def pick_di(register: tuple[float, float] | float | None = None, style: str | None = None, *,
            density: float | None = None, n: int = 3, max_dur_s: float | None = None,
            library: DILibrary | str | Path | None = None) -> list[DIClip]:
    """Pick up to ``n`` clips closest to the target section.

    ``register``: (low, high) MIDI notes, or one centre note. ``style``: one of STYLES or None.
    ``density``: target onsets per second. ``max_dur_s``: skip clips shorter than this need (Stomp mode
    uses 2 to 4 s; clips are never longer than the indexed segment). Different source files are
    preferred, so the picks differ in guitar/pickup, which lets the loss absorb a DI mismatch.
    """
    if style is not None and style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}, got {style!r}")
    if library is None:
        library = DEFAULT_LIBRARY
    if not isinstance(library, DILibrary):
        p = Path(library)
        if (p / MANIFEST).exists():
            library = load_manifest(p)
        elif p.is_dir() and any(f.suffix.lower() in AUDIO_EXT for f in p.rglob("*")):
            library = index_library(p)
        else:
            return [_fallback()]
    if not library.clips:
        return [_fallback()]
    if isinstance(register, (int, float)):
        register = (float(register), float(register))
    cands = [c for c in library.clips if max_dur_s is None or c.dur_s >= max_dur_s - 1e-6]
    if not cands:
        cands = library.clips

    def score(c: DIClip) -> float:
        s = 0.0
        if register is not None:
            lo, hi = register
            s += abs((c.pitch_lo + c.pitch_hi) / 2 - (lo + hi) / 2) + 0.5 * abs((c.pitch_hi - c.pitch_lo) - (hi - lo))
        if style is not None and c.style != style:
            s += 12.0
        if density is not None:
            s += 2.0 * abs(c.density - density)
        return s

    ranked = sorted(cands, key=lambda c: (score(c), c.path, c.start_s))
    out: list[DIClip] = []
    seen: set[str] = set()
    for c in ranked:                       # first pass: one clip per file
        if c.path not in seen:
            out.append(c)
            seen.add(c.path)
        if len(out) == n:
            return out
    for c in ranked:                       # top up from repeats if there are few files
        if c not in out:
            out.append(c)
        if len(out) == n:
            break
    return out
