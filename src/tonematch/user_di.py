"""Play-along user-DI capture on the HX Stomp (plan Step 7 dry signal, accuracy mode A; skill `reamp-capture`).

The player hears a section of their song plus a click count-in on the Stomp's outputs/headphones
(host USB out 1/2, which the Stomp sends straight to its outputs, unprocessed) and plays along.
In the same full-duplex stream the script records USB in 5/6 (the dry Main L/R inputs, H4 in
docs/notes/T003-stomp-control-spike.md) and USB in 1/2 (Path A, the processed sound). Because
input and output share the stream clock, trimming the recording by the calibrated round trip
(``cache/stomp/latency.json``) puts the DI on the song's timeline: sample 0 of a saved take is
the first sample of the section played.

What the round trip does and does not cover: it is USB out -> Input block -> DSP -> USB in. The
player's path is USB out -> DAC -> ear -> guitar -> ADC -> USB in. The two differ by the DAC/ADC
converters minus the DSP block latency, about a millisecond; ``extra_latency_samples`` adjusts it
if a loopback measurement ever says otherwise. The per-take dry-to-wet lag is measured and stored.

Song audio is read from the user's local file and written only under gitignored ``cache/``.
Maths here is offline and testable; the device stream is ``tonematch.reamp.duplex``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tonematch import reamp

SR = reamp.SR
SONG_TO = (1, 2)   # host USB out 1/2 -> Stomp outputs and headphones (not the Input block)
WET_FROM = (1, 2)  # host USB in 1/2 <- Path A (processed)
DRY_FROM = (5, 6)  # host USB in 5/6 <- dry Main L/R inputs (digital zero with nothing plugged in)
CLIP_LEVEL = 0.999           # |x| at or above this counts as clipped (about -0.01 dBFS)
QUIET_DBFS = -60.0           # RMS below this with some signal: probably noise, not playing
BLEED_CORR = 0.3             # |normalised correlation| with the song above this = the song leaked in
LEAD_S = 0.5                 # silence before the first click
ROOT = Path(__file__).resolve().parents[2]
USER_DI_DIR = ROOT / "cache" / "user_di"


def parse_span(text: str | None) -> tuple[float, float] | None:
    """'32.45:46.45' -> (32.45, 46.45)."""
    if not text:
        return None
    m = re.fullmatch(r"\s*([0-9.]+)\s*[:-]\s*([0-9.]+)\s*", text)
    if not m:
        raise ValueError(f"span must look like 32.45:46.45, got {text!r}")
    a, b = float(m.group(1)), float(m.group(2))
    if b <= a:
        raise ValueError(f"span end {b} is not after start {a}")
    return a, b


def db(x: float) -> float:
    return round(20 * np.log10(max(float(x), 1e-12)), 2)


# --------------------------------------------------------------------------- song and beat grid

@dataclass
class Section:
    audio: np.ndarray            # (n, 2) float32 at 48 kHz, gain applied
    song_offset_s: float         # song time of audio[0]
    source: str
    sections_json: str | None = None
    target: tuple[float, float] | None = None

    @property
    def n(self) -> int:
        return len(self.audio)


def load_section(path: str | Path, section: tuple[float, float] | None = None, song_offset_s: float = 0.0,
                 gain_db: float = 0.0) -> Section:
    """Decode the user's song like the separator does (tonematch.separation.audio.load_audio, so the
    timeline matches the stems) and cut `section` (song seconds). Without `section` the whole file
    is played and `song_offset_s` says where it starts in the song."""
    from tonematch.separation.audio import load_audio

    x = load_audio(path).T  # (n, 2)
    off = song_offset_s
    if section is not None:
        a, b = (round(t * SR) for t in section)
        if a >= len(x):
            raise ValueError(f"section starts at {section[0]} s, the file is {len(x) / SR:.2f} s")
        x, off = x[a:min(b, len(x))], song_offset_s + section[0]
    x = (x * np.float32(10 ** (gain_db / 20))).astype(np.float32)
    return Section(np.ascontiguousarray(x), off, str(path))


def find_sections_json(song: str | Path) -> Path | None:
    """The separator's beat grid for this song file, if it has been separated (cache/stems/<hash>/)."""
    from tonematch.separation.audio import file_hash

    d = ROOT / "cache" / "stems" / file_hash(song)
    hits = sorted(d.glob("*/sections.json")) if d.is_dir() else []
    return hits[0] if hits else None


@dataclass
class Grid:
    beat_s: float
    first_beat_s: float          # song time of the first beat at or after the section start
    source: str                  # "sections.json", "--bpm" or "default"
    bar_starts: tuple[float, ...] = ()  # song times of downbeats, when known (accented clicks)

    def accent(self, t: float, first: bool) -> bool:
        if not self.bar_starts:
            return first
        return any(abs(t - b) < 0.25 * self.beat_s for b in self.bar_starts)


def beat_grid(start_s: float, bpm: float | None = None, sections: dict | None = None) -> Grid:
    """Count-in clicks continue the song's own beat grid when one is known."""
    if bpm:
        return Grid(60.0 / bpm, start_s, "--bpm")
    beats = np.asarray((sections or {}).get("beat_times") or [], float)
    if len(beats) >= 2:
        beat = float(np.median(np.diff(beats)))
        later = beats[beats >= start_s - 1e-3]
        # past the last detected beat: extrapolate the grid
        first = float(later[0]) if len(later) else float(beats[-1] + beat * np.ceil((start_s - beats[-1]) / beat))
        bars = tuple(float(b) for b in (sections or {}).get("bar_starts") or [])
        return Grid(beat, first, "sections.json", bars)
    tempo = (sections or {}).get("tempo_bpm")
    if tempo:
        return Grid(60.0 / float(tempo), start_s, "sections.json tempo")
    return Grid(0.5, start_s, "default 120 bpm")


def click(accent: bool, peak_dbfs: float) -> np.ndarray:
    """A 25 ms decaying sine blip; an accented beat (a downbeat, else the first click) is an octave up."""
    t = np.arange(int(0.025 * SR)) / SR
    f = 2000.0 if accent else 1000.0
    y = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.006)
    return (y / np.max(np.abs(y)) * 10 ** (peak_dbfs / 20)).astype(np.float32)


@dataclass
class Playback:
    play: np.ndarray             # (m, 2) to SONG_TO
    countin_n: int               # index in `play` where the section starts
    click_at: list[int]          # indices of the clicks in `play`
    n_section: int
    tail_n: int
    grid: Grid | None = None


def build_playback(sec: Section | None, countin: int, grid: Grid | None, tail_s: float, click_dbfs: float = -12.0,
                   seconds: float | None = None) -> Playback:
    """[lead silence + count-in clicks on the song's grid][section][tail]. With no section, the
    'section' is `seconds` of silence (free playing after the count-in)."""
    if sec is None:
        body = np.zeros((round((seconds or 20.0) * SR), 2), np.float32)
        start_s = 0.0
    else:
        body, start_s = sec.audio, sec.song_offset_s
    grid = grid or Grid(0.5, start_s, "default 120 bpm")
    clicks_song_s = [grid.first_beat_s - m * grid.beat_s for m in range(countin, 0, -1)]
    pre_s = LEAD_S + (start_s - clicks_song_s[0] if clicks_song_s else 0.0)
    countin_n = round(pre_s * SR)
    tail_n = round(tail_s * SR)
    play = np.zeros((countin_n + len(body) + tail_n, 2), np.float32)
    play[countin_n:countin_n + len(body)] = body
    at = []
    for j, t in enumerate(clicks_song_s):
        i = countin_n + round((t - start_s) * SR)
        c = click(grid.accent(t, j == 0), click_dbfs)
        play[i:i + len(c)] += c[:, None]
        at.append(i)
    return Playback(play, countin_n, at, len(body), tail_n, grid)


# --------------------------------------------------------------------------- one take

@dataclass
class Take:
    dry: np.ndarray              # (n,) USB 5 = Main L / guitar
    dry_r: np.ndarray            # (n,) USB 6 = Main R
    wet: np.ndarray              # (n, 2) USB 1/2 = Path A
    offset_samples: int          # recording index of take sample 0 (countin_n + latency)
    meta: dict = field(default_factory=dict)


def split_take(rec: np.ndarray, pb: Playback, latency: int, extra_latency: int = 0) -> Take:
    """Cut the section (plus tail) out of the raw recording, compensated by the round trip."""
    if rec.ndim != 2 or rec.shape[1] < max(DRY_FROM):
        raise reamp.ReampError(f"need >= {max(DRY_FROM)} input channels, got {rec.shape}")
    start = pb.countin_n + latency + extra_latency
    n = pb.n_section + pb.tail_n
    seg = rec[start:start + n]
    if len(seg) < n:
        seg = np.concatenate([seg, np.zeros((n - len(seg), rec.shape[1]), np.float32)])
    seg = seg.astype(np.float32)
    return Take(dry=seg[:, DRY_FROM[0] - 1].copy(), dry_r=seg[:, DRY_FROM[1] - 1].copy(),
                wet=seg[:, [WET_FROM[0] - 1, WET_FROM[1] - 1]].copy(), offset_samples=start)


def _norm_xcorr(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Lag (b relative to a, samples) and normalised correlation of the best match within +-max_lag."""
    n = min(len(a), len(b))
    a64, b64 = np.asarray(a[:n], np.float64), np.asarray(b[:n], np.float64)
    den = float(np.sqrt(np.sum(a64**2) * np.sum(b64**2)))
    if den <= 0 or n == 0:
        return 0, 0.0
    m = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(b64, m) * np.conj(np.fft.rfft(a64, m)), m)
    lags = np.r_[np.arange(0, max_lag + 1), np.arange(-max_lag, 0)]
    vals = c[lags % m]
    k = int(np.argmax(np.abs(vals)))
    return int(lags[k]), round(float(vals[k] / den), 3)


def _levels(x: np.ndarray) -> dict:
    x = np.asarray(x, np.float32)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return {"rms_dbfs": round(reamp.rms_db(x), 2), "peak_dbfs": db(peak),
            "clipped_samples": int(np.sum(np.abs(x) >= CLIP_LEVEL)), "digital_zero": bool(peak == 0.0)}


def check_take(t: Take, pb: Playback, sec: Section | None, target: tuple[float, float] | None) -> tuple[dict, list[str]]:
    """Levels, clipping and silence checks; returns (report, warnings)."""
    warn: list[str] = []
    wet_m = reamp.to_mono(t.wet)
    rep: dict = {"dry": _levels(t.dry), "dry_right": _levels(t.dry_r), "wet": _levels(t.wet)}
    played = slice(0, pb.n_section)
    if target and sec is not None:
        a = max(0, round((target[0] - sec.song_offset_s) * SR))
        b = min(pb.n_section, round((target[1] - sec.song_offset_s) * SR))
        if b > a:
            rep["target_window_samples"] = [a, b]
            rep["dry_target"] = _levels(t.dry[a:b])
            rep["wet_target"] = _levels(t.wet[a:b])
    d = rep["dry"]
    if d["digital_zero"]:
        warn.append("dry input (USB 5) is digital zero: the guitar is not plugged in, or not into Main L/Mono "
                    "(USB 5/6 records the Stomp's Main L/R inputs)")
    elif d["rms_dbfs"] < QUIET_DBFS:
        warn.append(f"dry input is very quiet ({d['rms_dbfs']} dBFS RMS): noise only? check the cable and volume knob")
    if d["clipped_samples"]:
        warn.append(f"dry input clipped on {d['clipped_samples']} samples: turn the guitar volume down "
                    "(or raise the Stomp's input pad) and retake")
    if rep["wet"]["clipped_samples"]:
        warn.append(f"wet (USB 1/2) clipped on {rep['wet']['clipped_samples']} samples: lower the preset's output level")
    if not d["digital_zero"] and rep["wet"]["digital_zero"]:
        warn.append("wet (USB 1/2) is digital zero while the dry has signal: is the Input block on USB 5/6 "
                    "instead of Main L/R (Guitar)? This script does not change it")
    if not rep["dry_right"]["digital_zero"] and not d["digital_zero"] \
            and rep["dry_right"]["rms_dbfs"] > d["rms_dbfs"] - 6:
        warn.append("Main R (USB 6) carries signal as loud as Main L: is the guitar in the right jack? "
                    "the saved dry is Main L")
    if sec is not None and pb.n_section:
        song_m = reamp.to_mono(sec.audio)
        for name, x in (("dry", t.dry), ("wet", wet_m)):
            if not np.any(x[played]):
                continue
            lag, r = _norm_xcorr(song_m, x[played], max_lag=4 * SR // 100)
            rep[f"song_bleed_{name}"] = {"corr": r, "lag_samples": lag}
            if abs(r) > BLEED_CORR:
                warn.append(f"the song itself appears in the {name} recording (corr {r}, lag {lag}): "
                            "check Global Settings > Ins/Outs > USB In 1/2 Destination on the Stomp")
    if not d["digital_zero"] and not rep["wet"]["digital_zero"]:
        lag, r = _norm_xcorr(t.dry[played], wet_m[played], max_lag=SR // 100)
        rep["dry_to_wet"] = {"lag_samples": lag, "corr": r,
                             "note": "wet relative to dry; reliable only when corr is high (clean tones)"}
    return rep, warn


def next_take_index(out_dir: Path) -> int:
    ks = [int(m.group(1)) for p in out_dir.glob("take*_dry.wav") if (m := re.fullmatch(r"take(\d+)_dry", p.stem))]
    return max(ks, default=0) + 1


def save_take(out_dir: Path, k: int, t: Take, meta: dict) -> dict[str, Path]:
    import soundfile as sf

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"dry": out_dir / f"take{k}_dry.wav", "wet": out_dir / f"take{k}_wet.wav",
             "json": out_dir / f"take{k}.json"}
    for kind in ("dry", "wet"):
        if paths[kind].exists():
            raise FileExistsError(f"{paths[kind]} exists; takes are never overwritten")
    sf.write(paths["dry"], t.dry, SR, subtype="FLOAT")
    sf.write(paths["wet"], t.wet, SR, subtype="FLOAT")
    paths["json"].write_text(json.dumps(meta, indent=2, default=str))
    return paths

