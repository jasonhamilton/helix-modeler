"""T214 chain-transfer-v1: planning, the stage-wise block-serial renderer (fake pool) and the scoring."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.learning import chain_transfer as CT  # noqa: E402

SR = CT.SR


def pools():
    p = {}
    for s in CT.CHAIN_ORDER:
        ms = [f"{s}_m{i}" for i in range(3)]
        p[s] = {"models": ms, "knobs": {m: ["A", "B"] if i else ["A"] for i, m in enumerate(ms)},
                "families": {m: f"{s}_f" for m in ms}}
    p["amp"]["knobs"]["amp_m1"] = ["Drive", "Bass"]
    return p


FILES = ["Guitar-TECHS/P1_x/a.wav", "Guitar-TECHS/P2_x/b.wav", "Guitar-TECHS/P1_x/empty.wav"]
STARTS = {FILES[0]: [5 * SR, 9 * SR], FILES[1]: [int(0.2 * SR)], FILES[2]: []}


def test_make_chains_seeded_order_and_probs():
    a = CT.make_chains(FILES, STARTS, pools(), n=2000, seed=1)
    b = CT.make_chains(FILES, STARTS, pools(), n=2000, seed=1)
    assert a == b
    assert a != CT.make_chains(FILES, STARTS, pools(), n=2000, seed=2)
    for c in a:
        sl = CT.slots_of(c)
        assert sl == [s for s in CT.CHAIN_ORDER if s in sl]          # the fixed slot order
        assert "amp" in sl and "cab" in sl                            # always
        assert c["di_file"] != FILES[2]                               # no active window: never used
        for b_ in c["blocks"]:
            assert set(b_["cc"]) == set(pools()[b_["slot"]]["knobs"][b_["model"]])
            assert all(0 <= v <= 127 for v in b_["cc"].values())
        has_time = any(s in CT.TIME_SLOTS for s in sl)
        assert (c["lead"] > 0) == has_time
        assert 0 <= c["lead"] <= 2 * SR and c["start"] >= 0
        if has_time and c["start"] + c["lead"] >= 2 * SR:             # not clipped by the file start
            assert 0.5 * SR - 1 <= c["lead"]
    frac = {s: np.mean([s in CT.slots_of(c) for c in a]) for s in CT.CHAIN_ORDER}
    for s, p in CT.PROBS.items():
        assert abs(frac[s] - p) < 0.04, (s, frac[s])
    assert all(c["start"] + c["lead"] in STARTS[c["di_file"]] for c in a)  # the window itself is an active one


def test_make_chains_players():
    a = CT.make_chains(FILES, STARTS, pools(), n=50, seed=3)
    assert {c["player"] for c in a} <= {"P1", "P2"}


class FakePool:
    """Preset = ('gain', g) or ('fail', k) or ('silent',) or ('stereo', g). Output = input x gain."""

    def __init__(self, fails: dict | None = None):
        self.closed = False
        self.calls = 0
        self.fails = fails if fails is not None else {}
        self.last_errors = {}

    def render_batch(self, jobs, on_error="none"):
        self.calls += 1
        self.last_errors = {}
        out = []
        for j, (x, p) in enumerate(jobs):
            if p[0] == "fail":
                left = self.fails.get(p[1], 0)
                if left:
                    self.fails[p[1]] = left - 1
                    out.append(None)
                    self.last_errors[j] = "boom"
                    continue
                out.append(x * 1.0)
            elif p[0] == "silent":
                out.append(np.zeros_like(x))
            elif p[0] == "stereo":
                out.append(np.stack([x * p[1], x * p[1]], 1))
            else:
                out.append(x * p[1])
        return out

    def close(self):
        self.closed = True


def test_render_stage_retry_silence_and_fold():
    x = np.ones(100, np.float32)
    pool = FakePool({"a": 1, "b": 2})
    jobs = [(x, ("gain", 2.0)), (x, ("fail", "a")), (x, ("fail", "b")), (x, ("silent",)), (x, ("stereo", 3.0))]
    ys, errs = CT.render_stage(pool, jobs, make_job=lambda d, p: (d, p), chunk=2, log=lambda m: None)
    assert np.allclose(ys[0], 2.0) and ys[0].ndim == 1
    assert ys[1] is not None                       # failed once, retried once: ok
    assert ys[2] is None and "boom" in errs[2]     # failed twice: reported
    assert ys[3] is None and "silence" in errs[3]  # digital zero is a failure
    assert np.allclose(ys[4], 3.0) and ys[4].shape == (100,)
    assert set(errs) == {2, 3}


def test_render_stage_waits_on_guard():
    class Guard:
        n = 0

        def wait(self, log=None):
            Guard.n += 1
            return 0.0
    x = np.ones(10, np.float32)
    CT.render_stage(FakePool(), [(x, ("gain", 1.0))] * 5, make_job=lambda d, p: (d, p), chunk=2, guard=Guard(),
                    log=lambda m: None)
    assert Guard.n == 3


def test_run_chains_is_block_serial_and_drops_failed_chains():
    n = int(CT.CLIP_S * SR)
    chains = [
        {"id": "k0", "di_file": "f", "start": 0, "lead": 0,
         "blocks": [{"slot": "drive", "model": "d", "cc": {"g": 2}}, {"slot": "amp", "model": "a", "cc": {"g": 3}},
                    {"slot": "cab", "model": "c", "cc": {"g": 0.5}}]},
        {"id": "k1", "di_file": "f", "start": 0, "lead": 480,
         "blocks": [{"slot": "amp", "model": "a", "cc": {"g": 1}}, {"slot": "cab", "model": "c", "cc": {"g": 1}},
                    {"slot": "delay", "model": "x", "cc": {"g": 5}}]},
        {"id": "k2", "di_file": "f", "start": 0, "lead": 0,
         "blocks": [{"slot": "amp", "model": "a", "cc": {"fail": 1}}, {"slot": "cab", "model": "c", "cc": {"g": 1}}]},
    ]
    pools_opened = []

    def open_pool():
        p = FakePool({"z": 99})
        pools_opened.append(p)
        return p

    def presets(b):
        return ("fail", "z") if "fail" in b["cc"] else ("gain", float(b["cc"]["g"]))

    def read(f, s, k):
        return np.full(k, 0.1, np.float32)
    seen = []

    def embed(y):
        seen.append(len(y))
        return np.full((3, 4), float(np.mean(y)), np.float32)
    r = CT.run_chains(chains, presets, read, open_pool, embed, make_job=lambda d, p: (d, p), log=lambda m: None)
    o = list(CT.CHAIN_ORDER)
    assert all(p.closed for p in pools_opened) and len(pools_opened) == 4     # drive, amp, cab, delay
    assert set(seen) == {n}                                                   # lead-in dropped before embedding
    assert np.isclose(r["crops"][0, o.index("drive"), 0, 0], 0.2, atol=1e-3)
    assert np.isclose(r["crops"][0, o.index("amp"), 0, 0], 0.6, atol=1e-3)
    assert np.isclose(r["crops"][0, o.index("cab"), 0, 0], 0.3, atol=1e-3)    # 0.1 x 2 x 3 x 0.5
    assert np.isclose(r["crops"][1, o.index("delay"), 0, 0], 0.5, atol=1e-3)
    assert r["has"][1].tolist() == [s in ("amp", "cab", "delay") for s in o]
    assert "k2" in r["failed"] and not r["has"][2].any()                     # dropped, not rendered further
    assert np.isnan(r["out_db"][0, o.index("mod")])


def test_item_scores_match_score_slot():
    from tonematch.learning import afx_heads as ah
    from tonematch.learning import slot_dataset as SD
    rng = np.random.default_rng(0)
    n, M, W = 200, 7, 3
    mod_fam = np.array([0, 0, 1, 2, 2, 2, 3])
    out = {"fl": rng.normal(size=(n, 4)), "lc": rng.normal(size=(n, M)), "mu": rng.uniform(size=(n, M, W)),
           "sig": rng.uniform(0.05, 0.3, size=(n, M, W))}
    y = rng.integers(0, M, n)
    K = rng.uniform(size=(n, W))
    K[rng.uniform(size=(n, W)) < 0.3] = np.nan
    cal = ah.Calibration(T=1.3, scale=(1.0, 1.0, 1.0))
    ref = SD.score_slot(out, y, K, mod_fam, cal)
    lj, lf = ah.joint_logp(out["fl"], out["lc"], mod_fam, cal.T)
    s = CT.item_scores(lj, lf, out["mu"], y, mod_fam[y], K)
    a = CT.aggregate(s, boot=50)
    assert np.isclose(a["top1"], ref["model_top1"]) and np.isclose(a["top3"], ref["model_top3"])
    assert np.isclose(a["top5"], ref["model_top5"]) and np.isclose(a["fam1"], ref["family_top1"])
    assert np.isclose(a["knob"], ref["knob_within_true"])
    lo, hi = a["top1_ci"]
    assert lo <= a["top1"] <= hi
    sub = CT.aggregate(s, np.arange(n) < 50, boot=0)
    assert sub["n"] == 50


def test_masked_models_never_ranked():
    lj = np.log(np.array([[0.6, 0.3, 0.1]]))
    lj[:, 0] = -np.inf
    s = CT.item_scores(lj, np.zeros((1, 1)), np.zeros((1, 3, 1)), np.array([1]), np.array([0]), np.full((1, 1), np.nan))
    assert s["top1"][0] and s["pred"][0] == 1


def test_wilson():
    lo, hi = CT.wilson(50, 100)
    assert 0.39 < lo < 0.41 and 0.59 < hi < 0.61
    assert CT.wilson(0, 10)[0] == 0.0
    assert all(np.isnan(CT.wilson(0, 0)))


def test_contexts():
    c = {"blocks": [{"slot": "drive", "model": "d", "cc": {}}, {"slot": "amp", "model": "HD2_PreampX", "cc": {"Drive": 90}},
                    {"slot": "cab", "model": "c", "cc": {}}, {"slot": "reverb", "model": "r", "cc": {}}]}
    a = CT.contexts(c, "amp")
    assert a["drive present"] and a["reverb present"] and not a["delay present"]
    assert a["time effect after"] and a["delay or reverb after"] and a["preamp model"]
    assert a["amp gain knob >= 64"] is None
    d = CT.contexts(c, "drive")
    assert d["amp gain knob >= 64"] is True and "drive present" not in d
    r = CT.contexts(c, "reverb")
    assert not r["time effect after"] and not r["any block after (besides cab)"]


def test_script_refuses_more_than_four_hosts():
    import chain_transfer as S
    with pytest.raises(AssertionError):
        S.main(["render", "--hosts", "5", "--out", "/nonexistent"])
