"""Drive features, the calibration table lookup and the gain family's seed hook (T143). Synthetic data, no Native."""
from __future__ import annotations

import json

import numpy as np

from tonematch.search import drive_features as DF
from tonematch.search.drive_specialist import DriveTable, knob_seeds
from tonematch.search.gain_family import GainFamily


def test_features_see_gain_clipping_and_tone():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(48_000 * 3) * 0.1
    soft = DF.features(2.0 * x, x)
    hard = DF.features(np.tanh(12 * x), x)
    assert len(soft) == len(DF.NAMES)
    assert abs(soft[0] - 6.02) < 0.1                                    # gain_db of a x2
    assert hard[DF.NAMES.index("crest_db")] < soft[DF.NAMES.index("crest_db")] - 3   # clipping lowers the crest factor
    lp = np.convolve(x, np.ones(8) / 8, mode="same")                     # a dull tone: highs fall against lows
    t = DF.features(lp, x)
    assert t[DF.NAMES.index("tone8000")] < t[DF.NAMES.index("tone125")] - 3


def test_align_removes_a_small_lag():
    x = np.random.default_rng(1).standard_normal(48_000)
    a, b = DF.align(np.roll(x, 5), x)
    assert np.allclose(a[: len(b)], b[: len(a)], atol=1e-9) or np.corrcoef(a[:1000], b[:1000])[0, 1] > 0.999


def _table(tmp_path, n=12):
    rng = np.random.default_rng(2)
    models = {}
    for k, name in enumerate(("HD2_DistA", "HD2_DistB")):
        rows = []
        for j in range(n):
            drive = float(rng.uniform(0.05, 0.95))
            v = np.zeros(len(DF.NAMES))
            v[0] = 20 * drive + 40 * k            # model B sits far from model A in gain_db
            v[-4] = -10 * drive
            rows.append({"setting": j, "di": 0, "values": {"Drive": drive}, "summary": v.tolist()})
        models[name] = {"free": [["Drive", 0.0, 1.0, 0.5]], "rows": rows}
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"summary_keys": list(DF.NAMES), "models": models}))
    return p


def test_table_lookup_ranks_the_nearest_model_and_estimates_the_knob(tmp_path):
    tab = DriveTable.load(_table(tmp_path))
    v = np.zeros(len(DF.NAMES))
    v[0], v[-4] = 20 * 0.7, -7.0                                          # model A at Drive 0.7
    res = tab.lookup(v)
    assert min(res, key=lambda m: res[m]["fit"]) == "HD2_DistA"
    mu, sd = res["HD2_DistA"]["knobs"]["Drive"]
    assert abs(mu - 0.7) < 0.12 and sd < 0.15
    seeds = knob_seeds(res["HD2_DistA"], {"Drive": (0.0, 1.0)})
    assert abs(seeds["Drive"][0] - 0.7) < 0.12 and 0.10 <= seeds["Drive"][1] <= 0.30
    assert knob_seeds({"knobs": {"Drive": (0.5, 0.4)}}, {"Drive": (0.0, 1.0)}) == {}      # neighbours disagree: left free


def test_gain_family_seeds_only_drive_models_and_shortlist_is_opt_in(tmp_path):
    tab = DriveTable.load(_table(tmp_path))
    x = np.random.default_rng(3).standard_normal(48_000 * 2) * 0.1
    g = GainFamily(probs_fn=lambda t, d: {"drive": 0.5, "amp": 0.4}, table=tab)
    fs = g.seed(np.tanh(6 * x), x)
    assert fs.for_model("HD2_DistA", "amp", {"Drive": (0.0, 1.0)}) == {}
    assert set(fs.for_model("HD2_DistA", "drive", {"Drive": (0.0, 1.0)})) <= {"Drive"}
    assert g.shortlist(x, x, ["HD2_DistA", "HD2_DistB"]) == []                              # off unless asked
    g.shortlist_on = True
    assert sorted(g.shortlist(np.tanh(6 * x), x, ["HD2_DistA", "HD2_DistB"])) == ["HD2_DistA", "HD2_DistB"]
    assert GainFamily(probs_fn=lambda t, d: {"drive": 0.5}).shortlist(x, x, ["HD2_DistA"]) == []   # no table, no list
