"""engine_timing: measured time per test, 100-test estimates and real-world jobs, all derived from
ledger fixtures (nothing hard-coded), including the 'not measured' path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine_timing as et


def row(ts, engine, metric, value, kind="render-cost", notes="", wall="", dataset="smoke", renders=""):
    return {"timestamp": ts, "commit": "x", "agent": "t", "kind": kind, "engine": engine,
            "dataset": dataset, "metric": metric, "value": str(value), "renders": renders,
            "wall_clock_s": str(wall), "notes": notes}


def pool_rows(ts="2026-10-15T21:37:08+00:00", rates=(0.9, 1.8, 2.7, 3.6, 4.5)):
    return [row(ts, "native", "pool_renders_per_s", r,
                notes=f"NativeRenderPool {i} hosts; 10 s DI clip + 3 s preroll; total footprint {1750 * i} MB")
            for i, r in enumerate(rates, 1)]


def suite_rows():
    out = [row("2026-10-15T21:40:38+00:00", "-", "suite_wall_clock_s", 300, kind="benchmark", dataset="suite",
               notes="suite run 1; smoke_native pass 7s; smoke_stomp pass 13s; parity pass 100s; "
                     "loss_trust pass 87s")]
    for name in ("K", "A", "B0", "B00", "BT0"):
        out.append(row("2026-10-15T21:37:31+00:00", "both", "null_residual_db", -31, kind="parity",
                       dataset="parity-v0", notes=f"preset={name};fw=3.50"))
    return out


def ledger():
    rows = [
        row("2026-10-14T10:00:00+00:00", "native", "seconds_per_render", 1.9),
        row("2026-10-15T10:00:00+00:00", "native", "seconds_per_render", 0.2),
        row("2026-10-15T10:00:00+00:00", "stomp", "seconds_per_render", 2.5),
        row("2026-10-15T10:05:00+00:00", "stomp", "whole_preset_load_s", 0.5),
        row("2026-10-15T10:06:00+00:00", "stomp", "dual_path_throughput_gain", 2.0),
        row("2026-10-15T10:07:00+00:00", "native", "capture_s_per_chain", 40.0),
        row("2026-10-15T10:08:00+00:00", "native", "hc_mode_switch_load_s", 7.0,
            notes="same-mode load 0.15 s"),
        row("2026-10-15T10:09:00+00:00", "-", "seconds_per_score", 0.1),
    ] + pool_rows() + suite_rows()
    return et.index_rows(rows)


# ------------------------------------------------------------------ units


@pytest.mark.parametrize("s,txt", [
    (0.1677, "0.17 s"), (2.6794, "2.68 s"), (34.2, "34.2 s"), (142, "2.4 min"),
    (3420, "57 min"), (4840, "1.3 h"), (22606, "6.3 h"), (None, "not measured")])
def test_fmt_dur_always_has_units(s, txt):
    assert et.fmt_dur(s) == txt


# ------------------------------------------------------------------ measured: newest row wins


def test_newest_row_is_used_and_cited():
    T = et.measure(ledger())
    n = T["render_2s"]["native"]
    assert n["state"] == "measured" and n["seconds"] == 0.2 and n["ts"] == "2026-10-15T10:00:00+00:00"
    assert T["render_2s"]["stomp"]["seconds"] == 2.5
    assert T["score"]["any"]["seconds"] == 0.1
    assert T["capture"]["native"]["seconds"] == 40.0
    assert T["load"]["native"]["seconds"] == 0.15 and "7.00 s" in T["load"]["native"]["note"]
    assert T["capture"]["stomp"]["state"] == "n/a"


def test_new_ledger_rows_are_picked_up_next_run():
    rows = [dict(r) for r in ledger()]
    base = et.measure(rows)
    assert base["capture"]["native"]["seconds"] == 40.0
    rows.append(dict(row("2026-10-16T09:00:00+00:00", "native", "capture_s_per_chain_after", 12.0), _line=999))
    after = et.measure(rows)
    assert after["capture"]["native"]["seconds"] == 12.0
    assert after["capture"]["native"]["date"] == "2026-10-16"


def test_ten_second_render_from_pool_row():
    T = et.measure(ledger())
    c = T["render_10s"]["native"]
    assert c["state"] == "measured" and c["seconds"] == pytest.approx(1 / 0.9)
    assert T["render_10s"]["stomp"]["state"] == "not-measured"


def test_suite_parity_per_preset_and_smoke():
    T = et.measure(ledger())
    assert T["parity"]["any"]["seconds"] == pytest.approx(100 / 5) and T["parity"]["any"]["presets"] == 5
    assert T["smoke"]["stomp"]["seconds"] == 13 and T["smoke"]["native"]["seconds"] == 7


def test_firmware_note_follows_the_upgrade_row():
    rows = ledger() + et.index_rows([row("2026-10-15T12:00:00+00:00", "stomp", "blocks_locked", 1, kind="smoke",
                                         notes="after the human upgraded the pedal to firmware 3.80: locked")])
    for i, r in enumerate(rows):
        r["_line"] = i + 2
    T = et.measure(rows)
    assert T["render_2s"]["stomp"]["fw"] == et.FW_NOTE            # measured before the upgrade
    assert "fw" not in T["render_2s"]["native"]                      # Native has no firmware
    rows.append(dict(row("2026-10-16T00:00:00+00:00", "stomp", "seconds_per_render", 2.6), _line=1000))
    assert "fw" not in et.measure(rows)["render_2s"]["stomp"]       # re-measured on 3.80


# ------------------------------------------------------------------ not measured


def test_empty_ledger_says_not_measured_everywhere():
    T = et.measure([])
    for key, cols in et.ACTIVITIES:
        for c in cols:
            if key == "capture" and c == "stomp":
                continue
            assert T[key][c]["state"] == "not-measured"
            assert et.cell_text(T[key][c]) == "not measured"
    est, _ass = et.estimate_rows(T)
    for r in est:
        for col in ("stomp", "native", "any"):
            assert r[col]["state"] == "not-measured"
            assert et.est_text(r[col], "serial") == "not measured"
    jobs, take = et.job_rows(T, Path("/nonexistent"), [], [])
    assert all(j["state"] == "not-measured" and j["serial_s"] is None for j in jobs)
    assert "No job could be timed" in take


# ------------------------------------------------------------------ estimates = 100 x per-test


def test_estimates_are_100_times_measured():
    T = et.measure(ledger())
    est, ass = et.estimate_rows(T)
    by = {r["key"]: r for r in est}
    assert et.N_TESTS == 100
    assert by["render_2s"]["native"]["serial_s"] == pytest.approx(100 * 0.2)
    assert by["render_2s"]["stomp"]["serial_s"] == pytest.approx(100 * 2.5)
    # parallel: Native pool speed-up 4.5 / 0.9 = 5; Stomp dual path 2.0
    assert by["render_2s"]["native"]["parallel_s"] == pytest.approx(100 * 0.2 / 5)
    assert by["render_2s"]["stomp"]["parallel_s"] == pytest.approx(100 * 2.5 / 2)
    assert by["render_10s"]["native"]["parallel_s"] == pytest.approx(100 * (1 / 0.9) / 5)
    assert by["score"]["any"]["serial_s"] == pytest.approx(100 * 0.1)
    # a trial is render + score, the score stays serial in the pool
    assert by["trial"]["native"]["serial_s"] == pytest.approx(100 * (0.2 + 0.1))
    assert by["trial"]["native"]["parallel_s"] == pytest.approx(100 * (0.2 / 5 + 0.1))
    assert by["capture"]["native"]["serial_s"] == pytest.approx(100 * 40.0)
    assert by["capture"]["native"]["parallel_s"] is None
    # combined lines
    assert by["combined_testing"]["any"]["serial_s"] == pytest.approx(100 * (13 + 7 + 20))
    assert by["combined_comparison"]["any"]["serial_s"] == pytest.approx(100 * 0.1)
    assert by["combined_modelling"]["native"]["serial_s"] == pytest.approx(100 * (0.2 + 40.0 + 0.1))
    assert by["combined_modelling"]["stomp"]["serial_s"] == pytest.approx(100 * (2.5 + 0.5 + 0.1))
    ids = {a["n"]: a["id"] for a in ass}
    assert set(ids.values()) >= {"pool2", "dual", "serial"}


def test_missing_score_makes_a_lower_bound():
    rows = [r for r in ledger() if r["metric"] != "seconds_per_score"]
    T = et.measure(rows)
    assert T["score"]["any"]["state"] == "not-measured"
    est, _ = et.estimate_rows(T)
    by = {r["key"]: r for r in est}
    assert by["trial"]["native"]["lower"] is True
    assert by["trial"]["native"]["serial_s"] == pytest.approx(100 * 0.2)
    assert et.est_text(by["trial"]["native"], "serial").startswith("at least ")
    assert by["combined_comparison"]["any"]["state"] == "not-measured"


# ------------------------------------------------------------------ real-world jobs


def make_root(tmp_path, with_reports=True):
    (tmp_path / "docs" / "notes").mkdir(parents=True)
    (tmp_path / "board").mkdir()
    (tmp_path / "docs" / "notes" / "synthetic-benchmark-design.md").write_text(
        "## Cost\n\n| Step | Renders | Wall-clock |\n|---|---|---|\n"
        "| Native CI run (6 items x budget 40) | 240 | 7.5 min |\n"
        "| Native full recovery (120 items x budget 150) | 18,000 | 9.4 h |\n"
        "| Stomp truth (60 items x 2 clips) | 120 | 10 min |\n"
        "| Stomp recovery, 10-item subset x budget 150 (about 4 s each) | 1,500 | 1.7 h |\n")
    (tmp_path / "docs" / "notes" / "block-probe.md").write_text("**Duration: 47.25 s with the DI excerpts**\n")
    (tmp_path / "board" / "TASKS.md").write_text(
        '- [ ] <a id="t048"></a>T048 · native-engine · capture all (~600, ~30 s each) · mode 2\n')
    if with_reports:
        for name, renders, wall in (("a", 400, 1500), ("b", 500, 1500), ("c", 600, 1500),
                                    ("d_failed_run", 10, 5)):
            d = tmp_path / "cache" / "matches" / name
            d.mkdir(parents=True)
            (d / "report.md").write_text(
                f"- engine: Native; {renders} renders (1 host restarts) in {wall} s wall clock (requested 25 min "
                "incl. final checks)\n- cost of one score on this machine: render 2.30 s + loss 0.20 s\n")
    return tmp_path


def test_jobs_use_sizes_from_the_repo_and_measured_unit_times(tmp_path):
    root = make_root(tmp_path)
    T = et.measure(ledger(), root)
    assert T["score"]["any"]["seconds"] == 0.1  # ledger beats the local report
    jobs, take = et.job_rows(T, root, [], ledger())
    by = {j["id"]: j for j in jobs}
    r10, sc, speed = 1 / 0.9, 0.1, 5.0
    # median of 400/500/600 (the failed run is skipped)
    assert by["match_section"]["serial_s"] == pytest.approx(500 * (r10 + sc))
    assert by["match_section"]["parallel_s"] == pytest.approx(500 * (r10 / speed + sc))
    assert "median of 3 local runs" in by["match_section"]["size"]
    # same budget view: 25 min buys 1500 / (r10 + sc) renders
    assert by["match_budget"]["serial_s"] == 1500
    assert f"{1500 / (r10 + sc):,.0f} renders" in by["match_budget"]["size"]
    assert by["match_song"]["serial_s"] == pytest.approx(3 * by["match_section"]["serial_s"])
    assert by["verify_top3"]["serial_s"] == pytest.approx(3 * 20)
    assert by["synth_ci"]["serial_s"] == pytest.approx(240 * (0.2 + sc))
    assert by["synth_nightly"]["serial_s"] == pytest.approx(18000 * (0.2 + sc))
    assert by["synth_stomp_truth"]["serial_s"] == pytest.approx(120 * (2.5 + 0.5 + sc))
    assert by["synth_stomp_full"]["serial_s"] == pytest.approx(9000 * (2.5 + 0.5 + sc))
    assert by["library"]["serial_s"] == pytest.approx(600 * 40.0)
    assert by["library_packed"]["serial_s"] == pytest.approx(75 * 40.0)
    assert by["probe_one"]["serial_s"] == pytest.approx((47.25 + 3) * (r10 / 13))
    assert by["probe_stomp"]["serial_s"] is None and by["probe_stomp"]["state"] == "not-measured"
    assert "slowest" not in take and take.startswith("The longest job today")
    assert '"not measured"' in take  # probe_stomp has no measurement


def test_jobs_without_local_reports_or_notes_are_not_measured(tmp_path):
    root = make_root(tmp_path, with_reports=False)
    jobs, _ = et.job_rows(et.measure(ledger(), root), root, [], ledger())
    by = {j["id"]: j for j in jobs}
    assert by["match_section"]["serial_s"] is None and by["match_song"]["serial_s"] is None
    assert by["synth_ci"]["serial_s"] is not None  # sizes come from the note, not the reports
    (root / "docs" / "notes" / "synthetic-benchmark-design.md").unlink()
    jobs, _ = et.job_rows(et.measure(ledger(), root), root, [], ledger())
    assert {j["id"]: j for j in jobs}["synth_ci"]["serial_s"] is None


def test_score_falls_back_to_local_report_when_ledger_has_none(tmp_path):
    root = make_root(tmp_path)
    rows = [r for r in ledger() if r["metric"] != "seconds_per_score"]
    T = et.measure(rows, root)
    c = T["score"]["any"]
    assert c["state"] == "measured" and c["seconds"] == 0.2 and "local match report" in c["source"]


def test_placeholders_follow_the_ledger():
    T = et.measure(ledger())
    assert et.fill_placeholders("a {t:render_2s:native} b {t:render_10s:stomp}", T) == "a 0.20 s b not measured"
