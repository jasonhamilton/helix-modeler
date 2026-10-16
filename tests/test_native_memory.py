"""Native host memory fix (2026-10-15): reset renders must not re-create the plugin instance,
and the engine recycles a host that grows past its limit. Fake plugins and fake hosts only: no
Native, no pedalboard (native_host imports pedalboard only inside open/reload)."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines import native_host as nh


class FakeParam:
    def __init__(self, v: float = 0.0):
        self.history: list[float] = []
        self._v = v

    @property
    def raw_value(self) -> float:
        return self._v

    @raw_value.setter
    def raw_value(self, v: float) -> None:
        self.history.append(float(v))
        self._v = float(v)


class FakePlugin:
    """Records every process() call and every state load, like a VST3 under pedalboard."""

    reported_latency_samples = 0

    def __init__(self):
        self.calls: list[tuple[int, bool]] = []
        self.loads: list[bytes] = []
        self._state = b"default"
        self.parameters = {"cc_1": FakeParam(), "cc_2": FakeParam()}

    @property
    def raw_state(self) -> bytes:
        return self._state

    @raw_state.setter
    def raw_state(self, b: bytes) -> None:
        self.loads.append(bytes(b))
        self._state = bytes(b)

    def process(self, x, sr, buffer_size=512, reset=True):
        self.calls.append((int(x.shape[-1]), bool(reset)))
        return np.array(x, dtype=np.float32)


def _host(strategy: str = "state") -> tuple[nh.Host, FakePlugin]:
    h = nh.Host()
    h.plugin = FakePlugin()
    h.reset_strategy = strategy
    h.processed = 0
    h._rebase()
    return h, h.plugin


def _x(n: int) -> bytes:
    return np.zeros(n, np.float32).astype("<f4").tobytes()


def test_reset_render_never_asks_pedalboard_to_reset():
    """pedalboard's reset re-creates Native (Persists-audio type) and leaks ~355 MB per call."""
    h, p = _host()
    h.set_state(b"template")
    for _ in range(5):
        hdr, _y = h.render(_x(1000), reset=True)
        assert hdr["reset_via"] == "state"
    assert all(not reset for _, reset in p.calls)
    assert p.loads.count(b"template") == 6  # set_state + one re-apply per render
    assert h.instances == 0


def test_reset_render_reapplies_base_and_resends_knobs_forcing_the_change():
    h, p = _host()
    h.set_state(b"template")
    h.set_params({"cc_1": 0.3})
    p.parameters["cc_1"].history.clear()
    h.render(_x(64), reset=True)
    assert p.loads[-1] == b"template"
    # JUCE skips approximately-equal sets, so the value must move away and back
    assert p.parameters["cc_1"].history == [0.55, 0.3]
    assert p.parameters["cc_2"].history == []  # untouched since the state: left to the state
    h.set_state(b"other")
    p.parameters["cc_1"].history.clear()
    h.render(_x(64), reset=True)
    assert p.parameters["cc_1"].history == []  # a new state forgets older knobs


def test_set_params_always_sends_even_an_unchanged_value():
    h, p = _host()
    p.parameters["cc_2"].raw_value = 0.8
    p.parameters["cc_2"].history.clear()
    h.set_params({"cc_2": 0.8})
    assert p.parameters["cc_2"].history == [0.55, 0.8]


def test_state_reset_pads_to_a_whole_native_frame_and_beat(monkeypatch):
    """Native runs 16-sample frames behind a FIFO and its modulation on a free-running 120 BPM
    beat clock; renders are history-independent only aligned to both."""
    monkeypatch.setattr(nh, "FRAME_ENV", None)
    assert nh.reset_frame(48_000) == 24_000 and nh.reset_frame(44_100) == 176_400
    h, p = _host()
    h.render(_x(1001), reset=True)  # leaves the count off-frame
    p.calls.clear()
    h.render(_x(1000), reset=True)
    pad = (-1001) % nh.reset_frame(h.sr)
    assert p.calls[0] == (pad, False)
    assert (1001 + pad) % 24_000 == 0 and (1001 + pad) % nh.NATIVE_FRAME == 0


def test_reinstantiate_strategy_and_latent_plugins_use_pedalboard_reset():
    h, p = _host("reinstantiate")
    hdr, _ = h.render(_x(100), reset=True)
    assert hdr["reset_via"] == "pedalboard" and p.calls[-1] == (100, True) and h.instances == 1
    h, p = _host()
    p.reported_latency_samples = 64  # pedalboard trims latency only after a real reset
    hdr, _ = h.render(_x(100), reset=True)
    assert hdr["reset_via"] == "pedalboard" and p.calls[-1] == (100, True)


def test_flush_render_neither_resets_nor_reloads():
    h, p = _host()
    h.set_state(b"template")
    h.render(_x(100), reset=False)
    assert p.calls == [(100, False)] and p.loads == [b"template"]


def test_channel_probe_does_not_reset():
    h, p = _host()
    assert h._probe_channels() == 1
    assert p.calls == [(h.block, False)]


def test_unknown_strategy_refused():
    h, _ = _host()
    with pytest.raises(ValueError):
        h.set_reset_strategy("nope")


def test_mem_mb_reports_this_process():
    m = nh.mem_mb()
    assert m is None or m > 1.0


# --------------------------------------------------------------------------- engine watchdog


class FakeHostProcess:
    """HostProcess stand-in: a footprint that grows per render and drops on restart."""

    def __init__(self):
        self.info = {"has_state": True, "reset_strategy": "state", "params": []}
        self.mem_mb = 2000.0
        self.restarts = 0
        self.log: list[tuple] = []

    def call(self, cmd, payload=b"", **kw):
        self.log.append((cmd, payload, kw))
        if cmd == "render":
            self.mem_mb += 400.0
            return {}, payload
        return {}, b""

    def restart(self):
        self.restarts += 1
        self.mem_mb = 2000.0
        self.log.append(("restart", b"", {}))


def _engine(limit: float) -> tuple[nat.NativeEngine, FakeHostProcess]:
    e = nat.NativeEngine.__new__(nat.NativeEngine)
    host = FakeHostProcess()
    e.host, e.plugin, e.plugin_name = host, "fake.vst3", None
    e.mem_limit_mb, e.recycles = limit, 0
    e._initial_state, e._hc_mode = b"", None
    e._loaded_state, e._host_params = None, {}
    e.current_chain, e.current_template = None, None
    e._latency, e._plugin_latency = {None: 0}, 0
    e.preroll, e.reset_mode, e.flush_s, e.calibration_db = 0, "reset", 0.0, 0.0
    return e, host


def test_watchdog_recycles_and_restores_state_and_knobs():
    e, host = _engine(limit=3000.0)
    e._set_state(b"chain-state")
    host.call("set_params", values={"cc_1": 0.4})
    e._host_params = {"cc_1": 0.4}
    x = np.full(480, 0.1, np.float32)
    for _ in range(4):
        e.render(x)
    assert host.restarts == 1 and e.recycles == 1
    after = host.log[[c for c, *_ in host.log].index("restart") + 1:]
    cmds = [(c, p) for c, p, _ in after[:2]]
    assert cmds[0] == ("set_state", b"chain-state")
    assert after[1][0] == "set_params" and after[1][2]["values"] == {"cc_1": 0.4}


def test_watchdog_raises_limit_when_a_fresh_host_is_already_near_it():
    e, host = _engine(limit=2200.0)
    host.mem_mb = 2500.0
    assert e.check_memory() is True
    assert e.mem_limit_mb == pytest.approx(2000.0 + 2048)
    assert e.check_memory() is False


def test_watchdog_disabled_with_zero_limit():
    e, host = _engine(limit=0.0)
    host.mem_mb = 1e9
    assert e.check_memory() is False and host.restarts == 0


def test_parallel_scan_is_capped_by_memory(monkeypatch):
    monkeypatch.setenv("TONEMATCH_NATIVE_MAX_HOSTS", "3")
    assert nat.max_hosts_by_memory() == 3
    monkeypatch.delenv("TONEMATCH_NATIVE_MAX_HOSTS")
    assert nat.max_hosts_by_memory() >= 1


def test_memory_cap_allows_six_hosts_on_32_gb(monkeypatch):
    """2.5 GB per host, half of RAM: 6 hosts on 32 GB (human decision 2026-10-16)."""
    monkeypatch.delenv("TONEMATCH_NATIVE_MAX_HOSTS", raising=False)
    pages = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 32 * 2**30 // 4096}
    monkeypatch.setattr(nat.os, "sysconf", lambda k: pages[k])
    assert nat.max_hosts_by_memory() == 6


# --------------------------------------------------------------------------- reviewer follow-ups


@pytest.mark.parametrize(("v", "expect"), [(0.0, [0.25, 0.0]), (1.0, [0.75, 1.0])])
def test_edge_values_are_forced_on_set_params_and_on_replay(v, expect):
    h, p = _host()
    prm = p.parameters["cc_1"]
    prm.raw_value = v  # the cache already holds the value, as after a state load
    prm.history.clear()
    h.set_params({"cc_1": v})
    assert prm.history == expect
    prm.history.clear()
    h.render(_x(32), reset=True)
    assert prm.history == expect


def test_watchdog_never_lowers_a_user_limit():
    e, host = _engine(limit=20000.0)
    host.mem_mb = 25000.0

    def restart():  # a fresh host that is itself big, but still under limit - 2048
        host.restarts += 1
        host.mem_mb = 17000.0
    host.restart = restart
    assert e.check_memory() is True
    assert e.mem_limit_mb == 20000.0


def test_last_reset_via_is_surfaced(monkeypatch):
    e, host = _engine(limit=0.0)
    host.call = lambda cmd, payload=b"", **kw: ({"reset_via": "state"}, payload)
    e._raw_render(np.ones(16, np.float32))
    assert e.last_reset_via == "state"


def test_failed_open_kills_the_spawned_host(monkeypatch):
    killed = []

    class Proc:
        pid, returncode = 1, None
        stdin = stdout = None

        def poll(self):
            return None

        def kill(self):
            killed.append(True)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(nat.subprocess, "Popen", lambda *a, **k: Proc())

    def bad_call(self, cmd, payload=b"", **kw):
        raise nat.EngineError("Native host 'open' failed: boom")
    monkeypatch.setattr(nat.HostProcess, "call", bad_call)
    with pytest.raises(nat.EngineError):
        nat.HostProcess("x.vst3")
    assert killed == [True]


# --------------------------------------------------------------------------- T051 snapshot values


def _snap_preset():
    from tonematch.preset import from_dict

    raw = {"data": {"meta": {"name": "S"}, "device": 2162694, "tone": {
        "dsp0": {"block5": {"@model": "HD2_DL4PingPong", "@position": 2, "@enabled": True,
                            "Mix": 0.25, "Feedback": 0.4}},
        "snapshot0": {"@name": "A", "@valid": True, "controllers": {"dsp0": {"block5": {
            "Mix": {"@fs_enabled": False, "@value": 0.23}, "Feedback": {"@fs_enabled": False, "@value": 0.52}}}}},
        "snapshot1": {"@name": "B", "@valid": True, "controllers": {"dsp0": {"block5": {
            "Mix": {"@fs_enabled": False, "@value": 0.25}}}}}}}}
    return from_dict(raw)


def test_snapshot_mismatches_lists_only_disagreeing_values():
    m = nat.snapshot_mismatches(_snap_preset())
    assert len(m) == 2 and all(x.startswith("snapshot0:block5:") for x in m)


def test_sync_makes_block_values_win_in_objects_and_in_the_document():
    p = _snap_preset()
    assert nat.snapshot_mismatches(nat.sync_snapshots_to_blocks(p)) == []
    assert nat.snapshot_mismatches(p)  # the original is untouched
    d = nat.sync_hlx_snapshots(p.to_hlx_dict())
    c = d["data"]["tone"]["snapshot0"]["controllers"]["dsp0"]["block5"]
    assert c["Mix"]["@value"] == 0.25 and c["Feedback"]["@value"] == 0.4
