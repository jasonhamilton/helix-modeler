"""T213: the slot-dataset-v1 overnight supervisor (scripts/slot_overnight.py) and the hooks it needs: the plan's
silence floor and out_db flag (T212 option B), half-size training (--fraction), model top-3, the scratch base
override, the skip rule and the step order."""
import json
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import drive_dataset_render as DD  # noqa: E402
import slot_overnight as SO  # noqa: E402
from test_slot_dataset import POOL, _audio, _fake_shards, _manifest, _plan  # noqa: E402

from tonematch.learning import slot_dataset as SD  # noqa: E402


def test_fits_and_estimate():
    now = 1000.0
    assert SO.fits(now, 100, now + 116)            # 100 s x 1.15 = 115 s
    assert not SO.fits(now, 100, now + 114)
    assert not SO.fits(now, 100, now + 116, extra_s=300)
    assert SO.render_estimate_s(0, None) == 0.0
    assert SO.render_estimate_s(490, 4.9) == pytest.approx(100.0)
    assert SO.render_estimate_s(10, None) == float("inf")
    assert not SO.fits(now, SO.render_estimate_s(10, 0.0), now + 1e9)


def test_refill_candidates_skip_digital_zero_and_known_clips():
    fails = [{"id": "c1", "digital_zero": False}, {"id": "c2", "digital_zero": True},
             {"id": "c3", "digital_zero": False}, {"id": "c3", "digital_zero": False}]
    assert SO.refill_candidates(fails, None) == 2
    assert SO.refill_candidates(fails, {"clips": [{"id": "c1"}]}) == 1
    assert SO.refill_candidates([{"id": "c2", "digital_zero": True}], None) == 0


def _plan_args(tmp_path, **kw):
    return Namespace(slot="cab", out=str(tmp_path / "cab"), seed=1, target=30, limit=0, all_native=False,
                     fresh_splits=False, force=False, **kw)


def _patch_plan(monkeypatch, tmp_path, SR_):
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest()))
    monkeypatch.setattr(DD, "MANIFEST", man)
    monkeypatch.setattr(DD, "read_mono_file", _audio)
    monkeypatch.setattr(SR_, "DRIVE_PLAN", tmp_path / "missing.json")
    monkeypatch.setattr(DD, "v1_splits", lambda: {})
    monkeypatch.setattr(SD, "slot_pool", lambda slot, catalog=None, all_native=False: POOL)


def test_plan_stores_the_floor_and_out_db_only_when_asked(tmp_path, monkeypatch):
    import slot_dataset_render as SR_
    _patch_plan(monkeypatch, tmp_path, SR_)
    assert SR_.cmd_plan(_plan_args(tmp_path, silence_db=-240.0)) == 0
    p = json.loads((tmp_path / "cab/plan.json").read_text())
    assert p["silence_db"] == -240.0 and p["record_out_db"] is True
    a = _plan_args(tmp_path, silence_db=None)
    a.out, a.force = str(tmp_path / "cab2"), False
    assert SR_.cmd_plan(a) == 0
    p2 = json.loads((tmp_path / "cab2/plan.json").read_text())
    assert "silence_db" not in p2 and "record_out_db" not in p2           # default unchanged
    assert SR_.parse(["plan", "--slot", "cab", "--silence-db", "-240"]).silence_db == -240.0
    assert SR_.parse(["plan", "--slot", "cab"]).silence_db is None
    assert SR_.parse(["train", "--slot", "cab", "--fraction", "0.5"]).fraction == 0.5


def test_half_size_training_uses_the_first_half_of_the_plans_shards_and_their_refill(tmp_path):
    pytest.importorskip("sklearn")
    pytest.importorskip("torch")
    out, rout = tmp_path / "amp", tmp_path / "amp-refill"
    out.mkdir()
    plan = _plan(target=240, shard_size=60)
    main = {**plan, "clips": plan["clips"][:180], "shards": plan["shards"][:3], "n_clips": 180}
    (out / "plan.json").write_text(json.dumps(main))
    _fake_shards(out, main)
    # the refill holds 30 clips of main shard 0 (kept at half: ceil(0.5 x 3) = 2 shards) and 30 of shard 2 (dropped)
    rclips = plan["shards"][0]["clips"][:30] + plan["shards"][2]["clips"][:30]
    rplan = {**plan, "clips": [c for c in plan["clips"] if c["id"] in set(rclips)],
             "shards": [{"id": "s00000", "clips": rclips}], "n_clips": 60, "record_out_db": True}
    # the refill ids must not repeat finished main clips: drop them from the main shards' labels for this test
    for sid in ("s00000", "s00002"):
        d = out / "shards"
        lab = [json.loads(x) for x in (d / f"{sid}.labels.jsonl").read_text().splitlines()]
        keep = [i for i, r in enumerate(lab) if r["id"] not in set(rclips)]
        for suf in ("single", "crops"):
            arr = np.load(d / f"{sid}.{suf}.npy")
            np.save(d / f"{sid}.{suf}.npy", arr[keep])
        (d / f"{sid}.labels.jsonl").write_text("".join(json.dumps(lab[i]) + "\n" for i in keep))
    rout.mkdir()
    (rout / "plan.json").write_text(json.dumps(rplan))
    _fake_shards(rout, rplan)
    full = SD.run_train_slot(out, tmp_path / "mf", heads=("logreg",), arms=("main",), ledger=False, extra_outs=[rout])
    assert full["n"] == 180 and full["n_refill"] == 60 and full["fraction"] == 1.0
    half = SD.run_train_slot(out, tmp_path / "mh", heads=("logreg",), arms=("main",), ledger=False, extra_outs=[rout],
                             fraction=0.5)
    assert half["n"] == 120 and half["n_refill"] == 30 and half["n_before_fraction"] == 180
    r = half["results"]["main/logreg/multi/s0"]["test"]
    assert 0.0 <= r["model_top1"] <= r["model_top3"] <= r["model_top5"] <= 1.0
    assert "Top-3" in (out / "summary.md").read_text() and "train fraction 0.5" in (out / "summary.md").read_text()


def test_base_override_moves_every_slot_directory(tmp_path):
    code = ("import sys; sys.path.insert(0, 'scripts'); sys.path.insert(0, 'src'); import slot_dataset_render as S; "
            "print(S.BASE); print(S.MODELS_BASE)")
    env = {"PATH": "/usr/bin:/bin", "TONEMATCH_SLOT_BASE": str(tmp_path / "b"),
           "TONEMATCH_SLOT_MODELS": str(tmp_path / "m"), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [str(tmp_path / "b"), str(tmp_path / "m")]


def _overnight(tmp_path, monkeypatch, **kw):
    import slot_dataset_render as SR_
    monkeypatch.setattr(SR_, "BASE", tmp_path / "base")
    a = Namespace(deadline=time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3 * 3600)), wait_pid=0,
                  slots="cab,delay,mod", silence_db=-240.0, limit=0, hosts=0, note=str(tmp_path / "note.md"),
                  no_ledger=True, stop_margin_min=15.0, **kw)
    o = SO.Overnight(a)
    calls = []
    monkeypatch.setattr(o, "proc_check", lambda after: calls.append(("proc_check", after)))
    return o, calls


def test_render_lane_order_and_skip_rule(tmp_path, monkeypatch):
    o, calls = _overnight(tmp_path, monkeypatch)
    assert o.window_end - o.deadline == 900.0          # nothing runs in the last 15 minutes of the window
    (o.base / "amp").mkdir(parents=True)
    (o.base / "amp" / "plan.json").write_text("{}")
    o.rate = 4.9      # clips/s: cab (50,000) and delay (45,000) need ~3 h each with margin; mod (12,000) fits

    def plan_step(slot):
        calls.append(("plan", slot))
        return True

    def render(slot, refill=False):
        calls.append(("render", slot, refill))
        return True
    monkeypatch.setattr(o, "stats", lambda d: None)
    monkeypatch.setattr(o, "plan_step", plan_step)
    monkeypatch.setattr(o, "render_until_done", render)
    monkeypatch.setattr(o, "refill_step", lambda slot: calls.append(("refill", slot)) or slot == "amp")
    o.render_lane()
    seq = [c for c in calls if c[0] != "proc_check"]
    assert seq == [("render", "amp", False), ("refill", "amp"), ("plan", "mod"), ("render", "mod", False),
                   ("refill", "mod")]
    skipped = [e["slot"] for e in o.events if e["kind"] == "skip"]
    assert skipped == ["cab", "delay"]
    jobs = [o.tq.get_nowait() for _ in range(o.tq.qsize())]
    assert [(j["slot"], j["size"], j["fraction"], j["with_refill"]) for j in jobs] == [
        ("amp", "half", 0.5, True), ("amp", "full", 1.0, True), ("mod", "half", 0.5, False),
        ("mod", "full", 1.0, False)]


def test_incomplete_render_is_not_trained(tmp_path, monkeypatch):
    o, _calls = _overnight(tmp_path, monkeypatch)
    o.slots = ["amp"]
    (o.base / "amp").mkdir(parents=True)
    (o.base / "amp" / "plan.json").write_text("{}")
    monkeypatch.setattr(o, "render_until_done", lambda slot, refill=False: False)
    monkeypatch.setattr(o, "refill_step", lambda slot: pytest.fail("no refill after an incomplete render"))
    o.render_lane()
    assert o.tq.qsize() == 0 and any("not complete" in e.get("problem", "") for e in o.events)


def test_train_lane_skips_a_training_that_cannot_end_before_the_deadline(tmp_path, monkeypatch):
    o, _calls = _overnight(tmp_path, monkeypatch)
    monkeypatch.setattr(o, "stats", lambda d: {"clips_ok": 100_000})
    o.train_s_per_clip = 1.0                      # 50,000 s for the half arm: far past a 3-hour window
    ran = []
    monkeypatch.setattr(o, "run", lambda *a, **k: ran.append(a) or 0)
    o.tq.put({"slot": "amp", "size": "half", "fraction": 0.5, "with_refill": True})
    o.tq.put(None)
    o.train_lane()
    assert not ran and o.trains[0]["status"].startswith("skipped")


def test_results_note_renders_half_vs_full(tmp_path, monkeypatch):
    import slot_dataset_render as SR_
    base = tmp_path / "base"
    out = base / "amp"
    out.mkdir(parents=True)
    plan = _plan(target=120, shard_size=60)
    (out / "plan.json").write_text(json.dumps(plan))
    _fake_shards(out, plan)
    monkeypatch.setattr(SR_, "BASE", base)
    e = {"model_top1": 0.5, "model_top3": 0.8, "knob_within_true": 0.4, "knob_within_top1": 0.3,
         "all_within_true": 0.1, "by_name": {"Drive": {"within_true": 0.4, "n": 10}}}
    res = {"n": 120, "results": {"main/mlp/multi/s0": {"test": e, "fit_s": 3.0}}}
    trains = [{"slot": "amp", "size": "half", "result": res}, {"slot": "amp", "size": "full", "result": res},
              {"slot": "cab", "size": "half", "status": "skipped: x"}]
    txt = SO.results_note(base, ["amp", "cab"], [{"kind": "render", "slot": "amp", "t0": 0, "t1": 60,
                                                  "wall_s": 60, "problem": "render exited 1"}], trains, time.time())
    assert "| amp | 120 | 120 | 0 |" in txt and "| cab | no plan |" in txt
    assert "| Drive | 0.400 | 0.400 | 10 |" in txt and "render exited 1" in txt and "skipped: x" in txt
    assert "Nothing here is admitted" in txt


def test_launcher_parses_and_names_the_status_file():
    sh = ROOT / "scripts/slot_overnight.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    txt = sh.read_text()
    assert "caffeinate -i -w" in txt and "nice -n 10" in txt and "status.txt" in txt
