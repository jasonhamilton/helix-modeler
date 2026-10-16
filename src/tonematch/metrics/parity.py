"""Parity tolerances for the direct measurements (delay, reverb, modulation, distortion): how far two
engines may differ on each measure before a listener could tell. PROPOSED (tone-metrics, 2026-10-16),
not yet accepted by the lead; see docs/DECISIONS.md.

Each tolerance is tied to a published just-noticeable difference (JND), taken conservatively:

- Level, per band or overall: 1 dB. Level JND for broadband sound is about 1 dB; ISO 3382-1:2009
  Annex A lists 1 dB for strength G and for clarity C80 (the early/late, roughly dry/wet, ratio).
  Moore et al. found narrow spectral peaks detectable from about 1-2 dB; Green's profile analysis
  1-4 dB. 1 dB is the low end.
- Decay times (EDT, RT60): 5 %, the ISO 3382-1 Annex A JND (after Seraphim 1958); later studies put
  it higher (up to 20-40 % for RT in some conditions), so 5 % is strict.
- Timing: Friberg and Sundberg (1995, JASA 98:2524) measured the JND for displacing one tone of an
  isochronous sequence: about 10 ms for intervals under 240 ms and about 5 % of the interval above.
  Delay time is held to 2 % (less than half the 5 %, because echoes also sit against the song's
  beat) and the accumulated offset of the audible echoes to 10 ms. Pre-delay (an interval under
  240 ms) to 5 ms, half the 10 ms.
- LFO rate: 5 %, the same interval rule applied to the LFO period (a period of 0.2-2 s); amplitude
  modulation rate discrimination (Formby 1985) is coarser than this at low rates.
- Tremolo depth: 1 dB of peak-to-trough swing (level JND); depth discrimination in Wakefield and
  Viemeister (1990) is of the same order for deep modulation.
- Pitch depth: the larger of 3 cents and 10 %. Frequency difference limens are about 0.2-0.3 %
  (3-5 cents) at 0.5-2 kHz (Wier, Jesteadt and Green 1977); vibrato extent is heard less precisely
  than a steady pitch.
- Distortion harmonics: 1 dB per harmonic re the fundamental (profile-analysis low end).
"""
from __future__ import annotations

import math

TOLERANCES: dict[str, dict[str, float]] = {
    "delay": {"delay_time_pct": 2.0, "cum_time_err_ms": 10.0, "echo_level_db": 1.0, "slope_db_per_echo": 0.5},
    "reverb": {"edt_pct": 5.0, "rt60_pct": 5.0, "predelay_ms": 5.0, "tail_spectrum_db": 1.0, "tail_level_db": 1.0},
    "modulation": {"rate_pct": 5.0, "amp_depth_db": 1.0, "sweep_depth_db": 1.0, "pitch_depth_cents": 3.0,
                   "pitch_depth_pct": 10.0, "spectrum_shape_db": 1.0, "spectrum_level_db": 1.0},
    "distortion": {"core_band_db": 1.0, "harmonic_db": 1.0, "level_db": 1.0},
}
# A floor is the largest difference over 8 pairs of chance-only reverbs (about the 90th percentile).
# With about 10 checks per model, a difference counts as real only beyond 1.5 times the floor.
FLOOR_MARGIN = 1.5
MIN_CYCLES = 2.0           # LFO cycles the clip must hold for its rate to be measured
MIN_CONFIDENCE = 0.3       # share of the trajectory's variance one sinusoid at the rate must explain
# octave bands judged for decay times (125 Hz and 8 kHz are reported but carry little guitar energy)
REVERB_JUDGED_BANDS = (250.0, 500.0, 1000.0, 2000.0, 4000.0)


def _ok(v: float | None, tol: float) -> bool | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return abs(v) <= tol


def delay_verdict(c: dict, tol: dict | None = None) -> dict:
    t = tol or TOLERANCES["delay"]
    checks = {
        "delay_time": _ok(c["delay_err_pct"], t["delay_time_pct"]),
        "cum_time": _ok(c["cum_time_err_ms_audible"], t["cum_time_err_ms"]),
        "echo_level": _ok(c["max_abs_level_diff_db"], t["echo_level_db"]),
        "feedback_slope": _ok(c["slope_diff_db_per_echo"], t["slope_db_per_echo"]),
        "echo_count": c["n_echoes_ref"] == c["n_echoes_other"],
    }
    return {"checks": checks, "pass": all(v is True for v in checks.values())}


def reverb_verdict(c: dict, tol: dict | None = None, floor: dict | None = None) -> dict:
    """`floor` (optional): per-band 95th-percentile differences the estimator itself produces for two
    statistically identical tails ({"edt_pct": [...], "rt60_pct": [...]}). A band whose floor is above
    the tolerance cannot be judged at the JND and is reported as unresolved, not failed."""
    t = tol or TOLERANCES["reverb"]
    checks: dict = {}
    unresolved: list[str] = []
    for key, tk in (("edt_diff_pct", "edt_pct"), ("rt60_diff_pct", "rt60_pct")):
        for i, f in enumerate(c["bands_hz"]):
            if f not in REVERB_JUDGED_BANDS:
                continue
            v = c[key][i]
            fl = (floor or {}).get(tk, [0.0] * len(c["bands_hz"]))[i]
            name = f"{tk}@{int(f)}"
            if fl > t[tk] and math.isfinite(v):
                if abs(v) <= FLOOR_MARGIN * fl:
                    unresolved.append(name)      # cannot be told from chance in one free decay
                else:
                    checks[name] = False         # clearly outside what chance gives
                continue
            checks[name] = _ok(v, t[tk])
    for name, key, tk in (("predelay", "predelay_diff_ms", "predelay_ms"), ("tail_spectrum", "tail_spectrum_max_abs_db", "tail_spectrum_db"),
                          ("tail_level", "tail_level_diff_db", "tail_level_db")):
        v = c[key]
        fs = (floor or {}).get(tk, 0.0)
        if v is not None and fs > t[tk]:
            if abs(v) <= FLOOR_MARGIN * fs:
                unresolved.append(name)
            else:
                checks[name] = False
        else:
            checks[name] = _ok(v, t[tk])
    return {"checks": checks, "unresolved": unresolved, "pass": all(v is True for v in checks.values())}


def modulation_verdict(c: dict, tol: dict | None = None, *, cycles: float | None = None, confidence: float | None = None,
                       floor: dict | None = None) -> dict:
    """`cycles` and `confidence`: the smaller of the two profiles' LFO cycles in the clip and rate
    confidences. Below MIN_CYCLES cycles or MIN_CONFIDENCE the rate cannot be measured from this clip,
    and neither can anything that depends on it (depth fits, LFO-free spectrum): unresolved, not failed.
    `floor`: differences the Stomp shows against itself (e.g. {"pitch_depth_pct": 54}); a depth
    difference inside the floor is unresolved."""
    t = tol or TOLERANCES["modulation"]
    if (cycles is not None and cycles < MIN_CYCLES) or (confidence is not None and confidence < MIN_CONFIDENCE):
        return {"checks": {}, "unresolved": ["rate", "depth", "spectrum_shape", "spectrum_level"], "pass": False,
                "resolved": False}
    checks: dict = {"rate": _ok(c["rate_diff_pct"], t["rate_pct"])}
    unresolved: list[str] = []
    if c["kind"] == "tremolo":
        checks["depth"] = _ok(c["amp_depth_diff_db"], t["amp_depth_db"])
    elif c["kind"] == "sweep":
        checks["depth"] = _ok(c["sweep_depth_diff_db"], t["sweep_depth_db"])
    else:
        lim = max(t["pitch_depth_cents"], t["pitch_depth_pct"] / 100 * abs(c["primary_depth_ref"]))
        fl = (floor or {}).get("pitch_depth_pct")
        v = c["pitch_depth_diff_pct"]
        if fl is not None and fl > t["pitch_depth_pct"] and math.isfinite(v) and abs(v) <= fl:
            unresolved.append("depth")
        else:
            checks["depth"] = _ok(c["pitch_depth_diff_cents"], lim)
    checks["spectrum_shape"] = _ok(c["spec_shape_max_abs_db"], t["spectrum_shape_db"])
    checks["spectrum_level"] = _ok(c["spec_level_diff_db"], t["spectrum_level_db"])
    return {"checks": checks, "unresolved": unresolved, "pass": all(v is True for v in checks.values()), "resolved": True}


def distortion_verdict(d: dict, tol: dict | None = None) -> dict:
    t = tol or TOLERANCES["distortion"]
    checks = {
        "core_bands": _ok(d["core_band_diff_max_abs_db"], t["core_band_db"]),
        "harmonics": _ok(d.get("harmonic_diff_max_abs_db"), t["harmonic_db"]),
        "level": _ok(d["level_diff_db"], t["level_db"]),
    }
    return {"checks": checks, "pass": all(v is not False for v in checks.values()) and checks["core_bands"] is True}
