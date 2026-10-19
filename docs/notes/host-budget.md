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
| **busy cores** = `psutil.cpu_percent` x cores, mean of the last 3 samples | macOS, Linux with psutil ([T192](../../board/TASKS.md#t192 "CPU demand = busy cores")) | The primary demand. Each sample is a non-blocking `cpu_percent(interval=None)` window of 1 s or more (the first in a process waits 0.5 s; samples older than 20 s are dropped). The 1-minute load average is a **backstop**: above 1.2 x cores it replaces the number, so a deep run queue still pauses. Without psutil: the load average below, with a warning. |
| `os.getloadavg()` | macOS, Linux | run-queue length |
| `psutil.getloadavg()` + `cpu_percent` x cores | Windows | psutil emulates the load average from the processor queue length (waiting threads only, and zeros for its first seconds), so the running threads, utilisation x cores, are added. Not yet run on Windows hardware: check once on a real Windows box. |
| `psutil.cpu_percent` x cores | any system without a load average | A floor: a queue beyond 100 % is invisible, so utilisation of 95 % or more reads as exactly at the limit (`cores`). |
| nothing | | `measured=False`. The scaler grants at most 1 host in total and `status` says so. A failed measurement is never read as 0 (an idle machine). |

psutil is a declared dependency on every OS since [T192](../../board/TASKS.md#t192 "CPU demand = busy cores") (`pyproject.toml`, BSD-3; `licence_check.py` PASS 2026-10-19).

## Thresholds (fractions of the core count)

| Limit | Fraction | 4 cores | 10 cores | 64 cores |
|---|---|---|---|---|
| max_load | 1.00 | 4 | 10 | 64 |
| target_load | 0.90 | 3.6 | 9 | 57.6 |
| drain_load | target + 0.05 | 3.8 | 9.5 | 60.8 |
| resume_load | target - 0.05 | 3.4 | 8.5 | 54.4 |

User overrides (`limits.json`, env, `limits set`) still win per field. `status` shows the absolute values with the fraction, the demand source, and the core count with its source.

Not changed: the lease and demand-lease file formats, `state.json`, the grant arithmetic. `os.cpu_count()` caps in `engines/native.py` are not yet on `usable_cores()`. (The scripts that called `os.getloadavg()` directly moved to `measure_demand` in [T193](../../board/TASKS.md#t193 "Switch the remaining scripts' own os.getloadavg() guards to…"), below.)

## Busy cores and hysteresis ([T192](../../board/TASKS.md#t192 "CPU demand = busy cores"), human 2026-10-19 "yes")

**Why.** On the 10-core M1 Pro the macOS 1-minute load average read 1.5 to 2 above the cores actually busy (load 8.0 to 8.8 against 6.6 to 6.8 busy) and lagged: after a pause the CPU fell to about 1 busy core within seconds while the average kept the job paused, a busy/idle sawtooth.

**What changed.**
- `Demand.load[0]` is busy cores on macOS and Linux when psutil is present (`Demand.fast`, `.busy`, `.loadavg1`). The Windows path (queue length plus running threads) is unchanged, as is the no-load-average floor.
- Pause above 0.95 x cores (9.5), resume below 0.85 x cores (8.5): the same numbers as `drain_load` and `resume_load`, now on busy cores. The scaler's target stays 0.9 x cores; drain needs 2 consecutive checks above 0.95, resume 2 below 0.85; `max_hosts` caps as before. The scaler no longer projects a trend ahead on a `fast` demand (busy cores have no 1-minute lag to compensate).
- `host_budget.BusyGate(pause_above, resume_below)` is the reusable hysteresis state machine; between the two lines it keeps its state, so it cannot flap.
- `scripts/drive_dataset_render.py`: per-clip guard is a `BusyGate` on `measure_demand().load[0]`; `--max-load` now means busy cores (default 0.95 x cores; resume at 0.85/0.95 of it); it polls every 5 s while paused (was 15 s).
- Not changed: lease and state file formats, the grant arithmetic, `usable_cores()`.

## All guards unified ([T193](../../board/TASKS.md#t193 "Switch the remaining scripts' own os.getloadavg() guards to…"), human 2026-10-19 "yes")

`host_budget.LoadGuard(max_busy=None, cores=None)` wraps `measure_demand(cores)` and a `BusyGate` (pause above `max_busy`, default 0.95 x cores; resume at `max_busy` x 0.85/0.95, so 0.85 x cores by default). `.wait(log, poll_s)` blocks while paused and returns the seconds paused; `.paused()` is the non-blocking check; `.demand()` / `.last` give busy cores. Every script that paused on its own `os.getloadavg()` now uses it:

| Script | Was | Now |
|---|---|---|
| `nodi_drive_e2e.py` (render, match), `nodi_drive_e2e_v2.py` (render, run) | `--max-load 9` on load1, poll 10 s / 15 s | `--max-load` busy cores, default 0.95 x cores; same polls; pause message printed once |
| `distortion_step3.py` run, `distortion_step4.py` render, `pedal_probe_sim.py` run | pause above `--max-load` 9, resume 1 below it, poll 5 s | `--max-load` busy cores; resume at 0.85/0.95 of it; poll 5 s; stats `load1_max` is now `busy_max`, progress `load1` is now `busy` |
| `drive_tone_families.py` run | pause above `--max-load` 9, poll 5 s | same as above; `load1_*_sampled` is now `busy_*_sampled` |
| `time_specialist_fit.py` and `stomp_robust.py` `wait_for_load(limit)` | wait while load1 >= 7, poll 60 s | `limit` in busy cores, default 0.95 x cores; resume below 0.85 x cores; poll 60 s; `stomp_robust.load_ok()` returns busy cores |
| `host_budget_live.py` | `--start-below` 7.0 and `--abort-load` 9.9 on load1 | busy cores; defaults 0.85 x cores and 0.99 x cores; the monitor records `busy` next to `load1` (kept as telemetry), summary adds `peak_busy` |
| `drive_dataset_render.py` | ([T192](../../board/TASKS.md#t192 "CPU demand = busy cores")) | unchanged |

The `--max-load` values passed on a command line before (for example 9) now mean 9 busy cores, which is about what the old load-average line allowed on this machine, since the average read 1.5 to 2 above busy cores. Old callers that pass nothing get 0.95 x cores.

Still reading `os.getloadavg()`, as telemetry only (never a pause): `benchmark_suite.py` (contention probe), `native_block_library.py`, `native_capture_bench.py`, `native_pool_bench.py`, `native_render_probe.py`, `distortion_pilot_step2.py` and `distortion_step4_analysis.py` log lines, the `load_at_start` in `stomp_robust.py`, the `load1` field of `drive_dataset_render.py` heartbeat, the `load1` telemetry in `host_budget_live.py`, and the before/after load average in `engines/native.py`.

**Live sample, 2026-10-19 14:37 (idle: the batch had stopped at 14:35), 10 cores.** Busy cores 0.6 to 1.4 against load average 2.4 to 3.0 over 60 s: the average reads about 1.5 to 2 higher than the busy cores even with nothing running. Gate stayed unpaused. No sample under the batch was possible.
