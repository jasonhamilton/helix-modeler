"""Host canary: a Native host that renders differently from the machine's reference is replaced
(about 1 host in 10 does, 2026-10-15). Fake hosts only: no Native, no pedalboard."""
from __future__ import annotations

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines.base import EngineError

A = np.sin(np.arange(2400, dtype=np.float32) / 7.0).astype(np.float32)
B = (A * np.float32(1.001)).astype(np.float32)  # "mode B": slightly different everywhere (as _host)


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path / "cache"))


def _host(modes):
    it = iter(modes)

    class Fake(nat.HostProcess):
        def _spawn_process(self):
            self.mode = next(it)
            self.info = {"has_state": True}

        def _canary_render(self):
            if self.mode == "A":
                return A.copy()
            return (A * np.float32(1 + 0.001 * (ord(self.mode) - 65))).astype(np.float32)

        def close(self):
            pass

    return Fake("fake.vst3", initial_state=b"START", canary=True)


def test_first_reference_needs_two_agreeing_hosts():
    h = _host("BAA")  # the very first host is odd: it must not become the reference
    assert h.mode == "A" and h.canary_respawns == 2
    h2 = _host("A")
    assert h2.canary_respawns == 0 and h2.canary_null_db < -200


def test_an_odd_host_is_replaced():
    _host("AA")
    h = _host("BA")
    assert h.mode == "A" and h.canary_respawns == 1


def test_the_majority_wins_even_if_odd_hosts_came_first():
    _host("BB")  # two odd hosts in a row made the first reference (votes B=2)
    h = _host("AAA")  # A=1 loses, A=2 ties (oldest wins), A=3 is the majority
    assert h.mode == "A" and h.canary_respawns == 2
    assert _host("A").canary_respawns == 0
    assert _host("BA").canary_respawns == 1  # B=3 < A=4


def test_votes_persist_on_disk():
    _host("AA")
    f = nat._canary_file("fake.vst3", b"START")
    assert [v["n"] for v in nat._canary_votes(f)] == [2]


def test_gives_up_after_retries():
    _host("AA")
    with pytest.raises(EngineError, match="canary"):
        _host("BCDEFGH")  # seven odd hosts in a row, none agreeing with another


def test_no_canary_without_a_start_state_or_for_builtins(monkeypatch):
    called = []
    monkeypatch.setattr(nat.HostProcess, "_spawn_process", lambda self: called.append(1))
    monkeypatch.setattr(nat.HostProcess, "_check_canary", lambda self: called.append("canary"))
    nat.HostProcess("fake.vst3")
    nat.HostProcess("builtin:Gain", initial_state=b"S")
    assert "canary" not in called
    monkeypatch.setenv("TONEMATCH_NATIVE_CANARY", "0")
    nat.HostProcess("fake.vst3", initial_state=b"S")
    assert "canary" not in called
