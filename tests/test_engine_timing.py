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
    assert et.measure(rows)["render_2s"]["stomp"]["fw"] == et.FW_380  # re-measured on 3.80: no stale label


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
    assert set(ids.values()) >= {"pool2assumed", "dual", "serial"}


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


def test_score_is_never_taken_from_a_local_report(tmp_path):
    root = make_root(tmp_path)
    rows = [r for r in ledger() if r["metric"] != "seconds_per_score"]
    c = et.measure(rows, root)["score"]["any"]
    assert c["state"] == "not-measured" and et.cell_text(c) == "not measured"


def test_placeholders_follow_the_ledger():
    T = et.measure(ledger())
    assert et.fill_placeholders("a {t:render_2s:native} b {t:render_10s:stomp}", T) == "a 0.20 s b not measured"


# ------------------------------------------------------------------ newest ledger metrics (2026-10-16)

HOST_NOTE = ("scripts/native_pool_bench.py N={n} hosts; {c} s DI clip + 3 s preroll on 'BP cc' (8 blocks); "
             "render + MR-STFT score per test; footprint {mb} MB total; 10 CPUs")


def new_pool_rows():
    """Layout A (one row per host count, both clips in the notes) for N=1, layout B (one row per clip)."""
    rows = [row("2026-10-16T00:25:26+00:00", "native", "renders_per_s", 1.703,
                notes="phase B NativeRenderPool N=1 hosts (map_generation; test = render + MR-STFT score) on the "
                      "8-block C8 chain: 2 s DI clip + 3 s preroll 1.703 tests/s; 10 s clip 0.806 tests/s; "
                      "total footprint 1282 MB")]
    for n, r2, r10, mb in ((4, 6.836, 3.309, 5293), (6, 10.025, 4.756, 7919), (8, 12.238, 5.597, 10593)):
        rows.append(row("2026-10-16T00:34:16+00:00", "native", "renders_per_s", r2, notes=HOST_NOTE.format(n=n, c=2, mb=mb)))
        rows.append(row("2026-10-16T00:34:17+00:00", "native", "renders_per_s", r10, notes=HOST_NOTE.format(n=n, c=10, mb=mb)))
    # a newer 6-host sweep wins; 10 hosts is over the approved cap and is never the default
    rows.append(row("2026-10-16T00:38:02+00:00", "native", "renders_per_s", 9.879, notes=HOST_NOTE.format(n=6, c=2, mb=8993)))
    rows.append(row("2026-10-16T00:38:03+00:00", "native", "renders_per_s", 4.639, notes=HOST_NOTE.format(n=6, c=10, mb=8509)))
    rows.append(row("2026-10-16T00:38:03+00:00", "native", "renders_per_s", 13.102, notes=HOST_NOTE.format(n=10, c=2, mb=12974)))
    return rows


def stomp380_rows():
    s = "stomp-speed"
    return [
        row("2026-10-15T22:36:17+00:00", "stomp", "blocks_locked_after_tonepush_info", 1, kind="smoke",
            notes="after the human upgraded the pedal to firmware 3.80 (build x)"),
        row("2026-10-15T23:47:32+00:00", "stomp", "session_overhead_per_test_old_s", 3.8123, dataset=s),
        row("2026-10-15T23:47:32+00:00", "stomp", "session_overhead_per_test_new_s", 0.7716, dataset=s),
        row("2026-10-15T23:48:50+00:00", "stomp", "seconds_per_render_clip2s", 2.6889, dataset=s),
        row("2026-10-15T23:48:51+00:00", "stomp", "seconds_per_render_clip10s", 10.6708, dataset=s),
        row("2026-10-16T00:03:24+00:00", "stomp", "search_batch_per_trial_s_after", 6.5689, dataset=s,
            notes="fw=3.80; 10 amp-param trials on Cdly0; clip 3.0 s"),
        row("2026-10-16T00:10:35+00:00", "stomp", "verify_batch_per_finalist_s_before", 29.2271, dataset=s),
        row("2026-10-16T00:10:35+00:00", "stomp", "verify_batch_per_finalist_s_after", 24.6507, dataset=s),
        row("2026-10-16T00:17:11+00:00", "stomp", "search_batch_per_trial_s_before", 10.861, dataset=s,
            notes="fw=3.80; 10 amp-param trials on K; clip 10.0 s"),
        row("2026-10-16T00:17:11+00:00", "stomp", "search_batch_per_trial_s_after", 3.7649, dataset=s,
            notes="fw=3.80; 10 amp-param trials on K; clip 3.0 s"),
    ]


def newest_ledger():
    rows = new_pool_rows() + stomp380_rows() + [
        row("2026-10-16T00:50:49+00:00", "native", "capture_per_chain_s", 9.51, dataset="templates-B-B0",
            notes="T064: B0 and B re-captured"),
        row("2026-10-16T00:58:52+00:00", "native", "capture_per_chain_s", 0.22, dataset="catalog-3.82",
            notes="T064: 391 single-block chains in 3 parallel editor sessions with --verify none (refusal scan)"),
        row("2026-10-15T23:57:11+00:00", "native", "capture_per_chain_s", 0.39, dataset="block-library-v0"),
    ]
    return et.index_rows(rows)


def test_native_pool_uses_six_hosts_and_the_newest_sweep():
    g = et.pool_group(newest_ledger())
    assert et.DEFAULT_HOSTS == 6 and g["hosts"] == 6
    c2, c10 = g["clips"][2], g["clips"][10]
    assert c2["top_rate"] == 9.879 and c10["top_rate"] == 4.639             # the newest 6-host rows
    assert c2["one_rate"] == 1.703 and c10["one_rate"] == 0.806             # N=1 layout with both clips in notes
    assert c2["speedup"] == pytest.approx(9.879 / 1.703)
    assert c2["score"] is True and c2["mem_gb"] == pytest.approx(8.993)
    assert c2["top_row"]["timestamp"] == "2026-10-16T00:38:02+00:00"


def test_native_search_test_cell_cites_the_six_host_row():
    T = et.measure(newest_ledger())
    c = T["search_test"]["native"]
    assert c["seconds"] == pytest.approx(1 / 9.879) and c["hosts"] == 6 and c["ts"] == "2026-10-16T00:38:02+00:00"
    assert c["one_seconds"] == pytest.approx(1 / 1.703)
    assert "2026-10-16T00:25:26+00:00" in c["note"]                        # the one-host row is cited too
    assert T["search_test_10s"]["native"]["seconds"] == pytest.approx(1 / 4.639)


def test_native_trial_estimate_is_measured_tests_per_second_without_a_second_score():
    est, ass = et.estimate_rows(et.measure(newest_ledger()))
    t = {r["key"]: r for r in est}["trial"]["native"]
    assert t["serial_s"] == pytest.approx(100 / 1.703) and t["parallel_s"] == pytest.approx(100 / 9.879)
    assert t["lower"] is False
    assert any(a["id"] == "pool2" and "6" in a["text"] and "2026-10-16" in a["text"] for a in ass)


def test_capture_uses_the_newest_row_and_names_the_others():
    c = et.measure(newest_ledger())["capture"]["native"]
    assert c["seconds"] == 0.22 and c["sessions"] == 3 and c["verify_off"] is True
    assert "9.51 s per chain in templates-B-B0" in c["note"] and "2026-10-16T00:50:49+00:00" in c["note"]
    assert "0.39 s per chain in block-library-v0" in c["note"]
    assert "switched off" in c["note"]
    est, ass = et.estimate_rows(et.measure(newest_ledger()))
    cap = {r["key"]: r for r in est}["capture"]["native"]
    assert cap["serial_s"] == pytest.approx(22.0)
    assert any(a["id"] == "capparallel" and "3 editor sessions" in a["text"] for a in ass)


def test_stomp_speed_rows_on_fw380_carry_no_stale_label():
    T = et.measure(newest_ledger())
    trial, ses, ver = T["search_test"]["stomp"], T["session"]["stomp"], T["verify"]["stomp"]
    assert trial["seconds"] == 3.7649 and trial["ts"] == "2026-10-16T00:17:11+00:00" and trial["fw"] == et.FW_380
    assert "6.57 s" in trial["note"] and "10.9 s" in trial["note"]       # fixed-delay chain and the old figure
    assert ses["seconds"] == 0.7716 and "3.81 s" in ses["note"]
    assert ver["seconds"] == 24.6507 and "29.2" in ver["note"]
    assert T["render_2s"]["stomp"]["seconds"] == 2.6889 and T["render_2s"]["stomp"]["fw"] == et.FW_380
    assert T["render_10s"]["stomp"]["seconds"] == 10.6708
    assert T["search_test_10s"]["stomp"]["seconds"] == 10.861
    for c in (trial, ses, ver):
        assert "3.50" not in et.cell_text(c)


def test_stomp_estimates_pay_the_session_once():
    est, ass = et.estimate_rows(et.measure(newest_ledger()))
    by = {r["key"]: r for r in est}
    t = by["trial"]["stomp"]
    assert t["serial_s"] == pytest.approx(100 * 3.7649 + 0.7716) and t["lower"] is True    # score unmeasured
    assert by["verify"]["stomp"]["serial_s"] == pytest.approx(100 * 24.6507 + 0.7716)
    assert any(a["id"] == "session" for a in ass)


def test_verify_job_uses_the_finalist_row(tmp_path):
    root = make_root(tmp_path, with_reports=False)
    rows = newest_ledger()
    T = et.measure(rows, root)
    jobs, _ = et.job_rows(T, root, [], rows)
    by = {j["id"]: j for j in jobs}
    assert by["verify_top3"]["serial_s"] == pytest.approx(3 * 24.6507 + 0.7716)
    assert by["verify_top3"]["engine"] == "Stomp"
    assert by["synth_stomp_sub"]["serial_s"] == pytest.approx(1500 * 3.7649 + 0.7716)
    assert by["synth_ci"]["serial_s"] == pytest.approx(240 / 1.703) and by["synth_ci"]["parallel_s"] == pytest.approx(240 / 9.879)


def test_stomp_load_uses_the_fw380_whole_load_median_and_cells_name_their_metric():
    rows = newest_ledger() + et.index_rows([
        row("2026-10-15T23:40:21+00:00", "stomp", "whole_preset_write_s", 0.289, dataset="fw380-write",
            notes="fw=3.80; median op-21 write of 9 (0.273-0.342 s); whole load() median 0.534 s")])
    for i, r in enumerate(rows):
        r["_line"] = i + 2
    c = et.measure(rows)["load"]["stomp"]
    assert c["seconds"] == 0.534 and c["fw"] == et.FW_380 and "0.29 s" in c["note"]
    assert "whole_preset_write_s" in et.cell_text(c)                      # the row is named next to its link
    assert "ledger 2026-10-16T00:17:11+00:00 search_batch_per_trial_s_after" in et.cell_text(
        et.measure(rows)["search_test"]["stomp"])


# ------------------------------------------------------------------ score, search run and the time-per-sample table

def trust_rows():
    note = "term={t} scoring cost per candidate: candidate features + distance; render excluded; 10 s clip"
    rows = [row("2026-10-16T15:50:20+00:00", "native", metric="seconds_per_test", value=v, kind="loss-trust",
                dataset="helix-trust-v1", notes=note.format(t=t))
            for t, v in (("ltas_tilt", 0.007638), ("crest_flatness_hnr", 0.03427), ("envelope", 0.01158),
                         ("mrstft", 0.06384), ("embed_afxrep", 0.1113))]
    # the combined render + terms row carries a wall clock and is not a score time
    rows.append(row("2026-10-16T15:50:20+00:00", "native", "seconds_per_test", 0.3514, kind="loss-trust",
                    dataset="helix-trust-v1", wall=715.0, renders=2400, notes="term=render+dsp_terms: wall per candidate"))
    return rows


def run_rows():
    return [
        row("2026-10-14T21:02:08+00:00", "native", "wall_per_render_call_s", 8.8, renders=23, wall=202.3),
        row("2026-10-16T02:26:57+00:00", "stomp", "seconds_per_trial_default", 3.8, dataset="ds-sample-intro",
            renders=39, wall=169, notes="AFTER ... Search stage 122 s / 32 trials."),
        row("2026-10-16T02:26:00+00:00", "stomp", "seconds_per_trial_legacy", 8.4, dataset="ds-sample-intro",
            renders=39, wall=329),
        row("2026-10-16T03:41:35+00:00", "native", "seconds_per_trial_full", 8.3, kind="search",
            dataset="ds-sample-intro-cliplen", renders=39, wall=33, notes="policy full; Stomp estimate 8.3 s/trial"),
        row("2026-10-16T03:42:22+00:00", "native", "seconds_per_trial_loudest3", 3.8, kind="search",
            dataset="ds-sample-intro-cliplen", renders=39, wall=25),
    ]


def test_score_comes_from_the_trust_check_rows_when_present():
    c = et.measure(et.index_rows(trust_rows()), None)["score"]["any"]
    assert c["state"] == "measured"
    assert c["seconds"] == pytest.approx(0.007638 + 0.03427 + 0.01158)
    assert "mrstft" in c["note"] and "embed_afxrep" in c["note"]     # the terms left out are named
    assert "row " in et.cell_text(c)                                  # the line is named: rows share one time


def test_score_stays_not_measured_without_all_three_terms():
    rows = [r for r in trust_rows() if "envelope" not in r["notes"]]
    assert et.measure(et.index_rows(rows), None)["score"]["any"]["state"] == "not-measured"


def test_search_run_uses_the_new_rows_not_the_stale_one():
    T = et.measure(et.index_rows(run_rows()), None)["search_run"]
    assert T["native"]["seconds"] == pytest.approx(33 / 39) and T["native"]["metric"] == "seconds_per_trial_full"
    assert T["stomp"]["seconds"] == 3.8 and T["stomp"]["metric"] == "seconds_per_trial_default"
    only_old = et.measure(et.index_rows(run_rows()[:1]), None)["search_run"]
    assert only_old["native"]["state"] == "not-measured" and only_old["stomp"]["state"] == "not-measured"


def test_stomp_10s_render_cell_names_the_clip10s_metric():
    T = et.measure(et.index_rows(stomp380_rows()), None)
    assert T["render_10s"]["stomp"]["metric"] == "seconds_per_render_clip10s"


def test_time_per_sample_table_is_ledger_derived_and_marks_gaps():
    rows = et.index_rows(new_pool_rows() + stomp380_rows() + run_rows() + trust_rows())
    t = {r["activity"]: r for r in et.time_per_sample(rows)}
    n6 = t["Native test, 2 s clip, 6 hosts"]["native"]
    assert n6["seconds"] == pytest.approx(1 / 9.879) and n6["refs"]
    assert t["Native test, 10 s clip, 1 host"]["native"]["seconds"] == pytest.approx(1 / 0.806)
    assert t["Stomp render, 10 s clip (clip + about 0.67 s)"]["stomp"]["seconds"] == 10.6708
    assert et.total_text(t["Native test, 2 s clip, 6 hosts"]) == f"Native {et.fmt_dur(39 / 9.879)}"
    # no capture, separation, two-path or parity-spot rows in this ledger: all "not measured", never filled in
    for name in ("Template capture per model (block-library-v1)", "Separation per mix (sep-stress-v1)",
                 "Two-path pass on the Stomp (twopath-parity-fw380)", "Parity spot check per model (parity-spot-fw380)"):
        row_ = t[name]
        assert all(row_[c]["seconds"] is None for c in ("native", "stomp", "other")), name
    md = et.render_time_per_sample(rows)
    assert md.startswith(et.TPS_START) and md.rstrip().endswith(et.TPS_END)
    assert "not measured" in md and "ledger/runs.csv?plain=1#L" in md


def test_time_per_sample_two_path_row_does_not_invent_a_pass_time():
    rows = et.index_rows([
        row("2026-10-16T15:50:56+00:00", "stomp", "null_residual_db", -63.9, kind="parity",
            dataset="twopath-parity-fw380", wall=26.4, renders=4, notes="Path A: single vs dual A")])
    r = {x["activity"]: x for x in et.time_per_sample(rows)}["Two-path pass on the Stomp (twopath-parity-fw380)"]
    assert r["stomp"]["seconds"] is None and "26.4 s" in r["stomp"]["note"] and r["count"]["n"] == 2.0


def test_update_benchmarking_doc_rewrites_only_between_markers(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "ledger").mkdir()
    (tmp_path / "docs" / "BENCHMARKING.md").write_text(
        f"before\n{et.TPS_START}\nold\n{et.TPS_END}\nafter\n", encoding="utf-8")
    header = "timestamp,commit,agent,kind,engine,dataset,metric,value,renders,wall_clock_s,notes\n"
    (tmp_path / "ledger" / "runs.csv").write_text(
        header + "2026-10-16T00:38:02+00:00,x,t,render-cost,native,smoke,renders_per_s,9.879,100,10.1,"
                 "\"N=6 hosts; 2 s DI clip + 3 s preroll; render + MR-STFT score per test\"\n", encoding="utf-8")
    assert et.update_benchmarking_doc(tmp_path)
    text = (tmp_path / "docs" / "BENCHMARKING.md").read_text(encoding="utf-8")
    assert text.startswith("before\n") and text.endswith("after\n") and "old" not in text
    assert "[row 2](../ledger/runs.csv?plain=1#L2" in text
    (tmp_path / "docs" / "BENCHMARKING.md").write_text("no markers\n", encoding="utf-8")
    assert not et.update_benchmarking_doc(tmp_path)
