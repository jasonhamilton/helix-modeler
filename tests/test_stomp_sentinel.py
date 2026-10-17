"""StompEngine session sentinel (T108, stomp-verify-v1 rule 4). Offline: the fake pedal, compiler
and reamp of test_stomp_render_many (gain follows the chain's Drive values). No pedal."""
from __future__ import annotations

import numpy as np
import pytest
from test_stomp_render_many import CAT, LAT, Pedal, fake_compiler, hlx

from tonematch import catalog as C
from tonematch import reamp
from tonematch.engines import stomp

DRY = (0.3 * np.sin(np.linspace(0, 300, 9600))).astype(np.float32)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.delenv(stomp.ENV_WHOLE_PRESET, raising=False)
    monkeypatch.delenv(stomp.ENV_SENTINEL, raising=False)
    monkeypatch.delenv(stomp.ENV_SENTINEL_EVERY, raising=False)
    monkeypatch.setattr(C, "load_catalog", lambda *a, **k: CAT)
    monkeypatch.setattr(reamp, "find_device", lambda *a, **k: 0)
    state = {"noise": 0.0, "lat_extra": 0, "renders": 0}

    def make(sentinel=None, purpose=None, first=None):
        pedal = Pedal(first or hlx([("DistA", 0.2)]))
        rng = np.random.default_rng(0)

        def playrec(mono, device=None, pre_s=0.25, tail_s=0.1, out_pair=(5, 6), in_pair=(1, 2)):
            state["renders"] += 1
            pre, n = int(pre_s * 48000), len(mono)
            lat = LAT + state["lat_extra"]
            rec = np.zeros((pre + n + int(tail_s * 48000) + 64, 8), np.float32)
            y = pedal.gain("A") * mono + state["noise"] * rng.standard_normal(n).astype(np.float32)
            rec[pre + lat:pre + lat + n, 0:2] = y[:, None]
            return (rec if in_pair is None else rec[:, [in_pair[0] - 1, in_pair[1] - 1]]), pre

        monkeypatch.setattr(reamp, "playrec", playrec)
        cal = reamp.Calibration(latency_samples=LAT, measurements=[LAT], level_gain_db=0.0, tone_in_dbfs=-18,
                                tone_out_dbfs=-18, firmware="3.80", pa_latency=reamp.PA_LATENCY)
        eng = stomp.StompEngine(bridge=pedal, calibration=cal, session_dir=tmp_path, compiler=fake_compiler(pedal, []),
                                sentinel=sentinel, purpose=purpose)
        eng.PRE_S = 0.001
        eng.pre_roll_s = lambda: 0.001
        return eng, pedal

    return make, state


def ref():
    return hlx([("DistC", 0.4)], name="sentinel")


def test_default_rule_on_for_benchmark_purpose_only(monkeypatch):
    monkeypatch.delenv(stomp.ENV_SENTINEL, raising=False)
    monkeypatch.delenv(stomp.ENV_SENTINEL_EVERY, raising=False)
    assert stomp.resolve_sentinel(None, "benchmark_suite parity") is not None
    assert stomp.resolve_sentinel(None, "search") is None
    assert stomp.resolve_sentinel(None, None) is None
    assert stomp.resolve_sentinel(False, "benchmark") is None
    assert stomp.resolve_sentinel(True, None).every == stomp.Sentinel.every
    monkeypatch.setenv(stomp.ENV_SENTINEL, "on")
    monkeypatch.setenv(stomp.ENV_SENTINEL_EVERY, "7")
    assert stomp.resolve_sentinel(None, None).every == 7
    monkeypatch.setenv(stomp.ENV_SENTINEL, "off")
    assert stomp.resolve_sentinel(None, "benchmark") is None
    monkeypatch.setenv(stomp.ENV_SENTINEL, "maybe")
    with pytest.raises(stomp.EngineError):
        stomp.resolve_sentinel(None, None)


def test_sentinel_checks_every_n_and_puts_the_session_state_back(rig):
    make, _state = rig
    eng, pedal = make(sentinel=stomp.Sentinel(every=3, preset=ref()))
    eng.load(hlx([("DistA", 0.2)]))
    eng.set_params({(0, "Drive"): 0.7})
    want = eng.render(DRY)                   # first render: the sentinel takes its reference
    rep = eng.sentinel_report
    assert rep["enabled"] and rep["threshold_db"] == -60.0 and rep["checks"] == 0
    assert stomp._block_slots(pedal.chain)[1]["model_id"] == "DistA"          # back on the session's block
    assert stomp._block_slots(pedal.chain)[1]["params"][0]["value"] == pytest.approx(0.7)   # with its set_params
    for _ in range(6):
        y = eng.render(DRY)
        np.testing.assert_allclose(y, want, rtol=1e-6)
    assert rep["checks"] == 2 and all(v <= -60 for v in rep["nulls_db"])
    assert eng.preset is not None and eng.slot_map == {0: (1, False)}
    eng.set_params({(0, "Drive"): 0.1})   # the session can carry on in place after a check
    np.testing.assert_allclose(eng.render(DRY), 0.1 * 1.1 * DRY, rtol=1e-5)
    assert any(c == "restore" for c, _ in pedal.calls)
    assert not list(eng.session_dir.glob("sentinel_*.bin"))   # its backups are removed


def test_drift_raises_and_refuses_later_renders(rig):
    make, state = rig
    eng, _pedal = make(sentinel=stomp.Sentinel(every=2, preset=ref()))
    eng.load(hlx([("DistA", 0.2)]))
    eng.render(DRY)
    eng.render(DRY)
    state["lat_extra"] = 5      # a latency error the engine's +-2 sample alignment cannot hide
    with pytest.raises(stomp.SentinelDrift) as e:
        eng.render(DRY)
    assert e.value.report["tripped"] and "threshold" in e.value.report["reason"]
    state["lat_extra"] = 0
    with pytest.raises(stomp.SentinelDrift):
        eng.render(DRY)
    eng.close()
    assert eng.restore_report.get("match")


def test_check_now_and_unrepeatable_reference_disables(rig):
    make, state = rig
    eng, _ = make(sentinel=stomp.Sentinel(every=100, preset=ref()))
    eng.load(hlx([("DistA", 0.2)]))
    eng.render(DRY)
    assert eng.sentinel_check_now() <= -60
    state["noise"] = 0.5
    eng2, _ = make(sentinel=stomp.Sentinel(every=100, preset=ref()))
    eng2.load(hlx([("DistA", 0.2)]))
    eng2.render(DRY)
    assert not eng2.sentinel_report["enabled"] and "does not repeat" in eng2.sentinel_report["disabled_reason"]
    eng2.render(DRY)   # disabled: renders go on, no checks
    assert eng2.sentinel_report["checks"] == 0


def test_default_reference_is_the_first_loaded_preset_and_off_by_default(rig):
    make, _ = rig
    eng, _ = make(purpose="benchmark parity")
    first = hlx([("DistB", 0.3)])
    eng.load(first)
    eng.render(DRY)
    assert eng.sentinel_report["enabled"] and eng.sentinel_report["threshold_db"] is not None
    assert eng._first_preset is not first and eng._first_preset.slots[0].model_id == "DistB"
    plain, _ = make()
    plain.load(first)
    plain.render(DRY)
    assert plain.sentinel is None and plain.sentinel_report["floor_db"] is None


# --------------------------------------------------------------------------- T108: pre-roll floors


def test_pre_roll_floor_is_one_second_and_two_with_an_amp(monkeypatch, tmp_path):
    from test_stomp import make_chain, make_engine

    monkeypatch.setattr(stomp, "_CATEGORY_CACHE", {"HD2_AmpX": "amp", "DelayA": "delay", "_": ""})
    eng, _ = make_engine(monkeypatch, tmp_path, make_chain(model_id="HD2_AmpX"))
    assert eng.PRE_S == 1.0 and eng.AMP_PRE_S == 2.0
    assert eng._min_wait_s() == 2.0 and eng.pre_roll_s() == 2.0 and eng._wait_s() == 2.0
    for s in eng.chain["slots"]:
        if s.get("model_id") == "HD2_AmpX":
            s["enabled"] = False
    assert eng._min_wait_s() == 1.0 == eng.pre_roll_s()
    assert reamp.MAX_JITTER == 4
