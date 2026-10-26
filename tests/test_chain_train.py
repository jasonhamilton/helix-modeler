"""T215 chain-train-v1: training + scoring end to end on synthetic chain features (no Native, no AFx-Rep)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tonematch.learning import chain_train as TR  # noqa: E402
from tonematch.learning import chain_transfer as CT  # noqa: E402

D = 24


def pools():
    p = {}
    for s in CT.CHAIN_ORDER:
        ms = [f"{s}_m{i}" for i in range(3)]
        p[s] = {"models": ms, "knobs": {m: ["A", "B"] for m in ms}, "families": {m: m for m in ms}}
    return p


def fake_features(chains, rng, vec):
    X = np.zeros((len(chains), 3, D), np.float32)
    for i, c in enumerate(chains):
        v = sum(vec[(b["slot"], b["model"])] * (1 + b["cc"]["A"] / 127.0) for b in c["blocks"])
        X[i] = v[None] + 0.05 * rng.normal(size=(3, D))
    return X


@pytest.fixture()
def dirs(tmp_path):
    rng = np.random.default_rng(0)
    vec = {(s, m): rng.normal(size=D) for s in CT.CHAIN_ORDER for m in pools()[s]["models"]}
    files_tr = [f"Guitar-TECHS/P{1 + k % 2}_x/f{k}.wav" for k in range(6)]
    files_te = ["Guitar-TECHS/P1_x/te.wav", "Guitar-TECHS/P2_x/te.wav"]
    starts = {f: [0, 48_000 * 3] for f in files_tr + files_te}
    tr = CT.make_chains(files_tr, starts, pools(), n=240, seed=5)
    te = CT.make_chains(files_te, starts, pools(), n=60, seed=6)
    out, test = tmp_path / "train", tmp_path / "test"
    (out / "shards").mkdir(parents=True)
    test.mkdir()
    shards = [{"id": f"s{j:03d}", "chains": [c["id"] for c in tr[j * 60:(j + 1) * 60]]} for j in range(4)]
    (out / "chains.json").write_text(json.dumps({"shards": shards, "chains": tr}))
    Xtr = fake_features(tr, rng, vec)
    for j, s in enumerate(shards):
        cs = tr[j * 60:(j + 1) * 60]
        np.save(out / "shards" / f"{s['id']}.crops.npy", Xtr[j * 60:(j + 1) * 60].astype(np.float16))
        (out / "shards" / f"{s['id']}.labels.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cs))
        if j < 3:      # shard 3 unfinished: no JSON -> not loaded
            (out / "shards" / f"{s['id']}.json").write_text("{}")
    Xte = fake_features(te, rng, vec)
    crops = np.zeros((len(te), 7, 3, D), np.float16)
    has = np.zeros((len(te), 7), bool)
    for i, c in enumerate(te):
        k = CT.CHAIN_ORDER.index(CT.slots_of(c)[-1])
        crops[i, k], has[i, k] = Xte[i], True
    np.savez(test / "features.npz", crops=crops, has=has, ids=np.array([c["id"] for c in te]))
    (test / "chains.json").write_text(json.dumps({"chains": te, "files": files_te}))
    (test / "render_meta.json").write_text(json.dumps({"failed": {te[0]["id"]: "digital zero"}}))
    agg = {"top1": 0.0, "top3": 0.1, "fam1": 0.0, "knob": 0.3, "n": 10}
    (test / "results.json").write_text(json.dumps({"slots": {s: {"heads": {"mlp": {"all": agg, "breakdown": {
        "drive present": {"yes": agg, "no": agg}}}}} for s in CT.CHAIN_ORDER}}))
    single = {}
    for s in CT.CHAIN_ORDER:
        def ld(split, s=s):
            n = 90
            ms = pools()[s]["models"]
            lab = [{"model": ms[i % 3], "cc": {"A": int(rng.integers(0, 128)), "B": 5}, "di_file": files_tr[i % 6]
                    if split == "train" else "x/te.wav", "player": "P1" if i % 2 else "P2"} for i in range(n)]
            X = np.stack([vec[(s, r["model"])] * (1 + r["cc"]["A"] / 127.0) for r in lab])[:, None].repeat(3, 1)
            return lab, (X + 0.05 * rng.normal(size=X.shape)).astype(np.float32)
        single[s] = ld
    return out, test, single, tr


def test_run_end_to_end(dirs, tmp_path):
    out, test, single, tr = dirs
    res = TR.run(out, test, tmp_path / "models", pools=pools(), ledger=False, epochs=15, single_loaders=single,
                 log=lambda m: None)
    assert res["n_chains"] == 180 and res["n_shards"] == 3             # the unfinished shard is not used
    assert res["n_test"] == 59                                         # the failed test chain is dropped
    assert res["fractions"]["0.5"]["chains"] == 120                    # ceil(0.5 x 3) = 2 shards
    amp = res["slots"]["amp"]
    assert set(amp) == {"chain/0.5/logreg", "chain/0.5/mlp", "chain/1/logreg", "chain/1/mlp",
                        "single+chain/1/logreg", "single+chain/1/mlp"}
    assert amp["single+chain/1/mlp"]["train_clips"] == 180 + 90
    assert amp["chain/1/mlp"]["train_chain_clips"] == 180
    assert amp["chain/1/logreg"]["all"]["top1"] > 0.6                  # separable synthetic features
    assert amp["single+chain/1/mlp"]["single_block_test"]["n"] == 90
    assert "drive present" in amp["chain/1/mlp"]["breakdown"]
    pr = res["presence"]["1"]["logreg"]
    assert set(pr) == set(TR.OPTIONAL) and pr["drive"]["n"] == 59
    assert (tmp_path / "models" / "single_chain" / "amp_mlp.pkl").is_file()
    assert (out / "results.md").read_text().startswith("# chain-train-v1")


def test_test_files_must_not_train(dirs, tmp_path):
    out, test, single, tr = dirs
    doc = json.loads((test / "chains.json").read_text())
    doc["chains"][1]["di_file"] = tr[0]["di_file"]
    (test / "chains.json").write_text(json.dumps(doc))
    with pytest.raises(AssertionError):
        TR.run(out, test, tmp_path / "m", pools=pools(), ledger=False, epochs=2, single_loaders=single,
               log=lambda m: None, slots=("amp",))


def test_presence_scores():
    Y = np.array([[1, 0, 0, 0, 0], [0, 1, 0, 0, 0], [1, 0, 0, 0, 1], [1, 0, 0, 1, 0]], float)
    P = Y.copy()
    P[0, 0] = 0.2
    s = TR.presence_scores(P, Y)
    assert s["drive"]["acc"] == 0.75 and s["drive"]["tpr"] == 2 / 3 and s["drive"]["tnr"] == 1.0
    assert s["drive"]["majority"] == 0.75 and s["mod"]["acc"] == 1.0


def test_plan_guards():
    import chain_train as S
    with pytest.raises(AssertionError):
        S.main(["render", "--hosts", "5"])
    files = S.train_files()
    plan = json.loads(S.T214.DRIVE_PLAN.read_text()) if S.T214.DRIVE_PLAN.is_file() else None
    if plan is None:
        pytest.skip("no drive-dataset-v1 plan on this machine")
    assert not set(files) & set(plan["files"]["test"]) and all("/P3" not in f for f in files)


def test_resume_reuses_cached_fits_and_runs_oracle(dirs, tmp_path):
    out, test, single, tr = dirs
    logs = []
    res1 = TR.run(out, test, tmp_path / "models", pools=pools(), ledger=False, epochs=5, single_loaders=single,
                  log=logs.append, slots=("amp", "cab"), resume=True, oracle=True)
    assert res1["resumed"] == [] and not any("cached" in m for m in logs)
    o = res1["oracle_amp_cab"]
    assert o["n_cabs"] == 3 and o["n_test"] == 59 and o["n_train"] == 180
    for kind in ("logreg", "mlp"):
        for arm in ("true_cab", "random_cab"):
            assert 0.0 <= o[kind][arm]["top3"] <= 1.0 and o[kind][arm]["n"] == 59
    assert "Oracle: amp head with the TRUE cab id" in (out / "results.md").read_text()
    logs.clear()
    res2 = TR.run(out, test, tmp_path / "models", pools=pools(), ledger=False, epochs=5, single_loaders=single,
                  log=logs.append, slots=("amp", "cab"), resume=True, oracle=True)
    assert set(res2["resumed"]) == {f"{s}/{k}" for s in ("amp", "cab") for k in res1["slots"][s]}
    assert sum(": cached" in m for m in logs) == 12                     # 2 slots x 6 (arm, fraction, head) fits
    j = lambda x: json.loads(json.dumps(x, default=float))  # noqa: E731  (tuples become lists on disk)
    assert j(res2["slots"]["amp"]["chain/1/mlp"]["all"]) == j(res1["slots"]["amp"]["chain/1/mlp"]["all"])
    assert j(res2["presence"]) == j(res1["presence"]) and j(res2["oracle_amp_cab"]) == j(o)
    # a missing pickle forces a refit of that one full-size head
    (tmp_path / "models" / "chain" / "amp_mlp.pkl").unlink()
    logs.clear()
    TR.run(out, test, tmp_path / "models", pools=pools(), ledger=False, epochs=5, single_loaders=single,
           log=logs.append, slots=("amp",), resume=True, oracle=False)
    assert sum(": cached" in m for m in logs) == 5 and (tmp_path / "models" / "chain" / "amp_mlp.pkl").is_file()


def test_with_cab_onehot():
    chains = [{"blocks": [{"slot": "amp", "model": "a"}, {"slot": "cab", "model": "c1"}]},
              {"blocks": [{"slot": "amp", "model": "a"}]}]
    X = np.ones((2, 3, 4), np.float32)
    Z = TR.with_cab_onehot(chains, X, ["c0", "c1"], scale=2.0)
    assert Z.shape == (2, 3, 6) and Z[0, :, 5].tolist() == [2.0] * 3 and Z[0, :, 4].sum() == 0 and Z[1, :, 4:].sum() == 0
    R = TR.with_cab_onehot(chains, X, ["c0", "c1"], rng=np.random.default_rng(0))
    assert (R[:, :, 4:].sum(2) == 1).all()


def test_load_previous_ignores_other_data(tmp_path):
    (tmp_path / "results.json").write_text(json.dumps({"n_chains": 5, "n_shards": 1, "n_test": 2, "slots": {"amp": {}}}))
    assert TR.load_previous(tmp_path, {"n_chains": 5, "n_shards": 1, "n_test": 2})["slots"] == {"amp": {}}
    assert TR.load_previous(tmp_path, {"n_chains": 6, "n_shards": 1, "n_test": 2}) == {}
    assert TR.load_previous(tmp_path / "none", {"n_chains": 5, "n_shards": 1, "n_test": 2}) == {}
