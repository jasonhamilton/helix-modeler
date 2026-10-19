"""stomp_live_preview (T166): throttle, latest-wins, consistency check, restore on exit. No pedal."""
from __future__ import annotations

import contextlib
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stomp_live_preview as L


def write_best(d: Path, seq: int, model: str = "HD2_DistScream808", loss: float = 1.0,
               hlx_seq: int | None = None, values: dict | None = None) -> None:
    hs = seq if hlx_seq is None else hlx_seq
    (d / "current_best.hlx").write_text(json.dumps(
        {"data": {"meta": {"name": f"Drive match s{hs}"},
                  "tone": {"dsp0": {"inputA": {"@input": 1}, "block0": {"@model": model, "@position": 0}}}}}))
    (d / "current_best.json").write_text(json.dumps(
        {"seq": seq, "loss": loss, "model": model, "category": "drive",
         "values": values or {"Drive": 0.5, "Tone": 0.4, "Level": 0.6}}))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeLoader:
    def __init__(self, fail_on: set[int] | None = None):
        self.loaded: list[int] = []
        self.fail_on = fail_on or set()

    def __call__(self, best):
        if best.seq in self.fail_on:
            raise RuntimeError("device refused")
        self.loaded.append(best.seq)
        return {"load_s": 0.4, "readback_ok": True}


def make(tmp_path, loader=None, throttle=5.0):
    clock, lines = Clock(), []
    lp = L.LivePreview(tmp_path, loader or FakeLoader(), throttle_s=throttle, clock=clock, out=lines.append)
    return lp, clock, lines


def test_waits_when_no_file(tmp_path):
    lp, _, lines = make(tmp_path)
    assert lp.step() is None and lp.step() is None
    assert lp.loads == [] and sum("waiting" in x for x in lines) == 1


def test_loads_once_per_seq_and_prints_line(tmp_path):
    loader = FakeLoader()
    lp, clock, lines = make(tmp_path, loader)
    write_best(tmp_path, 1, loss=2.5)
    lp.step()
    clock.t = 100
    lp.step()                      # same seq: no reload
    assert loader.loaded == [1]
    line = [x for x in lines if x.startswith("seq 1")][0]
    assert "loss 2.5000" in line and "HD2_DistScream808" in line and "Drive=0.500" in line


def test_throttle_and_latest_wins(tmp_path):
    loader = FakeLoader()
    lp, clock, _ = make(tmp_path, loader, throttle=5.0)
    write_best(tmp_path, 1)
    lp.step()
    for i, seq in enumerate((2, 3, 4)):  # a burst inside the throttle window
        clock.t = 1.0 + i
        write_best(tmp_path, seq)
        lp.step()
    assert loader.loaded == [1]
    clock.t = 4.9
    lp.step()
    assert loader.loaded == [1]
    clock.t = 5.0
    lp.step()
    assert loader.loaded == [1, 4] and lp.skipped == 2   # 2 and 3 never loaded
    clock.t = 20
    lp.step()
    assert loader.loaded == [1, 4]


def test_hlx_ahead_of_json_is_not_loaded(tmp_path):
    loader = FakeLoader()
    lp, _, _ = make(tmp_path, loader)
    write_best(tmp_path, 3, hlx_seq=4)   # the writer renamed the .hlx, not yet the json
    lp.step()
    assert loader.loaded == []
    write_best(tmp_path, 4)
    lp.step()
    assert loader.loaded == [4]


def test_refused_load_is_skipped_not_retried(tmp_path):
    loader = FakeLoader(fail_on={1})
    lp, clock, lines = make(tmp_path, loader)
    write_best(tmp_path, 1)
    lp.step()
    clock.t = 10
    lp.step()
    assert loader.loaded == [] and len(lp.refused) == 1 and any("NOT loaded" in x for x in lines)
    write_best(tmp_path, 2)
    lp.step()
    assert loader.loaded == [2]


def test_fatal_bridge_error_propagates(tmp_path):
    from tonematch.engines.stomp_bridge import BridgeError

    def boom(best):
        raise BridgeError("helper died")
    lp, _, _ = make(tmp_path, boom)
    write_best(tmp_path, 1)
    try:
        lp.step()
    except BridgeError:
        pass
    else:
        raise AssertionError("BridgeError swallowed")


def test_first_load_hook_once(tmp_path):
    calls = []
    lp = L.LivePreview(tmp_path, FakeLoader(), throttle_s=0, clock=Clock(), out=lambda s: None,
                       on_first_load=lambda b, r: calls.append(b.seq))
    for seq in (1, 2):
        write_best(tmp_path, seq)
        lp.step()
    assert calls == [1] and len(lp.loads) == 2


def test_run_once_holds_after_first_load(tmp_path):
    loader = FakeLoader()
    lp, _, _ = make(tmp_path, loader, throttle=0)
    write_best(tmp_path, 1)
    n = [0]

    def sleep(_):
        n[0] += 1
        if n[0] == 2:
            write_best(tmp_path, 2)
    lp.run(poll_s=0, once=True, sleep=sleep, stop=lambda: n[0] >= 5)
    assert loader.loaded == [1]


# --------------------------------------------------------------------------- main(): restore on exit


class FakeBridge:
    release_report = {"ok": True}

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, events):
        self.events = events
        self.original = {"setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"}
        self.info = {"firmware": "3.80"}
        self.backup_path = Path("x_editbuffer.bin")
        self.bridge = FakeBridge()
        self.restore_report = {}
        self.close_args = None

    def close(self, verify_bytes=False):
        self.close_args = verify_bytes
        self.events.append(("close", verify_bytes, L.pedal_lock_state["held"]))
        self.restore_report = {"restore_ok": True, "bytes_equal_backup": True, "decoded_equal_backup": True}


def patch_main(monkeypatch, tmp_path, events, position=None, run_raises=KeyboardInterrupt):
    from tonematch import pedal_lock

    L.pedal_lock_state = {"held": False}

    @contextlib.contextmanager
    def session(purpose, **kw):
        L.pedal_lock_state["held"] = True
        events.append(("lock", purpose))
        try:
            yield None
        finally:
            L.pedal_lock_state["held"] = False
            events.append(("unlock",))
    monkeypatch.setattr(pedal_lock, "session", session)
    monkeypatch.setattr(pedal_lock, "held_here", lambda *a: L.pedal_lock_state["held"])
    monkeypatch.setattr(L, "hx_edit_running", lambda: False)
    monkeypatch.setattr(L, "pedal_position", lambda: position or {
        "firmware": "3.80", "setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"})
    engines = []

    def open_engine(run_dir):
        e = FakeEngine(events)
        engines.append(e)
        return e
    monkeypatch.setattr(L, "open_engine", open_engine)
    monkeypatch.setattr(L, "stomp_loader", lambda eng, cat: FakeLoader())
    monkeypatch.setattr("tonematch.catalog.load_catalog", lambda: {})

    def run(self, **kw):
        raise run_raises("stop")
    monkeypatch.setattr(L.LivePreview, "run", run)
    return engines


def _restore_signals():
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, signal.default_int_handler if s == signal.SIGINT else signal.SIG_DFL)


def test_ctrl_c_restores_inside_the_lock(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events)
    try:
        rc = L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run")])
    finally:
        _restore_signals()
    assert rc == 0
    assert engines[0].close_args is True
    assert ("close", True, True) in events                      # restored while the lock was held
    assert events.index(("close", True, True)) < events.index(("unlock",))
    s = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert s["restore_ok"] is True and s["lock_released"] is True


def test_sigterm_path_restores(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events, run_raises=L._Stop)
    try:
        L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run")])
    finally:
        _restore_signals()
    assert engines[0].close_args is True


def test_crash_still_restores(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events, run_raises=RuntimeError)
    try:
        rc = L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run")])
    finally:
        _restore_signals()
    assert rc == 1 and engines[0].close_args is True


def test_no_restore_on_exit_leaves_buffer(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events)
    try:
        L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run"), "--no-restore-on-exit"])
    finally:
        _restore_signals()
    e = engines[0]
    assert e.close_args is None and e._closed is True and e.bridge.closed


def test_refuses_off_scratch_without_opening(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events, position={
        "firmware": "3.80", "setlist": 0, "index": 0, "label": "01A", "name": "Song"})
    try:
        rc = L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run")])
    finally:
        _restore_signals()
    assert rc == 3 and engines == []


def test_refuses_with_hx_edit(monkeypatch, tmp_path):
    events = []
    engines = patch_main(monkeypatch, tmp_path, events)
    monkeypatch.setattr(L, "hx_edit_running", lambda: True)
    try:
        rc = L.main(["--dir", str(tmp_path), "--run-dir", str(tmp_path / "run")])
    finally:
        _restore_signals()
    assert rc == 2 and engines == [] and events == []


def test_open_engine_routes_guitar_input(monkeypatch, tmp_path):
    import tonematch.engines.stomp as S
    from tonematch import reamp

    class Eng:
        def __init__(self, **kw):
            self.kw = kw
            self.cal = reamp.Calibration(latency_samples=1, measurements=[1], level_gain_db=0.0,
                                         tone_in_dbfs=0.0, tone_out_dbfs=0.0, firmware="3.80")
    monkeypatch.setattr(S, "StompEngine", Eng)
    e = L.open_engine(tmp_path)
    assert e.cal.input_usb56 == L.INPUT_MAIN_LR == 1
    assert e.kw["sentinel"] is False and e.kw["slip_guard"] == "off"   # no pilot block, no renders
