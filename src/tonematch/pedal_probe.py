"""Measured-pedal mode (T187): fingerprint a real drive pedal through the HX Stomp's FX loop.

PREPARATION ONLY. The human has not approved the mode; nothing here is adopted, and nothing here
has run on the pedal. Every hardware step lives in ``scripts/pedal_probe.py`` behind ``--human``.

Signal path (edit buffer only, never a flash write)::

    host USB out 5/6 -> Input block (USB 5/6) -> FX Loop 1 block (Send 0 dB, Return 0 dB, Mix 100 %)
        -> Send L jack -> the pedal -> Return L jack -> Path A output -> host USB in 1/2

The FX Loop block goes into a slot that already holds a single block on the current edit buffer
(04C "Smoke Known": the delay at device slot 3). ``stompd compile`` refuses Send/Return blocks
("not built in v0", checked offline 2026-10-18), so the template is loaded IN PLACE: the device's own
`model` command on that slot, then Send/Return/Mix, the other blocks bypassed, the Input routed to
USB 5/6 (``StompEngine(whole_preset="off")``, ``plan_load``). ``close()`` puts the buffer back.

Calibration (pure maths here, tested on fake audio):
- **L1 loopback** (a cable from Send L to Return L, no pedal): impulses give the round-trip latency;
  the dfp-v1 probe through the bare loop gives the loop's gain at 1 kHz, its small-signal response
  (the "interface tilt"), its noise floor and a linearity check. The gain and the response are
  divided out of every pedal capture (output side).
- **L2 input scale** (optional, a cable from Send L to the Stomp's guitar input): the level the
  guitar input's converter sees for the probe's dBFS. Helix Native's 0 dBFS is the guitar input's
  0 dBFS, so ``send_db = -L2 gain`` makes the pedal see the probe at the level a Native model sees it.
  This is the only step that fixes the INPUT level; the loopback alone cannot split Send from Return.
- **Noise take**: the pedal's output with the probe replaced by silence, analysed like a capture.
  It gives a per-entry noise floor for the fingerprint (hum included).

Identification: the capture's fingerprint (``distortion_probe.analyse``, layout "v1") against the
dfp-v1 knob maps (``distortion_step4``), level-free curves. Optional fixes measured in T187's
simulation: a software input-level search (``shift_level``), and noise-floor handling (``mask`` /
``floor``).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal

from .metrics import distortion_probe as P
from .metrics import distortion_step3 as S3
from .metrics import distortion_step4 as S4

SR = P.SR
ROOT = Path(__file__).resolve().parents[2]
FX_LOOP_MODEL = "HD2_FXLoopMono1"         # "FX Loop 1": Send L / Return L on the HX Stomp
LIB_DIRS = ("dfp-v1", "dfp-v1-tone-ends")
OUT_ROOT = ROOT / "cache" / "distortion-fp" / "pedals"
LOOP_ROOT = ROOT / "cache" / "pedal-probe"
FAMILIES = ROOT / "cache" / "drive-maps" / "drive-families-v1.json"
VERSION = "pedal-probe-v0"

# calibration signal: impulses before the probe
IMPULSE_N = 3
IMPULSE_GAP_S = 0.25
IMPULSE_AMP = 0.5
LEAD_S = 0.5
AGREE_SAMPLES = 2
LOOP_SEND_DB = -6.0                       # L1 runs at Send -6 dB: a unity loop would put the probe's 0 dBFS
                                          # sweeps exactly at the Return's full scale
FLAT_TOL_DB = 0.25                        # a loop response inside +-this is left alone
RESP_BAND = (63.0, 16000.0)
CLIP_DBFS = -0.1
LEVEL_GRID_DB = tuple(float(v) for v in np.arange(-15.0, 15.01, 1.5))
NOISE_MARGIN_DB = 6.0
ABS_KEYS = ("M1", "Mh", "stair1k_db", "stair220_db")   # arrays in absolute output dB


# =============================================================================== the template

def template_slot(chain: dict) -> int:
    """The device slot (1..8, Path A) that takes the FX Loop block: the first slot holding a single
    block (not an Amp+Cab pair). The in-place route can only change a block that exists."""
    blocks = sorted((s for s in chain.get("slots", []) if s.get("kind") == "Block" and 1 <= s["pos"] <= 8),
                    key=lambda s: s["pos"])
    for s in blocks:
        if s.get("paired") is None and s.get("paired_id") is None:
            return int(s["pos"])
    raise ValueError("the edit buffer has no single (non Amp+Cab) block on Path A to turn into the FX Loop; "
                     "select the scratch preset 04C (its delay is at slot 3)")


def template_preset(chain: dict, catalog: dict, base=None, send_db: float = 0.0, return_db: float = 0.0):
    """The routing preset: one FX Loop 1 block (Send/Return in dB, Mix 1.0, trails off) at
    ``template_slot(chain)``; every other block absent, so ``plan_load`` bypasses it."""
    from .engines import native_library as lib

    if FX_LOOP_MODEL not in catalog.get("models", {}):
        raise ValueError(f"{FX_LOOP_MODEL} is not in the catalog")
    pos = template_slot(chain)
    pr = lib.single_block(FX_LOOP_MODEL, catalog, base if base is not None else lib.base_preset())
    s = pr.slots[0]
    lo, hi = catalog["models"][FX_LOOP_MODEL]["params"]["Send"]["min"], catalog["models"][FX_LOOP_MODEL]["params"]["Send"]["max"]
    for v in (send_db, return_db):
        if not lo <= v <= hi:
            raise ValueError(f"Send/Return {v} dB outside {lo}..{hi}")
    s.params.update({"Send": float(send_db), "Return": float(return_db), "Mix": 1.0})
    s.extra.update({"@path": 0, "@trails": False})
    s.position = pos - 1
    pr.name = "PEDAL PROBE"
    return pr


def template_hlx(chain: dict, catalog: dict, input_value: int, **kw) -> dict:
    """The template as .hlx JSON with Path A's input on USB 5/6 (for the offline compile check)."""
    from .engines.stomp import reamp_hlx_dict

    return reamp_hlx_dict(template_preset(chain, catalog, **kw), input_value)


def routing_plan(chain: dict, catalog: dict, **kw) -> dict:
    """What an in-place load of the template does on ``chain`` (pure, no device I/O)."""
    from .engines.stomp import plan_load

    pr = template_preset(chain, catalog, **kw)
    plan = plan_load(pr, chain)
    pos = template_slot(chain)
    cur = next(s for s in chain["slots"] if s["pos"] == pos)
    return {"slot": pos, "replaces": cur.get("model_id"), "model_command": cur.get("model_id") != FX_LOOP_MODEL,
            "bypass": plan["bypass"], "params": plan["targets"][pos]["params"],
            "input_routing": "USB 5/6", "writes_flash": False}


# =============================================================================== signals

_LAYOUT = None


def probe() -> tuple[np.ndarray, dict]:
    """The dfp-v1 probe (layout "v1"): 115 s, mono, 48 kHz."""
    global _LAYOUT
    sig, lay = P.build_probe("v1")
    _LAYOUT = lay
    return sig, lay


def layout() -> dict:
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = P.build_probe("v1")[1]
    return _LAYOUT


def impulse_block() -> tuple[np.ndarray, list[int]]:
    """Lead-in, IMPULSE_N impulses IMPULSE_GAP_S apart, then silence to the probe; impulse positions."""
    gap = int(IMPULSE_GAP_S * SR)
    n = int(LEAD_S * SR) + IMPULSE_N * gap + gap
    x = np.zeros(n, np.float32)
    pos = [int(LEAD_S * SR) + i * gap for i in range(IMPULSE_N)]
    x[pos] = IMPULSE_AMP
    return x, pos


def calibration_signal() -> tuple[np.ndarray, dict]:
    """Impulses then the probe. meta: impulse positions and the probe's start sample."""
    imp, pos = impulse_block()
    sig, _ = probe()
    return np.concatenate([imp, sig.astype(np.float32)]), {"impulses": pos, "probe_start": int(imp.size),
                                                          "probe_n": int(sig.size)}


# =============================================================================== calibration maths

def impulse_latency(rec: np.ndarray, positions: list[int], search_s: float = 0.2, rel_db: float = -20.0) -> list[int]:
    """Arrival of each impulse in ``rec`` (mono), relative to where it was sent: first sample within
    ``rel_db`` of the local peak, searched ``search_s`` after each send position."""
    rec = np.asarray(rec, float)
    w = int(search_s * SR)
    out = []
    for p in positions:
        seg = np.abs(rec[p:p + w])
        if seg.size == 0 or seg.max() <= 0:
            raise ValueError("no impulse came back (silent loop: is the cable in Send L -> Return L?)")
        out.append(int(np.argmax(seg >= seg.max() * 10 ** (rel_db / 20))))
    return out


def xcorr_lag(y: np.ndarray, x: np.ndarray, max_lag: int) -> int:
    """Lag (samples, y later than x is positive) maximising the cross-correlation, FFT based."""
    n = 1 << int(np.ceil(np.log2(len(x) + len(y) + 1)))
    c = np.fft.irfft(np.fft.rfft(np.asarray(y, float), n) * np.conj(np.fft.rfft(np.asarray(x, float), n)), n)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([c[:max_lag + 1], c[-max_lag:]])
    return int(lags[int(np.argmax(vals))])


def probe_self_lag(y: np.ndarray, max_lag: int = 4800) -> int:
    """Residual lag of a trimmed capture against the probe, on its first (quiet, linear) sweeps.
    A QA check of the latency trim: a few samples is jitter; more means the trim is wrong."""
    sig, lay = probe()
    seg = [s for s in lay["segments"] if s["name"] == "sweep"][:4]
    a, b = seg[0]["start"], seg[-1]["start"] + seg[-1]["n"]
    return xcorr_lag(np.asarray(y, float)[a:b], sig[a:b], max_lag)


def trim(rec: np.ndarray, start: int, latency: int, n: int) -> np.ndarray:
    """``n`` samples of ``rec`` from ``start + latency`` (zero padded at the end)."""
    rec = np.asarray(rec, np.float64)
    a = start + latency
    out = rec[max(a, 0):a + n]
    if a < 0:
        out = np.concatenate([np.zeros(-a), out])
    if out.size < n:
        out = np.concatenate([out, np.zeros(n - out.size)])
    return out[:n]


@dataclass
class LoopCal:
    """The bare loop (Send -> cable -> Return), from ``loop_calibration``."""
    latency_samples: int
    measurements: list[int]
    gain_1k_db: float                    # loop small-signal gain at 1 kHz, normalised to Send 0 dB
    response_db: list[float]             # small-signal response on P.FGRID, re 1 kHz
    flatness_db: float                   # max |response| over RESP_BAND
    noise_dbfs_rms: float
    peak_dbfs: float
    max_harmonic_db: float               # worst H2..H9 re H1 on the 1 kHz staircase, -46 dBFS and up
    clipped: bool
    self_lag: int                        # the probe's residual lag after the trim (QA)
    input_scale_db: float | None = None  # L2: guitar input's dBFS for the probe's dBFS (None: not measured)
    loop_send_db: float = 0.0            # the FX Loop Send during L1 (digital gain, removed from gain_1k_db)
    firmware: str = ""
    date: str = ""
    notes: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems()

    def problems(self) -> list[str]:
        p = []
        if max(self.measurements) - min(self.measurements) > AGREE_SAMPLES:
            p.append(f"impulse latencies disagree: {self.measurements}")
        if self.clipped:
            p.append(f"the loop clipped (peak {self.peak_dbfs:.1f} dBFS): lower the FX Loop Send")
        if abs(self.gain_1k_db) > 1.0:
            p.append(f"loop gain {self.gain_1k_db:+.2f} dB at 1 kHz: Send/Return levels or the Instrument/Line "
                     "setting differ (it is divided out, but check the Global Settings)")
        if self.max_harmonic_db > -60.0:
            p.append(f"the bare loop distorts (worst harmonic {self.max_harmonic_db:.1f} dB): a pedal in the "
                     "cable path, a Line/Instrument mismatch or a clipping Return")
        if abs(self.self_lag) > AGREE_SAMPLES + 2:
            p.append(f"the latency trim leaves {self.self_lag} samples of lag on the probe")
        return p

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1))
        return path

    @classmethod
    def load(cls, path: Path) -> LoopCal:
        d = json.loads(Path(path).read_text())
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def fingerprint(y: np.ndarray) -> tuple[dict, dict]:
    """dfp-v1 fingerprint of one aligned probe capture (the step-3 analyser, layout "v1")."""
    arr, sc = P.analyse(np.asarray(y, float), layout())
    for k in S3.DROPPED_SCALARS:
        sc.pop(k, None)
    return arr, sc


def loop_from_fingerprint(arr: dict, sc: dict) -> dict:
    """Gain, response, linearity of a bare-loop probe fingerprint."""
    f = P.FGRID
    ss = np.asarray(arr["ss_db"], float)
    g = float(sc["g0_ss_1k_db"])
    resp = ss - g
    band = (f >= RESP_BAND[0]) & (f <= RESP_BAND[1])
    st = np.asarray(arr["stair1k_db"], float)
    L = np.asarray(P.STAIR_LEVELS_V1)
    hi = L >= -46.0
    rel = st[1:, hi] - st[0, hi][None]
    return {"gain_1k_db": g, "response_db": [float(v) for v in resp],
            "flatness_db": float(np.nanmax(np.abs(resp[band]))),
            "max_harmonic_db": float(np.nanmax(rel)), "noise_dbfs_rms": float(sc["noise_floor_dbfs_rms"]),
            "peak_dbfs": float(sc["peak_dbfs"])}


def loop_calibration(rec: np.ndarray, meta: dict, firmware: str = "", notes: dict | None = None,
                     send_db: float = 0.0) -> tuple[LoopCal, dict, dict]:
    """``rec``: the mono capture of ``calibration_signal()`` through the bare loop, recorded from the
    first sample sent (no pre-roll; pass the pre-roll in ``meta["pre"]`` otherwise), with the FX Loop
    Send at ``send_db`` (LOOP_SEND_DB in the tool). Returns (LoopCal, loop fingerprint arrays, scalars)."""
    pre = int(meta.get("pre", 0))
    rec = np.asarray(rec, float)
    meas = impulse_latency(rec[pre:], meta["impulses"])
    lat = round(float(np.median(meas)))
    y = trim(rec, pre + meta["probe_start"], lat, meta["probe_n"])
    arr, sc = fingerprint(y)
    lp = loop_from_fingerprint(arr, sc)
    lp["gain_1k_db"] -= float(send_db)
    cal = LoopCal(latency_samples=lat, measurements=meas, clipped=bool(lp["peak_dbfs"] >= CLIP_DBFS), loop_send_db=float(send_db),
                  self_lag=probe_self_lag(y), firmware=firmware,
                  date=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), notes=notes or {}, **lp)
    return cal, arr, sc


def input_scale_db(rec_guitar_in: np.ndarray, meta: dict, latency: int) -> float:
    """L2: gain (dB) from the probe's dBFS to the guitar input's dBFS (dry monitor, host USB in 5/6),
    on the -18 dBFS 1 kHz step of the staircase (well inside both converters' range)."""
    pre = int(meta.get("pre", 0))
    y = trim(rec_guitar_in, pre + meta["probe_start"], latency, meta["probe_n"])
    sig, lay = probe()
    s = next(s for s in lay["segments"] if s["name"] == "stair" and s["f0"] == 1000.0)
    L = P.layout_stair_levels(lay)
    i = int(np.argmin(np.abs(np.asarray(L) + 16.0)))
    a = s["start"] + i * s["step_n"] + int(0.1 * SR)
    b = s["start"] + (i + 1) * s["step_n"] - int(0.02 * SR)
    from .metrics import distortion_id as D
    out = abs(D.sine_harmonics(y[a:b], 1000.0, SR, 1)[1])
    inn = abs(D.sine_harmonics(sig[a:b], 1000.0, SR, 1)[1])
    return float(20 * np.log10(out / inn))


def send_db_for(cal: LoopCal) -> float:
    """The FX Loop Send that puts the pedal input on Native's scale (0 when L2 was not measured)."""
    return 0.0 if cal.input_scale_db is None else float(np.clip(-cal.input_scale_db, -60.0, 6.0))


# =============================================================================== corrections

def zero_phase_gain(y: np.ndarray, gain_db_of_f) -> np.ndarray:
    """Apply a real, zero-phase gain G(f) (dB, a callable of frequency in Hz) by FFT."""
    y = np.asarray(y, float)
    n = 1 << int(np.ceil(np.log2(y.size + 1)))
    Y = np.fft.rfft(y, n)
    f = np.fft.rfftfreq(n, 1 / SR)
    return np.fft.irfft(Y * 10 ** (gain_db_of_f(f) / 20), n)[:y.size]


def response_fn(response_db, band=RESP_BAND):
    """G(f) from a response on P.FGRID: log-f interpolation inside ``band``, flat beyond it."""
    fg = np.asarray(P.FGRID, float)
    r = np.asarray(response_db, float)
    m = (fg >= band[0]) & (fg <= band[1]) & np.isfinite(r)
    lf, rv = np.log(fg[m]), r[m]

    def g(f):
        return np.interp(np.log(np.clip(f, fg[m][0], fg[m][-1])), lf, rv)
    return g


def correct_capture(y: np.ndarray, cal: LoopCal, response: bool = True) -> tuple[np.ndarray, dict]:
    """Divide the bare loop out of an aligned capture: its 1 kHz gain at Send 0 dB, and (``response``)
    its small-signal response when it is not flat within FLAT_TOL_DB. Both belong to the output side
    under this correction; an input-side tilt is not removed (see the T187 simulation). A capture at
    another Send keeps that Send's gain in its output level, which the level-free curves ignore."""
    y = np.asarray(y, float) * 10 ** (-cal.gain_1k_db / 20)
    done = {"gain_db": -cal.gain_1k_db, "response": False}
    if response and cal.flatness_db > FLAT_TOL_DB:
        g = response_fn(cal.response_db)
        y = zero_phase_gain(y, lambda f: -g(f))
        done["response"] = True
    return y, done


def average(takes: list[np.ndarray]) -> np.ndarray:
    """Coherent average of repeated aligned captures (noise falls 3 dB per doubling)."""
    n = min(len(t) for t in takes)
    return np.mean(np.stack([np.asarray(t, float)[:n] for t in takes]), axis=0)


def capture_quality(y: np.ndarray, noise_dbfs: float | None = None) -> dict:
    y = np.asarray(y, float)
    pk = float(20 * np.log10(np.max(np.abs(y)) + 1e-30))
    return {"peak_dbfs": pk, "clipped": pk >= CLIP_DBFS, "noise_dbfs_rms": noise_dbfs,
            "self_lag": probe_self_lag(y)}


# =============================================================================== nuisances (simulation)

def tilt(x: np.ndarray, db_per_decade: float, f_ref: float = 1000.0) -> np.ndarray:
    """A converter-like tilt: +``db_per_decade`` per decade above ``f_ref`` (zero phase, flat below
    20 Hz and above 20 kHz)."""
    return zero_phase_gain(x, lambda f: db_per_decade * np.log10(np.clip(f, 20.0, 20000.0) / f_ref))


def pickup_load(x: np.ndarray, r_load: float, r_src: float = 6500.0, l_src: float = 2.5) -> np.ndarray:
    """The treble loss of a pickup-like source (``r_src`` ohm in series with ``l_src`` henry) into a
    pedal input of ``r_load`` ohm, normalised to 0 dB at DC: a first-order low-pass at
    (r_src + r_load) / (2 pi L). 10 kOhm: 1.05 kHz; 47 kOhm: 3.4 kHz; 220 kOhm: 14.4 kHz; 1 MOhm: 64 kHz.
    Causal (bilinear), like the real loading."""
    fc = (r_src + r_load) / (2 * np.pi * l_src)
    fc = min(0.49 * SR, fc)
    b, a = signal.butter(1, fc, "low", fs=SR)
    return signal.lfilter(b, a, np.asarray(x, float))


def pickup_corner_hz(r_load: float, r_src: float = 6500.0, l_src: float = 2.5) -> float:
    return float((r_src + r_load) / (2 * np.pi * l_src))


def add_noise(y: np.ndarray, dbfs_rms: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.asarray(y, float) + 10 ** (dbfs_rms / 20) * rng.standard_normal(len(y))


def delay(y: np.ndarray, n: int) -> np.ndarray:
    """Delay by ``n`` samples (negative: early), same length."""
    y = np.asarray(y, float)
    if n == 0:
        return y.copy()
    if n > 0:
        return np.concatenate([np.zeros(n), y[:-n]])
    return np.concatenate([y[-n:], np.zeros(-n)])


def gain(y: np.ndarray, db: float, clip: bool = False) -> np.ndarray:
    out = np.asarray(y, float) * 10 ** (db / 20)
    return np.clip(out, -1.0, 1.0) if clip else out


# =============================================================================== fingerprint fixes

def _interp_levels(V: np.ndarray, levels, new_levels) -> np.ndarray:
    """Linear interpolation of V along its last axis (indexed by ``levels`` dB) at ``new_levels``;
    NaN outside the measured range."""
    L = np.asarray(levels, float)
    q = np.asarray(new_levels, float)
    V = np.asarray(V, float)
    out = np.full(V.shape[:-1] + (q.size,), np.nan)
    for j, x in enumerate(q):
        if x < L[0] - 1e-9 or x > L[-1] + 1e-9:
            continue
        i = int(np.clip(np.searchsorted(L, x) - 1, 0, L.size - 2))
        t = (x - L[i]) / (L[i + 1] - L[i])
        out[..., j] = (1 - t) * V[..., i] + t * V[..., i + 1]
    return out


def shift_level(arr: dict, sc: dict, delta_db: float) -> tuple[dict, dict]:
    """Re-express a fingerprint measured with the pedal seeing the probe ``delta_db`` LOUDER than
    nominal on the nominal level axis: every level-indexed table is read at (level - delta), values
    outside the measured range are NaN (masked), the small-signal gain loses delta. delta 0 is the
    identity. Used by the input-level search (the input scale is unknown without L2)."""
    if delta_db == 0:
        return arr, sc
    a = {k: np.asarray(v, float).copy() for k, v in arr.items()}
    s = dict(sc)
    d = float(delta_db)
    sw = np.asarray(P.SWEEP_LEVELS, float)
    st = np.asarray(P.STAIR_LEVELS_V1, float)
    hl = np.asarray(P.HARM_LEVELS, float)
    tl = np.asarray(P.TWO_TONE_LEVELS, float)
    M1 = np.asarray(arr["M1"], float)
    hidx = [list(P.SWEEP_LEVELS).index(v) for v in P.HARM_LEVELS]
    rel = np.asarray(arr["Mh"], float) - M1[:, hidx][None]
    a["M1"] = _interp_levels(M1, sw, sw - d)
    a["Mh"] = _interp_levels(rel, hl, hl - d) + a["M1"][:, hidx][None]
    for k in ("stair1k_db", "stair220_db"):
        a[k] = _interp_levels(arr[k], st, st - d)
    a["imd"] = np.moveaxis(_interp_levels(np.moveaxis(np.asarray(arr["imd"], float), 1, -1), tl, tl - d), -1, 1)
    i1k = int(np.argmin(np.abs(np.log(P.FGRID / 1000.0))))
    a["sat_db"] = a["M1"][:, -1] - a["M1"][i1k, -1]
    # The small-signal response and gain are re-read from the quietest level the shifted table still
    # has, so they stay consistent with it: the level-free curves subtract the gain from every
    # absolute level, and a linear region must cancel exactly (dimensions with almost no spread over
    # the library would otherwise turn a fraction of a dB into a large distance).
    j = int(np.argmax(np.isfinite(a["M1"][i1k])))
    a["ss_db"] = a["M1"][:, j] - sw[j]
    s["g0_ss_1k_db"] = float(a["M1"][i1k, j] - sw[j])
    return a, s


def noise_mask(arr: dict, noise_arr: dict, margin_db: float = NOISE_MARGIN_DB) -> dict:
    """Mask (NaN) every absolute-level entry of a capture's fingerprint that is within ``margin_db``
    of the same entry in the noise take's fingerprint."""
    a = {k: np.asarray(v, float).copy() for k, v in arr.items()}
    for k in ABS_KEYS:
        nz = np.asarray(noise_arr[k], float)
        a[k][~(a[k] >= nz + margin_db)] = np.nan
    return a


def noise_floor_arrays(arr: dict, floor_abs: dict, offset_db: float = 0.0) -> dict:
    """Raise every absolute-level entry to at least ``floor_abs + offset_db`` (the "floor" fix:
    applied to the capture and, shifted by the gain difference, to every library entry)."""
    a = {k: np.asarray(v, float).copy() for k, v in arr.items()}
    for k in ABS_KEYS:
        a[k] = np.fmax(a[k], np.asarray(floor_abs[k], float) + offset_db)
    return a


# =============================================================================== the library

class Library:
    """The dfp-v1 knob maps (``distortion_step4``) over the level-free curves.

    ``drop``: models left out (leave-one-out, "a pedal not in the library"). ``floor``: a noise
    take's absolute arrays and the capture's small-signal gain (floor fix): every library entry is
    raised to the noise floor moved to its own gain."""

    def __init__(self, root: Path = ROOT, dirs=LIB_DIRS, drop=(), exclude=S4.EXCLUDED, floor: tuple | None = None,
                 recs: list | None = None):
        self.recs_all = recs if recs is not None else S4.load_library(root, dirs, exclude=tuple(exclude))
        recs = [r for r in self.recs_all if r["model"] not in set(drop)]
        if floor is not None:
            fz, g0q = floor
            recs = [dict(r, arr=noise_floor_arrays(r["arr"], fz, float(r["sc"]["g0_ss_1k_db"]) - g0q)) for r in recs]
        self.recs = recs
        self.fs = S4.FeatureSpace([S4.feature_blocks(r["arr"], r["sc"], "curves") for r in recs])
        self.maps, self.raw = S4.build_maps(recs, self.fs)
        self.knobs = {r["model"]: (r["drive"], r["tone"]) for r in recs}
        self.models = sorted(self.maps)

    def query(self, arr: dict, sc: dict):
        return self.fs.transform(S4.feature_blocks(arr, sc, "curves"))

    def coarse(self, z, w) -> float:
        """Distance to the nearest measured setting of any model (cheap, for the level search)."""
        return min(S4.nearest_setting(Zm, pos, z, w)[0] for pos, Zm in self.raw.values())

    def rank(self, arr: dict, sc: dict) -> list[tuple[str, float, np.ndarray]]:
        z, w = self.query(arr, sc)
        return S4.rank_models(self.maps, z, w)

    def knob_for(self, model: str, arr: dict, sc: dict) -> np.ndarray:
        z, w = self.query(arr, sc)
        return self.maps[model].best(z, w)[1]

    def identify(self, arr: dict, sc: dict, level_search: bool = False, grid=LEVEL_GRID_DB, refine: int = 3) -> dict:
        """Ranking (model, distance, knob positions) and the input-level estimate. With
        ``level_search`` the capture is re-read at every offset of ``grid`` (``shift_level``), the
        ``refine`` best by the coarse distance are ranked in full, and the best is kept."""
        if not level_search:
            return {"delta_db": 0.0, "ranking": self.rank(arr, sc), "arr": arr, "sc": sc}
        sc_list = []
        for d in grid:
            a2, s2 = shift_level(arr, sc, d)
            sc_list.append((self.coarse(*self.query(a2, s2)), d, a2, s2))
        sc_list.sort(key=lambda r: r[0])
        best = None
        for _, d, a2, s2 in sc_list[:refine]:
            rk = self.rank(a2, s2)
            if best is None or rk[0][1] < best["ranking"][0][1]:
                best = {"delta_db": float(d), "ranking": rk, "arr": a2, "sc": s2}
        return best

    def knobs_named(self, model: str, pos: np.ndarray) -> dict[str, float]:
        dk, tk = self.knobs[model]
        out = {dk: float(pos[0])}
        if tk is not None and len(pos) > 1:
            out[tk] = float(pos[1])
        return out


def families(path: Path = FAMILIES) -> dict[str, str]:
    """model -> family label (members joined), from the T180 map; {} when missing."""
    from .search.shortlist import load_families

    fam = load_families(path)
    return {m: "|".join(sorted(v)) for m, v in fam.items()}


# =============================================================================== output (dfp-v1 format)

def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s.strip())[:80] or "pedal"


def write_fingerprint(pedal: str, setting: str, arr: dict, sc: dict, meta: dict, root: Path = OUT_ROOT) -> dict:
    """Write one measured setting as a dfp-v1 fingerprint (``distortion_probe.pack``: float16 arrays,
    scalars as JSON, no audio) at ``root/<pedal>/<setting>.npz``, and add it to ``root/<pedal>/index.json``
    in the dfp-v1 index shape. The entry carries ``tag`` "measured-pedal" so ``load_library`` never
    mixes it into the Helix library; ``cc`` is empty (a real pedal's knobs are clock positions in
    ``knobs``)."""
    d = Path(root) / slug(pedal)
    d.mkdir(parents=True, exist_ok=True)
    sc = dict(sc)
    sc["measured_pedal"] = {"pedal": pedal, "setting": setting, **{k: v for k, v in meta.items() if k != "knobs"}}
    b = P.pack(arr, sc)
    f = d / f"{slug(setting)}.npz"
    f.write_bytes(b)
    idxp = d / "index.json"
    idx = json.loads(idxp.read_text()) if idxp.is_file() else {"version": "dfp-v1", "kind": "measured-pedal",
                                                                 "probe": VERSION, "settings": []}
    try:
        rel = str(f.relative_to(ROOT))
    except ValueError:
        rel = str(f)
    ent = {"model": f"PEDAL:{pedal}", "setting": setting, "cc": {}, "kind": "pedal", "tag": "measured-pedal",
           "knobs": meta.get("knobs", {}), "file": rel, "bytes": len(b),
           "loopback": meta.get("loopback"), "corrections": meta.get("corrections"),
           "quality": meta.get("quality"), "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    idx["settings"] = [s for s in idx["settings"] if s["setting"] != setting] + [ent]
    idxp.write_text(json.dumps(idx, indent=1))
    return ent


def read_fingerprint(path: Path) -> tuple[dict, dict]:
    return P.unpack(Path(path).read_bytes())
