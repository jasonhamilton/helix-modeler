"""Elastic jobs (T138): claim queue, atomic writes, supervisor, priority, and the 17:22 scaler regression.
Fakes only; no Native."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tonematch.engines import elastic as el
from tonematch.engines import host_budget as hb

ROOT = Path(__file__).resolve().parents[1]
HOST_CMD = "python -m tonematch.engines.native_host"


# --------------------------------------------------------------------------- claims


class Clock:
    def __init__(self):
        self.t = 5000.0
        self.dead: set[int] = set()

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def alive(self, pid):
        return pid not in self.dead


def queue(tmp_path, clock, **kw):
    return el.ClaimQueue(tmp_path, now=clock.now, alive=clock.alive, sleep=clock.sleep, beat_s=3600, **kw)


def test_claim_is_exclusive_and_released(tmp_path):
    c = Clock()
    a, b = queue(tmp_path, c), queue(tmp_path, c)
    ca = a.try_claim("i001")
    assert ca is not None and b.try_claim("i001") is None
    assert b.claim_next(["i001", "i002"]).id == "i002"
    ca.release()
    assert not (tmp_path / ".claims" / "i001").exists()
    assert b.try_claim("i001") is not None


def test_dead_owner_is_reclaimed_only_on_the_second_look(tmp_path):
    c = Clock()
    a, b = queue(tmp_path, c), queue(tmp_path, c)
    a.try_claim("i001")
    me = os.getpid()
    c.dead.add(me)  # the owner (this pid) is now "dead"
    assert b.try_claim("i001") is None          # first look: suspect only
    assert "i001" in b.suspects
    c.t += 1.0
    assert b.try_claim("i001") is None          # gap (3 s) not over
    c.t += 3.0
    got = b.try_claim("i001")
    assert got is not None                       # second look, still stale: reclaimed
    c.dead.clear()
    assert a.try_claim("i001") is None           # the new owner is live


def test_stale_heartbeat_reclaims_but_a_fresh_one_does_not(tmp_path):
    c = Clock()
    a, b = queue(tmp_path, c), queue(tmp_path, c)
    ca = a.try_claim("i001")
    c.t += 200  # heartbeat older than stale_s (120)
    assert b.try_claim("i001") is None
    a._write_owner("i001", ca.token)  # the owner beats between the looks
    c.t += 4
    assert b.try_claim("i001") is None
    assert "i001" not in b.suspects


def test_two_reapers_cannot_both_win(tmp_path):
    c = Clock()
    a, r1, r2 = queue(tmp_path, c), queue(tmp_path, c), queue(tmp_path, c)
    a.try_claim("i001")
    c.dead.add(os.getpid())
    assert r1.try_claim("i001") is None and r2.try_claim("i001") is None
    c.t += 4
    c.dead.clear()  # the new owners below are "this pid" and must look alive
    c.dead.add(os.getpid())
    w1 = r1.try_claim("i001")
    c.dead.clear()
    w2 = r2.try_claim("i001")                   # r2's token check fails: the claim is r1's now
    assert w1 is not None and w2 is None


def test_release_never_removes_someone_elses_claim(tmp_path):
    c = Clock()
    a, b = queue(tmp_path, c), queue(tmp_path, c)
    ca = a.try_claim("i001")
    ca2 = el.Claim.__new__(el.Claim)
    ca2.queue, ca2.id, ca2.token, ca2._stop = b, "i001", "not-the-token", __import__("threading").Event()
    ca2.release()
    assert (tmp_path / ".claims" / "i001").exists()
    ca.release()


def test_release_pid_frees_a_dead_workers_claims(tmp_path):
    c = Clock()
    q = queue(tmp_path, c)
    q.try_claim("i001")
    q.try_claim("i002")
    assert sorted(q.release_pid(os.getpid())) == ["i001", "i002"] and not q.held()


def test_claim_without_owner_file_is_judged_by_age(tmp_path):
    c = Clock()
    q = queue(tmp_path, c)
    (tmp_path / ".claims" / "i001").mkdir()
    assert q.try_claim("i001") is None and "i001" not in q.suspects  # fresh dir: a claimer between mkdir and write


# --------------------------------------------------------------------------- atomic writes


def test_atomic_write_leaves_no_partial_file(tmp_path, monkeypatch):
    f = tmp_path / "i001.json"
    el.atomic_write_json(f, {"a": 1})
    assert json.loads(f.read_text()) == {"a": 1}
    real = os.replace

    def boom(a, b):
        raise OSError("stopped")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        el.atomic_write_json(f, {"a": 2})
    monkeypatch.setattr(os, "replace", real)
    assert json.loads(f.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["i001.json"]  # tmp removed


def test_atomic_wav_roundtrip(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    el.atomic_write_wav(tmp_path / "a.wav", np.zeros(100, dtype="float32"), 48000)
    assert [p.name for p in tmp_path.iterdir()] == ["a.wav"]


# --------------------------------------------------------------------------- the work loop (synthetic_v1)


@pytest.fixture(scope="module")
def sv1():
    spec = importlib.util.spec_from_file_location("synthetic_v1_elastic", ROOT / "scripts" / "synthetic_v1.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Args:
    def __init__(self, **kw):
        self.redo, self.as_worker, self.supervisor_pid, self.redo_since = False, False, None, None
        self.__dict__.update(kw)


def its(n):
    return [{"id": f"i{k:03d}"} for k in range(n)]


def test_done_items_are_not_redone_and_redo_redoes(sv1, tmp_path):
    seen = []
    proc = lambda it: (seen.append(it["id"]) or {"id": it["id"]})
    items = its(3)
    assert sv1.work_items(items, tmp_path, proc, lambda i, r: None, Args()) == 3
    seen.clear()
    assert sv1.work_items(items, tmp_path, proc, lambda i, r: None, Args()) == 0 and not seen
    time.sleep(0.02)
    assert sv1.work_items(items, tmp_path, proc, lambda i, r: None, Args(redo=True)) == 3
    assert sorted(seen) == ["i000", "i001", "i002"]  # each once, even though the loop re-scans
    assert not list((tmp_path / ".claims").iterdir())


def test_two_workers_never_take_the_same_item(sv1, tmp_path):
    items = its(6)
    log = []

    def proc_a(it):
        log.append(("a", it["id"]))
        # while a holds i000..., b runs a whole pass over the rest
        if it["id"] == "i000":
            sv1.work_items(items, tmp_path, lambda i: (log.append(("b", i["id"])) or {"id": i["id"]}),
                           lambda i, r: None, Args(as_worker=True, supervisor_pid=os.getpid()))
        return {"id": it["id"]}
    sv1.work_items(items, tmp_path, proc_a, lambda i, r: None, Args())
    ids = [i for _, i in log]
    assert sorted(ids) == sorted(set(ids)) == [x["id"] for x in items]
    assert ("a", "i000") in log and all(w == "b" for w, i in log if i != "i000")


def test_worker_stops_between_items_on_stop_file_and_never_mid_item(sv1, tmp_path):
    items = its(5)
    n = []

    def proc(it):
        n.append(it["id"])
        if len(n) == 2:  # the supervisor asks during item 2: it still completes, then the worker leaves
            el.atomic_write_text(el.stop_file(tmp_path, os.getpid()), "stop")
        return {"id": it["id"]}
    done = sv1.work_items(items, tmp_path, proc, lambda i, r: None, Args(as_worker=True, supervisor_pid=os.getpid()))
    assert done == 2 and (tmp_path / "i001.json").exists() and not (tmp_path / "i002.json").exists()
    assert not list((tmp_path / ".claims").iterdir())


def test_interrupted_item_leaves_no_json_and_no_claim(sv1, tmp_path):
    def proc(it):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        sv1.work_items(its(2), tmp_path, proc, lambda i, r: None, Args())
    assert not list(tmp_path.glob("i*.json")) and not list((tmp_path / ".claims").iterdir())


def test_worker_leaves_when_its_supervisor_is_gone(sv1, tmp_path, monkeypatch):
    monkeypatch.setattr(el, "pid_alive", lambda pid: False)
    done = sv1.work_items(its(3), tmp_path, lambda it: {"id": it["id"]}, lambda i, r: None,
                          Args(as_worker=True, supervisor_pid=999999))
    assert done == 0


def test_strip_flags(sv1):
    argv = ["run", "--workers", "auto", "--priority=high", "--tag", "x", "--set", "s"]
    assert sv1.strip_flags(argv, {"--workers": 1, "--priority": 1}) == ["run", "--tag", "x", "--set", "s"]


# --------------------------------------------------------------------------- the supervisor


class FakeProc:
    _next = 9000

    def __init__(self):
        FakeProc._next += 1
        self.pid, self.rc, self.signals = FakeProc._next, None, []

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)


class FakeLease:
    def __init__(self):
        self.granted, self.wanted_seen, self.info, self.released = 0, [], None, False

    def refresh(self, n_wanted=None, info=None):
        self.wanted_seen.append(n_wanted)
        self.info = info
        return min(self.granted, n_wanted) if n_wanted is not None else self.granted

    def release(self):
        self.released = True


def make_sup(tmp_path, left_box, lease, **kw):
    c = Clock()
    procs, kills = [], []

    def spawn():
        p = FakeProc()
        procs.append(p)
        return p
    sup = el.Supervisor(run_dir=tmp_path, left=lambda: left_box[0], total=10, spawn=spawn, lease=lease,
                        max_workers=kw.pop("max_workers", 8), queue=queue(tmp_path, c), poll_s=10, stagger_s=10,
                        now=c.now, sleep=c.sleep, killpg=lambda pg, sig: kills.append((pg, sig)),
                        log=lambda m: None, **kw)
    return sup, c, procs, kills


def test_supervisor_grows_with_the_grant_one_per_stagger_then_shrinks_newest_first(tmp_path):
    left, lease = [10], FakeLease()
    sup, c, procs, kills = make_sup(tmp_path, left, lease)
    lease.granted = 3
    for _ in range(5):
        sup.step()
        c.t += 10
    assert len(procs) == 3 and len(sup.active()) == 3        # filled to the grant, never above
    lease.granted = 1                                        # another job started: the share drops
    sup.step()
    assert not [w for w in sup.workers if w.stopping]        # one low look is not enough (no flapping)
    c.t += 10
    sup.step()
    stopping = [w for w in sup.workers if w.stopping]
    assert sorted(w.pid for w in stopping) == [procs[1].pid, procs[2].pid]
    assert all(el.stop_file(tmp_path, w.pid).exists() for w in stopping)
    assert all(p.signals == [] and p.rc is None for p in procs)        # nothing killed mid-render
    for p in procs[1:]:
        p.rc = 0                                             # they finish their item and leave
    sup.step()
    assert [w.pid for w in sup.workers] == [procs[0].pid]
    assert not kills


def test_supervisor_never_runs_more_workers_than_items_left(tmp_path):
    left, lease = [2], FakeLease()
    lease.granted = 6
    sup, c, procs, _ = make_sup(tmp_path, left, lease)
    for _ in range(6):
        sup.step()
        c.t += 10
    assert len(procs) == 2 and lease.wanted_seen[-1] == 2


def test_supervisor_finishes_when_items_and_workers_are_done(tmp_path):
    left, lease = [1], FakeLease()
    lease.granted = 1
    sup, _c, procs, _ = make_sup(tmp_path, left, lease)
    assert not sup.step()
    left[0] = 0
    assert not sup.step()          # a worker is still running
    procs[0].rc = 0
    assert sup.step()


def test_unexpected_worker_death_releases_claim_lease_and_host_group(tmp_path):
    left, lease = [4], FakeLease()
    lease.granted = 1
    released = []

    class B:
        def release_pid(self, pid):
            released.append(pid)
    sup, c, procs, kills = make_sup(tmp_path, left, lease, budget=B())
    sup.step()
    w = sup.workers[0]
    claim = sup.queue.try_claim("i001")        # this pid stands in for the worker that owned it
    assert claim is not None
    sup.queue.release_pid = lambda pid: ["i001"]
    procs[0].rc = -9
    c.t += 10
    sup.step()
    assert w.pid in released                                  # lease removed
    assert (w.pid, signal.SIGTERM) in kills and (w.pid, signal.SIGKILL) in kills   # its Native host group
    assert len(procs) == 2                                     # and a replacement is started


def test_crash_loop_gives_up(tmp_path):
    left, lease = [4], FakeLease()
    lease.granted = 1
    sup, c, procs, _ = make_sup(tmp_path, left, lease, max_failures=3)
    with pytest.raises(RuntimeError, match="died without finishing"):
        for _ in range(10):
            sup.step()
            for p in procs:
                p.rc = 1
            c.t += 10


def test_terminate_stops_workers_cooperatively_then_escalates(tmp_path):
    left, lease = [6], FakeLease()
    lease.granted = 2
    sup, c, procs, kills = make_sup(tmp_path, left, lease, grace_s=30)
    sup.step()
    c.t += 10
    sup.step()
    assert len(procs) == 2
    sup.terminate()
    # worker 0 finishes its item and exits; worker 1 ignores the stop file
    orig = c.sleep

    def sleeper(s):
        orig(s)
        procs[0].rc = 0
    sup.sleep = sleeper
    assert sup.run() is False
    assert procs[1].signals == [signal.SIGTERM]              # after the grace: SIGTERM, then the group
    assert (procs[1].pid, signal.SIGKILL) in kills and not sup.workers and lease.released


# --------------------------------------------------------------------------- SIGTERM with real processes

WORKER = textwrap.dedent('''
    import os, sys, time, pathlib
    run = pathlib.Path(sys.argv[1]); me = os.getpid()
    stop = run / ".workers" / f"{me}.stop"
    (run / f"started.{me}").write_text("x")
    for _ in range(600):
        if stop.exists():
            (run / f"stopped.{me}").write_text("clean"); sys.exit(0)
        time.sleep(0.05)
''')
SUPER = textwrap.dedent('''
    import os, sys, signal, subprocess, pathlib
    sys.path.insert(0, sys.argv[2])
    from tonematch.engines import elastic as el
    run = pathlib.Path(sys.argv[1]); worker = sys.argv[3]
    class L:
        granted = 2
        def refresh(self, n=None, info=None): return 2
        def release(self): pass
    sup = el.Supervisor(run_dir=run, left=lambda: 5, total=5, lease=L(), max_workers=2, poll_s=0.1, stagger_s=0,
        spawn=lambda: subprocess.Popen([sys.executable, worker, str(run)], start_new_session=True),
        log=lambda m: None, grace_s=20)
    signal.signal(signal.SIGTERM, lambda *a: sup.terminate())
    (run / "ready").write_text(str(os.getpid()))
    sup.run()
''')


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_sigterm_on_the_supervisor_stops_its_workers_and_leaves_no_orphans(tmp_path):
    (tmp_path / "w.py").write_text(WORKER)
    (tmp_path / "s.py").write_text(SUPER)
    sup = subprocess.Popen([sys.executable, str(tmp_path / "s.py"), str(tmp_path), str(ROOT / "src"),
                            str(tmp_path / "w.py")])
    try:
        t0 = time.time()
        while len(list(tmp_path.glob("started.*"))) < 2 and time.time() - t0 < 20:
            time.sleep(0.1)
        pids = [int(p.name.split(".")[1]) for p in tmp_path.glob("started.*")]
        assert len(pids) == 2 and all(el.pid_alive(p) for p in pids)
        sup.send_signal(signal.SIGTERM)
        sup.wait(timeout=30)
        assert len(list(tmp_path.glob("stopped.*"))) == 2        # both left cooperatively
        time.sleep(0.3)
        assert not any(el.pid_alive(p) and _is_child_running(p) for p in pids)
    finally:
        if sup.poll() is None:
            sup.kill()


def _is_child_running(pid):
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(out) and not out.startswith("Z")


# --------------------------------------------------------------------------- scaler: demand leases, priority


class Fake(hb.System):
    def __init__(self, load=0.0, cores=10, ram=32.0):
        self.t, self.load, self._cores, self._ram = 1000.0, load, cores, ram
        self.table, self.cpu, self.dead = [], {}, set()

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def loadavg(self):
        return (self.load,) * 3

    def cores(self):
        return self._cores

    def ram_gb(self):
        return self._ram

    def procs(self):
        return list(self.table)

    def cpu_by_pid(self):
        return dict(self.cpu)

    def pid_alive(self, pid):
        return pid not in self.dead

    def start_hosts(self, parent, n):
        for i in range(n):
            self.table.append((70000 + parent * 10 + i, parent, HOST_CMD))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TONEMATCH_HOST_") or k == "TONEMATCH_JOB_PRIORITY":
            monkeypatch.delenv(k)
    monkeypatch.setenv("TONEMATCH_HOST_BUDGET_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("TONEMATCH_HOST_SAMPLE_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_HEARTBEAT_S", "3600")
    monkeypatch.setenv("TONEMATCH_HOST_REGROW_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_STAGGER_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_MAX_HOSTS", "8")
    return tmp_path / "hb"


def mk(env, load=0.0):
    fs = Fake(load)
    return hb.Budget(system=fs, directory=env), fs


def test_demand_lease_grows_a_job_beyond_its_running_workers(env):
    b, _fs = mk(env)
    sup = b.acquire("j:a", 6, floor=0, demand_only=True)
    assert sup.refresh(6) >= 1
    for _ in range(10):
        g = sup.refresh(6)
    assert g == 6                                            # idle machine, cap 8: the job may run 6
    st = b.status()
    row = st["leases_by_job"]["j:a"][0]
    assert row["demand_only"] and row["granted"] == 6 and row["wanted"] == 6


def test_demand_reservation_is_room_a_second_job_cannot_take(env):
    b, _fs = mk(env)
    a = b.acquire("j:a", 8, floor=0, demand_only=True)
    for _ in range(4):
        a.refresh(8)
    other = b.acquire("j:b", 8, floor=0, demand_only=True)
    g = other.refresh(8)
    ga = a.refresh(8)
    assert ga >= 4 and g + ga <= 8                           # reserved room is never handed out twice


def test_priority_high_gets_hosts_first_and_normal_shrinks_cooperatively(env):
    b, _fs = mk(env)
    normal = b.acquire("j:norm", 8, floor=0, demand_only=True, priority="normal")
    for _ in range(10):
        n = normal.refresh(8)
    assert n == 8
    high = b.acquire("j:high", 8, floor=0, demand_only=True, priority="high")
    h0 = high.refresh(8)
    n1 = normal.refresh(8)                                   # capacity is short: the normal job shrinks
    assert n1 == 1                                           # to its minimum, at its next look
    for _ in range(10):
        h = high.refresh(8)
    assert h == 7 and h0 <= 7
    # when the high job ends the normal job grows back
    high.release()
    for _ in range(10):
        n = normal.refresh(8)
    assert n == 8


def test_two_normal_jobs_split_evenly(env):
    b, _fs = mk(env)
    a = b.acquire("j:a", 8, floor=0, demand_only=True)
    c = b.acquire("j:c", 8, floor=0, demand_only=True)
    for _ in range(12):
        ga, gc = a.refresh(8), c.refresh(8)
    assert (ga, gc) == (4, 4)


def test_worker_leases_inside_a_reservation_are_granted(env):
    b, _fs = mk(env)
    sup = b.acquire("j:a", 3, floor=0, demand_only=True)
    for _ in range(5):
        sup.refresh(3)
    workers = [b.acquire("j:a", 1, label="engine") for _ in range(3)]   # each worker's engine lease
    assert [int(w) for w in workers] == [1, 1, 1]
    assert b.status()["pending_hosts"] == 3                  # reservation and worker leases are not double counted
    for w in workers:
        w.release()


def test_status_shows_job_progress(env):
    b, _fs = mk(env)
    sup = b.acquire("j:a", 3, floor=0, demand_only=True, priority="high",
                    info={"workers": 2, "left": 4, "done": 2, "total": 6, "priority": "high"})
    sup.refresh(3, {"workers": 2, "left": 4, "done": 2, "total": 6, "priority": "high"})
    text = hb.format_status(b.status())
    assert "high" in text and "workers 2" in text and "left 4" in text and "done 2" in text


# --------------------------------------------------------------------------- the 17:22 regression


def test_load_12_46_with_8_hosts_drains_and_shrinks_a_supervised_job(env):
    """Lead's numbers: 1-min load 12.46, 8 hosts running, allowed 1, drain was off and nothing shrank."""
    b, fs = mk(env, load=12.46)
    def force_grant(lease, n):
        p = b._lease_path(lease.id)
        d = json.loads(p.read_text())
        d["granted"] = n
        p.write_text(json.dumps(d))
    sup = b.acquire("j:elastic", 6, floor=0, demand_only=True, priority="normal")
    force_grant(sup, 6)
    for pid in range(101, 109):                              # 8 hosts, one per worker
        w = b.acquire("j:elastic", 1, floor=0, label="engine")
        w.budget._lease_path(w.id).write_text(json.dumps({**json.loads(w.budget._lease_path(w.id).read_text()),
                                                          "pid": pid, "granted": 1}))
        fs.start_hosts(pid, 1)
    first = b.status()
    second = b.status()
    assert second["drain"] is True                           # over drain_load on two looks: drain engages
    assert first["allowed_total"] <= 1 and second["running_hosts"] == 8   # although running > allowed
    got = sup.refresh(6)
    assert got < 6                                           # the supervisor is told to stop workers


def test_one_look_over_the_hard_line_is_enough_to_drain(env):
    b, _fs = mk(env, load=12.46)
    assert b.status()["drain"] is True


def test_measured_per_host_cost_includes_the_python_side(env):
    b, fs = mk(env, load=4.0)
    for pid in (201, 202):
        b.acquire(f"j:{pid}", 1)
        fs.start_hosts(pid, 1)
        fs.cpu[pid] = 1.0                                    # the Python side burns a core
        fs.cpu[70000 + pid * 10] = 1.0                       # and so does the host
    v = b.status()
    assert v["constants"]["per_host_load"] == 1.0
    assert v["per_host_measured"] == pytest.approx(2.0)
    assert v["allowed_total"] <= 3                           # (9 - ~0) / 2 hosts, not 9


def test_start_up_is_staggered_one_host_per_ten_seconds(env, monkeypatch):
    monkeypatch.setenv("TONEMATCH_HOST_STAGGER_S", "10")
    b, fs = mk(env)
    sup = b.acquire("j:a", 6, floor=0, demand_only=True)
    g1 = sup.refresh(6)
    g2 = sup.refresh(6)
    assert (g1, g2) == (1, 1)                                # a second host only after 10 s
    fs.t += 10
    assert sup.refresh(6) == 2
    fs.t += 10
    assert sup.refresh(6) == 3
