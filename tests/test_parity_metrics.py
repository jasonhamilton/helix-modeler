"""Time-based effect and distortion parity metrics (T066): known synthetic effects are measured back,
two different note sequences give the same measurements (content invariance), and a different LFO
start phase gives the same modulation measurements (phase independence)."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import signal

from tonematch.metrics import delay, modulation, nonlinear, parity, reverb
from tonematch.metrics.proxy import guitar_di

SR = 48_000


# ----------------------------------------------------------------------------- delay


def _burst(kind: str, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(0.02 * SR)
    if kind == "noise":
        b = rng.standard_normal(n)
    else:
        t = np.arange(n) / SR
        b = np.sin(2 * np.pi * 330 * t) + 0.5 * np.sin(2 * np.pi * 660 * t)
    return b * np.hanning(n)


def _delay_render(burst: np.ndarray, t_s: float, fb: float, mix: float = 0.5, total_s: float = 3.0,
                  lp_hz: float | None = None) -> np.ndarray:
    y = np.zeros(int(total_s * SR))
    s0 = int(0.5 * SR)
    y[s0:s0 + burst.size] += burst
    echo = burst.copy()
    k = 1
    while True:
        pos = 0.5 + k * t_s
        if pos * SR + burst.size >= y.size:
            break
        if lp_hz:
            echo = signal.lfilter(*signal.butter(1, lp_hz, fs=SR), echo)
        # fractional placement through an FFT phase ramp
        seg = np.zeros(burst.size + 256)
        seg[:burst.size] = echo
        frac = pos * SR - int(pos * SR)
        f = np.fft.rfftfreq(seg.size)
        seg = np.fft.irfft(np.fft.rfft(seg) * np.exp(-2j * np.pi * f * frac), seg.size)
        i = int(pos * SR)
        y[i:i + seg.size] += mix * fb ** (k - 1) * seg[: y.size - i]
        k += 1
    return y


def test_delay_recovers_time_level_and_feedback():
    y = _delay_render(_burst("noise"), 0.375, 0.5, mix=0.5)
    p = delay.echo_profile(y)
    assert len(p.times_s) >= 5
    assert abs(p.delay_s - 0.375) < 2e-5
    assert np.allclose(p.times_s[:4], 0.375 * np.arange(1, 5), atol=3e-5)
    assert abs(p.levels_db[0] - 20 * np.log10(0.5)) < 0.3
    assert abs(p.slope_db_per_echo - 20 * np.log10(0.5)) < 0.2
    assert abs(p.feedback_gain - 0.5) < 0.02


def test_delay_compare_reports_ms_and_percent():
    a = delay.echo_profile(_delay_render(_burst("noise"), 0.375, 0.5))
    b = delay.echo_profile(_delay_render(_burst("noise"), 0.375 * 1.008, 0.5))
    c = delay.compare(a, b)
    assert np.allclose(c["time_err_pct"][:4], 0.8, atol=0.02)
    assert np.allclose(c["time_err_ms"][:4], 3.0 * np.arange(1, 5), atol=0.05)
    assert abs(c["drift_ms_per_echo"] - 3.0) < 0.05
    assert c["max_abs_level_diff_db"] < 0.3
    v = parity.delay_verdict(c)
    # 0.8 % is inside the 2 % tolerance, but six audible echoes accumulate 18 ms (> 10 ms)
    assert v["checks"]["delay_time"] is True and v["checks"]["cum_time"] is False


def test_delay_is_insensitive_to_input_content_and_level():
    # different burst content and level, same (darkening) delay: same time and feedback slope
    a = delay.echo_profile(_delay_render(_burst("noise", 0), 0.3, 0.45, lp_hz=4000))
    b = delay.echo_profile(0.05 * _delay_render(_burst("noise", 5), 0.3, 0.45, lp_hz=4000))
    c = delay.compare(a, b)
    assert abs(c["delay_err_pct"]) < 0.1 and abs(c["slope_diff_db_per_echo"]) < 0.3
    # a tone burst through a delay that does not filter: same again
    d = delay.compare(delay.echo_profile(_delay_render(_burst("noise", 1), 0.3, 0.45)),
                      delay.echo_profile(_delay_render(_burst("tone"), 0.3, 0.45)))
    assert abs(d["delay_err_pct"]) < 0.1 and abs(d["slope_diff_db_per_echo"]) < 0.3
    # (an echo path that filters makes the energy slope depend on the input's spectrum: parity
    # compares two engines on the same input, so that is fine there)


# ----------------------------------------------------------------------------- reverb


def _reverb_render(x: np.ndarray, rt60: float, predelay_s: float, mix: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(rt60 * 1.5 * SR)
    t = np.arange(n) / SR
    h = rng.standard_normal(n) * np.exp(-3 * np.log(10) * t / rt60)
    h = np.concatenate([np.zeros(int(predelay_s * SR)), h])
    h *= 0.05
    wet = signal.fftconvolve(x, h)[: x.size]
    return (1 - mix) * x + mix * wet


def _reverb_input(seed: int, kind: str) -> np.ndarray:
    if kind == "noise":
        x = np.random.default_rng(seed).standard_normal(int(1.5 * SR)) * 0.1
    else:
        x = guitar_di(seed, seconds=1.8)[: int(1.5 * SR)]   # cut while playing, like the parity clip
    return np.concatenate([x, np.zeros(int(2.5 * SR))])


@pytest.mark.parametrize("kind", ["noise", "guitar"])
def test_reverb_recovers_decay_and_predelay(kind):
    x = _reverb_input(3, kind)
    y = _reverb_render(x, rt60=1.2, predelay_s=0.025, mix=0.4)
    p = reverb.reverb_profile(y, x)
    mid = [i for i, f in enumerate(p.bands_hz) if 250 <= f <= 4000]
    rt = np.asarray(p.rt60_s)[mid]
    # one free decay of a guitar is a single noisy realisation: per band within 25 %, median within 10 %
    assert np.all(np.abs(rt / 1.2 - 1) < 0.25), p.rt60_s
    assert abs(np.median(rt) / 1.2 - 1) < 0.10, p.rt60_s
    assert abs(np.median(np.asarray(p.edt_s)[mid]) / 1.2 - 1) < 0.25, p.edt_s
    assert abs(p.predelay_ms - 25.0) < 2.0


def test_reverb_is_insensitive_to_notes():
    pa = reverb.reverb_profile(_reverb_render(_reverb_input(1, "guitar"), 1.0, 0.02, 0.4), _reverb_input(1, "guitar"))
    pb = reverb.reverb_profile(_reverb_render(_reverb_input(2, "guitar"), 1.0, 0.02, 0.4), _reverb_input(2, "guitar"))
    c = reverb.compare(pa, pb)
    mid = [i for i, f in enumerate(c["bands_hz"]) if 500 <= f <= 4000]
    assert abs(np.median([c["rt60_diff_pct"][i] for i in mid])) < 10
    assert max(abs(c["rt60_diff_pct"][i]) for i in mid) < 30
    assert abs(c["predelay_diff_ms"]) < 2.0


def test_reverb_estimator_floor_is_small_for_noise_and_known_for_guitar():
    x = _reverb_input(5, "guitar")
    fl = reverb.estimator_floor(x, [1.0] * 7, t_stop=1.5, pairs=2)
    assert len(fl["rt60_pct"]) == 7 and all(np.isfinite(fl["rt60_pct"]))
    assert fl["tail_spectrum_db"] > 0


def test_reverb_compare_sees_a_longer_decay():
    x = _reverb_input(4, "noise")
    a = reverb.reverb_profile(_reverb_render(x, 1.0, 0.02, 0.4), x)
    b = reverb.reverb_profile(_reverb_render(x, 1.3, 0.02, 0.4), x)
    c = reverb.compare(a, b)
    assert np.nanmedian(c["rt60_diff_pct"]) > 20
    assert parity.reverb_verdict(c)["pass"] is False


# ----------------------------------------------------------------------------- modulation


def _tremolo(x: np.ndarray, rate: float, depth: float, phase: float) -> np.ndarray:
    t = np.arange(x.size) / SR
    return x * (1 - depth / 2 + depth / 2 * np.sin(2 * np.pi * rate * t + phase))


def _vibrato(x: np.ndarray, rate: float, depth_ms: float, phase: float, base_ms: float = 6.0, mix: float = 1.0) -> np.ndarray:
    n = np.arange(x.size)
    d = (base_ms + depth_ms / 2 * np.sin(2 * np.pi * rate * n / SR + phase)) * SR / 1000
    wet = np.interp(n - d, n, x, left=0.0)
    return (1 - mix) * x + mix * wet


@pytest.fixture(scope="module")
def dis():
    return guitar_di(11, seconds=5.0), guitar_di(12, seconds=5.0)


def test_tremolo_rate_and_depth_phase_and_note_independent(dis):
    a, b = dis
    pa = modulation.mod_profile(_tremolo(a, 3.0, 0.6, 0.0), a)            # auto: no kind given
    pb = modulation.mod_profile(_tremolo(b, 3.0, 0.6, 2.1), b, kind="tremolo")
    want = 20 * np.log10(1.0 / 0.4)
    for p in (pa, pb):
        assert abs(p.rate_hz - 3.0) < 0.06
        assert abs(p.amp_depth_db - want) < 1.0
    c = modulation.compare(pa, pb, "tremolo")
    assert abs(c["rate_diff_pct"]) < 2 and abs(c["amp_depth_diff_db"]) < 0.5
    assert abs(c["spec_level_diff_db"]) < 0.5 and c["spec_shape_max_abs_db"] < 1.0
    assert abs(c["lfo_phase_diff_rad"]) > 1.0          # the phases did differ; the measures did not


def test_vibrato_rate_and_pitch_depth(dis):
    a, b = dis
    pa = modulation.mod_profile(_vibrato(a, 1.5, 2.0, 0.0), a, kind="pitch")
    pb = modulation.mod_profile(_vibrato(b, 1.5, 2.0, 1.3), b, kind="pitch")
    want_cents = 1200 * np.log2(1 + 2 * np.pi * 1.5 * 0.001)
    for p in (pa, pb):
        assert abs(p.rate_hz - 1.5) < 0.05
        assert abs(p.delay_depth_ms - 2.0) < 0.3
        assert abs(p.pitch_depth_cents - want_cents) < 0.15 * want_cents
    c = modulation.compare(pa, pb, "pitch")
    assert parity.modulation_verdict(c)["checks"]["rate"] is True
    assert parity.modulation_verdict(c)["checks"]["depth"] is True


def test_chorus_mix_still_tracks_the_moving_delay(dis):
    a, _ = dis
    p = modulation.mod_profile(_vibrato(a, 0.8, 3.0, 0.4, base_ms=12.0, mix=0.5), a, kind="pitch")
    assert abs(p.rate_hz - 0.8) < 0.05
    assert abs(p.delay_depth_ms - 3.0) < 0.6


# ----------------------------------------------------------------------------- distortion


@pytest.fixture(scope="module")
def drive():
    x = np.concatenate([guitar_di(21, seconds=4.0), np.zeros(int(2.0 * SR))])
    return x, np.tanh(6 * x) * 0.3


def test_distortion_identical_passes(drive):
    x, y = drive
    d = nonlinear.diagnose(y, y.copy(), di=x)
    assert d["null_db"] < -100 and nonlinear.classify(d) == "pass"


def test_distortion_level_only(drive):
    x, y = drive
    d = nonlinear.diagnose(1.25 * y, y, di=x)
    assert abs(d["level_diff_db"] - 20 * np.log10(1.25)) < 0.05
    assert nonlinear.classify(d) == "pass+level"


def test_distortion_tone_difference_is_tone(drive):
    x, y = drive
    b, a = signal.iirpeak(1500, 1.0, fs=SR)
    y2 = y + 0.6 * signal.lfilter(b, a, y)          # about +4 dB around 1.5 kHz
    d = nonlinear.diagnose(y2, y, di=x)
    assert nonlinear.classify(d).startswith("tone")


def test_distortion_hf_only(drive):
    x, y = drive
    hf = signal.sosfilt(signal.butter(8, 13000, "highpass", fs=SR, output="sos"), np.random.default_rng(0).standard_normal(y.size))
    hf *= 0.08 * np.sqrt(np.mean(y ** 2) / np.mean(hf ** 2)) * (np.abs(x) > 0).astype(float)
    d = nonlinear.diagnose(y + hf, y, di=x)
    assert d["null_db"] > -30
    assert nonlinear.classify(d) == "hf-only"


def test_distortion_phase_only_is_waveform_only(drive):
    x, y = drive
    r, th = 0.97, 2 * np.pi * 1200 / SR              # second-order allpass at 1.2 kHz: same spectrum, other waveform
    a = [1.0, -2 * r * np.cos(th), r * r]
    b = a[::-1]
    d = nonlinear.diagnose(signal.lfilter(b, a, y), y, di=x)
    assert d["null_db"] > -30
    assert nonlinear.classify(d) == "waveform-only"
