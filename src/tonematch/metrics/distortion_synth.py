"""Synthetic distortion systems with known truth, for validating `distortion_id` (T171 step 1).

Each system maps a 48 kHz input to a 48 kHz output. The memoryless parts run at 8x oversampling
(scipy `resample_poly`) so aliasing does not pollute the truth. Wiener-Hammerstein (W-H) systems
expose their truth in the scale convention of `distortion_id`: P, Q normalised to 0 dB at 1 kHz
and all gain inside the static curve `f` (input-referred, dBFS peak at 1 kHz).

Circuit values are illustrative (taken from the classic schematics quoted in
docs/notes/distortion-circuits.md, chosen so the knees fall in the -60 to -18 dBFS range the
Native drives show); they are not models of any Helix block. No Line 6 data is used.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

SR = 48000
OS = 8
REF_HZ = 1000.0

# 1N4148-class Shockley parameters
IS, N_ID, VT = 2.52e-9, 1.752, 0.02585


# ---------------------------------------------------------------------------- building blocks

def analog_sos(b, a, sr: int = SR):
    bz, az = signal.bilinear(b, a, sr)
    return signal.tf2sos(bz, az)


def hp1(fc):
    w = 2 * np.pi * fc
    return [1.0, 0.0], [1.0, w]


def lp1(fc):
    w = 2 * np.pi * fc
    return [w], [1.0, w]


def lp2(fc, q=0.7071):
    w = 2 * np.pi * fc
    return [w * w], [1.0, w / q, w * w]


def cascade(*tfs):
    b, a = np.array([1.0]), np.array([1.0])
    for bb, aa in tfs:
        b, a = np.polymul(b, bb), np.polymul(a, aa)
    return b, a


def gain_tf(g):
    return [float(g)], [1.0]


def sos_resp(sos, f):
    if sos is None:
        return np.ones(np.size(f), complex)
    _, h = signal.sosfreqz(sos, worN=np.atleast_1d(np.asarray(f, float)), fs=SR)
    return h


def diode_curve(m_pos: float = 1, m_neg: float = 1, r_series: float = 2200.0, vmax: float = 6.0):
    """Series resistor into anti-parallel diode strings to ground (m diodes per side), solved
    exactly: x = y + R i(y). Returns a vectorised f(x) in volts."""
    y = np.linspace(-vmax, vmax, 200001)
    i = np.where(y >= 0, IS * np.expm1(np.minimum(y / (m_pos * N_ID * VT), 700)),
                 -IS * np.expm1(np.minimum(-y / (m_neg * N_ID * VT), 700)))
    x = y + r_series * i
    m = np.abs(x) < 1e6
    xs, ys = x[m], y[m]

    def f(u):
        return np.interp(u, xs, ys)
    return f


def os_apply(u: np.ndarray, fun) -> np.ndarray:
    up = signal.resample_poly(u, OS, 1)
    return signal.resample_poly(fun(up), 1, OS)


# ---------------------------------------------------------------------------- systems

@dataclass
class System:
    name: str
    description: str
    is_wh: bool
    process: callable
    # W-H truth (None for non-W-H systems; for near-W-H systems the nominal outer filters)
    P: callable | None = None          # complex response, |P(1k)| = 1
    Q: callable | None = None          # complex response, |Q(1k)| = 1
    f: callable | None = None          # static curve, input-referred
    tau_s: float | None = None          # bias time constant (memory systems)
    notes: list = field(default_factory=list)


def wh_system(name, description, pre_tf, curve, drive, post_tf, out_gain, is_wh=True,
              notes=None) -> System:
    pre = analog_sos(*pre_tf) if pre_tf is not None else None
    post = analog_sos(*post_tf) if post_tf is not None else None
    gp = abs(sos_resp(pre, [REF_HZ])[0])
    gq = abs(sos_resp(post, [REF_HZ])[0])
    pre_n = pre.copy() if pre is not None else None
    post_n = post.copy() if post is not None else None
    if pre_n is not None:
        pre_n[0, :3] /= gp
    if post_n is not None:
        post_n[0, :3] /= gq

    def fstatic(u):
        return out_gain * gq * curve(drive * gp * np.asarray(u))

    def proc(x):
        u = signal.sosfilt(pre_n, x) if pre_n is not None else x
        y = os_apply(u, fstatic)
        return signal.sosfilt(post_n, y) if post_n is not None else y

    return System(name, description, is_wh, proc,
                  P=lambda q: sos_resp(pre_n, q), Q=lambda q: sos_resp(post_n, q), f=fstatic,
                  notes=notes or [])


def rat_pre(rf: float = 25e3, gbw: float = 1e6):
    """RAT gain leg: G = 1 + Rf / Zg, Zg = (47 + 2.2u) || (560 + 4.7u), times the op-amp
    closed-loop pole GBW / G_hf (distortion-circuits.md 2.3)."""
    R1, C1, R2, C2 = 47.0, 2.2e-6, 560.0, 4.7e-6
    a1, a2 = R1 * C1, R2 * C2
    den = np.polymul([a1, 1.0], [a2, 1.0])
    num = np.polyadd(den, rf * np.array([a1 * C2 + a2 * C1, C1 + C2, 0.0]))
    g_hf = 1 + rf / (R1 * R2 / (R1 + R2))
    return cascade((num, den), lp1(gbw / g_hf))


def tone_stack(t: float = 0.5, f_lo: float = 723.0, f_hi: float = 1809.0):
    """Big Muff tone: (1-t) LP(f_lo) + t HP(f_hi) (distortion-circuits.md 2.6)."""
    w1, w2 = 2 * np.pi * f_lo, 2 * np.pi * f_hi
    num = np.polyadd((1 - t) * np.array([w1, w1 * w2]), t * np.array([1.0, w1, 0.0]))
    den = np.polymul([1.0, w1], [1.0, w2])
    return num, den


def bias_drift_system(tau: float = 0.05, charge: float = 400.0, thr: float = 0.25) -> System:
    """Capacitor-coupled clipper with grid-current-style bias shift ('blocking'): u = d x - b,
    y = asymmetric diode curve(u); while u > thr the coupling cap charges (db/dt = charge (u-thr)),
    and it discharges with time constant `tau`. Output-coupled (20 Hz high-pass), so DC is lost.
    Run at 48 kHz (sample loop)."""
    pre = analog_sos(*hp1(20.0))
    post = analog_sos(*cascade(hp1(20.0), lp1(8000.0)))
    curve = diode_curve(1, 2, 2200.0)
    drive = 15.0
    dt = 1.0 / SR
    k_dis = dt / tau

    def proc(x):
        v = drive * signal.sosfilt(pre, x)
        u = np.empty_like(v)
        b = 0.0
        for n in range(v.size):
            un = v[n] - b
            u[n] = un
            if un > thr:
                b += charge * (un - thr) * dt
            b -= b * k_dis
        return signal.sosfilt(post, curve(u))
    return System("bias_drift", "capacitor-coupled asymmetric clipper with bias shift "
                  f"(discharge tau {tau * 1e3:.0f} ms); output coupled", False, proc, tau_s=tau,
                  notes=["not W-H: the clipper has memory (bias follows the signal envelope)"])


def build_systems() -> list[System]:
    sym = diode_curve(1, 1, 2200.0)
    asym = diode_curve(1, 2, 2200.0)
    hard_d = diode_curve(1, 1, 1000.0)
    out = []
    out.append(wh_system("tanh", "tanh, no filters", None, np.tanh, 10.0, None, 0.5))
    out.append(wh_system("hard_clip", "hard clip at +-1 (x8), no filters", None,
                         lambda u: np.clip(u, -1.0, 1.0), 8.0, None, 0.5))
    out.append(wh_system("asym_diode", "Shockley clipper, 1 diode up / 2 down, no filters", None,
                         asym, 15.0, None, 1.0))
    ts_pre = cascade(([1.0 + 40.0, 2 * np.pi * 720.0], [1.0, 2 * np.pi * 720.0]), hp1(15.0))
    out.append(wh_system("ts_like", "Tube-Screamer-like: 1 + 40 HP(720 Hz) into a symmetric "
                         "diode clipper, LP 723 Hz and LP2 6 kHz after", ts_pre, sym, 1.2,
                         cascade(lp1(723.0), lp2(6000.0)), 1.0))
    for nm, fc in (("rat_open", 16000.0), ("rat_dark", 1500.0)):
        out.append(wh_system(nm, f"RAT-like: gain leg 60 Hz/1.5 kHz, Rf 25k, GBW pole, hard "
                             f"diode pair to ground, LP {fc / 1000:g} kHz after", rat_pre(), hard_d,
                             0.5, cascade(lp1(fc), hp1(20.0)), 1.0))

    # Big Muff: two clip stages with a band-limiting stage between (not strictly W-H)
    s1_pre = analog_sos(*cascade(hp1(100.0), gain_tf(30.0)))
    inter = analog_sos(*cascade(hp1(200.0), lp1(5000.0), gain_tf(20.0)))
    post = analog_sos(*tone_stack(0.5))
    gp = abs(sos_resp(s1_pre, [REF_HZ])[0])
    gq = abs(sos_resp(post, [REF_HZ])[0])
    s1_n, post_n = s1_pre.copy(), post.copy()
    s1_n[0, :3] /= gp
    post_n[0, :3] /= gq

    def muff(x, s1_n=s1_n, post_n=post_n):
        u = signal.sosfilt(s1_n, x)
        y1 = os_apply(u, lambda v: sym(2.3 * gp * v))
        y2 = os_apply(signal.sosfilt(inter, y1), sym)
        return signal.sosfilt(post_n, y2) * gq
    out.append(System("big_muff", "Big-Muff-like: HP 100 Hz x30 -> diode clip -> HP 200 Hz / "
                      "LP 5 kHz x20 -> diode clip -> mid-scoop tone stack (723/1809 Hz blend)",
                      False, muff, P=lambda q: sos_resp(s1_n, q), Q=lambda q: sos_resp(post_n, q),
                      notes=["two clippers with a filter between: W-H only approximately"]))
    out.append(bias_drift_system())

    # non-W-H control: clean path plus a band-limited dirty path (Klon-like blend)
    d_pre = analog_sos(*cascade(hp1(400.0), gain_tf(1.0)))
    d_post = analog_sos(*lp1(3000.0))

    def blend(x):
        dirty = signal.sosfilt(d_post, os_apply(signal.sosfilt(d_pre, x),
                                                lambda v: hard_d(20.0 * v)))
        return 0.5 * x + dirty
    out.append(System("klon_blend", "non-W-H control: 0.5 x clean + [HP 400 Hz -> hard diode "
                      "clip -> LP 3 kHz] (clean/dirty blend)", False, blend,
                      notes=["parallel linear and nonlinear paths: not W-H"]))
    return out
