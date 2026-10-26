"""T208 slot-dataset-v1: planning (P3 never read, drive's file split reused, level knobs at default, lead-in),
the lead-in crop in the shared render loop, the k-knob heads, scoring, training end to end, and the re-plan guard."""
import functools
import json
import subprocess
import sys
import zlib
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import drive_dataset_render as DD  # noqa: E402

from tonematch.learning import afx_heads as ah  # noqa: E402
from tonematch.learning import slot_dataset as SD  # noqa: E402

SR = SD.SR


def _manifest():
    files = [f"Guitar-TECHS/{p}_chords/audio/directinput/f{k}.wav" for p in ("P1", "P2", "P3") for k in range(10)]
    return {"clips": [{"path": f} for f in files]}


@functools.lru_cache(maxsize=None)
def _audio(path):
    rng = np.random.default_rng(zlib.crc32(path.encode()))
    x = np.zeros(30 * SR, np.float32)
    for t in np.arange(0.2, 29.0, 0.7):
        i, n = int(t * SR), int(0.6 * SR)
        x[i:i + n] += (0.1 * rng.standard_normal(n) * np.exp(-np.arange(n) / (0.3 * SR))).astype(np.float32)
    return x


def _splits():
    out = {}
    for p in ("P1", "P2"):
        for k in range(10):
            out[f"Guitar-TECHS/{p}_chords/audio/directinput/f{k}.wav"] = \
                ("test" if k < 2 else "v1test" if k < 4 else "cal" if k == 4 else "train")
    return out


POOL = {"HD2_AmpFoo": [("Bass", 0.0, 1.0, 0.5), ("Ch Vol", 0.0, 1.0, 0.5), ("Drive", 0.0, 1.0, 0.5),
                       ("Output", 0.0, 1.0, 0.5)],
        "HD2_PreampFoo": [("Bass", 0.0, 1.0, 0.5), ("Drive", 0.0, 1.0, 0.5)],
        "HD2_AmpBar": [("Gain", 0.0, 1.0, 0.5), ("Mid", 0.0, 1.0, 0.5), ("Treble", 0.0, 1.0, 0.5)]}


def _plan(slot="amp", **kw):
    return SD.make_slot_plan(slot, _manifest(), POOL, _audio, splits=kw.pop("splits", _splits()),
                             target=kw.pop("target", 200), **kw)


def test_family_rules():
    assert SD.family_of("amp", "HD2_AmpFoo") == SD.family_of("amp", "HD2_PreampFoo") == "Foo"
    assert SD.family_of("cab", "HD2_CabMicIr_2x12Bell") == SD.family_of("cab", "HD2_Cab2x12Bell") == "2x12Bell"
    assert SD.family_of("delay", "HD2_DelayX") == "HD2_DelayX"
    assert SD.CHAIN_ORDER == ("drive", "amp", "cab", "mod", "delay", "reverb", "eq")


def test_plan_never_reads_p3_and_reuses_the_drive_split():
    plan = _plan()
    assert not any("P3" in c["di_file"] or c["player"] == "P3" for c in plan["clips"])
    sp = _splits()
    for c in plan["clips"]:
        assert c["split"] == sp[c["di_file"]]
    sets = {k: set(v) for k, v in plan["files"].items()}
    assert sets["test"] == {f for f, s in sp.items() if s == "test"}
    assert not (sets["test"] | sets["v1test"]) & (sets["train"] | sets["cal"])


def test_plan_levels_at_default_seeded_and_labelled():
    a, b = _plan(seed=5), _plan(seed=5)
    assert a["clips"] == b["clips"]
    assert a["knobs"]["HD2_AmpFoo"] == ["Bass", "Drive"]           # Ch Vol and Output stay at their default
    for c in a["clips"]:
        assert set(c["cc"]) == set(a["knobs"][c["model"]])
        assert all(0 <= v <= 127 for v in c["cc"].values())
        assert c["lead"] == 0 and c["slot"] == "amp"
        assert -80 < c["di_level_db"] < 0
        assert c["family"] == SD.family_of("amp", c["model"])
    assert a["n_clips"] == 200 and len(a["shards"]) == 2


def test_time_slots_get_a_random_lead_in_before_an_active_window():
    plan = _plan("delay", target=60)
    leads = [c["lead"] for c in plan["clips"]]
    assert all(0 <= x <= 2.0 * SR for x in leads) and max(leads) > 0.5 * SR
    for c in plan["clips"]:
        assert c["start"] + c["lead"] in DD.active_starts(_audio(c["di_file"]))   # the 10 s clip is an active window
    assert _plan("amp", target=10)["clips"][0]["lead"] == 0


def test_limit_plan_touches_every_split():
    plan = _plan(limit=6)
    assert plan["n_clips"] == 6
    assert {c["split"] for c in plan["clips"]} == {"train", "cal", "test", "v1test"}


class _Eng:
    def __init__(self):
        self.closed = False

    def load(self, p):
        self.m = p["model"]

    def render(self, dry):
        return dry * 2.0

    def close(self):
        self.closed = True


class _Block:
    def __init__(self, m):
        self.m = m

    def from_knobs(self, cc):
        return dict(cc)

    def with_values(self, v):
        return {"model": self.m, **v}


class _Feat:
    def __init__(self, v):
        self.mid = v


class _Emb:
    lens: list = []

    def embed_batch(self, clips):
        _Emb.lens += [len(c) for c in clips]
        return [_Feat(np.full(512, float(np.abs(c).mean()), np.float32)) for c in clips]


def test_render_loop_reads_the_lead_in_and_crops_it_before_embedding(tmp_path):
    plan = _plan("reverb", target=10, shard_size=10)
    asked = []

    def read(f, s, n):
        asked.append((s, n))
        return _audio(f)[s:s + n]
    _Emb.lens = []
    n = DD.work_loop(plan, tmp_path, open_engine=_Eng, blocks={m: _Block(m) for m in POOL}, emb=_Emb(),
                     read_excerpt=read, deadline=1e18, min_free_gb=1.0, max_load=99.0, free=lambda p: 100.0,
                     log=lambda m: None, loadavg=lambda: (1.0, 1.0, 1.0), label_keys=SD.LABEL_KEYS)
    assert n == 1
    by_start = {c["start"]: c for c in plan["clips"]}
    for s, k in asked:
        assert k == int(10 * SR) + by_start[s]["lead"]
    assert set(_Emb.lens) == {int(10 * SR) // 3, int(6 * SR)}          # crops of the 10 s clip after the lead
    labels, S, C = DD.load_new(tmp_path)
    assert len(labels) == 10 and set(labels[0]) == set(SD.LABEL_KEYS)


def test_drive_labels_unchanged_by_the_new_hooks():
    assert DD.LABEL_KEYS == ("id", "model", "family", "cc", "di_file", "start", "player", "split", "drive", "tone")


def test_knob_matrix_columns_follow_each_models_own_knobs():
    knobs = {"A": ["Bass", "Drive"], "B": ["Gain", "Mid", "Treble"]}
    L = [{"model": "A", "cc": {"Bass": 127, "Drive": 0}}, {"model": "B", "cc": {"Gain": 0, "Mid": 127, "Treble": 127}}]
    K = SD.knob_matrix(L, ["A", "B"], knobs)
    assert K.shape == (2, 3)
    assert K[0, 0] == 1.0 and K[0, 1] == 0.0 and np.isnan(K[0, 2])
    assert list(K[1]) == [0.0, 1.0, 1.0]


def _synthetic(n_per=30, seed=0, W=3):
    rng = np.random.default_rng(seed)
    mod_fam = np.array([0, 0, 1, 2])
    cent = rng.standard_normal((4, 16)) * 2
    y = np.repeat(np.arange(4), n_per)
    K = rng.uniform(0, 1, (len(y), W))
    K[y == 3, W - 1] = np.nan
    X = cent[y] + 0.3 * rng.standard_normal((len(y), 16))
    X[:, :W] += 3 * np.nan_to_num(K)
    return X, y, K, mod_fam


def test_heads_take_any_number_of_knob_columns():
    X, y, K, mf = _synthetic(W=3)
    h = ah.LinearHeads(mod_fam=mf, alpha=0.1).fit(X, y, K)
    o = h.predict(np.stack([X, X], 1))
    assert o["mu"].shape == (len(y), 4, 3) and np.isnan(o["mu"][:, 3, 2]).all()
    files = np.array([f"f{i % 6}" for i in range(len(y))])
    players = np.array(["P1" if i % 6 < 3 else "P2" for i in range(len(y))])
    K2 = np.concatenate([K, np.full((len(y), 1), np.nan)], 1)      # a column no clip has
    o2 = {**o, "mu": np.concatenate([o["mu"], np.full((len(y), 4, 1), np.nan)], 2),
          "sig": np.concatenate([o["sig"], np.full((len(y), 4, 1), np.nan)], 2)}
    cal = ah.calibrate(o2, y, K2, mf, files, players)
    assert len(cal.scale) == 4 and cal.scale[3] == 1.0
    r = SD.score_slot(o, y, K, mf, ah.calibrate(o, y, K, mf, files, players),
                      knob_names=[["a", "b", "c"]] * 3 + [["a", "b"]])
    assert r["model_top1"] > 0.9 and r["knob_within_true"] > 0.5
    assert set(r["by_name"]) == {"a", "b", "c"} and r["by_name"]["c"]["n"] == 90
    pytest.importorskip("torch")
    m = ah.MLPHeads(mod_fam=mf, epochs=2, hidden=16).fit(X, y, K)
    om = m.predict(X)
    assert om["sig"].shape == (len(y), 4, 3) and np.isnan(om["sig"][:, 3, 2]).all()


def test_old_two_knob_mlp_pickles_still_predict():
    pytest.importorskip("torch")
    X, y, K, mf = _synthetic(W=2)
    m = ah.MLPHeads(mod_fam=mf, epochs=1, hidden=8).fit(X, y, K)
    del m.__dict__["n_knobs"]                                     # as a checkpoint pickled before T208
    assert m.predict(X)["mu"].shape == (len(y), 4, 2)


def test_score_wrong_model_misses_every_knob_with_own_model():
    mf = np.array([0, 1])
    K = np.array([[0.5, 0.5]])
    mu = np.full((1, 2, 2), 0.5)
    out = {"fl": np.log(np.array([[0.1, 0.9]])), "lc": np.zeros((1, 2)), "mu": mu, "sig": np.full((1, 2, 2), 0.1)}
    r = SD.score_slot(out, np.array([0]), K, mf, None)
    assert r["model_top1"] == 0.0 and r["knob_within_true"] == 1.0 and r["knob_within_top1"] == 0.0
    assert r["all_within_true"] == 1.0


def _fake_shards(out: Path, plan: dict):
    """Shards with model-separable features (no Native, no encoder)."""
    rng = np.random.default_rng(1)
    cent = {m: rng.standard_normal(512) * 2 for m in plan["models"]}
    sdir = out / "shards"
    sdir.mkdir(parents=True, exist_ok=True)
    clips = {c["id"]: c for c in plan["clips"]}
    for s in plan["shards"]:
        cs = [clips[i] for i in s["clips"]]
        crops = np.stack([np.stack([cent[c["model"]] + 0.3 * rng.standard_normal(512) for _ in range(3)])
                          for c in cs]).astype(np.float16)
        for c, cr in zip(cs, crops):
            ks = plan["knobs"][c["model"]]
            for j, nm in enumerate(ks):
                cr[:, j] += np.float16(3 * c["cc"][nm] / 127)
        res = {"single": crops[:, 0], "crops": crops, "labels": [{k: c[k] for k in SD.LABEL_KEYS} for c in cs],
               "failures": [], "render_s": 1.0, "embed_s": 1.0, "paused_s": 0.0}
        DD.write_shard(sdir, s["id"], res, {})


def test_train_end_to_end_keeps_every_result(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    pytest.importorskip("torch")
    import slot_dataset_render as SR_

    out = tmp_path / "amp"
    out.mkdir()
    plan = _plan(target=240, shard_size=60)
    (out / "plan.json").write_text(json.dumps(plan))
    _fake_shards(out, plan)
    monkeypatch.setattr(SR_, "MODELS_BASE", tmp_path / "models")
    a = Namespace(slot="amp", out=str(out), heads="logreg,mlp", seeds=1, epochs=2, tol=0.15, device="cpu",
                  models_dir="", no_ledger=True)
    assert SR_.cmd_train(a) == 0
    res = json.loads((out / "summary.json").read_text())
    assert res["results"]["main/logreg/multi/s0"]["test"]["model_top1"] > 0.6
    assert (out / "summary-n240.md").is_file()
    assert (tmp_path / "models/amp/n240/heads_main_mlp_multi.pkl").is_file()
    assert SR_.cmd_train(a) == 0                                    # a second train never overwrites the first
    assert len(list(out.glob("summary-n240*.md"))) == 2
    assert len(list((tmp_path / "models/amp").iterdir())) == 2


def test_plan_refuses_to_replace_a_plan_with_rendered_shards(tmp_path, monkeypatch):
    import slot_dataset_render as SR_
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps(_manifest()))
    monkeypatch.setattr(DD, "MANIFEST", man)
    monkeypatch.setattr(DD, "read_mono_file", _audio)
    monkeypatch.setattr(SR_, "DRIVE_PLAN", tmp_path / "missing.json")
    monkeypatch.setattr(DD, "v1_splits", lambda: {})
    monkeypatch.setattr(SD, "slot_pool", lambda slot, catalog=None, all_native=False: POOL)
    a = Namespace(slot="amp", out=str(tmp_path / "amp"), seed=1, target=30, limit=0, all_native=False,
                  fresh_splits=False, force=False)
    assert SR_.cmd_plan(a) == 0
    first = (tmp_path / "amp/plan.json").read_text()
    a.force = True
    assert SR_.cmd_plan(a) == 0                                     # nothing rendered yet: --force resamples
    (tmp_path / "amp/shards").mkdir()
    (tmp_path / "amp/shards/s00000.json").write_text("{}")
    a.seed = 2
    assert SR_.cmd_plan(a) == 2                                     # refused: shards exist
    assert json.loads((tmp_path / "amp/plan.json").read_text())["seed"] == 1
    assert first


def test_batch_script_parses_and_refuses_without_a_plan(tmp_path):
    sh = ROOT / "scripts/slot_batch.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    if (ROOT / "cache/slot-dataset-v1/eq/plan.json").exists():
        pytest.skip("a real eq plan exists")
    r = subprocess.run(["bash", str(sh), "eq", "0.01"], capture_output=True, text=True)
    assert r.returncode == 2 and "no plan" in r.stdout


# ---------------------------------------------------------------- T210: silence floor, out_db, refill

class _QuietEng(_Eng):
    """Renders at -100 dBFS for one model, digital zero for another."""
    def render(self, dry):
        if self.m == "HD2_PreampFoo":
            return dry * 0.0
        if self.m == "HD2_AmpFoo":
            return (dry / (np.sqrt(np.mean(dry.astype(np.float64) ** 2)) + 1e-12) * 1e-5).astype(np.float32)
        return dry * 2.0


def test_render_one_floor_is_drives_by_default_and_configurable():
    x = np.full(48000, 1e-5, np.float32)            # -100 dBFS
    class E(_Eng):
        def render(self, dry):
            return dry
    eng, blk = E(), _Block("M")
    with pytest.raises(RuntimeError, match="silence"):
        DD.render_one(eng, blk, {"cc": {}}, x)                      # drive's -90 dB floor, unchanged
    assert DD.SILENCE_DB == -90.0
    y = DD.render_one(eng, blk, {"cc": {}}, x, silence_db=-240.0)
    assert len(y) == len(x)
    with pytest.raises(RuntimeError, match="silence"):
        DD.render_one(eng, blk, {"cc": {}}, x * 0, silence_db=-240.0)   # digital zero still fails


def _render_plan(plan, out, keys, silence_db=DD.SILENCE_DB, eng=_QuietEng):
    return DD.work_loop(plan, out, open_engine=eng, blocks={m: _Block(m) for m in POOL}, emb=_Emb(),
                        read_excerpt=lambda f, s, n: _audio(f)[s:s + n], deadline=1e18, min_free_gb=1.0,
                        max_load=99.0, free=lambda p: 100.0, log=lambda m: None, loadavg=lambda: (1.0, 1.0, 1.0),
                        label_keys=keys, silence_db=silence_db)


def test_main_plan_labels_unchanged_and_refill_records_out_db(tmp_path):
    import slot_dataset_render as SR_
    main_out = tmp_path / "amp"
    plan = _plan(target=60, shard_size=20)
    _render_plan(plan, main_out, SD.LABEL_KEYS)
    labels, _, _ = DD.load_new(main_out)
    assert labels and all(set(r) == set(SD.LABEL_KEYS) for r in labels)      # no out_db on a main plan
    assert {r["model"] for r in labels} == {"HD2_AmpBar"}
    fails = SR_.failed_silence(main_out)
    assert {x["model"] for x in fails} == {"HD2_AmpFoo", "HD2_PreampFoo"}
    assert all(x["digital_zero"] is False for x in fails)            # the renderer's own check (RuntimeError)
    for x in fails:                                                  # mark one model as the engine's digital zero
        if x["model"] == "HD2_PreampFoo":
            x["digital_zero"] = True
    rplan, added = SR_.make_refill_plan(plan, fails, silence_db=-240.0)
    assert added == sum(x["model"] == "HD2_AmpFoo" for x in fails)
    assert all(c["model"] == "HD2_AmpFoo" for c in rplan["clips"])
    clips = {c["id"]: c for c in plan["clips"]}
    assert all(c == clips[c["id"]] for c in rplan["clips"])           # identical clip records
    assert rplan["silence_db"] == -240.0 and rplan["record_out_db"] and rplan["models"] == plan["models"]
    rout = tmp_path / "amp-refill"
    _render_plan(rplan, rout, SD.LABEL_KEYS + ("out_db",), silence_db=rplan["silence_db"])
    rl, _, _ = DD.load_new(rout)
    assert len(rl) == added and all(-101 < r["out_db"] < -99 for r in rl)
    # appending later failures keeps every existing refill shard unchanged
    c0 = next(c for c in plan["clips"] if c["model"] == "HD2_AmpBar")   # a clip not yet in the refill
    extra = [{"id": c0["id"], "model": c0["model"], "error": "x silence", "digital_zero": False}]
    rplan2, added2 = SR_.make_refill_plan(plan, fails + extra, silence_db=-240.0, old=rplan)
    assert rplan2["shards"][:len(rplan["shards"])] == rplan["shards"] and added2 == 1
    with pytest.raises(ValueError):
        SR_.make_refill_plan(plan, fails, silence_db=-120.0, old=rplan)


def test_train_with_refill_adds_clips_and_can_drop_quiet_ones(tmp_path):
    pytest.importorskip("sklearn")
    pytest.importorskip("torch")
    out, rout = tmp_path / "amp", tmp_path / "amp-refill"
    out.mkdir()
    plan = _plan(target=240, shard_size=60)
    main = {**plan, "clips": plan["clips"][:180], "shards": plan["shards"][:3], "n_clips": 180}
    (out / "plan.json").write_text(json.dumps(main))
    _fake_shards(out, main)
    rplan = {**plan, "clips": plan["clips"][180:], "shards": [{"id": "s00000", "clips": plan["shards"][3]["clips"]}],
             "n_clips": 60, "record_out_db": True, "silence_db": -240.0}
    rout.mkdir()
    (rout / "plan.json").write_text(json.dumps(rplan))
    _fake_shards(rout, rplan)
    sd = rout / "shards" / "s00000.labels.jsonl"                     # half the refill clips are very quiet
    rows = [json.loads(x) for x in sd.read_text().splitlines()]
    for i, r in enumerate(rows):
        r["out_db"] = -130.0 if i % 2 else -60.0
    sd.write_text("".join(json.dumps(r) + "\n" for r in rows))
    res = SD.run_train_slot(out, tmp_path / "m", heads=("logreg",), arms=("main",), ledger=False,
                            extra_outs=[rout], min_out_db=-100.0)
    assert res["n"] == 210 and res["n_refill"] == 30 and res["n_refill_dropped_quiet"] == 30
    with pytest.raises(AssertionError):                               # a refill may not repeat main clip ids
        SD.run_train_slot(rout, tmp_path / "m2", heads=("logreg",), arms=("main",), ledger=False, extra_outs=[rout])


def test_batch_script_knows_refill(tmp_path):
    txt = (ROOT / "scripts/slot_batch.sh").read_text()
    assert 'BASESLOT="${SLOT%-refill}"' in txt and "$REFILL" in txt
