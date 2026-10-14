"""NativeEngine tests that run without Helix Native.

The process boundary, latency trim, determinism, cost measurement and the render pool are
exercised through pedalboard's builtin effects (``builtin:`` stand-ins), always inside the host
subprocess: pedalboard (GPLv3) is never imported into the pytest process, only located with
importlib.util.find_spec to decide whether the host tests can run.
The state codec is exercised on a synthetic Helix-like blob: no Line 6 data is used.
"""
from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines import native_state as ns
from tonematch.engines.base import EngineError, UnsupportedModelChange

needs_host = pytest.mark.skipif(importlib.util.find_spec("pedalboard") is None,
                                reason="plugin host (pedalboard) not installed: pip install -e .[native]")


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path / "cache"))


# --------------------------------------------------------------------------- state codec


def _fake_native_state(doc: dict) -> bytes:
    body = json.dumps(doc).encode()
    z = zlib.compress(body)
    comp = b"HXN\x01" + struct.pack("<I", len(body)) + struct.pack("<I", len(z)) + z + b"\x00tail"
    ctrl = b"\x00\x01ctrl"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>{ns.juce_b64_encode(comp)}'
           f"</IComponent><IEditController>{ns.juce_b64_encode(ctrl)}</IEditController></VST3PluginState>")
    return ns.wrap_vc2(xml)


DOC = {"data": {"device": 2162694, "tone": {"dsp0": {"block0": {"@model": "HD2_TestAmp", "Drive": 0.5}}}}}


def test_juce_b64_roundtrip():
    for n in (0, 1, 2, 3, 7, 64, 333):
        b = bytes(range(256))[:n] * 1 if n <= 256 else bytes(n)
        assert ns.juce_b64_decode(ns.juce_b64_encode(b)) == b


def test_scan_finds_preset_doc_and_replace_roundtrips():
    raw = _fake_native_state(DOC)
    parts = ns.split_vst3_state(raw)
    assert set(parts) == {"IComponent", "IEditController"}
    cand = ns.best_preset_candidate(ns.scan(parts["IComponent"]))
    assert cand is not None and cand.kind == "json"
    assert [l.kind for l in cand.path] == ["zlib", "json"]
    assert json.loads(cand.data) == DOC

    doc2 = json.loads(json.dumps(DOC))
    doc2["data"]["tone"]["dsp0"]["block0"]["Drive"] = 0.9
    doc2["data"]["tone"]["dsp0"]["block1"] = {"@model": "HD2_TestCab", "Level": 0.0}  # longer document
    new_comp = ns.replace(parts["IComponent"], cand.path, json.dumps(doc2).encode())
    raw2 = ns.join_vst3_state(raw, {"IComponent": new_comp})
    c2 = ns.split_vst3_state(raw2)["IComponent"]
    got = ns.best_preset_candidate(ns.scan(c2))
    assert json.loads(got.data) == doc2
    # length fields directly before the stream were fixed up
    assert struct.unpack("<I", c2[4:8])[0] == len(json.dumps(doc2).encode())
    assert struct.unpack("<I", c2[8:12])[0] == got.path[0].length
    assert c2.endswith(b"\x00tail")
    assert ns.split_vst3_state(raw2)["IEditController"] == b"\x00\x01ctrl"


def test_opaque_blob_has_no_candidate():
    rng = np.random.default_rng(1)
    assert ns.best_preset_candidate(ns.scan(rng.bytes(4096))) is None


# --------------------------------------------------------------------------- availability


def test_stadium_refused_and_builtin_not_available(monkeypatch):
    assert nat.is_stadium("/Library/Audio/Plug-Ins/VST3/Helix Stadium Native.vst3")
    with pytest.raises(EngineError):
        nat.NativeEngine("/x/Helix Stadium Native.vst3")
    monkeypatch.setenv("TONEMATCH_NATIVE_PLUGIN", "builtin:Gain")
    assert nat.NativeEngine.is_available() is False


# --------------------------------------------------------------------------- host boundary


@pytest.fixture
def delay_engine():
    e = nat.NativeEngine("builtin:Delay?delay_seconds=0.01&mix=1.0&feedback=0")
    yield e
    e.close()


@needs_host
def test_render_trims_latency_and_is_deterministic(delay_engine):
    x = nat.test_signal(0.5)
    y1, y2 = delay_engine.render(x), delay_engine.render(x)
    assert y1.dtype == np.float32 and y1.shape == x.shape
    assert 470 <= delay_engine.latency() <= 490  # 10 ms at 48 kHz, measured by impulse
    assert nat.null_db(x, y1) < -60               # wet-only delay, trimmed, equals the input
    assert nat.null_db(y1, y2) < -120


@needs_host
def test_silence_stays_silent_and_level_is_fixed(delay_engine):
    assert np.max(np.abs(delay_engine.render(np.zeros(4800, np.float32)))) == 0.0
    e = nat.NativeEngine("builtin:Gain?gain_db=0", calibration_db=-6.0)
    try:
        x = nat.test_signal(0.2)
        assert np.allclose(e.render(x), x * 10 ** (-6 / 20), atol=1e-6)
    finally:
        e.close()


@needs_host
def test_silent_output_raises():
    e = nat.NativeEngine("builtin:Gain?gain_db=-200")
    try:
        with pytest.raises(EngineError, match="silence"):
            e.render(nat.test_signal(0.2))
    finally:
        e.close()


@needs_host
def test_cost_is_measured_and_cached(delay_engine, tmp_path):
    c = delay_engine.measure_cost(max_workers=2)
    assert c.seconds_per_render > 0 and c.parallelism in (1, 2) and c.load_penalty_s >= 0
    assert (nat.cache_dir() / "cost.json").is_file()
    assert delay_engine.measure_cost(max_workers=2) == c


@dataclass
class _Slot:
    category: str
    model_id: str | None
    params: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class _Preset:
    slots: list


@needs_host
def test_automation_route_raises_for_unknown_chain(delay_engine):
    p = _Preset([_Slot("amp", "HD2_TestAmp", {"Drive": 0.4}), _Slot("cab", None, enabled=False)])
    with pytest.raises(UnsupportedModelChange):
        delay_engine.load(p)


@needs_host
def test_render_pool_matches_single_engine():
    pool = nat.NativeRenderPool(workers=2, plugin_path="builtin:Reverb")
    try:
        x = nat.test_signal(0.3)
        outs = pool.render_batch([nat.RenderJob(x) for _ in range(5)])
        assert len(outs) == 5 and all(nat.null_db(outs[0], o) < -120 for o in outs)
    finally:
        pool.close()


def test_gpl_host_never_imported_in_this_process():
    """Licence boundary: pedalboard lives only in the host subprocess (runs last in this file)."""
    import sys

    assert not any(m == "pedalboard" or m.startswith("pedalboard.") for m in sys.modules)
