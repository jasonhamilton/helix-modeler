"""T221 DI-invariance check: statistics of scripts/di_invariance_check.py on synthetic distances.
No cached renders, no engine, no embedding model."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import di_invariance_check as D


def _group(d_own, d_other, cond="other_player", types=None, gid="g", model="M"):
    """One target with candidates (truth first) on its own DI and on one other DI; every score gets
    the same distances."""
    n = len(d_own)
    types = types or ["true"] + ["knob_small"] * ((n - 1) // 2) + ["same_class"] * (n - 1 - (n - 1) // 2)
    mk = lambda v: {s: [None if x is None else float(x) for x in v] for s in D.SCORES}  # noqa: E731
    return {"gid": gid, "source": "V1", "family": "drive", "model": model, "target_di": "A", "types": types,
            "dis": {"A": {"cond": "own", "d": mk(d_own)}, "B": {"cond": cond, "d": mk(d_other)},
                    "C": {"cond": cond, "d": mk(d_other)}}}


def test_ratio_and_order_when_di_change_is_small():
    # own DI: truth 0, distractors 1..4; other DI: truth 0.2, distractors keep their order
    g = _group([0, 1, 2, 3, 4], [0.2, 1.1, 2.1, 3.1, 4.1])
    recs, between = D.records(g)
    r = [x for x in recs if x["score"] == "ltas_tilt" and x["di"] == "B"][0]
    assert r["r"] == pytest.approx(0.2 / 2.5)          # median of distractor distances 1..4 is 2.5
    assert r["r_knob"] == pytest.approx(0.2 / 1.5)     # knob distractors are candidates 1 and 2
    assert r["r_model"] == pytest.approx(0.2 / 3.5)
    assert r["rank1"] is True
    assert r["rho_own"] == pytest.approx(1.0)
    b = [x for x in between if x["score"] == "ltas_tilt"]
    assert len(b) == 1 and b[0]["rho"] == pytest.approx(1.0) and b[0]["cond"] == "other_player"


def test_ratio_above_one_and_order_lost_when_di_dominates():
    g = _group([0, 1, 2, 3, 4], [5.0, 4.0, 1.0, 3.0, 2.0])
    recs, _ = D.records(g)
    r = [x for x in recs if x["score"] == "envelope" and x["di"] == "B"][0]
    assert r["r"] == pytest.approx(2.0)
    assert r["rank1"] is False
    assert r["rho_own"] < 0.5


def test_missing_distances_are_skipped():
    g = _group([0, 1, 2, 3, 4], [None, 1, 2, 3, 4])
    recs, _ = D.records(g)
    assert recs == []
    g = _group([0, 1, None, 3, 4], [0.5, 1, 2, None, 4])
    recs, _ = D.records(g)
    r = recs[0]
    assert r["n_cand"] == 4 and np.isfinite(r["r"])


def test_summary_cells_and_tables():
    dist = [_group([0, 1, 2, 3, 4], [0.2, 1, 2, 3, 4], gid=f"g{i}", model=f"M{i % 3}") for i in range(6)]
    dist += [_group([0, 1, 2, 3, 4], [3, 1, 2, 3, 4], cond="same_player", gid=f"s{i}", model=f"M{i % 2}")
             for i in range(4)]
    summ = D.summarise(dist)
    c = summ["cells"]["V1|other_player|embed_afxrep"]
    assert c["n"] == 12 and c["n_models"] == 3            # 6 targets x 2 other DIs
    assert c["R"] == pytest.approx(0.2 / 2.5) and c["rank1"] == 1.0 and c["rho_own"] == pytest.approx(1.0)
    assert c["ci"]["R"][0] <= c["R"] <= c["ci"]["R"][1]
    s = summ["cells"]["V1|same_player|embed_afxrep"]
    assert s["R"] == pytest.approx(3 / 2.5) and s["rank1"] == 0.0
    assert "ALL|other_player|mrstft_v2" in summ["cells"]
    md = D.tables_md(summ)
    assert "#### V1 / other_player" in md and "#### ALL / same_player" in md
    assert "drive" in md


def test_v1_label_types():
    assert D.v1_label_type("true", None, None) == "true"
    assert D.v1_label_type("near:Tone", {"Gain": 33, "Tone": 87}, {"Gain": 33, "Tone": 68}) == "knob_small"
    assert D.v1_label_type("near:Gain", {"Gain": 127, "Tone": 68}, {"Gain": 33, "Tone": 68}) == "knob_large"
    assert D.v1_label_type("nbr:X", None, None) == "same_class"
    assert D.v1_label_type("random:Y", None, None) == "diff_class"
    assert D.cond_of("P1", "P1") == "same_player" and D.cond_of("P1", "P2") == "other_player"
