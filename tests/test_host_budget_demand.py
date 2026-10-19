"""Demand and core counting for the host scaler (T149): fakes only, no Native, no real psutil."""
import json
import os
import types

import pytest

from tonematch.engines import host_budget as hb

pytestmark = pytest.mark.host_budget


class FakeOs:
    """A stand-in for the os module: only the attributes a test gives it exist."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def fake_psutil(queue=None, util=0.0, affinity=None):
    m = types.SimpleNamespace()
    if queue is not None:
        m.getloadavg = lambda: queue
    if util is not None:
        def cpu_percent(interval=None):
            if isinstance(util, Exception):
                raise util
            return util
        m.cpu_percent = cpu_percent
    if affinity is not None:
        m.Process = lambda: types.SimpleNamespace(cpu_affinity=lambda: affinity)
    return m


# ------------------------------------------------------------------ demand


def test_unix_uses_getloadavg():
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (3.0, 2.0, 1.0)), psutil_mod=None)
    assert (d.load, d.source, d.measured) == ((3.0, 2.0, 1.0), "os.getloadavg", True)


def test_windows_psutil_queue_plus_running_threads():
    # Windows: no os.getloadavg; the emulated queue counts waiting threads only, so running threads
    # (utilisation x cores) are added: 0.5 queue + 60 % of 10 cores = 6.5
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=fake_psutil(queue=(0.5, 0.4, 0.3), util=60.0),
                          platform="win32")
    assert d.measured and d.source == "psutil.getloadavg + cpu_percent"
    assert d.load == pytest.approx((6.5, 6.4, 6.3))


def test_windows_saturated_cpu_reads_at_the_limit_even_with_empty_queue():
    d = hb.measure_demand(8, _os=FakeOs(), psutil_mod=fake_psutil(queue=(0.0, 0.0, 0.0), util=97.0),
                          platform="win32")
    assert d.load[0] >= 8.0   # never 0 for a busy machine: the emulated warm-up zeros are not "idle"


def test_windows_queue_without_utilisation_is_not_a_measurement():
    d = hb.measure_demand(8, _os=FakeOs(), psutil_mod=fake_psutil(queue=(0.0, 0.0, 0.0), util=RuntimeError()),
                          platform="win32")
    assert not d.measured and d.note


def test_cpu_percent_fallback_is_a_floor_and_95_percent_is_the_limit():
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=fake_psutil(queue=None, util=40.0), platform="linux")
    assert d.measured and d.load == (4.0, 4.0, 4.0) and "cpu_percent" in d.source
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=fake_psutil(queue=None, util=95.0), platform="linux")
    assert d.load == (10.0, 10.0, 10.0)
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=fake_psutil(queue=None, util=100.0), platform="linux")
    assert d.load[0] == 10.0


def test_no_source_at_all_is_unmeasured_not_zero_load():
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=None)
    assert not d.measured and d.source == "none" and "at most 1 host" in d.note
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (_ for _ in ()).throw(OSError())), psutil_mod=None)
    assert not d.measured


class BlindFake(hb.System):
    def __init__(self, cores=10):
        self.t, self._c = 1000.0, cores

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def demand(self):
        return hb.Demand((0.0, 0.0, 0.0), "none", False, "no load source: granting at most 1 host")

    def loadavg(self):
        return (0.0, 0.0, 0.0)

    def cores(self):
        return self._c

    def ram_gb(self):
        return 64.0

    def procs(self):
        return []

    def pid_alive(self, pid):
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TONEMATCH_HOST_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TONEMATCH_HOST_BUDGET_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("TONEMATCH_HOST_SAMPLE_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_HEARTBEAT_S", "3600")
    monkeypatch.setenv("TONEMATCH_HOST_REGROW_S", "0")
    monkeypatch.setenv("TONEMATCH_HOST_STAGGER_S", "0")
    return tmp_path / "hb"


def test_no_measurement_grants_at_most_one_host_and_status_says_so(env):
    b = hb.Budget(system=BlindFake(cores=64), directory=env)
    with b.acquire("a", 8, floor=1) as la, b.acquire("b", 8, floor=0) as lb:
        assert la.granted == 1 and lb.refresh() == 0
        s = b.status()
    assert s["allowed_total"] == 1 and s["demand_measured"] is False
    assert "at most 1 host" in hb.format_status(s)


def test_unmeasured_is_not_an_idle_machine_in_the_same_setup_when_measured(env):
    class Idle(BlindFake):
        def demand(self):
            return hb.Demand((0.0, 0.0, 0.0), "os.getloadavg")
    b = hb.Budget(system=Idle(cores=64), directory=env)
    with b.acquire("a", 8, floor=1) as la:
        assert la.granted == 8


# ------------------------------------------------------------------ cores


def test_affinity_beats_cpu_count():
    n, src = hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: {0, 1, 2}, cpu_count=lambda: 64),
                             cgroup_root=hb.Path("/nonexistent"), proc_cgroup=hb.Path("/nonexistent"))
    assert (n, src) == (3, "sched_getaffinity")


def test_process_cpu_count_then_psutil_affinity_then_cpu_count():
    nope = {"cgroup_root": hb.Path("/nonexistent"), "proc_cgroup": hb.Path("/nonexistent")}
    n, src = hb.usable_cores(_os=FakeOs(process_cpu_count=lambda: 6, cpu_count=lambda: 12), **nope)
    assert (n, src) == (6, "process_cpu_count")
    n, src = hb.usable_cores(_os=FakeOs(cpu_count=lambda: 12), psutil_mod=fake_psutil(affinity=[0, 1, 2, 3]), **nope)
    assert (n, src) == (4, "psutil cpu_affinity")
    n, src = hb.usable_cores(_os=FakeOs(cpu_count=lambda: 12), psutil_mod=None, **nope)
    assert (n, src) == (12, "cpu_count")
    n, _ = hb.usable_cores(_os=FakeOs(cpu_count=lambda: None), psutil_mod=None, **nope)
    assert n == 2


def _cgroup(tmp_path, own="200000 100000", parent="max 100000"):
    tmp_path = tmp_path / str(abs(hash((own, parent))))
    tmp_path.mkdir(exist_ok=True)
    root = tmp_path / "cg"
    (root / "job" / "sub").mkdir(parents=True)
    (root / "job" / "sub" / "cpu.max").write_text(own + "\n")
    (root / "job" / "cpu.max").write_text(parent + "\n")
    (root / "cpu.max").write_text("max 100000\n")
    pc = tmp_path / "proc_cgroup"
    pc.write_text("0::/job/sub\n")
    return root, pc


def test_cgroup_v2_quota_lowers_the_core_count(tmp_path):
    root, pc = _cgroup(tmp_path, own="200000 100000")
    n, src = hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: set(range(16))), cgroup_root=root, proc_cgroup=pc)
    assert n == 2 and "cgroup" in src


def test_cgroup_fractional_quota_rounds_down_never_below_one_and_parent_counts(tmp_path):
    root, pc = _cgroup(tmp_path, own="50000 100000")
    assert hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: set(range(16))), cgroup_root=root,
                           proc_cgroup=pc)[0] == 1
    root, pc = _cgroup(tmp_path, own="max 100000", parent="350000 100000")
    assert hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: set(range(16))), cgroup_root=root,
                           proc_cgroup=pc)[0] == 3


def test_cgroup_max_or_quota_above_affinity_changes_nothing(tmp_path):
    root, pc = _cgroup(tmp_path, own="max 100000")
    assert hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: {0, 1}), cgroup_root=root, proc_cgroup=pc) == (2, "sched_getaffinity")
    root, pc = _cgroup(tmp_path, own="800000 100000")
    assert hb.usable_cores(_os=FakeOs(sched_getaffinity=lambda pid: {0, 1}), cgroup_root=root, proc_cgroup=pc)[0] == 2


# ------------------------------------------------------------------ thresholds


@pytest.mark.parametrize("cores,expect", [
    (4, (4.0, 3.6, 3.8, 3.4)),
    (10, (10.0, 9.0, 9.5, 8.5)),
    (64, (64.0, 57.6, 60.8, 54.4)),
])
def test_thresholds_are_fractions_of_the_core_count(env, cores, expect):
    cfg = hb.Config.load(BlindFake(cores=cores), env)
    assert (cfg.max_load, cfg.target_load, cfg.drain_load, cfg.resume_load) == expect
    assert cfg.cores == cores and cfg.fractions() == {"max_load": 1.0, "target_load": 0.9,
                                                     "drain_load": 0.95, "resume_load": 0.85}


def test_ten_cores_unchanged_exactly():
    assert hb.derived_limits(10) == {"max_load": 10.0, "target_load": 9.0, "drain_load": 9.5, "resume_load": 8.5}


def test_cores_override_env_and_limits_json_and_cli(env, monkeypatch, capsys):
    env.mkdir(parents=True)
    (env / "limits.json").write_text(json.dumps({"cores": 8}))
    cfg = hb.Config.load(BlindFake(cores=64), env)
    assert cfg.cores == 8 and cfg.sources["cores"] == "limits.json" and cfg.target_load == 7.2
    assert cfg.max_hosts == min(7, 64 * 0.5 // 2.5)
    monkeypatch.setenv("TONEMATCH_HOST_CORES", "20")
    cfg = hb.Config.load(BlindFake(cores=64), env)
    assert cfg.cores == 20 and cfg.sources["cores"] == "env" and cfg.drain_load == 19.0
    monkeypatch.delenv("TONEMATCH_HOST_CORES")
    (env / "limits.json").unlink()
    assert hb.main(["limits", "set", "--cores", "6"]) == 0
    assert json.loads((env / "limits.json").read_text()) == {"cores": 6}
    assert "cores=6[limits.json]" in capsys.readouterr().out


def test_explicit_target_keeps_fractional_margins_and_user_overrides_win(env):
    env.mkdir(parents=True)
    (env / "limits.json").write_text(json.dumps({"target_load": 30, "drain_load": 31}))
    cfg = hb.Config.load(BlindFake(cores=64), env)
    assert (cfg.target_load, cfg.drain_load, cfg.resume_load) == (30.0, 31.0, 26.8)
    assert cfg.max_load == 64.0


def test_status_shows_absolute_values_and_fractions(env):
    class Idle(BlindFake):
        def demand(self):
            return hb.Demand((1.0, 1.0, 1.0), "os.getloadavg")
    b = hb.Budget(system=Idle(cores=10), directory=env)
    out = hb.format_status(b.status())
    assert "target_load=9.0 (0.90x)" in out and "drain_load=9.5 (0.95x)" in out and "demand: os.getloadavg" in out


def test_real_system_reports_a_source():
    s = hb.System()
    d = s.demand()
    assert d.source and s.cores() >= 1 and s.cores_source()


# ------------------------------------------------------------------ busy cores (T192)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def seq_psutil(utils, queue=None):
    """cpu_percent returns the next value of ``utils`` each call (the first call is the priming one)."""
    it = iter(utils)
    m = types.SimpleNamespace(cpu_percent=lambda interval=None: next(it))
    if queue is not None:
        m.getloadavg = lambda: queue
    return m


def sampler(mod, clk=None):
    clk = clk or Clock()
    return hb.BusySampler(mod, clock=clk, sleep=clk.sleep), clk


def test_busy_cores_is_utilisation_times_cores_and_beats_the_load_average():
    smp, _ = sampler(seq_psutil([0.0, 66.0]))
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (8.4, 7.0, 6.0)), psutil_mod=smp.mod,
                          platform="darwin", sampler=smp)
    assert d.fast and d.measured and d.busy == pytest.approx(6.6) and d.loadavg1 == 8.4
    assert d.load == pytest.approx((6.6, 7.0, 6.0)) and "busy cores" in d.source


def test_busy_cores_are_smoothed_over_three_samples_without_blocking():
    smp, clk = sampler(seq_psutil([0.0, 90.0, 60.0, 30.0, 0.0]))
    seen = []
    for _ in range(4):
        seen.append(smp.percent())
        clk.t += 2.0
    assert seen == pytest.approx([90.0, 75.0, 60.0, 30.0])   # mean of the last 3
    # calls closer than the minimum gap reuse the last sample (a per-clip check does not shrink the window)
    n = len(smp.samples)
    smp.percent()
    assert len(smp.samples) == n


def test_stale_samples_are_dropped_and_the_sampler_re_primes():
    smp, clk = sampler(seq_psutil([0.0, 50.0, 0.0, 10.0]))
    assert smp.percent() == 50.0
    clk.t += 60.0
    assert smp.percent() == 10.0 and len(smp.samples) == 1


def test_loadavg_backstop_over_1_2_times_cores():
    smp, _ = sampler(seq_psutil([0.0, 50.0]))
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (12.5, 11.0, 9.0)), psutil_mod=smp.mod,
                          platform="linux", sampler=smp)
    assert d.load[0] == 12.5 and "backstop" in d.source and d.busy == pytest.approx(5.0)
    smp, _ = sampler(seq_psutil([0.0, 50.0]))
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (12.0, 11.0, 9.0)), psutil_mod=smp.mod,
                          platform="linux", sampler=smp)
    assert d.load[0] == pytest.approx(5.0)    # exactly 1.2 x cores is not over it


def test_without_psutil_falls_back_to_the_load_average_with_a_warning():
    hb._WARNED.discard("nopsutil")
    with pytest.warns(RuntimeWarning, match="psutil is not installed"):
        d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (8.0, 7.0, 6.0)), psutil_mod=None,
                              platform="darwin")
    assert d.load == (8.0, 7.0, 6.0) and d.source == "os.getloadavg" and not d.fast and "psutil" in d.note


def test_a_failing_psutil_falls_back_to_the_load_average():
    def boom(interval=None):
        raise RuntimeError("no")
    mod = types.SimpleNamespace(cpu_percent=boom)
    d = hb.measure_demand(10, _os=FakeOs(getloadavg=lambda: (3.0, 2.0, 1.0)), psutil_mod=mod,
                          platform="linux", sampler=hb.BusySampler(mod))
    assert d.load == (3.0, 2.0, 1.0) and not d.fast


def test_windows_path_is_unchanged_even_with_a_sampler():
    mod = fake_psutil(queue=(0.5, 0.4, 0.3), util=60.0)
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=mod, platform="win32",
                          sampler=hb.BusySampler(types.SimpleNamespace(cpu_percent=lambda interval=None: 1 / 0)))
    assert not d.fast and d.source == "psutil.getloadavg + cpu_percent"
    assert d.load == pytest.approx((6.5, 6.4, 6.3))
    # no os.getloadavg and a psutil load average off Windows: busy cores too
    smp, _ = sampler(seq_psutil([0.0, 40.0], queue=(9.0, 8.0, 7.0)))
    d = hb.measure_demand(10, _os=FakeOs(), psutil_mod=smp.mod, platform="linux", sampler=smp)
    assert d.fast and d.load[0] == pytest.approx(4.0)


def test_gate_hysteresis_does_not_flap():
    g = hb.BusyGate.for_cores(10)
    assert (g.pause_above, g.resume_below) == (9.5, 8.5)
    states = [g.update(x) for x in [9.4, 9.5, 9.6, 9.4, 9.0, 8.6, 8.5, 9.9, 8.4, 8.9, 9.4, 9.6, 8.0]]
    assert states == [False, False, True, True, True, True, True, True, False, False, False, True, False]
    # wobbling around 9.5 after a pause never resumes; wobbling around 8.5 after a resume never pauses
    g = hb.BusyGate.for_cores(10)
    g.update(9.8)
    assert all(g.update(x) for x in [9.3, 9.6, 9.0, 9.7, 8.6, 9.5])
    g.update(8.0)
    assert not any(g.update(x) for x in [8.6, 8.4, 9.0, 8.5, 9.4, 9.5])
    with pytest.raises(ValueError):
        hb.BusyGate(8.0, 9.0)


def test_scaler_drains_on_busy_cores_and_does_not_project_a_trend(env):
    class Busy(BlindFake):
        busy = 4.0

        def demand(self):
            return hb.Demand((self.busy, self.busy, self.busy), "busy", busy=self.busy, fast=True)
    s = Busy(cores=10)
    b = hb.Budget(system=s, directory=env)
    with b.acquire("a", 6, floor=1) as la:
        assert la.granted >= 4
        s.busy = 9.7
        for _ in range(3):
            s.t += 6
            la.refresh()
        assert b.status()["drain"] is True
        s.busy = 9.0     # between resume and drain: stays drained (hysteresis)
        for _ in range(3):
            s.t += 6
            la.refresh()
        assert b.status()["drain"] is True
        s.busy = 8.0
        for _ in range(3):
            s.t += 6
            la.refresh()
        assert b.status()["drain"] is False


# --------------------------------------------------------------------------- LoadGuard (T193)
def _guard(readings, **kw):
    it = iter(readings)
    slept = []
    g = hb.LoadGuard(cores=10, measure=lambda: hb.Demand((next(it), 0, 0), "fake"), sleep=slept.append, **kw)
    return g, slept


def test_load_guard_defaults_and_flag():
    g, _ = _guard([0.0])
    assert (g.gate.pause_above, g.gate.resume_below) == (9.5, 8.5)
    g, _ = _guard([0.0], max_busy=8.0)
    assert g.gate.pause_above == 8.0 and abs(g.gate.resume_below - 8.0 * 0.85 / 0.95) < 1e-9


def test_load_guard_wait_hysteresis_and_log():
    g, slept = _guard([5.0, 9.8, 9.0, 8.7, 8.4])
    assert g.wait() == 0.0 and not slept          # 5.0: no pause
    logged = []
    assert g.wait(logged.append, poll_s=3) >= 0.0  # 9.8 pause; 9.0 and 8.7 stay paused; 8.4 resumes
    assert logged == [9.8] and slept == [3, 3, 3] and g.last == 8.4 and not g.gate.paused


def test_load_guard_paused_nonblocking():
    g, _ = _guard([9.6, 9.0, 8.0])
    assert g.paused() and g.paused() and not g.paused()
