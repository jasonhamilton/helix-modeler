"""playalong_record: offline helpers only (no audio device)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import playalong_record as P


def test_take_frames_shifts_by_latency_and_pads():
    rec = np.arange(20, dtype=np.float32).reshape(10, 2)
    out = P.take_frames(rec, latency=3, n=10)
    assert out.shape == (10, 2) and out[0, 0] == 6 and np.all(out[7:] == 0)


def test_level_report_flags_silence_clipping_and_usb_input():
    quiet = np.full(100, 1e-4, np.float32)
    loud = np.full(100, 0.999, np.float32)
    ok = np.full(100, 0.3, np.float32)
    assert "silent" in P.level_report(quiet, ok)[0]
    assert "clipped" in P.level_report(loud, ok)[0]
    assert "USB 5/6" in P.level_report(ok, np.zeros(100, np.float32))[0]
    assert P.level_report(ok, ok) == []
