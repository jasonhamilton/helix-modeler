# Host budget: cores and demand ([T133](../../board/TASKS.md#t133 "Machine-wide Native host scaler"), [T138](../../board/TASKS.md#t138 "Elastic jobs for the host scaler"), [T149](../../board/TASKS.md#t149 "Host scaler measures demand portably and counts cores honestly"))

The scaler in `src/tonematch/engines/host_budget.py` keeps CPU demand at or below 100 % of the cores this process may use. Human, 2026-10-18: "the load limit is the core count ... possibly need another mechanism on windows ... not let the demand go over 100% of cpu usage and create a bottleneck." The limit is never a fixed number; "10" was this Mac's core count.

## Cores

`usable_cores()` returns the number of logical CPUs this process may use, and how it knows:

1. `os.sched_getaffinity(0)` where it exists (Linux).
2. else `os.process_cpu_count()` (Python 3.13+, honours affinity on Windows too).
3. else the Windows process affinity through psutil.
4. else `os.cpu_count()` (macOS has no affinity API).
5. On Linux a cgroup v2 `cpu.max` quota (own cgroup and every parent, smallest wins) can only lower the result. A fractional quota rounds down, never below 1.

**Logical, not physical.** Load average counts runnable threads against logical CPUs. With SMT, two threads on one physical core both run, so 100 % of the logical CPUs is where the run queue starts to grow: that is the bottleneck the human described. Physical cores matter for throughput (a second SMT thread adds well under a core of work), but that is the job of `max_hosts` and `calibrate` (the sweet spot), not of the load line. Override with `limits set --cores N`, `TONEMATCH_HOST_CORES`, or `limits.json`; every derived threshold follows.

## Demand

`measure_demand()` (reached through `System.demand()`) returns `(load1, load5, load15)` in core units, the source, and whether anything was measured. First source that works:

| Source | Where | Notes |
|---|---|---|
| `os.getloadavg()` | macOS, Linux | run-queue length |
| `psutil.getloadavg()` + `cpu_percent` x cores | Windows | psutil emulates the load average from the processor queue length (waiting threads only, and zeros for its first seconds), so the running threads, utilisation x cores, are added. Not yet run on Windows hardware: check once on a real Windows box. |
| `psutil.cpu_percent` x cores | any system without a load average | A floor: a queue beyond 100 % is invisible, so utilisation of 95 % or more reads as exactly at the limit (`cores`). |
| nothing | | `measured=False`. The scaler grants at most 1 host in total and `status` says so. A failed measurement is never read as 0 (an idle machine). |

psutil is a declared dependency on Windows only (`pyproject.toml`, BSD-3; reviewer: licence-check).

## Thresholds (fractions of the core count)

| Limit | Fraction | 4 cores | 10 cores | 64 cores |
|---|---|---|---|---|
| max_load | 1.00 | 4 | 10 | 64 |
| target_load | 0.90 | 3.6 | 9 | 57.6 |
| drain_load | target + 0.05 | 3.8 | 9.5 | 60.8 |
| resume_load | target - 0.05 | 3.4 | 8.5 | 54.4 |

User overrides (`limits.json`, env, `limits set`) still win per field. `status` shows the absolute values with the fraction, the demand source, and the core count with its source.

Not changed: the lease and demand-lease file formats, `state.json`, the grant arithmetic. Other scripts that call `os.getloadavg()` directly (`scripts/host_budget_live.py`, `benchmark_suite.py`, `time_specialist_fit.py`, `stomp_robust.py`) and `os.cpu_count()` caps in `engines/native.py` are not yet on this function; owners to adopt `host_budget.System().demand()` and `usable_cores()`.
