"""Known synthetic blocks (numpy/scipy) for validating the block-probe analysis. NOT Helix models.

Each block maps a mono float64 signal at 48 kHz to its output and carries `truth`: the parameters
the analysis must recover. `validate()` runs the probe through each one and returns the accuracy
table (measured vs true, tolerance, pass), used by tests/test_probe.py and
`scripts/block_probe.py validate`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sps

SR = 48_000
LATENCY = 37   # every synthetic block also delays by this many samples, to exercise alignment


@dataclass
class Block:
    name: str
    fn: Callable[[np.ndarray], np.ndarray]
    truth: dict = field(default_factory=dict)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = self.fn(np.asarray(x, dtype=np.float64))
        return np.concatenate([np.zeros(LATENCY), y])[: x.size].astype(np.float32)


# ------------------------------------------------------------------------------------------- EQ

def rbj_peak(f0: float, gain_db: float, q: float, sr: int = SR) -> np.ndarray:
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / (2 * q)
    b = [1 + al * A, -2 * np.cos(w), 1 - al * A]
    a = [1 + al / A, -2 * np.cos(w), 1 - al / A]
    return np.concatenate([np.array(b) / a[0], np.array(a) / a[0]])[None, :]


def rbj_highshelf(f0: float, gain_db: float, sr: int = SR) -> np.ndarray:
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / 2 * np.sqrt(2)          # shelf slope S = 1
    c = np.cos(w)
    b = [A * ((A + 1) + (A - 1) * c + 2 * np.sqrt(A) * al),
         -2 * A * ((A - 1) + (A + 1) * c),
         A * ((A + 1) + (A - 1) * c - 2 * np.sqrt(A) * al)]
    a = [(A + 1) - (A - 1) * c + 2 * np.sqrt(A) * al,
         2 * ((A - 1) - (A + 1) * c),
         (A + 1) - (A - 1) * c - 2 * np.sqrt(A) * al]
    return np.concatenate([np.array(b) / a[0], np.array(a) / a[0]])[None, :]


def eq_sos() -> np.ndarray:
    """High-pass 80 Hz (2nd-order Butterworth), peak +6 dB at 1 kHz Q 1, high shelf -4 dB at 4 kHz."""
    hp = sps.butter(2, 80, "highpass", fs=SR, output="sos")
    return np.concatenate([hp, rbj_peak(1000, 6.0, 1.0), rbj_highshelf(4000, -4.0)])


def eq_true_db(freqs) -> np.ndarray:
    _, H = sps.sosfreqz(eq_sos(), worN=np.asarray(freqs, float), fs=SR)
    return 20 * np.log10(np.abs(H))


# ------------------------------------------------------------------------------------ nonlinear

def tanh_clip(drive: float) -> Block:
    return Block(f"tanh_x{drive:g}", lambda x: np.tanh(drive * x),
                 {"small_signal_gain_db": 20 * np.log10(drive), "ceiling": 1.0, "symmetry": 1.0})


def hard_clip_asym(gain: float, pos: float, neg: float) -> Block:
    return Block(f"hardclip_g{gain:g}_+{pos:g}_-{neg:g}", lambda x: np.clip(gain * x, -neg, pos),
                 {"small_signal_gain_db": 20 * np.log10(gain), "pos_ceiling": pos,
                  "neg_ceiling": neg, "symmetry": pos / neg})


# ------------------------------------------------------------------------------------- dynamics

def _envelope(x: np.ndarray) -> np.ndarray:
    return np.abs(sps.hilbert(x))


def compressor(threshold_db: float, ratio: float, attack_ms: float, release_ms: float) -> Block:
    """Feed-forward, hard knee, ideal (Hilbert) level detector, gain smoothed in dB by a one-pole
    with separate attack and release, so the gain-reduction trajectory is exactly exponential."""
    aa = float(np.exp(-1.0 / (attack_ms * 1e-3 * SR)))
    ar = float(np.exp(-1.0 / (release_ms * 1e-3 * SR)))

    def fn(x: np.ndarray) -> np.ndarray:
        lvl = 20 * np.log10(np.maximum(_envelope(x), 1e-12))
        target = np.maximum(lvl - threshold_db, 0.0) * (1 - 1 / ratio)
        gr = np.empty_like(target)
        s = 0.0
        for i, tg in enumerate(target.tolist()):
            s = tg + (aa if tg > s else ar) * (s - tg)
            gr[i] = s
        return x * 10 ** (-gr / 20)

    return Block(f"comp_t{threshold_db:g}_r{ratio:g}_a{attack_ms:g}_r{release_ms:g}", fn,
                 {"knee_dbfs": threshold_db, "ratio": ratio, "attack_ms": attack_ms,
                  "release_ms": release_ms,
                  "level_dependent_gain_db": -max(0.0, -10.0 - threshold_db) * (1 - 1 / ratio)})


def gate(threshold_db: float, floor_db: float = -60.0) -> Block:
    def fn(x: np.ndarray) -> np.ndarray:
        env = _envelope(x)
        sm = sps.sosfiltfilt(sps.butter(2, 50, fs=SR, output="sos"), env)
        g = np.where(20 * np.log10(np.maximum(sm, 1e-12)) > threshold_db, 1.0, 10 ** (floor_db / 20))
        g = sps.sosfiltfilt(sps.butter(2, 200, fs=SR, output="sos"), g)
        return x * g

    return Block(f"gate_t{threshold_db:g}", fn, {"gate_threshold_dbfs": threshold_db})


# ------------------------------------------------------------------------------- time and mod

def comb(x: np.ndarray, D: int, g: float, lp_pole: float | None = None) -> np.ndarray:
    """v[n] = LP(x[n-D] + g v[n-D]), computed D samples at a time (lfilter with an order-D
    denominator is O(N D)). With lp_pole p the loop holds the one-pole (1-p)/(1-p z^-1)."""
    v = np.zeros(x.size)
    prev = np.zeros(D)
    zi = np.zeros(1)
    for k in range(D, x.size + D, D):
        src = x[k - D:k] + g * prev[: min(D, x.size - (k - D))]
        if lp_pole is not None:
            src, zi = sps.lfilter([1 - lp_pole], [1, -lp_pole], src, zi=zi)
        n = min(D, x.size - k)
        if n <= 0:
            break
        v[k:k + n] = src[:n]
        prev = v[k:k + D]
    return v


def allpass(x: np.ndarray, D: int, g: float) -> np.ndarray:
    """Schroeder allpass y[n] = -g x[n] + x[n-D] + g y[n-D], D samples at a time."""
    y = np.zeros(x.size)
    for k in range(0, x.size, D):
        n = min(D, x.size - k)
        xd = x[k - D:k - D + n] if k >= D else np.zeros(n)
        yd = y[k - D:k - D + n] if k >= D else np.zeros(n)
        y[k:k + n] = -g * x[k:k + n] + xd + g * yd
    return y


def feedback_delay(time_ms: float, feedback: float, mix: float) -> Block:
    D = round(time_ms * SR / 1000)

    def fn(x: np.ndarray) -> np.ndarray:
        return x + mix * comb(x, D, feedback)

    return Block(f"delay_{time_ms:g}ms_fb{feedback:g}_m{mix:g}", fn,
                 {"time_ms": D * 1000 / SR, "feedback": feedback,
                  "echo_level_db": 20 * np.log10(mix)})


def schroeder_reverb(rt60_s: float, mix: float) -> Block:
    """Four parallel feedback combs with gains set for `rt60_s`, then two allpasses; dry + wet."""
    combs = (1687, 1601, 2053, 2251)
    aps = ((347, 0.7), (113, 0.7))

    def fn(x: np.ndarray) -> np.ndarray:
        wet = sum(comb(x, d, 10 ** (-3 * d / (rt60_s * SR))) for d in combs) / len(combs)
        for d, g in aps:
            wet = allpass(wet, d, g)
        return x + mix * wet

    return Block(f"schroeder_rt{rt60_s:g}_m{mix:g}", fn, {"rt60_s": rt60_s})


def tremolo(rate_hz: float, depth: float) -> Block:
    def fn(x: np.ndarray) -> np.ndarray:
        t = np.arange(x.size) / SR
        return x * (1 - depth * (0.5 + 0.5 * np.sin(2 * np.pi * rate_hz * t)))

    return Block(f"tremolo_{rate_hz:g}Hz_d{depth:g}", fn,
                 {"rate_hz": rate_hz, "am_depth_db_pp": -20 * np.log10(1 - depth)})


def vibrato(rate_hz: float, depth_ms: float, base_ms: float = 5.0) -> Block:
    """Delay line modulated by base + depth * sin (linear interpolation), 100 % wet."""
    def fn(x: np.ndarray) -> np.ndarray:
        n = np.arange(x.size)
        d = (base_ms + depth_ms * np.sin(2 * np.pi * rate_hz * n / SR)) * SR / 1000
        pos = n - d
        i = np.floor(pos).astype(int)
        fr = pos - i
        xp = np.concatenate([x, [0.0]])
        ok = i >= 0
        y = np.zeros_like(x)
        y[ok] = (1 - fr[ok]) * xp[i[ok]] + fr[ok] * xp[i[ok] + 1]
        return y

    return Block(f"vibrato_{rate_hz:g}Hz_{depth_ms:g}ms", fn,
                 {"rate_hz": rate_hz, "delay_depth_ms_pp": 2 * depth_ms,
                  # the marker sees base + depth * sin(phase at the marker): base +- depth
                  "latency_samples": LATENCY + base_ms * SR / 1000,
                  "latency_tol": depth_ms * SR / 1000 + 2})


def lp_feedback_delay(time_ms: float, feedback: float, mix: float, lp_hz: float) -> Block:
    """Delay with a one-pole low-pass inside the feedback loop (analog-style smeared echoes).
    Each echo loses energy in the filter, so only the time is a known truth here."""
    D = round(time_ms * SR / 1000)
    p = float(np.exp(-2 * np.pi * lp_hz / SR))

    def fn(x: np.ndarray) -> np.ndarray:
        return x + mix * comb(x, D, feedback, lp_pole=p)

    return Block(f"delay_lp{lp_hz:g}_{time_ms:g}ms_fb{feedback:g}", fn,
                 {"time_ms": D * 1000 / SR, "feedback_max": feedback})


def with_noise(block: Block, noise_dbfs: float = -90.0, seed: int = 7) -> Block:
    """The same block with white noise added at its output (RMS `noise_dbfs`), like a hardware
    capture's noise floor."""
    rng = np.random.default_rng(seed)

    def fn(x: np.ndarray) -> np.ndarray:
        return block.fn(x) + 10 ** (noise_dbfs / 20) * rng.standard_normal(x.size)

    return Block(f"{block.name}+noise{noise_dbfs:g}", fn, dict(block.truth))


def amp_like(drive: float = 20.0) -> Block:
    """Pre-EQ (peak +4 dB at 700 Hz), tanh(drive x), cab-like post filter (2nd-order HP 90 Hz and
    LP 5 kHz). Known: the small-signal response, pre * drive * post."""
    pre = rbj_peak(700, 4.0, 0.8)
    post = np.concatenate([sps.butter(2, 90, "highpass", fs=SR, output="sos"),
                           sps.butter(2, 5000, "lowpass", fs=SR, output="sos")])

    def fn(x: np.ndarray) -> np.ndarray:
        return sps.sosfilt(post, np.tanh(drive * sps.sosfilt(pre, x)))

    def small_signal_db(freqs) -> np.ndarray:
        f = np.asarray(freqs, float)
        _, a = sps.sosfreqz(pre, worN=f, fs=SR)
        _, b = sps.sosfreqz(post, worN=f, fs=SR)
        return 20 * np.log10(np.abs(a * b) * drive)

    # The cab-like filters' group delay (about 3 samples around the 0.5-4 kHz marker) is part of
    # what the marker measures, so the latency tolerance is wider here.
    blk = Block(f"amp_like_x{drive:g}", fn, {"small_signal_gain_db_440": float(
        small_signal_db([440.0])[0]), "latency_tol": 5})
    blk.truth["small_signal_db"] = small_signal_db
    return blk


def identity() -> Block:
    return Block("identity", lambda x: x.copy(), {"latency_samples": LATENCY})


def eq_block() -> Block:
    sos = eq_sos()
    return Block("biquad_eq", lambda x: sps.sosfilt(sos, x),
                 {"peak_hz": 1000.0, "peak_gain_db": float(eq_true_db([1000.0])[0])})


def default_blocks() -> list[Block]:
    return [identity(), eq_block(), tanh_clip(10.0), hard_clip_asym(4.0, 0.3, 0.5),
            compressor(-30.0, 4.0, 10.0, 100.0), gate(-55.0), feedback_delay(300.0, 0.5, 0.6),
            schroeder_reverb(1.5, 0.5), tremolo(4.0, 0.5), vibrato(1.5, 1.0),
            lp_feedback_delay(450.0, 0.6, 0.5, 2500.0),
            with_noise(schroeder_reverb(2.5, 0.3), -90.0),
            with_noise(compressor(-24.0, 8.0, 3.0, 250.0), -90.0),
            amp_like(20.0)]


# ---------------------------------------------------------------------------------- validation

def _row(block: str, feature: str, true: float, meas, tol: float, rel: bool = False) -> dict:
    if meas is None:
        err, ok = None, False
    else:
        err = (meas - true) / true if rel else meas - true
        ok = abs(err) <= tol
    return {"block": block, "feature": feature, "true": true, "measured": meas,
            "error": err, "tol": tol, "rel": rel, "pass": ok}


def checks(block: Block, fp: dict) -> list[dict]:
    """Accuracy rows for one block's fingerprint against its truth."""
    n, t = block.name, block.truth
    lat_true = t.get("latency_samples", LATENCY)
    rows = [_row(n, "latency_samples", lat_true, fp["alignment"]["latency_samples"],
                 t.get("latency_tol", 2))]
    sc = fp["static_curve"]
    tc = fp["transfer_curve"]
    dyn, dl, rv, md = fp["dynamics"], fp["delay"], fp["reverb"], fp["modulation"]
    h440 = next(p for p in fp["harmonics"]["per_freq"] if p["freq_hz"] == 440.0)
    if n == "identity":
        mag = np.array(fp["linear"]["mag_db"])
        rows += [_row(n, "fr_max_abs_db", 0.0, float(np.max(np.abs(mag))), 0.1),
                 _row(n, "thd_max_pct", 0.0, float(np.max(h440["thd_pct"])), 0.01),
                 _row(n, "delay_detected", 0, int(dl["detected"]), 0),
                 _row(n, "mod_detected", 0, int(md["kind"] != "none"), 0),
                 _row(n, "level_dependent_gain_db", 0.0, dyn["level_dependent_gain_db"], 0.05)]
    elif n == "biquad_eq":
        f = np.array(fp["linear"]["freqs_hz"])
        sel = (f >= 40) & (f <= 16000)
        err = np.array(fp["linear"]["mag_db"][1])[sel] - eq_true_db(f[sel])
        rows.append(_row(n, "fr_max_abs_err_db_40Hz_16kHz", 0.0, float(np.max(np.abs(err))), 0.5))
        rows.append(_row(n, "gain_1k_db", t["peak_gain_db"], fp["linear"]["gain_1k_db"][1], 0.2))
        rows.append(_row(n, "thd_max_pct", 0.0, float(np.max(h440["thd_pct"])), 0.01))
    elif n.startswith("tanh"):
        rows += [_row(n, "small_signal_gain_db", t["small_signal_gain_db"],
                      h440["static"]["small_signal_gain_db"], 0.3),
                 _row(n, "pos_ceiling", 1.0, tc["pos_ceiling"], 0.05, True),
                 _row(n, "symmetry", 1.0, tc["symmetry"], 0.05),
                 _row(n, "odd_even_db_min_(>=40)", 40.0,
                      min(v for v in h440["odd_even_db"][-4:]), 1e9),
                 _row(n, "transfer_r2", 1.0, tc["r2"], 0.01)]
        rows[-2]["pass"] = rows[-2]["measured"] >= 40.0
        rows[-2]["error"], rows[-2]["tol"] = None, None
    elif n.startswith("hardclip"):
        rows += [_row(n, "small_signal_gain_db", t["small_signal_gain_db"],
                      h440["static"]["small_signal_gain_db"], 0.3),
                 _row(n, "pos_ceiling", t["pos_ceiling"], tc["pos_ceiling"], 0.05, True),
                 _row(n, "neg_ceiling", t["neg_ceiling"], tc["neg_ceiling"], 0.05, True),
                 _row(n, "symmetry", t["symmetry"], tc["symmetry"], 0.05)]
        top = h440["h_db"][-1]
        rows.append(_row(n, "H2_db_at_-3dBFS_(even_present)", 0.0, top[0], 1e9))
        rows[-1]["pass"] = top[0] is not None and top[0] > -40.0
        rows[-1]["true"], rows[-1]["error"], rows[-1]["tol"] = "> -40", None, None
    elif n.startswith("comp"):
        rows += [_row(n, "knee_dbfs", t["knee_dbfs"], sc["knee_dbfs"], 1.5),
                 _row(n, "ratio", t["ratio"], sc["ratio"], 0.1, True),
                 _row(n, "attack_ms", t["attack_ms"], dyn["attack_ms"], 0.15, True),
                 _row(n, "release_ms", t["release_ms"], dyn["release_ms"], 0.15, True),
                 _row(n, "level_dependent_gain_db", t["level_dependent_gain_db"],
                      dyn["level_dependent_gain_db"], 0.5)]
    elif n.startswith("gate"):
        rows.append(_row(n, "gate_threshold_dbfs", t["gate_threshold_dbfs"],
                         dyn["gate_threshold_dbfs"], 5.0))
    elif n.startswith("delay_lp"):
        rows += [_row(n, "time_ms", t["time_ms"], dl["time_ms"], 0.5),
                 _row(n, "feedback_<=_loop_gain", t["feedback_max"], dl["feedback"], 0.15)]
        rows[-1]["pass"] = dl["feedback"] is not None and 0 < dl["feedback"] <= t["feedback_max"]
        rows[-1]["error"], rows[-1]["tol"] = None, None
    elif n.startswith("amp_like"):
        f = np.array(fp["linear"]["freqs_hz"])
        sel = (f >= 60) & (f <= 12000)
        err = np.array(fp["linear"]["mag_db"][0])[sel] - t["small_signal_db"](f[sel])
        rows += [_row(n, "small_signal_gain_db_440", t["small_signal_gain_db_440"],
                      h440["static"]["small_signal_gain_db"], 0.3),
                 _row(n, "fr_at_-40dBFS_max_abs_err_db", 0.0, float(np.max(np.abs(err))), 0.5),
                 _row(n, "thd_rises_with_level", 1,
                      int(h440["thd_pct"][-1] > 10 * max(h440["thd_pct"][0], 1e-6)), 0),
                 _row(n, "gain_drop_at_-6_vs_-40_dB_(<-3)", -3.0,
                      fp["linear"]["gain_1k_db"][-1] - fp["linear"]["gain_1k_db"][0], 1e9)]
        rows[-1]["pass"] = rows[-1]["measured"] < -3.0
        rows[-1]["error"], rows[-1]["tol"] = None, None
    elif n.startswith("delay"):
        rows += [_row(n, "time_ms", t["time_ms"], dl["time_ms"], 0.1),
                 _row(n, "feedback", t["feedback"], dl["feedback"], 0.03),
                 _row(n, "echo_level_db", t["echo_level_db"], dl["echo_level_db"], 0.3),
                 _row(n, "mod_detected", 0, int(md["kind"] != "none"), 0)]
    elif n.startswith("schroeder"):
        rows += [_row(n, "rt60_s", t["rt60_s"], rv["rt60_s"], 0.1, True),
                 _row(n, "delay_detected", 0, int(dl["detected"]), 0),
                 _row(n, "mod_detected", 0, int(md["kind"] != "none"), 0)]
    elif n.startswith("tremolo"):
        rows += [_row(n, "rate_hz", t["rate_hz"], md["rate_hz"], 0.03, True),
                 _row(n, "am_depth_db_pp", t["am_depth_db_pp"], md["am_depth_db_pp"], 0.5)]
    elif n.startswith("vibrato"):
        rows += [_row(n, "rate_hz", t["rate_hz"], md["rate_hz"], 0.03, True),
                 _row(n, "delay_depth_ms_pp", t["delay_depth_ms_pp"], md["delay_depth_ms_pp"],
                      0.1, True)]
    return rows


def validate(probe=None, blocks: list[Block] | None = None) -> tuple[list[dict], dict]:
    """Run each known block over the probe, analyse, and check. Returns (rows, fingerprints)."""
    from .analysis import analyse
    from .signals import build_probe_set

    probe = probe or build_probe_set(di=False)
    x = probe.signal.astype(np.float64)
    rows, fps = [], {}
    for b in blocks or default_blocks():
        fp = analyse(b(x), probe, subject={"synthetic": b.name})
        fps[b.name] = fp
        rows += checks(b, fp)
    return rows, fps
