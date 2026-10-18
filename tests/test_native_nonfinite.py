"""T132: a NaN-poisoned Native host. Measured on Native 3.82: one Panama Red setting renders inf/NaN,
after which EVERY render of that host process is NaN (state re-load, another chain, the empty-chain
flush and silence do not clear it; a re-prepare or a new process does). The engine must replace the
host and re-render once, raise only for a setting that is unstable on a clean host too, and the
in-run sentinel must see a poisoned host. Fake host, no Native, no pedalboard."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines.base import EngineError

REF = np.sin(np.arange(24000) / 9.0).astype(np.float32)


class PoisonHost(nat.HostProcess):
    """Renders the input unchanged, except: knob ``cc_bad`` >= 0.5 blows up near the end of the
    render and poisons the instance; a poisoned instance renders NaN for every chain until it is
    re-prepared or the process restarts (what Native 3.82 does, T132)."""

    def __init__(self):
        self.state, self.params = b"START", {}
        self.poisoned = False
        self.calls: list = []
        self.restarts = 0
        self.canary, self.primed = True, False
        self.canary_respawns = self.canary_reprepares = self.canary_fixed = 0
        self.canary_null_db = None
        self.mem_mb = None
        self.info = {"has_state": True}

    def call(self, cmd, payload=b"", **kw):
        self.calls.append(cmd)
        if cmd == "set_state":
            self.state, self.params = payload, {}   # does NOT clear the poison
        elif cmd == "set_params":
            self.params.update(kw["values"])
        elif cmd == "reprepare":
            self.poisoned = False
        elif cmd == "render":
            x = np.frombuffer(payload, dtype="<f4").astype(np.float32)
            if self.state == b"SENT":
                x = np.concatenate([np.zeros(4800, np.float32), REF])
            if self.poisoned:
                return {}, np.full(len(x), np.nan, np.float32).astype("<f4").tobytes()
            if self.params.get("cc_bad", 0.0) >= 0.5:
                y = x.copy()
                y[-3:] = np.inf
                self.poisoned = True
                return {}, y.astype("<f4").tobytes()
            return {}, x.astype("<f4").tobytes()
        return {}, b""

    def restart(self):
        self.restarts += 1
        self.poisoned = False
        self.state, self.params = b"START", {}


def _engine(host, every=0):
    e = object.__new__(nat.NativeEngine)
    e.host, e.plugin, e.plugin_name = host, "fake.vst3", None
    e._initial_state, e._hc_mode = b"START", None
    e._loaded_state, e._host_params = b"CHAIN", {"cc_1": 0.5}
    e.recycles, e.renders, e._since_sentinel = 0, 0, 0
    e.sentinel = {"checks": 0, "drifts": 0, "fixed": 0, "recycles": 0, "suspect_renders": 0, "skipped": 0,
                  "worst_db": None, "off": ""}
    e._sentinel_state, e._sentinel_ref, e.sentinel_every = b"SENT", REF.copy(), every
    e.reset_mode, e._preroll_fixed, e._plugin_latency = "reset", 0, 0
    e.current_chain, e.calibration_db, e.mem_limit_mb, e.last_gain_db = None, 0.0, 0.0, 0.0
    host.state, host.params = b"CHAIN", {"cc_1": 0.5}
    return e


X = (0.1 * np.sin(np.arange(4800) / 3.0)).astype(np.float32)


def test_null_db_of_a_non_finite_render_is_inf_not_nan():
    a = REF.copy()
    b = REF.copy()
    b[10] = np.nan
    assert nat.null_db(a, b) == float("inf") and nat.null_db(b, a) == float("inf")
    assert nat.null_db(a, a) <= nat.CANARY_THRESHOLD_DB


def test_a_poisoned_host_is_replaced_and_the_render_repeated():
    h = PoisonHost()
    e = _engine(h)
    h.poisoned = True                       # an earlier render left NaN in Native's DSP state
    np.testing.assert_array_equal(e.render(X), X)
    st = e.stats()
    assert st["nonfinite_events"] == 1 and st["nonfinite_recovered"] == 1 and st["nonfinite_unstable"] == 0
    assert st["nonfinite_recycles"] == 1 and e.recycles == 1 and h.restarts == 1
    assert h.state == b"CHAIN" and h.params == {"cc_1": 0.5}   # chain and knobs restored
    np.testing.assert_array_equal(e.render(X), X)              # and it stays clean
    assert h.restarts == 1


def test_an_unstable_setting_raises_and_leaves_the_host_clean():
    h = PoisonHost()
    e = _engine(h)
    e._host_params = {"cc_1": 0.5, "cc_bad": 1.0}
    h.params = dict(e._host_params)
    with pytest.raises(EngineError, match="unstable"):
        e.render(X)
    st = e.stats()
    assert st["nonfinite_events"] == 1 and st["nonfinite_unstable"] == 1 and st["nonfinite_recycles"] == 2
    assert not h.poisoned and e.renders == 0
    h.call("set_params", values={"cc_bad": 0.0})  # the next trial moves the knob away ...
    e._host_params["cc_bad"] = 0.0
    np.testing.assert_array_equal(e.render(X), X)  # ... and renders on the clean host, no recycle
    assert h.restarts == 2


def test_the_true_preset_after_a_poisoning_search_renders():
    """The T120 failure: a search trial poisons the host, the later true-preset render must work."""
    h = PoisonHost()
    e = _engine(h)
    e._host_params = {"cc_1": 0.5, "cc_bad": 1.0}
    h.params = dict(e._host_params)
    with pytest.raises(EngineError):
        e.render(X)
    for _ in range(3):                                     # other chains, other knobs
        h.call("set_state", b"OTHER")
        e._loaded_state, e._host_params = b"OTHER", {}
        np.testing.assert_array_equal(e.render(X), X)
    h.call("set_state", b"TRUE")
    e._loaded_state, e._host_params = b"TRUE", {"cc_1": 0.3}
    h.call("set_params", values={"cc_1": 0.3})
    np.testing.assert_array_equal(e.render(X), X)


def test_the_sentinel_sees_a_poisoned_host_and_fixes_it():
    h = PoisonHost()
    e = _engine(h)
    h.poisoned = True
    nd = e.sentinel_check()
    assert nd == float("inf")                               # was NaN, which passed the threshold test
    assert e.sentinel["drifts"] == 1 and e.sentinel["fixed"] == 1 and not h.poisoned
    assert h.state == b"CHAIN" and h.params == {"cc_1": 0.5}


def test_failed_renders_count_toward_the_sentinel_interval():
    h = PoisonHost()
    e = _engine(h, every=3)
    e._host_params = {"cc_1": 0.5, "cc_bad": 1.0}
    h.params = dict(e._host_params)
    for _ in range(3):
        with pytest.raises(EngineError):
            e.render(X)
    assert e._since_sentinel == 3 and e.sentinel["checks"] == 0
    with pytest.raises(EngineError):
        e.render(X)                                          # the 4th attempt runs the sentinel first
    assert e.sentinel["checks"] == 1


def test_a_finite_runaway_raises_without_a_recycle():
    """A shorter clip of an unstable setting is still finite but grows ~55 dB/s; it is not audio."""
    h = PoisonHost()
    e = _engine(h)
    with pytest.raises(EngineError, match="ran away"):
        e.render(X * np.float32(1e5))
    assert e.stats()["nonfinite_runaway"] == 1 and h.restarts == 0 and e.renders == 0
    np.testing.assert_array_equal(e.render(X), X)
