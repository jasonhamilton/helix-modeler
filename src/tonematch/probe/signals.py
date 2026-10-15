"""The block-probe signal set, version `probe-v1` (T046).

One mono float32 file at 48 kHz, made of segments separated by silence. Every segment's place and
parameters are in the layout (`ProbeSet.layout()`), so the analysis can find each part of a wet
render once it knows the latency. The layout is deterministic from code: rebuild it with the same
version and you get the same samples (the synthetic part is hashed into `synthetic_sha256`).

Level convention: dBFS is the PEAK of a sine (a full-scale sine is 0 dBFS).

Order (time-based segments first, so their tails are not polluted by louder earlier segments):

  lead-in silence
  impulse        single sample at -6 dBFS, 4 s tail             delay, reverb, (IR check)
  mod_sine       1 kHz at -18 dBFS, 4 s                          modulation rate and depth
  ess_<L>        Farina exponential sweep 20 Hz-20 kHz, 1.5 s,   linear frequency response per level
                 at -40, -24, -12, -6 dBFS, 0.75 s gap each
  steps_<f>      stepped sines 110/440/1760 Hz, 8 levels         harmonics H2..H9, THD, static curve,
                 -45..-3 dBFS ascending, 0.25 s per level, 1 s   clipping curve and symmetry
                 gap after each frequency (compressor release)
  tt_<f1>_<f2>_<L>  two-tone 440+554 Hz and 110+165 Hz (3:2     intermodulation
                 power-chord interval), -24 and -12 dBFS/tone
  level_step     1 kHz -40 / -10 / -40 dBFS, 1.5 / 1.0 / 1.5 s  gain-reduction envelope, attack,
                                                                 release, sag, level-dependent gain
  bursts         1 kHz -12 dBFS, 3 x 50 ms on / 250 ms off      attack overshoot
  gate_stair     1 kHz -30..-80 dBFS in 10 dB steps             gate / expander threshold
  di_<k>         2-3 real DI excerpts (Guitar-TECHS, referenced  history-dependent behaviour on
                 by path under data/di, never copied into the    real playing: coherence, effective
                 repo), peak-normalised to -10 dBFS              response, gain
  marker         50 ms linear chirp 500 Hz-4 kHz at -12 dBFS     latency (sync marker)
  tail silence

The sync marker is at the END so its own reverb or delay tail cannot leak into the impulse segment.
Latency up to `MAX_LATENCY_S` is supported; an engine with a known latency can pass it instead.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48_000
PROBE_VERSION = "probe-v1"
MAX_LATENCY_S = 0.25

ESS_F1, ESS_F2, ESS_T = 20.0, 20_000.0, 1.5
ESS_GAP_S = 0.75
ESS_LEVELS = (-40.0, -24.0, -12.0, -6.0)
STEP_FREQS = (110.0, 440.0, 1760.0)
STEP_LEVELS = (-45.0, -39.0, -33.0, -27.0, -21.0, -15.0, -9.0, -3.0)
STEP_S = 0.25
STEP_GROUP_GAP_S = 1.0
TWO_TONE_PAIRS = ((440.0, 554.0), (110.0, 165.0))
TWO_TONE_LEVELS = (-24.0, -12.0)
TWO_TONE_S, TWO_TONE_GAP_S = 0.5, 0.1
CARRIER_HZ = 1000.0
LEVEL_STEP = ((-40.0, 1.5), (-10.0, 1.0), (-40.0, 1.5))
BURSTS = (3, 0.05, 0.25, -12.0)  # count, on s, off s, level
GATE_LEVELS = (-30.0, -40.0, -50.0, -60.0, -70.0, -80.0)
GATE_ON_S, GATE_OFF_S = 0.25, 0.15
IMPULSE_DBFS, IMPULSE_TAIL_S = -6.0, 4.0
MOD_DBFS, MOD_S = -18.0, 4.0
MARKER_DBFS, MARKER_S, MARKER_F = -12.0, 0.05, (500.0, 4000.0)
DI_PEAK_DBFS, DI_S, DI_GAP_S = -10.0, 2.5, 0.5

# Real DI excerpts, relative to the DI library root (data/di). Guitar-TECHS, CC BY 4.0. Chords,
# palm-muted single notes, bends: three kinds of history the sines cannot show.
DI_CLIPS: tuple[dict, ...] = (
    {"id": "di_chords", "path": "Guitar-TECHS/P1_chords/audio/directinput/directinput_Drop3_7.wav",
     "start_s": 144.0},
    {"id": "di_palmmute",
     "path": "Guitar-TECHS/P1_techniques/audio/directinput/directinput_PalmMute.wav",
     "start_s": 52.0},
    {"id": "di_bends", "path": "Guitar-TECHS/P1_techniques/audio/directinput/directinput_Bendings.wav",
     "start_s": 220.0},
)


def db2amp(db: float) -> float:
    return float(10.0 ** (db / 20.0))


@dataclass
class Segment:
    name: str
    kind: str
    start: int          # samples into the probe
    length: int         # samples (includes any trailing gap that belongs to the segment)
    params: dict = field(default_factory=dict)

    def slice(self, latency: int = 0) -> slice:
        return slice(self.start + latency, self.start + latency + self.length)


@dataclass
class ProbeSet:
    signal: np.ndarray                  # float32 (n,)
    segments: list[Segment]
    sample_rate: int = SAMPLE_RATE
    version: str = PROBE_VERSION
    di: list[dict] = field(default_factory=list)   # references, with excerpt hashes
    synthetic_sha256: str = ""

    def seg(self, name: str) -> Segment:
        for s in self.segments:
            if s.name == name:
                return s
        raise KeyError(name)

    def of_kind(self, kind: str) -> list[Segment]:
        return [s for s in self.segments if s.kind == kind]

    @property
    def duration_s(self) -> float:
        return self.signal.size / self.sample_rate

    def layout(self) -> dict:
        return {
            "probe_version": self.version,
            "sample_rate": self.sample_rate,
            "n_samples": int(self.signal.size),
            "duration_s": round(self.duration_s, 4),
            "synthetic_sha256": self.synthetic_sha256,
            "sha256": hashlib.sha256(self.signal.astype("<f4").tobytes()).hexdigest(),
            "level_convention": "dBFS = peak of a sine",
            "max_latency_s": MAX_LATENCY_S,
            "di": self.di,
            "segments": [{"name": s.name, "kind": s.kind, "start": s.start, "length": s.length,
                          "params": s.params} for s in self.segments],
        }


def ess(level_dbfs: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Farina exponential sine sweep, ESS_F1..ESS_F2 over ESS_T s, 50 ms fade-in, 5 ms fade-out."""
    n = round(ESS_T * sr)
    t = np.arange(n) / sr
    L = ESS_T / np.log(ESS_F2 / ESS_F1)
    x = np.sin(2 * np.pi * ESS_F1 * L * (np.exp(t / L) - 1.0))
    fi, fo = int(0.05 * sr), int(0.005 * sr)
    x[:fi] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(fi) / fi)
    x[n - fo:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(fo) / fo)
    return db2amp(level_dbfs) * x


def ess_inverse(sr: int = SAMPLE_RATE) -> np.ndarray:
    """Farina inverse filter of the unit-level sweep: time reversal with a -6 dB/oct envelope."""
    x = ess(0.0, sr)
    n = x.size
    L = ESS_T / np.log(ESS_F2 / ESS_F1)
    tau = (n - 1 - np.arange(n)) / sr        # original time of each reversed sample
    return x[::-1] * np.exp(-tau / L)


def _ramp_env(levels_amp: np.ndarray, ramp: int) -> np.ndarray:
    """Smooth a piecewise-constant amplitude envelope with a raised-cosine of `ramp` samples."""
    if ramp <= 1:
        return levels_amp
    k = np.hanning(ramp + 2)[1:-1]
    k /= k.sum()
    pad = np.pad(levels_amp, (ramp, ramp), mode="edge")
    return np.convolve(pad, k, mode="same")[ramp:-ramp]


def stepped_sine(f: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    n_step = round(STEP_S * sr)
    env = np.repeat([db2amp(L) for L in STEP_LEVELS], n_step)
    env = _ramp_env(env, int(0.005 * sr))
    env[: int(0.005 * sr)] *= np.linspace(0, 1, int(0.005 * sr))
    env[-int(0.005 * sr):] *= np.linspace(1, 0, int(0.005 * sr))
    t = np.arange(env.size) / sr
    return env * np.sin(2 * np.pi * f * t)


def tone(f: float, dur_s: float, level_dbfs: float, fade_s: float = 0.005,
         sr: int = SAMPLE_RATE) -> np.ndarray:
    n = round(dur_s * sr)
    t = np.arange(n) / sr
    x = db2amp(level_dbfs) * np.sin(2 * np.pi * f * t)
    fn = max(1, int(fade_s * sr))
    x[:fn] *= np.linspace(0, 1, fn)
    x[-fn:] *= np.linspace(1, 0, fn)
    return x


def marker(sr: int = SAMPLE_RATE) -> np.ndarray:
    n = round(MARKER_S * sr)
    t = np.arange(n) / sr
    f0, f1 = MARKER_F
    ph = 2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / MARKER_S * t * t)
    return db2amp(MARKER_DBFS) * np.hanning(n) * np.sin(ph)


def _load_di(root: Path, clip: dict, sr: int) -> tuple[np.ndarray, dict]:
    import soundfile as sf

    p = root / clip["path"]
    fsr = sf.info(str(p)).samplerate
    n = round(DI_S * sr)
    x, _ = sf.read(str(p), start=round(clip["start_s"] * fsr),
                   frames=int(np.ceil(DI_S * fsr)) + 1, dtype="float64", always_2d=True)
    x = x.mean(axis=1)
    if fsr != sr:                       # some Guitar-TECHS takes are 44.1 kHz
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(sr, fsr)
        x = resample_poly(x, sr // g, fsr // g)
    x = x[:n]
    if x.size < n:
        raise ValueError(f"{p}: excerpt shorter than {DI_S} s")
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        raise ValueError(f"{p}: excerpt at {clip['start_s']} s is silent")
    gain = db2amp(DI_PEAK_DBFS) / peak
    fade = int(0.005 * sr)
    x = x * gain
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    ref = dict(clip, dur_s=DI_S, gain_db=round(float(20 * np.log10(gain)), 3),
               source="Guitar-TECHS", licence="CC BY 4.0",
               excerpt_sha256=hashlib.sha256(x.astype("<f4").tobytes()).hexdigest())
    return x, ref


def default_di_root() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "di"


def build_probe_set(*, di: bool | str = "auto", di_root: str | Path | None = None,
                    sr: int = SAMPLE_RATE) -> ProbeSet:
    """Build probe-v1. `di`: True (require the DI clips), False (synthetic only), "auto" (use them
    if the library is present). DI audio is read from the local library, never stored in the repo.
    """
    if sr != SAMPLE_RATE:
        raise ValueError("probe-v1 is defined at 48 kHz only")
    parts: list[np.ndarray] = []
    segs: list[Segment] = []
    pos = 0

    def silence(sec: float) -> None:
        nonlocal pos
        n = round(sec * sr)
        parts.append(np.zeros(n))
        pos += n

    def add(name: str, kind: str, x: np.ndarray, params: dict, gap_s: float = 0.0) -> None:
        nonlocal pos
        g = round(gap_s * sr)
        parts.append(np.concatenate([x, np.zeros(g)]))
        segs.append(Segment(name, kind, pos, x.size + g, params))
        pos += x.size + g

    silence(0.5)
    imp = np.zeros(round(IMPULSE_TAIL_S * sr))
    imp[0] = db2amp(IMPULSE_DBFS)
    add("impulse", "impulse", imp, {"level_dbfs": IMPULSE_DBFS, "tail_s": IMPULSE_TAIL_S})
    add("mod_sine", "mod_sine", tone(CARRIER_HZ, MOD_S, MOD_DBFS, fade_s=0.01),
        {"freq_hz": CARRIER_HZ, "level_dbfs": MOD_DBFS, "dur_s": MOD_S}, gap_s=0.5)
    for L in ESS_LEVELS:
        add(f"ess_{int(L)}", "ess", ess(L),
            {"level_dbfs": L, "f1": ESS_F1, "f2": ESS_F2, "sweep_s": ESS_T, "gap_s": ESS_GAP_S},
            gap_s=ESS_GAP_S)
    for f in STEP_FREQS:
        add(f"steps_{int(f)}", "stepped_sine", stepped_sine(f),
            {"freq_hz": f, "levels_dbfs": list(STEP_LEVELS), "step_s": STEP_S},
            gap_s=STEP_GROUP_GAP_S)
    for f1, f2 in TWO_TONE_PAIRS:
        for L in TWO_TONE_LEVELS:
            x = tone(f1, TWO_TONE_S, L) + tone(f2, TWO_TONE_S, L)
            add(f"tt_{int(f1)}_{int(f2)}_{int(L)}", "two_tone", x,
                {"f1": f1, "f2": f2, "level_dbfs_per_tone": L, "dur_s": TWO_TONE_S},
                gap_s=TWO_TONE_GAP_S)
    env = np.concatenate([np.full(round(d * sr), db2amp(L)) for L, d in LEVEL_STEP])
    env = _ramp_env(env, int(0.0005 * sr))
    t = np.arange(env.size) / sr
    add("level_step", "level_step", env * np.sin(2 * np.pi * CARRIER_HZ * t),
        {"freq_hz": CARRIER_HZ, "steps": [[L, d] for L, d in LEVEL_STEP]}, gap_s=0.3)
    count, on, off, L = BURSTS
    bx = np.concatenate([np.concatenate([tone(CARRIER_HZ, on, L, fade_s=0.0005),
                                         np.zeros(round(off * sr))]) for _ in range(count)])
    add("bursts", "bursts", bx, {"freq_hz": CARRIER_HZ, "count": count, "on_s": on, "off_s": off,
                                 "level_dbfs": L})
    gx = np.concatenate([np.concatenate([tone(CARRIER_HZ, GATE_ON_S, Lg, fade_s=0.002),
                                         np.zeros(round(GATE_OFF_S * sr))])
                         for Lg in GATE_LEVELS])
    add("gate_stair", "gate_stair", gx, {"freq_hz": CARRIER_HZ, "levels_dbfs": list(GATE_LEVELS),
                                         "on_s": GATE_ON_S, "off_s": GATE_OFF_S}, gap_s=0.2)
    synth_hash = hashlib.sha256(np.concatenate(parts).astype("<f4").tobytes()).hexdigest()

    refs: list[dict] = []
    root = Path(di_root) if di_root is not None else default_di_root()
    use_di = di is True or (di == "auto" and all((root / c["path"]).exists() for c in DI_CLIPS))
    if di is True and not all((root / c["path"]).exists() for c in DI_CLIPS):
        missing = [c["path"] for c in DI_CLIPS if not (root / c["path"]).exists()]
        raise FileNotFoundError(f"DI clips missing under {root}: {missing}")
    if use_di:
        for clip in DI_CLIPS:
            x, ref = _load_di(root, clip, sr)
            add(clip["id"], "di", x, {k: ref[k] for k in ("path", "start_s", "dur_s", "gain_db")},
                gap_s=DI_GAP_S)
            refs.append(ref)
    silence(0.5)
    add("marker", "marker", marker(), {"level_dbfs": MARKER_DBFS, "f0": MARKER_F[0],
                                       "f1": MARKER_F[1], "dur_s": MARKER_S},
        gap_s=MAX_LATENCY_S + 0.25)
    sig = np.concatenate(parts).astype(np.float32)
    version = PROBE_VERSION if use_di else PROBE_VERSION + "-nodi"
    return ProbeSet(sig, segs, sr, version, refs, synth_hash)


def write_probe_set(probe: ProbeSet, out_dir: str | Path) -> tuple[Path, Path]:
    """Write `<version>.wav` (float32) and `<version>.json` (layout) under out_dir."""
    import soundfile as sf

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wav = out / f"{probe.version}.wav"
    js = out / f"{probe.version}.json"
    sf.write(str(wav), probe.signal, probe.sample_rate, subtype="FLOAT")
    js.write_text(json.dumps(probe.layout(), indent=1) + "\n")
    return wav, js


def probe_from_layout(layout: dict | str | Path, *, di_root: str | Path | None = None) -> ProbeSet:
    """Rebuild the probe a layout describes and check it is the same (version and sample hash)."""
    if not isinstance(layout, dict):
        layout = json.loads(Path(layout).read_text())
    want_di = bool(layout.get("di"))
    if not str(layout["probe_version"]).startswith(PROBE_VERSION):
        raise ValueError(f"layout is {layout['probe_version']}, this code builds {PROBE_VERSION}")
    probe = build_probe_set(di=want_di, di_root=di_root)
    if probe.layout()["sha256"] != layout.get("sha256"):
        raise ValueError("rebuilt probe differs from the layout's sha256 (code or DI changed)")
    return probe
