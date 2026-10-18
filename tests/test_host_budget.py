"""Host scaler tests (T133): fake load and process tables, no Native needed."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tonematch.engines import host_budget as hb

pytestmark = pytest.mark.host_budget

HOST_CMD = "python -m tonematch.engines.native_host"


class Fake(hb.System):
    def __init__(self, load=0.0, cores=10, ram=32.0):
        self.t, self.load, self._cores, self._ram = 1000.0, load, cores, ram
        self.table: list[tuple[int, int, str]] = []
        self.dead: set[int] = set()

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def loadavg(self):
        return (self.load, self.load, self.load)

    def cores(self):
        return self._cores

    def ram_gb(self):
        return self._ram

    def procs(self):
        return list(self.table)

    def pid_alive(self, pid):
        return pid not in self.dead

    def start_hosts(self, parent, n, base=50000):
        for i in range(n):
            self.table.append((base + parent * 10 + i, parent, HOST_CMD))


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TONEMATCH_HOST_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TONEMATCH_HOST_BUDGET_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("TONEMATCH_HOST_SAMPLE_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_HEARTBEAT_S", "3600")
    monkeypatch.setenv("TONEMATCH_HOST_REGROW_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_STAGGER_S", "0")  # the stagger has its own tests
    return tmp_path / "hb"


def make(env, load=0.0, **kw):
    fs = Fake(load, **kw)
    return hb.Budget(system=fs, directory=env), fs


def as_pid(monkeypatch, pid):
    monkeypatch.setattr(os, "getpid", lambda: pid)


def test_derived_constants_and_env(env, monkeypatch):
    b, _ = make(env)
    assert (b.cfg.max_hosts, b.cfg.target_load, b.cfg.max_load) == (6, 9.0, 10.0)
    monkeypatch.setenv("TONEMATCH_HOST_TARGET_LOAD", "12")
    (env / "limits.json").write_text(json.dumps({"max_hosts": 3}))
    cfg = hb.Config.load(Fake(), env)
    assert cfg.target_load == 12 and cfg.sources["target_load"] == "env"
    assert cfg.max_hosts == 3 and cfg.sources["max_hosts"] == "limits.json"
    assert hb.Config.load(Fake(cores=32, ram=128), Path("/nonexistent")).max_hosts == 25


def test_grant_within_target_and_cap(env):
    b, _fs = make(env, load=2.0)
    ls = b.acquire("a:x", 6, 0)
    assert ls.granted == 6  # load 2 + 6 = 8 <= 9, hard cap 6
    s = b.status()
    assert s["allowed_total"] == 6 and s["pending_hosts"] == 6
    assert s["constants"]["target_load"] == 9.0


def test_refusal_when_it_would_exceed(env):
    b, fs = make(env, load=5.0)
    ls = b.acquire("a:x", 6, 0)
    assert ls.granted == 4  # 5 + 4 = 9, the most that stays at target
    fs.start_hosts(os.getpid(), 4)
    fs.load = 9.0
    with pytest.raises(hb.HostBudgetTimeout):
        b.acquire("b:y", 3, 0)  # a second job: nothing free until the first shrinks


def test_pending_counted(env):
    b, fs = make(env, load=3.0)
    a = b.acquire("a:x", 6, 0)
    assert a.granted == 6
    assert b.status()["pending_hosts"] == 6
    # nothing has started, so the load still reads 3: a second job must not see 6 more hosts free
    with pytest.raises(hb.HostBudgetTimeout):
        b.acquire("b:y", 6, 0)
    # once a's hosts have started, they are no longer pending
    fs.start_hosts(os.getpid(), 6)
    assert b.status()["pending_hosts"] == 0


def test_floor_of_one(env):
    b, _fs = make(env, load=9.0)
    ls = b.acquire("a:x", 6, 0)  # capacity is 0 at load 9.0; the floor lets one host in under max 10
    assert ls.granted == 1
    b2, _fs2 = make(env.parent / "hb2", load=10.0)
    with pytest.raises(hb.HostBudgetTimeout):  # 10 + 1 > max_load: the hard line wins
        b2.acquire("a:x", 6, 0)


def test_fair_share_two_and_three_jobs(env):
    b, fs = make(env, load=0.0)
    a = b.acquire("a:x", 6, 0)
    assert a.granted == 6
    # b arrives and waits; its request makes a's fair share 3 at a's next safe point
    other = hb.Budget(system=fs, directory=env)
    fs.t += 1
    wb = other.acquire("b:y", 6, 0, floor=0)
    assert wb.granted == 0 and a.refresh() == 3
    assert wb.refresh() == 3
    fs.t += 1
    wc = other.acquire("c:z", 6, 0, floor=0)
    assert a.refresh() == 2 and wb.refresh() == 2 and wc.refresh() == 2
    # leftovers go to the earliest: 4 hosts among 3 jobs -> 2,1,1
    fs.load = 7.0
    fs.start_hosts(os.getpid(), 2)
    assert [a.refresh(), wb.refresh(), wc.refresh()] == [2, 1, 1]


def test_never_below_one(env):
    b, _fs = make(env, load=0.0)
    a = b.acquire("a:x", 6, 0)
    others = [b.acquire(f"j{i}:z", 6, 0, floor=0) for i in range(8)]
    assert a.refresh() == 1  # shared by 9 jobs but a job that holds a lease keeps 1
    assert all(o.refresh() <= 1 for o in others)


def test_stale_two_check_race(env):
    b, fs = make(env, load=0.0)
    a = b.acquire("a:x", 2, 0)
    path = b._lease_path(a.id)
    data = json.loads(path.read_text())
    data["pid"] = 4242
    path.write_text(json.dumps(data))
    fs.dead.add(4242)
    b.status()
    assert path.exists(), "first look only marks a suspect"
    fs.t += 1
    b.status()
    assert path.exists(), "second look inside the gap still keeps it"
    fs.dead.discard(4242)  # the process was between states: alive again
    fs.t += 5
    b.status()
    assert path.exists() and b._state()["suspects"] == {}
    fs.dead.add(4242)
    b.status()
    fs.t += 3.5
    b.status()
    assert not path.exists()


def test_stale_by_heartbeat(env):
    b, fs = make(env, load=0.0)
    a = b.acquire("a:x", 2, 0)
    fs.t += 130
    b.status()
    fs.t += 4
    b.status()
    assert not b._lease_path(a.id).exists()


def test_drain_and_hysteresis(env):
    b, fs = make(env, load=3.0)
    a = b.acquire("a:x", 6, 0)
    fs.start_hosts(os.getpid(), 6)
    fs.load = 9.8
    assert not b.status()["drain"]  # one check above 9.5 is not enough
    assert b.status()["drain"]      # two in a row
    assert a.refresh() == 4 and a.drain  # one host fewer per check while draining
    assert a.refresh() == 3
    fs.load = 9.2                   # above the resume line: still draining
    assert b.status()["drain"]
    fs.load = 8.0
    b.status()
    assert b.status()["drain"] is False  # two checks at or below 8.5 end it
    fs.load = 4.0
    fs.table = fs.table[:3]         # the engine closed its extra hosts
    assert a.refresh() >= 3


def test_single_blip_does_not_drain(env):
    b, fs = make(env, load=9.8)
    b.status()
    fs.load = 5.0
    b.status()
    fs.load = 9.8
    assert not b.status()["drain"]


def test_flag_off(env, monkeypatch):
    monkeypatch.setenv("TONEMATCH_HOST_BUDGET", "off")
    ls = hb.acquire("a:x", 9, cap=6)
    assert ls.granted == 6 and ls.refresh() == 6
    ls2 = hb.acquire("a:x", 3)
    assert ls2.granted == 3
    ls.release()
    assert not (env / "leases").exists()


def test_context_manager_releases(env):
    b, _ = make(env)
    with b.acquire("a:x", 2, 0) as ls:
        assert list((env / "leases").glob("*.json"))
        assert ls.granted == 2
    assert not list((env / "leases").glob("*.json"))


def test_caps_are_upper_bounds_and_user_cap_wins(env, monkeypatch):
    b, _ = make(env)
    assert b.acquire("a:x", 6, 0, cap=2).granted == 2
    (env / "limits.json").write_text(json.dumps({"max_hosts": 8, "target_load": 11, "max_load": 12}))
    (env.parent / "hb3").mkdir()
    (env.parent / "hb3" / "limits.json").write_text((env / "limits.json").read_text())
    b2 = hb.Budget(system=Fake(0.0, cores=12), directory=env.parent / "hb3")
    assert b2.acquire("b:y", 8, 0, cap=2).granted == 8  # the user's own limit replaces the RAM cap
    monkeypatch.setenv("TONEMATCH_NATIVE_MAX_HOSTS", "1")
    assert b2.acquire("c:z", 8, 0, cap=1, floor=0).wanted == 1


def test_expected_conditions_reserve_room(env):
    b, _fs = make(env)
    a = b.acquire("a:x", 1, 0, expected_hosts=4, expected_minutes=30)
    c = b.acquire("c:z", 6, 0, floor=0)
    assert a.granted == 1
    # a plans 4 later but asks for 1 now: its share is reserved, so c gets 3 and not the leftover 5
    assert c.granted == 3


_CHILD = r"""
import sys, time, json, subprocess
from tonematch.engines import host_budget as hb
ls = hb.acquire(sys.argv[1], 6, 20, floor=0)
print(json.dumps({{"job": sys.argv[1], "granted": ls.granted}}), flush=True)
time.sleep(float(sys.argv[2]))
ls.release()
"""


def test_concurrent_processes(env):
    envv = dict(os.environ, TONEMATCH_HOST_BUDGET_DIR=str(env), TONEMATCH_HOST_BUDGET_FAKE_LOAD="0.5",
                TONEMATCH_HOST_SAMPLE_S="0", PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD.format(), f"j{i}:t", "3"],
                              stdout=subprocess.PIPE, text=True, env=envv) for i in range(5)]
    time.sleep(1.5)
    # while all five hold or wait: never more than 6 hosts granted, and the lock left no debris
    b = hb.Budget(system=Fake(0.5), directory=env)
    total = sum(x["granted"] for ls in b.status()["leases_by_job"].values() for x in ls)
    outs = [json.loads(p.communicate()[0].strip()) for p in procs]
    assert total <= 6
    assert sum(o["granted"] for o in outs) <= 6
    assert not (env / "lock.d").exists()
    assert not list((env / "leases").glob("*.json"))


class _Eng:
    n = 0

    def __init__(self, **kw):
        _Eng.n += 1
        self.kw, self.closed = kw, False
        self.templates, self._variants, self._plugin_latency = {}, {}, 0

    def load(self, preset):
        pass

    def render(self, dry):
        return dry

    def close(self):
        self.closed = True


def test_pool_grows_and_shrinks_between_generations(env, monkeypatch):
    from tonematch.engines import native as nat

    fs = Fake(load=1.0)
    monkeypatch.setattr(hb, "_default", hb.Budget(system=fs, directory=env))
    monkeypatch.setattr(nat, "NativeEngine", _Eng)
    monkeypatch.setattr(nat, "max_hosts_by_memory", lambda *a, **k: 6)
    pool = nat.NativeRenderPool(workers=6, plugin_path="/x/Native.vst3")
    assert pool.workers == 6
    fs.table = []
    fs.start_hosts(os.getpid(), 6)
    # a burner adds 4 load: 5 + 6 hosts = 11 -> the pool shrinks at its next generation, never mid-render
    fs.load = 10.0
    fs.t += 1
    pool.render_generation(object(), [{}] * 4, [1.0])
    assert 1 <= pool.workers < 6
    small = pool.workers
    fs.table = fs.table[:small]
    fs.load = 3.0
    fs.t += 200  # the burner is gone and its samples have aged out
    for _ in range(4):  # drain ends after two quiet checks, then the pool regrows
        fs.t += 1
        fs.table = fs.table[:pool.workers]
        pool.render_generation(object(), [{}] * 4, [1.0])
    assert pool.workers == 6 > small
    pool.close()
    assert not list((env / "leases").glob("*.json"))


def test_sweet_spot_stops_at_diminishing_returns():
    # near-linear to 4, then SMT-like crumbs: the sweet spot is 4, not the largest total
    rate = {1: 10, 2: 20, 3: 30, 4: 40, 5: 42, 6: 43, 7: 44, 8: 44.5}
    s = hb.sweet_spot(rate, cores=8, ram_hosts=6)
    assert s["max_hosts"] == 4 and s["min_hosts"] == 1 and s["marginal"][5] < 0.5
    assert hb.sweet_spot(rate, cores=8, ram_hosts=2)["max_hosts"] == 2   # RAM bounds it
    assert hb.sweet_spot({1: 10, 2: 20, 3: 30}, cores=3, ram_hosts=None)["max_hosts"] == 2  # leaves a core


def test_unconfigured_notice(env, capsys, monkeypatch):
    monkeypatch.setattr(hb, "_noticed", False)
    hb.notice_once()
    assert "setup" in capsys.readouterr().err
    monkeypatch.setattr(hb, "_noticed", False)
    (env).mkdir(parents=True, exist_ok=True)
    (env / "limits.json").write_text(json.dumps({"min_hosts": 1, "max_hosts": 4}))
    hb.notice_once()
    assert capsys.readouterr().err == ""


def test_rising_load_is_projected(env):
    b, fs = make(env, load=4.0)
    fs.start_hosts(os.getpid(), 4)
    fs.sys_ref = None
    b.status()
    fs.t += 10
    fs.load = 4.0 + 1.5  # a burner ramping in: +1.5 in 10 s
    st = b.status()
    # without the projection the non-native estimate is 1.5 (capacity 6); with it, 5.5 - 4 + 4.5
    assert st["nonnative_load"] > 3.0 and st["allowed_total"] < 6
    b2, fs2 = make(env.parent / "flat", load=5.5)
    fs2.start_hosts(os.getpid(), 4)
    b2.status()
    fs2.t += 10
    assert b2.status()["nonnative_load"] == 1.5  # steady load: no projection
