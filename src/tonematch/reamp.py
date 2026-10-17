"""USB reamping through the HX Stomp (plan Step 4 Stomp mode, Step 7 reamp; skill `reamp-capture`).

Audio path (measured on HX Stomp firmware 3.50, macOS, 2026-10-14; see docs/notes/T003-stomp-control-spike.md):
host USB out 5/6 -> the Input block when it is set to "USB 5/6"; record USB in 1/2 (Path A).
USB in 3/4 is Path B, USB in 5/6 the dry Main L/R inputs. The Input block's USB 5/6 value is
**6** on the Stomp, not the Floor menu's 15 (which is accepted and silent).

Latency is measured once with an impulse through a transparent chain and stored in
``cache/stomp/latency.json`` with the firmware it was measured on; a capture refuses to run on a
different firmware. Level matching is a fixed calibration gain from a -18 dBFS 1 kHz tone through
the transparent chain (unity on 3.50), never per-clip normalisation.

The device I/O imports ``sounddevice`` lazily so this module (and its maths) imports anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

SR = 48_000
ROOT = Path(__file__).resolve().parents[2]
CAL_PATH = ROOT / "cache" / "stomp" / "latency.json"
NAME_HINT = "HX Stomp"
PLAY_TO = (5, 6)       # 1-based host output channels -> the Input block on USB 5/6
RECORD_FROM = (1, 2)   # 1-based host input channels <- Path A
STOMP_INPUT_USB56 = 6  # Input block "@input" value for USB 5/6 on the HX Stomp (fw 3.50, measured)
PRE_S = 1.0
TAIL_S = 2.0
AGREE_SAMPLES = 2
# PortAudio buffering. "low" gives a 1206-1207 sample round trip on macOS (vs 12498-12499 with the
# default "high"); every capture and the calibration must use the same setting.
PA_LATENCY = "low"
# Stream-start latency jitter: renders of one setting land -1..+2 samples from the first (T108,
# stomp-verify-v1, 2026-10-17; 0..1 before). Aligned within this, with margin.
MAX_JITTER = 4


class ReampError(RuntimeError):
    pass


# --------------------------------------------------------------------------- maths (offline)

def impulse(n: int, at: int, amp: float = 0.5) -> np.ndarray:
    x = np.zeros(n, np.float32)
    x[at] = amp
    return x


def tone(seconds: float, freq: float = 1000.0, dbfs_rms: float = -18.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = np.sin(2 * np.pi * freq * t)
    x *= 10 ** (dbfs_rms / 20) / np.sqrt(np.mean(x**2))
    fade = int(0.01 * SR)
    ramp = np.linspace(0, 1, fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    return x.astype(np.float32)


def onset(x: np.ndarray, rel_db: float = -20.0) -> int:
    """Index of the first sample within rel_db of the absolute peak (the impulse's arrival)."""
    a = np.abs(np.asarray(x, dtype=np.float64))
    peak = float(a.max()) if a.size else 0.0
    if peak <= 0:
        raise ReampError("no impulse in the capture (silent)")
    return int(np.argmax(a >= peak * 10 ** (rel_db / 20)))


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return 10 * np.log10(max(float(np.mean(x**2)), 1e-30)) if x.size else -300.0


def null_db(a: np.ndarray, b: np.ndarray) -> float:
    """Residual energy of a-b relative to a, in dB (no alignment)."""
    n = min(len(a), len(b))
    a64, b64 = np.asarray(a[:n], np.float64), np.asarray(b[:n], np.float64)
    return 10 * np.log10(max(float(np.sum((a64 - b64) ** 2)), 1e-30) / max(float(np.sum(a64**2)), 1e-30))


def align(y_ext: np.ndarray, ref: np.ndarray, n: int, max_lag: int = MAX_JITTER) -> tuple[np.ndarray, int]:
    """Pick the n-sample window of y_ext (length n + 2*max_lag, centred) best matching ref.

    Returns (window, offset) with offset in -max_lag..max_lag. Used to cancel the 0..1 sample
    stream-start jitter between repeated renders of the same clip on the same chain."""
    best, best_k = -np.inf, max_lag
    r = np.asarray(ref[:n], np.float64)
    for k in range(2 * max_lag + 1):
        w = np.asarray(y_ext[k:k + n], np.float64)
        if len(w) < n:
            break
        score = float(np.dot(w, r))
        if score > best:
            best, best_k = score, k
    return np.asarray(y_ext[best_k:best_k + n], np.float32), best_k - max_lag


def to_mono(rec: np.ndarray, mode: str = "mid") -> np.ndarray:
    rec = np.asarray(rec, np.float32)
    if rec.ndim == 1:
        return rec
    if mode == "left":
        return rec[:, 0].copy()
    return (0.5 * (rec[:, 0] + rec[:, 1])).astype(np.float32)


def trim(rec_mono: np.ndarray, pre: int, latency: int, n: int, tail: int) -> np.ndarray:
    start = pre + latency
    out = rec_mono[start:start + n + tail]
    if len(out) < n + tail:
        out = np.concatenate([out, np.zeros(n + tail - len(out), np.float32)])
    return out.astype(np.float32)


# Clip lengths (T061 item 2). Hardware renders take about clip length + 0.67 s, so search trials
# use a short excerpt and only finalists get the long clip. Env overrides for experiments.
SEARCH_CLIP_S = float(os.environ.get("TONEMATCH_STOMP_SEARCH_CLIP_S", "3.0"))
FINALIST_CLIP_S = float(os.environ.get("TONEMATCH_STOMP_FINALIST_CLIP_S", "10.0"))


def excerpt(x: np.ndarray, seconds: float, sr: int = SR, hop_s: float = 0.25) -> tuple[np.ndarray, int]:
    """The loudest `seconds`-long window of `x` (RMS, on a `hop_s` grid), with its start sample.
    The whole clip when it is shorter. Deterministic, so every engine renders the same excerpt."""
    x = np.asarray(x, np.float32)
    n = round(seconds * sr)
    if n <= 0 or len(x) <= n:
        return x.copy(), 0
    hop = max(1, int(hop_s * sr))
    c = np.concatenate([[0.0], np.cumsum(np.asarray(x, np.float64) ** 2)])
    starts = np.arange(0, len(x) - n + 1, hop)
    e = c[starts + n] - c[starts]
    i = int(starts[int(np.argmax(e))])
    return x[i:i + n].copy(), i


def clip_for(x: np.ndarray, stage: str = "search", sr: int = SR) -> tuple[np.ndarray, int]:
    """`stage` "search" -> SEARCH_CLIP_S excerpt; "finalist" -> FINALIST_CLIP_S excerpt."""
    if stage not in ("search", "finalist"):
        raise ValueError(f"stage must be search or finalist, got {stage!r}")
    return excerpt(x, SEARCH_CLIP_S if stage == "search" else FINALIST_CLIP_S, sr)


# --------------------------------------------------------------------------- calibration file

@dataclass
class Calibration:
    latency_samples: int
    measurements: list[int]
    level_gain_db: float          # applied to every capture
    tone_in_dbfs: float
    tone_out_dbfs: float
    firmware: str
    build: str = ""
    device: str = NAME_HINT
    sample_rate: int = SR
    input_usb56: int = STOMP_INPUT_USB56
    mono: str = "mid"
    pa_latency: str = "high"      # files written before this field existed used the default
    date: str = ""
    notes: dict = field(default_factory=dict)

    def save(self, path: Path = CAL_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, path: Path = CAL_PATH) -> Calibration:
        if not path.exists():
            raise ReampError(f"no calibration at {path}; run scripts/reamp_capture.py --calibrate")
        d = json.loads(path.read_text())
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def check_firmware(self, firmware: str | None) -> None:
        """Refuse a calibration from another firmware or another stream buffering setting."""
        if firmware and firmware != self.firmware:
            raise ReampError(f"calibration was measured on firmware {self.firmware}, the pedal reports "
                             f"{firmware}; re-run --calibrate")
        if self.pa_latency != PA_LATENCY:
            raise ReampError(f"calibration used stream latency {self.pa_latency!r}, captures use "
                             f"{PA_LATENCY!r}; re-run --calibrate")


# --------------------------------------------------------------------------- device I/O

_STREAMS = 0
_STREAMS_LOCK = threading.Lock()


@contextmanager
def _streaming():
    """Count a live audio stream, so the control side never resets USB under it (T059)."""
    global _STREAMS
    with _STREAMS_LOCK:
        _STREAMS += 1
    try:
        yield
    finally:
        with _STREAMS_LOCK:
            _STREAMS -= 1


def audio_active() -> bool:
    """True while any stream opened through this module is playing or recording."""
    with _STREAMS_LOCK:
        return _STREAMS > 0


def refresh_audio_devices() -> bool:
    """Re-scan audio devices after the HX Stomp re-enumerated (a USB reset). PortAudio keeps the
    device list it saw at start-up, so without this the next stream would use a stale index.
    No-op when sounddevice was never imported or a stream is live. Returns True if re-scanned."""
    sd = sys.modules.get("sounddevice")
    if sd is None or audio_active():
        return False
    sd._terminate()
    sd._initialize()
    return True


def find_device(name_hint: str = NAME_HINT) -> int:
    import sounddevice as sd

    for i, d in enumerate(sd.query_devices()):
        if name_hint.lower() in d["name"].lower() and d["max_input_channels"] >= max(RECORD_FROM) \
                and d["max_output_channels"] >= max(PLAY_TO):
            # Pin 48 kHz explicitly (Helix hardware may not report rates class-compliantly on Linux).
            sd.check_output_settings(device=i, channels=d["max_output_channels"], samplerate=SR, dtype="float32")
            sd.check_input_settings(device=i, channels=d["max_input_channels"], samplerate=SR, dtype="float32")
            return i
    raise ReampError(f"no {name_hint!r} audio device with >= {max(PLAY_TO)} outputs at {SR} Hz")


def duplex(play: np.ndarray, out_channels: tuple[int, ...], device: int | None = None) -> np.ndarray:
    """One full-duplex stream, the same settings as the calibration (48 kHz, ``PA_LATENCY``).

    ``play`` is (n, len(out_channels)); column c goes to 1-based host output out_channels[c].
    Returns every input channel, (n, n_in). Input sample i and output sample i share the
    stream clock, so ``cache/stomp/latency.json`` applies to anything recorded here."""
    import sounddevice as sd

    dev = find_device() if device is None else device
    info = sd.query_devices(dev)
    n_out, n_in = int(info["max_output_channels"]), int(info["max_input_channels"])
    play = np.asarray(play, np.float32).reshape(len(play), -1)
    if play.shape[1] != len(out_channels) or max(out_channels) > n_out:
        raise ReampError(f"cannot map {play.shape[1]} columns to outputs {out_channels} of {n_out}")
    buf = np.zeros((len(play), n_out), np.float32)
    for col, ch in enumerate(out_channels):
        buf[:, ch - 1] = play[:, col]
    with _streaming():
        try:
            rec = sd.playrec(buf, samplerate=SR, device=(dev, dev), dtype="float32", channels=None,
                             input_mapping=list(range(1, n_in + 1)), output_mapping=list(range(1, n_out + 1)),
                             latency=PA_LATENCY, blocking=False)
            # Never block without a limit (T095: with CoreAudio failing, a blocking playrec waited
            # forever for a stream that never ran). The stream must end within its own length
            # plus STREAM_GRACE_S.
            deadline = time.monotonic() + len(buf) / SR + STREAM_GRACE_S
            while _stream_active(sd):
                if time.monotonic() > deadline:
                    sd.stop()
                    raise ReampError(f"the audio stream did not finish within {len(buf) / SR + STREAM_GRACE_S:.1f} s "
                                     "(CoreAudio or the HX Stomp's audio interface is not running; replug the "
                                     "USB cable, or restart the Mac's audio service)")
                time.sleep(0.01)
            if hasattr(sd, "wait"):
                sd.wait()   # returns at once now; raises the stream's own error, if any
        except BaseException:
            sd.stop()  # Ctrl-C mid-take: silence the outputs before re-raising
            raise
    return np.asarray(rec, np.float32)


STREAM_GRACE_S = 10.0


def _stream_active(sd) -> bool:
    get = getattr(sd, "get_stream", None)
    if get is None:      # a stand-in without stream objects (tests): playrec already returned the take
        return False
    try:
        return bool(get().active)
    except RuntimeError:   # no stream any more: finished
        return False


def playrec(mono: np.ndarray, device: int | None = None, pre_s: float = PRE_S, tail_s: float = TAIL_S,
            out_pair: tuple[int, int] = PLAY_TO, in_pair: tuple[int, int] | None = RECORD_FROM
            ) -> tuple[np.ndarray, int]:
    """Play `mono` on both channels of out_pair with pre-roll and tail; return (stereo capture, pre).
    ``in_pair=None`` returns every input channel (diagnostics)."""
    pre, tail = int(pre_s * SR), int(tail_s * SR)
    sig = np.concatenate([np.zeros(pre, np.float32), np.asarray(mono, np.float32), np.zeros(tail, np.float32)])
    rec = duplex(np.stack([sig] * len(out_pair), axis=1), out_pair, device)
    if in_pair is None:
        return rec.astype(np.float32), pre
    return rec[:, [in_pair[0] - 1, in_pair[1] - 1]].astype(np.float32), pre


def measure_latency(device: int | None = None, repeats: int = 3) -> list[int]:
    n = int(0.5 * SR)
    out = []
    for _ in range(repeats):
        rec, pre = playrec(impulse(n, 0), device, pre_s=0.5, tail_s=0.25)
        out.append(onset(to_mono(rec)) - pre)
    return out


def measure_level(latency: int, device: int | None = None) -> tuple[float, float]:
    x = tone(1.0)
    rec, pre = playrec(x, device, pre_s=0.25, tail_s=0.25)
    y = trim(to_mono(rec), pre, latency, len(x), 0)
    core = slice(int(0.1 * SR), len(x) - int(0.1 * SR))
    return rms_db(x[core]), rms_db(y[core])


def capture(dry: np.ndarray, cal: Calibration, device: int | None = None,
            tail_s: float = TAIL_S) -> np.ndarray:
    """Reamp `dry` and return the latency-trimmed, level-matched wet signal, len(dry) + tail."""
    x = np.asarray(dry, np.float32)
    if x.ndim != 1:
        raise ReampError(f"dry must be mono (n,), got {x.shape}")
    cal.check_firmware(None)
    rec, pre = playrec(x, device, pre_s=PRE_S, tail_s=tail_s)
    y = trim(to_mono(rec, cal.mono), pre, cal.latency_samples, len(x), int(tail_s * SR))
    return (y * np.float32(10 ** (cal.level_gain_db / 20))).astype(np.float32)


def calibrate(firmware: str, build: str = "", device: int | None = None, repeats: int = 3) -> Calibration:
    """Needs a transparent chain on the pedal (all blocks bypassed) with Input on USB 5/6."""
    lat = measure_latency(device, repeats)
    if max(lat) - min(lat) > AGREE_SAMPLES:
        raise ReampError(f"latency measurements disagree by more than {AGREE_SAMPLES} samples: {lat}")
    latency = round(float(np.median(lat)))
    t_in, t_out = measure_level(latency, device)
    if t_out < -60:
        raise ReampError(f"tone came back at {t_out:.1f} dBFS: is the Input block on USB 5/6?")
    return Calibration(latency_samples=latency, measurements=lat, level_gain_db=round(t_in - t_out, 3),
                       tone_in_dbfs=round(t_in, 3), tone_out_dbfs=round(t_out, 3), firmware=firmware,
                       build=build, pa_latency=PA_LATENCY, date=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
