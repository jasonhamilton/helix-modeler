"""scripts/benchmark_suite.py with stub runners and probes: no engine, no pedal, no ledger writes."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bs", ROOT / "scripts" / "benchmark_suite.py")
bs = importlib.util.module_from_spec(spec)
sys.modules["bs"] = bs
spec.loader.exec_module(bs)

SMOKE_OK = "native: OK peak -13.5 dBFS, 1.883 s/render\n"
PYTEST_OK = "..........\n410 passed, 1 skipped in 85.20s (0:01:25)\n"


class StubProbes(bs.Probes):
    def __init__(self, hx_edit=False, jobs=(), on_bus=True):
        self.hx_edit, self.jobs, self.on_bus = hx_edit, list(jobs), on_bus

    def hx_edit_running(self):
        return self.hx_edit

    def pedal_on_bus(self):
        return self.on_bus

    def pedal_jobs(self):
        return self.jobs

    def native_hosts(self):
        return 0

    def load_avg(self):
        return 0.1

    def machine(self):
        return {"os": "Darwin", "os_version": "27.0", "arch": "arm64", "cpu_count": 10,
                "ram_gb": 32, "native_version": "3.82"}


def fake_runner(calls, rc_by_script=None, ledger=None):
    """Answers by script name; appends a ledger row for the smoke script like the real one does."""
    rc_by_script = rc_by_script or {}

    def run(argv, env, timeout):
        calls.append(argv)
        joined = " ".join(argv)
        if "ledger_append.py" in joined:
            if ledger is not None:
                _append(ledger, "benchmark", "benchmark")
            return bs.CmdResult(0)
        if "engine_smoke.py" in joined:
            if ledger is not None and "--no-ledger" not in joined:
                _append(ledger, "benchmark", "smoke")
            return bs.CmdResult(rc_by_script.get("smoke", 0), SMOKE_OK)
        if "-m pytest" in joined:
            return bs.CmdResult(rc_by_script.get("pytest", 0), PYTEST_OK)
        if "-m ruff" in joined:
            return bs.CmdResult(0, "All checks passed!\n")
        if "block_probe.py" in joined:
            return bs.CmdResult(0, "probe v1\n41/41 checks pass (12 blocks), 3.2 s\n")
        return bs.CmdResult(2, "", "not enabled")

    return run


def _append(path, agent, kind):
    n = len(path.read_text().splitlines()) if path.exists() else 0
    head = "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
    line = f"2026-10-15T10:00:{n:02d}+00:00,abc,{agent},{kind},-,d,m,1,,,\n"
    path.write_text((path.read_text() if path.exists() else head) + line)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "synthetic_gate", lambda what: bs.Result(bs.NA, f"not available: {what}"))
    calls = []
    c = bs.Ctx(runner=fake_runner(calls, ledger=tmp_path / "runs.csv"), probes=StubProbes(),
               ledger_path=tmp_path / "runs.csv", out_dir=tmp_path / "out", no_hardware=True)
    c.calls = calls
    return c


def test_ids_are_in_the_fixed_order():
    assert bs.IDS == ["smoke_native", "smoke_stomp", "parity", "loss_trust", "synthetic_recovery",
                      "separation_stress", "block_probe", "unit_tests", "lint"]


def test_each_test_is_timed_and_total_is_the_sum(ctx):
    specs = bs.select(["smoke_native", "block_probe", "unit_tests", "lint"], None)
    # clock is read twice per test: start, end. Durations 1.5, 2.0, 30.25, 0.25.
    ticks = [0, 1.5, 10, 12, 20, 50.25, 60, 60.25]
    it = iter(ticks)
    run = bs.run_suite(ctx, specs, clock=lambda: next(it))
    got = {t["id"]: t["wall_clock_s"] for t in run["tests"]}
    assert got == {"smoke_native": 1.5, "block_probe": 2.0, "unit_tests": 30.25, "lint": 0.25}
    assert run["total_wall_clock_s"] == pytest.approx(sum(got.values()))
    assert all(t["status"] == bs.PASS for t in run["tests"])


def test_statuses_skipped_not_available_and_fail(ctx):
    ctx.runner = fake_runner(ctx.calls, {"pytest": 1}, ledger=None)
    run = bs.run_suite(ctx, bs.select(None, None))
    st = {t["id"]: t for t in run["tests"]}
    assert st["smoke_native"]["status"] == bs.PASS
    assert st["smoke_stomp"]["status"] == bs.SKIPPED
    assert "no pedal" in st["smoke_stomp"]["reason"] and st["smoke_stomp"]["wall_clock_s"] == 0
    assert st["parity"]["status"] == bs.SKIPPED
    assert st["synthetic_recovery"]["status"] == bs.NA
    assert st["separation_stress"]["status"] == bs.NA
    assert st["unit_tests"]["status"] == bs.FAIL
    assert st["loss_trust"]["status"] == bs.NA
    assert run["counts"] == {"pass": 3, "fail": 1, "skipped": 2, "not available": 3}
    assert sum(run["counts"].values()) == len(bs.IDS)
    # nothing was run against the pedal
    assert not any("parity_null" in " ".join(c) for c in ctx.calls)


@pytest.mark.parametrize("probes,word", [
    (StubProbes(hx_edit=True), "HX Edit"),
    (StubProbes(jobs=["123 stompd"]), "another job"),
    (StubProbes(on_bus=False), "no HX Stomp"),
])
def test_hardware_gate_refuses_when_pedal_is_not_free(ctx, probes, word, monkeypatch):
    ctx.no_hardware = False
    ctx.probes = probes
    monkeypatch.setenv("TONEMATCH_STOMPD", str(ROOT / "scripts" / "benchmark_suite.py"))
    res = bs.t_smoke_stomp(ctx)
    assert res.status in (bs.SKIPPED, bs.NA) and word in res.reason
    assert not ctx.calls


def test_ledger_rows_written_during_a_test_are_listed(ctx, tmp_path):
    ledger = tmp_path / "runs.csv"
    run = bs.run_suite(ctx, bs.select(["smoke_native"], None))
    assert run["tests"][0]["ledger_rows"] == ["2026-10-15T10:00:00+00:00"]
    ctx.no_ledger = True
    ctx.runner = fake_runner(ctx.calls, ledger=None)
    run = bs.run_suite(ctx, bs.select(["smoke_native"], None))
    assert run["tests"][0]["ledger_rows"] == [] and ledger.exists()
    assert "--no-ledger" in ctx.calls[-1]


def test_suite_ledger_row_is_appended_unless_no_ledger(ctx, tmp_path):
    run = bs.run_suite(ctx, bs.select(["lint"], None))
    ts = bs.append_suite_row(run, ctx)
    cmd = [c for c in ctx.calls if "ledger_append.py" in " ".join(c)][-1]
    assert ts and "suite_wall_clock_s" in cmd and "suite" in cmd
    n = len(ctx.calls)
    ctx.no_ledger = True
    assert bs.append_suite_row(run, ctx) is None and len(ctx.calls) == n


def test_history_and_latest_written(ctx, tmp_path):
    run = bs.run_suite(ctx, bs.select(["lint"], None))
    path = bs.write_reports(run, tmp_path / "rep")
    assert path.parent == tmp_path / "rep" / "history" and path.name == f"{run['run_id']}.json"
    assert json.loads(path.read_text()) == json.loads((tmp_path / "rep" / "latest.json").read_text())
    assert json.loads(path.read_text())["total_wall_clock_s"] == run["total_wall_clock_s"]


def test_unknown_test_id_is_refused():
    with pytest.raises(SystemExit):
        bs.select(["nope"], None)


def _run(total=10.0, statuses=None, rid="20261015T100000Z"):
    statuses = statuses or {"unit_tests": "pass", "lint": "pass"}
    tests = [{"id": k, "name": k, "checks": "c", "status": v, "reason": "r" if v != "pass" else "",
              "key_result": "k", "metrics": [], "pass_rule": "p", "wall_clock_s": total / 2,
              "ledger_rows": []} for k, v in statuses.items()]
    return {"run_id": rid, "started_utc": "2026-10-15T10:00:00+00:00", "commit": "abc",
            "machine": {"os": "Darwin", "os_version": "27", "arch": "arm64", "cpu_count": 10,
                        "ram_gb": 32, "native_version": "3.82", "stomp_firmware": "3.50"},
            "options": {"no_ledger": True}, "contention": {"notes": []},
            "total_wall_clock_s": total, "counts": {s: list(statuses.values()).count(s)
                                                    for s in bs.STATUSES},
            "tests": tests, "suite_ledger_row": None, "previous_run_id": None}


def test_compare_flags_regression_and_totals():
    prev, now = _run(10), _run(20, {"unit_tests": "fail", "lint": "pass"}, "20261016T100000Z")
    line, regress = bs.compare_runs(now, prev)
    assert regress == ["unit_tests"] and "unit_tests: pass to fail" in line and "+10 s" in line
    assert bs.compare_runs(now, None)[1] == []


def test_section_replaced_idempotently_between_markers(tmp_path):
    doc = tmp_path / "BENCHMARKING.md"
    doc.write_text("# T\n\n*Last updated: 2020-01-01.*\n\nLead.\n\n## First\n\ntext\n")
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(_run(10)))
    assert bs.render_doc(latest, tmp_path / "h", doc, link=False)
    once = doc.read_text()
    assert once.count(bs.START) == 1 and once.count(bs.END) == 1
    assert once.index("Lead.") < once.index(bs.START) < once.index("## First")
    assert "2026-10-15" in once and "Total run time" in once
    bs.render_doc(latest, tmp_path / "h", doc, link=False)
    assert doc.read_text() == once                      # idempotent
    latest.write_text(json.dumps(_run(99, rid="20261016T100000Z")))
    bs.render_doc(latest, tmp_path / "h", doc, link=False)
    new = doc.read_text()
    assert new.count(bs.START) == 1 and "1 min 39 s" in new and "## First\n\ntext\n" in new
    assert "Lead." in new                               # text outside the markers is untouched


def test_table_shows_status_time_and_reason():
    sec = bs.render_section(_run(10, {"unit_tests": "pass", "lint": "skipped"}), None)
    assert "| **unit_tests**" in sec and "skipped" in sec and "5 s" in sec
    assert "first recorded run" in sec


def test_evaluate_parity_rules():
    def res(k, tail=1.0, ok=True):
        r = {n: {"null_residual_db": k} for n in ("K", "A", "B0", "B00")}
        r["BT0"] = {"null_residual_db": 4.0, "tail_stomp_rms_db": -50 + tail, "tail_native_rms_db": -50}
        return {"results": r, "errors": [], "engines": {"stomp": {
            "firmware_version": "3.50", "restore_check": {"bytes_equal_backup": ok}},
            "native": {"firmware_version": "3.82"}}}

    assert bs.evaluate_parity(res(-35)).status == bs.PASS
    assert bs.evaluate_parity(res(-20)).status == bs.FAIL
    assert bs.evaluate_parity(res(-35, tail=3.5)).status == bs.FAIL
    bad = bs.evaluate_parity(res(-35, ok=False))
    assert bad.status == bs.FAIL and "byte" in bad.reason


def test_evaluate_trust_regression_guard():
    rep = {"items": 20, "seed": [1, 2], "chance_rank1_rate": 0.0625,
           "summary": {"heldout_notes": {"a": {"rank1_rate": 0.20}, "provisional_sum": {"rank1_rate": 0.3}}}}
    assert bs.evaluate_trust(rep, None).status == bs.PASS
    assert bs.evaluate_trust(rep, {"a": 0.22}).status == bs.PASS
    r = bs.evaluate_trust(rep, {"a": 0.40})
    assert r.status == bs.FAIL and "regression" in r.reason


def test_parse_pytest_summary():
    assert bs.parse_pytest(PYTEST_OK) == {"passed": 410, "failed": 0, "skipped": 1}
    d = bs.parse_pytest("FAILED x\n2 failed, 408 passed in 9.1s\n")
    assert d["failed"] == 2 and d["passed"] == 408


def test_unit_test_failure_names_the_failing_tests(ctx):
    out = ("FAILED tests/test_a.py::test_x - AssertionError: boom\n"
           "FAILED tests/test_b.py::test_y - assert 1 == 2\n1 failed, 3 passed in 1.0s\n")
    ctx.runner = lambda argv, env, timeout: bs.CmdResult(1, out)
    res = bs.t_unit(ctx)
    assert res.status == bs.FAIL
    assert res.reason == "failing: tests/test_a.py::test_x, tests/test_b.py::test_y"


def _synth_summary(tmp_path, **kw):
    d = tmp_path / "set"
    (d / "runs" / "suite").mkdir(parents=True)
    (d / "manifest.json").write_text("{}")
    s = {"items": 8, "block_f1": 0.5, "model_acc": 0.25, "model_acc_chance": 0.18, "param_mae": 0.2,
         "rank1_rate": 0.1, "rank1_rate_audible": 0.1, "loss_heldout": 5.0,
         "weights": "provisional, not gate evidence", **kw}
    (d / "runs" / "suite" / "summary.json").write_text(json.dumps(s))
    return d


def test_synthetic_recovery_reports_the_subset_run(ctx, monkeypatch, tmp_path):
    monkeypatch.undo()   # drop the fixture's NA stub; use the real gate against a temp set
    monkeypatch.setattr(bs, "SYNTH_SET", _synth_summary(tmp_path))
    ctx.runner = lambda argv, env, timeout: (ctx.calls.append(argv), bs.CmdResult(0, "ok"))[1]
    res = bs.t_synthetic(ctx)
    assert res.status == bs.PASS and "provisional, not gate evidence" in res.key_result
    args = " ".join(ctx.calls[0])
    assert "synthetic_v1.py run --items 8" in args and "--redo" in args
    assert {m["name"] for m in res.metrics} >= {"block_f1", "model_acc", "param_mae", "rank1_rate"}


def test_synthetic_recovery_regression_fails_and_missing_set_is_na(ctx, monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setattr(bs, "SYNTH_SET", _synth_summary(tmp_path))
    ctx.runner = lambda argv, env, timeout: bs.CmdResult(1, "", "REGRESSION synthetic-v1-n8 model_acc")
    assert bs.t_synthetic(ctx).status == bs.FAIL
    monkeypatch.setattr(bs, "SYNTH_SET", tmp_path / "nowhere")
    assert bs.t_synthetic(ctx).status == bs.NA
