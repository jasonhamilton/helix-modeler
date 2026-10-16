"""The shared pedal lock and the HX Stomp device check (T090). Fake process tables and clocks only."""
from __future__ import annotations

import json
import os
import sys

import pytest

from tonematch import pedal_lock as pl


class FakeProcs(pl.ProcessTable):
    host = "testhost"

    def __init__(self, alive=(), stompd_seq=None):
        self.alive_pids = set(alive) | {os.getpid()}
        self.stompd_seq = list(stompd_seq or [])   # successive answers of stompd_pids()
        self.stompd_calls = 0

    def alive(self, pid):
        return pid in self.alive_pids

    def stompd_pids(self):
        self.stompd_calls += 1
        return self.stompd_seq.pop(0) if self.stompd_seq else []


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make_env(procs=None, clock=None, answers=()):
    clock = clock or Clock()
    ans = list(answers)
    out = []
    env = pl.Env(procs=procs or FakeProcs(), now=clock.now, sleep=clock.sleep,
                 prompt=lambda q: ans.pop(0) if ans else "", out=out.append)
    env.clock, env.lines = clock, out
    return env


def write_lock(ld, pid=4242, host="testhost", hb_age=600.0, now=1_000_000.0, owner=True):
    ld.mkdir(parents=True)
    if owner:
        t = pl._utc(now - hb_age)
        (ld / "owner.json").write_text(json.dumps({"pid": pid, "agent": "benchmark", "host": host, "purpose": "x",
                                                  "created_utc": t, "heartbeat_utc": t, "token": "tok"}))
        os.utime(ld / "owner.json", (now - hb_age, now - hb_age))


def write_intent(ld, pid, now=1_000_000.0, age=1.0):
    d = pl.intent_dir(ld)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "host": "testhost", "updated_utc": pl._utc(now - age)}))


@pytest.fixture
def ld(tmp_path):
    return tmp_path / "stomp" / "pedal.lock"


# --------------------------------------------------------------------------- acquire / release


def test_acquire_writes_owner_json_and_release_removes(ld, monkeypatch):
    monkeypatch.setenv("TONEMATCH_AGENT", "stomp-engine")
    env = make_env()
    with pl.PedalLock("unit test", ld=ld, env=env, is_interactive=False) as lk:
        o = json.loads((ld / "owner.json").read_text())
        assert {"pid", "agent", "host", "purpose", "created_utc", "heartbeat_utc"} <= set(o)
        assert o["pid"] == os.getpid() and o["agent"] == "stomp-engine" and o["purpose"] == "unit test"
        assert lk.held and not list(pl.intent_dir(ld).glob("*.json"))   # intent removed after taking it
        env.clock.t += 31
        lk._held.heartbeat()
        assert pl._parse_utc(json.loads((ld / "owner.json").read_text())["heartbeat_utc"]) == pytest.approx(env.clock.t)
    assert not ld.exists() and pl.ENV_TOKEN not in os.environ


def test_reentrant_in_one_process(ld):
    env = make_env()
    a = pl.PedalLock("outer", ld=ld, env=env, is_interactive=False).acquire()
    b = pl.PedalLock("inner", ld=ld, env=env, is_interactive=False).acquire()
    b.release()
    assert ld.exists()               # the outer hold keeps it
    a.release()
    assert not ld.exists()


def test_child_inherits_by_token(ld, monkeypatch):
    write_lock(ld, pid=777, hb_age=1)
    monkeypatch.setenv(pl.ENV_TOKEN, "tok")
    env = make_env(FakeProcs(alive={777}))
    lk = pl.PedalLock("child", ld=ld, env=env, is_interactive=False).acquire()
    assert lk.held and lk._held.own is False
    lk.release()
    assert ld.exists()               # a child never removes its parent's lock


# --------------------------------------------------------------------------- stale checks


def test_confidently_stale_lock_is_removed_and_logged(ld):
    write_lock(ld, pid=4242, hb_age=600)
    env = make_env(FakeProcs(stompd_seq=[[], []]))
    lk = pl.PedalLock("t", ld=ld, env=env, is_interactive=False).acquire()
    assert lk.held and lk.removed and "stale" in lk.removed[0]
    assert pl.STOMPD_RECHECK_S in env.clock.slept            # stompd checked twice, 5 s apart
    log = [json.loads(x) for x in pl.log_path(ld).read_text().splitlines()]
    assert log[-1]["event"] == "removed" and log[-1]["owner"]["pid"] == 4242
    lk.release()


def test_live_owner_is_never_stale(ld):
    write_lock(ld, pid=4242, hb_age=600)
    a = pl.assess(ld, make_env(FakeProcs(alive={4242})))
    assert not a.stale and not a.checks["a_owner_dead"]["ok"]


def test_stompd_running_blocks(ld):
    write_lock(ld, hb_age=600)
    a = pl.assess(ld, make_env(FakeProcs(stompd_seq=[[555]])))
    assert not a.stale and not a.checks["b_no_stompd"]["ok"]


def test_stompd_momentarily_absent_between_runs_blocks(ld):
    # The race: the first look falls into the gap between two sessions of a batch; the second sees it.
    write_lock(ld, hb_age=600)
    procs = FakeProcs(stompd_seq=[[], [556]])
    a = pl.assess(ld, make_env(procs))
    assert not a.stale and not a.checks["b_no_stompd"]["ok"] and procs.stompd_calls == 2


def test_fresh_heartbeat_blocks_even_with_dead_pid(ld):
    write_lock(ld, hb_age=60)            # younger than the 120 s limit
    a = pl.assess(ld, make_env())
    assert not a.stale and not a.checks["c_heartbeat_old"]["ok"]


def test_fresh_intent_of_live_pid_blocks(ld):
    write_lock(ld, hb_age=600)
    write_intent(ld, 9001)
    a = pl.assess(ld, make_env(FakeProcs(alive={9001})))
    assert not a.stale and not a.checks["d_no_intent"]["ok"]


def test_intent_appearing_during_stompd_wait_blocks(ld):
    write_lock(ld, hb_age=600)
    procs = FakeProcs(alive={9002})
    env = make_env(procs)

    def sleep(s):
        env.clock.t += s
        write_intent(ld, 9002, now=env.clock.t)   # another agent starts its attempt meanwhile
    env.sleep = sleep
    a = pl.assess(ld, env)
    assert not a.stale and "appeared" in a.checks["d_no_intent"]["why"]


def test_old_or_dead_intents_do_not_block(ld):
    write_lock(ld, hb_age=600)
    write_intent(ld, 9003, age=600)                # alive but old
    write_intent(ld, 9004)                         # fresh but dead: removed as litter
    a = pl.assess(ld, make_env(FakeProcs(alive={9003})))
    assert a.stale
    assert not (pl.intent_dir(ld) / "9004.json").exists()


def test_other_host_is_never_stale(ld):
    write_lock(ld, host="otherhost", hb_age=6000)
    assert not pl.assess(ld, make_env()).stale


def test_legacy_lock_without_owner_goes_to_the_user(ld):
    write_lock(ld, owner=False)
    a = pl.assess(ld, make_env())
    assert not a.stale and not a.checks["a_owner_dead"]["ok"] and not a.checks["c_heartbeat_old"]["ok"]
    with pytest.raises(pl.LockHeld) as e:
        pl.PedalLock("t", ld=ld, env=make_env(), is_interactive=False).acquire()
    assert e.value.code == pl.EXIT_LOCK_HELD and "python -m tonematch.pedal_lock break" in str(e.value)
    assert ld.exists()


# --------------------------------------------------------------------------- not stale: the user decides


def test_non_interactive_exits_with_code_and_keeps_the_lock(ld):
    write_lock(ld, pid=4242, hb_age=5)
    with pytest.raises(pl.LockHeld) as e:
        pl.PedalLock("t", ld=ld, env=make_env(FakeProcs(alive={4242})), is_interactive=False).acquire()
    assert e.value.code == pl.EXIT_LOCK_HELD and "rm -rf" in str(e.value) and "owner.json" in str(e.value)
    assert ld.exists()


def test_interactive_yes_removes_and_takes_it(ld):
    write_lock(ld, owner=False)
    env = make_env(answers=["y"])
    lk = pl.PedalLock("t", ld=ld, env=env, is_interactive=True).acquire()
    assert lk.held and lk.removed == ["user"]
    assert any("check a_owner_dead" in x for x in env.lines)      # the checks were shown
    lk.release()


def test_interactive_no_keeps_it(ld):
    write_lock(ld, owner=False)
    with pytest.raises(pl.LockHeld):
        pl.PedalLock("t", ld=ld, env=make_env(answers=["n"]), is_interactive=True).acquire()
    assert ld.exists()


def test_waits_for_a_live_owner_then_takes_it(ld):
    write_lock(ld, pid=4242, hb_age=5)
    procs = FakeProcs(alive={4242})
    env = make_env(procs)

    def sleep(s):
        env.clock.t += s
        import shutil
        shutil.rmtree(ld, ignore_errors=True)        # the owner finishes during the wait
    env.sleep = sleep
    lk = pl.PedalLock("t", ld=ld, env=env, wait_s=60, is_interactive=False).acquire()
    assert lk.held
    lk.release()


def test_remove_refuses_a_lock_that_changed(ld):
    write_lock(ld, hb_age=600)
    a = pl.assess(ld, make_env())
    o = json.loads((ld / "owner.json").read_text())
    o["heartbeat_utc"] = pl._utc(1_000_000.0)
    (ld / "owner.json").write_text(json.dumps(o))
    assert pl.remove_lock(ld, a, "test", make_env()) is False and ld.exists()


# --------------------------------------------------------------------------- device check


def test_device_missing_non_interactive_exits_with_code_never_loops():
    calls = []

    def probe():
        calls.append(1)
        return False
    with pytest.raises(pl.DeviceMissing) as e:
        pl.require_device(probe=probe, is_interactive=False)
    assert e.value.code == pl.EXIT_NO_DEVICE and str(e.value) == pl.DEVICE_MISSING_MESSAGE and calls == [1]


def test_device_missing_interactive_retry_then_abort():
    seq = iter([False, True])
    assert pl.require_device(probe=lambda: next(seq), is_interactive=True, prompt=lambda q: "r") is True
    with pytest.raises(pl.DeviceMissing):
        pl.require_device(probe=lambda: False, is_interactive=True, prompt=lambda q: "a")


def test_unknown_os_is_not_blocked(monkeypatch):
    monkeypatch.setattr(pl.platform, "system", lambda: "Plan9")
    assert pl.device_present() is None


def test_macos_probe_matches_the_name(monkeypatch):
    class R:
        stdout = '+-o HX Stomp@01100000  <class IOUSBHostDevice>'
    monkeypatch.setattr(pl.subprocess, "run", lambda *a, **k: R())
    assert pl._probe_macos() is True
    R.stdout = "+-o Keyboard"
    assert pl._probe_macos() is False


NO_DEVICE_HELPER = """#!{python}
import sys
print("Error: no HX device found", file=sys.stderr, flush=True)
sys.exit(1)
"""


def test_bridge_maps_stompd_no_device_to_the_same_message(tmp_path, monkeypatch):
    from tonematch.engines import stomp_bridge as sb

    exe = tmp_path / "stompd"
    exe.write_text(NO_DEVICE_HELPER.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setenv(pl.ENV_INTERACTIVE, "0")
    monkeypatch.setattr(sb.StompBridge, "KILL_GRACE_S", 0.2)
    with pytest.raises(pl.DeviceMissing) as e:
        sb.StompBridge(binary=exe)
    assert str(e.value) == pl.DEVICE_MISSING_MESSAGE and e.value.code == pl.EXIT_NO_DEVICE


def test_bridge_takes_and_releases_the_lock(tmp_path, monkeypatch):
    from tonematch.engines import stomp_bridge as sb

    exe = tmp_path / "stompd"
    exe.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps({'ok':True,'event':'ready'}),flush=True)\n"
                   "for l in sys.stdin:\n    print(json.dumps({'ok':True}),flush=True)\n    "
                   "if json.loads(l)['cmd']=='quit': break\n"
                   "print(json.dumps({'ok':True,'event':'closed','release_sent':True}),flush=True)\n")
    exe.chmod(0o755)
    lock = tmp_path / "pedal.lock"
    monkeypatch.setenv(pl.ENV_LOCK_DIR, str(lock))
    b = sb.StompBridge(binary=exe, lock=True, purpose="bridge test", usb_reset="off")
    assert json.loads((lock / "owner.json").read_text())["purpose"] == "bridge test"
    b.close()
    assert not lock.exists()


# --------------------------------------------------------------------------- more branches


def test_heartbeat_never_overwrites_another_owner(ld):
    env = make_env()
    lk = pl.PedalLock("mine", ld=ld, env=env, is_interactive=False).acquire()
    theirs = {"pid": 1, "host": "testhost", "token": "theirs", "heartbeat_utc": pl._utc(0)}
    (ld / "owner.json").write_text(json.dumps(theirs))     # ours was removed; someone else took it
    env.clock.t += 31
    lk._held.heartbeat()
    assert json.loads((ld / "owner.json").read_text()) == theirs
    lk.release()
    assert ld.exists()                                     # release leaves their lock too


def test_heartbeat_thread_runs_only_for_the_owner(ld, monkeypatch):
    write_lock(ld, pid=777, hb_age=1)
    monkeypatch.setenv(pl.ENV_TOKEN, "tok")
    child = pl.PedalLock("child", ld=ld, env=make_env(FakeProcs(alive={777})), is_interactive=False).acquire()
    assert child._held._thread is None
    child.release()


def test_heartbeat_limit_spans_several_periods():
    assert max(pl.STALE_AFTER_S, pl.HEARTBEAT_LIMIT_FACTOR * pl.HEARTBEAT_S) >= 3 * pl.HEARTBEAT_S


def test_heartbeat_just_under_the_limit_blocks(ld):
    write_lock(ld, hb_age=pl.STALE_AFTER_S - 1)
    assert not pl.assess(ld, make_env()).checks["c_heartbeat_old"]["ok"]


def test_wait_runs_out_then_non_interactive_exits(ld):
    write_lock(ld, pid=4242, hb_age=5)
    env = make_env(FakeProcs(alive={4242}))
    with pytest.raises(pl.LockHeld) as e:
        pl.PedalLock("t", ld=ld, env=env, wait_s=40, poll_s=15, is_interactive=False).acquire()
    assert e.value.code == pl.EXIT_LOCK_HELD
    assert sum(env.clock.slept) >= 40 and any("waiting up to 40 s" in x for x in env.lines)
    assert sum("waiting" in x for x in env.lines) == 1      # told once, not every poll
    assert ld.exists()


def test_owner_json_changing_during_the_stompd_wait_blocks(ld):
    write_lock(ld, hb_age=600)
    env = make_env()

    def sleep(s):
        env.clock.t += s
        o = json.loads((ld / "owner.json").read_text())
        o["heartbeat_utc"] = pl._utc(env.clock.t)          # the owner came back to life meanwhile
        (ld / "owner.json").write_text(json.dumps(o))
    env.sleep = sleep
    a = pl.assess(ld, env)
    assert not a.stale and "changed" in a.checks["a_owner_dead"]["why"]


def test_process_table_that_cannot_tell_counts_as_busy(ld, monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")
    monkeypatch.setattr(pl.subprocess, "run", boom)
    assert pl.ProcessTable().stompd_pids() == [-1]
    write_lock(ld, hb_age=600)
    procs = FakeProcs(stompd_seq=[[-1]])
    assert not pl.assess(ld, make_env(procs)).stale


def test_real_process_table_sees_this_pid_and_names_stompd(monkeypatch):
    t = pl.ProcessTable()
    assert t.alive(os.getpid()) and not t.alive(0)

    class R:
        stdout = "  12 /usr/bin/zsh\n  34 /opt/x/stompd\n  56 stompd-offline\n  bad line\n"
    monkeypatch.setattr(pl.subprocess, "run", lambda *a, **k: R())
    assert t.stompd_pids() == [34, 56]


def test_intent_from_another_host_is_taken_as_live(ld):
    write_lock(ld, hb_age=600)
    d = pl.intent_dir(ld)
    d.mkdir(parents=True)
    (d / "31337.json").write_text(json.dumps({"pid": 31337, "host": "elsewhere", "updated_utc": pl._utc(1_000_000.0)}))
    a = pl.assess(ld, make_env())
    assert not a.stale and (d / "31337.json").exists()


def test_own_intent_is_ignored(ld):
    write_lock(ld, hb_age=600)
    write_intent(ld, os.getpid())
    assert pl.assess(ld, make_env(FakeProcs(stompd_seq=[[], []]))).stale


def test_missing_lock_assesses_as_absent(ld):
    a = pl.assess(ld, make_env())
    assert not a.exists and not a.stale


def test_remove_puts_back_a_lock_replaced_during_removal(ld, monkeypatch):
    write_lock(ld, hb_age=600)
    a = pl.assess(ld, make_env(FakeProcs(stompd_seq=[[], []])))
    assert a.stale
    real = os.rename
    state = {"n": 0}

    def rename(src, dst):
        real(src, dst)
        state["n"] += 1
        if state["n"] == 1:      # someone swapped in their own lock under the same name in between
            (dst / "owner.json").write_text(json.dumps({"pid": 1, "token": "new"}))
    monkeypatch.setattr(pl.os, "rename", rename)
    assert pl.remove_lock(ld, a, "test", make_env()) is False
    assert json.loads((ld / "owner.json").read_text())["token"] == "new"
    assert json.loads(pl.log_path(ld).read_text().splitlines()[-1])["event"] == "remove-aborted"


def test_interactive_yes_but_lock_changed_keeps_it(ld, monkeypatch):
    write_lock(ld, owner=False)
    monkeypatch.setattr(pl, "remove_lock", lambda *a, **k: False)
    with pytest.raises(pl.LockHeld):
        pl.PedalLock("t", ld=ld, env=make_env(answers=["y"]), is_interactive=True).acquire()
    assert ld.exists()


def test_interactive_prompt_eof_keeps_it(ld):
    write_lock(ld, owner=False)

    def eof(q):
        raise EOFError
    env = make_env()
    env.prompt = eof
    with pytest.raises(pl.LockHeld):
        pl.PedalLock("t", ld=ld, env=env, is_interactive=True).acquire()
    assert ld.exists()


def test_legacy_text_names_the_missing_owner(ld):
    write_lock(ld, owner=False)
    assert "legacy mkdir-only lock" in pl.assess(ld, make_env()).text()


# --------------------------------------------------------------------------- session, CLI, helpers


def test_session_checks_the_device_before_the_lock(ld, monkeypatch):
    monkeypatch.setattr(pl, "device_present", lambda probe=None: False)
    with pytest.raises(pl.DeviceMissing), pl.session("batch", ld=ld, is_interactive=False, env=make_env()):
        pass
    assert not ld.exists()


def test_session_holds_for_the_batch_and_releases(ld, monkeypatch):
    monkeypatch.setattr(pl, "device_present", lambda probe=None: True)
    with pl.session("batch", ld=ld, is_interactive=False, env=make_env()) as lk:
        assert lk.held and pl.held_here(ld)
        inner = pl.PedalLock("bridge", ld=ld, env=make_env(), is_interactive=False).acquire()
        inner.release()
        assert ld.exists()
    assert not ld.exists() and not pl.held_here(ld)


def test_device_check_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(pl.ENV_PROBE, "off")
    assert pl.device_present(lambda: False) is None


def test_broken_probe_does_not_block():
    def bad():
        raise RuntimeError
    assert pl.device_present(bad) is None


def test_ask_retry_eof_aborts():
    def eof(q):
        raise EOFError
    assert pl.ask_retry(prompt=eof) is False
    assert pl.ask_retry(prompt=lambda q: "") is True


def test_interactive_env_override(monkeypatch):
    monkeypatch.setenv(pl.ENV_INTERACTIVE, "1")
    assert pl.interactive() is True
    monkeypatch.setenv(pl.ENV_INTERACTIVE, "0")
    assert pl.interactive() is False


def test_parse_utc_forms():
    assert pl._parse_utc(None) is None and pl._parse_utc("junk") is None
    assert pl._parse_utc("2026-10-16T00:00:00Z") == pl._parse_utc("2026-10-16T00:00:00.000000Z")


def test_linux_probe_reads_sysfs(tmp_path, monkeypatch):
    dev = tmp_path / "1-1"
    dev.mkdir()
    (dev / "idVendor").write_text("0e41\n")
    (dev / "idProduct").write_text("4246\n")
    real = pl.Path
    monkeypatch.setattr(pl, "Path", lambda p: tmp_path if p == "/sys/bus/usb/devices" else real(p))
    assert pl._probe_linux() is True
    (dev / "idProduct").write_text("0001\n")
    assert pl._probe_linux() is False


def test_cli_status_device_and_break(ld, monkeypatch, capsys):
    monkeypatch.setenv(pl.ENV_LOCK_DIR, str(ld))
    monkeypatch.setenv(pl.ENV_INTERACTIVE, "0")
    assert pl.main(["status"]) == 0 and "no pedal lock" in capsys.readouterr().out
    monkeypatch.setattr(pl, "device_present", lambda probe=None: False)
    assert pl.main(["device"]) == pl.EXIT_NO_DEVICE
    write_lock(ld, owner=False)
    assert pl.main(["status"]) == 0 and ld.exists()
    assert pl.main(["break"]) == pl.EXIT_LOCK_HELD and ld.exists()      # non-interactive: never silently
    assert pl.main(["break", "--yes"]) == 0 and not ld.exists()
    assert "pedal_lock break" in pl.log_path(ld).read_text()
