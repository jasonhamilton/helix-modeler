"""T191 drive_dataset_render: planning (P3 never used, held-out files disjoint), resume and the disk guard, with fakes."""
import functools
import json
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import drive_dataset_render as DD  # noqa: E402

SR = DD.SR


def _manifest():
    files = []
    for p in ("P1", "P2", "P3"):
        for k in range(10):
            files.append(f"Guitar-TECHS/{p}_chords/audio/directinput/f{k}.wav")
    return {"clips": [{"path": f} for f in files]}


@functools.lru_cache(maxsize=None)
def _audio(path):
    rng = np.random.default_rng(zlib.crc32(path.encode()))
    x = np.zeros(30 * SR, np.float32)
    for t in np.arange(0.2, 29.0, 0.7):            # plucks: real playing with onsets
        i = int(t * SR)
        n = int(0.6 * SR)
        x[i:i + n] += (0.1 * rng.standard_normal(n) * np.exp(-np.arange(n) / (0.3 * SR))).astype(np.float32)
    return x


POOL = {"M_A": [("Drive", 0.0, 1.0, 0.5), ("Tone", 0.0, 1.0, 0.5), ("Output", 0.0, 1.0, 0.5)],
        "M_B": [("Gain", 0.0, 1.0, 0.5), ("Output", 0.0, 1.0, 0.5)]}
KNOBS = {"M_A": ("Drive", "Tone"), "M_B": ("Gain", None)}


@pytest.fixture(autouse=True)
def fake_families(monkeypatch):
    from tonematch.learning import drive_pilot as dp
    monkeypatch.setattr(dp, "families", lambda: (["F1"], ["M_A", "M_B"], {"M_A": "F1", "M_B": "F1"}))


def _v1():
    return {f"Guitar-TECHS/{p}_chords/audio/directinput/f{k}.wav": s
            for p in ("P1", "P2") for k, s in ((0, "test"), (1, "test"), (2, "cal"), (3, "train"), (4, "train"))}


def _plan(**kw):
    return DD.make_plan(_manifest(), POOL, KNOBS, _audio, v1=_v1(), target=kw.pop("target", 400), **kw)


def test_plan_never_uses_p3():
    plan = _plan()
    assert all(c["player"] in ("P1", "P2") for c in plan["clips"])
    assert not any("P3" in c["di_file"] for c in plan["clips"])
    assert not any("P3" in f for v in plan["files"].values() for f in v)


def test_held_out_split_by_whole_file_and_disjoint():
    plan = _plan()
    files = plan["files"]
    sets = {k: set(v) for k, v in files.items()}
    for a in sets:
        for b in sets:
            if a < b:
                assert not sets[a] & sets[b], (a, b)
    assert len(sets["test"]) == 4                         # 20 % of 20 P1/P2 files
    assert not sets["test"] & set(_v1())                  # only files T186/T189 never used
    assert {DD.player_of(f) for f in sets["test"]} == {"P1", "P2"}
    assert sets["v1test"] == {f for f, s in _v1().items() if s == "test"}
    for c in plan["clips"]:                                # every clip goes where its file goes
        assert c["di_file"] in sets[c["split"]]


def test_plan_seeded_level_at_default_and_sized():
    a, b = _plan(seed=3), _plan(seed=3)
    assert a["clips"] == b["clips"]
    for c in a["clips"]:
        assert "Output" not in c["cc"]
        assert all(0 <= v <= 127 for v in c["cc"].values())
    small = _plan(hosts=1, hours=0.01, sec_per_clip=1.0, target=10_000)
    assert small["n_clips"] == 36
    assert _plan(target=50)["n_clips"] == 50


def test_active_starts_rejects_silence():
    assert DD.active_starts(np.zeros(20 * SR, np.float32)) == []
    assert DD.active_starts(_audio("x"))


class FakeEngine:
    def __init__(self, silent_models=(), crash_every=0):
        self.silent, self.crash_every, self.n, self.closed, self.model = set(silent_models), crash_every, 0, False, None

    def load(self, preset):
        self.model = preset["model"]

    def render(self, dry):
        self.n += 1
        if self.crash_every and self.n % self.crash_every == 0:
            raise RuntimeError("host died")
        if self.model in self.silent:
            return np.zeros_like(dry)
        return dry * 2.0

    def close(self):
        self.closed = True


class FakeBlock:
    def __init__(self, m):
        self.m = m

    def from_knobs(self, cc):
        return dict(cc)

    def with_values(self, v):
        return {"model": self.m, **v}


class FakeFeat:
    def __init__(self, v):
        self.mid = v


class FakeEmb:
    def embed_batch(self, clips):
        return [FakeFeat(np.full(512, float(np.abs(c).mean()), np.float32)) for c in clips]


def _run(plan, out, **kw):
    engines = []
    ekw = kw.pop("engine_kw", {})

    def open_engine():
        engines.append(FakeEngine(**ekw))
        return engines[-1]
    blocks = {m: FakeBlock(m) for m in POOL}
    n = DD.work_loop(plan, out, open_engine=open_engine, blocks=blocks, emb=FakeEmb(),
                     read_excerpt=lambda f, s, n: _audio(f)[s:s + n], deadline=kw.pop("deadline", 1e18),
                     min_free_gb=8.5, max_load=9.0, free=kw.pop("free", lambda p: 100.0), log=lambda m: None,
                     loadavg=lambda: (1.0, 1.0, 1.0), **kw)
    return n, engines


def test_render_resume_and_shard_files(tmp_path):
    plan = _plan(target=30, shard_size=10)
    n, eng = _run(plan, tmp_path)
    assert n == 3 and eng[0].closed
    sd = tmp_path / "shards"
    for s in plan["shards"]:
        d = json.loads((sd / f"{s['id']}.json").read_text())
        assert d["n_ok"] == 10
        assert np.load(sd / f"{s['id']}.single.npy").dtype == np.float16
        assert np.load(sd / f"{s['id']}.crops.npy").shape == (10, 3, 512)
    n2, eng2 = _run(plan, tmp_path)                      # resume: nothing left, no engine opened
    assert n2 == 0 and not eng2
    (sd / f"{plan['shards'][1]['id']}.json").unlink()    # a shard without its JSON is not done
    n3, _ = _run(plan, tmp_path)
    assert n3 == 1
    labels, S, C = DD.load_new(tmp_path)
    assert len(labels) == 30 and S.shape == (30, 512) and C.shape == (30, 3, 512)


def test_render_silence_and_crash_are_retried_then_skipped(tmp_path):
    plan = _plan(target=20, shard_size=20)
    n, _ = _run(plan, tmp_path, engine_kw={"silent_models": ("M_B",), "crash_every": 7})
    d = json.loads((tmp_path / "shards" / "s00000.json").read_text())
    n_b = sum(1 for c in plan["clips"] if c["model"] == "M_B")
    assert d["n_failed"] >= n_b and d["n_ok"] + d["n_failed"] == 20
    assert all("silence" in f["error"] or "host died" in f["error"] for f in d["failures"])


def test_disk_guard_stops_cleanly(tmp_path):
    plan = _plan(target=30, shard_size=10)
    free = iter([100.0, 7.9, 7.9, 7.9])
    n, eng = _run(plan, tmp_path, free=lambda p: next(free))
    assert n == 1
    assert "disk" in DD.stop_flag(tmp_path).read_text()
    assert eng[0].closed
    n2, _ = _run(plan, tmp_path)                          # the flag stops the rest of this run's workers too
    assert n2 == 0


def test_deadline_stops(tmp_path):
    plan = _plan(target=20, shard_size=10)
    n, eng = _run(plan, tmp_path, deadline=0.0)
    assert n == 0 and not eng
    assert DD.stop_flag(tmp_path).read_text().strip() == "hours"


def test_stop_request_leaves_between_shards(tmp_path):
    plan = _plan(target=30, shard_size=10)

    class Watch:
        k = 0

        def should_stop(self):
            Watch.k += 1
            return Watch.k > 1
    n, eng = _run(plan, tmp_path, watch=Watch())
    assert n == 1 and eng[0].closed


def test_render_pauses_on_busy_cores_with_hysteresis_and_resumes(tmp_path):
    plan = _plan(target=10, shard_size=10)
    seq = iter([9.8, 9.2, 8.9, 8.6, 8.4] + [1.0] * 500)   # over 9.5, then not yet below 8.5, then clear
    slept, logs = [], []
    state = {"v": 1.0}

    def demand():
        return (state["v"], 0.0, 0.0)

    def sleep(s):
        slept.append(s)
        state["v"] = next(seq)

    state["v"] = 9.8
    gate = DD.make_gate(9.5)
    assert gate.resume_below == pytest.approx(8.5)
    blocks = {m: FakeBlock(m) for m in POOL}
    shard = plan["shards"][0]
    clips = {c["id"]: c for c in plan["clips"]}
    res = DD.render_shard(shard, clips, FakeEngine(), blocks, FakeEmb(), lambda f, s, n: _audio(f)[s:s + n],
                          loadavg=demand, sleep=sleep, log=logs.append, gate=gate)
    assert len(slept) == 5 and len(res["labels"]) == 10     # paused until 8.4 (< 8.5), then rendered all
    assert any("pausing" in m for m in logs) and any("resumed" in m for m in logs)


def test_rate_counts_only_this_runs_clips(tmp_path, monkeypatch):
    """A resumed run must not divide earlier batches' clips by this run's elapsed time."""
    import json
    import time
    import drive_dataset_render as D
    sdir = tmp_path / "shards"
    sdir.mkdir()
    plan = {"shards": [{"id": f"s{i:05d}"} for i in range(3)], "n_clips": 300}
    for i in range(2):   # two shards from an earlier batch
        (sdir / f"s{i:05d}.json").write_text(json.dumps({"n_ok": 100, "n_failed": 0}))
    monkeypatch.setattr(D, "demand_now", lambda: (1.0,))
    monkeypatch.setattr(D, "free_gb", lambda out: 50.0)
    t0 = time.time() - 100.0
    st = D.write_progress(tmp_path, plan, t0, clips_at_start=200)
    assert st["clips_ok_this_run"] == 0 and st["clips_per_s_this_run"] == 0.0
    (sdir / "s00002.json").write_text(json.dumps({"n_ok": 100, "n_failed": 0}))
    st = D.write_progress(tmp_path, plan, t0, clips_at_start=200)
    assert st["clips_ok"] == 300 and st["clips_ok_this_run"] == 100
    assert 0.9 < st["clips_per_s_this_run"] < 1.1
