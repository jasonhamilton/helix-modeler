#!/usr/bin/env python3
"""benchmark-suite: run every benchmark test in a fixed order, time each one, and record the run.

Owner: benchmark. Writes reports/benchmark/history/<UTC stamp>.json (small, committed) and
reports/benchmark/latest.json, appends one ledger row (kind=benchmark, dataset=suite,
metric=suite_wall_clock_s), and refreshes the generated section of docs/BENCHMARKING.md.

Fixed order: engine smoke (Native, Stomp), hardware parity, loss trust check (proxy, fixed seeds),
synthetic recovery, separation stress, block-probe validate, unit tests (pytest -q), lint (ruff).

  python scripts/benchmark_suite.py                  # everything, with the pedal if it is free
  python scripts/benchmark_suite.py --no-hardware    # Stomp tests are skipped ("no pedal")
  python scripts/benchmark_suite.py --only unit_tests lint --no-ledger
  python scripts/benchmark_suite.py --render-doc     # refresh only the doc section from latest.json

Each test is a child process, one at a time (so the Native host RAM cap in the engines holds and
timings do not fight each other). The hardware rules live in the scripts it calls: HX Edit closed,
edit buffer only, the pedal health check, restore of 04C and the byte-diff. The suite adds a
pre-check (HX Edit, another job on the pedal) and never interrupts another agent's job. No song
audio is read or written.

Statuses: pass, fail, skipped (we chose or were unable to run it), not available (the thing it
needs does not exist yet; this is neither a pass nor a failure).
Exit 0 when nothing failed, 1 when a test failed.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

PASS, FAIL, SKIPPED, NA = "pass", "fail", "skipped", "not available"
STATUSES = (PASS, FAIL, SKIPPED, NA)
AGENT = "benchmark"
SCHEMA = 1
REPORT_DIR = ROOT / "reports" / "benchmark"
HISTORY_DIR = REPORT_DIR / "history"
LATEST = REPORT_DIR / "latest.json"
DOC = ROOT / "docs" / "BENCHMARKING.md"
LEDGER = ROOT / "ledger" / "runs.csv"
START, END = "<!-- latest-run:start -->", "<!-- latest-run:end -->"

# Registered before the first run, never used for tuning or for any earlier evaluation
# (tuning used 0 and 1; 2026, 9101-9103 and 9201-9203 are spent).
TRUST_SEEDS = (9301, 9302)
TRUST_ITEMS_PER_SEED = 20
TRUST_TOLERANCE = 0.05          # a held-out rank-1 drop beyond this, per term, is a regression
PARITY_NULL_DB = -30.0
PARITY_TAIL_DB = 2.0
PARITY_DELAY_PAIR = ("B00", "BT0")
HW_PRESETS = ROOT / "cache" / "stomp" / "hw" / "presets"
PARITY_PRESETS = ("K", "A", "B0")
STOMPD = ROOT / "cache" / "stomp" / "bin" / "stompd"
STOMPD_COMPILE = ROOT / "cache" / "stomp" / "bin" / "stompd-compile"
BUSY_PATTERN = r"stompd|parity_null\.py|engine_smoke\.py|stomp_hw\.py|reamp_capture\.py|stomp_inject\.py"
HEALTH_WORDS = ("power-cycle", "power cycle", "PowerCycleNeeded", "does not answer")


# ----------------------------------------------------------------------------- plumbing


@dataclass
class CmdResult:
    rc: int
    out: str = ""
    err: str = ""


Runner = Callable[[list[str], dict, float | None], CmdResult]


def default_runner(argv: list[str], env: dict, timeout: float | None) -> CmdResult:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=ROOT,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        return CmdResult(124, "", f"timed out after {timeout:.0f} s: {e}")
    except OSError as e:
        return CmdResult(127, "", str(e))
    return CmdResult(p.returncode, p.stdout, p.stderr)


class Probes:
    """What the machine looks like right now. Replaced by a stub in tests."""

    def hx_edit_running(self) -> bool:
        return bool(_pgrep("HX Edit"))

    def pedal_on_bus(self) -> bool:
        from tonematch.engines.stomp_bridge import stomp_on_bus

        return bool(stomp_on_bus())

    def pedal_jobs(self) -> list[str]:
        """Other processes holding or about to use the pedal (not this suite's children)."""
        mine = {str(os.getpid())}
        return [ln for ln in _pgrep(BUSY_PATTERN) if ln.split(" ", 1)[0] not in mine]

    def native_hosts(self) -> int:
        return len(_pgrep("native_host"))

    def load_avg(self) -> float:
        return round(os.getloadavg()[0], 2)

    def machine(self) -> dict:
        return machine_info()


def _pgrep(pattern: str) -> list[str]:
    r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True, check=False)
    me = str(os.getpid())
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return [ln for ln in lines if ln.split(" ", 1)[0] != me and "benchmark_suite" not in ln
            and "pgrep" not in ln]


def machine_info() -> dict:
    info: dict = {"os": platform.system(), "os_version": platform.mac_ver()[0] or platform.release(),
                  "arch": platform.machine(), "cpu_count": os.cpu_count(),
                  "python": platform.python_version()}
    try:
        info["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30)
    except (ValueError, OSError, AttributeError):
        info["ram_gb"] = None
    try:
        from tonematch.engines.native import locate_plugin

        plug = locate_plugin()
        pl = plug / "Contents" / "Info.plist" if plug else None
        if pl and pl.exists():
            info["native_version"] = plistlib.loads(pl.read_bytes()).get("CFBundleShortVersionString")
    except Exception as e:  # noqa: BLE001
        info["native_version_error"] = f"{type(e).__name__}: {e}"
    return info


def git_state() -> tuple[str, bool]:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                                      stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.call(["git", "diff", "--quiet"], cwd=ROOT) != 0
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return "nogit", False


def ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Result:
    status: str
    reason: str = ""
    key_result: str = ""
    metrics: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class Spec:
    id: str
    name: str
    checks: str
    pass_rule: str
    fn: Callable[[Ctx], Result]
    hardware: bool = False


@dataclass
class Ctx:
    runner: Runner = default_runner
    probes: Probes = field(default_factory=Probes)
    no_hardware: bool = False
    no_ledger: bool = False
    ledger_path: Path = LEDGER
    out_dir: Path = ROOT / "cache" / "benchmark"
    previous: dict | None = None
    stamp: str = ""
    python: str = sys.executable
    pedal_unhealthy: str = ""
    machine_extra: dict = field(default_factory=dict)

    def env(self) -> dict:
        e = dict(os.environ)
        e["TONEMATCH_AGENT"] = AGENT
        e.setdefault("TONEMATCH_NATIVE_MAX_HOSTS", "1")      # Native host RAM cap: one at a time
        hostvenv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
        if "TONEMATCH_NATIVE_PYTHON" not in e and hostvenv.exists():
            e["TONEMATCH_NATIVE_PYTHON"] = str(hostvenv)
        if "TONEMATCH_STOMPD" not in e and STOMPD.exists():
            e["TONEMATCH_STOMPD"] = str(STOMPD)
        if "TONEMATCH_STOMPD_COMPILE" not in e and STOMPD_COMPILE.exists():
            e["TONEMATCH_STOMPD_COMPILE"] = str(STOMPD_COMPILE)
        return e

    def run(self, args: list[str], timeout: float | None = None) -> CmdResult:
        return self.runner([self.python, *args], self.env(), timeout)

    def prev_test(self, tid: str) -> dict | None:
        for t in (self.previous or {}).get("tests", []):
            if t.get("id") == tid:
                return t
        return None


def hardware_gate(ctx: Ctx) -> Result | None:
    """None when the pedal may be used now, else the skipped / not-available result."""
    if ctx.no_hardware:
        return Result(SKIPPED, "skipped: no pedal (--no-hardware)")
    if ctx.pedal_unhealthy:
        return Result(SKIPPED, f"skipped: pedal health check failed earlier in this run "
                               f"({ctx.pedal_unhealthy}); power-cycle the pedal")
    if ctx.probes.hx_edit_running():
        return Result(SKIPPED, "skipped: HX Edit is open and holds the pedal; quit it")
    jobs = ctx.probes.pedal_jobs()
    if jobs:
        return Result(SKIPPED, f"skipped: another job is using the pedal ({jobs[0][:60]}); "
                               f"not interrupted")
    if not ctx.probes.pedal_on_bus():
        return Result(NA, "not available: no HX Stomp found on USB")
    if not (ctx.env().get("TONEMATCH_STOMPD") and Path(ctx.env()["TONEMATCH_STOMPD"]).exists()):
        return Result(NA, "not available: the stompd helper is not built (TONEMATCH_STOMPD)")
    return None


def _tail(text: str, n: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else "..." + text[-n:]


# ----------------------------------------------------------------------------- the tests


SMOKE_RE = re.compile(r"^(?P<eng>\w+): OK peak (?P<peak>-?[\d.]+) dBFS, (?P<cost>[\d.]+) s/render", re.MULTILINE)


def smoke(ctx: Ctx, engine: str) -> Result:
    args = ["scripts/engine_smoke.py", "--engine", engine]
    if ctx.no_ledger:
        args.append("--no-ledger")
    r = ctx.run(args, timeout=900)
    text = r.out + r.err
    m = SMOKE_RE.search(r.out)
    if r.rc == 2:
        return Result(NA, "not available: " + (_tail(text) or "no engine found"))
    if r.rc == 0 and m:
        peak, cost = float(m["peak"]), float(m["cost"])
        return Result(PASS, "", f"renders cleanly: peak {peak:.1f} dBFS, {cost:.2f} s per render",
                      [{"name": "peak", "value": peak, "unit": "dBFS"},
                       {"name": "render_cost", "value": cost, "unit": "s/render"}])
    if any(w in text for w in HEALTH_WORDS) and engine == "stomp":
        ctx.pedal_unhealthy = "engine smoke"
    return Result(FAIL, _tail(text) or f"exit {r.rc}")


def t_smoke_native(ctx: Ctx) -> Result:
    return smoke(ctx, "native")


def t_smoke_stomp(ctx: Ctx) -> Result:
    gate = hardware_gate(ctx)
    return gate if gate else smoke(ctx, "stomp")


def parity_files() -> dict[str, Path]:
    inject = ROOT / "cache" / "stomp" / "inject"
    files = {n: inject / f"{n}.hlx" for n in PARITY_PRESETS}
    files.update({n: HW_PRESETS / f"{n}.hlx" for n in PARITY_DELAY_PAIR})
    return files


def evaluate_parity(summary: dict) -> Result:
    """Pure: a parity summary.json -> Result. Rules in docs/BENCHMARKING.md."""
    res = summary.get("results", {})
    fails: list[str] = []
    metrics: list[dict] = []
    parts: list[str] = []
    for name in (*PARITY_PRESETS, PARITY_DELAY_PAIR[0]):
        r = res.get(name, {})
        if "null_residual_db" not in r:
            fails.append(f"{name}: no result ({r.get('skipped', 'missing')})")
            continue
        v = float(r["null_residual_db"])
        metrics.append({"name": f"null_{name}", "value": round(v, 1), "unit": "dB"})
        parts.append(f"{name} {v:.1f}")
        if v > PARITY_NULL_DB:
            fails.append(f"{name} null {v:.1f} dB is above {PARITY_NULL_DB:.0f}")
    t = res.get(PARITY_DELAY_PAIR[1], {})
    if "tail_stomp_rms_db" in t and "tail_native_rms_db" in t:
        diff = float(t["tail_stomp_rms_db"]) - float(t["tail_native_rms_db"])
        metrics.append({"name": "delay_tail_level_diff", "value": round(diff, 2), "unit": "dB"})
        parts_tail = f"delay-tail level, Stomp minus Native {diff:+.1f} dB"
        if abs(diff) > PARITY_TAIL_DB:
            fails.append(f"delay tail level differs by {diff:+.1f} dB (limit {PARITY_TAIL_DB:.0f})")
    else:
        parts_tail = "delay tail not measured"
        fails.append(f"{PARITY_DELAY_PAIR[1]}: no tail measurement")
    rc = summary.get("engines", {}).get("stomp", {}).get("restore_check", {})
    # T097: the verdict is decoded equality (restore_ok); older summaries carry the bytes only.
    restored = rc.get("restore_ok", rc.get("bytes_equal_backup"))
    if restored is not True:
        fails.append("pedal restore not confirmed" if restored is None
                     else "pedal NOT restored to its backup after the run (restore_ok false)" if "restore_ok" in rc
                     else "pedal NOT byte-equal to its backup after the run")
    for e in summary.get("errors", []):
        fails.append(f"error: {_tail(str(e), 120)}")
    key = ("null (dB, lower is closer): " + ", ".join(parts) + f"; {parts_tail}; "
           + ("pedal restored to its backup" if restored else "restore not confirmed"))
    extra = {"stomp_firmware": summary.get("engines", {}).get("stomp", {}).get("firmware_version"),
             "native_version": summary.get("engines", {}).get("native", {}).get("firmware_version")}
    return Result(FAIL if fails else PASS, "; ".join(fails), key, metrics, extra)


def t_parity(ctx: Ctx) -> Result:
    gate = hardware_gate(ctx)
    if gate:
        return gate
    files = parity_files()
    missing = [n for n, p in files.items() if not p.exists()]
    if missing:
        return Result(NA, f"not available: preset files missing for {', '.join(missing)} "
                          f"(parity_null.py gen / cache/stomp/hw/presets)")
    out = ctx.out_dir / f"parity-{ctx.stamp}"
    args = ["scripts/parity_null.py", "run", "--presets", *(str(p) for p in files.values()),
            "--out-dir", str(out)]
    if ctx.no_ledger:
        args.append("--no-ledger")
    r = ctx.run(args, timeout=3600)
    text = r.out + r.err
    sj = out / "summary.json"
    if not sj.exists():
        if any(w in text for w in HEALTH_WORDS):
            ctx.pedal_unhealthy = "parity"
        return Result(FAIL, _tail(text) or f"exit {r.rc}, no summary written")
    res = evaluate_parity(json.loads(sj.read_text()))
    if r.rc != 0 and res.status == PASS:
        res.status, res.reason = FAIL, f"script exit {r.rc}: {_tail(text)}"
    return res


def evaluate_trust(report: dict, prev_metrics: dict[str, float] | None) -> Result:
    """Pure: a loss_trust_check JSON report -> Result. Proxy rules: regression guard only."""
    held = report["summary"]["heldout_notes"]
    rates = {t: float(v["rank1_rate"]) for t, v in held.items()}
    metrics = [{"name": f"rank1_{t}", "value": round(v, 3), "unit": "fraction"}
               for t, v in rates.items()]
    best_t = max(rates, key=rates.get)
    chance = float(report.get("chance_rank1_rate", 0.0625))
    key = (f"best held-out rank-1 {rates[best_t]:.2f} ({best_t}) over {report['items'] * len(report['seed'])} "
           f"items; chance {chance:.3f}; combined score {rates.get('provisional_sum', float('nan')):.2f}"
           f" vs the 0.95 needed on real Helix audio: not trusted yet")
    worse = [f"{t} {prev_metrics[t]:.2f} -> {v:.2f}" for t, v in rates.items()
             if prev_metrics and t in prev_metrics and v < prev_metrics[t] - TRUST_TOLERANCE]
    if worse:
        return Result(FAIL, "regression against the previous run: " + "; ".join(worse), key, metrics)
    return Result(PASS, "", key, metrics)


def t_trust(ctx: Ctx) -> Result:
    out = ctx.out_dir / f"loss-trust-{ctx.stamp}.json"
    jobs = max(1, min(4, (os.cpu_count() or 2) - 1))
    args = ["scripts/loss_trust_check.py", "--proxy", "--all", "--seed", *map(str, TRUST_SEEDS),
            "--items", str(TRUST_ITEMS_PER_SEED), "--jobs", str(jobs), "--out", str(out),
            "--note", "suite run"]
    if not ctx.no_ledger:
        args.append("--ledger")
    r = ctx.run(args, timeout=3600)
    if r.rc == 2:
        return Result(NA, "not available: " + _tail(r.err or r.out))
    if r.rc != 0 or not out.exists():
        return Result(FAIL, _tail(r.err + r.out) or f"exit {r.rc}")
    prev = ctx.prev_test("loss_trust")
    prev_m = {m["name"][len("rank1_"):]: m["value"] for m in (prev or {}).get("metrics", [])
              if m["name"].startswith("rank1_")} or None
    return evaluate_trust(json.loads(out.read_text()), prev_m)


SYNTH_SET = ROOT / "cache" / "synthetic-v1"
SYNTH_SUITE_ITEMS = 8           # the CI subset: one item per category, round-robin order
SYNTH_SUITE_BUDGET = 100        # search renders per item in the suite run
SYNTH_TARGETS = {"block_f1": 0.85, "model_acc": 0.80, "param_mae": 0.15, "rank1_rate": 0.95}


def synthetic_gate(what: str) -> Result | None:
    """None when synthetic-v1 is generated and Native is installed, else the not-available result."""
    if not (SYNTH_SET / "manifest.json").exists():
        return Result(NA, f"not available: synthetic-v1 is not generated ({what}); run "
                          f"scripts/synthetic_v1.py generate")
    return None


def t_synthetic(ctx: Ctx) -> Result:
    gate = synthetic_gate("recovery")
    if gate:
        return gate
    if ctx.probes.native_hosts() + 1 > 6:
        return Result(SKIPPED, f"skipped: {ctx.probes.native_hosts()} Native host processes are already "
                               f"running (limit 6 with this one)")
    tag = "suite"
    args = ["scripts/synthetic_v1.py", "run", "--items", str(SYNTH_SUITE_ITEMS), "--budget",
            str(SYNTH_SUITE_BUDGET), "--tag", tag, "--redo"]
    if not ctx.no_ledger:
        args.append("--ledger")
    r = ctx.run(args, timeout=7200)
    if r.rc == 3:
        return Result(SKIPPED, "skipped: too many Native host processes: " + _tail(r.err))
    summary = SYNTH_SET / "runs" / tag / "summary.json"
    if r.rc not in (0, 1) or not summary.exists():
        return Result(FAIL, _tail(r.err + r.out) or f"exit {r.rc}")
    s = json.loads(summary.read_text())
    metrics = [{"name": m, "value": round(s[m], 4), "unit": "fraction" if m != "loss_heldout" else "loss"}
               for m in ("block_f1", "model_acc", "param_mae", "rank1_rate", "rank1_rate_audible",
                         "loss_heldout") if m in s]
    key = (f"{s['items']} items, one-block presets: block F1 {s['block_f1']:.2f}, model accuracy "
           f"{s['model_acc']:.2f} (chance {s['model_acc_chance']:.2f}), setting error {s['param_mae']:.2f}, "
           f"true preset first {s.get('rank1_rate', float('nan')):.2f}; {s['weights']}; targets not frozen")
    if r.rc == 1:
        return Result(FAIL, "regression against the previous run: " + _tail(r.err), key, metrics)
    return Result(PASS, "", key, metrics)


def t_stress(ctx: Ctx) -> Result:
    return Result(NA, "not available: the density-stress variant of synthetic-v1 is not built; the "
                      "separation curve is recorded separately as sep-stress-v1")


PROBE_RE = re.compile(r"(\d+)/(\d+) checks pass \((\d+) blocks\)")


def t_probe(ctx: Ctx) -> Result:
    args = ["scripts/block_probe.py", "validate"]
    if not ctx.no_ledger:
        args.append("--ledger")
    r = ctx.run(args, timeout=1800)
    m = PROBE_RE.search(r.out)
    if not m:
        return Result(FAIL, _tail(r.err + r.out) or f"exit {r.rc}")
    ok, n, blocks = int(m[1]), int(m[2]), int(m[3])
    key = f"{ok} of {n} checks within tolerance on {blocks} known synthetic blocks"
    return Result(PASS if ok == n and r.rc == 0 else FAIL,
                  "" if ok == n else f"{n - ok} checks outside tolerance", key,
                  [{"name": "checks_passed", "value": ok, "unit": f"of {n}"}])


def parse_pytest(out: str) -> dict:
    """Counts from pytest -q's last summary line, e.g. '2 failed, 410 passed, 1 skipped in 85.2s'."""
    lines = [ln for ln in out.strip().splitlines() if re.search(r" in [\d.]+s", ln)]
    line = lines[-1] if lines else ""
    d = {k: int(m.group(1)) if (m := re.search(rf"(\d+) {k}", line)) else 0
         for k in ("passed", "failed", "skipped", "error", "errors")}
    d["failed"] += d.pop("error") + d.pop("errors")
    return d


def t_unit(ctx: Ctx) -> Result:
    r = ctx.run(["-m", "pytest", "-q"], timeout=3600)
    d = parse_pytest(r.out)
    if not (d["passed"] or d["failed"]):
        return Result(FAIL, _tail(r.err + r.out) or f"exit {r.rc}")
    key = f"{d['passed']} tests passed, {d['failed']} failed, {d['skipped']} skipped"
    ok = r.rc == 0 and not d["failed"]
    names = [ln.split(" - ")[0].replace("FAILED ", "").strip() for ln in r.out.splitlines()
             if ln.startswith(("FAILED ", "ERROR "))]
    why = "failing: " + ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return Result(PASS if ok else FAIL, "" if ok else (why if names else _tail(r.out, 300)), key,
                  [{"name": "passed", "value": d["passed"], "unit": "tests"},
                   {"name": "failed", "value": d["failed"], "unit": "tests"}])


def t_lint(ctx: Ctx) -> Result:
    r = ctx.run(["-m", "ruff", "check", "src", "tests", "scripts"], timeout=600)
    text = (r.out + r.err).strip()
    if r.rc == 0:
        return Result(PASS, "", "no lint problems in src, tests and scripts",
                      [{"name": "problems", "value": 0, "unit": "findings"}])
    m = re.search(r"Found (\d+) error", text)
    n = int(m[1]) if m else None
    return Result(FAIL, _tail(text), f"{n} lint problems" if n is not None else "ruff did not run",
                  [{"name": "problems", "value": n if n is not None else -1, "unit": "findings"}])


SPECS = [
    Spec("smoke_native", "Engine smoke, Helix Native",
         "Native loads a known preset and renders a 2 s clean guitar clip",
         "length right, no invalid values, peak between -30 and -1 dBFS, at least 20 dB above the "
         "noise floor", t_smoke_native),
    Spec("smoke_stomp", "Engine smoke, HX Stomp",
         "The pedal loads the same preset, renders the clip, and is put back on its own preset",
         "same checks as Native, health check passes, original preset restored", t_smoke_stomp, True),
    Spec("parity", "Hardware parity (null test)",
         "Same clean guitar clip and presets K, A, B0, B00 and BT0 through the Stomp and Native, "
         "aligned, level-matched and subtracted",
         f"null at or below {PARITY_NULL_DB:.0f} dB for K, A, B0 and B00; delay tail (BT0) level "
         f"within {PARITY_TAIL_DB:.0f} dB; 04C restored and byte-diffed", t_parity, True),
    Spec("loss_trust", "Loss trust check (proxy)",
         "Does each scoring method rank the true setup first among 15 decoys? On stand-in digital "
         "effects, not Helix, because the Helix set is not rendered",
         f"no method's held-out rank-1 falls more than {TRUST_TOLERANCE:.2f} below the previous run "
         f"(seeds {', '.join(map(str, TRUST_SEEDS))}); the 0.90 / 0.95 admission rule needs Helix audio",
         t_trust),
    Spec("synthetic_recovery", "Synthetic recovery (CI subset)",
         f"Can the matcher recover known presets from their sound? {SYNTH_SUITE_ITEMS} of the 96 "
         f"synthetic-v1 items, one host, {SYNTH_SUITE_BUDGET} search renders each, provisional loss weights",
         "no metric falls beyond its tolerance below the previous run of the same subset; Gate 2 "
         "targets are not applied to the subset", t_synthetic),
    Spec("separation_stress", "Separation stress",
         "How fast does accuracy fall as the mix gets denser?",
         "recorded as a curve, not gated", t_stress),
    Spec("block_probe", "Block-probe validate (synthetic)",
         "Does the probe analysis measure known synthetic blocks correctly?",
         "every check within its tolerance", t_probe),
    Spec("unit_tests", "Offline unit tests (pytest -q)",
         "The repository's offline tests", "no test fails", t_unit),
    Spec("lint", "Lint (ruff)", "Style and simple bug checks on src, tests and scripts",
         "no findings", t_lint),
]
IDS = [s.id for s in SPECS]


# ----------------------------------------------------------------------------- the run


def select(only: list[str] | None, skip: list[str] | None) -> list[Spec]:
    bad = [i for i in (only or []) + (skip or []) if i not in IDS]
    if bad:
        raise SystemExit(f"unknown test id(s) {bad}; choose from {IDS}")
    return [s for s in SPECS if (not only or s.id in only) and s.id not in (skip or [])]


def run_suite(ctx: Ctx, specs: list[Spec], clock: Callable[[], float] = time.perf_counter) -> dict:
    started = dt.datetime.now(dt.UTC)
    ctx.stamp = ctx.stamp or started.strftime("%Y%m%dT%H%M%SZ")
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    sha, dirty = git_state()
    machine = {**ctx.probes.machine(), **ctx.machine_extra}
    contention = {"load_avg_at_start": ctx.probes.load_avg(),
                  "native_host_processes_at_start": ctx.probes.native_hosts(), "notes": []}
    if contention["native_host_processes_at_start"]:
        contention["notes"].append(
            f"{contention['native_host_processes_at_start']} Native host process(es) from another "
            f"job were running; not interrupted, timings may be inflated")
    tests: list[dict] = []
    for spec in specs:
        before = len(ledger_rows(ctx.ledger_path))
        load = ctx.probes.load_avg()
        t0 = clock()
        try:
            res = spec.fn(ctx)
        except SystemExit as e:
            res = Result(FAIL, f"script stopped: {_tail(str(e))}")
        except Exception as e:  # noqa: BLE001
            res = Result(FAIL, f"{type(e).__name__}: {_tail(str(e))}")
        wall = clock() - t0
        if res.status in (SKIPPED, NA):
            wall = 0.0
        new = [r for r in ledger_rows(ctx.ledger_path)[before:] if r.get("agent") == AGENT]
        if load > 0.75 * (os.cpu_count() or 1):
            contention["notes"].append(f"{spec.id}: machine load {load} at start (busy); timing may "
                                       f"be inflated")
        tests.append({"id": spec.id, "name": spec.name, "checks": spec.checks, "status": res.status,
                      "reason": res.reason, "key_result": res.key_result, "metrics": res.metrics,
                      "pass_rule": spec.pass_rule, "wall_clock_s": round(wall, 2),
                      "ledger_rows": [r["timestamp"] for r in new], "extra": res.extra})
    if ctx.pedal_unhealthy:
        contention["notes"].append(f"pedal health check failed in {ctx.pedal_unhealthy}")
    for t in tests:
        if t["extra"].get("stomp_firmware"):
            machine["stomp_firmware"] = t["extra"]["stomp_firmware"]
        if t["extra"].get("native_version") and not machine.get("native_version"):
            machine["native_version"] = t["extra"]["native_version"]
    prev_machine = (ctx.previous or {}).get("machine", {})
    if "stomp_firmware" not in machine and prev_machine.get("stomp_firmware"):
        machine["stomp_firmware"] = prev_machine["stomp_firmware"]
        machine["stomp_firmware_note"] = "from an earlier run (the pedal was not read this time)"
    total = round(sum(t["wall_clock_s"] for t in tests), 2)
    counts = {s: sum(t["status"] == s for t in tests) for s in STATUSES}
    finished = dt.datetime.now(dt.UTC)
    run = {"schema": SCHEMA, "run_id": ctx.stamp, "started_utc": started.isoformat(timespec="seconds"),
           "finished_utc": finished.isoformat(timespec="seconds"),
           "elapsed_s": round((finished - started).total_seconds(), 1),
           "commit": sha + ("-dirty" if dirty else ""), "machine": machine,
           "options": {"no_hardware": ctx.no_hardware, "no_ledger": ctx.no_ledger,
                       "tests_run": [s.id for s in specs]},
           "contention": contention, "total_wall_clock_s": total, "counts": counts,
           "tests": tests, "suite_ledger_row": None,
           "previous_run_id": (ctx.previous or {}).get("run_id")}
    return run


def append_suite_row(run: dict, ctx: Ctx) -> str | None:
    if ctx.no_ledger:
        return None
    c = run["counts"]
    notes = (f"suite run {run['run_id']}; " + ", ".join(f"{c[s]} {s}" for s in STATUSES)
             + "; " + "; ".join(f"{t['id']} {t['status']} {t['wall_clock_s']:.0f}s"
                                for t in run["tests"]))
    before = len(ledger_rows(ctx.ledger_path))
    cmd = [ctx.python, "scripts/ledger_append.py", "--kind", "benchmark", "--engine", "-",
           "--dataset", "suite", "--metric", "suite_wall_clock_s", "--value",
           str(run["total_wall_clock_s"]), "--wall-clock-s", str(run["total_wall_clock_s"]),
           "--notes", notes.replace(",", ";")]
    ctx.runner(cmd, ctx.env(), 120)
    rows = ledger_rows(ctx.ledger_path)[before:]
    return rows[-1]["timestamp"] if rows else None


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_reports(run: dict, report_dir: Path = REPORT_DIR) -> Path:
    hist = report_dir / "history"
    hist.mkdir(parents=True, exist_ok=True)
    path = hist / f"{run['run_id']}.json"
    text = json.dumps(run, indent=1, default=str) + "\n"
    path.write_text(text)
    (report_dir / "latest.json").write_text(text)
    return path


# ----------------------------------------------------------------------------- the doc section


def fmt_dur(s: float) -> str:
    if s < 1:
        return "under 1 s" if s > 0 else "0 s"
    if s < 90:
        return f"{s:.0f} s"
    m, sec = divmod(round(s), 60)
    return f"{m} min {sec} s"


def compare_runs(run: dict, prev: dict | None) -> tuple[str, list[str]]:
    """(one line, regression list) comparing totals and statuses with the previous run."""
    if not prev:
        return "This is the first recorded run, so there is nothing to compare with yet.", []
    pt, ct = prev["total_wall_clock_s"], run["total_wall_clock_s"]
    d = ct - pt
    line = (f"Compared with the previous run ({prev['started_utc'][:10]}): total run time "
            f"{fmt_dur(ct)} against {fmt_dur(pt)} ("
            + (f"{'+' if d >= 0 else '-'}{fmt_dur(abs(d))}" if abs(d) >= 1 else "about the same") + ")")
    prev_by = {t["id"]: t for t in prev["tests"]}
    regress, changed, slower = [], [], []
    for t in run["tests"]:
        p = prev_by.get(t["id"])
        if not p:
            continue
        if p["status"] != t["status"]:
            changed.append(f"{t['name']}: {p['status']} to {t['status']}")
            if p["status"] == PASS and t["status"] == FAIL:
                regress.append(t["name"])
        elif t["status"] == PASS and p["wall_clock_s"] >= 5 and t["wall_clock_s"] > 1.5 * p["wall_clock_s"]:
            slower.append(f"{t['name']} took {fmt_dur(t['wall_clock_s'])} against "
                          f"{fmt_dur(p['wall_clock_s'])}")
    line += "; statuses " + ("unchanged" if not changed else "changed (" + "; ".join(changed) + ")")
    if slower:
        line += "; much slower: " + "; ".join(slower)
    return line + ".", regress


STATUS_WORDS = {PASS: "pass", FAIL: "**FAIL**", SKIPPED: "skipped", NA: "not available"}


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def ledger_cell(stamps: list[str]) -> str:
    u = list(dict.fromkeys(stamps))
    if not u:
        return "none"
    if len(u) <= 3:
        head = u[0]
        rest = [x[11:19] for x in u[1:]]
        return head + "".join(f", {x}" for x in rest)
    return f"{u[0]} x{len(u)}"


def render_section(run: dict, prev: dict | None) -> str:
    m = run["machine"]
    os_name = "macOS" if m.get("os") == "Darwin" else m.get("os", "?")
    mach = f"{os_name} {m.get('os_version', '')} ({m.get('arch', '')}, {m.get('cpu_count')} cores, "\
           f"{m.get('ram_gb')} GB)"
    fw = m.get("stomp_firmware")
    versions = (f"Helix Native {m.get('native_version', 'not found')}; HX Stomp firmware "
                f"{fw if fw else 'not read'}"
                + (f" ({m['stomp_firmware_note']})" if m.get("stomp_firmware_note") else ""))
    c = run["counts"]
    summary = ", ".join(f"{c[s]} {s}" for s in STATUSES if c[s])
    line, regress = compare_runs(run, prev)
    note = ("<!-- Generated by scripts/benchmark_suite.py --render-doc from "
            "reports/benchmark/latest.json. Do not edit by hand. -->")
    out = [START, note, "",
           "### Most recent benchmark run", "",
           f"- **Run:** {run['started_utc'][:16].replace('T', ' ')} UTC, commit `{run['commit']}`",
           f"- **Machine:** {mach}; {versions}",
           (f"- **Total run time:** {fmt_dur(run['total_wall_clock_s'])} "
            f"({run['total_wall_clock_s']:.0f} s), the sum of the test run times below. Skipped and "
            f"not-available tests count as 0 s."),
           f"- **Outcome:** {summary}."]
    if run["options"].get("no_ledger"):
        out.append("- **Ledger:** this run was a dry run (`--no-ledger`): no ledger rows were written.")
    elif run.get("suite_ledger_row"):
        out.append(f"- **Suite ledger row:** {run['suite_ledger_row']} "
                   f"(`benchmark`, dataset `suite`, metric `suite_wall_clock_s`).")
    for n in run["contention"]["notes"]:
        out.append(f"- **Timing note:** {n}.")
    out += ["", f"- **Against the previous run:** {line}"]
    if regress:
        out.append("- **REGRESSION:** " + "; ".join(regress) + " passed last time and fails now. "
                   + "Merges freeze until the owner fixes it (rule 2 above).")
    out += ["", "| Test | Status | Key result, and what it means | Pass rule | Run time | Ledger rows |",
            "| --- | --- | --- | --- | --- | --- |"]
    for t in run["tests"]:
        what = t["key_result"]
        if t["reason"]:
            reason = re.sub(r"^(not available|skipped): ", "", t["reason"])
            label = "Problem: " if t["status"] == FAIL else "Why: "
            what = (what + ". " if what else "") + label + reason
        if not what:
            what = "no result"
        out.append(f"| **{_cell(t['name'])}** — {_cell(t['checks'])} | {STATUS_WORDS[t['status']]} | "
                   f"{_cell(what)} | {_cell(t['pass_rule'])} | {fmt_dur(t['wall_clock_s'])} | "
                   f"{ledger_cell(t['ledger_rows'])} |")
    how = ("How to read it: *pass* means the test's rule was met; *fail* means it was not "
            "(a fail that was a pass last time is a regression); *skipped* means we chose not to run "
            "it or could not, for the reason shown (for example no pedal); *not available* means what "
            "the test needs does not exist yet. Neither of the last two is a pass or a failure. "
            "A null in dB is how much of the signal is left after subtracting the two outputs; lower "
            "is closer. Rank-1 is how often a scoring method put the true setup first; higher is "
            "better. Run time is wall-clock seconds on the machine above. The data is in "
            "`reports/benchmark/latest.json`, and every earlier run in `reports/benchmark/history/`.")
    out += ["", how, END]
    return "\n".join(out)


def link_section(section: str) -> str:
    """Task and evidence references -> links with hover text (idempotent)."""
    try:
        import evidence_links
        import task_links

        idx = evidence_links.Index(ROOT, "..")
        section = evidence_links.linkify_markdown(section, idx)
        board_rel = task_links.rel_prefix(ROOT, DOC.parent)
        return task_links.linkify_markdown(section, task_links.parse_board(ROOT), board_rel)
    except Exception as e:  # noqa: BLE001
        print(f"warning: link pass skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return section


def splice(doc_text: str, section: str) -> str:
    """Replace the text between the markers (markers included); insert it before the first '## '
    heading when the markers are absent. Same input, same output."""
    if START in doc_text and END in doc_text:
        a, b = doc_text.index(START), doc_text.index(END) + len(END)
        return doc_text[:a] + section + doc_text[b:]
    m = re.search(r"^## ", doc_text, re.MULTILINE)
    pos = m.start() if m else len(doc_text)
    return doc_text[:pos] + section + "\n\n" + doc_text[pos:]


def render_doc(latest: Path = LATEST, history: Path = HISTORY_DIR, doc: Path = DOC,
               link: bool = True) -> bool:
    run = load_json(latest)
    if not run:
        print(f"no {latest}; run the suite first", file=sys.stderr)
        return False
    prev = None
    if run.get("previous_run_id"):
        prev = load_json(history / f"{run['previous_run_id']}.json")
    section = render_section(run, prev)
    if link:
        section = link_section(section)
    text = doc.read_text()
    new = splice(text, section)
    new = re.sub(r"(\*Last updated: )[\d-]+", rf"\g<1>{run['started_utc'][:10]}", new, count=1)
    if new != text:
        doc.write_text(new)
    return True


# ----------------------------------------------------------------------------- cli


def print_table(run: dict) -> None:
    print(f"\n{'test':36s} {'status':14s} {'run time':>9s}  key result")
    for t in run["tests"]:
        print(f"{t['name'][:36]:36s} {t['status']:14s} {t['wall_clock_s']:8.1f}s  "
              f"{t['key_result'] or t['reason']}"[:200])
    print(f"{'TOTAL':36s} {'':14s} {run['total_wall_clock_s']:8.1f}s  "
          + ", ".join(f"{run['counts'][s]} {s}" for s in STATUSES))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="+", metavar="ID", help=f"run only these: {', '.join(IDS)}")
    ap.add_argument("--skip", nargs="+", metavar="ID", help="skip these")
    ap.add_argument("--no-hardware", action="store_true",
                    help="skip every test that needs the pedal (status: skipped, no pedal)")
    ap.add_argument("--no-ledger", action="store_true", help="write no ledger rows (also none from the tests)")
    ap.add_argument("--render-doc", action="store_true",
                    help="only refresh the generated section of docs/BENCHMARKING.md from latest.json")
    ap.add_argument("--list", action="store_true", help="list the test ids in run order")
    a = ap.parse_args(argv)
    if a.list:
        print("\n".join(IDS))
        return 0
    if a.render_doc:
        return 0 if render_doc() else 1
    specs = select(a.only, a.skip)
    ctx = Ctx(no_hardware=a.no_hardware, no_ledger=a.no_ledger, previous=load_json(LATEST))
    run = run_suite(ctx, specs)
    full = run["options"]["tests_run"] == IDS
    run["suite_ledger_row"] = append_suite_row(run, ctx)
    if full:
        path = write_reports(run)
    else:  # a partial run is not "the most recent benchmark run"; keep it out of the history
        path = ctx.out_dir / f"partial-{run['run_id']}.json"
        path.write_text(json.dumps(run, indent=1, default=str) + "\n")
    print_table(run)
    print(f"\nrun written to {path}")
    if full and render_doc():
        print(f"updated {DOC}")
    return 1 if run["counts"][FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
