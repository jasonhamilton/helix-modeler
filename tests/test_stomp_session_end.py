"""T059: how a Stomp control session ends. HX Edit's session-close (reported by the helper as a
`closed` event) and the software USB reset fallback, with a fake helper process. No pedal."""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tonematch import reamp
from tonematch.engines import stomp_bridge as sb
from tonematch.engines.stomp_bridge import BridgeError, PowerCycleNeeded, StompBridge

# Fake stompd. Session mode: ready, then JSON lines; `quit` -> bye, then (unless FAKE_CLOSED=none)
# the `closed` report; EOF on stdin also ends the session with the report, like the real helper.
# `reset <wait>` mode: logs and prints the reset result (FAKE_REAPPEAR=0 -> not back).
FAKE = """#!{python}
import json, os, sys, time
log = open(os.environ["FAKE_LOG"], "a")
def say(m):
    print(json.dumps(m), flush=True)
def closed():
    mode = os.environ.get("FAKE_CLOSED", "sent")
    if mode == "none":
        return
    if mode == "skipped":
        say({{"ok": True, "event": "closed", "release": True, "release_sent": False,
             "skipped": "a write had timed out: the device is not reading", "ms": 1}})
        return
    sent = mode == "sent"
    say({{"ok": True, "event": "closed", "release": sent, "release_sent": sent,
         "acks": {{"events": True, "data": True, "control": True}} if sent else {{}}, "ms": 900}})
    log.write("closed %s\\n" % mode); log.flush()
if sys.argv[1:2] == ["reset"]:
    log.write("reset %s %.3f\\n" % (sys.argv[2], time.time())); log.flush()
    back = os.environ.get("FAKE_REAPPEAR", "1") == "1"
    say({{"ok": True, "reset": True, "reappeared": back, "ms": 1200}})
    sys.exit(0)
say({{"ok": True, "event": "ready", "presets": 126, "teardown": "session-close", "release": True}})
for line in sys.stdin:
    cmd = json.loads(line)["cmd"]
    log.write("cmd %s %.3f\\n" % (cmd, time.time())); log.flush()
    if cmd == "info" and os.environ.get("FAKE_INFO_HANGS"):
        time.sleep(30)
    if cmd == "chain":
        time.sleep(1.0)  # answers late
    if cmd == "quit":
        say({{"ok": True, "bye": True}})
        closed()
        sys.exit(0)
    say({{"ok": True, "cmd": cmd}})
log.write("eof\\n"); log.flush()
closed()
"""


@pytest.fixture
def helper(tmp_path, monkeypatch):
    exe = tmp_path / "stompd"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "log.txt"
    log.write_text("")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv(sb.ENV_USB_RESET, raising=False)

    def lines() -> list[str]:
        return log.read_text().splitlines()

    return SimpleNamespace(exe=exe, lines=lines)


def idle() -> bool:
    return False


def resets(helper) -> list[str]:
    return [ln for ln in helper.lines() if ln.startswith("reset")]


def test_default_mode_is_auto_and_env_and_aliases(monkeypatch):
    monkeypatch.delenv(sb.ENV_USB_RESET, raising=False)
    assert sb.usb_reset_mode() == "auto"
    monkeypatch.setenv(sb.ENV_USB_RESET, "0")
    assert sb.usb_reset_mode() == "off"
    monkeypatch.setenv(sb.ENV_USB_RESET, "Always")
    assert sb.usb_reset_mode() == "always"
    assert sb.usb_reset_mode("off") == "off"            # the argument wins over the env
    with pytest.raises(BridgeError, match="must be one of"):
        sb.usb_reset_mode("sometimes")


def test_released_session_is_not_reset_in_auto(helper):
    with StompBridge(binary=helper.exe, audio_busy=idle) as b:
        b.call("info")
    assert b.released and b.release_report["acks"]["control"] is True
    assert b.reset_report == {"mode": "auto", "released": True, "reset": False,
                              "why": "the helper sent the session-close; no reset needed"}
    assert resets(helper) == []


def test_old_helper_without_release_is_reset_after_the_orderly_quit(helper, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSED", "none")       # a helper built before T059
    with StompBridge(binary=helper.exe, audio_busy=idle) as b:
        b.call("info")
    assert not b.released
    assert b.reset_report["reset"] is True and b.reset_report["reappeared"] is True
    lines = helper.lines()
    assert [ln.split()[:2] for ln in lines[:3]] == [["cmd", "info"], ["cmd", "info"], ["cmd", "quit"]]
    assert lines[3].startswith("reset") and len(lines) == 4             # health info, info, quit, then reset
    assert lines[-1].startswith(f"reset {StompBridge.RESET_WAIT_S:g}")
    quit_t = [float(ln.split()[2]) for ln in lines if ln.startswith("cmd")][-1]
    assert float(lines[-1].split()[2]) >= quit_t


def test_release_not_sent_is_reset_in_auto(helper, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSED", "off")        # STOMPD_NO_RELEASE / quit release=false
    with StompBridge(binary=helper.exe, audio_busy=idle):
        pass
    assert len(resets(helper)) == 1


def test_always_resets_even_after_a_release(helper):
    with StompBridge(binary=helper.exe, usb_reset="always", audio_busy=idle) as b:
        pass
    assert b.released and b.reset_report["reset"] is True
    assert len(resets(helper)) == 1


def test_off_switch_never_resets(helper, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSED", "none")
    monkeypatch.setenv(sb.ENV_USB_RESET, "off")
    with StompBridge(binary=helper.exe, audio_busy=idle) as b:
        pass
    assert b.reset_report == {"mode": "off", "released": False, "reset": False, "why": "off"}
    assert resets(helper) == []


def test_close_is_idempotent_one_reset(helper):
    b = StompBridge(binary=helper.exe, usb_reset="always", audio_busy=idle)
    b.close()
    b.close()
    assert len(resets(helper)) == 1


def test_never_resets_while_audio_streams(helper, monkeypatch):
    monkeypatch.setattr(StompBridge, "AUDIO_WAIT_S", 0.3)
    b = StompBridge(binary=helper.exe, usb_reset="always", audio_busy=lambda: True)
    b.close()
    assert b.reset_report["reset"] is False and "audio streaming" in b.reset_report["why"]
    assert resets(helper) == []


def test_reset_waits_for_the_stream_to_end(helper):
    state = {"busy": True, "freed_at": 0.0}

    def free_later() -> None:
        time.sleep(0.4)
        state["freed_at"] = time.time()
        state["busy"] = False

    b = StompBridge(binary=helper.exe, usb_reset="always", audio_busy=lambda: state["busy"])
    threading.Thread(target=free_later).start()
    b.close()
    (line,) = resets(helper)
    assert float(line.split()[2]) >= state["freed_at"] > 0


def test_reamp_streams_count_as_audio(helper, monkeypatch):
    """The default audio check is tonematch.reamp's live-stream counter."""
    seen = []

    class FakeSd:
        @staticmethod
        def query_devices(dev=None):
            return {"max_output_channels": 8, "max_input_channels": 8}

        @staticmethod
        def playrec(buf, **kw):
            seen.append(reamp.audio_active())
            return buf[:, :8]

        @staticmethod
        def stop():
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)
    assert reamp.audio_active() is False
    import numpy as np

    reamp.duplex(np.zeros((10, 2), np.float32), (5, 6), device=0)
    assert seen == [True] and reamp.audio_active() is False
    assert sb._reamp_audio_active() is False


def test_refresh_audio_devices_rescans_only_when_idle(monkeypatch):
    calls = []

    class FakeSd:
        _terminate = staticmethod(lambda: calls.append("t"))
        _initialize = staticmethod(lambda: calls.append("i"))

    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)
    assert reamp.refresh_audio_devices() is True and calls == ["t", "i"]
    with reamp._streaming():
        assert reamp.refresh_audio_devices() is False
    monkeypatch.delitem(sys.modules, "sounddevice")
    assert reamp.refresh_audio_devices() is False


def test_reset_that_loses_the_pedal_tells_the_user_to_replug(helper, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_REAPPEAR", "0")
    with StompBridge(binary=helper.exe, usb_reset="always", audio_busy=idle) as b:
        pass
    assert b.reset_report["reset"] is True and b.reset_report["reappeared"] is False
    assert "Unplug its USB cable" in capsys.readouterr().err


def test_wedged_pedal_is_never_reset(helper, monkeypatch):
    monkeypatch.setenv("FAKE_INFO_HANGS", "1")
    monkeypatch.setattr(StompBridge, "KILL_GRACE_S", 0.2)
    with pytest.raises(PowerCycleNeeded):
        StompBridge(binary=helper.exe, usb_reset="always", audio_busy=idle)
    assert resets(helper) == []


def test_helper_reporting_a_skipped_close_is_not_reset(helper, monkeypatch):
    monkeypatch.setenv("FAKE_CLOSED", "skipped")    # the device stopped reading: a wedge
    with StompBridge(binary=helper.exe, usb_reset="always", audio_busy=idle) as b:
        pass
    assert "wedged" in b.reset_report["why"] and resets(helper) == []


def test_kill_lets_the_helper_send_the_session_close(helper):
    """A timed-out request kills the helper, but stdin is closed first so it still ends the
    session properly (the real helper sends the close once the command in flight returns)."""
    b = StompBridge(binary=helper.exe, audio_busy=idle)
    with pytest.raises(BridgeError, match="did not answer"):
        b.call("chain", timeout_s=0.3)
    assert b._proc.poll() is not None
    b.close()
    assert "eof" in helper.lines() and b.released
    assert b.reset_report["reset"] is False


def test_scripts_switch_sets_the_env_default(monkeypatch):
    import argparse

    monkeypatch.delenv(sb.ENV_USB_RESET, raising=False)
    p = argparse.ArgumentParser()
    sb.add_usb_reset_arg(p)
    sb.apply_usb_reset_arg(p.parse_args([]))
    assert sb.usb_reset_mode() == "auto"
    sb.apply_usb_reset_arg(p.parse_args(["--usb-reset", "off"]))
    assert sb.usb_reset_mode() == "off"


@pytest.mark.parametrize("script", ["reamp_capture", "parity_null", "stomp_inject"])
def test_scripts_offer_the_off_switch(script):
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    args = {"reamp_capture": [], "parity_null": ["run"], "stomp_inject": ["run"]}[script]
    out = subprocess.run([sys.executable, str(root / "scripts" / f"{script}.py"), *args, "--help"],
                         capture_output=True, text=True, timeout=60, check=False)
    assert "--usb-reset" in out.stdout, out.stderr[-500:]


def test_fake_report_shape_matches_the_helper_note():
    """The `closed` event fields the bridge reads are the ones the real helper prints."""
    msg = json.loads('{"ok": true, "event": "closed", "release": true, "release_sent": true, '
                     '"acks": {"events": true, "data": true, "control": false}, "ms": 812}')
    b = StompBridge.__new__(StompBridge)
    b.release_report = msg
    assert b.released is True


def test_release_check_runs_read_only_sessions_and_reports_the_close(helper, monkeypatch, capsys):
    import argparse
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("stomp_hw", root / "scripts" / "stomp_hw.py")
    hw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hw)
    monkeypatch.setenv(sb.ENV_HELPER, str(helper.exe))
    a = argparse.Namespace(sessions=2, gap_s=0.0, chain=False, usb_reset="off")
    assert hw.cmd_release_check(a) == 0
    recs = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [r["session"] for r in recs] == [1, 2]
    assert all(r["release"]["release_sent"] and r["usb_reset"]["reset"] is False for r in recs)
    cmds = [ln.split()[1] for ln in helper.lines() if ln.startswith("cmd")]
    assert cmds == ["info", "info", "quit"] * 2           # health check, info, quit: nothing written
