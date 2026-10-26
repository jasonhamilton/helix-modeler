"""T233 product-chain-v1: sources and held-out split, chain sampling, labels, the gain/hook in run_chains, the small
heads and a tiny end-to-end training run on synthetic features (no Native, no audio files)."""
from __future__ import annotations

import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.learning import chain_transfer as CT  # noqa: E402
from tonematch.learning import product_chain as PC  # noqa: E402

SR = PC.SR


def cal_rows():
    def rows(pairs):
        return [{"decay": d, "rt60_s": t} for d, t in pairs]
    return {"HD2_ReverbRoom": rows([(0.1, 0.52), (0.4, 0.80), (0.7, 1.29), (1.0, 2.27)]),
            "HD2_ReverbPlate": rows([(0.1, 0.97), (0.4, 1.63), (0.55, 2.53), (0.7, 5.1)]),
            "HD2_ReverbHall": rows([(0.1, 0.73), (0.4, 1.18), (0.7, 2.38), (0.85, 5.0)])}


def drive_pool():
    return {"models": ["A", "B", "C"], "knobs": {"A": ["Drive"], "B": ["Drive", "Tone"], "C": ["Gain", "Bass", "Tone"]},
            "families": {"A": "od", "B": "od", "C": "dist"}}


def test_prs_split_is_disjoint_and_overlap_is_caught():
    dur = {"free.wav": 30.0}
    PC.check_prs_split(durations=dur)
    bad = {"x": [("free.wav", 10.0, 20.0)]}
    with pytest.raises(AssertionError):
        PC.check_prs_split(train=bad, durations=dur)
    held = {f for parts in PC.PRS_TEST.values() for f, _, _ in parts if f != "free.wav"}
    train = {f for parts in PC.PRS_TRAIN.values() for f, _, _ in parts}
    assert held == {"chord_E5.wav", "chord_A5.wav", "chord_D5.wav"} and not held & train


def test_build_source_and_excerpt():
    files = {"a.wav": np.arange(10, dtype=np.float32), "b.wav": np.arange(100, 110, dtype=np.float32)}
    x = PC.build_source([("a.wav", 0.0, None), ("b.wav", 2 / SR, 5 / SR)], files.__getitem__)
    assert list(x) == list(range(10)) + [102, 103, 104]
    assert list(PC.excerpt(x, 11, 4)) == [103, 104, 0, 0]


def test_rt60_inversion_round_trip_and_ranges():
    rows = cal_rows()
    assert PC.rt60_range(rows["HD2_ReverbPlate"]) == pytest.approx((0.97, 2.5))
    assert PC.rt60_range(rows["HD2_ReverbRoom"]) == pytest.approx((0.8, 2.27))
    for m, r in rows.items():
        a, b = PC.rt60_range(r)
        for t in np.linspace(a, b, 7):
            assert PC.rt60_for_decay(r, PC.decay_for_rt60(r, t)) == pytest.approx(t, rel=1e-6)


def test_make_chains_blocks_bounds_and_rates():
    srcs = ["Guitar-TECHS/P1_x/a.wav", "prs:runs", "Guitar-TECHS/P2_x/none.wav"]
    starts = {srcs[0]: [3 * SR, 6 * SR], srcs[1]: [int(0.3 * SR), 4 * SR], srcs[2]: []}
    cs = PC.make_chains(srcs, starts, drive_pool(), cal_rows(), n=3000, seed=5, split="train", prefix="t",
                        source_weights={srcs[0]: 0.8, srcs[1]: 0.2})
    assert cs == PC.make_chains(srcs, starts, drive_pool(), cal_rows(), n=3000, seed=5, split="train", prefix="t",
                                source_weights={srcs[0]: 0.8, srcs[1]: 0.2})
    assert all(c["di_file"] != srcs[2] for c in cs)
    assert abs(np.mean([c["source"] == "prs" for c in cs]) - 0.2) < 0.03
    assert {c["player"] for c in cs} == {"P1", "PRS"}
    rates = {s: np.mean([PC.block_of(c, s) is not None for c in cs]) for s in PC.ORDER}
    assert rates["drive"] == 1.0 and abs(rates["eq"] - 0.5) < 0.04 and abs(rates["reverb"] - 0.6) < 0.04
    rows = cal_rows()
    for c in cs:
        assert [b["slot"] for b in c["blocks"]] == [s for s in PC.ORDER if PC.block_of(c, s) is not None]
        assert -6 <= c["gain_draw_db"] <= 6
        d = c["blocks"][0]
        assert set(d["cc"]) == set(drive_pool()["knobs"][d["model"]]) and all(0 <= v <= 127 for v in d["cc"].values())
        e, r = PC.block_of(c, "eq"), PC.block_of(c, "reverb")
        if e is not None:
            v = e["values"]
            if e["type"] == "graphic":
                assert v["31p25Hz"] == v["62p5Hz"] == 0.0
                assert all(abs(v[k]) <= 6.0 for k in PC.GRAPHIC_KNOBS) and max(abs(v[k]) for k in PC.GRAPHIC_KNOBS) >= 1.5 - 1e-9
            else:
                assert all(abs(v[k]) <= 6.0 for k in ("LowGain", "MidGain", "HighGain"))
                assert 5000 <= v["HighCut"] <= 12000
        if r is not None:
            assert 0.15 <= r["mix"] <= 0.50 and r["values"]["Mix"] == r["mix"]
            a, b = PC.rt60_range(rows[r["model"]])
            assert a - 1e-6 <= r["rt60_s"] <= b + 1e-6
            assert r["values"]["Predelay"] == 0.011 and r["values"]["LowCut"] == 150.0 and r["values"]["HighCut"] == 3700.0
            assert PC.rt60_for_decay(rows[r["model"]], r["decay"]) == pytest.approx(r["rt60_s"], rel=2e-3)
        assert (c["lead"] > 0) == (r is not None) or c["start"] == 0
        assert c["start"] >= 0 and c["start"] + c["lead"] in starts[c["di_file"]]


def test_level_gain_cap_and_feature():
    assert PC.level_gain(5.0, -10.0) == 5.0
    assert PC.level_gain(5.0, -3.0) == pytest.approx(2.0)
    X = np.zeros((2, 3, 4), np.float32)
    Y = PC.with_level(X, np.array([-26.7, -36.7]))
    assert Y.shape == (2, 3, 5) and np.allclose(Y[0, :, -1], 0.0) and np.allclose(Y[1, :, -1], -1.0)


def test_eq_curve_of_a_gain_is_flat():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(10 * SR).astype(np.float32) * 0.1
    c = PC.eq_curve_db(x, 0.5 * x)
    assert len(c) == len(PC.CURVE_BANDS) and np.allclose(c, 20 * math.log10(0.5), atol=0.02)


class FakePool:
    def __init__(self):
        self.last_errors = {}

    def render_batch(self, jobs, on_error="none"):
        return [x * p for x, p in jobs]

    def close(self):
        pass


def test_run_chains_applies_input_gain_and_calls_the_hook():
    n = int(CT.CLIP_S * SR)
    chains = [{"id": "a", "di_file": "f", "start": 0, "lead": 0, "gain_db": 20 * math.log10(2.0),
               "blocks": [{"slot": "drive", "model": "d", "g": 3.0}, {"slot": "eq", "model": "e", "g": 0.5}]},
              {"id": "b", "di_file": "f", "start": 0, "lead": 0,
               "blocks": [{"slot": "drive", "model": "d", "g": 1.0}]}]
    seen = []
    out_level = {}

    def embed(y):
        out_level[len(seen)] = float(np.abs(y).max())
        return np.zeros((3, 512), np.float32)

    def hook(slot, i, x_in, y_out):
        seen.append((slot, i, float(np.abs(x_in).max()), float(np.abs(y_out).max())))
    res = CT.run_chains(chains, lambda b: b["g"], lambda f, s, k: np.full(k, 0.1, np.float32), FakePool, embed,
                        make_job=lambda d, p: (d, p), log=lambda m: None, order=PC.ORDER, embed_stages="final",
                        stage_hook=hook)
    assert ("drive", 0, pytest.approx(0.2, rel=1e-5), pytest.approx(0.6, rel=1e-5)) in seen
    assert ("eq", 0, pytest.approx(0.6, rel=1e-5), pytest.approx(0.3, rel=1e-5)) in seen
    assert ("drive", 1, pytest.approx(0.1, rel=1e-5), pytest.approx(0.1, rel=1e-5)) in seen
    assert res["has"][0].tolist() == [False, True, False] and res["has"][1].tolist() == [True, False, False]
    assert len(res["crops"][0, 0]) == 3 and n > 0


def test_small_heads_learn_and_pickle():
    rng = np.random.default_rng(1)
    n = 600
    X = rng.standard_normal((n, 3, 8)).astype(np.float32)
    s = X[:, 0, 0].copy()
    X[:, :, 0] = s[:, None]                    # the signal is in every crop, as a block's sound is
    yb = (s > 0).astype(float)
    yc = np.digitize(s, [-0.4, 0.4])
    yr = np.stack([2 * s, -s], 1)
    for kind in ("logreg", "mlp"):
        mb = PC.make_aux(kind, "bce", 1, epochs=30).fit(X, yb)
        assert PC.presence_score(mb.predict(X)[:, 0], yb)["acc"] > 0.9
        mc = PC.make_aux(kind, "ce", 3, epochs=30).fit(X, yc)
        assert PC.type_score(mc.predict(X), yc)["acc"] > 0.8
        mr = PC.make_aux(kind, "mse", 2, epochs=30).fit(X, yr)
        r = PC.regression_score(mr.predict(X), yr)
        assert r["mae"] < 0.5 * r["mae_const"]
        m2 = pickle.loads(pickle.dumps(mr))
        assert np.allclose(m2.predict(X), mr.predict(X), atol=1e-5)


def test_aux_targets():
    cs = [{"blocks": [{"slot": "drive"}, {"slot": "eq", "type": "graphic", "curve_db": [1.0] * 8},
                      {"slot": "reverb", "type": "Plate", "mix": 0.3, "rt60_s": 1.5}]},
          {"blocks": [{"slot": "drive"}]}]
    T = PC.aux_targets(cs)
    assert T["reverb_present"].tolist() == [1, 0] and T["reverb_type"].tolist() == [1, -1]
    assert T["reverb_logrt60"][0] == pytest.approx(math.log(1.5)) and math.isnan(T["reverb_mix"][1])
    assert T["eq_type"].tolist() == [1, -1] and T["eq_curve"][0].tolist() == [1.0] * 8


def _write_fake_run(out: Path, n_train=160, n_test=40, seed=0):
    rng = np.random.default_rng(seed)
    pool = drive_pool()
    cal = cal_rows()
    sdir = out / "shards"
    sdir.mkdir(parents=True)
    shards, allc = [], []
    for split, pre, n, files in (("lib-test", "e", n_test, ["Guitar-TECHS/P1_t/x.wav"]),
                                 ("prs-test", "h", n_test, ["prs:free-b"]),
                                 ("train", "s", n_train, [f"Guitar-TECHS/P{1 + i % 2}_a/f{i}.wav" for i in range(6)] + ["prs:runs"])):
        starts = {f: [0, SR] for f in files}
        cs = PC.make_chains(files, starts, pool, cal, n=n, seed=seed + len(pre), split=split, prefix=pre)
        for k in range(0, n, 40):
            part = cs[k:k + 40]
            sid = f"{pre}{k // 40:03d}"
            shards.append({"id": sid, "split": split, "chains": [c["id"] for c in part]})
            rows, crops = [], []
            mi = {m: i for i, m in enumerate(pool["models"])}
            for c in part:
                c["di_level_db"] = float(rng.uniform(-40, -25))
                for b in c["blocks"]:
                    if b["slot"] == "eq":
                        b["curve_db"] = [float(v) for v in rng.normal(0, 2, 8)]
                e = rng.standard_normal((3, 512)).astype(np.float32) * 0.1
                e[:, mi[c["blocks"][0]["model"]]] += 2.0
                e[:, 10] += 2.0 * (PC.block_of(c, "reverb") is not None)
                crops.append(e)
                rows.append(c)
            np.save(sdir / f"{sid}.crops.npy", np.stack(crops).astype(np.float16))
            (sdir / f"{sid}.labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (sdir / f"{sid}.json").write_text(json.dumps({"id": sid, "split": split, "n_ok": len(rows), "failed": {}}))
            allc += part
    (out / "chains.json").write_text(json.dumps({"shards": shards, "chains": allc}))
    return pool


def test_train_run_end_to_end_on_synthetic_features(tmp_path):
    from tonematch.learning import product_chain_train as TR
    out = tmp_path / "run"
    pool = _write_fake_run(out)
    res = TR.run(out, tmp_path / "models", pool, heads=("logreg",), epochs=2, aux_epochs=2, folds=2, log=lambda m: None)
    assert res["n_train"] == 160 and res["n_test"] == {"lib-test": 40, "prs-test": 40}
    F = res["fits"]
    for arm in ("lib", "lib+prs"):
        for fk in ("0.5", "1"):
            assert f"drive/{arm}/{fk}/logreg/cond" in F
            assert f"reverb_present/{arm}/{fk}/logreg" in F
    assert "drive/lib+prs/1/logreg/uncond" in F
    assert F["drive/lib+prs/1/logreg/cond"]["lib-test"]["all"]["top1"] > 0.8
    assert F["reverb_present/lib+prs/1/logreg"]["lib-test"]["acc"] > 0.8
    assert res["fractions"]["lib/1"]["prs_chains"] == 0 and res["fractions"]["lib+prs/1"]["prs_chains"] > 0
    d = TR.load_pickle(Path(F["drive/lib+prs/1/logreg/cond"]["pickle"]))
    X = PC.with_level(np.zeros((1, 3, 512), np.float32), np.array([-30.0]))
    p = TR.drive_predict(d, X)[0]
    assert len(p["top3"]) == 3 and abs(sum(q for _, q in p["top3"])) <= 1.0 + 1e-6
    assert "| lib+prs | 1 | logreg |" in TR.results_md(res)
    # resume: nothing refitted
    res2 = TR.run(out, tmp_path / "models", pool, heads=("logreg",), epochs=2, aux_epochs=2, folds=2, log=lambda m: None)
    assert res2["fits"].keys() == F.keys()
