"""Narrower CC ranges for time-like and rate params (T072): rules, fallback, template guard."""
from __future__ import annotations

from test_preset import synth

from tonematch import preset as P
from tonematch.engines import native as nat
from tonematch.engines import native_library as lib

CAT = {"models": {
    "HD2_DelayX": {"category": "delay", "stomp": True, "params": {
        "Time": {"kind": "continuous", "min": 0.0, "max": 8.0, "default": 0.6, "display": "time_ms_0_8000"},
        "Rate": {"kind": "continuous", "min": 0.1, "max": 8.0, "default": 0.3, "display": "frequency"},
        "Attack": {"kind": "continuous", "min": 0.1, "max": 5.0, "default": 1.0, "display": "time_ms"},
        "Mix": {"kind": "continuous", "min": 0.0, "max": 1.0, "default": 0.4, "display": "percent"}}},
    "HD2_DelayShort": {"category": "delay", "stomp": True, "params": {
        "Time": {"kind": "continuous", "min": 0.02, "max": 0.5, "default": 0.375, "display": "time_ms_20_500"}}},
    "HD2_ModX": {"category": "mod", "stomp": True, "params": {
        "Speed": {"kind": "continuous", "min": 0.0, "max": 15.0, "default": 1.59, "display": "frequency"}}},
    "HD2_TremX": {"category": "mod", "stomp": True, "params": {
        "Speed": {"kind": "continuous", "min": 0.0, "max": 15.0, "default": 7.55, "display": "frequency"}}},
    "HD2_ReverbX": {"category": "reverb", "stomp": True, "params": {
        "Decay": {"kind": "continuous", "min": 0.1, "max": 45.1, "default": 2.0, "display": "time_sec_DynamicHall"},
        "Predelay": {"kind": "continuous", "min": 0.0, "max": 0.2, "default": 0.03, "display": "time_ms_reverb"}}},
}}


def nr(mid, name):
    m = CAT["models"][mid]
    return nat.narrow_range(m["category"], name, m["params"][name])


def test_rules_narrow_delay_time_rates_and_long_decays_only():
    assert nr("HD2_DelayX", "Time") == (0.0, 1.27)          # 62.99 ms -> 10 ms steps
    assert nr("HD2_DelayX", "Rate") == (0.1, 2.64)          # 0.02 Hz steps, >= 2.5 Hz and 2 x default
    assert nr("HD2_DelayX", "Attack") is None               # not an echo time
    assert nr("HD2_DelayX", "Mix") is None
    assert nr("HD2_DelayShort", "Time") is None             # already narrower than 1.27 s
    assert nr("HD2_ModX", "Speed") == (0.0, 5.08)           # default 1.59 Hz needs >= 3.18 Hz
    assert nr("HD2_TremX", "Speed") is None                 # fast LFO: catalog step under 2 %
    assert nr("HD2_ReverbX", "Decay") == (0.1, 6.45)
    assert nr("HD2_ReverbX", "Predelay") is None            # 1.57 ms steps already
    lo, hi = nr("HD2_DelayX", "Time")
    assert abs(nat.cc_value(lo, hi, 38) - 0.38) < 1e-6 and nat.cc_value(lo, hi, 127) == np_f32(1.27)


def np_f32(x):
    import numpy as np

    return float(np.float32(x))


def delay_preset(time_s):
    p = P.from_dict(synth(), CAT)
    q = lib.single_block("HD2_DelayX", CAT, p)
    q.slots[0].params["Time"] = time_s
    return q


def test_cc_ranges_keep_the_catalog_range_for_a_value_outside_the_narrowed_one():
    assert nat.cc_ranges(delay_preset(0.6), CAT)["0:Time"] == (0.0, 1.27)
    assert nat.cc_ranges(delay_preset(3.0), CAT)["0:Time"] == (0.0, 8.0)
    assert nat.cc_ranges(delay_preset(0.6), CAT, narrow=False)["0:Time"] == (0.0, 8.0)
    # the capture key differs, so capture_missing never shares one capture between the two
    assert nat.capture_key(delay_preset(0.6), CAT) != nat.capture_key(delay_preset(3.0), CAT)


def template(p, ranges, name="t"):
    amap = {"0:@enabled": "cc_1", "0:Time": "cc_2", "0:Mix": "cc_3"}
    verified = list(amap)
    baked, h = nat.baked_fingerprint(p, amap, verified)
    return nat.ChainTemplate(nat.chain_key(p), b"s", amap, ranges, name=name, verified=verified,
                             baked=baked, baked_hash=h)


def test_template_refuses_values_outside_its_cc_range_and_prefers_the_finer_grid():
    p = delay_preset(0.6)
    narrow = template(p, {"0:@enabled": [0.0, 1.0], "0:Time": [0.0, 1.27], "0:Mix": [0.0, 1.0]}, "narrow")
    wide = template(p, {"0:@enabled": [0.0, 1.0], "0:Time": [0.0, 8.0], "0:Mix": [0.0, 1.0]}, "wide")
    assert nat.out_of_range(narrow, p) == [] and lib.matches(narrow, p)
    far = delay_preset(3.0)
    assert nat.out_of_range(narrow, far) == ["0:Time"] and not lib.matches(narrow, far)
    assert lib.matches(wide, far)
    eng = nat.NativeEngine.__new__(nat.NativeEngine)
    eng._variants = {narrow.chain: [wide, narrow]}
    assert eng._match_template(p, narrow.chain)[0] is narrow
    assert eng._match_template(far, narrow.chain)[0] is wide
    eng._variants = {narrow.chain: [narrow]}
    t, why = eng._match_template(far, narrow.chain)
    assert t is None and "outside its CC range" in why


def test_index_entry_records_narrowed_ranges():
    p = delay_preset(0.6)
    t = template(p, {"0:@enabled": [0.0, 1.0], "0:Time": [0.0, 1.27], "0:Mix": [0.0, 1.0]})
    assert lib.narrowed_ranges(t, p, CAT) == {"0:Time": [0.0, 1.27]}
