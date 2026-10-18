"""scripts/stomp_robust.py offline: the registered T112 truth, threshold and verdict, and phase_t112
end to end on a fake engine (slips injected). No pedal."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stomp_robust as R

from tonematch.engines import stomp as ST

SR = 48_000


def sig(n=SR, seed=1):
    return (np.random.default_rng(seed).standard_normal(n) * 0.05).astype(np.float32)


def test_truth_flags_a_slip_and_passes_a_repeat():
    r = sig()
    assert not R.truth(r, r + 1e-9, -140.0)["slipped"]
    y = np.concatenate([r[:20000], r[20001:], [0.0]]).astype(np.float32)
    assert R.truth(r, y, -140.0)["slipped"]


def test_theta_rule():
    dev = [{"worst_db": -112.0, "truth": {"slipped": False}}, {"worst_db": -108.0, "truth": {"slipped": False}},
           {"worst_db": -25.0, "truth": {"slipped": True}}]
    th = R.theta_from(dev)
    assert th["theta_db"] == -88.0 and th["dev_missed"] == 0 and th["dev_false"] == 0
    assert R.theta_from([{"worst_db": -30.0, "truth": {"slipped": False}}])["theta_db"] == R.THETA_CAP_DB


def test_slot_struct_reads_class_and_counts():
    s = "[{19: 0}, {19: 6, 20: {24: {23: false, 25: 633, 26: -1}, 9: 24, 10: true, 11: {2: 18, 3: 18, 4: []}}}]"
    assert R._slot_struct(s, 633) == (24, 18, 18) and R._slot_struct(s, 1) is None


class FakeEngine:
    """Enough of StompEngine for phase_t112: renders y = 0.5 x, slipped on the renders in ``slips``;
    the loopback verdict is computed with the real slip_check on a matching fake loopback."""

    PRE_S = 1.0

    def __init__(self, slips):
        self.slips = set(slips)
        self.n = 0
        self.slip_guard = "on"
        self.whole_preset = "fallback"
        self._pilot_on_pedal = False
        self.pilot_reason = None
        self.keep_takes = False
        self.takes = []
        self.slip_threshold_db = ST.SLIP_THRESHOLD_DB
        self.slip_retakes = ST.SLIP_RETAKES
        self.slip_stats = {"retakes": 0}
        self.stats_calls = 0

    def load(self, preset):
        self._pilot_on_pedal = self.slip_guard != "off"

    def stats(self):
        return {"slip_guard": dict(self.slip_stats)}

    def _take(self, x):
        k = self.n
        self.n += 1
        slipped = k in self.slips
        y = 0.5 * x
        if slipped:
            y = np.concatenate([y[:15000], y[15001:], [0.0]]).astype(np.float32)
        s = np.concatenate([x, ST.slip_marker()])
        lb = np.zeros(1000 + 1207 + len(s) + 100)
        ss = np.concatenate([s[:15000], s[15001:], [0.0]]) if slipped else s
        lb[1000 + 1207:1000 + 1207 + len(s)] = ss
        v = ST.slip_check(lb, s, 1000, 1207, threshold_db=self.slip_threshold_db)
        return y.astype(np.float32), v

    def render(self, x):
        if not self._pilot_on_pedal:
            return 0.5 * x
        for k in range(self.slip_retakes + 1):
            y, v = self._take(x)
            v["take"] = k
            if self.keep_takes:
                self.takes.append({"render": self.n, "take": k, "y": y, "verdict": v, "offset": 0})
            if v["ok"]:
                return y
            self.slip_stats["retakes"] += 1
        raise ST.StreamSlip("all takes slipped", {"verdicts": []})


@pytest.mark.parametrize("slips", [[], [12, 40, 41, 200, 300]])
def test_phase_t112_end_to_end_on_a_fake_engine(monkeypatch, tmp_path, slips):
    monkeypatch.setattr(R, "D_TAKES", 30)
    monkeypatch.setattr(R, "P_RENDERS", 40)
    monkeypatch.setattr(R, "s808_preset", lambda cat: object())
    # Render indices: 3 singles are not takes (guard off); the opening reference pair then D and P.
    eng = FakeEngine([s + 0 for s in slips])
    log: dict = {}
    R.phase_t112(eng, {}, sig(), tmp_path, log)
    t = log["t112"]
    assert t["pilot_on_pedal"] and t["theta"]["theta_db"] is not None
    (tmp_path / "session.json").write_text(json.dumps(log, default=str))
    v = R.analyse(tmp_path)["t112"]
    assert v["renders"] == 40 and v["escapes"] == 0 and v["false_retakes"] == 0
    assert v["pass"] is True
    assert v["fn"] == 0 and v["fp"] == 0
