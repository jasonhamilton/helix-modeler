"""Machine-wide Native host scaler (DECISIONS 2026-10-17 "machine load limit" and "a Native host
scaler replaces the per-agent 2-process cap").

Every Native host process (``tonematch.engines.native_host``) uses about one core. Instead of ad
hoc caps and ``pgrep`` counting, each opener takes a *lease* from a file registry under
``cache/host_budget/`` and the scaler decides how many hosts the machine can hold right now. The
hard rule is unchanged: the 1-minute load average must never exceed MAX_LOAD, which is the core count
(10 on the 10-core Mac; DECISIONS 2026-10-18 "the CPU limit is the machine's core count"). CPU demand
must not go over 100 % of the cores this process may use. Nothing here is a fixed number: every
threshold is a fraction of the core count (see "Cores and demand").

Design
------
Registry (``cache/host_budget/``, override ``TONEMATCH_HOST_BUDGET_DIR``):
  ``lock.d``        atomic mkdir lock (portable; held for milliseconds, broken after 30 s if abandoned)
  ``leases/*.json`` one file per lease: pid, job, wanted, granted, requested/granted time, heartbeat
  ``state.json``    load samples, drain flag, stale-lease suspects
  ``limits.json``   the user's own limits (``limits set`` / ``calibrate --save``), see below

Limits (every constant is settable by env, by ``limits.json``, else derived from the machine; the
status output records value and source):
  cores         the CPUs this process may use      env TONEMATCH_HOST_CORES        auto: see "Cores and demand"
  max_hosts      most Native hosts at once          env TONEMATCH_HOST_HARD_CAP     auto: min(cores - 1, RAM / 2.5 GB x 0.5)
  target_load   expected 1-minute load to stay at  env TONEMATCH_HOST_TARGET_LOAD  auto: 0.90 x cores (9.0 on 10 cores)
  per_host_load load one host adds                 env TONEMATCH_HOST_PER_HOST_LOAD  1.0
  max_load      the line never crossed (floor rule) env TONEMATCH_HOST_MAX_LOAD     auto: 1.00 x cores
  drain_load    drain above this for 2 checks      env TONEMATCH_HOST_DRAIN_LOAD   target + 0.05 x cores (9.5 on 10)
  resume_load   drain ends at or below this, twice env TONEMATCH_HOST_RESUME_LOAD  target - 0.05 x cores (8.5 on 10)
  trend_s       project a rising load this far ahead (the 1-minute average lags a burst)  default 30 s
  stale_s, stale_gap_s, heartbeat_s, sample_s, regrow_s, wait_s: see ``Config``.
6 hosts is what this 10-core, 32 GB Mac derives; other machines derive their own, and a user on an
idle machine can raise or lower any of them (``limits set``, ``calibrate``). A hard cap the user
sets explicitly also overrides the old RAM caps that callers pass in (the user decided).

Cores and demand (``usable_cores``, ``measure_demand``; docs/notes/host-budget.md).
  Cores = the CPUs this process may use: ``os.sched_getaffinity(0)`` where it exists, else
  ``os.process_cpu_count()`` (3.13+), else the Windows process affinity (psutil), else ``os.cpu_count()``;
  on Linux also capped by a cgroup v2 ``cpu.max`` quota. Logical CPUs, not physical: load counts runnable
  threads against logical CPUs, so 100 % of them is where the run queue starts to grow.
  Demand, in core units (T192, DECISIONS 2026-10-19). On macOS and Linux with psutil it is BUSY CORES:
  CPU utilisation x cores, the mean of the last 3 samples (each a non-blocking ``cpu_percent`` window of
  1 s or more). The macOS 1-minute load average reads 1.5 to 2 above the cores actually busy and lags,
  which made the old guard pause long after the CPU had gone idle. The load average stays as a BACKSTOP:
  above 1.2 x cores it replaces the busy-core number (a deep run queue). Pause above 0.95 x cores, resume
  below 0.85 x cores (``drain_load`` / ``resume_load`` below, two checks each; ``BusyGate`` for scripts).
  Without psutil: the load average, with a warning. Otherwise, first source that exists:
  ``os.getloadavg()`` (macOS, Linux);
  ``psutil.getloadavg()`` (Windows: queue length emulation, plus the running threads from CPU
  utilisation x cores); CPU utilisation x cores (utilisation of 95 % or more reads as "at the limit");
  else nothing. A failed measurement is never 0: with no measurement at all the scaler grants at most 1
  host and ``status`` says so.

Capacity. ``capacity = min(max_hosts, cores - 1, floor((target_load - nonnative) / per_host_load))``
where ``nonnative = max(0, load1 - running_hosts x per_host_load)``, taken as the max over the last
few samples so the lag of a 1-minute average cannot hide hosts that just started. (The brief's
``room = floor((target - nonnative - pending) / per) + running`` counts running hosts twice when
load1 already contains them; this is the same quantity written once: free headroom beyond the
running and pending hosts, plus the running hosts. ``pending`` enters the grant arithmetic below.)
Hosts that are not leased (old scripts) are counted from the process table and take capacity first.

Grants. The leases share ``pool = capacity - unleased_running``. A lease holds ``occupied =
max(granted, started)`` where ``started`` counts native_host children of the lease's pid; the
difference ``granted - started`` is ``pending``, load that is coming and not yet in load1. A lease may
grow only into ``pool - sum(occupied)``, so the expected load after every grant stays at or below
target_load. Fair share: jobs split ``pool`` evenly in rounds, one host per job per round, earliest
requester first, never more than a job wants, and a job that holds a lease never drops below 1.
Floor: when nothing is held, 1 host is granted if ``load1 + pending + 1 <= max_load``.

Drain. load1 above ``drain_load`` on 2 consecutive checks (a check is one new sample, at least
``sample_s`` apart) sets ``drain``: the pool is cut by one host per check (never below 1 per job) until
load1 is at or below ``resume_load`` on 2 checks. Engines act on it only at a safe point (between
generations, ``Lease.refresh``); a host is never killed mid-render. After a shrink a lease does not
regrow for ``regrow_s`` (anti-flap).

Stale leases. A lease whose pid is dead or whose heartbeat is older than ``stale_s`` is marked a
suspect; only a second look at least ``stale_gap_s`` later that still finds it stale reclaims it
(the pedal lock rule: a process between states is not misread).

Expected conditions (optional): ``acquire(..., expected_hosts=, expected_minutes=)``. While the job's
window is open its fair-share demand is at least ``expected_hosts``, so later jobs see the planned
demand and leave room for it. The job is still granted no more than it asks for.

``TONEMATCH_HOST_BUDGET=off`` restores the old behaviour exactly: no leases, no files.
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Self

HOST_MARK = "tonematch.engines.native_host"
ROOT = Path(__file__).resolve().parents[3]


class HostBudgetTimeout(TimeoutError):
    """No host could be granted within the timeout."""


def enabled() -> bool:
    return os.environ.get("TONEMATCH_HOST_BUDGET", "on").strip().lower() not in ("off", "0", "false", "no")


def budget_dir() -> Path:
    return Path(os.environ.get("TONEMATCH_HOST_BUDGET_DIR") or ROOT / "cache" / "host_budget")


# --------------------------------------------------------------------------- cores and demand

MAX_FRAC = 1.0        # the line never crossed: 100 % of the cores this process may use
TARGET_FRAC = 0.9     # expected load to stay at
MARGIN_FRAC = 0.05    # drain = target + this, resume = target - this (fractions of the core count)
AT_LIMIT_UTIL = 95.0  # CPU utilisation (percent) read as "at the limit" when no queue can be seen

_UNSET = object()


def derived_limits(cores: int) -> dict[str, float]:
    """Every threshold as a fraction of ``cores`` (10 cores: max 10, target 9, drain 9.5, resume 8.5)."""
    target = round(TARGET_FRAC * cores, 2)
    return {"max_load": round(MAX_FRAC * cores, 2), "target_load": target,
            "drain_load": round(target + MARGIN_FRAC * cores, 2),
            "resume_load": round(target - MARGIN_FRAC * cores, 2)}


def _cgroup_cpu_limit(root: Path = Path("/sys/fs/cgroup"), proc_cgroup: Path = Path("/proc/self/cgroup")
                      ) -> float | None:
    """Smallest cgroup v2 ``cpu.max`` quota (in CPUs) on this process's cgroup path, or None."""
    rel = ""
    with contextlib.suppress(OSError):
        for line in proc_cgroup.read_text().splitlines():
            if line.startswith("0::"):
                rel = line[3:].strip()
    best: float | None = None
    parts = [x for x in rel.split("/") if x]
    for depth in range(len(parts), -1, -1):  # own cgroup, then each parent, then the root
        f = root.joinpath(*parts[:depth]) / "cpu.max"
        try:
            quota, period = f.read_text().split()[:2]
            if quota != "max" and float(period) > 0:
                q = float(quota) / float(period)
                best = q if best is None else min(best, q)
        except (OSError, ValueError):
            continue
    return best


def usable_cores(_os=os, psutil_mod=_UNSET, cgroup_root: Path = Path("/sys/fs/cgroup"),
                 proc_cgroup: Path = Path("/proc/self/cgroup")) -> tuple[int, str]:
    """(logical CPUs this process may use, how we know). Affinity, then ``os.process_cpu_count``
    (3.13+), then the Windows process affinity (psutil), then ``os.cpu_count``; then a Linux cgroup v2
    ``cpu.max`` quota can only lower it (a fractional quota rounds down, never below 1)."""
    n, src = 0, ""
    if hasattr(_os, "sched_getaffinity"):
        with contextlib.suppress(OSError):
            n, src = len(_os.sched_getaffinity(0)), "sched_getaffinity"
    if not n and hasattr(_os, "process_cpu_count"):
        n, src = _os.process_cpu_count() or 0, "process_cpu_count"
    if not n:
        mod = psutil_mod
        if mod is _UNSET:
            try:
                import psutil as mod
            except ImportError:
                mod = None
        if mod is not None:
            with contextlib.suppress(Exception):
                n, src = len(mod.Process().cpu_affinity()), "psutil cpu_affinity"
    if not n:
        n, src = _os.cpu_count() or 2, "cpu_count"
    quota = _cgroup_cpu_limit(cgroup_root, proc_cgroup)
    if quota is not None and quota < n:
        n, src = max(1, math.floor(quota + 1e-9)), f"cgroup cpu.max ({quota:g} CPUs)"
    return max(1, int(n)), src


@dataclass(frozen=True)
class Demand:
    """CPU demand in core units. ``load[0]`` is what the scaler compares with its thresholds: busy cores
    where psutil can measure them (``fast``), else the run-queue length (1-minute load average)."""
    load: tuple[float, float, float]
    source: str
    measured: bool = True   # False: nothing could be measured; ``load`` is not evidence of an idle machine
    note: str = ""
    busy: float | None = None       # smoothed CPU utilisation x cores (None: not measured this way)
    loadavg1: float | None = None   # the 1-minute load average, when the system has one
    fast: bool = False              # True: load[0] has no 1-minute lag (busy cores), so no trend projection


BACKSTOP_FRAC = 1.2      # pause when the 1-minute load average exceeds this x cores (a deep run queue)
BUSY_SAMPLES = 3         # busy cores are the mean of the last this many samples
BUSY_MIN_GAP_S = 1.0     # a sample covers at least this long a window
BUSY_STALE_S = 20.0      # samples older than this are dropped (the caller was not looking)
BUSY_PRIME_S = 0.5       # the first sample in a process waits this long for its window


class BusySampler:
    """CPU utilisation (percent, all logical CPUs) smoothed over the last ``n`` samples.

    Each sample is ``psutil.cpu_percent(interval=None)``: the utilisation since the previous call, so no
    call blocks. The first call in a process (or after a long silence) primes psutil and waits
    ``prime_s`` for a real window. Calls closer than ``min_gap`` reuse the last sample, so a loop that
    checks every clip does not shrink the window."""

    def __init__(self, mod, n: int = BUSY_SAMPLES, min_gap: float = BUSY_MIN_GAP_S,
                 stale: float = BUSY_STALE_S, prime_s: float = BUSY_PRIME_S,
                 clock=time.monotonic, sleep=time.sleep):
        self.mod, self.n, self.min_gap, self.stale, self.prime_s = mod, n, min_gap, stale, prime_s
        self.clock, self.sleep = clock, sleep
        self.samples: list[float] = []
        self.last_t: float | None = None
        self._lock = threading.Lock()

    def percent(self) -> float | None:
        """Smoothed utilisation in percent, or None when psutil cannot measure it."""
        with self._lock:
            try:
                now = self.clock()
                if self.last_t is not None and now - self.last_t > self.stale:
                    self.samples, self.last_t = [], None
                if self.last_t is None:
                    self.mod.cpu_percent(interval=None)   # prime: the first reading is meaningless
                    self.sleep(self.prime_s)
                    self.samples.append(float(self.mod.cpu_percent(interval=None)))
                    self.last_t = self.clock()
                elif now - self.last_t >= self.min_gap:
                    self.samples.append(float(self.mod.cpu_percent(interval=None)))
                    self.last_t = now
            except Exception:  # noqa: BLE001  (any psutil failure means: no utilisation)
                return None
            self.samples = self.samples[-self.n:]
            return sum(self.samples) / len(self.samples) if self.samples else None


_SAMPLERS: dict[int, BusySampler] = {}
_WARNED: set[str] = set()


def _sampler_for(mod) -> BusySampler:
    smp = _SAMPLERS.get(id(mod))
    if smp is None or smp.mod is not mod:
        smp = _SAMPLERS[id(mod)] = BusySampler(mod)
    return smp


def _warn_once(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        import warnings
        warnings.warn(msg, RuntimeWarning, stacklevel=3)


class BusyGate:
    """Pause/resume with hysteresis on a demand number (busy cores): pause when it rises above
    ``pause_above``, resume only when it falls below ``resume_below``. In between it keeps its state, so
    a number that wobbles around the line cannot flap."""

    def __init__(self, pause_above: float, resume_below: float):
        if resume_below > pause_above:
            raise ValueError("resume_below must not exceed pause_above")
        self.pause_above, self.resume_below, self.paused = float(pause_above), float(resume_below), False

    @classmethod
    def for_cores(cls, cores: int, pause_frac: float = 0.95, resume_frac: float = 0.85) -> BusyGate:
        return cls(pause_frac * cores, resume_frac * cores)

    def update(self, demand: float) -> bool:
        if self.paused:
            if demand < self.resume_below:
                self.paused = False
        elif demand > self.pause_above:
            self.paused = True
        return self.paused


def measure_demand(cores: int, _os=os, psutil_mod=_UNSET, platform: str = sys.platform,
                   sampler: BusySampler | None = None) -> Demand:
    """The one place demand is measured. First source that works:
      0. macOS, Linux with psutil: BUSY CORES = CPU utilisation x cores, smoothed over the last 3
         samples (``BusySampler``). The 1-minute load average stays as a backstop: above
         ``BACKSTOP_FRAC`` x cores (12 on 10 cores) the demand reads as that load instead.
      1. ``os.getloadavg()`` (macOS, Linux; also the fallback, with a warning, when psutil is missing).
      2. ``psutil.getloadavg()``. On Windows it is emulated from the processor queue length, which
         counts waiting threads only, so the running threads (CPU utilisation x cores) are added.
         (The Windows path is unchanged.)
      3. CPU utilisation x cores, a floor: a queue beyond 100 % is invisible, so utilisation of 95 % or
         more is read as exactly at the limit (``cores``).
      4. Nothing: ``measured=False``. Never 0 as "idle"."""
    mod = psutil_mod
    if mod is _UNSET:
        try:
            import psutil as mod
        except ImportError:
            mod = None
    fn = getattr(_os, "getloadavg", None)
    if fn is not None:
        try:
            la = tuple(float(x) for x in fn())
        except OSError:
            la = None
        if la is not None:
            if platform != "win32" and mod is not None:
                u = (sampler or _sampler_for(mod)).percent()
                if u is not None:
                    busy = u / 100.0 * cores
                    backstop = BACKSTOP_FRAC * cores
                    hit = la[0] > backstop
                    d0 = max(busy, la[0]) if hit else busy
                    note = (f"busy cores {busy:.2f} (utilisation {u:.0f} %, last {BUSY_SAMPLES} samples); "
                            f"load average {la[0]:.2f}" + (f" is over the backstop {backstop:.1f}" if hit
                                                           else f" (backstop {backstop:.1f})"))
                    return Demand((d0, la[1], la[2]), "psutil.cpu_percent busy cores"
                                  + (" + loadavg backstop" if hit else ""), True, note,
                                  busy=busy, loadavg1=la[0], fast=True)
            if platform != "win32" and mod is None:
                _warn_once("nopsutil", "psutil is not installed: CPU demand falls back to the 1-minute "
                           "load average, which lags and over-reads; pip install psutil")
            return Demand(la, "os.getloadavg", True,
                          "psutil not installed: load average only (lags and over-reads busy cores)"
                          if platform != "win32" and mod is None else "", loadavg1=la[0])
    if mod is None:
        return Demand((0.0, 0.0, 0.0), "none", False,
                      "no load source (no os.getloadavg, psutil not installed): granting at most 1 host; "
                      "install psutil")

    def util() -> float | None:
        try:
            return float(mod.cpu_percent(interval=0.1))
        except Exception:  # noqa: BLE001  (any psutil failure means: no utilisation)
            return None

    queue = None
    if hasattr(mod, "getloadavg"):
        with contextlib.suppress(Exception):
            queue = tuple(float(x) for x in mod.getloadavg())
    if queue is not None and platform != "win32":
        u = (sampler or _sampler_for(mod)).percent()
        if u is not None:
            busy = u / 100.0 * cores
            hit = queue[0] > BACKSTOP_FRAC * cores
            return Demand((max(busy, queue[0]) if hit else busy, queue[1], queue[2]),
                          "psutil.cpu_percent busy cores" + (" + loadavg backstop" if hit else ""), True,
                          f"busy cores {busy:.2f}; load average {queue[0]:.2f}", busy=busy,
                          loadavg1=queue[0], fast=True)
        return Demand(queue, "psutil.getloadavg")
    u = util() if (queue is None or platform == "win32") else None
    if queue is not None:
        if u is None:
            # queue length only, running threads unseen: not a demand; never read as idle
            return Demand(queue, "psutil.getloadavg (queue only)", False,
                          "Windows queue length without CPU utilisation: granting at most 1 host")
        run = u / 100.0 * cores
        load = tuple(q + run for q in queue)
        if u >= AT_LIMIT_UTIL:
            load = tuple(max(x, float(cores)) for x in load)
        return Demand(load, "psutil.getloadavg + cpu_percent")
    if u is None:
        return Demand((0.0, 0.0, 0.0), "none", False,
                      "psutil could not measure load or CPU utilisation: granting at most 1 host")
    d = u / 100.0 * cores
    if u >= AT_LIMIT_UTIL:
        d = float(cores)
    return Demand((d, d, d), "psutil.cpu_percent (floor)", True,
                  "no load average on this system; demand is CPU utilisation x cores and cannot see a queue")


class LoadGuard:
    """The one load guard for scripts (T193): ``measure_demand`` (busy cores) behind a ``BusyGate``.

    ``max_busy`` is the pause line in busy cores (None: 0.95 x cores); the resume line keeps the 0.85/0.95
    ratio (0.85 x cores by default). ``cores`` defaults to ``usable_cores()``. ``measure`` and ``sleep``
    are injectable for tests. ``last`` is the most recent demand reading (``load[0]``)."""

    def __init__(self, max_busy: float | None = None, cores: int | None = None, *, measure=None,
                 sleep=time.sleep):
        self.cores = int(cores) if cores else usable_cores()[0]
        pause = float(max_busy) if max_busy else 0.95 * self.cores
        self.gate = BusyGate(pause, pause * 0.85 / 0.95)
        self._measure = measure or (lambda: measure_demand(self.cores))
        self._sleep = sleep
        self.last = 0.0

    def demand(self) -> float:
        """Busy cores now (the load average only if it is over its backstop, or psutil is missing)."""
        self.last = float(self._measure().load[0])
        return self.last

    def paused(self) -> bool:
        """Non-blocking: take one reading and update the gate. True: do not start new work."""
        return self.gate.update(self.demand())

    def wait(self, log=None, poll_s: float = 5.0) -> float:
        """Block while the gate is paused (pause above the line, resume below the lower one). ``log`` gets
        the demand once when a pause starts. Returns the seconds spent paused (0.0 if it did not pause)."""
        if not self.paused():
            return 0.0
        t0 = time.monotonic()
        if log is not None:
            log(round(self.last, 2))
        while True:
            self._sleep(poll_s)
            if not self.paused():
                return time.monotonic() - t0


# --------------------------------------------------------------------------- system access


class System:
    """Everything the scaler reads from the machine; tests substitute a fake."""

    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)

    _info: Demand | None = None
    _cores_src = "injected"

    def loadavg(self) -> tuple[float, float, float]:
        """(load1, load5, load15) in core units. Tests override this; the scaler calls ``demand()``."""
        fake = os.environ.get("TONEMATCH_HOST_BUDGET_FAKE_LOAD")  # test hook for subprocess tests
        if fake:
            v = float(fake)
            self._info = Demand((v, v, v), "env fake")
            return (v, v, v)
        self._info = measure_demand(self.cores())
        return self._info.load

    def demand(self) -> Demand:
        """Load plus where it came from and whether it was measured at all."""
        self._info = None
        load = self.loadavg()
        return self._info or Demand(tuple(load), "injected")

    def cores(self) -> int:
        n, self._cores_src = usable_cores()
        return n

    def cores_source(self) -> str:
        return self._cores_src

    def ram_gb(self) -> float | None:
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        except (ValueError, OSError, AttributeError):
            return None

    def procs(self) -> list[tuple[int, int, str]]:
        """(pid, ppid, command) of every process; empty where ``ps`` does not exist."""
        try:
            r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                               timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                out.append((int(parts[0]), int(parts[1]), parts[2]))
        return out

    def cpu_by_pid(self) -> dict[int, float]:
        """pid -> CPU use in cores (ps %cpu / 100). Only the real System reads ``ps``; fakes override."""
        if type(self) is not System:
            return {}
        try:
            r = subprocess.run(["ps", "-axo", "pid=,pcpu="], capture_output=True, text=True,
                               timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        out = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                with contextlib.suppress(ValueError):
                    out[int(parts[0])] = float(parts[1]) / 100.0
        return out

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                import psutil
                return psutil.pid_exists(pid)
            except ImportError:
                return True  # the heartbeat decides
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


# --------------------------------------------------------------------------- configuration

_FIELDS = {  # name: (env suffix, kind)
    "cores": "int", "max_hosts": "int", "min_hosts": "int", "target_load": "float", "per_host_load": "float", "max_load": "float",
    "drain_load": "float", "resume_load": "float", "stale_s": "float", "stale_gap_s": "float",
    "heartbeat_s": "float", "sample_s": "float", "regrow_s": "float", "wait_s": "float",
    "window_s": "float", "trend_s": "float", "stagger_s": "float", "max_per_host": "float",
}  # (every field is a limit the user can set)


@dataclass
class Config:
    cores: int = 10
    max_hosts: int = 6
    min_hosts: int = 1
    target_load: float = 9.0
    per_host_load: float = 1.0
    max_load: float = 10.0
    drain_load: float = 9.5
    resume_load: float = 8.5
    stale_s: float = 120.0
    stale_gap_s: float = 3.0
    heartbeat_s: float = 20.0
    sample_s: float = 5.0
    regrow_s: float = 30.0
    wait_s: float = 3600.0
    window_s: float = 90.0
    trend_s: float = 30.0
    stagger_s: float = 10.0
    max_per_host: float = 3.0
    configured: bool = False
    cores_note: str = ""
    sources: dict = field(default_factory=dict)

    @classmethod
    def load(cls, system: System | None = None, directory: Path | None = None) -> Config:
        """env > limits.json > derived from this machine."""
        s = system or System()
        ram = s.ram_gb()
        user: dict = {}
        with contextlib.suppress(OSError, ValueError):
            user = json.loads(((directory or budget_dir()) / "limits.json").read_text())
        # cores first: every other derived limit is a fraction of it
        cores, cores_src, cores_note = s.cores(), "auto", s.cores_source()
        if "cores" in user:
            cores, cores_src, cores_note = int(user["cores"]), "limits.json", "set by you"
        env_cores = os.environ.get("TONEMATCH_HOST_CORES")
        if env_cores not in (None, ""):
            cores, cores_src, cores_note = int(float(env_cores)), "env", "set by TONEMATCH_HOST_CORES"
        cores = max(1, cores)
        auto = {"cores": cores, "max_hosts": max(1, min(cores - 1, int(ram * 0.5 / 2.5) if ram else 4)),
                **derived_limits(cores)}
        cfg = cls()
        cfg.cores_note = cores_note
        derived_drain = derived_resume = True
        for name, kind in _FIELDS.items():
            val, src = getattr(cfg, name), "default"
            if name in auto:
                val, src = auto[name], "auto"
            if name in user:
                val, src = user[name], "limits.json"
            if name == "cores":
                val, src = cores, cores_src
                setattr(cfg, name, int(val))
                cfg.sources[name] = src
                continue
            env = os.environ.get("TONEMATCH_HOST_" + name.upper())
            if env in (None, "") and name == "max_hosts":
                env = os.environ.get("TONEMATCH_HOST_HARD_CAP")  # earlier name
            if env not in (None, ""):
                val, src = env, "env"
            val = int(float(val)) if kind == "int" else float(val)
            setattr(cfg, name, val)
            cfg.sources[name] = src
            if name == "drain_load" and src != "auto":
                derived_drain = False
            if name == "resume_load" and src != "auto":
                derived_resume = False
        # drain / resume follow an explicit target unless set themselves
        if cfg.sources["target_load"] != "auto":
            if derived_drain:
                cfg.drain_load = round(cfg.target_load + MARGIN_FRAC * cores, 2)
            if derived_resume:
                cfg.resume_load = round(cfg.target_load - MARGIN_FRAC * cores, 2)
        # "configured" = the user chose the host range (limits.json or env), not just the derived one
        cfg.configured = cfg.sources["max_hosts"] in ("env", "limits.json")
        return cfg

    def as_dict(self) -> dict:
        d = asdict(self)
        d["fractions"] = self.fractions()
        return d

    def fractions(self) -> dict[str, float]:
        """Each load threshold as a multiple of the core count (target 0.90, drain 0.95, ...)."""
        return {k: round(getattr(self, k) / self.cores, 3) for k in
                ("max_load", "target_load", "drain_load", "resume_load")}


# --------------------------------------------------------------------------- the registry


def default_job() -> str:
    agent = os.environ.get("TONEMATCH_AGENT", "human")
    tag = os.environ.get("TONEMATCH_RUN_TAG") or f"{Path(sys.argv[0] or 'python').name}-{os.getpid()}"
    return f"{agent}:{tag}"


class Budget:
    def __init__(self, cfg: Config | None = None, system: System | None = None,
                 directory: Path | None = None):
        self.dir = Path(directory) if directory else budget_dir()
        self.sys = system or System()
        self.cfg = cfg or Config.load(self.sys, self.dir)
        (self.dir / "leases").mkdir(parents=True, exist_ok=True)

    # -- file plumbing ------------------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        d = self.dir / "lock.d"
        t0 = time.time()
        while True:
            try:
                os.mkdir(d)
                break
            except FileExistsError:
                try:
                    if time.time() - d.stat().st_mtime > 30:  # abandoned (a holder needs milliseconds)
                        shutil.rmtree(d, ignore_errors=True)
                        continue
                except OSError:
                    continue
                if time.time() - t0 > 60:
                    raise HostBudgetTimeout("host budget lock busy for 60 s")
                time.sleep(0.02)
        try:
            yield
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _lease_path(self, lid: str) -> Path:
        return self.dir / "leases" / f"{lid}.json"

    def _write(self, path: Path, obj: dict) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(obj, sort_keys=True))
        os.replace(tmp, path)

    def _read_leases(self) -> list[dict]:
        out = []
        for p in sorted((self.dir / "leases").glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def _state(self) -> dict:
        try:
            st = json.loads((self.dir / "state.json").read_text())
        except (OSError, ValueError):
            st = {}
        st.setdefault("samples", [])
        st.setdefault("drain", False)
        st.setdefault("hi", 0)
        st.setdefault("lo", 0)
        st.setdefault("drain_cap", None)
        st.setdefault("last_sample", 0.0)
        st.setdefault("suspects", {})
        return st

    # -- the view (always under the lock) -------------------------------------------------

    def _reap(self, st: dict, leases: list[dict], now: float) -> list[dict]:
        keep, suspects = [], st["suspects"]
        for ls in leases:
            stale = (not self.sys.pid_alive(int(ls["pid"]))) or now - float(ls["heartbeat"]) > self.cfg.stale_s
            if not stale:
                suspects.pop(ls["id"], None)
                keep.append(ls)
                continue
            first = suspects.setdefault(ls["id"], now)
            if now - first >= self.cfg.stale_gap_s:  # second look, a gap later, still stale
                suspects.pop(ls["id"], None)
                with contextlib.suppress(OSError):
                    self._lease_path(ls["id"]).unlink()
            else:
                keep.append(ls)
        for k in [k for k in suspects if k not in {x["id"] for x in leases}]:
            suspects.pop(k)
        return keep

    def _measured_per_host(self, procs, host_ppids) -> float:
        """Load one host really adds: its own CPU plus its worker's Python side (the worker's own
        CPU shared across its hosts), measured from the process table; never below ``per_host_load``
        and never above ``max_per_host``. A job with one host and a busy Python side counts about 2."""
        cfg = self.cfg
        cpu = self.sys.cpu_by_pid()
        if not cpu:
            return cfg.per_host_load
        kids: dict[int, list[int]] = {}
        for pid, ppid, cmd in procs:
            if HOST_MARK in cmd and "host_budget" not in cmd:
                kids.setdefault(ppid, []).append(pid)
        costs = []
        for parent, hosts in kids.items():
            tree = cpu.get(parent, 0.0) + sum(cpu.get(h, 0.0) for h in hosts)
            costs.append(tree / len(hosts))
        if not costs:
            return cfg.per_host_load
        return min(cfg.max_per_host, max(cfg.per_host_load, sum(costs) / len(costs)))

    def _view(self, st: dict) -> dict:
        """Reap, sample, and compute capacity and occupancy. Mutates ``st`` (caller saves it)."""
        cfg, now = self.cfg, self.sys.now()
        leases = self._reap(st, self._read_leases(), now)
        procs = self.sys.procs()
        host_ppids = [ppid for _, ppid, cmd in procs if HOST_MARK in cmd and "host_budget" not in cmd]
        running = len(host_ppids)
        dem = self.sys.demand()
        l1, l5, l15 = dem.load
        per = self._measured_per_host(procs, host_ppids)
        new_check = now - st["last_sample"] >= cfg.sample_s
        if new_check:
            st["samples"] = (st["samples"] + [{"t": now, "load1": l1, "running": running,
                                               "native": running * per}])[-6:]
            st["last_sample"] = now
            st["hi"] = st["hi"] + 1 if l1 > cfg.drain_load else 0
            if l1 > cfg.max_load:
                st["hi"] = max(st["hi"], 2)  # over the hard line: one look is enough
            st["lo"] = st["lo"] + 1 if l1 <= cfg.resume_load else 0
            if st["hi"] >= 2 and not st["drain"]:
                st["drain"] = True
            if st["drain"] and st["lo"] >= 2:
                st["drain"], st["drain_cap"] = False, None
        recent = [s for s in st["samples"] if now - s["t"] <= cfg.window_s]
        # The 1-minute average lags a burst by tens of seconds, so project the current trend
        # ``trend_s`` ahead (only a rise counts, measured against a sample 8 s or more old).
        proj = l1
        older = [s for s in recent if now - s["t"] >= 8.0]
        if older and cfg.trend_s > 0 and not dem.fast:  # busy cores have no 1-minute lag to project over
            ref = older[-1]
            proj = l1 + max(0.0, (l1 - ref["load1"]) / (now - ref["t"])) * cfg.trend_s
        nonnative = max([max(0.0, proj - running * per)] +
                        [max(0.0, s["load1"] - s.get("native", s["running"] * per)) for s in recent])
        loads = [s["load1"] for s in recent[-3:]] + [l1]
        load_eff = max(l1, sum(loads) / len(loads))
        capacity = max(0, min(cfg.max_hosts, max(1, cfg.cores - 1),
                              math.floor((cfg.target_load - nonnative) / per + 1e-9)))
        if not dem.measured:
            capacity = min(capacity, 1)  # blind: never read a failed measurement as an idle machine
        by_pid: dict[int, int] = {}
        granted_pid: dict[int, int] = {}
        job_of_pid: dict[int, str] = {}
        for ls in leases:
            if ls.get("demand_only"):
                continue  # a demand lease reserves room for its job, it opens no host itself
            pid = int(ls["pid"])
            by_pid.setdefault(pid, sum(1 for pp in host_ppids if pp == pid))
            granted_pid[pid] = granted_pid.get(pid, 0) + int(ls["granted"])
            job_of_pid.setdefault(pid, ls["job"])
        started = sum(by_pid.values())
        pending = sum(max(0, granted_pid[p] - by_pid[p]) for p in granted_pid)
        occupied = sum(max(granted_pid[p], by_pid[p]) for p in granted_pid)
        real_occ_job: dict[str, int] = {}
        for p, g in granted_pid.items():
            real_occ_job[job_of_pid[p]] = real_occ_job.get(job_of_pid[p], 0) + max(g, by_pid[p])
        dem_job: dict[str, int] = {}
        for ls in leases:
            if ls.get("demand_only"):
                dem_job[ls["job"]] = dem_job.get(ls["job"], 0) + int(ls["granted"])
        # Hosts a supervisor was granted and has not started yet: reserved, and pending load.
        extra_by_job = {j: max(0, g - real_occ_job.get(j, 0)) for j, g in dem_job.items()}
        extra = sum(extra_by_job.values())
        pending += extra
        occupied += extra
        unleased = max(0, running - started)
        pool = max(0, capacity - unleased)
        held = sum(granted_pid.values()) + extra
        if st["drain"]:
            cap = st["drain_cap"] if st["drain_cap"] is not None else held
            if new_check:
                cap = min(cap, held) - 1
            st["drain_cap"] = cap
            jobs = len({ls["job"] for ls in leases if int(ls["granted"]) > 0})
            over_line = load_eff > cfg.max_load  # over the hard line a job may drop below one host
            pool = min(pool, max(0 if over_line else jobs, cap))
        if pool < 1 and occupied == 0 and load_eff + pending + 1 <= cfg.max_load:
            pool = 1  # the floor: never refuse the only job while the hard line allows one host
        return {"now": now, "load": (l1, l5, l15), "demand": dem, "load_eff": load_eff, "running": running,
                "pending": pending, "occupied": occupied, "unleased": unleased, "nonnative": nonnative,
                "capacity": capacity, "pool": pool, "leases": leases, "started_by_pid": by_pid,
                "drain": st["drain"], "granted_total": held, "per_host": per, "extra_by_job": extra_by_job,
                "real_occ_by_job": real_occ_job}

    def _targets(self, view: dict) -> dict[str, int]:
        """Fair share of ``view['pool']`` among jobs, then among each job's leases."""
        leases = sorted(view["leases"], key=lambda ls: (ls["t_req"], ls["id"]))
        jobs: dict[str, list[dict]] = {}
        for ls in leases:
            jobs.setdefault(ls["job"], []).append(ls)
        demand, prio = {}, {}
        for j, ls_list in jobs.items():
            d_real = sum(int(x["wanted"]) for x in ls_list if not x.get("demand_only"))
            d_dem = sum(int(x["wanted"]) for x in ls_list if x.get("demand_only"))
            exp = sum(int(x.get("expected_hosts") or 0) for x in ls_list
                      if x.get("expected_until") and view["now"] < float(x["expected_until"]))
            demand[j] = max(d_real, d_dem, exp)
            prio[j] = 1 if any(x.get("priority") == "high" for x in ls_list) else 0
        # high-priority jobs are served first at every step; earliest requester first within a tier
        order = sorted(jobs, key=lambda j: -prio[j])
        share = dict.fromkeys(order, 0)
        rem = view["pool"]
        for _ in range(max(1, self.cfg.min_hosts)):  # first the per-job minimum, high jobs first
            for j in order:
                if rem > 0 and share[j] < min(demand[j], self.cfg.min_hosts):
                    share[j] += 1
                    rem -= 1
        for tier in (1, 0):
            members = [j for j in order if prio[j] == tier]
            progress = True
            while rem > 0 and progress:
                progress = False
                for j in members:
                    if rem > 0 and share[j] < demand[j]:
                        share[j] += 1
                        rem -= 1
                        progress = True
        out: dict[str, int] = {}
        for j, ls_list in jobs.items():
            left = share[j]
            for ls in ls_list:
                if ls.get("demand_only"):
                    out[ls["id"]] = min(int(ls["wanted"]), share[j])
                    continue
                g = min(int(ls["wanted"]), left)
                out[ls["id"]] = g
                left -= g
        return out

    # -- status -----------------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock():
            st = self._state()
            v = self._view(st)
            self._write(self.dir / "state.json", st)
        now = v["now"]
        by_job: dict[str, list[dict]] = {}
        for ls in v["leases"]:
            by_job.setdefault(ls["job"], []).append({
                "id": ls["id"], "pid": ls["pid"], "label": ls.get("label", ""), "granted": ls["granted"],
                "wanted": ls["wanted"], "started": v["started_by_pid"].get(int(ls["pid"]), 0),
                "age_s": round(now - ls["t_req"]), "heartbeat_age_s": round(now - ls["heartbeat"]),
                "expected_hosts": ls.get("expected_hosts"), "expected_minutes": ls.get("expected_minutes"),
                "demand_only": bool(ls.get("demand_only")), "priority": ls.get("priority", "normal"),
                "info": ls.get("info") or {}})
        return {"enabled": enabled(), "load1": v["load"][0], "load5": v["load"][1], "load15": v["load"][2],
                "load_smoothed": round(v["load_eff"], 2), "cores": self.cfg.cores,
                "cores_source": self.cfg.sources.get("cores", ""), "cores_note": self.cfg.cores_note,
                "demand_source": v["demand"].source, "demand_measured": v["demand"].measured,
                "demand_note": v["demand"].note,
                "allowed_total": v["capacity"], "pool_for_leases": v["pool"],
                "running_hosts": v["running"], "unleased_hosts": v["unleased"], "pending_hosts": v["pending"],
                "nonnative_load": round(v["nonnative"], 2), "drain": v["drain"],
                "per_host_measured": round(v["per_host"], 2),
                "leases_by_job": by_job, "constants": self.cfg.as_dict()}

    def allowed_total(self) -> int:
        return self.status()["allowed_total"]

    # -- leases -------------------------------------------------------------------------------

    def acquire(self, job: str | None = None, n_wanted: int = 1, timeout: float | None = None, *,
                cap: int | None = None, floor: int = 1, label: str = "",
                expected_hosts: int | None = None, expected_minutes: float | None = None,
                demand_only: bool = False, priority: str | None = None, info: dict | None = None) -> Lease:
        """Take a lease for up to ``n_wanted`` hosts (and at most ``cap``, the caller's own bound such
        as the RAM cap). Waits (polling) until at least ``floor`` hosts can be granted; ``floor=0``
        never waits. Raises ``HostBudgetTimeout`` after ``timeout`` s (default ``wait_s``)."""
        cfg = self.cfg
        priority = priority or os.environ.get("TONEMATCH_JOB_PRIORITY") or "normal"
        want = max(1 if floor else 0, int(n_wanted))
        # The caller's cap (RAM, TONEMATCH_NATIVE_MAX_HOSTS) is an upper bound, except that a hard cap
        # the user set on purpose (env or limits.json) replaces the derived RAM cap, not an explicit
        # TONEMATCH_NATIVE_MAX_HOSTS.
        user_cap = cfg.sources.get("max_hosts") in ("env", "limits.json")
        if cap is not None and (not user_cap or os.environ.get("TONEMATCH_NATIVE_MAX_HOSTS")):
            want = max(1 if floor else 0, min(want, int(cap)))
        now = self.sys.now()
        lease = {"id": uuid.uuid4().hex[:12], "pid": os.getpid(), "job": job or default_job(),
                 "label": label, "wanted": want, "floor": int(floor), "granted": 0, "t_req": now,
                 "t_grant": None, "heartbeat": now, "last_shrink": 0.0,
                 "expected_hosts": expected_hosts, "expected_minutes": expected_minutes,
                 "demand_only": bool(demand_only), "priority": priority, "info": info or {},
                 "expected_until": now + 60 * expected_minutes if expected_minutes and expected_hosts else None}
        deadline = now + (cfg.wait_s if timeout is None else timeout)
        with self._lock():
            self._write(self._lease_path(lease["id"]), lease)
        while True:
            g = self._regrant(lease["id"])
            if g is not None and g >= floor:
                break
            if floor == 0:
                break
            if self.sys.now() >= deadline:
                self._remove(lease["id"])
                raise HostBudgetTimeout(f"no Native host slot for {lease['job']} within the timeout "
                                        f"(see: python -m tonematch.engines.host_budget status)")
            self.sys.sleep(1.0)
        out = Lease(self, lease["id"], lease["job"], want, floor, g or 0)
        out.demand_only, out.priority = bool(demand_only), priority
        return out

    def _remove(self, lid: str) -> None:
        with self._lock(), contextlib.suppress(OSError):
            self._lease_path(lid).unlink()

    def _regrant(self, lid: str, new_wanted: int | None = None, info: dict | None = None) -> int | None:
        """Recompute this lease's grant (grow into free room, shrink to its fair share, honour
        drain and cooldown). Returns the grant, or None if the lease was reclaimed."""
        with self._lock():
            st = self._state()
            p = self._lease_path(lid)
            try:
                me = json.loads(p.read_text())
            except (OSError, ValueError):
                return None
            if new_wanted is not None:
                me["wanted"] = max(1 if me["floor"] else 0, int(new_wanted))
            if info is not None:
                me["info"] = info
            v = self._view(st)
            for i, ls in enumerate(v["leases"]):
                if ls["id"] == lid:
                    v["leases"][i] = me
            tg = self._targets(v)
            cur, target = int(me["granted"]), tg.get(lid, 0)
            now = v["now"]
            if me.get("demand_only"):
                # A supervisor's lease: its grant is the job's fair share, grown only into free room
                # (the job's own running hosts already count as occupied).
                eff = max(cur, v["real_occ_by_job"].get(me["job"], 0))
                free = max(0, v["pool"] - v["occupied"])
                cooling = now - float(me.get("last_shrink") or 0) < self.cfg.regrow_s and cur > 0
                if target < cur:
                    new = max(target, min(self.cfg.min_hosts, int(me["wanted"])) if me["floor"] else 0)
                    if new < cur:
                        me["last_shrink"] = now
                else:
                    base = max(cur, min(target, eff))
                    grow = 0 if (v["drain"] or cooling) else min(max(0, target - base), free, 1 if self.cfg.stagger_s > 0 else 99)
                    if grow and now - float(st.get("last_grow") or 0) < self.cfg.stagger_s:
                        grow = 0  # stagger: one new host per stagger_s, machine-wide
                    if grow:
                        st["last_grow"] = now
                    new = base + grow
            elif target < cur:
                new = max(target, min(self.cfg.min_hosts, int(me["wanted"])) if me["floor"] else 0)
                if new < cur:
                    me["last_shrink"] = now
            elif target > cur:
                free = max(0, v["pool"] - v["occupied"] + v["extra_by_job"].get(me["job"], 0))
                cooling = now - float(me.get("last_shrink") or 0) < self.cfg.regrow_s and cur > 0
                grow = 0 if (v["drain"] or cooling) else min(target - cur, free)
                if grow and self.cfg.stagger_s > 0 and v["extra_by_job"].get(me["job"], 0) == 0:
                    grow = min(grow, 1)  # not inside a supervisor's reservation
                    if now - float(st.get("last_grow") or 0) < self.cfg.stagger_s:
                        grow = 0
                    elif grow:
                        st["last_grow"] = now
                new = cur + grow
            else:
                new = cur
            if new != cur:
                me["granted"] = new
                me["t_grant"] = now
            me["heartbeat"] = now
            self._write(p, me)
            self._write(self.dir / "state.json", st)
            self._last_view = v
            return new

    def release_pid(self, pid: int) -> int:
        """Remove every lease held by ``pid`` (a worker that died; the supervisor cleans up)."""
        n = 0
        with self._lock():
            for ls in self._read_leases():
                if int(ls["pid"]) == int(pid):
                    with contextlib.suppress(OSError):
                        self._lease_path(ls["id"]).unlink()
                        n += 1
        return n

    def heartbeat(self, lid: str) -> None:
        p = self._lease_path(lid)
        try:
            ls = json.loads(p.read_text())
        except (OSError, ValueError):
            return
        ls["heartbeat"] = self.sys.now()
        if p.exists():
            self._write(p, ls)

    def draining(self) -> bool:
        return bool(self._state().get("drain"))


class Lease:
    """A claim on ``granted`` hosts. Use as a context manager; ``refresh()`` at each safe point."""

    def __init__(self, budget: Budget | None, lid: str, job: str, wanted: int, floor: int, granted: int):
        self.budget, self.id, self.job, self.wanted, self.floor = budget, lid, job, wanted, floor
        self.granted = granted
        self.drain = False
        self.demand_only, self.priority = False, "normal"
        self._stop = threading.Event()
        self._thread = None
        if budget is not None:
            self._thread = threading.Thread(target=self._beat, daemon=True, name="host-budget-heartbeat")
            self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.budget.cfg.heartbeat_s):
            with contextlib.suppress(Exception):
                self.budget.heartbeat(self.id)

    def __int__(self) -> int:
        return self.granted

    def refresh(self, n_wanted: int | None = None, info: dict | None = None) -> int:
        """Re-check at a safe point (between generations or renders). Returns the hosts this lease
        may hold now: grown into free room, or shrunk to the fair share or under drain (never below
        ``floor``). The caller closes or opens hosts to match; nothing is killed here."""
        if self.budget is None:
            if n_wanted is not None:
                self.wanted = int(n_wanted)
                self.granted = self.wanted
            return self.granted
        g = self.budget._regrant(self.id, n_wanted, info)
        if g is None:  # reclaimed while we were away: take a fresh slot
            fresh = self.budget.acquire(self.job, n_wanted or self.wanted, floor=self.floor,
                                        demand_only=self.demand_only, priority=self.priority, info=info)
            self.id, g = fresh.id, fresh.granted
            fresh._stop.set()
        if n_wanted is not None:
            self.wanted = int(n_wanted)
        self.granted = g
        v = getattr(self.budget, "_last_view", None)
        self.drain = bool(v and v["drain"])
        return g

    def trim_to(self, n: int) -> None:
        """Tell the registry fewer hosts opened than granted (a host failed to start)."""
        if self.budget is None or n >= self.granted:
            return
        p = self.budget._lease_path(self.id)
        with self.budget._lock():
            try:
                ls = json.loads(p.read_text())
            except (OSError, ValueError):
                return
            ls["granted"] = max(int(n), 1 if self.floor else 0)
            self.budget._write(p, ls)
        self.granted = ls["granted"]

    def release(self) -> None:
        self._stop.set()
        if self.budget is not None:
            self.budget._remove(self.id)
            self.budget = None
        self.granted = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a) -> None:
        self.release()


# --------------------------------------------------------------------------- module API

_default: Budget | None = None


def get_budget() -> Budget:
    global _default
    if _default is None or _default.dir != budget_dir():
        _default = Budget()
    return _default


def acquire(job: str | None = None, n_wanted: int = 1, timeout: float | None = None, **kw) -> Lease:
    """See ``Budget.acquire``. With ``TONEMATCH_HOST_BUDGET=off``: no registry, the old caps only."""
    if not enabled():
        cap = kw.get("cap")
        n = max(1, int(n_wanted)) if cap is None else max(1, min(int(n_wanted), int(cap)))
        return Lease(None, "off", job or default_job(), n, kw.get("floor", 1), n)
    return get_budget().acquire(job, n_wanted, timeout, **kw)


def allowed_total() -> int:
    return get_budget().allowed_total() if enabled() else 10**6


# --------------------------------------------------------------------------- CLI


def format_status(s: dict) -> str:
    c = s["constants"]
    lines = [(f"load1/5/15 {s['load1']:.2f} / {s['load5']:.2f} / {s['load15']:.2f}"
             f"  (smoothed {s['load_smoothed']}, non-native estimate {s['nonnative_load']}, cores {s['cores']})"),
             (f"allowed hosts {s['allowed_total']}  (for leases {s['pool_for_leases']})  running {s['running_hosts']}"
             f" (unleased {s['unleased_hosts']})  pending {s['pending_hosts']}  per-host cost {s.get('per_host_measured', '?')}  drain {'ON' if s['drain'] else 'off'}"
             f"  budget {'on' if s['enabled'] else 'OFF'}"),
             (f"demand: {s.get('demand_source', '?')} ({'measured' if s.get('demand_measured', True) else 'NOT MEASURED'})"
              f"  cores {c['cores']}[{c['sources'].get('cores', '')}: {c.get('cores_note', '')}]"),
             "limits: " + ", ".join(f"{k}={c[k]}[{c['sources'].get(k, '')}]" for k in ("max_hosts",))
             + ", " + ", ".join(f"{k}={c[k]} ({c['fractions'][k]:.2f}x)[{c['sources'].get(k, '')}]" for k in
                                ("target_load", "max_load", "drain_load", "resume_load"))]
    if s.get("demand_note"):
        lines.append("DEMAND: " + s["demand_note"])
    if not s["constants"].get("configured"):
        lines.append("NOTE: " + _notice(Config(**{k: v for k, v in c.items() if k in Config.__dataclass_fields__})))
    if not s["leases_by_job"]:
        lines.append("leases: none")
    for job, ls in s["leases_by_job"].items():
        for x in ls:
            if x.get("demand_only"):  # a supervised job: one row with its workers and progress
                i = x.get("info") or {}
                lines.append(f"  {job}  [{x.get('priority', 'normal')}] pid {x['pid']}: wanted {x['wanted']} granted "
                             f"{x['granted']} workers {i.get('workers', '?')} done {i.get('done', '?')} "
                             f"left {i.get('left', '?')} of {i.get('total', '?')}  beat {x['heartbeat_age_s']}s")
                continue
            exp = f" expects {x['expected_hosts']} for {x['expected_minutes']} min" if x["expected_hosts"] else ""
            lines.append(f"  {job}  pid {x['pid']} {x['label']}: granted {x['granted']}/{x['wanted']}"
                         f" started {x['started']}  age {x['age_s']}s  beat {x['heartbeat_age_s']}s{exp}")
    return "\n".join(lines)


def _limits_path() -> Path:
    return budget_dir() / "limits.json"


def _burn(secs: float) -> int:
    t0, n = time.perf_counter(), 0
    while time.perf_counter() - t0 < secs:
        for _ in range(20000):
            n += 1
    return n


def _notice(cfg: Config) -> str:
    return (f"Native host limits are the machine's derived defaults (max {cfg.max_hosts} hosts, target load "
            f"{cfg.target_load:.1f}), not yours. Run `python -m tonematch.engines.host_budget setup` "
            "(recommended: it benchmarks this machine and you decide), or set them: "
            "`... limits set --min-hosts N --max-hosts N`.")


_noticed = False


def notice_once() -> None:
    """One stderr line per process while the user has not chosen min/max hosts (never blocks)."""
    global _noticed
    if _noticed or not enabled():
        return
    _noticed = True
    cfg = Config.load()
    if not cfg.configured:
        print("[host budget] " + _notice(cfg), file=sys.stderr, flush=True)


def sweet_spot(rate: dict[int, float], cores: int, ram_hosts: int | None, min_marginal: float = 0.5) -> dict:
    """Pick the largest k whose k-th process still adds at least ``min_marginal`` of one process's
    throughput (diminishing returns end the climb), then leave headroom: at most cores - 1 and at most
    what RAM holds. The goal is the sweet spot, not the largest total."""
    ks = sorted(rate)
    base = rate[ks[0]] / ks[0]
    best, marg = ks[0], {}
    for a, b in itertools.pairwise(ks):
        marg[b] = (rate[b] - rate[a]) / (b - a) / base
    for k in ks[1:]:
        if marg[k] >= min_marginal:
            best = k
        else:
            break
    cap = max(1, min(best, cores - 1, ram_hosts or best))
    return {"max_hosts": cap, "min_hosts": 1, "target_load": round(min(0.9 * cores, cap + 2.0), 1),
            "max_load": float(cores), "marginal": {k: round(v, 2) for k, v in marg.items()}}


def calibrate(max_procs: int | None, secs: float, save: bool, force: bool, min_marginal: float = 0.5,
              ask: bool = False) -> int:
    """Benchmark an idle machine with k plain-Python burners (k = 1 ...), report throughput and each
    added process's marginal gain, suggest the sweet spot. The user makes the final decision: with
    ``ask`` (or a terminal) it prompts to accept or edit; ``--save`` writes without asking."""
    import multiprocessing as mp

    sysm = System()
    cores = sysm.cores()
    dem = sysm.demand()
    l1 = dem.load[0]
    if not dem.measured and not force:
        print(f"cannot tell whether the machine is idle ({dem.note}); pass --force", file=sys.stderr)
        return 3
    if l1 > 1.0 and not force:
        print(f"machine is not idle (load1 {l1:.2f}); stop other work or pass --force", file=sys.stderr)
        return 3
    ram = sysm.ram_gb()
    ram_hosts = int(ram * 0.5 / 2.5) if ram else None
    ks = list(range(1, (max_procs or cores) + 1))
    rate: dict[int, float] = {}
    ctx = mp.get_context("spawn")
    for k in ks:
        with ctx.Pool(k) as pool:
            t0 = time.perf_counter()
            total = sum(pool.map(_burn, [secs] * k))
            wall = time.perf_counter() - t0
        rate[k] = total / wall / 1e6
        gain = "" if k == 1 else f"  marginal {(rate[k] - rate[k - 1]) / rate[1]:5.2f} of one process"
        print(f"{k:>2} processes: {rate[k]:8.1f} M ops/s{gain}", flush=True)
    sug = sweet_spot(rate, cores, ram_hosts, min_marginal)
    print(f"cores {cores}, RAM {ram and round(ram)} GB (holds {ram_hosts} hosts at 2.5 GB). Sweet spot: "
          f"{sug['max_hosts']} hosts (the last one that still adds {min_marginal:.0%} of a process), "
          f"leaving {cores - sug['max_hosts']} cores of headroom.")
    print("suggested limits:", json.dumps({k: v for k, v in sug.items() if k != "marginal"}))
    final = {k: v for k, v in sug.items() if k != "marginal"}
    if ask or (sys.stdin.isatty() and not save):
        ans = input("Use these? [y]es / [e]dit / [n]o: ").strip().lower()
        if ans.startswith("e"):
            for k in ("min_hosts", "max_hosts", "target_load"):
                v = input(f"  {k} [{final[k]}]: ").strip()
                if v:
                    final[k] = int(v) if k != "target_load" else float(v)
        elif not ans.startswith("y"):
            print("nothing saved")
            return 0
        save = True
    if save:
        p = _limits_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(final, indent=1))
        print(f"saved to {p}")
    return 0


def setup() -> int:
    """First-run choice: set min/max by hand, or benchmark (recommended) and then decide."""
    cfg = Config.load()
    print(_notice(cfg))
    if not sys.stdin.isatty():
        return 2
    ans = input("[b]enchmark this machine (recommended) / set [m]anually / [k]eep derived defaults: ").strip().lower()
    if ans.startswith("b"):
        return calibrate(None, 3.0, False, False, ask=True)
    if ans.startswith("m"):
        cur = {}
        for k in ("min_hosts", "max_hosts"):
            v = input(f"  {k} [{getattr(cfg, k)}]: ").strip()
            cur[k] = int(v) if v else getattr(cfg, k)
        p = _limits_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, indent=1))
        print(f"saved to {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tonematch.engines.host_budget",
                                 description="Machine-wide Native host scaler.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status", help="load, limit, running/pending hosts, drain, leases by job")
    p.add_argument("--json", action="store_true")
    w = sub.add_parser("watch", help="status every few seconds")
    w.add_argument("--interval", type=float, default=5.0)
    lim = sub.add_parser("limits", help="show, set or reset your own limits (cache/host_budget/limits.json)")
    lim.add_argument("action", choices=["show", "set", "reset"])
    for k, kind in _FIELDS.items():
        lim.add_argument("--" + k.replace("_", "-"), type=int if kind == "int" else float)
    c = sub.add_parser("calibrate", help="benchmark an idle machine and suggest limits")
    c.add_argument("--max-procs", type=int)
    c.add_argument("--min-marginal", type=float, default=0.5,
                   help="stop climbing when one more process adds less than this share of one process")
    sub.add_parser("setup", help="first run: benchmark (recommended) or set min/max hosts yourself")
    c.add_argument("--seconds", type=float, default=3.0)
    c.add_argument("--save", action="store_true")
    c.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        s = get_budget().status()
        print(json.dumps(s, indent=1) if a.json else format_status(s))
    elif a.cmd == "watch":
        try:
            while True:
                print(time.strftime("%H:%M:%S"), format_status(get_budget().status()), "\n", flush=True)
                time.sleep(a.interval)
        except KeyboardInterrupt:
            pass
    elif a.cmd == "limits":
        path = _limits_path()
        cur = json.loads(path.read_text()) if path.is_file() else {}
        if a.action == "reset":
            path.unlink(missing_ok=True)
            cur = {}
        elif a.action == "set":
            cur.update({k: getattr(a, k) for k in _FIELDS if getattr(a, k) is not None})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cur, indent=1))
        cfg = Config.load()
        print("limits.json:", json.dumps(cur))
        print("in force:", ", ".join(f"{k}={getattr(cfg, k)}[{cfg.sources[k]}]" for k in _FIELDS))
    elif a.cmd == "calibrate":
        return calibrate(a.max_procs, a.seconds, a.save, a.force, a.min_marginal)
    elif a.cmd == "setup":
        return setup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
