"""reamp.duplex never blocks without a limit (T095). A fake sounddevice module; no audio device."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from tonematch import reamp


def fake_sd(active_forever: bool):
    sd = types.ModuleType("sounddevice")
    state = {"stopped": False, "polls": 0}

    class Stream:
        @property
        def active(self):
            state["polls"] += 1
            return active_forever and not state["stopped"] or (not active_forever and state["polls"] < 3)

    sd.query_devices = lambda dev: {"max_output_channels": 8, "max_input_channels": 8}
    sd.playrec = lambda buf, **k: np.zeros((len(buf), 8), np.float32)
    sd.get_stream = lambda: Stream()
    sd.stop = lambda: state.update(stopped=True)
    sd.wait = lambda: None
    return sd, state


def test_a_stream_that_never_ends_raises(monkeypatch):
    sd, state = fake_sd(active_forever=True)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(reamp, "STREAM_GRACE_S", 0.05)
    with pytest.raises(reamp.ReampError, match="did not finish"):
        reamp.duplex(np.zeros((480, 2), np.float32), (5, 6), device=0)
    assert state["stopped"]


def test_a_normal_stream_returns_every_input(monkeypatch):
    sd, state = fake_sd(active_forever=False)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    rec = reamp.duplex(np.zeros((480, 2), np.float32), (5, 6), device=0)
    assert rec.shape == (480, 8) and not state["stopped"]
